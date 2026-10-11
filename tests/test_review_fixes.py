"""Bugs found in the backend review, and the check for transfers missing their other side."""

import json
import unittest
from unittest import mock

from finorganizer import banksync, csvio, db, ledger as L, money, planning, reports, server
from finorganizer.server import App, respond


def call(app, method, path, body=None, query=""):
    status, _, data, _ = respond(app, method, path, query, json.dumps(body).encode() if body is not None else b"")
    return status, json.loads(data)


class Base(unittest.TestCase):
    def setUp(self):
        self.app = App(db.connect(":memory:"))
        self.c = self.app.conn
        self.chk = L.add_account(self.c, "Checking", "checking", 100000)
        self.sav = L.add_account(self.c, "Savings", "savings", 0)


class InputTests(Base):
    def test_edits_refuse_values_of_the_wrong_type(self):
        tid = L.add_transaction(self.c, self.chk, "2026-03-01", -500, "Coffee")
        status, r = call(self.app, "PUT", "/api/transactions/%d" % tid, {"amount_cents": "abc"})
        self.assertEqual(status, 400)
        self.assertEqual(L.get_transaction(self.c, tid)["amount_cents"], -500)
        self.assertEqual(call(self.app, "PUT", "/api/transactions/%d" % tid, {"amount_cents": "-650"})[0], 200)
        self.assertEqual(L.get_transaction(self.c, tid)["amount_cents"], -650)
        self.assertEqual(call(self.app, "PUT", "/api/accounts/%d" % self.chk, {"interest_rate": "NaN"})[0], 400)
        self.assertEqual(call(self.app, "PUT", "/api/accounts/%d" % self.chk, {"opening_balance_cents": 1.5})[0], 400)
        gid = L.add_goal(self.c, "Trip", 50000)
        self.assertEqual(call(self.app, "PUT", "/api/goals/%d" % gid, {"target_cents": 0})[0], 400)
        self.assertEqual(call(self.app, "PUT", "/api/goals/%d" % gid, {"account_id": ""})[0], 200)

    def test_both_sides_of_a_transfer_cant_share_an_account(self):
        out_id, _ = L.add_transfer(self.c, self.chk, self.sav, "2026-03-01", 10000)
        with self.assertRaisesRegex(ValueError, "same account"):
            L.update_transaction(self.c, out_id, account_id=self.sav)

    def test_nan_and_infinity_are_not_amounts(self):
        for bad in ("nan", "NaN", "inf", "-Infinity"):
            with self.assertRaises(ValueError):
                money.to_cents(bad)

    def test_calculators_refuse_runaway_inputs(self):
        for path, body in (("/api/planning/growth", {"years": 10 ** 9, "rate": 7}),
                           ("/api/planning/loan", {"principal_cents": 100000, "rate": 5, "months": 10 ** 7}),
                           ("/api/planning/savings", {"target_cents": 100, "months": 12, "rate": "inf"}),
                           ("/api/planning/savings", {"target_cents": 100, "months": 1200, "rate": 1e9}),
                           ("/api/planning/debt", {"budget_cents": 10000, "debts": [{"rate": 5}]})):
            status, r = call(self.app, "POST", path, body)
            self.assertEqual(status, 400, (path, r))
        # A debt without a name is fine.
        r = planning.debt_payoff([{"balance_cents": 10000, "rate": 10, "min_payment_cents": 1000}], 5000)
        self.assertEqual(r["payoff_order"][0]["name"], "Debt 1")

    def test_unexpected_errors_are_reported_not_dropped(self):
        with mock.patch.object(server.reports, "dashboard", side_effect=RuntimeError("boom")):
            status, r = call(self.app, "GET", "/api/dashboard")
        self.assertEqual(status, 500)
        self.assertIn("boom", r["error"])

    def test_deleting_a_profile_that_doesnt_exist(self):
        status, _ = call(self.app, "DELETE", "/api/profiles/nobody")
        self.assertIn(status, (400, 404))


class DeleteTests(Base):
    def test_deleted_bank_account_isnt_recreated_by_the_next_sync(self):
        cid = self.c.execute("INSERT INTO connections (access_url) VALUES ('https://u:p@x/simplefin')").lastrowid
        self.c.execute("INSERT INTO remote_accounts (connection_id, remote_id, name, account_id)"
                       " VALUES (?, 'R1', 'Checking', ?)", (cid, self.chk))
        L.delete_account(self.c, self.chk)
        self.assertEqual(banksync.auto_assign(self.c, cid), [])
        self.assertEqual([a["name"] for a in L.list_accounts(self.c)], ["Savings"])

    def test_deleted_category_leaves_no_stale_auto_tag(self):
        tid = L.add_transaction(self.c, self.chk, "2026-03-01", -4500, "KROGER #1")
        self.assertEqual(L.get_transaction(self.c, tid)["auto_category"], 1)
        L.delete_category(self.c, L.get_transaction(self.c, tid)["category_id"])
        t = L.get_transaction(self.c, tid)
        self.assertEqual((t["category_id"], t["auto_category"]), (None, 0))


class CsvTests(Base):
    CSV = "Date,Description,Amount\n2026-03-01,Coffee Cart,-5.00\n2026-03-01,Coffee Cart,-5.00\n"

    def test_identical_rows_in_one_file_are_separate_purchases(self):
        self.assertEqual(csvio.import_csv(self.c, self.chk, self.CSV)["imported"], 2)
        again = csvio.import_csv(self.c, self.chk, self.CSV)
        self.assertEqual((again["imported"], again["duplicates"]), (0, 2))

    def test_unknown_account_is_one_clear_error(self):
        with self.assertRaises(L.NotFound):
            csvio.import_csv(self.c, 999, self.CSV)

    def test_export_is_safe_to_open_in_a_spreadsheet(self):
        L.add_transaction(self.c, self.chk, "2026-03-01", -123456, '=HYPERLINK("http://x")', memo="@SUM(A1)")
        L.add_transaction(self.c, self.chk, "2026-03-02", -5, "Tiny")
        text = csvio.export_csv(self.c)
        self.assertIn("'=HYPERLINK", text)
        self.assertIn("'@SUM", text)
        self.assertIn(",-1234.56,", text)
        self.assertIn(",-0.05,", text)


class BankTests(Base):
    def test_unreadable_items_are_skipped_and_reported(self):
        errors = []
        imp, _ = banksync._import_transactions(self.c, self.chk, [
            {"id": "a", "posted": 1772323200, "amount": "-5.00", "description": "OK"},
            {"id": "b", "posted": 1772323200, "amount": "lots", "description": "Bad amount"},
            {"posted": 1772323200, "amount": "-1.00", "description": "No id"},
        ], None, errors)
        self.assertEqual(imp, 1)
        self.assertEqual(errors, ["skipped 2 transactions the bank sent in an unreadable form"])

    def test_accounts_without_an_id_or_with_bad_balances(self):
        cid = self.c.execute("INSERT INTO connections (access_url) VALUES ('https://u:p@x/simplefin')").lastrowid
        banksync._upsert_remote(self.c, cid, [{"name": "No id"}, {"id": "X", "name": "Odd", "balance": "n/a",
                                                                  "balance-date": "soon"}])
        r = [dict(x) for x in self.c.execute("SELECT remote_id, balance_cents, balance_date FROM remote_accounts")]
        self.assertEqual(r, [{"remote_id": "X", "balance_cents": None, "balance_date": None}])


class TransferCheckTests(Base):
    def test_broken_links_are_repaired(self):
        a = L.add_transaction(self.c, self.chk, "2026-03-01", -1000, "Out")
        b = L.add_transaction(self.c, self.sav, "2026-03-01", 1000, "In")
        self.c.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (b, a))  # one-way link
        c = L.add_transaction(self.c, self.chk, "2026-03-02", -700, "Odd")
        d = L.add_transaction(self.c, self.chk, "2026-03-02", 700, "Same account")
        self.c.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (d, c))
        self.c.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (c, d))
        self.assertEqual(L.repair_transfers(self.c), 3)
        self.assertEqual(L.get_transaction(self.c, b)["transfer_id"], a)
        self.assertIsNone(L.get_transaction(self.c, c)["transfer_id"])
        self.assertEqual(L.repair_transfers(self.c), 0)

    def test_transfers_missing_their_other_side(self):
        transfer = L.find_category(self.c, "Transfer")["id"]
        # Recorded in checking only; savings has a deposit for the same amount 8 days later
        # (too far apart to pair automatically).
        lone = L.add_transaction(self.c, self.chk, "2026-03-01", -60000, "To savings", transfer)
        dep = L.add_transaction(self.c, self.sav, "2026-03-09", 60000, "Deposit")
        recent = L.add_transaction(self.c, self.chk, "2026-03-28", -5000, "Move", transfer)
        r = L.unmatched_transfers(self.c, as_of="2026-03-30")
        self.assertEqual(([t["id"] for t in r["missing"]], r["waiting"]), ([lone], 1))
        self.assertEqual([m["id"] for m in r["missing"][0]["matches"]], [dep])
        status, _ = call(self.app, "GET", "/api/transfers/unmatched")
        self.assertEqual(status, 200)
        # Shown on the dashboard too.
        self.assertTrue(any("only one account" in n["message"] for n in reports.insights(self.c, "2026-03")))
        # Linking the match resolves it; both balances add up.
        self.assertEqual(call(self.app, "POST", "/api/transactions/%d/link" % lone, {"other_id": dep})[0], 200)
        self.assertEqual(L.get_transaction(self.c, dep)["transfer_id"], lone)
        self.assertEqual([t["id"] for t in L.unmatched_transfers(self.c, as_of="2026-04-30")["missing"]], [recent])
        # One that involves an account outside FinOrganizer isn't flagged.
        call(self.app, "POST", "/api/transactions/%d/transfer" % recent, {"account_id": None})
        self.assertEqual(L.unmatched_transfers(self.c, as_of="2026-04-30"), {"missing": [], "waiting": 0})

    def test_link_pair_checks_the_two_sides(self):
        a = L.add_transaction(self.c, self.chk, "2026-03-01", -1000, "A")
        b = L.add_transaction(self.c, self.sav, "2026-03-01", 900, "B")
        c = L.add_transaction(self.c, self.chk, "2026-03-01", 1000, "C")
        with self.assertRaisesRegex(ValueError, "same amount"):
            L.link_pair(self.c, a, b)
        with self.assertRaisesRegex(ValueError, "same account"):
            L.link_pair(self.c, a, c)

    def test_deleting_an_account_leaves_the_other_halves_flagged(self):
        L.add_transfer(self.c, self.chk, self.sav, "2026-03-01", 10000)
        L.delete_account(self.c, self.sav)
        self.assertEqual(len(L.unmatched_transfers(self.c, as_of="2026-04-01")["missing"]), 1)


if __name__ == "__main__":
    unittest.main()
