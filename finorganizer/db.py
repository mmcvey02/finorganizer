"""SQLite storage: connection handling and schema.

All money is stored as integer cents. Transaction amounts are signed from the
account's point of view: positive = money in, negative = money out. Dates are
ISO-8601 strings (YYYY-MM-DD), months are YYYY-MM.
"""

import os
import sqlite3

DEFAULT_DB_PATH = os.environ.get(
    "FINORGANIZER_DB", os.path.join(os.path.expanduser("~"), ".finorganizer.db")
)

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
"""

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
