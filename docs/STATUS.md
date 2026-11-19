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

Next: review policy v1; then (next phase) Langfuse tracing and CP1/CP2/full
conversation label generation.

The sections below describe the state before this redesign.

## Earlier status (2026-11-08)

## Implemented and checked

| Area | Current state |
| --- | --- |
| Raw source | ABCD files copied unchanged with upstream commit, license, and hashes. |
| Selected data | Eight intents; 1,148 original conversations; two unlabeled cutpoint pools and case-isolated SQLite fixtures. |
| Linked simulation | 808 synthetic identities, 1,148 source-derived orders, 1,273 items, 32 synthetic combined-request prompts, and a checked-in relational SQLite database. |
| Policy | 22 rules transcribed from ABCD guidelines with source text/path and explicit unresolved issues. |
| Coupon service | Separate synthetic platform/merchant SQLite stores, local HTTP Oracle, HMAC cache, and a scoped agent-facing coupon wrapper. |
| Agent backend | Six-node LangGraph, SQLite conversation state, model extraction/proposal, three read-only query routes, policy gate, simulated action ledger, and CLI chat. |
| Checks | Original case-preparation tests; backend and Oracle unit/integration tests; eight-intent scripted graph smoke. A real `gpt-5.4-mini` full smoke run passed 7/8; the remaining later-delivery intent passed a targeted retest after prompt correction. |

The live smoke confirms the API-backed path can run. It is **not** a reviewed
accuracy estimate: eight hand-written prompts are too few, model behavior can
vary across runs, and the full eight-case batch has not been rerun after the
last intent-prompt correction. The scripted smoke checks graph wiring and
backend constraints, not language understanding. Local runtime reports and
credentials are intentionally excluded from Git.

## Not implemented yet

1. **128-case gold evaluation.** Select a manageable, reviewable sample from
   the 4,428/847 cutpoint candidates and synthetic cases; define expected
   CP1 tool/parameter and CP2 policy/action outcomes. Use model-assisted
   reference labeling if useful, then human-audit disagreements and ambiguous
   policy cases. Preserve source-conversation split boundaries.
2. **Langfuse tracing and monitoring.** Instrument the graph, model calls,
   read tools, Oracle boundary, policy gate, and CP1/CP2 scoring. Mask
   customer identifiers and coupon codes in public trace metadata. No
   Langfuse project or trace is connected yet.
3. **Customer-facing chat UI and chat API.** Only the local CLI chat and Oracle
   HTTP API exist. Add a small frontend and backend chat endpoint using a
   stable conversation ID. Keep each user's state isolated.
4. **More complete business handling.** The current flow does not collect
   trusted return address/method or charge consent for non-Gold upgrades.
   It cannot issue replacement coupons, and some source policy branches have
   missing or conflicting evidence. These paths hand off or block.
5. **Real deployment/security.** Account lookup in synthetic data is not
   authentication. There is no real retail integration, authorization system,
   production logging policy, or deployed service.

The next engineering milestone is to connect Langfuse and define the reference
label schema; the next evaluation milestone is a reviewed case sample. The
frontend can remain small and follow once the backend chat API is exposed.
