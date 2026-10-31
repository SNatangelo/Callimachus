# tests/infra/llm_runtime/test_schema.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import sqlite3

import pytest

from core.infra.llm_runtime.schema_bootstrap import ensure_schema
from core.infra.llm_runtime.schema import IMMUTABLE_TABLES, REQUIRED_COLUMNS


def _connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


def test_schema_has_minimum_relations_and_exact_required_columns():
    conn = _connection()
    names = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "credentials",
        "credential_models",
        "cooldown_events",
        "credential_state",
        "backoff_profiles",
        "model_observations",
    } <= names
    for table, expected in REQUIRED_COLUMNS.items():
        actual = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        assert actual == expected


def test_audit_relations_have_update_and_delete_guards():
    conn = _connection()
    triggers = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    assert {
        f"{table}_{operation}"
        for table in IMMUTABLE_TABLES
        for operation in ("no_update", "no_delete")
    } <= triggers
    conn.execute(
        "INSERT INTO credentials VALUES(?,?,?,?,?)",
        ("p:credentialless", "p", "p:credentialless", None, "t"),
    )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE credentials SET provider_id = 'other'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM credentials")


def test_current_lane_projection_remains_mutable():
    conn = _connection()
    conn.execute(
        "INSERT INTO credentials VALUES(?,?,?,?,?)",
        ("p:credentialless", "p", "p:credentialless", None, "t"),
    )
    conn.execute(
            "INSERT INTO runtime_projection_events VALUES(?,?,?,?,?,?,?,?)",
            ("projection", "run", "run-event", "credential_lane", "typed-v2", "typed-v2", "a" * 64, "t"),
    )
    conn.execute(
        "INSERT INTO credential_models VALUES(?,?,?,?,?,?,?)",
        (
            "p:credentialless",
            "model",
            "lane",
            1,
            "projection",
            "a" * 64,
            "t",
        ),
    )
    conn.execute("UPDATE credential_models SET available = 0")
    assert conn.execute(
        "SELECT available FROM credential_models"
    ).fetchone()[0] == 0
