# core/infra/db/http_trace_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed relational storage for raw LLM HTTP attempts."""

from __future__ import annotations

import math
from typing import Any


_OUTCOMES = {"response", "http_error", "invalid_response", "network_error"}
_TEXT_FIELDS = (
    "logical_request_id",
    "dispatch_attempt_id",
    "provider",
    "model",
    "credential_alias",
    "credential_fingerprint",
    "lane_id",
)
_FIELDS = {
    "network_attempt_id",
    "logical_request_id",
    "dispatch_attempt_id",
    "jury_stage",
    "provider",
    "model",
    "credential_alias",
    "credential_fingerprint",
    "lane_id",
    "started_at_ms",
    "url",
    "attempt",
    "outcome",
    "status",
    "retryable",
    "error_type",
    "duration_ms",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
}


def normalize_http_attempt(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the exact current transport-attempt shape."""
    if type(payload) is not dict or not {
        "network_attempt_id", "url", "attempt", "outcome", "duration_ms",
    }.issubset(payload):
        raise ValueError("raw HTTP attempt is missing required fields")
    unknown = set(payload) - _FIELDS
    if unknown:
        raise ValueError(
            "raw HTTP attempt has unknown fields: " + ", ".join(sorted(unknown))
        )

    out = dict(payload)
    for key in ("network_attempt_id", "url"):
        value = out[key]
        if type(value) is not str or not value.strip() or "\0" in value:
            raise ValueError(f"raw HTTP attempt {key} must be nonempty text")
    for key in _TEXT_FIELDS:
        value = out.get(key)
        if value is not None and (
            type(value) is not str or not value.strip() or "\0" in value
        ):
            raise ValueError(f"raw HTTP attempt {key} must be nonempty text or null")
    stage = out.get("jury_stage")
    if stage is not None and stage not in {
        "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
        "explanation_evidence", "jury2",
    }:
        raise ValueError("raw HTTP attempt jury_stage is invalid")
    attempt = out["attempt"]
    if type(attempt) is not int or attempt < 1:
        raise ValueError("raw HTTP attempt number must be a positive integer")
    outcome = out["outcome"]
    if type(outcome) is not str or outcome not in _OUTCOMES:
        raise ValueError("raw HTTP attempt outcome is invalid")
    for key in ("started_at_ms", "duration_ms"):
        value = out.get(key)
        if key == "duration_ms" or value is not None:
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"raw HTTP attempt {key} must be finite and nonnegative")
    status = out.get("status")
    if status is not None and (
        type(status) is not int or isinstance(status, bool) or not 100 <= status <= 599
    ):
        raise ValueError("raw HTTP attempt status is invalid")
    retryable = out.get("retryable")
    if retryable is not None and type(retryable) is not bool:
        raise ValueError("raw HTTP attempt retryable must be bool or null")
    error_type = out.get("error_type")
    if error_type is not None and (
        type(error_type) is not str or not error_type.strip() or "\0" in error_type
    ):
        raise ValueError("raw HTTP attempt error_type must be nonempty text or null")
    cache_values = tuple(
        out.get(key)
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
    )
    if any(
        value is not None and (type(value) is not int or value < 0)
        for value in cache_values
    ):
        raise ValueError("raw HTTP attempt cache tokens must be nonnegative integers or null")
    if (cache_values[0] is None) != (cache_values[1] is None):
        raise ValueError("raw HTTP attempt cache token accounting is incomplete")
    if cache_values[0] is not None and outcome != "response":
        raise ValueError("raw HTTP cache tokens require a response attempt")

    if outcome == "response" and (
        status is None or retryable is not None or error_type is not None
    ):
        raise ValueError("raw HTTP response attempt has inconsistent detail")
    if outcome == "http_error" and (
        status is None or retryable is None or error_type is not None
    ):
        raise ValueError("raw HTTP error attempt has inconsistent detail")
    if outcome in {"invalid_response", "network_error"} and (
        status is not None or retryable is None or error_type is None
    ):
        raise ValueError("raw HTTP transport attempt has inconsistent detail")
    return out


def insert_http_attempt(conn, payload: dict[str, Any], *, created_at: str) -> None:
    row = normalize_http_attempt(payload)
    conn.execute(
        """
        INSERT INTO llm_http_attempts(
          network_attempt_id, logical_request_id, dispatch_attempt_id,
          jury_stage, provider_id, model_id, credential_alias,
          credential_fingerprint, lane_id, started_at_ms, endpoint_url,
          attempt_number, outcome, http_status, retryable, error_type,
          duration_ms, prompt_cache_hit_tokens, prompt_cache_miss_tokens,
          created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["network_attempt_id"], row.get("logical_request_id"),
            row.get("dispatch_attempt_id"), row.get("jury_stage"),
            row.get("provider"), row.get("model"), row.get("credential_alias"),
            row.get("credential_fingerprint"), row.get("lane_id"),
            row.get("started_at_ms"), row["url"], row["attempt"],
            row["outcome"], row.get("status"),
            None if row.get("retryable") is None else int(row["retryable"]),
            row.get("error_type"), row["duration_ms"],
            row.get("prompt_cache_hit_tokens"),
            row.get("prompt_cache_miss_tokens"), created_at,
        ),
    )


def read_http_attempts(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM llm_http_attempts ORDER BY event_order"
    ).fetchall()
    return [
        {
            "network_attempt_id": row["network_attempt_id"],
            "logical_request_id": row["logical_request_id"],
            "dispatch_attempt_id": row["dispatch_attempt_id"],
            "jury_stage": row["jury_stage"],
            "provider": row["provider_id"],
            "model": row["model_id"],
            "credential_alias": row["credential_alias"],
            "credential_fingerprint": row["credential_fingerprint"],
            "lane_id": row["lane_id"],
            "started_at_ms": row["started_at_ms"],
            "url": row["endpoint_url"],
            "attempt": row["attempt_number"],
            "outcome": row["outcome"],
            "status": row["http_status"],
            "retryable": (
                None if row["retryable"] is None else bool(row["retryable"])
            ),
            "error_type": row["error_type"],
            "duration_ms": row["duration_ms"],
            "prompt_cache_hit_tokens": row["prompt_cache_hit_tokens"],
            "prompt_cache_miss_tokens": row["prompt_cache_miss_tokens"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
