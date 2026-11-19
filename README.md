# 125 Final Project: Retail Customer Service Agent

A course prototype of a policy-guided customer service agent for eight retail
request types. It uses [ABCD](https://github.com/asappresearch/abcd) as a source
of fictional conversations and policy text, then builds a separate, explicitly
simulated business database so the agent can query accounts, orders, products,
and coupon checkout attempts. No action changes a real store or payment system.

## Where to start

| Path | What it contains |
| --- | --- |
| [`raw_data/`](raw_data/README.md) | Unmodified ABCD files and source provenance. |
| [`processed_data/`](processed_data/README.md) | The complete raw-to-processed data lineage, case candidates, 808-customer simulation, SQLite schemas, and source-linked policy rules. |
| [`agent/`](agent/README.md) | LangGraph workflow, policy engine, read-only business tools, policy gate, simulated action ledger, and local coupon Oracle. |
| [`docs/POLICY_V1.md`](docs/POLICY_V1.md) | Policy v1 (generated from `processed_data/policy/policy_v1.json`, the single source of truth). |
| [`docs/WORKFLOW.md`](docs/WORKFLOW.md) | Current workflow and the two business evaluation checkpoints. |
| [`docs/STATUS.md`](docs/STATUS.md) | Implemented components, verified results, limitations, and next work. |

## Data path

```text
ABCD raw conversations + scenario facts + guidelines
    |-- select eight request types, preserve original train/dev/test splits
    |-- create conversation cutpoint candidates and isolated scenario fixtures
    |-- regroup 1,148 selected conversations into 808 simulated customers
    |     while retaining source IDs, splits, and membership levels
    |-- construct account/order/item SQLite tables and synthetic identity values
    |-- transcribe 22 source-linked policy rules from guidelines.json
    `-- policy v1: draft policy (not yet reviewed) built on those rules plus general
          rules (identity, confirmation, decline vs handoff)

Separate project simulation: platform checkout records + mock merchant coupons
    -> local Oracle HTTP service (ABCD does not provide these records)
```

The 8 smoke prompts and the 32 combined-request prompts are **synthetic test
inputs, not ABCD gold labels**. The larger cutpoint pools also have no reviewed
reference answers. See the [data README](processed_data/README.md) for exact
counts, field provenance, schema decisions, and known gaps.

## Run locally

Use Python 3.11+ from the repository root. On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,llm]"
.\.venv\Scripts\python -m agent.retail_graph.smoke_agent --scripted
.\.venv\Scripts\python -m unittest -q tests.test_prepared_cases agent.action_log.test_action_ledger agent.action_log.test_policy_gate agent.policy.test_engine agent.retail_tools.test_read_tools agent.retail_tools.test_check_coupon agent.retail_graph.test_graph
```

The scripted smoke runs ten multi-turn conversations covering all eight
intents through the compiled graph, the real policy engine and gate, actual
read-only SQLite tools, and a local Oracle HTTP server. It does **not**
measure LLM accuracy. The local Oracle and chat runtime databases are created
under ignored `outputs/`.

To rebuild the checked-in processed data from the original sources:

```powershell
.\.venv\Scripts\python scripts\prepare_test_cases.py
.\.venv\Scripts\python scripts\build_synthetic_customers.py
.\.venv\Scripts\python scripts\build_policy_rules.py raw_data\guidelines.json processed_data\policy\policy_rules.json
.\.venv\Scripts\python scripts\render_policy_doc.py   # after editing policy_v1.json
```

The real-model smoke uses `gpt-5.4-mini` by default. Keep API credentials out
of Git; `run_live_smoke.ps1` prompts for a key without saving it. The current
backend is a prototype, and its eight-case smoke results are not a substitute
for the planned 128-case reviewed evaluation.

## Current boundary

The core backend runs end to end on synthetic data. It verifies identity
(email, or name + zip), collects the order, item, or coupon, looks up records,
asks only for the facts the policy still needs, and then declines with a
reason, hands off, or offers options. Nothing is recorded until the customer
picks an option and says yes; the policy gate then re-checks it. It has no
customer-facing web UI, Langfuse tracing, reviewed gold set, real
authentication, or real coupon issuer. See [project status](docs/STATUS.md).
