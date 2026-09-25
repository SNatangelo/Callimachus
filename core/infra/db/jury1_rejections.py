# core/infra/db/jury1_rejections.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only persistence primitives for deterministic Jury1 rejections."""

from __future__ import annotations

import sqlite3
from typing import Any

from .llm_dispatches import require_text

STATE_CAUSES = frozenset({
    "schema_invalid",
    "evidence_cardinality_invalid",
    "grounding_invalid",
    "provenance_invalid",
    "stage_disagreement",
})
GROUNDING_CAUSES = frozenset({
    "grounding/empty_quotation",
    "grounding/ambiguous_exact_raw_match",
    "grounding/ambiguous_normalized_match",
    "grounding/ungrounded_omission_marker",
    "grounding/no_guarded_fuzzy_match",
    "grounding/ambiguous_guarded_fuzzy_match",
    "grounding/malformed_locator_result",
    "grounding/invalid_locator_range",
    "grounding/exact_locator_mismatch",
    "grounding/normalized_locator_mismatch",
    "grounding/fuzzy_locator_policy_violation",
    "grounding/decision_or_context_invalid",
    "grounding/duplicate_evidence_range",
    "grounding/duplicate_evidence_text",
    "grounding/evidence_outside_context",
})
CAUSES = STATE_CAUSES | GROUNDING_CAUSES


def validate_input(
    *, event_id: object, request: Any, state_cause: object, cause: object
) -> None:
    require_text("Jury1 rejection event id", event_id)
    if request is None or request["stage"] not in {
        "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
        "explanation_evidence",
    }:
        raise ValueError("Jury1 rejection request stage is invalid")
    if state_cause not in STATE_CAUSES:
        raise ValueError("Jury1 rejection state cause is invalid")
    if cause not in CAUSES:
        raise ValueError("Jury1 rejection cause is invalid")
    if state_cause == "grounding_invalid" and not str(cause).startswith("grounding/"):
        raise ValueError("grounding rejection cause is invalid")
    if state_cause != "grounding_invalid" and cause != state_cause:
        raise ValueError("Jury1 rejection cause does not match state cause")


def append(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    logical_request_id: str,
    state_cause: str,
    cause: str,
    created_at: str,
) -> None:
    """Append an immutable rejection or accept an exact replay."""
    row = conn.execute(
        "SELECT * FROM jury1_rejection_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    expected = (logical_request_id, state_cause, cause)
    if row is not None:
        actual = tuple(row[name] for name in ("logical_request_id", "state_cause", "cause"))
        if actual != expected:
            raise ValueError("Jury1 rejection replay differs from immutable record")
        return
    conn.execute(
        "INSERT INTO jury1_rejection_events("
        "event_id, logical_request_id, state_cause, cause, created_at"
        ") VALUES(?,?,?,?,?)",
        (event_id, *expected, created_at),
    )


def append_for_request(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    request: Any,
    state_cause: str,
    cause: str,
    created_at: str,
) -> None:
    """Validate linkage and append one rejection, rejecting candidate conflicts."""
    validate_input(event_id=event_id, request=request, state_cause=state_cause, cause=cause)
    logical_request_id = request["logical_request_id"]
    candidate = conn.execute(
        "SELECT 1 FROM verification_candidates "
        "WHERE origin_logical_request_id = ?",
        (logical_request_id,),
    ).fetchone()
    if candidate is not None:
        raise ValueError("Jury1 rejection conflicts with persisted candidate")
    append(
        conn,
        event_id=event_id,
        logical_request_id=logical_request_id,
        state_cause=state_cause,
        cause=cause,
        created_at=created_at,
    )


def append_for_logical_request(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    logical_request_id: str,
    state_cause: str,
    cause: str,
    created_at: str,
) -> None:
    request = conn.execute(
        "SELECT * FROM llm_logical_requests WHERE logical_request_id = ?",
        (logical_request_id,),
    ).fetchone()
    append_for_request(
        conn, event_id=event_id, request=request, state_cause=state_cause,
        cause=cause, created_at=created_at,
    )


def require_no_rejection(
    conn: sqlite3.Connection, logical_request_id: object
) -> None:
    row = conn.execute(
        "SELECT 1 FROM jury1_rejection_events WHERE logical_request_id = ?",
        (logical_request_id,),
    ).fetchone()
    if row is not None:
        raise ValueError("candidate conflicts with persisted Jury1 rejection")


def controller_event_fact(
    *, kind: str, request_id: str | None, candidate: object, cause: str | None,
) -> tuple[str, str, str] | None:
    rejection_event = (
        kind.startswith("jury1_rejected:")
        or kind in {"exhausted:jury1_guard", "candidate_cap_exhausted"}
    )
    if not rejection_event:
        return None
    if request_id is None or candidate is not None or cause not in CAUSES:
        raise ValueError("Jury1 rejection is missing request or closed cause")
    state_cause = (
        kind.split(":", 1)[1]
        if kind.startswith("jury1_rejected:")
        else "grounding_invalid"
        if cause.startswith("grounding/")
        else cause
    )
    return request_id, state_cause, cause


def validate_resume(
    conn: sqlite3.Connection, requests: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM jury1_rejection_events ORDER BY created_at, event_id"
        )
    ]
    for row in rows:
        validate_input(
            event_id=row["event_id"],
            request=requests.get(row["logical_request_id"]),
            state_cause=row["state_cause"],
            cause=row["cause"],
        )
        candidate = conn.execute(
            "SELECT 1 FROM verification_candidates "
            "WHERE origin_logical_request_id = ?",
            (row["logical_request_id"],),
        ).fetchone()
        if candidate is not None:
            raise ValueError("persisted Jury1 rejection conflicts with candidate")
    return rows
