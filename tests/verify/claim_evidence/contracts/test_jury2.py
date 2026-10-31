# tests/verify/claim_evidence/contracts/test_jury2.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import json

import pytest

from core.verify.claim_evidence.contracts.jury2 import (
    JURY2_SYSTEM_PROMPT, build_jury2_payload, build_jury2_prompt,
    jury2_eligible, parse_jury2_response,
)
from core.verify.claim_evidence.contracts.schema import ContractError
from core.verify.claim_evidence.domain.types import (
    CandidateRecord, GroundedEvidence, Jury1Decision,
)


CLAIM = "The immutable exact manuscript claim."


def candidate(outcome="supports"):
    decision = Jury1Decision(
        outcome, ("Selected source passage.",), "Jury1 explanation.",
        "The source-backed role." if outcome in {"supports", "partial"} else None,
        "The source-established opposite." if outcome == "contradicts" else None,
        None,
    )
    return CandidateRecord(
        1, decision,
        (GroundedEvidence("Selected source passage.", 0, 23, "s1", "a" * 64, "exact_raw", 1.0),),
        "f" * 64,
    )


@pytest.mark.parametrize(
    "outcome,relation,subject",
    [
        ("supports", "basis", "The source-backed role."),
        ("partial", "basis", "The source-backed role."),
        ("contradicts", "contrary", "The source-established opposite."),
    ],
)
def test_jury2_payload_contains_only_relation_subject_and_passages(outcome, relation, subject):
    value = build_jury2_payload(CLAIM, candidate(outcome))

    assert tuple(value) == (
        "asserted_relation", "passage_subject", "selected_passages",
    )
    assert value == {
        "asserted_relation": relation,
        "passage_subject": subject,
        "selected_passages": [{"span_id": "s1", "text": "Selected source passage."}],
    }


@pytest.mark.parametrize(
    "outcome,relation",
    [("supports", "basis"), ("partial", "basis"), ("contradicts", "contrary")],
)
def test_jury2_payload_uses_exact_claim_for_probability_only_jury1(
    outcome, relation,
):
    value = candidate(outcome)
    probability_only = Jury1Decision(
        outcome=outcome,
        evidence=value.decision.evidence,
        explanation=None,
        supported_part=None,
        incompatible_proposition=None,
        reason=None,
        provider_confidence=0.91,
    )
    candidate_value = CandidateRecord(
        cycle=value.cycle,
        decision=probability_only,
        grounded=value.grounded,
        fingerprint=value.fingerprint,
        duplicate_of_cycle=value.duplicate_of_cycle,
    )

    payload = build_jury2_payload(CLAIM, candidate_value)

    assert payload["asserted_relation"] == relation
    assert payload["passage_subject"] == CLAIM
    assert not {
        "claim", "outcome", "proposed_outcome", "targets", "citation_targets"
    } & set(payload)


@pytest.mark.parametrize("outcome", ["related", "off_topic", "non_decidable"])
def test_jury2_eligibility_is_closed_to_evidence_outcomes(outcome):
    assert jury2_eligible(candidate("supports"))
    assert not jury2_eligible(candidate(outcome))
    with pytest.raises(ContractError):
        build_jury2_payload(CLAIM, candidate(outcome))


def test_jury2_rejects_missing_grounded_passages_and_is_not_a_public_outcome_judge():
    value = candidate()
    empty = CandidateRecord(value.cycle, value.decision, (), value.fingerprint)
    with pytest.raises(ContractError):
        build_jury2_payload(CLAIM, empty)
    assert parse_jury2_response({"passages_fit_claim": True, "reason": "Fits."}).passages_fit_claim
    for response in (
        {"passages_fit_claim": True, "reason": "Fits.", "outcome": "supports"},
        {"passages_fit_claim": "true", "reason": "Fits."},
        {"passages_fit_claim": True, "reason": ""},
    ):
        with pytest.raises(ContractError):
            parse_jury2_response(response)


def test_jury2_prompt_uses_only_the_closed_passage_fit_payload():
    system, user = build_jury2_prompt(CLAIM, candidate())
    assert system == JURY2_SYSTEM_PROMPT
    assert json.loads(user) == build_jury2_payload(CLAIM, candidate())
