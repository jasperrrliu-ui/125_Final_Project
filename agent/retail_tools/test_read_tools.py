"""Small integration checks against the synthetic business database."""

import unittest

from agent.retail_tools.read_tools import DEFAULT_DB, lookup_customer, lookup_order


class ReadToolTests(unittest.TestCase):
    def test_account_then_own_order(self):
        customer = lookup_customer("account_id", "ACCT-000001")
        self.assertEqual(customer["status"], "found")
        self.assertEqual(customer["customer_id"], "C0001")
        order = lookup_order(customer["customer_id"], "5199420931")
        self.assertEqual(order["status"], "found")
        self.assertEqual(order["facts"]["items"][0]["amount_cents"], 5900)
        self.assertIn("shipping_status", order["missing_facts"])

    def test_order_owned_by_another_customer_is_not_exposed(self):
        self.assertEqual(lookup_order("C0002", "5199420931"), {"status": "not_found"})
        self.assertEqual(lookup_order("C0002", "NO-SUCH-ORDER"), {"status": "not_found"})

    def test_unknown_customer_and_invalid_identifier(self):
        self.assertEqual(lookup_customer("account_id", "ACCT-UNKNOWN"), {"status": "not_found"})
        with self.assertRaises(ValueError):
            lookup_customer("arbitrary_sql_column", "anything")

    def test_lookup_does_not_modify_fixture(self):
        self.assertTrue(DEFAULT_DB.exists())
        self.assertEqual(lookup_customer("email", "eli.gray.0001@example.test")["customer_id"], "C0001")


if __name__ == "__main__":
    unittest.main()
