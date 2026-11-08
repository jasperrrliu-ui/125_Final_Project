from pathlib import Path
import tempfile
import unittest

from agent.action_log.action_ledger import ActionLedger, ActionProposal, Authorization


class ActionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = ActionLedger(Path(self.tmp.name) / "actions.sqlite")
        self.proposal = ActionProposal(
            conversation_id="conversation-1",
            request_id="conversation-1:cancel:refund-item-0",
            intent="manage_cancel",
            action_type="refund_item",
            customer_id="C0001",
            order_id="O100",
            payload={"item_index": 0, "amount_cents": 5900},
        )
        self.authorization = Authorization(
            allowed_action="refund_item",
            verified_customer_id="C0001",
            verified_order_id="O100",
            policy_rule_id="cancel_order_received",
            policy_version="draft-v1",
            approved_payload={"item_index": 0, "amount_cents": 5900},
        )

    def test_records_simulation_without_touching_business_data(self):
        result = self.ledger.record_action(self.proposal, self.authorization)
        self.assertEqual(result["outcome"], "simulated")
        self.assertEqual(result["policy_rule_id"], "cancel_order_received")
        self.assertEqual(result["payload"], {"item_index": 0, "amount_cents": 5900})
        self.assertEqual(self.ledger.list_conversation("conversation-1"), [result])
        self.assertEqual(self.ledger.list_conversation("conversation-2"), [])

    def test_blocks_customer_or_policy_mismatch(self):
        wrong_account = Authorization(
            allowed_action="refund_item",
            verified_customer_id="C0002",
            verified_order_id="O100",
            policy_rule_id="cancel_order_received",
            policy_version="draft-v1",
        )
        result = self.ledger.record_action(self.proposal, wrong_account)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["reason_code"], "policy_gate_mismatch")

    def test_same_request_is_idempotent_and_conflicting_retry_fails(self):
        first = self.ledger.record_action(self.proposal, self.authorization)
        second = self.ledger.record_action(self.proposal, self.authorization)
        self.assertEqual(first, second)
        changed = ActionProposal(**{**self.proposal.__dict__, "payload": {"item_index": 0, "amount_cents": 6000}})
        with self.assertRaisesRegex(ValueError, "reused"):
            self.ledger.record_action(changed, self.authorization)

    def test_bad_refund_payload_is_rejected(self):
        changed = ActionProposal(**{**self.proposal.__dict__, "payload": {"item_index": 0, "amount_cents": -1}})
        with self.assertRaisesRegex(ValueError, "amount_cents"):
            self.ledger.record_action(changed, self.authorization)
        self.assertEqual(self.ledger.list_conversation("conversation-1"), [])

    def test_approved_action_cannot_change_item_or_amount(self):
        changed = ActionProposal(**{
            **self.proposal.__dict__,
            "payload": {"item_index": 0, "amount_cents": 6000},
        })
        result = self.ledger.record_action(changed, self.authorization)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["reason_code"], "policy_gate_mismatch")

    def test_handoff_has_reason_and_never_simulates_action(self):
        result = self.ledger.record_handoff(
            conversation_id="conversation-2",
            request_id="conversation-2:unhandled:handoff",
            intent="other",
            reason_code="outside_eight_intents",
        )
        self.assertEqual(result["outcome"], "handoff")
        self.assertEqual(result["action_type"], "handoff")
        self.assertIsNone(result["policy_rule_id"])
        self.assertEqual(self.ledger.list_conversation("conversation-2"), [result])

    def test_delivery_and_return_payloads_are_constrained(self):
        delivery = ActionProposal(**{
            **self.proposal.__dict__,
            "action_type": "reschedule_delivery",
            "payload": {"delivery_window": "tomorrow"},
        })
        delivery_auth = Authorization(**{**self.authorization.__dict__,
                                         "allowed_action": "reschedule_delivery",
                                         "approved_payload": {"delivery_window": "tomorrow"}})
        self.assertEqual(self.ledger.record_action(delivery, delivery_auth)["outcome"], "simulated")
        bad_return = ActionProposal(**{
            **self.proposal.__dict__,
            "request_id": "return-1",
            "action_type": "initiate_return",
            "payload": {"item_index": 0, "return_method": "by_mail", "address_confirmed": False},
        })
        with self.assertRaisesRegex(ValueError, "confirmed address"):
            self.ledger.record_action(bad_return, self.authorization)


if __name__ == "__main__":
    unittest.main()
