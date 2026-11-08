# Agent backend

This package contains the current backend prototype. All business-facing
order/customer reads are scoped SQLite queries. Actions are written only to a
**simulation ledger**, after a separate policy check; no real retailer data is
changed.

| Module | Responsibility |
| --- | --- |
| [`retail_graph/`](retail_graph/README.md) | Six-node LangGraph conversation flow, state persistence, model adapter, CLI chat, and eight-intent smoke runner. |
| [`retail_tools/`](retail_tools/README.md) | Read-only account and order lookup, plus one coupon wrapper that looks up a platform attempt and calls the Oracle. |
| [`action_log/`](action_log/README.md) | Source-linked policy gate and append-only simulated action ledger. |
| [`oracle_demo/`](oracle_demo/README.md) | Mock merchant and platform coupon databases with FastAPI/stdlib HTTP endpoints. |

The compiled flow is `understand -> route -> query -> route` or
`understand -> route -> propose -> authorize -> route -> reply`. The model
interprets customer language and proposes an action. Python controls allowed
queries, missing-information branches, and final policy authorization.
LangGraph's SQLite checkpointer keeps a conversation separate by `thread_id`.
See [`docs/WORKFLOW.md`](../docs/WORKFLOW.md) for CP1/CP2 semantics and
[`docs/STATUS.md`](../docs/STATUS.md) for current limits.

From the repository root:

```powershell
python -m agent.retail_graph.smoke_agent --scripted
python -m unittest -q agent.oracle_demo.test_core agent.retail_tools.test_read_tools agent.retail_tools.test_check_coupon agent.action_log.test_action_ledger agent.action_log.test_policy_gate agent.retail_graph.test_graph
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
