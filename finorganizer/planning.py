"""Financial planning calculators. Pure functions: cents in, cents out.

Rates are annual percentages (e.g. 6.5 means 6.5% APR).
"""

import math

MAX_MONTHS = 1200  # 100 years: guard against plans that never finish


def loan_payment(principal_cents, annual_rate, months):
    """Fixed monthly payment that repays ``principal`` over ``months``."""
    if months <= 0:
        raise ValueError("months must be positive")
    r = annual_rate / 100 / 12
    if r == 0:
        return math.ceil(principal_cents / months)
    return math.ceil(principal_cents * r / (1 - (1 + r) ** -months))


def amortization(principal_cents, annual_rate, months, extra_cents=0):
    """Month-by-month schedule for a fixed-rate loan, with optional extra payment."""
    payment = loan_payment(principal_cents, annual_rate, months)
    r = annual_rate / 100 / 12
    balance, schedule, total_interest = principal_cents, [], 0
    while balance > 0 and len(schedule) < MAX_MONTHS:
        interest = round(balance * r)
        principal = min(balance, payment + extra_cents - interest)
        if principal <= 0:
            raise ValueError("payment does not cover interest")
        balance -= principal
        total_interest += interest
        schedule.append({"month": len(schedule) + 1, "payment_cents": principal + interest,
                         "principal_cents": principal, "interest_cents": interest,
                         "balance_cents": balance})
    baseline_interest = payment * months - principal_cents
    return {
        "payment_cents": payment,
        "months": len(schedule),
        "total_interest_cents": total_interest,
        "total_paid_cents": principal_cents + total_interest,
        "interest_saved_cents": max(0, baseline_interest - total_interest) if extra_cents else 0,
        "schedule": schedule,
    }


def debt_payoff(debts, monthly_budget_cents, strategy="avalanche"):
    """Simulate paying off several debts with a fixed total monthly budget.

    ``debts``: list of dicts with name, balance_cents, rate (APR %), min_payment_cents.
    ``strategy``: "avalanche" (highest rate first, least interest) or
    "snowball" (smallest balance first, quickest wins).
    Any money left after minimums goes to the target debt; freed-up minimums roll over.
    """
    if strategy not in ("avalanche", "snowball"):
        raise ValueError("strategy must be avalanche or snowball")
    state = [{"name": d["name"], "balance": int(d["balance_cents"]), "rate": float(d["rate"]),
              "min": int(d["min_payment_cents"]), "interest": 0, "paid_off_month": None}
             for d in debts if int(d["balance_cents"]) > 0]
    minimums = sum(d["min"] for d in state)
    if monthly_budget_cents < minimums:
        raise ValueError("monthly budget is below the total minimum payments (%d cents)" % minimums)

    def order_key(d):
        return (-d["rate"], d["balance"]) if strategy == "avalanche" else (d["balance"], -d["rate"])

    month, timeline = 0, []
    while any(d["balance"] > 0 for d in state):
        month += 1
        if month > MAX_MONTHS:
            raise ValueError("debts are not paid off within 100 years at this budget")
        for d in state:
            if d["balance"] > 0:
                i = round(d["balance"] * d["rate"] / 100 / 12)
                d["balance"] += i
                d["interest"] += i
        budget = monthly_budget_cents
        active = [d for d in state if d["balance"] > 0]
        for d in active:
            pay = min(d["min"], d["balance"], budget)
            d["balance"] -= pay
            budget -= pay
        for d in sorted(active, key=order_key):
            if budget <= 0:
                break
            pay = min(budget, d["balance"])
            d["balance"] -= pay
            budget -= pay
        for d in state:
            if d["balance"] == 0 and d["paid_off_month"] is None:
                d["paid_off_month"] = month
        timeline.append({"month": month, "total_balance_cents": sum(d["balance"] for d in state)})

    return {
        "strategy": strategy,
        "months": month,
        "total_interest_cents": sum(d["interest"] for d in state),
        "payoff_order": [{"name": d["name"], "paid_off_month": d["paid_off_month"],
                          "interest_cents": d["interest"]}
                         for d in sorted(state, key=lambda d: d["paid_off_month"])],
        "timeline": timeline,
    }


def compound_growth(principal_cents, monthly_contribution_cents, annual_return, years,
                    inflation=0.0, contribution_growth=0.0):
    """Project an investment balance year by year (monthly compounding).

    ``contribution_growth`` raises the monthly contribution by that % each year.
    ``inflation`` is used to report balances in today's money.
    """
    r = annual_return / 100 / 12
    balance, contributed = float(principal_cents), float(principal_cents)
    contribution = float(monthly_contribution_cents)
    out = []
    for year in range(1, int(years) + 1):
        for _ in range(12):
            balance = balance * (1 + r) + contribution
            contributed += contribution
        real = balance / (1 + inflation / 100) ** year
        out.append({"year": year, "balance_cents": round(balance),
                    "contributed_cents": round(contributed),
                    "growth_cents": round(balance - contributed),
                    "real_balance_cents": round(real)})
        contribution *= 1 + contribution_growth / 100
    return out


def retirement_plan(current_age, retirement_age, current_savings_cents, monthly_contribution_cents,
                    annual_return=7.0, inflation=2.5, desired_annual_income_cents=0,
                    withdrawal_rate=4.0, contribution_growth=0.0):
    """Will savings support the desired income? Income is in today's money."""
    years = int(retirement_age) - int(current_age)
    if years <= 0:
        raise ValueError("retirement age must be after current age")
    projection = compound_growth(current_savings_cents, monthly_contribution_cents, annual_return,
                                 years, inflation, contribution_growth)
    final = projection[-1]
    target_real = round(desired_annual_income_cents * 100 / withdrawal_rate) if withdrawal_rate else 0
    sustainable_income_real = round(final["real_balance_cents"] * withdrawal_rate / 100)
    shortfall = max(0, target_real - final["real_balance_cents"])
    extra_monthly = 0
    if shortfall:
        # Extra monthly saving needed (nominal), solved from the annuity formula.
        r = annual_return / 100 / 12
        n = years * 12
        shortfall_nominal = shortfall * (1 + inflation / 100) ** years
        factor = n if r == 0 else ((1 + r) ** n - 1) / r
        extra_monthly = math.ceil(shortfall_nominal / factor)
    return {
        "years_to_retirement": years,
        "projected_balance_cents": final["balance_cents"],
        "projected_real_balance_cents": final["real_balance_cents"],
        "target_nest_egg_cents": target_real,
        "sustainable_annual_income_cents": sustainable_income_real,
        "on_track": shortfall == 0,
        "shortfall_cents": shortfall,
        "extra_monthly_needed_cents": extra_monthly,
        "projection": projection,
    }


def savings_plan(target_cents, current_cents, months, annual_rate=0.0):
    """Monthly deposit required to reach ``target`` in ``months``."""
    if months <= 0:
        raise ValueError("months must be positive")
    r = annual_rate / 100 / 12
    fv_current = current_cents * (1 + r) ** months
    remaining = max(0.0, target_cents - fv_current)
    factor = months if r == 0 else ((1 + r) ** months - 1) / r
    monthly = math.ceil(remaining / factor)
    return {"monthly_cents": monthly, "months": months,
            "total_deposits_cents": monthly * months,
            "interest_earned_cents": max(0, round(target_cents - current_cents - monthly * months))
            if remaining else round(fv_current - current_cents)}


def months_to_goal(target_cents, current_cents, monthly_cents, annual_rate=0.0):
    """How many months of ``monthly`` deposits until ``target`` is reached (None if never)."""
    r = annual_rate / 100 / 12
    balance, months = float(current_cents), 0
    while balance < target_cents:
        if months >= MAX_MONTHS or (monthly_cents <= 0 and r == 0):
            return None
        balance = balance * (1 + r) + monthly_cents
        months += 1
    return months


def emergency_fund(monthly_expenses_cents, liquid_savings_cents, months_target=6):
    target = monthly_expenses_cents * months_target
    covered = liquid_savings_cents / monthly_expenses_cents if monthly_expenses_cents else None
    return {"target_cents": target, "current_cents": liquid_savings_cents,
            "gap_cents": max(0, target - liquid_savings_cents),
            "months_covered": round(covered, 1) if covered is not None else None}


def budget_rule(monthly_income_cents, needs=50, wants=30, savings=20):
    """Split take-home income using the 50/30/20 rule (or custom percentages)."""
    if round(needs + wants + savings) != 100:
        raise ValueError("percentages must add up to 100")
    return {"needs_cents": round(monthly_income_cents * needs / 100),
            "wants_cents": round(monthly_income_cents * wants / 100),
            "savings_cents": round(monthly_income_cents * savings / 100)}
