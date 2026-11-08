"""Actual LangGraph routing, persistence and policy-gate smoke tests."""

from pathlib import Path
import tempfile
import unittest

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.action_log.action_ledger import ActionLedger
from agent.action_log.policy_gate import PolicyGate
from agent.retail_graph.graph import build_graph


class ScriptedInterpreter:
    """Predictable test double; not presented as the production LLM agent."""

    def extract(self, message, state):
        if message == "cancel":
            return {"requests": [{"intent": "manage_cancel"}]}
        if message == "account":
            return {"account_identifier": {"type": "account_id", "value": "ACCT-000300"}}
        if message == "order":
            return {"fields": {"order_id": "0066041981"}}
        if message == "cancel all":
            return {"account_identifier": {"type": "account_id", "value": "ACCT-000300"},
                    "requests": [{"intent": "manage_cancel", "order_id": "0066041981"}]}
        if message == "cancel and downgrade":
            return {"account_identifier": {"type": "account_id", "value": "ACCT-000300"},
                    "requests": [
                        {"intent": "manage_cancel", "order_id": "0066041981"},
                        {"intent": "manage_downgrade", "order_id": "0066041981"},
                    ]}
        if message == "coupon":
            return {"account_identifier": {"type": "account_id", "value": "ACCT-000001"},
                    "requests": [{"intent": "promo_code_out_of_date", "coupon_code": "OLD20"}]}
        if message == "return size":
            return {"account_identifier": {"type": "account_id", "value": "ACCT-000001"},
                    "requests": [{"intent": "return_size", "order_id": "5199420931"}]}
        if message == "wrong size was 8 instead of 7":
            return {"fields": {"problem_detail": "received size 8 instead of ordered size 7"}}
        if message == "cancel multi":
            return {"account_identifier": {"type": "account_id", "value": "ACCT-000165"},
                    "requests": [{"intent": "manage_cancel", "order_id": "0225483128"}]}
        if message == "guess jeans":
            return {"fields": {"item_name": "guess jeans"}}
        return {}

    def propose(self, request, state):
        if request["intent"] == "manage_cancel":
            return {"action_type": "cancel_item_and_refund",
                    "payload": {"item_index": request["item_index"],
                                "amount_cents": 9900 if getattr(self, "inflate", False) else 8400}}
        if request["intent"] == "manage_downgrade":
            return {"action_type": "reschedule_delivery",
                    "payload": {"delivery_window": "next_week"}}
        if request["intent"] == "promo_code_out_of_date":
            return {"action_type": "issue_replacement_coupon", "payload": {}}
        return {"action_type": None, "reason": "Unable to resolve"}


class RetailGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.ledger = ActionLedger(self.folder / "actions.sqlite")
        self.gate = PolicyGate(self.ledger)
        self.saver_context = SqliteSaver.from_conn_string(str(self.folder / "checkpoints.sqlite"))
        self.saver = self.saver_context.__enter__()
        self.addCleanup(lambda: self.saver_context.__exit__(None, None, None))
        self.interpreter = ScriptedInterpreter()
        self.graph = build_graph(
            self.interpreter, self.gate, self.saver,
            coupon_lookup=lambda customer, code: {
                "status": "resolved", "customer_right": False,
                "reason_code": "expired", "merchant_status": "expired"},
        )

    def send(self, thread_id, text):
        return self.graph.invoke(
            {"messages": [HumanMessage(content=text)]},
            {"configurable": {"thread_id": thread_id}},
        )

    def test_three_turn_chat_reuses_state_and_records_one_action(self):
        first = self.send("chat-1", "cancel")
        self.assertIn("account ID", first["reply_text"])
        second = self.send("chat-1", "account")
        self.assertIn("order ID", second["reply_text"])
        third = self.send("chat-1", "order")
        self.assertIn("Simulated action recorded", third["reply_text"])
        self.assertEqual(third["customer_id"], "C0300")
        self.assertEqual(third["requests"][0]["status"], "resolved")
        self.assertTrue(any(e["planned_tool"] == "lookup_customer" for e in third["cp1_events"]))
        self.assertTrue(any(e["planned_tool"] == "lookup_order" for e in third["cp1_events"]))
        self.assertEqual(len(third["cp2_events"]), 1)
        self.assertEqual(third["cp2_events"][0]["gate_outcome"], "simulated")
        self.assertEqual(third["cp2_events"][0]["policy_rule_id"], "cancel_order_received")
        self.assertEqual(len(self.ledger.list_conversation("chat-1")), 1)

    def test_wrong_amount_is_blocked_by_policy_gate(self):
        self.interpreter.inflate = True
        result = self.send("chat-inflated", "cancel all")
        self.assertIn("policy check blocked", result["reply_text"])
        event = self.ledger.list_conversation("chat-inflated")[0]
        self.assertEqual(event["outcome"], "blocked")
        self.assertEqual(result["cp2_events"][0]["reason_code"], "policy_gate_mismatch")

    def test_two_requests_keep_separate_results(self):
        result = self.send("chat-two", "cancel and downgrade")
        self.assertEqual([r["status"] for r in result["requests"]], ["resolved", "resolved"])
        self.assertEqual([r["intent"] for r in result["requests"]],
                         ["manage_cancel", "manage_downgrade"])
        self.assertEqual(len(self.ledger.list_conversation("chat-two")), 2)

    def test_coupon_does_not_require_order_or_issue_code(self):
        result = self.send("chat-coupon", "coupon")
        self.assertIn("replacement coupon issuance is not authorized", result["reply_text"])
        self.assertEqual(result["requests"][0]["coupon_result"]["reason_code"], "expired")
        self.assertEqual(self.ledger.list_conversation("chat-coupon"), [])

    def test_thread_isolation(self):
        self.send("chat-a", "cancel")
        other = self.send("chat-b", "order")
        self.assertEqual(other["requests"], [])
        self.assertIsNone(other.get("customer_id"))
        self.assertEqual(self.ledger.list_conversation("chat-b"), [])

    def test_return_asks_for_concrete_mismatch_before_resolution(self):
        first = self.send("chat-return", "return size")
        self.assertIn("describe the size or color mismatch", first["reply_text"])
        self.assertEqual(first["requests"][0]["status"], "open")
        second = self.send("chat-return", "wrong size was 8 instead of 7")
        self.assertEqual(second["requests"][0]["problem_detail"],
                         "received size 8 instead of ordered size 7")

    def test_multi_item_order_asks_which_item_then_matches_name(self):
        first = self.send("chat-items", "cancel multi")
        self.assertIn("which item", first["reply_text"])
        second = self.send("chat-items", "guess jeans")
        self.assertEqual(second["requests"][0]["item_index"], 1)
        self.assertEqual(self.ledger.list_conversation("chat-items")[0]["outcome"], "simulated")


if __name__ == "__main__":
    unittest.main()
