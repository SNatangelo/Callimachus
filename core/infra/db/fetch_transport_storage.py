# core/infra/db/fetch_transport_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only Fetch logical-request and physical HTTP observations."""

from __future__ import annotations

import math
from typing import Any


FETCH_TRANSPORT_DDL = """
CREATE TABLE IF NOT EXISTS fetch_transport_requests (
  request_id TEXT PRIMARY KEY, ref_id TEXT NOT NULL REFERENCES operational_references(ref_id),
  requested_endpoint TEXT NOT NULL, profile TEXT NOT NULL, strategy TEXT NOT NULL,
  request_fingerprint_sha256 TEXT NOT NULL,
  cache_outcome TEXT NOT NULL CHECK(cache_outcome IN ('miss','memory_hit','disk_hit','coalesced_wait','challenge_shortcut','signed_expired','not_applicable')),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fetch_transport_http_attempts (
  attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES fetch_transport_requests(request_id),
  attempt_index INTEGER NOT NULL CHECK(attempt_index >= 1),
  attempt_kind TEXT NOT NULL CHECK(attempt_kind IN ('primary','cookie_warmup','cookie_replay','provider_callback')),
  method TEXT NOT NULL CHECK(method='GET'), endpoint TEXT NOT NULL, http_status INTEGER,
  error_type TEXT, duration_ms REAL NOT NULL CHECK(duration_ms >= 0),
  admission_started_at_ms INTEGER, admitted_at_ms INTEGER, sent_at_ms INTEGER,
  outcome TEXT NOT NULL CHECK(outcome IN ('response','http_error','network_error')),
  created_at TEXT NOT NULL,
  CHECK((outcome='response' AND http_status IS NOT NULL AND error_type IS NULL)
     OR (outcome='http_error' AND http_status IS NOT NULL AND error_type IS NOT NULL)
     OR (outcome='network_error' AND http_status IS NULL AND error_type IS NOT NULL)),
  CHECK((admission_started_at_ms IS NULL AND admitted_at_ms IS NULL AND sent_at_ms IS NULL)
     OR (admission_started_at_ms IS NOT NULL AND admitted_at_ms IS NOT NULL AND sent_at_ms IS NOT NULL
         AND admission_started_at_ms <= admitted_at_ms AND admitted_at_ms <= sent_at_ms)),
  UNIQUE(request_id, attempt_index)
);
CREATE TABLE IF NOT EXISTS fetch_transport_failures (
  failure_id TEXT PRIMARY KEY, stage TEXT NOT NULL CHECK(stage IN ('open','logical_request','attempt')),
  ref_id TEXT, error_type TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

FETCH_TRANSPORT_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS fetch_transport_requests_no_update BEFORE UPDATE ON fetch_transport_requests BEGIN SELECT RAISE(ABORT, 'fetch transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fetch_transport_requests_no_delete BEFORE DELETE ON fetch_transport_requests BEGIN SELECT RAISE(ABORT, 'fetch transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fetch_transport_http_attempts_no_update BEFORE UPDATE ON fetch_transport_http_attempts BEGIN SELECT RAISE(ABORT, 'fetch transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fetch_transport_http_attempts_no_delete BEFORE DELETE ON fetch_transport_http_attempts BEGIN SELECT RAISE(ABORT, 'fetch transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fetch_transport_failures_no_update BEFORE UPDATE ON fetch_transport_failures BEGIN SELECT RAISE(ABORT, 'fetch transport is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fetch_transport_failures_no_delete BEFORE DELETE ON fetch_transport_failures BEGIN SELECT RAISE(ABORT, 'fetch transport is append-only'); END;
"""


def _text(name: str, value: Any) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError(f"fetch transport {name} must be nonempty text")
    return value


def insert_request(conn, payload: dict[str, Any], *, created_at: str) -> None:
    if set(payload) != {"request_id", "ref_id", "requested_endpoint", "profile", "strategy", "request_fingerprint_sha256", "cache_outcome"}:
        raise ValueError("fetch transport request is invalid")
    for key in ("request_id", "ref_id", "requested_endpoint", "profile", "strategy", "request_fingerprint_sha256"):
        _text(key, payload[key])
    fingerprint = payload["request_fingerprint_sha256"]
    if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
        raise ValueError("fetch transport request fingerprint is invalid")
    if payload["cache_outcome"] not in {"miss", "memory_hit", "disk_hit", "coalesced_wait", "challenge_shortcut", "signed_expired", "not_applicable"}:
        raise ValueError("fetch transport cache outcome is invalid")
    conn.execute("INSERT INTO fetch_transport_requests VALUES(?,?,?,?,?,?,?,?)", (
        payload["request_id"], payload["ref_id"], payload["requested_endpoint"],
        payload["profile"], payload["strategy"], fingerprint, payload["cache_outcome"], created_at,
    ))


def insert_attempt(conn, payload: dict[str, Any], *, created_at: str) -> None:
    required = {"attempt_id", "request_id", "attempt_index", "attempt_kind", "method", "endpoint", "status", "error_type", "duration_ms", "outcome"}
    allowed = required | {"admission_started_at_ms", "admitted_at_ms", "sent_at_ms"}
    if set(payload) - allowed or not required <= set(payload):
        raise ValueError("fetch transport attempt is invalid")
    for key in ("attempt_id", "request_id", "attempt_kind", "method", "endpoint", "outcome"):
        _text(key, payload[key])
    if type(payload["attempt_index"]) is not int or payload["attempt_index"] < 1:
        raise ValueError("fetch transport attempt index is invalid")
    if payload["attempt_kind"] not in {"primary", "cookie_warmup", "cookie_replay", "provider_callback"}:
        raise ValueError("fetch transport attempt kind is invalid")
    if payload["method"] != "GET":
        raise ValueError("fetch transport method is invalid")
    status = payload["status"]
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ValueError("fetch transport status is invalid")
    if payload["error_type"] is not None:
        _text("error_type", payload["error_type"])
    duration = payload["duration_ms"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        raise ValueError("fetch transport duration is invalid")
    if payload["outcome"] not in {"response", "http_error", "network_error"}:
        raise ValueError("fetch transport attempt outcome is invalid")
    if ((payload["outcome"] == "response" and (status is None or payload["error_type"] is not None))
            or (payload["outcome"] == "http_error" and (status is None or payload["error_type"] is None))
            or (payload["outcome"] == "network_error" and (status is not None or payload["error_type"] is None))):
        raise ValueError("fetch transport attempt detail is invalid")
    timing_names = ("admission_started_at_ms", "admitted_at_ms", "sent_at_ms")
    timings = tuple(payload.get(name) for name in timing_names)
    if any(value is None for value in timings):
        if any(value is not None for value in timings):
            raise ValueError("fetch transport attempt timing is incomplete")
    else:
        if any(type(value) is not int or value < 0 for value in timings):
            raise ValueError("fetch transport attempt timing is invalid")
        if not timings[0] <= timings[1] <= timings[2]:
            raise ValueError("fetch transport attempt timing is not monotonic")
    conn.execute("INSERT INTO fetch_transport_http_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        payload["attempt_id"], payload["request_id"], payload["attempt_index"], payload["attempt_kind"],
        payload["method"], payload["endpoint"], status, payload["error_type"], float(duration),
        *timings, payload["outcome"], created_at,
    ))


def insert_failure(conn, payload: dict[str, Any], *, created_at: str) -> None:
    if set(payload) != {"failure_id", "stage", "ref_id", "error_type"}:
        raise ValueError("fetch transport failure is invalid")
    for key in ("failure_id", "stage", "error_type"):
        _text(key, payload[key])
    if payload["ref_id"] is not None:
        _text("ref_id", payload["ref_id"])
    if payload["stage"] not in {"open", "logical_request", "attempt"}:
        raise ValueError("fetch transport failure stage is invalid")
    conn.execute("INSERT INTO fetch_transport_failures VALUES(?,?,?,?,?)", (
        payload["failure_id"], payload["stage"], payload["ref_id"], payload["error_type"], created_at,
    ))


def read_requests(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM fetch_transport_requests ORDER BY created_at, request_id")]


def read_attempts(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM fetch_transport_http_attempts ORDER BY created_at, request_id, attempt_index, attempt_id")]


def read_failures(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM fetch_transport_failures ORDER BY created_at, failure_id")]
