"""The saved cases still match the current graph and policy.

If a change to the graph or policy_v1.json breaks one of these tests, the
saved expectations are stale: rebuild with ``python -m evaluation.build_cases``
and review what changed before relabeling.
"""

import json
from pathlib import Path
import unittest

from evaluation.harness import ExpectedInterpreter, run_case, run_script

CASES = Path(__file__).resolve().parent / "cases"


def _load(name):
    with open(CASES / name, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


class SavedCasesTests(unittest.TestCase):
    def test_counts_and_unique_ids(self):
        cases = _load("cp1.jsonl") + _load("cp2.jsonl") + _load("conversation.jsonl")
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        manifest = json.loads((CASES / "manifest.json").read_text(encoding="utf-8"))
        for name, info in manifest["files"].items():
            self.assertEqual(len(_load(name)), info["cases"])

    def test_checkpoint_cases_replay_to_their_expectations(self):
        for case in _load("cp1.jsonl") + _load("cp2.jsonl"):
            with self.subTest(case=case["id"]):
                result = run_case(case, ExpectedInterpreter(case["expected"]["extraction"]))
                self.assertEqual(result["stop_point"], case["stop_point"])
                cp1 = [{k: e[k] for k in ("next_step", "planned_tool", "arguments", "asked")}
                       for e in result["cp1_events"]]
                self.assertEqual(cp1, case["expected"]["cp1"])
                cp2 = [{k: v for k, v in e.items() if k not in {"turn", "request_id"}}
                       for e in result["cp2_events"]]
                self.assertEqual(cp2, case["expected"]["cp2"])

    def test_conversation_reference_scripts_reach_expected_outcomes(self):
        for case in _load("conversation.jsonl"):
            with self.subTest(case=case["id"]):
                ref = run_script(case["reference_script"], case["coupon_verdicts"], case["id"])
                got = [{k: r[k] for k in ("intent", "outcome", "policy_rule_id")}
                       for r in ref["ledger"]]
                self.assertEqual(got, case["expected"]["ledger"])


if __name__ == "__main__":
    unittest.main()
