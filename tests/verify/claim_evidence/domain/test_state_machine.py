# tests/verify/claim_evidence/domain/test_state_machine.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from core.verify.claim_evidence.domain.state_machine import (
    StateMachineError,
    admit_jury1,
    apply_jury2,
    jury1_technical_failure,
    jury2_technical_failure,
    make_candidate,
    reject_jury1,
    start,
)
from core.verify.claim_evidence.domain.types import (
    GroundedEvidence,
    Jury1Decision,
    Jury1DecisionValidationError,
    Jury2Decision,
    StateMachinePolicy,
)


SOURCE_HASH = "s" * 64
CLAIM_HASH = "c" * 64


def _policy(level: str, *, cap: int = 5, jury1_technical: int = 3, jury2_technical: int = 3):
    return StateMachinePolicy(cap, jury1_technical, jury2_technical, level)


def _decision(
    outcome: str,
    *,
    evidence: str | None = None,
    explanation: str = "explanation",
) -> Jury1Decision:
    quoted = (evidence or f"evidence-{outcome}",) if outcome in {"supports", "partial", "contradicts"} else ()
    return Jury1Decision(
        outcome,
        quoted,
        explanation,
        "supported part" if outcome in {"supports", "partial"} else None,
        "incompatible proposition" if outcome == "contradicts" else None,
        "attribution" if outcome == "non_decidable" else None,
    )


def _candidate(state, outcome: str, *, evidence: str | None = None, explanation: str = "explanation"):
    decision = _decision(outcome, evidence=evidence, explanation=explanation)
    grounded = (
        GroundedEvidence(
            decision.evidence[0],
            0,
            len(decision.evidence[0]),
            f"span-{decision.evidence[0]}",
            SOURCE_HASH,
            "exact_raw",
            1.0,
        ),
    ) if decision.evidence else ()
    return make_candidate(
        state,
        decision,
        grounded,
        claim_hash=CLAIM_HASH,
        source_hash=SOURCE_HASH,
    )


@pytest.mark.parametrize(
    ("outcome", "level", "resolution"),
    [
        ("supports", "off", "jury2_off"),
        ("partial", "off", "jury2_off"),
        ("contradicts", "off", "jury2_off"),
        ("related", "high", "jury2_not_eligible"),
        ("off_topic", "high", "jury2_not_eligible"),
        ("non_decidable", "high", "jury2_not_eligible"),
    ],
)
def test_all_six_outcomes_reach_their_boundary_without_relabeling(outcome, level, resolution):
    state = start(_policy(level))
    result = admit_jury1(state, _candidate(state, outcome)).state
    assert result.terminal is not None
    assert result.terminal.status == "normal"
    assert result.terminal.outcome == outcome
    assert result.terminal.resolution == resolution
    assert result.terminal.representative.decision.outcome == outcome


def test_related_with_evidence_is_mechanically_invalid_and_consumes_a_cycle():
    state = start(_policy("medium"))
    invalid = Jury1Decision("related", ("forbidden",), "explanation", None, None, None)
    with pytest.raises(Jury1DecisionValidationError):
        make_candidate(
            state,
            invalid,
            (),
            claim_hash=CLAIM_HASH,
            source_hash=SOURCE_HASH,
        )
    state = reject_jury1(state, "evidence_cardinality_invalid").state
    assert state.candidate_cycles == 1
    assert state.mechanical_rejections == 1
    assert state.next_action == "jury1"


def test_candidate_requires_one_valid_grounded_record_per_evidence_quote():
    state = start(_policy("off"))
    with pytest.raises(StateMachineError, match="provenance"):
        make_candidate(
            state,
            _decision("supports"),
            (),
            claim_hash=CLAIM_HASH,
            source_hash=SOURCE_HASH,
        )


def test_five_deterministic_rejections_end_in_jury1_guard_exhaustion():
    state = start(_policy("medium"))
    for _ in range(5):
        state = reject_jury1(state, "grounding_invalid").state
    assert state.terminal is not None
    assert state.terminal.status == "exhausted"
    assert state.terminal.resolution == "jury1_guard"
    assert state.terminal.outcome is None


def test_jury2_off_records_not_evaluated():
    state = start(_policy("off"))
    state = admit_jury1(state, _candidate(state, "supports")).state
    assert state.terminal.assurance == "not_evaluated"
    assert state.terminal.jury2_passed is None


def test_low_jury2_no_is_contested_without_requeue():
    state = start(_policy("low"))
    state = admit_jury1(state, _candidate(state, "partial")).state
    state = apply_jury2(state, Jury2Decision(False, "not justified")).state
    assert state.terminal.status == "contested"
    assert state.terminal.outcome == "partial"
    assert state.terminal.resolution == "jury2_rejected_nonbinding"
    assert state.terminal.jury2_passed is False


def test_low_confidence_jury2_requeues_without_semantic_rejection_or_retry():
    state = start(_policy("high", cap=2))
    state = admit_jury1(state, _candidate(state, "supports")).state
    state = apply_jury2(state, Jury2Decision(True, None, 0.7, True)).state
    assert state.next_action == "jury1"
    assert state.terminal is None
    assert state.jury2_rejected == ()
    assert state.jury2_technical_failures == 0
    assert state.jury2_technical_failures_total == 0
    state = admit_jury1(state, _candidate(state, "supports")).state
    state = apply_jury2(state, Jury2Decision(False, None, 0.7, True)).state
    assert state.terminal.status == "uncertain"
    assert state.terminal.outcome is None
    assert state.terminal.resolution == "jury2_provider_uncertain"
    assert state.terminal.jury2_passed is None
    assert state.jury2_rejected == ()


def test_medium_strict_majority_uses_only_valid_rejected_candidates():
    state = start(_policy("medium"))
    for cycle, outcome in enumerate(("supports", "supports", "partial"), start=1):
        candidate = _candidate(state, outcome, evidence=f"evidence-{cycle}")
        state = admit_jury1(state, candidate).state
        state = apply_jury2(state, Jury2Decision(False, "not justified")).state
    state = reject_jury1(state, "grounding_invalid").state
    state = reject_jury1(state, "schema_invalid").state
    assert state.terminal.status == "contested"
    assert state.terminal.outcome == "supports"
    assert state.terminal.resolution == "majority_fallback"
    assert state.terminal.representative.cycle == 2
    assert state.terminal.tally == (("partial", 1), ("supports", 2))
    assert state.terminal.evidence_status == "jury2_rejected_candidate_evidence"


def test_medium_without_a_strict_majority_is_non_crediting_no_consensus():
    state = start(_policy("medium"))
    for cycle, outcome in enumerate(("supports", "partial"), start=1):
        state = admit_jury1(
            state, _candidate(state, outcome, evidence=f"evidence-{cycle}")
        ).state
        state = apply_jury2(state, Jury2Decision(False, "not justified")).state
    for _ in range(3):
        state = reject_jury1(state, "grounding_invalid").state
    assert state.terminal.status == "exhausted"
    assert state.terminal.resolution == "no_consensus"
    assert state.terminal.outcome is None


def test_high_may_tally_but_never_exposes_a_rejected_public_outcome():
    state = start(_policy("high"))
    for cycle, outcome in enumerate(("supports", "supports", "partial"), start=1):
        state = admit_jury1(
            state, _candidate(state, outcome, evidence=f"evidence-{cycle}")
        ).state
        state = apply_jury2(state, Jury2Decision(False, "not justified")).state
    state = reject_jury1(state, "grounding_invalid").state
    state = reject_jury1(state, "grounding_invalid").state
    assert state.terminal.resolution == "jury2_rejected"
    assert state.terminal.outcome is None
    assert state.terminal.observed_jury1_majority == "supports"
    assert state.terminal.tally == (("partial", 1), ("supports", 2))


def test_later_jury2_yes_accepts_a_new_candidate_after_semantic_no():
    state = start(_policy("high"))
    first = _candidate(state, "supports", evidence="first")
    state = admit_jury1(state, first).state
    state = apply_jury2(state, Jury2Decision(False, "not justified")).state
    second = _candidate(state, "partial", evidence="second")
    state = admit_jury1(state, second).state
    state = apply_jury2(state, Jury2Decision(True, "justified")).state
    assert state.terminal.status == "normal"
    assert state.terminal.outcome == "partial"
    assert state.terminal.jury2_passed is True


def test_jury2_technical_retries_do_not_consume_jury1_capacity():
    state = start(_policy("high", jury2_technical=3))
    state = admit_jury1(state, _candidate(state, "supports")).state
    first = jury2_technical_failure(state).state
    second = jury2_technical_failure(first).state
    assert first.next_action == second.next_action == "jury2"
    assert first.candidate_cycles == second.candidate_cycles == 1
    assert second.current_candidate == state.current_candidate
    assert second.jury2_technical_failures == 2


def test_jury2_technical_exhaustion_is_distinct_and_non_crediting():
    state = start(_policy("high", jury2_technical=2))
    state = admit_jury1(state, _candidate(state, "supports")).state
    state = jury2_technical_failure(state).state
    state = jury2_technical_failure(state).state
    assert state.terminal.resolution == "jury2_technical"
    assert state.terminal.outcome is None
    assert state.terminal.jury2_passed is None
    assert state.candidate_cycles == 1


def test_rejected_semantic_fingerprint_cannot_later_be_silently_accepted():
    state = start(_policy("medium"))
    first = _candidate(state, "supports", evidence="same", explanation="first")
    state = admit_jury1(state, first).state
    state = apply_jury2(state, Jury2Decision(False, "not justified")).state
    repeated = _candidate(state, "supports", evidence="same", explanation="cosmetic")
    assert repeated.fingerprint == first.fingerprint
    transition = admit_jury1(state, repeated)
    assert transition.event == "jury2_duplicate_rejected"
    assert transition.state.next_action == "jury1"
    assert transition.state.current_candidate is None
    assert transition.state.candidates[-1].duplicate_of_cycle == 1
    assert len(transition.state.jury2_rejected) == 2


def test_jury1_and_jury2_technical_exhaustion_remain_distinct():
    jury1 = start(_policy("high", jury1_technical=2))
    jury1 = jury1_technical_failure(jury1).state
    jury1 = jury1_technical_failure(jury1).state
    jury2 = start(_policy("high", jury2_technical=2))
    jury2 = admit_jury1(jury2, _candidate(jury2, "contradicts")).state
    jury2 = jury2_technical_failure(jury2).state
    jury2 = jury2_technical_failure(jury2).state
    assert jury1.terminal.resolution == "jury1_technical"
    assert jury2.terminal.resolution == "jury2_technical"
    assert jury1.terminal.outcome is jury2.terminal.outcome is None


def test_out_of_order_transition_fails_closed():
    state = start(_policy("high"))
    with pytest.raises(StateMachineError):
        apply_jury2(state, Jury2Decision(True, "unexpected"))
