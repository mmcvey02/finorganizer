"""Tests for profiles, the printable summary and SimpleFIN bank syncing."""

import base64
import datetime as dt
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from finorganizer import banksync, categorize, db, ledger as L, summary_page
from finorganizer.profiles import Profiles
from finorganizer.sample import load_sample_data
from finorganizer.server import App, make_handler

TODAY = dt.date(2026, 10, 5)


def ts(date_str):
    return int(dt.datetime.fromisoformat(date_str).replace(tzinfo=dt.timezone.utc).timestamp())


class FakeSimpleFIN:
    """Minimal SimpleFIN Bridge: one-time claim URLs and an /accounts endpoint."""

    def __init__(self):
        self.claimed = set()
        self.requests = []
        self.fail_accounts = False  # simulate an outage right after a token is claimed
        self.accounts = [
            {"org": {"name": "First Bank", "domain": "firstbank.example"}, "id": "CHK-1",
             "name": "Checking", "currency": "USD", "balance": "1500.00",
             "balance-date": ts("2026-10-04"),
             "transactions": [
                 {"id": "t1", "posted": ts("2026-09-01"), "amount": "2000.00", "description": "PAYROLL ACME"},
                 {"id": "t2", "posted": ts("2026-09-03"), "amount": "-45.10", "description": "FRESHMART #12",
                  "payee": "FreshMart"},
                 {"id": "t3", "posted": ts("2026-10-02"), "amount": "-454.90", "description": "RENT"},
                 {"id": "t4", "posted": ts("2026-10-04"), "amount": "-9.99", "description": "PENDING THING",
                  "pending": True},
             ]},
            {"org": {"name": "CardCo"}, "id": "CC-9", "name": "Rewards Visa", "currency": "USD",
             "balance": "-250.00", "balance-date": ts("2026-10-04"),
             "transactions": [
                 {"id": "c1", "posted": ts("2026-09-20"), "amount": "-250.00", "description": "AIRLINE"},
             ]},
        ]
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _blocked(self):
                # Like some hosting firewalls: reject the default Python user agent.
                if "Python-urllib" in (self.headers.get("User-Agent") or ""):
                    self._send(403, "<html><body>Access denied</body></html>", "text/html")
                    return True
                return False

            def do_POST(self):
                if self._blocked():
                    return
                if self.path.startswith("/claim/"):
                    tok = self.path.rsplit("/", 1)[1]
                    if tok in fake.claimed:
                        return self._send(403, "{}")
                    fake.claimed.add(tok)
                    return self._send(200, "http://user:s3cret@127.0.0.1:%d/simplefin" % fake.port, "text/plain")
                self._send(404, "{}")

            def do_GET(self):
                if self._blocked():
                    return
                if fake.fail_accounts:
                    return self._send(503, "Service Unavailable", "text/plain")
                url = urlparse(self.path)
                expected = "Basic " + base64.b64encode(b"user:s3cret").decode()
                if self.headers.get("Authorization") != expected:
                    return self._send(403, "{}")
                if url.path != "/simplefin/accounts":
                    return self._send(404, "{}")
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                fake.requests.append(q)
                start, end = int(q.get("start-date", 0)), int(q.get("end-date", 2 ** 40))
                out = []
                for a in fake.accounts:
                    a = dict(a)
                    txns = a.pop("transactions")
                    if not q.get("balances-only"):
                        a["transactions"] = [t for t in txns if start <= t["posted"] < end]
                    out.append(a)
                self._send(200, json.dumps({"errors": [], "accounts": out}))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def token(self, name="abc"):
        return base64.b64encode(("http://127.0.0.1:%d/claim/%s" % (self.port, name)).encode()).decode()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class BankSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeSimpleFIN()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()

    def setUp(self):
        self.c = db.connect(":memory:")

    def remote(self, name):
        conn = banksync.list_connections(self.c)[0]
        return next(r for r in conn["accounts"] if r["name"] == name)

    def test_full_flow(self):
        # An account typed in by hand earlier, named like the bank's, with one matching entry.
        mine = L.add_account(self.c, "Checking", "checking", 10000)
        L.add_transaction(self.c, mine, "2026-09-04", -4510, "Groceries run")

        r = banksync.connect(self.c, self.fake.token("flow"))
        self.assertIsNone(r["warning"])
        conns = banksync.list_connections(self.c)
        self.assertEqual(conns[0]["label"], "CardCo, First Bank")
        self.assertEqual(conns[0]["host"], "http://127.0.0.1")  # credentials never exposed
        self.assertNotIn("s3cret", json.dumps(conns))

        # Connecting downloads immediately: every bank account feeds an account right away.
        self.assertEqual({x["status"] for x in conns[0]["accounts"]}, {"linked"})
        self.assertEqual(self.remote("Checking")["account_id"], mine)  # matched by name
        self.assertEqual(sorted(r["sync"]["added_accounts"]), ["Checking", "Rewards Visa (CardCo)"])
        self.assertEqual((r["sync"]["imported"], r["sync"]["matched"]), (3, 1))  # pending skipped

        txs = {t["payee"]: t for t in L.list_transactions(self.c, account_id=mine)}
        self.assertEqual(set(txs), {"PAYROLL ACME", "Groceries run", "RENT"})
        manual = self.c.execute("SELECT external_id, cleared FROM transactions WHERE id = ?",
                                (txs["Groceries run"]["id"],)).fetchone()
        self.assertEqual(tuple(manual), ("t2", 1))  # bank charge matched the hand-typed entry
        self.assertTrue(txs["RENT"]["external_id"])  # marked as coming from the bank

        # The account created from the bank adds up to the bank balance.
        visa = self.remote("Rewards Visa")
        acct = L.get_account(self.c, visa["account_id"])
        self.assertEqual((acct["type"], acct["balance_cents"]), ("credit_card", -25000))
        self.assertEqual(visa["difference_cents"], 0)

        # The hand-made account's opening balance was off by $100 until matched to the bank.
        self.assertEqual(self.remote("Checking")["difference_cents"], -10000)
        banksync.match_bank_balance(self.c, self.remote("Checking")["id"])
        self.assertEqual(self.remote("Checking")["difference_cents"], 0)

        # Syncing again never duplicates.
        again = banksync.sync_connection(self.c, r["id"], today=TODAY + dt.timedelta(days=1))
        self.assertEqual((again["imported"], again["matched"], again["added_accounts"]), (0, 0, []))

    def test_downloads_follow_the_auto_categorize_setting(self):
        categorize.set_enabled(self.c, False)
        banksync.connect(self.c, self.fake.token("autocat-off"))
        cats = {t["payee"]: t["category_name"] for t in L.list_transactions(self.c)}
        self.assertEqual(set(cats.values()), {None})  # off: nothing guessed
        categorize.set_enabled(self.c, True)          # on again: what's waiting is sorted
        cats = {t["payee"]: t["category_name"] for t in L.list_transactions(self.c)}
        self.assertEqual(cats["PAYROLL ACME"], "Salary")

    def test_dont_import_is_respected(self):
        cid = banksync.add_connection(self.c, "http://user:s3cret@127.0.0.1:%d/simplefin" % self.fake.port)["id"]
        banksync.link_remote(self.c, self.remote("Rewards Visa")["id"], "ignore")
        r = banksync.sync_connection(self.c, cid, today=TODAY)
        self.assertEqual(r["added_accounts"], ["Checking (First Bank)"])
        self.assertEqual(self.remote("Rewards Visa")["status"], "ignored")
        self.assertEqual([a["name"] for a in L.list_accounts(self.c)], ["Checking (First Bank)"])

    def test_account_discovered_later_gets_full_history(self):
        cid = banksync.add_connection(self.c, "http://user:s3cret@127.0.0.1:%d/simplefin" % self.fake.port)["id"]
        banksync.sync_connection(self.c, cid, today=TODAY)
        self.fake.accounts.append({
            "org": {"name": "Credit Union"}, "id": "SAV-2", "name": "Savings", "currency": "USD",
            "balance": "900.00", "balance-date": ts("2026-10-20"),
            "transactions": [{"id": "s1", "posted": ts("2026-08-01"), "amount": "900.00",
                              "description": "OPENING DEPOSIT"}]})
        try:
            r = banksync.sync_connection(self.c, cid, today=TODAY + dt.timedelta(days=20))
        finally:
            self.fake.accounts.pop()
        self.assertEqual(r["added_accounts"], ["Savings (Credit Union)"])
        self.assertEqual(r["imported"], 1)  # 80-day-old deposit fetched despite the recent last sync
        sav = self.remote("Savings")
        self.assertEqual(L.get_account(self.c, sav["account_id"])["balance_cents"], 90000)

    def test_requests_are_windowed(self):
        cid = banksync.add_connection(self.c, "http://user:s3cret@127.0.0.1:%d/simplefin" % self.fake.port)["id"]
        self.fake.requests.clear()
        banksync.sync_connection(self.c, cid, today=TODAY)
        spans = [(int(q["end-date"]) - int(q["start-date"])) / 86400 for q in self.fake.requests]
        self.assertTrue(all(s <= banksync.WINDOW_DAYS for s in spans))
        self.assertGreaterEqual(sum(spans), banksync.FIRST_SYNC_DAYS)

    def test_ignore_and_relink_validation(self):
        banksync.add_connection(self.c, "http://user:s3cret@127.0.0.1:%d/simplefin" % self.fake.port)
        banksync.link_remote(self.c, self.remote("Checking")["id"], "ignore")
        self.assertEqual(self.remote("Checking")["status"], "ignored")
        a = L.add_account(self.c, "A", "checking")
        banksync.link_remote(self.c, self.remote("Checking")["id"], "link", a)
        with self.assertRaises(ValueError):
            banksync.link_remote(self.c, self.remote("Rewards Visa")["id"], "link", a)
        with self.assertRaises(ValueError):
            banksync.link_remote(self.c, self.remote("Rewards Visa")["id"], "bogus")

    def test_token_errors(self):
        with self.assertRaises(ValueError):
            banksync.connect(self.c, "not a token!")
        with self.assertRaisesRegex(ValueError, "web address"):
            banksync.connect(self.c, "https://beta-bridge.simplefin.org/")
        banksync.connect(self.c, self.fake.token("once"))
        with self.assertRaisesRegex(banksync.SyncError, "already used"):
            banksync.connect(self.c, self.fake.token("once"))

    def test_pasted_token_variants_are_accepted(self):
        claim = "http://127.0.0.1:%d/claim/" % self.fake.port
        std = lambda n: base64.b64encode((claim + n).encode()).decode()  # noqa: E731
        variants = {
            "quoted": '"%s"' % std("q"),
            "labelled": "Setup Token: %s" % std("l"),
            "wrapped": "\n  %s\n" % "\n".join([std("w")[i:i + 20] for i in range(0, len(std("w")), 20)]),
            "unpadded": std("pad-it").rstrip("="),
            "urlsafe": base64.urlsafe_b64encode((claim + "u?x=>>>").encode()).decode(),
            "invisible": "\u200b" + std("z") + "\ufeff",
            "claim url": claim + "direct",
        }
        for name, text in variants.items():
            with self.subTest(name):
                c = db.connect(":memory:")
                result = banksync.connect(c, text)
                self.assertIsNone(result["warning"])
                self.assertEqual(len(banksync.list_connections(c)[0]["accounts"]), 2)

    def test_pasting_an_access_url_works(self):
        access = "http://user:s3cret@127.0.0.1:%d/simplefin" % self.fake.port
        r = banksync.connect(self.c, access)
        self.assertIsNone(r["warning"])
        # Pasting the same access again doesn't create a duplicate connection.
        self.assertEqual(banksync.connect(self.c, access)["id"], r["id"])
        self.assertEqual(len(banksync.list_connections(self.c)), 1)

    def test_claimed_access_survives_an_outage(self):
        """A token can only be claimed once, so the access must be kept even if loading fails."""
        self.fake.fail_accounts = True
        try:
            r = banksync.connect(self.c, self.fake.token("outage"))
        finally:
            self.fake.fail_accounts = False
        self.assertIn("couldn't load your accounts", r["warning"])
        self.assertIn("503", banksync.list_connections(self.c)[0]["last_error"])
        # Later, syncing works with the saved access; no new token needed.
        result = banksync.sync_connection(self.c, r["id"], today=TODAY)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["added_accounts"]), 2)
        self.assertIsNone(banksync.list_connections(self.c)[0]["last_error"])

    def test_revoked_access_is_reported(self):
        good = "http://user:s3cret@127.0.0.1:%d/simplefin" % self.fake.port
        cid = banksync.add_connection(self.c, good)["id"]
        self.c.execute("UPDATE connections SET access_url = ?", (good.replace("s3cret", "wrong"),))
        r = banksync.sync_connection(self.c, cid, today=TODAY)
        self.assertIn("refused", r["errors"][0])
        self.assertIn("new setup token", banksync.list_connections(self.c)[0]["last_error"])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.p = Profiles(os.path.join(self.dir, "main.db"))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_lifecycle(self):
        self.assertEqual([x["id"] for x in self.p.list()], ["default"])
        pid = self.p.create("Jamie Lee")
        self.assertEqual(pid, "jamie-lee")
        self.assertEqual(self.p.create("Jamie  Lee!"), "jamie-lee-2")
        with self.assertRaises(ValueError):
            self.p.create("jamie lee")
        conn = self.p.open(pid)
        L.add_account(conn, "Jamie's", "checking")
        conn.close()
        default = self.p.open("default")
        self.assertEqual(L.list_accounts(default), [])  # data is separate
        default.close()
        self.p.rename(pid, "Jamie")
        self.assertEqual(self.p.find("JAMIE"), pid)
        self.p.remember(pid)
        self.assertEqual(self.p.last_used(), pid)
        self.p.delete(pid)
        self.assertEqual(self.p.last_used(), "default")
        with self.assertRaises(ValueError):
            self.p.delete("default")
        with self.assertRaises(ValueError):
            self.p.path("../evil")

    def test_server_switching(self):
        conn = self.p.open("default")
        app = App(conn, self.p, "default")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def call(method, path, body=None):
            req = urllib.request.Request(base + path, method=method, headers={"Content-Type": "application/json"},
                                         data=json.dumps(body).encode() if body is not None else None)
            try:
                with urllib.request.urlopen(req) as res:
                    return res.status, res.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        try:
            call("POST", "/api/accounts", {"name": "Default acct"})
            st, r = call("POST", "/api/profiles", {"name": "Sam"})
            self.assertEqual(st, 200)
            st, r = call("GET", "/api/accounts")
            self.assertEqual(json.loads(r), [])  # switched to Sam's empty profile
            st, r = call("GET", "/api/profiles")
            self.assertEqual(json.loads(r)["current"], "sam")
            st, page = call("GET", "/summary")
            self.assertIn(b"Financial Summary \xc2\xb7 Sam", page)
            self.assertEqual(call("POST", "/api/profiles/switch", {"id": "nope"})[0], 404)
            st, _ = call("DELETE", "/api/profiles/sam")  # deleting the active profile falls back to default
            self.assertEqual(st, 200)
            st, r = call("GET", "/api/accounts")
            self.assertEqual(json.loads(r)[0]["name"], "Default acct")
        finally:
            httpd.shutdown()
            httpd.server_close()
            app.conn.close()


class SummaryPageTests(unittest.TestCase):
    def test_render(self):
        c = db.connect(":memory:")
        load_sample_data(c, months=3)
        html = summary_page.render(c, profile_name="Alex <b>", auto_print=True)
        self.assertIn("Financial Summary · Alex &lt;b&gt;", html)
        for section in ("Accounts", "Income vs spending", "Net worth, 12 months", "Budget vs actual",
                        "Savings goals", "Upcoming bills", "Debts"):
            self.assertIn(section, html)
        self.assertEqual(html.count("<svg"), 2)  # cash-flow bars + net-worth line
        self.assertIn('class="bar"', html)       # inline bars for spending, budgets, goals
        self.assertIn("window.print()", html)
        empty = summary_page.render(db.connect(":memory:"))
        self.assertIn("None", empty)


if __name__ == "__main__":
    unittest.main()
