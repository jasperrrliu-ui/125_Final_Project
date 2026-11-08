"""A small, deterministic marketplace coupon Oracle with a mock merchant boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3


DEMO_TIME = "2026-11-01T12:00:00+00:00"


@contextmanager
def connection(path: Path):
    db = sqlite3.connect(path, timeout=30)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    return result.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class CheckRequest:
    merchant_id: str
    customer_id: str
    coupon_code: str
    cart_total_cents: int
    attempt_id: str | None = None
    as_of: str | None = None  # Fixed clock for the local simulation.

    def normalized(self) -> "CheckRequest":
        if self.cart_total_cents < 0:
            raise ValueError("cart_total_cents must be nonnegative")
        merchant_id = self.merchant_id.strip().upper()
        customer_id = self.customer_id.strip().upper()
        coupon_code = self.coupon_code.strip().upper()
        attempt_id = self.attempt_id.strip().upper() if self.attempt_id else None
        if not all((merchant_id, customer_id, coupon_code)):
            raise ValueError("merchant_id, customer_id and coupon_code are required")
        if self.as_of is not None:
            parse_time(self.as_of)
        return CheckRequest(merchant_id, customer_id, coupon_code,
                            self.cart_total_cents, attempt_id, self.as_of)


def initialize_demo(merchant_db: Path, platform_db: Path) -> None:
    """Create separate merchant and platform stores with a few auditable fixtures."""
    merchant_db.parent.mkdir(parents=True, exist_ok=True)
    platform_db.parent.mkdir(parents=True, exist_ok=True)
    with connection(merchant_db) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS merchants (
            merchant_id TEXT PRIMARY KEY, name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS coupons (
            merchant_id TEXT NOT NULL REFERENCES merchants(merchant_id),
            code TEXT NOT NULL, owner_customer_id TEXT NOT NULL,
            issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            min_cart_cents INTEGER NOT NULL, state TEXT NOT NULL
                CHECK(state IN ('active','redeemed')),
            discount_percent INTEGER NOT NULL, simulated_api_error TEXT,
            PRIMARY KEY(merchant_id,code)
        );
        """)
        db.execute("INSERT OR IGNORE INTO merchants VALUES (?,?)", ("M-101", "Demo Merchant"))
        coupons = [
            ("M-101", "GOOD20", "C0001", "2026-10-29T00:00:00+00:00", "2026-11-08T00:00:00+00:00", 5000, "active", 20, None),
            ("M-101", "OLD20", "C0001", "2026-10-01T00:00:00+00:00", "2026-10-31T00:00:00+00:00", 5000, "active", 20, None),
            ("M-101", "USED10", "C0001", "2026-10-29T00:00:00+00:00", "2026-11-08T00:00:00+00:00", 0, "redeemed", 10, None),
            ("M-101", "BASKET15", "C0001", "2026-10-29T00:00:00+00:00", "2026-11-08T00:00:00+00:00", 10000, "active", 15, None),
            ("M-101", "OTHER20", "C0002", "2026-10-29T00:00:00+00:00", "2026-11-08T00:00:00+00:00", 0, "active", 20, None),
            ("M-101", "DOWN", "C0001", "2026-10-29T00:00:00+00:00", "2026-11-08T00:00:00+00:00", 0, "active", 20, "timeout"),
        ]
        db.executemany("INSERT OR IGNORE INTO coupons VALUES (?,?,?,?,?,?,?,?,?)", coupons)
    with connection(platform_db) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS checkout_attempts (
            attempt_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL,
            customer_id TEXT NOT NULL, coupon_code TEXT NOT NULL,
            cart_total_cents INTEGER NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('accepted','rejected')),
            failure_code TEXT
        );
        CREATE TABLE IF NOT EXISTS merchant_reply_cache (
            request_hash TEXT PRIMARY KEY, response_json TEXT NOT NULL,
            checked_at TEXT NOT NULL, cache_until TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS merchant_call_log (
            call_id INTEGER PRIMARY KEY AUTOINCREMENT, request_hash TEXT NOT NULL,
            merchant_id TEXT NOT NULL, called_at TEXT NOT NULL, outcome TEXT NOT NULL
        );
        """)
        attempts = [
            ("ATT-PLATFORM-1", "M-101", "C0001", "GOOD20", 7500, "rejected", "COUPON_REJECTED"),
            ("ATT-EXPIRED-1", "M-101", "C0001", "OLD20", 7500, "rejected", "COUPON_REJECTED"),
            ("ATT-SUCCESS-1", "M-101", "C0001", "GOOD20", 7500, "accepted", None),
            ("ATT-PAYMENT-1", "M-101", "C0001", "GOOD20", 7500, "rejected", "PAYMENT_DECLINED"),
        ]
        db.executemany("INSERT OR IGNORE INTO checkout_attempts VALUES (?,?,?,?,?,?,?)", attempts)


class MerchantGateway:
    """Mock boundary. In production this method becomes the merchant HTTP client."""

    def __init__(self, merchant_db: Path):
        self.merchant_db = merchant_db

    def validate(self, req: CheckRequest, now: datetime) -> dict:
        with connection(self.merchant_db) as db:
            merchant = db.execute("SELECT 1 FROM merchants WHERE merchant_id=?", (req.merchant_id,)).fetchone()
            row = db.execute("SELECT owner_customer_id,issued_at,expires_at,min_cart_cents,state,discount_percent,simulated_api_error "
                             "FROM coupons WHERE merchant_id=? AND code=?", (req.merchant_id, req.coupon_code)).fetchone()
        if not merchant:
            return {"status": "unknown_merchant", "expires_at": None}
        if not row:
            return {"status": "unknown_code", "expires_at": None}
        owner, issued, expires, minimum, state, discount, api_error = row
        if api_error:
            raise ConnectionError(f"mock merchant API {api_error}")
        if now < parse_time(issued):
            status = "not_yet_issued"
        elif now >= parse_time(expires):
            status = "expired"
        elif state == "redeemed":
            status = "already_redeemed"
        elif owner != req.customer_id:
            status = "wrong_customer"
        elif req.cart_total_cents < minimum:
            status = "minimum_not_met"
        else:
            status = "valid"
        return {"status": status, "expires_at": expires,
                "discount_percent": discount if status == "valid" else None}


class OracleService:
    def __init__(self, merchant_db: Path, platform_db: Path, cache_secret: bytes,
                 gateway: MerchantGateway | None = None):
        if not cache_secret:
            raise ValueError("cache_secret must not be empty")
        self.platform_db = platform_db
        self.cache_secret = cache_secret
        self.gateway = gateway or MerchantGateway(merchant_db)

    def _hash(self, req: CheckRequest) -> str:
        # The checkout attempt is intentionally excluded: merchant eligibility
        # can be reused, while each platform checkout outcome is read fresh.
        payload = {"merchant_id": req.merchant_id, "customer_id": req.customer_id,
                   "coupon_code": req.coupon_code, "cart_total_cents": req.cart_total_cents}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self.cache_secret, canonical, hashlib.sha256).hexdigest()

    def merchant_call_count(self) -> int:
        with connection(self.platform_db) as db:
            return db.execute("SELECT COUNT(*) FROM merchant_call_log").fetchone()[0]

    def check(self, request: CheckRequest) -> dict:
        req = request.normalized()
        now = parse_time(req.as_of) if req.as_of else datetime.now(timezone.utc)
        request_hash = self._hash(req)
        with connection(self.platform_db) as db:
            # Serialize cache misses across local processes in this SQLite demo.
            db.execute("BEGIN IMMEDIATE")
            attempt = None
            if req.attempt_id:
                attempt = db.execute("SELECT merchant_id,customer_id,coupon_code,cart_total_cents,outcome,failure_code "
                                     "FROM checkout_attempts WHERE attempt_id=?", (req.attempt_id,)).fetchone()
                if not attempt or attempt[:4] != (req.merchant_id, req.customer_id, req.coupon_code, req.cart_total_cents):
                    return {"decision": "invalid_attempt_context", "customer_right": None,
                            "fault_party": None, "merchant_status": None,
                            "cache_hit": False, "request_hash": request_hash}
            cached = db.execute("SELECT response_json,checked_at,cache_until FROM merchant_reply_cache "
                                "WHERE request_hash=?", (request_hash,)).fetchone()
            hit = bool(cached and parse_time(cached[1]) <= now < parse_time(cached[2]))
            if hit:
                merchant_reply = json.loads(cached[0])
            else:
                try:
                    merchant_reply = self.gateway.validate(req, now)
                    outcome = "ok"
                except ConnectionError:
                    merchant_reply = None
                    outcome = "unavailable"
                db.execute("INSERT INTO merchant_call_log(request_hash,merchant_id,called_at,outcome) VALUES (?,?,?,?)",
                           (request_hash, req.merchant_id, iso(now), outcome))
                if merchant_reply is not None:
                    # Short negative cache; positive cache never passes coupon expiry.
                    seconds = 300 if merchant_reply["status"] == "valid" else 30
                    until = now + timedelta(seconds=seconds)
                    expiry = merchant_reply.get("expires_at")
                    if expiry and now < parse_time(expiry):
                        until = min(until, parse_time(expiry))
                    db.execute("INSERT OR REPLACE INTO merchant_reply_cache VALUES (?,?,?,?)",
                               (request_hash, json.dumps(merchant_reply), iso(now), iso(until)))
        if merchant_reply is None:
            return {"decision": "merchant_unavailable", "customer_right": None,
                    "fault_party": None, "merchant_status": None,
                    "cache_hit": False, "request_hash": request_hash}
        status = merchant_reply["status"]
        if status != "valid":
            decision, customer_right, fault_party = status, False, None
        elif attempt is None:
            decision, customer_right, fault_party = "need_checkout_attempt", None, None
        elif attempt[4] == "rejected" and attempt[5] == "COUPON_REJECTED":
            decision, customer_right, fault_party = "platform_rejected_valid_coupon", True, "platform"
        elif attempt[4] == "rejected":
            decision, customer_right, fault_party = "unrelated_checkout_failure", False, None
        else:
            decision, customer_right, fault_party = "coupon_accepted", False, None
        return {"decision": decision, "customer_right": customer_right,
                "fault_party": fault_party, "merchant_status": status,
                "cache_hit": hit, "request_hash": request_hash}
