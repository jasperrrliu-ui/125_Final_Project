# Policy gate and simulated action ledger

These are **internal backend components**, not tools exposed to the model.
Nothing here sends a refund, changes an order, issues a coupon, or writes to
`synthetic_retail.sqlite`.

## Policy gate (`policy_gate.py`)

The graph calls `PolicyGate.authorize(AuthorizationRequest)` only after the
customer picked an offered option and said yes. The gate:

1. checks the intent is in policy scope and the customer confirmed;
2. re-reads the customer's tier and, when the request has an order, the order
   and item from the read-only business database (another customer's order
   is `order_not_verified`);
3. rebuilds the facts with the same adapters the graph uses
   (`agent/policy/records.py`) and runs the same policy engine;
4. allows the action only if the chosen option is still in the engine's
   options, with an identical action and payload.

The ledger then compares the requested option with the gate's authorization
and records `simulated` or `blocked`. A tampered amount, a changed customer
statement, or an option the policy no longer allows is blocked.

`record_outcome` records requests that end without an action: `declined`
(policy says no), `customer_declined` (customer said no or withdrew), and
`handoff`.

## Ledger (`action_ledger.py`)

Append-only SQLite table `action_events_v2` (renamed from `action_events`
so older local ledgers do not conflict with the new outcome values). One row
per request ID; an identical retry returns the original row, a changed retry
is rejected.

Actions: `initiate_return`, `refund_item`, `cancel_item_and_refund`,
`reschedule_delivery`, `credit_shipping_fee`, `issue_promo_code` (not tied to
an order).

Run from the repository root:

```powershell
python -m unittest -v agent.action_log.test_action_ledger agent.action_log.test_policy_gate
```
