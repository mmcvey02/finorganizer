import datetime as dt
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from finorganizer import csvio, db, ledger as L, planning, reports
from finorganizer.money import add_months, fmt, month_bounds, next_occurrence, to_cents
from finorganizer.sample import load_sample_data
from finorganizer.server import App, make_handler


def fresh():
    return db.connect(":memory:")


class MoneyTests(unittest.TestCase):
    def test_to_cents(self):
        self.assertEqual(to_cents("1,234.56"), 123456)
        self.assertEqual(to_cents("$12"), 1200)
        self.assertEqual(to_cents("(45.00)"), -4500)
        self.assertEqual(to_cents("-0.015"), -2)
        self.assertEqual(to_cents(7), 700)
        with self.assertRaises(ValueError):
            to_cents("abc")

    def test_fmt(self):
        self.assertEqual(fmt(-123456), "-$1,234.56")
        self.assertEqual(fmt(5), "$0.05")

    def test_dates(self):
        self.assertEqual(add_months(dt.date(2026, 1, 31), 1), dt.date(2026, 2, 28))
        self.assertEqual(month_bounds("2024-02"), ("2024-02-01", "2024-02-29"))
        self.assertEqual(next_occurrence("2026-01-01", "biweekly"), dt.date(2026, 1, 15))
        self.assertEqual(next_occurrence("2026-01-15", "quarterly"), dt.date(2026, 4, 15))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.c = fresh()
        self.chk = L.add_account(self.c, "Checking", "checking", 100000)
        self.sav = L.add_account(self.c, "Savings", "savings", 0)
        self.groceries = L.find_category(self.c, "Groceries")["id"]
        self.salary = L.find_category(self.c, "Salary")["id"]

    def test_balance_and_listing(self):
        L.add_transaction(self.c, self.chk, "2026-03-02", -5000, "Market", self.groceries)
        L.add_transaction(self.c, self.chk, "2026-03-01", 300000, "Payroll", self.salary)
        self.assertEqual(L.get_account(self.c, self.chk)["balance_cents"], 395000)
        txs = L.list_transactions(self.c, search="mark")
        self.assertEqual([t["payee"] for t in txs], ["Market"])

    def test_duplicate_account_rejected(self):
        with self.assertRaises(ValueError):
            L.add_account(self.c, "checking")

    def test_find_account_partial(self):
        self.assertEqual(L.find_account(self.c, "sav")["id"], self.sav)

    def test_transfer_is_linked_and_excluded_from_reports(self):
        L.add_transaction(self.c, self.chk, "2026-03-01", 300000, "Payroll", self.salary)
        out_id, in_id = L.add_transfer(self.c, self.chk, self.sav, "2026-03-05", 50000)
        self.assertEqual(L.get_account(self.c, self.sav)["balance_cents"], 50000)
        s = reports.month_summary(self.c, "2026-03")
        self.assertEqual(s["income_cents"], 300000)
        self.assertEqual(s["expense_cents"], 0)
        L.update_transaction(self.c, out_id, amount_cents=-60000)
        self.assertEqual(L.get_transaction(self.c, in_id)["amount_cents"], 60000)
        L.delete_transaction(self.c, in_id)
        self.assertEqual(L.list_transactions(self.c, account_id=self.chk)[0]["payee"], "Payroll")

    def test_budget_status_and_copy(self):
        L.set_budget(self.c, self.groceries, "2026-03", 40000)
        L.add_transaction(self.c, self.chk, "2026-03-10", -45000, "Market", self.groceries)
        L.add_transaction(self.c, self.chk, "2026-03-11", 5000, "Refund", self.groceries)
        (b,) = L.budget_status(self.c, "2026-03")
        self.assertEqual((b["spent_cents"], b["remaining_cents"], b["percent_used"]), (40000, 0, 100.0))
        self.assertEqual(L.copy_budgets(self.c, "2026-03", "2026-04"), 1)
        self.assertEqual(L.budget_status(self.c, "2026-04")[0]["budget_cents"], 40000)
        L.set_budget(self.c, self.groceries, "2026-04", 0)
        self.assertEqual(L.budget_status(self.c, "2026-04"), [])

    def test_goals(self):
        g = L.add_goal(self.c, "Trip", 120000, "2027-01-01", saved_cents=20000)
        L.contribute_goal(self.c, g, 10000)
        linked = L.add_goal(self.c, "Rainy day", 100000, account_id=self.sav)
        L.add_transfer(self.c, self.chk, self.sav, "2026-03-05", 25000)
        goals = {x["id"]: x for x in L.list_goals(self.c, as_of="2026-01-01")}
        self.assertEqual(goals[g]["current_cents"], 30000)
        self.assertEqual(goals[g]["months_left"], 12)
        self.assertEqual(goals[g]["monthly_needed_cents"], 7500)
        self.assertEqual(goals[linked]["percent"], 25.0)

    def test_recurring_post_due(self):
        rid = L.add_recurring(self.c, "Rent", self.chk, -150000, "monthly", "2026-01-01",
                              L.find_category(self.c, "Rent / Mortgage")["id"])
        self.assertEqual(len(L.upcoming(self.c, 60, as_of="2026-01-01")), 3)
        created = L.post_due(self.c, as_of="2026-03-15")
        self.assertEqual(len(created), 3)
        self.assertEqual(L.list_recurring(self.c)[0]["next_date"], "2026-04-01")
        self.assertEqual(L.post_due(self.c, as_of="2026-03-15"), [])
        L.update_recurring(self.c, rid, active=0)
        self.assertEqual(L.upcoming(self.c, 365, as_of="2026-03-15"), [])


class ReportTests(unittest.TestCase):
    def test_sample_data_reports(self):
        c = fresh()
        load_sample_data(c, months=4)
        d = reports.dashboard(c)
        self.assertGreater(d["summary"]["income_cents"], 0)
        self.assertEqual(len(d["cash_flow"]), 6)
        nw = reports.net_worth(c)
        self.assertEqual(nw["net_worth_cents"], sum(a["balance_cents"] for a in nw["accounts"]))
        hist = reports.net_worth_history(c, 3)
        self.assertEqual(hist[-1]["net_worth_cents"], nw["net_worth_cents"])
        spend = reports.spending_by_category(c, "2000-01-01", "2100-01-01")
        self.assertNotIn("Transfer", [s["category_name"] for s in spend])
        self.assertAlmostEqual(sum(s["percent"] for s in spend), 100, delta=1)
        with self.assertRaises(ValueError):
            load_sample_data(c)


class PlanningTests(unittest.TestCase):
    def test_loan(self):
        r = planning.amortization(2_500_000, 6.5, 60)
        self.assertEqual(r["payment_cents"], 48916)
        self.assertEqual(r["months"], 60)
        self.assertEqual(r["schedule"][-1]["balance_cents"], 0)
        faster = planning.amortization(2_500_000, 6.5, 60, extra_cents=10000)
        self.assertLess(faster["months"], 60)
        self.assertGreater(faster["interest_saved_cents"], 0)
        self.assertEqual(planning.loan_payment(1200, 0, 12), 100)

    def test_debt_avalanche_beats_snowball(self):
        debts = [{"name": "Card", "balance_cents": 500000, "rate": 24, "min_payment_cents": 10000},
                 {"name": "Small", "balance_cents": 100000, "rate": 5, "min_payment_cents": 5000}]
        av = planning.debt_payoff(debts, 50000, "avalanche")
        sn = planning.debt_payoff(debts, 50000, "snowball")
        self.assertLess(av["total_interest_cents"], sn["total_interest_cents"])
        self.assertEqual(av["payoff_order"][0]["name"], "Card")
        self.assertEqual(sn["payoff_order"][0]["name"], "Small")
        with self.assertRaises(ValueError):
            planning.debt_payoff(debts, 1000)

    def test_growth_and_retirement(self):
        g = planning.compound_growth(0, 10000, 0, 2)
        self.assertEqual(g[-1]["balance_cents"], 240000)
        r = planning.retirement_plan(30, 65, 0, 0, desired_annual_income_cents=4_000_000)
        self.assertFalse(r["on_track"])
        r2 = planning.retirement_plan(30, 65, 0, r["extra_monthly_needed_cents"],
                                      desired_annual_income_cents=4_000_000)
        self.assertTrue(r2["on_track"])

    def test_savings(self):
        self.assertEqual(planning.savings_plan(120000, 0, 12)["monthly_cents"], 10000)
        self.assertLess(planning.savings_plan(120000, 0, 12, 5)["monthly_cents"], 10000)
        self.assertEqual(planning.months_to_goal(100000, 0, 10000), 10)
        self.assertIsNone(planning.months_to_goal(100000, 0, 0))
        self.assertEqual(planning.budget_rule(100000)["wants_cents"], 30000)


class CSVTests(unittest.TestCase):
    def setUp(self):
        self.c = fresh()
        self.a = L.add_account(self.c, "Card", "credit_card")

    def test_import_amount_column_and_dedupe(self):
        L.add_transaction(self.c, self.a, "2026-01-01", -1000, "Coffee Hut",
                          L.find_category(self.c, "Dining Out")["id"])
        text = ("Date,Description,Amount,Category\n"
                "01/05/2026,Coffee Hut,-4.50,\n"
                "2026-01-06,Paycheck,100.00,Bonus\n"
                "bad,row,1,\n")
        r = csvio.import_csv(self.c, self.a, text)
        self.assertEqual((r["imported"], r["duplicates"], len(r["errors"])), (2, 0, 1))
        coffee = L.list_transactions(self.c, search="Coffee")[0]
        self.assertEqual(coffee["category_name"], "Dining Out")  # learned from history
        again = csvio.import_csv(self.c, self.a, text)
        self.assertEqual((again["imported"], again["duplicates"]), (0, 2))

    def test_debit_credit_columns_and_export(self):
        text = "Posted Date,Payee,Debit,Credit\n2026-02-01,Shop,25.00,\n2026-02-02,Refund,,5.00\n"
        csvio.import_csv(self.c, self.a, text)
        amounts = sorted(t["amount_cents"] for t in L.list_transactions(self.c))
        self.assertEqual(amounts, [-2500, 500])
        out = csvio.export_csv(self.c)
        self.assertIn("Shop", out)
        self.assertIn("-25.00", out)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = fresh()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(cls.conn)))
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        st, r = self.call("POST", "/api/accounts", {"name": "API Checking", "opening_balance_cents": 1000})
        self.assertEqual(st, 200)
        aid = r["id"]
        st, _ = self.call("POST", "/api/accounts", {"name": "API Checking"})
        self.assertEqual(st, 400)
        st, r = self.call("POST", "/api/transactions",
                          {"account_id": aid, "amount_cents": -250, "payee": "Snack", "date": "2026-05-01"})
        self.assertEqual(st, 200)
        st, txs = self.call("GET", "/api/transactions?account_id=%d" % aid)
        self.assertEqual(txs[0]["payee"], "Snack")
        st, _ = self.call("PUT", "/api/transactions/%d" % r["id"], {"category_id": ""})
        self.assertEqual(st, 200)
        st, d = self.call("GET", "/api/dashboard?month=2026-05")
        self.assertEqual(d["summary"]["expense_cents"], 250)
        st, p = self.call("POST", "/api/planning/debt", {
            "budget_cents": 20000,
            "debts": [{"name": "X", "balance_cents": 100000, "rate": 10, "min_payment_cents": 5000}]})
        self.assertEqual(p["avalanche"]["months"], p["snowball"]["months"])
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)
        self.assertEqual(self.call("DELETE", "/api/transactions/99999")[0], 404)
        self.assertEqual(self.call("POST", "/api/transactions", {"account_id": "x"})[0], 400)

    def test_static_and_traversal(self):
        with urllib.request.urlopen(self.base + "/") as res:
            self.assertIn(b"FinOrganizer", res.read())
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/../db.py")


if __name__ == "__main__":
    unittest.main()
