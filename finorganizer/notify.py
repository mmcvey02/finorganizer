"""Notifications: email and iPhone alerts about bills, budgets, unusual activity and a
weekly summary. Everything here is per profile (stored in the profile's database).

``run`` checks one profile and sends whatever is due. Each alert has a key and goes out
once: keys are claimed in the ``notifications_sent`` table before sending, so the app and
the background check (see schedule.py) never send the same alert twice. Alerts that come
up during quiet hours wait for the next check after 7 am.
"""

import datetime as dt
import hashlib
import json
import os
import smtplib
import socket
import ssl
import uuid
from email.message import EmailMessage
from html import escape

from . import webpush
from .db import get_setting, rows, set_setting
from .ledger import budget_status, list_accounts, unmatched_transfers, upcoming
from .money import fmt
from .net import ssl_context

DEFAULTS = {
    "enabled": False,
    "events": {"bills": True, "budgets": True, "activity": True, "weekly": True},
    "bill_days": 3,               # remind this many days before a bill is due
    "large_cents": 50000,         # "large purchase" from $500
    "low_balance_cents": 10000,   # checking/savings/cash below $100
    "quiet_hours": True,          # nothing between 10 pm and 7 am
    "background": True,           # check even when FinOrganizer is closed
    "email": {"enabled": False, "to": "", "from": "", "host": "", "port": 587,
              "security": "starttls", "username": ""},
    "push": {"enabled": True},
}
QUIET_START, QUIET_END = 22, 7
PASSWORD_KEY = "notify_smtp_password"
DEVICES_KEY = "notify_devices"
MAX_PUSHES = 4          # per check; more alerts are folded into one "and N more"
KEEP_SENT_DAYS = 400    # forget sent keys after this long
SMTP_TIMEOUT = 30
SECURITY = ("starttls", "ssl", "none")


# --------------------------------------------------------------------- settings

def _merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(base[k], v) if isinstance(base.get(k), dict) and isinstance(v, dict) else v
    return out


def get_config(conn):
    try:
        saved = json.loads(get_setting(conn, "notify_config") or "{}")
    except ValueError:
        saved = {}
    return _merge(DEFAULTS, saved if isinstance(saved, dict) else {})


def devices(conn):
    try:
        found = json.loads(get_setting(conn, DEVICES_KEY) or "[]")
    except ValueError:
        return []
    return [d for d in found if isinstance(d, dict) and d.get("endpoint")]


def _save_devices(conn, items):
    set_setting(conn, DEVICES_KEY, json.dumps(items))


def _as_int(v, what, low, high):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError("%s must be a whole number" % what)
    if not low <= v <= high:
        raise ValueError("%s must be between %d and %d" % (what, low, high))
    return v


def update_config(conn, changes):
    """Apply settings from the Settings page; checks each value. Returns the new config."""
    cfg = get_config(conn)
    was_on = cfg["enabled"]
    changes = dict(changes or {})
    password = changes.pop("password", None)
    email = changes.pop("email", None) or {}
    for key in ("enabled", "quiet_hours", "background"):
        if key in changes:
            cfg[key] = bool(changes[key])
    for key, value in (changes.get("events") or {}).items():
        if key in cfg["events"]:
            cfg["events"][key] = bool(value)
    if "push" in changes and isinstance(changes["push"], dict) and "enabled" in changes["push"]:
        cfg["push"]["enabled"] = bool(changes["push"]["enabled"])
    if "bill_days" in changes:
        cfg["bill_days"] = _as_int(changes["bill_days"], "days before a bill", 0, 30)
    if "large_cents" in changes:
        cfg["large_cents"] = _as_int(changes["large_cents"], "large purchase amount", 1, 10 ** 11)
    if "low_balance_cents" in changes:
        cfg["low_balance_cents"] = _as_int(changes["low_balance_cents"], "low balance amount", 0, 10 ** 11)
    e = cfg["email"]
    for key in ("to", "from", "host", "username"):
        if key in email:
            e[key] = str(email[key] or "").strip()
    if "enabled" in email:
        e["enabled"] = bool(email["enabled"])
    if "port" in email:
        e["port"] = _as_int(email["port"], "email server port", 1, 65535)
    if "security" in email:
        if email["security"] not in SECURITY:
            raise ValueError("email security must be one of: %s" % ", ".join(SECURITY))
        e["security"] = email["security"]
    for key in ("to", "from"):
        if e[key] and ("@" not in e[key] or any(c in e[key] for c in "\r\n,;<> ")):
            raise ValueError("%s doesn't look like an email address" % ("the address to send to" if key == "to" else "the sender address"))
    if e["enabled"] and not (e["to"] and e["host"]):
        raise ValueError("to send email, fill in your email address and the email server")
    set_setting(conn, "notify_config", json.dumps(cfg))
    if password is not None:
        set_setting(conn, PASSWORD_KEY, str(password))
    if cfg["enabled"] and not was_on:
        _start_from_now(conn)
    return cfg


def _start_from_now(conn):
    """Only transactions from now on count as new activity (no flood of old purchases)."""
    top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM transactions").fetchone()[0]
    set_setting(conn, "notify_since_tx", str(top))


def add_device(conn, code):
    device = webpush.parse_pairing_code(code)
    items = [d for d in devices(conn) if d["endpoint"] != device["endpoint"]]
    device["id"] = "d" + uuid.uuid4().hex[:11]  # starts with a letter: never read as a number
    device["added"] = dt.date.today().isoformat()
    items.append(device)
    _save_devices(conn, items)
    return {"id": device["id"], "name": device["name"]}


def remove_device(conn, device_id):
    items = devices(conn)
    left = [d for d in items if d.get("id") != device_id]
    if len(left) == len(items):
        raise LookupError("device not found")
    _save_devices(conn, left)


def public_state(conn):
    """What the Settings page shows: never the password or the devices' keys."""
    from . import schedule
    cfg = get_config(conn)
    try:
        last = json.loads(get_setting(conn, "notify_last_result") or "null")
    except ValueError:
        last = None
    return {
        "config": cfg,
        "has_password": bool(get_setting(conn, PASSWORD_KEY)),
        "devices": [{"id": d.get("id"), "name": d.get("name"), "added": d.get("added"),
                     "service": _service(d["endpoint"])} for d in devices(conn)],
        "push_available": webpush.available(),
        "last_run": get_setting(conn, "notify_last_run"),
        "last_result": last,
        "background": schedule.status(),
    }


def _service(endpoint):
    host = endpoint.split("/")[2] if "//" in endpoint else ""
    return "Apple" if host.endswith("apple.com") else "Google" if "google" in host else host


# ----------------------------------------------------------------------- alerts

def _alert(key, kind, title, body, url="#dashboard"):
    return {"key": key, "kind": kind, "title": title, "body": body, "url": url}


def _day(d):
    return d.strftime("%a %b %d").replace(" 0", " ")


def collect(conn, cfg, now):
    """Every alert that applies right now (sent or not)."""
    today = now.date()
    events = cfg["events"]
    out = []
    if events.get("bills"):
        for o in upcoming(conn, cfg["bill_days"], as_of=today):
            if o["amount_cents"] >= 0:
                continue  # money coming in (e.g. a paycheck) isn't a bill
            d = dt.date.fromisoformat(o["date"])
            when = "overdue" if d < today else "today" if d == today else "tomorrow" if d == today + dt.timedelta(1) else _day(d)
            out.append(_alert("bill:%d:%s" % (o["recurring_id"], o["date"]), "bills",
                              "%s %s: %s" % ("Bill" if d >= today else "Unpaid bill", "due " + when if d >= today else "overdue", o["name"]),
                              "%s from %s, due %s." % (fmt(-o["amount_cents"]), o["account_name"], _day(d)), "#recurring"))
    if events.get("budgets"):
        month = today.strftime("%Y-%m")
        for b in budget_status(conn, month):
            if not b["budget_cents"] or b["spent_cents"] <= 0:
                continue
            share = b["spent_cents"] / b["budget_cents"]
            spent = "%s of %s spent this month." % (fmt(b["spent_cents"]), fmt(b["budget_cents"]))
            if b["spent_cents"] > b["budget_cents"]:  # exactly on budget isn't over it
                out.append(_alert("budget:%d:%s:100" % (b["category_id"], month), "budgets",
                                  "Over budget: %s" % b["category_name"], spent, "#budgets?month=" + month))
            elif share >= 0.8:
                out.append(_alert("budget:%d:%s:80" % (b["category_id"], month), "budgets",
                                  "%s budget %d%% used" % (b["category_name"], int(share * 100)), spent,
                                  "#budgets?month=" + month))
    if events.get("activity"):
        out += _activity(conn, cfg, today)
    if events.get("weekly"):
        out += _weekly(conn, today)
    return out


def _activity(conn, cfg, today):
    out = []
    since = get_setting(conn, "notify_since_tx")
    if since is None:
        _start_from_now(conn)
        since = get_setting(conn, "notify_since_tx")
    for t in rows(conn.execute(
            "SELECT t.id, t.date, t.amount_cents, t.payee, a.name AS account_name FROM transactions t"
            " JOIN accounts a ON a.id = t.account_id LEFT JOIN categories c ON c.id = t.category_id"
            " WHERE t.id > ? AND t.amount_cents <= ? AND t.transfer_id IS NULL AND t.date >= ?"
            " AND COALESCE(c.kind, '') NOT IN ('transfer', 'investment') ORDER BY t.id",
            (int(since), -cfg["large_cents"], (today - dt.timedelta(days=14)).isoformat()))):
        out.append(_alert("large:%d" % t["id"], "activity", "Large purchase: %s" % fmt(-t["amount_cents"]),
                          "%s, %s, %s." % (t["payee"] or "No payee", t["account_name"], _day(dt.date.fromisoformat(t["date"]))),
                          "#transactions"))
    for a in list_accounts(conn):
        if a["type"] not in ("checking", "savings", "cash"):
            continue
        key = "lowbal:%d" % a["id"]
        if a["balance_cents"] < cfg["low_balance_cents"]:
            out.append(_alert(key, "activity", "Low balance: %s" % a["name"],
                              "%s is down to %s." % (a["name"], fmt(a["balance_cents"])), "#accounts"))
        else:  # back above the line: warn again the next time it drops
            conn.execute("DELETE FROM notifications_sent WHERE key = ?", (key,))
    conn.commit()
    for c in rows(conn.execute("SELECT id, label, last_error FROM connections WHERE last_error IS NOT NULL")):
        digest = hashlib.sha1(c["last_error"].encode()).hexdigest()[:10]
        out.append(_alert("sync:%d:%s" % (c["id"], digest), "activity", "Bank sync problem: %s" % c["label"],
                          c["last_error"][:200], "#accounts"))
    for t in unmatched_transfers(conn, as_of=today)["missing"]:
        out.append(_alert("unmatched:%d" % t["id"], "activity", "Transfer missing its other side",
                          "%s %s in %s on %s isn't in any of your other accounts." % (
                              t["payee"] or "Transfer", fmt(t["amount_cents"]), t["account_name"], t["date"]),
                          "#transactions"))
    return out


def _weekly(conn, today):
    from . import reports
    start = today - dt.timedelta(days=today.weekday() + 7)   # Monday of last week
    end = start + dt.timedelta(days=6)
    s = reports.period_summary(conn, start.isoformat(), end.isoformat())
    if not s["transaction_count"] and not s["invested_cents"]:
        return []
    top = reports.spending_by_category(conn, start.isoformat(), end.isoformat())[:3]
    bills = [o for o in upcoming(conn, 7, as_of=today) if o["amount_cents"] < 0]
    lines = ["Spent %s, income %s, saved %s." % (fmt(s["expense_cents"]), fmt(s["income_cents"]), fmt(s["net_cents"]))]
    if s["invested_cents"] > 0:
        lines.append("Invested %s." % fmt(s["invested_cents"]))
    if top:
        lines.append("Top spending: %s." % ", ".join("%s %s" % (c["category_name"], fmt(c["spent_cents"])) for c in top))
    if bills:
        lines.append("Coming up: %d bill%s, %s in all." % (len(bills), "" if len(bills) == 1 else "s",
                                                           fmt(-sum(o["amount_cents"] for o in bills))))
    title = "Your week: %s to %s" % (start.strftime("%b %d").replace(" 0", " "), end.strftime("%b %d").replace(" 0", " "))
    return [_alert("weekly:%s" % start.isoformat(), "weekly", title, " ".join(lines), "#dashboard")]


# ----------------------------------------------------------------------- sending

def quiet(cfg, now):
    return cfg["quiet_hours"] and (now.hour >= QUIET_START or now.hour < QUIET_END)


def _claim(conn, key):
    """Reserve an alert for sending; False if it was sent (or is being sent) already."""
    claimed = conn.execute("INSERT OR IGNORE INTO notifications_sent (key) VALUES (?)", (key,)).rowcount == 1
    conn.commit()
    return claimed


def _unclaim(conn, keys):
    conn.executemany("DELETE FROM notifications_sent WHERE key = ?", [(k,) for k in keys])
    conn.commit()


def channels(conn, cfg):
    """The ways this profile can be reached right now."""
    out = []
    if cfg["email"]["enabled"] and cfg["email"]["to"] and cfg["email"]["host"]:
        out.append("email")
    if cfg["push"]["enabled"] and devices(conn):
        out.append("push")
    return out


def send_email(conn, cfg, subject, alerts, profile_name=""):
    e = cfg["email"]
    if e["security"] == "none" and e["host"] not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("unencrypted email is only allowed to a server on this computer")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = e["from"] or (e["username"] if "@" in e["username"] else e["to"])
    msg["To"] = e["to"]
    footer = "Sent by FinOrganizer%s. Change or turn off notifications in Settings." % (
        " for " + profile_name if profile_name else "")
    msg.set_content("\n\n".join("%s\n%s" % (a["title"], a["body"]) for a in alerts) + "\n\n-- \n" + footer)
    msg.add_alternative(
        "<div style=\"font-family:-apple-system,Segoe UI,sans-serif;font-size:15px;color:#111\">%s"
        "<p style=\"color:#777;font-size:12px;margin-top:24px\">%s</p></div>" % (
            "".join("<p><strong>%s</strong><br>%s</p>" % (escape(a["title"]), escape(a["body"])) for a in alerts),
            escape(footer)), subtype="html")
    password = get_setting(conn, PASSWORD_KEY) or ""
    try:
        if e["security"] == "ssl":
            server = smtplib.SMTP_SSL(e["host"], e["port"], timeout=SMTP_TIMEOUT, context=ssl_context())
        else:
            server = smtplib.SMTP(e["host"], e["port"], timeout=SMTP_TIMEOUT)
        with server:
            if e["security"] == "starttls":
                server.starttls(context=ssl_context())
            if e["username"]:
                server.login(e["username"], password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise ValueError("the email server didn't accept your username and password. For Gmail and "
                         "iCloud, use an app password, not your normal password")
    except (smtplib.SMTPException, OSError, ssl.SSLError, socket.timeout) as err:
        raise ValueError("couldn't send email: %s" % (err or type(err).__name__))


def _send_push(conn, alerts):
    """One notification per alert (at most MAX_PUSHES). Returns (sent_any, errors)."""
    if len(alerts) > MAX_PUSHES:
        shown = alerts[:MAX_PUSHES - 1]
        rest = alerts[MAX_PUSHES - 1:]
        shown.append(_alert("more", "more", "%d more FinOrganizer alerts" % len(rest),
                            "; ".join(a["title"] for a in rest)[:180], "#dashboard"))
    else:
        shown = alerts
    sent_any, errors, keep = False, [], []
    for d in devices(conn):
        try:
            for a in shown:
                webpush.send(d, {"title": a["title"], "body": a["body"], "url": a["url"], "tag": a["key"]})
            sent_any = True
            keep.append(d)
        except webpush.PushError as err:
            if err.gone:
                errors.append("%s was removed: %s" % (d.get("name", "iPhone"), err))
            else:
                errors.append("%s: %s" % (d.get("name", "iPhone"), err))
                keep.append(d)
    if len(keep) != len(devices(conn)):
        _save_devices(conn, keep)
    return sent_any, errors


def deliver(conn, cfg, alerts, profile_name=""):
    """Send alerts on every channel. Returns (delivered_somewhere, errors)."""
    ok, errors = False, []
    ways = channels(conn, cfg)
    if "email" in ways:
        subject = alerts[0]["title"] if len(alerts) == 1 else "%d FinOrganizer alerts" % len(alerts)
        if profile_name:
            subject += " (%s)" % profile_name
        try:
            send_email(conn, cfg, subject, alerts, profile_name)
            ok = True
        except ValueError as err:
            errors.append("Email: %s" % err)
    if "push" in ways:
        sent, errs = _send_push(conn, alerts)
        ok = ok or sent
        errors += ["iPhone: %s" % e for e in errs]
    return ok, errors


def run(conn, now=None, profile_name="", sync_banks=False, ignore_quiet=False):
    """Check this profile and send what's due. Returns a summary dict."""
    cfg = get_config(conn)
    now = now or dt.datetime.now()
    if not cfg["enabled"]:
        return {"status": "off", "sent": [], "errors": []}
    errors = []
    if sync_banks:
        errors += _sync_if_stale(conn, now)
    conn.execute("DELETE FROM notifications_sent WHERE sent_at < datetime('now', ?)", ("-%d days" % KEEP_SENT_DAYS,))
    alerts = collect(conn, cfg, now)
    done = {r[0] for r in conn.execute("SELECT key FROM notifications_sent")}
    pending = [a for a in alerts if a["key"] not in done]
    result = {"status": "ok", "sent": [], "errors": errors, "waiting": 0}
    if not pending:
        pass
    elif not channels(conn, cfg):
        result.update(status="no_channel", waiting=len(pending))
    elif quiet(cfg, now) and not ignore_quiet:
        result.update(status="quiet", waiting=len(pending))
    else:
        mine = [a for a in pending if _claim(conn, a["key"])]
        if mine:
            ok, errs = deliver(conn, cfg, mine, profile_name)
            result["errors"] += errs
            if ok:
                result["sent"] = [a["title"] for a in mine]
                for a in mine:  # over budget implies the 80% warning
                    if a["key"].endswith(":100"):
                        _claim(conn, a["key"][:-4] + ":80")
            else:
                _unclaim(conn, [a["key"] for a in mine])  # try again next time
                result.update(status="failed", waiting=len(mine))
    set_setting(conn, "notify_last_run", now.isoformat(timespec="seconds"))
    set_setting(conn, "notify_last_result", json.dumps(result))
    return result


def _sync_if_stale(conn, now, hours=3):
    from . import banksync
    stale = [c for c in rows(conn.execute("SELECT id, last_sync FROM connections"))
             if not c["last_sync"] or dt.datetime.fromisoformat(c["last_sync"]) < now - dt.timedelta(hours=hours)]
    errors = []
    for c in stale:
        try:
            for e in banksync.sync_connection(conn, c["id"])["errors"]:
                errors.append("Bank sync: %s" % e)
        except Exception as err:  # never let a bank problem stop the alerts
            errors.append("Bank sync: %s" % err)
    return errors


def send_test(conn, channel, profile_name=""):
    cfg = get_config(conn)
    alert = _alert("test", "test", "FinOrganizer test notification",
                   "Notifications are working%s." % (" for " + profile_name if profile_name else ""))
    if channel == "email":
        if not (cfg["email"]["to"] and cfg["email"]["host"]):
            raise ValueError("fill in your email address and the email server first")
        send_email(conn, cfg, alert["title"], [alert], profile_name)
        return {"sent": True}
    if channel == "push":
        if not devices(conn):
            raise ValueError("add an iPhone first")
        sent, errors = _send_push(conn, [alert])
        if not sent:
            raise ValueError("; ".join(errors) or "couldn't send")
        return {"sent": True, "errors": errors}
    raise ValueError("channel must be email or push")


# ------------------------------------------------------------ background check

def wants_background(profiles):
    """Whether any profile has notifications on with background checks."""
    from . import db
    for p in profiles.list():
        conn = db.connect(profiles.path(p["id"]))
        try:
            cfg = get_config(conn)
            if cfg["enabled"] and cfg["background"]:
                return True
        finally:
            conn.close()
    return False


def sync_schedule(profiles):
    """Install the background check when a profile wants it, remove it when none does.
    Returns an error message, or None."""
    from . import schedule
    if profiles is None or not schedule.supported():
        return None
    try:
        if wants_background(profiles):
            schedule.install(profiles.base_path)
        elif schedule.installed():
            schedule.uninstall()
    except OSError as err:
        return str(err)
    return None


def run_all(profiles, now=None, log=True):
    """Check every profile (used by the background task). Returns {profile id: result}."""
    from . import db
    results, everyone = {}, profiles.list()
    for p in everyone:
        conn = db.connect(profiles.path(p["id"]))
        try:
            if get_config(conn)["enabled"]:
                # Name the profile in messages only when there's more than one.
                results[p["id"]] = run(conn, now, p["name"] if len(everyone) > 1 else "", sync_banks=True)
        except Exception as err:  # one profile's problem must not stop the others
            results[p["id"]] = {"status": "error", "sent": [], "errors": [str(err)]}
        finally:
            conn.close()
    if log and results:
        _log(profiles, now or dt.datetime.now(), results)
    return results


def log_path(profiles):
    return os.path.join(os.path.dirname(profiles.base_path), "notifications.log")


def _log(profiles, now, results, keep=300):
    lines = ["%s %s: %s%s" % (now.isoformat(timespec="seconds"), pid, r["status"],
                               (" sent " + "; ".join(r["sent"])) if r.get("sent") else "") +
             ("".join(" | " + e for e in r.get("errors", [])))
             for pid, r in results.items()] or ["%s no profile has notifications on" % now.isoformat(timespec="seconds")]
    path = log_path(profiles)
    try:
        old = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join((old + lines)[-keep:]) + "\n")
    except OSError:
        pass
