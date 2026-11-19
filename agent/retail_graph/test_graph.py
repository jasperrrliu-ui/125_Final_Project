"""LangGraph routing, persistence, confirmation, and policy-gate integration."""

from pathlib import Path
import tempfile
import unittest

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.action_log.action_ledger import ActionLedger
from agent.action_log.policy_gate import PolicyGate
from agent.retail_graph.graph import build_graph, match_item


FELIX = {"email": "felix.hayes.0300@example.test"}  # guest; 0066041981 is Order Received


class ScriptedInterpreter:
    """Predictable test double keyed by message text."""

    replies = {
        "cancel": {"requests": [{"intent": "manage_cancel"}]},
        "id": {"identity": FELIX},
        "account only": {"identity": {"account_id": "ACCT-000300"}},
        "order": {"fields": {"order_id": "0066041981"}},
        "yes": {"confirmation": "yes"},
        "no": {"confirmation": "no"},
        "drop it": {"withdraw_active_request": True},
        "cancel all": {"identity": FELIX,
                       "requests": [{"intent": "manage_cancel", "order_id": "0066041981"}]},
        "cancel and downgrade": {"identity": FELIX, "requests": [
            {"intent": "manage_cancel", "order_id": "0066041981"},
            {"intent": "manage_downgrade", "order_id": "0066041981"}]},
        "password": {"out_of_scope": "reset my password"},
        "other customer": {"identity": {"email": "eli.gray.0001@example.test"}},
    }

    def extract(self, message, state):
        return self.replies.get(message, {})


class RetailGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.ledger = ActionLedger(folder / "actions.sqlite")
        self.gate = PolicyGate(self.ledger)
        self.saver_context = SqliteSaver.from_conn_string(str(folder / "checkpoints.sqlite"))
        self.saver = self.saver_context.__enter__()
        self.addCleanup(lambda: self.saver_context.__exit__(None, None, None))
        self.graph = build_graph(ScriptedInterpreter(), self.gate, self.saver,
                                 coupon_lookup=lambda c, code: {"status": "not_found"})

    def send(self, thread_id, text):
        return self.graph.invoke({"messages": [HumanMessage(content=text)]},
                                 {"configurable": {"thread_id": thread_id}})

    def test_multi_turn_chat_asks_offers_confirms_and_records_once(self):
        self.assertIn("verify your identity", self.send("t1", "cancel")["reply_text"])
        self.assertIn("order ID", self.send("t1", "id")["reply_text"])
        offer = self.send("t1", "order")
        self.assertIn("refund $84.00", offer["reply_text"])
        self.assertEqual(self.ledger.list_conversation("t1"), [])  # nothing before yes
        done = self.send("t1", "yes")
        self.assertIn("Done.", done["reply_text"])
        self.assertEqual(done["requests"][0]["status"], "resolved")
        events = self.ledger.list_conversation("t1")
        self.assertEqual([(e["outcome"], e["policy_rule_id"]) for e in events],
                         [("simulated", "cancel_order_received")])
        tools = [e["planned_tool"] for e in done["cp1_events"] if e["planned_tool"]]
        self.assertEqual(tools, ["verify_customer", "lookup_order"])

    def test_account_id_alone_is_not_identity(self):
        self.send("t2", "cancel")
        result = self.send("t2", "account only")
        self.assertIsNone(result.get("customer_id"))
        self.assertIn("verify your identity", result["reply_text"])

    def test_customer_can_say_no(self):
        self.send("t3", "cancel all")
        result = self.send("t3", "no")
        self.assertEqual(result["requests"][0]["status"], "customer_declined")
        self.assertEqual(self.ledger.list_conversation("t3")[0]["outcome"], "customer_declined")

    def test_customer_can_withdraw_while_waiting(self):
        self.send("t4", "cancel")
        result = self.send("t4", "drop it")
        self.assertEqual(result["requests"][0]["status"], "customer_declined")

    def test_two_requests_are_handled_in_turn(self):
        first = self.send("t5", "cancel and downgrade")
        self.assertEqual([r["status"] for r in first["requests"]], ["offered", "open"])
        second = self.send("t5", "yes")
        self.assertEqual([r["status"] for r in second["requests"]], ["resolved", "offered"])
        third = self.send("t5", "yes")
        self.assertEqual([r["status"] for r in third["requests"]], ["resolved", "resolved"])
        self.assertEqual(len(self.ledger.list_conversation("t5")), 2)

    def test_out_of_scope_is_answered_immediately(self):
        self.send("t6", "cancel all")
        result = self.send("t6", "password")
        self.assertIn("reset my password", result["reply_text"])
        self.assertEqual(self.ledger.list_conversation("t6")[0]["outcome"], "handoff")
        self.assertEqual(result["requests"][0]["status"], "offered")  # cancel still waits

    def test_verified_identity_is_locked(self):
        self.send("t7", "cancel all")
        result = self.send("t7", "other customer")
        self.assertIn("already verified", result["reply_text"])
        self.assertEqual(result["customer_id"], "C0300")

    def test_thread_isolation(self):
        self.send("t8a", "cancel all")
        other = self.send("t8b", "yes")
        self.assertEqual(other["requests"], [])
        self.assertEqual(self.ledger.list_conversation("t8b"), [])

    def test_item_name_matching(self):
        items = [{"item_index": 0, "name": "guess_jacket"},
                 {"item_index": 1, "name": "tommy_hilfiger shirt"}]
        self.assertEqual(match_item("the jacket", items), 0)
        self.assertEqual(match_item("Tommy Hilfiger shirt", items), 1)
        self.assertIsNone(match_item("jeans", items))
        self.assertIsNone(match_item("guess", [*items, {"item_index": 2, "name": "guess jeans"}]))


if __name__ == "__main__":
    unittest.main()
