"""Restated and withdrawn source rows.

Aggregators and spreadsheets both restate history. The rule is that a
restatement appends a new revision rather than updating in place, because a
mutable raw row makes a run unreplayable and turns the attestation trail into
a claim nobody can check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ledgerline.reconciliation import Account, EventKind, RawTxn, SourceRole, reconcile
from ledgerline.reconciliation.engine import current_revisions, fingerprint
from ledgerline.reconciliation.metrics import burn

WHEN = datetime(2026, 8, 1, tzinfo=UTC)
WINDOW = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))
BANK = Account(id="bank", source="mercury", role=SourceRole.BANK, name="Mercury operating")


def row(row_id: str, amount: str, *, revision: int = 0, voided: bool = False) -> RawTxn:
    """Two revisions of one transaction share source_id and differ in revision."""
    return RawTxn(
        id=row_id, source="plaid", source_id="plaid_txn_1", account_id="bank",
        amount=Decimal(amount), currency="USD", occurred_at=WHEN,
        counterparty="Datadog", revision=revision, voided=voided,
    )


# --- collapsing -------------------------------------------------------------

def test_only_the_newest_revision_reconciles():
    current, superseded, _ = current_revisions([row("v0", "-100"), row("v1", "-140", revision=1)])
    assert [r.id for r in current] == ["v1"]
    assert [r.id for r in superseded] == ["v0"]


def test_superseded_rows_are_returned_not_dropped():
    """Drill-down still has to show what a figure used to be built from."""
    _, superseded, _ = current_revisions([row("v0", "-100"), row("v1", "-140", revision=1)])
    assert superseded[0].amount == Decimal("-100")


def test_a_pending_row_restated_higher_does_not_double_count():
    """The failure this prevents: pending $100 and posted $140 both counting."""
    result = reconcile([row("v0", "-100"), row("v1", "-140", revision=1)], [BANK])
    assert burn(result.events, *WINDOW).value == Decimal("140")


def test_withdrawn_rows_count_toward_nothing():
    result = reconcile([row("v0", "-100"), row("v1", "-100", revision=1, voided=True)], [BANK])
    assert burn(result.events, *WINDOW).value == Decimal("0")
    assert result.events == []


def test_a_single_row_is_unaffected():
    current, superseded, exceptions = current_revisions([row("v0", "-100")])
    assert len(current) == 1 and not superseded and not exceptions


# --- saying it out loud -----------------------------------------------------

def test_a_restated_amount_raises_a_review_exception():
    """A number moving with no new transaction behind it is what a CFO notices."""
    result = reconcile([row("v0", "-100"), row("v1", "-140", revision=1)], [BANK])
    restated = [x for x in result.exceptions if x.reason == "restated_amount"]
    assert len(restated) == 1
    assert "-100" in restated[0].detail and "-140" in restated[0].detail


def test_a_withdrawal_is_reported():
    result = reconcile([row("v0", "-100"), row("v1", "-100", revision=1, voided=True)], [BANK])
    assert [x.reason for x in result.exceptions] == ["withdrawn_by_source"]


def test_a_restatement_that_changes_nothing_is_silent():
    """Re-ingesting identical data must not generate queue noise."""
    result = reconcile([row("v0", "-100"), row("v1", "-100", revision=1)], [BANK])
    assert [x for x in result.exceptions if x.reason == "restated_amount"] == []


# --- determinism ------------------------------------------------------------

def test_collapsing_is_order_independent():
    rows = [row("v1", "-140", revision=1), row("v0", "-100")]
    forward, _, _ = current_revisions(rows)
    backward, _, _ = current_revisions(list(reversed(rows)))
    assert [r.id for r in forward] == [r.id for r in backward] == ["v1"]


def test_rows_at_the_same_revision_are_duplicates_not_restatements():
    """A re-sync must still reach the duplicate_record pass and leave evidence.

    Collapsing them here would get the arithmetic right and lose the fact that
    a re-sync happened -- correct number, missing audit trail.
    """
    rows = [row("b", "-100"), row("a", "-100")]
    current, superseded, exceptions = current_revisions(rows)
    assert sorted(r.id for r in current) == ["a", "b"]
    assert superseded == [] and exceptions == []

    result = reconcile(rows, [BANK])
    rules = {e.rule.value for ev in result.events for e in ev.evidence}
    assert "duplicate_record" in rules
    assert burn(result.events, *WINDOW).value == Decimal("100")


def test_a_restatement_changes_the_fingerprint():
    """A run over restated data must not claim to be the earlier run."""
    assert fingerprint([row("v0", "-100")]) != fingerprint([row("v1", "-140", revision=1)])


def test_the_engine_never_sees_a_superseded_row():
    """Structural, not remembered: reconcile() collapses before any pass runs."""
    events = reconcile([row("v0", "-100"), row("v1", "-140", revision=1)], [BANK]).events
    assert len(events) == 1
    assert events[0].kind is EventKind.SPEND
    assert events[0].amount == Decimal("-140")


def test_a_withdrawal_alone_changes_the_fingerprint():
    """Same amount, opposite meaning: voiding must not hash as the original."""
    assert fingerprint([row("v0", "-100")]) != fingerprint([row("v0", "-100", voided=True)])
