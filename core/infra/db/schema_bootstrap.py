#!/usr/bin/env python3
# core/infra/db/schema_bootstrap.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Current-schema bootstrap and validation for DB-native runs."""

from __future__ import annotations

import re
import sqlite3

from .schema import APPEND_ONLY_TRIGGERS_SQL, SCHEMA_SQL, SCHEMA_VERSION

_REQUIRED_CURRENT_SCHEMA_OBJECTS = tuple(re.findall(
    r"CREATE\s+(?:TABLE|TRIGGER)\s+IF\s+NOT\s+EXISTS\s+([A-Za-z0-9_]+)",
    SCHEMA_SQL + APPEND_ONLY_TRIGGERS_SQL, flags=re.IGNORECASE,
))
_FORBIDDEN_CURRENT_OBJECTS = (
    "citation_projections", "verdict_attempts", "jury_log", "run_settings",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Bootstrap an empty database; reject every existing database."""
    objects = conn.execute(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if objects:
        raise RuntimeError("existing run database is unsupported; create a fresh run")
    conn.execute("BEGIN IMMEDIATE")
    try:
        _execute_sql_script(conn, SCHEMA_SQL)
        _execute_sql_script(conn, APPEND_ONLY_TRIGGERS_SQL)
        set_schema_version(conn, SCHEMA_VERSION)
        conn.execute(
            "INSERT INTO verification_ledger_metadata(singleton, fingerprint_version) "
            "VALUES(1, 'claim-evidence-fingerprint-v2')"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
def inspect_schema_version(conn: sqlite3.Connection) -> int:
    meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if meta is None:
        raise RuntimeError("run database has no schema metadata")
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        raise RuntimeError("run database has no schema_version metadata")
    try:
        return int(row["value"])
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise RuntimeError("invalid schema_version in meta table") from exc


def require_current_schema_version(conn: sqlite3.Connection) -> int:
    version = inspect_schema_version(conn)
    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is unsupported (current: {SCHEMA_VERSION})"
        )
    return version


def require_current_schema_structure(conn: sqlite3.Connection) -> int:
    version = require_current_schema_version(conn)
    present = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
    )}
    missing = sorted(set(_REQUIRED_CURRENT_SCHEMA_OBJECTS) - present)
    if missing:
        raise RuntimeError("database schema is missing required current objects: " + ", ".join(missing))
    removed = sorted(set(_FORBIDDEN_CURRENT_OBJECTS) & present)
    if removed:
        raise RuntimeError("current schema contains removed objects: " + ", ".join(removed))
    return version


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),)
    )


def _execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete SQLite schema statement")
