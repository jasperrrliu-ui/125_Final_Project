# Agent backend

This package contains the current backend prototype. All business-facing
order/customer reads are scoped SQLite queries. Actions are written only to a
**simulation ledger**, after a separate policy check; no real retailer data is
changed.

| Module | Responsibility |
| --- | --- |
| [`policy/`](policy/engine.py) | Policy engine: the only reader of `processed_data/policy/policy_v1.json`. Returns needed facts, or decline / handoff / options with exact payloads. |
| [`retail_graph/`](retail_graph/README.md) | Five-node LangGraph conversation flow, state persistence, model adapter, CLI chat, and multi-turn smoke runner. |
| [`retail_tools/`](retail_tools/README.md) | Read-only identity verification and order lookup, plus one coupon wrapper that looks up a platform attempt and calls the Oracle. |
| [`action_log/`](action_log/README.md) | Policy gate (re-reads records, re-runs the policy) and append-only simulated action ledger. |
| [`oracle_demo/`](oracle_demo/README.md) | Mock merchant and platform coupon databases with FastAPI/stdlib HTTP endpoints. |

The compiled flow is `understand -> route -> (query -> route)* ->
(authorize -> route)* -> reply`. The model only reads what the customer said.
The policy engine decides what is needed and what is allowed; the gate checks
it again before the ledger records anything. See
[`docs/WORKFLOW.md`](../docs/WORKFLOW.md) and
[`docs/POLICY_V1.md`](../docs/POLICY_V1.md).

From the repository root:

```powershell
python -m agent.retail_graph.smoke_agent --scripted
python -m unittest -q agent.policy.test_engine agent.oracle_demo.test_core agent.retail_tools.test_read_tools agent.retail_tools.test_check_coupon agent.action_log.test_action_ledger agent.action_log.test_policy_gate agent.retail_graph.test_graph
```

The Oracle demo can be started separately for interactive chat:

```powershell
python -m agent.oracle_demo.init_demo
python -m uvicorn agent.oracle_demo.api:app --host 127.0.0.1 --port 8000
```

In another terminal, set a local `OPENAI_API_KEY`, then run
`python -m agent.retail_graph.run_chat --thread-id demo-1`. `gpt-5.4-mini` is
the default model; `RETAIL_AGENT_MODEL` can override it. Credentials and
runtime databases are not committed.
