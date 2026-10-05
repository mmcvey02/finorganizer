"""One-page printable financial summary, rendered as a self-contained HTML document.

Open it in a browser and print (or "Save as PDF"). Layout is sized for a single
Letter/A4 page; long lists are trimmed to their most important rows.
"""

import datetime as dt
from html import escape

from . import reports
from .db import LIABILITY_TYPES
from .ledger import budget_status, list_goals, upcoming
from .money import fmt, month_bounds, month_of, today

TYPE_LABELS = {
    "checking": "Checking", "savings": "Savings", "credit_card": "Credit card", "cash": "Cash",
    "investment": "Investment", "retirement": "Retirement", "loan": "Loan",
    "mortgage": "Mortgage", "property": "Property", "other": "Other",
}


def _m(cents, whole=False):
    if cents is None:
        return ""
    if whole:
        sign = "-" if cents < 0 else ""
        return "%s$%s" % (sign, "{:,.0f}".format(abs(cents) / 100))
    return fmt(cents)


def _month_name(month):
    y, m = map(int, month.split("-"))
    return dt.date(y, m, 1).strftime("%B %Y")


def _table(headers, body_rows, cls=""):
    head = "".join('<th class="%s">%s</th>' % ("r" if h.startswith(">") else "", escape(h.lstrip(">")))
                   for h in headers)
    rows_html = "".join("<tr>%s</tr>" % "".join(
        '<td class="%s">%s</td>' % (c[0], c[1]) if isinstance(c, tuple) else "<td>%s</td>" % c
        for c in r) for r in body_rows)
    if not body_rows:
        rows_html = '<tr><td colspan="%d" class="muted">None</td></tr>' % len(headers)
    return '<table class="%s"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' % (cls, head, rows_html)


def _change(history):
    if len(history) < 2:
        return ""
    d = history[-1]["net_worth_cents"] - history[0]["net_worth_cents"]
    return '<span class="%s">%s%s</span> over %d months' % (
        "neg" if d < 0 else "pos", "+" if d >= 0 else "", _m(d, True), len(history) - 1)


def _num(cents, whole=False):
    return ("r neg" if (cents or 0) < 0 else "r", _m(cents, whole))


# Print palette (validated categorical slots 1-2 of the app's chart palette).
INCOME = "#2a78d6"
EXPENSE = "#eb6834"
GRID = "#e3e3e3"
INK2 = "#555"


def _short(cents):
    v, sign = abs(cents) / 100, "-" if cents < 0 else ""
    if v >= 1e6:
        return "%s$%.1fM" % (sign, v / 1e6)
    if v >= 1e3:
        return "%s$%.0fk" % (sign, v / 1e3) if v >= 1e4 else "%s$%.1fk" % (sign, v / 1e3)
    return "%s$%.0f" % (sign, v)


def _ticks(lo, hi, n=3):
    if hi == lo:
        hi = lo + 100
    raw = (hi - lo) / n
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = (lo // step) * step
    out, v = [], start
    while v < hi + step:
        out.append(v)
        v += step
    return out


def _bar_path(x, y0, y1, w, r=2.5):
    """Bar from baseline y0 to value y1, rounded only at the data end."""
    r = min(r, w / 2, abs(y1 - y0))
    if y1 <= y0:  # grows upward
        return "M%.1f,%.1f V%.1f Q%.1f,%.1f %.1f,%.1f H%.1f Q%.1f,%.1f %.1f,%.1f V%.1f Z" % (
            x, y0, y1 + r, x, y1, x + r, y1, x + w - r, x + w, y1, x + w, y1 + r, y0)
    return "M%.1f,%.1f V%.1f Q%.1f,%.1f %.1f,%.1f H%.1f Q%.1f,%.1f %.1f,%.1f V%.1f Z" % (
        x, y0, y1 - r, x, y1, x + r, y1, x + w - r, x + w, y1, x + w, y1 - r, y0)


def cash_flow_svg(flows, w=330, h=130):
    """Grouped income/expense bars per month, with a legend."""
    m = dict(t=8, r=4, b=16, l=34)
    vals = [f["income_cents"] for f in flows] + [f["expense_cents"] for f in flows]
    ticks = _ticks(0, max(vals + [0]))
    top = ticks[-1] or 1
    ph = h - m["t"] - m["b"]
    y = lambda v: m["t"] + ph * (1 - v / top)  # noqa: E731
    band = (w - m["l"] - m["r"]) / max(len(flows), 1)
    bw = min(16, band * 0.32)
    out = ['<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="Income and spending by month">' % (w, h)]
    for t in ticks:
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s" stroke-width=".6"/>' % (m["l"], w - m["r"], y(t), y(t), GRID))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="7.5" fill="%s">%s</text>' % (m["l"] - 3, y(t) + 2.2, INK2, _short(t)))
    for i, f in enumerate(flows):
        cx = m["l"] + band * i + band / 2
        for j, (val, color) in enumerate(((f["income_cents"], INCOME), (f["expense_cents"], EXPENSE))):
            x = cx - bw - 1 + j * (bw + 2)  # 2-unit gap between the pair
            if val > 0:
                out.append('<path d="%s" fill="%s"/>' % (_bar_path(x, y(0), y(val), bw), color))
        label = dt.date(*map(int, f["month"].split("-")), 1).strftime("%b")
        out.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="7.5" fill="%s">%s</text>' % (cx, h - 5, INK2, label))
    out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="#888" stroke-width=".8"/>' % (m["l"], w - m["r"], y(0), y(0)))
    out.append("</svg>")
    legend = ('<div class="legend"><span><i style="background:%s"></i>Income</span>'
              '<span><i style="background:%s"></i>Spending</span></div>' % (INCOME, EXPENSE))
    return legend + "".join(out)


def net_worth_svg(history, w=330, h=118):
    """Net worth line over time with start/end values labelled."""
    m = dict(t=10, r=8, b=16, l=40)
    vals = [p["net_worth_cents"] for p in history]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.1 or abs(hi) * 0.1 or 100
    ticks = _ticks(lo - pad, hi + pad)
    tlo, thi = ticks[0], ticks[-1]
    ph, pw = h - m["t"] - m["b"], w - m["l"] - m["r"]
    x = lambda i: m["l"] + pw * i / max(len(history) - 1, 1)  # noqa: E731
    y = lambda v: m["t"] + ph * (1 - (v - tlo) / ((thi - tlo) or 1))  # noqa: E731
    out = ['<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="Net worth over time">' % (w, h)]
    for t in ticks:
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s" stroke-width=".6"/>' % (m["l"], w - m["r"], y(t), y(t), GRID))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="7.5" fill="%s">%s</text>' % (m["l"] - 3, y(t) + 2.2, INK2, _short(t)))
    every = max(1, len(history) // 6)
    for i, p in enumerate(history):
        if i % every == 0 or i == len(history) - 1:
            label = dt.date(*map(int, p["month"].split("-")), 1).strftime("%b")
            out.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="7.5" fill="%s">%s</text>' % (x(i), h - 5, INK2, label))
    pts = " ".join("%.1f,%.1f" % (x(i), y(v)) for i, v in enumerate(vals))
    out.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>' % (pts, INCOME))
    xe, ye = x(len(vals) - 1), y(vals[-1])
    out.append('<circle cx="%.1f" cy="%.1f" r="2.6" fill="%s" stroke="#fff" stroke-width="1"/>' % (xe, ye, INCOME))
    out.append('<text x="%.1f" y="%.1f" text-anchor="end" font-size="7" font-weight="700" fill="#111">%s</text>'
               % (xe, ye - 5 if ye > m["t"] + 8 else ye + 10, _short(vals[-1])))
    out.append("</svg>")
    return "".join(out)


def _bar(percent, color=INCOME, over=False):
    pct = max(0.0, min(100.0, percent or 0))
    return '<div class="bar"><span style="width:%.1f%%;background:%s"></span></div>' % (
        pct, "#b42318" if over else color)


def render(conn, month=None, profile_name="", auto_print=False):
    month = month or month_of(today())
    start, end = month_bounds(month)
    nw = reports.net_worth(conn)
    summary = reports.month_summary(conn, month)
    flows = reports.cash_flow(conn, 6, month)
    spending = reports.spending_by_category(conn, start, end)
    budgets = [b for b in budget_status(conn, month) if b["budget_cents"]]
    goals = list_goals(conn)
    bills = upcoming(conn, 30)
    notes = reports.insights(conn, month)
    debts = [a for a in nw["accounts"] if a["type"] in LIABILITY_TYPES and a["balance_cents"] < 0]

    accounts_rows = [(escape(a["name"]), escape(TYPE_LABELS.get(a["type"], a["type"])),
                      _num(a["balance_cents"])) for a in nw["accounts"]]
    accounts_rows.append(("<b>Net worth</b>", "", ("r b", _m(nw["net_worth_cents"]))))

    history = reports.net_worth_history(conn, 12, month)
    top_spend = max([s["spent_cents"] for s in spending] or [1])
    spend_rows = [(escape(s["category_name"]), ("barcell", _bar(100 * s["spent_cents"] / top_spend, EXPENSE)),
                   _num(s["spent_cents"], True), ("r", "%.0f%%" % s["percent"]))
                  for s in spending[:7]]
    if len(spending) > 7:
        rest = sum(s["spent_cents"] for s in spending[7:])
        spend_rows.append(("Other (%d)" % (len(spending) - 7), ("barcell", _bar(100 * rest / top_spend, "#999")),
                           _num(rest, True), ("r", "%.0f%%" % sum(s["percent"] for s in spending[7:]))))
    budget_rows = [(escape(b["category_name"]),
                    ("barcell", _bar(b["percent_used"], over=b["spent_cents"] > b["budget_cents"])),
                    _num(b["spent_cents"], True), _num(b["budget_cents"], True), _num(b["remaining_cents"], True))
                   for b in budgets[:8]]
    goal_rows = [(escape(g["name"]), ("barcell", _bar(g["percent"])),
                  ("r", "%s / %s" % (_m(g["current_cents"], True), _m(g["target_cents"], True))),
                  escape(dt.date.fromisoformat(g["target_date"]).strftime("%b '%y") if g["target_date"] else ""),
                  _num(g["monthly_needed_cents"], True) if g["monthly_needed_cents"] else ("r", ""))
                 for g in goals[:5]]
    bill_rows = [(escape(b["date"][5:]), escape(b["name"]), _num(b["amount_cents"])) for b in bills[:8]]
    debt_rows = [(escape(d["name"]), _num(-d["balance_cents"]),
                  ("r", "%.1f%%" % d["interest_rate"] if d["interest_rate"] else "")) for d in debts]

    tiles = [("Net worth", _m(nw["net_worth_cents"], True)),
             ("Assets", _m(nw["assets_cents"], True)),
             ("Debts", _m(nw["liabilities_cents"], True)),
             ("Income (%s)" % _month_name(month)[:3], _m(summary["income_cents"], True)),
             ("Spending", _m(summary["expense_cents"], True)),
             ("Savings rate", "%.0f%%" % summary["savings_rate"] if summary["savings_rate"] is not None else "–")]

    title = "Financial Summary" + (" · " + profile_name if profile_name else "")
    return PAGE % {
        "title": escape(title),
        "heading": escape(title),
        "subtitle": "%s · prepared %s" % (escape(_month_name(month)), today().strftime("%B %d, %Y").replace(" 0", " ")),
        "tiles": "".join('<div class="tile"><div class="k">%s</div><div class="v">%s</div></div>'
                         % (escape(k), escape(v)) for k, v in tiles),
        "accounts": _table(["Account", "Type", ">Balance"], accounts_rows),
        "debts_block": ("<h2>Debts</h2>" + _table(["Debt", ">Owed", ">APR"], debt_rows)) if debts else "",
        "cashflow_chart": cash_flow_svg(flows),
        "networth_chart": net_worth_svg(history),
        "networth_change": _change(history),
        "spending": _table(["Category", "", ">Spent", ">Share"], spend_rows, "bars"),
        "budgets": _table(["Category", "", ">Spent", ">Budget", ">Left"], budget_rows, "bars budgets"),
        "goals": _table(["Goal", "", ">Saved / target", "By", ">/mo"], goal_rows, "bars goals"),
        "bills": _table(["Date", "Bill", ">Amount"], bill_rows),
        "notes": "".join("<li>%s</li>" % escape(n["message"]) for n in notes[:5]) or "<li>No notes.</li>",
        "script": "<script>window.addEventListener('load', () => setTimeout(() => window.print(), 300));</script>"
                  if auto_print else "",
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
  @page { size: auto; margin: 10mm; }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: #fff; color: #111; }
  body { font: 9pt/1.3 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
         max-width: 190mm; margin: 0 auto; padding: 8mm 0; }
  header { display: flex; justify-content: space-between; align-items: baseline;
           border-bottom: 2px solid #111; padding-bottom: 4px; margin-bottom: 8px; }
  h1 { font-size: 15pt; margin: 0; }
  h2 { font-size: 9.5pt; margin: 0 0 3px; text-transform: uppercase; letter-spacing: .04em; color: #333; }
  .sub { color: #555; }
  .tiles { display: grid; grid-template-columns: repeat(6, 1fr); gap: 6px; margin-bottom: 6px; }
  .tile { border: 1px solid #ccc; border-radius: 4px; padding: 4px 6px; }
  .tile .k { font-size: 7.5pt; color: #555; }
  .tile .v { font-size: 12pt; font-weight: 700; font-variant-numeric: tabular-nums; }
  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: 7px 14px; }
  section { break-inside: avoid; }
  table { width: 100%%; border-collapse: collapse; }
  th, td { padding: 1.5px 4px; border-bottom: 1px solid #e3e3e3; text-align: left; vertical-align: top; }
  th { font-size: 7.5pt; color: #555; font-weight: 600; border-bottom: 1px solid #999; }
  .r { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .neg { color: #b42318; }
  .b { font-weight: 700; }
  .muted { color: #777; }
  .pos { color: #1d7a3a; }
  h2.split { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  h2.split { white-space: nowrap; }
  h2 .note { text-transform: none; letter-spacing: 0; font-weight: 400; font-size: 8pt; color: #555; }
  td { white-space: nowrap; } td:first-child { white-space: normal; }
  table.bars.goals td.barcell, table.bars.budgets td.barcell { width: 24%%; }
  .legend { display: flex; gap: 12px; font-size: 7.5pt; color: #555; margin: 1px 0 2px; }
  .legend i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
  section.chart svg { display: block; }
  table.bars td.barcell { width: 32%%; padding-right: 8px; vertical-align: middle; }
  .bar { height: 7px; background: #eee; border-radius: 3.5px; overflow: hidden; }
  .bar span { display: block; height: 100%%; border-radius: 3.5px; min-width: 1px; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  ul { margin: 0; padding-left: 14px; }
  footer { margin-top: 8px; color: #777; font-size: 7pt; text-align: center; }
  .toolbar { position: fixed; top: 10px; right: 10px; }
  .toolbar button { font: inherit; font-size: 10pt; padding: 6px 12px; border-radius: 6px;
                    border: 1px solid #2a78d6; background: #2a78d6; color: #fff; cursor: pointer; }
  @media print { .toolbar { display: none; } body { padding: 0; } }
  @media screen and (max-width: 700px) {
    body { padding: 12px 16px; } .tiles { grid-template-columns: repeat(3, 1fr); } .cols { grid-template-columns: 1fr; }
  }
</style></head>
<body>
<div class="toolbar"><button onclick="window.print()">Print / Save as PDF</button></div>
<header><h1>%(heading)s</h1><div class="sub">%(subtitle)s</div></header>
<div class="tiles">%(tiles)s</div>
<div class="cols">
  <section class="chart"><h2>Income vs spending, last 6 months</h2>%(cashflow_chart)s</section>
  <section class="chart"><h2 class="split">Net worth, 12 months <span class="note">%(networth_change)s</span></h2>%(networth_chart)s</section>
  <section><h2>Accounts</h2>%(accounts)s</section>
  <section><h2>Spending this month</h2>%(spending)s</section>
  <section><h2>Budget vs actual</h2>%(budgets)s</section>
  <section><h2>Savings goals</h2>%(goals)s
    <h2 style="margin-top:8px">Notes</h2><ul>%(notes)s</ul></section>
  <section><h2>Upcoming bills (30 days)</h2>%(bills)s</section>
  <section>%(debts_block)s</section>
</div>
<footer>Generated by FinOrganizer</footer>
%(script)s
</body></html>
"""
