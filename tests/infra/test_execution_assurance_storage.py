# tests/infra/test_execution_assurance_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import sqlite3

import pytest

from core.infra.db import ExecutionAssuranceRecord, RunRepository
from core.infra.db.execution_assurance_storage import EXECUTION_ASSURANCE_DDL


def _create(tmp_path, name: str, assurance: ExecutionAssuranceRecord | None = None):
    return RunRepository.create(
        str(tmp_path / name),
        run_id=name,
        input_path="paper.pdf",
        input_sha256="abc",
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile=None,
        challenge_mode=None,
        fixture_fingerprint="fixture",
        execution_assurance=assurance,
    )


@pytest.mark.parametrize("assurance", [
    ExecutionAssuranceRecord("standalone", "standalone_unattested"),
    ExecutionAssuranceRecord("agent", "agent_attested", agent_identity="codex"),
    ExecutionAssuranceRecord("agent", "agent_attested", agent_identity="custom-runner.v1"),
    ExecutionAssuranceRecord(
        "agent", "agent_unprotected_acknowledged", agent_identity="claude-code",
        failure_reason="authority unavailable", acknowledged_at="2026-08-24T10:00:00+00:00",
    ),
])
def test_execution_assurance_roundtrips_valid_initial_state(tmp_path, assurance):
    repo = _create(tmp_path, f"run-{assurance.protection}", assurance)
    try:
        assert repo.get_execution_assurance() == assurance
    finally:
        repo.close()


def test_execution_assurance_downgrade_is_monotonic_and_clone_is_conservative(tmp_path):
    initial = ExecutionAssuranceRecord("agent", "agent_attested", agent_identity="codex")
    repo = _create(tmp_path, "parent", initial)
    downgraded = ExecutionAssuranceRecord(
        "agent", "agent_unprotected_acknowledged", agent_identity="codex",
        failure_reason="authority socket unavailable",
        acknowledged_at="2026-08-24T10:00:00+00:00",
    )
    try:
        repo.downgrade_execution_assurance(downgraded)
        assert repo.get_execution_assurance() == downgraded
        with pytest.raises(ValueError, match="cannot be upgraded"):
            repo.downgrade_execution_assurance(initial)
        clone = repo.clone_as_fresh_run(new_run_dir=str(tmp_path / "clone"), new_run_id="clone")
        try:
            assert clone.get_execution_assurance() == downgraded
        finally:
            clone.close()
    finally:
        repo.close()


def test_execution_assurance_records_agent_identity_on_standalone_takeover(tmp_path):
    repo = _create(tmp_path, "standalone")
    takeover = ExecutionAssuranceRecord(
        "standalone", "agent_unprotected_acknowledged", agent_identity="codex",
        failure_reason="authority socket unavailable",
        acknowledged_at="2026-08-24T10:00:00+00:00",
    )
    try:
        repo.downgrade_execution_assurance(takeover)
        assert repo.get_execution_assurance() == takeover
    finally:
        repo.close()


@pytest.mark.parametrize("assurance", [
    ExecutionAssuranceRecord("agent", "agent_attested"),
    ExecutionAssuranceRecord(
        "agent", "agent_unprotected_acknowledged",
        failure_reason="authority unavailable", acknowledged_at="2026-08-24T10:00:00+00:00",
    ),
])
def test_execution_assurance_rejects_missing_agent_identity_at_api_boundary(tmp_path, assurance):
    with pytest.raises(ValueError, match="agent identity"):
        _create(tmp_path, f"missing-{assurance.protection}", assurance)


@pytest.mark.parametrize("identity", ["", " custom", "custom ", "custom runner", "custom\nrunner", "custom\u00e8", "1" * 65, 1])
def test_execution_assurance_rejects_invalid_agent_identity_at_api_boundary(tmp_path, identity):
    with pytest.raises(ValueError, match="agent identity"):
        _create(
            tmp_path,
            "invalid-api",
            ExecutionAssuranceRecord("agent", "agent_attested", agent_identity=identity),
        )


@pytest.mark.parametrize("identity", ["", " custom", "custom runner", "custom\nrunner", "custom\x00runner", "custom\u00e8", "1" * 65])
def test_execution_assurance_rejects_invalid_agent_identity_at_database_boundary(identity):
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(EXECUTION_ASSURANCE_DDL)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO execution_assurance(
                  singleton, initial_origin, protection, agent_identity,
                  failure_reason, acknowledged_at
                ) VALUES(1, 'agent', 'agent_attested', ?, NULL, NULL)
                """,
                (identity,)
            )
    finally:
        conn.close()


@pytest.mark.parametrize("assurance", [
    ExecutionAssuranceRecord("agent", "agent_attested", agent_identity="codex"),
    ExecutionAssuranceRecord(
        "agent", "agent_unprotected_acknowledged", agent_identity="codex",
        failure_reason="authority unavailable", acknowledged_at="2026-08-24T10:00:00+00:00",
    ),
])
def test_execution_assurance_rejects_missing_agent_identity_at_database_boundary(tmp_path, assurance):
    repo = _create(tmp_path, f"database-{assurance.protection}", assurance)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            repo._conn.execute("UPDATE execution_assurance SET agent_identity=NULL WHERE singleton=1")
    finally:
        repo.close()


def test_execution_assurance_rejects_identity_mismatch_upgrade_and_delete(tmp_path):
    repo = _create(
        tmp_path,
        "agent",
        ExecutionAssuranceRecord("agent", "agent_attested", agent_identity="custom-runner.v1"),
    )
    try:
        with pytest.raises(ValueError, match="identity is immutable"):
            repo.downgrade_execution_assurance(ExecutionAssuranceRecord(
                "agent", "agent_unprotected_acknowledged", agent_identity="codex",
                failure_reason="authority unavailable", acknowledged_at="2026-08-24T10:00:00+00:00",
            ))
        repo.downgrade_execution_assurance(ExecutionAssuranceRecord(
            "agent", "agent_unprotected_acknowledged", agent_identity="custom-runner.v1",
            failure_reason="authority unavailable", acknowledged_at="2026-08-24T10:00:00+00:00",
        ))
        with pytest.raises(sqlite3.IntegrityError, match="transition is not monotonic"):
            repo._conn.execute("UPDATE execution_assurance SET protection='agent_attested' WHERE singleton=1")
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            repo._conn.execute("DELETE FROM execution_assurance WHERE singleton=1")
    finally:
        repo.close()
