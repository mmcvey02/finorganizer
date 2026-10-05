import unittest

from finorganizer import categorize, csvio, db, ledger as L
from finorganizer.categorize import Categorizer, normalize_payee


def cat_name(conn, cat_id):
    if cat_id is None:
        return None
    return conn.execute("SELECT name FROM categories WHERE id = ?", (cat_id,)).fetchone()[0]


class NormalizeTests(unittest.TestCase):
    def test_bank_noise_is_removed(self):
        self.assertEqual(normalize_payee("SQ *BLUE BOTTLE COFFEE #123 SAN FRANCISCO CA"), "blue bottle coffee")
        self.assertEqual(normalize_payee("Blue Bottle Coffee"), "blue bottle coffee")
        self.assertEqual(normalize_payee("POS DEBIT 0412 TRADER JOE'S #552"), "trader joe")
        self.assertEqual(normalize_payee("12345"), "")


class GuessTests(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.cz = Categorizer(self.c)

    def guess(self, payee, amount=-1000, memo=""):
        return cat_name(self.c, self.cz.guess(payee, memo, amount))

    def test_common_bank_descriptions(self):
        cases = {
            "KROGER #0456 COLUMBUS OH": "Groceries",
            "WHOLEFDS MKT 10234": "Groceries",
            "STARBUCKS STORE 11223": "Dining Out",
            "DOORDASH*CHIPOTLE": "Dining Out",
            "UBER EATS HELP.UBER.COM": "Dining Out",
            "UBER *TRIP HELP.UBER.COM": "Public Transit",
            "SHELL OIL 57444 AUSTIN TX": "Fuel",
            "NETFLIX.COM": "Subscriptions",
            "SPOTIFY USA": "Subscriptions",
            "AMZN Mktp US*2K4L": "Shopping",
            "TARGET 00012345": "Shopping",
            "VERIZON WIRELESS PAYMENTS": "Phone & Internet",
            "COMCAST CABLE COMM": "Phone & Internet",
            "PG&E WEB ONLINE": "Utilities",
            "GEICO AUTO": "Insurance",
            "CVS/PHARMACY #1234": "Healthcare",
            "PLANET FITNESS": "Fitness",
            "DELTA AIR LINES": "Travel",
            "MARRIOTT HOTEL NYC": "Travel",
            "AMC THEATRES 0456": "Entertainment",
            "THE HOME DEPOT #1234": "Home Maintenance",
            "MONTHLY SERVICE FEE": "Fees & Charges",
            "IRS USATAXPYMT": "Taxes",
        }
        wrong = {p: (self.guess(p), want) for p, want in cases.items() if self.guess(p) != want}
        self.assertEqual(wrong, {})

    def test_income_needs_money_in(self):
        self.assertEqual(self.guess("ACME CORP PAYROLL", 285000), "Salary")
        self.assertEqual(self.guess("ACME CORP PAYROLL", -5000), None)  # not income when money leaves
        self.assertEqual(self.guess("INTEREST PAID", 123), "Interest & Dividends")
        self.assertIsNone(self.guess("STARBUCKS REFUND", 500))  # a refund isn't spending

    def test_unknown_stays_uncategorized(self):
        self.assertIsNone(self.guess("ZXQ HOLDINGS 4471"))
        self.assertIsNone(self.guess(""))

    def test_no_false_matches_inside_words(self):
        self.assertIsNone(self.guess("TAXI SERVICE OF BOSTON"))   # not "tax"
        self.assertIsNone(self.guess("BUSINESS SOLUTIONS LLC"))   # not "bus"
        self.assertIsNone(self.guess("ATM WITHDRAWAL 0042"))      # cash, not a fee

    def test_your_history_wins_over_rules(self):
        a = L.add_account(self.c, "Checking", "checking")
        gifts = L.find_category(self.c, "Gifts & Donations")["id"]
        L.add_transaction(self.c, a, "2026-01-01", -2000, "STARBUCKS #1", gifts)  # chosen by you
        L.add_transaction(self.c, a, "2026-01-02", -2000, "Starbucks 99", gifts)
        cz = Categorizer(self.c)
        self.assertEqual(cat_name(self.c, cz.guess("STARBUCKS STORE 11223", "", -500)), "Gifts & Donations")

    def test_guesses_never_teach_the_history(self):
        a = L.add_account(self.c, "Checking", "checking")
        tid = L.add_transaction(self.c, a, "2026-01-01", -2000, "STARBUCKS #1")
        self.assertEqual(L.get_transaction(self.c, tid)["auto_category"], 1)
        self.assertEqual(Categorizer(self.c).history, {})

    def test_deleted_categories_are_skipped(self):
        L.delete_category(self.c, L.find_category(self.c, "Fuel")["id"])
        self.assertIsNone(Categorizer(self.c).guess("SHELL OIL 123", "", -4000))


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.a = L.add_account(self.c, "Checking", "checking")

    def test_new_transactions_are_categorized_and_flagged(self):
        t = L.get_transaction(self.c, L.add_transaction(self.c, self.a, "2026-03-01", -4500, "KROGER #12"))
        self.assertEqual((t["category_name"], t["auto_category"]), ("Groceries", 1))
        chosen = L.find_category(self.c, "Dining Out")["id"]
        t2 = L.get_transaction(self.c, L.add_transaction(self.c, self.a, "2026-03-01", -4500, "KROGER", chosen))
        self.assertEqual((t2["category_name"], t2["auto_category"]), ("Dining Out", 0))
        t3 = L.get_transaction(self.c, L.add_transaction(self.c, self.a, "2026-03-01", -4500, "ZZZ 123"))
        self.assertIsNone(t3["category_id"])

    def test_csv_import_uses_the_categorizer(self):
        csvio.import_csv(self.c, self.a, "Date,Description,Amount\n2026-03-01,NETFLIX.COM,-15.99\n"
                                         "2026-03-02,MYSTERY VENDOR 9,-3.00\n")
        got = {t["payee"]: t["category_name"] for t in L.list_transactions(self.c)}
        self.assertEqual(got, {"NETFLIX.COM": "Subscriptions", "MYSTERY VENDOR 9": None})

    def test_choosing_a_category_teaches_similar_ones(self):
        ids = [L.add_transaction(self.c, self.a, "2026-03-0%d" % i, -1200, "SQ *JOES TACO SHACK #%d" % i,
                                 auto_categorize=False) for i in range(1, 4)]
        L.add_transaction(self.c, self.a, "2026-03-05", 1200, "SQ *JOES TACO SHACK REFUND", auto_categorize=False)
        dining = L.find_category(self.c, "Dining Out")["id"]
        L.update_transaction(self.c, ids[0], category_id=dining)
        self.assertEqual(L.get_transaction(self.c, ids[0])["auto_category"], 0)
        self.assertEqual(categorize.apply_to_similar(self.c, ids[0]), 2)  # not the refund (money in)
        self.assertEqual({L.get_transaction(self.c, i)["category_name"] for i in ids}, {"Dining Out"})
        # ...and future ones from that merchant follow your choice.
        new = L.add_transaction(self.c, self.a, "2026-04-01", -900, "SQ *JOES TACO SHACK #9")
        self.assertEqual(L.get_transaction(self.c, new)["category_name"], "Dining Out")

    def test_existing_transactions_are_sorted_once(self):
        L.add_transaction(self.c, self.a, "2026-03-01", -4500, "SAFEWAY 123", auto_categorize=False)
        L.add_transaction(self.c, self.a, "2026-03-02", -100, "UNKNOWN THING", auto_categorize=False)
        self.assertEqual(categorize.backfill_once(self.c), 1)
        self.assertEqual(categorize.backfill_once(self.c), 0)  # only on first run
        names = {t["payee"]: t["category_name"] for t in L.list_transactions(self.c)}
        self.assertEqual(names, {"SAFEWAY 123": "Groceries", "UNKNOWN THING": None})

    def test_transfers_are_left_alone(self):
        sav = L.add_account(self.c, "Savings", "savings")
        L.add_transfer(self.c, self.a, sav, "2026-03-01", 5000)
        categorize.categorize_uncategorized(self.c)
        self.assertEqual({t["category_name"] for t in L.list_transactions(self.c)}, {"Transfer"})


if __name__ == "__main__":
    unittest.main()
