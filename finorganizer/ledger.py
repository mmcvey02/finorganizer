"""Core bookkeeping: accounts, categories, transactions, transfers, budgets,
goals and recurring items. Every function takes an open sqlite3 connection.
"""

from .db import ACCOUNT_TYPES, CATEGORY_KINDS, FREQUENCIES, row, rows
from .money import next_occurrence, parse_date, parse_month, today


class NotFound(LookupError):
    pass


def _require(value, what):
    if value is None:
        raise NotFound("%s not found" % what)
    return value


def _require_name(value, what="name"):
    value = (value or "").strip() if isinstance(value, str) or value is None else value
    if not value:
        raise ValueError("%s can't be blank" % what)
    return value


def _update(conn, table, item_id, fields, allowed):
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join("%s = ?" % k for k in sets)
    cur = conn.execute(
        "UPDATE %s SET %s WHERE id = ?" % (table, cols), [*sets.values(), item_id]
    )
    if cur.rowcount == 0:
        raise NotFound("%s %s not found" % (table, item_id))
    conn.commit()


# --------------------------------------------------------------------- accounts

def add_account(conn, name, type="checking", opening_balance_cents=0,
                institution="", interest_rate=0.0):
    name = (name or "").strip()
    if not name:
        raise ValueError("account name is required")
    if type not in ACCOUNT_TYPES:
        raise ValueError("account type must be one of: %s" % ", ".join(ACCOUNT_TYPES))
    if conn.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        raise ValueError("an account named %r already exists" % name)
    cur = conn.execute(
        "INSERT INTO accounts (name, type, institution, opening_balance_cents, interest_rate)"
        " VALUES (?, ?, ?, ?, ?)",
        (name, type, institution, int(opening_balance_cents), float(interest_rate)),
    )
    conn.commit()
    return cur.lastrowid


def list_accounts(conn, include_archived=False):
    sql = """
        SELECT a.*, a.opening_balance_cents + COALESCE(SUM(t.amount_cents), 0) AS balance_cents,
               COUNT(t.id) AS transaction_count
        FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
        %s GROUP BY a.id ORDER BY a.archived, a.name COLLATE NOCASE
    """ % ("" if include_archived else "WHERE a.archived = 0")
    return rows(conn.execute(sql))


def get_account(conn, account_id):
    acct = row(conn.execute(
        """SELECT a.*, a.opening_balance_cents + COALESCE(SUM(t.amount_cents), 0) AS balance_cents
           FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
           WHERE a.id = ? GROUP BY a.id""",
        (account_id,),
    ))
    return _require(acct, "account %s" % account_id)


def find_account(conn, ref):
    """Look up an account by id or (case-insensitive) name."""
    if str(ref).isdigit():
        return get_account(conn, int(ref))
    r = row(conn.execute("SELECT id FROM accounts WHERE name = ? COLLATE NOCASE", (ref,)))
    if r is None:
        matches = rows(conn.execute(
            "SELECT id, name FROM accounts WHERE name LIKE ? AND archived = 0", ("%" + ref + "%",)))
        if len(matches) > 1:
            raise ValueError("%r matches several accounts: %s"
                             % (ref, ", ".join(m["name"] for m in matches)))
        r = matches[0] if matches else None
    return get_account(conn, _require(r, "account %r" % ref)["id"])


def update_account(conn, account_id, **fields):
    if "name" in fields:
        fields["name"] = _require_name(fields["name"], "account name")
    if "type" in fields and fields["type"] not in ACCOUNT_TYPES:
        raise ValueError("invalid account type")
    _update(conn, "accounts", account_id, fields,
            {"name", "type", "institution", "opening_balance_cents", "interest_rate", "archived"})


def delete_account(conn, account_id):
    cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise NotFound("account %s not found" % account_id)


# ------------------------------------------------------------------- categories

def list_categories(conn):
    return rows(conn.execute(
        "SELECT * FROM categories ORDER BY kind, group_name, name COLLATE NOCASE"))


def add_category(conn, name, kind="expense", group_name=""):
    name = (name or "").strip()
    if not name:
        raise ValueError("category name is required")
    if kind not in CATEGORY_KINDS:
        raise ValueError("category kind must be one of: %s" % ", ".join(CATEGORY_KINDS))
    if conn.execute("SELECT 1 FROM categories WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        raise ValueError("a category named %r already exists" % name)
    cur = conn.execute(
        "INSERT INTO categories (name, kind, group_name) VALUES (?, ?, ?)",
        (name, kind, group_name or ""),
    )
    conn.commit()
    return cur.lastrowid


def update_category(conn, category_id, **fields):
    if "name" in fields:
        fields["name"] = _require_name(fields["name"], "category name")
    if "kind" in fields and fields["kind"] not in CATEGORY_KINDS:
        raise ValueError("invalid category kind")
    _update(conn, "categories", category_id, fields, {"name", "kind", "group_name"})


def delete_category(conn, category_id):
    cur = conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise NotFound("category %s not found" % category_id)


def find_category(conn, ref, create_kind=None):
    """Look up a category by id or name; optionally create it if missing."""
    if ref is None or ref == "":
        return None
    if isinstance(ref, int) or str(ref).isdigit():
        r = row(conn.execute("SELECT * FROM categories WHERE id = ?", (int(ref),)))
        return _require(r, "category %s" % ref)
    r = row(conn.execute("SELECT * FROM categories WHERE name = ? COLLATE NOCASE", (ref,)))
    if r is None and create_kind:
        cid = add_category(conn, ref, create_kind)
        r = row(conn.execute("SELECT * FROM categories WHERE id = ?", (cid,)))
    return _require(r, "category %r" % ref)


# ----------------------------------------------------------------- transactions

def add_transaction(conn, account_id, date, amount_cents, payee="", category_id=None,
                    memo="", cleared=False, commit=True):
    date = parse_date(date).isoformat()
    get_account(conn, account_id)
    cur = conn.execute(
        "INSERT INTO transactions (account_id, date, amount_cents, payee, category_id, memo, cleared)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, date, int(amount_cents), payee or "", category_id, memo or "", int(bool(cleared))),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def add_transfer(conn, from_account_id, to_account_id, date, amount_cents, memo=""):
    """Move money between two accounts. Creates a linked pair of transactions."""
    if from_account_id == to_account_id:
        raise ValueError("cannot transfer to the same account")
    amount_cents = abs(int(amount_cents))
    if amount_cents == 0:
        raise ValueError("transfer amount must be non-zero")
    src, dst = get_account(conn, from_account_id), get_account(conn, to_account_id)
    cat = find_category(conn, "Transfer", create_kind="transfer")["id"]
    out_id = add_transaction(conn, src["id"], date, -amount_cents, "Transfer to " + dst["name"],
                             cat, memo, commit=False)
    in_id = add_transaction(conn, dst["id"], date, amount_cents, "Transfer from " + src["name"],
                            cat, memo, commit=False)
    conn.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (in_id, out_id))
    conn.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (out_id, in_id))
    conn.commit()
    return out_id, in_id


TX_SELECT = """
    SELECT t.*, a.name AS account_name, c.name AS category_name, c.kind AS category_kind,
           c.group_name AS category_group
    FROM transactions t
    JOIN accounts a ON a.id = t.account_id
    LEFT JOIN categories c ON c.id = t.category_id
"""


def list_transactions(conn, account_id=None, category_id=None, start=None, end=None,
                      search=None, uncategorized=False, limit=None, offset=0):
    where, params = [], []
    if account_id:
        where.append("t.account_id = ?")
        params.append(account_id)
    if category_id:
        where.append("t.category_id = ?")
        params.append(category_id)
    if uncategorized:
        where.append("t.category_id IS NULL")
    if start:
        where.append("t.date >= ?")
        params.append(parse_date(start).isoformat())
    if end:
        where.append("t.date <= ?")
        params.append(parse_date(end).isoformat())
    if search:
        where.append("(t.payee LIKE ? OR t.memo LIKE ?)")
        params += ["%" + search + "%"] * 2
    sql = TX_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY t.date DESC, t.id DESC"
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
    return rows(conn.execute(sql, params))


def get_transaction(conn, tx_id):
    return _require(row(conn.execute(TX_SELECT + " WHERE t.id = ?", (tx_id,))),
                    "transaction %s" % tx_id)


def update_transaction(conn, tx_id, **fields):
    if "date" in fields:
        fields["date"] = parse_date(fields["date"]).isoformat()
    tx = get_transaction(conn, tx_id)
    _update(conn, "transactions", tx_id, fields,
            {"account_id", "date", "amount_cents", "payee", "category_id", "memo", "cleared"})
    # Keep the other side of a transfer in sync on date / amount.
    if tx["transfer_id"]:
        mirror = {}
        if "date" in fields:
            mirror["date"] = fields["date"]
        if "amount_cents" in fields:
            mirror["amount_cents"] = -int(fields["amount_cents"])
        if mirror:
            _update(conn, "transactions", tx["transfer_id"], mirror, set(mirror))


TRANSFER_MATCH_DAYS = 4


def _pair_key(a, b):
    return "not_transfer:%d-%d" % (min(a, b), max(a, b))


def link_transfer(conn, out_id, in_id):
    """Mark two existing transactions as the two sides of one transfer."""
    cat = find_category(conn, "Transfer", create_kind="transfer")["id"]
    conn.execute("UPDATE transactions SET transfer_id = ?, category_id = ? WHERE id = ?", (in_id, cat, out_id))
    conn.execute("UPDATE transactions SET transfer_id = ?, category_id = ? WHERE id = ?", (out_id, cat, in_id))


def unlink_transfer(conn, tx_id):
    """Split a transfer back into two ordinary transactions (and don't auto-link them again)."""
    tx = get_transaction(conn, tx_id)
    if not tx["transfer_id"]:
        raise ValueError("that transaction isn't a transfer")
    other = tx["transfer_id"]
    conn.execute("UPDATE transactions SET transfer_id = NULL, category_id = NULL WHERE id IN (?, ?)",
                 (tx_id, other))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (_pair_key(tx_id, other),))
    conn.commit()


def detect_transfers(conn):
    """Link bank-downloaded money moving between your own accounts as transfers.

    A card payment shows up twice when both accounts are linked: money out of checking
    and money into the card. Left alone, that counts as both spending and income. A pair
    is linked only when the amounts match exactly, the accounts differ, the dates are at
    most a few days apart and each side has exactly one possible partner. Pairs the user
    split apart ("Not a transfer") are never re-linked. Returns the number of pairs linked.
    """
    cands = rows(conn.execute(
        "SELECT id, account_id, date, amount_cents FROM transactions"
        " WHERE transfer_id IS NULL AND external_id IS NOT NULL AND amount_cents != 0"))
    rejected = {r[0] for r in conn.execute("SELECT key FROM settings WHERE key LIKE 'not_transfer:%'")}
    by_amount = {}
    for t in cands:
        by_amount.setdefault(t["amount_cents"], []).append(t)

    def partners(t):
        d = parse_date(t["date"])
        return [o for o in by_amount.get(-t["amount_cents"], [])
                if o["account_id"] != t["account_id"]
                and abs((parse_date(o["date"]) - d).days) <= TRANSFER_MATCH_DAYS
                and _pair_key(t["id"], o["id"]) not in rejected]

    linked, used = 0, set()
    for t in cands:
        if t["amount_cents"] >= 0 or t["id"] in used:
            continue
        mine = partners(t)
        if len(mine) == 1 and mine[0]["id"] not in used and len(partners(mine[0])) == 1:
            link_transfer(conn, t["id"], mine[0]["id"])
            used.update((t["id"], mine[0]["id"]))
            linked += 1
    conn.commit()
    return linked


def delete_transaction(conn, tx_id):
    tx = get_transaction(conn, tx_id)
    conn.execute("DELETE FROM transactions WHERE id IN (?, ?)", (tx_id, tx["transfer_id"] or -1))
    conn.commit()


# ---------------------------------------------------------------------- budgets

def set_budget(conn, category_id, month, amount_cents):
    parse_month(month)
    if amount_cents is None or int(amount_cents) <= 0:
        conn.execute("DELETE FROM budgets WHERE category_id = ? AND month = ?", (category_id, month))
    else:
        conn.execute(
            "INSERT INTO budgets (category_id, month, amount_cents) VALUES (?, ?, ?)"
            " ON CONFLICT (category_id, month) DO UPDATE SET amount_cents = excluded.amount_cents",
            (category_id, month, int(amount_cents)),
        )
    conn.commit()


def copy_budgets(conn, from_month, to_month, overwrite=False):
    """Copy every budget line from one month to another. Returns lines copied."""
    parse_month(from_month)
    parse_month(to_month)
    conflict = "DO UPDATE SET amount_cents = excluded.amount_cents" if overwrite else "DO NOTHING"
    cur = conn.execute(
        "INSERT INTO budgets (category_id, month, amount_cents)"
        " SELECT category_id, ?, amount_cents FROM budgets WHERE month = ? AND true"
        " ON CONFLICT (category_id, month) " + conflict,
        (to_month, from_month),
    )
    conn.commit()
    return cur.rowcount


def budget_status(conn, month):
    """Budgeted vs actual spending per category for ``month``."""
    from .money import month_bounds
    start, end = month_bounds(month)
    data = rows(conn.execute(
        """
        SELECT c.id AS category_id, c.name AS category_name, c.group_name, c.kind,
               COALESCE(b.amount_cents, 0) AS budget_cents,
               COALESCE((SELECT -SUM(t.amount_cents) FROM transactions t
                         WHERE t.category_id = c.id AND t.date BETWEEN ? AND ?), 0) AS spent_cents
        FROM categories c
        LEFT JOIN budgets b ON b.category_id = c.id AND b.month = ?
        WHERE c.kind = 'expense'
        ORDER BY c.group_name, c.name COLLATE NOCASE
        """,
        (start, end, month),
    ))
    out = []
    for d in data:
        if d["budget_cents"] == 0 and d["spent_cents"] == 0:
            continue
        d["remaining_cents"] = d["budget_cents"] - d["spent_cents"]
        d["percent_used"] = (round(100 * d["spent_cents"] / d["budget_cents"], 1)
                             if d["budget_cents"] else None)
        out.append(d)
    return out


# ------------------------------------------------------------------------ goals

def add_goal(conn, name, target_cents, target_date=None, account_id=None, saved_cents=0, notes=""):
    if not (name or "").strip():
        raise ValueError("goal name is required")
    if int(target_cents) <= 0:
        raise ValueError("goal target must be positive")
    if target_date:
        target_date = parse_date(target_date).isoformat()
    cur = conn.execute(
        "INSERT INTO goals (name, target_cents, target_date, account_id, saved_cents, notes)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (name.strip(), int(target_cents), target_date or None, account_id, int(saved_cents), notes),
    )
    conn.commit()
    return cur.lastrowid


def list_goals(conn, as_of=None):
    """Goals with progress. A goal linked to an account tracks that account's balance."""
    as_of = parse_date(as_of or today())
    out = []
    for g in rows(conn.execute(
            "SELECT g.*, a.name AS account_name FROM goals g"
            " LEFT JOIN accounts a ON a.id = g.account_id ORDER BY g.target_date IS NULL, g.target_date")):
        current = g["saved_cents"]
        if g["account_id"]:
            current = get_account(conn, g["account_id"])["balance_cents"]
        g["current_cents"] = current
        g["remaining_cents"] = max(0, g["target_cents"] - current)
        g["percent"] = round(min(100.0, 100 * max(current, 0) / g["target_cents"]), 1)
        g["monthly_needed_cents"] = None
        g["months_left"] = None
        if g["target_date"] and g["remaining_cents"] > 0:
            td = parse_date(g["target_date"])
            months = (td.year - as_of.year) * 12 + (td.month - as_of.month)
            if td.day < as_of.day:
                months -= 1
            g["months_left"] = max(months, 0)
            g["monthly_needed_cents"] = -(-g["remaining_cents"] // max(months, 1))
        g["complete"] = g["remaining_cents"] == 0
        out.append(g)
    return out


def contribute_goal(conn, goal_id, amount_cents):
    cur = conn.execute("UPDATE goals SET saved_cents = saved_cents + ? WHERE id = ?",
                       (int(amount_cents), goal_id))
    conn.commit()
    if cur.rowcount == 0:
        raise NotFound("goal %s not found" % goal_id)


def update_goal(conn, goal_id, **fields):
    if "name" in fields:
        fields["name"] = _require_name(fields["name"], "goal name")
    if "target_date" in fields:
        fields["target_date"] = (parse_date(fields["target_date"]).isoformat()
                                 if fields["target_date"] else None)
    if "account_id" in fields and fields["account_id"] in ("", None):
        fields["account_id"] = None
    _update(conn, "goals", goal_id, fields,
            {"name", "target_cents", "target_date", "account_id", "saved_cents", "notes"})


def delete_goal(conn, goal_id):
    cur = conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise NotFound("goal %s not found" % goal_id)


# -------------------------------------------------------------------- recurring

def add_recurring(conn, name, account_id, amount_cents, frequency, next_date,
                  category_id=None, payee=""):
    if frequency not in FREQUENCIES:
        raise ValueError("frequency must be one of: %s" % ", ".join(FREQUENCIES))
    if not (name or "").strip():
        raise ValueError("name is required")
    get_account(conn, account_id)
    next_date = parse_date(next_date)
    cur = conn.execute(
        "INSERT INTO recurring (name, account_id, amount_cents, category_id, payee, frequency,"
        " next_date, anchor_day) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name.strip(), account_id, int(amount_cents), category_id, payee or name.strip(), frequency,
         next_date.isoformat(), next_date.day),
    )
    conn.commit()
    return cur.lastrowid


def list_recurring(conn, include_inactive=True):
    return rows(conn.execute(
        """SELECT r.*, a.name AS account_name, c.name AS category_name
           FROM recurring r JOIN accounts a ON a.id = r.account_id
           LEFT JOIN categories c ON c.id = r.category_id
           %s ORDER BY r.active DESC, r.next_date""" % ("" if include_inactive else "WHERE r.active = 1")))


def update_recurring(conn, rec_id, **fields):
    if "frequency" in fields and fields["frequency"] not in FREQUENCIES:
        raise ValueError("invalid frequency")
    if "name" in fields:
        fields["name"] = _require_name(fields["name"], "name")
    if "next_date" in fields:
        d = parse_date(fields["next_date"])
        current = row(conn.execute("SELECT next_date FROM recurring WHERE id = ?", (rec_id,)))
        fields["next_date"] = d.isoformat()
        if not current or current["next_date"] != fields["next_date"]:
            fields["anchor_day"] = d.day  # only a changed date redefines the intended day
    _update(conn, "recurring", rec_id, fields,
            {"name", "account_id", "amount_cents", "category_id", "payee", "frequency",
             "next_date", "anchor_day", "active"})


def delete_recurring(conn, rec_id):
    cur = conn.execute("DELETE FROM recurring WHERE id = ?", (rec_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise NotFound("recurring item %s not found" % rec_id)


def upcoming(conn, days=30, as_of=None):
    """Expand active recurring items into individual occurrences in the next ``days``."""
    import datetime as dt
    start = parse_date(as_of or today())
    end = start + dt.timedelta(days=days)
    out = []
    for r in list_recurring(conn, include_inactive=False):
        d = parse_date(r["next_date"])
        while d <= end:
            out.append({"recurring_id": r["id"], "name": r["name"], "date": d.isoformat(),
                        "amount_cents": r["amount_cents"], "account_name": r["account_name"],
                        "category_name": r["category_name"], "overdue": d < start})
            d = next_occurrence(d, r["frequency"], r["anchor_day"])
    out.sort(key=lambda o: o["date"])
    return out


def post_due(conn, as_of=None):
    """Record every recurring occurrence dated on/before ``as_of`` as a transaction.

    Advances each item's ``next_date``. Returns the ids of created transactions.
    """
    as_of = parse_date(as_of or today())
    created = []
    for r in list_recurring(conn, include_inactive=False):
        d = parse_date(r["next_date"])
        while d <= as_of:
            created.append(add_transaction(conn, r["account_id"], d, r["amount_cents"],
                                           r["payee"] or r["name"], r["category_id"],
                                           "Recurring: " + r["name"], commit=False))
            d = next_occurrence(d, r["frequency"], r["anchor_day"])
        conn.execute("UPDATE recurring SET next_date = ? WHERE id = ?", (d.isoformat(), r["id"]))
    conn.commit()
    return created
