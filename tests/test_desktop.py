"""Desktop shell tests with a stand-in for pywebview (no display needed)."""

import os
import shutil
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.request

from finorganizer import db, desktop, ledger as L


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self):
        for fn in self.handlers:
            fn()


class _Window:
    def __init__(self, fake, title, url, **kw):
        self.fake, self.title, self.url, self.kw = fake, title, url, kw
        self.events = types.SimpleNamespace(closed=_Event(), loaded=_Event())
        self.destroyed = False
        self.save_path = None

    def destroy(self):
        self.destroyed = True
        if self in self.fake.windows:
            self.fake.windows.remove(self)
        self.events.closed.fire()

    def create_file_dialog(self, kind, **kw):
        return self.save_path


def make_fake_webview(on_start):
    fake = types.ModuleType("webview")
    fake.windows = []
    fake.settings = {}
    fake.FileDialog = types.SimpleNamespace(SAVE=20)

    def create_window(title, url, **kw):
        w = _Window(fake, title, url, **kw)
        fake.windows.append(w)
        return w

    def start(func=None, args=(), **kw):
        fake.start_kwargs = kw
        on_start(fake)  # simulates the user's session; returns when "all windows are closed"

    fake.create_window = create_window
    fake.start = start
    return fake


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "fin.db")
        conn = db.connect(self.db)
        L.add_account(conn, "Checking", "checking", 1000)
        conn.close()

    def tearDown(self):
        sys.modules.pop("webview", None)
        shutil.rmtree(self.dir)

    def test_closing_main_window_stops_everything(self):
        seen = {}

        def session(fake):
            main = fake.windows[0]
            seen["title"] = main.title
            with urllib.request.urlopen(main.url + "/api/accounts") as res:
                seen["status"] = res.status
            seen["url"] = main.url
            # User clicks Print: a summary window opens via the JS bridge.
            api = main.kw["js_api"]
            api.open_summary("2026-10")
            summary = fake.windows[1]
            seen["summary_url"] = summary.url
            # User exports transactions through the native Save dialog.
            main.save_path = os.path.join(self.dir, "out.csv")
            seen["saved"] = api.export_csv()
            # User closes the main window: the summary window must close too.
            main.destroy()
            seen["summary_closed"] = summary.destroyed and not fake.windows

        sys.modules["webview"] = make_fake_webview(session)
        self.assertEqual(desktop.run(self.db), 0)

        self.assertEqual(seen["title"], "FinOrganizer")
        self.assertEqual(seen["status"], 200)
        self.assertTrue(seen["summary_url"].endswith("/summary?print=1&month=2026-10"))
        self.assertTrue(seen["summary_closed"])
        with open(seen["saved"], encoding="utf-8") as f:
            self.assertTrue(f.read().startswith("id,date,account_name"))
        # After the window is gone, the server no longer answers.
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(seen["url"] + "/api/meta", timeout=2)
        start_kwargs = sys.modules["webview"].start_kwargs
        self.assertFalse(start_kwargs["private_mode"])  # keeps theme preference between runs
        self.assertTrue(os.path.isdir(start_kwargs["storage_path"]))

    def test_cancelled_save_dialog(self):
        def session(fake):
            main = fake.windows[0]
            self.assertIsNone(main.kw["js_api"].export_csv())
            main.destroy()

        sys.modules["webview"] = make_fake_webview(session)
        self.assertEqual(desktop.run(self.db), 0)

    def test_server_stops_even_if_window_crashes(self):
        captured = {}

        def session(fake):
            captured["url"] = fake.windows[0].url
            raise RuntimeError("web engine crashed")

        sys.modules["webview"] = make_fake_webview(session)
        with self.assertRaises(RuntimeError):
            desktop.run(self.db)
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(captured["url"] + "/api/meta", timeout=2)


if __name__ == "__main__":
    unittest.main()
