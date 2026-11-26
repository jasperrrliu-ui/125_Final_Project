# Ask the Oracle: local prototype

This is a **synthetic** marketplace coupon service. ABCD does not provide coupon records, merchant APIs, or Oracle replies. The fixture is checked in as code; its runtime SQLite databases are generated locally.

## What is implemented

```text
Agent check_coupon tool -> POST /oracle/check -> platform Oracle
                                  -> HMAC request hash + cache
                                  -> mock merchant gateway -> merchant_coupons.sqlite
                                  -> checkout_attempts in platform_oracle.sqlite
```

The merchant database owns the codes, issue/expiry dates, owners and basket minimums. The platform database owns checkout attempts, query cache and merchant-call log. The platform never treats the merchant coupon table as part of its customer/order database. `MerchantGateway.validate()` is the simulated external API boundary; replacing that one method with an HTTP client is the later production-like step.

The Oracle compares the merchant's eligibility verdict with the platform's checkout failure. A coupon that is valid according to the merchant but rejected by the platform with `COUPON_REJECTED` yields `platform_rejected_valid_coupon`. A payment decline is not labeled as a coupon error. A missing checkout attempt yields `need_checkout_attempt`; merchant outage yields `merchant_unavailable`, never a guessed verdict. This prototype **does not issue replacement coupons**.

The HMAC key covers merchant ID, customer ID, normalized coupon code and basket total. The cache holds the merchant reply for up to 5 minutes when valid and 30 seconds for invalid results, never beyond the coupon expiration. An attempt ID is not part of the merchant-cache key: the same merchant reply can be reused while each checkout attempt is checked separately. The platform SQLite write lock prevents duplicate mock calls from concurrent identical requests in this local demo. For a real remote merchant API, use a distributed lock and invalidation when merchant rules change; a short TTL alone permits temporary staleness.

## Run

From the repository root:

```powershell
python -m agent.oracle_demo.init_demo
python -m unittest -v agent.oracle_demo.test_core agent.retail_tools.test_check_coupon
```

The tests need only Python's standard library. They also run a local HTTP server and POST to it, demonstrating the tool boundary without an LLM or paid merchant API. The agent reaches this route through `agent/retail_tools/check_coupon.py`.

HTTP server (`GET /health` and `POST /oracle/check`, standard library only):

```powershell
$env:ORACLE_CACHE_SECRET='local-demo-key-change-me'
python -m agent.oracle_demo.stdlib_api
```

Example request (in a second terminal) with body
`{"merchant_id": "M-101", "customer_id": "C0001", "coupon_code": "GOOD20", "cart_total_cents": 7500, "attempt_id": "ATT-PLATFORM-1", "as_of": "2026-11-01T12:00:00+00:00"}`
posted to `http://127.0.0.1:8000/oracle/check`.

First call has `cache_hit: false`; the same second call has `cache_hit: true`, with one row in `merchant_call_log`. Try `OLD20` / `ATT-EXPIRED-1` for expiry, `USED10` for redemption, `BASKET15` with a basket below 10000 cents, and `DOWN` for merchant outage.

`as_of` is an explicit simulation clock. A production endpoint would take server time and should not accept this client override. The demo coupon dates are fixed around 2026-11-01 to make tests deterministic.

## What an agent would do

The agent calls this tool only after identifying the merchant, customer, coupon code and relevant basket or checkout attempt. If any required identifier is missing, it asks the customer first. The HTTP adapter is used by the LangGraph workflow. Its decisions have **not** been evaluated against reviewed ABCD gold labels.
