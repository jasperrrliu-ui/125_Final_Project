"""Trusted gate between a chosen option and the simulation ledger.

The graph offers options computed by the policy engine and records which one
the customer accepted. Before anything is written, this gate re-reads the
customer and order from the business database (read-only), rebuilds the
facts, and runs the same policy engine again. The chosen option must still be
allowed, with an identical action and payload, and the customer must have
confirmed it. No real order, payment, or coupon service is modified.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import sqlite3
from typing import Any

try:  # Package import when LangGraph calls this backend component.
    from .action_ledger import ActionLedger, ActionProposal, Authorization
except ImportError:  # Direct local test execution from this folder.
    from action_ledger import ActionLedger, ActionProposal, Authorization

from agent.policy.engine import DEFAULT_POLICY_FILE, Decision, Policy, evaluate, resolve_facts
from agent.policy.records import coupon_record, customer_record, order_address, order_record
from agent.retail_tools.read_tools import lookup_order


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUSINESS_DB = ROOT / "processed_data" / "customer_simulation" / "synthetic_retail.sqlite"


@dataclass(frozen=True)
class AuthorizationRequest:
    """What the graph asks the gate to authorize.

    ``customer_id`` comes from identity verification, ``coupon_result`` from
    the backend coupon tool, and ``customer_facts`` are statements the
    customer made (they stay labeled source=customer in the engine).
    ``option`` is the option the graph offered and the customer chose.
    """

    conversation_id: str
    request_key: str
    intent: str
    customer_id: str
    order_id: str | None
    item_index: int | None
    option: dict[str, Any]
    confirmed: bool
    customer_facts: dict[str, Any] = field(default_factory=dict)
    coupon_result: dict[str, Any] | None = None


class PolicyGate:
    def __init__(self, ledger: ActionLedger,
                 business_db: Path = DEFAULT_BUSINESS_DB,
                 policy: Policy | None = None,
                 policy_file: Path = DEFAULT_POLICY_FILE):
        self.ledger = ledger
        self.business_db = Path(business_db)
        self.policy = policy or Policy.load(policy_file)
        digest = hashlib.sha256(repr(self.policy.raw).encode("utf-8")).hexdigest()[:12]
        self.version = f"{self.policy.policy_id}@{self.policy.version}+{digest}"

    # ------------------------------------------------------------ re-reads

    def _member_level(self, customer_id: str) -> str | None:
        with closing(sqlite3.connect(
            f"file:{self.business_db.resolve().as_posix()}?mode=ro", uri=True
        )) as db:
            row = db.execute("SELECT member_level FROM customers WHERE customer_id=?",
                             (customer_id,)).fetchone()
        return row[0] if row else None

    def _deny(self, req: AuthorizationRequest, reason: str,
              decision: Decision | None = None) -> tuple[Authorization, Decision | None]:
        return Authorization(
            allowed_action=None, verified_customer_id=None, verified_order_id=None,
            policy_rule_id=None, policy_version=self.version, approved_payload=None,
            denial_reason=reason,
        ), decision

    # -------------------------------------------------------------- review

    def review(self, req: AuthorizationRequest) -> tuple[Authorization, Decision | None]:
        """Recompute the decision from re-read records; never trust stored facts."""
        if req.intent not in self.policy.intents:
            return self._deny(req, "outside_policy_scope")
        if not req.confirmed:
            return self._deny(req, "customer_confirmation_missing")
        if not req.customer_id:
            return self._deny(req, "customer_not_verified")
        member_level = self._member_level(req.customer_id)
        if member_level is None:
            return self._deny(req, "customer_not_verified")

        targets = self.policy.targets(req.intent)
        records: dict[str, Any] = {"customer": customer_record(member_level)}
        context: dict[str, Any] = {}
        if "order_id" in targets:
            if not req.order_id:
                return self._deny(req, "order_not_verified")
            order = lookup_order(req.customer_id, req.order_id, db_path=self.business_db)
            if order["status"] != "found":
                return self._deny(req, "order_not_verified")
            records["order"] = order_record(order["facts"])
            context["address"] = order_address(order["facts"])
            if "item" in targets:
                item = next((x for x in order["facts"]["items"]
                             if x["item_index"] == req.item_index), None)
                if item is None:
                    return self._deny(req, "item_not_verified")
                context["item"] = item
        if "coupon_code" in targets:
            # The coupon verdict was written to state by the backend tool,
            # not by the model; it is the only non-re-read system record.
            records["coupon_check"] = coupon_record(req.coupon_result)

        facts = resolve_facts(self.policy, records, req.customer_facts)
        decision = evaluate(self.policy, req.intent, facts, context)
        if decision.kind != "options":
            return self._deny(req, f"policy_decision_{decision.kind}", decision)
        chosen = next((o for o in decision.options
                       if o.option_id == req.option.get("option_id")), None)
        if chosen is None:
            return self._deny(req, "option_not_allowed", decision)
        return Authorization(
            allowed_action=chosen.action,
            verified_customer_id=req.customer_id,
            verified_order_id=req.order_id if "order_id" in targets else None,
            policy_rule_id=chosen.rule_id,
            policy_version=self.version,
            approved_payload=chosen.payload,
        ), decision

    def authorize(self, req: AuthorizationRequest) -> dict[str, Any]:
        """Review, then record a simulated or blocked ledger event."""
        authorization, _ = self.review(req)
        proposal = ActionProposal(
            conversation_id=req.conversation_id,
            request_id=req.request_key,
            intent=req.intent,
            action_type=req.option.get("action"),
            customer_id=req.customer_id,
            order_id=req.order_id if "order_id" in self.policy.targets(req.intent) else None,
            payload=req.option.get("payload") or {},
        )
        return self.ledger.record_action(proposal, authorization)

    def record_outcome(self, **kwargs: Any) -> dict[str, Any]:
        """Record a decline, a customer refusal, or a handoff."""
        kwargs.setdefault("policy_version", self.version)
        return self.ledger.record_outcome(**kwargs)
