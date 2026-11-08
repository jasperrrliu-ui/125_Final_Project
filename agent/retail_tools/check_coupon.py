"""One simple agent-facing coupon lookup over platform records and Oracle HTTP.

The agent supplies a customer ID from lookup_customer and the customer's
original coupon code. Merchant ID, basket amount and attempt ID come only from
the platform's checkout record, never from the LLM.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_PLATFORM_DB = (
    Path(__file__).resolve().parents[2]
    / "outputs" / "oracle_demo" / "platform_oracle.sqlite"
)


def check_coupon(customer_id: str, coupon_code: str, *,
                 oracle_url: str = "http://127.0.0.1:8000",
                 platform_db: Path = DEFAULT_PLATFORM_DB,
                 as_of: str | None = None) -> dict:
    """Find one checkout attempt, ask Oracle, and return a small verdict.

    oracle_url, platform_db and as_of are backend/test configuration, not
    parameters the model should choose. as_of is only for a fixed demo clock.
    """
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("customer_id must be non-empty")
    if not isinstance(coupon_code, str) or not coupon_code.strip():
        raise ValueError("coupon_code must be non-empty")
    customer_id = customer_id.strip().upper()
    coupon_code = coupon_code.strip().upper()
    try:
        with closing(sqlite3.connect(
            f"file:{Path(platform_db).resolve().as_posix()}?mode=ro", uri=True
        )) as db:
            db.row_factory = sqlite3.Row
            attempts = db.execute(
                "SELECT attempt_id, merchant_id, customer_id, coupon_code, "
                "cart_total_cents FROM checkout_attempts "
                "WHERE customer_id=? AND upper(coupon_code)=? LIMIT 2",
                (customer_id, coupon_code),
            ).fetchall()
    except sqlite3.Error:
        return {"status": "source_unavailable", "customer_right": None}

    if not attempts:
        return {"status": "not_found", "customer_right": None}
    if len(attempts) > 1:
        return {"status": "needs_more_information",
                "reason_code": "multiple_checkout_attempts", "customer_right": None}

    attempt = attempts[0]
    payload = {
        "attempt_id": attempt["attempt_id"],
        "merchant_id": attempt["merchant_id"],
        "customer_id": attempt["customer_id"],
        "coupon_code": attempt["coupon_code"],
        "cart_total_cents": attempt["cart_total_cents"],
        "as_of": as_of,
    }
    request = Request(
        oracle_url.rstrip("/") + "/oracle/check",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return {"status": "source_unavailable", "customer_right": None}

    if not isinstance(result, dict):
        return {"status": "source_unavailable", "customer_right": None}
    decision = result.get("decision")
    if decision in {"merchant_unavailable", "invalid_attempt_context", "need_checkout_attempt"}:
        return {"status": "source_unavailable", "customer_right": None,
                "reason_code": decision}
    if decision is None or type(result.get("customer_right")) is not bool:
        return {"status": "source_unavailable", "customer_right": None}
    return {
        "status": "resolved",
        "customer_right": result["customer_right"],
        "reason_code": decision,
        "merchant_status": result.get("merchant_status"),
    }
