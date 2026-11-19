"""Policy engine: fact resolution, three-valued rules, options, declines."""

import unittest

from agent.policy.engine import Fact, FactRequest, Policy, evaluate, resolve_facts


ITEM = {"item_index": 0, "name": "guess_jacket", "amount_cents": 7400}


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = Policy.load()

    def decide(self, intent, member, order=None, customer=None, coupon=None):
        records = {"customer": {"member_level": member}}
        if order is not None:
            records["order"] = order
        if coupon is not None:
            records["coupon_check"] = coupon
        facts = resolve_facts(self.policy, records, customer or {})
        return evaluate(self.policy, intent, facts, {"item": ITEM, "address": "1 Main St"})

    def test_fact_falls_back_to_customer_when_system_has_no_value(self):
        facts = resolve_facts(self.policy, {"customer": {"member_level": "silver"},
                                            "order": {"original_packaging": None}}, {})
        self.assertIsInstance(facts["original_packaging"], FactRequest)
        self.assertEqual(facts["original_packaging"].source, "customer")
        facts = resolve_facts(self.policy, {"customer": {}, "order": {"original_packaging": False}},
                              {"original_packaging": True})
        self.assertEqual(facts["original_packaging"], Fact("original_packaging", False, "system"))

    def test_system_fact_requested_before_tool_runs(self):
        decision = self.decide("manage_cancel", "gold")
        self.assertEqual(decision.kind, "need")
        self.assertEqual(decision.need[0].tool, "order")

    def test_gold_return_offers_three_methods(self):
        decision = self.decide("return_size", "gold", order={"purchase_date": "2018-01-01"})
        self.assertEqual(decision.kind, "options")
        self.assertEqual([o.option_id for o in decision.options],
                         ["return_by_mail", "return_in_store", "return_drop_off"])
        self.assertEqual(decision.options[0].payload,
                         {"item_index": 0, "return_method": "by_mail", "address_confirmed": True})

    def test_silver_asks_only_for_receipt_when_system_facts_fail(self):
        order = {"purchase_date": "2019-08-22", "original_packaging": False}
        decision = self.decide("return_color", "silver", order=order)
        self.assertEqual(decision.kind, "need")
        self.assertEqual([n.name for n in decision.need], ["has_receipt"])
        no = self.decide("return_color", "silver", order=order, customer={"has_receipt": False})
        self.assertEqual((no.kind, no.rule_id), ("decline", "return_not_eligible"))
        yes = self.decide("return_color", "silver", order=order, customer={"has_receipt": True})
        self.assertEqual((yes.kind, yes.rule_id), ("options", "return_silver"))

    def test_date_window_uses_fixed_clock(self):
        recent = {"purchase_date": "2020-03-10", "original_packaging": False}
        self.assertEqual(self.decide("return_size", "guest", order=recent).kind, "options")
        old = {"purchase_date": "2020-02-15", "original_packaging": False}
        self.assertEqual(self.decide("return_size", "guest", order=old).kind, "need")

    def test_cancel_amount_comes_from_record(self):
        decision = self.decide("manage_cancel", "guest", order={"shipping_status": "Order Received"})
        self.assertEqual(decision.options[0].payload, {"item_index": 0, "amount_cents": 7400})
        self.assertIn("$74.00", decision.options[0].summary)

    def test_shipped_guest_cancel_is_declined_not_handed_off(self):
        decision = self.decide("manage_cancel", "guest", order={"shipping_status": "In Transit"})
        self.assertEqual((decision.kind, decision.reason_code), ("decline", "guest_wait_for_arrival"))

    def test_missing_shipping_status_hands_off(self):
        decision = self.decide("manage_upgrade", "gold", order={"shipping_status": None})
        self.assertEqual(decision.kind, "handoff")
        self.assertIn("shipping_status", decision.reason_code)

    def test_non_gold_upgrade_carries_fee(self):
        decision = self.decide("manage_upgrade", "silver", order={"shipping_status": "order received"})
        self.assertEqual(decision.options[0].payload,
                         {"delivery_window": "tomorrow", "charge_amount_cents": 2000})

    def test_promo_oracle_unavailable_skips_for_member_hands_off_for_guest(self):
        none = {"oracle_customer_right": None}
        member = self.decide("promo_code_invalid", "bronze", coupon=none)
        self.assertEqual((member.kind, member.rule_id), ("options", "promo_member_goodwill"))
        guest = self.decide("promo_code_invalid", "guest", coupon=none)
        self.assertEqual(guest.kind, "handoff")

    def test_promo_guest_outcomes_follow_oracle(self):
        right = self.decide("promo_code_out_of_date", "guest", coupon={"oracle_customer_right": True})
        self.assertEqual(right.rule_id, "promo_platform_error")
        wrong = self.decide("promo_code_out_of_date", "guest", coupon={"oracle_customer_right": False})
        self.assertEqual((wrong.kind, wrong.rule_id), ("decline", "promo_guest_no_code"))

    def test_identity_methods(self):
        self.assertEqual(self.policy.identity_method({"email": "a@b.c"}), ["email"])
        self.assertEqual(self.policy.identity_method({"full_name": "A B", "zip_code": "1"}),
                         ["full_name", "zip_code"])
        self.assertIsNone(self.policy.identity_method({"account_id": "ACCT-000001"}))


if __name__ == "__main__":
    unittest.main()
