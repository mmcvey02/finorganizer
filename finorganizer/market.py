"""Market prices: current quotes and one year of daily closes, from Yahoo Finance.

Uses Yahoo's public chart endpoint, which needs no account or API key. It is
unofficial, so every caller must cope with it failing: prices are a bonus on top of
what you've recorded, never something the rest of the app depends on.

Results are cached in memory for a few minutes so moving between pages doesn't
download the same data again.
"""

import datetime as dt
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .net import describe_ssl_error, ssl_context

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=1y&interval=1d&includePrePost=false"
TIMEOUT = 15
CACHE_SECONDS = 15 * 60
# Yahoo refuses requests without a browser-like User-Agent.
USER_AGENT = "Mozilla/5.0 (FinOrganizer personal finance app)"

# What "general market info" shows: broad, well-known yardsticks.
MARKET_SYMBOLS = [
    ("^GSPC", "S&P 500", "500 large US companies"),
    ("^IXIC", "Nasdaq Composite", "US stocks, tech-heavy"),
    ("^DJI", "Dow Jones Industrial Average", "30 large US companies"),
    ("^RUT", "Russell 2000", "US small companies"),
    ("VXUS", "International stocks", "Vanguard Total International Stock ETF"),
    ("AGG", "US bonds", "iShares Core US Aggregate Bond ETF"),
    ("^TNX", "10-year Treasury yield", "interest rate, in percent"),
    ("^VIX", "VIX volatility index", "expected stock market swings; above 30 is turbulent"),
    ("GC=F", "Gold", "per ounce, futures"),
]
BENCHMARK = "^GSPC"


class MarketError(Exception):
    pass


_cache = {}
_lock = threading.Lock()


def available():
    """False where we can't make web requests (the iPhone version runs inside a web page)."""
    return sys.platform != "emscripten"


def _download(symbol):
    url = CHART_URL % urllib.parse.quote(symbol, safe="")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl_context()) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise MarketError("%s: no such symbol" % symbol)
        if e.code == 429:
            raise MarketError("the price service is busy; try again in a few minutes")
        raise MarketError("the price service returned HTTP %d" % e.code)
    except urllib.error.URLError as e:
        raise MarketError("couldn't reach the price service: %s" % describe_ssl_error(e))
    except (OSError, ValueError) as e:
        raise MarketError("couldn't read prices (%s)" % e)


def parse_chart(symbol, payload):
    """Turn Yahoo's chart JSON into {symbol, name, type, currency, price, previous_close, history}.

    ``history`` is [(iso_date, close)] oldest first, with gaps (holidays, bad points) dropped.
    """
    chart = (payload or {}).get("chart") or {}
    results = chart.get("result") or []
    if not results:
        err = chart.get("error") or {}
        raise MarketError("%s: %s" % (symbol, err.get("description") or "no price data"))
    r = results[0]
    meta = r.get("meta") or {}
    closes = (((r.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
    history = []
    for ts, close in zip(r.get("timestamp") or [], closes):
        if close is None or not isinstance(ts, (int, float)):
            continue
        offset = meta.get("gmtoffset") or 0
        day = dt.datetime.fromtimestamp(ts + offset, dt.timezone.utc).date().isoformat()
        if history and history[-1][0] == day:
            history[-1] = (day, float(close))
        else:
            history.append((day, float(close)))
    price = meta.get("regularMarketPrice")
    if price is None and history:
        price = history[-1][1]
    if price is None:
        raise MarketError("%s: no price data" % symbol)
    previous = meta.get("previousClose")
    if previous is None:
        previous = history[-2][1] if len(history) >= 2 else meta.get("chartPreviousClose")
    as_of = None
    if isinstance(meta.get("regularMarketTime"), (int, float)):
        as_of = dt.datetime.fromtimestamp(meta["regularMarketTime"] + (meta.get("gmtoffset") or 0),
                                          dt.timezone.utc).date().isoformat()
    return {
        "symbol": symbol.upper(),
        "name": meta.get("longName") or meta.get("shortName") or "",
        "type": (meta.get("instrumentType") or "").upper(),
        "currency": meta.get("currency") or "USD",
        "price": float(price),
        "previous_close": float(previous) if previous is not None else None,
        "as_of": as_of or (history[-1][0] if history else None),
        "history": history,
    }


def chart(symbol, fetch=None):
    """One symbol's quote and past year of closes (cached). Raises MarketError."""
    symbol = symbol.strip().upper()
    now = time.time()
    with _lock:
        hit = _cache.get(symbol)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    if fetch is None and not available():
        raise MarketError("live prices aren't available in this version of FinOrganizer")
    data = parse_chart(symbol, (fetch or _download)(symbol))
    with _lock:
        _cache[symbol] = (now, data)
    return data


def charts(symbols, fetch=None):
    """Look up several symbols at once. Returns ({symbol: data}, {symbol: error message})."""
    symbols = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    found, errors = {}, {}
    if not symbols:
        return found, errors

    def one(sym):
        try:
            return sym, chart(sym, fetch), None
        except MarketError as e:
            return sym, None, str(e)

    with ThreadPoolExecutor(max_workers=min(6, len(symbols))) as pool:
        for sym, data, err in pool.map(one, symbols):
            if data:
                found[sym] = data
            else:
                errors[sym] = err
    return found, errors


def cached(symbols, max_age=3600):
    """Already-downloaded data for these symbols (no network), up to ``max_age`` seconds old."""
    now, out = time.time(), {}
    with _lock:
        for s in symbols:
            hit = _cache.get(s.strip().upper())
            if hit and now - hit[0] < max_age:
                out[s.strip().upper()] = hit[1]
    return out


def clear_cache():
    with _lock:
        _cache.clear()


# ------------------------------------------------------------------ summaries

def _close_on_or_before(history, day):
    best = None
    for d, close in history:
        if d <= day:
            best = close
        else:
            break
    return best


def _change(now, then):
    if now is None or not then:
        return None
    return round(100.0 * (now - then) / then, 2)


def changes(data, today=None):
    """Percent change over 1 day, 1 month, year to date and 1 year."""
    today = today or dt.date.today()
    h, price = data["history"], data["price"]
    if not h:
        return {"day": _change(price, data.get("previous_close")), "month": None, "ytd": None, "year": None}
    month_ago = (today - dt.timedelta(days=30)).isoformat()
    year_start = dt.date(today.year - 1, 12, 31).isoformat()
    first = h[0][1] if h[0][0] <= (today - dt.timedelta(days=360)).isoformat() else None
    return {
        "day": _change(price, data.get("previous_close")),
        "month": _change(price, _close_on_or_before(h, month_ago)),
        "ytd": _change(price, _close_on_or_before(h, year_start)),
        "year": _change(price, first),
    }


def overview(fetch=None, today=None):
    """The market yardsticks in MARKET_SYMBOLS with their recent moves."""
    found, errors = charts([s for s, _, _ in MARKET_SYMBOLS], fetch)
    items = []
    for sym, label, about in MARKET_SYMBOLS:
        d = found.get(sym)
        if not d:
            continue
        item = {"symbol": sym, "label": label, "about": about, "price": d["price"], "as_of": d["as_of"],
                "is_rate": sym == "^TNX", "spark": [round(c, 4) for _, c in d["history"][-66:]]}
        item.update(changes(d, today))
        if sym == "^TNX":  # a yield: show the move in percentage points, not percent of a percent
            item["day"] = (round(d["price"] - d["previous_close"], 2)
                           if d.get("previous_close") is not None else None)
            day0 = today or dt.date.today()
            then = _close_on_or_before(d["history"], (day0 - dt.timedelta(days=30)).isoformat())
            item["month"] = round(d["price"] - then, 2) if then is not None else None
            h = d["history"]
            year_ago = h[0][1] if h and h[0][0] <= (day0 - dt.timedelta(days=360)).isoformat() else None
            item["year"] = round(d["price"] - year_ago, 2) if year_ago is not None else None
            then = _close_on_or_before(d["history"], dt.date((today or dt.date.today()).year - 1, 12, 31).isoformat())
            item["ytd"] = round(d["price"] - then, 2) if then is not None else None
        items.append(item)
    return {"items": items, "errors": sorted(set(errors.values()))[:3],
            "notes": market_notes(items)}


def market_notes(items):
    """A few plain-English observations about what the market is doing."""
    by = {i["symbol"]: i for i in items}
    notes = []
    sp = by.get("^GSPC")
    if sp and sp.get("ytd") is not None:
        notes.append("The S&P 500 is %s %.1f%% so far this year." % ("up" if sp["ytd"] >= 0 else "down", abs(sp["ytd"])))
    if sp and sp.get("month") is not None and sp["month"] <= -10:
        notes.append("US stocks have fallen more than 10%% in the past month (a \"correction\"). "
                     "Drops like this are normal; selling after one locks in the loss.")
    vix = by.get("^VIX")
    if vix:
        if vix["price"] >= 30:
            notes.append("Volatility is high (VIX %.0f): expect bigger daily swings than usual." % vix["price"])
        elif vix["price"] < 15:
            notes.append("Markets are calm right now (VIX %.0f)." % vix["price"])
    tnx = by.get("^TNX")
    if tnx:
        notes.append("The 10-year Treasury yields %.2f%%; bonds and high-yield savings pay more when this is higher."
                     % tnx["price"])
    intl = by.get("VXUS")
    if sp and intl and sp.get("year") is not None and intl.get("year") is not None:
        lead = "US" if sp["year"] >= intl["year"] else "International"
        notes.append("%s stocks have done better over the past year (S&P 500 %+.1f%%, international %+.1f%%)."
                     % (lead, sp["year"], intl["year"]))
    return notes
