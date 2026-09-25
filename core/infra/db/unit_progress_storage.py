# core/infra/db/unit_progress_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only crash-resume markers for completed pipeline work units."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


UNIT_PROGRESS_GROUPS = frozenset({
    "fetch_auto",
    "fetch_ocr",
    "fetch_cache_seed",
    "resolve_discovery",
})

UNIT_PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS integrity_unit_completions (
  unit_group TEXT NOT NULL
    CHECK(unit_group IN ('fetch_auto','fetch_ocr','fetch_cache_seed','resolve_discovery')),
  unit_id TEXT NOT NULL CHECK(length(unit_id)>0 AND instr(unit_id,char(0))=0),
  payload_json TEXT NOT NULL,
  completed_at TEXT NOT NULL CHECK(length(completed_at)>0),
  PRIMARY KEY(unit_group, unit_id)
);
"""

UNIT_PROGRESS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS integrity_unit_completions_no_update
BEFORE UPDATE ON integrity_unit_completions BEGIN
  SELECT RAISE(ABORT, 'integrity_unit_completions is append-only');
END;
CREATE TRIGGER IF NOT EXISTS integrity_unit_completions_no_delete
BEFORE DELETE ON integrity_unit_completions BEGIN
  SELECT RAISE(ABORT, 'integrity_unit_completions is append-only');
END;
"""


def _text(value: Any, *, field: str) -> str:
    if type(value) is not str or not value or "\0" in value:
        raise ValueError(f"{field} must be nonempty NUL-free text")
    return value


def _payload_json(value: Any) -> str:
    if type(value) is not dict:
        raise ValueError("integrity unit payload must be an object")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def append_completion(
    conn: sqlite3.Connection,
    *,
    unit_group: str,
    unit_id: str,
    payload: dict[str, Any],
    completed_at: str,
) -> None:
    unit_group = _text(unit_group, field="integrity unit group")
    if unit_group not in UNIT_PROGRESS_GROUPS:
        raise ValueError("unsupported integrity unit group")
    unit_id = _text(unit_id, field="integrity unit id")
    completed_at = _text(completed_at, field="integrity unit completion time")
    encoded = _payload_json(payload)
    conn.execute(
        """
        INSERT INTO integrity_unit_completions(
          unit_group, unit_id, payload_json, completed_at
        ) VALUES(?, ?, ?, ?)
        """,
        (unit_group, unit_id, encoded, completed_at),
    )


def list_completions(
    conn: sqlite3.Connection, unit_group: str
) -> list[dict[str, Any]]:
    unit_group = _text(unit_group, field="integrity unit group")
    if unit_group not in UNIT_PROGRESS_GROUPS:
        raise ValueError("unsupported integrity unit group")
    rows = conn.execute(
        """
        SELECT unit_group, unit_id, payload_json, completed_at
        FROM integrity_unit_completions
        WHERE unit_group = ?
        ORDER BY rowid
        """,
        (unit_group,),
    ).fetchall()
    results = []
    for row in rows:
        payload = json.loads(row[2])
        if type(payload) is not dict or _payload_json(payload) != row[2]:
            raise RuntimeError("integrity unit payload is not canonical")
        results.append({
            "unit_group": row[0],
            "unit_id": row[1],
            "payload": payload,
            "completed_at": row[3],
        })
    return results
