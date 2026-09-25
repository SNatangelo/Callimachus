# core/verify/claim_evidence/domain/jury1_flow.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure routing and deterministic projection for staged holistic Jury1 facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .jury1_decisions import (
    ContraryGateDecision,
    ExplanationEvidenceDecision,
    FullSupportGateDecision,
    Jury1FlowError,
    SOURCE_NON_DECIDABLE_REASONS,
    SupportGateDecision,
    TopicGateDecision,
)
from .types import Jury1Decision


JURY1_FLOW_STAGES = (
    "support_gate",
    "full_support_gate",
    "contrary_gate",
    "topic_gate",
    "explanation_evidence",
)
Jury1FlowStage = Literal[
    "support_gate",
    "full_support_gate",
    "contrary_gate",
    "topic_gate",
    "explanation_evidence",
]
DeterminedOutcome = Literal[
    "supports", "partial", "contradicts", "related", "off_topic"
]


@dataclass(frozen=True, slots=True)
class MappedJury1Decision:
    decision: Jury1Decision
    source_span_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Jury1FlowEvaluation:
    next_stage: Jury1FlowStage | None
    mapped: MappedJury1Decision | None
    rejection_cause: Literal["stage_disagreement"] | None
    determined_outcome: DeterminedOutcome | None = None


def evaluate_jury1_flow(
    payload: Mapping[str, Any],
    answers: Mapping[str, object],
) -> Jury1FlowEvaluation:
    """Return the next independent call or one deterministic terminal candidate."""
    claim, spans, mode = _projection_inputs(payload)
    if not isinstance(answers, Mapping) or set(answers) - set(JURY1_FLOW_STAGES):
        raise Jury1FlowError("Jury1 flow answers contain an unknown stage")

    support = answers.get("support_gate")
    if support is None:
        _require_answer_keys(answers, set())
        return _pending("support_gate")
    _validate_support(support)
    if getattr(support, "provider_uncertain", False):
        return _non_decidable(mode, "provider_uncertain", None, support.provider_confidence)

    if support.source_supports_any:
        return _support_branch(claim, spans, mode, answers)
    return _no_support_branch(claim, spans, mode, answers)


def _support_branch(
    claim: str,
    spans: Mapping[str, str],
    mode: str,
    answers: Mapping[str, object],
) -> Jury1FlowEvaluation:
    full = answers.get("full_support_gate")
    if full is None:
        _require_answer_keys(answers, {"support_gate"})
        return _pending("full_support_gate")
    _validate_full_support(full)
    if getattr(full, "provider_uncertain", False):
        return _non_decidable(mode, "provider_uncertain", None, full.provider_confidence)
    outcome: DeterminedOutcome = (
        "supports" if full.source_supports_fully else "partial"
    )
    return _explanation_branch(
        claim=claim,
        spans=spans,
        mode=mode,
        answers=answers,
        allowed_before={"support_gate", "full_support_gate"},
        outcome=outcome,
    )


def _no_support_branch(
    claim: str,
    spans: Mapping[str, str],
    mode: str,
    answers: Mapping[str, object],
) -> Jury1FlowEvaluation:
    contrary = answers.get("contrary_gate")
    if contrary is None:
        _require_answer_keys(answers, {"support_gate"})
        return _pending("contrary_gate")
    _validate_contrary(contrary)
    if getattr(contrary, "provider_uncertain", False):
        return _non_decidable(mode, "provider_uncertain", None, contrary.provider_confidence)

    if contrary.paper_demonstrates_opposite:
        return _explanation_branch(
            claim=claim,
            spans=spans,
            mode=mode,
            answers=answers,
            allowed_before={"support_gate", "contrary_gate"},
            outcome="contradicts",
        )

    topic = answers.get("topic_gate")
    if topic is None:
        _require_answer_keys(answers, {"support_gate", "contrary_gate"})
        return _pending("topic_gate")
    _validate_topic(topic)
    if getattr(topic, "provider_uncertain", False):
        return _non_decidable(mode, "provider_uncertain", None, topic.provider_confidence)
    outcome: DeterminedOutcome = (
        "related" if topic.same_specific_subject else "off_topic"
    )
    return _explanation_branch(
        claim=claim,
        spans=spans,
        mode=mode,
        answers=answers,
        allowed_before={"support_gate", "contrary_gate", "topic_gate"},
        outcome=outcome,
    )


def _explanation_branch(
    *,
    claim: str,
    spans: Mapping[str, str],
    mode: str,
    answers: Mapping[str, object],
    allowed_before: set[str],
    outcome: DeterminedOutcome,
) -> Jury1FlowEvaluation:
    value = answers.get("explanation_evidence")
    if value is None:
        _require_answer_keys(answers, allowed_before)
        return _pending("explanation_evidence", determined_outcome=outcome)

    _require_answer_keys(answers, allowed_before | {"explanation_evidence"})
    _validate_explanation(value, spans, outcome)
    if value.non_decidable_reason is not None:
        return _non_decidable(mode, value.non_decidable_reason, value.reason, value.provider_confidence)

    supported_part = (
        value.supported_content
        if outcome in {"supports", "partial"}
        else None
    )
    incompatible = (
        value.incompatible_proposition if outcome == "contradicts" else None
    )
    probability_only = _confidence(value.provider_confidence)
    explanation = None if probability_only else _format_explanation(outcome, value)
    evidence = tuple(spans[span_id] for span_id in value.evidence_span_ids)
    decision = Jury1Decision(
        outcome,
        evidence,
        explanation,
        None if probability_only else supported_part,
        None if probability_only else incompatible,
        None,
        _minimum_confidence(answers),
    )
    return _complete(decision, value.evidence_span_ids)


def _format_explanation(
    outcome: DeterminedOutcome,
    value: ExplanationEvidenceDecision,
) -> str:
    if outcome == "supports":
        return (
            f"Supported: {value.supported_content} "
            f"Reason: {value.reason}"
        )
    if outcome == "partial":
        return (
            f"Supported: {value.supported_content} "
            f"Unsupported: {value.unsupported_content} "
            f"Reason: {value.reason}"
        )
    if outcome == "contradicts":
        return (
            f"Opposite proposition: {value.incompatible_proposition} "
            f"Reason: {value.reason}"
        )
    return value.reason


def _projection_inputs(
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, str], str]:
    expected = {
        "claim",
        "claim_context",
        "citation_marker",
        "source_spans",
        "source_hash",
        "cited_source_mode",
        "task",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise Jury1FlowError("Jury1 flow payload is invalid")
    for name in ("claim", "claim_context", "citation_marker"):
        if not _text(payload[name]):
            raise Jury1FlowError(f"{name} is invalid")
    source_hash = payload["source_hash"]
    if (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or any(character not in "0123456789abcdef" for character in source_hash)
    ):
        raise Jury1FlowError("source_hash is invalid")
    mode = payload["cited_source_mode"]
    if mode not in {"full_text", "extractive_rag"}:
        raise Jury1FlowError("source mode is invalid")
    task = payload["task"]
    if (
        not isinstance(task, Mapping)
        or set(task) != {"task_id", "instructions"}
        or task["task_id"] != "support_gate"
        or not _text(task["instructions"])
    ):
        raise Jury1FlowError("base Jury1 task is invalid")
    return payload["claim"], _source_spans(payload["source_spans"]), mode


def _source_spans(value: object) -> dict[str, str]:
    if not isinstance(value, list) or not value:
        raise Jury1FlowError("source span payload is invalid")
    result: dict[str, str] = {}
    for item in value:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"span_id", "text"}
            or not _text(item["span_id"])
            or not _text(item["text"])
            or item["span_id"] in result
        ):
            raise Jury1FlowError("source span payload is invalid")
        result[item["span_id"]] = item["text"]
    return result


def _validate_support(value: object) -> None:
    if (
        not isinstance(value, SupportGateDecision)
        or type(value.source_supports_any) is not bool
        or not _valid_provider_uncertainty(value)
    ):
        raise Jury1FlowError("support gate answer is invalid")


def _validate_full_support(value: object) -> None:
    if (
        not isinstance(value, FullSupportGateDecision)
        or type(value.source_supports_fully) is not bool
        or not _valid_provider_uncertainty(value)
    ):
        raise Jury1FlowError("full-support gate answer is invalid")


def _validate_contrary(value: object) -> None:
    if (
        not isinstance(value, ContraryGateDecision)
        or type(value.paper_demonstrates_opposite) is not bool
        or not _valid_provider_uncertainty(value)
    ):
        raise Jury1FlowError("contrary gate answer is invalid")


def _validate_topic(value: object) -> None:
    if (
        not isinstance(value, TopicGateDecision)
        or type(value.same_specific_subject) is not bool
        or not _valid_provider_uncertainty(value)
    ):
        raise Jury1FlowError("topic gate answer is invalid")


def _validate_explanation(
    value: object,
    spans: Mapping[str, str],
    outcome: DeterminedOutcome,
) -> None:
    if not isinstance(value, ExplanationEvidenceDecision):
        raise Jury1FlowError("explanation/evidence answer is invalid")
    if (
        not _valid_provider_uncertainty(value)
        or value.provider_uncertain
        != (value.non_decidable_reason == "provider_uncertain")
    ):
        raise Jury1FlowError("provider uncertainty is invalid")
    probability_only = _confidence(value.provider_confidence)
    if not probability_only and not _text(value.reason):
        raise Jury1FlowError("explanation/evidence answer is invalid")
    _validate_non_decidable(value.non_decidable_reason)
    if not _span_ids(value.evidence_span_ids, spans):
        raise Jury1FlowError("explanation evidence span IDs are invalid")

    if value.non_decidable_reason is not None:
        if (
            value.supported_content is not None
            or value.unsupported_content is not None
            or value.incompatible_proposition is not None
            or value.evidence_span_ids
        ):
            raise Jury1FlowError("non-decidable explanation must be empty")
        return

    evidence_required = outcome in {"supports", "partial", "contradicts"}
    if evidence_required != bool(value.evidence_span_ids):
        raise Jury1FlowError("explanation evidence does not match outcome")
    if len(value.evidence_span_ids) > 6:
        raise Jury1FlowError("explanation evidence exceeds six spans")

    if outcome in {"supports", "partial"}:
        if not probability_only and not _text(value.supported_content):
            raise Jury1FlowError("supported explanation is incomplete")
        if outcome == "partial":
            if not probability_only and not _text(value.unsupported_content):
                raise Jury1FlowError("partial explanation is incomplete")
        elif value.unsupported_content is not None:
            raise Jury1FlowError("unsupported content does not match outcome")
    elif value.supported_content is not None or value.unsupported_content is not None:
        raise Jury1FlowError("support fields do not match outcome")

    if outcome == "contradicts":
        if not probability_only and not _text(value.incompatible_proposition):
            raise Jury1FlowError("contrary explanation is incomplete")
    elif value.incompatible_proposition is not None:
        raise Jury1FlowError("incompatible proposition does not match outcome")


def _span_ids(value: object, spans: Mapping[str, str]) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == len(set(value))
        and all(isinstance(item, str) and item in spans for item in value)
    )


def _valid_provider_uncertainty(value: object) -> bool:
    uncertain = getattr(value, "provider_uncertain", None)
    confidence = getattr(value, "provider_confidence", None)
    return type(uncertain) is bool and (not uncertain or _confidence(confidence))


def _validate_non_decidable(value: object) -> None:
    if value is not None and value not in SOURCE_NON_DECIDABLE_REASONS:
        raise Jury1FlowError("non_decidable_reason is invalid")


def _confidence(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


def _minimum_confidence(answers: Mapping[str, object]) -> float | None:
    values = [getattr(answer, "provider_confidence", None) for answer in answers.values()]
    valid = [float(value) for value in values if _confidence(value)]
    return min(valid) if valid else None


def _require_answer_keys(
    answers: Mapping[str, object],
    expected: set[str],
) -> None:
    if set(answers) != expected:
        raise Jury1FlowError("answers do not match the selected branch")


def _pending(
    stage: Jury1FlowStage,
    *,
    determined_outcome: DeterminedOutcome | None = None,
) -> Jury1FlowEvaluation:
    return Jury1FlowEvaluation(stage, None, None, determined_outcome)


def _complete(
    decision: Jury1Decision,
    span_ids: tuple[str, ...],
) -> Jury1FlowEvaluation:
    return Jury1FlowEvaluation(None, MappedJury1Decision(decision, span_ids), None)


def _non_decidable(
    mode: str,
    reason: str,
    detail: str | None,
    confidence: float | None = None,
) -> Jury1FlowEvaluation:
    _validate_non_decidable(reason)
    if reason == "retrieval_limit" and mode != "extractive_rag":
        raise Jury1FlowError("retrieval_limit requires extractive_rag")
    decision = Jury1Decision(
        "non_decidable",
        (),
        None if _confidence(confidence) else detail,
        None,
        None,
        reason,
        confidence,
    )
    return _complete(decision, ())


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
