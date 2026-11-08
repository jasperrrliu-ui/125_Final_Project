# Simulated action ledger

This is an **internal recording component**, not an agent tool exposed to an LLM. It stores what the agent proposed and whether a trusted policy gate allowed a simulated action. It never sends a refund, changes an order, issues a coupon, or writes to `synthetic_retail.sqlite`.

The separate database path is set by the caller; the CLI uses `outputs/retail_graph_demo/actions.sqlite`. `ActionLedger` creates an append-only `action_events` table when initialized. Each event has a conversation ID, stable request ID, intent, action type, customer/order IDs, validated payload, policy rule and version, outcome, reason, and timestamp. `list_conversation()` returns the payload for CP2 inspection but omits the internal request hash. A repeated request ID with identical content returns the original event; changed content with the same ID is rejected.

Supported simulated actions: `initiate_return`, `refund_item`,
`cancel_item_and_refund`, `reschedule_delivery`, and `credit_shipping_fee`.
A separate ledger API can record a `handoff` outcome; the current graph records
routine handoffs in CP2 state rather than the action ledger. Coupon
issuance is absent because the platform-versus-merchant authority is not yet
resolved.

```text
LLM proposes action
    -> policy_gate.py returns Authorization
    -> ActionLedger checks proposal against Authorization
    -> outcome: simulated or blocked
    -> CP2 evaluation can inspect the recorded event
```

`policy_gate.py` is now a backend policy evaluator for simulated actions. It
re-reads the customer tier, order ownership, shipping status and item amount
from the read-only business database. It checks that the applicable rule ID
and conditions exist in the source-linked `policy_rules.json`, then builds the
Authorization. The ledger compares the **exact approved payload**, including
item and amount. The LLM must never construct Authorization or call the ledger
directly; the LangGraph workflow calls `PolicyGate.review_and_record`.

The source policy file is still marked draft. The gate authorizes only branches
with enough verified facts: returns for Gold or Silver/Bronze with recorded
original packaging plus confirmed street, city, state, ZIP code and return method;
cancellation for received orders and shipped Silver/Gold members (the source
wording for shipped Bronze is unresolved); upgrade for received orders (non-Gold requires
backend-confirmed $20 consent); downgrade for received orders. Shipped-member downgrade
cannot credit an unknown shipping fee. Other return branches need a receipt
source or precise date handling. Coupon issuance has no action authority yet.
All outcomes remain **simulation records**, never real order or payment changes.

The current CLI/test simulator supplies identity and order information through
chat. The full ABCD `scenario` is not included in the agent's prompt. A
synthetic account lookup alone is not production authentication.

Run the local tests from the workspace root:

```powershell
python -m unittest -v agent.action_log.test_action_ledger agent.action_log.test_policy_gate
```
