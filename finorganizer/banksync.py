"""Automatic account syncing through SimpleFIN Bridge (https://beta-bridge.simplefin.org).

SimpleFIN gives read-only access to balances and transactions. The user creates a
*setup token* on the SimpleFIN Bridge site; we exchange it once for an *access URL*
(which embeds read-only credentials) and use that to download data.

Flow:
  1. connect(conn, setup_token)    -> claims the token, stores the access URL, discovers accounts
  2. link_remote(...)              -> user picks, per bank account: create new / link existing / ignore
  3. sync(conn)                    -> imports new posted transactions into linked accounts
"""

import base64
import binascii
import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .csvio import _payee_category_map
from .db import row, rows
from .ledger import NotFound, add_account, detect_transfers, get_account
from .money import to_cents
from . import __version__
from .net import describe_ssl_error, ssl_context

TIMEOUT = 60
FIRST_SYNC_DAYS = 90      # history fetched the first time an account is linked
OVERLAP_DAYS = 7          # re-check this many days before the last sync (late-posting items)
WINDOW_DAYS = 60          # SimpleFIN servers limit how many days one request may cover
MATCH_DAYS = 3            # a manual entry this close in date with the same amount is the same txn


class SyncError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# ----------------------------------------------------------------- HTTP layer

# Some hosting firewalls reject the default "Python-urllib" agent, so identify ourselves.
USER_AGENT = "FinOrganizer/%s (+https://github.com/mmcvey02/finorganizer)" % __version__


def _body_hint(err):
    try:
        text = err.read(400).decode("utf-8", "replace")
    except Exception:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return (": " + text[:160]) if text else ""


def _open(req):
    req.add_header("User-Agent", USER_AGENT)
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl_context())
    except urllib.error.HTTPError as e:
        hint = _body_hint(e)
        if e.code in (401, 403):
            raise SyncError("SimpleFIN refused access (HTTP %d%s)" % (e.code, hint), e.code)
        raise SyncError("SimpleFIN returned HTTP %d%s" % (e.code, hint), e.code)
    except urllib.error.URLError as e:
        raise SyncError("could not reach SimpleFIN: %s" % describe_ssl_error(e))


# Characters that sneak in when copying from web pages and emails.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00a0"), None)


def parse_setup_input(text):
    """Work out what the user pasted. Returns ("claim", claim_url) or ("access", access_url).

    Accepts a setup token (standard or URL-safe base64, with or without padding, even
    wrapped in quotes or after a "Setup token:" label), a claim URL, or an access URL.
    """
    t = (text or "").translate(_INVISIBLE).strip()
    url = re.search(r"https?://[^\s\"'<>`]+", t)
    if url:
        found = url.group(0).rstrip(".,;)")
        parts = urllib.parse.urlsplit(found)
        if "/claim/" in parts.path:
            return "claim", found
        if parts.username:
            return "access", found
        raise ValueError("that's a web address, not a setup token. On SimpleFIN Bridge, create a "
                         "new connection and copy the long setup token it shows")
    candidates = sorted(re.findall(r"[A-Za-z0-9+/_=-]{16,}", "".join(t.split())), key=len, reverse=True)
    for cand in candidates:
        body = cand.rstrip("=")
        body += "=" * (-len(body) % 4)
        for decode in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decode(body).decode("utf-8").strip()
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            if decoded.startswith(("https://", "http://")):
                kind = "access" if urllib.parse.urlsplit(decoded).username else "claim"
                return kind, decoded
    raise ValueError("that doesn't look like a SimpleFIN setup token. Copy the whole token "
                     "(a long string of letters and numbers) from SimpleFIN Bridge and paste it again")


def claim_setup_token(token):
    """Exchange a one-time setup token (or claim URL) for a long-lived access URL."""
    kind, url = parse_setup_input(token)
    if kind == "access":
        return url
    req = urllib.request.Request(url, data=b"", method="POST", headers={"Content-Length": "0"})
    try:
        with _open(req) as res:
            access_url = res.read().decode("utf-8", "replace").strip()
    except SyncError as e:
        if e.status == 403:
            raise SyncError("SimpleFIN says this setup token was already used or has been revoked. "
                            "Each token works only once. Create a new one on SimpleFIN Bridge "
                            "and paste that (%s)" % e, e.status)
        raise
    if not access_url.startswith(("https://", "http://")):
        raise SyncError("unexpected response when claiming the setup token: %r" % access_url[:80])
    return access_url


def fetch_accounts(access_url, start=None, end=None, balances_only=False):
    """GET {access_url}/accounts. ``start``/``end`` are dates; returns the parsed JSON."""
    parts = urllib.parse.urlsplit(access_url)
    netloc = parts.hostname + (":%d" % parts.port if parts.port else "")
    params = {}
    if start:
        params["start-date"] = int(dt.datetime.combine(start, dt.time()).replace(
            tzinfo=dt.timezone.utc).timestamp())
    if end:
        params["end-date"] = int(dt.datetime.combine(end, dt.time()).replace(
            tzinfo=dt.timezone.utc).timestamp())
    if balances_only:
        params["balances-only"] = 1
    url = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/") + "/accounts",
                                   urllib.parse.urlencode(params), ""))
    headers = {}
    if parts.username:
        cred = "%s:%s" % (urllib.parse.unquote(parts.username), urllib.parse.unquote(parts.password or ""))
        headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
    with _open(urllib.request.Request(url, headers=headers)) as res:
        try:
            return json.loads(res.read())
        except ValueError:
            raise SyncError("SimpleFIN sent a response that isn't valid JSON")


def _errors(payload):
    out = list(payload.get("errors") or [])
    for e in payload.get("errlist") or []:  # newer servers send structured errors
        out.append((e.get("msg") or e.get("message") or str(e)) if isinstance(e, dict) else str(e))
    return [str(e) for e in out]


def _ts_date(ts):
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date()


def mask_url(access_url):
    p = urllib.parse.urlsplit(access_url)
    return "%s://%s" % (p.scheme, p.hostname or "")


# --------------------------------------------------------------- persistence

def list_connections(conn):
    out = []
    for c in rows(conn.execute("SELECT * FROM connections ORDER BY id")):
        c["host"] = mask_url(c.pop("access_url"))
        c["accounts"] = rows(conn.execute(
            """SELECT r.*, a.name AS account_name, a.opening_balance_cents
                      + COALESCE((SELECT SUM(amount_cents) FROM transactions t WHERE t.account_id = a.id), 0)
                      AS local_balance_cents
               FROM remote_accounts r LEFT JOIN accounts a ON a.id = r.account_id
               WHERE r.connection_id = ? ORDER BY r.institution, r.name""", (c["id"],)))
        for r in c["accounts"]:
            r["status"] = "ignored" if r["ignored"] else ("linked" if r["account_id"] else "new")
            r["difference_cents"] = (r["balance_cents"] - r["local_balance_cents"]
                                     if r["account_id"] and r["balance_cents"] is not None else None)
        out.append(c)
    return out


def _upsert_remote(conn, connection_id, accounts):
    for a in accounts:
        org = a.get("org") or {}
        bal = a.get("balance")
        conn.execute(
            """INSERT INTO remote_accounts (connection_id, remote_id, name, institution, currency,
                                            balance_cents, balance_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (connection_id, remote_id) DO UPDATE SET
                   name = excluded.name, institution = excluded.institution,
                   currency = excluded.currency, balance_cents = excluded.balance_cents,
                   balance_date = excluded.balance_date""",
            (connection_id, str(a["id"]), a.get("name") or "Account",
             org.get("name") or org.get("domain") or "", a.get("currency") or "USD",
             to_cents(bal) if bal not in (None, "") else None,
             _ts_date(a["balance-date"]).isoformat() if a.get("balance-date") else None))
    conn.commit()


def connect(conn, setup_token, label=""):
    """Claim a setup token and save the connection, then discover its accounts.

    The access URL is saved *before* contacting the bank data endpoint: a setup token
    can only be claimed once, so a later network hiccup must not throw the access away.
    Returns ``{"id": connection_id, "warning": message_or_None}``.
    """
    access_url = claim_setup_token(setup_token)
    result = add_connection(conn, access_url, label)
    if result["warning"] is None or "SimpleFIN reported" in result["warning"]:
        # Download right away so balances and transactions appear without another click.
        result["sync"] = sync_connection(conn, result["id"])
        if result["sync"]["errors"] and result["warning"] is None:
            result["warning"] = "Connected, but the first download had a problem: " + \
                "; ".join(result["sync"]["errors"])
    return result


def add_connection(conn, access_url, label=""):
    existing = row(conn.execute("SELECT id FROM connections WHERE access_url = ?", (access_url,)))
    if existing:
        cid = existing["id"]
    else:
        cid = conn.execute("INSERT INTO connections (label, access_url) VALUES (?, ?)",
                           (label or "SimpleFIN", access_url)).lastrowid
        conn.commit()
    try:
        payload = fetch_accounts(access_url, balances_only=True)
    except SyncError as e:
        msg = "Connected, but couldn't load your accounts yet (%s). Try Sync now in a minute." % e
        conn.execute("UPDATE connections SET last_error = ? WHERE id = ?", (str(e), cid))
        conn.commit()
        return {"id": cid, "warning": msg}
    accounts = payload.get("accounts") or []
    if not label:
        orgs = sorted({(a.get("org") or {}).get("name") or "" for a in accounts} - {""})
        label = ", ".join(orgs)[:80] or "SimpleFIN"
    errors = _errors(payload)
    conn.execute("UPDATE connections SET label = ?, last_error = ? WHERE id = ?",
                 (label, "; ".join(errors) or None, cid))
    conn.commit()
    _upsert_remote(conn, cid, accounts)
    warning = None
    if errors:
        warning = "SimpleFIN reported: " + "; ".join(errors)
    elif not accounts:
        warning = ("Connected, but SimpleFIN didn't return any accounts. Make sure you've linked "
                   "your banks on the SimpleFIN Bridge site, then click Sync now.")
    return {"id": cid, "warning": warning}


def delete_connection(conn, connection_id):
    """Forget a connection. Imported transactions and accounts are kept."""
    cur = conn.execute("DELETE FROM connections WHERE id = ?", (connection_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise NotFound("connection %s not found" % connection_id)


def _guess_type(name, balance_cents):
    n = name.lower()
    for words, t in ((("credit", "card", "visa", "mastercard", "amex"), "credit_card"),
                     (("mortgage",), "mortgage"), (("loan", "auto"), "loan"),
                     (("401", "ira", "roth", "retire"), "retirement"),
                     (("brokerage", "invest", "stock"), "investment"),
                     (("saving",), "savings")):
        if any(w in n for w in words):
            return t
    return "credit_card" if (balance_cents or 0) < 0 else "checking"


def link_remote(conn, remote_account_id, action, account_id=None, type=None):
    """Decide what a bank account feeds: action is 'new', 'link' or 'ignore'."""
    r = row(conn.execute("SELECT * FROM remote_accounts WHERE id = ?", (remote_account_id,)))
    if r is None:
        raise NotFound("bank account %s not found" % remote_account_id)
    if action == "ignore":
        conn.execute("UPDATE remote_accounts SET ignored = 1, account_id = NULL WHERE id = ?", (r["id"],))
    elif action == "link":
        get_account(conn, account_id)
        taken = conn.execute("SELECT 1 FROM remote_accounts WHERE account_id = ? AND id != ?",
                             (account_id, r["id"])).fetchone()
        if taken:
            raise ValueError("that account is already linked to another bank account")
        conn.execute("UPDATE remote_accounts SET account_id = ?, ignored = 0 WHERE id = ?",
                     (account_id, r["id"]))
        _flag(conn, "backfill", account_id)
    elif action == "new":
        base = r["name"] if not r["institution"] else "%s (%s)" % (r["name"], r["institution"])
        name, n = base, 2
        while conn.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
            name, n = "%s %d" % (base, n), n + 1
        new_id = add_account(conn, name, type or _guess_type(r["name"], r["balance_cents"]),
                             r["balance_cents"] or 0, r["institution"])
        conn.execute("UPDATE remote_accounts SET account_id = ?, ignored = 0 WHERE id = ?",
                     (new_id, r["id"]))
        # Opening balance is the bank balance until history arrives; the next sync rebases it.
        _flag(conn, "backfill", new_id)
        _flag(conn, "rebase_opening", new_id)
    else:
        raise ValueError("action must be new, link or ignore")
    conn.commit()


def _flag(conn, name, account_id):
    """Per-account to-do markers for the next sync, kept in the settings table."""
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
                 ("%s:%d" % (name, account_id),))


def _take_flag(conn, name, account_id):
    return conn.execute("DELETE FROM settings WHERE key = ?",
                        ("%s:%d" % (name, account_id),)).rowcount > 0


def match_bank_balance(conn, remote_account_id):
    """Adjust the linked account's opening balance so its balance equals the bank's."""
    r = row(conn.execute("SELECT * FROM remote_accounts WHERE id = ?", (remote_account_id,)))
    if r is None or not r["account_id"] or r["balance_cents"] is None:
        raise ValueError("bank account is not linked or has no balance")
    local = get_account(conn, r["account_id"])["balance_cents"]
    conn.execute("UPDATE accounts SET opening_balance_cents = opening_balance_cents + ? WHERE id = ?",
                 (r["balance_cents"] - local, r["account_id"]))
    conn.commit()


# ---------------------------------------------------------------------- sync

def _windows(start, end):
    cur = start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=WINDOW_DAYS), end)
        yield cur, nxt
        cur = nxt


def _import_transactions(conn, account_id, txns, learned):
    imported = matched = 0
    for t in txns:
        if t.get("pending"):
            continue  # pending items change id/amount when they post; take them once posted
        ext = str(t["id"])
        if conn.execute("SELECT 1 FROM transactions WHERE account_id = ? AND external_id = ?",
                        (account_id, ext)).fetchone():
            continue
        date = _ts_date(t.get("posted") or t.get("transacted_at"))
        amount = to_cents(t["amount"])
        payee = (t.get("payee") or t.get("description") or "").strip()
        memo = (t.get("memo") or "").strip()
        if payee and t.get("description") and t["description"].strip() != payee and not memo:
            memo = t["description"].strip()
        # Same transaction already typed in by hand? Attach the bank id instead of duplicating.
        manual = conn.execute(
            """SELECT id FROM transactions WHERE account_id = ? AND external_id IS NULL
               AND transfer_id IS NULL AND amount_cents = ? AND date BETWEEN ? AND ?
               ORDER BY ABS(julianday(date) - julianday(?)) LIMIT 1""",
            (account_id, amount, (date - dt.timedelta(days=MATCH_DAYS)).isoformat(),
             (date + dt.timedelta(days=MATCH_DAYS)).isoformat(), date.isoformat())).fetchone()
        if manual:
            conn.execute("UPDATE transactions SET external_id = ?, cleared = 1 WHERE id = ?",
                         (ext, manual[0]))
            matched += 1
            continue
        conn.execute(
            "INSERT INTO transactions (account_id, date, amount_cents, payee, category_id, memo,"
            " cleared, external_id) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (account_id, date.isoformat(), amount, payee, learned.get(payee.lower()), memo, ext))
        imported += 1
    return imported, matched


def auto_assign(conn, connection_id):
    """Give every undecided bank account somewhere to go, so its data shows up right away.

    Links it to an existing, not-yet-linked account with the same name, or creates a new
    account. Accounts the user marked "Don't import" are left alone, and any choice can be
    changed later on the Accounts page. Returns the names of the local accounts used.
    """
    used = []
    for r in rows(conn.execute("SELECT * FROM remote_accounts WHERE connection_id = ?"
                               " AND account_id IS NULL AND ignored = 0", (connection_id,))):
        names = [r["name"]] + (["%s (%s)" % (r["name"], r["institution"])] if r["institution"] else [])
        match = None
        for name in names:
            match = row(conn.execute(
                "SELECT id, name FROM accounts WHERE name = ? COLLATE NOCASE AND archived = 0"
                " AND id NOT IN (SELECT account_id FROM remote_accounts WHERE account_id IS NOT NULL)",
                (name,)))
            if match:
                break
        if match:
            link_remote(conn, r["id"], "link", match["id"])
            used.append(match["name"])
        else:
            link_remote(conn, r["id"], "new")
            acct = row(conn.execute("SELECT a.name FROM remote_accounts r JOIN accounts a"
                                    " ON a.id = r.account_id WHERE r.id = ?", (r["id"],)))
            used.append(acct["name"])
    return used


def sync_connection(conn, connection_id, today=None, _rerun=False):
    c = row(conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)))
    if c is None:
        raise NotFound("connection %s not found" % connection_id)
    today = today or dt.date.today()
    added = auto_assign(conn, connection_id)
    end = today + dt.timedelta(days=1)
    if c["last_sync"]:
        start = dt.date.fromisoformat(c["last_sync"][:10]) - dt.timedelta(days=OVERLAP_DAYS)
    else:
        start = today - dt.timedelta(days=FIRST_SYNC_DAYS)
    # Newly linked accounts get full history even if the connection synced before.
    for name in ("backfill", "rebase_opening"):
        conn.execute("DELETE FROM settings WHERE key LIKE ? AND CAST(substr(key, ?) AS INTEGER)"
                     " NOT IN (SELECT id FROM accounts)", (name + ":%", len(name) + 2))
    if conn.execute("SELECT 1 FROM settings WHERE key LIKE 'backfill:%'").fetchone():
        start = min(start, today - dt.timedelta(days=FIRST_SYNC_DAYS))

    result = {"connection_id": connection_id, "label": c["label"], "imported": 0, "matched": 0,
              "added_accounts": added, "errors": []}
    learned = _payee_category_map(conn)
    discovered = []
    try:
        remote_by_id = {}
        for w_start, w_end in _windows(start, end):
            payload = fetch_accounts(c["access_url"], w_start, w_end)
            result["errors"] += [e for e in _errors(payload) if e not in result["errors"]]
            accounts = payload.get("accounts") or []
            _upsert_remote(conn, connection_id, accounts)
            for a in accounts:
                remote_by_id.setdefault(str(a["id"]), []).extend(a.get("transactions") or [])
        # Bank accounts that appeared for the first time in this download.
        before = {r["id"] for r in rows(conn.execute(
            "SELECT id FROM remote_accounts WHERE connection_id = ? AND account_id IS NOT NULL",
            (connection_id,)))}
        discovered = auto_assign(conn, connection_id)
        for r in rows(conn.execute("SELECT * FROM remote_accounts WHERE connection_id = ?",
                                   (connection_id,))):
            if r["ignored"] or not r["account_id"]:
                continue
            if r["id"] not in before:
                continue  # just discovered: the follow-up pass fetches its full history
            imp, mat = _import_transactions(conn, r["account_id"], remote_by_id.get(r["remote_id"], []), learned)
            result["imported"] += imp
            result["matched"] += mat
            _take_flag(conn, "backfill", r["account_id"])
            if _take_flag(conn, "rebase_opening", r["account_id"]):
                # Account was created from the bank: make history + opening add up to the bank balance.
                if r["balance_cents"] is not None:
                    total = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM transactions"
                                         " WHERE account_id = ?", (r["account_id"],)).fetchone()[0]
                    conn.execute("UPDATE accounts SET opening_balance_cents = ? WHERE id = ?",
                                 (r["balance_cents"] - total, r["account_id"]))
        conn.execute("UPDATE connections SET last_sync = ?, last_error = ? WHERE id = ?",
                     (dt.datetime.now().isoformat(timespec="seconds"),
                      "; ".join(result["errors"]) or None, connection_id))
        conn.commit()
        # Payments between your own linked accounts arrive twice; pair them up as transfers.
        result["transfers_linked"] = detect_transfers(conn)
    except SyncError as e:
        conn.rollback()
        if e.status in (401, 403):
            e = SyncError("%s. Access was revoked or expired: disconnect this bank and connect "
                          "it again with a new setup token" % e, e.status)
        conn.execute("UPDATE connections SET last_error = ? WHERE id = ?", (str(e), connection_id))
        conn.commit()
        result["errors"].append(str(e))
        return result
    if discovered and not _rerun:
        more = sync_connection(conn, connection_id, today, _rerun=True)
        result["imported"] += more["imported"]
        result["matched"] += more["matched"]
        result["transfers_linked"] = result.get("transfers_linked", 0) + more.get("transfers_linked", 0)
        result["added_accounts"] += discovered
        result["errors"] += [e for e in more["errors"] if e not in result["errors"]]
    return result


def sync(conn, today=None):
    """Sync every connection. Returns per-connection results."""
    return [sync_connection(conn, c["id"], today)
            for c in rows(conn.execute("SELECT id FROM connections ORDER BY id"))]
