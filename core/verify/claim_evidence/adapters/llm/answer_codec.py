# core/verify/claim_evidence/adapters/llm/answer_codec.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical durable encoding for completed v9 claim-evidence answers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TypeAlias

from ...application.ports import FatalDispatchError
from ...domain.fingerprint import FINGERPRINT_VERSION, answer_fingerprint
from ...domain.jury1_flow import (
    ContraryGateDecision,
    ExplanationEvidenceDecision,
    FullSupportGateDecision,
    SupportGateDecision,
    TopicGateDecision,
)
from ...domain.replies import (
    ContraryGateAnswer,
    ExplanationEvidenceAnswer,
    FullSupportGateAnswer,
    Jury2Answer,
    SupportGateAnswer,
    TopicGateAnswer,
)
from ...domain.types import Jury2Decision, LogicalRequest


CompletedAnswer: TypeAlias = (
    SupportGateAnswer
    | FullSupportGateAnswer
    | ContraryGateAnswer
    | TopicGateAnswer
    | ExplanationEvidenceAnswer
    | Jury2Answer
)


def answer_hash(
    answer: CompletedAnswer,
    *,
    version: str = FINGERPRINT_VERSION,
) -> str:
    return answer_fingerprint(durable_answer(answer), version=version)


def durable_answer(answer: CompletedAnswer) -> dict[str, Any]:
    """Return the canonical SQLite-boundary representation of one answer."""
    payload = asdict(answer)
    # Keep the policy routing fact only when it applies. Ordinary answers retain
    # their pre-policy canonical shape, while an abstention survives crash/replay.
    if not payload["decision"].get("provider_uncertain", False):
        payload["decision"].pop("provider_uncertain", None)
    if isinstance(answer, ExplanationEvidenceAnswer):
        payload["decision"]["evidence_span_ids"] = list(
            answer.decision.evidence_span_ids
        )
    return payload


def persisted_answer(
    request: LogicalRequest,
    record: dict[str, Any],
    *,
    version: str = FINGERPRINT_VERSION,
) -> CompletedAnswer:
    try:
        raw = record["answer"]
        answer = _decode_stage(request.stage, raw, raw["decision"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FatalDispatchError("durable completed answer is invalid") from exc
    if (
        answer.logical_request_id != request.logical_request_id
        or answer.payload_fingerprint != request.payload_fingerprint
        or record.get("answer_hash") != answer_hash(answer, version=version)
    ):
        raise FatalDispatchError("durable completed answer is invalid")
    return answer


def _decode_stage(
    stage: str,
    raw: dict[str, Any],
    decision: dict[str, Any],
) -> CompletedAnswer:
    identity = (
        raw["logical_request_id"],
        raw["payload_fingerprint"],
    )
    if stage == "support_gate":
        return SupportGateAnswer(
            *identity,
            SupportGateDecision(decision["source_supports_any"], decision.get("provider_confidence"), decision.get("provider_uncertain", False)),
        )
    if stage == "full_support_gate":
        return FullSupportGateAnswer(
            *identity,
            FullSupportGateDecision(decision["source_supports_fully"], decision.get("provider_confidence"), decision.get("provider_uncertain", False)),
        )
    if stage == "contrary_gate":
        return ContraryGateAnswer(
            *identity,
            ContraryGateDecision(decision["paper_demonstrates_opposite"], decision.get("provider_confidence"), decision.get("provider_uncertain", False)),
        )
    if stage == "topic_gate":
        return TopicGateAnswer(
            *identity,
            TopicGateDecision(decision["same_specific_subject"], decision.get("provider_confidence"), decision.get("provider_uncertain", False)),
        )
    if stage == "explanation_evidence":
        return ExplanationEvidenceAnswer(
            *identity,
            ExplanationEvidenceDecision(
                decision["reason"],
                decision["supported_content"],
                decision["unsupported_content"],
                decision["incompatible_proposition"],
                tuple(decision["evidence_span_ids"]),
                decision["non_decidable_reason"],
                decision.get("provider_confidence"),
                decision.get("provider_uncertain", False),
            ),
        )
    if stage == "jury2":
        return Jury2Answer(
            *identity,
            Jury2Decision(
                decision["passages_fit_claim"],
                decision["reason"],
                decision.get("provider_confidence"),
                decision.get("provider_uncertain", False),
            ),
        )
    raise ValueError("durable answer stage is invalid")
