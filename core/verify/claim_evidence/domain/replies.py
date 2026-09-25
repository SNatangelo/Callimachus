# core/verify/claim_evidence/domain/replies.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Typed provider replies and fail-closed boundary normalization."""
from __future__ import annotations

from dataclasses import dataclass

from .types import (
    PROTOCOL_ERROR_CODES,
    TECHNICAL_FAILURE_CAUSES,
    DispatchAssignment,
    Jury2Decision,
    LogicalRequest,
    TechnicalFailure,
    technical_failure,
)


@dataclass(frozen=True, slots=True)
class Jury2Answer:
    logical_request_id: str
    payload_fingerprint: str
    decision: Jury2Decision


@dataclass(frozen=True, slots=True)
class SupportGateAnswer:
    logical_request_id: str
    payload_fingerprint: str
    decision: object


@dataclass(frozen=True, slots=True)
class FullSupportGateAnswer:
    logical_request_id: str
    payload_fingerprint: str
    decision: object


@dataclass(frozen=True, slots=True)
class ContraryGateAnswer:
    logical_request_id: str
    payload_fingerprint: str
    decision: object


@dataclass(frozen=True, slots=True)
class TopicGateAnswer:
    logical_request_id: str
    payload_fingerprint: str
    decision: object


@dataclass(frozen=True, slots=True)
class ExplanationEvidenceAnswer:
    logical_request_id: str
    payload_fingerprint: str
    decision: object


def normalise_jury_reply(
    request: LogicalRequest,
    assignment: DispatchAssignment,
    reply: object,
) -> object:
    answers = (
        SupportGateAnswer,
        FullSupportGateAnswer,
        ContraryGateAnswer,
        TopicGateAnswer,
        ExplanationEvidenceAnswer,
        Jury2Answer,
    )
    matches = (
        isinstance(reply, (*answers, TechnicalFailure))
        and reply.logical_request_id == request.logical_request_id
        and reply.payload_fingerprint == request.payload_fingerprint
    )
    expected = {
        "support_gate": SupportGateAnswer,
        "full_support_gate": FullSupportGateAnswer,
        "contrary_gate": ContraryGateAnswer,
        "topic_gate": TopicGateAnswer,
        "explanation_evidence": ExplanationEvidenceAnswer,
        "jury2": Jury2Answer,
    }[request.stage]
    if matches and isinstance(reply, expected):
        return reply
    if matches and isinstance(reply, TechnicalFailure):
        return _normalise_failure(request, assignment, reply)
    return technical_failure(
        request, "provider_failure", dispatch_id=assignment.dispatch_attempt_id
    )


def _normalise_failure(
    request: LogicalRequest,
    assignment: DispatchAssignment,
    reply: TechnicalFailure,
) -> TechnicalFailure:
    cause = (
        reply.cause
        if reply.cause in TECHNICAL_FAILURE_CAUSES
        else "provider_failure"
    )
    retry_after = reply.retry_after_seconds
    if cause != "rate_limited" or not _nonnegative(retry_after):
        retry_after = None
    protocol_error_code = (
        reply.protocol_error_code
        if (
            reply.detail == "protocol_invalid"
            and reply.protocol_error_code in PROTOCOL_ERROR_CODES
        )
        else None
    )
    response_hash = (
        reply.response_hash
        if protocol_error_code is not None and _hash(reply.response_hash)
        else None
    )
    return technical_failure(
        request,
        cause,
        retry_after=retry_after,
        status=reply.http_status,
        dispatch_id=assignment.dispatch_attempt_id,
        observed_at_ms=(
            reply.observed_at_ms if _nonnegative(reply.observed_at_ms) else None
        ),
        detail="protocol_invalid" if protocol_error_code is not None else None,
        protocol_error_code=protocol_error_code,
        response_hash=response_hash,
    )


def _nonnegative(value: int | None) -> bool:
    return value is None or (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _hash(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and not set(value) - set("0123456789abcdef")
    )
