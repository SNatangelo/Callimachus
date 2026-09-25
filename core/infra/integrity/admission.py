# core/infra/integrity/admission.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted, bounded admission of external task answers into a run."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from typing import Any

from core.infra.db import ExecutionAssuranceRecord, RunRepository

from .authority import AuthorityError, FileAuthority
from .local_identity import local_process_identity


MAX_ADMITTED_FILE_BYTES = 512 * 1024 * 1024
MAX_ADMITTED_FILES = 512
MAX_ADMITTED_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_PRODUCER_CLASSES = {"operator", "agent", "automation"}
_SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _producer(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or "\0" in value or ":" not in value:
        raise AuthorityError("authenticated producer identity is invalid")
    producer_class, identity = value.split(":", 1)
    if producer_class not in _PRODUCER_CLASSES or not identity.strip():
        raise AuthorityError("authenticated producer identity is invalid")
    return producer_class, identity.strip()


def _actor_type(producer_class: str) -> str:
    return {
        "operator": "user",
        "agent": "agent",
        "automation": "script",
    }[producer_class]


def _ensure_real_directory(path: str, *, create: bool) -> None:
    if create:
        os.makedirs(path, mode=0o700, exist_ok=True)
    if os.path.islink(path) or not os.path.isdir(path):
        raise AuthorityError("controlled inbox path must be a real directory")


def _open_admitted_source(source_path: str) -> tuple[str, int, os.stat_result]:
    requested = os.path.abspath(source_path)
    realpath = os.path.realpath(requested)
    requested_key = os.path.normcase(os.path.normpath(requested))
    realpath_key = os.path.normcase(os.path.normpath(realpath))
    if realpath_key != requested_key or os.path.islink(requested):
        raise AuthorityError("admitted source must not use symlinks")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        source = os.open(requested, flags)
    except OSError as exc:
        raise AuthorityError("admitted source file is unavailable") from exc
    try:
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode):
            raise AuthorityError("admitted source must be a regular file")
        if before.st_size > MAX_ADMITTED_FILE_BYTES:
            raise AuthorityError("admitted source exceeds the closed size limit")
    except Exception:
        os.close(source)
        raise
    return requested, source, before


def _verify_copied_source(
    source: int,
    *,
    expected_byte_count: int,
    expected_digest: bytes,
    remaining_answer_bytes: int,
) -> None:
    try:
        os.lseek(source, 0, os.SEEK_SET)
        verification_digest = hashlib.sha256()
        verification_byte_count = 0
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            verification_byte_count += len(chunk)
            if verification_byte_count > MAX_ADMITTED_FILE_BYTES:
                raise AuthorityError("admitted source exceeds the closed size limit")
            if verification_byte_count > remaining_answer_bytes:
                raise AuthorityError(
                    "admitted sources exceed the closed aggregate size limit"
                )
            verification_digest.update(chunk)
    except AuthorityError:
        raise
    except OSError as exc:
        raise AuthorityError("admitted source could not be verified after copying") from exc
    if (
        verification_byte_count != expected_byte_count
        or verification_digest.digest() != expected_digest
    ):
        raise AuthorityError("admitted source changed while copied")


def _copy_one_file(
    source_path: str,
    destination_path: str,
    *,
    run_dir: str,
    remaining_answer_bytes: int,
) -> dict[str, Any]:
    requested, source, before = _open_admitted_source(source_path)
    destination = None
    try:
        if before.st_size > remaining_answer_bytes:
            raise AuthorityError(
                "admitted sources exceed the closed aggregate size limit"
            )
        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            destination_flags |= os.O_BINARY
        destination = os.open(destination_path, destination_flags, 0o600)
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            byte_count += len(chunk)
            if byte_count > MAX_ADMITTED_FILE_BYTES:
                raise AuthorityError("admitted source exceeds the closed size limit")
            if byte_count > remaining_answer_bytes:
                raise AuthorityError(
                    "admitted sources exceed the closed aggregate size limit"
                )
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(destination, chunk[offset:])
                if written <= 0:
                    raise AuthorityError("controlled inbox write was incomplete")
                offset += written
        _verify_copied_source(
            source,
            expected_byte_count=byte_count,
            expected_digest=digest.digest(),
            remaining_answer_bytes=remaining_answer_bytes,
        )
        os.fsync(destination)
    except Exception:
        if destination is not None:
            os.close(destination)
            destination = None
        try:
            os.unlink(destination_path)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(source)
        if destination is not None:
            os.close(destination)

    return {
        "original_name": os.path.basename(requested),
        "stored_path": os.path.relpath(destination_path, run_dir).replace(os.sep, "/"),
        "sha256": digest.hexdigest(),
        "byte_count": byte_count,
    }


def _preflight_source_files(source_paths: list[str]) -> None:
    if len(source_paths) > MAX_ADMITTED_FILES:
        raise AuthorityError("task answer exceeds the closed file-count limit")
    total_bytes = 0
    for source_path in source_paths:
        _, source, before = _open_admitted_source(source_path)
        try:
            total_bytes += before.st_size
            if total_bytes > MAX_ADMITTED_TOTAL_BYTES:
                raise AuthorityError(
                    "admitted sources exceed the closed aggregate size limit"
                )
        finally:
            os.close(source)


def _copy_payload_files(
    value: Any,
    *,
    run_dir: str,
    answer_id: str,
) -> tuple[Any, list[dict[str, Any]], str | None]:
    source_paths: list[str] = []

    def collect(item: Any) -> Any:
        if isinstance(item, dict):
            output = {}
            for key, child in item.items():
                if key == "file_path":
                    if not isinstance(child, str) or not child.strip():
                        raise AuthorityError("task answer file_path is invalid")
                    index = len(source_paths)
                    source_paths.append(child)
                    output[key] = ("__controlled_file__", index)
                else:
                    output[key] = collect(child)
            return output
        if isinstance(item, list):
            return [collect(child) for child in item]
        return item

    transformed = collect(value)
    if not source_paths:
        return transformed, [], None
    _preflight_source_files(source_paths)

    inbox_root = os.path.join(run_dir, ".integrity-inbox")
    _ensure_real_directory(inbox_root, create=True)
    answers_root = os.path.join(inbox_root, "task-answers")
    _ensure_real_directory(answers_root, create=True)
    answer_root = os.path.join(answers_root, answer_id)
    try:
        os.mkdir(answer_root, mode=0o700)
    except OSError as exc:
        raise AuthorityError("controlled answer inbox already exists") from exc

    files: list[dict[str, Any]] = []
    destinations: list[str] = []
    copied_bytes = 0
    try:
        for index, source_path in enumerate(source_paths):
            extension = os.path.splitext(source_path)[1]
            extension = extension if _SAFE_EXTENSION.fullmatch(extension) else ".bin"
            destination = os.path.join(answer_root, f"file-{index:03d}{extension.lower()}")
            file_record = _copy_one_file(
                source_path,
                destination,
                run_dir=run_dir,
                remaining_answer_bytes=MAX_ADMITTED_TOTAL_BYTES - copied_bytes,
            )
            destinations.append(destination)
            files.append(file_record)
            copied_bytes += file_record["byte_count"]

        def replace(item: Any) -> Any:
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and item[0] == "__controlled_file__"
            ):
                return destinations[item[1]]
            if isinstance(item, dict):
                return {key: replace(child) for key, child in item.items()}
            if isinstance(item, list):
                return [replace(child) for child in item]
            return item

        return replace(transformed), files, answer_root
    except Exception:
        for destination in reversed(destinations):
            try:
                os.unlink(destination)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(answer_root)
        except OSError:
            pass
        raise


def _ingress_kind(payload: Any, file_count: int) -> str:
    inline = False

    def visit(item: Any) -> None:
        nonlocal inline
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"text", "quote"} and isinstance(child, str) and child:
                    inline = True
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(payload)
    if file_count and inline:
        return "controlled_mixed"
    if file_count:
        return "controlled_file"
    if inline:
        return "controlled_inline"
    return "controlled_metadata"


def admit_task_answer(
    authority: FileAuthority,
    *,
    run_dir: str,
    task_id: str,
    raw_payload: dict[str, Any],
    producer: str,
    authenticated_uid: int,
) -> dict[str, Any]:
    """Admit one closed task answer and return its signed run checkpoint."""
    if not isinstance(raw_payload, dict):
        raise AuthorityError("task answer payload must be an object")
    producer_class, producer_identity = _producer(producer)
    if type(authenticated_uid) is not int or authenticated_uid < 0:
        raise AuthorityError("authenticated UID is invalid")
    caller = f"{producer_class}:{producer_identity}@uid:{authenticated_uid}"
    lease = authority.begin_transition(
        run_dir,
        checkpoint_kind="external_input",
        caller_role="trusted_writer",
        authenticated_caller=caller,
    )["lease"]
    answer_id = f"answer-{uuid.uuid4().hex[:20]}"
    answer_root = None
    answer_persisted = False
    repo = None
    try:
        repo = RunRepository.open(run_dir)
        repo.validate_task_answer(task_id=task_id, raw_payload=raw_payload)
        controlled_payload, files, answer_root = _copy_payload_files(
            raw_payload,
            run_dir=os.path.abspath(run_dir),
            answer_id=answer_id,
        )
        provenance = {
            "answer_id": answer_id,
            "producer_class": producer_class,
            "producer_identity": producer_identity,
            "authenticated_uid": authenticated_uid,
            "ingress_kind": _ingress_kind(controlled_payload, len(files)),
            "admitted_at": _now(),
            "file_count": len(files),
            "authority_id": authority.authority_id,
        }
        repo.submit_task_answer_with_provenance(
            task_id=task_id,
            actor_type=_actor_type(producer_class),
            raw_payload=controlled_payload,
            answer_id=answer_id,
            provenance=provenance,
            files=files,
            expected_assurance=repo.get_execution_assurance(),
        )
        answer_persisted = True
        repo.close()
        repo = None
        committed = authority.commit_transition(
            run_dir,
            lease_id=lease["lease_id"],
            caller_role="trusted_writer",
            authenticated_caller=caller,
        )
        return {
            **committed,
            "answer_id": answer_id,
            "provenance": {**provenance, "files": files},
        }
    except BaseException as exc:
        if repo is not None:
            repo.close()
        if not answer_persisted and answer_root is not None:
            try:
                for name in os.listdir(answer_root):
                    os.unlink(os.path.join(answer_root, name))
                os.rmdir(answer_root)
            except OSError:
                pass
        try:
            authority.abort_transition(
                run_dir,
                lease_id=lease["lease_id"],
                reason=f"task answer admission raised {type(exc).__name__}",
                caller_role="trusted_writer",
                authenticated_caller=caller,
            )
        except AuthorityError as abort_exc:
            raise AuthorityError(
                f"task answer admission failed and transition abort failed: {abort_exc}"
            ) from exc
        raise


def admit_task_answer_locally(
    *, run_dir: str, task_id: str, raw_payload: dict[str, Any],
    assurance: ExecutionAssuranceRecord,
    agent_identity: str | None = None,
) -> dict[str, Any]:
    """Controlled local admission for standalone/unprotected executions."""
    if not isinstance(raw_payload, dict):
        raise AuthorityError("task answer payload must be an object")
    persisted = RunRepository.open_readonly(run_dir)
    try:
        if persisted.get_execution_assurance() != assurance:
            raise AuthorityError("local task admission assurance does not match persisted run")
    finally:
        persisted.close()
    if assurance.protection == "agent_attested":
        raise AuthorityError("attested task answers require authority admission")
    if not _is_run_owner(run_dir):
        raise AuthorityError(
            "this task must be rerun through the official trusted runner; "
            "the submitting process cannot write a protected run"
        )
    if agent_identity is not None and agent_identity != assurance.agent_identity:
        raise AuthorityError("local task producer does not match persisted agent identity")
    if agent_identity is None:
        producer_class, producer_identity, actor_type = (
            "operator", "local-operator-unattested", "user"
        )
    elif assurance.protection == "agent_unprotected_acknowledged":
        producer_class, producer_identity, actor_type = (
            "agent", f"agent-unattested:{agent_identity}", "agent"
        )
    else:
        raise AuthorityError("local task admission assurance state is invalid")
    # Resolve identity before creating any inbox files or writing an answer.
    # Windows keeps its complete SID alongside the RID in the legacy numeric
    # field; neither value upgrades local execution assurance.
    authenticated_uid, principal = local_process_identity()
    if principal is not None:
        producer_identity = f"{producer_identity}@{principal}"
    answer_id = f"answer-{uuid.uuid4().hex[:20]}"
    answer_root = None
    repo = None
    try:
        controlled_payload, files, answer_root = _copy_payload_files(
            raw_payload, run_dir=os.path.abspath(run_dir), answer_id=answer_id
        )
        repo = RunRepository.open(run_dir)
        provenance = {
            "answer_id": answer_id,
            "producer_class": producer_class,
            "producer_identity": producer_identity,
            "authenticated_uid": authenticated_uid,
            "ingress_kind": _ingress_kind(controlled_payload, len(files)),
            "admitted_at": _now(),
            "file_count": len(files),
            "authority_id": "local-unattested",
        }
        repo.submit_task_answer_with_provenance(
            task_id=task_id,
            actor_type=actor_type,
            raw_payload=controlled_payload,
            answer_id=answer_id,
            provenance=provenance,
            files=files,
            expected_assurance=assurance,
        )
        return {"answer_id": answer_id, "provenance": {**provenance, "files": files}}
    except BaseException:
        if answer_root is not None:
            try:
                for name in os.listdir(answer_root):
                    os.unlink(os.path.join(answer_root, name))
                os.rmdir(answer_root)
            except OSError:
                pass
        raise
    finally:
        if repo is not None:
            repo.close()


def _is_run_owner(run_dir: str) -> bool:
    if os.name != "posix":
        return True
    try:
        current_uid = os.getuid()
        return current_uid == 0 or (
            os.stat(os.path.join(run_dir, "run.sqlite")).st_uid == current_uid
        )
    except OSError as exc:
        raise AuthorityError("cannot determine run database ownership") from exc
