"""Money moved between your own accounts is never counted as spending or income."""

import json
import unittest

from finorganizer import csvio, db, ledger as L, reports
from finorganizer.server import App, respond


def call(app, method, path, body=None, query=""):
    status, _, data, _ = respond(app, method, path, query, json.dumps(body).encode() if body is not None else b"")
    assert status == 200, data
    return json.loads(data)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.app = App(db.connect(":memory:"))
        self.c = self.app.conn
        self.chk = L.add_account(self.c, "Checking", "checking", 500000)
        self.sav = L.add_account(self.c, "Savings", "savings", 100000)

    def spent(self):
        return reports.period_summary(self.c, "2026-01-01", "2026-12-31")["expense_cents"]

    def income(self):
        return reports.period_summary(self.c, "2026-01-01", "2026-12-31")["income_cents"]

    def tx(self, payee):
        return next(t for t in L.list_transactions(self.c) if t["payee"] == payee)

    def balance(self, acct):
        return L.get_account(self.c, acct)["balance_cents"]

    def test_both_sides_typed_in_are_paired(self):
        csvio.import_csv(self.c, self.chk, "Date,Description,Amount\n2026-03-01,Online Transfer to SAV 1234,-600.00\n")
        csvio.import_csv(self.c, self.sav, "Date,Description,Amount\n2026-03-02,Deposit from CHK 9876,600.00\n")
        self.assertTrue(self.tx("Online Transfer to SAV 1234")["transfer_id"])
        self.assertEqual((self.spent(), self.income()), (0, 0))
        self.assertEqual((self.balance(self.chk), self.balance(self.sav)), (440000, 160000))

    def test_ordinary_lookalikes_are_not_paired(self):
        L.add_transaction(self.c, self.chk, "2026-03-01", -5000, "Corner Shop")
        L.add_transaction(self.c, self.sav, "2026-03-01", 5000, "Cash deposit")
        L.reconcile_transfers(self.c)
        self.assertIsNone(self.tx("Corner Shop")["transfer_id"])
        self.assertEqual((self.spent(), self.income()), (5000, 5000))

    def test_your_own_category_wins(self):
        rent = L.find_category(self.c, "Rent / Mortgage")["id"]
        L.add_transaction(self.c, self.chk, "2026-03-01", -60000, "Transfer to landlord", rent)
        L.add_transaction(self.c, self.sav, "2026-03-01", 60000, "Transfer in")
        L.reconcile_transfers(self.c)
        self.assertIsNone(self.tx("Transfer to landlord")["transfer_id"])

    def test_one_sided_transfer_naming_your_account_is_not_spending(self):
        L.add_transaction(self.c, self.chk, "2026-03-01", -60000, "Online transfer to Savings")
        t = self.tx("Online transfer to Savings")
        self.assertEqual((t["category_name"], t["category_kind"]), ("Transfer", "transfer"))
        self.assertEqual(self.spent(), 0)
        # A transfer to someone else is still spending (it names none of your accounts).
        L.add_transaction(self.c, self.chk, "2026-03-02", -2500, "Zelle transfer to Jamie")
        self.assertNotEqual(self.tx("Zelle transfer to Jamie")["category_kind"], "transfer")
        self.assertEqual(self.spent(), 2500)

    def test_marking_a_transfer_adds_the_other_side(self):
        tid = L.add_transaction(self.c, self.chk, "2026-03-01", -60000, "ONLINE XFER 55")
        self.assertEqual(self.spent(), 60000)  # unknown text: counted as spending until you say otherwise
        r = call(self.app, "POST", "/api/transactions/%d/transfer" % tid, {"account_id": self.sav})
        self.assertEqual((r["created"], r["account_name"]), (True, "Savings"))
        self.assertEqual(self.spent(), 0)
        self.assertEqual((self.balance(self.chk), self.balance(self.sav)), (440000, 160000))
        other = L.get_transaction(self.c, L.get_transaction(self.c, tid)["transfer_id"])
        self.assertEqual((other["account_id"], other["amount_cents"], other["payee"]),
                         (self.sav, 60000, "Transfer from Checking"))

    def test_marking_links_an_entry_already_there(self):
        tid = L.add_transaction(self.c, self.chk, "2026-03-01", -60000, "Payment 77")
        L.add_transaction(self.c, self.sav, "2026-03-03", 60000, "Incoming 12")
        r = call(self.app, "POST", "/api/transactions/%d/transfer" % tid, {"account_id": self.sav})
        self.assertFalse(r["created"])
        self.assertEqual(len(L.list_transactions(self.c)), 2)
        self.assertEqual(self.balance(self.sav), 160000)

    def test_remembered_transfers_apply_to_past_and_future(self):
        old = L.add_transaction(self.c, self.chk, "2026-02-01", -20000, "ONLINE XFER 55")
        tid = L.add_transaction(self.c, self.chk, "2026-03-01", -20000, "ONLINE XFER 55")
        r = call(self.app, "POST", "/api/transactions/%d/transfer" % tid, {"account_id": self.sav, "remember": True})
        self.assertEqual(r["similar"], 1)
        self.assertTrue(L.get_transaction(self.c, old)["transfer_id"])
        # Next month's arrives by CSV: handled without asking.
        csvio.import_csv(self.c, self.chk, "Date,Description,Amount\n2026-04-01,Online Xfer 55,-200.00\n")
        self.assertTrue(self.tx("Online Xfer 55")["transfer_id"])
        self.assertEqual(self.spent(), 0)
        self.assertEqual(self.balance(self.sav), 160000)

    def test_not_a_transfer_undoes_it(self):
        tid = L.add_transaction(self.c, self.chk, "2026-03-01", -60000, "ONLINE XFER 55")
        call(self.app, "POST", "/api/transactions/%d/transfer" % tid, {"account_id": self.sav, "remember": True})
        call(self.app, "POST", "/api/transactions/%d/unlink-transfer" % tid)
        # The entry FinOrganizer added is gone, and the rule doesn't bring it back.
        self.assertEqual([t["payee"] for t in L.list_transactions(self.c)], ["ONLINE XFER 55"])
        L.reconcile_transfers(self.c)
        self.assertIsNone(L.get_transaction(self.c, tid)["transfer_id"])
        self.assertEqual(self.balance(self.sav), 100000)

    def test_other_account_not_tracked(self):
        tid = L.add_transaction(self.c, self.chk, "2026-03-01", -60000, "To brokerage")
        call(self.app, "POST", "/api/transactions/%d/transfer" % tid, {"account_id": None})
        self.assertEqual(L.get_transaction(self.c, tid)["category_name"], "Transfer")
        self.assertEqual(self.spent(), 0)

    def test_spending_breakdown_for_the_pie_chart(self):
        L.add_transaction(self.c, self.chk, "2026-03-01", -4500, "KROGER #1")
        L.add_transaction(self.c, self.chk, "2026-03-02", -1500, "NETFLIX.COM")
        L.add_transaction(self.c, self.sav, "2026-03-03", -1000, "Mystery 4")
        L.add_transfer(self.c, self.chk, self.sav, "2026-03-04", 60000)
        r = call(self.app, "GET", "/api/reports/spending-by-category", query="start=2026-03-01&end=2026-03-31")
        self.assertEqual(r["total_cents"], 7000)  # the transfer isn't spending
        got = {d["category_name"]: (d["spent_cents"], d["category_id"] is not None) for d in r["categories"]}
        self.assertEqual(got, {"Groceries": (4500, True), "Subscriptions": (1500, True), "Uncategorized": (1000, False)})
        r = call(self.app, "GET", "/api/reports/spending-by-category", query="account_id=%d" % self.sav)
        self.assertEqual([d["category_name"] for d in r["categories"]], ["Uncategorized"])


if __name__ == "__main__":
    unittest.main()
