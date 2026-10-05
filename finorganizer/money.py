"""Helpers for converting between user-facing amounts and integer cents, and date math."""

import calendar
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def to_cents(value):
    """Parse ``value`` (str/int/float/Decimal) into integer cents.

    Accepts strings like "1,234.56", "$12", "(45.00)" (accounting negative).
    """
    if value is None or value == "":
        raise ValueError("amount is required")
    if isinstance(value, bool):
        raise ValueError("invalid amount")
    if isinstance(value, int):
        return value * 100
    s = str(value).strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = s.replace(",", "").replace("$", "").replace(" ", "")
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError("invalid amount: %r" % value)
    cents = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return -cents if negative else cents


def fmt(cents, symbol="$"):
    """Format cents as a currency string, e.g. -123456 -> '-$1,234.56'."""
    sign = "-" if cents < 0 else ""
    return "%s%s%s" % (sign, symbol, "{:,.2f}".format(abs(cents) / 100))


def parse_date(value):
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value).strip())


def today():
    return dt.date.today()


def month_of(date):
    return parse_date(date).strftime("%Y-%m")


def parse_month(month):
    """Validate a YYYY-MM string and return (year, month)."""
    y, m = str(month).split("-")
    y, m = int(y), int(m)
    if not 1 <= m <= 12:
        raise ValueError("invalid month: %r" % month)
    return y, m


def month_bounds(month):
    """Return (first_day, last_day) ISO strings for a YYYY-MM month."""
    y, m = parse_month(month)
    last = calendar.monthrange(y, m)[1]
    return "%04d-%02d-01" % (y, m), "%04d-%02d-%02d" % (y, m, last)


def add_months(date, n):
    date = parse_date(date)
    total = date.month - 1 + n
    y, m = date.year + total // 12, total % 12 + 1
    d = min(date.day, calendar.monthrange(y, m)[1])
    return dt.date(y, m, d)


def shift_month(month, n):
    y, m = parse_month(month)
    return add_months(dt.date(y, m, 1), n).strftime("%Y-%m")


def months_between(start_month, end_month):
    """Inclusive list of YYYY-MM from start to end."""
    out, cur = [], start_month
    while cur <= end_month:
        out.append(cur)
        cur = shift_month(cur, 1)
    return out


def next_occurrence(date, frequency, anchor_day=None):
    """The following due date. ``anchor_day`` is the intended day of the month, so a
    bill due on the 31st falls on Feb 28 and then returns to Mar 31 instead of drifting."""
    date = parse_date(date)
    if frequency == "weekly":
        return date + dt.timedelta(days=7)
    if frequency == "biweekly":
        return date + dt.timedelta(days=14)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}.get(frequency)
    if months is None:
        raise ValueError("unknown frequency: %r" % frequency)
    nxt = add_months(date, months)
    if anchor_day:
        nxt = nxt.replace(day=min(int(anchor_day), calendar.monthrange(nxt.year, nxt.month)[1]))
    return nxt


# Approximate occurrences per month, used for normalising recurring items.
PER_MONTH = {
    "weekly": 52 / 12,
    "biweekly": 26 / 12,
    "monthly": 1,
    "quarterly": 1 / 3,
    "yearly": 1 / 12,
}
