"""The dashboard scenario, end to end.

These tests encode the failure the whole layer exists to prevent: six sources
reporting overlapping views of the same money, and a dashboard that adds them
all up.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ledgerline.reconciliation import Account, Config, EventKind, RawTxn, SourceRole, reconcile
from ledgerline.reconciliation.metrics import (
    Balance,
    apply_exception_warnings,
    burn,
    cash_on_hand,
    naive_burn,
    naive_revenue,
    revenue,
    runway_months,
)

UTC = timezone.utc
JUL = datetime(2026, 7, 1, tzinfo=UTC)
AUG = datetime(2026, 8, 1, tzinfo=UTC)
SEP = datetime(2026, 9, 1, tzinfo=UTC)


def d(day: int, month: int = 7) -> datetime:
    return datetime(2026, month, day, 12, 0, tzinfo=UTC)


ACCOUNTS = [
    Account("acc_mercury", "mercury", SourceRole.BANK, "Mercury operating"),
    Account("acc_stripe", "stripe", SourceRole.PROCESSOR, "Stripe balance"),
    Account("acc_ramp", "ramp", SourceRole.CARD, "Ramp card"),
    Account("acc_gusto", "gusto", SourceRole.PAYROLL, "Gusto payroll"),
    Account("acc_qb", "quickbooks", SourceRole.LEDGER, "QuickBooks GL"),
]
CASH_ACCOUNTS = ["acc_mercury"]


def scenario() -> list[RawTxn]:
    """One month of overlapping reality."""
    txns: list[RawTxn] = []

    # --- Stripe collects 85,000 across three charges, batched into one payout
    for n, amount in enumerate([Decimal("40000"), Decimal("30000"), Decimal("15000")], 1):
        txns.append(RawTxn(
            id=f"t_charge_{n}", source="stripe", source_id=f"ch_{n}",
            account_id="acc_stripe", amount=amount, currency="USD",
            occurred_at=d(10), description=f"charge {n}",
            counterparty=f"Customer {n}", batch_id="po_1abc",
        ))
    # The payout itself: money leaving Stripe, net of 2,600 in fees
    txns.append(RawTxn(
        id="t_payout", source="stripe", source_id="po_1abc",
        account_id="acc_stripe", amount=Decimal("-82400"), currency="USD",
        occurred_at=d(12), description="payout to Mercury",
    ))
    # ...and arriving in the bank. Same money, second time it has been reported.
    txns.append(RawTxn(
        id="t_deposit", source="mercury", source_id="mtx_9001",
        account_id="acc_mercury", amount=Decimal("82400"), currency="USD",
        occurred_at=d(12), description="STRIPE TRANSFER",
        counterparty="Stripe", external_refs={"stripe_payout_id": "po_1abc"},
    ))
    # ...and booked in the GL. Third time.
    txns.append(RawTxn(
        id="t_qb_rev", source="quickbooks", source_id="qb_501",
        account_id="acc_qb", amount=Decimal("40000"), currency="USD",
        occurred_at=d(13), description="Sales income",
        counterparty="Customer 1",
    ))

    # --- Payroll: Gusto has the detail, Mercury has the line, GL has the entry
    txns.append(RawTxn(
        id="t_gusto", source="gusto", source_id="pr_88",
        account_id="acc_gusto", amount=Decimal("-185000"), currency="USD",
        occurred_at=d(18), description="Semi-monthly run, 47 people",
        counterparty="Gusto", category_hint="payroll",
    ))
    txns.append(RawTxn(
        id="t_mercury_payroll", source="mercury", source_id="mtx_9002",
        account_id="acc_mercury", amount=Decimal("-185000"), currency="USD",
        occurred_at=d(18), description="GUSTO PAYROLL", counterparty="Gusto",
    ))

    # --- AWS on the Ramp card, reported twice by a re-sync, plus a GL echo
    for dup, tid in enumerate(["t_ramp_aws", "t_ramp_aws_resync"]):
        txns.append(RawTxn(
            id=tid, source="ramp", source_id="ramp_771",
            account_id="acc_ramp", amount=Decimal("-18400"), currency="USD",
            occurred_at=d(20), description="AWS", counterparty="AWS",
        ))
    txns.append(RawTxn(
        id="t_qb_aws", source="quickbooks", source_id="qb_502",
        account_id="acc_qb", amount=Decimal("-18400"), currency="USD",
        occurred_at=d(22), description="Cloud infrastructure",
        counterparty="Amazon Web Services, Inc.",
    ))

    # --- A GL entry nothing else knows about
    txns.append(RawTxn(
        id="t_qb_journal", source="quickbooks", source_id="qb_503",
        account_id="acc_qb", amount=Decimal("-5000"), currency="USD",
        occurred_at=d(25), description="Accrued consulting",
        counterparty="Northwind Advisory",
    ))

    # --- Our own money arriving with no counterpart ingested
    txns.append(RawTxn(
        id="t_mystery_in", source="mercury", source_id="mtx_9003",
        account_id="acc_mercury", amount=Decimal("30000"), currency="USD",
        occurred_at=d(27), description="TRANSFER FROM BREX",
        counterparty="Brex",
    ))
    return txns


@pytest.fixture
def result():
    return reconcile(scenario(), ACCOUNTS)


# ---------------------------------------------------------------------------
# The bug this layer exists to prevent
# ---------------------------------------------------------------------------

def test_naive_summation_triple_counts_revenue():
    txns = scenario()
    # 85,000 of charges + 82,400 bank deposit + 40,000 GL entry + 30,000 mystery
    assert naive_revenue(txns, JUL, AUG) == Decimal("237400")


def test_reconciled_revenue_counts_each_dollar_once(result):
    assert revenue(result.events, JUL, AUG).value == Decimal("85000")


def test_bank_deposit_is_a_transfer_not_revenue(result):
    transfers = result.by_kind(EventKind.TRANSFER)
    assert len(transfers) == 1
    assert transfers[0].amount == Decimal("82400")
    assert {"t_payout", "t_deposit"} <= set(transfers[0].member_txn_ids)


def test_processor_fee_is_recovered_from_the_gap(result):
    fees = result.by_kind(EventKind.FEE)
    assert len(fees) == 1
    # 85,000 collected, 82,400 paid out
    assert fees[0].amount == Decimal("-2600")


def test_payroll_counted_once_with_the_specialist_keeping_detail(result):
    payroll = result.by_kind(EventKind.PAYROLL)
    assert len(payroll) == 1
    assert payroll[0].amount == Decimal("-185000")
    assert payroll[0].primary_txn_id == "t_gusto"          # Gusto knows it was 47 people
    assert "t_mercury_payroll" in payroll[0].suppressed_txn_ids


def test_duplicate_resync_does_not_double_spend(result):
    spend = [e for e in result.by_kind(EventKind.SPEND)]
    aws = [e for e in spend if e.amount == Decimal("-18400")]
    assert len(aws) == 1
    assert "t_ramp_aws_resync" in aws[0].suppressed_txn_ids


def test_ledger_echo_corroborates_but_does_not_add(result):
    aws = next(e for e in result.events if e.primary_txn_id == "t_ramp_aws")
    assert "t_qb_aws" in aws.suppressed_txn_ids
    assert aws.amount == Decimal("-18400")


def test_reconciled_burn_excludes_transfers_and_echoes(result):
    # payroll 185,000 + AWS 18,400 + processor fee 2,600
    assert burn(result.events, JUL, AUG).value == Decimal("206000")
    assert naive_burn(scenario(), JUL, AUG) == Decimal("512600")


# ---------------------------------------------------------------------------
# What it refuses to guess about
# ---------------------------------------------------------------------------

def test_unmatched_internal_inflow_is_blocked_not_counted(result):
    blocking = [x for x in result.exceptions if x.severity == "blocking"]
    assert [x.reason for x in blocking] == ["possible_unmatched_transfer"]
    assert blocking[0].txn_ids == ("t_mystery_in",)
    # and crucially it is nowhere near revenue
    assert all("t_mystery_in" not in e.member_txn_ids
               for e in result.by_kind(EventKind.REVENUE))


def test_ledger_only_entry_is_surfaced_for_a_human(result):
    reasons = [x.reason for x in result.exceptions]
    assert "ledger_only_record" in reasons
    only = next(x for x in result.exceptions if x.reason == "ledger_only_record")
    assert only.txn_ids == ("t_qb_journal",)


def test_ambiguous_ledger_match_is_withheld_rather_than_guessed():
    """Two identical primary rows: the GL row cannot be attributed, so it is not."""
    txns = [
        RawTxn(id="p1", source="ramp", source_id="r1", account_id="acc_ramp",
               amount=Decimal("-500"), currency="USD", occurred_at=d(5),
               description="Loom", counterparty="Loom"),
        RawTxn(id="p2", source="ramp", source_id="r2", account_id="acc_ramp",
               amount=Decimal("-500"), currency="USD", occurred_at=d(5),
               description="Vertex", counterparty="Vertex"),
        RawTxn(id="l1", source="quickbooks", source_id="q1", account_id="acc_qb",
               amount=Decimal("-500"), currency="USD", occurred_at=d(6),
               description="Software", counterparty="Software subscription"),
    ]
    res = reconcile(txns, ACCOUNTS)
    assert [x.reason for x in res.exceptions] == ["ambiguous_ledger_match"]
    assert burn(res.events, JUL, AUG).value == Decimal("1000")   # not 1,500


def test_primary_sources_disagreeing_is_flagged_not_averaged():
    txns = [
        RawTxn(id="a", source="stripe", source_id="ch_1", account_id="acc_stripe",
               amount=Decimal("1000"), currency="USD", occurred_at=d(5),
               description="charge", external_refs={"link": "shared_1"}),
        RawTxn(id="b", source="mercury", source_id="mtx_1", account_id="acc_mercury",
               amount=Decimal("900"), currency="USD", occurred_at=d(5),
               description="deposit", external_refs={"link": "shared_1"}),
    ]
    res = reconcile(txns, ACCOUNTS)
    assert any(x.reason == "primary_sources_disagree" for x in res.exceptions)


# ---------------------------------------------------------------------------
# Properties that have to hold for attestation to mean anything
# ---------------------------------------------------------------------------

def test_run_is_deterministic_regardless_of_input_order():
    txns = scenario()
    a = reconcile(txns, ACCOUNTS)
    shuffled = txns[:]
    random.Random(7).shuffle(shuffled)
    b = reconcile(shuffled, ACCOUNTS)
    assert a.input_fingerprint == b.input_fingerprint
    assert [(e.kind, e.amount, e.primary_txn_id) for e in a.events] == \
           [(e.kind, e.amount, e.primary_txn_id) for e in b.events]


def test_every_event_carries_evidence(result):
    for e in result.events:
        assert e.evidence, f"{e.key} has no evidence trail"
        assert 0 < e.confidence <= 1.0


def test_no_transaction_contributes_to_two_events(result):
    seen: set[str] = set()
    for e in result.events:
        contributing = set(e.member_txn_ids) - set(e.suppressed_txn_ids)
        assert not (contributing & seen), "a row is being counted twice"
        seen |= contributing


# ---------------------------------------------------------------------------
# What the dashboard actually renders
# ---------------------------------------------------------------------------

def test_headline_metrics_hang_together(result):
    balances = [Balance("acc_mercury", d(31), Decimal("4200000"))]
    cash = cash_on_hand(balances, CASH_ACCOUNTS)
    assert cash.value == Decimal("4200000")

    r = runway_months(result.events, cash, AUG, lookback_months=1)
    # 206,000 out, 85,000 in -> 121,000 net burn for the month
    assert r.value == Decimal("34.7")

    metrics = [cash, r]
    apply_exception_warnings(metrics, result.exceptions)
    # a blocking exception exists, so the dashboard must say so
    assert all(m.unreconciled_warning for m in metrics)


def test_metrics_expose_their_own_provenance(result):
    m = revenue(result.events, JUL, AUG)
    assert m.event_keys, "a metric with no drill-down is an assertion, not a number"
    keys = {e.key for e in result.events}
    assert set(m.event_keys) <= keys


# ---------------------------------------------------------------------------
# Regression tests for three silent-failure modes.
#
# All three shared a shape: the engine was rigorous about what it *matched* and
# lax about what it *assumed*, and each failure was silent rather than an
# exception.  These lock the fixes in place.
# ---------------------------------------------------------------------------

from decimal import Decimal as _D
from datetime import datetime as _dt

from ledgerline.reconciliation.types import (
    Account as _Account,
    RawTxn as _RawTxn,
    SourceRole as _SourceRole,
)
from ledgerline.reconciliation.engine import reconcile as _reconcile, fingerprint as _fingerprint
from ledgerline.reconciliation.metrics import (
    Balance as _Balance,
    cash_on_hand as _cash_on_hand,
    revenue as _revenue,
)

_WHEN = _dt(2026, 8, 1)
_WINDOW = (_dt(2026, 1, 1), _dt(2027, 1, 1))


def _bank(name="Mercury operating", source="mercury"):
    return _Account(id="bank", source=source, role=_SourceRole.BANK, name=name)


def _inflow(counterparty, description, amount=_D(250000)):
    return _RawTxn(
        id="x", source="mercury", source_id="x1", account_id="bank",
        amount=amount, currency="USD", occurred_at=_WHEN,
        counterparty=counterparty, description=description,
    )


def test_unrecognised_rail_is_not_silently_booked_as_revenue():
    """An unenumerated bank must not turn our own money into revenue quietly.

    Before the fix, INTERNAL_INSTITUTION_HINTS was a hardcoded 14-item tuple:
    'Brex' was caught and 'Column NA' sailed through as $250,000 of revenue
    with no exception raised at all.
    """
    txn = _inflow("COLUMN NA", "ACH credit COLUMN NA 887766")
    result = _reconcile([txn], [_bank()])
    assert result.exceptions, "an unattributed six-figure inflow must be flagged"
    assert result.exceptions[0].reason == "unattributed_inflow"


def test_internal_hints_derive_from_the_orgs_own_accounts():
    """Banking at Column should make 'Column' a recognised internal rail."""
    accounts = [_bank(), _Account(id="col", source="column", role=_SourceRole.BANK,
                                  name="Column settlement")]
    txn = _inflow("COLUMN NA", "ACH credit COLUMN NA 887766")
    result = _reconcile([txn], accounts)
    reasons = {x.reason for x in result.exceptions}
    assert "possible_unmatched_transfer" in reasons
    assert _revenue(result.events, *_WINDOW).value == 0


def test_generic_account_words_do_not_become_internal_hints():
    """'Mercury operating' must not make every merchant named '...operating' internal."""
    txn = _inflow("OPERATING THEATRE SUPPLIES LLC", "card payment", amount=_D(400))
    result = _reconcile([txn], [_bank()])
    assert not any(x.reason == "possible_unmatched_transfer" for x in result.exceptions)


def test_fingerprint_covers_fields_that_change_the_outcome():
    """external_refs drives pass 1, so it must change the fingerprint.

    Before the fix these two hashed identically, which made the attestation
    claim -- 'these numbers came from exactly these rows' -- unprovable.
    """
    base = dict(id="t1", source="mercury", source_id="m1", account_id="bank",
                amount=_D(100), currency="USD", occurred_at=_WHEN)
    plain = _RawTxn(**base)
    referenced = _RawTxn(**base, external_refs={"stripe_payout_id": "po_zzz"})
    assert _fingerprint([plain]) != _fingerprint([referenced])

    named = _RawTxn(**base, counterparty="Datadog")
    assert _fingerprint([plain]) != _fingerprint([named])


def test_fingerprint_is_still_stable_for_identical_input():
    base = dict(id="t1", source="mercury", source_id="m1", account_id="bank",
                amount=_D(100), currency="USD", occurred_at=_WHEN,
                counterparty="Datadog", external_refs={"a": "1", "b": "2"})
    # Dict ordering must not leak into the hash.
    other = dict(base, external_refs={"b": "2", "a": "1"})
    assert _fingerprint([_RawTxn(**base)]) == _fingerprint([_RawTxn(**other)])


def test_cash_excludes_foreign_currency_rather_than_adding_it():
    """Mixed currency must refuse to count, not sum EUR into a USD total.

    Before the fix this returned 2000 'USD' from 1000 USD + 1000 EUR.
    """
    balances = [
        _Balance("bank", _WHEN, _D(1000), "USD"),
        _Balance("eur", _WHEN, _D(1000), "EUR"),
    ]
    metric = _cash_on_hand(balances, ["bank", "eur"])
    assert metric.value == _D(1000)
    assert metric.currency == "USD"
    assert metric.unreconciled_warning and "EUR" in metric.unreconciled_warning


def test_cash_is_unwarned_when_every_account_is_reporting_currency():
    balances = [_Balance("bank", _WHEN, _D(1000), "USD")]
    metric = _cash_on_hand(balances, ["bank"])
    assert metric.value == _D(1000)
    assert metric.unreconciled_warning is None
