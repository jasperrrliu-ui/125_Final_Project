"""End-to-end coupon wrapper checks against the local Oracle HTTP service."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from agent.retail_tools.check_coupon import check_coupon

from agent.oracle_demo.core import DEMO_TIME, initialize_demo
from agent.oracle_demo.stdlib_api import make_server


class CouponToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        initialize_demo(self.folder / "merchant_coupons.sqlite",
                        self.folder / "platform_oracle.sqlite")
        self.server = make_server(port=0, data_dir=self.folder,
                                  secret=b"tool-test-key")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.platform_db = self.folder / "platform_oracle.sqlite"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def check(self, customer="C0001", code="OLD20"):
        return check_coupon(customer, code, oracle_url=self.url,
                            platform_db=self.platform_db, as_of=DEMO_TIME)

    def test_unique_expired_coupon_is_resolved(self):
        result = self.check()
        self.assertEqual(result, {"status": "resolved", "customer_right": False,
                                  "reason_code": "expired", "merchant_status": "expired"})

    def test_multiple_attempts_are_not_guessed(self):
        result = self.check(code="GOOD20")
        self.assertEqual(result["status"], "needs_more_information")
        self.assertIsNone(result["customer_right"])
        with closing(sqlite3.connect(self.platform_db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM merchant_call_log").fetchone()[0], 0)

    def test_no_attempt_and_wrong_customer(self):
        self.assertEqual(self.check(code="NOPE")["status"], "not_found")
        self.assertEqual(self.check(customer="C0002")["status"], "not_found")

    def test_platform_misrejection_after_unique_attempt(self):
        with closing(sqlite3.connect(self.platform_db)) as db, db:
            db.execute("DELETE FROM checkout_attempts WHERE attempt_id IN (?,?)",
                       ("ATT-SUCCESS-1", "ATT-PAYMENT-1"))
        result = self.check(code="GOOD20")
        self.assertEqual(result["status"], "resolved")
        self.assertTrue(result["customer_right"])
        self.assertEqual(result["reason_code"], "platform_rejected_valid_coupon")

    def test_merchant_outage_is_not_a_verdict(self):
        with closing(sqlite3.connect(self.platform_db)) as db, db:
            db.execute("INSERT INTO checkout_attempts VALUES (?,?,?,?,?,?,?)",
                       ("ATT-DOWN-1", "M-101", "C0001", "DOWN", 7500,
                        "rejected", "COUPON_REJECTED"))
        result = self.check(code="DOWN")
        self.assertEqual(result["status"], "source_unavailable")
        self.assertIsNone(result["customer_right"])


if __name__ == "__main__":
    unittest.main()
