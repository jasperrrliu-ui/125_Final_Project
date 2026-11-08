"""Extract a reviewable policy draft from ABCD's original guidelines.

Run from any directory:
  python build_policy_rules.py <guidelines.json> <policy_rules.json>

The decision conditions below are a structured transcription. Each rule carries
the exact source sentence(s) used for it. Unspecified behavior stays unresolved.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


def build(source_path: Path) -> dict:
    source_bytes = source_path.read_bytes()
    guidelines = json.loads(source_bytes)

    def action(flow: str, subflow: str, button: str) -> tuple[int, dict]:
        actions = guidelines[flow]["subflows"][subflow]["actions"]
        matches = [(i, entry) for i, entry in enumerate(actions) if entry["button"] == button]
        if len(matches) != 1:
            raise ValueError(f"Expected one {flow}/{subflow}/{button} action")
        return matches[0]

    def evidence(flow: str, subflow: str, button: str, *subtext_indices: int) -> list[dict]:
        i, entry = action(flow, subflow, button)
        lines = [(f"actions/{i}/text", entry["text"])]
        lines.extend((f"actions/{i}/subtext/{j}", entry["subtext"][j]) for j in subtext_indices)
        return [
            {"source_path": f"{flow}/subflows/{subflow}/{path}", "source_text": line}
            for path, line in lines
        ]

    # Collapse only subflows whose complete action content is identical.
    for flow, names in [
        ("Product Defect", ["Return Due to Size", "Return Due to Color", "Return Due to Stain"]),
        ("Purchase Dispute", ["Promo Code Invalid", "Promo Code Out of Date"]),
    ]:
        first = guidelines[flow]["subflows"][names[0]]["actions"]
        if any(guidelines[flow]["subflows"][name]["actions"] != first for name in names[1:]):
            raise ValueError(f"Shared policy text differs in {flow}")

    rules: list[dict] = []

    def add(rule_id: str, applies_to: list[str], when: dict, decision: str,
            citations: list[dict], details: dict | None = None) -> None:
        rules.append({
            "id": rule_id,
            "applies_to": applies_to,
            "when": when,
            "decision": decision,
            **({"details": details} if details else {}),
            "origin": "abcd_guidelines",
            "evidence": citations,
        })

    returns = ["return_size", "return_color", "return_stain"]
    ret = lambda *indices: evidence("Product Defect", "Return Due to Size", "Membership Privileges", *indices)
    add("return_gold", returns, {"member_level": "gold"}, "return_allowed",
        ret(0), {"eligibility": "unlimited_returns"})
    add("return_silver", returns,
        {"all": [{"member_level": "silver"}, {"any": [
            {"purchase_within": "last_6_months"}, {"has_receipt": True}, {"original_packaging": True}
        ]}]}, "return_allowed", ret(1))
    add("return_bronze", returns,
        {"all": [{"member_level": "bronze"}, {"any": [
            {"purchase_within": "last_90_days"}, {"has_receipt": True}, {"original_packaging": True}
        ]}]}, "return_allowed", ret(2))
    add("return_guest", returns,
        {"all": [{"member_level": "guest"}, {"any": [
            {"purchase_within": "last_30_days"}, {"has_receipt": True}
        ]}]}, "return_allowed", ret(3))
    add("return_allowed_next_steps", returns, {"return_allowed": True},
        "collect_full_address_and_return_method",
        evidence("Product Defect", "Return Due to Size", "End Conversation", 0)
        + evidence("Product Defect", "Return Due to Size", "Enter Details", 0, 1, 2)
        + evidence("Product Defect", "Return Due to Size", "Update Order", 0, 1),
        {"return_method_options": ["By Mail", "In Store", "Drop off Center"]})
    add("return_not_allowed", returns, {"return_allowed": False},
        "explain_and_end", evidence("Product Defect", "Return Due to Size", "End Conversation", 1))

    promos = ["promo_code_invalid", "promo_code_out_of_date"]
    promo = lambda button, *indices: evidence("Purchase Dispute", "Promo Code Invalid", button, *indices)
    add("promo_more_than_seven_days", promos, {"promo_code_age_days": {"gt": 7}},
        "classify_out_of_date", promo("N/A", 0))
    add("promo_not_more_than_seven_days", promos, {"promo_code_age_days": {"lte": 7}},
        "classify_invalid", promo("N/A", 0))
    add("promo_oracle_customer_right", promos, {"oracle_customer_right": True},
        "generate_promo_code", promo("Ask the Oracle", 1, 2, 3)
        + promo("Promo Code", 0, 1))
    add("promo_oracle_customer_wrong_member", promos,
        {"all": [{"oracle_customer_right": False}, {"member_level": {"in": ["bronze", "silver", "gold"]}}]},
        "generate_promo_code", promo("Membership Privileges", 0, 1, 2)
        + promo("Promo Code", 0, 1))
    add("promo_oracle_customer_wrong_guest", promos,
        {"all": [{"oracle_customer_right": False}, {"member_level": "guest"}]},
        "apologize_no_promo_code", promo("Membership Privileges", 0, 1, 3))
    add("promo_if_customer_asks_value", promos, {"customer_asks_promo_value": True},
        "say_20_percent_off_next_purchase", promo("Promo Code", 2))

    cancel = ["manage_cancel"]
    can = lambda button, *indices: evidence("Order Issue", "Manage Cancel", button, *indices)
    add("cancel_order_received", cancel, {"shipping_status": "Order Received"},
        "remove_unwanted_item_and_refund_now", can("Shipping Status", 0, 1)
        + can("Offer Refund", 0, 1))
    add("cancel_shipped_member", cancel,
        {"all": [{"shipping_status": {"in": ["In Transit", "Out for Delivery", "Delivered"]}},
                 {"member_level": {"in": ["bronze", "silver", "gold"]}}]},
        "refund_now_customer_calls_back_for_return_label_after_arrival",
        can("Shipping Status", 2) + can("Membership Privileges", 0)
        + can("Offer Refund", 0, 1))
    add("cancel_shipped_guest", cancel,
        {"all": [{"shipping_status": {"in": ["In Transit", "Out for Delivery", "Delivered"]}},
                 {"member_level": "guest"}]},
        "wait_until_arrival_then_call_to_start_return",
        can("Shipping Status", 2) + can("Membership Privileges", 1))

    upgrade = ["manage_upgrade"]
    up = lambda button, *indices: evidence("Order Issue", "Manage Upgrade", button, *indices)
    add("upgrade_already_shipped", upgrade,
        {"shipping_status": {"in": ["In Transit", "Out for Delivery", "Delivered"]}},
        "cannot_change_shipping_date_explain_and_end", up("Shipping Status", 0, 2))
    add("upgrade_gold_order_received", upgrade,
        {"all": [{"shipping_status": "Order Received"}, {"member_level": "gold"}]},
        "waive_fee_and_update_delivery_to_tomorrow",
        up("Shipping Status", 1) + up("Membership Privileges", 0, 1)
        + up("Update Order", 0))
    add("upgrade_non_gold_order_received", upgrade,
        {"all": [{"shipping_status": "Order Received"},
                 {"member_level": {"in": ["guest", "bronze", "silver"]}}]},
        "ask_permission_to_charge_20_usd",
        up("Shipping Status", 1) + up("Membership Privileges", 0, 2))
    add("upgrade_non_gold_after_permission", upgrade,
        {"all": [{"shipping_status": "Order Received"},
                 {"member_level": {"in": ["guest", "bronze", "silver"]}},
                 {"customer_authorizes_20_usd_charge": True}]},
        "update_delivery_to_tomorrow",
        up("Membership Privileges", 2) + up("Update Order", 0))

    downgrade = ["manage_downgrade"]
    down = lambda button, *indices: evidence("Order Issue", "Manage Downgrade", button, *indices)
    add("downgrade_order_received", downgrade, {"shipping_status": "Order Received"},
        "update_delivery_to_next_week", down("Shipping Status", 0, 1)
        + down("Membership Privileges", 0, 1, 2) + down("Update Order", 0))
    add("downgrade_shipped_member", downgrade,
        {"all": [{"shipping_status": {"in": ["In Transit", "Out for Delivery", "Delivered"]}},
                 {"member_level": {"in": ["bronze", "silver", "gold"]}}]},
        "add_account_credit_for_shipping_fee_and_end",
        down("Shipping Status", 2) + down("Membership Privileges", 0, 1))
    add("downgrade_shipped_guest", downgrade,
        {"all": [{"shipping_status": {"in": ["In Transit", "Out for Delivery", "Delivered"]}},
                 {"member_level": "guest"}]},
        "apologize_no_change_and_end", down("Shipping Status", 2)
        + down("Membership Privileges", 2))

    return {
        "schema_version": "0.1",
        "status": "draft_for_user_review",
        "source": {"file": "raw_data/guidelines.json",
                   "sha256": hashlib.sha256(source_bytes).hexdigest()},
        "scope_intents": returns + promos + cancel + upgrade + downgrade,
        "source_equivalence_checks": [
            {"subflows": ["Return Due to Size", "Return Due to Color", "Return Due to Stain"],
             "same_actions": True},
            {"subflows": ["Promo Code Invalid", "Promo Code Out of Date"],
             "same_actions": True},
        ],
        "rules": rules,
        "unresolved_source_issues": [
            {"id": "date_boundaries", "note": "The source says 'last 6 months', 'last 90 days', and 'last 30 days' without specifying boundary inclusivity or evaluation clock."},
            {"id": "oracle_yes_no_wording", "note": "The promo instructions ask whether the customer made an error, then say 'customer is right (Oracle says Yes)'. The direction of the Boolean is inconsistent."},
            {"id": "cancel_bronze_input", "note": "The cancel branch includes bronze members, then says to enter 'Gold or Silver, depending on the level'. This does not describe what to enter for bronze."},
            {"id": "upgrade_payment_refusal", "note": "The source asks permission to charge $20 and says the answer 'should be yes'; it does not specify a refusal branch."},
            {"id": "promo_issuance_authority", "note": "ABCD instructs the representative to generate a promo code. It does not model a merchant-owned coupon service or identify its issuer."},
        ],
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python build_policy_rules.py <guidelines.json> <policy_rules.json>")
    output = Path(sys.argv[2])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build(Path(sys.argv[1])), ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(output)
