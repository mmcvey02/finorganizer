"""Investment holdings, portfolio performance and suggestions.

Holdings (what an investment account owns: a fund, a stock, cash) come from three
places, recorded in ``holdings.source``:

* ``manual``: typed in by you;
* ``import``: a positions file downloaded from the brokerage (Schwab, Fidelity, Vanguard...);
* ``bank``: SimpleFIN, for brokerages whose connection reports holdings.

A new import or bank download replaces that account's earlier imported/bank holdings;
holdings you typed in are never touched by it.

Live prices (see market.py) are optional: when they're available, each holding's value
is updated to shares x latest price. The suggestions are general rules of thumb about
diversification, costs and saving, not personal financial advice.
"""

import csv
import datetime as dt
import io
import re

from .db import INVESTMENT_ACCOUNT_TYPES, LIABILITY_TYPES, get_setting, row, rows, set_setting
from .ledger import NotFound, get_account, list_accounts
from .money import fmt, to_cents, today as _today

ASSET_CLASSES = {
    "us_stock": "US stocks",
    "intl_stock": "International stocks",
    "bond": "Bonds",
    "balanced": "Stock & bond mix",
    "cash": "Cash",
    "real_estate": "Real estate",
    "crypto": "Crypto",
    "other": "Other",
}
KINDS = {"fund": "Fund", "stock": "Individual stock", "cash": "Cash", "other": "Other"}
SOURCES = ("manual", "import", "bank")
RISK_LEVELS = ("conservative", "moderate", "aggressive")
STOCK_CLASSES = ("us_stock", "intl_stock")
# A stock & bond mix (target-date or balanced fund) is counted as roughly this much stock.
BALANCED_STOCK_SHARE = 0.6

DISCLAIMER = ("These suggestions are general rules of thumb based on the numbers in FinOrganizer. "
              "They aren't personal financial, tax or investment advice; for big decisions, "
              "consider a fee-only fiduciary advisor.")

# Well-known funds whose asset class can't be guessed from the name alone (and common cash
# sweep funds). Anything not listed is classified from its name, which you can override.
_KNOWN = {}
for _cls, _kind, _syms in [
    ("us_stock", "fund", "VTI VOO SPY IVV ITOT SCHB SCHX SCHD SCHG SCHV SCHA SCHM SCHK SWTSX SWPPX SWLGX "
                         "SWSSX FXAIX FSKAX FZROX FNILX QQQ QQQM VUG VTV VB VO DIA IWM IWF IWD IWB RSP "
                         "VIG VYM VFIAX VTSAX VIGAX MGK SPLG SPYG SPYV VONG VOOG SPTM"),
    ("intl_stock", "fund", "VXUS VEA VWO IXUS IEFA IEMG EFA EEM SCHF SCHE SCHC SWISX VTIAX VTMGX FTIHX "
                           "FZILX FSPSX SPDW ACWX VEU"),
    ("bond", "fund", "BND AGG SCHZ SCHP SCHR SCHO SCHI SCHQ SWAGX FXNAX VBTLX BNDX TLT IEF SHY GOVT "
                     "VGIT VGSH VGLT TIP VTIP MUB VTEB BSV BIV BLV LQD HYG JNK SGOV BIL FBND"),
    ("cash", "cash", "SWVXX SNVXX SNSXX SNOXX SWTXX VMFXX SPAXX FDRXX FZFXX SPRXX VMRXX VUSXX CASH CORE"),
    ("real_estate", "fund", "VNQ SCHH XLRE IYR VNQI USRT"),
    ("crypto", "fund", "IBIT FBTC GBTC BITO ETHE ETHA BITB ARKB"),
    ("crypto", "other", "BTC-USD ETH-USD SOL-USD"),
    ("other", "fund", "GLD IAU SLV DBC PDBC"),
    ("balanced", "fund", "AOR AOM AOA AOK VBIAX VSMGX VASGX VSCGX VWELX"),
]:
    for _s in _syms.split():
        _KNOWN[_s] = (_cls, _kind)
# Funds spread across the US and abroad: count them as these shares of each.
_SPLIT = {"VT": {"us_stock": 0.62, "intl_stock": 0.38}, "ACWI": {"us_stock": 0.64, "intl_stock": 0.36},
          "VTWAX": {"us_stock": 0.62, "intl_stock": 0.38}}

_WORDS = [
    ("cash", "cash", ("money market", "money fund", "cash", "sweep", "settlement", "treasury obligations",
                      "government money", "prime money")),
    ("balanced", "fund", ("target", "retirement 20", "lifestrategy", "balanced", "allocation", "freedom 20",
                          "wellington", "moderate growth", "conservative growth")),
    ("bond", "fund", ("bond", "treasury", "aggregate", "fixed income", "municipal", "muni ", "tips",
                      "income fund", "corporate", "govt", "government")),
    ("real_estate", "fund", ("reit", "real estate")),
    ("crypto", "fund", ("bitcoin", "ethereum", "crypto")),
    ("intl_stock", "fund", ("international", "intl", "ex-us", "ex us", "emerging", "developed",
                            "europe", "pacific", "foreign", "world ex", "global ex", "asia", "japan")),
    ("other", "fund", ("gold", "silver", "commodit")),
]
_FUND_HINTS = ("etf", "fund", "index", "trust", "portfolio", "ishares", "vanguard", "spdr", "schwab ",
               "fidelity ", "invesco", "admiral", "investor sh", "institutional")


def clean_symbol(symbol):
    """'SPAXX**' -> 'SPAXX', ' brk.b ' -> 'BRK.B'."""
    return re.sub(r"[^A-Z0-9.\-^=/]", "", str(symbol or "").upper())


def quote_symbol(symbol):
    """The symbol the price service uses: 'BRK.B' and 'BRK/B' are 'BRK-B' there."""
    s = clean_symbol(symbol)
    if re.fullmatch(r"[A-Z]{1,5}[./][A-Z]", s):
        s = s[:-2] + "-" + s[-1]
    return s


def guess_class(symbol, name="", quote_type=""):
    """Best guess of (asset_class, kind) for a holding."""
    sym, text = clean_symbol(symbol), " %s " % (name or "").lower()
    if sym in _SPLIT:
        return "us_stock", "fund"
    if sym in _KNOWN:
        return _KNOWN[sym]
    qt = (quote_type or "").upper()
    if qt == "MONEYMARKET":
        return "cash", "cash"
    if qt == "CRYPTOCURRENCY":
        return "crypto", "other"
    if not sym and not text.strip():
        return "other", "other"
    if not sym and "cash" in text:
        return "cash", "cash"
    for cls, kind, words in _WORDS:
        if any(w in text for w in words):
            if cls == "cash" and sym and qt in ("EQUITY", "ETF") and "money" not in text:
                continue  # e.g. a company with "Cash" in its name
            return cls, kind
    is_fund = (qt in ("ETF", "MUTUALFUND") or any(w in text for w in _FUND_HINTS)
               or bool(re.fullmatch(r"[A-Z]{4}X", sym)))
    return "us_stock", ("fund" if is_fund else "stock")


def _split(h):
    """{asset_class: share} for one holding (most are 100% one class)."""
    if not h.get("asset_class_set") and clean_symbol(h.get("symbol")) in _SPLIT:
        return _SPLIT[clean_symbol(h["symbol"])]
    return {h["asset_class"]: 1.0}


# ------------------------------------------------------------------ storage

def _resolve(h):
    """Fill in the asset class and kind when they weren't chosen by hand."""
    h = dict(h)
    cls, kind = guess_class(h.get("symbol"), h.get("name"), h.get("quote_type"))
    h["asset_class_set"] = bool(h.get("asset_class"))
    h["asset_class"] = h.get("asset_class") or cls
    h["kind"] = h.get("kind") or kind
    return h


def list_holdings(conn, account_id=None):
    sql = ("SELECT h.*, a.name AS account_name, a.type AS account_type FROM holdings h"
           " JOIN accounts a ON a.id = h.account_id WHERE a.archived = 0")
    args = ()
    if account_id is not None:
        sql += " AND h.account_id = ?"
        args = (account_id,)
    return [_resolve(h) for h in rows(conn.execute(sql + " ORDER BY a.name COLLATE NOCASE, h.value_cents DESC", args))]


def get_holding(conn, holding_id):
    h = row(conn.execute("SELECT * FROM holdings WHERE id = ?", (holding_id,)))
    if h is None:
        raise NotFound("holding %s not found" % holding_id)
    return h


def _num(v, what, allow_none=True):
    if v is None or (isinstance(v, str) and v.strip() in ("", "--", "N/A", "n/a")):
        if allow_none:
            return None
        raise ValueError("%s is required" % what)
    try:
        f = float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except ValueError:
        raise ValueError("%s must be a number" % what)
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("%s must be a number" % what)
    return f


def _clean_fields(fields):
    out = {}
    if "symbol" in fields:
        out["symbol"] = clean_symbol(fields["symbol"])
    if "name" in fields:
        out["name"] = str(fields["name"] or "").strip()[:120]
    if "shares" in fields:
        out["shares"] = _num(fields["shares"], "shares") or 0.0
    if "price" in fields:
        out["price"] = _num(fields["price"], "price")
    for key in ("value_cents", "cost_basis_cents"):
        if key in fields:
            v = fields[key]
            out[key] = None if v in (None, "") else int(v)
    if "expense_ratio" in fields:
        er = _num(fields["expense_ratio"], "expense ratio")
        if er is not None and not 0 <= er <= 5:
            raise ValueError("expense ratio is a yearly percentage, e.g. 0.03")
        out["expense_ratio"] = er
    if "asset_class" in fields:
        c = fields["asset_class"] or ""
        if c and c not in ASSET_CLASSES:
            raise ValueError("asset class must be one of: %s" % ", ".join(ASSET_CLASSES))
        out["asset_class"] = c
    if "kind" in fields:
        k = fields["kind"] or ""
        if k and k not in KINDS:
            raise ValueError("kind must be one of: %s" % ", ".join(KINDS))
        out["kind"] = k
    return out


def add_holding(conn, account_id, symbol="", name="", shares=0, value_cents=None, cost_basis_cents=None,
                price=None, asset_class="", kind="", expense_ratio=None, source="manual",
                external_id=None, commit=True):
    get_account(conn, account_id)
    f = _clean_fields({"symbol": symbol, "name": name, "shares": shares, "price": price,
                       "value_cents": value_cents, "cost_basis_cents": cost_basis_cents,
                       "asset_class": asset_class, "kind": kind, "expense_ratio": expense_ratio})
    if not f["symbol"] and not f["name"]:
        raise ValueError("enter a symbol or a name")
    if f["value_cents"] is None:
        if f["price"] is None:
            raise ValueError("enter the current value, or shares and price")
        f["value_cents"] = int(round(f["shares"] * f["price"] * 100))
    if f["price"] is None and f["shares"]:
        f["price"] = f["value_cents"] / 100.0 / f["shares"]
    hid = conn.execute(
        "INSERT INTO holdings (account_id, symbol, name, shares, price, value_cents, cost_basis_cents,"
        " asset_class, kind, expense_ratio, source, external_id, price_date)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, f["symbol"], f["name"], f["shares"], f["price"], f["value_cents"], f["cost_basis_cents"],
         f["asset_class"], f["kind"], f["expense_ratio"], source, external_id, _today().isoformat())).lastrowid
    if commit:
        conn.commit()
    return hid


def update_holding(conn, holding_id, **fields):
    h = get_holding(conn, holding_id)
    if "account_id" in fields:
        get_account(conn, int(fields["account_id"]))
    f = _clean_fields(fields)
    if "account_id" in fields:
        f["account_id"] = int(fields["account_id"])
    symbol = f.get("symbol", h["symbol"])
    if not symbol and not f.get("name", h["name"]):
        raise ValueError("enter a symbol or a name")
    shares = f.get("shares", h["shares"])
    if ("shares" in f or "price" in f) and "value_cents" not in f:
        price = f.get("price", h["price"])
        if price is not None:
            f["value_cents"] = int(round(shares * price * 100))
    elif "value_cents" in f and f["value_cents"] is None:
        raise ValueError("value is required")
    elif "value_cents" in f and shares and "price" not in f:
        f["price"] = f["value_cents"] / 100.0 / shares
    if not f:
        return
    # Edited by hand: keep it through later imports and bank downloads.
    f["source"] = "manual"
    f["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE holdings SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in f),
                 list(f.values()) + [holding_id])
    conn.commit()


def delete_holding(conn, holding_id):
    if conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,)).rowcount == 0:
        raise NotFound("holding %s not found" % holding_id)
    conn.commit()


# ------------------------------------------------------------- positions file

_COLUMNS = {
    "symbol": ("symbol", "ticker"),
    "name": ("description", "investment name", "security name", "name", "security"),
    "shares": ("quantity", "qty", "shares"),
    "price": ("last price", "share price", "price"),
    "value": ("market value", "mkt val", "current value", "total value", "value"),
    "cost": ("cost basis total", "cost basis", "total cost", "cost"),
    "type": ("security type", "asset type", "type"),
}
_SKIP_ROWS = ("account total", "total", "pending activity", "grand total")


def _find_columns(header):
    cols, low = {}, [h.strip().lower() for h in header]
    for key, names in _COLUMNS.items():
        for name in names:  # most specific name first
            for i, h in enumerate(low):
                if i in cols.values():
                    continue
                if h == name or h.startswith(name + " ") or h.startswith(name + "(") or (len(name) > 4 and name in h):
                    if key == "price" and ("chng" in h or "change" in h or "%" in h):
                        continue
                    if key == "value" and ("chng" in h or "change" in h or "%" in h):
                        continue
                    if key == "cost" and ("per share" in h or "avg" in h or "average" in h):
                        continue
                    cols[key] = i
                    break
            if key in cols:
                break
    return cols


def _money_or_none(v):
    s = str(v or "").strip()
    if s in ("", "--", "N/A", "n/a", "-"):
        return None
    try:
        return to_cents(s)
    except ValueError:
        return None


def parse_positions(text):
    """Read a brokerage positions download into holdings dicts.

    Works with the CSV files Schwab, Fidelity, Vanguard and most brokerages offer
    ("Export" on the positions page): it looks for a header row with a Symbol column and
    reads quantity, price, market value and cost basis where present.
    """
    text = (text or "").lstrip("﻿")
    lines = list(csv.reader(io.StringIO(text)))
    start = cols = None
    for i, r in enumerate(lines[:40]):
        c = _find_columns(r)
        if "symbol" in c and ("value" in c or "shares" in c):
            start, cols = i, c
            break
    if start is None:
        raise ValueError("couldn't find the holdings in that file. It needs a header row with "
                         "Symbol and Quantity or Market Value columns (download the positions "
                         "page as a CSV file from your brokerage)")
    out, skipped = [], 0
    for r in lines[start + 1:]:
        if not any(x.strip() for x in r):
            if out:
                break  # Fidelity and others put notes after a blank line
            continue
        get = lambda k: r[cols[k]].strip() if k in cols and cols[k] < len(r) else ""
        sym, name = get("symbol"), get("name")
        name = "" if name in ("--", "-", "N/A", "n/a") else name
        if sym.lower() in _SKIP_ROWS or (not sym and name.lower() in _SKIP_ROWS):
            continue
        if sym.lower().startswith(("cash & cash", "cash and cash")) or sym.lower() in ("cash", "cash & sweep"):
            name, sym = name or sym, ""
        shares = _num(get("shares"), "quantity") if get("shares") not in ("", "--", "N/A") else None
        price = _money_or_none(get("price"))
        value = _money_or_none(get("value"))
        if value is None and shares is not None and price is not None:
            value = int(round(shares * price))
        if value is None or (not sym and not name):
            skipped += 1
            continue
        stype = get("type").lower()
        cls = kind = ""
        if "cash" in stype or "money market" in stype or (not sym and "cash" in name.lower()):
            cls, kind = "cash", "cash"
        elif stype in ("equity", "stock", "stocks"):
            kind = "stock"
        out.append({"symbol": clean_symbol(sym), "name": name or sym, "shares": shares or 0.0,
                    "price": price / 100.0 if price is not None else None, "value_cents": value,
                    "cost_basis_cents": _money_or_none(get("cost")), "asset_class": cls, "kind": kind})
    if not out:
        raise ValueError("no holdings found in that file")
    return out, skipped


def import_positions(conn, account_id, text):
    """Replace an account's imported holdings with the ones in a positions file."""
    get_account(conn, account_id)
    items, skipped = parse_positions(text)
    conn.execute("DELETE FROM holdings WHERE account_id = ? AND source IN ('import', 'bank')", (account_id,))
    for h in items:
        add_holding(conn, account_id, source="import", commit=False, **h)
    conn.commit()
    return {"imported": len(items), "skipped": skipped,
            "value_cents": sum(h["value_cents"] for h in items)}


def apply_bank_holdings(conn, account_id, holdings):
    """Store the holdings a SimpleFIN download reported for one account (replacing the last ones)."""
    items = []
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        try:
            value = to_cents(h.get("market_value"))
        except (ValueError, TypeError):
            continue
        try:
            cost = to_cents(h["cost_basis"]) if h.get("cost_basis") not in (None, "") else None
        except (ValueError, TypeError):
            cost = None
        if cost == 0 and value:
            cost = None  # brokerages send 0 when they don't know it
        try:
            shares = float(h.get("shares") or 0)
        except (TypeError, ValueError):
            shares = 0.0
        sym, name = clean_symbol(h.get("symbol")), str(h.get("description") or "").strip()
        if not sym and not name:
            continue
        items.append((sym, name, shares, value, cost, str(h.get("id") or "") or None))
    if not items and not holdings:
        return 0
    conn.execute("DELETE FROM holdings WHERE account_id = ? AND source IN ('import', 'bank')", (account_id,))
    manual = {r[0] for r in conn.execute("SELECT symbol FROM holdings WHERE account_id = ? AND symbol != ''",
                                         (account_id,))}
    n = 0
    for sym, name, shares, value, cost, ext in items:
        if sym and sym in manual:
            continue  # you've entered this one yourself; yours wins
        add_holding(conn, account_id, sym, name or sym, shares, value, cost, source="bank",
                    external_id=ext, commit=False)
        n += 1
    return n


# ------------------------------------------------------------------- prices

def symbols_to_price(conn):
    return sorted({quote_symbol(h["symbol"]) for h in list_holdings(conn)
                   if h["symbol"] and h["kind"] != "cash" and h["asset_class"] != "cash"})


def apply_prices(conn, quotes, today=None):
    """Update each holding's price and value from live quotes ({symbol: market.chart data})."""
    day = (today or _today()).isoformat()
    n = 0
    for h in list_holdings(conn):
        q = quotes.get(quote_symbol(h["symbol"])) if h["symbol"] else None
        if not q or h["kind"] == "cash" or h["asset_class"] == "cash" or not h["shares"]:
            continue
        value = int(round(h["shares"] * q["price"] * 100))
        conn.execute("UPDATE holdings SET price = ?, value_cents = ?, price_date = ? WHERE id = ?",
                     (q["price"], value, q.get("as_of") or day, h["id"]))
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------- portfolio

def get_profile(conn):
    age = get_setting(conn, "invest_age")
    return {"age": int(age) if age and age.isdigit() else None,
            "risk": get_setting(conn, "invest_risk") or "moderate"}


def set_profile(conn, age=None, risk=None):
    if age not in (None, ""):
        try:
            age = int(age)
        except (TypeError, ValueError):
            raise ValueError("age must be a whole number")
        if not 14 <= age <= 110:
            raise ValueError("age must be between 14 and 110")
        set_setting(conn, "invest_age", str(age))
    elif age == "":
        conn.execute("DELETE FROM settings WHERE key = 'invest_age'")
        conn.commit()
    if risk is not None:
        if risk not in RISK_LEVELS:
            raise ValueError("risk must be one of: %s" % ", ".join(RISK_LEVELS))
        set_setting(conn, "invest_risk", risk)
    return get_profile(conn)


def target_stock_percent(profile):
    """A common rule of thumb: about 110 minus your age in stocks, nudged for risk tolerance."""
    base = 110 - profile["age"] if profile.get("age") else 70
    base += {"conservative": -15, "moderate": 0, "aggressive": 10}.get(profile.get("risk"), 0)
    return max(20, min(95, base))


def _investment_accounts(conn):
    held = {r[0] for r in conn.execute("SELECT DISTINCT account_id FROM holdings")}
    return [a for a in list_accounts(conn) if a["type"] in INVESTMENT_ACCOUNT_TYPES or a["id"] in held]


def _bank_balances(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT account_id, balance_cents FROM remote_accounts WHERE account_id IS NOT NULL"
        " AND balance_cents IS NOT NULL")}


def contributions(conn, start, end):
    """Money moved into investment accounts from your other accounts, minus money taken out."""
    types = "(%s)" % ", ".join("'%s'" % t for t in INVESTMENT_ACCOUNT_TYPES)
    return conn.execute(
        "SELECT COALESCE(SUM(t.amount_cents), 0) FROM transactions t"
        " JOIN accounts a ON a.id = t.account_id JOIN transactions o ON o.id = t.transfer_id"
        " JOIN accounts oa ON oa.id = o.account_id"
        " WHERE t.date > ? AND t.date <= ? AND a.type IN " + types + " AND oa.type NOT IN " + types,
        (start, end)).fetchone()[0]


def record_snapshot(conn, accounts, day):
    for a in accounts:
        conn.execute("INSERT INTO portfolio_snapshots (date, account_id, value_cents, cost_cents)"
                     " VALUES (?, ?, ?, ?) ON CONFLICT (date, account_id) DO UPDATE SET"
                     " value_cents = excluded.value_cents, cost_cents = excluded.cost_cents",
                     (day, a["id"], a["value_cents"], a["cost_cents"]))
    conn.commit()


def history(conn):
    return rows(conn.execute("SELECT date, SUM(value_cents) AS value_cents FROM portfolio_snapshots"
                             " WHERE account_id IN (SELECT id FROM accounts WHERE archived = 0)"
                             " GROUP BY date ORDER BY date"))


def portfolio(conn, quotes=None, today=None, record=True):
    """Everything the Investments page shows except market news.

    ``quotes`` ({symbol: market.chart data}) adds today's change and the past-year comparison.
    """
    today = today or _today()
    quotes = quotes or {}
    holdings = list_holdings(conn)
    bank = _bank_balances(conn)
    accounts = []
    for a in _investment_accounts(conn):
        hs = [h for h in holdings if h["account_id"] == a["id"]]
        held = sum(h["value_cents"] for h in hs)
        bal = bank.get(a["id"], a["balance_cents"])
        if hs and any(h["source"] == "bank" for h in hs) and a["id"] in bank and bank[a["id"]] - held > 100:
            # The bank's balance includes cash it didn't list as a holding.
            hs.append(_resolve({"id": None, "account_id": a["id"], "account_name": a["name"],
                                "account_type": a["type"], "symbol": "", "name": "Cash (not listed as a holding)",
                                "shares": 0, "price": None, "value_cents": bank[a["id"]] - held,
                                "cost_basis_cents": None, "asset_class": "cash", "kind": "cash",
                                "expense_ratio": None, "source": "bank", "price_date": None, "synthetic": True}))
            holdings.append(hs[-1])
        value = sum(h["value_cents"] for h in hs) if hs else bal
        known_cost = [h for h in hs if h["cost_basis_cents"] is not None]
        accounts.append({"id": a["id"], "name": a["name"], "type": a["type"], "institution": a["institution"],
                         "value_cents": value, "balance_cents": a["balance_cents"], "holding_count": len(hs),
                         "cost_cents": sum(h["cost_basis_cents"] for h in known_cost) if known_cost else None,
                         "uses_balance": not hs})
    total = sum(a["value_cents"] for a in accounts)

    # Per-holding figures.
    for h in holdings:
        h["weight"] = round(100.0 * h["value_cents"] / total, 2) if total > 0 else 0
        c = h["cost_basis_cents"]
        h["gain_cents"] = h["value_cents"] - c if c is not None else None
        h["gain_pct"] = round(100.0 * (h["value_cents"] - c) / c, 2) if c else None
        q = quotes.get(quote_symbol(h["symbol"])) if h["symbol"] else None
        h["day_change_cents"] = (int(round(h["shares"] * (q["price"] - q["previous_close"]) * 100))
                                 if q and q.get("previous_close") is not None and h["shares"]
                                 and h["asset_class"] != "cash" else None)
        h["asset_class_label"] = ASSET_CLASSES.get(h["asset_class"], h["asset_class"])

    with_cost = [h for h in holdings if h["cost_basis_cents"] is not None]
    cost = sum(h["cost_basis_cents"] for h in with_cost)
    gain = sum(h["gain_cents"] for h in with_cost)
    day_moves = [h["day_change_cents"] for h in holdings if h["day_change_cents"] is not None]

    # Allocation by asset class (accounts without holdings count as "unknown").
    alloc = {}
    for h in holdings:
        for cls, share in _split(h).items():
            alloc[cls] = alloc.get(cls, 0) + h["value_cents"] * share
    unknown = sum(a["value_cents"] for a in accounts if a["uses_balance"])
    allocation = [{"asset_class": k, "label": ASSET_CLASSES[k], "value_cents": int(round(v)),
                   "percent": round(100.0 * v / total, 1) if total > 0 else 0}
                  for k, v in sorted(alloc.items(), key=lambda kv: -kv[1]) if v]
    if unknown:
        allocation.append({"asset_class": "unknown", "label": "Not broken down (no holdings entered)",
                           "value_cents": unknown, "percent": round(100.0 * unknown / total, 1) if total > 0 else 0})

    if record and accounts:
        record_snapshot(conn, accounts, today.isoformat())
    hist = history(conn)
    year_ago = (today - dt.timedelta(days=365)).isoformat()
    since = None
    if len(hist) >= 2:
        first = hist[0]
        added = contributions(conn, first["date"], today.isoformat())
        growth = total - first["value_cents"] - added
        since = {"start_date": first["date"], "start_cents": first["value_cents"], "contributed_cents": added,
                 "growth_cents": growth,
                 "growth_pct": round(100.0 * growth / (first["value_cents"] + max(added, 0)), 2)
                 if first["value_cents"] + max(added, 0) > 0 else None}

    profile = get_profile(conn)
    result = {
        "as_of": today.isoformat(),
        "total_cents": total,
        "cost_cents": cost if with_cost else None,
        "gain_cents": gain if with_cost else None,
        "gain_pct": round(100.0 * gain / cost, 2) if cost else None,
        "cost_coverage": round(100.0 * sum(h["value_cents"] for h in with_cost) / total, 1) if total > 0 else 0,
        "day_change_cents": sum(day_moves) if day_moves else None,
        "invested_year_cents": _invested(conn, year_ago, today.isoformat()),
        "accounts": accounts,
        "holdings": holdings,
        "allocation": allocation,
        "history": hist,
        "since_tracking": since,
        "past_year": past_year(holdings, quotes, total),
        "profile": profile,
        "target_stock_percent": target_stock_percent(profile),
        "asset_classes": ASSET_CLASSES,
        "kinds": KINDS,
    }
    result["suggestions"] = suggestions(conn, result, today)
    result["disclaimer"] = DISCLAIMER
    return result


def _invested(conn, start, end):
    from .reports import invested
    return invested(conn, start, end)


def past_year(holdings, quotes, total):
    """How today's holdings would have done over the past year, next to the S&P 500.

    Assumes the same shares were held all year (it shows how your current mix of
    investments has behaved, not your actual return, which depends on when you bought).
    """
    from .market import BENCHMARK
    bench = quotes.get(BENCHMARK)
    if not bench or len(bench["history"]) < 20 or total <= 0:
        return None
    dates = [d for d, _ in bench["history"]]
    priced, fixed = [], 0
    for h in holdings:
        q = quotes.get(quote_symbol(h["symbol"])) if h["symbol"] else None
        if q and len(q["history"]) >= 20 and h["shares"] and h["asset_class"] != "cash":
            priced.append((h, dict(q["history"])))
        else:
            fixed += h["value_cents"]
    covered = sum(h["value_cents"] for h, _ in priced)
    if not priced or covered < 0.5 * total:
        return None
    series, last = [], {}
    for d in dates:
        v = fixed
        for h, closes in priced:
            if d in closes:
                last[h["id"]] = closes[d]
            price = last.get(h["id"])
            if price is None:
                price = next(iter(closes.values()))  # before its first close: use the first one
            v += h["shares"] * price * 100
        series.append((d, v))
    bench_closes = dict(bench["history"])
    start_v, start_b = series[0][1], bench_closes[dates[0]]
    points = [{"date": d, "portfolio_pct": round(100.0 * (v - start_v) / start_v, 2) if start_v else 0,
               "benchmark_pct": round(100.0 * (bench_closes[d] - start_b) / start_b, 2)}
              for d, v in series]
    step = max(1, len(points) // 60)
    sampled = points[::step]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return {"start_date": dates[0], "portfolio_pct": points[-1]["portfolio_pct"],
            "benchmark_pct": points[-1]["benchmark_pct"], "benchmark": "S&P 500",
            "coverage_pct": round(100.0 * covered / total, 1), "points": sampled}


# -------------------------------------------------------------- suggestions

def _whole(cents):
    """$5,223 rather than $5,223.10, for rough amounts."""
    return fmt(int(round(cents / 100.0)) * 100)[:-3]


def _s(level, title, message, kind):
    return {"level": level, "title": title, "message": message, "kind": kind}


def suggestions(conn, p, today=None):
    """Plain-English ideas for improving the portfolio, most important first."""
    from .reports import average_monthly, period_summary
    today = today or _today()
    out = []
    total = p["total_cents"]
    holdings = [h for h in p["holdings"]]
    accts = list_accounts(conn)

    # Foundations first: emergency cash and expensive debt.
    avg = average_monthly(conn, 3)
    liquid = sum(a["balance_cents"] for a in accts if a["type"] in ("checking", "savings", "cash"))
    if avg["expense_cents"] > 0 and liquid < 3 * avg["expense_cents"]:
        out.append(_s("warning", "Build your emergency fund",
                      "You have %s in cash accounts, about %.1f months of spending. Most guidelines suggest "
                      "3 to 6 months (%s to %s) in savings before investing more, so you're never forced to "
                      "sell investments at a bad time." % (fmt(liquid), liquid / avg["expense_cents"],
                                                           fmt(3 * avg["expense_cents"]), fmt(6 * avg["expense_cents"])),
                      "foundation"))
    debts = [a for a in accts if a["type"] in LIABILITY_TYPES and a["balance_cents"] < 0 and (a["interest_rate"] or 0) >= 8]
    for a in sorted(debts, key=lambda a: -a["interest_rate"])[:2]:
        out.append(_s("warning", "Pay down %s first" % a["name"],
                      "It charges %.1f%% interest on %s. Paying it off is a guaranteed %.1f%% return, more "
                      "than stocks average over time, so extra money usually does more good there."
                      % (a["interest_rate"], fmt(-a["balance_cents"]), a["interest_rate"]), "foundation"))

    if total <= 0:
        out.append(_s("info", "Add your investments",
                      "Add your brokerage and retirement accounts on the Accounts page (type Investment or "
                      "Retirement), then add their holdings here, or import a positions file from your "
                      "brokerage, to see performance and suggestions.", "setup"))
        return out

    if any(a["uses_balance"] for a in p["accounts"]):
        names = ", ".join(a["name"] for a in p["accounts"] if a["uses_balance"])
        out.append(_s("info", "Add holdings to see the full picture",
                      "%s %s no holdings entered, so FinOrganizer can't tell what %s invested in. Import a "
                      "positions file from the brokerage or add the holdings by hand."
                      % (names, "has" if names.count(",") == 0 else "have",
                         "it's" if names.count(",") == 0 else "they're"), "setup"))

    # Stock / bond mix against a rule-of-thumb target.
    alloc = {a["asset_class"]: a["value_cents"] for a in p["allocation"]}
    known = total - alloc.get("unknown", 0)
    if known > 0:
        stocks = alloc.get("us_stock", 0) + alloc.get("intl_stock", 0) + BALANCED_STOCK_SHARE * alloc.get("balanced", 0)
        safe = alloc.get("bond", 0) + alloc.get("cash", 0) + (1 - BALANCED_STOCK_SHARE) * alloc.get("balanced", 0)
        stock_pct = 100.0 * stocks / known
        target = p["target_stock_percent"]
        why = ("for age %d with %s risk tolerance" % (p["profile"]["age"], p["profile"]["risk"])
               if p["profile"]["age"] else "(a typical middle-of-the-road mix; add your age for a personal target)")
        diff = stock_pct - target
        if abs(diff) > 10 and stocks + safe > 0:
            move = abs(diff) / 100.0 * known
            if diff > 0:
                msg = ("About %.0f%% of your investments are in stocks; a common guideline %s is about %d%%. "
                       "Moving roughly %s into bond funds would lower how far your portfolio can fall in a "
                       "crash. In retirement accounts this doesn't trigger taxes; in a taxable account, "
                       "directing new contributions to bonds avoids selling." % (stock_pct, why, target, _whole(move)))
                out.append(_s("warning", "More stock-heavy than your target", msg, "allocation"))
            else:
                msg = ("Only about %.0f%% of your investments are in stocks; a common guideline %s is about %d%%. "
                       "Over long periods stocks have grown much faster than bonds and cash, so moving roughly "
                       "%s toward a broad stock index fund could help your money keep ahead of inflation, if "
                       "you won't need it for 5+ years." % (stock_pct, why, target, _whole(move)))
                out.append(_s("info", "More conservative than your target", msg, "allocation"))
        else:
            out.append(_s("good", "Your stock/bond mix is on target",
                          "About %.0f%% stocks against a guideline of %d%% %s. Check once or twice a year and "
                          "rebalance if it drifts more than 10 points." % (stock_pct, target, why), "allocation"))

        intl, us = alloc.get("intl_stock", 0), alloc.get("us_stock", 0)
        if us + intl > 0.2 * known and intl < 0.15 * (us + intl):
            out.append(_s("info", "Consider international stocks",
                          "Only %.0f%% of your stocks are outside the US. Many diversified portfolios hold 20-40%% "
                          "of their stocks internationally, usually through one total international index fund, "
                          "so a slump in one country hurts less." % (100.0 * intl / (us + intl)), "allocation"))

        cash = alloc.get("cash", 0)
        if cash > 0.10 * known and cash > 1_000_00:
            out.append(_s("info", "Uninvested cash",
                          "%s (%.0f%%) of your investment accounts is sitting in cash. If it isn't money you'll "
                          "need soon, investing it according to your plan puts it to work; in a retirement "
                          "account especially, cash left uninvested is a common and costly oversight."
                          % (fmt(cash), 100.0 * cash / known), "allocation"))

        crypto = alloc.get("crypto", 0)
        if crypto > 0.10 * known:
            out.append(_s("warning", "High crypto share",
                          "Crypto is %.0f%% of your investments. It can lose more than half its value in a year; "
                          "many planners suggest keeping speculative holdings to 5%% or less." % (100.0 * crypto / known),
                          "risk"))

    # Concentration in single companies.
    singles = [h for h in holdings if h["kind"] == "stock"]
    for h in sorted(singles, key=lambda h: -h["value_cents"]):
        if h["weight"] > 10:
            out.append(_s("warning", "%s is %.0f%% of your portfolio" % (h["symbol"] or h["name"], h["weight"]),
                          "A lot rides on one company. A common guideline is to keep any single stock under "
                          "5-10% of your investments; trimming it, or putting new money elsewhere, spreads the risk.",
                          "risk"))
    single_total = sum(h["value_cents"] for h in singles)
    if single_total > 0.3 * total and len(singles) > 1:
        out.append(_s("info", "Lots of individual stocks",
                      "Individual stocks are %.0f%% of your portfolio. Broad index funds own hundreds or thousands "
                      "of companies for a tiny fee, and most professional stock pickers don't beat them over time."
                      % (100.0 * single_total / total), "risk"))

    # Costs.
    priced = [h for h in holdings if h.get("expense_ratio") is not None]
    for h in sorted([h for h in priced if h["expense_ratio"] >= 0.5], key=lambda h: -h["value_cents"] * h["expense_ratio"])[:3]:
        yearly = int(h["value_cents"] * h["expense_ratio"] / 100)
        out.append(_s("warning", "%s charges %.2f%% a year" % (h["symbol"] or h["name"], h["expense_ratio"]),
                      "That's about %s a year on what you hold. Comparable index funds often cost 0.10%% or "
                      "less; over decades the difference compounds into a large sum." % fmt(yearly), "cost"))
    if priced and not any(h["expense_ratio"] >= 0.5 for h in priced):
        w = sum(h["value_cents"] * h["expense_ratio"] for h in priced) / max(1, sum(h["value_cents"] for h in priced))
        out.append(_s("good", "Low costs", "Your funds' average expense ratio is %.2f%% a year." % w, "cost"))
    elif not priced and any(h["kind"] == "fund" for h in holdings):
        out.append(_s("info", "Check your funds' fees",
                      "Add each fund's expense ratio (its yearly fee, on the fund's page at your brokerage) to "
                      "see what you're paying. Fees above 0.5% a year are worth a second look.", "cost"))

    # Tax-loss harvesting in taxable accounts.
    losers = [h for h in holdings if h["account_type"] == "investment" and h["gain_cents"] is not None
              and h["gain_cents"] <= -500_00 and (h["gain_pct"] or 0) <= -10]
    if losers:
        names = ", ".join("%s (%s)" % (h["symbol"] or h["name"], fmt(h["gain_cents"])) for h in losers[:3])
        out.append(_s("info", "Possible tax-loss harvesting",
                      "In your taxable account, %s %s below what you paid. Selling and buying a similar (not "
                      "identical) fund can lower this year's taxes while staying invested. Avoid buying the same "
                      "fund back within 30 days (the wash-sale rule)." % (names, "is" if len(losers) == 1 else "are"),
                      "tax"))

    # Saving rate.
    year_ago = (today - dt.timedelta(days=365)).isoformat()
    s = period_summary(conn, year_ago, today.isoformat())
    if s["income_cents"] > 0:
        rate = 100.0 * p["invested_year_cents"] / s["income_cents"]
        if rate < 10:
            out.append(_s("info", "Invest a bit more each month",
                          "Over the past year you moved %s (%.0f%% of income) into investments that FinOrganizer "
                          "can see. A common goal is 15%% of income for retirement, including any employer match; "
                          "an automatic monthly transfer makes it painless. (Paycheck 401(k) contributions don't "
                          "show up here unless you record them.)" % (fmt(max(0, p["invested_year_cents"])), max(0, rate)),
                          "saving"))
        elif rate >= 15:
            out.append(_s("good", "Strong saving habit",
                          "You invested %.0f%% of your income over the past year, at or above the common 15%% goal."
                          % rate, "saving"))

    if holdings and p["cost_coverage"] < 50:
        out.append(_s("info", "Add cost basis to track gains",
                      "Most holdings have no cost basis (what you paid). Add it, or import a positions file that "
                      "includes it, to see your gains and losses.", "setup"))

    order = {"warning": 0, "info": 1, "good": 2}
    out.sort(key=lambda x: order[x["level"]])
    return out
