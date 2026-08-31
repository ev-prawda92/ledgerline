"""Reading and writing the immutable side of the model.

Everything here is deliberately boring.  The one idea worth stating is that
ingest is *idempotent by construction* rather than by care: a row's identity is
`(org_id, source, source_id, revision)`, the database enforces it, and this
module skips what already exists instead of trying to be clever about it.  An
interrupted sync is therefore safe to simply run again.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..reconciliation.metrics import Balance
from ..reconciliation.normalize import normalize_counterparty
from ..reconciliation.schema import Account as AccountRow
from ..reconciliation.schema import Balance as BalanceRow
from ..reconciliation.schema import Integration as IntegrationRow
from ..reconciliation.schema import RawTransaction
from ..reconciliation.types import Account, RawTxn, SourceRole


@dataclass
class IngestResult:
    inserted: int = 0
    #: Rows already present at this exact revision. Expected and healthy: it is
    #: what a re-sync looks like when nothing has changed.
    already_present: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.already_present


def _aware(value: datetime | None) -> datetime | None:
    """SQLite drops timezones. Treat a naive value as the UTC it was stored as."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


#: Which roles contribute to cash on hand.
#:
#: Deliberately only banks. A card account is a liability, not cash. A processor
#: balance is the company's money, but counting it alongside the bank
#: double-counts a payout in flight -- the same dollars appear in the Stripe
#: balance and in the deposit that has not landed yet. Including it needs a
#: timing rule, so it waits for one rather than quietly inflating the headline
#: number every CFO checks first.
CASH_ROLES = frozenset({SourceRole.BANK})


def ensure_integration(
    session: Session, org_id: str, source: str, role: SourceRole,
) -> IntegrationRow:
    """Find or create the integration a set of accounts belongs to."""
    row = session.execute(
        select(IntegrationRow).where(
            IntegrationRow.org_id == org_id, IntegrationRow.source == source,
        )).scalars().first()
    if row is None:
        row = IntegrationRow(org_id=org_id, source=source, role=role.value,
                             status="connected")
        session.add(row)
        session.flush()
    return row


def save_accounts(session: Session, org_id: str, accounts: Iterable[Account]) -> int:
    existing = {
        row.id for row in session.execute(
            select(AccountRow).where(AccountRow.org_id == org_id)).scalars()
    }
    added = 0
    for account in accounts:
        if account.id in existing:
            continue
        integration = ensure_integration(session, org_id, account.source, account.role)
        session.add(AccountRow(
            id=account.id, org_id=org_id, integration_id=integration.id,
            external_id=account.id.rsplit(":", 1)[-1],
            name=account.name, role=account.role.value,
            currency=account.currency, is_internal=account.is_internal,
            is_cash=account.role in CASH_ROLES,
        ))
        existing.add(account.id)
        added += 1
    session.flush()
    return added


def save_raw_transactions(
    session: Session, org_id: str, rows: Sequence[RawTxn],
) -> IngestResult:
    """Append rows that are not already stored at this revision.

    Never updates. A source restating a transaction arrives as a new row at a
    higher revision (see :class:`~ledgerline.reconciliation.types.RawTxn`), so
    there is nothing here that can overwrite history.
    """
    result = IngestResult()
    if not rows:
        return result

    keys = {(r.source, r.source_id, r.revision) for r in rows}
    present = {
        (row.source, row.source_id, row.revision)
        for row in session.execute(
            select(RawTransaction).where(
                RawTransaction.org_id == org_id,
                RawTransaction.source.in_({k[0] for k in keys}),
            )).scalars()
    }

    seen_in_batch: set[tuple[str, str, int]] = set()
    for row in rows:
        key = (row.source, row.source_id, row.revision)
        if key in present or key in seen_in_batch:
            result.already_present += 1
            continue
        seen_in_batch.add(key)
        session.add(RawTransaction(
            id=row.id, org_id=org_id, account_id=row.account_id,
            source=row.source, source_id=row.source_id,
            amount=row.amount, currency=row.currency, occurred_at=row.occurred_at,
            description=row.description, counterparty=row.counterparty,
            counterparty_key=normalize_counterparty(row.counterparty),
            category_hint=row.category_hint, external_refs=dict(row.external_refs),
            batch_id=row.batch_id, revision=row.revision, voided=row.voided,
        ))
        result.inserted += 1
    session.flush()
    return result


def save_balances(session: Session, org_id: str, balances: Iterable[Balance]) -> int:
    added = 0
    for balance in balances:
        exists = session.execute(
            select(BalanceRow).where(
                BalanceRow.account_id == balance.account_id,
                BalanceRow.as_of == balance.as_of,
            )).scalars().first()
        if exists:
            continue
        session.add(BalanceRow(
            org_id=org_id, account_id=balance.account_id, as_of=balance.as_of,
            amount=balance.amount, currency=balance.currency,
        ))
        added += 1
    session.flush()
    return added


def load_accounts(session: Session, org_id: str) -> list[Account]:
    rows = session.execute(
        select(AccountRow, IntegrationRow)
        .join(IntegrationRow, AccountRow.integration_id == IntegrationRow.id)
        .where(AccountRow.org_id == org_id)
    ).all()
    return [
        Account(id=account.id, source=integration.source, role=SourceRole(account.role),
                name=account.name, currency=account.currency,
                is_internal=bool(account.is_internal))
        for account, integration in rows
    ]


def cash_account_ids(session: Session, org_id: str) -> list[str]:
    """Accounts whose balances make up cash on hand. See CASH_ROLES."""
    return [
        row.id for row in session.execute(
            select(AccountRow).where(
                AccountRow.org_id == org_id, AccountRow.is_cash.is_(True),
            )).scalars()
    ]


def load_raw_transactions(session: Session, org_id: str) -> list[RawTxn]:
    """Every stored row, including superseded revisions.

    Superseded rows are deliberately included: `reconcile()` collapses them
    itself, and filtering here would mean two places decide which revision is
    current -- which is how they come to disagree.
    """
    rows = session.execute(
        select(RawTransaction)
        .where(RawTransaction.org_id == org_id)
        .order_by(RawTransaction.occurred_at, RawTransaction.id)
    ).scalars()
    return [
        RawTxn(
            id=row.id, source=row.source, source_id=row.source_id,
            account_id=row.account_id, amount=Decimal(str(row.amount)),
            currency=row.currency, occurred_at=_aware(row.occurred_at),
            description=row.description or "", counterparty=row.counterparty,
            category_hint=row.category_hint, external_refs=row.external_refs or {},
            batch_id=row.batch_id, revision=row.revision or 0,
            voided=bool(row.voided),
        )
        for row in rows
    ]


def latest_balances(session: Session, org_id: str) -> list[Balance]:
    return [
        Balance(account_id=row.account_id, as_of=_aware(row.as_of),
                amount=Decimal(str(row.amount)), currency=row.currency)
        for row in session.execute(
            select(BalanceRow).where(BalanceRow.org_id == org_id)).scalars()
    ]
