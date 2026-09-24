# tests/verify/claim_evidence/domain/test_policy.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from core.verify.claim_evidence.domain.policy import (
    PolicyError,
    jury2_eligible,
    jury2_no_action,
    strict_majority,
    validate_policy,
)
from core.verify.claim_evidence.domain.types import (
    CandidateRecord,
    Jury1Decision,
    StateMachinePolicy,
)


def _candidate(cycle: int, outcome: str) -> CandidateRecord:
    decision = Jury1Decision(
        outcome,
        (f"evidence-{cycle}",),
        f"explanation-{cycle}",
        f"part-{cycle}" if outcome in {"supports", "partial"} else None,
        f"incompatible-{cycle}" if outcome == "contradicts" else None,
        None,
    )
    return CandidateRecord(cycle, decision, (), f"fingerprint-{cycle}")


def test_policy_is_explicit_and_positive():
    policy = StateMachinePolicy(5, 3, 3, "medium")
    assert validate_policy(policy) is policy
    for invalid in (
        StateMachinePolicy(0, 3, 3, "medium"),
        StateMachinePolicy(5, True, 3, "medium"),
        StateMachinePolicy(5, 3, 3, "unexpected"),  # type: ignore[arg-type]
    ):
        with pytest.raises(PolicyError):
            validate_policy(invalid)


def test_only_evidence_outcomes_are_jury2_eligible():
    assert all(jury2_eligible(value) for value in ("supports", "partial", "contradicts"))
    assert not any(jury2_eligible(value) for value in ("related", "off_topic", "non_decidable"))


def test_jury2_no_policy_distinguishes_advisory_requeue_and_exhaustion():
    assert jury2_no_action(level="low", cycle=1, candidate_cap=5) == "contested"
    assert jury2_no_action(level="medium", cycle=4, candidate_cap=5) == "requeue"
    assert jury2_no_action(level="high", cycle=4, candidate_cap=5) == "requeue"
    assert jury2_no_action(level="medium", cycle=5, candidate_cap=5) == "exhausted"
    assert jury2_no_action(level="high", cycle=5, candidate_cap=5) == "exhausted"
    with pytest.raises(PolicyError):
        jury2_no_action(level="off", cycle=1, candidate_cap=5)


def test_strict_majority_uses_all_rejected_candidates_and_latest_representative():
    candidates = (
        _candidate(1, "supports"),
        _candidate(2, "supports"),
        _candidate(3, "partial"),
    )
    result = strict_majority(candidates)
    assert result.winner == "supports"
    assert result.representative == candidates[1]
    assert result.tally == (("partial", 1), ("supports", 2))


def test_plurality_tie_and_empty_history_have_no_majority():
    assert strict_majority((_candidate(1, "supports"), _candidate(2, "partial"))).winner is None
    assert strict_majority((
        _candidate(1, "supports"),
        _candidate(2, "partial"),
        _candidate(3, "contradicts"),
    )).winner is None
    assert strict_majority(()).winner is None
