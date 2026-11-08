# Customer, order, and coupon read tools

These are two read-only functions used after the customer provides an account
clue and, for an order request, an order ID. They are not a chatbot or an
identity-authentication system. The LangGraph workflow decides when to ask
for missing clues and when to call the functions.

```python
customer = lookup_customer("account_id", "ACCT-000001")
# {'status': 'found', 'customer_id': 'C0001', 'member_level': 'silver'}

order = lookup_order(customer["customer_id"], "5199420931")
# Returns the order's purchase date, shipping status, packaging, items,
# and an explicit list of unavailable facts.
```

An order belonging to another customer returns `not_found`, just like an
unknown order. Missing shipping status remains `null`; the tool does not guess
that the order has not shipped. No raw customer profile, test-case ID, or
address is returned. The functions use parameterized queries and open the
fixture in SQLite read-only mode.

Run from the repository root: `python -m unittest -v agent.retail_tools.test_read_tools`.

## Coupon lookup

`check_coupon(customer_id, coupon_code)` is the single agent-facing coupon
operation. It searches the platform's checkout attempts for that customer and
code. One match is sent to the local Oracle HTTP endpoint, which checks the
merchant's simulated coupon rules and compares the two sides. Zero matches
return `not_found`; multiple matches return `needs_more_information` without
calling the merchant. A server or merchant failure returns
`source_unavailable`, never a guessed yes/no. It does not issue a new coupon.

`oracle_url`, `platform_db`, and the fixed test clock `as_of` are backend
configuration; they are not inputs that the LLM should fill. The same
`POST /oracle/check` route is available through the optional FastAPI wrapper
or the standard-library HTTP server. Tests use the latter so no external
package installation is required.

The current platform fixture has four demonstration checkout attempts, not
attempts tied to the ABCD source cases. `GOOD20` has three attempts, so the
combined tool deliberately asks for more information instead of choosing one.
The fixture will need attempt times or another customer-visible clue before
that ambiguity can be resolved in a chat.

Run from the repository root: `python -m unittest -v agent.retail_tools.test_check_coupon`.
