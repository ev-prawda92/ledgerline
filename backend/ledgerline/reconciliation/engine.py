"""The reconciliation engine.

Six sources report overlapping views of the same money.  This turns those
overlapping views into one non-duplicated list of things that actually
happened, plus an explicit list of the cases it refuses to guess about.

Design rules, in priority order:

1.  Never guess silently.  A match the engine is not sure about becomes a
    ReconciliationException for a human, not a quiet merge.
2.  Deterministic.  Same inputs -> same outputs, always.  No model calls, no
    randomness, no wall-clock dependence.  The run is fingerprinted so an
    auditor can prove the numbers came from these rows.
3.  Every merge carries Evidence.  The dashboard number is only trustworthy
    if you can walk it back to the rows and the rule that combined them.
4.  Ledgers corroborate, they never contribute.  QuickBooks restating a
    Stripe charge is not a second charge.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Iterable, Sequence

from .normalize import (
    counterparty_similarity,
    internal_hints_for_accounts,
    looks_like_internal_movement,
    normalize_counterparty,
)
from .types import (
    Account,
    EconomicEvent,
    Evidence,
    EventKind,
    MatchRule,
    RawTxn,
    ReconciliationException,
    ReconciliationResult,
    SourceRole,
)


@dataclass(frozen=True)
class Config:
    # How far apart two records for the same event may sit.  Bank settlement
    # lags processor events by a day or two; ledgers post later still.
    transfer_window_days: int = 5
    ledger_window_days: int = 10
    # Fees mean a processor payout rarely equals the gross it covers.
    fee_tolerance_pct: Decimal = Decimal("0.05")
    # Below this, a candidate match is surfaced rather than applied.
    min_auto_match_confidence: float = 0.80
    # A ledger row with no primary counterpart is worth a human's attention.
    flag_ledger_only: bool = True
    # Backstop for the case name-matching cannot catch: a large bank inflow that
    # no other source corroborates.  Named-rail detection only recognises
    # institutions we can name; this catches the rest.  Set to None to disable.
    unattributed_inflow_threshold: Decimal | None = Decimal("25000")


# Which source wins when several describe the same event.  The specialist that
# owns the detail beats the bank line that only knows an amount.
_PRECEDENCE = {
    SourceRole.PAYROLL: 0,
    SourceRole.PROCESSOR: 1,
    SourceRole.CARD: 2,
    SourceRole.BANK: 3,
    SourceRole.LEDGER: 9,
}


class _DSU:
    """Union-find over transaction ids, so passes can merge freely."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        # Keep the lexicographically smaller root for determinism.
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        self.parent[hi] = lo
        return True

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return {k: sorted(v) for k, v in out.items()}


def reconcile(
    txns: Sequence[RawTxn],
    accounts: Sequence[Account],
    config: Config | None = None,
) -> ReconciliationResult:
    cfg = config or Config()
    acct = {a.id: a for a in accounts}
    # Derived from this org's own accounts, so the internal-transfer guard works
    # for a company banking somewhere we never hardcoded.
    internal_hints = internal_hints_for_accounts(accounts)
    # Deterministic input ordering: everything downstream depends on this.
    rows = sorted(txns, key=lambda t: (t.occurred_at, t.source, t.source_id, t.id))
    by_id = {t.id: t for t in rows}

    dsu = _DSU()
    for t in rows:
        dsu.add(t.id)

    evidence: list[Evidence] = []
    exceptions: list[ReconciliationException] = []
    # Records that describe an event but must not contribute value to it.
    suppressed: set[str] = set()
    transfer_groups: set[str] = set()
    fee_events: list[EconomicEvent] = []
    # Inflows that look like our own money arriving with no counterpart.
    # We refuse to call these revenue -- overstating cash is the one failure
    # that loses a CFO permanently.
    suspected_internal: set[str] = set()

    def role(t: RawTxn) -> SourceRole:
        return acct[t.account_id].role

    def link(a: RawTxn, b: RawTxn, rule: MatchRule, conf: float, note: str) -> None:
        dsu.union(a.id, b.id)
        evidence.append(Evidence(rule=rule, confidence=conf, txn_ids=(a.id, b.id), note=note))

    # ------------------------------------------------------------------
    # Pass 0 -- the same source reported the same record twice.
    # A re-sync that lacks an idempotency key is the most common way a
    # dashboard silently doubles a number.
    # ------------------------------------------------------------------
    seen: dict[tuple[str, str], RawTxn] = {}
    for t in rows:
        key = (t.source, t.source_id)
        if key in seen:
            first = seen[key]
            link(first, t, MatchRule.DUPLICATE_RECORD, 1.0,
                 f"{t.source} reported {t.source_id} more than once")
            suppressed.add(t.id)
        else:
            seen[key] = t

    # ------------------------------------------------------------------
    # Pass 1 -- explicit cross references.  When one system names another's
    # identifier there is nothing to infer.
    # ------------------------------------------------------------------
    ref_index: dict[str, list[RawTxn]] = defaultdict(list)
    for t in rows:
        ref_index[t.source_id].append(t)
        for value in t.external_refs.values():
            ref_index[value].append(t)
    for value, group in sorted(ref_index.items()):
        distinct = {g.id: g for g in group}
        if len(distinct) < 2:
            continue
        members = sorted(distinct.values(), key=lambda t: t.id)
        anchor = members[0]
        for other in members[1:]:
            if other.source == anchor.source and other.batch_id == anchor.batch_id:
                continue
            link(anchor, other, MatchRule.EXPLICIT_REFERENCE, 0.99,
                 f"shared identifier {value}")

    # ------------------------------------------------------------------
    # Pass 2 -- internal transfers: equal and opposite across two accounts we
    # own.  This is the Stripe payout landing in Mercury.  Both rows are real;
    # the money did not appear, it moved.
    # ------------------------------------------------------------------
    unclaimed = [t for t in rows if t.id not in suppressed]
    outs = [t for t in unclaimed if t.amount < 0 and acct[t.account_id].is_internal]
    ins = [t for t in unclaimed if t.amount > 0 and acct[t.account_id].is_internal]
    used_in: set[str] = set()
    for o in outs:
        window = timedelta(days=cfg.transfer_window_days)
        best: tuple[float, RawTxn] | None = None
        for i in ins:
            if i.id in used_in or i.account_id == o.account_id:
                continue
            if i.currency != o.currency or i.abs_amount != o.abs_amount:
                continue
            if abs(i.occurred_at - o.occurred_at) > window:
                continue
            if dsu.find(i.id) == dsu.find(o.id):
                conf = 0.99  # already linked by reference; confirm as transfer
            else:
                conf = 0.90
                # Naming the other institution is strong corroboration.
                if (looks_like_internal_movement(i.description, internal_hints)
                        or looks_like_internal_movement(o.description, internal_hints)):
                    conf = 0.95
            if best is None or conf > best[0]:
                best = (conf, i)
        if best is None:
            # Dangerous case: a bank inflow that looks like our own money
            # arriving, with no counterpart ingested.  Counting it as revenue
            # is exactly how cash gets inflated, so refuse and escalate.
            continue
        conf, match = best
        used_in.add(match.id)
        link(o, match, MatchRule.TRANSFER_PAIR, conf,
             f"{o.abs_amount} {o.currency} moved {acct[o.account_id].name} -> {acct[match.account_id].name}")
        transfer_groups.add(dsu.find(o.id))

    # Unpaired inflows that smell like our own rails.
    for i in ins:
        if i.id in used_in or i.id in suppressed:
            continue
        if dsu.find(i.id) in transfer_groups:
            continue
        if (looks_like_internal_movement(i.counterparty, internal_hints)
                or looks_like_internal_movement(i.description, internal_hints)):
            exceptions.append(ReconciliationException(
                reason="possible_unmatched_transfer",
                txn_ids=(i.id,),
                detail=(f"{i.abs_amount} {i.currency} arrived in "
                        f"{acct[i.account_id].name} from what looks like our own "
                        f"rails ({i.counterparty or i.description!r}) with no matching "
                        "outflow ingested. Counting this as revenue would overstate cash."),
                severity="blocking",
            ))
            suspected_internal.add(i.id)

    # ------------------------------------------------------------------
    # Pass 3 -- batches.  A processor payout covers many component charges.
    # The components stay as individual events; the gap between their sum and
    # the payout is the processor's fee, and it is real spend.
    # ------------------------------------------------------------------
    batches: dict[str, list[RawTxn]] = defaultdict(list)
    for t in rows:
        if t.batch_id and t.id not in suppressed:
            batches[t.batch_id].append(t)
    for batch_id, members in sorted(batches.items()):
        payout = next((t for t in rows if t.source_id == batch_id
                       or t.external_refs.get("self") == batch_id), None)
        components = [t for t in members if payout is None or t.id != payout.id]
        if payout is None or not components:
            continue
        gross = sum((t.amount for t in components), Decimal(0))
        net = payout.abs_amount
        gap = gross - net
        if gap == 0:
            continue
        tolerance = gross * cfg.fee_tolerance_pct
        if abs(gap) <= abs(tolerance):
            fee_events.append(EconomicEvent(
                key=f"fee:{batch_id}",
                kind=EventKind.FEE,
                amount=-abs(gap),
                currency=payout.currency,
                occurred_at=payout.occurred_at,
                counterparty=payout.source,
                primary_txn_id=payout.id,
                member_txn_ids=(payout.id,),
                evidence=(Evidence(
                    rule=MatchRule.PAYOUT_NET_OF_FEES,
                    confidence=0.95,
                    txn_ids=tuple(sorted(t.id for t in components)) + (payout.id,),
                    note=(f"{len(components)} charges totalling {gross} paid out as "
                          f"{net}; difference booked as processor fee"),
                ),),
                confidence=0.95,
                suppressed_txn_ids=(payout.id,),
                _sources=(payout.source,),
            ))
        else:
            exceptions.append(ReconciliationException(
                reason="batch_does_not_balance",
                txn_ids=tuple(sorted(t.id for t in components)) + (payout.id,),
                detail=(f"batch {batch_id}: components total {gross} but payout is "
                        f"{net}; gap of {gap} exceeds the {cfg.fee_tolerance_pct:%} "
                        "fee tolerance"),
                severity="blocking",
            ))

    # ------------------------------------------------------------------
    # Pass 4 -- a specialist source and the bank describing the same outflow.
    # Gusto knows it was payroll for 47 people; Mercury only knows 185,000 left.
    # Same event, and the specialist keeps the detail.
    # ------------------------------------------------------------------
    specialists = [t for t in rows
                   if t.id not in suppressed
                   and role(t) in (SourceRole.PAYROLL, SourceRole.CARD, SourceRole.PROCESSOR)]
    bank_rows = [t for t in rows if t.id not in suppressed and role(t) is SourceRole.BANK]
    claimed_bank: set[str] = set()
    for s in specialists:
        window = timedelta(days=cfg.transfer_window_days)
        for b in bank_rows:
            if b.id in claimed_bank or dsu.find(b.id) in transfer_groups:
                continue
            if dsu.find(b.id) == dsu.find(s.id):
                continue
            if b.currency != s.currency or b.amount != s.amount:
                continue
            if abs(b.occurred_at - s.occurred_at) > window:
                continue
            sim = counterparty_similarity(b.counterparty or b.description,
                                          s.counterparty or s.source)
            conf = 0.85 + 0.10 * sim
            claimed_bank.add(b.id)
            suppressed.add(b.id)
            link(s, b, MatchRule.SPECIALIST_OVER_BANK, round(conf, 2),
                 f"{s.source} holds the detail for the {b.abs_amount} {b.currency} "
                 f"line on {acct[b.account_id].name}")
            break

    # ------------------------------------------------------------------
    # Pass 5 -- ledger echoes.  QuickBooks is bookkeeping about events other
    # systems already reported.  Corroboration, never contribution.
    # ------------------------------------------------------------------
    ledger_rows = [t for t in rows if role(t) is SourceRole.LEDGER and t.id not in suppressed]
    primary_rows = [t for t in rows if role(t).is_primary and t.id not in suppressed]
    for l in ledger_rows:
        window = timedelta(days=cfg.ledger_window_days)
        candidates = [
            p for p in primary_rows
            if p.currency == l.currency
            and p.amount == l.amount
            and abs(p.occurred_at - l.occurred_at) <= window
        ]
        if not candidates:
            if cfg.flag_ledger_only:
                exceptions.append(ReconciliationException(
                    reason="ledger_only_record",
                    txn_ids=(l.id,),
                    detail=(f"{l.source} books {l.amount} {l.currency} to "
                            f"{l.counterparty or 'an unnamed counterparty'} but no "
                            "primary source reports it. Either an integration is "
                            "missing or this is a manual journal entry."),
                    severity="review",
                ))
            continue
        scored = sorted(
            ((counterparty_similarity(l.counterparty or l.description,
                                      c.counterparty or c.description), c)
             for c in candidates),
            key=lambda pair: (-pair[0], pair[1].id),
        )
        top_score, top = scored[0]
        # A single candidate matching on exact amount and date is strong even
        # when the merchant strings look nothing alike ("AWS" vs "Amazon Web
        # Services").  Several candidates means the name has to carry the
        # decision, and usually it cannot.
        if len(scored) == 1:
            conf = 0.85 + 0.10 * top_score
        else:
            conf = 0.70 + 0.20 * top_score
        ambiguous = len(scored) > 1 and abs(scored[1][0] - top_score) < 1e-9
        if ambiguous or conf < cfg.min_auto_match_confidence:
            exceptions.append(ReconciliationException(
                reason="ambiguous_ledger_match",
                txn_ids=(l.id,),
                candidates=tuple(c.id for _, c in scored[:5]),
                detail=(f"{l.source} row for {l.amount} {l.currency} matches "
                        f"{len(scored)} primary records equally well; not merging "
                        "automatically."),
                severity="review",
            ))
            suppressed.add(l.id)  # withhold rather than double count
            continue
        suppressed.add(l.id)
        link(top, l, MatchRule.LEDGER_ECHO, round(conf, 2),
             f"{l.source} restates {top.source} record {top.source_id}")

    # A ledger row never adds value to a metric, matched or not. An unmatched
    # one is usually an accrual or a manual journal entry -- real bookkeeping,
    # but not money that moved. It stays visible for drill-down and it is
    # already queued as an exception.
    suppressed |= {t.id for t in rows if role(t) is SourceRole.LEDGER}

    # ------------------------------------------------------------------
    # Build events out of the groups.
    # ------------------------------------------------------------------
    ev_by_txn: dict[str, list[Evidence]] = defaultdict(list)
    for e in evidence:
        for tid in e.txn_ids:
            ev_by_txn[tid].append(e)

    events: list[EconomicEvent] = []
    for root, member_ids in sorted(dsu.groups().items()):
        members = [by_id[m] for m in member_ids]
        contributing = [m for m in members if m.id not in suppressed]
        is_transfer = root in transfer_groups
        primary = min(
            contributing or members,
            key=lambda t: (_PRECEDENCE[role(t)], t.occurred_at, t.id),
        )
        group_evidence = tuple(sorted(
            {id(e): e for tid in member_ids for e in ev_by_txn.get(tid, [])}.values(),
            key=lambda e: (e.rule.value, e.txn_ids),
        ))
        confidence = min((e.confidence for e in group_evidence), default=1.0)

        if is_transfer:
            kind = EventKind.TRANSFER
            amount = primary.abs_amount
        elif primary.id in suspected_internal:
            # Held out of every metric until a human says what it was.
            kind = EventKind.UNKNOWN
            amount = primary.amount
        else:
            kind = _classify(primary, role(primary), acct)
            amount = primary.amount

        # Backstop: a material inflow the bank reported that nothing else
        # corroborates.  The named-rail check in pass 2 only catches
        # institutions we can recognise; this catches an unrecognised rail
        # before it is quietly counted as revenue.
        threshold = cfg.unattributed_inflow_threshold
        if (threshold is not None
                and kind is EventKind.REVENUE
                and role(primary) is SourceRole.BANK
                and not group_evidence
                and amount >= threshold):
            exceptions.append(ReconciliationException(
                reason="unattributed_inflow",
                txn_ids=(primary.id,),
                detail=(f"{amount} {primary.currency} arrived in "
                        f"{acct[primary.account_id].name} from "
                        f"{primary.counterparty or 'an unnamed counterparty'} with no "
                        "corroborating record from any other source. Counted as "
                        "revenue pending confirmation it is not an internal transfer."),
                severity="review",
            ))
            confidence = min(confidence, 0.6)

        # Disagreement between two primary sources is never silently averaged.
        primary_amounts = {m.amount for m in contributing if role(m).is_primary}
        if len(primary_amounts) > 1 and not is_transfer:
            exceptions.append(ReconciliationException(
                reason="primary_sources_disagree",
                txn_ids=tuple(member_ids),
                detail=(f"linked records report different amounts "
                        f"{sorted(primary_amounts)}; using {primary.source} "
                        f"({primary.amount}) pending review"),
                severity="blocking",
            ))
            confidence = min(confidence, 0.5)

        events.append(EconomicEvent(
            key=f"ev:{root}",
            kind=kind,
            amount=amount,
            currency=primary.currency,
            occurred_at=primary.occurred_at,
            counterparty=primary.counterparty,
            primary_txn_id=primary.id,
            member_txn_ids=tuple(member_ids),
            evidence=group_evidence or (Evidence(
                rule=MatchRule.UNMATCHED, confidence=1.0, txn_ids=(primary.id,),
                note=f"single record from {primary.source}, nothing to reconcile"),),
            confidence=confidence,
            suppressed_txn_ids=tuple(sorted(m.id for m in members if m.id in suppressed)),
            _sources=tuple(m.source for m in members),
        ))

    events.extend(fee_events)
    events.sort(key=lambda e: (e.occurred_at, e.key))
    exceptions.sort(key=lambda x: (x.severity != "blocking", x.reason, x.txn_ids))

    return ReconciliationResult(
        events=events,
        exceptions=exceptions,
        input_fingerprint=fingerprint(rows),
    )


def _classify(t: RawTxn, r: SourceRole, acct: dict[str, Account]) -> EventKind:
    if r is SourceRole.LEDGER:
        # Nothing here moved money on its own account.
        return EventKind.UNKNOWN
    hint = (t.category_hint or "").lower()
    if hint in {"fee", "processor_fee", "interchange"}:
        return EventKind.FEE
    if r is SourceRole.PAYROLL or hint == "payroll":
        return EventKind.PAYROLL
    if t.amount > 0:
        # Money into an owned account from outside is revenue unless a source
        # says otherwise.  Transfers were already claimed in pass 2.
        return EventKind.REVENUE if r in (SourceRole.PROCESSOR, SourceRole.BANK) else EventKind.UNKNOWN
    return EventKind.SPEND


def fingerprint(rows: Iterable[RawTxn]) -> str:
    """Stable hash of the inputs, so a run can be proved reproducible.

    This is what makes attestation meaningful: the number on the dashboard came
    from exactly these rows under exactly this code path.  That claim only holds
    if every field capable of changing the outcome is hashed -- `external_refs`
    drives pass 1 and `counterparty` feeds passes 4 and 5, so omitting them
    would let two genuinely different reconciliations fingerprint identically.
    """
    h = hashlib.sha256()
    for t in rows:
        refs = ";".join(f"{k}={v}" for k, v in sorted(t.external_refs.items()))
        h.update("|".join([
            t.source, t.source_id, t.account_id, str(t.amount), t.currency,
            t.occurred_at.isoformat(), t.batch_id or "",
            t.description or "", t.counterparty or "", t.category_hint or "", refs,
        ]).encode())
    return h.hexdigest()
