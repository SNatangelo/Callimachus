# tests/infra/integrity/test_authority.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from core.infra.db import RunRepository
from core.infra.db.schema import SCHEMA_VERSION
from core.infra.integrity import (
    AuthorityIntegrityError,
    AuthorityPermissionError,
    FileAuthority,
    IntegrityAuthorityClient,
)
from core.infra.integrity import authority as authority_module
from core.infra.integrity.service import WorkerCapabilityRegistry, serve


HASH = "a" * 64


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


def _authority(tmp_path):
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
    return authority, protected_root, authority_root


def _mirror_checkpoint(run_dir, result):
    repo = RunRepository.open(str(run_dir))
    try:
        repo.append_artifact_checkpoint(result["checkpoint"], result["files"])
    finally:
        repo.close()


def test_regular_file_digest_reads_crlf_and_ctrl_z_as_binary_bytes(tmp_path, monkeypatch):
    payload = b"first\r\nsecond\x1aafter\r\n"
    path = tmp_path / "binary.dat"
    path.write_bytes(payload)

    native_binary_flag = getattr(authority_module.os, "O_BINARY", 0)
    synthetic_binary_flag = 1 << 29
    opened_flags = []
    real_open = authority_module.os.open

    def windows_crt_open(file_path, flags, *args, **kwargs):
        opened_flags.append(flags)
        native_flags = flags & ~synthetic_binary_flag
        if flags & synthetic_binary_flag:
            native_flags |= native_binary_flag
        return real_open(file_path, native_flags, *args, **kwargs)

    monkeypatch.setattr(
        authority_module.os, "O_BINARY", synthetic_binary_flag, raising=False,
    )
    monkeypatch.setattr(authority_module.os, "open", windows_crt_open)

    assert FileAuthority._regular_file_digest(str(path)) == (
        hashlib.sha256(payload).hexdigest(), len(payload),
    )
    assert opened_flags and opened_flags[0] & synthetic_binary_flag


def test_authority_detects_file_and_local_database_rewrite(tmp_path):
    authority, protected_root, _ = _authority(tmp_path)
    run_dir = _run(protected_root)
    enrolled = authority.enroll(str(run_dir), caller_role="trusted_writer")
    _mirror_checkpoint(run_dir, enrolled)
    assert authority.check(
        str(run_dir), authenticated_caller="uid:test"
    )["status"] == "clean"

    (run_dir / "artifact.txt").write_text("altered", encoding="utf-8")
    repo = RunRepository.open(str(run_dir))
    try:
        repo._conn.execute(
            "UPDATE meta SET value='forged' WHERE key='schema_version'"
        )
        repo._conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        repo._conn.commit()
    finally:
        repo.close()
    result = authority.check(str(run_dir), authenticated_caller="uid:test")
    assert result["status"] == "violated"
    assert result["differences"] == [{
        "difference_kind": "changed",
        "logical_path": "artifact.txt",
    }]


def test_agent_cannot_enroll_or_commit_clean_transition(tmp_path):
    authority, protected_root, _ = _authority(tmp_path)
    run_dir = _run(protected_root)
    with pytest.raises(AuthorityPermissionError, match="trusted writer"):
        authority.enroll(str(run_dir), caller_role="agent")
    enrolled = authority.enroll(str(run_dir), caller_role="trusted_writer")
    _mirror_checkpoint(run_dir, enrolled)
    with pytest.raises(AuthorityPermissionError, match="trusted writer"):
        authority.begin_transition(
            str(run_dir),
            checkpoint_kind="phase_boundary",
            caller_role="agent",
            authenticated_caller="uid:agent",
        )


def test_override_is_signed_irreversible_and_not_audit_ready(tmp_path):
    authority, protected_root, authority_root = _authority(tmp_path)
    run_dir = _run(protected_root)
    enrolled = authority.enroll(str(run_dir), caller_role="trusted_writer")
    _mirror_checkpoint(run_dir, enrolled)
    (run_dir / "artifact.txt").write_text("altered", encoding="utf-8")
    result = authority.override(
        str(run_dir),
        reason="diagnose an altered fixture",
        authenticated_caller="uid:agent",
    )
    assert result["status"] == "debug_overridden"
    assert result["audit_ready"] is False
    assert result["override"]["authenticated_caller"] == "uid:agent"
    ledger = next((authority_root / "ledgers").glob("*.jsonl"))
    events = [
        json.loads(row)["event"]
        for row in ledger.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["kind"] for event in events] == [
        "checkpoint",
        "violation",
        "checkpoint",
    ]
    assert (
        events[-1]["integrity_override"]["record"]["override_id"]
        == result["override"]["override_id"]
    )
    repo = RunRepository.open(str(run_dir))
    try:
        repo.append_artifact_integrity_override_bundle(
            violation=result["violation"],
            differences=result["differences"],
            checkpoint=result["checkpoint"],
            files=result["files"],
            override=result["override"],
        )
        assert repo.artifact_integrity_summary()["state"] == "debug_overridden"
    finally:
        repo.close()
    checked = authority.check(str(run_dir), authenticated_caller="uid:agent")
    assert checked["status"] == "debug_overridden"
    assert checked["audit_ready"] is False


def test_trusted_transition_requires_clean_start_and_commits_one_checkpoint(tmp_path):
    authority, protected_root, _ = _authority(tmp_path)
    run_dir = _run(protected_root)
    enrolled = authority.enroll(str(run_dir), caller_role="trusted_writer")
    _mirror_checkpoint(run_dir, enrolled)
    lease = authority.begin_transition(
        str(run_dir),
        checkpoint_kind="phase_boundary",
        caller_role="trusted_writer",
        authenticated_caller="uid:writer",
    )["lease"]
    repo = RunRepository.open(str(run_dir))
    try:
        repo.update_run_phase("resolve")
    finally:
        repo.close()
    committed = authority.commit_transition(
        str(run_dir),
        lease_id=lease["lease_id"],
        caller_role="trusted_writer",
        authenticated_caller="uid:writer",
    )
    _mirror_checkpoint(run_dir, committed)
    assert committed["checkpoint"]["sequence"] == 1
    assert committed["checkpoint"]["phase"] == "resolve"
    assert authority.check(
        str(run_dir), authenticated_caller="uid:writer"
    )["status"] == "clean"


def test_aborted_transition_closes_lease_without_authorising_mutation(tmp_path):
    authority, protected_root, _ = _authority(tmp_path)
    run_dir = _run(protected_root)
    enrolled = authority.enroll(str(run_dir), caller_role="trusted_writer")
    _mirror_checkpoint(run_dir, enrolled)
    lease = authority.begin_transition(
        str(run_dir),
        checkpoint_kind="phase_boundary",
        caller_role="trusted_writer",
        authenticated_caller="uid:writer",
    )["lease"]

    with pytest.raises(AuthorityPermissionError, match="trusted writer"):
        authority.abort_transition(
            str(run_dir),
            lease_id=lease["lease_id"],
            reason="agent tried to close writer lease",
            caller_role="agent",
            authenticated_caller="uid:agent",
        )

    aborted = authority.abort_transition(
        str(run_dir),
        lease_id=lease["lease_id"],
        reason="phase raised before a trusted boundary",
        caller_role="trusted_writer",
        authenticated_caller="uid:writer",
    )

    assert aborted == {"status": "aborted", "lease_id": lease["lease_id"]}
    replacement = authority.begin_transition(
        str(run_dir),
        checkpoint_kind="phase_boundary",
        caller_role="trusted_writer",
        authenticated_caller="uid:writer",
    )["lease"]
    assert replacement["lease_id"] != lease["lease_id"]


def test_debug_override_closes_interrupted_transition_before_new_checkpoint(tmp_path):
    authority, protected_root, _ = _authority(tmp_path)
    run_dir = _run(protected_root)
    enrolled = authority.enroll(str(run_dir), caller_role="trusted_writer")
    _mirror_checkpoint(run_dir, enrolled)
    authority.begin_transition(
        str(run_dir),
        checkpoint_kind="phase_boundary",
        caller_role="trusted_writer",
        authenticated_caller="uid:writer",
    )
    (run_dir / "artifact.txt").write_text("partial phase output", encoding="utf-8")

    overridden = authority.override(
        str(run_dir),
        reason="inspect interrupted phase output",
        authenticated_caller="uid:agent",
    )

    assert overridden["status"] == "debug_overridden"
    assert overridden["checkpoint"]["integrity_state"] == "debug_overridden"


def test_crash_recovery_restores_signed_checkpoint_and_closes_orphaned_lease(
    tmp_path,
):
    authority, protected_root, authority_root = _authority(tmp_path)
    run_dir = _run(protected_root)
    enrolled = authority.enroll(str(run_dir), caller_role="trusted_writer")
    _mirror_checkpoint(run_dir, enrolled)
    lease = authority.begin_transition(
        str(run_dir),
        checkpoint_kind="phase_boundary",
        caller_role="trusted_writer",
        authenticated_caller="uid:writer",
        authenticated_process="process:before-crash",
        transition_id="transition-crash-test",
        content_store_required=False,
    )["lease"]
    heartbeat = authority.heartbeat_transition(
        str(run_dir),
        lease_id=lease["lease_id"],
        caller_role="trusted_writer",
        authenticated_caller="uid:writer",
        authenticated_process="process:before-crash",
    )

    (run_dir / "artifact.txt").write_text("partial-crash-write", encoding="utf-8")
    repo = RunRepository.open(str(run_dir))
    repo.update_run_phase("resolve")
    repo.close()

    recovered = authority.recover_pipeline_transition(
        str(run_dir),
        caller_role="trusted_writer",
        authenticated_caller="uid:writer",
        authenticated_process="process:after-crash",
    )

    assert recovered["status"] == "recovered"
    assert len(recovered["recoveries"]) == 1
    recovery = recovered["recoveries"][0]
    assert recovery["lease_id"] == lease["lease_id"]
    assert recovery["transition_id"] == "transition-crash-test"
    assert recovery["record"]["last_heartbeat_at"] == heartbeat["heartbeat_at"]
    assert recovery["record"]["database_changed"] is True
    assert recovery["record"]["restored_manifest_sha256"] == enrolled["checkpoint"][
        "manifest_sha256"
    ]
    assert (run_dir / "artifact.txt").read_text(encoding="utf-8") == "original"
    repo = RunRepository.open(str(run_dir))
    assert repo.get_run().phase == "parse"
    repo.close()
    assert authority.check(
        str(run_dir), authenticated_caller="uid:writer"
    )["status"] == "clean"

    next_lease = authority.begin_transition(
        str(run_dir),
        checkpoint_kind="phase_boundary",
        caller_role="trusted_writer",
        authenticated_caller="uid:writer",
        authenticated_process="process:after-crash",
        transition_id="transition-after-recovery",
        content_store_required=False,
    )["lease"]
    assert next_lease["lease_id"] != lease["lease_id"]

    ledger = next((authority_root / "ledgers").glob("*.jsonl"))
    events = [
        json.loads(row)["event"]
        for row in ledger.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event.get("kind") == "crash_recovery"
        and event.get("record", {}).get("recovery_id")
        == recovery["record"]["recovery_id"]
        for event in events
    )


def test_crash_recovery_never_accepts_changes_without_an_open_lease(tmp_path):
    authority, protected_root, _ = _authority(tmp_path)
    run_dir = _run(protected_root)
    enrolled = authority.enroll(str(run_dir), caller_role="trusted_writer")
    _mirror_checkpoint(run_dir, enrolled)
    (run_dir / "artifact.txt").write_text("outside-lease", encoding="utf-8")

    recovered = authority.recover_pipeline_transition(
        str(run_dir),
        caller_role="trusted_writer",
        authenticated_caller="uid:writer",
        authenticated_process="process:resume",
    )

    assert recovered == {"status": "none", "recoveries": []}
    assert authority.check(
        str(run_dir), authenticated_caller="uid:writer"
    )["status"] == "violated"


def test_real_process_exit_preserves_completed_unit_and_rolls_back_inflight(
    tmp_path,
):
    authority, protected_root, authority_root = _authority(tmp_path)
    run_dir = _run(protected_root)
    authority.enroll(str(run_dir), caller_role="trusted_writer")

    completed = authority.begin_transition(
        str(run_dir),
        checkpoint_kind="unit",
        caller_role="trusted_writer",
        authenticated_caller="uid:parent",
        authenticated_process="process:parent",
        transition_id="transition-completed-unit",
        content_store_required=False,
    )
    (run_dir / "artifact.txt").write_text("completed-unit", encoding="utf-8")
    repo = RunRepository.open(str(run_dir))
    try:
        repo.append_integrity_unit_completion(
            "fetch_auto", "r1", {"ref_id": "r1", "status": "stored"}
        )
    finally:
        repo.close()
    authority.commit_transition(
        str(run_dir),
        lease_id=completed["lease"]["lease_id"],
        caller_role="trusted_writer",
        authenticated_caller="uid:parent",
        authenticated_process="process:parent",
    )

    child = """
import os
import sys
from pathlib import Path
from core.infra.db import RunRepository
from core.infra.integrity import FileAuthority

authority_root, protected_root, run_dir = sys.argv[1:4]
authority = FileAuthority(
    authority_root=authority_root,
    key_file=str(Path(authority_root, "key")),
    protected_root=protected_root,
    authority_id="test-authority",
    enforce_permissions=False,
)
authority.begin_transition(
    run_dir,
    checkpoint_kind="unit",
    caller_role="trusted_writer",
    authenticated_caller="uid:child",
    authenticated_process=f"process:child:{os.getpid()}",
    transition_id="transition-inflight-unit",
    content_store_required=False,
)
Path(run_dir, "artifact.txt").write_text("inflight-unit", encoding="utf-8")
repo = RunRepository.open(run_dir)
repo.append_integrity_unit_completion(
    "fetch_auto", "r2", {"ref_id": "r2", "status": "stored"}
)
repo.close()
os._exit(73)
"""
    project_root = Path(__file__).resolve().parents[3]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(project_root), environment.get("PYTHONPATH")))
    )
    exited = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            str(authority_root),
            str(protected_root),
            str(run_dir),
        ],
        cwd=str(project_root),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
    assert exited.returncode == 73, exited.stderr

    recovered = authority.recover_pipeline_transition(
        str(run_dir),
        caller_role="trusted_writer",
        authenticated_caller="uid:child",
        authenticated_process="process:recovery",
    )
    assert recovered["status"] == "recovered"
    assert (run_dir / "artifact.txt").read_text(encoding="utf-8") == "completed-unit"
    repo = RunRepository.open(str(run_dir))
    try:
        assert [
            row["unit_id"]
            for row in repo.list_integrity_unit_completions("fetch_auto")
        ] == ["r1"]
    finally:
        repo.close()
    assert authority.check(
        str(run_dir), authenticated_caller="uid:child"
    )["status"] == "clean"


def test_authority_rejects_forged_external_ledger(tmp_path):
    authority, protected_root, authority_root = _authority(tmp_path)
    run_dir = _run(protected_root)
    enrolled = authority.enroll(str(run_dir), caller_role="trusted_writer")
    _mirror_checkpoint(run_dir, enrolled)
    ledger = next((authority_root / "ledgers").glob("*.jsonl"))
    rows = ledger.read_text(encoding="utf-8").splitlines()
    envelope = json.loads(rows[0])
    envelope["event"]["record"]["manifest_sha256"] = "f" * 64
    ledger.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    with pytest.raises(AuthorityIntegrityError, match="digest|signature"):
        authority.check(str(run_dir), authenticated_caller="uid:test")


def test_socket_client_uses_peer_identity_and_returns_authority_result(tmp_path_factory, monkeypatch):
    if not hasattr(os, "getuid"):
        pytest.skip("Unix peer identity is unavailable")
    test_root = tmp_path_factory.mktemp("a")
    probe_path = test_root / "probe.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as capability_probe:
            capability_probe.bind(str(probe_path))
    except (OSError, PermissionError):
        pytest.skip("Unix sockets are unavailable on this filesystem")
    probe_path.unlink()
    authority, protected_root, authority_root = _authority(test_root)
    run_dir = _run(protected_root)
    socket_path = authority_root / "authority.sock"
    ready = threading.Event()
    peer_uid = os.getuid()
    monkeypatch.setattr(
        "core.infra.integrity.service.os.getuid", lambda: peer_uid + 1
    )
    thread = threading.Thread(
        target=serve,
        kwargs={
            "socket_path": str(socket_path),
            "authority": authority,
            "trusted_writer_uids": frozenset({peer_uid}),
            "worker_capabilities": WorkerCapabilityRegistry(
                process_attestor=lambda *_args: True
            ),
            "ready_event": ready,
            "max_requests": 3,
        },
        daemon=True,
    )
    thread.start()
    assert ready.wait(5)
    client = IntegrityAuthorityClient(str(socket_path))
    client.activate_worker(str(run_dir))
    enrolled = client.enroll(str(run_dir))
    _mirror_checkpoint(run_dir, enrolled)
    checked = client.check(str(run_dir))
    assert checked["status"] == "clean"
    assert checked["trust_domain_isolated"] is True
    thread.join(5)
    assert not thread.is_alive()
