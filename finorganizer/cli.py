"""Command-line interface: `finorganizer <command> ...` (or `python -m finorganizer`)."""

import argparse
import json
import sqlite3
import sys

from . import csvio, db, ledger as L, planning, reports
from .money import fmt, month_bounds, month_of, to_cents, today


# ----------------------------------------------------------------- formatting

def table(rows, columns):
    """Print ``rows`` (dicts) as an aligned table. ``columns`` = [(header, key_or_fn, align)]."""
    if not rows:
        print("(nothing to show)")
        return
    cells = [[str(fn(r) if callable(fn) else (r.get(fn) if r.get(fn) is not None else ""))
              for _, fn, _ in columns] for r in rows]
    widths = [max(len(h), *(len(c[i]) for c in cells)) for i, (h, _, _) in enumerate(columns)]

    def line(vals):
        return "  ".join(v.rjust(w) if a == ">" else v.ljust(w)
                         for v, w, (_, _, a) in zip(vals, widths, columns)).rstrip()

    print(line([h for h, _, _ in columns]))
    print("  ".join("-" * w for w in widths))
    for c in cells:
        print(line(c))


def money(key):
    return lambda r: fmt(r[key]) if r.get(key) is not None else ""


def kv(pairs):
    w = max(len(k) for k, _ in pairs)
    for k, v in pairs:
        print("%s  %s" % (k.ljust(w), v))


def pct(v):
    return "n/a" if v is None else "%.1f%%" % v


# ------------------------------------------------------------------- commands

def cmd_demo(conn, a):
    from .sample import load_sample_data
    load_sample_data(conn, months=a.months)
    print("Loaded %d months of sample data." % a.months)


def cmd_serve(conn, a):
    from .server import serve
    serve(conn, a.host, a.port, open_browser=a.open, profiles=a.profiles, profile_id=a.profile_id)


def cmd_profile(conn, a):
    P = a.profiles
    if a.action == "create":
        pid = P.create(a.name)
        P.remember(pid)
        print("Created profile %r and switched to it." % a.name)
    elif a.action == "use":
        pid = P.find(a.name)
        P.remember(pid)
        print("Now using profile %r." % P._name(pid))
    elif a.action == "rename":
        P.rename(P.find(a.name), a.new_name)
        print("Renamed.")
    elif a.action == "delete":
        pid = P.find(a.name)
        if not a.yes:
            sys.exit("This permanently deletes profile %r and all of its data. Re-run with --yes."
                     % P._name(pid))
        if pid == a.profile_id:
            conn.close()
        P.delete(pid)
        if P.last_used() == pid or pid == a.profile_id:
            P.remember("default")
        print("Deleted.")
    else:
        for p in P.list():
            print("%s %-20s %s" % ("*" if p["id"] == a.profile_id else " ", p["name"], P.path(p["id"])))


def cmd_summary(conn, a):
    from .summary_page import render
    name = db.get_setting(conn, "profile_name") or ""
    html = render(conn, a.month, name)
    path = a.file or "financial-summary.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote %s. Open it in a browser and print, or choose 'Save as PDF'." % path)
    if a.open:
        import os
        import webbrowser
        webbrowser.open("file://" + os.path.abspath(path))


def cmd_bank(conn, a):
    from . import banksync as B
    if a.action == "connect":
        cid = B.connect(conn, a.token, a.label or "")
        print("Connected (#%d). Choose what each bank account should feed:" % cid)
        _bank_list(conn)
        print("\nThen run: finorganizer bank link <ID> --new | --account NAME | --ignore")
    elif a.action == "link":
        if a.ignore:
            B.link_remote(conn, a.id, "ignore")
        elif a.account:
            B.link_remote(conn, a.id, "link", L.find_account(conn, a.account)["id"])
        else:
            B.link_remote(conn, a.id, "new", type=a.type)
        print("Saved. Run 'finorganizer bank sync' to download transactions.")
    elif a.action == "sync":
        for r in B.sync(conn):
            print("%s: imported %d, matched %d existing%s" % (
                r["label"], r["imported"], r["matched"],
                ", %d new account(s) waiting to be linked" % r["new_accounts"] if r["new_accounts"] else ""))
            for e in r["errors"]:
                print("  ! " + e)
    elif a.action == "match":
        B.match_bank_balance(conn, a.id)
        print("Account balance now matches the bank.")
    elif a.action == "remove":
        B.delete_connection(conn, a.id)
        print("Connection removed. Imported transactions were kept.")
    else:
        _bank_list(conn)


def _bank_list(conn):
    from . import banksync as B
    conns = B.list_connections(conn)
    if not conns:
        print("No bank connections. Create a setup token at https://beta-bridge.simplefin.org"
              " and run: finorganizer bank connect <TOKEN>")
        return
    for c in conns:
        print("\n#%d %s  (%s)  last sync: %s%s" % (c["id"], c["label"], c["host"], c["last_sync"] or "never",
                                                  "  ERROR: " + c["last_error"] if c["last_error"] else ""))
        table(c["accounts"], [
            ("ID", "id", ">"), ("Bank account", "name", "<"), ("Institution", "institution", "<"),
            ("Bank balance", money("balance_cents"), ">"), ("Status", "status", "<"),
            ("Feeds", "account_name", "<"), ("Difference", money("difference_cents"), ">")])


def cmd_account(conn, a):
    if a.action == "add":
        aid = L.add_account(conn, a.name, a.type, to_cents(a.balance), a.institution, a.rate)
        print("Created account #%d %s" % (aid, a.name))
    elif a.action == "list":
        accts = L.list_accounts(conn, a.all)
        table(accts, [("ID", "id", ">"), ("Name", "name", "<"), ("Type", "type", "<"),
                      ("Institution", "institution", "<"), ("APR", lambda r: r["interest_rate"] or "", ">"),
                      ("Balance", money("balance_cents"), ">")])
        nw = reports.net_worth(conn)
        print("\nNet worth: %s  (assets %s, liabilities %s)" % (
            fmt(nw["net_worth_cents"]), fmt(nw["assets_cents"]), fmt(nw["liabilities_cents"])))
    elif a.action == "archive":
        L.update_account(conn, L.find_account(conn, a.account)["id"], archived=1)
        print("Archived.")
    elif a.action == "delete":
        acct = L.find_account(conn, a.account)
        if not a.yes:
            sys.exit("This deletes %s and all of its transactions. Re-run with --yes to confirm."
                     % acct["name"])
        L.delete_account(conn, acct["id"])
        print("Deleted.")


def cmd_category(conn, a):
    if a.action == "add":
        cid = L.add_category(conn, a.name, a.kind, a.group)
        print("Created category #%d %s" % (cid, a.name))
    else:
        table(L.list_categories(conn), [("ID", "id", ">"), ("Name", "name", "<"),
                                        ("Kind", "kind", "<"), ("Group", "group_name", "<")])


def _cat_id(conn, name, amount=0):
    if not name:
        return None
    return L.find_category(conn, name, create_kind="income" if amount > 0 else "expense")["id"]


def cmd_tx(conn, a):
    if a.action == "add":
        amount = to_cents(a.amount)
        if a.expense:
            amount = -abs(amount)
        acct = L.find_account(conn, a.account)
        tid = L.add_transaction(conn, acct["id"], a.date or today(), amount, a.payee,
                                _cat_id(conn, a.category, amount), a.memo)
        print("Recorded #%d: %s %s in %s" % (tid, fmt(amount), a.payee, acct["name"]))
    elif a.action == "list":
        start, end = a.start, a.end
        if a.month:
            start, end = month_bounds(a.month)
        txs = L.list_transactions(
            conn, account_id=L.find_account(conn, a.account)["id"] if a.account else None,
            category_id=_cat_id(conn, a.category) if a.category else None,
            start=start, end=end, search=a.search, uncategorized=a.uncategorized, limit=a.limit)
        table(txs, [("ID", "id", ">"), ("Date", "date", "<"), ("Account", "account_name", "<"),
                    ("Payee", "payee", "<"), ("Category", "category_name", "<"),
                    ("Amount", money("amount_cents"), ">")])
    elif a.action == "categorize":
        tx = L.get_transaction(conn, a.id)
        L.update_transaction(conn, a.id, category_id=_cat_id(conn, a.category, tx["amount_cents"]))
        print("Updated.")
    elif a.action == "delete":
        L.delete_transaction(conn, a.id)
        print("Deleted.")


def cmd_transfer(conn, a):
    src, dst = L.find_account(conn, a.source), L.find_account(conn, a.dest)
    L.add_transfer(conn, src["id"], dst["id"], a.date or today(), to_cents(a.amount), a.memo)
    print("Transferred %s from %s to %s" % (fmt(abs(to_cents(a.amount))), src["name"], dst["name"]))


def cmd_budget(conn, a):
    month = getattr(a, "month", None) or month_of(today())
    if a.action == "set":
        cat = L.find_category(conn, a.category, create_kind="expense")
        L.set_budget(conn, cat["id"], month, to_cents(a.amount))
        print("Budget for %s in %s set to %s" % (cat["name"], month, fmt(to_cents(a.amount))))
    elif a.action == "copy":
        n = L.copy_budgets(conn, a.source, a.dest, a.overwrite)
        print("Copied %d budget line(s)." % n)
    else:
        status = L.budget_status(conn, month)
        print("Budget for %s\n" % month)
        table(status, [("Category", "category_name", "<"), ("Budget", money("budget_cents"), ">"),
                       ("Spent", money("spent_cents"), ">"), ("Left", money("remaining_cents"), ">"),
                       ("Used", lambda r: pct(r["percent_used"]), ">")])
        tb = sum(s["budget_cents"] for s in status)
        ts = sum(s["spent_cents"] for s in status)
        print("\nTotal budgeted %s, spent %s, remaining %s" % (fmt(tb), fmt(ts), fmt(tb - ts)))


def cmd_goal(conn, a):
    if a.action == "add":
        acct = L.find_account(conn, a.account)["id"] if a.account else None
        gid = L.add_goal(conn, a.name, to_cents(a.target), a.date, acct, to_cents(a.saved))
        print("Created goal #%d %s" % (gid, a.name))
    elif a.action == "contribute":
        L.contribute_goal(conn, a.id, to_cents(a.amount))
        print("Added %s to goal #%d" % (fmt(to_cents(a.amount)), a.id))
    elif a.action == "delete":
        L.delete_goal(conn, a.id)
        print("Deleted.")
    else:
        table(L.list_goals(conn), [
            ("ID", "id", ">"), ("Goal", "name", "<"), ("Target", money("target_cents"), ">"),
            ("Saved", money("current_cents"), ">"), ("Progress", lambda r: "%.0f%%" % r["percent"], ">"),
            ("By", "target_date", "<"), ("Need / month", money("monthly_needed_cents"), ">"),
            ("Tracks", "account_name", "<")])


def cmd_recurring(conn, a):
    if a.action == "add":
        amount = to_cents(a.amount)
        if a.expense:
            amount = -abs(amount)
        acct = L.find_account(conn, a.account)
        rid = L.add_recurring(conn, a.name, acct["id"], amount, a.frequency, a.next or today(),
                              _cat_id(conn, a.category, amount), a.payee or a.name)
        print("Created recurring item #%d %s" % (rid, a.name))
    elif a.action == "post":
        created = L.post_due(conn)
        print("Recorded %d due transaction(s)." % len(created))
    elif a.action == "upcoming":
        items = L.upcoming(conn, a.days)
        table(items, [("Date", "date", "<"), ("Name", "name", "<"), ("Account", "account_name", "<"),
                      ("Amount", money("amount_cents"), ">"),
                      ("", lambda r: "OVERDUE" if r["overdue"] else "", "<")])
        print("\nNet over next %d days: %s" % (a.days, fmt(sum(i["amount_cents"] for i in items))))
    elif a.action == "delete":
        L.delete_recurring(conn, a.id)
        print("Deleted.")
    else:
        table(L.list_recurring(conn), [
            ("ID", "id", ">"), ("Name", "name", "<"), ("Account", "account_name", "<"),
            ("Amount", money("amount_cents"), ">"), ("Every", "frequency", "<"),
            ("Next", "next_date", "<"), ("Active", lambda r: "yes" if r["active"] else "no", "<")])


def cmd_report(conn, a):
    month = a.month or month_of(today())
    if a.kind == "summary":
        s = reports.month_summary(conn, month)
        kv([("Month", month), ("Income", fmt(s["income_cents"])), ("Expenses", fmt(s["expense_cents"])),
            ("Net", fmt(s["net_cents"])), ("Savings rate", pct(s["savings_rate"]))])
        print()
        for i in reports.insights(conn, month):
            print({"good": "[+]", "warning": "[!]", "info": "[i]"}[i["level"]], i["message"])
    elif a.kind == "spending":
        start, end = month_bounds(month)
        table(reports.spending_by_category(conn, start, end), [
            ("Category", "category_name", "<"), ("Spent", money("spent_cents"), ">"),
            ("Share", lambda r: pct(r["percent"]), ">"),
            ("", lambda r: "#" * int(r["percent"] / 2), "<")])
    elif a.kind == "cashflow":
        table(reports.cash_flow(conn, a.months, month), [
            ("Month", "month", "<"), ("Income", money("income_cents"), ">"),
            ("Expenses", money("expense_cents"), ">"), ("Net", money("net_cents"), ">"),
            ("Saved", lambda r: pct(r["savings_rate"]), ">")])
    elif a.kind == "networth":
        table(reports.net_worth_history(conn, a.months, month), [
            ("Month", "month", "<"), ("Net worth", money("net_worth_cents"), ">")])
    elif a.kind == "year":
        y = reports.year_review(conn, a.year or today().year)
        s = y["summary"]
        kv([("Year", y["year"]), ("Income", fmt(s["income_cents"])),
            ("Expenses", fmt(s["expense_cents"])), ("Net", fmt(s["net_cents"])),
            ("Savings rate", pct(s["savings_rate"]))])
        print("\nTop spending categories")
        table(y["spending_by_category"][:10], [("Category", "category_name", "<"),
                                                ("Spent", money("spent_cents"), ">")])
        print("\nTop payees")
        table(y["top_payees"], [("Payee", "payee", "<"), ("Spent", money("spent_cents"), ">"),
                                ("Count", "count", ">")])


def cmd_plan(conn, a):
    if a.kind == "loan":
        r = planning.amortization(to_cents(a.principal), a.rate, a.months, to_cents(a.extra))
        kv([("Monthly payment", fmt(r["payment_cents"])), ("Months to pay off", r["months"]),
            ("Total interest", fmt(r["total_interest_cents"])), ("Total paid", fmt(r["total_paid_cents"]))]
           + ([("Interest saved", fmt(r["interest_saved_cents"]))] if a.extra != "0" else []))
        if a.schedule:
            print()
            table(r["schedule"], [("Month", "month", ">"), ("Payment", money("payment_cents"), ">"),
                                  ("Principal", money("principal_cents"), ">"),
                                  ("Interest", money("interest_cents"), ">"),
                                  ("Balance", money("balance_cents"), ">")])
    elif a.kind == "debt":
        debts = [{"name": x["name"], "balance_cents": -x["balance_cents"], "rate": x["interest_rate"],
                  "min_payment_cents": max(25_00, round(-x["balance_cents"] * 0.02))}
                 for x in L.list_accounts(conn)
                 if x["type"] in db.LIABILITY_TYPES and x["balance_cents"] < 0]
        if not debts:
            print("No debts found (credit card, loan or mortgage accounts with a balance owed).")
            return
        budget = to_cents(a.budget)
        for strategy in ("avalanche", "snowball"):
            r = planning.debt_payoff(debts, budget, strategy)
            print("%s: debt-free in %d months, total interest %s" % (
                strategy.title(), r["months"], fmt(r["total_interest_cents"])))
            for d in r["payoff_order"]:
                print("   month %3d  %s" % (d["paid_off_month"], d["name"]))
    elif a.kind == "retirement":
        r = planning.retirement_plan(a.age, a.retire_at, to_cents(a.savings), to_cents(a.monthly),
                                     a.return_rate, a.inflation, to_cents(a.income))
        kv([("Years to retirement", r["years_to_retirement"]),
            ("Projected balance", fmt(r["projected_balance_cents"])),
            ("In today's money", fmt(r["projected_real_balance_cents"])),
            ("Sustainable income / yr", fmt(r["sustainable_annual_income_cents"]) + " (today's money)"),
            ("Target nest egg", fmt(r["target_nest_egg_cents"])),
            ("On track", "yes" if r["on_track"] else "no - save %s more per month"
             % fmt(r["extra_monthly_needed_cents"]))])
    elif a.kind == "savings":
        r = planning.savings_plan(to_cents(a.target), to_cents(a.current), a.months, a.rate)
        kv([("Save each month", fmt(r["monthly_cents"])), ("Months", r["months"]),
            ("Interest earned", fmt(r["interest_earned_cents"]))])
    elif a.kind == "emergency":
        avg = reports.average_monthly(conn, 3)
        liquid = sum(x["balance_cents"] for x in L.list_accounts(conn)
                     if x["type"] in ("checking", "savings", "cash"))
        r = planning.emergency_fund(avg["expense_cents"], liquid, a.months)
        kv([("Avg monthly expenses", fmt(avg["expense_cents"])), ("Target", fmt(r["target_cents"])),
            ("Liquid savings", fmt(r["current_cents"])), ("Gap", fmt(r["gap_cents"])),
            ("Months covered", r["months_covered"] if r["months_covered"] is not None else "n/a")])


def cmd_import(conn, a):
    acct = L.find_account(conn, a.account)
    with open(a.file, encoding="utf-8-sig") as f:
        r = csvio.import_csv(conn, acct["id"], f.read(), a.invert, a.day_first)
    print("Imported %d, skipped %d duplicate(s)." % (r["imported"], r["duplicates"]))
    for e in r["errors"]:
        print("  " + e)


def cmd_export(conn, a):
    text = csvio.export_csv(conn, start=a.start, end=a.end)
    if a.file:
        with open(a.file, "w", newline="", encoding="utf-8") as f:
            f.write(text)
        print("Wrote %s" % a.file)
    else:
        sys.stdout.write(text)


def cmd_dashboard(conn, a):
    print(json.dumps(reports.dashboard(conn, a.month), indent=2))


# --------------------------------------------------------------------- parser

def build_parser():
    p = argparse.ArgumentParser(prog="finorganizer",
                                description="Track finances, budget, and plan ahead.")
    p.add_argument("--db", help="database file (default: $FINORGANIZER_DB or ~/.finorganizer.db)")
    p.add_argument("--profile", help="profile to use (default: the last one used)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open the app in your browser")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("profile", help="separate finances for different people")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("list")
    x = ss.add_parser("create")
    x.add_argument("name")
    x = ss.add_parser("use", help="switch to a profile")
    x.add_argument("name")
    x = ss.add_parser("rename")
    x.add_argument("name")
    x.add_argument("new_name")
    x = ss.add_parser("delete")
    x.add_argument("name")
    x.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_profile)

    s = sub.add_parser("summary", help="one-page printable summary (HTML)")
    s.add_argument("--month")
    s.add_argument("--file", help="output path (default: financial-summary.html)")
    s.add_argument("--open", action="store_true", help="open it in the browser")
    s.set_defaults(fn=cmd_summary)

    s = sub.add_parser("bank", help="link bank accounts via SimpleFIN for automatic updates")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("list")
    x = ss.add_parser("connect", help="connect using a SimpleFIN setup token")
    x.add_argument("token")
    x.add_argument("--label")
    x = ss.add_parser("link", help="choose what a bank account feeds")
    x.add_argument("id", type=int, help="bank account ID from 'bank list'")
    g = x.add_mutually_exclusive_group(required=True)
    g.add_argument("--new", action="store_true", help="create a new account for it")
    g.add_argument("--account", help="feed an existing account (id or name)")
    g.add_argument("--ignore", action="store_true", help="don't import it")
    x.add_argument("--type", choices=db.ACCOUNT_TYPES, help="account type when using --new")
    ss.add_parser("sync", help="download new transactions and balances")
    x = ss.add_parser("match", help="set the account balance to the bank's balance")
    x.add_argument("id", type=int)
    x = ss.add_parser("remove")
    x.add_argument("id", type=int)
    s.set_defaults(fn=cmd_bank)

    s = sub.add_parser("demo", help="load sample data into an empty database")
    s.add_argument("--months", type=int, default=6)
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("account", help="manage accounts")
    ss = s.add_subparsers(dest="action", required=True)
    x = ss.add_parser("add")
    x.add_argument("name")
    x.add_argument("--type", default="checking", choices=db.ACCOUNT_TYPES)
    x.add_argument("--balance", default="0", help="opening balance (negative for debts)")
    x.add_argument("--institution", default="")
    x.add_argument("--rate", type=float, default=0.0, help="interest rate / APR %%")
    x = ss.add_parser("list")
    x.add_argument("--all", action="store_true", help="include archived")
    for name in ("archive", "delete"):
        x = ss.add_parser(name)
        x.add_argument("account", help="id or name")
        if name == "delete":
            x.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_account)

    s = sub.add_parser("category", help="manage categories")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("list")
    x = ss.add_parser("add")
    x.add_argument("name")
    x.add_argument("--kind", default="expense", choices=db.CATEGORY_KINDS)
    x.add_argument("--group", default="")
    s.set_defaults(fn=cmd_category)

    s = sub.add_parser("tx", help="record and browse transactions")
    ss = s.add_subparsers(dest="action", required=True)
    x = ss.add_parser("add")
    x.add_argument("account", help="id or name")
    x.add_argument("amount", help="positive = money in, negative = money out")
    x.add_argument("payee")
    x.add_argument("--expense", "-e", action="store_true", help="treat amount as money out")
    x.add_argument("--category", "-c")
    x.add_argument("--date", "-d")
    x.add_argument("--memo", "-m", default="")
    x = ss.add_parser("list")
    x.add_argument("--account")
    x.add_argument("--category")
    x.add_argument("--month")
    x.add_argument("--start")
    x.add_argument("--end")
    x.add_argument("--search")
    x.add_argument("--uncategorized", action="store_true")
    x.add_argument("--limit", type=int, default=50)
    x = ss.add_parser("categorize")
    x.add_argument("id", type=int)
    x.add_argument("category")
    x = ss.add_parser("delete")
    x.add_argument("id", type=int)
    s.set_defaults(fn=cmd_tx)

    s = sub.add_parser("transfer", help="move money between accounts")
    s.add_argument("source")
    s.add_argument("dest")
    s.add_argument("amount")
    s.add_argument("--date")
    s.add_argument("--memo", default="")
    s.set_defaults(fn=cmd_transfer)

    s = sub.add_parser("budget", help="monthly budgets")
    ss = s.add_subparsers(dest="action", required=True)
    x = ss.add_parser("show")
    x.add_argument("--month")
    x = ss.add_parser("set")
    x.add_argument("category")
    x.add_argument("amount")
    x.add_argument("--month")
    x = ss.add_parser("copy")
    x.add_argument("source", help="YYYY-MM")
    x.add_argument("dest", help="YYYY-MM")
    x.add_argument("--overwrite", action="store_true")
    s.set_defaults(fn=cmd_budget)

    s = sub.add_parser("goal", help="savings goals")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("list")
    x = ss.add_parser("add")
    x.add_argument("name")
    x.add_argument("target")
    x.add_argument("--date", help="target date YYYY-MM-DD")
    x.add_argument("--account", help="track this account's balance")
    x.add_argument("--saved", default="0")
    x = ss.add_parser("contribute")
    x.add_argument("id", type=int)
    x.add_argument("amount")
    x = ss.add_parser("delete")
    x.add_argument("id", type=int)
    s.set_defaults(fn=cmd_goal)

    s = sub.add_parser("recurring", help="bills, subscriptions and paychecks")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("list")
    x = ss.add_parser("add")
    x.add_argument("name")
    x.add_argument("account")
    x.add_argument("amount")
    x.add_argument("--expense", "-e", action="store_true")
    x.add_argument("--frequency", "-f", default="monthly", choices=db.FREQUENCIES)
    x.add_argument("--next", help="next due date YYYY-MM-DD")
    x.add_argument("--category", "-c")
    x.add_argument("--payee")
    ss.add_parser("post", help="record all due occurrences as transactions")
    x = ss.add_parser("upcoming")
    x.add_argument("--days", type=int, default=30)
    x = ss.add_parser("delete")
    x.add_argument("id", type=int)
    s.set_defaults(fn=cmd_recurring)

    s = sub.add_parser("report", help="summaries and trends")
    s.add_argument("kind", choices=("summary", "spending", "cashflow", "networth", "year"))
    s.add_argument("--month")
    s.add_argument("--months", type=int, default=12)
    s.add_argument("--year", type=int)
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("plan", help="planning calculators")
    ss = s.add_subparsers(dest="kind", required=True)
    x = ss.add_parser("loan", help="loan payment & amortization")
    x.add_argument("principal")
    x.add_argument("rate", type=float, help="APR %%")
    x.add_argument("months", type=int)
    x.add_argument("--extra", default="0", help="extra payment per month")
    x.add_argument("--schedule", action="store_true")
    x = ss.add_parser("debt", help="payoff plan for your debt accounts")
    x.add_argument("budget", help="total monthly amount for debt payments")
    x = ss.add_parser("retirement")
    x.add_argument("--age", type=int, required=True)
    x.add_argument("--retire-at", type=int, default=65)
    x.add_argument("--savings", default="0")
    x.add_argument("--monthly", default="0")
    x.add_argument("--income", default="0", help="desired yearly income in today's money")
    x.add_argument("--return-rate", type=float, default=7.0)
    x.add_argument("--inflation", type=float, default=2.5)
    x = ss.add_parser("savings", help="monthly deposit needed for a target")
    x.add_argument("target")
    x.add_argument("months", type=int)
    x.add_argument("--current", default="0")
    x.add_argument("--rate", type=float, default=0.0)
    x = ss.add_parser("emergency", help="emergency fund check from your data")
    x.add_argument("--months", type=int, default=6)
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("import", help="import a bank CSV")
    s.add_argument("account")
    s.add_argument("file")
    s.add_argument("--invert", action="store_true", help="flip signs (purchases shown positive)")
    s.add_argument("--day-first", action="store_true", help="dates are DD/MM/YYYY")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("export", help="export transactions as CSV")
    s.add_argument("--file")
    s.add_argument("--start")
    s.add_argument("--end")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("dashboard", help="dashboard data as JSON")
    s.add_argument("--month")
    s.set_defaults(fn=cmd_dashboard)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    from .banksync import SyncError
    from .profiles import Profiles
    try:
        args.profiles = Profiles(args.db)
        args.profile_id = (args.profiles.find(args.profile) if args.profile
                           else args.profiles.last_used())
        conn = args.profiles.open(args.profile_id)
    except (ValueError, LookupError) as e:
        sys.exit("error: %s" % e)
    try:
        args.fn(conn, args)
    except (ValueError, LookupError, sqlite3.Error, SyncError) as e:
        sys.exit("error: %s" % e)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
