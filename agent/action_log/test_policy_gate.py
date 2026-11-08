"""Check that source policy and re-read DB facts control simulated actions."""

from pathlib import Path
import tempfile
import unittest

from agent.action_log.action_ledger import ActionLedger, ActionProposal
from agent.action_log.policy_gate import PolicyGate, TrustedContext


class PolicyGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = ActionLedger(Path(self.temp.name) / "actions.sqlite")
        self.gate = PolicyGate(self.ledger)

    def proposal(self, case: str, intent: str, action: str,
                 customer: str, order: str, payload: dict) -> ActionProposal:
        return ActionProposal("conversation-test", case, intent, action,
                              customer, order, payload)

    def test_received_order_can_cancel_only_its_actual_item_amount(self):
        good = self.proposal("cancel-good", "manage_cancel", "cancel_item_and_refund",
                             "C0300", "0066041981",
                             {"item_index": 0, "amount_cents": 8400})
        self.assertEqual(self.gate.review_and_record(good, TrustedContext("C0300"))["outcome"],
                         "simulated")
        inflated = self.proposal("cancel-inflated", "manage_cancel", "cancel_item_and_refund",
                                 "C0300", "0066041981",
                                 {"item_index": 0, "amount_cents": 9900})
        self.assertEqual(self.gate.review_and_record(inflated, TrustedContext("C0300"))["outcome"],
                         "blocked")

    def test_wrong_customer_and_missing_shipping_status_block(self):
        wrong = self.proposal("wrong-owner", "manage_cancel", "cancel_item_and_refund",
                              "C0300", "0066041981",
                              {"item_index": 0, "amount_cents": 8400})
        result = self.gate.review_and_record(wrong, TrustedContext("C0002"))
        self.assertEqual(result["outcome"], "blocked")
        missing = self.proposal("missing-status", "manage_cancel", "cancel_item_and_refund",
                                "C0001", "5199420931",
                                {"item_index": 0, "amount_cents": 5900})
        result = self.gate.review_and_record(missing, TrustedContext("C0001"))
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["reason_code"], "shipping_status_missing")

    def test_shipped_member_gets_refund_but_shipped_guest_does_not(self):
        member = self.proposal("shipped-member", "manage_cancel", "refund_item",
                               "C0414", "0138162349",
                               {"item_index": 0, "amount_cents": 8900})
        self.assertEqual(self.gate.review_and_record(member, TrustedContext("C0414"))["outcome"],
                         "simulated")
        guest = self.proposal("shipped-guest", "manage_cancel", "refund_item",
                              "C0478", "0404012051",
                              {"item_index": 0, "amount_cents": 5900})
        result = self.gate.review_and_record(guest, TrustedContext("C0478"))
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["reason_code"], "shipped_guest_wait_for_arrival")
        bronze = self.proposal("shipped-bronze", "manage_cancel", "refund_item",
                               "C0485", "0331458736",
                               {"item_index": 0, "amount_cents": 6400})
        result = self.gate.review_and_record(bronze, TrustedContext("C0485"))
        self.assertEqual(result["reason_code"], "cancel_bronze_source_ambiguity")

    def test_upgrade_gold_and_non_gold_consent(self):
        gold = self.proposal("upgrade-gold", "manage_upgrade", "reschedule_delivery",
                             "C0007", "7027853016", {"delivery_window": "tomorrow"})
        self.assertEqual(self.gate.review_and_record(gold, TrustedContext("C0007"))["outcome"],
                         "simulated")
        non_gold = self.proposal("upgrade-guest-no-consent", "manage_upgrade",
                                 "reschedule_delivery", "C0300", "0066041981",
                                 {"delivery_window": "tomorrow", "charge_amount_cents": 2000})
        self.assertEqual(self.gate.review_and_record(non_gold, TrustedContext("C0300"))["outcome"],
                         "blocked")
        consented = self.proposal("upgrade-guest-consent", "manage_upgrade",
                                  "reschedule_delivery", "C0300", "0066041981",
                                  {"delivery_window": "tomorrow", "charge_amount_cents": 2000})
        context = TrustedContext("C0300", authorized_20_usd_charge=True)
        self.assertEqual(self.gate.review_and_record(consented, context)["outcome"],
                         "simulated")

    def test_downgrade_and_unverified_shipping_fee(self):
        received = self.proposal("downgrade-received", "manage_downgrade",
                                 "reschedule_delivery", "C0300", "0066041981",
                                 {"delivery_window": "next_week"})
        self.assertEqual(self.gate.review_and_record(received, TrustedContext("C0300"))["outcome"],
                         "simulated")
        shipped = self.proposal("downgrade-shipped", "manage_downgrade",
                                "credit_shipping_fee", "C0414", "0138162349",
                                {"amount_cents": 500})
        result = self.gate.review_and_record(shipped, TrustedContext("C0414"))
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["reason_code"], "shipping_fee_unavailable")

    def test_return_requires_source_eligibility_and_confirmed_details(self):
        proposed = self.proposal("return-missing-confirmation", "return_size",
                                 "initiate_return", "C0001", "5199420931",
                                 {"item_index": 0, "return_method": "by_mail",
                                  "address_confirmed": True})
        result = self.gate.review_and_record(proposed, TrustedContext("C0001"))
        self.assertEqual(result["reason_code"], "return_details_not_confirmed")
        confirmed = self.proposal("return-confirmed", "return_size",
                                  "initiate_return", "C0001", "5199420931",
                                  {"item_index": 0, "return_method": "by_mail",
                                   "address_confirmed": True})
        context = TrustedContext("C0001", return_address={
                                     "street": "1 Test Street", "city": "Boston",
                                     "state": "MA", "zip_code": "02101"},
                                 return_address_confirmed=True,
                                 return_method_confirmed="by_mail")
        result = self.gate.review_and_record(confirmed, context)
        self.assertEqual(result["outcome"], "simulated")
        self.assertEqual(result["policy_rule_id"], "return_silver")

    def test_coupon_issuance_has_no_permission_yet(self):
        proposed = self.proposal("promo-unavailable", "promo_code_invalid",
                                 "refund_item", "C0001", "5199420931",
                                 {"item_index": 0, "amount_cents": 5900})
        auth = self.gate.review(proposed, TrustedContext("C0001"))
        self.assertIsNone(auth.allowed_action)
        self.assertEqual(auth.denial_reason, "coupon_issuance_not_implemented")


if __name__ == "__main__":
    unittest.main()
