"""Tests for the Investments page: holdings, positions files, prices, performance and suggestions."""

import datetime as dt
import json
import unittest
from unittest import mock

from finorganizer import banksync, db, investments as I, ledger as L, market as M
from finorganizer.server import App, respond

TODAY = dt.date(2026, 11, 2)

SCHWAB_CSV = '''"Positions for account Individual ...123 as of 04:00 PM ET, 2026/10/30"

"Symbol","Description","Qty (Quantity)","Price","Price Chng % (Price Change %)","Mkt Val (Market Value)","Day Chng $ (Day Change $)","Cost Basis","Gain $ (Gain/Loss $)","Security Type"
"SWTSX","SCHWAB TOTAL STOCK MARKET INDEX","500","$15.00","0.5%","$7,500.00","$10.00","$6,000.00","$1,500.00","Mutual Fund"
"AAPL","APPLE INC","40","$250.00","1%","$10,000.00","$100.00","$4,000.00","$6,000.00","Equity"
"SCHZ","SCHWAB US AGGREGATE BOND ETF","100","$23.00","0%","$2,300.00","$0.00","$2,500.00","-$200.00","ETFs & Closed End Funds"
"Cash & Cash Investments","--","--","--","--","$1,200.00","--","--","--","Cash and Money Market"
"Account Total","--","--","--","--","$21,000.00","--","--","--","--"
'''

FIDELITY_CSV = '''Account Number,Account Name,Symbol,Description,Quantity,Last Price,Last Price Change,Current Value,Today's Gain/Loss Dollar,Cost Basis Total,Average Cost Basis,Type
X123,ROTH IRA,SPAXX**,HELD IN MONEY MARKET,,,,$310.55,,,,Cash
X123,ROTH IRA,FXAIX,FIDELITY 500 INDEX FUND,10.5,$200.00,+$1.00,$2100.00,+$10.50,$1800.00,$171.43,Cash
X123,ROTH IRA,BRK.B,BERKSHIRE HATHAWAY INC CLASS B,2,$480.00,-$2.00,$960.00,-$4.00,$700.00,$350.00,Cash

"The data and information in this spreadsheet is provided to you solely for your use..."
'''


def chart_payload(symbol, closes, start=dt.date(2025, 11, 3), name="", qtype="ETF"):
    """A Yahoo chart response with one close per weekday from ``start``."""
    days, d = [], start
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    ts = [int(dt.datetime(x.year, x.month, x.day, 21, tzinfo=dt.timezone.utc).timestamp()) for x in days]
    return {"chart": {"result": [{
        "meta": {"symbol": symbol, "regularMarketPrice": closes[-1], "previousClose": closes[-2],
                 "longName": name, "instrumentType": qtype, "gmtoffset": -18000, "regularMarketTime": ts[-1]},
        "timestamp": ts, "indicators": {"quote": [{"close": closes}]}}], "error": None}}


def linear(start, end, n=260):
    return [start + (end - start) * i / (n - 1) for i in range(n)]


class FakeMarket:
    """Prices for a few symbols, rising steadily over the year (S&P 500 up 10%)."""

    def __init__(self):
        self.calls = []
        self.series = {"^GSPC": linear(6000, 6600), "SWTSX": linear(12, 15), "SCHZ": linear(23, 23),
                       "AAPL": linear(200, 250), "VXUS": linear(60, 66), "^TNX": linear(4.5, 4.1),
                       "^VIX": linear(15, 32)}

    def __call__(self, symbol):
        self.calls.append(symbol)
        if symbol not in self.series:
            raise M.MarketError("%s: no such symbol" % symbol)
        return chart_payload(symbol, self.series[symbol])


class ClassifyTests(unittest.TestCase):
    def test_guesses(self):
        g = I.guess_class
        self.assertEqual(g("VTI"), ("us_stock", "fund"))
        self.assertEqual(g("SWISX"), ("intl_stock", "fund"))
        self.assertEqual(g("SWVXX"), ("cash", "cash"))
        self.assertEqual(g("SPAXX**"), ("cash", "cash"))
        self.assertEqual(g("", "Cash & Cash Investments"), ("cash", "cash"))
        self.assertEqual(g("XYZBX", "Something Short-Term Bond Fund"), ("bond", "fund"))
        self.assertEqual(g("SWYNX", "Schwab Target 2050 Index Fund"), ("balanced", "fund"))
        self.assertEqual(g("ABCDX", "Emerging Markets Equity"), ("intl_stock", "fund"))
        self.assertEqual(g("AAPL", "APPLE INC"), ("us_stock", "stock"))
        self.assertEqual(g("ABCDX", "Some Growth"), ("us_stock", "fund"))  # 5-letter X: a mutual fund
        self.assertEqual(g("MSFT", "Microsoft", "EQUITY"), ("us_stock", "stock"))

    def test_symbols(self):
        self.assertEqual(I.quote_symbol("brk.b"), "BRK-B")
        self.assertEqual(I.quote_symbol("BRK/B"), "BRK-B")
        self.assertEqual(I.quote_symbol("SPAXX**"), "SPAXX")
        self.assertEqual(I.quote_symbol("^GSPC"), "^GSPC")


class PositionsFileTests(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.brk = L.add_account(self.c, "Schwab Brokerage", "investment", 0, "Charles Schwab")

    def test_schwab(self):
        items, skipped = I.parse_positions(SCHWAB_CSV)
        self.assertEqual(skipped, 0)
        self.assertEqual([(h["symbol"], h["value_cents"]) for h in items],
                         [("SWTSX", 750000), ("AAPL", 1000000), ("SCHZ", 230000), ("", 120000)])
        self.assertEqual(items[0]["shares"], 500)
        self.assertEqual(items[0]["price"], 15.0)
        self.assertEqual(items[0]["cost_basis_cents"], 600000)
        self.assertEqual(items[1]["kind"], "stock")
        self.assertEqual((items[3]["name"], items[3]["asset_class"], items[3]["cost_basis_cents"]),
                         ("Cash & Cash Investments", "cash", None))

    def test_fidelity(self):
        items, _ = I.parse_positions(FIDELITY_CSV)
        self.assertEqual([(h["symbol"], h["value_cents"], h["cost_basis_cents"]) for h in items],
                         [("SPAXX", 31055, None), ("FXAIX", 210000, 180000), ("BRK.B", 96000, 70000)])
        self.assertEqual(items[0]["asset_class"], "cash")

    def test_not_a_positions_file(self):
        with self.assertRaises(ValueError):
            I.parse_positions("Date,Amount,Payee\n2026-01-01,5.00,Shop\n")

    def test_reimport_replaces_but_keeps_your_own(self):
        I.import_positions(self.c, self.brk, SCHWAB_CSV)
        mine = I.add_holding(self.c, self.brk, "VXUS", "Vanguard Total Intl", 10, price=66.0)
        r = I.import_positions(self.c, self.brk, SCHWAB_CSV)
        self.assertEqual((r["imported"], r["value_cents"]), (4, 2100000))
        self.assertEqual(len(I.list_holdings(self.c, self.brk)), 5)
        # Editing an imported holding makes it yours: the next import leaves it alone.
        aapl = next(h for h in I.list_holdings(self.c) if h["symbol"] == "AAPL")
        I.update_holding(self.c, aapl["id"], shares=50)
        self.assertEqual(I.get_holding(self.c, aapl["id"])["value_cents"], 1250000)  # 50 x $250
        I.import_positions(self.c, self.brk, SCHWAB_CSV)
        self.assertEqual(sorted(h["symbol"] for h in I.list_holdings(self.c, self.brk)),
                         ["", "AAPL", "AAPL", "SCHZ", "SWTSX", "VXUS"])
        self.assertIsNotNone(I.get_holding(self.c, mine))


class HoldingEditTests(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.a = L.add_account(self.c, "IRA", "retirement", 0)

    def test_value_from_shares_and_price(self):
        h = I.get_holding(self.c, I.add_holding(self.c, self.a, "vti", shares=3, price=300.5))
        self.assertEqual((h["symbol"], h["value_cents"]), ("VTI", 90150))
        I.update_holding(self.c, h["id"], value_cents=100000)
        self.assertAlmostEqual(I.get_holding(self.c, h["id"])["price"], 1000 / 3)

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            I.add_holding(self.c, self.a, "", "")
        with self.assertRaises(ValueError):
            I.add_holding(self.c, self.a, "VTI", shares=1)  # no value or price
        with self.assertRaises(ValueError):
            I.add_holding(self.c, self.a, "VTI", value_cents=100, asset_class="stonks")
        with self.assertRaises(ValueError):
            I.add_holding(self.c, self.a, "VTI", value_cents=100, expense_ratio=30)

    def test_deleting_the_account_deletes_its_holdings(self):
        I.add_holding(self.c, self.a, "VTI", value_cents=100)
        L.delete_account(self.c, self.a)
        self.assertEqual(I.list_holdings(self.c), [])


class MarketTests(unittest.TestCase):
    def setUp(self):
        M.clear_cache()

    def test_parse_and_changes(self):
        d = M.parse_chart("SPY", chart_payload("SPY", linear(100, 120), name="SPDR S&P 500"))
        self.assertEqual((d["name"], d["price"], d["currency"]), ("SPDR S&P 500", 120, "USD"))
        self.assertEqual(d["history"][0][0], "2025-11-03")
        ch = M.changes(d, TODAY)
        self.assertEqual(ch["year"], 20.0)
        self.assertGreater(ch["ytd"], 0)
        self.assertGreater(ch["month"], 0)
        self.assertLess(ch["day"], 0.1)

    def test_errors(self):
        with self.assertRaises(M.MarketError):
            M.parse_chart("NOPE", {"chart": {"result": None, "error": {"description": "No data found"}}})
        found, errors = M.charts(["^GSPC", "NOPE"], fetch=FakeMarket())
        self.assertEqual(list(found), ["^GSPC"])
        self.assertIn("NOPE", errors)

    def test_cache(self):
        fake = FakeMarket()
        M.chart("aapl", fake)
        M.chart("AAPL", fake)
        self.assertEqual(fake.calls, ["AAPL"])
        self.assertEqual(list(M.cached(["AAPL", "SWTSX"])), ["AAPL"])

    def test_overview(self):
        o = M.overview(fetch=FakeMarket(), today=TODAY)
        by = {i["symbol"]: i for i in o["items"]}
        self.assertEqual(by["^GSPC"]["year"], 10.0)
        self.assertEqual(by["^TNX"]["year"], -0.4)  # a rate: change in percentage points
        self.assertTrue(by["^TNX"]["is_rate"])
        self.assertTrue(any("S&P 500 is up" in n for n in o["notes"]))
        self.assertTrue(any("Volatility is high" in n for n in o["notes"]))
        self.assertEqual(len(o["items"]), 4)  # the rest weren't available


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        M.clear_cache()
        self.c = db.connect(":memory:")
        self.chk = L.add_account(self.c, "Checking", "checking", 2000000)
        self.brk = L.add_account(self.c, "Schwab Brokerage", "investment", 0, "Charles Schwab")
        I.import_positions(self.c, self.brk, SCHWAB_CSV)

    def quotes(self):
        found, _ = M.charts(I.symbols_to_price(self.c) + [M.BENCHMARK], fetch=FakeMarket())
        return found

    def test_summary(self):
        p = I.portfolio(self.c, today=TODAY)
        self.assertEqual(p["total_cents"], 2100000)
        self.assertEqual((p["cost_cents"], p["gain_cents"]), (1250000, 730000))
        self.assertAlmostEqual(p["cost_coverage"], 94.3, places=1)
        alloc = {a["asset_class"]: a["percent"] for a in p["allocation"]}
        self.assertEqual(alloc, {"us_stock": 83.3, "bond": 11.0, "cash": 5.7})
        aapl = next(h for h in p["holdings"] if h["symbol"] == "AAPL")
        self.assertEqual((aapl["gain_cents"], aapl["gain_pct"], round(aapl["weight"], 1)), (600000, 150.0, 47.6))
        self.assertIsNone(p["past_year"])  # no prices yet
        self.assertEqual(p["history"], [{"date": TODAY.isoformat(), "value_cents": 2100000}])

    def test_account_without_holdings_counts_its_balance(self):
        L.add_account(self.c, "401(k)", "retirement", 5000000)
        p = I.portfolio(self.c, today=TODAY)
        self.assertEqual(p["total_cents"], 7100000)
        self.assertEqual(p["allocation"][-1]["asset_class"], "unknown")
        self.assertTrue(any(s["title"] == "Add holdings to see the full picture" for s in p["suggestions"]))

    def test_live_prices_and_past_year(self):
        q = self.quotes()
        self.assertEqual(I.apply_prices(self.c, q, TODAY), 3)  # cash isn't priced
        p = I.portfolio(self.c, q, today=TODAY)
        self.assertEqual(p["total_cents"], 2100000)  # the file's prices match the fake market
        py = p["past_year"]
        self.assertEqual(py["benchmark_pct"], 10.0)
        # A year ago: SWTSX 500 x 12 + AAPL 40 x 200 + SCHZ 2300 + cash 1200 = 17,500 -> 21,000
        self.assertEqual(py["portfolio_pct"], 20.0)
        self.assertLessEqual(len(py["points"]), 70)
        self.assertEqual(py["points"][-1]["portfolio_pct"], 20.0)
        # Today's move: SWTSX and AAPL rose a little since yesterday's close.
        self.assertGreater(p["day_change_cents"], 0)

    def test_growth_since_tracking_excludes_contributions(self):
        I.portfolio(self.c, today=dt.date(2026, 10, 1))
        L.add_transfer(self.c, self.chk, self.brk, "2026-10-15", 100000)
        I.add_holding(self.c, self.brk, "", "Cash", value_cents=100000, asset_class="cash")
        h = next(h for h in I.list_holdings(self.c) if h["symbol"] == "AAPL")
        I.update_holding(self.c, h["id"], price=262.5)  # +$500
        p = I.portfolio(self.c, today=TODAY)
        s = p["since_tracking"]
        self.assertEqual((s["start_cents"], s["contributed_cents"], s["growth_cents"]), (2100000, 100000, 50000))
        self.assertEqual(p["invested_year_cents"], 100000)

    def test_profile_and_target(self):
        self.assertEqual(I.target_stock_percent(I.get_profile(self.c)), 70)
        I.set_profile(self.c, 30, "aggressive")
        self.assertEqual(I.target_stock_percent(I.get_profile(self.c)), 90)
        I.set_profile(self.c, 80, "conservative")
        self.assertEqual(I.target_stock_percent(I.get_profile(self.c)), 20)
        with self.assertRaises(ValueError):
            I.set_profile(self.c, 5)
        with self.assertRaises(ValueError):
            I.set_profile(self.c, risk="yolo")
        I.set_profile(self.c, "")
        self.assertIsNone(I.get_profile(self.c)["age"])


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.chk = L.add_account(self.c, "Checking", "checking", 3000000)
        self.brk = L.add_account(self.c, "Brokerage", "investment", 0)
        self.ira = L.add_account(self.c, "IRA", "retirement", 0)

    def titles(self, **profile):
        if profile:
            I.set_profile(self.c, **profile)
        return {s["title"]: s for s in I.portfolio(self.c, today=TODAY)["suggestions"]}

    def test_empty(self):
        L.delete_account(self.c, self.brk)
        L.delete_account(self.c, self.ira)
        self.assertIn("Add your investments", self.titles())

    def test_concentration_international_fees_and_tax_losses(self):
        I.add_holding(self.c, self.brk, "TSLA", "Tesla Inc", 10, 3000000, 4000000)
        I.add_holding(self.c, self.ira, "VTI", "Vanguard Total Stock Market ETF", 10, 6000000, expense_ratio=0.03)
        I.add_holding(self.c, self.ira, "ABCDX", "Pricey Active Growth Fund", 10, 1000000, expense_ratio=1.1)
        t = self.titles()
        self.assertIn("TSLA is 30% of your portfolio", t)
        self.assertIn("Consider international stocks", t)
        self.assertIn("ABCDX charges 1.10% a year", t)
        self.assertIn("$110.00 a year", t["ABCDX charges 1.10% a year"]["message"])
        self.assertIn("Possible tax-loss harvesting", t)  # taxable account, down $10,000
        self.assertIn("More stock-heavy than your target", t)
        # Warnings come first.
        levels = [s["level"] for s in I.portfolio(self.c, today=TODAY)["suggestions"]]
        self.assertEqual(levels, sorted(levels, key={"warning": 0, "info": 1, "good": 2}.get))

    def test_on_target_and_cash(self):
        I.add_holding(self.c, self.ira, "VTI", value_cents=4000000)
        I.add_holding(self.c, self.ira, "VXUS", value_cents=2000000)
        I.add_holding(self.c, self.ira, "BND", value_cents=2500000)
        I.add_holding(self.c, self.ira, "SWVXX", value_cents=1500000)
        t = self.titles(age=45)
        self.assertIn("Your stock/bond mix is on target", t)  # 60% stocks vs 65%
        self.assertIn("Uninvested cash", t)
        self.assertIn("Check your funds' fees", t)
        self.assertNotIn("Consider international stocks", t)

    def test_too_conservative(self):
        I.add_holding(self.c, self.ira, "BND", value_cents=9000000)
        I.add_holding(self.c, self.ira, "VTI", value_cents=1000000)
        t = self.titles(age=25)
        self.assertIn("More conservative than your target", t)
        self.assertIn("$75,000", t["More conservative than your target"]["message"])  # 85% - 10% of $100k

    def test_foundations(self):
        L.add_account(self.c, "Visa", "credit_card", -500000, interest_rate=24.9)
        L.add_transaction(self.c, self.chk, "2026-10-10", -2900000, "Big spend")  # leaves $1,000 of cash
        I.add_holding(self.c, self.ira, "VTI", value_cents=100000)
        t = self.titles()
        self.assertIn("Pay down Visa first", t)
        self.assertIn("Build your emergency fund", t)


class BankHoldingsTests(unittest.TestCase):
    def test_simplefin_holdings_are_stored(self):
        from tests.test_features import FakeSimpleFIN, ts
        fake = FakeSimpleFIN()
        try:
            fake.accounts = [{
                "org": {"name": "Charles Schwab"}, "id": "SCH-1", "name": "Brokerage Account", "currency": "USD",
                "balance": "10500.00", "balance-date": ts("2026-10-04"), "transactions": [],
                "holdings": [
                    {"id": "h1", "symbol": "SWTSX", "description": "Schwab Total Stock Mkt", "shares": "500",
                     "market_value": "7500.00", "cost_basis": "6000.00", "currency": "USD"},
                    {"id": "h2", "symbol": "SCHZ", "description": "Schwab US Agg Bond", "shares": "100",
                     "market_value": "2300.00", "cost_basis": "0.00"},
                    {"id": "h3", "symbol": "", "description": "", "market_value": "1.00"},  # nothing to go on
                ]}]
            c = db.connect(":memory:")
            banksync.connect(c, fake.token("holdings"))
            acct = L.find_account(c, "Brokerage Account (Charles Schwab)")
            self.assertEqual(acct["type"], "investment")
            hs = {h["symbol"]: h for h in I.list_holdings(c, acct["id"])}
            self.assertEqual(set(hs), {"SWTSX", "SCHZ"})
            self.assertEqual((hs["SWTSX"]["value_cents"], hs["SWTSX"]["cost_basis_cents"], hs["SWTSX"]["source"]),
                             (750000, 600000, "bank"))
            self.assertIsNone(hs["SCHZ"]["cost_basis_cents"])  # 0 means unknown
            # The bank's balance includes $700 of cash it didn't list as a holding.
            p = I.portfolio(c, today=TODAY)
            self.assertEqual(p["total_cents"], 1050000)
            cash = [h for h in p["holdings"] if h.get("synthetic")]
            self.assertEqual(cash[0]["value_cents"], 70000)
            # Next sync replaces them.
            fake.accounts[0]["holdings"] = fake.accounts[0]["holdings"][:1]
            banksync.sync(c)
            self.assertEqual([h["symbol"] for h in I.list_holdings(c, acct["id"])], ["SWTSX"])
        finally:
            fake.close()


class ApiTests(unittest.TestCase):
    def setUp(self):
        M.clear_cache()
        self.app = App(db.connect(":memory:"))
        self.brk = L.add_account(self.app.conn, "Brokerage", "investment", 0)

    def call(self, method, path, body=None, query=""):
        status, _, data, _ = respond(self.app, method, path, query, json.dumps(body).encode() if body else b"")
        return status, json.loads(data)

    def test_holdings_crud(self):
        s, r = self.call("POST", "/api/holdings", {"account_id": self.brk, "symbol": "VTI", "shares": 2, "price": 300})
        self.assertEqual(s, 200)
        hid = r["id"]
        self.assertEqual(self.call("PUT", "/api/holdings/%d" % hid, {"cost_basis_cents": 50000})[0], 200)
        s, d = self.call("GET", "/api/investments")
        self.assertEqual((d["total_cents"], d["gain_cents"], d["live"]), (60000, 10000, False))
        self.assertEqual(self.call("POST", "/api/holdings", {"account_id": self.brk})[0], 400)
        self.assertEqual(self.call("DELETE", "/api/holdings/%d" % hid)[0], 200)
        self.assertEqual(self.call("DELETE", "/api/holdings/%d" % hid)[0], 404)

    def test_import_and_profile(self):
        s, r = self.call("POST", "/api/holdings/import", {"account_id": self.brk, "csv": SCHWAB_CSV})
        self.assertEqual((s, r["imported"]), (200, 4))
        self.assertEqual(self.call("POST", "/api/holdings/import", {"account_id": self.brk, "csv": "nope"})[0], 400)
        s, r = self.call("PUT", "/api/investments/profile", {"age": "40", "risk": "moderate"})
        self.assertEqual((s, r["age"]), (200, 40))

    def test_live_prices(self):
        self.call("POST", "/api/holdings/import", {"account_id": self.brk, "csv": SCHWAB_CSV})
        fake = FakeMarket()
        with mock.patch.object(M, "_download", fake):
            s, d = self.call("GET", "/api/investments", query="live=1")
            self.assertEqual(s, 200)
            self.assertTrue(d["live"])
            self.assertIsNotNone(d["past_year"])
            # Later page loads reuse the downloaded prices without downloading again.
            n = len(fake.calls)
            s, d = self.call("GET", "/api/investments")
            self.assertEqual(len(fake.calls), n)
            self.assertIsNotNone(d["past_year"])
            s, m = self.call("GET", "/api/market")
            self.assertEqual(s, 200)
            self.assertEqual(m["items"][0]["label"], "S&P 500")

    def test_market_offline(self):
        def down(symbol):
            raise M.MarketError("couldn't reach the price service: no network")
        with mock.patch.object(M, "_download", down):
            s, r = self.call("GET", "/api/market")
            self.assertEqual(s, 502)
            self.assertIn("couldn't reach", r["error"])
            self.call("POST", "/api/holdings", {"account_id": self.brk, "symbol": "VTI", "value_cents": 100})
            s, d = self.call("GET", "/api/investments", query="live=1")
            self.assertEqual((s, d["live"], d["total_cents"]), (200, False, 100))
            self.assertTrue(d["price_errors"])

    def test_iphone_version_has_no_market(self):
        with mock.patch.object(M, "available", lambda: False):
            self.assertEqual(self.call("GET", "/api/market")[0], 503)
            s, d = self.call("GET", "/api/investments", query="live=1")
            self.assertEqual((s, d["market_available"]), (200, False))


class SampleDataTests(unittest.TestCase):
    def test_sample_has_holdings_matching_the_401k(self):
        from finorganizer.sample import load_sample_data
        c = db.connect(":memory:")
        load_sample_data(c)
        k = L.find_account(c, "401(k)")
        self.assertEqual(sum(h["value_cents"] for h in I.list_holdings(c, k["id"])), k["balance_cents"])


if __name__ == "__main__":
    unittest.main()
