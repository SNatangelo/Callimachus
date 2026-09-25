# core/infra/integrity/authority.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""External, policy-constrained authority for artifact integrity.

The authority owns the signing key and append-only ledger.  It reads run
artifacts itself and never accepts a client-provided manifest.  Production use
requires this process, its ledger, and the protected artifact root to be owned
by an OS identity unavailable to workspace agents.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.infra.db.connection import connect_readonly, db_path
from core.infra.db.schema_bootstrap import require_current_schema_structure
from core.shared.typed_canonical import encode as typed_canonical

from .manifest import (
    ArtifactManifest,
    ManifestDiff,
    build_content_store_manifest,
    build_run_manifest,
)


AUTHORITY_PROTOCOL_VERSION = "callimachus.integrity-authority.v3"
LEDGER_VERSION = "callimachus.integrity-ledger.v2"
SIGNATURE_ALGORITHM = "hmac-sha256"
ZERO_HASH = "0" * 64
CONTENT_STORE_SUBJECT_ID = "callimachus.global-content-store.v1"
CONTENT_STORE_RELATIVE_ID = "content_store"
RECOVERY_SNAPSHOT_VERSION = "callimachus.recovery-snapshot.v1"


class AuthorityError(RuntimeError):
    """The trusted authority rejected or could not verify an operation."""


class AuthorityPermissionError(AuthorityError):
    """The authenticated peer is not allowed to perform the operation."""


class AuthorityIntegrityError(AuthorityError):
    """The external ledger or protected artifact state is invalid."""


@dataclass(frozen=True)
class _RunSnapshot:
    run_id: str
    phase: str
    relative_run_dir: str
    manifest: ArtifactManifest
    local_checkpoint_count: int
    session_id: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AuthorityError(f"{label} must be non-empty NUL-free text")
    return value.strip()


def _json_object_no_duplicates(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise AuthorityIntegrityError(f"duplicate JSON key in authority ledger: {key}")
        output[key] = value
    return output


def _hmac_hex(key: bytes, payload: Any) -> str:
    return hmac.new(key, typed_canonical(payload), hashlib.sha256).hexdigest()


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(typed_canonical(payload)).hexdigest()


def _atomic_append(path: str, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode):
            raise AuthorityIntegrityError("authority ledger is not a regular file")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise AuthorityIntegrityError("authority ledger append was incomplete")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def initialise_authority(authority_root: str, key_file: str) -> None:
    """Create a new protected authority root and 256-bit key, never replacing one."""
    root = os.path.abspath(authority_root)
    key_path = os.path.abspath(key_file)
    os.makedirs(root, mode=0o700, exist_ok=True)
    if os.path.lexists(key_path):
        raise AuthorityError("authority key already exists")
    os.makedirs(os.path.dirname(key_path), mode=0o700, exist_ok=True)
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        key = secrets.token_hex(32).encode("ascii") + b"\n"
        offset = 0
        while offset < len(key):
            written = os.write(descriptor, key[offset:])
            if written <= 0:
                raise AuthorityIntegrityError("authority key write was incomplete")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FileAuthority:
    """Trusted authority core; callers supply a peer role established by IPC."""

    def __init__(
        self,
        *,
        authority_root: str,
        key_file: str,
        protected_root: str,
        authority_id: str,
        content_store_root: str | None = None,
        enforce_permissions: bool = True,
    ) -> None:
        self._authority_root = os.path.abspath(authority_root)
        self._protected_root = os.path.abspath(protected_root)
        self._content_store_root = (
            os.path.abspath(content_store_root) if content_store_root else None
        )
        self.authority_id = _text(authority_id, "authority_id")
        self._enforce_permissions = bool(enforce_permissions)
        self._require_secure_directory(self._authority_root, "authority root")
        self._require_secure_directory(self._protected_root, "protected root")
        if self._content_store_root is not None:
            try:
                common_root = os.path.commonpath(
                    (self._authority_root, self._content_store_root)
                )
            except ValueError:
                common_root = None
            if common_root in {self._authority_root, self._content_store_root}:
                raise AuthorityIntegrityError(
                    "content store root and authority root must not overlap"
                )
            self._require_secure_directory(
                self._content_store_root, "content store root"
            )
        self._key = self._load_key(os.path.abspath(key_file))
        self._ledger_root = os.path.join(self._authority_root, "ledgers")
        os.makedirs(self._ledger_root, mode=0o700, exist_ok=True)
        self._require_secure_directory(self._ledger_root, "ledger root")
        self._object_root = os.path.join(self._authority_root, "objects")
        self._snapshot_root = os.path.join(self._authority_root, "snapshots")
        self._recovery_root = os.path.join(self._authority_root, "recoveries")
        for path, label in (
            (self._object_root, "authority object root"),
            (self._snapshot_root, "authority snapshot root"),
            (self._recovery_root, "authority recovery root"),
        ):
            os.makedirs(path, mode=0o700, exist_ok=True)
            self._require_secure_directory(path, label)

    def _require_secure_directory(self, path: str, label: str) -> None:
        if os.path.islink(path) or not os.path.isdir(path):
            raise AuthorityIntegrityError(f"{label} must be a real directory")
        if self._enforce_permissions and os.name == "posix":
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o022:
                raise AuthorityIntegrityError(
                    f"{label} must not be group- or world-writable"
                )

    def _load_key(self, path: str) -> bytes:
        if os.path.islink(path) or not os.path.isfile(path):
            raise AuthorityIntegrityError("authority key must be a regular file")
        if self._enforce_permissions and os.name == "posix":
            key_stat = os.stat(path)
            if stat.S_IMODE(key_stat.st_mode) & 0o077:
                raise AuthorityIntegrityError("authority key permissions must be 0600")
            if hasattr(os, "geteuid") and key_stat.st_uid != os.geteuid():
                raise AuthorityIntegrityError("authority key must be owned by the service user")
        with open(path, "rb") as handle:
            key = handle.read().strip()
        if len(key) < 32:
            raise AuthorityIntegrityError("authority key must contain at least 256 bits")
        return key

    def _resolve_run(self, run_dir: str) -> tuple[str, str]:
        requested = os.path.abspath(_text(run_dir, "run_dir"))
        if (
            os.path.realpath(requested) != requested
            or os.path.islink(requested)
            or not os.path.isdir(requested)
        ):
            raise AuthorityIntegrityError("run directory must be a real directory")
        try:
            inside = os.path.commonpath((self._protected_root, requested))
        except ValueError as exc:
            raise AuthorityPermissionError("run directory is outside protected root") from exc
        if inside != self._protected_root or requested == self._protected_root:
            raise AuthorityPermissionError("run directory is outside protected root")
        relative = os.path.relpath(requested, self._protected_root).replace(os.sep, "/")
        if relative.startswith("../") or relative in (".", ".."):
            raise AuthorityPermissionError("run directory is outside protected root")
        return requested, relative

    def resolve_run_dir(self, run_dir: str) -> str:
        """Return the canonical protected run path after fail-closed validation."""
        absolute, _relative = self._resolve_run(run_dir)
        return absolute

    def _snapshot(self, run_dir: str) -> _RunSnapshot:
        absolute, relative = self._resolve_run(run_dir)
        connection = connect_readonly(db_path(absolute))
        try:
            require_current_schema_structure(connection)
            row = connection.execute("SELECT run_id, phase FROM run LIMIT 1").fetchone()
            if row is None:
                raise AuthorityIntegrityError("protected run has no run identity")
            checkpoint_count = int(connection.execute(
                "SELECT COUNT(*) FROM artifact_checkpoints"
            ).fetchone()[0])
            session_row = connection.execute(
                "SELECT session_id FROM run_sessions "
                "WHERE status='active' AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            manifest = build_run_manifest(connection, absolute)
            return _RunSnapshot(
                run_id=str(row["run_id"]),
                phase=str(row["phase"]),
                relative_run_dir=relative,
                manifest=manifest,
                local_checkpoint_count=checkpoint_count,
                session_id=None if session_row is None else str(session_row["session_id"]),
            )
        finally:
            connection.close()

    def _ledger_path(self, run_id: str) -> str:
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        return os.path.join(self._ledger_root, f"{digest}.jsonl")

    def _record_signature(
        self,
        *,
        kind: str,
        run_id: str,
        relative_run_dir: str,
        unsigned_record: dict[str, Any],
        children: list[dict[str, Any]],
    ) -> str:
        return _hmac_hex(self._key, {
            "version": AUTHORITY_PROTOCOL_VERSION,
            "kind": kind,
            "run_id": run_id,
            "relative_run_dir": relative_run_dir,
            "record": unsigned_record,
            "children": children,
        })

    def _verify_record_event(self, run_id: str, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind not in {
            "checkpoint",
            "violation",
            "override",
            "crash_recovery",
        }:
            return
        expected_fields = {"kind", "relative_run_dir", "record", "children"}
        if kind == "checkpoint":
            expected_fields.update(
                {"transition_lease_id", "integrity_override", "recovery_snapshot"}
            )
        elif kind == "crash_recovery":
            expected_fields.update(
                {"lease_id", "transition_id", "observed_recovery_snapshot"}
            )
        if set(event) != expected_fields:
            raise AuthorityIntegrityError("authority record event has unsupported fields")
        record = event.get("record")
        children = event.get("children")
        if not isinstance(record, dict) or not isinstance(children, list):
            raise AuthorityIntegrityError("authority record event is malformed")
        if record.get("signature_algorithm") != SIGNATURE_ALGORITHM:
            raise AuthorityIntegrityError("authority record signature algorithm is invalid")
        signature = record.get("signature")
        if not isinstance(signature, str):
            raise AuthorityIntegrityError("authority record signature is missing")
        unsigned = dict(record)
        unsigned.pop("signature", None)
        expected = self._record_signature(
            kind=kind,
            run_id=run_id,
            relative_run_dir=event["relative_run_dir"],
            unsigned_record=unsigned,
            children=children,
        )
        if not hmac.compare_digest(expected, signature):
            raise AuthorityIntegrityError("authority record signature is invalid")
        if kind == "checkpoint":
            self._validate_recovery_snapshot(event["recovery_snapshot"])
            override_event = event["integrity_override"]
            if override_event is not None:
                if not isinstance(override_event, dict):
                    raise AuthorityIntegrityError("authority override bundle is malformed")
                self._verify_record_event(run_id, {
                    "kind": "override",
                    "relative_run_dir": event["relative_run_dir"],
                    **override_event,
                    })
        elif kind == "crash_recovery":
            observed_snapshot = self._validate_recovery_snapshot(
                event["observed_recovery_snapshot"]
            )
            if record.get("observed_recovery_snapshot_sha256") != _hash_payload(
                observed_snapshot
            ):
                raise AuthorityIntegrityError(
                    "crash recovery snapshot identity is invalid"
                )

    def _read_events(self, run_id: str) -> list[dict[str, Any]]:
        path = self._ledger_path(run_id)
        if not os.path.exists(path):
            return []
        if os.path.islink(path) or not os.path.isfile(path):
            raise AuthorityIntegrityError("authority ledger is not a regular file")
        events: list[dict[str, Any]] = []
        previous = ZERO_HASH
        with open(path, encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.endswith("\n"):
                    raise AuthorityIntegrityError("authority ledger has a partial final record")
                try:
                    envelope = json.loads(
                        raw_line,
                        object_pairs_hook=_json_object_no_duplicates,
                    )
                except (json.JSONDecodeError, AuthorityIntegrityError) as exc:
                    raise AuthorityIntegrityError(
                        f"authority ledger record {line_number} is invalid"
                    ) from exc
                expected_fields = {
                    "version", "run_id", "previous_event_sha256", "event",
                    "event_sha256", "ledger_signature",
                }
                if not isinstance(envelope, dict) or set(envelope) != expected_fields:
                    raise AuthorityIntegrityError("authority ledger envelope is malformed")
                if envelope["version"] != LEDGER_VERSION or envelope["run_id"] != run_id:
                    raise AuthorityIntegrityError("authority ledger identity is inconsistent")
                if envelope["previous_event_sha256"] != previous:
                    raise AuthorityIntegrityError("authority ledger chain is broken")
                event_payload = {
                    "version": LEDGER_VERSION,
                    "run_id": run_id,
                    "previous_event_sha256": previous,
                    "event": envelope["event"],
                }
                event_hash = _hash_payload(event_payload)
                if event_hash != envelope["event_sha256"]:
                    raise AuthorityIntegrityError("authority ledger event digest is invalid")
                signed_payload = {**event_payload, "event_sha256": event_hash}
                expected_signature = _hmac_hex(self._key, signed_payload)
                if not hmac.compare_digest(
                    expected_signature, str(envelope["ledger_signature"])
                ):
                    raise AuthorityIntegrityError("authority ledger signature is invalid")
                event = envelope["event"]
                if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
                    raise AuthorityIntegrityError("authority ledger event is malformed")
                self._verify_record_event(run_id, event)
                events.append(event)
                previous = event_hash
        return events

    def _append_event(self, run_id: str, event: dict[str, Any]) -> None:
        events = self._read_events(run_id)
        path = self._ledger_path(run_id)
        previous = ZERO_HASH
        if events:
            with open(path, "rb") as handle:
                last_line = handle.readlines()[-1]
            previous = str(json.loads(last_line)["event_sha256"])
        event_payload = {
            "version": LEDGER_VERSION,
            "run_id": run_id,
            "previous_event_sha256": previous,
            "event": event,
        }
        event_hash = _hash_payload(event_payload)
        envelope = {
            **event_payload,
            "event_sha256": event_hash,
            "ledger_signature": _hmac_hex(
                self._key, {**event_payload, "event_sha256": event_hash}
            ),
        }
        encoded = (
            json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        _atomic_append(path, encoded)

    @staticmethod
    def _record_events(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        output = [event for event in events if event.get("kind") == kind]
        if kind == "override":
            for event in events:
                bundled = event.get("integrity_override")
                if event.get("kind") == "checkpoint" and isinstance(bundled, dict):
                    output.append({
                        "kind": "override",
                        "relative_run_dir": event["relative_run_dir"],
                        **bundled,
                })
        return output

    def _audit_records(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        checkpoints = [
            {**event["record"], "files": event["children"]}
            for event in self._record_events(events, "checkpoint")
        ]
        violations = [
            {**event["record"], "differences": event["children"]}
            for event in self._record_events(events, "violation")
        ]
        overrides = [
            event["record"] for event in self._record_events(events, "override")
        ]
        recoveries = [
            {**event["record"], "entries": event["children"]}
            for event in self._record_events(events, "crash_recovery")
        ]
        violations.sort(key=lambda row: (row["observed_at"], row["violation_id"]))
        overrides.sort(key=lambda row: (row["created_at"], row["override_id"]))
        recoveries.sort(
            key=lambda row: (row["created_at"], row["recovery_id"])
        )
        return {
            "checkpoints": checkpoints,
            "violations": violations,
            "overrides": overrides,
            "recoveries": recoveries,
        }

    def _latest_checkpoint(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        checkpoints = self._record_events(events, "checkpoint")
        if not checkpoints:
            raise AuthorityIntegrityError("authority ledger has no checkpoint")
        return checkpoints[-1]

    def _require_writer(self, caller_role: str) -> None:
        if caller_role != "trusted_writer":
            raise AuthorityPermissionError("operation requires the trusted writer")

    @staticmethod
    def _process_identity(
        authenticated_caller: str,
        authenticated_process: str | None,
    ) -> tuple[str, str]:
        caller = _text(authenticated_caller, "authenticated_caller")
        process = _text(
            authenticated_process or f"caller:{caller}",
            "authenticated_process",
        )
        return caller, process

    def _require_lease_owner(
        self,
        lease: dict[str, Any],
        *,
        authenticated_caller: str,
        authenticated_process: str | None,
    ) -> tuple[str, str]:
        caller, process = self._process_identity(
            authenticated_caller, authenticated_process
        )
        if lease.get("authenticated_caller") != caller:
            raise AuthorityPermissionError(
                "artifact transition belongs to another authenticated caller"
            )
        if lease.get("owner_process") != process:
            raise AuthorityPermissionError(
                "artifact transition belongs to another process instance"
            )
        return caller, process

    def _signed_record(
        self,
        *,
        kind: str,
        snapshot: _RunSnapshot,
        record: dict[str, Any],
        children: list[dict[str, Any]],
    ) -> dict[str, Any]:
        unsigned = {**record, "authority_id": self.authority_id,
                    "signature_algorithm": SIGNATURE_ALGORITHM}
        signature = self._record_signature(
            kind=kind,
            run_id=snapshot.run_id,
            relative_run_dir=snapshot.relative_run_dir,
            unsigned_record=unsigned,
            children=children,
        )
        return {**unsigned, "signature": signature}

    def _signed_subject_record(
        self,
        *,
        kind: str,
        subject_id: str,
        relative_run_dir: str,
        record: dict[str, Any],
        children: list[dict[str, Any]],
    ) -> dict[str, Any]:
        unsigned = {
            **record,
            "authority_id": self.authority_id,
            "signature_algorithm": SIGNATURE_ALGORITHM,
        }
        signature = self._record_signature(
            kind=kind,
            run_id=subject_id,
            relative_run_dir=relative_run_dir,
            unsigned_record=unsigned,
            children=children,
        )
        return {**unsigned, "signature": signature}

    @staticmethod
    def _manifest_files(manifest: ArtifactManifest) -> list[dict[str, Any]]:
        return [entry.payload() for entry in manifest.files]

    @staticmethod
    def _regular_file_digest(path: str) -> tuple[str, int]:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise AuthorityIntegrityError(
                f"protected snapshot source is unavailable: {path}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise AuthorityIntegrityError(
                    f"protected snapshot source is not a regular file: {path}"
                )
            digest = hashlib.sha256()
            byte_count = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                byte_count += len(chunk)
            after = os.fstat(descriptor)
            stable_identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            if not stable_identity or byte_count != after.st_size:
                raise AuthorityIntegrityError(
                    f"protected snapshot source changed while read: {path}"
                )
            return digest.hexdigest(), byte_count
        finally:
            os.close(descriptor)

    def _store_file_object(
        self,
        source_path: str,
        *,
        expected_sha256: str,
        expected_byte_count: int,
    ) -> str:
        if (
            len(expected_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in expected_sha256)
            or expected_byte_count < 0
        ):
            raise AuthorityIntegrityError("manifest file identity is invalid")
        bucket = os.path.join(self._object_root, expected_sha256[:2])
        os.makedirs(bucket, mode=0o700, exist_ok=True)
        self._require_secure_directory(bucket, "authority object bucket")
        target = os.path.join(bucket, expected_sha256)
        if os.path.lexists(target):
            observed_sha256, observed_byte_count = self._regular_file_digest(target)
            if (
                observed_sha256 != expected_sha256
                or observed_byte_count != expected_byte_count
            ):
                raise AuthorityIntegrityError(
                    "protected artifact object differs from its digest identity"
                )
            return os.path.relpath(target, self._authority_root).replace(os.sep, "/")

        temporary = f"{target}.tmp-{uuid.uuid4().hex}"
        source_flags = os.O_RDONLY
        target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
            target_flags |= os.O_NOFOLLOW
        source_descriptor = None
        target_descriptor = None
        try:
            source_descriptor = os.open(source_path, source_flags)
            source_before = os.fstat(source_descriptor)
            if not stat.S_ISREG(source_before.st_mode):
                raise AuthorityIntegrityError(
                    f"artifact is not a regular file: {source_path}"
                )
            target_descriptor = os.open(temporary, target_flags, 0o600)
            digest = hashlib.sha256()
            byte_count = 0
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                byte_count += len(chunk)
                offset = 0
                while offset < len(chunk):
                    written = os.write(target_descriptor, chunk[offset:])
                    if written <= 0:
                        raise AuthorityIntegrityError(
                            "protected artifact object write was incomplete"
                        )
                    offset += written
            source_after = os.fstat(source_descriptor)
            if (
                (source_before.st_dev, source_before.st_ino, source_before.st_size,
                 source_before.st_mtime_ns)
                != (source_after.st_dev, source_after.st_ino, source_after.st_size,
                    source_after.st_mtime_ns)
                or digest.hexdigest() != expected_sha256
                or byte_count != expected_byte_count
            ):
                raise AuthorityIntegrityError(
                    f"artifact changed or differed while protected: {source_path}"
                )
            os.fsync(target_descriptor)
        finally:
            if source_descriptor is not None:
                os.close(source_descriptor)
            if target_descriptor is not None:
                os.close(target_descriptor)
        try:
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return os.path.relpath(target, self._authority_root).replace(os.sep, "/")

    def _store_database_snapshot(
        self,
        database_path: str,
        *,
        subject_id: str,
        checkpoint_id: str,
    ) -> dict[str, Any]:
        subject_digest = hashlib.sha256(subject_id.encode("utf-8")).hexdigest()
        subject_root = os.path.join(self._snapshot_root, subject_digest)
        os.makedirs(subject_root, mode=0o700, exist_ok=True)
        self._require_secure_directory(subject_root, "authority database snapshot root")
        target = os.path.join(subject_root, f"{checkpoint_id}.sqlite")
        temporary = f"{target}.tmp-{uuid.uuid4().hex}"
        source = None
        destination = None
        try:
            source = connect_readonly(database_path)
            destination = sqlite3.connect(temporary)
            source.backup(destination)
            integrity_row = destination.execute("PRAGMA integrity_check").fetchone()
            if integrity_row is None or str(integrity_row[0]).lower() != "ok":
                raise AuthorityIntegrityError(
                    "protected database snapshot failed SQLite integrity_check"
                )
            destination.close()
            destination = None
            source.close()
            source = None
            with open(temporary, "rb") as handle:
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
            for path in (temporary, f"{temporary}-journal", f"{temporary}-wal",
                         f"{temporary}-shm"):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            raise
        sha256, byte_count = self._regular_file_digest(target)
        return {
            "relative_path": os.path.relpath(target, self._authority_root).replace(
                os.sep, "/"
            ),
            "sha256": sha256,
            "byte_count": byte_count,
        }

    def _protect_recovery_snapshot(
        self,
        *,
        subject_id: str,
        artifact_root: str,
        manifest: ArtifactManifest,
        checkpoint_id: str,
    ) -> dict[str, Any]:
        for entry in manifest.files:
            source_path = os.path.join(artifact_root, *entry.logical_path.split("/"))
            self._store_file_object(
                source_path,
                expected_sha256=entry.sha256,
                expected_byte_count=entry.byte_count,
            )
        database = self._store_database_snapshot(
            os.path.join(artifact_root, manifest.database_filename),
            subject_id=subject_id,
            checkpoint_id=checkpoint_id,
        )
        return {
            "version": RECOVERY_SNAPSHOT_VERSION,
            "scope": manifest.scope,
            "manifest_sha256": manifest.manifest_sha256,
            "database": {
                "filename": manifest.database_filename,
                **database,
            },
            "file_object_count": len(manifest.files),
        }

    @staticmethod
    def _validate_recovery_snapshot(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {
            "version",
            "scope",
            "manifest_sha256",
            "database",
            "file_object_count",
        }:
            raise AuthorityIntegrityError("checkpoint recovery snapshot is malformed")
        database = value.get("database")
        if not isinstance(database, dict) or set(database) != {
            "filename",
            "relative_path",
            "sha256",
            "byte_count",
        }:
            raise AuthorityIntegrityError(
                "checkpoint database recovery snapshot is malformed"
            )
        if value.get("version") != RECOVERY_SNAPSHOT_VERSION:
            raise AuthorityIntegrityError("checkpoint recovery snapshot version is invalid")
        for key in ("scope", "manifest_sha256"):
            _text(value.get(key), f"recovery snapshot {key}")
        for key in ("filename", "relative_path", "sha256"):
            _text(database.get(key), f"recovery snapshot database {key}")
        if not isinstance(database.get("byte_count"), int) or database["byte_count"] < 0:
            raise AuthorityIntegrityError(
                "checkpoint database recovery snapshot byte count is invalid"
            )
        if (
            not isinstance(value.get("file_object_count"), int)
            or value["file_object_count"] < 0
        ):
            raise AuthorityIntegrityError(
                "checkpoint recovery snapshot file count is invalid"
            )
        return value

    def _run_checkpoint_event(
        self,
        *,
        snapshot: _RunSnapshot,
        events: list[dict[str, Any]],
        checkpoint_kind: str,
        integrity_state: str,
        transition_lease_id: str | None,
        integrity_override: dict[str, Any] | None,
    ) -> dict[str, Any]:
        record, files = self._new_checkpoint(
            snapshot=snapshot,
            events=events,
            checkpoint_kind=checkpoint_kind,
            integrity_state=integrity_state,
        )
        artifact_root, _relative = self._resolve_run(
            os.path.join(self._protected_root, *snapshot.relative_run_dir.split("/"))
        )
        recovery_snapshot = self._protect_recovery_snapshot(
            subject_id=snapshot.run_id,
            artifact_root=artifact_root,
            manifest=snapshot.manifest,
            checkpoint_id=record["checkpoint_id"],
        )
        confirmed = self._snapshot(artifact_root)
        if (
            confirmed.run_id != snapshot.run_id
            or confirmed.manifest.manifest_sha256
            != snapshot.manifest.manifest_sha256
        ):
            raise AuthorityIntegrityError(
                "run artifacts changed while the protected checkpoint was created"
            )
        return {
            "kind": "checkpoint",
            "relative_run_dir": snapshot.relative_run_dir,
            "transition_lease_id": transition_lease_id,
            "integrity_override": integrity_override,
            "recovery_snapshot": recovery_snapshot,
            "record": record,
            "children": files,
        }

    def _content_checkpoint_event(
        self,
        *,
        manifest: ArtifactManifest,
        events: list[dict[str, Any]],
        checkpoint_kind: str,
        integrity_state: str,
        transition_lease_id: str | None,
        integrity_override: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if self._content_store_root is None:
            raise AuthorityIntegrityError("content store authority is not configured")
        record, files = self._new_content_checkpoint(
            manifest=manifest,
            events=events,
            checkpoint_kind=checkpoint_kind,
            integrity_state=integrity_state,
        )
        recovery_snapshot = self._protect_recovery_snapshot(
            subject_id=CONTENT_STORE_SUBJECT_ID,
            artifact_root=self._content_store_root,
            manifest=manifest,
            checkpoint_id=record["checkpoint_id"],
        )
        confirmed = self._content_manifest()
        if confirmed.manifest_sha256 != manifest.manifest_sha256:
            raise AuthorityIntegrityError(
                "content store changed while the protected checkpoint was created"
            )
        return {
            "kind": "checkpoint",
            "relative_run_dir": CONTENT_STORE_RELATIVE_ID,
            "transition_lease_id": transition_lease_id,
            "integrity_override": integrity_override,
            "recovery_snapshot": recovery_snapshot,
            "record": record,
            "children": files,
        }

    def _new_checkpoint(
        self,
        *,
        snapshot: _RunSnapshot,
        events: list[dict[str, Any]],
        checkpoint_kind: str,
        integrity_state: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        checkpoints = self._record_events(events, "checkpoint")
        previous = checkpoints[-1]["record"] if checkpoints else None
        files = self._manifest_files(snapshot.manifest)
        record = self._signed_record(
            kind="checkpoint",
            snapshot=snapshot,
            record={
                "checkpoint_id": f"checkpoint-{uuid.uuid4().hex}",
                "sequence": 0 if previous is None else int(previous["sequence"]) + 1,
                "phase": snapshot.phase,
                "checkpoint_kind": checkpoint_kind,
                "created_at": _now(),
                "previous_checkpoint_id": (
                    None if previous is None else previous["checkpoint_id"]
                ),
                "manifest_sha256": snapshot.manifest.manifest_sha256,
                "database_projection_sha256": (
                    snapshot.manifest.database_projection_sha256
                ),
                "file_count": len(files),
                "integrity_state": integrity_state,
            },
            children=files,
        )
        return record, files

    def enroll(self, run_dir: str, *, caller_role: str) -> dict[str, Any]:
        self._require_writer(caller_role)
        snapshot = self._snapshot(run_dir)
        events = self._read_events(snapshot.run_id)
        if events or snapshot.local_checkpoint_count:
            raise AuthorityIntegrityError("run is already enrolled or has an orphan mirror")
        checkpoint_event = self._run_checkpoint_event(
            snapshot=snapshot,
            events=[],
            checkpoint_kind="enrolled",
            integrity_state="clean",
            transition_lease_id=None,
            integrity_override=None,
        )
        self._append_event(snapshot.run_id, checkpoint_event)
        return {
            "status": "clean",
            "checkpoint": checkpoint_event["record"],
            "files": checkpoint_event["children"],
        }

    def _difference(
        self,
        checkpoint_event: dict[str, Any],
        observed: ArtifactManifest,
    ) -> ManifestDiff:
        record = checkpoint_event["record"]
        expected_files = {
            entry["logical_path"]: (entry["sha256"], entry["byte_count"])
            for entry in checkpoint_event["children"]
        }
        observed_files = {
            entry.logical_path: (entry.sha256, entry.byte_count)
            for entry in observed.files
        }
        shared = expected_files.keys() & observed_files.keys()
        return ManifestDiff(
            database_changed=(
                record["database_projection_sha256"]
                != observed.database_projection_sha256
            ),
            missing_files=tuple(sorted(expected_files.keys() - observed_files.keys())),
            unexpected_files=tuple(sorted(observed_files.keys() - expected_files.keys())),
            changed_files=tuple(sorted(
                path for path in shared
                if expected_files[path] != observed_files[path]
            )),
        )

    @staticmethod
    def _difference_rows(difference: ManifestDiff) -> list[dict[str, Any]]:
        rows = [
            *(
                {"difference_kind": "changed", "logical_path": path}
                for path in difference.changed_files
            ),
            *(
                {"difference_kind": "missing", "logical_path": path}
                for path in difference.missing_files
            ),
            *(
                {"difference_kind": "unexpected", "logical_path": path}
                for path in difference.unexpected_files
            ),
        ]
        return sorted(rows, key=lambda row: (row["difference_kind"], row["logical_path"]))

    def _protected_snapshot_database(
        self, checkpoint_event: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        recovery = self._validate_recovery_snapshot(
            checkpoint_event.get("recovery_snapshot")
        )
        record = checkpoint_event.get("record")
        children = checkpoint_event.get("children")
        if not isinstance(record, dict) or not isinstance(children, list):
            raise AuthorityIntegrityError("checkpoint recovery bundle is malformed")
        if (
            recovery["manifest_sha256"] != record.get("manifest_sha256")
            or recovery["file_object_count"] != len(children)
        ):
            raise AuthorityIntegrityError(
                "checkpoint recovery snapshot differs from signed manifest"
            )
        relative_path = recovery["database"]["relative_path"]
        if os.path.isabs(relative_path) or ".." in Path(relative_path).parts:
            raise AuthorityIntegrityError(
                "checkpoint database recovery path is outside authority root"
            )
        absolute_path = os.path.abspath(
            os.path.join(self._authority_root, *relative_path.split("/"))
        )
        try:
            common = os.path.commonpath((self._snapshot_root, absolute_path))
        except ValueError as exc:
            raise AuthorityIntegrityError(
                "checkpoint database recovery path is outside snapshot root"
            ) from exc
        if common != self._snapshot_root or os.path.realpath(absolute_path) != absolute_path:
            raise AuthorityIntegrityError(
                "checkpoint database recovery path is outside snapshot root"
            )
        expected = recovery["database"]
        observed_sha256, observed_byte_count = self._regular_file_digest(absolute_path)
        if (
            observed_sha256 != expected["sha256"]
            or observed_byte_count != expected["byte_count"]
        ):
            raise AuthorityIntegrityError(
                "protected database snapshot differs from signed recovery identity"
            )
        return absolute_path, recovery

    def _protected_file_object(self, row: dict[str, Any]) -> str:
        if not isinstance(row, dict) or set(row) != {
            "logical_path",
            "sha256",
            "byte_count",
        }:
            raise AuthorityIntegrityError("checkpoint file entry is malformed")
        sha256 = row["sha256"]
        byte_count = row["byte_count"]
        if not isinstance(sha256, str) or not isinstance(byte_count, int):
            raise AuthorityIntegrityError("checkpoint file identity is malformed")
        path = os.path.join(self._object_root, sha256[:2], sha256)
        observed_sha256, observed_byte_count = self._regular_file_digest(path)
        if observed_sha256 != sha256 or observed_byte_count != byte_count:
            raise AuthorityIntegrityError(
                "protected artifact object differs from signed recovery identity"
            )
        return path

    @staticmethod
    def _require_real_parent(artifact_root: str, target: str) -> None:
        parent = os.path.dirname(target)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        if os.path.realpath(parent) != parent:
            raise AuthorityIntegrityError(
                "artifact restore target has a symlinked parent"
            )
        try:
            common = os.path.commonpath((artifact_root, target))
        except ValueError as exc:
            raise AuthorityIntegrityError(
                "artifact restore target is outside artifact root"
            ) from exc
        if common != artifact_root:
            raise AuthorityIntegrityError(
                "artifact restore target is outside artifact root"
            )

    def _restore_regular_file(
        self,
        source: str,
        target: str,
        *,
        artifact_root: str,
        expected_sha256: str,
        expected_byte_count: int,
    ) -> None:
        self._require_real_parent(artifact_root, target)
        if os.path.lexists(target) and (
            os.path.islink(target) or not os.path.isfile(target)
        ):
            raise AuthorityIntegrityError(
                f"artifact restore target is not a regular file: {target}"
            )
        temporary = f"{target}.restore-{uuid.uuid4().hex}"
        try:
            with open(source, "rb") as source_handle, open(
                temporary, "xb"
            ) as target_handle:
                shutil.copyfileobj(source_handle, target_handle, 1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            observed_sha256, observed_byte_count = self._regular_file_digest(temporary)
            if (
                observed_sha256 != expected_sha256
                or observed_byte_count != expected_byte_count
            ):
                raise AuthorityIntegrityError(
                    "artifact restore copy differs from protected snapshot"
                )
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _restore_checkpoint_artifacts(
        self,
        *,
        artifact_root: str,
        checkpoint_event: dict[str, Any],
        observed_manifest: ArtifactManifest,
    ) -> list[dict[str, Any]]:
        artifact_root = os.path.abspath(artifact_root)
        database_source, recovery = self._protected_snapshot_database(
            checkpoint_event
        )
        expected_rows = checkpoint_event["children"]
        protected_files = {
            row["logical_path"]: self._protected_file_object(row)
            for row in expected_rows
        }
        expected_paths = {row["logical_path"] for row in expected_rows}
        observed_paths = {entry.logical_path for entry in observed_manifest.files}
        actions: list[dict[str, Any]] = []

        for logical_path in sorted(observed_paths - expected_paths):
            target = os.path.join(artifact_root, *logical_path.split("/"))
            if os.path.islink(target) or not os.path.isfile(target):
                raise AuthorityIntegrityError(
                    f"unexpected crash artifact is not a regular file: {logical_path}"
                )
            os.unlink(target)
            actions.append({"action": "removed_unexpected", "logical_path": logical_path})

        database_filename = recovery["database"]["filename"]
        database_target = os.path.join(artifact_root, database_filename)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = f"{database_target}{suffix}"
            if os.path.lexists(sidecar):
                if os.path.islink(sidecar) or not os.path.isfile(sidecar):
                    raise AuthorityIntegrityError(
                        "database recovery sidecar is not a regular file"
                    )
                os.unlink(sidecar)
        self._restore_regular_file(
            database_source,
            database_target,
            artifact_root=artifact_root,
            expected_sha256=recovery["database"]["sha256"],
            expected_byte_count=recovery["database"]["byte_count"],
        )
        actions.append({"action": "restored_database", "logical_path": database_filename})

        expected_by_path = {row["logical_path"]: row for row in expected_rows}
        for logical_path in sorted(expected_paths):
            row = expected_by_path[logical_path]
            target = os.path.join(artifact_root, *logical_path.split("/"))
            self._restore_regular_file(
                protected_files[logical_path],
                target,
                artifact_root=artifact_root,
                expected_sha256=row["sha256"],
                expected_byte_count=row["byte_count"],
            )
            actions.append({"action": "restored_file", "logical_path": logical_path})

        for current, directories, files in os.walk(artifact_root, topdown=False):
            if current == artifact_root or directories or files:
                continue
            try:
                os.rmdir(current)
            except OSError:
                pass
        return actions

    def check(self, run_dir: str, *, authenticated_caller: str) -> dict[str, Any]:
        _text(authenticated_caller, "authenticated_caller")
        snapshot = self._snapshot(run_dir)
        events = self._read_events(snapshot.run_id)
        checkpoint = self._latest_checkpoint(events)
        if checkpoint["relative_run_dir"] != snapshot.relative_run_dir:
            raise AuthorityIntegrityError("run directory differs from enrolled identity")
        difference = self._difference(checkpoint, snapshot.manifest)
        if difference.clean:
            state = checkpoint["record"]["integrity_state"]
            return {
                "status": state,
                "audit_ready": state == "clean",
                "checkpoint": checkpoint["record"],
                "files": checkpoint["children"],
                "audit_records": self._audit_records(events),
            }
        rows = self._difference_rows(difference)
        expected_id = checkpoint["record"]["checkpoint_id"]
        existing = None
        for event in reversed(self._record_events(events, "violation")):
            record = event["record"]
            if (
                record["expected_checkpoint_id"] == expected_id
                and record["observed_manifest_sha256"]
                == snapshot.manifest.manifest_sha256
            ):
                existing = event
                break
        if existing is None:
            violation = self._signed_record(
                kind="violation",
                snapshot=snapshot,
                record={
                    "violation_id": f"violation-{uuid.uuid4().hex}",
                    "observed_at": _now(),
                    "phase": snapshot.phase,
                    "session_id": snapshot.session_id,
                    "authenticated_caller": authenticated_caller,
                    "expected_checkpoint_id": expected_id,
                    "expected_manifest_sha256": checkpoint["record"]["manifest_sha256"],
                    "observed_manifest_sha256": snapshot.manifest.manifest_sha256,
                    "database_changed": difference.database_changed,
                    "difference_count": len(rows),
                },
                children=rows,
            )
            existing = {
                "kind": "violation",
                "relative_run_dir": snapshot.relative_run_dir,
                "record": violation,
                "children": rows,
            }
            self._append_event(snapshot.run_id, existing)
            events.append(existing)
        return {
            "status": "violated",
            "audit_ready": False,
            "checkpoint": checkpoint["record"],
            "violation": existing["record"],
            "differences": existing["children"],
            "audit_records": self._audit_records(events),
        }

    def begin_transition(
        self,
        run_dir: str,
        *,
        checkpoint_kind: str,
        caller_role: str,
        authenticated_caller: str,
        authenticated_process: str | None = None,
        transition_id: str | None = None,
        content_store_required: bool = False,
    ) -> dict[str, Any]:
        self._require_writer(caller_role)
        status = self.check(run_dir, authenticated_caller=authenticated_caller)
        if status["status"] == "violated":
            raise AuthorityIntegrityError("cannot begin a transition from altered artifacts")
        snapshot = self._snapshot(run_dir)
        events = self._read_events(snapshot.run_id)
        open_leases = self._open_transition_ids(events)
        if open_leases:
            raise AuthorityIntegrityError("an artifact transition is already open")
        if checkpoint_kind not in {
            "phase_boundary", "pause", "external_input", "report", "unit"
        }:
            raise AuthorityError("checkpoint kind is invalid for a transition")
        clean_caller = _text(authenticated_caller, "authenticated_caller")
        clean_process = _text(
            authenticated_process or f"caller:{clean_caller}",
            "authenticated_process",
        )
        clean_transition_id = _text(
            transition_id or f"transition-{uuid.uuid4().hex}",
            "transition_id",
        )
        now = _now()
        lease = {
            "kind": "transition_begin",
            "lease_id": f"lease-{uuid.uuid4().hex}",
            "transition_id": clean_transition_id,
            "relative_run_dir": snapshot.relative_run_dir,
            "expected_checkpoint_id": status["checkpoint"]["checkpoint_id"],
            "checkpoint_kind": checkpoint_kind,
            "phase": snapshot.phase,
            "content_store_required": bool(content_store_required),
            "authenticated_caller": clean_caller,
            "owner_process": clean_process,
            "created_at": now,
            "heartbeat_at": now,
        }
        self._append_event(snapshot.run_id, lease)
        return {"lease": lease}

    def commit_transition(
        self,
        run_dir: str,
        *,
        lease_id: str,
        caller_role: str,
        authenticated_caller: str,
        authenticated_process: str | None = None,
    ) -> dict[str, Any]:
        self._require_writer(caller_role)
        clean_lease_id = _text(lease_id, "lease_id")
        snapshot = self._snapshot(run_dir)
        events = self._read_events(snapshot.run_id)
        lease = next((
            event for event in reversed(events)
            if event.get("kind") == "transition_begin"
            and event.get("lease_id") == clean_lease_id
        ), None)
        if lease is None:
            raise AuthorityIntegrityError("artifact transition lease is unavailable")
        self._require_lease_owner(
            lease,
            authenticated_caller=authenticated_caller,
            authenticated_process=authenticated_process,
        )
        if clean_lease_id not in self._open_transition_ids(events):
            raise AuthorityIntegrityError("artifact transition lease is already closed")
        latest = self._latest_checkpoint(events)
        if latest["record"]["checkpoint_id"] != lease["expected_checkpoint_id"]:
            raise AuthorityIntegrityError("artifact transition lease is stale")
        state = latest["record"]["integrity_state"]
        checkpoint_event = self._run_checkpoint_event(
            snapshot=snapshot,
            events=events,
            checkpoint_kind=lease["checkpoint_kind"],
            integrity_state=state,
            transition_lease_id=clean_lease_id,
            integrity_override=None,
        )
        self._append_event(snapshot.run_id, checkpoint_event)
        return {
            "status": state,
            "checkpoint": checkpoint_event["record"],
            "files": checkpoint_event["children"],
        }

    def abort_transition(
        self,
        run_dir: str,
        *,
        lease_id: str,
        reason: str,
        caller_role: str,
        authenticated_caller: str,
        authenticated_process: str | None = None,
    ) -> dict[str, Any]:
        self._require_writer(caller_role)
        clean_lease_id = _text(lease_id, "lease_id")
        snapshot = self._snapshot(run_dir)
        events = self._read_events(snapshot.run_id)
        lease = next(
            (
                event
                for event in reversed(events)
                if event.get("kind") == "transition_begin"
                and event.get("lease_id") == clean_lease_id
            ),
            None,
        )
        if lease is None:
            raise AuthorityIntegrityError("artifact transition lease is unavailable")
        clean_caller, _clean_process = self._require_lease_owner(
            lease,
            authenticated_caller=authenticated_caller,
            authenticated_process=authenticated_process,
        )
        if clean_lease_id not in self._open_transition_ids(events):
            raise AuthorityIntegrityError("artifact transition lease is already closed")
        event = {
            "kind": "transition_abort",
            "lease_id": clean_lease_id,
            "relative_run_dir": snapshot.relative_run_dir,
            "reason": _text(reason, "transition abort reason"),
            "authenticated_caller": clean_caller,
            "created_at": _now(),
        }
        self._append_event(snapshot.run_id, event)
        return {"status": "aborted", "lease_id": clean_lease_id}

    def heartbeat_transition(
        self,
        run_dir: str,
        *,
        lease_id: str,
        caller_role: str,
        authenticated_caller: str,
        authenticated_process: str | None = None,
    ) -> dict[str, Any]:
        self._require_writer(caller_role)
        clean_lease_id = _text(lease_id, "lease_id")
        snapshot = self._snapshot(run_dir)
        events = self._read_events(snapshot.run_id)
        if clean_lease_id not in self._open_transition_ids(events):
            raise AuthorityIntegrityError(
                "artifact transition lease is unavailable or closed"
            )
        lease = next(
            event
            for event in reversed(events)
            if event.get("kind") == "transition_begin"
            and event.get("lease_id") == clean_lease_id
        )
        caller, process = self._require_lease_owner(
            lease,
            authenticated_caller=authenticated_caller,
            authenticated_process=authenticated_process,
        )
        heartbeat_at = _now()
        self._append_event(snapshot.run_id, {
            "kind": "transition_heartbeat",
            "lease_id": clean_lease_id,
            "transition_id": lease["transition_id"],
            "relative_run_dir": snapshot.relative_run_dir,
            "authenticated_caller": caller,
            "owner_process": process,
            "heartbeat_at": heartbeat_at,
        })
        return {
            "status": "active",
            "lease_id": clean_lease_id,
            "heartbeat_at": heartbeat_at,
        }

    @classmethod
    def _open_transition_leases(
        cls, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        open_ids = cls._open_transition_ids(events)
        return [
            event
            for event in events
            if event.get("kind") == "transition_begin"
            and event.get("lease_id") in open_ids
        ]

    def _recover_subject_transition(
        self,
        *,
        subject_id: str,
        relative_run_dir: str,
        artifact_root: str,
        observed_manifest: ArtifactManifest,
        events: list[dict[str, Any]],
        lease: dict[str, Any],
        authenticated_caller: str,
        authenticated_process: str,
        verify_manifest,
    ) -> dict[str, Any]:
        if lease.get("authenticated_caller") != authenticated_caller:
            raise AuthorityPermissionError(
                "interrupted transition belongs to another authenticated caller"
            )
        if lease.get("owner_process") == authenticated_process:
            raise AuthorityIntegrityError(
                "artifact transition is still owned by the current process instance"
            )
        latest = self._latest_checkpoint(events)
        if latest["record"]["checkpoint_id"] != lease.get("expected_checkpoint_id"):
            raise AuthorityIntegrityError(
                "interrupted transition does not start at the latest checkpoint"
            )
        difference = self._difference(latest, observed_manifest)
        rows = self._difference_rows(difference)
        recovery_id = f"recovery-{uuid.uuid4().hex}"
        observed_recovery_snapshot = latest["recovery_snapshot"]
        actions: list[dict[str, Any]] = []
        if not difference.clean:
            observed_recovery_snapshot = self._protect_recovery_snapshot(
                subject_id=subject_id,
                artifact_root=artifact_root,
                manifest=observed_manifest,
                checkpoint_id=f"{recovery_id}-observed",
            )
            actions = self._restore_checkpoint_artifacts(
                artifact_root=artifact_root,
                checkpoint_event=latest,
                observed_manifest=observed_manifest,
            )
        restored_manifest = verify_manifest()
        if (
            restored_manifest.manifest_sha256
            != latest["record"]["manifest_sha256"]
        ):
            raise AuthorityIntegrityError(
                "crash recovery did not restore the signed artifact checkpoint"
            )
        entries = [
            {"entry_kind": "difference", **row} for row in rows
        ] + [
            {"entry_kind": "action", **row} for row in actions
        ]
        record = self._signed_subject_record(
            kind="crash_recovery",
            subject_id=subject_id,
            relative_run_dir=relative_run_dir,
            record={
                "recovery_id": recovery_id,
                "subject_scope": observed_manifest.scope,
                "lease_id": lease["lease_id"],
                "transition_id": lease["transition_id"],
                "expected_checkpoint_id": latest["record"]["checkpoint_id"],
                "authenticated_caller": authenticated_caller,
                "previous_owner_process": lease["owner_process"],
                "recovered_by_process": authenticated_process,
                "last_heartbeat_at": self._latest_lease_heartbeat(events, lease),
                "observed_manifest_sha256": observed_manifest.manifest_sha256,
                "restored_manifest_sha256": restored_manifest.manifest_sha256,
                "database_changed": difference.database_changed,
                "difference_count": len(rows),
                "action_count": len(actions),
                "observed_recovery_snapshot_sha256": _hash_payload(
                    observed_recovery_snapshot
                ),
                "created_at": _now(),
            },
            children=entries,
        )
        event = {
            "kind": "crash_recovery",
            "lease_id": lease["lease_id"],
            "transition_id": lease["transition_id"],
            "relative_run_dir": relative_run_dir,
            "observed_recovery_snapshot": observed_recovery_snapshot,
            "record": record,
            "children": entries,
        }
        self._append_event(subject_id, event)
        return event

    @staticmethod
    def _latest_lease_heartbeat(
        events: list[dict[str, Any]], lease: dict[str, Any]
    ) -> str:
        heartbeat = lease.get("heartbeat_at") or lease.get("created_at")
        for event in events:
            if (
                event.get("kind") == "transition_heartbeat"
                and event.get("lease_id") == lease.get("lease_id")
            ):
                heartbeat = event.get("heartbeat_at")
        return _text(heartbeat, "transition heartbeat")

    def recover_pipeline_transition(
        self,
        run_dir: str,
        *,
        caller_role: str,
        authenticated_caller: str,
        authenticated_process: str | None = None,
    ) -> dict[str, Any]:
        self._require_writer(caller_role)
        caller, process = self._process_identity(
            authenticated_caller, authenticated_process
        )
        run_snapshot = self._snapshot(run_dir)
        run_events = self._read_events(run_snapshot.run_id)
        run_leases = self._open_transition_leases(run_events)
        if len(run_leases) > 1:
            raise AuthorityIntegrityError("run has multiple open artifact transitions")

        content_events: list[dict[str, Any]] = []
        content_leases: list[dict[str, Any]] = []
        if self._content_store_root is not None:
            content_events = self._read_events(CONTENT_STORE_SUBJECT_ID)
            content_leases = [
                lease
                for lease in self._open_transition_leases(content_events)
                if lease.get("owner_run_id") == run_snapshot.run_id
            ]
            if len(content_leases) > 1:
                raise AuthorityIntegrityError(
                    "run has multiple open content store transitions"
                )

        if not run_leases and not content_leases:
            return {"status": "none", "recoveries": []}

        run_lease = run_leases[0] if run_leases else None
        content_lease = content_leases[0] if content_leases else None
        if run_lease is not None and run_lease.get("content_store_required"):
            if content_lease is None:
                already_recovered = any(
                    event.get("kind") == "crash_recovery"
                    and event.get("transition_id") == run_lease.get("transition_id")
                    for event in content_events
                )
                if not already_recovered:
                    raise AuthorityIntegrityError(
                        "paired content store transition is missing"
                    )
            elif content_lease.get("transition_id") != run_lease.get("transition_id"):
                raise AuthorityIntegrityError(
                    "run and content store transitions have different identities"
                )
        elif run_lease is not None and content_lease is not None:
            raise AuthorityIntegrityError(
                "read-only run transition unexpectedly owns content store mutation"
            )

        recoveries: list[dict[str, Any]] = []
        if content_lease is not None:
            content_manifest = self._content_manifest()
            recoveries.append(self._recover_subject_transition(
                subject_id=CONTENT_STORE_SUBJECT_ID,
                relative_run_dir=CONTENT_STORE_RELATIVE_ID,
                artifact_root=self._content_store_root,
                observed_manifest=content_manifest,
                events=content_events,
                lease=content_lease,
                authenticated_caller=caller,
                authenticated_process=process,
                verify_manifest=self._content_manifest,
            ))
        if run_lease is not None:
            recoveries.append(self._recover_subject_transition(
                subject_id=run_snapshot.run_id,
                relative_run_dir=run_snapshot.relative_run_dir,
                artifact_root=os.path.abspath(run_dir),
                observed_manifest=run_snapshot.manifest,
                events=run_events,
                lease=run_lease,
                authenticated_caller=caller,
                authenticated_process=process,
                verify_manifest=lambda: self._snapshot(run_dir).manifest,
            ))
        return {"status": "recovered", "recoveries": recoveries}

    def override(
        self,
        run_dir: str,
        *,
        reason: str,
        authenticated_caller: str,
    ) -> dict[str, Any]:
        clean_reason = _text(reason, "override reason")
        clean_caller = _text(authenticated_caller, "authenticated_caller")
        checked = self.check(run_dir, authenticated_caller=clean_caller)
        if checked["status"] != "violated":
            raise AuthorityIntegrityError("integrity override requires an observed mismatch")
        snapshot = self._snapshot(run_dir)
        events = self._read_events(snapshot.run_id)
        open_leases = {
            event["lease_id"] for event in events if event.get("kind") == "transition_begin"
        } - {
            event["transition_lease_id"]
            for event in events
            if event.get("kind") == "checkpoint"
            and event.get("transition_lease_id") is not None
        }
        open_leases -= {
            event["lease_id"]
            for event in events
            if event.get("kind") == "transition_abort"
        }
        for open_lease_id in sorted(open_leases):
            self._append_event(snapshot.run_id, {
                "kind": "transition_abort",
                "lease_id": open_lease_id,
                "relative_run_dir": snapshot.relative_run_dir,
                "reason": "closed by recorded artifact integrity override",
                "authenticated_caller": clean_caller,
                "created_at": _now(),
            })
        if open_leases:
            events = self._read_events(snapshot.run_id)
        checkpoint, files = self._new_checkpoint(
            snapshot=snapshot,
            events=events,
            checkpoint_kind="integrity_override",
            integrity_state="debug_overridden",
        )
        override = self._signed_record(
            kind="override",
            snapshot=snapshot,
            record={
                "override_id": f"override-{uuid.uuid4().hex}",
                "violation_id": checked["violation"]["violation_id"],
                "resulting_checkpoint_id": checkpoint["checkpoint_id"],
                "reason": clean_reason,
                "authenticated_caller": clean_caller,
                "created_at": _now(),
            },
            children=[],
        )
        artifact_root, _relative = self._resolve_run(run_dir)
        recovery_snapshot = self._protect_recovery_snapshot(
            subject_id=snapshot.run_id,
            artifact_root=artifact_root,
            manifest=snapshot.manifest,
            checkpoint_id=checkpoint["checkpoint_id"],
        )
        confirmed = self._snapshot(run_dir)
        if confirmed.manifest.manifest_sha256 != snapshot.manifest.manifest_sha256:
            raise AuthorityIntegrityError(
                "run artifacts changed while the protected checkpoint was created"
            )
        self._append_event(snapshot.run_id, {
            "kind": "checkpoint",
            "relative_run_dir": snapshot.relative_run_dir,
            "transition_lease_id": None,
            "integrity_override": {"record": override, "children": []},
            "recovery_snapshot": recovery_snapshot,
            "record": checkpoint,
            "children": files,
        })
        return {
            "status": "debug_overridden",
            "audit_ready": False,
            "violation": checked["violation"],
            "differences": checked["differences"],
            "checkpoint": checkpoint,
            "files": files,
            "override": override,
        }

    def _content_manifest(self) -> ArtifactManifest:
        if self._content_store_root is None:
            raise AuthorityIntegrityError("content store root is not configured")
        database_path = os.path.join(self._content_store_root, "content.sqlite")
        if os.path.islink(database_path) or not os.path.isfile(database_path):
            raise AuthorityIntegrityError(
                "content store database must be a regular non-symlink file"
            )
        uri = f"{Path(database_path).as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            return build_content_store_manifest(
                connection, self._content_store_root
            )
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            raise AuthorityIntegrityError(
                "content store manifest could not be built"
            ) from exc
        finally:
            if "connection" in locals():
                connection.close()

    def _content_signed_record(
        self,
        *,
        kind: str,
        record: dict[str, Any],
        children: list[dict[str, Any]],
    ) -> dict[str, Any]:
        unsigned = {
            **record,
            "authority_id": self.authority_id,
            "signature_algorithm": SIGNATURE_ALGORITHM,
        }
        signature = self._record_signature(
            kind=kind,
            run_id=CONTENT_STORE_SUBJECT_ID,
            relative_run_dir=CONTENT_STORE_RELATIVE_ID,
            unsigned_record=unsigned,
            children=children,
        )
        return {**unsigned, "signature": signature}

    def _new_content_checkpoint(
        self,
        *,
        manifest: ArtifactManifest,
        events: list[dict[str, Any]],
        checkpoint_kind: str,
        integrity_state: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        checkpoints = self._record_events(events, "checkpoint")
        previous = checkpoints[-1]["record"] if checkpoints else None
        files = self._manifest_files(manifest)
        record = self._content_signed_record(
            kind="checkpoint",
            record={
                "checkpoint_id": f"content-checkpoint-{uuid.uuid4().hex}",
                "sequence": 0 if previous is None else int(previous["sequence"]) + 1,
                "checkpoint_kind": checkpoint_kind,
                "created_at": _now(),
                "previous_checkpoint_id": (
                    None if previous is None else previous["checkpoint_id"]
                ),
                "manifest_sha256": manifest.manifest_sha256,
                "database_projection_sha256": (
                    manifest.database_projection_sha256
                ),
                "file_count": len(files),
                "integrity_state": integrity_state,
            },
            children=files,
        )
        return record, files

    @staticmethod
    def _open_transition_ids(events: list[dict[str, Any]]) -> set[str]:
        opened = {
            event["lease_id"]
            for event in events
            if event.get("kind") == "transition_begin"
        }
        closed = {
            event["transition_lease_id"]
            for event in events
            if event.get("kind") == "checkpoint"
            and event.get("transition_lease_id") is not None
        } | {
            event["lease_id"]
            for event in events
            if event.get("kind") == "transition_abort"
        } | {
            event["lease_id"]
            for event in events
            if event.get("kind") == "crash_recovery"
        }
        return opened - closed

    def enroll_content_store(self, *, caller_role: str) -> dict[str, Any]:
        self._require_writer(caller_role)
        manifest = self._content_manifest()
        events = self._read_events(CONTENT_STORE_SUBJECT_ID)
        if events:
            raise AuthorityIntegrityError("content store is already enrolled")
        checkpoint_event = self._content_checkpoint_event(
            manifest=manifest,
            events=[],
            checkpoint_kind="enrolled",
            integrity_state="clean",
            transition_lease_id=None,
            integrity_override=None,
        )
        self._append_event(CONTENT_STORE_SUBJECT_ID, checkpoint_event)
        return {
            "status": "clean",
            "audit_ready": True,
            "checkpoint": checkpoint_event["record"],
            "files": checkpoint_event["children"],
        }

    def check_content_store(self, *, authenticated_caller: str) -> dict[str, Any]:
        caller = _text(authenticated_caller, "authenticated_caller")
        manifest = self._content_manifest()
        events = self._read_events(CONTENT_STORE_SUBJECT_ID)
        checkpoint = self._latest_checkpoint(events)
        if checkpoint["relative_run_dir"] != CONTENT_STORE_RELATIVE_ID:
            raise AuthorityIntegrityError(
                "content store ledger has an invalid subject identity"
            )
        difference = self._difference(checkpoint, manifest)
        if difference.clean:
            state = checkpoint["record"]["integrity_state"]
            return {
                "status": state,
                "audit_ready": state == "clean",
                "checkpoint": checkpoint["record"],
                "files": checkpoint["children"],
                "audit_records": self._audit_records(events),
            }
        rows = self._difference_rows(difference)
        expected_id = checkpoint["record"]["checkpoint_id"]
        existing = next(
            (
                event
                for event in reversed(self._record_events(events, "violation"))
                if event["record"]["expected_checkpoint_id"] == expected_id
                and event["record"]["observed_manifest_sha256"]
                == manifest.manifest_sha256
            ),
            None,
        )
        if existing is None:
            violation = self._content_signed_record(
                kind="violation",
                record={
                    "violation_id": f"content-violation-{uuid.uuid4().hex}",
                    "observed_at": _now(),
                    "authenticated_caller": caller,
                    "expected_checkpoint_id": expected_id,
                    "expected_manifest_sha256": checkpoint["record"][
                        "manifest_sha256"
                    ],
                    "observed_manifest_sha256": manifest.manifest_sha256,
                    "database_changed": difference.database_changed,
                    "difference_count": len(rows),
                },
                children=rows,
            )
            existing = {
                "kind": "violation",
                "relative_run_dir": CONTENT_STORE_RELATIVE_ID,
                "record": violation,
                "children": rows,
            }
            self._append_event(CONTENT_STORE_SUBJECT_ID, existing)
            events.append(existing)
        return {
            "status": "violated",
            "audit_ready": False,
            "checkpoint": checkpoint["record"],
            "violation": existing["record"],
            "differences": existing["children"],
            "audit_records": self._audit_records(events),
        }

    def begin_content_store_transition(
        self,
        run_dir: str,
        *,
        transition_id: str,
        caller_role: str,
        authenticated_caller: str,
        authenticated_process: str | None = None,
    ) -> dict[str, Any]:
        self._require_writer(caller_role)
        run_snapshot = self._snapshot(run_dir)
        checked = self.check_content_store(
            authenticated_caller=authenticated_caller
        )
        if checked["status"] == "violated":
            raise AuthorityIntegrityError(
                "cannot begin content store transition with altered artifacts"
            )
        events = self._read_events(CONTENT_STORE_SUBJECT_ID)
        if self._open_transition_ids(events):
            raise AuthorityIntegrityError(
                "a content store integrity transition is already open"
            )
        clean_caller, clean_process = self._process_identity(
            authenticated_caller, authenticated_process
        )
        now = _now()
        lease = {
            "kind": "transition_begin",
            "lease_id": f"content-lease-{uuid.uuid4().hex}",
            "transition_id": _text(transition_id, "transition_id"),
            "relative_run_dir": CONTENT_STORE_RELATIVE_ID,
            "owner_run_id": run_snapshot.run_id,
            "owner_relative_run_dir": run_snapshot.relative_run_dir,
            "expected_checkpoint_id": checked["checkpoint"]["checkpoint_id"],
            "checkpoint_kind": "content_store_transition",
            "authenticated_caller": clean_caller,
            "owner_process": clean_process,
            "created_at": now,
            "heartbeat_at": now,
        }
        self._append_event(CONTENT_STORE_SUBJECT_ID, lease)
        return {"lease": lease}

    def require_content_store_transition_run(
        self, run_dir: str, *, lease_id: str
    ) -> None:
        """Fail unless the open content-store lease belongs to this run path."""
        _absolute, relative = self._resolve_run(run_dir)
        clean_lease_id = _text(lease_id, "lease_id")
        events = self._read_events(CONTENT_STORE_SUBJECT_ID)
        lease = next(
            (
                event
                for event in reversed(events)
                if event.get("kind") == "transition_begin"
                and event.get("lease_id") == clean_lease_id
            ),
            None,
        )
        if lease is None or clean_lease_id not in self._open_transition_ids(events):
            raise AuthorityIntegrityError(
                "content store transition lease is unavailable or closed"
            )
        if lease.get("owner_relative_run_dir") != relative:
            raise AuthorityPermissionError(
                "content store transition lease belongs to a different run"
            )

    def commit_content_store_transition(
        self,
        *,
        lease_id: str,
        caller_role: str,
        authenticated_caller: str,
        authenticated_process: str | None = None,
    ) -> dict[str, Any]:
        self._require_writer(caller_role)
        clean_lease_id = _text(lease_id, "lease_id")
        manifest = self._content_manifest()
        events = self._read_events(CONTENT_STORE_SUBJECT_ID)
        lease = next(
            (
                event
                for event in reversed(events)
                if event.get("kind") == "transition_begin"
                and event.get("lease_id") == clean_lease_id
            ),
            None,
        )
        if lease is None or clean_lease_id not in self._open_transition_ids(events):
            raise AuthorityIntegrityError(
                "content store transition lease is unavailable or closed"
            )
        self._require_lease_owner(
            lease,
            authenticated_caller=authenticated_caller,
            authenticated_process=authenticated_process,
        )
        latest = self._latest_checkpoint(events)
        if latest["record"]["checkpoint_id"] != lease["expected_checkpoint_id"]:
            raise AuthorityIntegrityError("content store transition lease is stale")
        state = latest["record"]["integrity_state"]
        checkpoint_event = self._content_checkpoint_event(
            manifest=manifest,
            events=events,
            checkpoint_kind="content_store_transition",
            integrity_state=state,
            transition_lease_id=clean_lease_id,
            integrity_override=None,
        )
        self._append_event(CONTENT_STORE_SUBJECT_ID, checkpoint_event)
        return {
            "status": state,
            "checkpoint": checkpoint_event["record"],
            "files": checkpoint_event["children"],
        }

    def abort_content_store_transition(
        self,
        *,
        lease_id: str,
        reason: str,
        caller_role: str,
        authenticated_caller: str,
        authenticated_process: str | None = None,
    ) -> dict[str, Any]:
        self._require_writer(caller_role)
        clean_lease_id = _text(lease_id, "lease_id")
        events = self._read_events(CONTENT_STORE_SUBJECT_ID)
        if clean_lease_id not in self._open_transition_ids(events):
            raise AuthorityIntegrityError(
                "content store transition lease is unavailable or closed"
            )
        lease = next(
            event
            for event in reversed(events)
            if event.get("kind") == "transition_begin"
            and event.get("lease_id") == clean_lease_id
        )
        clean_caller, _clean_process = self._require_lease_owner(
            lease,
            authenticated_caller=authenticated_caller,
            authenticated_process=authenticated_process,
        )
        event = {
            "kind": "transition_abort",
            "lease_id": clean_lease_id,
            "relative_run_dir": CONTENT_STORE_RELATIVE_ID,
            "reason": _text(reason, "transition abort reason"),
            "authenticated_caller": clean_caller,
            "created_at": _now(),
        }
        self._append_event(CONTENT_STORE_SUBJECT_ID, event)
        return {"status": "aborted", "lease_id": clean_lease_id}

    def heartbeat_content_store_transition(
        self,
        *,
        lease_id: str,
        caller_role: str,
        authenticated_caller: str,
        authenticated_process: str | None = None,
    ) -> dict[str, Any]:
        self._require_writer(caller_role)
        clean_lease_id = _text(lease_id, "lease_id")
        events = self._read_events(CONTENT_STORE_SUBJECT_ID)
        if clean_lease_id not in self._open_transition_ids(events):
            raise AuthorityIntegrityError(
                "content store transition lease is unavailable or closed"
            )
        lease = next(
            event
            for event in reversed(events)
            if event.get("kind") == "transition_begin"
            and event.get("lease_id") == clean_lease_id
        )
        caller, process = self._require_lease_owner(
            lease,
            authenticated_caller=authenticated_caller,
            authenticated_process=authenticated_process,
        )
        heartbeat_at = _now()
        self._append_event(CONTENT_STORE_SUBJECT_ID, {
            "kind": "transition_heartbeat",
            "lease_id": clean_lease_id,
            "transition_id": lease["transition_id"],
            "relative_run_dir": CONTENT_STORE_RELATIVE_ID,
            "authenticated_caller": caller,
            "owner_process": process,
            "heartbeat_at": heartbeat_at,
        })
        return {
            "status": "active",
            "lease_id": clean_lease_id,
            "heartbeat_at": heartbeat_at,
        }

    def override_content_store(
        self, *, reason: str, authenticated_caller: str
    ) -> dict[str, Any]:
        clean_reason = _text(reason, "override reason")
        clean_caller = _text(authenticated_caller, "authenticated_caller")
        checked = self.check_content_store(authenticated_caller=clean_caller)
        if checked["status"] != "violated":
            raise AuthorityIntegrityError(
                "content store override requires an observed mismatch"
            )
        events = self._read_events(CONTENT_STORE_SUBJECT_ID)
        for lease_id in sorted(self._open_transition_ids(events)):
            self._append_event(CONTENT_STORE_SUBJECT_ID, {
                "kind": "transition_abort",
                "lease_id": lease_id,
                "relative_run_dir": CONTENT_STORE_RELATIVE_ID,
                "reason": "closed by recorded content store integrity override",
                "authenticated_caller": clean_caller,
                "created_at": _now(),
            })
        events = self._read_events(CONTENT_STORE_SUBJECT_ID)
        manifest = self._content_manifest()
        checkpoint, files = self._new_content_checkpoint(
            manifest=manifest,
            events=events,
            checkpoint_kind="integrity_override",
            integrity_state="debug_overridden",
        )
        override = self._content_signed_record(
            kind="override",
            record={
                "override_id": f"content-override-{uuid.uuid4().hex}",
                "violation_id": checked["violation"]["violation_id"],
                "resulting_checkpoint_id": checkpoint["checkpoint_id"],
                "reason": clean_reason,
                "authenticated_caller": clean_caller,
                "created_at": _now(),
            },
            children=[],
        )
        recovery_snapshot = self._protect_recovery_snapshot(
            subject_id=CONTENT_STORE_SUBJECT_ID,
            artifact_root=self._content_store_root,
            manifest=manifest,
            checkpoint_id=checkpoint["checkpoint_id"],
        )
        confirmed = self._content_manifest()
        if confirmed.manifest_sha256 != manifest.manifest_sha256:
            raise AuthorityIntegrityError(
                "content store changed while the protected checkpoint was created"
            )
        self._append_event(CONTENT_STORE_SUBJECT_ID, {
            "kind": "checkpoint",
            "relative_run_dir": CONTENT_STORE_RELATIVE_ID,
            "transition_lease_id": None,
            "integrity_override": {"record": override, "children": []},
            "recovery_snapshot": recovery_snapshot,
            "record": checkpoint,
            "children": files,
        })
        return {
            "status": "debug_overridden",
            "audit_ready": False,
            "violation": checked["violation"],
            "differences": checked["differences"],
            "checkpoint": checkpoint,
            "files": files,
            "override": override,
        }
