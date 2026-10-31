# tests/infra/integrity/test_worker_capability.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os

import pytest

from core.infra.integrity import AuthorityPermissionError, IntegrityAuthorityClient
from core.infra.integrity.client import AuthorityRejected
from core.infra.integrity.authority import AUTHORITY_PROTOCOL_VERSION
from core.infra.integrity import service as integrity_service
from core.infra.integrity.service import (
    _attest_service_check_result,
    WorkerCapabilityRegistry,
    _is_official_command,
    dispatch_request,
    serve,
)


_TEST_PEER_UID = 1001


@pytest.fixture(autouse=True)
def _stable_test_process_identity(monkeypatch):
    monkeypatch.setattr(
        integrity_service,
        "_process_instance",
        lambda pid, uid: f"uid:{uid}:boot:test:pid:{pid}:start:test",
    )


class _RecordingAuthority:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def resolve_run_dir(run_dir: str) -> str:
        return os.path.realpath(os.path.abspath(run_dir))

    def enroll(self, run_dir: str, *, caller_role: str):
        self.calls.append((run_dir, caller_role))
        return {"caller_role": caller_role}

    def check(self, run_dir: str, *, authenticated_caller: str):
        self.calls.append((f"check:{run_dir}", authenticated_caller))
        return {"status": "clean"}

    def check_content_store(self, *, authenticated_caller: str):
        self.calls.append(("content_store_check", authenticated_caller))
        return {"status": "clean"}

    def enroll_content_store(self, *, caller_role: str):
        self.calls.append(("content_store", caller_role))
        return {"caller_role": caller_role}

    def begin_transition(
        self,
        run_dir: str,
        *,
        checkpoint_kind: str,
        caller_role: str,
        authenticated_caller: str,
        authenticated_process: str,
        transition_id: str,
        content_store_required: bool,
    ):
        return {
            "authenticated_process": authenticated_process,
            "caller_role": caller_role,
        }

    def require_content_store_transition_run(self, run_dir: str, *, lease_id: str):
        self.calls.append((f"lease:{lease_id}", run_dir))

    def commit_content_store_transition(
        self,
        *,
        lease_id: str,
        caller_role: str,
        authenticated_caller: str,
        authenticated_process: str,
    ):
        return {"caller_role": caller_role, "lease_id": lease_id}


class _RecordingClient(IntegrityAuthorityClient):
    def __init__(self) -> None:
        super().__init__("/unused")
        self.requests = []

    def _request(self, operation, payload, *, worker_capability):
        self.requests.append((operation, payload, worker_capability))
        if operation == "activate_worker":
            return {
                "capability": "worker-secret",
                "run_dir": os.path.realpath(os.path.abspath(payload["run_dir"])),
                "scope": "worker",
            }
        if operation == "activate_admin":
            return {"capability": "admin-secret", "run_dir": None, "scope": "admin"}
        return {"status": "ok"}


def _code_root(tmp_path):
    root = tmp_path / "code"
    (root / "core").mkdir(parents=True)
    (root / "run.py").write_text("# official entrypoint\n", encoding="utf-8")
    (root / "core" / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def _request(operation: str, payload: dict, capability=None):
    return {
        "version": AUTHORITY_PROTOCOL_VERSION,
        "operation": operation,
        "payload": payload,
        "worker_capability": capability,
    }


def _registry(tmp_path):
    return WorkerCapabilityRegistry(
        code_root=_code_root(tmp_path),
        process_attestor=lambda *_args: True,
    )


def test_official_command_contract_is_hardcoded_to_callimachus_entrypoints(tmp_path):
    code_root = _code_root(tmp_path)
    assert _is_official_command(
        ["python", "run.py", "--input", "paper.pdf"],
        str(code_root),
        "python3",
        "worker",
        code_root,
    )
    assert _is_official_command(
        [
            "python",
            "-m",
            "core.infra.integrity.service",
            "enroll-content-store",
            "--socket",
            "authority.sock",
        ],
        str(code_root),
        "python3",
        "admin",
        code_root,
    )
    assert not _is_official_command(
        ["python", "-c", "pass", "run.py"],
        str(code_root),
        "python3",
        "worker",
        code_root,
    )


def test_client_keeps_capabilities_in_memory_and_scopes_writer_requests(tmp_path):
    client = _RecordingClient()
    run_dir = tmp_path / "run-1"
    other_run = tmp_path / "run-2"

    with pytest.raises(AuthorityRejected, match="not activated for this run"):
        client.check(str(run_dir))
    with pytest.raises(AuthorityRejected, match="not activated for this run"):
        client.check_content_store(str(run_dir))
    assert client.requests == []

    client.activate_worker(str(run_dir))
    client.enroll(str(run_dir))
    client.commit_content_store_transition(str(run_dir), "lease-1")
    client.check(str(run_dir))
    client.check_content_store(str(run_dir))
    with pytest.raises(AuthorityRejected, match="not activated for this run"):
        client.enroll(str(other_run))
    client.activate_admin()
    client.enroll_content_store()

    assert client.requests == [
        ("activate_worker", {"run_dir": str(run_dir)}, None),
        ("enroll", {"run_dir": str(run_dir)}, "worker-secret"),
        (
            "content_store_commit_transition",
            {"run_dir": str(run_dir), "lease_id": "lease-1"},
            "worker-secret",
        ),
        ("check", {"run_dir": str(run_dir)}, "worker-secret"),
        (
            "content_store_check",
            {"run_dir": str(run_dir)},
            "worker-secret",
        ),
        ("activate_admin", {}, None),
        ("content_store_enroll", {}, "admin-secret"),
    ]


def test_trusted_uid_cannot_mutate_without_process_run_capability(tmp_path):
    authority = _RecordingAuthority()
    registry = _registry(tmp_path)
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    peer_uid = _TEST_PEER_UID
    peer_pid = os.getpid()

    with pytest.raises(AuthorityPermissionError, match="active Callimachus worker"):
        dispatch_request(
            authority,
            _request("enroll", {"run_dir": str(run_dir)}),
            peer_uid=peer_uid,
            peer_pid=peer_pid,
            trusted_writer_uids=frozenset({peer_uid}),
            worker_capabilities=registry,
        )

    assert authority.calls == []


@pytest.mark.parametrize("operation", ["check", "content_store_check"])
def test_worker_checks_require_capability_bound_to_process_and_run(
    tmp_path, operation
):
    authority = _RecordingAuthority()
    registry = _registry(tmp_path)
    run_dir = tmp_path / "run-1"
    other_run = tmp_path / "run-2"
    run_dir.mkdir()
    other_run.mkdir()
    peer_uid = _TEST_PEER_UID
    peer_pid = os.getpid()
    payload = {"run_dir": str(run_dir)}

    with pytest.raises(AuthorityPermissionError, match="active Callimachus worker"):
        dispatch_request(
            authority,
            _request(operation, payload),
            peer_uid=peer_uid,
            peer_pid=peer_pid,
            trusted_writer_uids=frozenset({peer_uid}),
            worker_capabilities=registry,
        )
    assert authority.calls == []

    activated = dispatch_request(
        authority,
        _request("activate_worker", payload),
        peer_uid=peer_uid,
        peer_pid=peer_pid,
        trusted_writer_uids=frozenset({peer_uid}),
        worker_capabilities=registry,
    )
    capability = activated["capability"]
    accepted = dispatch_request(
        authority,
        _request(operation, payload, capability),
        peer_uid=peer_uid,
        peer_pid=peer_pid,
        trusted_writer_uids=frozenset({peer_uid}),
        worker_capabilities=registry,
    )
    assert accepted["status"] == "clean"
    assert len(authority.calls) == 1

    for rejected_uid, rejected_pid, rejected_run in (
        (peer_uid, peer_pid, str(other_run)),
        (peer_uid, peer_pid + 1, str(run_dir)),
        (peer_uid + 1, peer_pid, str(run_dir)),
    ):
        with pytest.raises(AuthorityPermissionError, match="process and run"):
            dispatch_request(
                authority,
                _request(
                    operation,
                    {"run_dir": rejected_run},
                    capability,
                ),
                peer_uid=rejected_uid,
                peer_pid=rejected_pid,
                trusted_writer_uids=frozenset({peer_uid}),
                worker_capabilities=registry,
            )
    assert len(authority.calls) == 1


def test_worker_capability_is_bound_to_process_and_run(tmp_path):
    authority = _RecordingAuthority()
    registry = _registry(tmp_path)
    run_dir = tmp_path / "run-1"
    other_run = tmp_path / "run-2"
    run_dir.mkdir()
    other_run.mkdir()
    peer_uid = _TEST_PEER_UID
    peer_pid = os.getpid()

    activated = dispatch_request(
        authority,
        _request("activate_worker", {"run_dir": str(run_dir)}),
        peer_uid=peer_uid,
        peer_pid=peer_pid,
        trusted_writer_uids=frozenset({peer_uid}),
        worker_capabilities=registry,
    )
    capability = activated["capability"]
    accepted = dispatch_request(
        authority,
        _request("enroll", {"run_dir": str(run_dir)}, capability),
        peer_uid=peer_uid,
        peer_pid=peer_pid,
        trusted_writer_uids=frozenset({peer_uid}),
        worker_capabilities=registry,
    )
    assert accepted["caller_role"] == "trusted_writer"
    started = dispatch_request(
        authority,
        _request(
            "begin_transition",
            {
                "run_dir": str(run_dir),
                "checkpoint_kind": "phase_boundary",
                "transition_id": "transition-1",
                "content_store_required": False,
            },
            capability,
        ),
        peer_uid=peer_uid,
        peer_pid=peer_pid,
        trusted_writer_uids=frozenset({peer_uid}),
        worker_capabilities=registry,
    )
    assert started["authenticated_process"].endswith(
        f":capability:{activated['capability_id'][:32]}"
    )
    committed = dispatch_request(
        authority,
        _request(
            "content_store_commit_transition",
            {"run_dir": str(run_dir), "lease_id": "content-lease-1"},
            capability,
        ),
        peer_uid=peer_uid,
        peer_pid=peer_pid,
        trusted_writer_uids=frozenset({peer_uid}),
        worker_capabilities=registry,
    )
    assert committed["caller_role"] == "trusted_writer"
    assert ("lease:content-lease-1", str(run_dir)) in authority.calls

    with pytest.raises(AuthorityPermissionError, match="process and run"):
        dispatch_request(
            authority,
            _request("enroll", {"run_dir": str(other_run)}, capability),
            peer_uid=peer_uid,
            peer_pid=peer_pid,
            trusted_writer_uids=frozenset({peer_uid}),
            worker_capabilities=registry,
        )
    with pytest.raises(AuthorityPermissionError, match="process and run"):
        dispatch_request(
            authority,
            _request("enroll", {"run_dir": str(run_dir)}, capability),
            peer_uid=peer_uid,
            peer_pid=peer_pid + 1,
            trusted_writer_uids=frozenset({peer_uid}),
            worker_capabilities=registry,
        )



def test_worker_activation_rejects_code_changed_since_authority_start(tmp_path):
    authority = _RecordingAuthority()
    registry = _registry(tmp_path)
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    code_file = tmp_path / "code" / "core" / "worker.py"
    code_file.write_text("VALUE = 2\n", encoding="utf-8")
    peer_uid = _TEST_PEER_UID

    with pytest.raises(AuthorityPermissionError, match="code changed"):
        dispatch_request(
            authority,
            _request("activate_worker", {"run_dir": str(run_dir)}),
            peer_uid=peer_uid,
            peer_pid=os.getpid(),
            trusted_writer_uids=frozenset({peer_uid}),
            worker_capabilities=registry,
        )


def test_worker_capability_rejects_code_changed_after_activation(tmp_path):
    registry = _registry(tmp_path)
    peer_uid = _TEST_PEER_UID
    peer_pid = os.getpid()
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()

    activated = registry.issue(
        peer_uid=peer_uid,
        peer_pid=peer_pid,
        scope="worker",
        run_dir=str(run_dir),
    )
    assert registry.authorize(
        activated["capability"],
        peer_uid=peer_uid,
        peer_pid=peer_pid,
        scope="worker",
        run_dir=str(run_dir),
    )

    (tmp_path / "code" / "core" / "worker.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    with pytest.raises(AuthorityPermissionError, match="code changed"):
        registry.authorize(
            activated["capability"],
            peer_uid=peer_uid,
            peer_pid=peer_pid,
            scope="worker",
            run_dir=str(run_dir),
        )


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX peer credentials")
def test_serve_requires_authority_uid_isolation_before_socket_bind(tmp_path, monkeypatch):
    authority = _RecordingAuthority()
    socket_path = tmp_path / "authority.sock"
    authority_uid = os.getuid()

    with pytest.raises(AuthorityPermissionError, match="trusted-writer UID"):
        serve(
            socket_path=str(socket_path),
            authority=authority,
            trusted_writer_uids=frozenset({authority_uid}),
            max_requests=0,
        )

    assert not socket_path.exists()
    with pytest.raises(AuthorityPermissionError, match="configured agent UID"):
        serve(
            socket_path=str(socket_path),
            authority=authority,
            trusted_writer_uids=frozenset(),
            peer_identities={authority_uid: "agent:unisolated"},
            max_requests=0,
        )

    assert not socket_path.exists()
    with pytest.raises(AuthorityPermissionError, match="must not overlap"):
        serve(
            socket_path=str(socket_path),
            authority=authority,
            trusted_writer_uids=frozenset({authority_uid + 1}),
            peer_identities={authority_uid + 1: "agent:overlap"},
            max_requests=0,
        )

    assert not socket_path.exists()

    class _Listener:
        closed = False

        def close(self):
            self.closed = True

    listener = _Listener()
    monkeypatch.setattr(
        "core.infra.integrity.service._prepare_socket",
        lambda _socket_path: (listener, str(socket_path)),
    )
    serve(
        socket_path=str(socket_path),
        authority=authority,
        trusted_writer_uids=frozenset({authority_uid + 1}),
        peer_identities={authority_uid + 2: "agent:isolated"},
        max_requests=0,
    )
    assert listener.closed


def test_service_check_results_attest_isolated_trust_domain():
    result = {"status": "clean", "audit_ready": True}
    assert _attest_service_check_result("check", result) == {
        **result,
        "trust_domain_isolated": True,
    }
    assert _attest_service_check_result("content_store_check", result) == {
        **result,
        "trust_domain_isolated": True,
    }
    assert _attest_service_check_result("enroll", result) is result


def test_generic_python_process_cannot_activate_official_worker(tmp_path):
    authority = _RecordingAuthority()
    registry = WorkerCapabilityRegistry()
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    peer_uid = _TEST_PEER_UID

    with pytest.raises(AuthorityPermissionError, match="official Callimachus process"):
        dispatch_request(
            authority,
            _request("activate_worker", {"run_dir": str(run_dir)}),
            peer_uid=peer_uid,
            peer_pid=os.getpid(),
            trusted_writer_uids=frozenset({peer_uid}),
            worker_capabilities=registry,
        )


def test_content_store_enrollment_requires_official_admin_capability(tmp_path):
    authority = _RecordingAuthority()
    registry = _registry(tmp_path)
    peer_uid = _TEST_PEER_UID
    peer_pid = os.getpid()

    with pytest.raises(AuthorityPermissionError, match="active Callimachus worker"):
        dispatch_request(
            authority,
            _request("content_store_enroll", {}),
            peer_uid=peer_uid,
            peer_pid=peer_pid,
            trusted_writer_uids=frozenset({peer_uid}),
            worker_capabilities=registry,
        )
    activated = dispatch_request(
        authority,
        _request("activate_admin", {}),
        peer_uid=peer_uid,
        peer_pid=peer_pid,
        trusted_writer_uids=frozenset({peer_uid}),
        worker_capabilities=registry,
    )
    accepted = dispatch_request(
        authority,
        _request("content_store_enroll", {}, activated["capability"]),
        peer_uid=peer_uid,
        peer_pid=peer_pid,
        trusted_writer_uids=frozenset({peer_uid}),
        worker_capabilities=registry,
    )
    assert accepted["caller_role"] == "trusted_writer"
