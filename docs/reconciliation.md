# Ledgerline — reconciliation layer

Six sources report overlapping views of the same money. This turns those
overlapping views into one non-duplicated list of things that actually
happened, and an explicit queue of the cases it refuses to guess about.

Every headline number on the dashboard is computed from the output of this
layer, never from raw source rows.

## The problem, in one example

Stripe collects $85,000 across three charges and pays out $82,400 to Mercury.
Gusto runs $185,000 of payroll, which also shows up as a Mercury withdrawal.
Ramp reports an $18,400 AWS charge, a re-sync reports it again, and QuickBooks
books it a third time.

Add up what the sources say and you get **$237,400 of revenue and $512,600 of
burn**. The true figures are **$85,000 and $206,000**. Runway computed the
naive way is wrong by a factor that would change hiring decisions.

Run `python demo.py` to see it.

## The model

```
raw_transactions        immutable, one row exactly as a source reported it
       │
       │  reconcile()  — pure, deterministic, replayable
       ▼
economic_events         one row per thing that actually happened
event_members           which raw rows belong to which event, and their role
reconciliation_links    the evidence: which rule merged what, at what confidence
reconciliation_excep…   what the engine refused to guess about → a human
reconciliation_runs     input fingerprint + ruleset version → attestation
```

Raw rows are never mutated and never deleted. Reconciliation is a pure
function of them, so changing a rule means re-running history rather than
migrating numbers — and any figure the dashboard ever showed can be reproduced
from the rows that produced it.

## Source roles

The single most important distinction. A **primary** source observed the money
move. A **ledger** is bookkeeping about something a primary source already
reported — it corroborates, it never contributes.

| Role | Example | Authoritative for |
|---|---|---|
| `bank` | Mercury | cash balances |
| `processor` | Stripe | revenue |
| `card` | Ramp | card spend |
| `payroll` | Gusto | payroll |
| `ledger` | QuickBooks, NetSuite | nothing — corroboration only |

## Matching rules, in order

| Rule | What it catches | Confidence |
|---|---|---|
| `duplicate_record` | same source, same id, ingested twice | 1.00 |
| `explicit_reference` | one system names another's identifier | 0.99 |
| `transfer_pair` | equal and opposite across two accounts you own | 0.90–0.99 |
| `payout_net_of_fees` | batch of charges vs the smaller payout — the gap is a fee | 0.95 |
| `specialist_over_bank` | Gusto knows it was 47 people; Mercury only knows $185,000 left | 0.85–0.95 |
| `ledger_echo` | QuickBooks restating a primary record | 0.85–0.95 |

Anything below the auto-match threshold, or ambiguous between candidates,
becomes an exception rather than a merge.

## Four rules the code holds to

1. **Never guess silently.** An uncertain match becomes a human's work item,
   not a quiet merge. `ambiguous_ledger_match` withholds the row entirely
   rather than attributing it to the wrong thing.
2. **Deterministic.** No model calls, no randomness, no wall-clock dependence.
   Shuffled inputs produce identical output. Runs are fingerprinted.
3. **Every merge carries evidence.** A number is only trustworthy if you can
   walk it back to the rows and the rule that combined them. This is also
   exactly what the AI analyst cites and what attestation stores.
4. **Refusing to count beats overstating.** An inflow that looks like your own
   money arriving with no counterpart ingested is held out of every metric and
   flagged `blocking`. Overstating cash is the failure a CFO does not forgive.

## Metrics

- **Cash is a stock, not a flow** — taken from the latest balance each bank
  reports, never by summing transactions. One missed row and a summed figure
  drifts forever.
- **Transfers are excluded from everything.** Moving your own money is not
  revenue and not burn.
- Every metric returns the event keys behind it, so the dashboard can drill
  down and the analyst can cite instead of assert.
- When blocking exceptions exist, metrics carry a visible warning. A number
  that might be wrong should say so.

## Files

| File | What it is |
|---|---|
| `reconciliation/types.py` | the vocabulary: sources, events, evidence, exceptions |
| `reconciliation/normalize.py` | counterparty normalisation (the duplicate-software answer) |
| `reconciliation/engine.py` | the six passes and the exception logic |
| `reconciliation/metrics.py` | headline numbers, computed from events only |
| `reconciliation/schema.py` | SQLAlchemy tables |
| `tests/test_reconcile.py` | the scenario above, plus the properties attestation depends on |
| `demo.py` | prints the before/after |

```
pip install sqlalchemy pytest
python -m pytest tests/ -q
python demo.py
```

## Wiring it in

Integration agents write only to `raw_transactions` — no interpretation, no
maths, the payload kept as returned. A reconciliation run then reads the org's
rows, calls `reconcile()`, and writes a fresh set of `economic_events` plus a
`reconciliation_runs` row. The dashboard, the timeline and the analyst read
events; nothing outside this layer reads raw rows to compute a number.

Human resolutions of exceptions are written to `manual_links` and applied as
inputs on the next run, so the system gets more accurate over time without the
engine ever having guessed.

## Deliberately not done yet

- **Multi-currency.** Amounts must match exactly today. FX needs a rate table
  and a decision about which date's rate is authoritative.
- **Fuzzy aggregate matching.** Batches are matched by explicit `batch_id`
  only. Summing arbitrary subsets to hit a target is combinatorial and it is
  where this kind of engine starts inventing relationships.
- **Vendor aliasing.** `normalize_counterparty` handles noise prefixes and
  legal suffixes. "AWS" vs "Amazon Web Services" needs an alias table, which
  should be learned from confirmed exceptions rather than hardcoded.
- **Incremental runs.** Full re-runs are correct and fast enough at this size.
  Windowed re-runs need care so that a late-arriving row can still pair with
  something outside the window.
