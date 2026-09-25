# core/infra/db/performance_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed write-once storage for the optional performance snapshot."""

from __future__ import annotations

import math
from typing import Any


def normalize_performance_spans(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if type(rows) is not list:
        raise ValueError("performance spans must be a list")
    normalized = []
    identities = set()
    for row in rows:
        if type(row) is not dict or set(row) != {
            "phase", "key", "count", "total_s", "max_s",
        }:
            raise ValueError("performance span has invalid fields")
        phase = row["phase"]
        key = row["key"]
        count = row["count"]
        total = row["total_s"]
        maximum = row["max_s"]
        if type(phase) is not str or not phase.strip() or "\0" in phase:
            raise ValueError("performance phase must be nonempty text")
        if key is not None and (
            type(key) is not str or not key.strip() or "\0" in key
        ):
            raise ValueError("performance key must be nonempty text or null")
        if type(count) is not int or isinstance(count, bool) or count < 1:
            raise ValueError("performance count must be a positive integer")
        if any(
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
            for value in (total, maximum)
        ) or maximum > total:
            raise ValueError("performance durations are invalid")
        identity = (phase, key)
        if identity in identities:
            raise ValueError("performance span identity is duplicated")
        identities.add(identity)
        normalized.append(dict(row))
    return normalized


def write_performance_spans(
    conn, rows: list[dict[str, Any]], *, session_id: str, recorded_at: str
) -> None:
    rows = normalize_performance_spans(rows)
    if type(session_id) is not str or not session_id:
        raise ValueError("performance session id is required")
    if not conn.execute(
        "SELECT 1 FROM run_sessions WHERE session_id=?", (session_id,)
    ).fetchone():
        raise ValueError("performance session does not exist")
    if conn.execute(
        "SELECT 1 FROM performance_snapshot WHERE session_id=?", (session_id,)
    ).fetchone():
        raise ValueError("performance snapshot is write-once for this session")
    conn.execute(
        "INSERT INTO performance_snapshot(session_id,recorded_at,span_count) VALUES(?,?,?)",
        (session_id, recorded_at, len(rows)),
    )
    conn.executemany(
        """
        INSERT INTO performance_spans(
          session_id, phase, span_key_present, span_key, sample_count,
          total_seconds, max_seconds, recorded_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            (
                session_id, row["phase"], int(row["key"] is not None), row["key"] or "",
                row["count"], row["total_s"], row["max_s"], recorded_at,
            )
            for row in rows
        ),
    )


def read_performance_spans(conn) -> list[dict[str, Any]]:
    inconsistent = conn.execute(
        """
        SELECT s.session_id
        FROM performance_snapshot AS s
        LEFT JOIN performance_spans AS p ON p.session_id=s.session_id
        GROUP BY s.session_id, s.span_count
        HAVING s.span_count != COUNT(p.session_id)
            OR SUM(CASE WHEN p.recorded_at != s.recorded_at THEN 1 ELSE 0 END) != 0
        UNION ALL
        SELECT p.session_id
        FROM performance_spans AS p
        LEFT JOIN performance_snapshot AS s ON s.session_id=p.session_id
        WHERE s.session_id IS NULL
        """
    ).fetchone()
    if inconsistent is not None:
        raise RuntimeError("performance snapshots are inconsistent")
    rows = [
        {
            "phase": row["phase"],
            "key": row["span_key"] if row["span_key_present"] else None,
            "count": row["count"],
            "total_s": row["total_s"],
            "max_s": row["max_s"],
            "recorded_at": row["recorded_at"],
        }
        for row in conn.execute(
            """
            SELECT phase, span_key_present, span_key,
                   SUM(sample_count) AS count, SUM(total_seconds) AS total_s,
                   MAX(max_seconds) AS max_s, MAX(recorded_at) AS recorded_at
            FROM performance_spans
            GROUP BY phase, span_key_present, span_key
            ORDER BY total_s DESC, phase, span_key
            """
        )
    ]
    return rows
