# Processed data: from ABCD to an agent test environment

This directory contains **project transformations and simulations**, separate
from the unchanged files in [`raw_data/`](../raw_data/README.md). Start with
this page before using a JSONL file or SQLite database as agent evidence.

## The three branches

| Branch | Input | Output | What is original vs. constructed |
| --- | --- | --- | --- |
| [`abcd_cases/`](abcd_cases/README.md) | `abcd_v1.1.json.gz`, `kb.json` | 1,148 selected conversations, isolated scenario facts, 4,428 tool-cut candidates, 847 policy-cut candidates, SQLite | Dialogue, scenario values, and historical actions come from ABCD; turn indexing, selection, cutpoints, and relational schema are ours. |
| [`customer_simulation/`](customer_simulation/README.md) | The same 1,148 selected ABCD scenarios and dialogues | 808 linked synthetic customers, 1,148 orders, 1,273 item rows, identity-rewritten dialogue, 32 combined-request prompts, SQLite | Membership level and each source order/item fact are preserved; customer identities, cross-conversation grouping, combined prompts, IDs, and DB relations are ours. |
| [`policy/`](policy/README.md) | `raw_data/guidelines.json`, checked against `kb.json` and `ontology.json` | 22 source-linked rules in `policy_rules.json` | Source sentences are copied as evidence; rule IDs, structured conditions, and conservative implementation choices are ours. |

The two SQLite databases have **different purposes**. `abcd_cases/scenario_fixtures.sqlite`
keeps every source scenario separate under its original `convo_id`. It does
not assert that repeated ABCD names identify the same customer.
`customer_simulation/synthetic_retail.sqlite` deliberately links several
source conversations to a new simulated customer so the agent can practice
account, order, and product queries. The schemas are respectively
[`sql/scenario_schema.sql`](../sql/scenario_schema.sql) and
[`sql/synthetic_retail_schema.sql`](../sql/synthetic_retail_schema.sql).

## Exact transformation steps

1. Filter the original train/dev/test partitions to the eight intents:
   `return_size`, `return_color`, `return_stain`, `promo_code_invalid`,
   `promo_code_out_of_date`, `manage_cancel`, `manage_upgrade`, and
   `manage_downgrade`. This yields **1,148 conversations** (925/111/112 in
   original train/dev/test). Keep `convo_id` and split on every derived record.
2. Normalize turns and copy the `scenario.personal`, `scenario.order`, and
   `scenario.product` background into case-isolated JSONL and SQLite. Select
   candidate cutpoints before observed actions and after policy-relevant
   evidence. The **4,428 tool** and **847 policy** rows are candidate moments,
   not independent customers or verified correct answers.
3. For a second, agent-facing business simulation, group conversations only
   within the same original split and membership tier. Use a fixed seed
   (`1252026`) to allocate **808 distinct customer identities**: 548 have one
   request type, 180 have two, and 80 have three. The original eight-intent
   conversations used only **10 distinct scenario names**. We replace those
   reused names and identifiers with unique synthetic account IDs, usernames,
   emails (`example.test`), and test phone numbers. We do not randomly change
   the source membership tier or order facts.
4. Write **1,148 orders** and **1,273 item rows** from the ABCD `scenario`
   records, preserving missing values as SQL `NULL`. The agent-facing view
   links customers to orders and items. `source_cases` preserves the ABCD
   conversation ID and the source/simulation distinction.
5. Create **32 new combined-request opening messages** (24 two-intent, 8
   three-intent). These are synthetic prompts assembled from separate ABCD
   needs; they are not historical ABCD conversations. The identity-rewritten
   historical dialogues remain in `rewritten_conversations.jsonl` for source
   comparison, but their actions are not gold labels.
6. Transcribe the eight relevant subflows in `guidelines.json` into 22 rules.
   Each rule stores its source path and original policy text. Ambiguous policy
   wording stays listed as unresolved, rather than being silently invented.

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
python scripts\prepare_test_cases.py
python scripts\build_synthetic_customers.py
python scripts\build_policy_rules.py raw_data\guidelines.json processed_data\policy\policy_rules.json
python -m unittest -q tests.test_prepared_cases
```

`abcd_cases/manifest.json` and `customer_simulation/manifest.json` give source
hashes, counts, and review flags. The policy JSON gives the guideline hash
and exact evidence paths. Rebuilding may rewrite generated files but must not
change the raw source. Any unexplained count/hash change needs investigation.

## Reference labels are not present yet

The original representative actions in `delexed.targets` are useful **label
candidates**, not automatic ground truth for our new tools or for a chosen
earlier cutoff. The two checkpoint definitions are in
[`docs/WORKFLOW.md`](../docs/WORKFLOW.md). A planned 128-case gold set requires
case review, expected tool/parameter and policy/action labels, and human
audit of model-assisted labeling. No JSONL or smoke-test result here should be
reported as that finished evaluation set.
