"""Regression tests for fixed bugs."""

import datetime as dt
import unittest
from unittest import mock

from finorganizer import db, desktop, ledger as L, reports
from finorganizer.money import next_occurrence


def bank_tx(conn, account, date, cents, payee, ext):
    conn.execute("INSERT INTO transactions (account_id, date, amount_cents, payee, external_id)"
                 " VALUES (?, ?, ?, ?, ?)", (account, date, cents, payee, ext))
    conn.commit()


class RecurringDayTests(unittest.TestCase):
    def test_end_of_month_bills_do_not_drift(self):
        self.assertEqual(next_occurrence("2026-02-28", "monthly", 31), dt.date(2026, 3, 31))
        c = db.connect(":memory:")
        a = L.add_account(c, "Checking", "checking")
        L.add_recurring(c, "Rent", a, -100000, "monthly", "2026-01-31")
        dates = [u["date"] for u in L.upcoming(c, 120, as_of="2026-01-01")]
        self.assertEqual(dates, ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"])
        L.post_due(c, as_of="2026-03-01")
        self.assertEqual(L.list_recurring(c)[0]["next_date"], "2026-03-31")

    def test_editing_other_fields_keeps_the_intended_day(self):
        c = db.connect(":memory:")
        a = L.add_account(c, "Checking", "checking")
        rid = L.add_recurring(c, "Rent", a, -100000, "monthly", "2026-01-31")
        L.post_due(c, as_of="2026-02-01")  # next due Feb 28
        L.update_recurring(c, rid, name="Rent!", next_date="2026-02-28", amount_cents=-110000)
        self.assertEqual(L.upcoming(c, 60, as_of="2026-02-01")[-1]["date"], "2026-03-31")
        L.update_recurring(c, rid, next_date="2026-03-15")  # a real date change moves the day
        self.assertEqual([u["date"] for u in L.upcoming(c, 50, as_of="2026-03-01")],
                         ["2026-03-15", "2026-04-15"])

    def test_old_databases_get_the_new_column(self):
        c = db.connect(":memory:")
        cols = {r[1] for r in c.execute("PRAGMA table_info(recurring)")}
        self.assertIn("anchor_day", cols)


class TransferDetectionTests(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.chk = L.add_account(self.c, "Checking", "checking")
        self.card = L.add_account(self.c, "Card", "credit_card")
        self.sav = L.add_account(self.c, "Savings", "savings")

    def test_card_payment_seen_from_both_banks_is_not_income_or_spending(self):
        bank_tx(self.c, self.chk, "2026-03-10", -50000, "PAYMENT TO CARD", "a1")
        bank_tx(self.c, self.card, "2026-03-12", 50000, "PAYMENT THANK YOU", "b1")
        bank_tx(self.c, self.chk, "2026-03-11", -4500, "GROCER", "a2")
        self.assertEqual(L.detect_transfers(self.c), 1)
        s = reports.month_summary(self.c, "2026-03")
        self.assertEqual((s["income_cents"], s["expense_cents"]), (0, 4500))
        self.assertEqual(L.detect_transfers(self.c), 0)  # idempotent

    def test_ambiguous_or_distant_matches_are_left_alone(self):
        bank_tx(self.c, self.chk, "2026-03-10", -20000, "OUT", "a1")
        bank_tx(self.c, self.card, "2026-03-10", 20000, "IN 1", "b1")
        bank_tx(self.c, self.sav, "2026-03-11", 20000, "IN 2", "c1")    # two candidates: ambiguous
        bank_tx(self.c, self.chk, "2026-03-01", -7000, "OUT 2", "a2")
        bank_tx(self.c, self.card, "2026-03-20", 7000, "IN 3", "b2")    # too far apart
        bank_tx(self.c, self.chk, "2026-03-05", -3000, "SAME ACCT", "a3")
        bank_tx(self.c, self.chk, "2026-03-05", 3000, "REFUND", "a4")  # same account: a refund
        self.assertEqual(L.detect_transfers(self.c), 0)

    def test_manual_entries_are_never_auto_linked(self):
        L.add_transaction(self.c, self.chk, "2026-03-10", -50000, "typed by hand")
        bank_tx(self.c, self.card, "2026-03-10", 50000, "PAYMENT", "b1")
        self.assertEqual(L.detect_transfers(self.c), 0)

    def test_not_a_transfer_is_remembered(self):
        bank_tx(self.c, self.chk, "2026-03-10", -50000, "OUT", "a1")
        bank_tx(self.c, self.card, "2026-03-10", 50000, "IN", "b1")
        L.detect_transfers(self.c)
        tx = L.list_transactions(self.c, account_id=self.chk)[0]
        L.unlink_transfer(self.c, tx["id"])
        self.assertIsNone(L.get_transaction(self.c, tx["id"])["transfer_id"])
        self.assertEqual(L.detect_transfers(self.c), 0)
        with self.assertRaises(ValueError):
            L.unlink_transfer(self.c, tx["id"])


class ValidationTests(unittest.TestCase):
    def test_blank_names_are_rejected(self):
        c = db.connect(":memory:")
        a = L.add_account(c, "Checking", "checking")
        g = L.add_goal(c, "Trip", 1000)
        r = L.add_recurring(c, "Rent", a, -1, "monthly", "2026-01-01")
        cat = L.add_category(c, "Pets")
        for fn, ident in ((L.update_account, a), (L.update_goal, g), (L.update_recurring, r),
                          (L.update_category, cat)):
            with self.subTest(fn.__name__):
                with self.assertRaises(ValueError):
                    fn(c, ident, name="   ")
        L.update_account(c, a, name="  Everyday  ")
        self.assertEqual(L.get_account(c, a)["name"], "Everyday")


class DesktopLinkTests(unittest.TestCase):
    def test_open_url_only_opens_web_addresses(self):
        api = desktop.Api(None, "http://127.0.0.1:1")
        with mock.patch("webbrowser.open") as opened:
            self.assertTrue(api.open_url("https://github.com/mmcvey02/finorganizer/releases/latest"))
            self.assertFalse(api.open_url("file:///etc/passwd"))
            self.assertFalse(api.open_url(None))
        opened.assert_called_once_with("https://github.com/mmcvey02/finorganizer/releases/latest")


if __name__ == "__main__":
    unittest.main()
