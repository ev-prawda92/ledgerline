# Ledgerline

**Know your numbers.**

A financial OS for founders and small finance teams. Ledgerline connects the
startup fintech stack — Stripe, Mercury, Ramp, QuickBooks, Gusto — into one
canonical model, corrects the double-counting that comes from six sources
describing the same money, and puts a dashboard, a timeline and an AI analyst
on top of the corrected figures.

It sits on top of the existing stack as an intelligence layer. It does not
replace accounting software.

## The one idea

Six sources report overlapping views of the same money. A Stripe payout appears
in Stripe, again in Mercury, and again in QuickBooks. Payroll appears in Gusto,
in the bank line, and in the GL. Add up what the sources say and every headline
number is inflated — and cash, burn and runway are exactly what a CFO
cross-checks against their bank first.

On the 13-record scenario in `backend/demo.py`:

| | Naive summation | Reconciled |
|---|---|---|
| Revenue | $237,400 | **$85,000** |
| Burn | $512,600 | **$206,000** |

That correction is the product. Everything else is an interface to it.

## The invariant

```
integrations → raw_transactions → reconciliation → economic_events → metrics / timeline / analyst
```

**Nothing outside `ledgerline.reconciliation` computes a financial figure from a
raw row.** Raw rows are immutable and never deleted; reconciliation is a pure,
deterministic function over them; metrics read only the events it produces. A
rule change replays history rather than migrating numbers.

Two corollaries that are easy to violate by accident:

- An integration's only job is to authenticate, paginate and write immutable
  rows. It never dedupes across sources and never computes a total.
- The AI analyst states no figure it cannot cite, and performs no arithmetic.
  Numbers come from SQL; prose comes from Claude.

## Status

| Piece | State |
|---|---|
| Reconciliation engine | Built — 24 tests, deterministic, fingerprinted |
| Canonical schema | Built — `org_id` throughout, idempotent re-sync constraint |
| Metrics from events | Built |
| Dashboard | Designed (mockup only) |
| Integrations / OAuth | Not started |
| Persistence, workers, run loop | Not started |
| Exception queue, analyst, auth, deploy | Not started |

## Quickstart

```
make install      # venv + editable install
make test         # 24 tests, no database needed — the engine is pure
make demo         # prints the before/after correction above
make up           # postgres + redis
make migrate      # alembic upgrade head
```

## Layout

```
backend/
  ledgerline/
    reconciliation/   the engine — pure, no DB, no model calls, exhaustively testable
    integrations/     one module per source (not started)
    api/              FastAPI routers (thin)
    config.py db.py main.py
  alembic/            migrations against reconciliation/schema.py
  tests/
  demo.py
frontend/             not scaffolded — see frontend/README.md for why
docs/
  HANDOFF.md          full context brief
  reconciliation.md   how the engine works, rule by rule
```

## How reconciliation decides

**Source roles** are the core distinction. A *primary* source observed the money
move (bank, processor, card, payroll). A *ledger* (QuickBooks, NetSuite) is
bookkeeping about something a primary source already reported — it corroborates
and never contributes value.

**Matching rules, in order:** `duplicate_record` → `explicit_reference` →
`transfer_pair` → `payout_net_of_fees` → `specialist_over_bank` → `ledger_echo`.

**Four principles the code holds to:**

1. Never guess silently — an uncertain match becomes a human's work item.
2. Deterministic — no model calls, no randomness; shuffled inputs give identical
   output; every run is fingerprinted over every field that can change it.
3. Every merge carries evidence (rule, confidence, contributing rows). This is
   simultaneously the analyst's citations and the attestation trail.
4. Refusing to count beats overstating.

Exceptions are the approval queue. Human resolutions are written to
`manual_links` and applied as inputs on the next run, so accuracy improves
without the engine ever having guessed.

## Deliberately not done

Multi-currency (needs an FX rate table and a decision on which date's rate is
authoritative; `cash_on_hand` currently excludes foreign accounts and says so).
Fuzzy aggregate matching — batches match by explicit `batch_id` only, because
summing arbitrary subsets to hit a target is where engines like this start
inventing relationships. Vendor aliasing ("AWS" vs "Amazon Web Services") needs
an alias table learned from confirmed exceptions. Incremental runs — full
re-runs are correct and fast enough at this size.

## Next

Stage 1 is Stripe and Mercury, together, because they *overlap* and therefore
actually exercise the engine — one integration alone teaches OAuth and proves
nothing. It ends with the number that validates the whole thesis: how large the
correction is on a real company's real data.
