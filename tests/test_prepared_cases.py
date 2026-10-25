"""Integration checks for generated cases and isolated scenario fixtures."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from prepare_test_cases import materialize_agent_input  # noqa: E402


def rows(name: str) -> list[dict]:
    path = ROOT / "prepared_data" / name
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


class PreparedCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conversations = {row["convo_id"]: row for row in rows("conversations.jsonl")}
        cls.tool_cases = rows("tool_cases.jsonl")
        cls.policy_cases = rows("policy_cases.jsonl")

    def test_cases_hide_future_turns_and_scenario(self) -> None:
        for case in [*self.tool_cases, *self.policy_cases]:
            conversation = self.conversations[case["convo_id"]]
            agent_input = materialize_agent_input(case, conversation)
            messages = agent_input["messages"]
            self.assertEqual(len(messages), case["cutoff_after_turn"])
            self.assertNotIn("scenario", agent_input)
            self.assertNotIn("subflow", agent_input)
            if case["kind"] == "tool_call":
                self.assertEqual(case["source_event_turn"], case["cutoff_after_turn"] + 1)
                self.assertEqual(
                    conversation["messages"][case["cutoff_after_turn"]]["speaker"], "action"
                )

    def test_case_ids_are_unique_and_splits_are_preserved(self) -> None:
        all_cases = [*self.tool_cases, *self.policy_cases]
        self.assertEqual(len(all_cases), len({case["case_id"] for case in all_cases}))
        for case in all_cases:
            self.assertEqual(case["split"], self.conversations[case["convo_id"]]["split"])
            self.assertEqual(case["reference_label_status"], "not_created")

    def test_known_policy_cutpoints(self) -> None:
        by_conversation = {case["convo_id"]: case for case in self.policy_cases}
        self.assertEqual(by_conversation[9072]["cutoff_after_turn"], 22)
        self.assertEqual(by_conversation[8395]["cutoff_after_turn"], 17)
        self.assertEqual(by_conversation[2225]["cutoff_after_turn"], 17)

    def test_sqlite_preserves_per_conversation_isolation(self) -> None:
        with sqlite3.connect(ROOT / "prepared_data" / "scenario_fixtures.sqlite") as connection:
            count = connection.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0]
            self.assertEqual(count, len(self.conversations))
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            repeated_name = connection.execute(
                "SELECT customer_name FROM accounts GROUP BY customer_name "
                "HAVING COUNT(*) > 1 LIMIT 1"
            ).fetchone()[0]
            order_ids = connection.execute(
                "SELECT DISTINCT o.order_id FROM accounts a "
                "JOIN orders o USING (convo_id) WHERE a.customer_name = ?",
                (repeated_name,),
            ).fetchall()
            self.assertGreater(len(order_ids), 1)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM products").fetchone()[0],
                connection.execute("SELECT SUM(num_products) FROM orders").fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
