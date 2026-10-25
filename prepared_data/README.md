# Prepared test inputs

These files are generated from the unchanged ABCD source with
`python scripts/prepare_test_cases.py`. They cover the eight intents listed in
`manifest.json`. Run `python -m unittest discover -s tests -v` after rebuilding.

| File | Purpose |
| --- | --- |
| `conversations.jsonl` | Original chat turns, normalized with positional turn numbers. |
| `scenario_fixtures.jsonl` | Account, order, and product facts extracted from each raw `scenario`. |
| `scenario_fixtures.sqlite` | Local SQLite copy of those facts, built using `sql/scenario_schema.sql`; ignored by Git and recreated by the script. |
| `tool_cases.jsonl` | Cut immediately before an observed, in-scope action. |
| `policy_cases.jsonl` | Cut after a policy-relevant piece of evidence or action. |
| `manifest.json` | Source hash, counts, and review flags. |

The case files contain **no reference labels or expected outcomes**. A case
identifies a conversation and `cutoff_after_turn`. To run it, look up the
conversation and pass only its messages through that turn to the agent. The
fixture and case metadata are for the test harness; the agent must obtain
business facts through tools rather than seeing the fixture directly.

The original `train`/`dev`/`test` split is preserved. The `turn` field is the
position in the recorded conversation. `source_turn_count` retains the original
ABCD number, which has gaps in 73 selected conversations.

Policy cutpoints are automatically selected **candidates**, not verified
decision points. A return case may still require another question. The ABCD
scenario does not structure the promo flow's Oracle result; cases with this
gap carry `oracle_result_not_structured_in_scenario`. Other gaps are listed in
`review_flags` and counted in the manifest. Review the cutpoints and define
tool behavior before creating reference labels or claiming agent accuracy.

ABCD reuses fictional customer names across distinct conversations. The
SQLite schema therefore joins records by hidden `convo_id`, keeping each
scenario isolated. This is a test fixture namespace, not a customer ID that
the agent should see.
