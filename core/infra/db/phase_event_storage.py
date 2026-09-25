# core/infra/db/phase_event_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed relational persistence for phase-event audit details."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping


JsonDict = dict[str, Any]

_PHASES = frozenset({
    "parse", "resolve", "resolve_repair", "fetch", "gaps", "style", "verify",
    "web_research", "report", "done",
})
_EVENT_TYPES = frozenset({"enter", "exit", "pause", "resume", "fail", "done"})
_DETAIL_KINDS = frozenset({
    "none", "session_created", "session_resumed", "pause", "restart",
    "gate_failure", "frozen_fork",
})
_SHA256 = frozenset("0123456789abcdef")


def _exact(payload: Any, fields: set[str]) -> JsonDict:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("phase event payload has an unsupported shape")
    return dict(payload)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def normalize_phase_event(
    phase: Any, event_type: Any, session_id: Any, payload: Any,
) -> tuple[str, str, str | None, str, JsonDict | None]:
    """Validate a public event projection and return its closed detail form."""
    if phase not in _PHASES or event_type not in _EVENT_TYPES:
        raise ValueError("unsupported phase event")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("session_id must be text or null")
    if payload is None and event_type in {"enter", "exit", "fail", "done"}:
        return phase, event_type, session_id, "none", None
    if payload is None:
        raise ValueError("phase event detail is required")
    if not isinstance(payload, dict):
        raise ValueError("phase event payload must be an object or null")
    if event_type == "enter" and set(payload) == {"created_via"}:
        detail = _exact(payload, {"created_via"})
        if detail["created_via"] != "core.run":
            raise ValueError("created_via is unsupported")
        if session_id is None:
            raise ValueError("session_created event requires session_id")
        return phase, event_type, session_id, "session_created", detail
    if event_type == "resume" and set(payload) == {"resumed_via"}:
        detail = _exact(payload, {"resumed_via"})
        if detail["resumed_via"] != "core.run":
            raise ValueError("resumed_via is unsupported")
        if session_id is None:
            raise ValueError("session_resumed event requires session_id")
        return phase, event_type, session_id, "session_resumed", detail
    if event_type == "pause":
        detail = _exact(payload, {"slot", "pending_tasks"})
        if detail["slot"] not in {"fetch", "research", "verify", "parse_review"}:
            raise ValueError("pause slot is unsupported")
        _nonnegative_int(detail["pending_tasks"], "pending_tasks")
        return phase, event_type, session_id, "pause", detail
    if event_type == "resume":
        detail = _exact(payload, {"restart_reason"})
        _text(detail["restart_reason"], "restart_reason")
        if phase != "parse" or session_id is not None:
            raise ValueError("restart event must be parse/resume")
        return phase, event_type, session_id, "restart", detail
    if event_type == "fail":
        detail = _exact(payload, {"reason"})
        if detail["reason"] != "gate_failed":
            raise ValueError("gate_failure reason is unsupported")
        return phase, event_type, session_id, "gate_failure", detail
    if event_type == "enter":
        detail = _exact(payload, {"run_origin", "parent_run_id", "source_inventory_sha256"})
        if detail["run_origin"] not in {
            "forked_from_frozen_fetch",
            "forked_from_completed_verify",
            "forked_from_reference_only",
        }:
            raise ValueError("frozen_fork run_origin is unsupported")
        expected_phase = (
            "fetch" if detail["run_origin"] == "forked_from_reference_only"
            else "verify"
        )
        if phase != expected_phase or session_id is not None:
            raise ValueError(f"fork event must be {expected_phase}/enter")
        _text(detail["parent_run_id"], "parent_run_id")
        digest = detail["source_inventory_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in _SHA256 for c in digest):
            raise ValueError("source_inventory_sha256 must be a lowercase SHA-256")
        return phase, event_type, session_id, "frozen_fork", detail
    raise ValueError("payload is not valid for this phase event")


def insert_phase_event(
    conn: sqlite3.Connection, *, phase: Any, event_type: Any, created_at: str,
    session_id: Any = None, payload: Any = None,
) -> int:
    phase, event_type, session_id, kind, detail = normalize_phase_event(
        phase, event_type, session_id, payload,
    )
    cur = conn.execute(
        "INSERT INTO phase_events(phase,event_type,created_at,session_id,detail_kind) VALUES(?,?,?,?,?)",
        (phase, event_type, created_at, session_id, kind),
    )
    event_id = int(cur.lastrowid)
    if kind == "session_created":
        conn.execute("INSERT INTO phase_event_session_created(event_id,created_via) VALUES(?,?)", (event_id, detail["created_via"]))
    elif kind == "session_resumed":
        conn.execute("INSERT INTO phase_event_session_resumed(event_id,resumed_via) VALUES(?,?)", (event_id, detail["resumed_via"]))
    elif kind == "pause":
        conn.execute("INSERT INTO phase_event_pause(event_id,slot,pending_tasks) VALUES(?,?,?)", (event_id, detail["slot"], detail["pending_tasks"]))
    elif kind == "restart":
        conn.execute("INSERT INTO phase_event_restart(event_id,restart_reason) VALUES(?,?)", (event_id, detail["restart_reason"]))
    elif kind == "gate_failure":
        conn.execute("INSERT INTO phase_event_gate_failure(event_id,reason) VALUES(?,?)", (event_id, detail["reason"]))
    elif kind == "frozen_fork":
        conn.execute("INSERT INTO phase_event_frozen_fork(event_id,run_origin,parent_run_id,source_inventory_sha256) VALUES(?,?,?,?)", (event_id, detail["run_origin"], detail["parent_run_id"], detail["source_inventory_sha256"]))
    return event_id


def read_phase_event(conn: sqlite3.Connection, row: Mapping[str, Any]) -> JsonDict | None:
    kind = row["detail_kind"]
    if kind not in _DETAIL_KINDS:
        raise RuntimeError("phase event detail kind is unsupported")
    tables = (
        "phase_event_session_created", "phase_event_session_resumed", "phase_event_pause",
        "phase_event_restart", "phase_event_gate_failure", "phase_event_frozen_fork",
    )
    actual = [table for table in tables if conn.execute(
        f"SELECT 1 FROM {table} WHERE event_id=?", (row["event_id"],)
    ).fetchone() is not None]
    if kind == "none":
        if actual:
            raise RuntimeError("phase event has unexpected detail rows")
        try:
            normalize_phase_event(row["phase"], row["event_type"], row["session_id"], None)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("phase event parent contradicts event contract") from exc
        return None
    table, fields = {
        "session_created": ("phase_event_session_created", ("created_via",)),
        "session_resumed": ("phase_event_session_resumed", ("resumed_via",)),
        "pause": ("phase_event_pause", ("slot", "pending_tasks")),
        "restart": ("phase_event_restart", ("restart_reason",)),
        "gate_failure": ("phase_event_gate_failure", ("reason",)),
        "frozen_fork": ("phase_event_frozen_fork", ("run_origin", "parent_run_id", "source_inventory_sha256")),
    }[kind]
    if actual != [table]:
        raise RuntimeError("phase event detail rows contradict detail kind")
    detail = conn.execute(f"SELECT * FROM {table} WHERE event_id=?", (row["event_id"],)).fetchone()
    payload = {field: detail[field] for field in fields}
    try:
        normalize_phase_event(row["phase"], row["event_type"], row["session_id"], payload)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("phase event detail contradicts event contract") from exc
    return payload
