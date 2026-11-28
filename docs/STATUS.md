# Project status

## 2026-11-19: policy-v1 redesign

* **Policy v1** (`processed_data/policy/policy_v1.json`, rendered to
  `docs/POLICY_V1.md`) is now the single source of truth. It adds general
  rules (identity by email or name + zip, one customer per conversation,
  explicit confirmation before any change, decline vs handoff), declares where
  each fact may come from (system first, customer if allowed), fixes the
  evaluation clock at 2020-04-01, and restores ABCD branches the old gate
  blocked: customer-stated receipts, Bronze shipped cancel, shipped-member
  shipping credit ($8, from the ABCD Shipping Cost flow), and promo code
  issuance. Every rule is tagged `abcd`, `tau_bench`, or `assumption`.
  **Status: draft, not yet reviewed.**
* **Policy engine** (`agent/policy/engine.py`) evaluates ordered rules with
  three-valued logic and returns needed facts, decline, handoff, or options
  with exact payloads. Over the 1,148 source orders (before any customer
  answers), 779 get options, 180 need one customer answer (receipt), 125 are
  declined, and 64 are handed off (missing shipping status, or guest promo
  cases without an Oracle verdict).
* **Graph**: `route` is one ladder for all intents; the `propose` node and
  all policy text in the model prompt are removed. The model only extracts
  what the customer said. Options are offered and need an explicit yes;
  customers can pick, refuse, withdraw, or change details.
* **Gate** re-reads records and re-runs the engine before the ledger records
  anything; the ledger now records declines, refusals, and handoffs too.
* **Checks**: 57 unit tests and 10 scripted multi-turn smoke conversations
  pass. The live model path has **not** been re-run after the redesign.

Next: review policy v1; then build the evaluation harness and reviewed
CP1/CP2/full-conversation cases; then a small chat UI.

## 2026-11-26: cleanup

Removed code and data the policy-v1 backend no longer uses: the October
ABCD cutpoint pools (`processed_data/abcd_cases/`, `scripts/prepare_test_cases.py`,
`sql/scenario_schema.sql`, `tests/`), the superseded 22-rule
`policy_rules.json` and its builder, the unused FastAPI Oracle wrapper and
`agent_tool.py` adapter, and the FastAPI/uvicorn/httpx dependencies. Historical
ABCD conversation prefixes do not fit the current workflow (the agent asks
different questions in a different order), so evaluation cases will be built
from the synthetic database instead. The removed files remain in Git history.

## 2026-11-28: evaluation definitions and event records

* `docs/WORKFLOW.md` now defines the two checkpoints for the current design:
  **CP1** (information gathering: extraction and the next query or question)
  and **CP2** (decision and action: the policy decision against the true
  facts, and actions only after a real choice and yes), plus full
  conversations with a simulated customer, the case format, and how labels
  are made.
* The graph records what the evaluation needs: `extract_events` (the model's
  extraction for each message), a `turn` on every event, tool `arguments` in
  CP1, and `facts_used` plus option payloads in CP2.
* **Checks**: 54 unit tests and 10 scripted smoke conversations pass. The
  live model path still has to be run (`run_live_smoke.ps1`); this
  environment has no OpenAI access.

Next: evaluation harness and cases; chat UI.

