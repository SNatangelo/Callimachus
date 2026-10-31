# tests/infra/integrity/test_gate.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os

import pytest

from core.infra.db import RunRepository
from core.infra.db.integrity_storage import INTEGRITY_TRIGGERS_SQL
from core.fetch.storage import content_store
from core.infra.integrity import (
    ArtifactIntegrityViolation,
    AuthorityPermissionError,
    AuthorityUnavailable,
    ContentStoreIntegrityViolation,
    FileAuthority,
    IntegrityGateError,
    RunIntegrityGate,
)


HASH = "a" * 64


class _DirectClient:
    def __init__(self, authority: FileAuthority):
        self.authority = authority
        self.content_store_check_runs: list[str] = []

    def enroll(self, run_dir: str):
        return self.authority.enroll(run_dir, caller_role="trusted_writer")

    def check(self, run_dir: str):
        return self.authority.check(run_dir, authenticated_caller="uid:test")

    def begin_transition(
        self,
        run_dir: str,
        checkpoint_kind: str,
        *,
        transition_id: str,
        content_store_required: bool,
    ):
        return self.authority.begin_transition(
            run_dir,
            checkpoint_kind=checkpoint_kind,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
            transition_id=transition_id,
            content_store_required=content_store_required,
        )

    def commit_transition(self, run_dir: str, lease_id: str):
        return self.authority.commit_transition(
            run_dir,
            lease_id=lease_id,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def abort_transition(self, run_dir: str, lease_id: str, reason: str):
        return self.authority.abort_transition(
            run_dir,
            lease_id=lease_id,
            reason=reason,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def override(self, run_dir: str, reason: str):
        return self.authority.override(
            run_dir,
            reason=reason,
            authenticated_caller="uid:test",
        )

    def enroll_content_store(self):
        return self.authority.enroll_content_store(caller_role="trusted_writer")

    def check_content_store(self, run_dir: str):
        self.content_store_check_runs.append(run_dir)
        return self.authority.check_content_store(authenticated_caller="uid:test")

    def begin_content_store_transition(self, run_dir: str, transition_id: str):
        return self.authority.begin_content_store_transition(
            run_dir,
            transition_id=transition_id,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def commit_content_store_transition(self, run_dir: str, lease_id: str):
        return self.authority.commit_content_store_transition(
            lease_id=lease_id,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def abort_content_store_transition(
        self, run_dir: str, lease_id: str, reason: str
    ):
        return self.authority.abort_content_store_transition(
            lease_id=lease_id,
            reason=reason,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def heartbeat_transition(self, run_dir: str, lease_id: str):
        return self.authority.heartbeat_transition(
            run_dir,
            lease_id=lease_id,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def heartbeat_content_store_transition(self, run_dir: str, lease_id: str):
        return self.authority.heartbeat_content_store_transition(
            lease_id=lease_id,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def recover_pipeline_transition(self, run_dir: str):
        return self.authority.recover_pipeline_transition(
            run_dir,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:resume",
        )

    def override_content_store(self, run_dir: str, reason: str):
        return self.authority.override_content_store(
            reason=reason, authenticated_caller="uid:test"
        )


class _UnavailableClient:
    def check(self, run_dir: str):
        raise AuthorityUnavailable("offline")


def _run(protected_root):
    run_dir = protected_root / "run-1"
    repo = RunRepository.create(
        str(run_dir),
        run_id="run-1",
        input_path="paper.pdf",
        input_sha256=HASH,
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile="default",
        challenge_mode="off",
        fixture_fingerprint="fixture",
    )
    repo.close()
    (run_dir / "artifact.txt").write_text("original", encoding="utf-8")
    return run_dir


def _gate(tmp_path):
    authority_root = tmp_path / "authority"
    protected_root = tmp_path / "protected"
    authority_root.mkdir()
    protected_root.mkdir()
    key_file = authority_root / "key"
    key_file.write_text("k" * 64, encoding="ascii")
    os.chmod(key_file, 0o600)
    authority = FileAuthority(
        authority_root=str(authority_root),
        key_file=str(key_file),
        protected_root=str(protected_root),
        authority_id="test-authority",
        enforce_permissions=False,
    )
    return RunIntegrityGate(_DirectClient(authority)), protected_root


def _gate_with_content_store(tmp_path):
    authority_root = tmp_path / "authority"
    protected_root = tmp_path / "protected"
    state_root = tmp_path / "state"
    authority_root.mkdir()
    protected_root.mkdir()
    content_store.preflight(
        environ={content_store.ENV_STATE_DIR: str(state_root)}
    )
    store_root = content_store.storage_root(
        environ={content_store.ENV_STATE_DIR: str(state_root)}
    )
    key_file = authority_root / "key"
    key_file.write_text("k" * 64, encoding="ascii")
    os.chmod(key_file, 0o600)
    authority = FileAuthority(
        authority_root=str(authority_root),
        key_file=str(key_file),
        protected_root=str(protected_root),
        content_store_root=store_root,
        authority_id="test-authority",
        enforce_permissions=False,
    )
    gate = RunIntegrityGate(_DirectClient(authority))
    gate.client.enroll_content_store()
    return gate, protected_root, store_root


def test_environment_configuration_is_mandatory():
    with pytest.raises(IntegrityGateError, match="CITATION_VERIFIER_INTEGRITY_SOCKET"):
        RunIntegrityGate.from_environment({})


def test_worker_activation_is_bound_to_the_requested_run(tmp_path):
    expected = os.path.realpath(os.path.abspath(tmp_path))

    class Client:
        def activate_worker(self, run_dir):
            return {"scope": "worker", "run_dir": expected}

    assert RunIntegrityGate(Client()).activate_worker(str(tmp_path))["run_dir"] == expected

    class WrongRunClient:
        def activate_worker(self, run_dir):
            return {"scope": "worker", "run_dir": f"{expected}-other"}

    with pytest.raises(IntegrityGateError, match="malformed worker activation"):
        RunIntegrityGate(WrongRunClient()).activate_worker(str(tmp_path))


def test_enrollment_and_clean_preflight_reconcile_exact_signed_history(tmp_path):
    gate, protected_root = _gate(tmp_path)
    run_dir = _run(protected_root)

    gate.enroll(str(run_dir))
    checked = gate.preflight(str(run_dir))

    assert checked["status"] == "clean"
    repo = RunRepository.open(str(run_dir))
    try:
        summary = repo.artifact_integrity_summary()
        assert summary["state"] == "clean"
        assert summary["checkpoint_count"] == 1
    finally:
        repo.close()


def test_readonly_worker_preflight_and_admission_never_open_local_repo(monkeypatch):
    class Client:
        def check(self, _run_dir):
            return {"status": "clean"}

        def admit_task_answer(self, _run_dir, _task_id, _payload):
            return {
                "answer_id": "answer-1",
                "provenance": {"producer_class": "agent"},
            }

    gate = RunIntegrityGate(Client())
    monkeypatch.setattr(
        RunRepository,
        "open",
        classmethod(lambda *_args: pytest.fail("readonly worker opened run DB")),
    )
    assert gate.preflight("unused", mirror_audit_records=False)["status"] == "clean"
    assert gate.admit_task_answer(
        "unused", "task-1", {"found": False}, mirror_checkpoint=False
    )["answer_id"] == "answer-1"


def test_mismatch_stops_before_domain_mutation_and_mirrors_violation(tmp_path):
    gate, protected_root = _gate(tmp_path)
    run_dir = _run(protected_root)
    gate.enroll(str(run_dir))
    (run_dir / "artifact.txt").write_text("altered", encoding="utf-8")

    with pytest.raises(ArtifactIntegrityViolation, match="stopped before domain mutation"):
        gate.preflight(str(run_dir))

    repo = RunRepository.open(str(run_dir))
    try:
        summary = repo.artifact_integrity_summary()
        assert summary["state"] == "violated"
        assert summary["violation_count"] == 1
    finally:
        repo.close()


def test_debug_override_is_tracked_and_debug_settings_are_checkpointed(tmp_path):
    gate, protected_root = _gate(tmp_path)
    run_dir = _run(protected_root)
    gate.enroll(str(run_dir))
    (run_dir / "artifact.txt").write_text("altered", encoding="utf-8")

    checked = gate.preflight(
        str(run_dir),
        debug_override=True,
        override_reason="inspect an agent-modified artifact",
    )

    assert checked["status"] == "debug_overridden"
    repo = RunRepository.open(str(run_dir))
    try:
        summary = repo.artifact_integrity_summary()
        assert summary["state"] == "debug_overridden"
        assert summary["checkpoint_count"] == 3
        assert summary["violation_count"] == 1
        assert summary["override_count"] == 1
        assert repo.get_run_setting("debug_mode") is True
        assert repo.get_run_setting("debug_labels") == [
            "artifact_integrity_debug",
            "artifact_integrity_override",
        ]
    finally:
        repo.close()


def test_debug_flag_cannot_bypass_unavailable_authority(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    gate = RunIntegrityGate(_UnavailableClient())

    with pytest.raises(IntegrityGateError, match="authority preflight failed"):
        gate.preflight(
            str(run_dir),
            debug_override=True,
            override_reason="diagnostic only",
        )


def test_signed_history_rejects_a_locally_rewritten_mirror(tmp_path):
    gate, protected_root = _gate(tmp_path)
    run_dir = _run(protected_root)
    gate.enroll(str(run_dir))
    repo = RunRepository.open(str(run_dir))
    try:
        repo._conn.execute("DROP TRIGGER artifact_checkpoints_no_update")
        repo._conn.execute(
            "UPDATE artifact_checkpoints SET signature=?",
            ("f" * 64,),
        )
        repo._conn.executescript(INTEGRITY_TRIGGERS_SQL)
        repo._conn.commit()
    finally:
        repo.close()

    with pytest.raises(IntegrityGateError, match="mirror differs"):
        gate.preflight(str(run_dir))


def test_content_store_mismatch_stops_run_before_domain_mutation(tmp_path):
    gate, protected_root, store_root = _gate_with_content_store(tmp_path)
    run_dir = _run(protected_root)
    gate.enroll(str(run_dir))
    with open(os.path.join(store_root, "agent-added.txt"), "w", encoding="utf-8") as out:
        out.write("adulterated")

    with pytest.raises(ContentStoreIntegrityViolation, match="content store integrity"):
        gate.preflight_content_store(str(run_dir))
    assert gate.client.content_store_check_runs == [str(run_dir)]


def test_content_store_debug_override_taints_the_consuming_run(tmp_path):
    gate, protected_root, store_root = _gate_with_content_store(tmp_path)
    run_dir = _run(protected_root)
    gate.enroll(str(run_dir))
    with open(os.path.join(store_root, "agent-added.txt"), "w", encoding="utf-8") as out:
        out.write("diagnostic input")

    checked = gate.preflight_content_store(
        str(run_dir),
        debug_override=True,
        override_reason="inspect adulterated shared content",
    )

    assert checked["status"] == "debug_overridden"
    repo = RunRepository.open(str(run_dir))
    try:
        assert repo.get_run_setting("debug_mode") is True
        assert "artifact_integrity_override" in repo.get_run_setting("debug_labels")
    finally:
        repo.close()


def test_content_store_lease_is_bound_to_its_owner_run(tmp_path):
    gate, protected_root, _store_root = _gate_with_content_store(tmp_path)
    run_dir = _run(protected_root)
    gate.enroll(str(run_dir))
    opened = gate.client.begin_content_store_transition(
        str(run_dir), "transition-run-scope"
    )
    lease_id = opened["lease"]["lease_id"]
    gate.client.authority.require_content_store_transition_run(
        str(run_dir), lease_id=lease_id
    )

    other_run = protected_root / "run-2"
    other_run.mkdir()
    with pytest.raises(AuthorityPermissionError, match="different run"):
        gate.client.authority.require_content_store_transition_run(
            str(other_run), lease_id=lease_id
        )

    gate.client.abort_content_store_transition(
        str(run_dir), lease_id, "test cleanup"
    )


def test_pipeline_transition_checkpoints_run_and_shared_content_store(tmp_path):
    gate, protected_root, store_root = _gate_with_content_store(tmp_path)
    run_dir = _run(protected_root)
    gate.enroll(str(run_dir))
    lease = gate.begin_pipeline_transition(
        str(run_dir), checkpoint_kind="phase_boundary"
    )
    (run_dir / "artifact.txt").write_text("trusted phase", encoding="utf-8")
    with open(os.path.join(store_root, "trusted.txt"), "w", encoding="utf-8") as out:
        out.write("trusted shared content")

    gate.commit_pipeline_transition(str(run_dir), lease)

    assert gate.preflight(str(run_dir))["status"] == "clean"


def test_pipeline_crash_recovery_restores_run_and_content_store_together(tmp_path):
    gate, protected_root, store_root = _gate_with_content_store(tmp_path)
    run_dir = _run(protected_root)
    gate.enroll(str(run_dir))
    lease = gate.begin_pipeline_transition(
        str(run_dir), checkpoint_kind="phase_boundary"
    )

    (run_dir / "artifact.txt").write_text("partial-run", encoding="utf-8")
    crash_object = os.path.join(store_root, "partial-content-object")
    with open(crash_object, "w", encoding="utf-8") as handle:
        handle.write("partial-content")

    recovered = gate.recover_pipeline_transition(str(run_dir))

    assert recovered["status"] == "recovered"
    assert {row["lease_id"] for row in recovered["recoveries"]} == {
        lease.run_lease_id,
        lease.content_store_lease_id,
    }
    assert (run_dir / "artifact.txt").read_text(encoding="utf-8") == "original"
    assert not os.path.exists(crash_object)
    assert gate.preflight(str(run_dir))["status"] == "clean"
    assert gate.preflight_content_store(str(run_dir))["status"] == "clean"
    repo = RunRepository.open(str(run_dir))
    summary = repo.artifact_integrity_summary()
    repo.close()
    assert summary["audit_ready"] is True
    assert summary["crash_recovery_count"] == 1
    assert gate.preflight_content_store(str(run_dir))["status"] == "clean"


def test_partial_pair_commit_keeps_run_unit_and_rolls_back_open_store_lease(
    tmp_path,
):
    gate, protected_root, store_root = _gate_with_content_store(tmp_path)
    run_dir = _run(protected_root)
    gate.enroll(str(run_dir))
    lease = gate.begin_pipeline_transition(
        str(run_dir), checkpoint_kind="unit"
    )
    (run_dir / "artifact.txt").write_text("completed-unit", encoding="utf-8")
    repo = RunRepository.open(str(run_dir))
    try:
        repo.append_integrity_unit_completion(
            "fetch_auto", "r1", {"ref_id": "r1", "status": "stored"}
        )
    finally:
        repo.close()
    partial_store_object = os.path.join(store_root, "partial-store-object")
    with open(partial_store_object, "w", encoding="utf-8") as handle:
        handle.write("not-yet-committed")

    # Simulate termination after the first half of commit_pipeline_transition.
    gate.commit_transition(str(run_dir), lease.run_lease_id)
    recovered = gate.recover_pipeline_transition(str(run_dir))

    assert recovered["status"] == "recovered"
    assert (run_dir / "artifact.txt").read_text(encoding="utf-8") == "completed-unit"
    assert not os.path.exists(partial_store_object)
    repo = RunRepository.open(str(run_dir))
    try:
        assert [
            row["unit_id"]
            for row in repo.list_integrity_unit_completions("fetch_auto")
        ] == ["r1"]
    finally:
        repo.close()
    assert gate.preflight(str(run_dir))["status"] == "clean"
    assert gate.preflight_content_store(str(run_dir))["status"] == "clean"
