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

from finorganizer import banksync, db, ledger as L, summary_page
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

            def do_POST(self):
                if self.path.startswith("/claim/"):
                    tok = self.path.rsplit("/", 1)[1]
                    if tok in fake.claimed:
                        return self._send(403, "{}")
                    fake.claimed.add(tok)
                    return self._send(200, "http://user:s3cret@127.0.0.1:%d/simplefin" % fake.port, "text/plain")
                self._send(404, "{}")

            def do_GET(self):
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
        cid = banksync.connect(self.c, self.fake.token("flow"))
        conns = banksync.list_connections(self.c)
        self.assertEqual(conns[0]["label"], "CardCo, First Bank")
        self.assertEqual(conns[0]["host"], "http://127.0.0.1")  # credentials never exposed
        self.assertNotIn("s3cret", json.dumps(conns))
        self.assertEqual({r["status"] for r in conns[0]["accounts"]}, {"new"})

        # Nothing is imported until the user decides what each bank account feeds.
        r = banksync.sync_connection(self.c, cid, today=TODAY)
        self.assertEqual((r["imported"], r["new_accounts"]), (0, 2))

        # Checking feeds an existing account where one transaction was already typed in.
        mine = L.add_account(self.c, "My Checking", "checking", 0)
        L.add_transaction(self.c, mine, "2026-09-04", -4510, "Groceries run")
        banksync.link_remote(self.c, self.remote("Checking")["id"], "link", mine)
        banksync.link_remote(self.c, self.remote("Rewards Visa")["id"], "new")

        r = banksync.sync_connection(self.c, cid, today=TODAY)
        self.assertEqual(r["errors"], [])
        self.assertEqual((r["imported"], r["matched"]), (3, 1))  # pending item skipped
        txs = {t["payee"]: t for t in L.list_transactions(self.c, account_id=mine)}
        self.assertEqual(set(txs), {"PAYROLL ACME", "Groceries run", "RENT"})
        manual = self.c.execute("SELECT external_id, cleared FROM transactions WHERE id = ?",
                                (txs["Groceries run"]["id"],)).fetchone()
        self.assertEqual(tuple(manual), ("t2", 1))  # bank charge matched the hand-typed entry
        self.assertEqual(len(L.list_transactions(self.c, account_id=mine)), 3)

        # The account created from the bank adds up to the bank balance.
        visa = self.remote("Rewards Visa")
        acct = L.get_account(self.c, visa["account_id"])
        self.assertEqual((acct["type"], acct["balance_cents"]), ("credit_card", -25000))
        self.assertEqual(visa["difference_cents"], 0)

        # The linked manual account differs from the bank until the user matches it.
        chk = self.remote("Checking")
        self.assertEqual(chk["difference_cents"], 150000 - (200000 - 4510 - 45490))
        banksync.match_bank_balance(self.c, chk["id"])
        self.assertEqual(self.remote("Checking")["difference_cents"], 0)

        # Syncing again never duplicates.
        r = banksync.sync_connection(self.c, cid, today=TODAY + dt.timedelta(days=1))
        self.assertEqual((r["imported"], r["matched"]), (0, 0))

    def test_requests_are_windowed(self):
        cid = banksync.connect(self.c, self.fake.token("windows"))
        self.fake.requests.clear()
        banksync.sync_connection(self.c, cid, today=TODAY)
        spans = [(int(q["end-date"]) - int(q["start-date"])) / 86400 for q in self.fake.requests]
        self.assertTrue(all(s <= banksync.WINDOW_DAYS for s in spans))
        self.assertGreaterEqual(sum(spans), banksync.FIRST_SYNC_DAYS)

    def test_ignore_and_relink_validation(self):
        banksync.connect(self.c, self.fake.token("ignore"))
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
        banksync.connect(self.c, self.fake.token("once"))
        with self.assertRaises(banksync.SyncError):
            banksync.connect(self.c, self.fake.token("once"))

    def test_revoked_access_is_reported(self):
        good = "http://user:s3cret@127.0.0.1:%d/simplefin" % self.fake.port
        cid = banksync.add_connection(self.c, good)
        self.c.execute("UPDATE connections SET access_url = ?", (good.replace("s3cret", "wrong"),))
        r = banksync.sync_connection(self.c, cid, today=TODAY)
        self.assertIn("refused", r["errors"][0])
        self.assertIn("refused", banksync.list_connections(self.c)[0]["last_error"])


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
