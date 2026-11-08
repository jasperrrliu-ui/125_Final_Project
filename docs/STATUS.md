# Project status (2026-11-08)

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
