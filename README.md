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

## Windows app

GitHub Actions builds the Windows programs and the Mac app on every push (`.github/workflows/build.yml`).
Neither needs Python installed.

| Program | What it is |
|---|---|
| **`FinOrganizer.exe`** | The desktop app. Opens in its own window, with no browser and no console. **Closing the window quits FinOrganizer completely.** |
| `FinOrganizer-cli.exe` | Command-line tool, e.g. `FinOrganizer-cli.exe report summary`. Run with no arguments, it opens the app in your web browser instead (keep its console window open while you use it). |

- **Download:** get `FinOrganizer.exe` (and optionally `FinOrganizer-cli.exe`) from the
  [latest release](https://github.com/mmcvey02/finorganizer/releases/latest). Every build of the
  main branch is published there automatically as version 1.2.*build number*.
- **Updates:** the app checks for a newer release when it opens (switch this off under
  ⚙ Settings) and shows a banner. **Update now** downloads the new version, verifies it against
  GitHub's checksum, swaps it in and restarts. Your data, profiles and bank connections are
  untouched. You can also check any time from ⚙ Settings.
- **Requirements:** the desktop window uses Microsoft Edge WebView2, which is built into
  Windows 10 and 11. If it's missing, FinOrganizer says so; install the WebView2 Runtime from
  Microsoft or use `FinOrganizer-cli.exe`.
- **Your data** lives in `%APPDATA%\FinOrganizer\finorganizer.db` and is shared by both programs.
  Copy that file to back it up.
- Windows SmartScreen may warn about an unrecognized app because the programs aren't
  code-signed. Choose "More info" then "Run anyway".

To build them yourself on a Windows machine:

```bash
pip install pyinstaller pywebview truststore certifi
python packaging/build_exe.py      # output: dist\FinOrganizer.exe and dist\FinOrganizer-cli.exe
```

Running from source, the desktop window is `pip install pywebview` then
`python -m finorganizer.desktop` (or `pip install .[desktop]` and run `finorganizer-desktop`).

## Mac app

The same desktop app is built for macOS as a disk image, in two versions:

| File | For |
|---|---|
| `FinOrganizer-mac-apple-silicon.dmg` | Macs with an M1, M2, M3, M4 or later chip (Apple menu → About This Mac shows "Chip: Apple M…") |
| `FinOrganizer-mac-intel.dmg` | Older Macs with an Intel processor |

- **Install:** download the right `.dmg` from the
  [latest release](https://github.com/mmcvey02/finorganizer/releases/latest), open it, and drag
  **FinOrganizer** onto **Applications**.
- **First launch:** the app isn't notarized by Apple (that requires a paid Apple Developer
  account), so macOS blocks it the first time. Open it once, click **Done** on the warning, then go
  to **System Settings → Privacy & Security**, scroll down and click **Open Anyway** next to the
  FinOrganizer message. After that it opens normally.
- **Closing the window** (or Cmd+Q) quits FinOrganizer completely.
- **Your data** lives in `~/Library/Application Support/FinOrganizer/`. Copy that folder to back it up.
- **Updates:** when a new version is out, click **Update now** (banner or ⚙ Settings). The app
  downloads the right `.dmg`, verifies its checksum and code signature, replaces itself in
  Applications and restarts; no "Open Anyway" needed again. Your data is kept. This requires
  FinOrganizer to be in a folder you can write to (Applications is fine) and opened from there,
  not straight from the disk image.

To build it yourself on a Mac: `pip install pyinstaller pywebview truststore certifi`,
`python packaging/build_exe.py`, then `packaging/make_dmg.sh dist/FinOrganizer.dmg`.

## iPhone app

FinOrganizer also runs on iPhone (iOS 16.4 or later recommended) as a home-screen web app:

1. On the iPhone, open **https://mmcvey02.github.io/finorganizer/** in Safari.
2. Tap **Share** → **Add to Home Screen** → **Add**.
3. Open FinOrganizer from its icon. It runs full screen, works offline, and updates itself
   when a new version is published.

It's the same program as the desktop app (Python running inside the page via
[Pyodide](https://pyodide.org)), with these differences:

- **Your data stays on the phone**, in Safari's storage for the app. Nothing is sent online,
  and it isn't shared with your computer automatically.
- **No bank connections**: banks' servers don't accept requests from web pages. Import a CSV
  from your bank's site or app instead (Transactions → Import CSV), or keep banks linked in the
  desktop app and move a backup over.
- **Moving data:** ⚙ Settings → *Your data* → **Back up now** saves the current profile as one
  file (on iPhone through the share sheet, e.g. *Save to Files*). **Restore from a backup** loads
  such a file on any device, so you can move data from the computer to the phone and back.
- Back up now and then: deleting the home-screen app or clearing Safari's website data deletes
  the data stored in it.

To build it yourself: `npm pack pyodide@314.0.7 && tar xzf pyodide-314.0.7.tgz`, then
`python packaging/build_web.py --pyodide-dir package` and serve `dist/web/` over HTTPS.

## Profiles, printing and bank connections

**Profiles** keep separate finances for different people (a partner, a parent, a child), each
in its own data file with its own accounts, budgets, goals and bank connections. Switch or create
profiles from the menu at the top right of the app; "Manage profiles…" renames or deletes them.
Your original data is the "Default" profile. Extra profiles are stored next to it, in
`%APPDATA%\FinOrganizer\finorganizer-profiles\` on Windows or `~/.finorganizer-profiles/` elsewhere.
The app reopens the last profile you used.

**Printable summary.** The **Print** button opens a one-page summary of the current profile
(net worth, an income-vs-spending chart, a net-worth trend chart, accounts, spending by category,
budget vs actual, goals, upcoming bills, debts and notes) and opens the print dialog. Choose
"Save as PDF" to keep a copy. It's laid out to fit one Letter or A4 page.

**Linked banks (automatic updates)** use [SimpleFIN Bridge](https://beta-bridge.simplefin.org),
a low-cost, subscription-based, read-only service that connects to thousands of US banks:

1. Sign up at SimpleFIN Bridge and connect your banks there.
2. Create a *setup token* ("New connection").
3. In FinOrganizer go to **Accounts → Connect bank** and paste the token.
4. That's it: connecting downloads right away, and each bank account is linked to an existing
   account with the same name or gets a new one, so its transactions appear on the
   **Transactions** page (tagged "bank"). You can change where any bank account goes, or choose
   **Don't import**, on the Accounts page.
5. New data arrives whenever you click **Sync banks** (Transactions page) or **Sync now**
   (Accounts page). The app also syncs by itself when opened and every few hours while open.

Syncing downloads posted transactions (pending ones are picked up once they post), never
creates duplicates, matches transactions you already typed in by hand, auto-categorizes payees
you've categorized before, and shows any difference between the bank's balance and the app's,
with a one-click **Match bank** fix. Access is read-only: FinOrganizer can never move money.
The connection's access key is stored in your profile's data file, so protect that file like
your other financial records; removing the connection in the app (or revoking it on the
SimpleFIN site) cuts off access.

## Features

**Tracking**
- Accounts of every kind: checking, savings, cash, credit cards, loans, mortgages, investments,
  retirement and property, with balances, interest rates and archiving.
- Transactions with payees, categories, memos and a cleared flag; search and filter by account,
  category, date range or uncategorized.
- Automatic categorization (on by default; ⚙ Settings → Categories turns it off per profile):
  new transactions get a category from your own history and common merchants, and every new
  transaction or category you choose re-checks the ones still uncategorized. Guesses are marked
  "auto"; anything unrecognized stays uncategorized.
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
finorganizer profile create "Jamie"          # new profile (and switch to it)
finorganizer profile use Default             # switch back; or add --profile NAME to any command
finorganizer summary --open                  # one-page printable summary
finorganizer bank connect <SETUP_TOKEN>      # link banks via SimpleFIN
finorganizer bank link 1 --new               # or --account "Everyday Checking" / --ignore
finorganizer bank sync
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
  profiles.py  separate profiles (one database file each)
  banksync.py  SimpleFIN bank connections and syncing
  summary_page.py  one-page printable summary with charts
  desktop.py   desktop app: native window via pywebview; closing it quits
  updater.py   checks GitHub Releases and installs updates in the Windows app
  backup.py    one-file backups of a profile, and restoring them
  webapp.py    entry point for the iPhone / web version (runs on Pyodide)
  server.py    local JSON API + static file server (standard library only)
  cli.py       command-line interface
  sample.py    demo data generator
  static/      single-page web UI (HTML/CSS/vanilla JS)
web/           iPhone / web version: in-page bridge, service worker (built by packaging/build_web.py)
tests/         unit and API tests: python -m unittest discover -s tests
```

The web server binds to `127.0.0.1` by default and has no login, so it's meant for use on your
own computer. Don't expose it to a network you don't trust.
