from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from agent.oracle_demo.agent_tool import ask_the_oracle
from agent.oracle_demo.core import CheckRequest, DEMO_TIME, OracleService, initialize_demo
from agent.oracle_demo.stdlib_api import make_server


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        self.merchant = folder / "merchant.sqlite"
        self.platform = folder / "platform.sqlite"
        initialize_demo(self.merchant, self.platform)
        self.oracle = OracleService(self.merchant, self.platform, b"unit-test-secret")

    def tearDown(self):
        self.temp.cleanup()

    def req(self, code="GOOD20", amount=7500, attempt="ATT-PLATFORM-1", customer="C0001", when=DEMO_TIME):
        return CheckRequest("M-101", customer, code, amount, attempt, when)

    def test_valid_coupon_rejected_by_platform_and_cache(self):
        first = self.oracle.check(self.req())
        second = self.oracle.check(self.req())
        self.assertEqual(first["decision"], "platform_rejected_valid_coupon")
        self.assertTrue(first["customer_right"])
        self.assertEqual(first["fault_party"], "platform")
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(self.oracle.merchant_call_count(), 1)
        with closing(sqlite3.connect(self.platform)) as db:
            cache = db.execute("SELECT request_hash,response_json FROM merchant_reply_cache").fetchone()
        self.assertEqual(len(cache[0]), 64)
        self.assertNotIn("GOOD20", cache[1])

    def test_changed_context_and_cache_expiry_call_merchant_again(self):
        self.oracle.check(self.req(attempt=None))
        changed_cart = self.oracle.check(self.req(amount=6000, attempt=None))
        changed_customer = self.oracle.check(self.req(customer="C0002", attempt=None))
        later = self.oracle.check(self.req(attempt=None, when="2026-11-01T12:06:00+00:00"))
        self.assertFalse(changed_cart["cache_hit"])
        self.assertEqual(changed_customer["merchant_status"], "wrong_customer")
        self.assertFalse(later["cache_hit"])
        self.assertEqual(self.oracle.merchant_call_count(), 4)

    def test_expired_and_redeemed_are_not_platform_faults(self):
        expired = self.oracle.check(self.req(code="OLD20", attempt="ATT-EXPIRED-1"))
        redeemed = self.oracle.check(self.req(code="USED10", attempt=None))
        self.assertEqual(expired["decision"], "expired")
        self.assertFalse(expired["customer_right"])
        self.assertEqual(redeemed["decision"], "already_redeemed")
        self.assertFalse(redeemed["customer_right"])

    def test_payment_failure_is_not_called_a_coupon_error(self):
        result = self.oracle.check(self.req(attempt="ATT-PAYMENT-1"))
        self.assertEqual(result["decision"], "unrelated_checkout_failure")
        self.assertFalse(result["customer_right"])

    def test_missing_or_mismatched_checkout_evidence(self):
        no_attempt = self.oracle.check(self.req(attempt=None))
        self.assertEqual(no_attempt["decision"], "need_checkout_attempt")
        calls = self.oracle.merchant_call_count()
        mismatch = self.oracle.check(self.req(amount=9000))
        self.assertEqual(mismatch["decision"], "invalid_attempt_context")
        self.assertEqual(self.oracle.merchant_call_count(), calls)

    def test_merchant_outage_is_not_cached_as_a_verdict(self):
        first = self.oracle.check(self.req(code="DOWN", attempt=None))
        second = self.oracle.check(self.req(code="DOWN", attempt=None))
        self.assertEqual(first["decision"], "merchant_unavailable")
        self.assertIsNone(first["customer_right"])
        self.assertEqual(second["decision"], "merchant_unavailable")
        self.assertEqual(self.oracle.merchant_call_count(), 2)

    def test_concurrent_identical_checks_share_one_mock_api_call(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(self.oracle.check,[self.req() for _ in range(4)]))
        self.assertEqual(self.oracle.merchant_call_count(), 1)
        self.assertEqual(sum(not r["cache_hit"] for r in results), 1)

    def test_agent_http_tool_calls_the_oracle(self):
        server = make_server(port=0, data_dir=Path(self.temp.name), secret=b"unit-test-secret")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            args = dict(merchant_id="M-101", customer_id="C0001", coupon_code="GOOD20",
                        cart_total_cents=7500, attempt_id="ATT-PLATFORM-1", as_of=DEMO_TIME)
            first = ask_the_oracle(url, **args)
            second = ask_the_oracle(url, **args)
            self.assertEqual(first["decision"], "platform_rejected_valid_coupon")
            self.assertTrue(second["cache_hit"])
            with closing(sqlite3.connect(Path(self.temp.name) / "platform_oracle.sqlite")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM merchant_call_log").fetchone()[0], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
