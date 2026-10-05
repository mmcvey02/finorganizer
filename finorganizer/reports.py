"""Read-only analysis over the ledger: summaries, trends, net worth, insights.

Transfers between your own accounts are excluded from income/expense figures.
"""

from .db import LIABILITY_TYPES, rows
from .ledger import budget_status, list_accounts, list_goals, upcoming
from .money import fmt, month_bounds, month_of, months_between, shift_month, today

# Income = positive non-transfer flows; expenses = negative non-transfer flows.
# Uncategorised transactions count by sign.
_NOT_TRANSFER = "t.transfer_id IS NULL AND COALESCE(c.kind, '') != 'transfer'"


def period_summary(conn, start, end):
    r = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN t.amount_cents > 0 THEN t.amount_cents END), 0),
                  COALESCE(-SUM(CASE WHEN t.amount_cents < 0 THEN t.amount_cents END), 0),
                  COUNT(*)
           FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.date BETWEEN ? AND ? AND """ + _NOT_TRANSFER,
        (start, end),
    ).fetchone()
    income, expenses, count = r[0], r[1], r[2]
    net = income - expenses
    return {
        "start": start, "end": end,
        "income_cents": income, "expense_cents": expenses, "net_cents": net,
        "savings_rate": round(100 * net / income, 1) if income else None,
        "transaction_count": count,
    }


def month_summary(conn, month):
    s = period_summary(conn, *month_bounds(month))
    s["month"] = month
    return s


def spending_by_category(conn, start, end):
    data = rows(conn.execute(
        """SELECT COALESCE(c.name, 'Uncategorized') AS category_name,
                  COALESCE(c.group_name, '') AS group_name,
                  -SUM(t.amount_cents) AS spent_cents, COUNT(*) AS count
           FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.date BETWEEN ? AND ? AND """ + _NOT_TRANSFER + """
             AND (c.kind = 'expense' OR (c.id IS NULL AND t.amount_cents < 0))
           GROUP BY category_name HAVING spent_cents > 0 ORDER BY spent_cents DESC""",
        (start, end),
    ))
    total = sum(d["spent_cents"] for d in data)
    for d in data:
        d["percent"] = round(100 * d["spent_cents"] / total, 1) if total else 0
    return data


def income_by_category(conn, start, end):
    return rows(conn.execute(
        """SELECT COALESCE(c.name, 'Uncategorized') AS category_name,
                  SUM(t.amount_cents) AS amount_cents
           FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.date BETWEEN ? AND ? AND """ + _NOT_TRANSFER + """
             AND (c.kind = 'income' OR (c.id IS NULL AND t.amount_cents > 0))
           GROUP BY category_name HAVING amount_cents > 0 ORDER BY amount_cents DESC""",
        (start, end),
    ))


def cash_flow(conn, months=12, end_month=None):
    """Income / expenses / net for each of the last ``months`` months."""
    end_month = end_month or month_of(today())
    start_month = shift_month(end_month, -(months - 1))
    return [month_summary(conn, m) for m in months_between(start_month, end_month)]


def top_payees(conn, start, end, limit=10):
    return rows(conn.execute(
        """SELECT t.payee, -SUM(t.amount_cents) AS spent_cents, COUNT(*) AS count
           FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.date BETWEEN ? AND ? AND t.amount_cents < 0 AND """ + _NOT_TRANSFER + """
           GROUP BY t.payee ORDER BY spent_cents DESC LIMIT ?""",
        (start, end, limit),
    ))


def net_worth(conn):
    accounts = list_accounts(conn)
    assets = sum(a["balance_cents"] for a in accounts if a["balance_cents"] > 0)
    liabilities = -sum(a["balance_cents"] for a in accounts if a["balance_cents"] < 0)
    by_type = {}
    for a in accounts:
        by_type[a["type"]] = by_type.get(a["type"], 0) + a["balance_cents"]
    return {
        "assets_cents": assets,
        "liabilities_cents": liabilities,
        "net_worth_cents": assets - liabilities,
        "by_type": by_type,
        "accounts": accounts,
    }


def net_worth_history(conn, months=12, end_month=None):
    """Net worth at the end of each of the last ``months`` months."""
    end_month = end_month or month_of(today())
    opening = conn.execute(
        "SELECT COALESCE(SUM(opening_balance_cents), 0) FROM accounts WHERE archived = 0"
    ).fetchone()[0]
    out = []
    for m in months_between(shift_month(end_month, -(months - 1)), end_month):
        _, last = month_bounds(m)
        tx = conn.execute(
            "SELECT COALESCE(SUM(t.amount_cents), 0) FROM transactions t"
            " JOIN accounts a ON a.id = t.account_id WHERE a.archived = 0 AND t.date <= ?",
            (last,),
        ).fetchone()[0]
        out.append({"month": m, "net_worth_cents": opening + tx})
    return out


def average_monthly(conn, months=3, end_month=None):
    """Average income and expenses over the last ``months`` complete months."""
    end_month = end_month or shift_month(month_of(today()), -1)
    flows = cash_flow(conn, months, end_month)
    n = len(flows) or 1
    return {
        "months": months,
        "income_cents": sum(f["income_cents"] for f in flows) // n,
        "expense_cents": sum(f["expense_cents"] for f in flows) // n,
    }


def insights(conn, month=None):
    """Plain-language observations about the user's finances."""
    month = month or month_of(today())
    notes = []
    cur = month_summary(conn, month)
    prev = month_summary(conn, shift_month(month, -1))
    avg = average_monthly(conn, 3, shift_month(month, -1))

    if cur["income_cents"] and cur["savings_rate"] is not None:
        rate = cur["savings_rate"]
        if rate < 0:
            notes.append(("warning", "You've spent more than you earned this month."))
        elif rate < 10:
            notes.append(("info", "Savings rate is %.0f%% this month; 15-20%% is a common target." % rate))
        else:
            notes.append(("good", "You're saving %.0f%% of your income this month." % rate))

    if prev["expense_cents"] and cur["expense_cents"] > prev["expense_cents"] * 1.2:
        notes.append(("info", "Spending is up %.0f%% compared with last month."
                      % (100 * (cur["expense_cents"] / prev["expense_cents"] - 1))))

    for b in budget_status(conn, month):
        if b["budget_cents"] and b["spent_cents"] > b["budget_cents"]:
            notes.append(("warning", "Over budget on %s by %s." % (
                b["category_name"], fmt(b["spent_cents"] - b["budget_cents"]))))

    liquid = sum(a["balance_cents"] for a in list_accounts(conn)
                 if a["type"] in ("checking", "savings", "cash"))
    if avg["expense_cents"]:
        months_cover = liquid / avg["expense_cents"]
        if months_cover < 3:
            notes.append(("warning", "Cash covers %.1f months of expenses; aim for 3-6 months."
                          % max(months_cover, 0)))
        else:
            notes.append(("good", "Your cash covers %.1f months of expenses." % months_cover))

    high_interest = [a for a in list_accounts(conn)
                     if a["type"] in LIABILITY_TYPES and a["balance_cents"] < 0 and a["interest_rate"] >= 15]
    for a in high_interest:
        notes.append(("warning", "%s carries %s at %.1f%% APR; paying it down is a guaranteed return."
                      % (a["name"], fmt(-a["balance_cents"]), a["interest_rate"])))

    uncategorized = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL AND date BETWEEN ? AND ?",
        month_bounds(month),
    ).fetchone()[0]
    if uncategorized:
        notes.append(("info", "%d transaction(s) this month have no category." % uncategorized))

    return [{"level": lvl, "message": msg} for lvl, msg in notes]


def dashboard(conn, month=None):
    month = month or month_of(today())
    start, end = month_bounds(month)
    nw = net_worth(conn)
    return {
        "month": month,
        "summary": month_summary(conn, month),
        "net_worth": {k: v for k, v in nw.items() if k != "accounts"},
        "accounts": nw["accounts"],
        "spending_by_category": spending_by_category(conn, start, end),
        "cash_flow": cash_flow(conn, 6, month),
        "budgets": budget_status(conn, month),
        "goals": list_goals(conn),
        "upcoming": upcoming(conn, 30),
        "insights": insights(conn, month),
    }


def year_review(conn, year):
    year = int(year)
    start, end = "%d-01-01" % year, "%d-12-31" % year
    return {
        "year": year,
        "summary": period_summary(conn, start, end),
        "by_month": cash_flow(conn, 12, "%d-12" % year),
        "spending_by_category": spending_by_category(conn, start, end),
        "income_by_category": income_by_category(conn, start, end),
        "top_payees": top_payees(conn, start, end),
    }

