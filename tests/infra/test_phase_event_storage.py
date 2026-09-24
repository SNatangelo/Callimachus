# tests/infra/test_phase_event_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Current typed phase-event storage contract."""
from __future__ import annotations

import sqlite3

import pytest

from core.infra.db.phase_event_storage import insert_phase_event, read_phase_event
from core.infra.db.schema import APPEND_ONLY_TRIGGERS_SQL, SCHEMA_SQL


def _fresh() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.executescript(APPEND_ONLY_TRIGGERS_SQL)
    conn.execute("INSERT INTO run_sessions VALUES('s','t','t',NULL,'active',NULL,NULL)")
    return conn


def test_current_schema_has_closed_event_relations() -> None:
    conn = _fresh()
    assert "payload_json" not in {row[1] for row in conn.execute("PRAGMA table_info(phase_events)")}


@pytest.mark.parametrize("phase,event_type,session_id,payload", [
    ("parse", "enter", "s", {"created_via": "core.run"}),
    ("fetch", "pause", "s", {"slot": "fetch", "pending_tasks": 0}),
    ("parse", "resume", None, {"restart_reason": "retry"}),
    ("done", "done", "s", None),
])
def test_typed_phase_events_roundtrip(phase: str, event_type: str, session_id: str | None, payload: dict | None) -> None:
    conn = _fresh()
    event_id = insert_phase_event(conn, phase=phase, event_type=event_type, created_at="t", session_id=session_id, payload=payload)
    assert read_phase_event(conn, conn.execute("SELECT * FROM phase_events WHERE event_id=?", (event_id,)).fetchone()) == payload


def test_typed_phase_event_rejects_invalid_payload_before_insert() -> None:
    conn = _fresh()
    with pytest.raises(ValueError):
        insert_phase_event(conn, phase="fetch", event_type="pause", created_at="t", session_id="s", payload={"slot": "fetch", "pending_tasks": True})
    assert conn.execute("SELECT count(*) FROM phase_events").fetchone()[0] == 0


def test_typed_phase_event_reader_fails_closed_on_missing_detail() -> None:
    conn = _fresh()
    conn.execute("INSERT INTO phase_events(phase,event_type,created_at,session_id,detail_kind) VALUES('fetch','pause','t','s','pause')")
    with pytest.raises(RuntimeError, match="contradict detail kind"):
        read_phase_event(conn, conn.execute("SELECT * FROM phase_events").fetchone())
