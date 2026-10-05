"""Desktop app: FinOrganizer in its own native window (no browser).

Uses pywebview, which embeds the system web engine (Microsoft Edge WebView2 on
Windows, WebKit on macOS) in a normal application window. The local server runs in a background
thread, bound to 127.0.0.1 only. Closing the main window stops the server,
closes the database and ends the process.

    pip install pywebview        # one-time, when running from source
    python -m finorganizer.desktop
"""

import argparse
import os
import sys
import threading
import time
import traceback

from . import csvio, updater
from .profiles import Profiles
from .server import make_server

APP_NAME = "FinOrganizer"


def _data_dir(profiles):
    return os.path.dirname(profiles.base_path)


def _message_box(title, text, error=False):
    """Show a native message box when there's no window (or console) to report in."""
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
    elif sys.platform == "darwin":
        import subprocess
        quote = lambda t: '"%s"' % t.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
        subprocess.run(["osascript", "-e", "display dialog %s with title %s buttons {\"OK\"} with icon %s"
                        % (quote(text), quote(title), "stop" if error else "note")], check=False)
    else:
        print("%s: %s" % (title, text), file=sys.stderr)


class Api:
    """Methods callable from the page as ``window.pywebview.api.<name>()``.

    Attributes starting with an underscore are not exposed to JavaScript.
    """

    def __init__(self, server_app, base_url):
        self._app = server_app
        self._url = base_url
        self._main = None
        self._relaunch = None

    def open_summary(self, month=""):
        """Open the printable summary in its own window."""
        import webview
        url = "%s/summary?print=1%s" % (self._url, "&month=" + month if month else "")
        webview.create_window("Financial Summary", url, width=900, height=820)

    def install_update(self):
        """Download the latest version, then close so it can start in our place."""
        info = updater.check()
        if info["error"]:
            return {"ok": False, "message": info["error"]}
        if not info["available"]:
            return {"ok": False, "message": "You already have the latest version (%s)." % info["current"]}
        if not info["can_install"]:
            return {"ok": False, "message": "Automatic install only works in the Windows app.",
                    "page": info["page"]}
        try:
            self._relaunch = updater.install(info)
        except (updater.UpdateError, OSError) as e:
            return {"ok": False, "message": "Update failed: %s" % e, "page": info["page"]}
        # Close shortly after replying; run() starts the new version once we've shut down.
        threading.Timer(1.0, self._main.destroy).start()
        return {"ok": True, "message": "Updated to %s. Restarting…" % info["latest"]}

    def export_csv(self, start="", end=""):
        """Ask where to save, then write the transactions CSV there."""
        import webview
        result = self._main.create_file_dialog(
            webview.FileDialog.SAVE, save_filename="transactions.csv",
            file_types=("CSV files (*.csv)", "All files (*.*)"))
        if not result:
            return None
        path = result if isinstance(result, str) else result[0]
        with self._app.lock:
            text = csvio.export_csv(self._app.conn, start=start or None, end=end or None)
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write(text)
        return path


def run(db_path=None, smoke_test=False):
    """Start the desktop app; returns the process exit code."""
    import webview

    if updater.can_self_update():
        # Remove the previous version left by an update (it may still be exiting for a moment).
        def _cleanup():
            for _ in range(10):
                updater.cleanup_old()
                if not os.path.exists(sys.executable + ".old"):
                    return
                time.sleep(3)
        threading.Thread(target=_cleanup, daemon=True).start()

    profiles = Profiles(db_path)
    profile_id = profiles.last_used()
    conn = profiles.open(profile_id)
    httpd, server_app, url = make_server(conn, port=8765, fallback_port=True,
                                         profiles=profiles, profile_id=profile_id)
    server_thread = threading.Thread(target=httpd.serve_forever, name="finorganizer-server", daemon=True)
    server_thread.start()

    api = Api(server_app, url)
    main = webview.create_window(APP_NAME, url, js_api=api, width=1280, height=860, min_size=(420, 520))
    api._main = main
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True

    def on_main_closed():
        # Closing the main window quits the app, including any summary windows.
        for w in list(webview.windows):
            if w is not main:
                try:
                    w.destroy()
                except Exception:
                    pass

    main.events.closed += on_main_closed

    outcome = {"ok": not smoke_test}

    def smoke(window):
        """CI check: wait for the dashboard to render, then close the window."""
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                ready = window.evaluate_js(
                    "!!document.querySelector('#view .card') && document.title === 'FinOrganizer'")
            except Exception:
                ready = False
            if ready:
                outcome["ok"] = True
                break
            time.sleep(0.5)
        window.destroy()

    storage = os.path.join(_data_dir(profiles), "webview")
    os.makedirs(storage, exist_ok=True)
    try:
        webview.start(smoke if smoke_test else None, (main,) if smoke_test else (),
                      private_mode=False, storage_path=storage)
    finally:
        # The window is gone: stop serving and release the database.
        httpd.shutdown()
        httpd.server_close()
        with server_app.lock:
            server_app.conn.close()
    if api._relaunch:
        updater.relaunch(api._relaunch)  # start the updated version now that we've let go
    return 0 if outcome["ok"] else 1


def _log_path(db_path):
    return os.path.join(_data_dir(Profiles(db_path)), "finorganizer-desktop.log")


def main(argv=None):
    parser = argparse.ArgumentParser(prog=APP_NAME, description="FinOrganizer desktop app")
    parser.add_argument("--db", help="database file (default: your FinOrganizer data folder)")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        code = run(args.db, args.smoke_test)
    except ImportError:
        _message_box(APP_NAME, "The desktop window needs the 'pywebview' package.\n\n"
                               "Install it with:  pip install pywebview", error=True)
        code = 2
    except Exception:
        details = traceback.format_exc()
        try:
            with open(_log_path(args.db), "a", encoding="utf-8") as f:
                f.write("\n--- %s ---\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), details))
            where = "\n\nDetails were saved to:\n" + _log_path(args.db)
        except OSError:
            where = ""
        advice = ("On Windows this usually means the Microsoft Edge WebView2 Runtime is missing; "
                  "install it from Microsoft, or use FinOrganizer-cli.exe, which opens the app in "
                  "your browser." if os.name == "nt" else
                  "Please send the details file to whoever maintains your copy of FinOrganizer.")
        _message_box(APP_NAME, "FinOrganizer couldn't start its window.\n\n%s%s\n\n%s"
                     % (details.strip().splitlines()[-1], where, advice), error=True)
        code = 1
    # Make sure no background thread (web engine, server) keeps the process alive.
    if sys.stdout:
        sys.stdout.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
