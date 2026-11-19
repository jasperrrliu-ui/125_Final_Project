"""The gate re-reads records, re-runs the policy, and only records what it allows."""

from pathlib import Path
import tempfile
import unittest

from agent.action_log.action_ledger import ActionLedger
from agent.action_log.policy_gate import AuthorizationRequest, PolicyGate


CANCEL_OPTION = {"option_id": "cancel_and_refund", "action": "cancel_item_and_refund",
                 "payload": {"item_index": 0, "amount_cents": 8400}}


class PolicyGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = ActionLedger(Path(self.temp.name) / "actions.sqlite")
        self.gate = PolicyGate(self.ledger)

    def request(self, key, **overrides):
        values = dict(conversation_id="conv", request_key=key, intent="manage_cancel",
                      customer_id="C0300", order_id="0066041981", item_index=0,
                      option=CANCEL_OPTION, confirmed=True)
        values.update(overrides)
        return AuthorizationRequest(**values)

    def test_confirmed_allowed_option_is_simulated(self):
        result = self.gate.authorize(self.request("ok"))
        self.assertEqual((result["outcome"], result["policy_rule_id"]),
                         ("simulated", "cancel_order_received"))

    def test_unconfirmed_option_is_blocked(self):
        result = self.gate.authorize(self.request("unconfirmed", confirmed=False))
        self.assertEqual((result["outcome"], result["reason_code"]),
                         ("blocked", "customer_confirmation_missing"))

    def test_tampered_amount_is_blocked(self):
        inflated = {**CANCEL_OPTION, "payload": {"item_index": 0, "amount_cents": 9900}}
        result = self.gate.authorize(self.request("inflated", option=inflated))
        self.assertEqual((result["outcome"], result["reason_code"]),
                         ("blocked", "policy_gate_mismatch"))

    def test_other_customers_order_is_blocked(self):
        result = self.gate.authorize(self.request("foreign", customer_id="C0002"))
        self.assertEqual((result["outcome"], result["reason_code"]),
                         ("blocked", "order_not_verified"))

    def test_option_not_allowed_by_current_policy_is_blocked(self):
        refund = {"option_id": "refund_now", "action": "refund_item",
                  "payload": {"item_index": 0, "amount_cents": 8400}}
        result = self.gate.authorize(self.request("wrong-option", option=refund))
        self.assertEqual((result["outcome"], result["reason_code"]),
                         ("blocked", "option_not_allowed"))

    def test_customer_fact_change_is_rechecked(self):
        # Silver, packaging "no", bought 2019-08-22: eligible only with a receipt.
        option = {"option_id": "return_by_mail", "action": "initiate_return",
                  "payload": {"item_index": 0, "return_method": "by_mail",
                              "address_confirmed": True}}
        base = dict(intent="return_color", customer_id="C0107", order_id="5110435718",
                    option=option)
        allowed = self.gate.authorize(self.request("receipt", customer_facts={"has_receipt": True},
                                                   **base))
        self.assertEqual(allowed["outcome"], "simulated")
        denied = self.gate.authorize(self.request("no-receipt", customer_facts={"has_receipt": False},
                                                  **base))
        self.assertEqual((denied["outcome"], denied["reason_code"]),
                         ("blocked", "policy_decision_decline"))

    def test_promo_code_is_orderless(self):
        option = {"option_id": "issue_code_goodwill", "action": "issue_promo_code",
                  "payload": {"discount_percent": 20}}
        result = self.gate.authorize(self.request(
            "promo", intent="promo_code_invalid", customer_id="C0001", order_id=None,
            item_index=None, option=option, coupon_result={"status": "not_found"}))
        self.assertEqual((result["outcome"], result["order_id"]), ("simulated", None))

    def test_record_outcome_carries_policy_version(self):
        result = self.gate.record_outcome(outcome="declined", conversation_id="conv",
                                          request_id="d1", intent="manage_upgrade",
                                          reason_code="already_shipped")
        self.assertEqual(result["outcome"], "declined")
        self.assertEqual(result["policy_version"], self.gate.version)


if __name__ == "__main__":
    unittest.main()
