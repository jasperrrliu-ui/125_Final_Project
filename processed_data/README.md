# Processed data: from ABCD to an agent test environment

This directory contains **project transformations and simulations**, separate
from the unchanged files in [`raw_data/`](../raw_data/README.md). Start with
this page before using a JSONL file or SQLite database as agent evidence.

## The two branches

| Branch | Input | Output | What is original vs. constructed |
| --- | --- | --- | --- |
| [`customer_simulation/`](customer_simulation/README.md) | 1,148 ABCD scenarios and dialogues for the eight selected intents | 808 linked synthetic customers, 1,148 orders, 1,273 item rows, identity-rewritten dialogue, 32 combined-request prompts, SQLite | Membership level and each source order/item fact are preserved; customer identities, cross-conversation grouping, combined prompts, IDs, and DB relations are ours. |
| [`policy/`](policy/README.md) | `raw_data/guidelines.json`, plus general rules modeled on tau-bench | `policy_v1.json`, the single policy source for the engine and gate | Each rule records its source path; rule IDs, structured conditions, and assumptions (tagged `assumption`) are ours. |

`customer_simulation/synthetic_retail.sqlite` deliberately links several
source conversations to a new simulated customer so the agent can practice
account, order, and product queries. Its schema is
[`sql/synthetic_retail_schema.sql`](../sql/synthetic_retail_schema.sql).

## Exact transformation steps

1. Filter the original train/dev/test partitions to the eight intents:
   `return_size`, `return_color`, `return_stain`, `promo_code_invalid`,
   `promo_code_out_of_date`, `manage_cancel`, `manage_upgrade`, and
   `manage_downgrade`. This yields **1,148 conversations** (925/111/112 in
   original train/dev/test). Keep `convo_id` and split on every derived record.
2. For a second, agent-facing business simulation, group conversations only
   within the same original split and membership tier. Use a fixed seed
   (`1252026`) to allocate **808 distinct customer identities**: 548 have one
   request type, 180 have two, and 80 have three. The original eight-intent
   conversations used only **10 distinct scenario names**. We replace those
   reused names and identifiers with unique synthetic account IDs, usernames,
   emails (`example.test`), and test phone numbers. We do not randomly change
   the source membership tier or order facts.
3. Write **1,148 orders** and **1,273 item rows** from the ABCD `scenario`
   records, preserving missing values as SQL `NULL`. The agent-facing view
   links customers to orders and items. `source_cases` preserves the ABCD
   conversation ID and the source/simulation distinction.
4. Create **32 new combined-request opening messages** (24 two-intent, 8
   three-intent). These are synthetic prompts assembled from separate ABCD
   needs; they are not historical ABCD conversations. The identity-rewritten
   historical dialogues remain in `rewritten_conversations.jsonl` for source
   comparison, but their actions are not gold labels.
5. Transcribe the eight relevant subflows in `guidelines.json` into
   `policy/policy_v1.json`. Each rule stores its source path; additions not in
   the source are tagged `assumption` and await review.

The customer simulator flags **44** source dialogues with an email mentioned
in conversation that differs from the `scenario` email. It avoids those cases
in the 32 combined-request examples. It records **6** order-change cases with
missing source shipping status instead of inventing a status; other return and
promo scenarios also often lack shipping status because their source flows did
not need it. The system-queryable shipping status in the simulation is a
project choice: ABCD does not provide a live carrier API.

## What is in the customer SQLite database?

| Table/view | Main fields and source | Agent use |
| --- | --- | --- |
| `customers` | Synthetic `customer_id`, name, account ID, username, email, phone; original membership tier; original split | Locate a candidate account; demo identity is not real authentication. |
| `orders` | Original `order_id`, purchase date, shipping status, packaging, payment method, address values; synthetic customer foreign key; source conversation ID | Verify account/order ownership and obtain policy-relevant order facts. Missing source fields remain `NULL`. |
| `items` | Original product name, amount, brand, type, and image URL with project-created `item_index` | Identify which item and amount a proposed action concerns. |
| `source_cases` | Original conversation ID, split and subflow plus synthetic customer/order links and review flags | Audit lineage; this table is test metadata, not model-visible customer history. |
| `order_shipping_lookup` | Account ID + order ID + source shipping status | Demonstrates a read-only, scoped shipping lookup. |

The actual agent uses only selected read-only queries. It must not receive the
whole `scenario` as its prompt. The policy gate re-reads the SQLite facts
before recording a simulated action. The database does not contain verified
receipts, return-address consent, customer consent to a $20 charge, real
payment transactions, or a time series of shipment changes.

## Coupon data is a separate simulation

ABCD mentions an abstract `Ask the Oracle` action but supplies no structured
original coupon code, merchant database, checkout log, API response, or
replacement-code issuer. [`agent/oracle_demo/`](../agent/oracle_demo/README.md)
creates a mock merchant SQLite database and a separate platform SQLite
database under ignored `outputs/oracle_demo/`. The platform has checkout
attempts and a cache; the merchant has coupon eligibility rules. Those rows
are **project-created fixtures**, not joins or labels derived from ABCD cases.
Their runtime database files are regenerated by the Oracle initializer. The
agent's coupon wrapper checks a platform attempt and calls the local Oracle
HTTP API; it never issues a replacement code.

## Rebuild and audit

From the repository root, after installing dependencies:

```powershell
python scripts\build_synthetic_customers.py
python scripts\render_policy_doc.py   # after editing policy_v1.json
```

`customer_simulation/manifest.json` gives source hashes, counts, and review
flags. The policy JSON gives the exact evidence path of each rule. Rebuilding may rewrite generated files but must not
change the raw source. Any unexplained count/hash change needs investigation.

## Reference labels are not present yet

Historical ABCD conversation prefixes are not used as test cases: the agent
asks different questions in a different order than the original human agents,
so a replayed prefix does not match the agent's own state. Evaluation cases
will be built from the synthetic database (see
[`docs/WORKFLOW.md`](../docs/WORKFLOW.md) for the checkpoints). A reviewed
case set requires expected step/parameter and policy/decision labels and
human audit of model-assisted labeling. No JSONL or smoke-test result here should be
reported as that finished evaluation set.
