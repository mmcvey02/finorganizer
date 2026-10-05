"""Entry point for the iPhone / web version, which runs inside the browser.

There, FinOrganizer runs on Pyodide (Python compiled to WebAssembly) with no server:
the page's JavaScript calls ``request`` instead of sending HTTP requests, and the
data folder is kept in the browser's own storage. See packaging/build_web.py.
"""

import os

from . import server
from .profiles import Profiles

_app = None


def start(data_dir):
    """Open the data in ``data_dir`` (the last profile used)."""
    global _app
    os.makedirs(data_dir, exist_ok=True)
    profiles = Profiles(base_path=os.path.join(data_dir, "finorganizer.db"))
    pid = profiles.last_used()
    _app = server.App(profiles.open(pid), profiles, pid)
    return _app


def request(method, url, body=""):
    """Answer one request like the local server would.

    Returns (status, content_type, body_bytes, content_disposition).
    """
    if _app is None:
        raise RuntimeError("call start() first")
    path, _, query = url.partition("?")
    raw = body.encode("utf-8") if isinstance(body, str) else (body or b"")
    status, ctype, data, headers = server.respond(_app, method.upper(), path, query, raw)
    return status, ctype, data, headers.get("Content-Disposition", "")
