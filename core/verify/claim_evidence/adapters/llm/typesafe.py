# core/verify/claim_evidence/adapters/llm/typesafe.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Strict decoder for TypeSafe's probability-only SystemOne replies."""
from __future__ import annotations

import json
import math
from typing import Any

from ...contracts.schema import ContractError
from ...domain.jury1_decisions import (ContraryGateDecision, ExplanationEvidenceDecision,
    FullSupportGateDecision, SupportGateDecision, TopicGateDecision)
from ...domain.types import Jury2Decision


def decode(stage: str, raw: str, payload: bytes, minimum: float, model: str) -> object:
    """Decode a TypeSafe reply.

    ``minimum`` is a policy input, deliberately not a wire-contract rule.  A
    syntactically valid below-threshold reply is still an observed provider
    answer and must reach the audit/policy boundary rather than being rewritten
    as a protocol failure.
    """
    try:
        body = json.loads(raw)
        answers = body["answers"]
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ContractError("response_fields_invalid") from exc
    if not isinstance(body, dict) or body.get("model") != model or not isinstance(answers, dict) or not answers:
        raise ContractError("response_fields_invalid")
    request = _request_payload(payload)
    if stage == "explanation_evidence" and request.get("determined_outcome") in {
        "supports", "partial", "contradicts",
    }:
        return _spans(answers, request, minimum)
    if set(answers) != {"decision"}:
        raise ContractError("response_fields_invalid")
    value, confidence = _choice(answers["decision"])
    uncertain = confidence < minimum
    if stage == "support_gate": return SupportGateDecision(value, confidence, uncertain)
    if stage == "full_support_gate": return FullSupportGateDecision(value, confidence, uncertain)
    if stage == "contrary_gate": return ContraryGateDecision(value, confidence, uncertain)
    if stage == "topic_gate": return TopicGateDecision(value, confidence, uncertain)
    if stage == "jury2": return Jury2Decision(value, None, confidence, uncertain)
    if stage == "explanation_evidence":
        return ExplanationEvidenceDecision(
            None, None, None, None, (), "provider_uncertain" if uncertain else (None if value else "no_consensus"), confidence, uncertain
        )
    raise ContractError("response_fields_invalid")


def _choice(answer: Any) -> tuple[bool, float]:
    if not isinstance(answer, dict) or set(answer) != {"type", "choice", "probabilities", "confidence"} or answer["type"] != "choice":
        raise ContractError("response_fields_invalid")
    if answer["choice"] not in {"true", "false"}:
        raise ContractError("response_fields_invalid")
    probs = answer["probabilities"]
    if not isinstance(probs, dict) or set(probs) != {"true", "false"}:
        raise ContractError("response_fields_invalid")
    for value in (*probs.values(), answer["confidence"]):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ContractError("response_fields_invalid")
    if not math.isclose(
        sum(float(value) for value in probs.values()),
        1.0,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ContractError("response_fields_invalid")
    if float(probs[answer["choice"]]) != max(float(value) for value in probs.values()):
        raise ContractError("response_fields_invalid")
    return answer["choice"] == "true", float(answer["confidence"])


def _request_payload(payload: bytes) -> dict[str, Any]:
    try:
        request = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("response_fields_invalid") from exc
    if not isinstance(request, dict):
        raise ContractError("response_fields_invalid")
    return request


def _spans(
    answers: dict[str, Any], request: dict[str, Any], minimum: float,
) -> ExplanationEvidenceDecision:
    spans = request.get("source_spans")
    if not isinstance(spans, list) or set(answers) != {row.get("span_id") for row in spans}:
        raise ContractError("response_fields_invalid")
    selected: list[tuple[float, str, float]] = []
    confidences: list[float] = []
    ambiguous = False
    for span in spans:
        answer = answers[span["span_id"]]
        if not isinstance(answer, dict) or set(answer) != {"type", "noul"} or answer["type"] != "noul":
            raise ContractError("response_fields_invalid")
        p = answer["noul"]
        if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ContractError("response_fields_invalid")
        certainty = max(float(p), 1 - float(p))
        confidences.append(certainty)
        ambiguous = ambiguous or certainty < minimum
        # Ambiguous spans are ordinary observations.  Exclude them from the
        # proposed evidence set while retaining their result in the completed
        # dispatch rather than rejecting the whole provider response.
        if (
            certainty >= minimum
            and p >= .5
            and isinstance(span, dict)
            and isinstance(span.get("span_id"), str)
        ):
            selected.append((float(p), span["span_id"], certainty))
    ids = tuple(item[1] for item in sorted(selected, reverse=True)[:6])
    uncertain = not ids and ambiguous
    reason = None if ids else ("provider_uncertain" if uncertain else "retrieval_limit")
    confidence = min(item[2] for item in selected) if selected else min(confidences)
    return ExplanationEvidenceDecision(
        None, None, None, None, ids, reason, confidence, uncertain,
    )
