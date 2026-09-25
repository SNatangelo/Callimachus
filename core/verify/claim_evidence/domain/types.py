# core/verify/claim_evidence/domain/types.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable records and frozen taxonomies for claim-evidence verification."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


EVIDENCE_OUTCOMES = frozenset({"supports", "partial", "contradicts"})
OUTCOMES = EVIDENCE_OUTCOMES | frozenset({"related", "off_topic", "non_decidable"})
NON_DECIDABLE_REASONS = frozenset({"attribution", "material_limit", "no_consensus", "verification_unavailable", "retrieval_limit", "provider_uncertain"})


class Jury1DecisionValidationError(ValueError):
    """A persisted Jury1Decision is not a canonical contract record."""


MECHANICAL_REJECTION_CAUSES = frozenset(
    {
        "schema_invalid",
        "evidence_cardinality_invalid",
        "grounding_invalid",
        "provenance_invalid",
        "stage_disagreement",
    }
)
TECHNICAL_FAILURE_CAUSES = frozenset(
    {
        "rate_limited",
        "credential_invalid",
        "lane_unavailable",
        "timeout",
        "transport",
        "provider_failure",
    }
)
PROTOCOL_ERROR_CODES = frozenset(
    {
        "contract_invalid",
        "evidence_basis_missing",
        "evidence_cardinality_exceeded",
        "evidence_non_decidable_nonempty",
        "evidence_span_overlap",
        "invalid_utf8_payload",
        "json_duplicate_keys",
        "json_object_ambiguous",
        "json_object_missing",
        "json_wrapper_competing",
        "json_wrapper_extra_value",
        "non_decidable_reason_invalid",
        "response_boolean_invalid",
        "response_content_empty",
        "response_expected_null",
        "response_fields_invalid",
        "response_list_duplicate",
        "response_string_invalid",
        "response_string_list_invalid",
    }
)
EXHAUSTION_CAUSES = frozenset(
    {
        "jury1_technical",
        "jury1_guard",
        "jury2_technical",
        "no_consensus",
        "jury2_rejected",
    }
)


@dataclass(frozen=True, slots=True)
class Jury1Decision:
    outcome: str
    evidence: tuple[str, ...]
    explanation: str | None
    supported_part: str | None
    incompatible_proposition: str | None
    reason: str | None
    provider_confidence: float | None = None


def validate_jury1_decision(decision: Jury1Decision) -> Jury1Decision:
    """Validate the immutable six-outcome record without assigning semantics."""
    if not isinstance(decision, Jury1Decision) or decision.outcome not in OUTCOMES:
        raise Jury1DecisionValidationError("outcome is invalid")
    evidence = decision.evidence
    if not isinstance(evidence, tuple) or any(not isinstance(item, str) or not item.strip() for item in evidence):
        raise Jury1DecisionValidationError("evidence is invalid")
    probability_only = _confidence(decision.provider_confidence)
    if probability_only:
        if decision.explanation is not None:
            raise Jury1DecisionValidationError("explanation is invalid")
    elif not isinstance(decision.explanation, str) or not decision.explanation.strip():
        raise Jury1DecisionValidationError("explanation is invalid")
    if (decision.outcome in EVIDENCE_OUTCOMES) != bool(evidence):
        raise Jury1DecisionValidationError("evidence cardinality is invalid")
    if decision.outcome in {"supports", "partial"}:
        if not probability_only and (not isinstance(decision.supported_part, str) or not decision.supported_part.strip()):
            raise Jury1DecisionValidationError("supported_part is invalid")
    elif decision.supported_part is not None:
        raise Jury1DecisionValidationError("supported_part is invalid")
    if decision.outcome == "contradicts":
        if not probability_only and (not isinstance(decision.incompatible_proposition, str) or not decision.incompatible_proposition.strip()):
            raise Jury1DecisionValidationError("incompatible_proposition is invalid")
    elif decision.incompatible_proposition is not None:
        raise Jury1DecisionValidationError("incompatible_proposition is invalid")
    if decision.outcome == "non_decidable":
        if decision.reason not in NON_DECIDABLE_REASONS:
            raise Jury1DecisionValidationError("reason is invalid")
    elif decision.reason is not None:
        raise Jury1DecisionValidationError("reason is invalid")
    return decision


@dataclass(frozen=True, slots=True)
class Jury2Decision:
    passages_fit_claim: bool
    reason: str | None
    provider_confidence: float | None = None
    provider_uncertain: bool = False


def _confidence(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


@dataclass(frozen=True, slots=True)
class ProviderLane:
    key_alias: str
    key_fingerprint: str | None
    model: str
    jury1_eligible: bool
    jury2_eligible: bool


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    name: str
    credential_aliases: tuple[str, ...]
    credential_fingerprints: tuple[str | None, ...]
    models: tuple[str, ...]
    pairing_mode: str
    lanes: tuple[ProviderLane, ...]


@dataclass(frozen=True, slots=True)
class GroundedEvidence:
    text: str
    raw_start: int
    raw_end: int
    span_id: str
    source_hash: str
    match_mode: str
    score: float


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    cycle: int
    decision: Jury1Decision
    grounded: tuple[GroundedEvidence, ...]
    fingerprint: str
    duplicate_of_cycle: int | None = None


@dataclass(frozen=True, slots=True)
class StateMachinePolicy:
    candidate_cap: int
    jury1_technical_cap: int
    jury2_technical_cap: int
    jury2_level: Literal["off", "low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class TerminalResult:
    status: Literal["normal", "contested", "uncertain", "exhausted", "cancelled"]
    outcome: str | None
    resolution: str
    assurance: Literal["passed", "not_evaluated", "contested", "non_crediting"]
    jury2_passed: bool | None
    representative: CandidateRecord | None
    tally: tuple[tuple[str, int], ...] = ()
    observed_jury1_majority: str | None = None
    evidence_status: str | None = None


@dataclass(frozen=True, slots=True)
class MachineState:
    policy: StateMachinePolicy
    candidate_cycles: int = 0
    jury1_technical_failures: int = 0
    jury1_technical_failures_total: int = 0
    jury2_technical_failures: int = 0
    jury2_technical_failures_total: int = 0
    mechanical_rejections: int = 0
    candidates: tuple[CandidateRecord, ...] = ()
    jury2_rejected: tuple[CandidateRecord, ...] = ()
    rejected_fingerprints: tuple[str, ...] = ()
    current_candidate: CandidateRecord | None = None
    next_action: Literal["jury1", "jury2", "terminal"] = "jury1"
    terminal: TerminalResult | None = None


@dataclass(frozen=True, slots=True)
class MachineTransition:
    state: MachineState
    event: str


@dataclass(frozen=True, slots=True)
class LogicalRequest:
    stage: Literal[
        "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
        "explanation_evidence", "jury2",
    ]
    logical_request_id: str
    payload: bytes
    payload_fingerprint: str
    candidate_cycle: int
    system_prompt: str
    candidate_fingerprint: str | None = None
    deadline_ms: int | None = None


@dataclass(frozen=True, slots=True)
class TechnicalFailure:
    stage: Literal[
        "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
        "explanation_evidence", "jury2",
    ]
    logical_request_id: str
    payload_fingerprint: str
    cause: str
    detail: str | None = None
    protocol_error_code: str | None = None
    response_hash: str | None = None
    retry_after_seconds: int | None = None
    http_status: int | None = None
    dispatch_attempt_id: str | None = None
    observed_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DispatchAssignment:
    provider: str
    credential_alias: str
    credential_fingerprint: str | None
    model: str
    lane_id: tuple[str, str, str]
    dispatch_attempt_id: str = ""
    post_cooldown: bool = False
    started_at_ms: int = 0
    credential_cursor: int = 0
    model_draw_index: int = 0
    prior_global_start_ms: int | None = None
    prior_model_start_ms: int | None = None
    next_eligible_ms: int = 0
    global_interval_ms: int = 0
    model_interval_ms: int = 0
    prior_cooldown: object | None = None
    cooldown_override_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class SchedulerState:
    selection: object
    pacing: object
    cooldowns: tuple[tuple[tuple[str, str, str], object], ...] = ()
    key_eligibility_ms: tuple[tuple[tuple[str, str], int], ...] = ()
    disabled_credentials: frozenset[tuple[str, str]] = frozenset()
    disabled_lanes: frozenset[tuple[str, str, str]] = frozenset()
    dispatch_counter: int = 0


def validate_logical_request(request: LogicalRequest) -> LogicalRequest:
    deadline = request.deadline_ms if isinstance(request, LogicalRequest) else None
    if (
        not isinstance(request, LogicalRequest)
        or request.stage not in {
            "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
            "explanation_evidence", "jury2",
        }
        or not request.logical_request_id
        or not isinstance(request.payload, bytes)
        or not request.payload
        or not isinstance(request.system_prompt, str)
        or not request.system_prompt
        or deadline is not None
        and (
            isinstance(deadline, bool)
            or not isinstance(deadline, int)
            or deadline < 0
        )
    ):
        raise ValueError("logical request is invalid")
    from .fingerprint import provider_prompt_fingerprint
    if provider_prompt_fingerprint(request.system_prompt, request.payload) != request.payload_fingerprint:
        raise ValueError("logical request prompt provenance is invalid")
    return request


def logical_request_provenance(
    request: LogicalRequest,
) -> tuple[str, str, int, str | None, int | None]:
    return (
        request.stage,
        request.payload_fingerprint,
        request.candidate_cycle,
        request.candidate_fingerprint,
        request.deadline_ms,
    )


def technical_failure(
    request: LogicalRequest,
    cause: str,
    *,
    retry_after: int | None = None,
    status: int | None = None,
    dispatch_id: str | None = None,
    observed_at_ms: int | None = None,
    detail: str | None = None,
    protocol_error_code: str | None = None,
    response_hash: str | None = None,
) -> TechnicalFailure:
    return TechnicalFailure(
        request.stage,
        request.logical_request_id,
        request.payload_fingerprint,
        cause,
        detail=detail,
        protocol_error_code=protocol_error_code,
        response_hash=response_hash,
        retry_after_seconds=retry_after,
        http_status=status,
        dispatch_attempt_id=dispatch_id,
        observed_at_ms=observed_at_ms,
    )


@dataclass(frozen=True, slots=True)
class ControllerEvent:
    kind: str
    candidate_cycle: int
    request_id: str | None = None
    payload_fingerprint: str | None = None
    candidate_fingerprint: str | None = None
    cause: str | None = None
    terminal: TerminalResult | None = None
    candidate: CandidateRecord | None = None
    jury2_decision: Jury2Decision | None = None
