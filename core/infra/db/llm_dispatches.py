# core/infra/db/llm_dispatches.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only logical-request and dispatch-ledger primitives."""

from __future__ import annotations

import math
import re
import sqlite3
from typing import Any


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CREDENTIAL_ALIAS_RE = re.compile(
    r"^[A-Za-z0-9_.-]+:(?:[1-9][0-9]*|credentialless)$"
)
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_FAILURE_TECHNICAL_RESULTS = frozenset(
    {
        "protocol_invalid",
        "rate_limited",
        "credential_invalid",
        "lane_unavailable",
        "timeout",
        "transport",
        "provider_failure",
    }
)
_RETRY_CAUSES = frozenset(
    {
        "rate_limited",
        "credential_invalid",
        "lane_unavailable",
        "timeout",
        "transport",
        "provider_failure",
        "lease_expired",
    }
)
_PROTOCOL_ERROR_CODES = frozenset(
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
_SOURCE_NON_DECIDABLE_REASONS = frozenset(
    {"material_limit", "no_consensus", "verification_unavailable", "retrieval_limit", "provider_uncertain"}
)
_REQUEST_FIELDS = (
    "logical_request_id",
    "claim_id",
    "ref_id",
    "scope",
    "candidate_id",
    "candidate_cycle",
    "stage",
    "payload_hash",
    "source_hash",
    "context_hash",
    "retrieval_hash",
    "prompt_hash",
    "model_hash",
    "policy_hash",
)
_TERMINAL_RESULTS = frozenset({"completed", "failed", "abandoned"})
_JURY1_FLOW_STAGES = frozenset(
    {
        "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
        "explanation_evidence",
    }
)
_REQUEST_STAGES = _JURY1_FLOW_STAGES | {"jury2"}


def append_logical_request(
    conn: sqlite3.Connection,
    *,
    request: dict[str, Any],
    created_at: str,
) -> None:
    """Insert one immutable request or accept an exact identity replay."""
    values = (
        request["logical_request_id"],
        request["claim_id"],
        request["ref_id"],
        request.get("scope", ""),
        request.get("candidate_id"),
        request["candidate_cycle"],
        request["stage"],
        request["payload_hash"],
        request["source_hash"],
        request["context_hash"],
        request["retrieval_hash"],
        request["prompt_hash"],
        request["model_hash"],
        request["policy_hash"],
        created_at,
    )
    row = conn.execute(
        "SELECT * FROM llm_logical_requests WHERE logical_request_id = ?",
        (values[0],),
    ).fetchone()
    if row is not None:
        if tuple(row[name] for name in _REQUEST_FIELDS) != values[:-1]:
            raise ValueError("logical request replay differs from immutable record")
        if _decode_typed_request(conn, dict(row)) != request["payload"]:
            raise ValueError("logical request replay payload differs from immutable record")
        return
    conn.execute(
        """INSERT INTO llm_logical_requests(
          logical_request_id, claim_id, ref_id, scope, candidate_id,
          candidate_cycle, stage, payload_hash, source_hash, context_hash,
          retrieval_hash, prompt_hash, model_hash, policy_hash, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        values,
    )
    _append_request_payload(conn, request)


def lease_dispatch(
    conn: sqlite3.Connection,
    *,
    attempt: dict[str, Any],
    created_at: str,
) -> None:
    """Append an attempt and atomically acquire its request's sole live lease."""
    request = conn.execute(
        "SELECT payload_hash FROM llm_logical_requests WHERE logical_request_id = ?",
        (attempt["logical_request_id"],),
    ).fetchone()
    if request is None or request["payload_hash"] != attempt["payload_hash"]:
        raise ValueError("dispatch payload hash does not match logical request")
    values = (
        attempt["dispatch_attempt_id"],
        attempt["logical_request_id"],
        attempt["provider_id"],
        attempt["model_id"],
        attempt["credential_id"],
        attempt.get("credential_fingerprint"),
        attempt["lane_id"],
        attempt["credential_cursor"],
        attempt["model_draw_index"],
        attempt["selection_hash"],
        attempt["pacing_hash"],
        attempt.get("prior_global_start_at"),
        attempt.get("prior_model_start_at"),
        attempt.get("next_eligible_at"),
        attempt.get("global_interval_ms", 0),
        attempt.get("model_interval_ms", 0),
        attempt.get("queued_at", created_at),
        created_at,
        created_at,
    )
    try:
        conn.execute(
            "INSERT INTO llm_dispatch_attempts VALUES("
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("dispatch attempt id already exists") from exc
    cursor = conn.execute(
        """
        INSERT INTO llm_dispatch_leases(
          logical_request_id, dispatch_attempt_id, status, updated_at
        ) VALUES(?, ?, 'live', ?)
        ON CONFLICT(logical_request_id) DO UPDATE SET
          dispatch_attempt_id = excluded.dispatch_attempt_id,
          status = 'live',
          updated_at = excluded.updated_at
        WHERE llm_dispatch_leases.status = 'released'
        """,
        (attempt["logical_request_id"], attempt["dispatch_attempt_id"], created_at),
    )
    if cursor.rowcount != 1:
        raise ValueError("logical request already has a live or completed dispatch")
    append_dispatch_event(
        conn,
        event_id=f"{attempt['dispatch_attempt_id']}:leased",
        logical_request_id=attempt["logical_request_id"],
        dispatch_attempt_id=attempt["dispatch_attempt_id"],
        event_type="leased",
        payload={},
        created_at=created_at,
    )


def start_dispatch(
    conn: sqlite3.Connection,
    *,
    logical_request_id: str,
    dispatch_attempt_id: str,
    created_at: str,
) -> None:
    """Record the single provider-visible start for one live attempt."""
    _require_live_lease(conn, logical_request_id, dispatch_attempt_id)
    append_dispatch_event(
        conn,
        event_id=f"{dispatch_attempt_id}:started",
        logical_request_id=logical_request_id,
        dispatch_attempt_id=dispatch_attempt_id,
        event_type="started",
        payload={},
        created_at=created_at,
    )


def finish_dispatch(
    conn: sqlite3.Connection,
    *,
    logical_request_id: str,
    dispatch_attempt_id: str,
    result: str,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    """Append one technical terminal and release/finalize its live lease."""
    if result not in _TERMINAL_RESULTS:
        raise ValueError("invalid dispatch terminal result")
    _require_live_lease(conn, logical_request_id, dispatch_attempt_id)
    if result != "abandoned" and not _event_exists(
        conn, dispatch_attempt_id, "started"
    ):
        raise ValueError("provider-visible dispatch was not started")
    append_dispatch_event(
        conn,
        event_id=f"{dispatch_attempt_id}:terminal",
        logical_request_id=logical_request_id,
        dispatch_attempt_id=dispatch_attempt_id,
        event_type=result,
        payload=payload,
        created_at=created_at,
    )
    status = "terminal" if result == "completed" else "released"
    cursor = conn.execute(
        """
        UPDATE llm_dispatch_leases
        SET status = ?, updated_at = ?
        WHERE logical_request_id = ? AND dispatch_attempt_id = ? AND status = 'live'
        """,
        (status, created_at, logical_request_id, dispatch_attempt_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("dispatch lease compare-and-set failed")


def append_dispatch_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    logical_request_id: str,
    dispatch_attempt_id: str | None,
    event_type: str,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    """Append an event, treating a byte-identical identity replay as a no-op."""
    _validate_dispatch_event(
        conn,
        event_id=event_id,
        logical_request_id=logical_request_id,
        dispatch_attempt_id=dispatch_attempt_id,
        event_type=event_type,
        payload=payload,
    )
    row = conn.execute(
        "SELECT * FROM llm_dispatch_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    expected = (logical_request_id, dispatch_attempt_id, event_type)
    if row is not None:
        actual = tuple(
            row[name]
            for name in (
                "logical_request_id",
                "dispatch_attempt_id",
                "event_type",
            )
        )
        if actual != expected or decode_event(dict(row), conn=conn)["payload"] != payload:
            raise ValueError("dispatch event replay differs from immutable record")
        return
    conn.execute(
        """
        INSERT INTO llm_dispatch_events(
          event_id, logical_request_id, dispatch_attempt_id,
          event_type, created_at
        ) VALUES(?,?,?,?,?)
        """,
        (event_id, *expected, created_at),
    )
    _append_event_detail(
        conn,
        event_id,
        logical_request_id,
        dispatch_attempt_id,
        event_type,
        payload,
    )


def _require_live_lease(
    conn: sqlite3.Connection,
    logical_request_id: str,
    dispatch_attempt_id: str,
) -> None:
    row = conn.execute(
        """
        SELECT status, dispatch_attempt_id
        FROM llm_dispatch_leases WHERE logical_request_id = ?
        """,
        (logical_request_id,),
    ).fetchone()
    if (
        row is None
        or row["status"] != "live"
        or row["dispatch_attempt_id"] != dispatch_attempt_id
    ):
        raise ValueError("dispatch lease is not live")


def _event_exists(
    conn: sqlite3.Connection,
    dispatch_attempt_id: str,
    event_type: str,
) -> bool:
    return (
        conn.execute(
            """
            SELECT 1 FROM llm_dispatch_events
            WHERE dispatch_attempt_id = ? AND event_type = ?
            """,
            (dispatch_attempt_id, event_type),
        ).fetchone()
        is not None
    )


def validate_request_shape(request: dict[str, Any]) -> None:
    for name in ("logical_request_id", "claim_id"):
        require_text(name, request.get(name))
    stage = request.get("stage")
    if stage not in _REQUEST_STAGES:
        raise ValueError("invalid jury stage")
    require_text("ref_id", request.get("ref_id"))
    if not isinstance(request.get("scope", ""), str):
        raise ValueError("scope must be text")
    require_positive_int("candidate_cycle", request.get("candidate_cycle"))
    candidate_id = request.get("candidate_id")
    if (stage in _JURY1_FLOW_STAGES and candidate_id is not None) or (
        stage == "jury2" and not isinstance(candidate_id, str)
    ):
        raise ValueError("logical request candidate linkage is invalid")
    if stage == "jury2":
        require_text("candidate_id", candidate_id)
    require_hash("payload_hash", request.get("payload_hash"))
    for name in ("source_hash", "context_hash", "retrieval_hash"):
        require_hash(name, request.get(name))
    for name in ("prompt_hash", "model_hash", "policy_hash"):
        require_hash(name, request.get(name))
    _validate_request_payload(stage, request.get("payload"))
    if (
        stage in _JURY1_FLOW_STAGES
        and request["payload"]["source_hash"] != request["source_hash"]
    ):
        raise ValueError("Jury1 payload source hash differs from request provenance")


def _validate_request_payload(stage: str, payload: object) -> None:
    if not isinstance(payload, dict):
        raise ValueError("logical request payload must be an object")
    if stage in _JURY1_FLOW_STAGES:
        explanation = stage == "explanation_evidence"
        expected = {
            "claim",
            "claim_context",
            "citation_marker",
            "source_spans",
            "source_hash",
            "cited_source_mode",
            "task",
        } | ({"determined_outcome"} if explanation else set())
        if set(payload) != expected:
            raise ValueError("Jury1 payload shape is invalid")
        for name in ("claim", "claim_context", "citation_marker"):
            require_text(name, payload.get(name))
        require_hash("source_hash", payload.get("source_hash"))
        if payload.get("cited_source_mode") not in {"full_text", "extractive_rag"}:
            raise ValueError("Jury1 source mode is invalid")
        if explanation:
            if payload.get("determined_outcome") not in {
                "supports", "partial", "contradicts", "related", "off_topic",
            }:
                raise ValueError("explanation determined outcome is invalid")
        task = payload.get("task")
        if (
            not isinstance(task, dict)
            or set(task) != {"task_id", "instructions"}
            or task.get("task_id") != stage
            or not isinstance(task.get("instructions"), str)
            or not task["instructions"].strip()
        ):
            raise ValueError("Jury1 task is invalid")
        spans = payload.get("source_spans")
        # SQLite length(TEXT) stops at NUL; mirror the child-table CHECK
        # so an invalid request fails before persistence.
        if (
            not isinstance(spans, list)
            or not spans
            or any(
                not isinstance(item, dict)
                or set(item) != {"span_id", "text"}
                or not isinstance(item["span_id"], str)
                or not item["span_id"].strip()
                or not isinstance(item["text"], str)
                or not item["text"].strip()
                or not item["text"].strip(" ").split("\x00", 1)[0]
                for item in spans
            )
            or len({item["span_id"] for item in spans}) != len(spans)
        ):
            raise ValueError("Jury1 source spans are invalid")
        return

    if stage != "jury2" or set(payload) != {
        "asserted_relation", "passage_subject", "selected_passages",
    }:
        raise ValueError("Jury2 payload shape is invalid")
    if payload.get("asserted_relation") not in {"basis", "contrary"}:
        raise ValueError("Jury2 asserted_relation is invalid")
    require_text("passage_subject", payload.get("passage_subject"))
    passages = payload.get("selected_passages")
    if (
        not isinstance(passages, list)
        or not passages
        or any(
            not isinstance(item, dict)
            or set(item) != {"span_id", "text"}
            or not isinstance(item["span_id"], str)
            or not item["span_id"].strip()
            or not isinstance(item["text"], str)
            or not item["text"].strip()
            or not item["text"].strip(" ").split("\x00", 1)[0]
            for item in passages
        )
        or len({item["span_id"] for item in passages}) != len(passages)
        or len({item["text"] for item in passages}) != len(passages)
    ):
        raise ValueError("Jury2 selected_passages are invalid")
def _append_request_payload(conn: sqlite3.Connection, request: dict[str, Any]) -> None:
    payload = request["payload"]
    identifier = request["logical_request_id"]
    if request["stage"] in _JURY1_FLOW_STAGES:
        conn.execute(
            "INSERT INTO llm_jury1_request_payloads VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                payload["claim"],
                payload["claim_context"],
                payload["citation_marker"],
                payload["source_hash"],
                payload["cited_source_mode"],
                payload.get("determined_outcome"),
                payload["task"]["task_id"],
                payload["task"]["instructions"],
                len(payload["source_spans"]),
            ),
        )
        conn.executemany(
            "INSERT INTO llm_jury1_request_source_spans VALUES(?,?,?,?)",
            (
                (identifier, index, item["span_id"], item["text"])
                for index, item in enumerate(payload["source_spans"])
            ),
        )
        return
    conn.execute(
        "INSERT INTO llm_jury2_request_payloads VALUES(?,?,?,?)",
        (
            identifier,
            payload["asserted_relation"],
            payload["passage_subject"],
            len(payload["selected_passages"]),
        ),
    )
    conn.executemany(
        "INSERT INTO llm_jury2_request_passages VALUES(?,?,?,?)",
        (
            (identifier, index, item["span_id"], item["text"])
            for index, item in enumerate(payload["selected_passages"])
        ),
    )
def validate_dispatch_shape(attempt: dict[str, Any]) -> None:
    for name in (
        "dispatch_attempt_id",
        "logical_request_id",
        "provider_id",
        "model_id",
        "lane_id",
    ):
        require_text(name, attempt.get(name))
    for name in ("payload_hash", "selection_hash", "pacing_hash"):
        require_hash(name, attempt.get(name))
    require_nonnegative_int("credential_cursor", attempt.get("credential_cursor"))
    require_nonnegative_int("model_draw_index", attempt.get("model_draw_index"))
    require_nonnegative_int(
        "global_interval_ms", attempt.get("global_interval_ms", 0)
    )
    require_nonnegative_int(
        "model_interval_ms", attempt.get("model_interval_ms", 0)
    )
    alias = attempt.get("credential_id")
    fingerprint = attempt.get("credential_fingerprint")
    if not isinstance(alias, str) or _CREDENTIAL_ALIAS_RE.fullmatch(alias) is None:
        raise ValueError("credential_id must be a non-secret configured alias")
    if alias.endswith(":credentialless"):
        if fingerprint is not None:
            raise ValueError("credentialless lane forbids a fingerprint")
    else:
        require_hash("credential_fingerprint", fingerprint)


def dispatch_terminal_payload(
    *,
    result: str,
    technical_result: str,
    http_status: int | None,
    latency_ms: float | None,
    retry_cause: str | None,
    answer_hash: str | None,
    retry_after_seconds: int | None = None,
    protocol_error_code: str | None = None,
    response_hash: str | None = None,
    answer: dict[str, Any] | None = None,
    scheduler_control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if result not in _TERMINAL_RESULTS:
        raise ValueError("invalid dispatch terminal result")
    if not isinstance(technical_result, str) or not technical_result.strip():
        raise ValueError("technical_result is required")
    if http_status is not None and (
        isinstance(http_status, bool)
        or not isinstance(http_status, int)
        or not 100 <= http_status <= 599
    ):
        raise ValueError("http_status is invalid")
    if result == "abandoned":
        if latency_ms is not None:
            raise ValueError("abandoned latency_ms is invalid")
    elif (
        isinstance(latency_ms, bool)
        or not isinstance(latency_ms, (int, float))
        or not math.isfinite(latency_ms)
        or latency_ms < 0
    ):
        raise ValueError("latency_ms is invalid")
    if result == "completed":
        if (
            technical_result != "answer_received"
            or retry_cause is not None
            or retry_after_seconds is not None
            or not isinstance(answer, dict)
        ):
            raise ValueError("completed terminal semantics are invalid")
        require_hash("answer_hash", answer_hash)
    elif result == "failed":
        if (
            technical_result not in _FAILURE_TECHNICAL_RESULTS
            or retry_cause not in _RETRY_CAUSES - {"lease_expired"}
            or answer_hash is not None
            or answer is not None
        ):
            raise ValueError("failed terminal semantics are invalid")
    elif (
        technical_result != "not_started"
        or retry_cause != "lease_expired"
        or http_status is not None
        or retry_after_seconds is not None
        or answer_hash is not None
        or answer is not None
        or scheduler_control is not None
    ):
        raise ValueError("abandoned terminal semantics are invalid")
    if retry_after_seconds is not None and (
        isinstance(retry_after_seconds, bool)
        or not isinstance(retry_after_seconds, int)
        or retry_after_seconds < 0
        or retry_cause != "rate_limited"
    ):
        raise ValueError("retry_after_seconds is invalid")
    if technical_result == "protocol_invalid":
        if protocol_error_code not in _PROTOCOL_ERROR_CODES:
            raise ValueError("protocol_error_code is invalid")
    elif protocol_error_code is not None or response_hash is not None:
        raise ValueError("protocol diagnostics require protocol_invalid")
    if response_hash is not None:
        require_hash("response_hash", response_hash)
    if scheduler_control is not None and not isinstance(
        scheduler_control, dict
    ):
        raise ValueError("scheduler control payload is invalid")
    payload = {
        "technical_result": technical_result,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "retry_cause": retry_cause,
        "answer_hash": answer_hash,
        "retry_after_seconds": retry_after_seconds,
    }
    if protocol_error_code is not None:
        payload["protocol_error_code"] = protocol_error_code
        payload["response_hash"] = response_hash
    if answer is not None:
        payload["answer"] = answer
    if scheduler_control is not None:
        payload["scheduler_control"] = scheduler_control
    return payload


def _decode_completed_decision(
    conn: sqlite3.Connection,
    event_id: str,
    stage: str,
) -> dict[str, Any]:
    roots = {
        "support_gate": "llm_dispatch_support_gate_answers",
        "full_support_gate": "llm_dispatch_full_support_gate_answers",
        "contrary_gate": "llm_dispatch_contrary_gate_answers",
        "topic_gate": "llm_dispatch_topic_gate_answers",
        "explanation_evidence": "llm_dispatch_explanation_evidence_answers",
        "jury2": "llm_dispatch_jury2_answers",
    }
    expected = roots.get(stage)
    if expected is None:
        raise ValueError("terminal answer stage is invalid")
    present = {
        table
        for table in roots.values()
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE event_id=?", (event_id,)
        ).fetchone() is not None
    }
    if present != {expected}:
        raise ValueError("terminal answer detail cardinality is invalid")

    if stage == "support_gate":
        row = conn.execute(
            "SELECT source_supports_any,provider_confidence,provider_uncertain FROM llm_dispatch_support_gate_answers "
            "WHERE event_id=?",
            (event_id,),
        ).fetchone()
        return {"source_supports_any": bool(row["source_supports_any"]), "provider_confidence": row["provider_confidence"]} | _decoded_uncertainty(row)
    if stage == "full_support_gate":
        row = conn.execute(
            "SELECT source_supports_fully,provider_confidence,provider_uncertain FROM "
            "llm_dispatch_full_support_gate_answers WHERE event_id=?",
            (event_id,),
        ).fetchone()
        return {"source_supports_fully": bool(row["source_supports_fully"]), "provider_confidence": row["provider_confidence"]} | _decoded_uncertainty(row)
    if stage == "contrary_gate":
        row = conn.execute(
            "SELECT paper_demonstrates_opposite,provider_confidence,provider_uncertain FROM "
            "llm_dispatch_contrary_gate_answers WHERE event_id=?",
            (event_id,),
        ).fetchone()
        return {
            "paper_demonstrates_opposite": bool(
                row["paper_demonstrates_opposite"]
            ),
            "provider_confidence": row["provider_confidence"],
        } | _decoded_uncertainty(row)
    if stage == "topic_gate":
        row = conn.execute(
            "SELECT same_specific_subject,provider_confidence,provider_uncertain FROM llm_dispatch_topic_gate_answers "
            "WHERE event_id=?",
            (event_id,),
        ).fetchone()
        return {"same_specific_subject": bool(row["same_specific_subject"]), "provider_confidence": row["provider_confidence"]} | _decoded_uncertainty(row)
    if stage == "explanation_evidence":
        row = conn.execute(
            "SELECT reason,supported_content,unsupported_content,"
            "incompatible_proposition,non_decidable_reason,evidence_span_count,provider_confidence,provider_uncertain "
            "FROM llm_dispatch_explanation_evidence_answers WHERE event_id=?",
            (event_id,),
        ).fetchone()
        span_rows = conn.execute(
            "SELECT span_order,span_id FROM "
            "llm_dispatch_explanation_evidence_spans WHERE event_id=? "
            "ORDER BY span_order",
            (event_id,),
        ).fetchall()
        if len(span_rows) != row["evidence_span_count"]:
            raise ValueError("explanation evidence count is invalid")
        return {
            "reason": row["reason"],
            "supported_content": row["supported_content"],
            "unsupported_content": row["unsupported_content"],
            "incompatible_proposition": row["incompatible_proposition"],
            "evidence_span_ids": [item["span_id"] for item in span_rows],
            "non_decidable_reason": row["non_decidable_reason"],
            "provider_confidence": row["provider_confidence"],
        } | _decoded_uncertainty(row)

    row = conn.execute(
        "SELECT passages_fit_claim,reason,provider_confidence,provider_uncertain FROM llm_dispatch_jury2_answers "
        "WHERE event_id=?",
        (event_id,),
    ).fetchone()
    return {
        "passages_fit_claim": bool(row["passages_fit_claim"]),
        "reason": row["reason"],
        "provider_confidence": row["provider_confidence"],
    } | _decoded_uncertainty(row)


def _decoded_uncertainty(row: sqlite3.Row) -> dict[str, bool]:
    return {"provider_uncertain": True} if bool(row["provider_uncertain"]) else {}


def decode_request(
    row: dict[str, Any], *, conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Decode one logical request from the current typed payload relations."""
    record = dict(row)
    record["payload"] = _decode_typed_request(conn, record)
    validate_request_shape(record)
    return record


def _decode_typed_request(
    conn: sqlite3.Connection,
    record: dict[str, Any],
) -> dict[str, Any]:
    identifier = record["logical_request_id"]
    subtype_tables = {
        "jury1": "llm_jury1_request_payloads",
        "jury2": "llm_jury2_request_payloads",
    }
    present = {
        kind
        for kind, table in subtype_tables.items()
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE logical_request_id=?", (identifier,)
        ).fetchone() is not None
    }
    expected = "jury1" if record["stage"] in _JURY1_FLOW_STAGES else "jury2"
    if present != {expected}:
        raise ValueError("logical request typed payload cardinality is invalid")

    if expected == "jury1":
        row = conn.execute(
            "SELECT claim_text,claim_context,citation_marker,source_hash,"
            "cited_source_mode,determined_outcome,task_id,"
            "task_instructions,source_span_count "
            "FROM llm_jury1_request_payloads WHERE logical_request_id=?",
            (identifier,),
        ).fetchone()
        spans = conn.execute(
            "SELECT span_order,span_id,text "
            "FROM llm_jury1_request_source_spans WHERE logical_request_id=? "
            "ORDER BY span_order",
            (identifier,),
        ).fetchall()
        if (
            row["task_id"] != record["stage"]
            or [item["span_order"] for item in spans]
            != list(range(row["source_span_count"]))
        ):
            raise ValueError("Jury1 typed payload ordering is invalid")
        payload = {
            "source_spans": [
                {"span_id": item["span_id"], "text": item["text"]}
                for item in spans
            ],
            "source_hash": row["source_hash"],
            "cited_source_mode": row["cited_source_mode"],
            "claim": row["claim_text"],
            "claim_context": row["claim_context"],
            "citation_marker": row["citation_marker"],
            "task": {
                "task_id": row["task_id"],
                "instructions": row["task_instructions"],
            },
        }
        if row["task_id"] == "explanation_evidence":
            payload["determined_outcome"] = row["determined_outcome"]
        return payload

    row = conn.execute(
        "SELECT asserted_relation,passage_subject,passage_count "
        "FROM llm_jury2_request_payloads WHERE logical_request_id=?",
        (identifier,),
    ).fetchone()
    passages = conn.execute(
        "SELECT passage_order,span_id,text "
        "FROM llm_jury2_request_passages WHERE logical_request_id=? "
        "ORDER BY passage_order",
        (identifier,),
    ).fetchall()
    if [item["passage_order"] for item in passages] != list(
        range(row["passage_count"])
    ):
        raise ValueError("Jury2 typed payload ordering is invalid")
    return {
        "asserted_relation": row["asserted_relation"],
        "passage_subject": row["passage_subject"],
        "selected_passages": [
            {"span_id": item["span_id"], "text": item["text"]}
            for item in passages
        ],
    }
def decode_event(
    row: dict[str, Any], *, conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Decode one dispatch event from the current typed detail relations."""
    record = dict(row)
    record["payload"] = _decode_event_detail(conn, record)
    return record


def _append_event_detail(
    conn: sqlite3.Connection,
    event_id: str,
    logical_request_id: str,
    dispatch_attempt_id: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Write exactly one closed typed detail variant after validation."""
    if event_type in {"leased", "started"}:
        if payload:
            raise ValueError("dispatch lifecycle payload must be empty")
        return
    if event_type == "pacing_wait":
        _validate_pacing_payload(payload)
        if set(payload) != {"pacing_hash", "prior_global_start_at", "prior_model_start_at", "next_eligible_at", "global_interval_ms", "model_interval_ms"}:
            raise ValueError("pacing payload keys are invalid")
        conn.execute("INSERT INTO llm_dispatch_pacing_details VALUES(?,?,?,?,?,?,?)", (event_id, payload["pacing_hash"], payload["prior_global_start_at"], payload["prior_model_start_at"], payload["next_eligible_at"], payload["global_interval_ms"], payload["model_interval_ms"]))
        return
    _validate_terminal_payload(
        conn,
        event_id,
        logical_request_id,
        dispatch_attempt_id,
        event_type,
        payload,
    )
    base = ("technical_result", "http_status", "latency_ms", "retry_cause", "answer_hash", "retry_after_seconds")
    conn.execute("INSERT INTO llm_dispatch_terminal_details VALUES(?,?,?,?,?,?,?)", (event_id, *(payload[key] for key in base)))
    if payload.get("protocol_error_code") is not None:
        conn.execute(
            "INSERT INTO llm_dispatch_protocol_errors VALUES(?,?,?)",
            (
                event_id,
                payload["protocol_error_code"],
                payload.get("response_hash"),
            ),
        )
    if "answer" in payload:
        answer = payload["answer"]
        conn.execute("INSERT INTO llm_dispatch_terminal_answers VALUES(?,?,?,?)", (event_id, answer["logical_request_id"], answer["payload_fingerprint"], payload["answer_hash"]))
        decision = answer["decision"]
        stage = conn.execute("SELECT stage FROM llm_logical_requests WHERE logical_request_id=?", (answer["logical_request_id"],)).fetchone()["stage"]
        if stage in _JURY1_FLOW_STAGES:
            _append_jury1_flow_answer(conn, event_id, stage, decision)
        elif stage == "jury2":
            conn.execute("INSERT INTO llm_dispatch_jury2_answers VALUES(?,?,?,?,?)", (event_id, int(decision["passages_fit_claim"]), decision["reason"], decision.get("provider_confidence"), int(decision.get("provider_uncertain", False))))
        else:
            raise ValueError("completed answer stage is invalid")
    if payload.get("scheduler_control") is not None:
        control = payload["scheduler_control"]
        conn.execute("INSERT INTO llm_dispatch_scheduler_controls VALUES(?,?)", (event_id, control["source_event_id"]))
        _append_scheduler_children(conn, event_id, control)


def _append_jury1_flow_answer(
    conn: sqlite3.Connection,
    event_id: str,
    stage: str,
    decision: dict[str, Any],
) -> None:
    if stage == "support_gate":
        conn.execute(
        "INSERT INTO llm_dispatch_support_gate_answers VALUES(?,?,?,?)",
        (event_id, int(decision["source_supports_any"]), decision.get("provider_confidence"), int(decision.get("provider_uncertain", False))),
        )
        return
    if stage == "full_support_gate":
        conn.execute(
        "INSERT INTO llm_dispatch_full_support_gate_answers VALUES(?,?,?,?)",
        (event_id, int(decision["source_supports_fully"]), decision.get("provider_confidence"), int(decision.get("provider_uncertain", False))),
        )
        return
    if stage == "contrary_gate":
        conn.execute(
        "INSERT INTO llm_dispatch_contrary_gate_answers VALUES(?,?,?,?)",
        (event_id, int(decision["paper_demonstrates_opposite"]), decision.get("provider_confidence"), int(decision.get("provider_uncertain", False))),
        )
        return
    if stage == "topic_gate":
        conn.execute(
        "INSERT INTO llm_dispatch_topic_gate_answers VALUES(?,?,?,?)",
        (event_id, int(decision["same_specific_subject"]), decision.get("provider_confidence"), int(decision.get("provider_uncertain", False))),
        )
        return
    if stage != "explanation_evidence":
        raise ValueError("Jury1 flow answer stage is invalid")
    evidence = decision["evidence_span_ids"]
    conn.execute(
        "INSERT INTO llm_dispatch_explanation_evidence_answers "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            decision["reason"],
            decision["supported_content"],
            decision["unsupported_content"],
            decision["incompatible_proposition"],
            decision["non_decidable_reason"],
            len(evidence),
            decision.get("provider_confidence"),
            int(decision.get("provider_uncertain", False)),
        ),
    )
    conn.executemany(
        "INSERT INTO llm_dispatch_explanation_evidence_spans VALUES(?,?,?)",
        (
            (event_id, span_order, span_id)
            for span_order, span_id in enumerate(evidence)
        ),
    )
def _decode_event_detail(conn: sqlite3.Connection, record: dict[str, Any]) -> dict[str, Any]:
    event_id, event_type = record["event_id"], record["event_type"]
    detail_tables = (
        "llm_dispatch_pacing_details",
        "llm_dispatch_terminal_details",
        "llm_dispatch_protocol_errors",
        "llm_dispatch_terminal_answers",
        "llm_dispatch_support_gate_answers",
        "llm_dispatch_full_support_gate_answers",
        "llm_dispatch_contrary_gate_answers",
        "llm_dispatch_explanation_evidence_answers",
        "llm_dispatch_explanation_evidence_spans",
        "llm_dispatch_topic_gate_answers",
        "llm_dispatch_jury2_answers",
        "llm_dispatch_scheduler_controls",
        "llm_dispatch_scheduler_cooldowns",
        "llm_dispatch_scheduler_lanes",
        "llm_dispatch_scheduler_profiles",
    )
    present = {
        table
        for table in detail_tables
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE event_id=?", (event_id,)
        ).fetchone()
        is not None
    }
    if event_type in {"leased", "started"}:
        if present:
            raise ValueError("dispatch lifecycle event has unexpected detail")
        payload: dict[str, Any] = {}
        _validate_dispatch_event(
            conn,
            event_id=event_id,
            logical_request_id=record["logical_request_id"],
            dispatch_attempt_id=record.get("dispatch_attempt_id"),
            event_type=event_type,
            payload=payload,
        )
        return payload
    if event_type == "pacing_wait":
        if present != {"llm_dispatch_pacing_details"}:
            raise ValueError("pacing event detail cardinality is invalid")
        row = conn.execute("SELECT pacing_hash, prior_global_start_at, prior_model_start_at, next_eligible_at, global_interval_ms, model_interval_ms FROM llm_dispatch_pacing_details WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError("pacing detail is missing")
        payload = dict(row)
        _validate_dispatch_event(
            conn,
            event_id=event_id,
            logical_request_id=record["logical_request_id"],
            dispatch_attempt_id=record.get("dispatch_attempt_id"),
            event_type=event_type,
            payload=payload,
        )
        return payload
    if event_type not in _TERMINAL_RESULTS:
        raise ValueError("invalid dispatch event type")
    row = conn.execute("SELECT technical_result,http_status,latency_ms,retry_cause,answer_hash,retry_after_seconds FROM llm_dispatch_terminal_details WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        raise ValueError("terminal detail is missing")
    if "llm_dispatch_pacing_details" in present:
        raise ValueError("terminal event has pacing detail")
    payload = dict(row)
    protocol_error = conn.execute(
        "SELECT error_code,response_hash FROM llm_dispatch_protocol_errors "
        "WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if protocol_error is not None:
        payload["protocol_error_code"] = protocol_error["error_code"]
        payload["response_hash"] = protocol_error["response_hash"]
    answer = conn.execute("SELECT logical_request_id,payload_fingerprint FROM llm_dispatch_terminal_answers WHERE event_id=?", (event_id,)).fetchone()
    answer_children = {
            "llm_dispatch_support_gate_answers",
            "llm_dispatch_full_support_gate_answers",
        "llm_dispatch_contrary_gate_answers",
            "llm_dispatch_explanation_evidence_answers",
            "llm_dispatch_explanation_evidence_spans",
        "llm_dispatch_topic_gate_answers",
        "llm_dispatch_jury2_answers",
    }
    if answer is None and present & answer_children:
        raise ValueError("terminal answer child has no parent")
    if answer is not None:
        request_id = answer["logical_request_id"]
        stage_row = conn.execute(
            "SELECT stage FROM llm_logical_requests WHERE logical_request_id=?",
            (request_id,),
        ).fetchone()
        if stage_row is None:
            raise ValueError("terminal answer request is missing")
        decision = _decode_completed_decision(
            conn, event_id, stage_row["stage"]
        )
        payload["answer"] = {"logical_request_id": request_id, "payload_fingerprint": answer["payload_fingerprint"], "decision": decision}
    control = _decode_scheduler_control(conn, event_id)
    if control is not None: payload["scheduler_control"] = control
    _validate_dispatch_event(
        conn,
        event_id=event_id,
        logical_request_id=record["logical_request_id"],
        dispatch_attempt_id=record.get("dispatch_attempt_id"),
        event_type=event_type,
        payload=payload,
    )
    return payload


def _validate_terminal_payload(
    conn: sqlite3.Connection,
    event_id: str,
    logical_request_id: str,
    dispatch_attempt_id: str | None,
    result: str,
    payload: object,
) -> None:
    if not isinstance(payload, dict):
        raise ValueError("dispatch terminal payload is invalid")
    base = {
        "technical_result",
        "http_status",
        "latency_ms",
        "retry_cause",
        "answer_hash",
        "retry_after_seconds",
    }
    protocol = (
        {"protocol_error_code", "response_hash"}
        if payload.get("technical_result") == "protocol_invalid"
        else set()
    )
    expected = base | protocol | (
        {"answer"} if result == "completed" else set()
    )
    has_control = "scheduler_control" in payload
    if frozenset(payload) not in {
        frozenset(expected),
        frozenset(expected | {"scheduler_control"}),
    }:
        raise ValueError("dispatch terminal payload keys are invalid")
    if has_control and payload["scheduler_control"] is None:
        raise ValueError("scheduler control must be absent rather than null")
    dispatch_terminal_payload(
        result=result,
        technical_result=payload["technical_result"],
        http_status=payload["http_status"],
        latency_ms=payload["latency_ms"],
        retry_cause=payload["retry_cause"],
        answer_hash=payload["answer_hash"],
        retry_after_seconds=payload["retry_after_seconds"],
        protocol_error_code=payload.get("protocol_error_code"),
        response_hash=payload.get("response_hash"),
        answer=payload.get("answer"),
        scheduler_control=payload.get("scheduler_control"),
    )
    if dispatch_attempt_id is None:
        raise ValueError("terminal dispatch attempt is missing")
    attempt = conn.execute(
        """SELECT logical_request_id, provider_id, model_id, credential_id,
                  credential_fingerprint, lane_id
           FROM llm_dispatch_attempts WHERE dispatch_attempt_id=?""",
        (dispatch_attempt_id,),
    ).fetchone()
    if attempt is None or attempt["logical_request_id"] != logical_request_id:
        raise ValueError("dispatch event identity mismatch")
    if result == "completed":
        answer = payload["answer"]
        if answer.get("logical_request_id") != logical_request_id:
            raise ValueError("answer logical request identity is invalid")
        request = conn.execute(
            "SELECT prompt_hash, stage FROM llm_logical_requests WHERE logical_request_id=?",
            (logical_request_id,),
        ).fetchone()
        metadata = conn.execute(
            "SELECT fingerprint_version FROM verification_ledger_metadata WHERE singleton=1"
        ).fetchone()
        if request is None or metadata is None:
            raise ValueError("answer provenance is missing")
        from core.verify.claim_evidence.domain.fingerprint import answer_fingerprint

        if (
            answer.get("payload_fingerprint") != request["prompt_hash"]
            or payload["answer_hash"]
            != answer_fingerprint(answer, version=metadata["fingerprint_version"])
        ):
            raise ValueError("answer provenance hash is invalid")
        _validate_answer_decision(answer, request["stage"])
    if has_control:
        control = payload["scheduler_control"]
        _validate_scheduler_control(event_id, control)
        for child in (control["cooldown"], control["lane"]):
            if child is not None and (
                child["credential_id"], child["model_id"]
            ) != (attempt["credential_id"], attempt["model_id"]):
                raise ValueError("scheduler control observation binding is invalid")
        lane = control["lane"]
        if lane is not None and lane["lane_id"] != attempt["lane_id"]:
            raise ValueError("scheduler lane observation binding is invalid")
        profile = control["profile"]
        if profile is not None and (
            profile["provider_id"],
            profile["model_id"],
            profile["credential_fingerprint"],
        ) != (
            attempt["provider_id"],
            attempt["model_id"],
            attempt["credential_fingerprint"],
        ):
            raise ValueError("scheduler profile observation binding is invalid")


def _validate_answer_decision(answer: object, stage: str) -> None:
    expected_answer_fields = {
        "decision", "logical_request_id", "payload_fingerprint",
    }
    if not isinstance(answer, dict) or set(answer) != expected_answer_fields:
        raise ValueError("answer shape is invalid")
    decision = answer["decision"]
    if stage in _JURY1_FLOW_STAGES:
        _validate_canonical_jury1_decision(decision, stage)
        return
    if stage != "jury2" or not isinstance(decision, dict) or set(decision) not in (
        {"passages_fit_claim", "reason", "provider_confidence"},
        {"passages_fit_claim", "reason", "provider_confidence", "provider_uncertain"},
    ):
        raise ValueError("jury2 answer is invalid")
    if type(decision["passages_fit_claim"]) is not bool:
        raise ValueError("jury2 answer is invalid")
    _validate_optional_confidence_rationale(
        decision["provider_confidence"], decision["reason"], "jury2"
    )
    _validate_provider_uncertain(decision, decision["provider_confidence"])


def _validate_optional_confidence(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("provider confidence is invalid")
    return True


def _validate_optional_confidence_rationale(
    confidence: object, rationale: object, label: str
) -> bool:
    probability_only = _validate_optional_confidence(confidence)
    if probability_only:
        if rationale is not None:
            raise ValueError(f"probability-only {label} answer contains rationale")
    else:
        require_text(f"{label} reason", rationale)
    return probability_only


def _validate_provider_uncertain(
    decision: dict[str, Any], confidence: object,
) -> bool:
    if "provider_uncertain" not in decision:
        return False
    if decision["provider_uncertain"] is not True or confidence is None:
        raise ValueError("provider uncertainty is invalid")
    return True


def _validate_canonical_jury1_decision(decision: object, stage: str) -> None:
    if not isinstance(decision, dict):
        raise ValueError("Jury1 flow answer is invalid")
    boolean_fields = {
        "support_gate": "source_supports_any",
        "full_support_gate": "source_supports_fully",
        "contrary_gate": "paper_demonstrates_opposite",
        "topic_gate": "same_specific_subject",
    }
    boolean_field = boolean_fields.get(stage)
    if boolean_field is not None:
        if set(decision) not in ({boolean_field, "provider_confidence"}, {boolean_field, "provider_confidence", "provider_uncertain"}) or type(decision[boolean_field]) is not bool:
            raise ValueError("Jury1 gate answer is invalid")
        _validate_optional_confidence(decision["provider_confidence"])
        _validate_provider_uncertain(decision, decision["provider_confidence"])
        return
    explanation_fields = {
        "reason",
        "supported_content",
        "unsupported_content",
        "incompatible_proposition",
        "evidence_span_ids",
        "non_decidable_reason",
    }
    if stage != "explanation_evidence" or set(decision) not in (
        explanation_fields | {"provider_confidence"},
        explanation_fields | {"provider_confidence", "provider_uncertain"},
    ):
        raise ValueError("explanation/evidence answer is invalid")

    probability_only = _validate_optional_confidence_rationale(
        decision["provider_confidence"], decision["reason"], "explanation/evidence"
    )
    provider_uncertain = _validate_provider_uncertain(
        decision, decision["provider_confidence"]
    )
    if probability_only:
        if any(decision[name] is not None for name in (
            "supported_content", "unsupported_content", "incompatible_proposition",
        )):
            raise ValueError("probability-only explanation/evidence answer contains rationale")
        evidence = decision["evidence_span_ids"]
        if (
            not isinstance(evidence, list)
            or any(not isinstance(item, str) or not item.strip() for item in evidence)
            or len(evidence) > 6
            or len(evidence) != len(set(evidence))
        ):
            raise ValueError("explanation evidence span IDs are invalid")
        non_decidable = decision["non_decidable_reason"]
        if non_decidable is not None and (
            non_decidable not in _SOURCE_NON_DECIDABLE_REASONS or evidence
        ):
            raise ValueError("non-decidable explanation is invalid")
        if provider_uncertain != (non_decidable == "provider_uncertain"):
            raise ValueError("provider uncertainty is inconsistent")
        return
    for field in (
        "supported_content",
        "unsupported_content",
        "incompatible_proposition",
    ):
        value = decision[field]
        if value is not None:
            require_text(field, value)
    evidence = decision["evidence_span_ids"]
    if (
        not isinstance(evidence, list)
        or any(not isinstance(value, str) or not value.strip() for value in evidence)
        or len(set(evidence)) != len(evidence)
        or len(evidence) > 6
    ):
        raise ValueError("explanation evidence is invalid")
    non_decidable = decision["non_decidable_reason"]
    allowed_reasons = {
        "material_limit",
        "no_consensus",
        "verification_unavailable",
        "retrieval_limit", "provider_uncertain",
    }
    if non_decidable is not None and non_decidable not in allowed_reasons:
        raise ValueError("non-decidable reason is invalid")
    if provider_uncertain != (non_decidable == "provider_uncertain"):
        raise ValueError("provider uncertainty is inconsistent")
    if non_decidable is not None and (
        evidence
        or decision["supported_content"] is not None
        or decision["unsupported_content"] is not None
        or decision["incompatible_proposition"] is not None
    ):
        raise ValueError("non-decidable explanation must be empty")


def _validate_scheduler_control(event_id: str, control: object) -> None:
    if (
        not isinstance(control, dict)
        or set(control) != {"source_event_id", "cooldown", "lane", "profile"}
        or control["source_event_id"] != event_id
    ):
        raise ValueError("scheduler control shape is invalid")
    if not any(control[key] is not None for key in ("cooldown", "lane", "profile")):
        raise ValueError("scheduler control is empty")
    _validate_scheduler_cooldown(control["cooldown"])
    _validate_scheduler_lane(control["lane"])
    _validate_scheduler_profile(control["profile"])


def _validate_cooldown_state(payload: object) -> None:
    expected = {
        "next_eligible_at",
        "last_applied_seconds",
        "learned_seconds",
        "post_cooldown_successes",
        "policy_version",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("cooldown state shape is invalid")
    require_nonnegative_int("next_eligible_at", payload["next_eligible_at"])
    for key in ("last_applied_seconds", "learned_seconds"):
        if payload[key] is not None:
            require_nonnegative_int(key, payload[key])
    require_nonnegative_int(
        "post_cooldown_successes", payload["post_cooldown_successes"]
    )
    require_text("policy_version", payload["policy_version"])


def _validate_scheduler_cooldown(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError("scheduler cooldown is invalid")
    common = {
        "credential_id",
        "model_id",
        "next_eligible_at",
        "status",
        "disabled",
        "reason",
    }
    status = value.get("status")
    expected = common if status == "quarantined" else common | {"payload"}
    if set(value) != expected:
        raise ValueError("scheduler cooldown shape is invalid")
    if (
        not isinstance(value["credential_id"], str)
        or _CREDENTIAL_ALIAS_RE.fullmatch(value["credential_id"]) is None
    ):
        raise ValueError("scheduler cooldown credential_id is invalid")
    require_text("scheduler cooldown model_id", value["model_id"])
    if status == "quarantined":
        if (
            value["disabled"] is not True
            or value["reason"] != "credential_invalid"
            or value["next_eligible_at"] is not None
        ):
            raise ValueError("scheduler quarantine is invalid")
    elif status == "cooldown":
        require_nonnegative_int("next_eligible_at", value["next_eligible_at"])
        if value["disabled"] is not False or value["reason"] != "rate_limited":
            raise ValueError("scheduler cooldown is invalid")
        _validate_cooldown_state(value["payload"])
        if value["next_eligible_at"] != value["payload"]["next_eligible_at"] * 1000:
            raise ValueError("scheduler cooldown eligibility is inconsistent")
    elif status == "cleared":
        if (
            value["disabled"] is not False
            or value["reason"] is not None
            or value["next_eligible_at"] is not None
        ):
            raise ValueError("scheduler cleared is invalid")
        _validate_cooldown_state(value["payload"])
    else:
        raise ValueError("scheduler cooldown status is invalid")


def _validate_scheduler_lane(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {
        "credential_id",
        "model_id",
        "lane_id",
        "available",
    }:
        raise ValueError("scheduler lane shape is invalid")
    if (
        not isinstance(value["credential_id"], str)
        or _CREDENTIAL_ALIAS_RE.fullmatch(value["credential_id"]) is None
    ):
        raise ValueError("scheduler lane credential_id is invalid")
    for key in ("model_id", "lane_id"):
        require_text(f"scheduler lane {key}", value[key])
    if value["available"] is not False:
        raise ValueError("scheduler lane available is invalid")


def _validate_scheduler_profile(value: object) -> None:
    if value is None:
        return
    expected = {
        "provider_id",
        "credential_fingerprint",
        "model_id",
        "profile_version",
        "baseline_seconds",
        "multiplier",
        "local_max_seconds",
        "stable_successes",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("scheduler profile shape is invalid")
    if (
        not isinstance(value["provider_id"], str)
        or _PROVIDER_RE.fullmatch(value["provider_id"]) is None
    ):
        raise ValueError("scheduler profile provider_id is invalid")
    require_hash("credential_fingerprint", value["credential_fingerprint"])
    require_text("scheduler profile model_id", value["model_id"])
    require_text("scheduler profile version", value["profile_version"])
    require_nonnegative_int("baseline_seconds", value["baseline_seconds"])
    require_positive_int("multiplier", value["multiplier"])
    require_nonnegative_int("local_max_seconds", value["local_max_seconds"])
    require_nonnegative_int("stable_successes", value["stable_successes"])
    if value["local_max_seconds"] < value["baseline_seconds"]:
        raise ValueError("scheduler profile is invalid")


def _append_scheduler_children(conn: sqlite3.Connection, event_id: str, control: dict[str, Any]) -> None:
    _validate_scheduler_control(event_id, control)
    cooldown, lane, profile = control["cooldown"], control["lane"], control["profile"]
    if cooldown is not None:
        state = cooldown.get("payload")
        conn.execute("INSERT INTO llm_dispatch_scheduler_cooldowns VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, cooldown["credential_id"], cooldown["model_id"], cooldown["next_eligible_at"], cooldown["status"], int(cooldown["disabled"]), cooldown["reason"], None if state is None else state["next_eligible_at"], None if state is None else state["policy_version"], None if state is None else state["last_applied_seconds"], None if state is None else state["learned_seconds"], None if state is None else state["post_cooldown_successes"]))
    if lane is not None:
        conn.execute("INSERT INTO llm_dispatch_scheduler_lanes VALUES(?,?,?,?,?)", (event_id, lane["credential_id"], lane["model_id"], lane["lane_id"], 0))
    if profile is not None:
        conn.execute("INSERT INTO llm_dispatch_scheduler_profiles VALUES(?,?,?,?,?,?,?,?,?)", (event_id, *(profile[key] for key in ("provider_id", "credential_fingerprint", "model_id", "profile_version", "baseline_seconds", "multiplier", "local_max_seconds", "stable_successes"))))


def _decode_scheduler_control(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    parent = conn.execute("SELECT source_event_id FROM llm_dispatch_scheduler_controls WHERE event_id=?", (event_id,)).fetchone()
    cooldown = conn.execute("SELECT * FROM llm_dispatch_scheduler_cooldowns WHERE event_id=?", (event_id,)).fetchone()
    lane = conn.execute("SELECT * FROM llm_dispatch_scheduler_lanes WHERE event_id=?", (event_id,)).fetchone()
    profile = conn.execute("SELECT * FROM llm_dispatch_scheduler_profiles WHERE event_id=?", (event_id,)).fetchone()
    if parent is None:
        if any(row is not None for row in (cooldown, lane, profile)):
            raise ValueError("scheduler control child has no parent")
        return None
    c = None if cooldown is None else {"credential_id": cooldown["credential_id"], "model_id": cooldown["model_id"], "next_eligible_at": cooldown["next_eligible_at"], "status": cooldown["status"], "disabled": bool(cooldown["disabled"]), "reason": cooldown["reason"]}
    if c is not None and cooldown["status"] != "quarantined": c["payload"] = {"next_eligible_at": cooldown["state_next_eligible_at"], "last_applied_seconds": cooldown["last_applied_seconds"], "learned_seconds": cooldown["learned_seconds"], "post_cooldown_successes": cooldown["post_cooldown_successes"], "policy_version": cooldown["policy_version"]}
    l = None if lane is None else {"credential_id": lane["credential_id"], "model_id": lane["model_id"], "lane_id": lane["lane_id"], "available": bool(lane["available"])}
    p = None if profile is None else {key: profile[key] for key in ("provider_id", "credential_fingerprint", "model_id", "profile_version", "baseline_seconds", "multiplier", "local_max_seconds", "stable_successes")}
    control = {"source_event_id": parent["source_event_id"], "cooldown": c, "lane": l, "profile": p}
    _validate_scheduler_control(event_id, control)
    return control


def validate_resume(
    requests: dict[str, dict[str, Any]],
    attempts: list[dict[str, Any]],
    events: list[dict[str, Any]],
    leases: list[dict[str, Any]],
) -> None:
    attempt_map = {row["dispatch_attempt_id"]: row for row in attempts}
    state: dict[str, set[str]] = {identifier: set() for identifier in attempt_map}
    for attempt in attempts:
        request = requests.get(attempt["logical_request_id"])
        if request is None:
            raise ValueError("dispatch attempt request is missing")
        validate_dispatch_shape({**attempt, "payload_hash": request["payload_hash"]})
    for event in events:
        if event["event_type"] == "pacing_wait":
            if event["logical_request_id"] not in requests:
                raise ValueError("pacing event request is missing")
            _validate_pacing_payload(event["payload"])
            continue
        attempt = attempt_map.get(event["dispatch_attempt_id"])
        if attempt is None or attempt["logical_request_id"] != event["logical_request_id"]:
            raise ValueError("dispatch event identity mismatch")
        _record_attempt_event(state[event["dispatch_attempt_id"]], event)
    lease_by_attempt = {row["dispatch_attempt_id"]: row for row in leases}
    for attempt_id, event_types in state.items():
        if "leased" not in event_types:
            raise ValueError("dispatch attempt is missing its lease event")
        if event_types & {"completed", "failed"} and "started" not in event_types:
            raise ValueError("dispatch terminal exists without a provider start")
        if len(event_types & _TERMINAL_RESULTS) > 1:
            raise ValueError("dispatch has multiple technical terminals")
        if not event_types & _TERMINAL_RESULTS:
            lease = lease_by_attempt.get(attempt_id)
            if lease is None or lease["status"] != "live":
                raise ValueError("unterminated dispatch lacks the live lease")
    for lease in leases:
        _validate_lease_projection(lease, state)


def _record_attempt_event(
    event_types: set[str],
    event: dict[str, Any],
) -> None:
    event_type = event["event_type"]
    if event_type in _TERMINAL_RESULTS:
        payload = event["payload"]
        if not isinstance(payload, dict):
            raise ValueError("dispatch terminal payload is invalid")
        dispatch_terminal_payload(
            result=event_type,
            technical_result=payload.get("technical_result"),
            http_status=payload.get("http_status"),
            latency_ms=payload.get("latency_ms"),
            retry_cause=payload.get("retry_cause"),
            answer_hash=payload.get("answer_hash"),
            retry_after_seconds=payload.get("retry_after_seconds"),
            protocol_error_code=payload.get("protocol_error_code"),
            response_hash=payload.get("response_hash"),
            answer=payload.get("answer"),
            scheduler_control=payload.get("scheduler_control"),
        )
    if event_type in event_types:
        raise ValueError("dispatch lifecycle event is duplicated")
    event_types.add(event_type)


def _validate_lease_projection(
    lease: dict[str, Any],
    state: dict[str, set[str]],
) -> None:
    event_types = state.get(lease["dispatch_attempt_id"])
    if event_types is None:
        raise ValueError("dispatch lease references a missing attempt")
    terminal = event_types & _TERMINAL_RESULTS
    expected = (
        not terminal
        if lease["status"] == "live"
        else terminal == {"completed"}
        if lease["status"] == "terminal"
        else terminal in ({"failed"}, {"abandoned"})
    )
    if not expected:
        raise ValueError("dispatch lease projection differs from event ledger")


def _validate_pacing_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("pacing event payload is invalid")
    if set(payload) != {"pacing_hash", "prior_global_start_at", "prior_model_start_at", "next_eligible_at", "global_interval_ms", "model_interval_ms"}:
        raise ValueError("pacing payload keys are invalid")
    require_hash("pacing_hash", payload.get("pacing_hash"))
    require_nonnegative_int(
        "global_interval_ms", payload.get("global_interval_ms")
    )
    require_nonnegative_int(
        "model_interval_ms", payload.get("model_interval_ms")
    )
    require_text("next_eligible_at", payload.get("next_eligible_at"))
    for field in ("prior_global_start_at", "prior_model_start_at"):
        if payload.get(field) is not None:
            require_text(field, payload[field])


def _validate_dispatch_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    logical_request_id: str,
    dispatch_attempt_id: str | None,
    event_type: str,
    payload: object,
) -> None:
    if not isinstance(payload, dict):
        raise ValueError("dispatch event payload must be an object")
    _validate_event_identity(
        conn,
        event_id,
        logical_request_id,
        dispatch_attempt_id,
        event_type,
        payload,
    )
    if event_type in {"leased", "started"}:
        if payload:
            raise ValueError("dispatch lifecycle payload must be empty")
    elif event_type == "pacing_wait":
        _validate_pacing_payload(payload)
    else:
        _validate_terminal_payload(
            conn,
            event_id,
            logical_request_id,
            dispatch_attempt_id,
            event_type,
            payload,
        )


def _validate_event_identity(
    conn: sqlite3.Connection,
    event_id: str,
    logical_request_id: str,
    dispatch_attempt_id: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    require_text("event_id", event_id)
    require_text("logical_request_id", logical_request_id)
    if event_type not in {"leased", "started", "completed", "failed", "abandoned", "pacing_wait"}:
        raise ValueError("invalid dispatch event type")
    if conn.execute(
        "SELECT 1 FROM llm_logical_requests WHERE logical_request_id=?",
        (logical_request_id,),
    ).fetchone() is None:
        raise ValueError("dispatch event request is missing")
    if event_type == "pacing_wait":
        if dispatch_attempt_id is not None: raise ValueError("pacing event identity is invalid")
        from core.verify.claim_evidence.domain.fingerprint import payload_fingerprint
        metadata = conn.execute("SELECT fingerprint_version FROM verification_ledger_metadata WHERE singleton=1").fetchone()
        if metadata is None or event_id != "pacing:" + payload_fingerprint({"logical_request_id": logical_request_id, **payload}, version=metadata["fingerprint_version"]):
            raise ValueError("pacing event id is invalid")
        return
    if dispatch_attempt_id is None: raise ValueError("dispatch lifecycle identity is invalid")
    attempt = conn.execute(
        "SELECT logical_request_id FROM llm_dispatch_attempts WHERE dispatch_attempt_id=?",
        (dispatch_attempt_id,),
    ).fetchone()
    if attempt is None or attempt["logical_request_id"] != logical_request_id:
        raise ValueError("dispatch event identity mismatch")
    suffix = "terminal" if event_type in _TERMINAL_RESULTS else event_type
    if event_id != f"{dispatch_attempt_id}:{suffix}": raise ValueError("dispatch event id is invalid")


def require_hash(name: str, value: object) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 hex digest")


def require_positive_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def require_nonnegative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be non-empty trimmed text")


def require_document_text(name: str, value: object) -> None:
    """Validate exact source content without normalizing its frozen bytes."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must contain non-whitespace text")
