# core/infra/db/resolve_transport_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only physical Resolve transport observations."""

from __future__ import annotations

import math
import re
from typing import Any

_PROVIDER_OPERATIONS = {
    ("openalex", "doi_lookup"),
    ("openalex", "title_search"),
    ("semantic_scholar", "doi_lookup"),
    ("springer_meta", "doi_lookup"),
    ("pubmed", "efetch"),
    ("pubmed", "pmc_idconv"),
    ("resolve_http", "request"),
}

RESOLVE_TRANSPORT_DDL = """
CREATE TABLE IF NOT EXISTS resolve_transport_operations (
  operation_id TEXT PRIMARY KEY, provider TEXT NOT NULL, operation TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('batch', 'scalar')), chunk_index INTEGER,
  item_count INTEGER NOT NULL CHECK(item_count >= 1),
  target_kind TEXT NOT NULL CHECK(target_kind IN ('references','manuscript_identity')),
  predecessor_operation_id TEXT REFERENCES resolve_transport_operations(operation_id),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resolve_transport_operation_refs (
  operation_id TEXT NOT NULL REFERENCES resolve_transport_operations(operation_id),
  ref_id TEXT NOT NULL REFERENCES operational_references(ref_id),
  ref_order INTEGER NOT NULL CHECK(ref_order >= 0), PRIMARY KEY(operation_id, ref_id),
  UNIQUE(operation_id, ref_order)
);
CREATE TABLE IF NOT EXISTS resolve_transport_operation_manuscript_targets (
  operation_id TEXT PRIMARY KEY REFERENCES resolve_transport_operations(operation_id),
  input_sha256 TEXT NOT NULL CHECK(
    length(input_sha256)=64 AND input_sha256 NOT GLOB '*[^0-9a-f]*'
  )
);
CREATE TABLE IF NOT EXISTS resolve_transport_http_attempts (
  attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES resolve_transport_operations(operation_id),
  request_id TEXT NOT NULL, attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
  method TEXT NOT NULL CHECK(method IN ('GET', 'POST')), endpoint TEXT NOT NULL,
  http_status INTEGER, error_type TEXT, started_at_ms INTEGER NOT NULL CHECK(started_at_ms >= 0),
  retry_after_seconds REAL, duration_ms REAL NOT NULL CHECK(duration_ms >= 0),
  outcome TEXT NOT NULL CHECK(outcome IN ('response', 'http_error', 'network_error')),
  created_at TEXT NOT NULL, UNIQUE(request_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS resolve_transport_failures (
  failure_id TEXT PRIMARY KEY, stage TEXT NOT NULL CHECK(stage IN ('operation','mapping','attempt','search_provenance')),
  target_kind TEXT NOT NULL CHECK(target_kind IN ('run','reference','manuscript_identity')),
  ref_id TEXT REFERENCES operational_references(ref_id), manuscript_input_sha256 TEXT,
  error_type TEXT NOT NULL, created_at TEXT NOT NULL,
  CHECK(
    (target_kind='run' AND ref_id IS NULL AND manuscript_input_sha256 IS NULL)
    OR (target_kind='reference' AND ref_id IS NOT NULL AND manuscript_input_sha256 IS NULL)
    OR (target_kind='manuscript_identity' AND ref_id IS NULL
        AND length(manuscript_input_sha256)=64
        AND manuscript_input_sha256 NOT GLOB '*[^0-9a-f]*')
  )
);
"""
RESOLVE_TRANSPORT_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS resolve_transport_operations_no_update BEFORE UPDATE ON resolve_transport_operations BEGIN SELECT RAISE(ABORT, 'resolve transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_transport_operations_no_delete BEFORE DELETE ON resolve_transport_operations BEGIN SELECT RAISE(ABORT, 'resolve transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_transport_operation_refs_no_update BEFORE UPDATE ON resolve_transport_operation_refs BEGIN SELECT RAISE(ABORT, 'resolve transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_transport_operation_refs_no_delete BEFORE DELETE ON resolve_transport_operation_refs BEGIN SELECT RAISE(ABORT, 'resolve transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_transport_operation_manuscript_targets_no_update BEFORE UPDATE ON resolve_transport_operation_manuscript_targets BEGIN SELECT RAISE(ABORT, 'resolve transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_transport_operation_manuscript_targets_no_delete BEFORE DELETE ON resolve_transport_operation_manuscript_targets BEGIN SELECT RAISE(ABORT, 'resolve transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_transport_http_attempts_no_update BEFORE UPDATE ON resolve_transport_http_attempts BEGIN SELECT RAISE(ABORT, 'resolve transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_transport_http_attempts_no_delete BEFORE DELETE ON resolve_transport_http_attempts BEGIN SELECT RAISE(ABORT, 'resolve transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_transport_failures_no_update BEFORE UPDATE ON resolve_transport_failures BEGIN SELECT RAISE(ABORT, 'resolve transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_transport_failures_no_delete BEFORE DELETE ON resolve_transport_failures BEGIN SELECT RAISE(ABORT, 'resolve transport is append-only'); END;
"""


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value.strip() or "\0" in value:
        raise ValueError(f"resolve transport {name} must be nonempty text")
    return value


def _require_manuscript_target(conn, input_sha256: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM manuscript_identity mi JOIN run r "
        "ON r.input_sha256=mi.input_sha256 "
        "WHERE mi.singleton=1 AND mi.input_sha256=?",
        (input_sha256,),
    ).fetchone()
    if row is None:
        raise ValueError(
            "resolve transport manuscript target does not match the run identity"
        )


def insert_operation(conn, payload: dict[str, Any], *, created_at: str) -> None:
    required = {
        "operation_id",
        "provider",
        "operation",
        "mode",
        "item_count",
    }
    allowed = required | {
        "ref_ids", "target_kind", "manuscript_input_sha256",
        "chunk_index", "predecessor_operation_id",
    }
    if (
        type(payload) is not dict
        or not required <= set(payload)
        or set(payload) - allowed
    ):
        raise ValueError("resolve transport operation is invalid")
    operation_id = _text("operation_id", payload["operation_id"])
    provider = _text("provider", payload["provider"])
    operation = _text("operation", payload["operation"])
    target_kind = payload.get("target_kind", "references")
    refs, item_count, mode = payload.get("ref_ids"), payload["item_count"], payload["mode"]
    if (provider, operation) not in _PROVIDER_OPERATIONS or mode not in {
        "batch",
        "scalar",
    }:
        raise ValueError("resolve transport operation is invalid")
    if target_kind not in {"references", "manuscript_identity"}:
        raise ValueError("resolve transport operation target is invalid")
    manuscript_input_sha256 = payload.get("manuscript_input_sha256")
    if target_kind == "references":
        if type(refs) is not list or not refs or len(set(refs)) != len(refs):
            raise ValueError("resolve transport operation references are invalid")
        if manuscript_input_sha256 is not None:
            raise ValueError("resolve transport operation target is invalid")
        for ref_id in refs:
            _text("ref_id", ref_id)
    else:
        if refs is not None or mode != "scalar":
            raise ValueError("resolve transport operation target is invalid")
        if (
            type(manuscript_input_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", manuscript_input_sha256) is None
        ):
            raise ValueError("resolve transport manuscript target is invalid")
        _require_manuscript_target(conn, manuscript_input_sha256)
        refs = []
    if type(item_count) is not int or not 1 <= item_count <= (len(refs) or 1):
        raise ValueError("resolve transport item count is invalid")
    chunk_index = payload.get("chunk_index")
    if mode == "scalar":
        if item_count != 1 or chunk_index is not None:
            raise ValueError("resolve transport scalar operation is invalid")
    elif type(chunk_index) is not int or chunk_index < 0:
        raise ValueError("resolve transport batch chunk index is invalid")
    predecessor = payload.get("predecessor_operation_id")
    if target_kind == "manuscript_identity" and predecessor is not None:
        raise ValueError("resolve transport manuscript target is invalid")
    if predecessor is not None:
        _text("predecessor_operation_id", predecessor)
        if predecessor == operation_id:
            raise ValueError("resolve transport predecessor cannot be self")
        predecessor_row = conn.execute(
            "SELECT provider, mode FROM resolve_transport_operations "
            "WHERE operation_id=?",
            (predecessor,),
        ).fetchone()
        if (
            predecessor_row is None
            or mode != "scalar"
            or predecessor_row[0] != provider
            or predecessor_row[1] != "batch"
        ):
            raise ValueError("resolve transport predecessor is invalid")
        predecessor_refs = {
            row[0]
            for row in conn.execute(
                "SELECT ref_id FROM resolve_transport_operation_refs "
                "WHERE operation_id=?",
                (predecessor,),
            )
        }
        if not set(refs).issubset(predecessor_refs):
            raise ValueError("resolve transport predecessor is invalid")
    conn.execute(
        "INSERT INTO resolve_transport_operations VALUES(?,?,?,?,?,?,?,?,?)",
        (
            operation_id,
            provider,
            operation,
            mode,
            chunk_index,
            item_count,
            target_kind,
            predecessor,
            created_at,
        ),
    )
    if target_kind == "references":
        conn.executemany(
            "INSERT INTO resolve_transport_operation_refs VALUES(?,?,?)",
            ((operation_id, ref_id, index) for index, ref_id in enumerate(refs)),
        )
    else:
        conn.execute(
            "INSERT INTO resolve_transport_operation_manuscript_targets VALUES(?,?)",
            (operation_id, manuscript_input_sha256),
        )


def insert_attempt(conn, payload: dict[str, Any], *, created_at: str) -> None:
    required = {
        "attempt_id",
        "operation_id",
        "request_id",
        "attempt_number",
        "method",
        "endpoint",
        "duration_ms",
        "outcome",
    }
    allowed = required | {"status", "error_type", "started_at_ms", "retry_after_seconds"}
    if (
        type(payload) is not dict
        or not required <= set(payload)
        or set(payload) - allowed
    ):
        raise ValueError("resolve transport attempt is invalid")
    for key in ("attempt_id", "operation_id", "request_id", "endpoint"):
        _text(key, payload[key])
    if payload["method"] not in {"GET", "POST"} or payload["outcome"] not in {
        "response",
        "http_error",
        "network_error",
    }:
        raise ValueError("resolve transport attempt kind is invalid")
    if type(payload["attempt_number"]) is not int or payload["attempt_number"] < 1:
        raise ValueError("resolve transport attempt number is invalid")
    duration_ms = payload["duration_ms"]
    if (
        type(duration_ms) not in (int, float)
        or not math.isfinite(duration_ms)
        or duration_ms < 0
    ):
        raise ValueError("resolve transport duration is invalid")
    started_at_ms = payload.get("started_at_ms")
    if type(started_at_ms) is not int or started_at_ms < 0:
        raise ValueError("resolve transport start time is invalid")
    retry_after_seconds = payload.get("retry_after_seconds")
    if retry_after_seconds is not None and (
        type(retry_after_seconds) not in (int, float)
        or not math.isfinite(retry_after_seconds)
        or retry_after_seconds < 0
    ):
        raise ValueError("resolve transport retry-after is invalid")
    status = payload.get("status")
    error_type = payload.get("error_type")
    outcome = payload["outcome"]
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ValueError("resolve transport status is invalid")
    if error_type is not None:
        _text("error_type", error_type)
    if outcome == "response" and (status is None or error_type is not None):
        raise ValueError("resolve transport response detail is invalid")
    if outcome == "http_error" and (status is None or error_type is None):
        raise ValueError("resolve transport HTTP error detail is invalid")
    if outcome == "network_error" and error_type is None:
        raise ValueError("resolve transport network error detail is invalid")
    conn.execute(
        "INSERT INTO resolve_transport_http_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            payload["attempt_id"],
            payload["operation_id"],
            payload["request_id"],
            payload["attempt_number"],
            payload["method"],
            payload["endpoint"],
            status,
            error_type,
            started_at_ms,
            retry_after_seconds,
            duration_ms,
            outcome,
            created_at,
        ),
    )


def insert_ref_mapping(conn, operation_id: str, ref_id: str) -> None:
    _text("operation_id", operation_id)
    _text("ref_id", ref_id)
    operation_row = conn.execute(
        "SELECT target_kind FROM resolve_transport_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if operation_row is None or operation_row[0] != "references":
        raise ValueError("resolve transport operation does not target references")
    if (
        conn.execute(
            "SELECT 1 FROM resolve_transport_operation_refs "
            "WHERE operation_id=? AND ref_id=?",
            (operation_id, ref_id),
        ).fetchone()
        is not None
    ):
        return
    ref_order = conn.execute(
        "SELECT COALESCE(MAX(ref_order), -1) + 1 "
        "FROM resolve_transport_operation_refs WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO resolve_transport_operation_refs VALUES(?,?,?)",
        (operation_id, ref_id, ref_order),
    )


def read_operations(conn):
    rows = conn.execute(
        "SELECT * FROM resolve_transport_operations ORDER BY created_at, operation_id"
    )
    return [dict(row) for row in rows]


def read_ref_mappings(conn):
    rows = conn.execute(
        "SELECT * FROM resolve_transport_operation_refs "
        "ORDER BY operation_id, ref_order"
    )
    return [dict(row) for row in rows]


def read_manuscript_targets(conn):
    rows = conn.execute(
        "SELECT * FROM resolve_transport_operation_manuscript_targets ORDER BY operation_id"
    )
    return [dict(row) for row in rows]


def read_attempts(conn):
    rows = conn.execute(
        "SELECT * FROM resolve_transport_http_attempts "
        "ORDER BY created_at, request_id, attempt_number, attempt_id"
    )
    return [dict(row) for row in rows]


def insert_failure(conn, payload: dict[str, Any], *, created_at: str) -> None:
    required = {"failure_id", "stage", "error_type"}
    allowed = required | {"target_kind", "ref_id", "manuscript_input_sha256"}
    if type(payload) is not dict or not required <= set(payload) or set(payload) - allowed:
        raise ValueError("resolve transport failure is invalid")
    for key in ("failure_id", "stage", "error_type"):
        _text(key, payload[key])
    if payload["stage"] not in {"operation", "mapping", "attempt", "search_provenance"}:
        raise ValueError("resolve transport failure stage is invalid")
    target_kind = payload.get("target_kind")
    ref_id = payload.get("ref_id")
    manuscript_input_sha256 = payload.get("manuscript_input_sha256")
    if target_kind is None:
        target_kind = "reference" if ref_id is not None else "run"
    if target_kind == "reference":
        _text("ref_id", ref_id)
        if manuscript_input_sha256 is not None:
            raise ValueError("resolve transport failure target is invalid")
    elif target_kind == "run":
        if ref_id is not None or manuscript_input_sha256 is not None:
            raise ValueError("resolve transport failure target is invalid")
    elif (
        target_kind != "manuscript_identity" or ref_id is not None
        or type(manuscript_input_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", manuscript_input_sha256) is None
    ):
        raise ValueError("resolve transport failure target is invalid")
    if target_kind == "manuscript_identity":
        _require_manuscript_target(conn, manuscript_input_sha256)
    conn.execute(
        "INSERT INTO resolve_transport_failures VALUES(?,?,?,?,?,?,?)",
        (
            payload["failure_id"],
            payload["stage"],
            target_kind,
            ref_id,
            manuscript_input_sha256,
            payload["error_type"],
            created_at,
        ),
    )


def read_failures(conn):
    rows = conn.execute(
        "SELECT * FROM resolve_transport_failures ORDER BY created_at, failure_id"
    )
    return [dict(row) for row in rows]
