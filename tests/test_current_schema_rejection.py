# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from core.infra.db import RunRepository
from core.infra.db.schema import SCHEMA_VERSION


def _create_run(run_dir: str) -> RunRepository:
    return RunRepository.create(
        run_dir,
        run_id="schema-rejection-test",
        input_path="synthetic-paper.txt",
        input_sha256="a" * 64,
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile="default",
        challenge_mode="off",
        fixture_fingerprint="deployment-test",
    )


def _assert_rejected_immutably(run_dir: str, message: str) -> None:
    path = os.path.join(run_dir, "run.sqlite")
    before_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    before_mtime_ns = os.stat(path).st_mtime_ns
    sidecars = [f"{path}-wal", f"{path}-shm", f"{path}-journal"]
    before_sidecars = {sidecar: os.path.exists(sidecar) for sidecar in sidecars}

    for opener in (RunRepository.open, RunRepository.open_readonly):
        with pytest.raises(RuntimeError, match=message):
            opener(run_dir)

    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == before_hash
    assert os.stat(path).st_mtime_ns == before_mtime_ns
    assert {sidecar: os.path.exists(sidecar) for sidecar in sidecars} == before_sidecars


def test_open_and_open_readonly_reject_inconsistent_current_schema_states_immutably(
    tmp_path,
):
    repo = _create_run(str(tmp_path))
    repo._conn.execute("PRAGMA journal_mode = DELETE")
    repo.close()
    path = tmp_path / "run.sqlite"

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
    _assert_rejected_immutably(str(tmp_path), "unsupported")

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
    _assert_rejected_immutably(str(tmp_path), "unsupported")

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        conn.execute("DROP TABLE meta")
    _assert_rejected_immutably(str(tmp_path), "schema.*metadata")

    structural_dir = tmp_path / "missing-trigger"
    structural_dir.mkdir()
    structural = _create_run(str(structural_dir))
    structural._conn.execute("PRAGMA journal_mode = DELETE")
    structural.close()
    structural_path = structural_dir / "run.sqlite"
    with sqlite3.connect(structural_path) as conn:
        conn.execute("DROP TRIGGER phase_events_no_update")
    _assert_rejected_immutably(
        str(structural_dir), "missing required current objects"
    )
