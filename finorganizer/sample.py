"""Generate realistic demo data so the app can be explored without real accounts."""

import datetime as dt
import random

from . import ledger as L
from .money import add_months, month_of, today


def _cat(conn, name):
    return L.find_category(conn, name)["id"]


def load_sample_data(conn, months=6, seed=42):
    if conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
        raise ValueError("database already has accounts; sample data is only for an empty database")
    rnd = random.Random(seed)
    end = today()
    start = add_months(end.replace(day=1), -(months - 1))

    checking = L.add_account(conn, "Everyday Checking", "checking", 2_450_00, "First Bank")
    savings = L.add_account(conn, "High-Yield Savings", "savings", 8_000_00, "First Bank", 4.2)
    card = L.add_account(conn, "Rewards Visa", "credit_card", -640_00, "CardCo", 22.9)
    L.add_account(conn, "401(k)", "retirement", 42_300_00, "Fidelity")
    L.add_account(conn, "Car Loan", "loan", -11_800_00, "Auto Credit", 6.4)

    def tx(acct, date, amount, payee, cat):
        L.add_transaction(conn, acct, date, amount, payee, _cat(conn, cat), commit=False)

    d = start
    while d <= end:
        m_first = d
        for day in (1, 15):
            pd = m_first.replace(day=day)
            if pd <= end:
                tx(checking, pd, 2_850_00, "Acme Corp Payroll", "Salary")
        fixed = [(1, -1_650_00, "Parkview Apartments", "Rent / Mortgage", checking),
                 (5, -rnd.randint(90_00, 140_00), "City Power & Water", "Utilities", checking),
                 (8, -75_00, "FiberNet", "Phone & Internet", checking),
                 (12, -128_00, "SafeDrive Insurance", "Insurance", checking),
                 (18, -15_99, "StreamFlix", "Subscriptions", card),
                 (20, -10_99, "MusicBox", "Subscriptions", card),
                 (3, -45_00, "Iron Gym", "Fitness", card)]
        for day, amt, payee, cat, acct in fixed:
            pd = m_first.replace(day=day)
            if pd <= end:
                tx(acct, pd, amt, payee, cat)
        variable = [("Groceries", ("FreshMart", "Green Grocer", "Costless"), 9, (45_00, 160_00)),
                    ("Dining Out", ("Taco Town", "Bella Pasta", "Corner Cafe", "Sushi Go"), 7, (12_00, 75_00)),
                    ("Fuel", ("Shell", "Chevron"), 4, (35_00, 60_00)),
                    ("Shopping", ("Amazon", "Target", "Bookshop"), 3, (15_00, 140_00)),
                    ("Entertainment", ("Cinema 9", "Concert Hall"), 2, (15_00, 90_00)),
                    ("Personal Care", ("Style Salon", "Pharmacy Plus"), 1, (20_00, 60_00))]
        days_in_month = (add_months(m_first, 1) - m_first).days
        for cat, payees, count, (lo, hi) in variable:
            for _ in range(count):
                pd = m_first + dt.timedelta(days=rnd.randrange(days_in_month))
                if pd <= end:
                    tx(card if rnd.random() < 0.6 else checking, pd, -rnd.randint(lo, hi),
                       rnd.choice(payees), cat)
        conn.commit()
        pay = m_first.replace(day=25)
        if pay <= end:
            L.add_transfer(conn, checking, card, pay, 1_250_00, "Card payment")
            L.add_transfer(conn, checking, savings, pay, 500_00, "Monthly savings")
            L.add_transfer(conn, checking, conn.execute(
                "SELECT id FROM accounts WHERE name = 'Car Loan'").fetchone()[0], pay, 310_00, "Car payment")
        d = add_months(d, 1)

    for name, amt in [("Groceries", 700_00), ("Dining Out", 300_00), ("Fuel", 200_00),
                      ("Shopping", 250_00), ("Entertainment", 120_00), ("Utilities", 140_00),
                      ("Rent / Mortgage", 1_650_00), ("Subscriptions", 30_00), ("Personal Care", 60_00)]:
        for k in range(3):
            L.set_budget(conn, _cat(conn, name), month_of(add_months(end, -k)), amt)

    L.add_goal(conn, "Emergency Fund", 20_000_00, add_months(end, 18).isoformat(), savings)
    L.add_goal(conn, "Japan Trip", 5_000_00, add_months(end, 10).isoformat(), saved_cents=1_200_00)
    L.add_goal(conn, "New Laptop", 1_800_00, add_months(end, 4).isoformat(), saved_cents=400_00)

    def nxt_day(day):
        d = end.replace(day=day)
        return d if d > end else add_months(d, 1)

    nxt = nxt_day(1)
    L.add_recurring(conn, "Rent", checking, -1_650_00, "monthly", nxt, _cat(conn, "Rent / Mortgage"),
                    "Parkview Apartments")
    L.add_recurring(conn, "Internet", checking, -75_00, "monthly", nxt_day(8),
                    _cat(conn, "Phone & Internet"), "FiberNet")
    L.add_recurring(conn, "StreamFlix", card, -15_99, "monthly", nxt_day(18),
                    _cat(conn, "Subscriptions"))
    L.add_recurring(conn, "Car insurance", checking, -128_00, "monthly", nxt_day(12),
                    _cat(conn, "Insurance"), "SafeDrive Insurance")
    L.add_recurring(conn, "Domain renewal", card, -18_00, "yearly", add_months(end, 2),
                    _cat(conn, "Subscriptions"), "Registrar")
