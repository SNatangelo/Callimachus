#!/usr/bin/env python3
# core/infra/db/models.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Typed record models for the DB-native run store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    created_at: str
    updated_at: str
    status: str
    phase: str
    input_path: str
    input_sha256: str
    accuracy: str
    style: str | None
    model_id: str | None
    http_profile: str | None
    challenge_mode: str | None
    fixture_fingerprint: str
    parent_run_id: str | None = None
    run_origin: str = "fresh"


@dataclass(frozen=True)
class ExecutionAssuranceRecord:
    """Persistent assurance posture for the lifetime of a run."""

    initial_origin: str
    protection: str
    agent_identity: str | None = None
    failure_reason: str | None = None
    acknowledged_at: str | None = None


@dataclass(frozen=True)
class RunSessionRecord:
    session_id: str
    started_at: str
    heartbeat_at: str
    ended_at: str | None
    status: str
    pid: int | None
    host: str | None


@dataclass(frozen=True)
class PhaseEventRecord:
    event_id: int
    phase: str
    event_type: str
    created_at: str
    session_id: str | None
    payload: JsonDict | None


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    sentence: str
    context_window: str | None
    marker_raw: str | None
    claim_scope: str | None
    parser_sentence_index: int | None
    marker_group_index: int | None
    marker_group_count: int | None
    marker_start: int | None
    marker_end: int | None
    structural_provenance: JsonDict | None
    claim_order: int


@dataclass(frozen=True)
class ReferenceRecord:
    ref_id: str
    ref_number: int
    raw_entry: str
    title: str | None
    doi: str | None
    pmid: str | None
    isbn: str | None
    url: str | None
    year: int | None
    ay_surname: str | None
    ay_year: int | None
    ay_suffix: str | None
    source_type: str | None
    source_kind: str | None
    indexability: str | None
    source_type_confidence: str | None
    cited_coordinates: tuple[JsonDict, ...] = ()


@dataclass(frozen=True)
class CitationRecord:
    citation_id: int
    claim_id: str
    ref_id: str
    ref_number: int


@dataclass(frozen=True)
class ResolveResultRecord:
    ref_id: str
    status: str
    via: str | None
    matched_title: str | None
    abstract: str | None
    abstract_via: str | None
    retracted: bool
    fulltext_exists: bool | str | None
    oa_status: str | None
    work_type: str | None
    resolution_basis: str | None
    existence_confidence: str | None
    reason: str | None
    reference_status_tag: str | None
    fabrication_risk: str | None
    resolved_identifier: JsonDict | None
    tag_reason: str | None
    fulltext_links: JsonDict | list[JsonDict] | None
    auxiliary_fulltext_links: JsonDict | list[JsonDict] | None
    evidence_profile: JsonDict | None
    attempts: JsonDict | list[JsonDict] | None
    trace: JsonDict | list[JsonDict] | None
    resolver_attempts: JsonDict | list[JsonDict] | None
    identifier_validations: JsonDict | list[JsonDict] | None
    retraction_checks: JsonDict | list[JsonDict] | None
    weak_corroboration_events: JsonDict | list[JsonDict] | None
    updated_at: str


@dataclass(frozen=True)
class FetchAttemptRecord:
    fetch_attempt_id: int
    ref_id: str
    method: str | None
    url: str | None
    kind: str | None
    final_url: str | None
    status_code: int | None
    content_type: str | None
    outcome: str
    reason: str | None
    challenge_blocked: bool
    paywalled: bool
    trace: JsonDict | None
    origin: str | None
    created_at: str


@dataclass(frozen=True)
class SourceTextRecord:
    source_text_id: str
    ref_id: str
    identity_key: str
    tier: str
    origin: str
    stored_path: str
    sha256: str
    char_count: int
    source_ref: str | None
    mapping: str | None
    match_signal: str | None
    match_score: float | None
    identity_status: str | None
    identity_note: str | None
    content_version: str | None
    provenance_relation: str | None
    supplied_by: str | None
    supplied_via: str | None
    file_format: str | None
    extraction_flags: list[str]
    extraction_method: str | None
    recorded_at: str


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    slot: str
    ref_id: str | None
    claim_id: str | None
    scope: str | None
    status: str
    task_kind: str
    generation: int
    task_payload: JsonDict
    created_at: str
    answered_at: str | None
    applied_at: str | None


@dataclass(frozen=True)
class TaskAnswerRecord:
    answer_id: str
    task_id: str
    actor_type: str
    generation: int
    answer_kind: str
    raw_payload: JsonDict
    submitted_at: str
    accepted_for_processing: bool
