# tests/infra/test_artifact_integrity_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import sqlite3

import pytest

from core.infra.db import RunRepository


HASH = "a" * 64
SIGNATURE = "b" * 64


def _repo(tmp_path) -> RunRepository:
    return RunRepository.create(
        str(tmp_path),
        run_id="integrity-run",
        input_path="paper.pdf",
        input_sha256=HASH,
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile="default",
        challenge_mode="off",
        fixture_fingerprint="fixture",
    )


def _checkpoint(
    checkpoint_id: str,
    sequence: int,
    *,
    previous: str | None,
    state: str = "clean",
    manifest: str = HASH,
    checkpoint_kind: str = "phase_boundary",
):
    return {
        "checkpoint_id": checkpoint_id,
        "sequence": sequence,
        "phase": "fetch",
        "checkpoint_kind": checkpoint_kind,
        "created_at": f"2026-08-22T00:00:0{sequence}Z",
        "previous_checkpoint_id": previous,
        "manifest_sha256": manifest,
        "database_projection_sha256": "c" * 64,
        "file_count": 1,
        "integrity_state": state,
        "authority_id": "test-authority",
        "signature_algorithm": "hmac-sha256",
        "signature": SIGNATURE,
    }


def test_unit_checkpoint_kind_is_persisted(tmp_path):
    repo = _repo(tmp_path)
    try:
        repo.append_artifact_checkpoint(
            _checkpoint(
                "checkpoint-unit",
                0,
                previous=None,
                checkpoint_kind="unit",
            ),
            FILES,
        )
        assert repo.list_artifact_checkpoints()[0]["checkpoint_kind"] == "unit"
    finally:
        repo.close()


def _recovery():
    return {
        "recovery_id": "recovery-1",
        "subject_scope": "run",
        "lease_id": "lease-1",
        "transition_id": "transition-1",
        "expected_checkpoint_id": "checkpoint-1",
        "authenticated_caller": "operator:test",
        "previous_owner_process": "process:before",
        "recovered_by_process": "process:after",
        "last_heartbeat_at": "2026-08-22T00:00:00Z",
        "observed_manifest_sha256": "e" * 64,
        "restored_manifest_sha256": HASH,
        "database_changed": True,
        "difference_count": 1,
        "action_count": 2,
        "observed_recovery_snapshot_sha256": "f" * 64,
        "created_at": "2026-08-22T00:00:01Z",
        "authority_id": "test-authority",
        "signature_algorithm": "hmac-sha256",
        "signature": SIGNATURE,
    }


FILES = [{
    "logical_path": "sources/source.txt",
    "sha256": "d" * 64,
    "byte_count": 12,
}]


def test_checkpoint_chain_is_append_only_and_exact_replay_is_idempotent(tmp_path):
    repo = _repo(tmp_path)
    try:
        first = _checkpoint("checkpoint-0", 0, previous=None)
        repo.append_artifact_checkpoint(first, FILES)
        repo.append_artifact_checkpoint(first, FILES)
        second = _checkpoint("checkpoint-1", 1, previous="checkpoint-0")
        repo.append_artifact_checkpoint(second, FILES)
        rows = repo.list_artifact_checkpoints()
        assert [row["checkpoint_id"] for row in rows] == [
            "checkpoint-0", "checkpoint-1"
        ]
        assert rows[0]["files"] == FILES
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            repo._conn.execute(
                "UPDATE artifact_checkpoints SET phase='verify' "
                "WHERE checkpoint_id='checkpoint-0'"
            )
    finally:
        repo.close()


def test_checkpoint_must_extend_latest_chain(tmp_path):
    repo = _repo(tmp_path)
    try:
        repo.append_artifact_checkpoint(
            _checkpoint("checkpoint-0", 0, previous=None), FILES
        )
        with pytest.raises(ValueError, match="current chain"):
            repo.append_artifact_checkpoint(
                _checkpoint("checkpoint-2", 2, previous="checkpoint-0"), FILES
            )
    finally:
        repo.close()


def test_debug_override_is_irreversible_and_summary_is_not_audit_ready(tmp_path):
    repo = _repo(tmp_path)
    try:
        first = _checkpoint("checkpoint-0", 0, previous=None)
        repo.append_artifact_checkpoint(first, FILES)
        violation = {
            "violation_id": "violation-1",
            "observed_at": "2026-08-22T00:00:01Z",
            "phase": "fetch",
            "session_id": "session-1",
            "authenticated_caller": "uid:test",
            "expected_checkpoint_id": "checkpoint-0",
            "expected_manifest_sha256": HASH,
            "observed_manifest_sha256": "e" * 64,
            "database_changed": True,
            "difference_count": 1,
            "authority_id": "test-authority",
            "signature_algorithm": "hmac-sha256",
            "signature": SIGNATURE,
        }
        differences = [{
            "difference_kind": "changed",
            "logical_path": "sources/source.txt",
        }]
        repo.append_artifact_integrity_violation(violation, differences)
        overridden = _checkpoint(
            "checkpoint-1", 1, previous="checkpoint-0",
            state="debug_overridden", manifest="e" * 64,
        )
        repo.append_artifact_checkpoint(overridden, FILES)
        override = {
            "override_id": "override-1",
            "violation_id": "violation-1",
            "resulting_checkpoint_id": "checkpoint-1",
            "reason": "diagnose a deliberately altered fixture",
            "authenticated_caller": "operator:test",
            "created_at": "2026-08-22T00:00:02Z",
            "authority_id": "test-authority",
            "signature_algorithm": "hmac-sha256",
            "signature": SIGNATURE,
        }
        repo.append_artifact_integrity_override(override)
        summary = repo.artifact_integrity_summary()
        assert summary["state"] == "debug_overridden"
        assert summary["audit_ready"] is False
        assert summary["override_count"] == 1
        with pytest.raises(ValueError, match="irreversible"):
            repo.append_artifact_checkpoint(
                _checkpoint("checkpoint-2", 2, previous="checkpoint-1"), FILES
            )
    finally:
        repo.close()


def test_unresolved_violation_is_reported_as_violated(tmp_path):
    repo = _repo(tmp_path)
    try:
        repo.append_artifact_checkpoint(
            _checkpoint("checkpoint-0", 0, previous=None), FILES
        )
        repo.append_artifact_integrity_violation(
            {
                "violation_id": "violation-1",
                "observed_at": "2026-08-22T00:00:01Z",
                "phase": "fetch",
                "session_id": None,
                "authenticated_caller": "uid:test",
                "expected_checkpoint_id": "checkpoint-0",
                "expected_manifest_sha256": HASH,
                "observed_manifest_sha256": "e" * 64,
                "database_changed": False,
                "difference_count": 1,
                "authority_id": "test-authority",
                "signature_algorithm": "hmac-sha256",
                "signature": SIGNATURE,
            },
            [{
                "difference_kind": "missing",
                "logical_path": "sources/source.txt",
            }],
        )
        summary = repo.artifact_integrity_summary()
        assert summary["state"] == "violated"
        assert summary["audit_ready"] is False
        assert summary["unresolved_violation_ids"] == ["violation-1"]
    finally:
        repo.close()


def test_debug_override_bundle_rolls_back_if_override_is_invalid(tmp_path):
    repo = _repo(tmp_path)
    try:
        first = _checkpoint("checkpoint-0", 0, previous=None)
        repo.append_artifact_checkpoint(first, FILES)
        violation = {
            "violation_id": "violation-atomic",
            "observed_at": "2026-08-22T00:00:01Z",
            "phase": "fetch",
            "session_id": None,
            "authenticated_caller": "uid:test",
            "expected_checkpoint_id": "checkpoint-0",
            "expected_manifest_sha256": HASH,
            "observed_manifest_sha256": "e" * 64,
            "database_changed": True,
            "difference_count": 0,
            "authority_id": "test-authority",
            "signature_algorithm": "hmac-sha256",
            "signature": SIGNATURE,
        }
        checkpoint = _checkpoint(
            "checkpoint-1", 1, previous="checkpoint-0",
            state="debug_overridden", manifest="e" * 64,
        )
        invalid_override = {
            "override_id": "override-atomic",
            "violation_id": "missing-violation",
            "resulting_checkpoint_id": "checkpoint-1",
            "reason": "diagnostic fixture",
            "authenticated_caller": "operator:test",
            "created_at": "2026-08-22T00:00:02Z",
            "authority_id": "test-authority",
            "signature_algorithm": "hmac-sha256",
            "signature": SIGNATURE,
        }
        with pytest.raises(ValueError, match="unavailable records"):
            repo.append_artifact_integrity_override_bundle(
                violation=violation,
                differences=[],
                checkpoint=checkpoint,
                files=FILES,
                override=invalid_override,
            )
        assert [row["checkpoint_id"] for row in repo.list_artifact_checkpoints()] == [
            "checkpoint-0"
        ]
        assert repo.artifact_integrity_summary()["violation_count"] == 0
    finally:
        repo.close()


def test_checkpoint_and_violation_dict_order_is_not_semantic(tmp_path):
    repo = _repo(tmp_path)
    try:
        checkpoint = _checkpoint("checkpoint-0", 0, previous=None)
        reordered = dict(reversed(list(checkpoint.items())))
        repo.append_artifact_checkpoint(reordered, FILES)
        repo.append_artifact_checkpoint(checkpoint, FILES)
        assert repo.artifact_integrity_summary()["state"] == "clean"
    finally:
        repo.close()


def test_crash_recovery_is_append_only_exact_and_keeps_run_audit_ready(tmp_path):
    repo = _repo(tmp_path)
    repo.append_artifact_checkpoint(
        _checkpoint("checkpoint-1", 0, previous=None), FILES
    )
    recovery = _recovery()
    entries = [
        {
            "entry_kind": "difference",
            "difference_kind": "changed",
            "logical_path": "artifact.txt",
        },
        {
            "entry_kind": "action",
            "action": "restored_database",
            "logical_path": "run.sqlite",
        },
        {
            "entry_kind": "action",
            "action": "restored_file",
            "logical_path": "artifact.txt",
        },
    ]

    repo.append_artifact_crash_recovery(recovery, entries)
    repo.append_artifact_crash_recovery(recovery, entries)
    summary = repo.artifact_integrity_summary()

    assert summary["state"] == "clean"
    assert summary["audit_ready"] is True
    assert summary["crash_recovery_count"] == 1
    assert summary["crash_recoveries"] == [{**recovery, "entries": entries}]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        repo._conn.execute(
            "UPDATE artifact_crash_recoveries SET transition_id='forged'"
        )
    with pytest.raises(ValueError, match="replay differs"):
        repo.append_artifact_crash_recovery(
            {**recovery, "recovered_by_process": "process:forged"}, entries
        )
    repo.close()
