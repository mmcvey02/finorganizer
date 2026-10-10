"""Automatic transaction categorization.

For a transaction without a category we try, in order:

1. Your own history: the category you've used most for the same merchant. Bank
   descriptions are normalized first, so "SQ *BLUE BOTTLE COFFEE #123 SAN FRANCISCO CA"
   and "Blue Bottle Coffee" count as the same merchant.
2. Built-in rules: well-known merchants and keywords (Kroger -> Groceries,
   Netflix -> Subscriptions, Payroll -> Salary, ...). Rules know whether money is
   coming in or going out, so a refund from a store isn't treated as spending.
3. Otherwise the transaction stays uncategorized.

Guesses are flagged (``auto_category = 1``) so the app can show them and so they
never "teach" the history in step 1; only categories you chose (or confirmed by
changing them) do. Categories that no longer exist are simply skipped.

Automatic categorization can be turned off per profile (Settings). While it's on,
every change that brings in new information (new transactions, a category you
chose) re-checks all transactions that are still uncategorized, since a match that
wasn't possible before may be now.
"""

import re
from collections import Counter

from .db import row, rows

# Words that say how a payment was made rather than who it was paid to.
NOISE = {
    "pos", "debit", "credit", "card", "purchase", "purchases", "recurring", "ach", "checkcard",
    "check", "visa", "mastercard", "mc", "sq", "tst", "pp", "paypal", "ppd", "web", "id", "ref",
    "online", "pmt", "payment", "payments", "transaction", "txn", "dbt", "crd", "withdrawal",
    "pending", "the", "inc", "llc", "ltd", "co", "corp", "com", "www", "store", "us", "usa",
    "authorized", "on", "at", "to", "from", "for", "via", "preauth", "electronic", "eft",
}
# Two-letter US state codes often trail merchant names ("... SAN FRANCISCO CA").
STATES = set("al ak az ar ca co ct de fl ga hi ia id il in ks ky la ma md me mi mn mo ms mt nc nd ne "
             "nh nj nm nv ny oh ok or pa ri sc sd tn tx ut va vt wa wi wv wy dc".split())


def normalize_payee(text):
    """A stable key for a merchant, ignoring card/terminal noise, numbers and locations."""
    words = re.sub(r"[^a-z&]+", " ", (text or "").lower()).split()
    keep = [w for w in words if w not in NOISE and len(w) > 1]
    # Drop a trailing state code (and the city before it would be too risky to guess).
    while keep and keep[-1] in STATES and len(keep) > 1:
        keep.pop()
    return " ".join(keep[:3])


# (category name, direction, patterns). direction: "out" = money leaving, "in" = arriving,
# "any" = either. Patterns are matched as whole words/phrases, case-insensitively.
RULES = [
    ("Salary", "in", ["payroll", "salary", "direct dep", "direct deposit", "paycheck", "wages", "adp", "gusto", "paychex"]),
    ("Interest & Dividends", "in", ["interest paid", "interest earned", "interest payment", "dividend", "div", "int pymt"]),
    ("Bonus", "in", ["bonus"]),
    ("Rent / Mortgage", "out", ["rent", "mortgage", "apartments", "apartment", "property management", "property mgmt", "leasing", "hoa", "home loan"]),
    ("Utilities", "out", ["electric", "electricity", "power", "energy", "water", "sewer", "utility", "utilities", "pg&e", "pge", "con ed", "coned", "duke energy", "dominion", "edison", "national grid", "xcel", "gas co", "natural gas", "trash", "waste management"]),
    ("Phone & Internet", "out", ["verizon", "at&t", "att", "t mobile", "tmobile", "comcast", "xfinity", "spectrum", "cox", "centurylink", "frontier", "optimum", "mint mobile", "cricket", "boost mobile", "google fi", "starlink", "internet", "wireless"]),
    ("Subscriptions", "out", ["netflix", "spotify", "hulu", "disney", "disney plus", "hbo", "hbo max", "paramount", "peacock", "youtube premium", "youtube", "apple com bill", "itunes", "icloud", "prime video", "amazon prime", "audible", "adobe", "microsoft", "dropbox", "patreon", "substack", "nytimes", "new york times", "wsj", "chatgpt", "openai", "anthropic", "claude", "github", "subscription"]),
    ("Groceries", "out", ["grocery", "groceries", "supermarket", "kroger", "safeway", "whole foods", "wholefds", "trader joe", "trader joes", "aldi", "publix", "wegmans", "heb", "h e b", "albertsons", "food lion", "giant", "stop & shop", "meijer", "sprouts", "harris teeter", "winco", "ralphs", "vons", "fred meyer", "hy vee", "food", "market", "freshmart"]),
    ("Dining Out", "out", ["restaurant", "cafe", "coffee", "starbucks", "dunkin", "mcdonald", "mcdonalds", "burger", "chipotle", "subway", "taco", "pizza", "domino", "dominos", "sushi", "grill", "bistro", "kitchen", "diner", "bakery", "doordash", "uber eats", "ubereats", "grubhub", "postmates", "wendy", "wendys", "chick fil", "kfc", "panera", "shake shack", "five guys", "popeyes", "sonic", "bar", "pub", "brewery", "eatery"]),
    ("Fuel", "out", ["shell", "chevron", "exxon", "exxonmobil", "mobil", "bp", "texaco", "arco", "sunoco", "valero", "citgo", "marathon", "speedway", "phillips", "conoco", "circle k", "quiktrip", "qt", "racetrac", "wawa", "sheetz", "fuel", "gas station", "gasoline", "petro"]),
    ("Public Transit", "out", ["uber", "lyft", "metro", "transit", "mta", "bart", "septa", "amtrak", "caltrain", "clipper", "ventra", "train", "bus", "parking", "toll", "ez pass", "ezpass", "fastrak"]),
    ("Car Maintenance", "out", ["auto repair", "autozone", "advance auto", "o reilly", "oreilly", "jiffy lube", "valvoline", "midas", "pep boys", "firestone", "goodyear", "discount tire", "car wash", "tire", "tires", "dmv", "mechanic"]),
    ("Insurance", "out", ["insurance", "geico", "progressive", "state farm", "allstate", "liberty mutual", "farmers", "nationwide", "usaa", "travelers", "lemonade", "metlife", "aflac"]),
    ("Healthcare", "out", ["pharmacy", "cvs", "walgreens", "rite aid", "medical", "clinic", "hospital", "doctor", "dental", "dentist", "orthodont", "optometr", "vision", "urgent care", "labcorp", "quest diagnostics", "kaiser", "health", "therapy"]),
    ("Fitness", "out", ["gym", "fitness", "planet fitness", "la fitness", "equinox", "orangetheory", "crossfit", "peloton", "yoga", "pilates", "ymca", "climbing"]),
    ("Shopping", "out", ["amazon", "amzn", "target", "walmart", "wal mart", "best buy", "ebay", "etsy", "ikea", "costco", "sams club", "kohls", "macys", "nordstrom", "tj maxx", "marshalls", "ross", "old navy", "gap", "zara", "h&m", "uniqlo", "nike", "apple store", "shein", "temu", "wayfair", "bookshop", "barnes"]),
    ("Home Maintenance", "out", ["home depot", "lowes", "lowe s", "menards", "ace hardware", "hardware", "plumbing", "plumber", "electrician", "hvac", "pest control", "lawn", "landscaping", "cleaning service"]),
    ("Entertainment", "out", ["cinema", "cinemas", "movie", "movies", "theater", "theatre", "amc", "regal", "fandango", "ticketmaster", "stubhub", "live nation", "concert", "steam", "playstation", "xbox", "nintendo", "twitch", "bowling", "museum", "zoo"]),
    ("Travel", "out", ["airline", "airlines", "airways", "delta", "united", "american airlines", "southwest", "jetblue", "alaska air", "spirit airlines", "frontier airlines", "hotel", "motel", "marriott", "hilton", "hyatt", "airbnb", "vrbo", "expedia", "booking com", "hotels com", "kayak", "priceline", "hertz", "avis", "enterprise rent", "budget rent"]),
    ("Education", "out", ["tuition", "university", "college", "school", "coursera", "udemy", "skillshare", "masterclass", "textbook", "student loan"]),
    ("Personal Care", "out", ["salon", "barber", "barbershop", "spa", "nails", "massage", "ulta", "sephora", "cosmetics"]),
    ("Gifts & Donations", "out", ["donation", "donate", "charity", "gofundme", "red cross", "unicef", "church", "tithe", "foundation"]),
    ("Taxes", "out", ["irs", "tax", "taxes", "franchise tax", "dept of revenue", "treasury"]),
    ("Fees & Charges", "out", ["fee", "fees", "overdraft", "service charge", "interest charge", "late charge", "finance charge", "atm fee", "foreign transaction"]),
]


def _compile(patterns):
    alts = "|".join(re.escape(p).replace(r"\ ", r"[\s\W]*") for p in sorted(patterns, key=len, reverse=True))
    return re.compile(r"(?<![a-z])(?:%s)(?![a-z])" % alts)


_COMPILED = [(name, direction, _compile(pats)) for name, direction, pats in RULES]


class Categorizer:
    """Built once per batch (import, sync), then asked for many guesses."""

    MIN_SHARE = 0.6   # history must point at one category at least this often

    def __init__(self, conn):
        self.categories = {r["name"].lower(): r["id"] for r in rows(conn.execute(
            "SELECT id, name FROM categories WHERE kind != 'transfer'"))}
        self.kinds = {r["id"]: r["kind"] for r in rows(conn.execute("SELECT id, kind FROM categories"))}
        counts = {}
        for r in conn.execute(
                "SELECT t.payee, t.category_id FROM transactions t JOIN categories c ON c.id = t.category_id"
                " WHERE t.transfer_id IS NULL AND c.kind != 'transfer' AND COALESCE(t.auto_category, 0) = 0"
                " AND t.payee != ''"):
            key = normalize_payee(r[0])
            if key:
                counts.setdefault(key, Counter())[r[1]] += 1
        self.history = {}
        for key, c in counts.items():
            cat, n = c.most_common(1)[0]
            if n / sum(c.values()) >= self.MIN_SHARE:
                self.history[key] = cat

    def _fits(self, category_id, amount_cents):
        kind = self.kinds.get(category_id)
        if kind == "income":
            return amount_cents > 0
        if kind == "expense":
            return amount_cents < 0 or amount_cents == 0
        return False

    def guess(self, payee, memo="", amount_cents=0):
        """Best category id for this transaction, or None if there's no confident match."""
        key = normalize_payee(payee)
        if key:
            hit = self.history.get(key)
            if hit is None:  # same merchant with a longer/shorter description
                for k, cat in self.history.items():
                    if k.startswith(key + " ") or key.startswith(k + " "):
                        hit = cat
                        break
            if hit is not None and self._fits(hit, amount_cents):
                return hit
        text = " ".join(re.sub(r"[^a-z&]+", " ", (s or "").lower()) for s in (payee, memo))
        for name, direction, pattern in _COMPILED:
            if direction == "in" and amount_cents <= 0 or direction == "out" and amount_cents > 0:
                continue
            if pattern.search(text):
                cat = self.categories.get(name.lower())
                if cat is not None:
                    return cat
        return None


def categorize_uncategorized(conn, categorizer=None):
    """Fill in a category for every uncategorized, non-transfer transaction we can."""
    cz = categorizer or Categorizer(conn)
    changed = 0
    for t in rows(conn.execute("SELECT id, payee, memo, amount_cents FROM transactions"
                               " WHERE category_id IS NULL AND transfer_id IS NULL")):
        cat = cz.guess(t["payee"], t["memo"], t["amount_cents"])
        if cat is not None:
            conn.execute("UPDATE transactions SET category_id = ?, auto_category = 1 WHERE id = ?", (cat, t["id"]))
            changed += 1
    conn.commit()
    return changed


SETTING = "auto_categorize"


def enabled(conn):
    """Whether automatic categorization is on for this profile (it is unless turned off)."""
    r = row(conn.execute("SELECT value FROM settings WHERE key = ?", (SETTING,)))
    return r is None or r["value"] != "0"


def set_enabled(conn, on):
    """Turn automatic categorization on or off. Turning it on sorts what's waiting;
    returns how many transactions that categorized."""
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                 " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (SETTING, "1" if on else "0"))
    conn.commit()
    return recheck(conn)


def recheck(conn):
    """Look again at every uncategorized transaction, if automatic categorization is on.

    Called whenever new transactions arrive or you choose a category: either can make
    a match possible that wasn't before. Returns how many were categorized.
    """
    return categorize_uncategorized(conn) if enabled(conn) else 0


def apply_to_similar(conn, tx_id):
    """After you categorize a transaction, give the same category to other uncategorized
    (or auto-guessed) transactions from the same merchant. Returns how many changed."""
    t = row(conn.execute("SELECT payee, category_id, amount_cents FROM transactions WHERE id = ?", (tx_id,)))
    if not t or not t["category_id"]:
        return 0
    key = normalize_payee(t["payee"])
    if not key:
        return 0
    kind = row(conn.execute("SELECT kind FROM categories WHERE id = ?", (t["category_id"],)))
    if not kind or kind["kind"] == "transfer":
        return 0
    same_sign = "amount_cents > 0" if t["amount_cents"] > 0 else "amount_cents <= 0"
    changed = 0
    for o in rows(conn.execute(
            "SELECT id, payee FROM transactions WHERE id != ? AND transfer_id IS NULL AND " + same_sign +
            " AND (category_id IS NULL OR auto_category = 1) AND payee != ''", (tx_id,))):
        if normalize_payee(o["payee"]) == key:
            conn.execute("UPDATE transactions SET category_id = ?, auto_category = 1 WHERE id = ?",
                         (t["category_id"], o["id"]))
            changed += 1
    conn.commit()
    return changed
