import base64
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from finorganizer import backup, db, ledger as L, webapp
from finorganizer.profiles import Profiles
from finorganizer.server import App, respond


def call(app, method, url, body=None):
    path, _, query = url.partition("?")
    raw = json.dumps(body).encode() if body is not None else b""
    status, ctype, data, headers = respond(app, method, path, query, raw)
    return status, (json.loads(data) if ctype.startswith("application/json") else data), headers


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "finorganizer.db")
        self.app = App(db.connect(self.path))

    def tearDown(self):
        self.app.conn.close()
        shutil.rmtree(self.dir)

    def test_backup_and_restore_round_trip(self):
        a = L.add_account(self.app.conn, "Checking", "checking", 100000)
        L.add_transaction(self.app.conn, a, "2026-09-01", -4500, "KROGER #12")
        status, data, headers = call(self.app, "GET", "/api/backup.db")
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(backup.SQLITE_HEADER))
        self.assertRegex(headers["Content-Disposition"], r'FinOrganizer-backup-\d{4}-\d\d-\d\d\.db')

        L.add_account(self.app.conn, "Added later", "savings")
        status, result, _ = call(self.app, "POST", "/api/restore", {"data": base64.b64encode(data).decode()})
        self.assertEqual((status, result), (200, {"accounts": 1, "transactions": 1}))
        self.assertEqual([x["name"] for x in L.list_accounts(self.app.conn)], ["Checking"])
        self.assertTrue(os.path.exists(self.path + ".before-restore"))  # the replaced data is kept
        # The app keeps working on the restored data.
        status, _, _ = call(self.app, "POST", "/api/accounts", {"name": "New", "type": "cash"})
        self.assertEqual(status, 200)

    def test_bad_files_change_nothing(self):
        L.add_account(self.app.conn, "Checking", "checking")
        other = os.path.join(self.dir, "other.db")
        c = sqlite3.connect(other)
        c.execute("CREATE TABLE notes (x)")
        c.commit()
        c.close()
        with open(other, "rb") as f:
            unrelated = f.read()
        for data in (b"hello", unrelated, backup.SQLITE_HEADER + b"\0" * 200, b""):
            status, result, _ = call(self.app, "POST", "/api/restore", {"data": base64.b64encode(data).decode()})
            self.assertEqual(status, 400, data[:20])
            self.assertIn("error", result)
        status, result, _ = call(self.app, "POST", "/api/restore", {"data": "not base64!"})
        self.assertEqual(status, 400)
        self.assertEqual([x["name"] for x in L.list_accounts(self.app.conn)], ["Checking"])
        self.assertFalse(os.path.exists(self.path + ".restore"))

    def test_older_backups_are_upgraded(self):
        old = os.path.join(self.dir, "old.db")
        c = db.connect(old)
        c.execute("ALTER TABLE transactions DROP COLUMN auto_category")  # as saved by an older version
        c.commit()
        c.close()
        with open(old, "rb") as f:
            data = f.read()
        self.app.restore(data)
        cols = {r[1] for r in self.app.conn.execute("PRAGMA table_info(transactions)")}
        self.assertIn("auto_category", cols)


class WebAppTests(unittest.TestCase):
    """The entry point the iPhone web app calls instead of the HTTP server."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        if webapp._app:
            webapp._app.conn.close()
        webapp._app = None
        shutil.rmtree(self.dir)

    def test_requests(self):
        data_dir = os.path.join(self.dir, "data")
        webapp.start(data_dir)
        status, ctype, body, _ = webapp.request("POST", "/api/accounts", json.dumps({"name": "Checking"}))
        self.assertEqual(status, 200)
        aid = json.loads(body)["id"]
        webapp.request("POST", "/api/transactions", json.dumps(
            {"account_id": aid, "date": "2026-09-02", "amount_cents": -1599, "payee": "NETFLIX.COM"}))
        status, ctype, body, _ = webapp.request("GET", "/api/transactions?limit=5")
        self.assertEqual([t["category_name"] for t in json.loads(body)], ["Subscriptions"])
        status, ctype, body, disp = webapp.request("GET", "/api/export.csv?start=2026-09-01")
        self.assertIn("NETFLIX.COM", body.decode())
        self.assertIn("transactions.csv", disp)
        status, ctype, body, _ = webapp.request("GET", "/summary?month=2026-09")
        self.assertTrue(ctype.startswith("text/html"))
        status, _, body, _ = webapp.request("POST", "/api/sample")
        self.assertEqual(status, 400)  # only into an empty profile
        status, _, body, _ = webapp.request("GET", "/api/nope")
        self.assertEqual(status, 404)
        # Profiles live in the same data folder, and the last one used reopens.
        webapp.request("POST", "/api/profiles", json.dumps({"name": "Sam"}))
        webapp._app.conn.close()
        self.assertEqual(webapp.start(data_dir).profile_id, "sam")
        status, _, body, _ = webapp.request("POST", "/api/sample")
        self.assertEqual(status, 200)
        self.assertGreater(len(json.loads(webapp.request("GET", "/api/accounts")[2])), 2)
        self.assertEqual([p["id"] for p in Profiles(os.path.join(data_dir, "finorganizer.db")).list()],
                         ["default", "sam"])


if __name__ == "__main__":
    unittest.main()
