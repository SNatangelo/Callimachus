# core/fetch/transport/semantic_scholar_pacing.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in cross-process admission and 429 state for Semantic Scholar only."""

from __future__ import annotations

import os
import sqlite3


ENV_PACING_DB = "CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB"
_HOST_SUFFIX = "semanticscholar.org"
_BASELINE_SECONDS = 5.0
_MAX_SECONDS = 300.0
_STABLE_SUCCESSES = 2
_DECREASE = 0.5
_DECAY_HALFLIFE_SECONDS = 24 * 3600.0


class PacingUnavailable(RuntimeError):
    """The explicitly requested shared pacing database cannot be used."""


def applies(host: str) -> bool:
    return host == _HOST_SUFFIX or host.endswith("." + _HOST_SUFFIX)


class SemanticScholarPacer:
    """SQLite-backed, atomic admission for the Semantic Scholar host family."""

    def __init__(self, path: str, *, wall_time_fn) -> None:
        if not os.path.isabs(path):
            raise ValueError("Semantic Scholar pacing database path must be absolute")
        self._path = path
        self._wall = wall_time_fn

    @classmethod
    def from_environ(cls, environ: dict[str, str], *, wall_time_fn):
        path = str(environ.get(ENV_PACING_DB) or "").strip()
        return cls(path, wall_time_fn=wall_time_fn) if path else None

    def _connect(self) -> sqlite3.Connection:
        try:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS semantic_scholar_pacing ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                "next_allowed REAL NOT NULL, cooldown_until REAL NOT NULL, "
                "last_applied_seconds REAL NOT NULL, in_cooldown INTEGER NOT NULL, "
                "post_cooldown_successes INTEGER NOT NULL, learned_seconds REAL NOT NULL, "
                "learned_updated_at REAL NOT NULL, last_admission_token INTEGER NOT NULL, "
                "cooldown_admission_cutoff INTEGER NOT NULL)"
            )
            return conn
        except (OSError, sqlite3.Error) as exc:
            raise PacingUnavailable("Semantic Scholar shared pacing is unavailable") from exc

    @staticmethod
    def _row(conn: sqlite3.Connection):
        row = conn.execute("SELECT * FROM semantic_scholar_pacing WHERE singleton=1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO semantic_scholar_pacing VALUES(1,0,0,0,0,0,0,0,0,0)"
            )
            row = conn.execute("SELECT * FROM semantic_scholar_pacing WHERE singleton=1").fetchone()
        return row

    @staticmethod
    def _decay_learned(conn: sqlite3.Connection, row, now: float):
        learned, updated_at = float(row[6]), float(row[7])
        if learned <= 0.0 or updated_at <= 0.0 or now <= updated_at:
            return row
        effective = learned * (_DECREASE ** ((now - updated_at) / _DECAY_HALFLIFE_SECONDS))
        effective = effective if effective >= _BASELINE_SECONDS else 0.0
        conn.execute(
            "UPDATE semantic_scholar_pacing SET learned_seconds=?, learned_updated_at=? WHERE singleton=1",
            (effective, now if effective > 0.0 else 0.0),
        )
        return conn.execute(
            "SELECT * FROM semantic_scholar_pacing WHERE singleton=1"
        ).fetchone()

    @staticmethod
    def _normalize_stale_open_episode(conn: sqlite3.Connection, row, now: float):
        """Turn an idle open episode into decayed learned state before admission."""
        stale_for = max(0.0, now - row[2])
        if not (bool(row[4]) and row[3] > 0.0 and stale_for >= row[3]):
            return row
        learned = min(row[3], _MAX_SECONDS) * (
            _DECREASE ** (stale_for / _DECAY_HALFLIFE_SECONDS)
        )
        learned = learned if learned >= _BASELINE_SECONDS else 0.0
        conn.execute(
            "UPDATE semantic_scholar_pacing SET learned_seconds=?, learned_updated_at=?, "
            "in_cooldown=0, post_cooldown_successes=0 WHERE singleton=1",
            (learned, now if learned > 0.0 else 0.0),
        )
        return conn.execute(
            "SELECT * FROM semantic_scholar_pacing WHERE singleton=1"
        ).fetchone()

    def acquire(
        self, *, rate: float, max_wait_seconds: float | None,
        decline_active_cooldown: bool = False,
    ) -> tuple[int, float]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn)
            now = self._wall()
            interval = 0.0 if rate <= 0.0 else 1.0 / rate
            slot = max(now, row[2]) if interval == 0.0 else max(now, row[1], row[2])
            wait = max(0.0, slot - now)
            if decline_active_cooldown and row[2] > now:
                conn.rollback()
                return 0, max(0.0, row[2] - now)
            if max_wait_seconds is not None and wait > max_wait_seconds:
                conn.rollback()
                return 0, wait
            row = self._normalize_stale_open_episode(conn, row, now)
            token = int(row[8]) + 1
            conn.execute(
                "UPDATE semantic_scholar_pacing SET next_allowed=?, last_admission_token=? WHERE singleton=1",
                (slot + interval, token),
            )
            conn.commit()
            return token, wait
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise PacingUnavailable("Semantic Scholar shared pacing is unavailable") from exc
        finally:
            conn.close()

    def cooldown(self, *, retry_after: float, admission_token: int | None) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn)
            now = self._wall()
            row = self._decay_learned(conn, row, now)
            floor = max(0.0, float(retry_after or 0.0))
            late = admission_token is not None and admission_token <= row[9]
            if late:
                if floor > row[3]:
                    applied = min(floor, _MAX_SECONDS)
                    conn.execute(
                        "UPDATE semantic_scholar_pacing SET last_applied_seconds=?, cooldown_until=? WHERE singleton=1",
                        (applied, max(row[2], now + applied)),
                    )
            else:
                if row[4] and row[3] > 0.0:
                    adaptive = min(row[3] * 2.0, _MAX_SECONDS)
                else:
                    adaptive = min(row[6], _MAX_SECONDS)
                applied = min(max(floor, adaptive) or _BASELINE_SECONDS, _MAX_SECONDS)
                conn.execute(
                    "UPDATE semantic_scholar_pacing SET cooldown_until=?, last_applied_seconds=?, "
                    "in_cooldown=1, post_cooldown_successes=0, cooldown_admission_cutoff=? WHERE singleton=1",
                    (max(row[2], now + applied), applied, row[8]),
                )
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise PacingUnavailable("Semantic Scholar shared pacing is unavailable") from exc
        finally:
            conn.close()

    def report_success(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn)
            row = self._decay_learned(conn, row, self._wall())
            if row[4]:
                successes = int(row[5]) + 1
                if successes >= _STABLE_SUCCESSES:
                    conn.execute(
                        "UPDATE semantic_scholar_pacing SET learned_seconds=?, learned_updated_at=?, "
                        "in_cooldown=0, post_cooldown_successes=0 WHERE singleton=1",
                        (row[3], self._wall()),
                    )
                else:
                    conn.execute(
                        "UPDATE semantic_scholar_pacing SET post_cooldown_successes=? WHERE singleton=1",
                        (successes,),
                    )
            elif row[6] > 0.0:
                successes = int(row[5]) + 1
                if successes >= _STABLE_SUCCESSES:
                    learned = row[6] * _DECREASE
                    conn.execute(
                        "UPDATE semantic_scholar_pacing SET learned_seconds=?, learned_updated_at=?, "
                        "post_cooldown_successes=0 WHERE singleton=1",
                        (learned if learned >= _BASELINE_SECONDS else 0.0, self._wall()),
                    )
                else:
                    conn.execute(
                        "UPDATE semantic_scholar_pacing SET post_cooldown_successes=? WHERE singleton=1",
                        (successes,),
                    )
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise PacingUnavailable("Semantic Scholar shared pacing is unavailable") from exc
        finally:
            conn.close()

    def snapshot(self) -> dict[str, float | bool]:
        conn = self._connect()
        try:
            row = self._row(conn)
            return {
                "next_allowed": row[1], "cooldown_until": row[2],
                "last_applied_seconds": row[3], "in_cooldown": bool(row[4]),
                "post_cooldown_successes": row[5], "learned_seconds": row[6],
                "learned_updated_at": row[7], "last_admission_token": row[8],
                "cooldown_admission_cutoff": row[9],
            }
        finally:
            conn.close()
