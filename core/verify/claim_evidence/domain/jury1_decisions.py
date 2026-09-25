# core/verify/claim_evidence/domain/jury1_decisions.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Validated value objects used by the staged holistic Jury1 router."""

from __future__ import annotations

from dataclasses import dataclass


SOURCE_NON_DECIDABLE_REASONS = frozenset(
    {"material_limit", "no_consensus", "verification_unavailable", "retrieval_limit", "provider_uncertain"}
)


class Jury1FlowError(ValueError):
    """Persisted model-produced flow facts are internally inconsistent."""


@dataclass(frozen=True, slots=True)
class SupportGateDecision:
    source_supports_any: bool
    provider_confidence: float | None = None
    provider_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class FullSupportGateDecision:
    source_supports_fully: bool
    provider_confidence: float | None = None
    provider_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class ContraryGateDecision:
    paper_demonstrates_opposite: bool
    provider_confidence: float | None = None
    provider_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class TopicGateDecision:
    same_specific_subject: bool
    provider_confidence: float | None = None
    provider_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class ExplanationEvidenceDecision:
    reason: str | None
    supported_content: str | None
    unsupported_content: str | None
    incompatible_proposition: str | None
    evidence_span_ids: tuple[str, ...]
    non_decidable_reason: str | None
    provider_confidence: float | None = None
    provider_uncertain: bool = False
