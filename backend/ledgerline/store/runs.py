"""Running reconciliation against the database, and storing what it produced.

A run is the unit of attestation.  It records the fingerprint of its inputs and
the ruleset version that read them, so any figure the dashboard ever showed can
be traced to a run, and that run can be reproduced from rows that cannot have
changed underneath it.

Reconciliation is a *full* re-run every time, not an incremental update.  That
is what makes a rule change replay history instead of migrating numbers, and at
the volumes a private company generates it is fast enough that the simplicity is
free.  Two consequences fall out of it, and both are handled here:

*   Events are rewritten per run, so the timeline needs a key that is stable
    across runs or it churns every night.  The engine's event key is derived
    from the rows themselves, so it serves.
*   Exceptions would otherwise be re-inserted nightly, and anything a human
    dismissed would come straight back.  They are matched on a stable key and
    carried forward with their status intact.  A queue nobody can clear is
    worse than no queue.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..reconciliation.engine import Config, reconcile
from ..reconciliation.schema import EconomicEvent as EventRow
from ..reconciliation.schema import EventMember as MemberRow
from ..reconciliation.schema import ReconciliationExceptionRow
from ..reconciliation.schema import ReconciliationLink as LinkRow
from ..reconciliation.schema import ReconciliationRun as RunRow
from ..reconciliation.types import ReconciliationException, ReconciliationResult
from .raw import load_accounts, load_raw_transactions


def exception_key(exception: ReconciliationException) -> str:
    """Identity of a queue item, independent of which run produced it."""
    material = exception.reason + "|" + "|".join(sorted(exception.txn_ids))
    return hashlib.sha256(material.encode()).hexdigest()[:32]


@dataclass
class RunSummary:
    run_id: str
    events: int
    exceptions_opened: int
    exceptions_carried: int
    #: Items that were open last night and are not raised any more -- usually
    #: because a later revision or a new source resolved them.
    exceptions_closed: int
    fingerprint: str


def run_reconciliation(
    session: Session,
    org_id: str,
    *,
    config: Config | None = None,
    triggered_by: str = "agent",
) -> tuple[RunSummary, ReconciliationResult]:
    rows = load_raw_transactions(session, org_id)
    accounts = load_accounts(session, org_id)
    result = reconcile(rows, accounts, config)

    run = RunRow(
        org_id=org_id,
        started_at=datetime.now(UTC),
        input_fingerprint=result.input_fingerprint,
        ruleset_version=settings.ruleset_version,
        event_count=len(result.events),
        exception_count=len(result.exceptions),
        triggered_by=triggered_by,
    )
    session.add(run)
    session.flush()

    known_txn_ids = {row.id for row in rows}
    events_by_key: dict[str, EventRow] = {}

    for event in result.events:
        # A fee event's primary is the payout row, which is a real stored row;
        # any event whose primary is synthetic is skipped rather than violating
        # the foreign key, because a dangling reference in the evidence chain is
        # worse than a missing event.
        if event.primary_txn_id not in known_txn_ids:
            continue
        row = EventRow(
            org_id=org_id, run_id=run.id, kind=event.kind.value,
            amount=event.amount, currency=event.currency,
            occurred_at=event.occurred_at, counterparty=event.counterparty,
            primary_txn_id=event.primary_txn_id, confidence=event.confidence,
            dedupe_key=event.key,
        )
        session.add(row)
        session.flush()
        events_by_key[event.key] = row

        suppressed = set(event.suppressed_txn_ids)
        for txn_id in event.member_txn_ids:
            if txn_id not in known_txn_ids:
                continue
            session.add(MemberRow(
                event_id=row.id, txn_id=txn_id,
                contribution="suppressed" if txn_id in suppressed else "contributing",
            ))
        for evidence in event.evidence:
            session.add(LinkRow(
                run_id=run.id, event_id=row.id, rule=evidence.rule.value,
                confidence=evidence.confidence, txn_ids=list(evidence.txn_ids),
                note=evidence.note,
            ))

    opened, carried = _persist_exceptions(session, org_id, run.id, result)
    closed = _close_resolved_exceptions(session, org_id, result)

    run.finished_at = datetime.now(UTC)
    session.flush()

    return RunSummary(
        run_id=run.id, events=len(events_by_key), exceptions_opened=opened,
        exceptions_carried=carried, exceptions_closed=closed,
        fingerprint=result.input_fingerprint,
    ), result


def _persist_exceptions(
    session: Session, org_id: str, run_id: str, result: ReconciliationResult,
) -> tuple[int, int]:
    existing = {
        row.dedupe_key: row
        for row in session.execute(
            select(ReconciliationExceptionRow)
            .where(ReconciliationExceptionRow.org_id == org_id)).scalars()
    }
    opened = carried = 0
    for exception in result.exceptions:
        key = exception_key(exception)
        prior = existing.get(key)
        if prior is not None:
            # Still raised. Keep the original row so its age is meaningful, and
            # keep a human's decision: a dismissed item does not reopen itself.
            prior.run_id = run_id
            carried += 1
            continue
        session.add(ReconciliationExceptionRow(
            org_id=org_id, run_id=run_id, reason=exception.reason,
            severity=exception.severity, dedupe_key=key,
            txn_ids=list(exception.txn_ids),
            candidate_txn_ids=list(exception.candidates),
            detail=exception.detail, status="open",
        ))
        opened += 1
    session.flush()
    return opened, carried


def _close_resolved_exceptions(
    session: Session, org_id: str, result: ReconciliationResult,
) -> int:
    """Close open items the engine no longer raises.

    Usually a later revision or a newly connected source answered the question
    on its own. Leaving them open would make the queue accumulate work that no
    longer exists.
    """
    still_raised = {exception_key(x) for x in result.exceptions}
    closed = 0
    for row in session.execute(
        select(ReconciliationExceptionRow).where(
            ReconciliationExceptionRow.org_id == org_id,
            ReconciliationExceptionRow.status == "open",
        )).scalars():
        if row.dedupe_key not in still_raised:
            row.status = "resolved"
            row.resolved_by = "engine"
            row.resolved_at = datetime.now(UTC)
            row.resolution = {"reason": "no longer raised by reconciliation"}
            closed += 1
    session.flush()
    return closed
