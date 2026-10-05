# FinOrganizer

A personal finance app for tracking all your money in one place, budgeting, and planning ahead.
It runs locally, stores everything in a single SQLite file on your machine, and needs nothing
beyond Python 3.9+ (no third-party packages).

You can use it two ways:

- **Web app**: a dashboard in your browser with charts, forms and reports.
- **Command line**: every feature is scriptable from the terminal.

## Quick start

```bash
# try it with 6 months of realistic sample data (uses a separate demo database)
python -m finorganizer --db demo.db demo
python -m finorganizer --db demo.db serve        # open http://127.0.0.1:8765

# or start with your own data (stored in ~/.finorganizer.db by default)
python -m finorganizer serve
```

Optionally install it to get a `finorganizer` command: `pip install .`

Set `FINORGANIZER_DB=/path/to/file.db` (or pass `--db`) to choose where data is stored.
Back up your data by copying that file.

## Windows app (FinOrganizer.exe)

A standalone `FinOrganizer.exe` is built automatically on every push by GitHub Actions
(`.github/workflows/windows-exe.yml`). No Python install is needed to run it.

- **Download:** open the repository's **Actions** tab, pick the latest "Build Windows executable"
  run, and download the `FinOrganizer-windows` artifact (a zip containing the .exe). Pushing a tag
  like `v1.0.0` also attaches the .exe to a GitHub Release.
- **Run:** double-click `FinOrganizer.exe`. A console window opens and your browser opens the app.
  Keep the window open while you use it; close it to quit.
- **Your data** lives in `%APPDATA%\FinOrganizer\finorganizer.db`. Copy that file to back it up.
- **Command line:** the same .exe accepts every CLI command, e.g.
  `FinOrganizer.exe report summary` or `FinOrganizer.exe --db demo.db demo`.
- Windows SmartScreen may warn about an unrecognized app because the .exe isn't code-signed.
  Choose "More info" then "Run anyway".

To build it yourself on a Windows machine:

```bash
pip install pyinstaller
python packaging/build_exe.py      # output: dist\FinOrganizer.exe
```

## Features

**Tracking**
- Accounts of every kind: checking, savings, cash, credit cards, loans, mortgages, investments,
  retirement and property, with balances, interest rates and archiving.
- Transactions with payees, categories, memos and a cleared flag; search and filter by account,
  category, date range or uncategorized.
- Transfers between accounts (card payments, savings deposits, loan payments) are recorded as
  linked pairs and never counted as income or spending.
- Bank CSV import that detects common column layouts (amount, or debit/credit columns), supports
  day-first dates and sign flipping, skips duplicates, and auto-categorizes payees you've seen before.
- CSV export.

**Budgeting and expenses**
- Monthly budgets per category with spent / remaining / progress, last month's spending for
  reference, and one-click "copy last month".
- Recurring bills, subscriptions and paychecks (weekly to yearly): a 60-day calendar, monthly and
  yearly totals, and one click to record everything that's due.
- Savings goals tracked manually or linked to an account's balance, with the monthly amount needed
  to hit the target date.

**Reports and insights**
- Dashboard: net worth, monthly income, spending, savings rate, cash-flow chart, spending by
  category, budgets, goals, upcoming bills.
- Automatic insights: savings rate, spending spikes, overspent budgets, emergency fund coverage,
  high-interest debt, uncategorized transactions.
- Cash flow by month, spending by category for any period, top payees, net worth over time,
  and a year-in-review.

**Planning calculators**
- Debt payoff: avalanche vs snowball comparison, prefilled from your debt accounts.
- Loan calculator: payment, amortization, and the effect of extra payments.
- Retirement projection: inflation-adjusted, with the extra monthly saving needed to reach your
  target income.
- Savings goal: monthly deposit needed, including interest.
- Emergency fund check from your actual spending, plus a 50/30/20 budget split from your income.

## Command line examples

```bash
finorganizer account add "Everyday Checking" --type checking --balance 2450
finorganizer account add "Visa" --type credit_card --balance -640 --rate 22.9
finorganizer tx add checking 54.20 "FreshMart" -e -c Groceries     # -e = money out
finorganizer tx add checking 2850 "Payroll" -c Salary
finorganizer transfer checking visa 300 --memo "Card payment"
finorganizer tx list --month 2026-10
finorganizer budget set Groceries 700
finorganizer budget show
finorganizer goal add "Emergency Fund" 20000 --date 2027-12-31 --account savings
finorganizer recurring add Rent checking 1650 -e -f monthly --next 2026-11-01 -c "Rent / Mortgage"
finorganizer recurring upcoming
finorganizer report summary          # this month + insights
finorganizer report cashflow --months 12
finorganizer report year --year 2026
finorganizer plan debt 600           # avalanche vs snowball for your debt accounts
finorganizer plan loan 25000 6.5 60 --extra 100 --schedule
finorganizer plan retirement --age 35 --savings 40000 --monthly 800 --income 60000
finorganizer import checking statement.csv
finorganizer export --file transactions.csv
```

Accounts can be referred to by id, full name, or any unique part of the name.
Run `finorganizer <command> --help` for all options.

## How amounts work

Amounts are signed from the account's point of view: positive is money in, negative is money out.
Debts (credit cards, loans) have negative balances, so net worth is the sum of all balances.
Money is stored as integer cents to avoid rounding errors.

## Project layout

```
finorganizer/
  db.py        SQLite schema and default categories
  money.py     amount parsing/formatting and date helpers
  ledger.py    accounts, categories, transactions, transfers, budgets, goals, recurring items
  reports.py   summaries, cash flow, net worth, insights, dashboard
  planning.py  loan, debt payoff, retirement, savings and emergency-fund calculators
  csvio.py     CSV import/export
  server.py    local JSON API + static file server (standard library only)
  cli.py       command-line interface
  sample.py    demo data generator
  static/      single-page web UI (HTML/CSS/vanilla JS)
tests/         unit and API tests: python -m unittest discover -s tests
```

The web server binds to `127.0.0.1` by default and has no login, so it's meant for use on your
own computer. Don't expose it to a network you don't trust.
