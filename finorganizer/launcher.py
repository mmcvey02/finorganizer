"""Entry point for the packaged command-line executable (FinOrganizer-cli.exe).

Double-clicking it (no arguments) starts the web app and opens it in the browser.
Run with arguments from a terminal, it behaves like the normal CLI, e.g.
``FinOrganizer-cli.exe report summary``. The desktop app (FinOrganizer.exe) is desktop.py.
"""

import sys
import traceback


def main():
    if len(sys.argv) > 1:
        from .cli import main as cli_main
        cli_main()
        return
    try:
        from .profiles import Profiles
        from .server import serve
        profiles = Profiles()
        pid = profiles.last_used()
        conn = profiles.open(pid)
        print("FinOrganizer")
        print("Your data is stored in: %s" % profiles.base_path)
        print("Additional profiles are stored in: %s" % profiles.dir)
        print("Keep this window open while you use the app; close it to quit.\n")
        serve(conn, open_browser=True, fallback_port=True, profiles=profiles, profile_id=pid)
    except Exception:
        # Window would vanish on a double-click; leave the error visible.
        traceback.print_exc()
        input("\nFinOrganizer hit an error. Press Enter to close.")


if __name__ == "__main__":
    main()
