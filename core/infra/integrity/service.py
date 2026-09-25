# core/infra/integrity/service.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Unix-socket service hosting the external artifact integrity authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import stat
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable

from .authority import (
    AUTHORITY_PROTOCOL_VERSION,
    AuthorityError,
    AuthorityPermissionError,
    FileAuthority,
    initialise_authority,
)
from .client import IntegrityAuthorityClient, receive_frame, send_frame
from .admission import admit_task_answer


_WORKER_MUTATIONS = frozenset(
    {
        "enroll",
        "begin_transition",
        "commit_transition",
        "abort_transition",
        "heartbeat_transition",
        "recover_pipeline_transition",
        "override",
        "content_store_begin_transition",
        "content_store_commit_transition",
        "content_store_abort_transition",
        "content_store_heartbeat_transition",
        "content_store_override",
    }
)
_ADMIN_MUTATIONS = frozenset({"content_store_enroll"})
_CONTENT_LEASE_MUTATIONS = frozenset(
    {
        "content_store_commit_transition",
        "content_store_abort_transition",
        "content_store_heartbeat_transition",
    }
)
_CODE_SUFFIXES = frozenset({".json", ".md", ".py", ".txt"})


def _callimachus_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _worker_code_digest(code_root: Path) -> str:
    root = code_root.resolve()
    candidates = [root / "run.py"]
    core_root = root / "core"
    if core_root.is_dir():
        candidates.extend(
            path
            for path in core_root.rglob("*")
            if path.suffix in _CODE_SUFFIXES and "__pycache__" not in path.parts
        )
    digest = hashlib.sha256()
    ordered = sorted(
        candidates, key=lambda item: item.relative_to(root).as_posix()
    )
    for path in ordered:
        try:
            before = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(before.st_mode):
                raise AuthorityError(
                    "Callimachus worker code contains a non-regular file"
                )
            content = path.read_bytes()
            after = path.lstat()
        except OSError as exc:
            raise AuthorityError("Callimachus worker code could not be read") from exc
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_before != identity_after or len(content) != before.st_size:
            raise AuthorityError("Callimachus worker code changed while it was read")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    if len(candidates) < 2:
        raise AuthorityError("Callimachus worker code root is incomplete")
    return digest.hexdigest()


def _attest_official_process(
    pid: int,
    uid: int,
    scope: str,
    code_root: Path,
) -> bool:
    if pid <= 0:
        return False
    try:
        process_dir = Path(f"/proc/{pid}")
        if process_dir.stat().st_uid != uid:
            return False
        executable = Path(os.path.realpath(process_dir / "exe")).name
        command = process_dir.joinpath("cmdline").read_bytes().split(b"\0")
        argv = [os.fsdecode(value) for value in command if value]
        cwd = os.readlink(process_dir / "cwd")
    except (OSError, UnicodeError):
        return False
    return _is_official_command(argv, cwd, executable, scope, code_root)


def _is_official_command(
    argv: list[str],
    cwd: str,
    executable: str,
    scope: str,
    code_root: Path,
) -> bool:
    if not executable.startswith("python") or len(argv) < 2:
        return False
    if scope == "worker":
        script = argv[1]
        resolved_script = os.path.realpath(
            script if os.path.isabs(script) else os.path.join(cwd, script)
        )
        return resolved_script == os.path.realpath(code_root / "run.py")
    if scope == "admin":
        return argv[1:4] == [
            "-m",
            "core.infra.integrity.service",
            "enroll-content-store",
        ]
    return False


@dataclass(frozen=True)
class _WorkerCapability:
    capability_id: str
    code_sha256: str
    peer_uid: int
    peer_pid: int
    process_instance: str
    run_dir: str | None
    scope: str


class WorkerCapabilityRegistry:
    """In-memory, process-bound authority for official Callimachus writers."""

    def __init__(
        self,
        *,
        code_root: str | os.PathLike[str] | None = None,
        process_attestor: Callable[[int, int, str, Path], bool] | None = None,
    ) -> None:
        self._code_root = (
            Path(code_root) if code_root is not None else _callimachus_root()
        )
        self._code_sha256 = _worker_code_digest(self._code_root)
        self._process_attestor = process_attestor or _attest_official_process
        self._sessions: dict[str, _WorkerCapability] = {}

    def issue(
        self,
        *,
        peer_uid: int,
        peer_pid: int | None,
        scope: str,
        run_dir: str | None,
    ) -> dict[str, Any]:
        if peer_pid is None or not self._process_attestor(
            peer_pid, peer_uid, scope, self._code_root
        ):
            raise AuthorityPermissionError(
                "writer activation requires an official Callimachus process"
            )
        process_instance = _process_instance(peer_pid, peer_uid)
        if ":boot:" not in process_instance:
            raise AuthorityPermissionError(
                "writer activation requires a stable process identity"
            )
        if _worker_code_digest(self._code_root) != self._code_sha256:
            raise AuthorityPermissionError(
                "Callimachus worker code changed after authority startup"
            )
        for capability_id, session in tuple(self._sessions.items()):
            if (
                _process_instance(session.peer_pid, session.peer_uid)
                != session.process_instance
            ):
                self._sessions.pop(capability_id, None)
            elif (
                session.peer_uid == peer_uid
                and session.process_instance == process_instance
                and session.scope == scope
                and session.run_dir == run_dir
            ):
                self._sessions.pop(capability_id, None)
        token = secrets.token_urlsafe(48)
        capability_id = hashlib.sha256(token.encode("ascii")).hexdigest()
        self._sessions[capability_id] = _WorkerCapability(
            capability_id=capability_id,
            code_sha256=self._code_sha256,
            peer_uid=peer_uid,
            peer_pid=peer_pid,
            process_instance=process_instance,
            run_dir=run_dir,
            scope=scope,
        )
        return {
            "capability": token,
            "capability_id": capability_id,
            "code_sha256": self._code_sha256,
            "run_dir": run_dir,
            "scope": scope,
        }

    def authorize(
        self,
        token: Any,
        *,
        peer_uid: int,
        peer_pid: int | None,
        scope: str,
        run_dir: str | None,
    ) -> str:
        if not isinstance(token, str) or not token:
            raise AuthorityPermissionError(
                "mutation requires an active Callimachus worker capability"
            )
        capability_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
        session = self._sessions.get(capability_id)
        if session is None:
            raise AuthorityPermissionError(
                "Callimachus worker capability is invalid or expired"
            )
        if (
            peer_pid is None
            or session.peer_uid != peer_uid
            or session.peer_pid != peer_pid
            or session.process_instance != _process_instance(peer_pid, peer_uid)
            or session.scope != scope
            or session.run_dir != run_dir
        ):
            raise AuthorityPermissionError(
                "Callimachus worker capability does not match this process and run"
            )
        if not self._process_attestor(peer_pid, peer_uid, scope, self._code_root):
            raise AuthorityPermissionError(
                "Callimachus worker process attestation is no longer valid"
            )
        if _worker_code_digest(self._code_root) != self._code_sha256:
            raise AuthorityPermissionError(
                "Callimachus worker code changed after authority startup"
            )
        return f"{session.process_instance}:capability:{session.capability_id[:32]}"


def _peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise AuthorityError("peer credentials are unavailable on this platform")
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    pid, uid, gid = struct.unpack("3i", raw)
    return int(pid), int(uid), int(gid)


def _peer_uid(connection: socket.socket) -> int:
    return _peer_credentials(connection)[1]


def _process_instance(pid: int | None, uid: int) -> str:
    if pid is None or pid <= 0:
        return f"uid:{uid}:process-unavailable"
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = stat_line[stat_line.rfind(")") + 2 :].split()
        start_ticks = tail[19]
    except (OSError, IndexError, ValueError):
        return f"uid:{uid}:pid:{pid}"
    return f"uid:{uid}:boot:{boot_id}:pid:{pid}:start:{start_ticks}"


def _payload_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AuthorityError(f"{key} must be non-empty NUL-free text")
    return value


def _payload_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise AuthorityError(f"{key} must be a boolean")
    return value


def _parse_peer_identities(values: list[str]) -> dict[int, str]:
    output: dict[int, str] = {}
    for raw in values:
        if "=" not in raw:
            raise AuthorityError("peer identity must use UID=CLASS:IDENTITY")
        uid_text, identity = raw.split("=", 1)
        try:
            uid = int(uid_text)
        except ValueError as exc:
            raise AuthorityError("peer identity UID must be an integer") from exc
        if uid < 0 or uid in output:
            raise AuthorityError("peer identity UID is invalid or duplicated")
        if (
            ":" not in identity
            or identity.split(":", 1)[0] not in {"operator", "agent", "automation"}
            or not identity.split(":", 1)[1].strip()
            or "\0" in identity
        ):
            raise AuthorityError("peer identity must use CLASS:IDENTITY")
        output[uid] = identity.strip()
    return output


def dispatch_request(
    authority: FileAuthority,
    request: dict[str, Any],
    *,
    peer_uid: int,
    peer_pid: int | None = None,
    trusted_writer_uids: frozenset[int],
    peer_identities: dict[int, str] | None = None,
    worker_capabilities: WorkerCapabilityRegistry | None = None,
) -> dict[str, Any]:
    if set(request) != {
        "version",
        "operation",
        "payload",
        "worker_capability",
    }:
        raise AuthorityError("authority request has unsupported fields")
    if request["version"] != AUTHORITY_PROTOCOL_VERSION:
        raise AuthorityError("authority request protocol version is invalid")
    operation = request["operation"]
    payload = request["payload"]
    if not isinstance(operation, str) or not isinstance(payload, dict):
        raise AuthorityError("authority request is malformed")
    if operation in {"activate_worker", "activate_admin"}:
        if peer_uid not in trusted_writer_uids:
            raise AuthorityPermissionError(
                "writer activation requires a server-approved UID"
            )
        if worker_capabilities is None:
            raise AuthorityPermissionError("writer capability service is unavailable")
        if operation == "activate_worker":
            if set(payload) != {"run_dir"}:
                raise AuthorityError("worker activation payload has unsupported fields")
            run_dir = authority.resolve_run_dir(_payload_text(payload, "run_dir"))
            return worker_capabilities.issue(
                peer_uid=peer_uid,
                peer_pid=peer_pid,
                scope="worker",
                run_dir=run_dir,
            )
        if payload:
            raise AuthorityError("admin activation payload has unsupported fields")
        return worker_capabilities.issue(
            peer_uid=peer_uid,
            peer_pid=peer_pid,
            scope="admin",
            run_dir=None,
        )

    role = "agent"
    configured_identity = (peer_identities or {}).get(peer_uid)
    caller = (
        f"{configured_identity}@uid:{peer_uid}"
        if configured_identity is not None
        else f"uid:{peer_uid}"
    )
    process = _process_instance(peer_pid, peer_uid)
    if operation in _WORKER_MUTATIONS | _ADMIN_MUTATIONS:
        if worker_capabilities is None:
            raise AuthorityPermissionError("writer capability service is unavailable")
        scope = "admin" if operation in _ADMIN_MUTATIONS else "worker"
        run_dir = None
        if scope == "worker":
            run_dir = authority.resolve_run_dir(_payload_text(payload, "run_dir"))
        process = worker_capabilities.authorize(
            request["worker_capability"],
            peer_uid=peer_uid,
            peer_pid=peer_pid,
            scope=scope,
            run_dir=run_dir,
        )
        role = "trusted_writer"
        if operation in _CONTENT_LEASE_MUTATIONS:
            if run_dir is None:
                raise AuthorityError("content-store lease run scope is unavailable")
            authority.require_content_store_transition_run(
                run_dir,
                lease_id=_payload_text(payload, "lease_id"),
            )
    if operation == "admit_task_answer":
        if configured_identity is None:
            raise AuthorityPermissionError(
                "task-answer peer identity is not configured"
            )
        if set(payload) != {"run_dir", "task_id", "raw_payload"}:
            raise AuthorityError("task-answer admission payload has unsupported fields")
        return admit_task_answer(
            authority,
            run_dir=_payload_text(payload, "run_dir"),
            task_id=_payload_text(payload, "task_id"),
            raw_payload=payload["raw_payload"],
            producer=configured_identity,
            authenticated_uid=peer_uid,
        )
    if operation == "content_store_enroll":
        return authority.enroll_content_store(caller_role=role)
    if operation == "content_store_check":
        return authority.check_content_store(authenticated_caller=caller)
    if operation == "content_store_begin_transition":
        return authority.begin_content_store_transition(
            _payload_text(payload, "run_dir"),
            transition_id=_payload_text(payload, "transition_id"),
            caller_role=role,
            authenticated_caller=caller,
            authenticated_process=process,
        )
    if operation == "content_store_commit_transition":
        return authority.commit_content_store_transition(
            lease_id=_payload_text(payload, "lease_id"),
            caller_role=role,
            authenticated_caller=caller,
            authenticated_process=process,
        )
    if operation == "content_store_abort_transition":
        return authority.abort_content_store_transition(
            lease_id=_payload_text(payload, "lease_id"),
            reason=_payload_text(payload, "reason"),
            caller_role=role,
            authenticated_caller=caller,
            authenticated_process=process,
        )
    if operation == "content_store_heartbeat_transition":
        return authority.heartbeat_content_store_transition(
            lease_id=_payload_text(payload, "lease_id"),
            caller_role=role,
            authenticated_caller=caller,
            authenticated_process=process,
        )
    if operation == "content_store_override":
        return authority.override_content_store(
            reason=_payload_text(payload, "reason"),
            authenticated_caller=caller,
        )
    run_dir = _payload_text(payload, "run_dir")
    if operation == "enroll":
        return authority.enroll(run_dir, caller_role=role)
    if operation == "check":
        return authority.check(run_dir, authenticated_caller=caller)
    if operation == "begin_transition":
        return authority.begin_transition(
            run_dir,
            checkpoint_kind=_payload_text(payload, "checkpoint_kind"),
            caller_role=role,
            authenticated_caller=caller,
            authenticated_process=process,
            transition_id=_payload_text(payload, "transition_id"),
            content_store_required=_payload_bool(
                payload, "content_store_required"
            ),
        )
    if operation == "commit_transition":
        return authority.commit_transition(
            run_dir,
            lease_id=_payload_text(payload, "lease_id"),
            caller_role=role,
            authenticated_caller=caller,
            authenticated_process=process,
        )
    if operation == "abort_transition":
        return authority.abort_transition(
            run_dir,
            lease_id=_payload_text(payload, "lease_id"),
            reason=_payload_text(payload, "reason"),
            caller_role=role,
            authenticated_caller=caller,
            authenticated_process=process,
        )
    if operation == "heartbeat_transition":
        return authority.heartbeat_transition(
            run_dir,
            lease_id=_payload_text(payload, "lease_id"),
            caller_role=role,
            authenticated_caller=caller,
            authenticated_process=process,
        )
    if operation == "recover_pipeline_transition":
        return authority.recover_pipeline_transition(
            run_dir,
            caller_role=role,
            authenticated_caller=caller,
            authenticated_process=process,
        )
    if operation == "override":
        return authority.override(
            run_dir,
            reason=_payload_text(payload, "reason"),
            authenticated_caller=caller,
        )
    raise AuthorityError("authority operation is unsupported")


def _prepare_socket(socket_path: str) -> tuple[socket.socket, str]:
    path = os.path.abspath(socket_path)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if os.path.lexists(path):
        current = os.lstat(path)
        if not stat.S_ISSOCK(current.st_mode):
            raise AuthorityError("authority socket path exists and is not a socket")
        os.unlink(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    os.chmod(path, 0o660)
    listener.listen(16)
    return listener, path


def _attest_service_check_result(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    if operation in {"check", "content_store_check"}:
        return {**result, "trust_domain_isolated": True}
    return result


def serve(
    *,
    socket_path: str,
    authority: FileAuthority,
    trusted_writer_uids: frozenset[int],
    peer_identities: dict[int, str] | None = None,
    worker_capabilities: WorkerCapabilityRegistry | None = None,
    ready_event: Event | None = None,
    max_requests: int | None = None,
) -> None:
    authority_uid = os.getuid()
    configured_agent_uids = frozenset((peer_identities or {}).keys())
    overlapping_uids = trusted_writer_uids & configured_agent_uids
    if overlapping_uids:
        raise AuthorityPermissionError(
            "trusted-writer UIDs must not overlap configured agent UIDs"
        )
    if authority_uid in trusted_writer_uids:
        raise AuthorityPermissionError(
            "authority must not run under a trusted-writer UID"
        )
    if authority_uid in configured_agent_uids:
        raise AuthorityPermissionError(
            "authority must not run under a configured agent UID"
        )
    capabilities = worker_capabilities or WorkerCapabilityRegistry()
    listener, bound_path = _prepare_socket(socket_path)
    served = 0
    if ready_event is not None:
        ready_event.set()
    try:
        while max_requests is None or served < max_requests:
            connection, _ = listener.accept()
            with connection:
                try:
                    peer_pid, peer_uid, _peer_gid = _peer_credentials(connection)
                    request = receive_frame(connection)
                    result = dispatch_request(
                        authority,
                        request,
                        peer_uid=peer_uid,
                        peer_pid=peer_pid,
                        trusted_writer_uids=trusted_writer_uids,
                        peer_identities=peer_identities,
                        worker_capabilities=capabilities,
                    )
                    result = _attest_service_check_result(request["operation"], result)
                    response = {
                        "version": AUTHORITY_PROTOCOL_VERSION,
                        "ok": True,
                        "result": result,
                    }
                except (AuthorityError, ValueError, RuntimeError) as exc:
                    response = {
                        "version": AUTHORITY_PROTOCOL_VERSION,
                        "ok": False,
                        "message": str(exc),
                    }
                send_frame(connection, response)
            served += 1
    finally:
        listener.close()
        try:
            os.unlink(bound_path)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    initialise = sub.add_parser("init")
    initialise.add_argument("--authority-root", required=True)
    initialise.add_argument("--key-file", required=True)
    enroll_store = sub.add_parser("enroll-content-store")
    enroll_store.add_argument("--socket", required=True)
    run = sub.add_parser("serve")
    run.add_argument("--socket", required=True)
    run.add_argument("--authority-root", required=True)
    run.add_argument("--key-file", required=True)
    run.add_argument("--protected-root", required=True)
    run.add_argument("--content-store-root", required=True)
    run.add_argument("--authority-id", required=True)
    run.add_argument(
        "--trusted-writer-uid",
        type=int,
        action="append",
        required=True,
        help="UID allowed to activate an attested Callimachus writer",
    )
    run.add_argument(
        "--peer-identity",
        action="append",
        default=[],
        metavar="UID=CLASS:IDENTITY",
    )
    args = parser.parse_args(argv)
    if args.command == "init":
        initialise_authority(args.authority_root, args.key_file)
        return 0
    if args.command == "enroll-content-store":
        client = IntegrityAuthorityClient(args.socket)
        client.activate_admin()
        result = client.enroll_content_store()
        print(json.dumps(result, sort_keys=True))
        return 0
    authority = FileAuthority(
        authority_root=args.authority_root,
        key_file=args.key_file,
        protected_root=args.protected_root,
        authority_id=args.authority_id,
        content_store_root=args.content_store_root,
    )
    serve(
        socket_path=args.socket,
        authority=authority,
        trusted_writer_uids=frozenset(args.trusted_writer_uid),
        peer_identities=_parse_peer_identities(args.peer_identity),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
