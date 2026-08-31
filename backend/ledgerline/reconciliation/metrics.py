"""Headline metrics, computed from reconciled events only.

Two rules that are easy to get wrong and expensive to get wrong:

1.  Cash is a *stock*, not a flow.  Take it from the latest balance each bank
    reports, never by summing transactions -- one missed row and the number
    drifts forever.
2.  Transfers are excluded from everything.  Moving your own money is not
    revenue and not burn.

Every metric returns the event keys behind it so the dashboard can offer
drill-down and the analyst can cite sources instead of asserting numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from .types import EconomicEvent, EventKind, RawTxn


@dataclass(frozen=True)
class Balance:
    account_id: str
    as_of: datetime
    amount: Decimal
    currency: str = "USD"


@dataclass
class Metric:
    name: str
    value: Decimal
    currency: str = "USD"
    # Provenance: what this number is made of.  Never render a metric without it.
    event_keys: tuple[str, ...] = ()
    note: str = ""
    # Lowest confidence of any contributing event -- surface this in the UI.
    confidence: float = 1.0
    unreconciled_warning: str | None = None


COUNTED_OUTFLOWS = (EventKind.SPEND, EventKind.PAYROLL, EventKind.FEE)


def _window(events: Sequence[EconomicEvent], start: datetime, end: datetime):
    return [e for e in events if start <= e.occurred_at < end]


def cash_on_hand(
    balances: Sequence[Balance],
    cash_account_ids: Sequence[str],
    reporting_currency: str = "USD",
) -> Metric:
    """Latest reported balance across cash accounts, in one currency only.

    Multi-currency is deliberately not supported yet -- it needs an FX rate
    table and a decision about which date's rate is authoritative.  Until then
    this *excludes* foreign-currency accounts and says so, rather than adding
    EUR to USD and labelling the result USD.  Not-done has to mean refuse, not
    quietly produce a wrong number.
    """
    latest: dict[str, Balance] = {}
    for b in balances:
        if b.account_id not in cash_account_ids:
            continue
        if b.account_id not in latest or b.as_of > latest[b.account_id].as_of:
            latest[b.account_id] = b

    counted = {k: v for k, v in latest.items() if v.currency == reporting_currency}
    excluded = sorted(k for k, v in latest.items() if v.currency != reporting_currency)
    total = sum((b.amount for b in counted.values()), Decimal(0))

    warning = None
    if excluded:
        other = ", ".join(sorted({latest[k].currency for k in excluded}))
        warning = (
            f"{len(excluded)} account(s) held in {other} are excluded from this "
            f"figure -- no FX conversion, so this is {reporting_currency} only"
        )

    return Metric(
        name="cash_on_hand",
        value=total,
        currency=reporting_currency,
        note=(f"latest reported balance across {len(counted)} "
              f"{reporting_currency} cash accounts"),
        unreconciled_warning=warning,
    )


def revenue(events: Sequence[EconomicEvent], start: datetime, end: datetime) -> Metric:
    rows = [e for e in _window(events, start, end) if e.kind is EventKind.REVENUE]
    return Metric(
        name="revenue",
        value=sum((e.amount for e in rows), Decimal(0)),
        event_keys=tuple(e.key for e in rows),
        confidence=min((e.confidence for e in rows), default=1.0),
        note=f"{len(rows)} reconciled revenue events; transfers excluded",
    )


def burn(events: Sequence[EconomicEvent], start: datetime, end: datetime) -> Metric:
    rows = [e for e in _window(events, start, end) if e.kind in COUNTED_OUTFLOWS]
    return Metric(
        name="burn",
        value=-sum((e.amount for e in rows), Decimal(0)),   # positive number
        event_keys=tuple(e.key for e in rows),
        confidence=min((e.confidence for e in rows), default=1.0),
        note=f"{len(rows)} outflow events; internal transfers excluded",
    )


def net_burn(events: Sequence[EconomicEvent], start: datetime, end: datetime) -> Metric:
    b, r = burn(events, start, end), revenue(events, start, end)
    return Metric(
        name="net_burn",
        value=b.value - r.value,
        event_keys=b.event_keys + r.event_keys,
        confidence=min(b.confidence, r.confidence),
        note="outflows minus revenue over the period",
    )


def runway_months(
    events: Sequence[EconomicEvent],
    cash: Metric,
    as_of: datetime,
    lookback_months: int = 3,
) -> Metric:
    start = as_of - timedelta(days=30 * lookback_months)
    nb = net_burn(events, start, as_of)
    monthly = nb.value / Decimal(lookback_months)
    if monthly <= 0:
        return Metric(
            name="runway_months",
            value=Decimal("Infinity"),
            note="cash-flow positive over the trailing period",
            confidence=nb.confidence,
        )
    return Metric(
        name="runway_months",
        value=(cash.value / monthly).quantize(Decimal("0.1")),
        event_keys=nb.event_keys,
        confidence=nb.confidence,
        note=f"cash / trailing {lookback_months}-month average net burn of {monthly:.0f}",
    )


def apply_exception_warnings(metrics: Sequence[Metric], exceptions: Sequence) -> None:
    """Attach a visible warning when blocking exceptions exist.

    A number that might be wrong should say so on the dashboard.  Silence is
    how trust gets lost the first time someone checks against their bank.
    """
    blocking = [x for x in exceptions if getattr(x, "severity", "") == "blocking"]
    if not blocking:
        return
    reasons = ", ".join(sorted({x.reason for x in blocking}))
    for m in metrics:
        m.unreconciled_warning = (
            f"{len(blocking)} unreconciled item(s) may affect this figure ({reasons})"
        )


# ---------------------------------------------------------------------------
# For comparison in tests and demos: what the dashboard would show without
# reconciliation.  This is the bug, written down.
# ---------------------------------------------------------------------------

def naive_revenue(txns: Sequence[RawTxn], start: datetime, end: datetime) -> Decimal:
    return sum((t.amount for t in txns
                if t.amount > 0 and start <= t.occurred_at < end), Decimal(0))


def naive_burn(txns: Sequence[RawTxn], start: datetime, end: datetime) -> Decimal:
    return -sum((t.amount for t in txns
                 if t.amount < 0 and start <= t.occurred_at < end), Decimal(0))
