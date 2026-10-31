# tests/infra/test_task_answer_provenance_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import sqlite3

import pytest

from core.infra.db.task_answer_provenance_storage import (
    TASK_ANSWER_PROVENANCE_DDL,
    insert_provenance,
    read_provenance,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE task_answers(answer_id TEXT PRIMARY KEY)")
    conn.executescript(TASK_ANSWER_PROVENANCE_DDL)
    conn.execute("INSERT INTO task_answers VALUES('answer-1')")
    return conn


def _record() -> dict:
    return {
        "answer_id": "answer-1",
        "producer_class": "agent",
        "producer_identity": "codex",
        "authenticated_uid": 1001,
        "ingress_kind": "controlled_file",
        "admitted_at": "2026-08-22T12:00:00+00:00",
        "file_count": 1,
        "authority_id": "authority-1",
    }


def _files() -> list[dict]:
    return [{
        "original_name": "paper.pdf",
        "stored_path": ".integrity-inbox/answer-1/abc.pdf",
        "sha256": "a" * 64,
        "byte_count": 123,
    }]


def test_task_answer_provenance_round_trips_typed_agent_identity_and_files():
    conn = _connection()
    try:
        insert_provenance(conn, _record(), _files())
        observed = read_provenance(conn, "answer-1")
    finally:
        conn.close()

    assert observed == {**_record(), "files": _files()}


def test_task_answer_provenance_rejects_client_shape_and_is_append_only():
    conn = _connection()
    try:
        malformed = {**_record(), "claimed_actor": "user"}
        with pytest.raises(ValueError, match="unsupported fields"):
            insert_provenance(conn, malformed, _files())
        insert_provenance(conn, _record(), _files())
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE task_answer_provenance SET producer_identity='user'"
            )
    finally:
        conn.close()


def test_task_answer_provenance_rejects_noncanonical_stored_paths():
    conn = _connection()
    try:
        files = [{**_files()[0], "stored_path": "safe/../../outside.pdf"}]
        with pytest.raises(ValueError, match="stored path"):
            insert_provenance(conn, _record(), files)
    finally:
        conn.close()
