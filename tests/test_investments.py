"""Investing is neither spending nor income; it's reported as invested."""

import os
import shutil
import sqlite3
import tempfile
import unittest

from finorganizer import db, ledger as L, reports


class InvestmentTests(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.chk = L.add_account(self.c, "Checking", "checking", 500000)
        self.ira = L.add_account(self.c, "Roth IRA", "retirement", 0)
        self.inv = L.find_category(self.c, "Investments")

    def summary(self):
        return reports.period_summary(self.c, "2026-03-01", "2026-03-31")

    def test_investing_outside_finorganizer(self):
        L.add_transaction(self.c, self.chk, "2026-03-02", 300000, "Payroll")
        L.add_transaction(self.c, self.chk, "2026-03-03", -50000, "Brokerage deposit", self.inv["id"])
        L.add_transaction(self.c, self.chk, "2026-03-20", 10000, "Brokerage withdrawal", self.inv["id"])
        L.add_transaction(self.c, self.chk, "2026-03-04", -20000, "Groceries")
        s = self.summary()
        self.assertEqual((s["income_cents"], s["expense_cents"], s["invested_cents"]), (300000, 20000, 40000))
        self.assertEqual([d["category_name"] for d in reports.spending_by_category(self.c, "2026-03-01", "2026-03-31")],
                         ["Groceries"])

    def test_investing_into_an_account_here(self):
        L.add_transfer(self.c, self.chk, self.ira, "2026-03-05", 65000)
        s = self.summary()
        self.assertEqual((s["expense_cents"], s["invested_cents"]), (0, 65000))
        self.assertEqual(L.get_account(self.c, self.ira)["balance_cents"], 65000)
        # Moving money between two investment accounts isn't new investing.
        brk = L.add_account(self.c, "Brokerage", "investment", 0)
        L.add_transfer(self.c, self.ira, brk, "2026-03-06", 1000)
        self.assertEqual(self.summary()["invested_cents"], 65000)
        self.assertTrue(any("invested" in n["message"] for n in reports.insights(self.c, "2026-03")))

    def test_brokerages_are_recognised(self):
        t = L.get_transaction(self.c, L.add_transaction(self.c, self.chk, "2026-03-05", -25000, "VANGUARD BUY"))
        self.assertEqual(t["category_name"], "Investments")
        t = L.get_transaction(self.c, L.add_transaction(self.c, self.chk, "2026-03-05", 1200, "VANGUARD DIVIDEND"))
        self.assertEqual(t["category_name"], "Interest & Dividends")  # dividends are income


class MigrationTests(unittest.TestCase):
    def test_existing_data_gets_the_category_once(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "old.db")
            c = db.connect(path)
            c.execute("DELETE FROM categories WHERE kind = 'investment'")
            c.execute("DELETE FROM settings WHERE key = 'cat_investments_v1'")  # as saved by an older version
            c.commit()
            c.close()
            c = db.connect(path)
            self.assertEqual(L.find_category(c, "Investments")["kind"], "investment")
            L.delete_category(c, L.find_category(c, "Investments")["id"])
            c.close()
            c = db.connect(path)  # deleted on purpose: stays deleted
            self.assertIsNone(c.execute("SELECT 1 FROM categories WHERE name = 'Investments'").fetchone())
            c.close()
        finally:
            shutil.rmtree(d)

    def test_a_category_of_yours_with_that_name_is_kept(self):
        c = sqlite3.connect(":memory:")
        c.executescript(db.SCHEMA)
        c.execute("INSERT INTO categories (name, kind, group_name) VALUES ('Investments', 'expense', 'Mine')")
        db.init_schema(c)
        self.assertEqual(c.execute("SELECT kind FROM categories WHERE name = 'Investments'").fetchone()[0], "expense")


if __name__ == "__main__":
    unittest.main()
