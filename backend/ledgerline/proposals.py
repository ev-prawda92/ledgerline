"""The boundary where AI is allowed to touch reconciliation: it may propose.

Ledgerline's reconciliation engine makes no model calls, and that is not a
limitation to be engineered away -- it is the property the product is sold on.
A figure is trustworthy because the same rows and the same rules always produce
it, and because every merge carries evidence a human can check.

But there is real work a model is good at and a rule engine is not: looking at
an exception the engine refused to guess about and saying *"'AMZN WS EMEA' and
'Amazon Web Services' are probably the same vendor"*, or *"this $30,000 inflow
matches the outflow from the Brex account three days earlier."*  Throwing that
capability away would be as wasteful as letting it decide.

So the rule is: **models propose, humans confirm, deterministic code disposes.**

    engine -> exception -> [model proposes] -> human confirms -> manual_link
                                                              -> next run is
                                                                 deterministic

Three invariants make that safe, and every one of them is enforced by this
module's types rather than by anyone remembering:

1.  A proposal is never an input to :func:`~ledgerline.reconciliation.engine.reconcile`.
    Only confirmed records are.  A pending proposal cannot move a number.
2.  Every proposal records which model produced it, on what evidence, and when.
    A bad batch of suggestions must be traceable and revocable in bulk.
3.  Confirmation requires a human identity.  There is no auto-confirm threshold,
    however confident the model is, because a confidently wrong merge is
    precisely the failure the engine's exceptions exist to prevent.

The compounding effect is the point.  Each confirmation is a permanent,
deterministic fact -- an alias, a link -- so the system gets more accurate with
use while the reconciliation path stays as auditable as it was on day one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class ProposalKind(str, Enum):
    #: Two or more raw rows describe one economic event.
    LINK = "link"
    #: A normalised counterparty string means an existing canonical vendor.
    ALIAS = "alias"
    #: An inflow the engine held out of revenue is in fact an internal transfer.
    TRANSFER = "transfer"


class ProposalStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    #: Withdrawn because the underlying exception resolved another way.
    SUPERSEDED = "superseded"


@dataclass
class Proposal:
    """A suggestion awaiting a human.

    Deliberately inert: it carries no method that applies it.  Applying happens
    in the resolution path, against confirmed records only.
    """

    id: str
    org_id: str
    kind: ProposalKind
    #: The exception this answers, so the queue can show them together.
    exception_reason: str
    #: Rows or keys the proposal concerns.
    subject_ids: tuple[str, ...]
    #: What the model suggests: a canonical_id for ALIAS, a counterpart row id
    #: for LINK or TRANSFER.
    suggestion: str
    #: Plain-language reasoning shown to the human. This is the product surface
    #: of the proposal -- a suggestion a reviewer cannot evaluate is noise.
    rationale: str
    #: Which model, at which version. Non-optional so a bad batch is revocable.
    proposed_by: str
    confidence: float = 0.0
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None

    @property
    def is_actionable(self) -> bool:
        """Only confirmed proposals may become reconciliation inputs."""
        return self.status is ProposalStatus.CONFIRMED and self.confirmed_by is not None


def confirm(proposal: Proposal, *, human: str, when: datetime | None = None) -> Proposal:
    """Confirm a proposal. Requires a person; there is no automatic path.

    Raises rather than silently no-opping on a non-pending proposal, because a
    double-confirm usually means two reviewers acted on the same queue item and
    that is worth surfacing.
    """
    if not human:
        raise ValueError("confirmation requires a human identity")
    if proposal.status is not ProposalStatus.PENDING:
        raise ValueError(f"cannot confirm a proposal that is already {proposal.status.value}")
    proposal.status = ProposalStatus.CONFIRMED
    proposal.confirmed_by = human
    proposal.confirmed_at = when or datetime.now(UTC)
    return proposal


def reject(proposal: Proposal, *, human: str) -> Proposal:
    if not human:
        raise ValueError("rejection requires a human identity")
    proposal.status = ProposalStatus.REJECTED
    proposal.confirmed_by = human
    proposal.confirmed_at = datetime.now(UTC)
    return proposal


def actionable(proposals: list[Proposal]) -> list[Proposal]:
    """Filter to what may legally reach the reconciliation layer.

    Call this at the boundary. It is the single place the invariant is enforced,
    which is why it exists as a function rather than a comprehension at each
    call site.
    """
    return [p for p in proposals if p.is_actionable]
