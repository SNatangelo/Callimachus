# core/verify/claim_evidence/contracts/jury1_flow.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Frozen contracts for independent holistic Jury1 calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..domain.jury1_flow import (
    ContraryGateDecision,
    ExplanationEvidenceDecision,
    FullSupportGateDecision,
    SOURCE_NON_DECIDABLE_REASONS,
    SupportGateDecision,
    TopicGateDecision,
)
from .prompt_loader import load_jury1_flow_prompt_spec
from .schema import ContractError, exact_object, nonempty_string, unique_nonempty_strings


JURY1_FLOW_PROMPT_SPEC = load_jury1_flow_prompt_spec(
    Path(__file__).with_name("prompts") / "jury1.json",
    "jury1_flow_prompt",
)
JURY1_FLOW_SYSTEM_PROMPT = JURY1_FLOW_PROMPT_SPEC.system_prompt
JURY1_FLOW_TASKS = frozenset(
    task_id for task_id, _ in JURY1_FLOW_PROMPT_SPEC.tasks
)

# Public aliases retained within the current API surface.
JURY1_PROMPT_SPEC = JURY1_FLOW_PROMPT_SPEC
JURY1_SYSTEM_PROMPT = JURY1_FLOW_SYSTEM_PROMPT

_OUTCOMES = frozenset(
    {"supports", "partial", "contradicts", "related", "off_topic"}
)


def build_jury1_flow_payload(
    *,
    task_id: str,
    cited_source_mode: str,
    source_hash: str,
    source_spans: Sequence[Mapping[str, Any]],
    claim: str,
    claim_context: str,
    citation_marker: str,
    determined_outcome: str | None = None,
) -> dict[str, Any]:
    """Build one independently replayable source-first exact-claim payload."""
    if task_id not in JURY1_FLOW_TASKS:
        raise ContractError("Jury1 flow task is invalid")
    if cited_source_mode not in {"full_text", "extractive_rag"}:
        raise ContractError("cited_source_mode is invalid")
    if (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or any(character not in "0123456789abcdef" for character in source_hash)
    ):
        raise ContractError("source_hash is invalid")

    payload: dict[str, Any] = {
        "source_spans": _source_spans(source_spans),
        "source_hash": source_hash,
        "cited_source_mode": cited_source_mode,
        "claim": nonempty_string(claim, "claim"),
        "claim_context": nonempty_string(claim_context, "claim_context"),
        "citation_marker": nonempty_string(citation_marker, "citation_marker"),
        "task": {
            "task_id": task_id,
            "instructions": JURY1_FLOW_PROMPT_SPEC.task_prompt(task_id),
        },
    }
    if task_id == "explanation_evidence":
        if determined_outcome not in _OUTCOMES:
            raise ContractError("determined_outcome is invalid")
        payload["determined_outcome"] = determined_outcome
    elif determined_outcome is not None:
        raise ContractError("determined_outcome is only valid after classification")
    return payload


def build_jury1_flow_prompt(**values: Any) -> tuple[str, str]:
    payload = build_jury1_flow_payload(**values)
    return (
        JURY1_FLOW_SYSTEM_PROMPT,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def parse_support_gate_response(value: Any) -> SupportGateDecision:
    answer = exact_object(value, frozenset({"source_supports_any"}))
    return SupportGateDecision(_boolean(answer["source_supports_any"], "source_supports_any"))


def parse_full_support_gate_response(value: Any) -> FullSupportGateDecision:
    answer = exact_object(value, frozenset({"source_supports_fully"}))
    return FullSupportGateDecision(
        _boolean(answer["source_supports_fully"], "source_supports_fully")
    )


def parse_contrary_gate_response(value: Any) -> ContraryGateDecision:
    answer = exact_object(value, frozenset({"paper_demonstrates_opposite"}))
    return ContraryGateDecision(
        _boolean(answer["paper_demonstrates_opposite"], "paper_demonstrates_opposite")
    )


def parse_topic_gate_response(value: Any) -> TopicGateDecision:
    answer = exact_object(value, frozenset({"same_specific_subject"}))
    return TopicGateDecision(
        _boolean(answer["same_specific_subject"], "same_specific_subject")
    )


def parse_explanation_evidence_response(value: Any) -> ExplanationEvidenceDecision:
    answer = exact_object(
        value,
        frozenset(
            {
                "reason",
                "supported_content",
                "unsupported_content",
                "incompatible_proposition",
                "evidence_span_ids",
                "non_decidable_reason",
            }
        ),
    )
    reason = nonempty_string(answer["reason"], "reason")
    supported = _optional_text(answer["supported_content"], "supported_content")
    unsupported = _optional_text(
        answer["unsupported_content"], "unsupported_content"
    )
    incompatible = _optional_text(
        answer["incompatible_proposition"], "incompatible_proposition"
    )
    evidence = unique_nonempty_strings(
        answer["evidence_span_ids"], "evidence_span_ids"
    )
    if len(evidence) > 6:
        raise ContractError(
            "explanation evidence exceeds six spans",
            code="evidence_cardinality_exceeded",
        )
    non_decidable = _non_decidable_reason(answer["non_decidable_reason"])
    if non_decidable is not None and (
        supported is not None
        or unsupported is not None
        or incompatible is not None
        or evidence
    ):
        raise ContractError(
            "non-decidable explanation must not contain semantic fields or evidence",
            code="evidence_non_decidable_nonempty",
        )
    return ExplanationEvidenceDecision(
        reason,
        supported,
        unsupported,
        incompatible,
        evidence,
        non_decidable,
    )


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ContractError(
            f"{field} must be boolean",
            code="response_boolean_invalid",
        )
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return nonempty_string(value, field)


def _source_spans(
    value: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ContractError("source_spans must be non-empty")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"span_id", "text"}:
            raise ContractError("source span fields must be exact")
        span_id = nonempty_string(item["span_id"], "span_id")
        text = nonempty_string(item["text"], "source span text")
        if "\x00" in text:
            raise ContractError("source span text must not contain NUL")
        if span_id in seen:
            raise ContractError("source span IDs must be unique")
        seen.add(span_id)
        result.append({"span_id": span_id, "text": text})
    return result


def _non_decidable_reason(value: Any) -> str | None:
    if value is None:
        return None
    if value not in SOURCE_NON_DECIDABLE_REASONS:
        raise ContractError(
            "non_decidable_reason is invalid",
            code="non_decidable_reason_invalid",
        )
    return value
