"""Counterparty resolution and the proposal boundary.

These two together are the compounding asset: a model may suggest that two
vendor strings are the same, but only a human's confirmation changes a number,
and once confirmed it is permanent and deterministic.
"""

from __future__ import annotations

import pytest

from ledgerline.counterparty import (
    CanonicalCounterparty,
    CounterpartyAlias,
    CounterpartyResolver,
)
from ledgerline.counterparty.resolver import duplicate_spend_candidates
from ledgerline.proposals import (
    Proposal,
    ProposalKind,
    ProposalStatus,
    actionable,
    confirm,
    reject,
)

AWS = CanonicalCounterparty(id="cp_aws", display_name="Amazon Web Services",
                            category="infrastructure")
DATADOG = CanonicalCounterparty(id="cp_dd", display_name="Datadog", category="software")


def resolver(*aliases: CounterpartyAlias) -> CounterpartyResolver:
    return CounterpartyResolver.from_records([AWS, DATADOG], aliases)


# --- resolution is a lookup, never a guess ----------------------------------

def test_confirmed_alias_resolves_to_the_canonical_vendor():
    r = resolver(CounterpartyAlias("amzn ws emea", "cp_aws", confirmed_by="evan"))
    assert r.resolve("AMZN WS EMEA").canonical is AWS
    assert r.resolve("AMZN WS EMEA").provisional is False


def test_unconfirmed_strings_never_merge_however_similar():
    """'AWS' and 'Amazon Web Services' stay separate until a human says so.

    Merging on similarity would make spend-by-vendor move without any
    transaction behind the change, which is unrecoverable trust damage.
    """
    r = resolver()
    assert r.same_vendor("AWS", "Amazon Web Services") is False


def test_unresolved_strings_still_resolve_to_themselves():
    """Unresolved is a safe state: totals stay correct, nothing is merged."""
    r = resolver()
    result = r.resolve("SOME NEW VENDOR LLC")
    assert result.provisional is True
    assert result.display == "some new vendor"


def test_unresolved_strings_are_collected_for_the_proposal_queue():
    r = resolver()
    r.resolve("DATADOG, INC. 8005551234")
    r.resolve("SQ *DATADOG INC")
    assert r.unresolved == {"datadog"}


def test_resolution_is_stable_across_repeated_calls():
    """A run is only replayable if identity is fixed for its duration."""
    r = resolver(CounterpartyAlias("datadog", "cp_dd", confirmed_by="evan"))
    assert [r.resolve("Datadog Inc").display for _ in range(5)] == ["Datadog"] * 5


def test_alias_pointing_at_a_deleted_vendor_is_skipped_not_fatal():
    r = CounterpartyResolver.from_records(
        [DATADOG], [CounterpartyAlias("ghost", "cp_gone", confirmed_by="evan")])
    assert r.resolve("ghost").provisional is True


# --- duplicate spend --------------------------------------------------------

def test_duplicate_spend_finds_one_vendor_billing_through_two_channels():
    r = resolver(
        CounterpartyAlias("datadog", "cp_dd", confirmed_by="evan"),
        CounterpartyAlias("dd inc", "cp_dd", confirmed_by="evan"),
    )
    candidates = duplicate_spend_candidates(r, [
        ("DATADOG, INC.", "ramp"),
        ("DD INC", "quickbooks"),
        ("Some Other Co", "ramp"),
    ])
    assert candidates == [("Datadog", ["quickbooks", "ramp"])]


# --- the proposal boundary --------------------------------------------------

def proposal() -> Proposal:
    return Proposal(
        id="p1", org_id="org_1", kind=ProposalKind.ALIAS,
        exception_reason="ambiguous_ledger_match",
        subject_ids=("amzn ws emea",), suggestion="cp_aws",
        rationale="Both bill from an Amazon entity for infrastructure.",
        proposed_by="claude-opus-5", confidence=0.91,
    )


def test_a_pending_proposal_cannot_reach_reconciliation():
    """However confident the model is, pending means it moves no number."""
    p = proposal()
    assert p.is_actionable is False
    assert actionable([p]) == []


def test_confirmation_requires_a_human_and_makes_it_actionable():
    p = confirm(proposal(), human="evan")
    assert p.status is ProposalStatus.CONFIRMED
    assert p.confirmed_by == "evan"
    assert actionable([p]) == [p]


def test_there_is_no_automatic_confirmation_path():
    with pytest.raises(ValueError, match="human identity"):
        confirm(proposal(), human="")


def test_double_confirmation_raises_rather_than_silently_passing():
    """Usually means two reviewers acted on the same queue item."""
    p = confirm(proposal(), human="evan")
    with pytest.raises(ValueError, match="already confirmed"):
        confirm(p, human="someone-else")


def test_rejected_proposals_are_never_actionable():
    p = reject(proposal(), human="evan")
    assert actionable([p]) == []


def test_every_proposal_names_the_model_that_made_it():
    """A bad batch of suggestions has to be revocable in bulk."""
    assert proposal().proposed_by == "claude-opus-5"


def test_alias_keys_are_normalised_on_ingest():
    """An alias stored as written would never match a lookup, and silently so.

    The vendor would simply stop merging, with no error anywhere.
    """
    r = resolver(CounterpartyAlias("DD Inc.", "cp_dd", confirmed_by="evan"))
    assert r.resolve("DD INC").canonical is DATADOG
    assert r.resolve("dd").canonical is DATADOG
