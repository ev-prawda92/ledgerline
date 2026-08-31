"""Run the dashboard scenario and show what reconciliation changes.

    python demo.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from test_reconcile import ACCOUNTS, CASH_ACCOUNTS, scenario  # noqa: E402

from ledgerline.reconciliation import reconcile  # noqa: E402
from ledgerline.reconciliation.metrics import (  # noqa: E402
    Balance,
    burn,
    cash_on_hand,
    naive_burn,
    naive_revenue,
    revenue,
    runway_months,
)

UTC = UTC
JUL, AUG = datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)


def money(x: Decimal) -> str:
    return f"${x:,.0f}"


txns = scenario()
res = reconcile(txns, ACCOUNTS)
balances = [Balance("acc_mercury", datetime(2026, 7, 31, tzinfo=UTC), Decimal("4200000"))]
cash = cash_on_hand(balances, CASH_ACCOUNTS)

print(f"\n{len(txns)} raw records from 5 sources -> {len(res.events)} economic events\n")

print("WITHOUT RECONCILIATION (just add up what the sources say)")
print(f"  Revenue   {money(naive_revenue(txns, JUL, AUG)):>12}")
print(f"  Burn      {money(naive_burn(txns, JUL, AUG)):>12}")

rev, brn = revenue(res.events, JUL, AUG), burn(res.events, JUL, AUG)
run = runway_months(res.events, cash, AUG, lookback_months=1)
print("\nWITH RECONCILIATION")
print(f"  Revenue   {money(rev.value):>12}   {rev.note}")
print(f"  Burn      {money(brn.value):>12}   {brn.note}")
print(f"  Cash      {money(cash.value):>12}   {cash.note}")
print(f"  Runway    {str(run.value) + ' mo':>12}   {run.note}")

print("\nWHAT WAS COLLAPSED")
for e in res.events:
    if len(e.member_txn_ids) > 1 or e.suppressed_txn_ids:
        rules = ", ".join(sorted({ev.rule.value for ev in e.evidence}))
        print(f"  {e.kind.value:<9} {money(e.amount):>12}  {rules}")
        for ev in e.evidence:
            print(f"{'':>14} - {ev.note} (confidence {ev.confidence})")

print("\nWHAT IT REFUSED TO GUESS ABOUT")
for x in res.exceptions:
    print(f"  [{x.severity}] {x.reason}: {x.detail}")

print(f"\nrun fingerprint {res.input_fingerprint[:16]}...  (same inputs always "
      f"produce this)\n")
