# core/infra/db/task_answer_provenance_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Typed provenance for task answers admitted by the trusted boundary."""
from __future__ import annotations

import re
import sqlite3
from typing import Any


TASK_ANSWER_PROVENANCE_DDL = r"""
CREATE TABLE IF NOT EXISTS task_answer_provenance (
    answer_id TEXT PRIMARY KEY REFERENCES task_answers(answer_id) ON DELETE CASCADE,
    producer_class TEXT NOT NULL CHECK(producer_class IN ('operator','agent','automation')),
    producer_identity TEXT NOT NULL CHECK(
        length(trim(producer_identity)) > 0 AND instr(producer_identity, char(0)) = 0
    ),
    authenticated_uid INTEGER NOT NULL CHECK(
        typeof(authenticated_uid) = 'integer' AND authenticated_uid >= 0
    ),
    ingress_kind TEXT NOT NULL CHECK(ingress_kind IN (
        'controlled_metadata','controlled_inline','controlled_file','controlled_mixed'
    )),
    admitted_at TEXT NOT NULL CHECK(
        length(trim(admitted_at)) > 0 AND instr(admitted_at, char(0)) = 0
    ),
    file_count INTEGER NOT NULL CHECK(
        typeof(file_count) = 'integer' AND file_count >= 0
    ),
    authority_id TEXT NOT NULL CHECK(
        length(trim(authority_id)) > 0 AND instr(authority_id, char(0)) = 0
    )
);

CREATE TABLE IF NOT EXISTS task_answer_provenance_files (
    answer_id TEXT NOT NULL REFERENCES task_answer_provenance(answer_id) ON DELETE CASCADE,
    file_order INTEGER NOT NULL CHECK(
        typeof(file_order) = 'integer' AND file_order >= 0
    ),
    original_name TEXT NOT NULL CHECK(
        length(trim(original_name)) > 0 AND instr(original_name, char(0)) = 0
    ),
    stored_path TEXT NOT NULL CHECK(
        length(trim(stored_path)) > 0 AND instr(stored_path, char(0)) = 0
    ),
    sha256 TEXT NOT NULL CHECK(
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    byte_count INTEGER NOT NULL CHECK(
        typeof(byte_count) = 'integer' AND byte_count >= 0
    ),
    PRIMARY KEY(answer_id, file_order),
    UNIQUE(answer_id, stored_path)
);

CREATE TRIGGER IF NOT EXISTS task_answer_provenance_no_update
BEFORE UPDATE ON task_answer_provenance
BEGIN SELECT RAISE(ABORT, 'task answer provenance is append-only'); END;
CREATE TRIGGER IF NOT EXISTS task_answer_provenance_no_delete
BEFORE DELETE ON task_answer_provenance
BEGIN SELECT RAISE(ABORT, 'task answer provenance is append-only'); END;
CREATE TRIGGER IF NOT EXISTS task_answer_provenance_files_no_update
BEFORE UPDATE ON task_answer_provenance_files
BEGIN SELECT RAISE(ABORT, 'task answer provenance files are append-only'); END;
CREATE TRIGGER IF NOT EXISTS task_answer_provenance_files_no_delete
BEFORE DELETE ON task_answer_provenance_files
BEGIN SELECT RAISE(ABORT, 'task answer provenance files are append-only'); END;
"""


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PRODUCER_CLASSES = {"operator", "agent", "automation"}
_INGRESS_KINDS = {
    "controlled_metadata",
    "controlled_inline",
    "controlled_file",
    "controlled_mixed",
}


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{label} must be non-empty NUL-free text")
    return value.strip()


def insert_provenance(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    files: list[dict[str, Any]],
) -> None:
    expected = {
        "answer_id",
        "producer_class",
        "producer_identity",
        "authenticated_uid",
        "ingress_kind",
        "admitted_at",
        "file_count",
        "authority_id",
    }
    if not isinstance(record, dict) or set(record) != expected:
        raise ValueError("task answer provenance has unsupported fields")
    for field in ("answer_id", "producer_identity", "admitted_at", "authority_id"):
        _text(record[field], field)
    if record["producer_class"] not in _PRODUCER_CLASSES:
        raise ValueError("task answer producer class is invalid")
    if record["ingress_kind"] not in _INGRESS_KINDS:
        raise ValueError("task answer ingress kind is invalid")
    if type(record["authenticated_uid"]) is not int or record["authenticated_uid"] < 0:
        raise ValueError("authenticated_uid must be a nonnegative integer")
    if type(record["file_count"]) is not int or record["file_count"] != len(files):
        raise ValueError("task answer provenance file count is invalid")

    conn.execute(
        """
        INSERT INTO task_answer_provenance(
            answer_id, producer_class, producer_identity, authenticated_uid,
            ingress_kind, admitted_at, file_count, authority_id
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        tuple(record[field] for field in (
            "answer_id", "producer_class", "producer_identity", "authenticated_uid",
            "ingress_kind", "admitted_at", "file_count", "authority_id",
        )),
    )
    seen_paths: set[str] = set()
    for order, file_record in enumerate(files):
        if not isinstance(file_record, dict) or set(file_record) != {
            "original_name", "stored_path", "sha256", "byte_count"
        }:
            raise ValueError("task answer provenance file has unsupported fields")
        original_name = _text(file_record["original_name"], "original_name")
        stored_path = _text(file_record["stored_path"], "stored_path")
        if (
            stored_path in seen_paths
            or stored_path.startswith("/")
            or "\\" in stored_path
            or any(part in {"", ".", ".."} for part in stored_path.split("/"))
        ):
            raise ValueError("task answer provenance stored path is invalid")
        seen_paths.add(stored_path)
        if not _HASH_RE.fullmatch(str(file_record["sha256"])):
            raise ValueError("task answer provenance SHA-256 is invalid")
        byte_count = file_record["byte_count"]
        if type(byte_count) is not int or byte_count < 0:
            raise ValueError("task answer provenance byte count is invalid")
        conn.execute(
            "INSERT INTO task_answer_provenance_files VALUES(?,?,?,?,?,?)",
            (
                record["answer_id"], order, original_name, stored_path,
                file_record["sha256"], byte_count,
            ),
        )


def read_provenance(
    conn: sqlite3.Connection, answer_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM task_answer_provenance WHERE answer_id=?", (answer_id,)
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["files"] = [
        {
            "original_name": child["original_name"],
            "stored_path": child["stored_path"],
            "sha256": child["sha256"],
            "byte_count": child["byte_count"],
        }
        for child in conn.execute(
            """
            SELECT original_name, stored_path, sha256, byte_count
            FROM task_answer_provenance_files
            WHERE answer_id=? ORDER BY file_order
            """,
            (answer_id,),
        )
    ]
    if record["file_count"] != len(record["files"]):
        raise RuntimeError("task answer provenance file inventory is incomplete")
    return record
