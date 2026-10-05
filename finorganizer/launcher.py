"""Entry point for the packaged desktop executable (FinOrganizer.exe).

Double-clicking it (no arguments) starts the web app and opens the browser.
Run with arguments from a terminal, it behaves like the normal CLI, e.g.
``FinOrganizer.exe report summary``.
"""

import sys
import traceback


def main():
    if len(sys.argv) > 1:
        from .cli import main as cli_main
        cli_main()
        return
    try:
        from . import db
        from .server import serve
        conn = db.connect()
        print("FinOrganizer")
        print("Your data is stored in: %s" % db.DEFAULT_DB_PATH)
        print("Keep this window open while you use the app; close it to quit.\n")
        serve(conn, open_browser=True, fallback_port=True)
    except Exception:
        # Window would vanish on a double-click; leave the error visible.
        traceback.print_exc()
        input("\nFinOrganizer hit an error. Press Enter to close.")


if __name__ == "__main__":
    main()
