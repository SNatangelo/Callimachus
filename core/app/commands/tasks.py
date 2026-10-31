#!/usr/bin/env python3
# core/app/commands/tasks.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Plain-text task inspector and answer submitter for DB-native runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import threading
import uuid
from types import SimpleNamespace
from typing import Any

try:
    from core.infra.db import RunRepository
except ImportError:  # direct execution
    from db import RunRepository
from core.fetch.admission import provided_fulltext
from core.infra.integrity import AuthorityError, IntegrityGateError, RunIntegrityGate
from core.infra.integrity.admission import admit_task_answer_locally
from core.infra.integrity.execution_assurance import resolve_existing
from core.invocation import run_command
from core.shared.filesystem_paths import io_path, logical_path


_GUIDED_FETCH_STAGE_SCHEMA = "callimachus.guided-fetch-stage.v1"
_GUIDED_FETCH_STAGE_ROOT = "sources/guided_fetch_stages"
_GUIDED_FETCH_STAGE_LOCK = threading.RLock()
_GUIDED_FETCH_STAGE_FIELDS = {
    "schema", "action", "sequence", "event_id", "task_id", "task_kind",
    "task_generation", "ref_id", "source_tier", "identity_attested",
    "source_ref", "original_filename", "original_path", "controlled_path",
    "sha256", "byte_count", "supersedes_ocr_scan_ids",
}
_GUIDED_FETCH_IDENTITY_SKIP_FIELDS = {
    "schema", "action", "sequence", "event_id", "task_id", "task_kind",
    "task_generation", "ref_id", "source_tier", "source_stage_event_id",
    "source_sha256", "actor_type",
}
_GUIDED_FETCH_STAGE_FILENAME = re.compile(r"^(?P<sequence>[0-9]{8})-(?P<event_id>[0-9a-f]{32})\.json$")


def _repo(run_dir: str) -> RunRepository:
    """Open task views without granting the submitting peer write access."""
    return RunRepository.open_readonly(run_dir)


def _answer_gate(args):
    try:
        return resolve_existing(
            args.run,
            args.agent_identity,
            debug_override=args.debug_override_artifact_integrity,
            override_reason=args.debug_override_reason,
            # The authority performs the integrity check atomically inside
            # admit_task_answer.  Do not grant the submitting agent a worker
            # capability merely to duplicate that preflight here.
            activation_only=True,
            activate_worker=False,
            mirror_audit_records=False,
            allow_local_downgrade=True,
        )
    except (AuthorityError, IntegrityGateError) as exc:
        raise SystemExit(f"integrity gate stopped task answer: {exc}") from exc


def _read_text_maybe(path_or_text: str) -> str:
    if os.path.exists(path_or_text):
        with open(path_or_text, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path_or_text


def _require_task(repo: RunRepository, task_id: str):
    task = repo.get_task(task_id)
    if task is None:
        raise SystemExit(f"task not found: {task_id}")
    if task.status == "applied":
        raise SystemExit(f"task already applied: {task_id}")
    if task.status == "cancelled":
        raise SystemExit(f"task cancelled: {task_id}")
    return task


def _contains_guided_fetch_payload(payload: Any) -> bool:
    """Whether a closed Fetch answer claims user-guided provenance."""
    if isinstance(payload, dict):
        return bool(payload.get("guided_fetch")) or any(
            _contains_guided_fetch_payload(value) for value in payload.values()
        )
    if isinstance(payload, list):
        return any(_contains_guided_fetch_payload(value) for value in payload)
    return False


def admit_fetch_payload(
    run_dir: str,
    task_id: str,
    payload: dict[str, Any],
    *,
    agent_identity: str | None = None,
    debug_override_artifact_integrity: bool = False,
    debug_override_reason: str | None = None,
) -> dict:
    """Admit a closed Fetch answer through the existing integrity boundary.

    Guided Fetch is a human-operated recovery surface. An agent-identified
    process may open its desktop UI, but it cannot submit or attest its
    answers: a separate authenticated operator channel is required.
    """
    if not isinstance(payload, dict):
        raise SystemExit("fetch answer payload must be an object")
    guided_payload = _contains_guided_fetch_payload(payload)
    if guided_payload and agent_identity is not None:
        raise SystemExit(
            "guided Fetch answers require a separately authenticated human "
            "operator; an agent-identified caller cannot submit them"
        )
    args = SimpleNamespace(
        run=run_dir,
        agent_identity=agent_identity,
        debug_override_artifact_integrity=debug_override_artifact_integrity,
        debug_override_reason=debug_override_reason,
    )
    resolution = _answer_gate(args)
    gate = getattr(resolution, "gate", resolution)
    repo = _repo(run_dir)
    try:
        _require_task(repo, task_id)
    finally:
        repo.close()
    try:
        return (
            gate.admit_task_answer(run_dir, task_id, payload, mirror_checkpoint=False)
            if gate is not None else admit_task_answer_locally(
                run_dir=run_dir,
                task_id=task_id,
                raw_payload=payload,
                assurance=resolution.assurance,
                agent_identity=agent_identity,
            )
        )
    except (AuthorityError, IntegrityGateError) as exc:
        raise SystemExit(f"integrity gate rejected task answer: {exc}") from exc


def store_guided_ocr_artifact(
    run_dir: str,
    *,
    scan_id: str,
    ref_id: str,
    ref_number: int | None,
    scan_path: str,
    scan_sha256: str,
    scan_token: str,
    source_ref: str,
    display_name: str,
    status: str,
    reason: str | None = None,
    text: str | None = None,
    method: str | None = None,
    discarded: bool = False,
    identity_attested: bool = False,
    debug_override_artifact_integrity: bool = False,
    debug_override_reason: str | None = None,
) -> dict[str, Any]:
    """Persist one selected scan/OCR result under an artifact checkpoint.

    Staging an OCR result does not answer its Fetch task. Protected runs record
    the scan, derived text, and metadata through a signed external-input
    transition so a closed Guided Fetch window can safely resume the operation.
    """
    if not isinstance(scan_id, str) or not re.fullmatch(r"[0-9a-f]{64}", scan_id):
        raise ValueError("guided OCR scan_id is invalid")
    if not isinstance(ref_id, str) or not ref_id.strip():
        raise ValueError("guided OCR ref_id is invalid")
    if not isinstance(scan_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", scan_sha256):
        raise ValueError("guided OCR scan hash is invalid")
    if status not in {"pending", "failed", "done"}:
        raise ValueError("guided OCR artifact status is invalid")
    if not isinstance(scan_token, str) or not scan_token:
        raise ValueError("guided OCR scan token is invalid")
    if status == "done" and (not isinstance(text, str) or not method):
        raise ValueError("completed guided OCR artifact requires text and method")
    if type(discarded) is not bool or (discarded and status != "done"):
        raise ValueError("only a completed guided OCR artifact can be discarded")
    if type(identity_attested) is not bool:
        raise ValueError("guided OCR identity attestation must be boolean")
    if text is not None and status != "done":
        raise ValueError("guided OCR text is only valid for a completed artifact")
    source = Path(scan_path)
    source_io = Path(io_path(source))
    if not source_io.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError("guided OCR source must be an existing PDF")

    run_root = logical_path(run_dir)
    artifact_dir = run_root / "sources" / "guided_ocr"
    scan_target = artifact_dir / f"{scan_id}.pdf"
    metadata_target = artifact_dir / f"{scan_id}.json"
    artifact_dir_io = Path(io_path(artifact_dir))
    scan_target_io = Path(io_path(scan_target))
    metadata_target_io = Path(io_path(metadata_target))
    source_digest = _file_sha256(scan_path)
    if source_digest != scan_sha256:
        raise ValueError("guided OCR scan changed before it could be stored")
    if provided_fulltext.ocr_scan_id(ref_id, scan_token, scan_sha256) != scan_id:
        raise ValueError("guided OCR scan identity does not match its PDF")
    text_sha256 = (
        hashlib.sha256(text.encode("utf-8")).hexdigest()
        if text is not None else None
    )
    text_target = (
        artifact_dir / f"{scan_id}-{text_sha256}.txt"
        if text_sha256 is not None else None
    )
    text_target_io = None if text_target is None else Path(io_path(text_target))
    relative_text_path = (
        text_target.relative_to(run_root).as_posix()
        if text_target is not None else None
    )
    metadata = {
        "scan_id": scan_id,
        "ref_id": ref_id,
        "ref_number": ref_number,
        "display_name": Path(display_name).name or "scan.pdf",
        "scan_sha256": scan_sha256,
        "scan_token": scan_token,
        "source_ref": source_ref,
        "status": status,
        "reason": reason,
        "ocr_method": method,
        "discarded": discarded,
        "identity_attested": identity_attested,
        "text_path": relative_text_path,
        "text_sha256": text_sha256,
    }

    gate = None
    lease_id = None
    try:
        try:
            resolution = _answer_gate(SimpleNamespace(
                run=run_dir,
                agent_identity=None,
                debug_override_artifact_integrity=debug_override_artifact_integrity,
                debug_override_reason=debug_override_reason,
            ))
        except SystemExit as exc:
            raise ValueError(str(exc)) from exc
        gate = getattr(resolution, "gate", resolution)
        if gate is not None:
            lease_id = gate.begin_transition(
                run_dir, checkpoint_kind="external_input"
            )

        sources_dir = run_root / "sources"
        sources_dir_io = Path(io_path(sources_dir))
        if sources_dir_io.is_symlink():
            raise ValueError("guided OCR sources directory cannot be a symlink")
        if artifact_dir_io.is_symlink():
            raise ValueError("guided OCR artifact directory cannot be a symlink")
        sources_dir_io.mkdir(parents=True, exist_ok=True)
        artifact_dir_io.mkdir(parents=True, exist_ok=True)
        if artifact_dir_io.is_symlink():
            raise ValueError("guided OCR artifact directory cannot be a symlink")
        if scan_target_io.is_symlink() or metadata_target_io.is_symlink():
            raise ValueError("guided OCR artifact files cannot be symlinks")
        if scan_target_io.exists():
            if _file_sha256(scan_target) != scan_sha256:
                raise ValueError("guided OCR scan_id already names different PDF bytes")
        else:
            with source_io.open("rb") as reader, scan_target_io.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            if _file_sha256(scan_target) != scan_sha256:
                raise ValueError("guided OCR copied scan hash does not match source")

        if text_target is not None and text_target_io is not None:
            if text_target_io.is_symlink():
                raise ValueError("guided OCR text artifact cannot be a symlink")
            if text_target_io.exists():
                if _file_sha256(text_target) != text_sha256:
                    raise ValueError("guided OCR text artifact hash does not match")
            else:
                with text_target_io.open("xb") as handle:
                    handle.write(text.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())

        if metadata_target_io.exists():
            try:
                previous = json.loads(metadata_target_io.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("guided OCR artifact metadata is unreadable") from exc
            if (
                previous.get("scan_id") != scan_id
                or previous.get("ref_id") != ref_id
                or previous.get("scan_sha256") != scan_sha256
                or previous.get("identity_attested") is not identity_attested
            ):
                raise ValueError("guided OCR artifact identity changed")
        temporary = artifact_dir / f".{scan_id}.{uuid.uuid4().hex}.tmp"
        temporary_io = Path(io_path(temporary))
        try:
            with temporary_io.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(metadata, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_io, metadata_target_io)
        finally:
            try:
                temporary_io.unlink()
            except FileNotFoundError:
                pass
        _ensure_guided_ocr_fetch_task(run_dir, ref_id)
        if gate is not None and lease_id is not None:
            gate.commit_transition(run_dir, lease_id)
            lease_id = None
        return {
            **metadata,
            "scan_path": str(scan_target),
            "text_file_path": (
                None if text_target is None else str(text_target)
            ),
        }
    except Exception:
        if gate is not None and lease_id is not None:
            try:
                gate.abort_transition(
                    run_dir, lease_id, reason="guided OCR artifact staging failed"
                )
            except Exception:
                pass
        raise


def store_guided_fetch_stage_artifact(
    run_dir: str,
    *,
    task_id: str,
    task_kind: str,
    task_generation: int,
    ref_id: str,
    payload: dict[str, Any],
    original_path: str,
    original_filename: str,
    supersedes_ocr_scan_ids: list[str] | None = None,
    debug_override_artifact_integrity: bool = False,
    debug_override_reason: str | None = None,
) -> dict[str, Any]:
    """Durably stage one confirmed Guided Fetch source without answering its task.

    Each event and its controlled copies are immutable. On protected runs the
    files become resumable only after the external-input checkpoint commits.
    """
    return _write_guided_fetch_stage_event(
        run_dir,
        action="stage",
        task_id=task_id,
        task_kind=task_kind,
        task_generation=task_generation,
        ref_id=ref_id,
        payload=payload,
        original_path=original_path,
        original_filename=original_filename,
        supersedes_ocr_scan_ids=supersedes_ocr_scan_ids,
        debug_override_artifact_integrity=debug_override_artifact_integrity,
        debug_override_reason=debug_override_reason,
    )


def discard_guided_fetch_stage_artifact(
    run_dir: str,
    *,
    task_id: str,
    task_kind: str,
    task_generation: int,
    ref_id: str,
    supersedes_ocr_scan_ids: list[str] | None = None,
    debug_override_artifact_integrity: bool = False,
    debug_override_reason: str | None = None,
) -> dict[str, Any]:
    """Append a durable tombstone for a confirmed or damaged staged source."""
    return _write_guided_fetch_stage_event(
        run_dir,
        action="discard",
        task_id=task_id,
        task_kind=task_kind,
        task_generation=task_generation,
        ref_id=ref_id,
        payload=None,
        original_path=None,
        original_filename=None,
        supersedes_ocr_scan_ids=supersedes_ocr_scan_ids,
        debug_override_artifact_integrity=debug_override_artifact_integrity,
        debug_override_reason=debug_override_reason,
    )


def record_guided_fetch_identity_skip_intent(
    run_dir: str,
    *,
    task_id: str,
    task_kind: str,
    task_generation: int,
    ref_id: str,
    source_tier: str,
    source_sha256: str,
    source_stage_event_id: str,
    agent_identity: str | None,
    debug_override_artifact_integrity: bool = False,
    debug_override_reason: str | None = None,
) -> dict[str, Any]:
    """Journal an operator's explicit choice to leave this staged source unverified."""
    if agent_identity is not None:
        raise ValueError("guided identity review skip requires the human operator")
    if (
        source_tier not in {"fulltext", "abstract"}
        or not isinstance(source_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
        or not isinstance(source_stage_event_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", source_stage_event_id)
    ):
        raise ValueError("guided identity review skip source binding is invalid")

    with _GUIDED_FETCH_STAGE_LOCK:
        if not _guided_fetch_skip_matches_current_stage(
            run_dir,
            task_id=task_id,
            task_kind=task_kind,
            task_generation=task_generation,
            ref_id=ref_id,
            source_tier=source_tier,
            source_sha256=source_sha256,
            source_stage_event_id=source_stage_event_id,
        ):
            raise ValueError(
                "guided identity review skip no longer matches the current staged source"
            )
        state = load_guided_fetch_stage_artifacts(run_dir)
        if any(
            isinstance(record, dict)
            and record.get("source_stage_event_id") == source_stage_event_id
            and record.get("task_id") == task_id
            and record.get("task_generation") == task_generation
            for record in state.get("identity_skip_intents", [])
        ):
            return {
                "record": next(
                    record for record in state["identity_skip_intents"]
                    if record.get("source_stage_event_id") == source_stage_event_id
                    and record.get("task_id") == task_id
                    and record.get("task_generation") == task_generation
                ),
                "payload": None,
            }
        return _write_guided_fetch_stage_event(
            run_dir,
            action="identity_skip",
            task_id=task_id,
            task_kind=task_kind,
            task_generation=task_generation,
            ref_id=ref_id,
            payload=None,
            original_path=None,
            original_filename=None,
            supersedes_ocr_scan_ids=None,
            debug_override_artifact_integrity=debug_override_artifact_integrity,
            debug_override_reason=debug_override_reason,
            identity_skip_source_tier=source_tier,
            identity_skip_source_sha256=source_sha256,
            identity_skip_source_stage_event_id=source_stage_event_id,
            agent_identity=agent_identity,
        )


def _guided_fetch_skip_matches_current_stage(
    run_dir: str,
    *,
    task_id: str,
    task_kind: str,
    task_generation: int,
    ref_id: str,
    source_tier: str,
    source_sha256: str,
    source_stage_event_id: str,
) -> bool:
    ref_key = hashlib.sha256(ref_id.encode("utf-8")).hexdigest()
    state = load_guided_fetch_stage_artifacts(run_dir)
    staged = state.get("latest", {}).get(ref_key)
    record = staged.get("record") if isinstance(staged, dict) else None
    if not isinstance(record, dict) or not (
        record.get("action") == "stage"
        and record.get("event_id") == source_stage_event_id
        and record.get("task_id") == task_id
        and record.get("task_kind") == task_kind
        and record.get("task_generation") == task_generation
        and record.get("ref_id") == ref_id
        and record.get("source_tier") == source_tier
        and record.get("sha256") == source_sha256
        and record.get("identity_attested") is True
    ):
        return False

    repo = RunRepository.open_readonly(run_dir)
    try:
        task = repo.get_task(task_id)
        if (
            task is None
            or task.slot != "fetch"
            or task.task_kind != task_kind
            or task.status != "pending"
            or task.generation != task_generation
        ):
            return False
        if task_kind == "fetch":
            return task.ref_id == ref_id
        view = repo.task_view(task_id)
        references = view.get("references") if isinstance(view, dict) else None
        return isinstance(references, list) and any(
            isinstance(item, dict) and item.get("ref_id") == ref_id
            for item in references
        )
    finally:
        repo.close()


def _write_guided_fetch_stage_event(
    run_dir: str,
    *,
    action: str,
    task_id: str,
    task_kind: str,
    task_generation: int,
    ref_id: str,
    payload: dict[str, Any] | None,
    original_path: str | None,
    original_filename: str | None,
    supersedes_ocr_scan_ids: list[str] | None,
    debug_override_artifact_integrity: bool,
    debug_override_reason: str | None,
    identity_skip_source_tier: str | None = None,
    identity_skip_source_sha256: str | None = None,
    identity_skip_source_stage_event_id: str | None = None,
    agent_identity: str | None = None,
) -> dict[str, Any]:
    if action not in {"stage", "discard", "identity_skip"}:
        raise ValueError("guided Fetch stage action is invalid")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("guided Fetch stage task_id is invalid")
    if task_kind not in {"fetch", "browser_challenge"}:
        raise ValueError("guided Fetch stage task kind is invalid")
    if type(task_generation) is not int or task_generation < 0:
        raise ValueError("guided Fetch stage task generation is invalid")
    if not isinstance(ref_id, str) or not ref_id.strip():
        raise ValueError("guided Fetch stage ref_id is invalid")
    ocr_ids = sorted(set(supersedes_ocr_scan_ids or []))
    if any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in ocr_ids):
        raise ValueError("guided Fetch stage OCR supersession identities are invalid")

    source: Path | None = None
    source_io: Path | None = None
    source_sha256: str | None = None
    source_byte_count: int | None = None
    source_tier: str | None = None
    identity_attested: bool | None = None
    source_ref: str | None = None
    if action == "identity_skip":
        if (
            agent_identity is not None
            or payload is not None
            or original_path is not None
            or original_filename is not None
            or identity_skip_source_tier not in {"fulltext", "abstract"}
            or not isinstance(identity_skip_source_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", identity_skip_source_sha256)
            or not isinstance(identity_skip_source_stage_event_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", identity_skip_source_stage_event_id)
        ):
            raise ValueError("guided Fetch identity skip event has an invalid source binding")
    if action == "stage":
        if not isinstance(payload, dict) or set(payload) - {
            "found", "guided_fetch", "identity_attested", "source_tier",
            "file_path", "url",
        } or payload.get("found") is not True or payload.get("guided_fetch") is not True:
            raise ValueError("guided Fetch staged payload has an invalid shape")
        source_path = payload.get("file_path")
        if not isinstance(source_path, str) or not source_path.strip():
            raise ValueError("guided Fetch stage requires a controlled source file")
        source = Path(source_path)
        source_io = Path(io_path(source))
        if source_io.is_symlink() or not source_io.is_file():
            raise ValueError("guided Fetch source must be an existing regular file")
        source_tier = payload.get("source_tier", "fulltext")
        identity_attested = payload.get("identity_attested")
        source_ref = payload.get("url")
        if source_tier not in {"fulltext", "abstract"} or type(identity_attested) is not bool:
            raise ValueError("guided Fetch staged source provenance is invalid")
        if identity_attested is not True:
            raise ValueError("guided Fetch source must carry its explicit identity attestation")
        if source_ref is not None and (
            not isinstance(source_ref, str) or not source_ref.strip()
        ):
            raise ValueError("guided Fetch staged source_ref is invalid")
        if not isinstance(original_path, str) or not original_path.strip():
            raise ValueError("guided Fetch original path provenance is missing")
        if (
            not isinstance(original_filename, str)
            or not original_filename.strip()
            or Path(original_filename).name != original_filename
        ):
            raise ValueError("guided Fetch original filename provenance is invalid")
        if not isinstance(source_ref, (str, type(None))):
            raise ValueError("guided Fetch source_ref is invalid")
        source_sha256 = _file_sha256(source)
        if source_sha256 is None:
            raise ValueError("guided Fetch source file could not be read")
        source_byte_count = source_io.stat().st_size
        if source == Path(original_path):
            original_path = str(logical_path(source, strict=True))
        else:
            original_path = str(original_path)

    with _GUIDED_FETCH_STAGE_LOCK:
        run_root = logical_path(run_dir)
        ref_key = hashlib.sha256(ref_id.encode("utf-8")).hexdigest()
        artifact_dir = run_root / "sources" / "guided_fetch_stages" / ref_key
        artifact_dir_io = Path(io_path(artifact_dir))
        stage_root_io = Path(io_path(run_root / "sources" / "guided_fetch_stages"))
        sources_io = Path(io_path(run_root / "sources"))
        if sources_io.is_symlink() or stage_root_io.is_symlink() or artifact_dir_io.is_symlink():
            raise ValueError("guided Fetch stage directory cannot be a symlink")
        if artifact_dir_io.exists() and not artifact_dir_io.is_dir():
            raise ValueError("guided Fetch stage directory is not a directory")
        max_sequence = 0
        if artifact_dir_io.exists():
            for item in artifact_dir_io.iterdir():
                match = _GUIDED_FETCH_STAGE_FILENAME.fullmatch(item.name)
                if match is not None:
                    max_sequence = max(max_sequence, int(match.group("sequence")))
        sequence = max_sequence + 1
        if sequence > 99_999_999:
            raise ValueError("guided Fetch stage event limit was reached")
        event_id = uuid.uuid4().hex
        stem = f"{sequence:08d}-{event_id}"
        extension = source.suffix.lower() if source is not None else ""
        if extension and not re.fullmatch(r"\.[a-z0-9]{1,16}", extension):
            extension = ""
        controlled_path = (
            artifact_dir / f"{stem}-source{extension}"
            if action == "stage" else None
        )
        metadata_path = artifact_dir / f"{stem}.json"
        temporary_source = artifact_dir / f".{stem}-source.tmp"
        temporary_metadata = artifact_dir / f".{stem}.json.tmp"
        created_paths: list[Path] = []
        gate = None
        lease_id = None
        commit_attempted = False
        try:
            try:
                resolution = _answer_gate(SimpleNamespace(
                    run=run_dir,
                    agent_identity=agent_identity,
                    debug_override_artifact_integrity=debug_override_artifact_integrity,
                    debug_override_reason=debug_override_reason,
                ))
            except SystemExit as exc:
                raise ValueError(str(exc)) from exc
            gate = getattr(resolution, "gate", resolution)
            if gate is not None:
                lease_id = gate.begin_transition(run_dir, checkpoint_kind="external_input")

            sources_io.mkdir(parents=True, exist_ok=True)
            stage_root_io.mkdir(parents=True, exist_ok=True)
            artifact_dir_io.mkdir(parents=True, exist_ok=True)
            if sources_io.is_symlink() or stage_root_io.is_symlink() or artifact_dir_io.is_symlink():
                raise ValueError("guided Fetch stage directory cannot be a symlink")

            if action == "stage":
                assert source is not None and source_io is not None
                assert controlled_path is not None and source_sha256 is not None
                _copy_guided_fetch_stage_file(
                    source_io, temporary_source, controlled_path,
                    expected_sha256=source_sha256,
                    expected_byte_count=source_byte_count,
                )
                created_paths.append(controlled_path)

            common_record = {
                "schema": _GUIDED_FETCH_STAGE_SCHEMA,
                "action": action,
                "sequence": sequence,
                "event_id": event_id,
                "task_id": task_id,
                "task_kind": task_kind,
                "task_generation": task_generation,
                "ref_id": ref_id,
            }
            if action == "identity_skip":
                record = {
                    **common_record,
                    "source_tier": identity_skip_source_tier,
                    "source_sha256": identity_skip_source_sha256,
                    "source_stage_event_id": identity_skip_source_stage_event_id,
                    "actor_type": "user",
                }
            else:
                record = {
                    **common_record,
                    "source_tier": source_tier,
                    "identity_attested": identity_attested,
                    "source_ref": source_ref,
                    "original_filename": original_filename,
                    "original_path": original_path,
                    "controlled_path": (
                        None if controlled_path is None
                        else controlled_path.relative_to(run_root).as_posix()
                    ),
                    "sha256": source_sha256,
                    "byte_count": source_byte_count,
                    "supersedes_ocr_scan_ids": ocr_ids,
                }
            _validate_guided_fetch_stage_record(record, ref_key=ref_key)
            with Path(io_path(temporary_metadata)).open(
                "x", encoding="utf-8", newline="\n"
            ) as handle:
                json.dump(record, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(Path(io_path(temporary_metadata)), Path(io_path(metadata_path)))
            created_paths.append(metadata_path)
            if gate is not None and lease_id is not None:
                commit_attempted = True
                gate.commit_transition(run_dir, lease_id)
                lease_id = None
            restored_payload = None
            if action == "stage":
                restored_payload = _guided_fetch_stage_payload(run_root, record)
            return {"record": record, "payload": restored_payload}
        except Exception:
            if not commit_attempted:
                for path in [
                    *created_paths,
                    temporary_source,
                    temporary_metadata,
                ]:
                    try:
                        Path(io_path(path)).unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                if gate is not None and lease_id is not None:
                    try:
                        gate.abort_transition(
                            run_dir, lease_id,
                            reason="guided Fetch stage artifact write failed",
                        )
                    except Exception:
                        pass
            raise


def load_guided_fetch_stage_artifacts(run_dir: str) -> dict[str, Any]:
    """Load latest confirmed Guided Fetch events, distinguishing signed commits.

    Protected runs consult the latest signed checkpoint file inventory. Files
    left by an interrupted, uncommitted stage therefore cannot shadow the last
    committed event. A damaged latest committed event is returned as an issue,
    never replaced with an older stage.
    """
    root = logical_path(run_dir)
    stage_root = root / "sources" / "guided_fetch_stages"
    stage_root_io = Path(io_path(stage_root))
    sources_io = Path(io_path(root / "sources"))
    if sources_io.is_symlink() or stage_root_io.is_symlink():
        raise ValueError("guided Fetch stage directory cannot be a symlink")
    if not stage_root_io.exists():
        return {"latest": {}, "issues": {}, "identity_skip_intents": []}
    if not stage_root_io.is_dir():
        raise ValueError("guided Fetch stage directory is not a directory")

    repo = RunRepository.open_readonly(run_dir)
    try:
        assurance = repo.get_execution_assurance()
        protected = assurance.protection == "agent_attested"
        checkpoints = repo.list_artifact_checkpoints()
    finally:
        repo.close()
    checkpoint_files: dict[str, tuple[str, int]] | None = None
    if protected:
        if not checkpoints:
            raise ValueError("protected Guided Fetch run has no signed artifact checkpoint")
        latest_files = checkpoints[-1].get("files")
        if not isinstance(latest_files, list):
            raise ValueError("Guided Fetch signed checkpoint file inventory is unavailable")
        checkpoint_files = {}
        for entry in latest_files:
            if not isinstance(entry, dict):
                raise ValueError("Guided Fetch signed checkpoint entry is malformed")
            path = entry.get("logical_path")
            digest = entry.get("sha256")
            byte_count = entry.get("byte_count")
            if (
                isinstance(path, str) and isinstance(digest, str)
                and type(byte_count) is int
            ):
                checkpoint_files[path] = (digest, byte_count)

    event_paths: dict[str, list[tuple[int, Path, str]]] = {}
    if checkpoint_files is not None:
        prefix = f"{_GUIDED_FETCH_STAGE_ROOT}/"
        for relative, expected in checkpoint_files.items():
            if not relative.startswith(prefix) or not relative.endswith(".json"):
                continue
            parts = PurePosixPath(relative).parts
            if len(parts) != 4:
                continue
            ref_key, filename = parts[2], parts[3]
            match = _GUIDED_FETCH_STAGE_FILENAME.fullmatch(filename)
            if not re.fullmatch(r"[0-9a-f]{64}", ref_key) or match is None:
                continue
            event_paths.setdefault(ref_key, []).append(
                (int(match.group("sequence")), root.joinpath(*parts), relative)
            )
    else:
        for ref_dir in stage_root_io.iterdir():
            if ref_dir.is_symlink() or not ref_dir.is_dir():
                continue
            ref_key = ref_dir.name
            if not re.fullmatch(r"[0-9a-f]{64}", ref_key):
                continue
            for metadata_io in ref_dir.iterdir():
                if metadata_io.suffix.lower() != ".json":
                    continue
                match = _GUIDED_FETCH_STAGE_FILENAME.fullmatch(metadata_io.name)
                if match is None:
                    continue
                relative = (stage_root / ref_key / metadata_io.name).as_posix()
                event_paths.setdefault(ref_key, []).append(
                    (int(match.group("sequence")), stage_root / ref_key / metadata_io.name, relative)
                )

    latest: dict[str, dict[str, Any]] = {}
    issues: dict[str, str] = {}
    identity_skip_intents: list[dict[str, Any]] = []
    for ref_key, rows in event_paths.items():
        max_sequence = max(row[0] for row in rows)
        events: list[tuple[int, dict[str, Any]]] = []
        invalid_events: list[tuple[int, str]] = []
        damaged_latest = False
        for sequence, metadata_path, relative in sorted(rows):
            try:
                metadata_io = Path(io_path(metadata_path))
                if metadata_io.is_symlink() or not metadata_io.is_file():
                    raise ValueError("committed stage event is missing")
                if checkpoint_files is not None:
                    expected = checkpoint_files.get(relative)
                    if (
                        expected is None
                        or _file_sha256(metadata_path) != expected[0]
                        or metadata_io.stat().st_size != expected[1]
                    ):
                        raise ValueError("stage event failed its signed checkpoint hash")
                record = json.loads(metadata_io.read_text(encoding="utf-8"))
                _validate_guided_fetch_stage_record(record, ref_key=ref_key)
                if record["sequence"] != sequence:
                    raise ValueError("stage event sequence does not match its immutable filename")
                events.append((sequence, record))
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                invalid_events.append((sequence, type(exc).__name__))
                if sequence == max_sequence:
                    damaged_latest = True
                    issues[ref_key] = (
                        f"Confirmed Guided Fetch draft is damaged ({type(exc).__name__}). "
                        "Discard it or confirm the source again."
                    )
        if damaged_latest:
            continue

        stage_events = [
            (sequence, record) for sequence, record in events
            if record["action"] in {"stage", "discard"}
        ]
        if not stage_events:
            if invalid_events:
                _, error_type = max(invalid_events, key=lambda row: row[0])
                issues[ref_key] = (
                    f"Confirmed Guided Fetch draft is damaged ({error_type}). "
                    "Discard it or confirm the source again."
                )
            continue
        stage_sequence, stage_record = max(stage_events, key=lambda row: row[0])
        invalid_after_stage = [
            (sequence, error_type)
            for sequence, error_type in invalid_events
            if sequence >= stage_sequence
        ]
        if invalid_after_stage:
            _, error_type = max(invalid_after_stage, key=lambda row: row[0])
            issues[ref_key] = (
                f"Confirmed Guided Fetch draft is damaged ({error_type}). "
                "Discard it or confirm the source again."
            )
            continue
        try:
            if stage_record["action"] == "stage":
                if checkpoint_files is not None:
                    _validate_committed_guided_fetch_stage_files(
                        root, stage_record, checkpoint_files,
                    )
                else:
                    _validate_guided_fetch_stage_files(root, stage_record)
            latest[ref_key] = {
                "record": stage_record,
                "payload": (
                    None if stage_record["action"] == "discard"
                    else _guided_fetch_stage_payload(root, stage_record)
                ),
            }
        except (OSError, ValueError, KeyError) as exc:
            issues[ref_key] = (
                f"Confirmed Guided Fetch draft is damaged ({type(exc).__name__}). "
                "Discard it or confirm the source again."
            )
            continue

        if stage_record["action"] != "stage":
            continue
        matching_decisions = [
            (sequence, record) for sequence, record in events
            if sequence > stage_sequence
            and record["action"] == "identity_skip"
            and record["source_stage_event_id"] == stage_record["event_id"]
            and record["task_id"] == stage_record["task_id"]
            and record["task_kind"] == stage_record["task_kind"]
            and record["task_generation"] == stage_record["task_generation"]
            and record["ref_id"] == stage_record["ref_id"]
            and record["source_tier"] == stage_record["source_tier"]
            and record["source_sha256"] == stage_record["sha256"]
        ]
        if matching_decisions:
            _, decision = max(matching_decisions, key=lambda row: row[0])
            identity_skip_intents.append({
                **decision,
                "source_controlled_path": stage_record["controlled_path"],
            })
    return {
        "latest": latest,
        "issues": issues,
        "identity_skip_intents": identity_skip_intents,
    }


def guided_fetch_identity_skip_intents(
    repository: RunRepository, run_dir: str,
) -> set[tuple[str, str, str, str]]:
    """Return exact staged-source choices backed by current operator Fetch answers."""
    state = load_guided_fetch_stage_artifacts(run_dir)
    tasks = {
        task.task_id: task for task in repository.list_tasks(slot="fetch")
        if task.task_kind in {"fetch", "browser_challenge"}
    }
    source_records_by_ref: dict[str, list[Any]] = {}
    intents: set[tuple[str, str, str, str]] = set()
    for decision in state.get("identity_skip_intents", []):
        task = tasks.get(decision.get("task_id")) if isinstance(decision, dict) else None
        if (
            task is None
            or task.status != "applied"
            or task.generation != decision.get("task_generation")
            or task.task_kind != decision.get("task_kind")
        ):
            continue
        answer = repository.get_latest_task_answer(task.task_id)
        if (
            answer is None
            or answer.accepted_for_processing is not True
            or answer.generation != task.generation
            or answer.actor_type != "user"
        ):
            continue
        provenance = repository.get_task_answer_provenance(answer.answer_id)
        if (
            not isinstance(provenance, dict)
            or provenance.get("producer_class") != "operator"
        ):
            continue
        ref_id = decision["ref_id"]
        answer_source = _guided_fetch_answer_source(
            answer.raw_payload,
            task_kind=task.task_kind,
            ref_id=ref_id,
        )
        if (
            answer_source is None
            or answer_source[0] != decision["source_tier"]
            or not _guided_fetch_answer_matches_stage_path(
                run_dir,
                answer_source[1],
                decision.get("source_controlled_path"),
                answer_id=answer.answer_id,
                expected_sha256=decision["source_sha256"],
            )
        ):
            continue
        if ref_id not in source_records_by_ref:
            source_records_by_ref[ref_id] = repository.list_source_texts(ref_id)
        answer_provenance = f"controlled_task_answer:{answer.answer_id}"
        matching_sources = [source for source in source_records_by_ref[ref_id] if (
            source.ref_id == ref_id
            and source.tier == decision["source_tier"]
            and source.mapping == "operator_attested"
            and source.supplied_by == "user"
            and source.supplied_via == answer_provenance
        )]
        if len(matching_sources) != 1:
            continue
        source_sha256 = matching_sources[0].sha256
        if not isinstance(source_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", source_sha256,
        ):
            continue
        intents.add((answer.answer_id, ref_id, decision["source_tier"], source_sha256))
    return intents


def _guided_fetch_answer_source(
    payload: Any, *, task_kind: str, ref_id: str,
) -> tuple[str, str] | None:
    if not isinstance(payload, dict):
        return None
    if task_kind == "fetch":
        item = payload
    elif task_kind == "browser_challenge":
        items = payload.get("items")
        if not isinstance(items, list):
            return None
        matching = [
            candidate for candidate in items
            if isinstance(candidate, dict) and candidate.get("ref_id") == ref_id
        ]
        if len(matching) != 1:
            return None
        item = matching[0]
    else:
        return None
    tier = item.get("source_tier", "fulltext")
    file_path = item.get("file_path")
    if (
        item.get("found") is not True
        or item.get("guided_fetch") is not True
        or item.get("identity_attested") is not True
        or tier not in {"fulltext", "abstract"}
        or not isinstance(file_path, str)
        or not file_path.strip()
    ):
        return None
    return tier, file_path


def _guided_fetch_answer_matches_stage_path(
    run_dir: str,
    answer_path: str,
    controlled_path: Any,
    *,
    answer_id: str,
    expected_sha256: str,
) -> bool:
    if (
        not isinstance(controlled_path, str) or not controlled_path
        or not isinstance(answer_id, str)
        or not re.fullmatch(r"answer-[0-9a-f]{20}", answer_id)
        or not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        return False
    parts = PurePosixPath(controlled_path).parts
    if (
        PurePosixPath(controlled_path).is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return False
    root = logical_path(run_dir)
    expected = root.joinpath(*parts)
    answer = Path(answer_path)
    expected_io = Path(io_path(expected))
    answer_io = Path(io_path(answer))
    root_io = Path(io_path(root))
    root_abs = Path(os.path.abspath(root_io))
    answer_abs = Path(os.path.abspath(answer_io))
    try:
        relative_answer = answer_abs.relative_to(root_abs)
    except ValueError:
        return False
    inbox_parts = relative_answer.parts
    admitted_copy_path = (
        len(inbox_parts) == 4
        and inbox_parts[0] == ".integrity-inbox"
        and inbox_parts[1] == "task-answers"
        and inbox_parts[2] == answer_id
        and re.fullmatch(r"file-[0-9]{3}\.[a-z0-9]{1,16}", inbox_parts[3])
    )
    if not admitted_copy_path:
        return False
    if answer_io.is_symlink() or not answer_io.is_file():
        return False
    if _file_sha256(answer_io) != expected_sha256:
        return False
    return _file_sha256(expected_io) == expected_sha256


def guided_fetch_identity_skip_answer_id(
    source_record: Any,
    ref_id: str,
    tier: str,
    intents: set[tuple[str, str, str, str]],
) -> str | None:
    """Match a source ledger row to its exact human-admitted skip decision."""
    if (
        source_record is None
        or source_record.ref_id != ref_id
        or source_record.tier != tier
        or source_record.mapping != "operator_attested"
        or source_record.supplied_by != "user"
        or not isinstance(source_record.supplied_via, str)
        or not source_record.supplied_via.startswith("controlled_task_answer:")
    ):
        return None
    answer_id = source_record.supplied_via.removeprefix("controlled_task_answer:")
    return (
        answer_id
        if (answer_id, ref_id, tier, source_record.sha256) in intents
        else None
    )


def _copy_guided_fetch_stage_file(
    source: Path,
    temporary: Path,
    target: Path,
    *,
    expected_sha256: str | None,
    expected_byte_count: int | None,
) -> None:
    source_io = Path(io_path(source))
    temporary_io = Path(io_path(temporary))
    target_io = Path(io_path(target))
    if source_io.is_symlink() or not source_io.is_file():
        raise ValueError("guided Fetch source changed before its controlled copy")
    with source_io.open("rb") as reader, temporary_io.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    if (
        _file_sha256(temporary) != expected_sha256
        or temporary_io.stat().st_size != expected_byte_count
    ):
        raise ValueError("guided Fetch source changed while it was being copied")
    os.replace(temporary_io, target_io)


def _validate_guided_fetch_stage_record(record: Any, *, ref_key: str) -> None:
    if isinstance(record, dict) and record.get("action") == "identity_skip":
        _validate_guided_fetch_identity_skip_record(record, ref_key=ref_key)
        return
    if not isinstance(record, dict) or set(record) != _GUIDED_FETCH_STAGE_FIELDS:
        raise ValueError("guided Fetch stage record has unsupported fields")
    if (
        record.get("schema") != _GUIDED_FETCH_STAGE_SCHEMA
        or record.get("action") not in {"stage", "discard"}
        or type(record.get("sequence")) is not int
        or record["sequence"] < 1
        or not isinstance(record.get("event_id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", record["event_id"])
        or not isinstance(record.get("task_id"), str)
        or not record["task_id"].strip()
        or record.get("task_kind") not in {"fetch", "browser_challenge"}
        or type(record.get("task_generation")) is not int
        or record["task_generation"] < 0
        or not isinstance(record.get("ref_id"), str)
        or hashlib.sha256(record["ref_id"].encode("utf-8")).hexdigest() != ref_key
    ):
        raise ValueError("guided Fetch stage identity is invalid")
    ocr_ids = record.get("supersedes_ocr_scan_ids")
    if (
        not isinstance(ocr_ids, list)
        or ocr_ids != sorted(set(ocr_ids))
        or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in ocr_ids)
    ):
        raise ValueError("guided Fetch OCR supersession list is invalid")
    if record["action"] == "discard":
        if any(record.get(key) is not None for key in (
            "source_tier", "identity_attested", "source_ref", "original_filename",
            "original_path", "controlled_path", "sha256", "byte_count",
        )):
            raise ValueError("guided Fetch discard event must not contain a source")
        return
    if (
        record.get("source_tier") not in {"fulltext", "abstract"}
        or record.get("identity_attested") is not True
        or not isinstance(record.get("original_filename"), str)
        or not record["original_filename"]
        or Path(record["original_filename"]).name != record["original_filename"]
        or not isinstance(record.get("original_path"), str)
        or not record["original_path"].strip()
        or not isinstance(record.get("source_ref"), (str, type(None)))
        or not isinstance(record.get("controlled_path"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256") or ""))
        or type(record.get("byte_count")) is not int
        or record["byte_count"] < 0
    ):
        raise ValueError("guided Fetch source artifact metadata is invalid")


def _validate_guided_fetch_identity_skip_record(
    record: dict[str, Any], *, ref_key: str,
) -> None:
    if not isinstance(record, dict) or set(record) != _GUIDED_FETCH_IDENTITY_SKIP_FIELDS:
        raise ValueError("guided Fetch identity skip event has unsupported fields")
    if (
        record.get("schema") != _GUIDED_FETCH_STAGE_SCHEMA
        or record.get("action") != "identity_skip"
        or type(record.get("sequence")) is not int
        or record["sequence"] < 1
        or not isinstance(record.get("event_id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", record["event_id"])
        or not isinstance(record.get("task_id"), str)
        or not record["task_id"].strip()
        or record.get("task_kind") not in {"fetch", "browser_challenge"}
        or type(record.get("task_generation")) is not int
        or record["task_generation"] < 0
        or not isinstance(record.get("ref_id"), str)
        or hashlib.sha256(record["ref_id"].encode("utf-8")).hexdigest() != ref_key
        or record.get("source_tier") not in {"fulltext", "abstract"}
        or not isinstance(record.get("source_stage_event_id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", record["source_stage_event_id"])
        or not isinstance(record.get("source_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["source_sha256"])
        or record.get("actor_type") != "user"
    ):
        raise ValueError("guided Fetch identity skip event binding is invalid")


def _guided_fetch_stage_payload(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "found": True,
        "guided_fetch": True,
        "identity_attested": record["identity_attested"],
        "source_tier": record["source_tier"],
        "file_path": str(root.joinpath(*PurePosixPath(record["controlled_path"]).parts)),
    }
    if record["source_ref"] is not None:
        payload["url"] = record["source_ref"]
    return payload


def _validate_guided_fetch_stage_files(root: Path, record: dict[str, Any]) -> None:
    _validate_guided_fetch_stage_file(
        root, record["controlled_path"], ref_id=record["ref_id"],
        sequence=record["sequence"], event_id=record["event_id"], suffix="source",
        sha256=record["sha256"], byte_count=record["byte_count"],
    )


def _validate_committed_guided_fetch_stage_files(
    root: Path, record: dict[str, Any], checkpoint_files: dict[str, tuple[str, int]],
) -> None:
    _validate_guided_fetch_stage_files(root, record)
    expected = checkpoint_files.get(record["controlled_path"])
    if expected != (record["sha256"], record["byte_count"]):
        raise ValueError("guided Fetch controlled copy is absent from the signed checkpoint")


def _validate_guided_fetch_stage_file(
    root: Path,
    relative: str,
    *,
    ref_id: str,
    sequence: int,
    event_id: str,
    suffix: str,
    sha256: str,
    byte_count: int,
) -> None:
    if (
        not isinstance(relative, str) or "\\" in relative
        or PurePosixPath(relative).is_absolute()
        or re.match(r"^[A-Za-z]:", relative)
    ):
        raise ValueError("guided Fetch controlled path is invalid")
    parts = PurePosixPath(relative).parts
    ref_key = hashlib.sha256(ref_id.encode("utf-8")).hexdigest()
    if (
        len(parts) != 4 or parts[:3] != ("sources", "guided_fetch_stages", ref_key)
        or not parts[3].startswith(f"{sequence:08d}-{event_id}-{suffix}")
    ):
        raise ValueError("guided Fetch controlled path is outside its event directory")
    path = root.joinpath(*parts)
    path_io = Path(io_path(path))
    if path_io.is_symlink() or not path_io.is_file():
        raise ValueError("guided Fetch controlled source copy is missing")
    resolved = logical_path(path, strict=True)
    resolved.relative_to(root)
    if _file_sha256(resolved) != sha256 or path_io.stat().st_size != byte_count:
        raise ValueError("guided Fetch controlled source copy failed its hash")


def _ensure_guided_ocr_fetch_task(run_dir: str, ref_id: str) -> None:
    """Give a parked scan an answerable Fetch task when retrieval already ended.

    Called inside the same integrity transition as the scan artifact. Existing
    retrieval tasks retain their original identity and answer boundary.
    """
    from core.infra.db.task_storage import (
        _FETCH_REFERENCE_FIELDS, _operational_reference, _source_identity,
    )

    repo = RunRepository.open(run_dir)
    try:
        matches = []
        for task in repo.list_pending_tasks(slot="fetch"):
            view = repo.task_view(task.task_id)
            if not isinstance(view, dict) or view.get("kind") not in {"fetch", "browser_challenge"}:
                continue
            covered = (
                [view.get("ref_id")] if view["kind"] == "fetch" else
                [item.get("ref_id") for item in view.get("references") or []]
            )
            if ref_id in covered:
                matches.append(task.task_id)
        if len(matches) > 1:
            raise ValueError("OCR scan matches multiple pending Fetch tasks")
        if matches:
            return
        task_id = "fetch:guided-ocr:" + hashlib.sha256(ref_id.encode("utf-8")).hexdigest()
        if repo.get_task(task_id) is not None:
            raise ValueError("guided OCR Fetch task has already been answered")
        reference = _operational_reference(repo._conn, ref_id, _FETCH_REFERENCE_FIELDS)
        identity = _source_identity(repo._conn, ref_id)
        repo.create_task(
            task_id=task_id, slot="fetch", ref_id=ref_id,
            task_payload={
                "kind": "fetch", "status": "pending", "ref_id": ref_id,
                "ref_number": reference["ref_number"], "reference": reference,
                "source_identity": identity,
                "instructions": "Review the parked PDF, run OCR, or explicitly waive this source.",
                "answer": None,
            },
        )
    finally:
        repo.close()


def _file_sha256(path: str | os.PathLike[str]) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(io_path(path), "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def admit_source_identity_review(
    run_dir: str,
    task_id: str,
    *,
    action: str,
    target_sha256: str,
    reason: str,
    agent_identity: str | None = None,
    debug_override_artifact_integrity: bool = False,
    debug_override_reason: str | None = None,
) -> dict:
    """Admit one closed human source-identity decision without CLI parsing."""
    action = action.replace("-", "_")
    if action not in {"attest_identity", "keep_unverified"}:
        raise SystemExit("identity attestation action is invalid")
    if agent_identity is not None:
        raise SystemExit("source identity attestation requires the human operator")
    if not isinstance(target_sha256, str) or not target_sha256:
        raise SystemExit("identity attestation requires target_sha256")
    if not isinstance(reason, str) or not reason.strip():
        raise SystemExit("identity attestation requires a reason")
    args = SimpleNamespace(
        run=run_dir,
        agent_identity=agent_identity,
        debug_override_artifact_integrity=debug_override_artifact_integrity,
        debug_override_reason=debug_override_reason,
    )
    resolution = _answer_gate(args)
    gate = getattr(resolution, "gate", resolution)
    repo = _repo(run_dir)
    try:
        task = _require_task(repo, task_id)
        if task.task_kind != "source_identity_attestation" or repo.task_view(task_id) is None:
            raise SystemExit("task is not a source identity attestation")
    finally:
        repo.close()
    payload = {
        "action": action,
        "target_sha256": target_sha256,
        "reason": reason,
    }
    try:
        return (
            gate.admit_task_answer(run_dir, task_id, payload, mirror_checkpoint=False)
            if gate is not None else admit_task_answer_locally(
                run_dir=run_dir,
                task_id=task_id,
                raw_payload=payload,
                assurance=resolution.assurance,
                agent_identity=agent_identity,
            )
        )
    except (AuthorityError, IntegrityGateError) as exc:
        raise SystemExit(f"integrity gate rejected task answer: {exc}") from exc


def _print_task(task: dict) -> None:
    print(f"task_id: {task.get('task_id')}")
    print(f"slot: {task.get('slot')}")
    print(f"db_status: {task.get('db_status')}")
    print(f"kind: {task.get('kind')}")
    if task.get("ref_number") is not None:
        print(f"reference: [{task.get('ref_number')}]")
    if task.get("claim_id"):
        print(f"claim_id: {task.get('claim_id')}")
    if task.get("scope"):
        print(f"scope: {task.get('scope')}")
    if task.get("instructions"):
        print("\ninstructions:")
        print(task["instructions"])
    answer = task.get("answer")
    if answer is not None:
        print("\nlatest_answer:")
        for key, value in answer.items():
            print(f"  {key}: {value}")

    if task.get("kind") == "manual_parse_review":
        for key in (
            "review_kind", "target_sha256", "note_id", "note_number", "raw_note",
            "ref_id", "ref_number", "raw_entry", "title", "doi",
            "occurrence_id", "marker_raw", "candidates",
        ):
            if key in task:
                print(f"{key}: {task[key]}")
    if task.get("kind") == "source_identity_attestation":
        for key in ("ref_id", "ref_number", "source_text_id", "source_tier", "target_sha256", "source_identity", "resolve_identity"):
            if key in task:
                print(f"{key}: {task[key]}")


def cmd_list(args) -> int:
    repo = _repo(args.run)
    try:
        statuses = None
        if args.status:
            statuses = tuple(args.status)
        tasks = repo.list_task_views(slot=args.slot, statuses=statuses)
    finally:
        repo.close()
    if not tasks:
        print("no tasks")
        return 0
    print("task_id\tslot\tdb_status\tkind\tref\tscope\tanswered")
    for task in tasks:
        answered = "yes" if task.get("answer") is not None else "no"
        print(
            f"{task.get('task_id')}\t{task.get('slot')}\t{task.get('db_status')}\t"
            f"{task.get('kind')}\t{task.get('ref_number','')}\t{task.get('scope','')}\t{answered}"
        )
    return 0


def cmd_show(args) -> int:
    repo = _repo(args.run)
    try:
        task = repo.task_view(args.task)
    finally:
        repo.close()
    if task is None:
        raise SystemExit(f"task not found: {args.task}")
    _print_task(task)
    return 0


def cmd_answer_fetch(args) -> int:
    repo = _repo(args.run)
    try:
        _require_task(repo, args.task)
        task = repo.task_view(args.task) or {}
        ocr_text_file = getattr(args, "ocr_text_file", None)
        if ocr_text_file:
            conflicting = (
                args.file_path,
                args.text_file,
                args.url,
                args.not_found,
                args.item_file,
                args.item_text_file,
                args.item_not_found,
            )
            if any(conflicting):
                raise SystemExit(
                    "--ocr-text-file is mutually exclusive with other Fetch answer inputs"
                )
            if task.get("kind") != "fetch":
                raise SystemExit("--ocr-text-file is valid only for an ordinary Fetch task")
            if (
                not isinstance(ocr_text_file, str)
                or not ocr_text_file.lower().endswith(".txt")
                or not os.path.isfile(ocr_text_file)
            ):
                raise SystemExit("--ocr-text-file must name an existing .txt file")
            ref_id = task.get("ref_id")
            if not isinstance(ref_id, str) or not ref_id:
                raise SystemExit("ordinary Fetch task has no reference identity")
            try:
                scan_ref = provided_fulltext.precomputed_ocr_source_ref(
                    args.run, ref_id
                )
            except ValueError as exc:
                raise SystemExit(f"precomputed OCR answer rejected: {exc}") from exc
            payload = {"found": True, "file_path": ocr_text_file, "url": scan_ref}
        elif task.get("kind") == "browser_challenge":
            if (
                getattr(args, "source_tier", "fulltext") == "abstract"
                and (args.item_text_file or [])
            ):
                raise SystemExit("abstract Fetch answers require captured files")
            items = []
            for item in args.item_file or []:
                ref_id, value = item.split("=", 1)
                answer_item = {"ref_id": ref_id, "file_path": value, "found": True}
                if getattr(args, "source_tier", "fulltext") != "fulltext":
                    answer_item["source_tier"] = args.source_tier
                if getattr(args, "guided_fetch", False):
                    answer_item.update(guided_fetch=True, identity_attested=True)
                items.append(answer_item)
            for item in args.item_text_file or []:
                ref_id, value = item.split("=", 1)
                answer_item = {"ref_id": ref_id, "text": _read_text_maybe(value), "found": True}
                if getattr(args, "source_tier", "fulltext") != "fulltext":
                    answer_item["source_tier"] = args.source_tier
                if getattr(args, "guided_fetch", False):
                    answer_item.update(guided_fetch=True, identity_attested=True)
                items.append(answer_item)
            for ref_id in args.item_not_found or []:
                item = {"ref_id": ref_id, "found": False}
                if getattr(args, "guided_fetch", False):
                    item.update(disposition="user_waived", guided_fetch=True)
                items.append(item)
            payload = {"items": items}
        else:
            payload = {}
            if args.url:
                payload["url"] = args.url
            if args.not_found:
                payload["found"] = False
                if getattr(args, "guided_fetch", False):
                    payload.update(disposition="user_waived", guided_fetch=True)
            elif args.file_path:
                payload["found"] = True
                payload["file_path"] = args.file_path
            elif args.text_file:
                if getattr(args, "source_tier", "fulltext") == "abstract":
                    raise SystemExit("abstract Fetch answers require a captured file")
                payload["found"] = True
                payload["text"] = _read_text_maybe(args.text_file)
            else:
                raise SystemExit("fetch answer requires --not-found, --file-path, or --text-file")
            if payload.get("found") is True:
                if getattr(args, "source_tier", "fulltext") != "fulltext":
                    payload["source_tier"] = args.source_tier
                if getattr(args, "guided_fetch", False):
                    payload.update(guided_fetch=True, identity_attested=True)
    finally:
        repo.close()
    admitted = admit_fetch_payload(
        args.run,
        args.task,
        payload,
        agent_identity=getattr(args, "agent_identity", None),
        debug_override_artifact_integrity=getattr(
            args, "debug_override_artifact_integrity", False
        ),
        debug_override_reason=getattr(args, "debug_override_reason", None),
    )
    producer = admitted["provenance"]
    print(
        f"stored answer for {args.task} as "
        f"{producer['producer_class']}:{producer['producer_identity']}"
    )
    return 0


def _bulk_skip_fetch_args(args, task_id: str):
    """Build the ordinary not-found answer accepted by cmd_answer_fetch."""
    return argparse.Namespace(
        run=args.run,
        task=task_id,
        file_path=None,
        text_file=None,
        ocr_text_file=None,
        url=None,
        not_found=True,
        item_file=None,
        item_text_file=None,
        item_not_found=None,
        source_tier="fulltext",
        guided_fetch=False,
        agent_identity=args.agent_identity,
        debug_override_artifact_integrity=args.debug_override_artifact_integrity,
        debug_override_reason=args.debug_override_reason,
    )


def cmd_skip_fetch(args) -> int:
    """Mark every pending ordinary FETCH task as source-text not found.

    This is deliberately a retrieval answer only. It does not modify Resolve's
    bibliographic existence/identity result. Browser-challenge tasks are not
    guessed in bulk and must be handled through their explicit item contract.
    """
    repo = _repo(args.run)
    try:
        pending = sorted(
            (task for task in repo.list_tasks(slot="fetch") if task.status == "pending"),
            key=lambda task: task.task_id,
        )
    finally:
        repo.close()
    if not pending:
        print("no pending FETCH tasks")
        return 0
    ordinary = [task for task in pending if task.task_kind == "fetch"]
    identity = [
        task for task in pending
        if task.task_kind == "source_identity_attestation"
    ]
    unsupported = [
        task.task_id for task in pending
        if task.task_kind not in {"fetch", "source_identity_attestation"}
    ]
    if unsupported:
        raise SystemExit(
            "bulk Fetch skip supports only ordinary fetch tasks; handle these "
            "non-ordinary tasks individually: " + ", ".join(unsupported)
        )
    if not ordinary:
        print("no pending ordinary FETCH tasks to skip")
        print(
            f"{len(identity)} source-identity review(s) remain; answer attest_identity "
            "or keep_unverified before resuming Fetch."
        )
        return 0
    print(f"skipping {len(ordinary)} pending FETCH task(s) as source text not found")
    for task in ordinary:
        cmd_answer_fetch(_bulk_skip_fetch_args(args, task.task_id))
    print(f"skipped {len(ordinary)} pending FETCH task(s)")
    if identity:
        print(
            f"{len(identity)} source-identity review(s) remain; answer attest_identity "
            "or keep_unverified before resuming Fetch."
        )
    print(f"next: {run_command('--run', args.run, '--resume', '--no-fetch')}")
    return 0


def _bulk_guided_proceed_args(args, task: dict):
    """Build one explicit guided waiver without changing not-found semantics."""
    browser_refs = [item["ref_id"] for item in task.get("references") or []]
    return argparse.Namespace(
        run=args.run,
        task=task["task_id"],
        file_path=None,
        text_file=None,
        ocr_text_file=None,
        url=None,
        not_found=task.get("kind") == "fetch",
        item_file=None,
        item_text_file=None,
        item_not_found=browser_refs if task.get("kind") == "browser_challenge" else None,
        source_tier="fulltext",
        guided_fetch=True,
        agent_identity=args.agent_identity,
        debug_override_artifact_integrity=args.debug_override_artifact_integrity,
        debug_override_reason=args.debug_override_reason,
    )


def cmd_guided_proceed(args) -> int:
    """Close every pending Fetch task as user-waived after guided recovery."""
    repo = _repo(args.run)
    try:
        pending = sorted(
            (repo.task_view(task.task_id) or {} for task in repo.list_tasks(slot="fetch")
             if task.status == "pending"),
            key=lambda task: task.get("task_id", ""),
        )
    finally:
        repo.close()
    retrieval = [
        task for task in pending
        if task.get("kind") in {"fetch", "browser_challenge"}
    ]
    identity = [
        task for task in pending
        if task.get("kind") == "source_identity_attestation"
    ]
    unsupported = [
        task.get("task_id", "") for task in pending
        if task.get("kind") not in {
            "fetch", "browser_challenge", "source_identity_attestation",
        }
    ]
    if unsupported:
        raise SystemExit("guided Fetch proceed cannot waive non-retrieval tasks: " + ", ".join(unsupported))
    if not retrieval:
        print("no pending retrieval Fetch tasks to close")
        print(
            f"{len(identity)} source-identity review(s) remain; answer attest_identity "
            "or keep_unverified before resuming Fetch."
        )
        return 0
    for task in retrieval:
        cmd_answer_fetch(_bulk_guided_proceed_args(args, task))
    print(f"closed {len(retrieval)} pending Fetch task(s) as user waived")
    if identity:
        print(
            f"{len(identity)} source-identity review(s) remain; answer attest_identity "
            "or keep_unverified before resuming Fetch."
        )
    return 0


def _identity_skip_args(args, task: dict):
    """Build one explicit keep-unverified answer for an identity task."""
    return argparse.Namespace(
        run=args.run,
        task=task["task_id"],
        target_sha256=task["target_sha256"],
        action="keep-unverified",
        reason=args.reason,
        source_text=None,
        source_text_file=None,
        title=None,
        doi=None,
        ref=None,
        claim=None,
        agent_identity=args.agent_identity,
        debug_override_artifact_integrity=args.debug_override_artifact_integrity,
        debug_override_reason=args.debug_override_reason,
    )


def cmd_skip_identity(args) -> int:
    """Keep every pending source-identity review explicitly unverified."""
    if args.agent_identity is not None:
        raise SystemExit("source identity review skip requires the human operator")
    repo = _repo(args.run)
    try:
        pending = sorted(
            (
                repo.task_view(task.task_id) or {}
                for task in repo.list_tasks(status="pending")
                if task.task_kind == "source_identity_attestation"
            ),
            key=lambda task: task.get("task_id", ""),
        )
    finally:
        repo.close()
    if not pending:
        print("no pending source-identity tasks")
        return 0
    if any(
        not task.get("task_id") or not task.get("target_sha256")
        for task in pending
    ):
        raise SystemExit("pending source-identity task is missing its frozen target")
    for task in pending:
        cmd_answer_review(_identity_skip_args(args, task))
    print(
        f"skipped {len(pending)} pending source-identity task(s); "
        "their sources remain unverified"
    )
    print(f"next: {run_command('--run', args.run, '--resume')}")
    return 0


def cmd_answer_research(args) -> int:
    resolution = _answer_gate(args)
    gate = getattr(resolution, "gate", resolution)
    repo = _repo(args.run)
    try:
        _require_task(repo, args.task)
        if args.not_found:
            payload = {"found": False, "findings": []}
        else:
            findings = []
            for item in args.finding or []:
                url, stance, quote = item.split("|", 2)
                findings.append({
                    "url": url,
                    "stance": stance,
                    "quote": _read_text_maybe(quote),
                })
            payload = {"found": bool(findings), "findings": findings}
    finally:
        repo.close()
    try:
        admitted = (
            gate.admit_task_answer(
                args.run, args.task, payload, mirror_checkpoint=False
            )
            if gate is not None else admit_task_answer_locally(
                run_dir=args.run, task_id=args.task, raw_payload=payload,
                assurance=resolution.assurance,
                agent_identity=args.agent_identity,
            )
        )
    except (AuthorityError, IntegrityGateError) as exc:
        raise SystemExit(f"integrity gate rejected task answer: {exc}") from exc
    producer = admitted["provenance"]
    print(
        f"stored answer for {args.task} as "
        f"{producer['producer_class']}:{producer['producer_identity']}"
    )
    return 0


def cmd_answer_review(args) -> int:
    repo = _repo(args.run)
    try:
        task = _require_task(repo, args.task)
        view = repo.task_view(args.task)
        is_identity = task.task_kind == "source_identity_attestation" and view is not None
        if is_identity:
            if any((
                args.source_text,
                args.source_text_file,
                args.title,
                args.doi,
                getattr(args, "ref", None),
                getattr(args, "claim", None),
            )):
                raise SystemExit("identity attestation does not accept Parse review inputs")
        elif task.task_kind != "manual_parse_review" or view is None:
            raise SystemExit("task is not a supported manual review")
        else:
            action = args.action.replace("-", "_")
            source_inputs = list(args.source_text or [])
            for path in args.source_text_file or []:
                with open(path, encoding="utf-8", errors="strict") as fh:
                    source_inputs.append(fh.read())
            has_identity = args.title is not None or args.doi is not None
            review_kind = view.get("review_kind")
            if review_kind == "citation_reference_review":
                if action not in {"select_reference", "keep_unresolved"} or source_inputs or has_identity or getattr(args, "claim", None):
                    raise SystemExit("citation review accepts select-reference --ref, or keep-unresolved")
                if action == "select_reference" and not getattr(args, "ref", None):
                    raise SystemExit("select-reference requires --ref")
            elif review_kind == "reference_claim_review":
                if action not in {"select_claim", "keep_unresolved"} or source_inputs or has_identity or getattr(args, "ref", None):
                    raise SystemExit("inverse citation review accepts select-claim --claim, or keep-unresolved")
                if action == "select_claim" and not getattr(args, "claim", None):
                    raise SystemExit("select-claim requires --claim")
            elif action == "split_sources":
                if has_identity:
                    raise SystemExit("split-sources does not accept --title or --doi")
                if len(source_inputs) < 2:
                    raise SystemExit("split-sources requires at least two source inputs")
            elif action == "correct_identity":
                if source_inputs:
                    raise SystemExit("correct-identity does not accept source inputs")
                if not has_identity:
                    raise SystemExit("correct-identity requires --title and/or --doi")
            elif source_inputs or has_identity:
                raise SystemExit(f"{args.action} does not accept source/title/doi inputs")
            if review_kind not in {"citation_reference_review", "reference_claim_review"} and (getattr(args, "ref", None) or getattr(args, "claim", None)):
                raise SystemExit("--ref/--claim are valid only for citation attribution reviews")
            if action == "keep_unresolved" and (getattr(args, "ref", None) or getattr(args, "claim", None)):
                raise SystemExit("keep-unresolved does not accept --ref or --claim")
            payload = {
                "action": action,
                "target_sha256": args.target_sha256,
                "reason": args.reason,
            }
            if action == "split_sources":
                payload["source_texts"] = source_inputs
            elif action == "correct_identity":
                payload.update(title=args.title, doi=args.doi)
            elif action == "select_reference":
                payload["ref_id"] = getattr(args, "ref", None)
            elif action == "select_claim":
                payload["claim_id"] = getattr(args, "claim", None)
    finally:
        repo.close()
    if is_identity:
        admitted = admit_source_identity_review(
            args.run,
            args.task,
            action=args.action,
            target_sha256=args.target_sha256,
            reason=args.reason,
            agent_identity=getattr(args, "agent_identity", None),
            debug_override_artifact_integrity=getattr(args, "debug_override_artifact_integrity", False),
            debug_override_reason=getattr(args, "debug_override_reason", None),
        )
        print(f"stored review answer {args.task} {admitted['provenance']['producer_class']}:{admitted['provenance']['producer_identity']}")
        return 0
    resolution = _answer_gate(args)
    gate = getattr(resolution, "gate", resolution)
    try:
        admitted = gate.admit_task_answer(args.run, args.task, payload, mirror_checkpoint=False) if gate is not None else admit_task_answer_locally(run_dir=args.run, task_id=args.task, raw_payload=payload, assurance=resolution.assurance, agent_identity=args.agent_identity)
    except (AuthorityError, IntegrityGateError) as exc:
        raise SystemExit(f"integrity gate rejected task answer: {exc}") from exc
    print(f"stored review answer {args.task} {admitted['provenance']['producer_class']}:{admitted['provenance']['producer_identity']}")
    return 0


def _add_integrity_debug_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent-identity",
        metavar="AGENT_IDENTITY",
        help="stable opaque harness identity (1-64 ASCII characters)",
    )
    parser.add_argument(
        "--debug-override-artifact-integrity",
        action="store_true",
        help="continue diagnostically only after a trusted mismatch override",
    )
    parser.add_argument(
        "--debug-override-reason",
        help="mandatory operator reason for the artifact-integrity debug override",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list")
    p.add_argument("--run", required=True)
    p.add_argument(
        "--slot", choices=["fetch", "research", "verify", "parse_review"]
    )
    p.add_argument("--status", action="append",
                   choices=["pending", "answered", "applied", "cancelled"])
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show")
    p.add_argument("--run", required=True)
    p.add_argument("--task", required=True)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser(
        "skip-fetch",
        help="mark every pending ordinary Fetch task as source text not found",
    )
    p.add_argument("--run", required=True)
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_skip_fetch)

    p = sub.add_parser("answer-fetch")
    p.add_argument("--run", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--file-path")
    p.add_argument("--text-file")
    p.add_argument(
        "--ocr-text-file",
        help="authenticated OCR text for the task's one pending parked PDF",
    )
    p.add_argument("--url")
    p.add_argument("--source-tier", choices=["fulltext", "abstract"], default="fulltext")
    p.add_argument("--guided-fetch", action="store_true",
                   help="record a user-provided source from guided Fetch")
    p.add_argument("--not-found", action="store_true")
    p.add_argument("--item-file", action="append",
                   help="browser challenge item: REF_ID=PATH")
    p.add_argument("--item-text-file", action="append",
                   help="browser challenge item: REF_ID=PATH_TO_TEXT")
    p.add_argument("--item-not-found", action="append",
                   help="browser challenge item: REF_ID")
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_answer_fetch)

    p = sub.add_parser("guided-proceed", help="close pending guided Fetch recovery as user waived")
    p.add_argument("--run", required=True)
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_guided_proceed)

    p = sub.add_parser(
        "skip-identity",
        help="skip pending source-identity reviews and keep their sources unverified",
    )
    p.add_argument("--run", required=True)
    p.add_argument(
        "--reason",
        default="Operator skipped source identity review; source remains unverified.",
    )
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_skip_identity)

    p = sub.add_parser("answer-research")
    p.add_argument("--run", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--finding", action="append",
                   help="URL|STANCE|QUOTE_OR_QUOTE_FILE")
    p.add_argument("--not-found", action="store_true")
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_answer_research)
    p = sub.add_parser("answer-review")
    p.add_argument("--run", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--target-sha256", required=True)
    p.add_argument("--action", required=True, choices=["no-sources", "split-sources", "correct-identity", "keep-ambiguous", "select-reference", "select-claim", "keep-unresolved", "attest-identity", "keep-unverified"])
    p.add_argument("--reason", required=True)
    p.add_argument("--source-text", action="append")
    p.add_argument("--source-text-file", action="append")
    p.add_argument("--title")
    p.add_argument("--doi")
    p.add_argument("--ref")
    p.add_argument("--claim")
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_answer_review)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
