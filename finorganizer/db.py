"""SQLite storage: connection handling and schema.

All money is stored as integer cents. Transaction amounts are signed from the
account's point of view: positive = money in, negative = money out. Dates are
ISO-8601 strings (YYYY-MM-DD), months are YYYY-MM.
"""

import os
import sqlite3
import sys

def _default_db_path():
    if os.environ.get("FINORGANIZER_DB"):
        return os.environ["FINORGANIZER_DB"]
    if os.name == "nt" and os.environ.get("APPDATA"):
        # Windows: keep data in %APPDATA%\FinOrganizer, next to other per-user app data.
        folder = os.path.join(os.environ["APPDATA"], "FinOrganizer")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "finorganizer.db")
    legacy = os.path.join(os.path.expanduser("~"), ".finorganizer.db")
    if sys.platform == "darwin" and not os.path.exists(legacy):
        # macOS: the standard per-user app data folder (an existing ~/.finorganizer.db keeps working).
        folder = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "FinOrganizer")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "finorganizer.db")
    return legacy


DEFAULT_DB_PATH = _default_db_path()

ACCOUNT_TYPES = (
    "checking",
    "savings",
    "credit_card",
    "cash",
    "investment",
    "retirement",
    "loan",
    "mortgage",
    "property",
    "other",
)
# Account types whose balance is normally negative (money owed).
LIABILITY_TYPES = ("credit_card", "loan", "mortgage")

CATEGORY_KINDS = ("income", "expense", "transfer")

FREQUENCIES = ("weekly", "biweekly", "monthly", "quarterly", "yearly")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    institution TEXT NOT NULL DEFAULT '',
    opening_balance_cents INTEGER NOT NULL DEFAULT 0,
    interest_rate REAL NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'expense',
    group_name TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    payee TEXT NOT NULL DEFAULT '',
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    memo TEXT NOT NULL DEFAULT '',
    transfer_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    cleared INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category_id);

CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    month TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    UNIQUE (category_id, month)
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    target_cents INTEGER NOT NULL,
    target_date TEXT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    saved_cents INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recurring (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    payee TEXT NOT NULL DEFAULT '',
    frequency TEXT NOT NULL,
    next_date TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Linked bank connections (SimpleFIN). access_url embeds read-only credentials.
CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'simplefin',
    label TEXT NOT NULL DEFAULT '',
    access_url TEXT NOT NULL,
    last_sync TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Accounts reported by a connection, and which local account (if any) they feed.
CREATE TABLE IF NOT EXISTS remote_accounts (
    id INTEGER PRIMARY KEY,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    remote_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    institution TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'USD',
    balance_cents INTEGER,
    balance_date TEXT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    ignored INTEGER NOT NULL DEFAULT 0,
    UNIQUE (connection_id, remote_id)
);
"""

# Columns added after the first release; applied to existing databases on open.
MIGRATIONS = [
    ("transactions", "external_id", "ALTER TABLE transactions ADD COLUMN external_id TEXT"),
]

DEFAULT_CATEGORIES = [
    ("Salary", "income", "Income"),
    ("Bonus", "income", "Income"),
    ("Interest & Dividends", "income", "Income"),
    ("Other Income", "income", "Income"),
    ("Rent / Mortgage", "expense", "Housing"),
    ("Utilities", "expense", "Housing"),
    ("Home Maintenance", "expense", "Housing"),
    ("Groceries", "expense", "Food"),
    ("Dining Out", "expense", "Food"),
    ("Fuel", "expense", "Transportation"),
    ("Public Transit", "expense", "Transportation"),
    ("Car Maintenance", "expense", "Transportation"),
    ("Insurance", "expense", "Insurance"),
    ("Healthcare", "expense", "Health"),
    ("Fitness", "expense", "Health"),
    ("Phone & Internet", "expense", "Bills"),
    ("Subscriptions", "expense", "Bills"),
    ("Shopping", "expense", "Lifestyle"),
    ("Entertainment", "expense", "Lifestyle"),
    ("Travel", "expense", "Lifestyle"),
    ("Gifts & Donations", "expense", "Giving"),
    ("Education", "expense", "Personal"),
    ("Personal Care", "expense", "Personal"),
    ("Taxes", "expense", "Taxes"),
    ("Fees & Charges", "expense", "Other"),
    ("Miscellaneous", "expense", "Other"),
    ("Transfer", "transfer", "Transfers"),
]


def connect(path=None):
    """Open (and initialise if needed) the database at ``path``."""
    path = path or DEFAULT_DB_PATH
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    for table, column, sql in MIGRATIONS:
        if column not in {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}:
            conn.execute(sql)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_external"
                 " ON transactions(account_id, external_id) WHERE external_id IS NOT NULL")
    if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO categories (name, kind, group_name) VALUES (?, ?, ?)",
            DEFAULT_CATEGORIES,
        )
    conn.commit()


def rows(cursor):
    return [dict(r) for r in cursor.fetchall()]


def row(cursor):
    r = cursor.fetchone()
    return dict(r) if r else None


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                 " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()
