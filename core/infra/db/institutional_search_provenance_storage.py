# core/infra/db/institutional_search_provenance_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only provenance for institutional web-search decisions."""

from __future__ import annotations

import re
from typing import Any


INSTITUTIONAL_SEARCH_PROVENANCE_DDL = """
CREATE TABLE IF NOT EXISTS institutional_search_provenance (
  search_id TEXT PRIMARY KEY,
  ref_id TEXT NOT NULL REFERENCES operational_references(ref_id),
  query_sha256 TEXT NOT NULL CHECK(length(query_sha256)=64 AND query_sha256 NOT GLOB '*[^0-9a-f]*'),
  outcome TEXT NOT NULL CHECK(outcome IN ('answered','refused','no_answer')),
  selected_backend TEXT,
  candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS institutional_search_backend_attempts (
  search_id TEXT NOT NULL REFERENCES institutional_search_provenance(search_id),
  backend_order INTEGER NOT NULL CHECK(backend_order >= 0), backend TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN ('answered','refused')),
  transport_outcome TEXT CHECK(transport_outcome IN ('response','http_error','network_error')),
  http_status INTEGER, PRIMARY KEY(search_id, backend_order),
  CHECK(
    (transport_outcome IS NULL AND http_status IS NULL)
    OR (transport_outcome IN ('response','http_error') AND typeof(http_status)='integer' AND http_status BETWEEN 100 AND 599)
    OR (transport_outcome='network_error' AND http_status IS NULL)
  )
);
CREATE TABLE IF NOT EXISTS institutional_search_candidates (
  search_id TEXT NOT NULL REFERENCES institutional_search_provenance(search_id),
  candidate_order INTEGER NOT NULL CHECK(candidate_order >= 0), title TEXT NOT NULL, url TEXT NOT NULL,
  PRIMARY KEY(search_id, candidate_order)
);
"""

INSTITUTIONAL_SEARCH_PROVENANCE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS institutional_search_provenance_no_update BEFORE UPDATE ON institutional_search_provenance BEGIN SELECT RAISE(ABORT, 'institutional search provenance is append-only'); END;
CREATE TRIGGER IF NOT EXISTS institutional_search_provenance_no_delete BEFORE DELETE ON institutional_search_provenance BEGIN SELECT RAISE(ABORT, 'institutional search provenance is append-only'); END;
CREATE TRIGGER IF NOT EXISTS institutional_search_backend_attempts_no_update BEFORE UPDATE ON institutional_search_backend_attempts BEGIN SELECT RAISE(ABORT, 'institutional search provenance is append-only'); END;
CREATE TRIGGER IF NOT EXISTS institutional_search_backend_attempts_no_delete BEFORE DELETE ON institutional_search_backend_attempts BEGIN SELECT RAISE(ABORT, 'institutional search provenance is append-only'); END;
CREATE TRIGGER IF NOT EXISTS institutional_search_candidates_no_update BEFORE UPDATE ON institutional_search_candidates BEGIN SELECT RAISE(ABORT, 'institutional search provenance is append-only'); END;
CREATE TRIGGER IF NOT EXISTS institutional_search_candidates_no_delete BEFORE DELETE ON institutional_search_candidates BEGIN SELECT RAISE(ABORT, 'institutional search provenance is append-only'); END;
"""


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value.strip() or "\0" in value:
        raise ValueError(f"institutional search {name} must be nonempty text")
    return value


def insert(conn, payload: dict[str, Any], *, created_at: str) -> None:
    required = {"search_id", "ref_id", "query_sha256", "outcome", "attempts", "candidates"}
    allowed = required | {"selected_backend"}
    if type(payload) is not dict or not required <= set(payload) or set(payload) - allowed:
        raise ValueError("institutional search provenance is invalid")
    search_id, ref_id = _text("search_id", payload["search_id"]), _text("ref_id", payload["ref_id"])
    fingerprint = payload["query_sha256"]
    if type(fingerprint) is not str or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("institutional search query fingerprint is invalid")
    outcome, selected = payload["outcome"], payload.get("selected_backend")
    attempts, candidates = payload["attempts"], payload["candidates"]
    if outcome not in {"answered", "refused", "no_answer"} or type(attempts) is not list or type(candidates) is not list:
        raise ValueError("institutional search provenance is invalid")
    if selected is not None:
        _text("selected backend", selected)
    if (outcome == "answered" and (selected is None or not attempts)) or (outcome != "answered" and (selected is not None or candidates)):
        raise ValueError("institutional search outcome is invalid")
    normalized_attempts = []
    for order, attempt in enumerate(attempts):
        if type(attempt) is not dict or set(attempt) - {"backend", "outcome", "transport_outcome", "http_status"}:
            raise ValueError("institutional search backend attempt is invalid")
        backend = _text("backend", attempt.get("backend"))
        attempt_outcome, transport_outcome, status = attempt.get("outcome"), attempt.get("transport_outcome"), attempt.get("http_status")
        status_is_http = type(status) is int and 100 <= status <= 599
        transport_is_valid = (
            (transport_outcome is None and status is None)
            or (transport_outcome in {"response", "http_error"} and status_is_http)
            or (transport_outcome == "network_error" and status is None)
        )
        if attempt_outcome not in {"answered", "refused"} or not transport_is_valid:
            raise ValueError("institutional search backend attempt is invalid")
        normalized_attempts.append((search_id, order, backend, attempt_outcome, transport_outcome, status))
    if selected is not None and not any(row[2] == selected and row[3] == "answered" for row in normalized_attempts):
        raise ValueError("institutional search selected backend is invalid")
    normalized_candidates = []
    for order, candidate in enumerate(candidates):
        if type(candidate) is not dict or set(candidate) - {"title", "url"}:
            raise ValueError("institutional search candidate is invalid")
        title, url = candidate.get("title"), candidate.get("url")
        if type(title) is not str or "\0" in title or type(url) is not str or not url.strip() or "\0" in url:
            raise ValueError("institutional search candidate is invalid")
        normalized_candidates.append((search_id, order, title, url))
    conn.execute("INSERT INTO institutional_search_provenance VALUES(?,?,?,?,?,?,?)", (search_id, ref_id, fingerprint, outcome, selected, len(candidates), created_at))
    conn.executemany("INSERT INTO institutional_search_backend_attempts VALUES(?,?,?,?,?,?)", normalized_attempts)
    conn.executemany("INSERT INTO institutional_search_candidates VALUES(?,?,?,?)", normalized_candidates)


def read(conn) -> list[dict[str, Any]]:
    rows = []
    for row in conn.execute("SELECT * FROM institutional_search_provenance ORDER BY created_at, search_id"):
        record = dict(row)
        record["attempts"] = [dict(item) for item in conn.execute("SELECT backend, outcome, transport_outcome, http_status FROM institutional_search_backend_attempts WHERE search_id=? ORDER BY backend_order", (record["search_id"],))]
        record["candidates"] = [dict(item) for item in conn.execute("SELECT title, url FROM institutional_search_candidates WHERE search_id=? ORDER BY candidate_order", (record["search_id"],))]
        rows.append(record)
    return rows
