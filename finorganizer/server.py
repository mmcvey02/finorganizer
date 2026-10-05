"""Local web app: a JSON API over the ledger plus the single-page UI in ./static.

Uses only the standard library. Money in the API is always integer cents.
"""

import json
import mimetypes
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import banksync, csvio, db, ledger, planning, reports, summary_page, updater
from .db import ACCOUNT_TYPES, CATEGORY_KINDS, FREQUENCIES
from .profiles import DEFAULT_ID
from .money import month_bounds, month_of, today

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _int(v, name="value"):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise HTTPError(400, "%s must be an integer" % name)


def _opt_int(v):
    return None if v in (None, "", "null") else _int(v)


def _num(body, key, default=None):
    v = body.get(key, default)
    if v is None:
        raise HTTPError(400, "%s is required" % key)
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HTTPError(400, "%s must be a number" % key)


class Router:
    def __init__(self):
        self.routes = []

    def add(self, method, pattern, fn):
        self.routes.append((method, re.compile("^" + pattern + "$"), fn))

    def match(self, method, path):
        allowed = False
        for m, rx, fn in self.routes:
            mo = rx.match(path)
            if mo:
                if m == method:
                    return fn, [int(g) if g.isdigit() else g for g in mo.groups()]
                allowed = True
        raise HTTPError(405 if allowed else 404, "not found")


def build_router(app=None):
    r = Router()
    L = ledger

    # meta
    r.add("GET", "/api/meta", lambda c, q, b: {
        "account_types": ACCOUNT_TYPES, "category_kinds": CATEGORY_KINDS,
        "frequencies": FREQUENCIES, "today": today().isoformat(),
        "version": updater.current_version(), "can_self_update": updater.can_self_update()})
    r.add("GET", "/api/dashboard", lambda c, q, b: reports.dashboard(c, q.get("month")))

    # accounts
    r.add("GET", "/api/accounts", lambda c, q, b: L.list_accounts(c, q.get("archived") == "1"))
    r.add("POST", "/api/accounts", lambda c, q, b: {"id": L.add_account(
        c, b.get("name"), b.get("type", "checking"), _int(b.get("opening_balance_cents", 0)),
        b.get("institution", ""), float(b.get("interest_rate") or 0))})
    r.add("PUT", r"/api/accounts/(\d+)", lambda c, q, b, i: L.update_account(c, i, **b))
    r.add("DELETE", r"/api/accounts/(\d+)", lambda c, q, b, i: L.delete_account(c, i))

    # categories
    r.add("GET", "/api/categories", lambda c, q, b: L.list_categories(c))
    r.add("POST", "/api/categories", lambda c, q, b: {"id": L.add_category(
        c, b.get("name"), b.get("kind", "expense"), b.get("group_name", ""))})
    r.add("PUT", r"/api/categories/(\d+)", lambda c, q, b, i: L.update_category(c, i, **b))
    r.add("DELETE", r"/api/categories/(\d+)", lambda c, q, b, i: L.delete_category(c, i))

    # transactions
    def list_tx(c, q, b):
        return L.list_transactions(
            c, account_id=_opt_int(q.get("account_id")), category_id=_opt_int(q.get("category_id")),
            start=q.get("start") or None, end=q.get("end") or None, search=q.get("search") or None,
            uncategorized=q.get("uncategorized") == "1", limit=_opt_int(q.get("limit")),
            offset=_opt_int(q.get("offset")) or 0)

    def add_tx(c, q, b):
        return {"id": L.add_transaction(
            c, _int(b.get("account_id"), "account_id"), b.get("date") or today(),
            _int(b.get("amount_cents"), "amount_cents"), b.get("payee", ""),
            _opt_int(b.get("category_id")), b.get("memo", ""), bool(b.get("cleared")))}

    def update_tx(c, q, b, i):
        if "category_id" in b:
            b["category_id"] = _opt_int(b["category_id"])
        L.update_transaction(c, i, **b)

    r.add("GET", "/api/transactions", list_tx)
    r.add("POST", "/api/transactions", add_tx)
    r.add("PUT", r"/api/transactions/(\d+)", update_tx)
    r.add("DELETE", r"/api/transactions/(\d+)", lambda c, q, b, i: L.delete_transaction(c, i))
    r.add("POST", "/api/transfers", lambda c, q, b: {"ids": L.add_transfer(
        c, _int(b.get("from_account_id")), _int(b.get("to_account_id")), b.get("date") or today(),
        _int(b.get("amount_cents")), b.get("memo", ""))})

    # budgets
    r.add("GET", "/api/budgets", lambda c, q, b: L.budget_status(c, q.get("month") or month_of(today())))
    r.add("PUT", "/api/budgets", lambda c, q, b: L.set_budget(
        c, _int(b.get("category_id")), b.get("month"), _int(b.get("amount_cents", 0))))
    r.add("POST", "/api/budgets/copy", lambda c, q, b: {"copied": L.copy_budgets(
        c, b.get("from_month"), b.get("to_month"), bool(b.get("overwrite")))})

    # goals
    r.add("GET", "/api/goals", lambda c, q, b: L.list_goals(c))
    r.add("POST", "/api/goals", lambda c, q, b: {"id": L.add_goal(
        c, b.get("name"), _int(b.get("target_cents")), b.get("target_date") or None,
        _opt_int(b.get("account_id")), _int(b.get("saved_cents", 0)), b.get("notes", ""))})
    r.add("PUT", r"/api/goals/(\d+)", lambda c, q, b, i: L.update_goal(c, i, **b))
    r.add("DELETE", r"/api/goals/(\d+)", lambda c, q, b, i: L.delete_goal(c, i))
    r.add("POST", r"/api/goals/(\d+)/contribute",
          lambda c, q, b, i: L.contribute_goal(c, i, _int(b.get("amount_cents"))))

    # recurring
    r.add("GET", "/api/recurring", lambda c, q, b: L.list_recurring(c))
    r.add("POST", "/api/recurring", lambda c, q, b: {"id": L.add_recurring(
        c, b.get("name"), _int(b.get("account_id")), _int(b.get("amount_cents")),
        b.get("frequency", "monthly"), b.get("next_date") or today(),
        _opt_int(b.get("category_id")), b.get("payee", ""))})
    r.add("PUT", r"/api/recurring/(\d+)", lambda c, q, b, i: L.update_recurring(c, i, **b))
    r.add("DELETE", r"/api/recurring/(\d+)", lambda c, q, b, i: L.delete_recurring(c, i))
    r.add("POST", "/api/recurring/post-due", lambda c, q, b: {"created": L.post_due(c)})
    r.add("GET", "/api/upcoming", lambda c, q, b: L.upcoming(c, _int(q.get("days", 30))))

    # reports
    def spending(c, q, b):
        month = q.get("month") or month_of(today())
        start, end = q.get("start"), q.get("end")
        if not (start and end):
            start, end = month_bounds(month)
        return {"start": start, "end": end,
                "summary": reports.period_summary(c, start, end),
                "spending": reports.spending_by_category(c, start, end),
                "income": reports.income_by_category(c, start, end),
                "top_payees": reports.top_payees(c, start, end)}

    r.add("GET", "/api/reports/spending", spending)
    r.add("GET", "/api/reports/cashflow",
          lambda c, q, b: reports.cash_flow(c, _int(q.get("months", 12)), q.get("end_month")))
    r.add("GET", "/api/reports/networth",
          lambda c, q, b: reports.net_worth_history(c, _int(q.get("months", 12))))
    r.add("GET", "/api/reports/year",
          lambda c, q, b: reports.year_review(c, _int(q.get("year", today().year))))
    r.add("GET", "/api/reports/averages",
          lambda c, q, b: reports.average_monthly(c, _int(q.get("months", 3))))

    # planning calculators
    r.add("POST", "/api/planning/loan", lambda c, q, b: planning.amortization(
        _int(b.get("principal_cents")), _num(b, "rate"), _int(b.get("months")),
        _int(b.get("extra_cents", 0))))
    r.add("POST", "/api/planning/debt", lambda c, q, b: {
        s: planning.debt_payoff(b.get("debts") or [], _int(b.get("budget_cents")), s)
        for s in ("avalanche", "snowball")})
    r.add("POST", "/api/planning/retirement", lambda c, q, b: planning.retirement_plan(
        _int(b.get("current_age")), _int(b.get("retirement_age")),
        _int(b.get("current_savings_cents", 0)), _int(b.get("monthly_contribution_cents", 0)),
        _num(b, "annual_return", 7), _num(b, "inflation", 2.5),
        _int(b.get("desired_annual_income_cents", 0)), _num(b, "withdrawal_rate", 4),
        _num(b, "contribution_growth", 0)))
    r.add("POST", "/api/planning/savings", lambda c, q, b: planning.savings_plan(
        _int(b.get("target_cents")), _int(b.get("current_cents", 0)), _int(b.get("months")),
        _num(b, "rate", 0)))
    r.add("POST", "/api/planning/growth", lambda c, q, b: planning.compound_growth(
        _int(b.get("principal_cents", 0)), _int(b.get("monthly_cents", 0)),
        _num(b, "rate", 7), _int(b.get("years")), _num(b, "inflation", 0)))

    def emergency(c, q, b):
        avg = reports.average_monthly(c, 3)
        liquid = sum(a["balance_cents"] for a in ledger.list_accounts(c)
                     if a["type"] in ("checking", "savings", "cash"))
        expenses = _int(b.get("monthly_expenses_cents") or avg["expense_cents"])
        result = planning.emergency_fund(expenses, liquid, _int(b.get("months_target", 6)))
        result["monthly_expenses_cents"] = expenses
        result["rule"] = planning.budget_rule(avg["income_cents"]) if avg["income_cents"] else None
        result["avg_income_cents"] = avg["income_cents"]
        return result

    r.add("POST", "/api/planning/emergency", emergency)

    # profiles (separate database per person)
    def need_profiles():
        if app is None or app.profiles is None:
            raise HTTPError(400, "profiles are not available")
        return app.profiles

    def list_profiles(c, q, b):
        if app is None or app.profiles is None:
            return {"current": DEFAULT_ID, "profiles": [{"id": DEFAULT_ID, "name": "Default"}]}
        return {"current": app.profile_id, "profiles": app.profiles.list()}

    def create_profile(c, q, b):
        pid = need_profiles().create(b.get("name"))
        if b.get("switch", True):
            app.switch(pid)
        return {"id": pid}

    def delete_profile(c, q, b, pid):
        pid = str(pid)
        if pid == app.profile_id:
            app.switch(DEFAULT_ID)  # close it first; Windows can't delete an open file
        need_profiles().delete(pid)

    r.add("GET", "/api/profiles", list_profiles)
    r.add("POST", "/api/profiles", create_profile)
    def switch_profile(c, q, b):
        need_profiles()
        app.switch(str(b.get("id") or ""))

    r.add("POST", "/api/profiles/switch", switch_profile)
    r.add("PUT", r"/api/profiles/([a-z0-9-]+)", lambda c, q, b, pid: need_profiles().rename(str(pid), b.get("name")))
    r.add("DELETE", r"/api/profiles/([a-z0-9-]+)", delete_profile)

    # linked bank connections (SimpleFIN)
    r.add("GET", "/api/connections", lambda c, q, b: banksync.list_connections(c))
    r.add("POST", "/api/connections", lambda c, q, b: banksync.connect(
        c, b.get("setup_token") or "", b.get("label") or ""))
    r.add("DELETE", r"/api/connections/(\d+)", lambda c, q, b, i: banksync.delete_connection(c, i))
    r.add("POST", "/api/connections/sync", lambda c, q, b: banksync.sync(c))
    r.add("PUT", r"/api/remote-accounts/(\d+)", lambda c, q, b, i: banksync.link_remote(
        c, i, b.get("action"), _opt_int(b.get("account_id")), b.get("type") or None))
    r.add("POST", r"/api/remote-accounts/(\d+)/match-balance",
          lambda c, q, b, i: banksync.match_bank_balance(c, i))

    # import / export
    r.add("POST", "/api/import", lambda c, q, b: csvio.import_csv(
        c, _int(b.get("account_id")), b.get("csv") or "", bool(b.get("invert")),
        bool(b.get("day_first"))))
    return r


class App:
    def __init__(self, conn, profiles=None, profile_id=DEFAULT_ID):
        self.conn = conn
        self.profiles = profiles
        self.profile_id = profile_id
        self.lock = threading.RLock()
        self.router = build_router(self)

    @property
    def profile_name(self):
        return db.get_setting(self.conn, "profile_name") or (
            "" if self.profile_id == DEFAULT_ID else self.profile_id)

    def switch(self, profile_id):
        """Point the app at another profile's database (caller holds the lock)."""
        new_conn = self.profiles.open(profile_id)
        old, self.conn, self.profile_id = self.conn, new_conn, profile_id
        old.close()
        self.profiles.remember(profile_id)

    def handle(self, method, path, query, body):
        fn, args = self.router.match(method, path)
        with self.lock:
            try:
                return fn(self.conn, query, body, *args)
            except ledger.NotFound as e:
                raise HTTPError(404, str(e))
            except LookupError as e:
                raise HTTPError(404, str(e).strip("'\""))
            except banksync.SyncError as e:
                self.conn.rollback()
                raise HTTPError(502, str(e))
            except sqlite3.IntegrityError as e:
                self.conn.rollback()
                msg = str(e)
                if "UNIQUE" in msg:
                    msg = "that name is already in use"
                raise HTTPError(409, msg)
            except (ValueError, TypeError) as e:
                self.conn.rollback()
                raise HTTPError(400, str(e))


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FinOrganizer"

        def log_message(self, fmt, *args):
            if os.environ.get("FINORGANIZER_LOG"):
                super().log_message(fmt, *args)

        def _send(self, status, payload, content_type="application/json; charset=utf-8", headers=None):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/api/export.csv" and method == "GET":
                    with app.lock:
                        text = csvio.export_csv(app.conn, start=query.get("start") or None,
                                                end=query.get("end") or None)
                    return self._send(200, text.encode(), "text/csv; charset=utf-8",
                                      {"Content-Disposition": 'attachment; filename="transactions.csv"'})
                if url.path == "/api/update" and method == "GET":
                    # Network call to GitHub: done outside the database lock.
                    return self._send(200, updater.check())
                if url.path == "/summary" and method == "GET":
                    with app.lock:
                        page = summary_page.render(app.conn, query.get("month") or None,
                                                   app.profile_name, query.get("print") == "1")
                    return self._send(200, page.encode(), "text/html; charset=utf-8")
                if not url.path.startswith("/api/"):
                    if method != "GET":
                        raise HTTPError(405, "method not allowed")
                    return self._static(url.path)
                body = {}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(length))
                    except ValueError:
                        raise HTTPError(400, "invalid JSON body")
                    if not isinstance(body, dict):
                        raise HTTPError(400, "JSON body must be an object")
                result = app.handle(method, url.path, query, body)
                self._send(200, {"ok": True} if result is None else result)
            except HTTPError as e:
                self._send(e.status, {"error": str(e)})

        def _static(self, path):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            full = os.path.realpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
                raise HTTPError(404, "not found")
            with open(full, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, data, ctype)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def make_server(conn, host="127.0.0.1", port=8765, fallback_port=False,
                profiles=None, profile_id=DEFAULT_ID):
    """Create (but don't start) the HTTP server. Returns (httpd, app, url).

    With ``fallback_port``, pick a free port if ``port`` is taken.
    """
    app = App(conn, profiles, profile_id)
    handler = make_handler(app)
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError:
        if not fallback_port:
            raise
        httpd = ThreadingHTTPServer((host, 0), handler)
    httpd.daemon_threads = True
    return httpd, app, "http://%s:%d" % (host, httpd.server_address[1])


def serve(conn, host="127.0.0.1", port=8765, open_browser=False, fallback_port=False,
          profiles=None, profile_id=DEFAULT_ID):
    """Run the web app in the foreground until Ctrl+C."""
    httpd, _app, url = make_server(conn, host, port, fallback_port, profiles, profile_id)
    print("FinOrganizer running at %s  (Ctrl+C to stop)" % url, flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
