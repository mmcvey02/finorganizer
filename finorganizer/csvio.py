"""CSV import (bank statements) and export."""

import csv
import datetime as dt
import io
from collections import Counter

from .ledger import add_transaction, find_category, get_account, list_transactions
from .categorize import Categorizer, enabled, process_new
from .money import to_cents

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y",
                "%b %d, %Y", "%d %b %Y")

# Common header names used by banks, lower-cased.
COLUMN_ALIASES = {
    "date": ("date", "transaction date", "posted date", "posting date", "trans date"),
    "amount": ("amount", "amount ($)", "transaction amount", "value"),
    "debit": ("debit", "withdrawal", "withdrawals", "money out", "debit amount"),
    "credit": ("credit", "deposit", "deposits", "money in", "credit amount"),
    "payee": ("payee", "description", "merchant", "name", "details", "narrative"),
    "category": ("category",),
    "memo": ("memo", "notes", "note", "reference"),
}


def parse_any_date(value, day_first=False):
    value = value.strip()
    formats = DATE_FORMATS
    if day_first:
        formats = ("%d/%m/%Y", "%d/%m/%y") + DATE_FORMATS
    for f in formats:
        try:
            return dt.datetime.strptime(value, f).date()
        except ValueError:
            continue
    raise ValueError("unrecognised date: %r" % value)


def _map_columns(headers):
    lower = {h.strip().lower(): h for h in headers if h}
    mapping = {}
    for key, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if a in lower:
                mapping[key] = lower[a]
                break
    if "date" not in mapping:
        raise ValueError("CSV needs a date column (found: %s)" % ", ".join(headers))
    if "amount" not in mapping and not ("debit" in mapping or "credit" in mapping):
        raise ValueError("CSV needs an amount column, or debit/credit columns")
    return mapping


def _field(rec, mapping, key):
    col = mapping.get(key)
    return (rec.get(col) or "").strip() if col else ""


def import_csv(conn, account_id, text, invert=False, day_first=False, skip_duplicates=True):
    """Import transactions from CSV ``text`` into an account.

    ``invert`` flips signs (for statements where purchases are positive, e.g. many
    credit cards). Returns a dict with counts of imported / duplicates / errors.
    """
    get_account(conn, account_id)  # one clear error instead of one per line
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    mapping = _map_columns(reader.fieldnames or [])
    categorizer = Categorizer(conn) if enabled(conn) else None
    # A row is a duplicate if the account already has it. Identical rows within the file
    # (two $5 coffees on the same day) are separate purchases, so they're counted: the
    # 2nd matching row is a duplicate only if the account already has two.
    existing, seen = Counter(), Counter()
    if skip_duplicates:
        for r in conn.execute("SELECT date, amount_cents, LOWER(payee) FROM transactions"
                              " WHERE account_id = ?", (account_id,)):
            existing[tuple(r)] += 1
    imported, duplicates, errors = 0, 0, []
    for lineno, rec in enumerate(reader, start=2):
        try:
            date = parse_any_date(_field(rec, mapping, "date"), day_first)
            if _field(rec, mapping, "amount"):
                amount = to_cents(_field(rec, mapping, "amount"))
            else:
                debit = _field(rec, mapping, "debit")
                credit = _field(rec, mapping, "credit")
                amount = (to_cents(credit) if credit else 0) - (abs(to_cents(debit)) if debit else 0)
            if invert:
                amount = -amount
            payee = _field(rec, mapping, "payee")
            memo = _field(rec, mapping, "memo")
            cat_name = _field(rec, mapping, "category")
            category_id = None
            if cat_name:
                category_id = find_category(
                    conn, cat_name, create_kind="income" if amount > 0 else "expense")["id"]
            key = (date.isoformat(), amount, payee.lower())
            seen[key] += 1
            if seen[key] <= existing[key]:
                duplicates += 1
                continue
            add_transaction(conn, account_id, date, amount, payee, category_id, memo, commit=False,
                            categorizer=categorizer, auto_categorize=categorizer is not None)
            imported += 1
        except (ValueError, KeyError, LookupError) as e:
            errors.append("line %d: %s" % (lineno, e))
    conn.commit()
    if imported:
        process_new(conn)  # categories in the file can make other matches possible
    return {"imported": imported, "duplicates": duplicates, "errors": errors}


EXPORT_FIELDS = ("id", "date", "account_name", "payee", "category_name", "amount", "memo", "cleared")


def _cell(text):
    """Text that a spreadsheet won't run as a formula (bank descriptions are untrusted)."""
    text = text or ""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _amount(cents):
    return "%s%d.%02d" % ("-" if cents < 0 else "", abs(cents) // 100, abs(cents) % 100)


def export_csv(conn, **filters):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_FIELDS)
    for t in reversed(list_transactions(conn, **filters)):
        w.writerow([t["id"], t["date"], _cell(t["account_name"]), _cell(t["payee"]),
                    _cell(t["category_name"]), _amount(t["amount_cents"]), _cell(t["memo"]), int(t["cleared"])])
    return buf.getvalue()
