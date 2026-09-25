# core/infra/llm_runtime/schema_bootstrap.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Exact-current schema bootstrap and preflight for runtime state."""

from __future__ import annotations

import sqlite3

from .connection import RuntimeStateError
from .schema import (
    APPEND_ONLY_SQL,
    IMMUTABLE_TABLES,
    REQUIRED_COLUMNS,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)

_WRITE_PROBE_KEY = "__llm_runtime_write_probe__"


def _run_sql(conn: sqlite3.Connection, script: str) -> None:
    pending = ""
    for line in script.splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            if pending.strip():
                conn.execute(pending)
            pending = ""
    if pending.strip():
        raise RuntimeStateError("frozen schema SQL is incomplete")


def _normal_sql(value: str | None) -> str:
    return "".join((value or "").replace("IF NOT EXISTS", "").lower().split())


def _schema_signature(conn: sqlite3.Connection) -> tuple[object, ...]:
    tables = tuple(
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )
    parts: list[object] = [tables]
    for table in tables:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        info = tuple(tuple(row) for row in conn.execute(f"PRAGMA table_info({table})"))
        foreign_keys = tuple(
            tuple(row) for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        )
        indexes = []
        for index in conn.execute(f"PRAGMA index_list({table})"):
            name = index[1]
            indexes.append(
                (
                    tuple(index[2:]),
                    tuple(
                        tuple(row)
                        for row in conn.execute(f"PRAGMA index_info({name})")
                    ),
                )
            )
        parts.append(
            (table, _normal_sql(sql), info, foreign_keys, tuple(sorted(indexes, key=repr)))
        )
    triggers = tuple(
        sorted(
            (row[0], _normal_sql(row[1]))
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            )
        )
    )
    return tuple(parts) + (triggers,)


def _reference_signature(schema: str, triggers: str) -> tuple[object, ...]:
    reference = sqlite3.connect(":memory:")
    try:
        _run_sql(reference, schema)
        _run_sql(reference, triggers)
        return _schema_signature(reference)
    finally:
        reference.close()


_CURRENT_SIGNATURE = _reference_signature(SCHEMA_SQL, APPEND_ONLY_SQL)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Bootstrap an empty database or require the exact current schema."""
    try:
        _check_integrity(conn)
        tables = _table_names(conn)
        if not tables:
            _bootstrap(conn)
        _require_current_schema(conn)
        _check_writable(conn)
    except RuntimeStateError:
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise RuntimeStateError("LLM runtime state preflight failed") from exc


def preflight_existing_readonly(conn: sqlite3.Connection) -> None:
    """Require the exact current schema without mutating an existing database."""
    try:
        _check_integrity(conn)
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeStateError("LLM runtime state foreign key check failed")
        _require_current_schema(conn)
    except RuntimeStateError:
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise RuntimeStateError("LLM runtime state preflight failed") from exc


def _check_integrity(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if row is None or row[0] != "ok":
        raise RuntimeStateError("LLM runtime state integrity check failed")


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _bootstrap(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        _run_sql(conn, SCHEMA_SQL)
        _run_sql(conn, APPEND_ONLY_SQL)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _require_current_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        raise RuntimeStateError("LLM runtime schema version is missing")
    try:
        version = int(row[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeStateError("LLM runtime schema version is invalid") from exc
    if version > SCHEMA_VERSION:
        raise RuntimeStateError("LLM runtime schema is newer than this software")
    if version != SCHEMA_VERSION:
        raise RuntimeStateError("LLM runtime schema is not current")
    if _schema_signature(conn) != _CURRENT_SIGNATURE:
        raise RuntimeStateError("LLM runtime schema is incomplete")
    if not REQUIRED_COLUMNS.keys() <= _table_names(conn):
        raise RuntimeStateError("LLM runtime schema is incomplete")
    for table, expected in REQUIRED_COLUMNS.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if actual != expected:
            raise RuntimeStateError("LLM runtime schema is incomplete")
    triggers = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    expected_triggers = {
        f"{table}_{operation}"
        for table in IMMUTABLE_TABLES
        for operation in ("no_update", "no_delete")
    }
    if not expected_triggers <= triggers:
        raise RuntimeStateError("LLM runtime schema is incomplete")


def _check_writable(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, '1')",
            (_WRITE_PROBE_KEY,),
        )
        conn.execute("DELETE FROM meta WHERE key = ?", (_WRITE_PROBE_KEY,))
        conn.execute("ROLLBACK")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
