# core/verify/claim_evidence/domain/state_machine.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure bounded claim-evidence state-machine transitions."""
from dataclasses import replace

from .fingerprint import FINGERPRINT_VERSION, candidate_fingerprint
from .policy import jury2_eligible, jury2_no_action, strict_majority, validate_policy
from .types import (
    MECHANICAL_REJECTION_CAUSES,
    CandidateRecord,
    GroundedEvidence,
    Jury1Decision,
    Jury2Decision,
    MachineState,
    MachineTransition,
    StateMachinePolicy,
    TerminalResult,
    validate_jury1_decision,
)


class StateMachineError(ValueError):
    """An event is inconsistent with the immutable machine state."""


def start(policy: StateMachinePolicy) -> MachineState:
    return MachineState(validate_policy(policy))


def make_candidate(
    state: MachineState,
    decision: Jury1Decision,
    grounded: tuple[GroundedEvidence, ...],
    *,
    claim_hash: str,
    source_hash: str,
    fingerprint_version: str = FINGERPRINT_VERSION,
) -> CandidateRecord:
    _require_action(state, "jury1")
    decision = validate_jury1_decision(decision)
    if (
        not _valid_grounded(decision, grounded, source_hash)
        or not isinstance(claim_hash, str)
        or not claim_hash
        or not isinstance(source_hash, str)
        or not source_hash
    ):
        raise StateMachineError("candidate provenance is invalid")
    material = tuple(
        {
            "span_id": item.span_id,
            "raw_start": item.raw_start,
            "raw_end": item.raw_end,
            "text": item.text,
            "source_hash": item.source_hash,
        }
        for item in grounded
    )
    fields = {
        "claim_hash": claim_hash,
        "source_hash": source_hash,
        "supported_part": decision.supported_part,
        "incompatible_proposition": decision.incompatible_proposition,
        "reason": decision.reason,
        "provider_confidence": decision.provider_confidence,
    }
    identity = candidate_fingerprint(
        outcome=decision.outcome,
        evidence=decision.evidence,
        outcome_fields=fields,
        grounded=material, version=fingerprint_version,
    )
    return CandidateRecord(state.candidate_cycles + 1, decision, grounded, identity)


def jury1_technical_failure(state: MachineState) -> MachineTransition:
    _require_action(state, "jury1")
    failures = state.jury1_technical_failures + 1
    updated = replace(
        state,
        jury1_technical_failures=failures,
        jury1_technical_failures_total=state.jury1_technical_failures_total + 1,
    )
    if failures < state.policy.jury1_technical_cap:
        return MachineTransition(updated, "jury1_technical_retry")
    return _exhaust(updated, "jury1_technical")


def provider_uncertain(state: MachineState, jury: str) -> MachineTransition:
    """Terminate an observed but below-policy provider reply without a retry."""
    if jury not in {"jury1", "jury2"}:
        raise StateMachineError("provider uncertainty jury is invalid")
    expected = "jury1" if jury == "jury1" else "jury2"
    _require_action(state, expected)
    terminal = TerminalResult(
        "uncertain", None, f"{jury}_provider_uncertain", "non_crediting",
        None, None,
    )
    return MachineTransition(
        replace(state, current_candidate=None, next_action="terminal", terminal=terminal),
        f"{jury}_provider_uncertain",
    )


def jury2_provider_uncertain(state: MachineState) -> MachineTransition:
    """Requeue an uncertain Jury2 candidate without treating it as a no vote."""
    _require_action(state, "jury2")
    if state.current_candidate is None:
        raise StateMachineError("Jury2 has no current candidate")
    if state.candidate_cycles >= state.policy.candidate_cap:
        return provider_uncertain(state, "jury2")
    return MachineTransition(
        replace(
            state,
            current_candidate=None,
            jury2_technical_failures=0,
            next_action="jury1",
        ),
        "jury2_provider_uncertain",
    )


def reject_jury1(state: MachineState, cause: str) -> MachineTransition:
    _require_action(state, "jury1")
    if cause not in MECHANICAL_REJECTION_CAUSES:
        raise StateMachineError("mechanical rejection cause is invalid")
    updated = replace(
        state,
        candidate_cycles=state.candidate_cycles + 1,
        jury1_technical_failures=0,
        mechanical_rejections=state.mechanical_rejections + 1,
    )
    if updated.candidate_cycles < state.policy.candidate_cap:
        return MachineTransition(updated, f"jury1_rejected:{cause}")
    return _candidate_cap_terminal(updated)


def admit_jury1(state: MachineState, candidate: CandidateRecord) -> MachineTransition:
    _require_action(state, "jury1")
    if (
        not isinstance(candidate, CandidateRecord)
        or candidate.cycle != state.candidate_cycles + 1
        or candidate.cycle > state.policy.candidate_cap
    ):
        raise StateMachineError("candidate cycle is invalid")
    duplicate_cycle = _rejected_cycle(state, candidate.fingerprint)
    if duplicate_cycle is not None:
        candidate = replace(candidate, duplicate_of_cycle=duplicate_cycle)
    updated = replace(
        state,
        candidate_cycles=candidate.cycle,
        jury1_technical_failures=0,
        jury2_technical_failures=0,
        candidates=state.candidates + (candidate,),
    )
    if not jury2_eligible(candidate.decision.outcome):
        return _normal(updated, candidate, "jury2_not_eligible", "not_evaluated", None)
    if state.policy.jury2_level == "off":
        return _normal(updated, candidate, "jury2_off", "not_evaluated", None)
    if duplicate_cycle is not None:
        return _jury2_no(updated, candidate, "jury2_duplicate_rejected")
    updated = replace(updated, current_candidate=candidate, next_action="jury2")
    return MachineTransition(updated, "jury2_required")


def jury2_technical_failure(state: MachineState) -> MachineTransition:
    _require_action(state, "jury2")
    failures = state.jury2_technical_failures + 1
    updated = replace(
        state,
        jury2_technical_failures=failures,
        jury2_technical_failures_total=state.jury2_technical_failures_total + 1,
    )
    if failures < state.policy.jury2_technical_cap:
        return MachineTransition(updated, "jury2_technical_retry")
    return _exhaust(updated, "jury2_technical")


def apply_jury2(state: MachineState, decision: Jury2Decision) -> MachineTransition:
    _require_action(state, "jury2")
    if (
        not isinstance(decision, Jury2Decision)
        or type(decision.passages_fit_claim) is not bool
        or type(decision.provider_uncertain) is not bool
        or (decision.provider_uncertain and not _confidence(decision.provider_confidence))
        or (decision.reason is None and not _confidence(decision.provider_confidence))
        or (decision.reason is not None and (not isinstance(decision.reason, str) or not decision.reason.strip()))
    ):
        raise StateMachineError("Jury2 decision is invalid")
    candidate = state.current_candidate
    if candidate is None:
        raise StateMachineError("Jury2 has no current candidate")
    if decision.provider_uncertain:
        return jury2_provider_uncertain(state)
    if decision.passages_fit_claim:
        updated = replace(state, jury2_technical_failures=0)
        return _normal(updated, candidate, "jury2_accepted", "passed", True)
    return _jury2_no(
        replace(state, jury2_technical_failures=0),
        candidate,
        "jury2_rejected",
    )


def cancel(state: MachineState) -> MachineTransition:
    if state.next_action == "terminal":
        raise StateMachineError("terminal state cannot be cancelled")
    terminal = TerminalResult(
        "cancelled", None, "cancelled", "non_crediting", None, None
    )
    return MachineTransition(
        replace(
            state,
            current_candidate=None,
            next_action="terminal",
            terminal=terminal,
        ),
        "cancelled",
    )


def _jury2_no(
    state: MachineState,
    candidate: CandidateRecord,
    event: str,
) -> MachineTransition:
    rejected = state.jury2_rejected + (candidate,)
    updated = replace(
        state,
        jury2_rejected=rejected,
        rejected_fingerprints=state.rejected_fingerprints + (candidate.fingerprint,),
        current_candidate=None,
    )
    action = jury2_no_action(
        level=state.policy.jury2_level,
        cycle=state.candidate_cycles,
        candidate_cap=state.policy.candidate_cap,
    )
    if action == "requeue":
        return MachineTransition(replace(updated, next_action="jury1"), event)
    if action == "contested":
        return _contested(
            updated,
            candidate,
            "jury2_rejected_nonbinding",
            (),
            event,
        )
    return _candidate_cap_terminal(updated, event=event)


def _candidate_cap_terminal(
    state: MachineState,
    *,
    event: str = "candidate_cap_exhausted",
) -> MachineTransition:
    if not state.jury2_rejected:
        return _exhaust(state, "jury1_guard")
    majority = strict_majority(state.jury2_rejected)
    if state.policy.jury2_level == "medium":
        if majority.winner is None or majority.representative is None:
            return _exhaust(state, "no_consensus", tally=majority.tally)
        return _contested(
            state,
            majority.representative,
            "majority_fallback",
            majority.tally,
            event,
        )
    if state.policy.jury2_level == "high":
        return _exhaust(
            state,
            "jury2_rejected",
            tally=majority.tally,
            observed=majority.winner,
        )
    raise StateMachineError("candidate-cap history is inconsistent with policy")


def _normal(
    state: MachineState,
    candidate: CandidateRecord,
    resolution: str,
    assurance: str,
    jury2_passed: bool | None,
) -> MachineTransition:
    evidence_status = (
        "jury2_approved_candidate_evidence"
        if jury2_passed
        else "jury1_candidate_evidence"
        if candidate.grounded
        else None
    )
    terminal = TerminalResult(
        "normal",
        candidate.decision.outcome,
        resolution,
        assurance,
        jury2_passed,
        candidate,
        evidence_status=evidence_status,
    )
    return MachineTransition(
        replace(state, current_candidate=None, next_action="terminal", terminal=terminal),
        resolution,
    )


def _contested(
    state: MachineState,
    candidate: CandidateRecord,
    resolution: str,
    tally: tuple[tuple[str, int], ...],
    event: str,
) -> MachineTransition:
    terminal = TerminalResult(
        "contested",
        candidate.decision.outcome,
        resolution,
        "contested",
        False,
        candidate,
        tally,
        candidate.decision.outcome if resolution == "majority_fallback" else None,
        "jury2_rejected_candidate_evidence",
    )
    return MachineTransition(
        replace(state, current_candidate=None, next_action="terminal", terminal=terminal),
        event,
    )


def _exhaust(
    state: MachineState,
    cause: str,
    *,
    tally: tuple[tuple[str, int], ...] = (),
    observed: str | None = None,
) -> MachineTransition:
    terminal = TerminalResult(
        "exhausted",
        None,
        cause,
        "non_crediting",
        False if cause in {"no_consensus", "jury2_rejected"} else None,
        None,
        tally,
        observed,
    )
    return MachineTransition(
        replace(
            state,
            current_candidate=None,
            next_action="terminal",
            terminal=terminal,
        ),
        f"exhausted:{cause}",
    )


def _rejected_cycle(state: MachineState, fingerprint: str) -> int | None:
    return next(
        (
            candidate.cycle
            for candidate in state.jury2_rejected
            if candidate.fingerprint == fingerprint
        ),
        None,
    )


def _valid_grounded(
    decision: Jury1Decision,
    grounded: tuple[GroundedEvidence, ...],
    source_hash: str,
) -> bool:
    if (
        not isinstance(grounded, tuple)
        or len(grounded) != len(decision.evidence)
        or any(not isinstance(item, GroundedEvidence) for item in grounded)
    ):
        return False
    identities: set[tuple[int, int]] = set()
    for item in grounded:
        identity = (item.raw_start, item.raw_end)
        if (
            not item.text
            or isinstance(item.raw_start, bool)
            or isinstance(item.raw_end, bool)
            or item.raw_start < 0
            or item.raw_end <= item.raw_start
            or not item.span_id
            or item.source_hash != source_hash
            or item.match_mode not in {"exact_raw", "normalized", "fuzzy"}
            or isinstance(item.score, bool)
            or not isinstance(item.score, (int, float))
            or not 0 <= item.score <= 1
            or identity in identities
        ):
            return False
        identities.add(identity)
    return True


def _require_action(state: MachineState, action: str) -> None:
    if not isinstance(state, MachineState) or state.next_action != action:
        raise StateMachineError(f"state does not permit {action}")


def _confidence(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1
