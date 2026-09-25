#!/usr/bin/env python3
# core/resolve/resolve_types.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Typed output contracts for the resolver transition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal


IdentityClass = Literal["global", "authority_local", "none"]
StageName = Literal[
    "declared_present",
    "declared_cache_lookup",
    "declared_validation",
    "strong_discovery",
    "discovered_validation",
    "strong_confirmed",
    "weak_aggregation",
    "retraction_check",
    "final_resolution",
]
RetractionStatus = Literal["not_retracted", "retracted", "unknown"]
RetractionCheckedVia = Literal["retraction_watch", "authority_metadata", "none"]


class IdentityState(str, Enum):
    RESOLVED_STRONG_DECLARED = "resolved_strong_declared"
    RESOLVED_STRONG_DISCOVERED = "resolved_strong_discovered"
    DECLARED_IDENTIFIER_FAILED = "declared_identifier_failed"
    WEAKLY_CORROBORATED = "weakly_corroborated"
    UNVERIFIED = "unverified"
    FABRICATED = "fabricated"


@dataclass(frozen=True)
class Identity:
    scheme: str
    value: str
    is_strong: bool
    class_: IdentityClass

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "value": self.value,
            "is_strong": self.is_strong,
            "class_": self.class_,
        }


@dataclass(frozen=True)
class EvidencePayload:
    fulltext_links: list[dict[str, Any]]
    auxiliary_fulltext_links: list[dict[str, Any]]
    oa_status: str
    fulltext_exists: bool | str
    abstract: str | None
    matched_title: str | None
    work_type: str | None
    via: str | None
    fulltext_availability: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "fulltext_links": self.fulltext_links,
            "auxiliary_fulltext_links": self.auxiliary_fulltext_links,
            "oa_status": self.oa_status,
            "fulltext_exists": self.fulltext_exists,
            "abstract": self.abstract,
            "matched_title": self.matched_title,
            "work_type": self.work_type,
            "via": self.via,
        }
        if self.fulltext_availability is not None:
            payload["fulltext_availability"] = self.fulltext_availability
        return payload


@dataclass(frozen=True)
class StageRecord:
    stage: StageName
    outcome: str
    detail: dict[str, Any]
    at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "outcome": self.outcome,
            "detail": self.detail,
            "at": self.at,
        }


ResolutionTrace = list[StageRecord]


@dataclass(frozen=True)
class Retraction:
    status: RetractionStatus
    checked_via: RetractionCheckedVia
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked_via": self.checked_via,
            "evidence": self.evidence,
        }
