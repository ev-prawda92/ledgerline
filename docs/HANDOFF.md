# Ledgerline — context handoff

Self-contained brief for picking this up in a fresh conversation. Covers what
Ledgerline is, how it's architected, where it sits against the competition, the
decisions already made, and the reconciliation layer that's been built and tested.

---

## 1. What Ledgerline is

A "Financial OS" for a company's CFO and finance team. It connects Ramp, Stripe,
Mercury, QuickBooks, Gusto and NetSuite into one canonical model and puts two
things on top: a snapshot dashboard (cash on hand, monthly burn, runway, MRR,
upcoming payroll, outstanding invoices) and an AI Financial Analyst that reasons
across all sources at once.

Tagline: **"Know your numbers."**

Positioning: an intelligence layer that sits on top of the existing stack rather
than replacing accounting software. The framing line from the mockup —
*"Ledgerline doesn't replace your stack — it's the layer that understands what
all of it means together"* — is doing real work and should be kept.

Two surfaces beyond the dashboard:

- **Financial Timeline** — a chronological narrative of what happened to the
  company's money, each entry attributed to the source that produced it
  (Stripe deposit, Ramp flag, QuickBooks overdue invoice).
- **Always-on agents** — continuously probing connected sources, detecting
  anomalies, feeding both the timeline and the alerting.

There is a polished mockup already built. It is a designed product, not a sketch.

## 2. Architecture

Four layers:

| Layer | What it is | Stack |
|---|---|---|
| Data | Canonical financial model — every transaction, balance, invoice, payroll run normalised into common types regardless of source | PostgreSQL, SQLAlchemy, Alembic |
| Agent | One Cortex agent per integration; scheduled OAuth pulls, normalise, reconcile, detect | Cortex runtime, OAuth2 |
| Intelligence | AI analyst: question → query canonical model → structured prompt → Claude API → streamed answer with source attribution | FastAPI, Claude API, SSE |
| Presentation | CFO/analyst dashboard plus an admin panel that is Cortex's UI branded as Ledgerline settings | Next.js, React, Tailwind |

Real-time path: agents write to an `events` table → PG `LISTEN/NOTIFY` or Redis
pub/sub → API → SSE to the dashboard.

**Cortex** is a separate product line — an agent control plane (agent registry,
RBAC, approval workflows, attestation, trust scoring, audit trail). In Ledgerline
it runs as the invisible backend. Note the current boundary: `app.py` imports
`cortex_engine.py`, so Cortex is a *library*, not a service. That's fine for
shipping speed but means Cortex has no independent surface. If it's ever to be
sold or used separately, the boundary needs to become a service call — a cheap
decision now, an expensive one after production data exists.

## 3. Competitive read: Datarails

Datarails now brands itself **"FinanceOS — the AI operating system for finance
teams"**, which is essentially the same framing. Worth knowing precisely:

- $70M Series C (Jan 2026) at ~$550M, $175M total raised; 70% YoY revenue growth
  in 2025; 2,000+ customers; 600+ integrations.
- Shipped three AI Finance Agents: Strategy, Planning, Reporting — generating
  board-ready PowerPoint, PDF and Excel from unified ERP/CRM/HRIS data.
- Modules: FP&A, Month-End Close, Cash, Spend Control, and a "FinanceOS AI
  Connector" that exposes governed financial data to external AI tools
  (Anthropic named specifically).
- Series C earmarks money for "potential sector acquisitions."

**Where Ledgerline is actually different:**

- Datarails' centre of gravity is Excel — "keep working in your spreadsheet, we
  handle the plumbing." Ledgerline's user never opens a spreadsheet.
- Their integrations are enterprise back-office (NetSuite, SAP, Workday,
  Dynamics, Yardi); Ledgerline's are the startup fintech stack (Ramp, Stripe,
  Mercury, Gusto). Almost no overlap in sources means almost no overlap in customer.
- They are a *planning* tool (budgeting, forecasting, close, spend approvals —
  FP&A analyst workflows). Ledgerline is a *monitoring* tool for a founder or a
  one-person finance team.
- Their AI is invoked; Ledgerline's agents run continuously. That's an
  architectural difference, not a marketing one.
- Nothing they have resembles the Financial Timeline.

**Risks noted:** "Financial OS" is no longer distinctive positioning. A
"Planning" item in the Ledgerline sidebar picks a fight on Datarails' home turf
and makes the product legible as "a cheaper Datarails" — consider cutting it
from the nav unless it's real. NetSuite in the connected-sources list points at
their buyer, not Ledgerline's; decide whether that's deliberate.

**Can anything be sold to Datarails?** Assessed as: not as a vendor. They just
raised nine figures to make agent governance their core differentiator and have
a CTO with an ML background plus an EVP of R&D — they build that, they don't
license it. The one genuine gap is attestation: their governance claim is an
assertion ("more private, secure, accurate"), not a mechanism, while their agents
now autonomously produce board deliverables. Immutable, hash-chained,
per-action provenance is a real answer to "what did the agent access and can you
prove it." But that's an acquisition conversation, not a sales one, and pitching
them means walking a well-funded adjacent player through the architecture for a
low-probability deal. Recommendation: don't approach now; compete quietly.

## 4. Decisions made

- **Ledgerline is the destination.** The CFO lives in Ledgerline; it is not a
  data source behind someone else's interface.
- **MCP is a side door, not the front door.** Exposing Ledgerline as an MCP
  server is parked as a possible later distribution play. The more useful near-term
  inversion is Ledgerline as an MCP *client*: as sources ship their own MCP
  servers, those become integrations that don't need a bespoke agent.
- **Excel as a source, not a destination.** An `excel_agent` watching a file and
  normalising it into the canonical model is just a sixth integration and closes
  a real gap (many target customers keep the true numbers in a sheet). Excel as
  the *workspace* is Datarails' moat and is being deliberately avoided.
- **Attestation moves to the front.** It was specified as a compliance bullet in
  the admin panel. It belongs in the analyst's answer: every figure one click
  from the rows, the source, the sync time and the agent that produced it.
- **Numbers come from SQL, prose comes from Claude.** The model never does
  arithmetic and never states a figure it cannot cite.
- **Build order: reconciliation → events and timeline → analyst.** The analyst is
  the demo; the timeline is the reason a user comes back on a Tuesday, since
  cash/burn/runway barely move day to day.

## 5. Open items

- **Data ownership tension.** Cortex's stated principle is that it controls
  access but doesn't store customer data. Ledgerline's foundation is a Postgres
  model holding every transaction plus live OAuth tokens for Stripe, Mercury and
  QuickBooks. Implications: read-only scopes everywhere, tokens in a KMS rather
  than a database column, bank data via an aggregator to move liability, and
  SOC 2 before the first real customer rather than after.
- **Mockup bug.** The AI answer's "MAPPED ACROSS" row lists *Gusto · Ramp ·
  Rillet*. Rillet is a competitor and isn't a connected source — should be QuickBooks.
- **Schema gaps identified:** a hard uniqueness constraint on
  (source, source_id) for idempotent re-syncs; a `currency` field; a real
  counterparty-normalisation table (vendor dedupe is the entire answer to "are we
  paying for duplicate software?", and it is not an AI problem).
- **SSE durability.** Have the stream replay from the `events` table with a
  last-event-id. `LISTEN/NOTIFY` is a nudge; the table is the truth. Clients disconnect.

## 6. What has been built: the reconciliation layer

Working, tested Python. 17 tests passing. Delivered as
`ledgerline-reconciliation.zip`.

### The problem it solves

Six sources report overlapping views of the same money. A Stripe payout appears
in Stripe, in Mercury, and again in QuickBooks. Payroll appears in Gusto, in the
bank line and in the GL. Sum what the sources say and every headline number is
inflated — and cash, burn and runway are what a CFO will cross-check against
their bank first.

On a 13-record scenario built from the mockup's own figures:

| | Naive summation | Reconciled |
|---|---|---|
| Revenue | $237,400 | **$85,000** |
| Burn | $512,600 | **$206,000** |

### How it works

Raw rows are immutable and never deleted. Reconciliation is a pure, deterministic
function over them producing `economic_events`; all metrics read events, never
raw rows. A rule change means replaying history, not migrating numbers.

**Source roles** are the core distinction. A *primary* source observed the money
move (bank, processor, card, payroll). A *ledger* (QuickBooks, NetSuite) is
bookkeeping about something a primary source already reported — it corroborates
and never contributes value.

**Matching rules, in order:** `duplicate_record` (same source id ingested twice)
→ `explicit_reference` (one system names another's id) → `transfer_pair` (equal
and opposite across two owned accounts) → `payout_net_of_fees` (charges vs the
smaller payout; the gap is a real fee) → `specialist_over_bank` (Gusto knows it
was 47 people, Mercury only knows $185,000 left) → `ledger_echo`.

**Four principles the code holds to:**

1. Never guess silently — an uncertain match becomes a human's work item.
2. Deterministic — no model calls, no randomness; shuffled inputs give identical
   output; runs are fingerprinted.
3. Every merge carries evidence (rule, confidence, contributing rows) — this is
   simultaneously the analyst's citations and the attestation trail.
4. Refusing to count beats overstating — an inflow that looks like the company's
   own money with no counterpart is held out of every metric and flagged blocking.

**Metrics rules:** cash is a stock taken from the latest bank-reported balance,
never a sum of transactions; transfers are excluded from everything; every metric
returns the event keys behind it; blocking exceptions attach a visible warning.

Exceptions are the Cortex approval queue. Human resolutions are written to
`manual_links` and applied as inputs on the next run, so accuracy improves
without the engine ever having guessed.

### Files

`reconciliation/types.py` (vocabulary), `normalize.py` (counterparty dedupe),
`engine.py` (six passes + exceptions), `metrics.py`, `schema.py` (SQLAlchemy
tables), `tests/test_reconcile.py`, `demo.py` (prints before/after), `README.md`.

```
pip install sqlalchemy pytest
python -m pytest tests/ -q
python demo.py
```

### Deliberately not done

Multi-currency (needs an FX rate table and a decision on which date's rate is
authoritative); fuzzy aggregate matching (batches match by explicit `batch_id`
only — summing arbitrary subsets to hit a target is where engines like this start
inventing relationships); vendor aliasing ("AWS" vs "Amazon Web Services" needs an
alias table learned from confirmed exceptions); incremental runs (full re-runs are
correct and fast enough at this size).

## 7. Suggested next steps

1. Run `demo.py` against real source data and check the rules match how the
   actual APIs behave.
2. Wire the engine into the FastAPI app: agents write only raw rows; a run
   produces events; nothing outside the layer computes a number from raw rows.
3. Build the events/timeline surface on top of reconciled events.
4. Then the analyst, citing evidence rather than asserting figures.
