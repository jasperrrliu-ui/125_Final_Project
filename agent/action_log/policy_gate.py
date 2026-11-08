"""Trusted gate between an agent's proposed action and the simulation ledger.

The agent must not call ActionLedger with an Authorization it made up. Backend
code calls this gate, which re-reads the business database and policy file.
No real order, payment, or coupon service is modified.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

try:  # Package import when LangGraph calls this backend component.
    from .action_ledger import ActionLedger, ActionProposal, Authorization
except ImportError:  # Direct local test execution from this folder.
    from action_ledger import ActionLedger, ActionProposal, Authorization


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUSINESS_DB = ROOT / "processed_data" / "customer_simulation" / "synthetic_retail.sqlite"
DEFAULT_POLICY_FILE = ROOT / "processed_data" / "policy" / "policy_rules.json"
SHIPPED = {"in transit", "out for delivery", "delivered"}
MEMBERS = {"bronze", "silver", "gold"}
RETURNS = {"return_size", "return_color", "return_stain"}
PROMOS = {"promo_code_invalid", "promo_code_out_of_date"}


def _rule_matches(condition: dict, facts: dict) -> bool:
    """Evaluate only the small condition vocabulary used by approved actions."""
    if "all" in condition:
        return all(_rule_matches(part, facts) for part in condition["all"])
    if "any" in condition:
        return any(_rule_matches(part, facts) for part in condition["any"])
    for key, expected in condition.items():
        actual = facts.get(key)
        if actual is None:
            return False
        if isinstance(expected, dict):
            if "in" not in expected:
                return False
            choices = expected["in"]
            if isinstance(actual, str):
                if actual.casefold() not in {str(choice).casefold() for choice in choices}:
                    return False
            elif actual not in choices:
                return False
        elif isinstance(expected, str) and isinstance(actual, str):
            if actual.casefold() != expected.casefold():
                return False
        elif actual != expected:
            return False
    return True


@dataclass(frozen=True)
class TrustedContext:
    """Backend state, never arguments supplied by the LLM or browser directly."""

    customer_id: str
    return_address: dict[str, str] | None = None
    return_address_confirmed: bool = False
    return_method_confirmed: str | None = None
    authorized_20_usd_charge: bool = False


class PolicyGate:
    def __init__(self, ledger: ActionLedger,
                 business_db: Path = DEFAULT_BUSINESS_DB,
                 policy_file: Path = DEFAULT_POLICY_FILE):
        self.ledger = ledger
        self.business_db = Path(business_db)
        policy_bytes = Path(policy_file).read_bytes()
        policy = json.loads(policy_bytes)
        self.rules = {rule["id"]: rule for rule in policy["rules"]}
        self.scope = set(policy["scope_intents"])
        self.version = "draft-" + hashlib.sha256(policy_bytes).hexdigest()[:12]

    def _authorization(self, proposal: ActionProposal, *,
                       action: str | None = None, payload: dict | None = None,
                       rule_id: str | None = None,
                       reason: str | None = None,
                       facts: dict | None = None) -> Authorization:
        if action is not None:
            rule = self.rules.get(rule_id)
            if not rule or proposal.intent not in rule["applies_to"]:
                raise ValueError("approved action has no source policy rule for this intent")
            if facts is None or not _rule_matches(rule["when"], facts):
                raise ValueError("approved action does not match the source policy condition")
        return Authorization(
            allowed_action=action,
            verified_customer_id=proposal.customer_id if action else None,
            verified_order_id=proposal.order_id if action else None,
            policy_rule_id=rule_id,
            policy_version=self.version,
            approved_payload=payload,
            denial_reason=reason,
        )

    def review(self, proposal: ActionProposal, context: TrustedContext) -> Authorization:
        """Recheck ownership and policy; never trust tier, status or price from LLM."""
        if proposal.intent not in self.scope:
            return self._authorization(proposal, reason="outside_eight_intents")
        if not context.customer_id or proposal.customer_id != context.customer_id:
            return self._authorization(proposal, reason="customer_not_verified")

        with closing(sqlite3.connect(
            f"file:{self.business_db.resolve().as_posix()}?mode=ro", uri=True
        )) as db:
            db.row_factory = sqlite3.Row
            customer = db.execute(
                "SELECT member_level FROM customers WHERE customer_id=?",
                (context.customer_id,),
            ).fetchone()
            order = db.execute(
                "SELECT shipping_status, packaging FROM orders "
                "WHERE customer_id=? AND order_id=?",
                (context.customer_id, proposal.order_id),
            ).fetchone()
            item = None
            item_index = proposal.payload.get("item_index") if isinstance(proposal.payload, dict) else None
            if order is not None and type(item_index) is int:
                item = db.execute(
                    "SELECT amount FROM items WHERE order_id=? AND item_index=?",
                    (proposal.order_id, item_index),
                ).fetchone()

        if customer is None:
            return self._authorization(proposal, reason="customer_or_order_not_verified")
        tier = customer["member_level"].casefold() if customer["member_level"] else None
        if tier not in MEMBERS | {"guest"}:
            return self._authorization(proposal, reason="unknown_membership_level")

        if proposal.intent in PROMOS:
            # No platform-owned replacement-code issuer has been approved yet.
            return self._authorization(proposal, reason="coupon_issuance_not_implemented")

        if order is None:
            return self._authorization(proposal, reason="customer_or_order_not_verified")
        status = order["shipping_status"].casefold() if order["shipping_status"] else None
        facts = {
            "member_level": tier,
            "shipping_status": status,
            "original_packaging": str(order["packaging"]).casefold() == "yes",
            "customer_authorizes_20_usd_charge": context.authorized_20_usd_charge,
        }

        if proposal.intent in RETURNS:
            if item is None:
                return self._authorization(proposal, reason="item_not_verified")
            packaging_yes = str(order["packaging"]).casefold() == "yes"
            if tier == "gold":
                rule_id = "return_gold"
            elif tier in {"silver", "bronze"} and packaging_yes:
                rule_id = "return_silver" if tier == "silver" else "return_bronze"
            else:
                # Receipt is not stored, and the source leaves date boundaries
                # unspecified. Do not turn missing evidence into permission.
                return self._authorization(proposal, reason="return_eligibility_unresolved")
            address = context.return_address
            address_complete = (
                isinstance(address, dict)
                and all(isinstance(address.get(part), str) and address[part].strip()
                        for part in ("street", "city", "state", "zip_code"))
            )
            if (not context.return_address_confirmed
                    or not address_complete
                    or not context.return_method_confirmed):
                return self._authorization(proposal, reason="return_details_not_confirmed")
            return self._authorization(
                proposal, action="initiate_return", rule_id=rule_id,
                facts=facts,
                payload={"item_index": item_index,
                         "return_method": context.return_method_confirmed,
                         "address_confirmed": True},
            )

        if status is None:
            return self._authorization(proposal, reason="shipping_status_missing")
        if status not in SHIPPED | {"order received"}:
            return self._authorization(proposal, reason="shipping_status_unknown")

        if proposal.intent == "manage_cancel":
            if item is None or item["amount"] is None:
                return self._authorization(proposal, reason="item_not_verified")
            amount = int(Decimal(str(item["amount"])) * 100)
            if amount <= 0:
                return self._authorization(proposal, reason="item_amount_unavailable")
            payload = {"item_index": item_index, "amount_cents": amount}
            if status == "order received":
                return self._authorization(
                    proposal, action="cancel_item_and_refund", payload=payload,
                    rule_id="cancel_order_received", facts=facts,
                )
            if tier in {"silver", "gold"}:
                return self._authorization(
                    proposal, action="refund_item", payload=payload,
                    rule_id="cancel_shipped_member", facts=facts,
                )
            if tier == "bronze":
                return self._authorization(proposal, reason="cancel_bronze_source_ambiguity")
            return self._authorization(proposal, reason="shipped_guest_wait_for_arrival")

        if proposal.intent == "manage_upgrade":
            if status in SHIPPED:
                return self._authorization(proposal, reason="already_shipped_no_upgrade")
            if tier == "gold":
                return self._authorization(
                    proposal, action="reschedule_delivery",
                    payload={"delivery_window": "tomorrow"},
                    rule_id="upgrade_gold_order_received", facts=facts,
                )
            if not context.authorized_20_usd_charge:
                return self._authorization(proposal, reason="customer_charge_consent_missing")
            return self._authorization(
                proposal, action="reschedule_delivery",
                payload={"delivery_window": "tomorrow", "charge_amount_cents": 2000},
                rule_id="upgrade_non_gold_after_permission", facts=facts,
            )

        if proposal.intent == "manage_downgrade":
            if status == "order received":
                return self._authorization(
                    proposal, action="reschedule_delivery",
                    payload={"delivery_window": "next_week"},
                    rule_id="downgrade_order_received", facts=facts,
                )
            if tier in MEMBERS:
                # The DB has no verified shipping-fee amount to credit.
                return self._authorization(proposal, reason="shipping_fee_unavailable")
            return self._authorization(proposal, reason="shipped_guest_no_change")

        return self._authorization(proposal, reason="unsupported_intent")

    def review_and_record(self, proposal: ActionProposal,
                          context: TrustedContext) -> dict:
        """Only this backend path should be wired to the agent workflow."""
        authorization = self.review(proposal, context)
        return self.ledger.record_action(proposal, authorization)
