"""HTTP tool adapter for the LangGraph agent; no LLM is instantiated here."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen


def ask_the_oracle(base_url: str, *, merchant_id: str, customer_id: str,
                   coupon_code: str, cart_total_cents: int,
                   attempt_id: str | None = None,
                   as_of: str | None = None) -> dict:
    payload = {"merchant_id": merchant_id, "customer_id": customer_id,
               "coupon_code": coupon_code, "cart_total_cents": cart_total_cents,
               "attempt_id": attempt_id, "as_of": as_of}
    request = Request(base_url.rstrip("/") + "/oracle/check",
                      data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=5) as response:
        return json.load(response)
