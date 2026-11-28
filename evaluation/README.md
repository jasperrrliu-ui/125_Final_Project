# Evaluation cases

Test cases for the two checkpoints and for full conversations defined in
[`docs/WORKFLOW.md`](../docs/WORKFLOW.md). They are built from the synthetic
retail database, using only orders from the ABCD **test and dev** splits
(210 orders whose customer can be verified by email).

| File | Cases | Categories |
| --- | ---: | --- |
| `cases/cp1.jsonl` | 60 | opening message (24: each intent three ways), two requests in one message (6), request plus something out of scope (2), identity answers (8), order ID answers (8), which item (4), coupon code (4), correction after an offer (4) |
| `cases/cp2.jsonl` | 40 | one decision per policy rule plus a missing-record handoff (17), answers to the receipt question (8), choice and consent after an offer (15) |
| `cases/conversation.jsonl` | 20 | 16 single-request scenarios (two per intent: accept, refuse, withdraw, receipt yes/no, declined by policy) and 4 with two requests |

Every policy-v1 rule is covered by at least one CP2 case.

## How a checkpoint case works

A CP1 or CP2 case is a scripted **prefix** (customer messages with their
correct extractions) that brings the graph to a **stop point**, such as
`identity`, `order_id`, `facts:has_receipt`, or `choose_and_confirm`, plus
one new customer **message**. `harness.run_case` replays the prefix with its
scripted extractions, so the state is always the same, and sends only the
case message to the interpreter under test.

Expected results are not typed by hand. `build_cases.py` sends the case's
expected extraction (what a correct reading of the message gives) through
the real graph and stores the events it produces. Each case also states what
its author intended (for example "no action is written", or "the agent asks
for identity again"), and the build fails if the graph disagrees.

Main fields:

| Field | Meaning |
| --- | --- |
| `prefix`, `stop_point`, `message` | The scripted turns, where they stop, and the message under test. |
| `true_facts` | The facts of the case, including ones only the customer knows (`has_receipt`) and the Oracle verdict for promo cases. |
| `coupon_verdict` | The Oracle result the coupon tool returns in this case. |
| `expected.extraction` | A correct reading of the message, in the interpreter's output shape. |
| `expected.items` | Which order item each spoken item name refers to; score by the resolved item, not the words. |
| `expected.cp1` | The steps the agent takes this turn: tool and arguments, or what it asks. |
| `expected.cp2` | Decisions this turn: kind, rule, `facts_used`, options with payloads, or the authorization. |
| `expected.ledger`, `expected.action_written` | Ledger rows written this turn; whether an action was executed. |
| `label_status` | `derived_from_construction` (CP1) or `provisional_engine` (CP2 and conversations). |
| `notes` | Scoring notes, for example "expected order_id is the digits only". |

A conversation case gives a simulated customer a `customer_brief`, `persona`,
`goal`, `hidden_facts`, and `preference`, and expects the final ledger rows in
`expected.ledger`. `reference_script` is one correct path through the
conversation; the simulated customer may take a different one.

## Label status

* **CP1** expectations follow from how each message was written. A person
  should still read through the messages and their expected extractions.
* **CP2 and conversation** expectations come from the policy engine and are
  **provisional**. They become reference labels after a model that reads only
  `raw_data/guidelines.json` and `true_facts` gives its own decision and a
  person settles every disagreement (see `docs/WORKFLOW.md`, Labels).

## Commands

```powershell
python -m evaluation.build_cases        # rebuild the case files (deterministic)
python -m unittest -q evaluation.test_cases
```

`evaluation.test_cases` replays every case with its expected extraction. If a
change to the graph or to `policy_v1.json` makes it fail, the saved
expectations are out of date: rebuild, check the diff, and relabel what
changed.

## Not done yet

* A runner that sends each case message to the real model and a scorer that
  compares the result with `expected` (with the normalizations in `notes`).
* Reference labels for CP2 and conversations.
* The simulated customer for conversation cases.
