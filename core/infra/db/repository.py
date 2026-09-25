#!/usr/bin/env python3
# core/infra/db/repository.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted repository API for DB-native runs.

This module defines the public interface that the orchestrator and other trusted
core modules will use. The LLM must never write to the database directly.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from functools import wraps
import re
import socket
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from core.invocation import run_command

from .connection import (
    configure_writable_connection,
    connect_existing_writable,
    connect_readonly,
    connect_run,
    db_path,
)
from .schema_bootstrap import (
    ensure_schema,
    require_current_schema_structure,
)
from .fetch_candidates import (
    add_event as _add_frozen_fetch_candidate_event,
    admit_batch as _admit_frozen_fetch_candidate_batch,
    claim as _claim_frozen_fetch_candidate,
    eligible as _eligible_frozen_fetch_candidates,
    freeze_stage as _freeze_fetch_candidate_stage,
    lifecycle_summaries as _frozen_fetch_candidate_lifecycle_summaries,
    read_record as _read_frozen_fetch_candidate_record,
)
from .fetch_traces import (
    read_fetch_trace,
    read_source_flags,
    normalize_source_flags,
    replace_fetch_trace,
    replace_source_flags,
)
from .execution_assurance_storage import (
    downgrade as _downgrade_execution_assurance,
    insert as _insert_execution_assurance,
    read as _read_execution_assurance,
    standalone_unattested as _standalone_unattested,
)
from .http_trace_storage import insert_http_attempt, read_http_attempts
from .integrity_storage import (
    append_checkpoint as _append_artifact_checkpoint,
    append_override as _append_artifact_override,
    append_recovery as _append_artifact_recovery,
    append_violation as _append_artifact_violation,
    integrity_summary as _artifact_integrity_summary,
    list_checkpoints as _list_artifact_checkpoints,
    list_recoveries as _list_artifact_recoveries,
)
from .performance_storage import read_performance_spans, write_performance_spans
from .unit_progress_storage import (
    append_completion as _append_integrity_unit_completion,
    list_completions as _list_integrity_unit_completions,
)
from .phase_event_storage import insert_phase_event, read_phase_event
from .resolve_attempt_storage import (
    read_resolve_attempts as _read_resolve_attempts,
    replace_resolve_attempts as _replace_resolve_attempts,
    validate_resolve_attempts as _validate_resolve_attempts,
)
from .resolve_trace_storage import (
    read_resolution_trace as _read_resolution_trace,
    replace_resolution_trace as _replace_resolution_trace,
    split_resolution_trace as _split_resolution_trace,
    validate_resolution_trace as _validate_resolution_trace,
)
from .resolve_transport_storage import (
    insert_attempt as _insert_resolve_transport_attempt,
    insert_failure as _insert_resolve_transport_failure,
    insert_operation as _insert_resolve_transport_operation,
    insert_ref_mapping as _insert_resolve_transport_ref_mapping,
    read_attempts as _read_resolve_transport_attempts,
    read_failures as _read_resolve_transport_failures,
    read_manuscript_targets as _read_resolve_transport_manuscript_targets,
    read_operations as _read_resolve_transport_operations,
    read_ref_mappings as _read_resolve_transport_ref_mappings,
)
from .institutional_search_provenance_storage import (
    insert as _insert_institutional_search_provenance,
    read as _read_institutional_search_provenance,
)
from .fetch_transport_storage import (
    insert_attempt as _insert_fetch_transport_attempt,
    insert_failure as _insert_fetch_transport_failure,
    insert_request as _insert_fetch_transport_request,
    read_attempts as _read_fetch_transport_attempts,
    read_failures as _read_fetch_transport_failures,
    read_requests as _read_fetch_transport_requests,
)
from .credential_observation_storage import (
    append_observation as _append_credential_observation,
    read_inventory as _read_credential_inventory,
    read_observations as _read_credential_observations,
    transport_observation_payload as _transport_credential_observation,
    write_inventory as _write_credential_inventory,
)
from .run_setting_storage import (
    COMPLEX_KEYS, SCALAR_KEYS, list_settings as _list_typed_settings,
    normalize_setting, read_setting as _read_typed_setting,
    write_setting as _write_typed_setting,
)
from .task_storage import (
    _read_source_text,
    normalize_task_answer,
    normalize_task_payload,
    read_task_answer,
    read_task_payload,
    replace_task_answer,
    replace_task_payload,
    _source_identity_attestation_snapshot,
    _source_identity_attestation_sha,
)
from .task_answer_provenance_storage import (
    insert_provenance as _insert_task_answer_provenance,
    read_provenance as _read_task_answer_provenance,
)
from .models import (
    CitationRecord,
    ClaimRecord,
    PhaseEventRecord,
    ReferenceRecord,
    ResolveResultRecord,
    RunRecord,
    ExecutionAssuranceRecord,
    RunSessionRecord,
    SourceTextRecord,
    TaskAnswerRecord,
    TaskRecord,
)


def _parse_coverage_thresholds() -> tuple[float, int]:
    """Load the Parse coverage policy used to validate its stored projection."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "parse",
        "config",
        "coverage.json",
    )
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        return float(raw["min_coverage_pct"]), int(raw["min_references"])
    except (OSError, ValueError, KeyError, TypeError):
        return 50.0, 5


_MIN_COVERAGE_PCT, _MIN_COVERAGE_REFS = _parse_coverage_thresholds()


JsonDict = dict[str, Any]
_SETTING_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}$")
_DB_WRITE_LOCK = threading.RLock()
_CANTOPEN_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.25, 0.5, 1.0)


class _OpenRunResource:
    """One writable SQLite connection shared by leased repository handles."""

    def __init__(self, conn):
        self.conn = conn
        self.connection_lock = threading.RLock()
        self.lease_count = 0
        self.schema_version: int | None = None


_OPEN_RUN_REPOSITORIES: dict[str, _OpenRunResource] = {}


def _is_sqlite_cantopen(error: Exception) -> bool:
    """Return whether SQLite reported that an existing database could not open."""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    error_code = getattr(error, "sqlite_errorcode", None)
    if error_code is not None:
        return (int(error_code) & 0xFF) == sqlite3.SQLITE_CANTOPEN
    return "unable to open database file" in str(error).lower()


class RunRepository:
    """Abstract trusted repository for DB-native runs.

    Concrete SQLite implementation will live behind this API. `run.py` and the
    rest of the core should depend on these semantic methods, never on raw SQL.
    """

    def claim_evidence_adapter(self):
        """Return the narrow adapter for the new verifier ledger."""
        from core.verify.claim_evidence.adapters.run_repository import ClaimEvidenceRunRepository
        return ClaimEvidenceRunRepository(self)

    @classmethod
    def create(
        cls,
        run_dir: str,
        *,
        run_id: str,
        input_path: str,
        input_sha256: str,
        accuracy: str,
        style: str | None,
        model_id: str | None,
        http_profile: str | None,
        challenge_mode: str | None,
        fixture_fingerprint: str,
        parent_run_id: str | None = None,
        run_origin: str = "fresh",
        execution_assurance: ExecutionAssuranceRecord | None = None,
    ) -> "RunRepository":
        run_id = _validate_identifier("run_id", run_id)
        path = db_path(run_dir)
        if os.path.exists(path):
            raise RuntimeError(f"run database already exists in {run_dir}")
        repo = cls(run_dir, connect_run(run_dir))
        ensure_schema(repo._conn)
        repo._schema_version = require_current_schema_structure(repo._conn)
        repo._conn.execute(
            "INSERT OR IGNORE INTO verification_ledger_metadata(singleton, fingerprint_version) VALUES(1, 'claim-evidence-fingerprint-v2')"
        )
        created_at = _now()
        assurance = _standalone_unattested() if execution_assurance is None else execution_assurance
        with repo._conn:
            repo._conn.execute(
                """
                INSERT INTO run(
                  run_id, created_at, updated_at, status, phase,
                  input_path, input_sha256, accuracy, style, model_id,
                  http_profile, challenge_mode, fixture_fingerprint,
                  parent_run_id, run_origin
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    created_at,
                    created_at,
                    "active",
                    "parse",
                    _clean_text(input_path) or "",
                    _clean_text(input_sha256) or "",
                    _clean_text(accuracy) or "",
                    _clean_text(style),
                    _clean_text(model_id),
                    _clean_text(http_profile),
                    _clean_text(challenge_mode),
                    _clean_text(fixture_fingerprint) or "",
                    _clean_text(parent_run_id),
                    _clean_text(run_origin) or "fresh",
                ),
            )
            _insert_execution_assurance(repo._conn, assurance)
        return repo

    @classmethod
    def open(cls, run_dir: str) -> "RunRepository":
        path = db_path(run_dir)
        if not os.path.exists(path):
            raise RuntimeError(f"no sqlite run database found in {run_dir}")
        absolute_run_dir = os.path.abspath(run_dir)
        with _DB_WRITE_LOCK:
            existing = _OPEN_RUN_REPOSITORIES.get(absolute_run_dir)
            if existing is not None:
                existing.lease_count += 1
                return cls(run_dir, existing.conn, resource=existing)
        last_error: sqlite3.OperationalError | None = None
        for delay in (*_CANTOPEN_RETRY_DELAYS_SECONDS, None):
            try:
                with _DB_WRITE_LOCK:
                    existing = _OPEN_RUN_REPOSITORIES.get(absolute_run_dir)
                    if existing is not None:
                        existing.lease_count += 1
                        return cls(run_dir, existing.conn, resource=existing)
                    repo = cls(run_dir, connect_existing_writable(path))
                    try:
                        require_current_schema_structure(repo._conn)
                        row = repo._conn.execute("SELECT run_id FROM run LIMIT 1").fetchone()
                        if row is None:
                            raise RuntimeError(f"no run record found in {run_dir}")
                        configure_writable_connection(repo._conn)
                        repo._schema_version = require_current_schema_structure(repo._conn)
                    except Exception:
                        repo.close()
                        raise
                    resource = _OpenRunResource(repo._conn)
                    resource.lease_count = 1
                    resource.schema_version = repo._schema_version
                    repo._resource = resource
                    repo._connection_lock = resource.connection_lock
                    repo._claim_evidence_connection_lock = resource.connection_lock
                    _OPEN_RUN_REPOSITORIES[absolute_run_dir] = resource
                    return repo
            except sqlite3.OperationalError as error:
                if not _is_sqlite_cantopen(error):
                    raise
                last_error = error
            if delay is not None:
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    @classmethod
    @contextmanager
    def session(cls, run_dir: str):
        """Pin one writable repository connection for a pipeline drive."""
        repo = cls.open(run_dir)
        try:
            yield repo
        finally:
            repo.close()

    @classmethod
    def open_readonly(cls, run_dir: str) -> "RunRepository":
        """Open an existing run without schema bootstrap, migration, or writes."""
        path = db_path(run_dir)
        if not os.path.exists(path):
            raise RuntimeError(f"no sqlite run database found in {run_dir}")
        repo = cls(run_dir, connect_readonly(path))
        try:
            repo._schema_version = require_current_schema_structure(repo._conn)
            row = repo._conn.execute("SELECT run_id FROM run LIMIT 1").fetchone()
            if row is None:
                raise RuntimeError(f"no run record found in {run_dir}")
        except Exception:
            repo.close()
            raise
        return repo

    def __init__(self, run_dir: str, conn, *, resource: _OpenRunResource | None = None):
        self._run_dir = os.path.abspath(run_dir)
        self._conn = conn
        self._resource = resource
        self._connection_lock = (
            resource.connection_lock if resource is not None else threading.RLock()
        )
        self._claim_evidence_connection_lock = self._connection_lock
        self._schema_version: int | None = (
            resource.schema_version if resource is not None else None
        )
        self._closed = False

    def __getattribute__(self, name: str):
        value = object.__getattribute__(self, name)
        if name.startswith("_") or not callable(value):
            return value
        connection_lock = object.__getattribute__(self, "_connection_lock")

        @wraps(value)
        def locked(*args, **kwargs):
            with connection_lock:
                if name != "close" and object.__getattribute__(self, "_closed"):
                    raise sqlite3.ProgrammingError("Cannot operate on a closed repository handle.")
                return value(*args, **kwargs)

        return locked

    @property
    def schema_version(self) -> int | None:
        """The schema version validated when this repository was opened."""
        return self._schema_version

    @contextmanager
    def _write_transaction(self):
        with _DB_WRITE_LOCK:
            with self._conn:
                yield

    def close(self) -> None:
        if self._closed:
            return
        with _DB_WRITE_LOCK:
            if self._closed:
                return
            self._closed = True
            resource = self._resource
            if resource is not None:
                resource.lease_count -= 1
                if resource.lease_count:
                    return
                if _OPEN_RUN_REPOSITORIES.get(self._run_dir) is resource:
                    del _OPEN_RUN_REPOSITORIES[self._run_dir]
            self._conn.close()

    def get_run(self) -> RunRecord:
        row = self._conn.execute("SELECT * FROM run LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("run record missing")
        return _run_record(row)

    def get_execution_assurance(self) -> ExecutionAssuranceRecord:
        return _read_execution_assurance(self._conn)

    def downgrade_execution_assurance(self, value: ExecutionAssuranceRecord) -> None:
        with self._write_transaction():
            _downgrade_execution_assurance(self._conn, value)
            self._conn.execute("UPDATE run SET updated_at = ?", (_now(),))

    def write_completed_remediation_provenance(self, value: JsonDict) -> None:
        expected = {
            "parent_run_id", "parent_input_sha256", "parent_report_sha256",
            "parent_journal_sha256", "source_inventory_sha256", "created_at",
        }
        if set(value) != expected:
            raise ValueError("completed remediation provenance has unsupported fields")
        parent_run_id = _validate_identifier("parent_run_id", value["parent_run_id"])
        fields = tuple(value[key] for key in (
            "parent_input_sha256", "parent_report_sha256", "parent_journal_sha256",
            "source_inventory_sha256",
        ))
        if any(type(field) is not str or not re.fullmatch(r"[0-9a-f]{64}", field) for field in fields):
            raise ValueError("completed remediation provenance has invalid SHA-256")
        created_at = _clean_text(value["created_at"])
        if not created_at:
            raise ValueError("completed remediation provenance requires created_at")
        with self._write_transaction():
            if self._conn.execute(
                "SELECT 1 FROM completed_remediation_provenance WHERE singleton=1"
            ).fetchone() is not None:
                raise RuntimeError("completed remediation provenance is already recorded")
            self._conn.execute(
                """
                INSERT INTO completed_remediation_provenance(
                  singleton,parent_run_id,parent_input_sha256,parent_report_sha256,
                  parent_journal_sha256,source_inventory_sha256,created_at
                ) VALUES(1,?,?,?,?,?,?)
                """,
                (parent_run_id, *fields, created_at),
            )

    def get_completed_remediation_provenance(self) -> JsonDict | None:
        row = self._conn.execute(
            "SELECT parent_run_id,parent_input_sha256,parent_report_sha256,"
            "parent_journal_sha256,source_inventory_sha256,created_at "
            "FROM completed_remediation_provenance WHERE singleton=1"
        ).fetchone()
        return None if row is None else dict(row)

    def freeze_fetch_candidate_stage(
        self, ref_id: str, stage: str, context: JsonDict, candidates: list[JsonDict]
    ) -> list[int]:
        """Persist one immutable finalized Fetch stage in one transaction."""
        with self._write_transaction():
            return _freeze_fetch_candidate_stage(self._conn, ref_id, stage, context, candidates)

    def record_frozen_fetch_candidate_event(
        self, candidate_id: int, event_type: str, *, fetch_attempt_id: int | None = None,
        reason_code: str | None = None, reason_detail: str | None = None,
    ) -> bool:
        with self._write_transaction():
            return _add_frozen_fetch_candidate_event(
                self._conn, candidate_id, event_type, fetch_attempt_id=fetch_attempt_id,
                reason_code=reason_code, reason_detail=reason_detail,
            )

    def list_eligible_frozen_fetch_candidates(self) -> list[int]:
        return _eligible_frozen_fetch_candidates(self._conn)

    def admit_frozen_fetch_candidate_batch(self, candidate_ids: list[int]) -> bool:
        with self._write_transaction():
            return _admit_frozen_fetch_candidate_batch(self._conn, candidate_ids)

    def claim_frozen_fetch_candidate(self, candidate_id: int) -> bool:
        """Atomically claim an eligible candidate for one replay attempt."""
        with self._write_transaction():
            return _claim_frozen_fetch_candidate(self._conn, candidate_id)

    def get_frozen_fetch_candidate_record(self, candidate_id: int) -> JsonDict:
        return _read_frozen_fetch_candidate_record(self._conn, candidate_id)

    def list_frozen_fetch_candidate_lifecycle_summaries(
        self, ref_id: str
    ) -> list[JsonDict]:
        """Return validated frozen retry lifecycle facts for one reference."""
        return _frozen_fetch_candidate_lifecycle_summaries(self._conn, ref_id)

    def update_run_phase(self, phase: str) -> None:
        with self._write_transaction():
            self._conn.execute(
                "UPDATE run SET phase = ?, updated_at = ?",
                (phase, _now()),
            )

    def update_run_status(self, status: str) -> None:
        with self._write_transaction():
            self._conn.execute(
                "UPDATE run SET status = ?, updated_at = ?",
                (status, _now()),
            )

    def mark_run_phase(
        self,
        phase: str,
        *,
        status: str | None = None,
        event_type: str | None = None,
        session_id: str | None = None,
        payload: JsonDict | None = None,
    ) -> int | None:
        """Persist a phase transition and its audit event atomically."""
        event_id = None
        with self._write_transaction():
            now = _now()
            self._conn.execute(
                "UPDATE run SET phase = ?, updated_at = ?",
                (phase, now),
            )
            if status is not None:
                self._conn.execute(
                    "UPDATE run SET status = ?, updated_at = ?",
                    (status, now),
                )
            if event_type:
                event_id = insert_phase_event(
                    self._conn, phase=phase, event_type=event_type, created_at=now,
                    session_id=session_id, payload=_sanitize_json_value(payload),
                )
        if event_type:
            self._schema_version = require_current_schema_structure(self._conn)
        return event_id

    def touch_run(self) -> None:
        with self._write_transaction():
            self._conn.execute(
                "UPDATE run SET updated_at = ?",
                (_now(),),
            )

    def set_run_setting(self, key: str, value: Any) -> None:
        key = _validate_setting_key(key)
        value = normalize_setting(key, _sanitize_json_value(value))
        with self._write_transaction():
            _write_typed_setting(self._conn, key, value)
            self._conn.execute("UPDATE run SET updated_at = ?", (_now(),))

    def update_run_settings(self, values: dict[str, Any]) -> None:
        if not values:
            return
        now = _now()
        normalized = []
        for key, value in values.items():
            key = _validate_setting_key(key)
            normalized.append((key, normalize_setting(key, _sanitize_json_value(value))))
        with self._write_transaction():
            for key, value in normalized:
                _write_typed_setting(self._conn, key, value)
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))

    def get_run_setting(self, key: str, default: Any = None) -> Any:
        key = _validate_setting_key(key)
        return _read_typed_setting(self._conn, key, default)

    def list_run_settings(self) -> dict[str, Any]:
        return _list_typed_settings(self._conn)

    def append_artifact_checkpoint(
        self, record: JsonDict, files: list[JsonDict]
    ) -> None:
        with self._write_transaction():
            _append_artifact_checkpoint(self._conn, record, files)

    def append_artifact_integrity_violation(
        self, record: JsonDict, differences: list[JsonDict]
    ) -> None:
        with self._write_transaction():
            _append_artifact_violation(self._conn, record, differences)

    def append_artifact_integrity_override(self, record: JsonDict) -> None:
        with self._write_transaction():
            _append_artifact_override(self._conn, record)

    def append_artifact_crash_recovery(
        self, record: JsonDict, entries: list[JsonDict]
    ) -> None:
        with self._write_transaction():
            _append_artifact_recovery(self._conn, record, entries)

    def list_artifact_crash_recoveries(self) -> list[JsonDict]:
        return _list_artifact_recoveries(self._conn)

    def append_artifact_integrity_override_bundle(
        self,
        *,
        violation: JsonDict,
        differences: list[JsonDict],
        checkpoint: JsonDict,
        files: list[JsonDict],
        override: JsonDict,
    ) -> None:
        """Atomically mirror a trusted violation, debug baseline, and override."""
        with self._write_transaction():
            _append_artifact_violation(self._conn, violation, differences)
            _append_artifact_checkpoint(self._conn, checkpoint, files)
            _append_artifact_override(self._conn, override)

    def list_artifact_checkpoints(self) -> list[JsonDict]:
        return _list_artifact_checkpoints(self._conn)

    def artifact_integrity_summary(self) -> JsonDict:
        return _artifact_integrity_summary(self._conn)

    def append_http_attempt(self, payload: JsonDict) -> None:
        """Append one closed raw-transport observation."""
        with self._write_transaction():
            insert_http_attempt(self._conn, payload, created_at=_now())

    def list_http_attempts(self) -> list[JsonDict]:
        return read_http_attempts(self._conn)

    def write_performance_spans(self, rows: list[JsonDict], *, session_id: str) -> None:
        with self._write_transaction():
            write_performance_spans(
                self._conn, rows, session_id=session_id, recorded_at=_now()
            )

    def append_resolve_transport_operation(self, payload: JsonDict) -> None:
        with self._write_transaction():
            _insert_resolve_transport_operation(
                self._conn, payload, created_at=_now()
            )

    def append_resolve_transport_ref_mapping(
        self, operation_id: str, ref_id: str
    ) -> None:
        with self._write_transaction():
            _insert_resolve_transport_ref_mapping(self._conn, operation_id, ref_id)

    def write_credential_inventory(self, inventory: list[JsonDict]) -> None:
        with self._write_transaction():
            _write_credential_inventory(self._conn, inventory, recorded_at=_now())

    def credential_inventory(self) -> JsonDict | None:
        return _read_credential_inventory(self._conn)

    def append_credential_transport_observation(self, payload: JsonDict) -> None:
        with self._write_transaction():
            _append_credential_observation(self._conn, payload, created_at=_now())

    def list_credential_transport_observations(self) -> list[JsonDict]:
        return _read_credential_observations(self._conn)

    def append_resolve_transport_attempt(
        self,
        payload: JsonDict,
        *,
        credential: JsonDict | None = None,
    ) -> None:
        with self._write_transaction():
            created_at = _now()
            _insert_resolve_transport_attempt(
                self._conn, payload, created_at=created_at
            )
            if credential is not None:
                observation = _transport_credential_observation(
                    credential,
                    observation_id="resolve:" + payload["attempt_id"],
                    channel="resolve",
                    outcome=payload["outcome"],
                    http_status=payload.get("status"),
                )
                _append_credential_observation(
                    self._conn, observation, created_at=created_at
                )

    def append_resolve_transport_failure(self, payload: JsonDict) -> None:
        with self._write_transaction():
            _insert_resolve_transport_failure(self._conn, payload, created_at=_now())

    def list_resolve_transport_operations(self) -> list[JsonDict]:
        return _read_resolve_transport_operations(self._conn)

    def list_resolve_transport_ref_mappings(self) -> list[JsonDict]:
        return _read_resolve_transport_ref_mappings(self._conn)

    def list_resolve_transport_manuscript_targets(self) -> list[JsonDict]:
        return _read_resolve_transport_manuscript_targets(self._conn)

    def list_resolve_transport_attempts(self) -> list[JsonDict]:
        return _read_resolve_transport_attempts(self._conn)

    def list_resolve_transport_failures(self) -> list[JsonDict]:
        return _read_resolve_transport_failures(self._conn)

    def append_institutional_search_provenance(self, payload: JsonDict) -> None:
        with self._write_transaction():
            _insert_institutional_search_provenance(self._conn, payload, created_at=_now())

    def list_institutional_search_provenance(self) -> list[JsonDict]:
        return _read_institutional_search_provenance(self._conn)

    def append_fetch_transport_request(self, payload: JsonDict) -> None:
        with self._write_transaction():
            _insert_fetch_transport_request(self._conn, payload, created_at=_now())

    def append_fetch_transport_attempt(
        self,
        payload: JsonDict,
        *,
        credential: JsonDict | None = None,
    ) -> None:
        with self._write_transaction():
            created_at = _now()
            _insert_fetch_transport_attempt(
                self._conn, payload, created_at=created_at
            )
            if credential is not None:
                observation = _transport_credential_observation(
                    credential,
                    observation_id="fetch:" + payload["attempt_id"],
                    channel="fetch",
                    outcome=payload["outcome"],
                    http_status=payload.get("status"),
                )
                _append_credential_observation(
                    self._conn, observation, created_at=created_at
                )

    def append_fetch_transport_failure(self, payload: JsonDict) -> None:
        with self._write_transaction():
            _insert_fetch_transport_failure(self._conn, payload, created_at=_now())

    def list_fetch_transport_requests(self) -> list[JsonDict]:
        return _read_fetch_transport_requests(self._conn)

    def list_fetch_transport_attempts(self) -> list[JsonDict]:
        return _read_fetch_transport_attempts(self._conn)

    def list_fetch_transport_failures(self) -> list[JsonDict]:
        return _read_fetch_transport_failures(self._conn)

    def list_performance_spans(self) -> list[JsonDict]:
        return read_performance_spans(self._conn)

    def status_snapshot(self) -> JsonDict:
        run = self.get_run()
        return {
            "schema": "citation-verifier.run-status.v1",
            "run_dir": self._run_dir,
            "phase": run.phase,
            "created_at": run.created_at,
            "done": run.phase == "done",
            "report_present": os.path.exists(os.path.join(self._run_dir, "report.md")),
            "report_journal_present": os.path.exists(os.path.join(self._run_dir, "report.journal.md")),
            "pending_tasks_total": self._count_tasks(status="pending"),
            "answered_tasks_waiting_resume_total": self._count_tasks(status="answered"),
            "pending_tasks_by_slot": self._count_tasks_by_slot(status="pending"),
            "answered_tasks_waiting_resume_by_slot": self._count_tasks_by_slot(status="answered"),
            "resume_command": run_command("--run", self._run_dir, "--resume"),
            "next_action": self._next_action(run.phase),
            "run_status": run.status,
        }

    def start_session(self, *, pid: int | None, host: str | None) -> str:
        session_id = f"session-{uuid.uuid4().hex[:12]}"
        now = _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO run_sessions(
                  session_id, started_at, heartbeat_at, ended_at, status, pid, host
                ) VALUES(?, ?, ?, NULL, 'active', ?, ?)
                """,
                (session_id, now, now, pid, host),
            )
            self._conn.execute(
                "UPDATE run SET status = ?, updated_at = ?",
                ("active", now),
            )
        return session_id

    def claim_driver_session(
        self,
        *,
        pid: int | None,
        host: str | None,
        stale_after_seconds: int = 120,
    ) -> str | None:
        """Atomically claim the sole live driver lease for this run.

        ``flock`` is still the fast same-host guard, but it is not sufficient
        after a terminal/client crash or on platforms without ``fcntl``.  The
        database lease makes the ownership visible in the durable audit trail
        and refuses a second driver until the active owner's heartbeat is
        stale.  A stale lease is explicitly closed as interrupted before a new
        one is issued.
        """
        session_id = f"session-{uuid.uuid4().hex[:12]}"
        now = _now()
        with self._write_transaction():
            active = self._conn.execute(
                """
                SELECT * FROM run_sessions
                WHERE status = 'active' AND ended_at IS NULL
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
            if active is not None:
                if not _is_stale(active["heartbeat_at"], stale_after_seconds):
                    return None
                self._conn.execute(
                    """
                    UPDATE run_sessions
                    SET ended_at = ?, status = ?
                    WHERE session_id = ?
                    """,
                    (now, "interrupted", active["session_id"]),
                )
            self._conn.execute(
                """
                INSERT INTO run_sessions(
                  session_id, started_at, heartbeat_at, ended_at, status, pid, host
                ) VALUES(?, ?, ?, NULL, 'active', ?, ?)
                """,
                (session_id, now, now, pid, host),
            )
            self._conn.execute(
                "UPDATE run SET status = ?, updated_at = ?",
                ("active", now),
            )
        return session_id

    def heartbeat_session(self, session_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE run_sessions SET heartbeat_at = ? WHERE session_id = ?",
                (_now(), session_id),
            )

    def end_session(self, session_id: str, *, status: str) -> None:
        now = _now()
        with self._conn:
            self._conn.execute(
                """
                UPDATE run_sessions
                SET ended_at = ?, status = ?
                WHERE session_id = ?
                """,
                (now, status, session_id),
            )
            if status in ("completed", "failed", "interrupted"):
                run_status = "completed" if status == "completed" else status
                self._conn.execute(
                    "UPDATE run SET status = ?, updated_at = ?",
                    (run_status, now),
                )

    def get_active_session(self) -> RunSessionRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM run_sessions
            WHERE status = 'active' AND ended_at IS NULL
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        return None if row is None else _run_session_record(row)

    def detect_interrupted_run(self, *, stale_after_seconds: int) -> bool:
        active = self.get_active_session()
        if active is None:
            return False
        return _is_stale(active.heartbeat_at, stale_after_seconds)

    def mark_interrupted_if_stale(self, *, stale_after_seconds: int) -> bool:
        active = self.get_active_session()
        if active is None or not _is_stale(active.heartbeat_at, stale_after_seconds):
            return False
        self.end_session(active.session_id, status="interrupted")
        return True

    def can_resume(self) -> bool:
        run = self.get_run()
        return run.status in ("active", "paused", "interrupted")

    def can_restart(self) -> bool:
        run = self.get_run()
        return run.status != "completed"

    def _reject_parse_mutation_after_frozen_fetch(self) -> None:
        """Keep immutable Fetch and Resolve transport audits tied to Parse."""
        frozen_plan = self._conn.execute(
            "SELECT 1 FROM fetch_candidate_plans LIMIT 1"
        ).fetchone()
        if frozen_plan is not None:
            raise RuntimeError(
                "cannot restart or replace Parse after frozen Fetch candidates; "
                "use clone_as_fresh_run instead"
            )
        transport_audit = self._conn.execute(
            "SELECT 1 FROM resolve_transport_operations "
            "UNION ALL SELECT 1 FROM resolve_transport_failures "
            "UNION ALL SELECT 1 FROM fetch_transport_requests "
            "UNION ALL SELECT 1 FROM fetch_transport_failures LIMIT 1"
        ).fetchone()
        if transport_audit:
            raise RuntimeError(
                "cannot restart or replace Parse after Fetch transport audit or Resolve transport audit; "
                "use clone_as_fresh_run instead"
            )

    def clone_as_fresh_run(
        self,
        *,
        new_run_dir: str,
        new_run_id: str,
        run_origin: str = "restarted_from_interrupted",
        execution_assurance: ExecutionAssuranceRecord | None = None,
        preserve_model_id: bool = True,
    ) -> "RunRepository":
        run = self.get_run()
        source_assurance = self.get_execution_assurance()
        target_assurance = (
            source_assurance if execution_assurance is None else execution_assurance
        )
        if (
            source_assurance.protection != "agent_attested"
            and target_assurance.protection == "agent_attested"
        ):
            raise RuntimeError(
                "an unattested run cannot be cloned as agent_attested"
            )
        return type(self).create(
            new_run_dir,
            run_id=new_run_id,
            input_path=run.input_path,
            input_sha256=run.input_sha256,
            accuracy=run.accuracy,
            style=run.style,
            model_id=run.model_id if preserve_model_id else None,
            http_profile=run.http_profile,
            challenge_mode=run.challenge_mode,
            fixture_fingerprint=run.fixture_fingerprint,
            parent_run_id=run.run_id,
            run_origin=run_origin,
            execution_assurance=target_assurance,
        )

    def restart_in_place(self, *, reason: str) -> None:
        now = _now()
        with self._conn:
            self._reject_parse_mutation_after_frozen_fetch()
            self._conn.execute(
                """
                UPDATE run_sessions
                SET status='interrupted', ended_at=?, heartbeat_at=?
                WHERE status='active'
                """,
                (now, now),
            )
            for table in (
                "tasks",
                "task_answers",
                "fetch_attempts",
                "source_text_materialization_intents",
                "source_texts",
                "unreadable_sources",
            ):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute("DELETE FROM resolve_results")
            self._conn.execute(
                """
                UPDATE run
                SET status = ?, phase = ?, updated_at = ?
                """,
                ("active", "parse", now),
            )
            insert_phase_event(
                self._conn, phase="parse", event_type="resume", created_at=now,
                payload=_sanitize_json_value({"restart_reason": reason}),
            )

    def append_phase_event(
        self,
        phase: str,
        event_type: str,
        *,
        session_id: str | None = None,
        payload: JsonDict | None = None,
    ) -> int:
        with self._write_transaction():
            event_id = insert_phase_event(
                self._conn, phase=phase, event_type=event_type, created_at=_now(),
                session_id=session_id, payload=_sanitize_json_value(payload),
            )
        self._schema_version = require_current_schema_structure(self._conn)
        return event_id

    def list_phase_events(self) -> list[PhaseEventRecord]:
        return [_phase_event_record(self._conn, row) for row in self._conn.execute(
            "SELECT * FROM phase_events ORDER BY event_id"
        )]

    def replace_parse_payload(
        self,
        *,
        claims: list[JsonDict],
        references: list[JsonDict],
        citations: list[JsonDict],
        manuscript_text: str | None = None,
        manuscript_identity: JsonDict | None = None,
        table_only_citations: list[JsonDict] | None = None,
        coverage: JsonDict | None = None,
        parse_extract: JsonDict | None = None,
        footnote_notes: list[JsonDict] | None = None,
        footnote_note_sources: list[JsonDict] | None = None,
        claim_footnotes: list[JsonDict] | None = None,
        footnote_note_parents: list[JsonDict] | None = None,
        ambiguities: list[JsonDict] | None = None,
        orphan_diagnostics: list[JsonDict] | None = None,
    ) -> None:
        now = _now()
        with self._conn:
            self._reject_parse_mutation_after_frozen_fetch()
            for table in (
                "manual_footnote_source_overrides",
                "manual_reference_identity_overrides",
                "manual_parse_review_applications",
                "manual_citation_attribution_overrides",
                "unresolved_citation_candidates",
                "unresolved_citations",
                "task_answers",
                "tasks",
                "unreadable_sources",
                "source_text_materialization_intents",
                "source_texts",
                "fetch_attempts",
                "resolve_results",
                "reference_identity",
                "claim_footnotes",
                "citations",
                "parse_link_silent_citation_markers",
                "claim_marker_members",
                "claims",
                "manuscript_identity_identifiers",
                "manuscript_identity",
                "manuscript_text",
                "parse_table_citation_markers",
                "footnote_note_sources",
                "footnote_note_parents",
                "footnote_notes",
                "operational_references",
                "reference_entries",
            ):
                self._conn.execute(f"DELETE FROM {table}")

            if manuscript_text is not None:
                if (
                    not isinstance(manuscript_text, str)
                    or not manuscript_text.strip()
                    or "\x00" in manuscript_text
                ):
                    raise ValueError("manuscript text must be nonempty NUL-free text")
                encoded_manuscript = manuscript_text.encode("utf-8")
                self._conn.execute(
                    "INSERT INTO manuscript_text VALUES(1,?,?,?)",
                    (
                        manuscript_text,
                        hashlib.sha256(encoded_manuscript).hexdigest(),
                        len(manuscript_text),
                    ),
                )

            if manuscript_identity is not None:
                self._replace_manuscript_identity(manuscript_identity)

            for idx, claim in enumerate(claims, start=1):
                claim_id = _validate_identifier(
                    "claim_id",
                    str(claim.get("claim_id") or claim.get("id") or f"claim-{idx}"),
                )
                self._conn.execute(
                    """
                    INSERT INTO claims(
                      claim_id, sentence, context_window, marker_raw, claim_scope,
                      parser_sentence_index, marker_group_index, marker_group_count,
                      marker_start, marker_end, structural_provenance_json, claim_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim_id,
                        _clean_text(claim["sentence"]) or "",
                        _clean_text(claim.get("context_window")),
                        _clean_text(claim.get("marker_raw")),
                        _clean_text(claim.get("claim_scope")),
                        _nullable_nonnegative_int(claim.get("parser_sentence_index"), "parser_sentence_index"),
                        _nullable_nonnegative_int(claim.get("marker_group_index"), "marker_group_index"),
                        _nullable_positive_int(claim.get("marker_group_count"), "marker_group_count"),
                        _nullable_nonnegative_int(claim.get("marker_start"), "marker_start"),
                        _nullable_positive_int(claim.get("marker_end"), "marker_end"),
                        _claim_structural_provenance_json(claim.get("structural_provenance")),
                        int(claim.get("claim_order") or idx),
                    ),
                )
                marker_numbers = claim.get("marker_numbers")
                if isinstance(marker_numbers, list):
                    for member_order, marker_number in enumerate(marker_numbers):
                        if type(marker_number) is not int:
                            raise ValueError(
                                "claim marker_numbers must contain only integers"
                            )
                        self._conn.execute(
                            """
                            INSERT INTO claim_marker_members(claim_id, member_order, marker_number)
                            VALUES(?, ?, ?)
                            """,
                            (claim_id, member_order, marker_number),
                        )

            ref_numbers: dict[str, int] = {}
            used_ref_numbers: set[int] = set()
            for idx, ref in enumerate(references, start=1):
                ref_id = _validate_identifier(
                    "ref_id",
                    str(ref.get("ref_id") or ref.get("id") or f"ref-{idx}"),
                )
                ref_number = int(ref.get("ref_number") or idx)
                while ref_number in used_ref_numbers:
                    ref_number += 1  # deduplicate; should not happen with well-formed input
                used_ref_numbers.add(ref_number)
                ref_numbers[ref_id] = ref_number
                self._conn.execute(
                    "INSERT INTO operational_references VALUES(?,?,?,?,?,?,?)",
                    (ref_id, ref_number, "raw_parse", ref_id, None, None, now),
                )
                self._conn.execute(
                    """
                    INSERT INTO reference_entries(
                      ref_id, ref_number, raw_entry, title, doi, pmid, isbn, url, year,
                      ay_surname, ay_year, ay_suffix,
                      source_type, source_kind, indexability, source_type_confidence
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ref_id,
                        ref_number,
                        _clean_text(ref.get("raw_entry")) or "",
                        _clean_text(ref.get("title")),
                        _clean_text(ref.get("doi")),
                        _clean_text(ref.get("pmid")),
                        _clean_text(ref.get("isbn")),
                        _clean_text(ref.get("url")),
                        ref.get("year"),
                        _clean_text(ref.get("ay_surname")),
                        ref.get("ay_year"),
                        _clean_text(ref.get("ay_suffix")),
                        _clean_text(ref.get("source_type")),
                        _clean_text(ref.get("source_kind")),
                        _clean_text(ref.get("indexability")),
                        _clean_text(ref.get("source_type_confidence")),
                    ),
                )
                self._replace_cited_coordinates(
                    ref_id, _clean_text(ref.get("raw_entry")) or "",
                    ref.get("cited_coordinates") or [],
                )

            unresolved = []
            link_silent_citations = []
            seen_edges: set[tuple[str, str]] = set()
            for citation in citations:
                raw_ref_id = citation.get("ref_id")
                if raw_ref_id is None:
                    unresolved.append(dict(citation))
                    continue
                # A marker can resolve to a reference without belonging to a claim
                # sentence.  A link-silent marker is a typed coverage fact rather than
                # a (claim, ref) edge to verify; unknown claimless shapes remain
                # excluded because they carry no declared provenance.
                raw_claim_id = citation.get("claim_id")
                if raw_claim_id is None:
                    if citation.get("provenance") == "link_silent_resolved":
                        link_silent_citations.append(citation)
                    continue
                ref_id = _validate_identifier("ref_id", str(raw_ref_id))
                claim_id = _validate_identifier("claim_id", str(raw_claim_id))
                # One claim can carry two markers to the same source ("…[5]…[5]…");
                # that is a single edge, and citations(claim_id, ref_id) is UNIQUE.
                if (claim_id, ref_id) in seen_edges:
                    continue
                seen_edges.add((claim_id, ref_id))
                self._conn.execute(
                    """
                    INSERT INTO citations(claim_id, ref_id, ref_number)
                    VALUES(?, ?, ?)
                    """,
                    (
                        claim_id,
                        ref_id,
                        int(citation.get("ref_number") or ref_numbers[ref_id]),
                    ),
                )
            # Some author-year adapters expose ambiguity only as a diagnostic;
            # retain it even when their citation list omitted the duplicate row.
            known_unresolved = {(x.get("claim_id"), x.get("marker_raw")) for x in unresolved}
            for ambiguity in ambiguities or []:
                key = (ambiguity.get("claim_id"), ambiguity.get("marker_raw"))
                if key not in known_unresolved:
                    item = {"_diagnostic_only": True, **dict(ambiguity)}
                    unresolved.append(item)
                    known_unresolved.add(key)
            # Keep all parser unresolved facts relationally; they are not edges.
            diagnostics = list(orphan_diagnostics or [])
            by_marker = {(x.get("claim_id"), str(x.get("marker_raw"))): x for x in diagnostics if x.get("marker_raw")}
            # Diagnostics without a citation row are still immutable Parse facts.
            raw_keys = {(x.get("claim_id"), str(x.get("marker_raw"))) for x in unresolved}
            for diagnostic in diagnostics:
                key = (diagnostic.get("claim_id"), str(diagnostic.get("marker_raw")))
                if key not in raw_keys:
                    unresolved.append({"_diagnostic_only": True, **diagnostic})
                    raw_keys.add(key)
            for pos, citation in enumerate(unresolved):
                diagnostic = by_marker.get((citation.get("claim_id"), str(citation.get("marker_raw"))), {})
                candidate_ids = citation.get("candidate_ref_ids") or diagnostic.get("candidate_ref_ids") or []
                if not candidate_ids and (citation.get("candidates") is not None or diagnostic.get("candidates") is not None):
                    items = citation.get("candidates") if citation.get("candidates") is not None else diagnostic["candidates"]
                    if not isinstance(items, list):
                        raise ValueError("unresolved citation diagnostic candidates are invalid")
                    candidate_ids = []
                    for item in items:
                        if not isinstance(item, dict) or set(item) != {"ref_number", "raw_entry"} or type(item["ref_number"]) is not int or not isinstance(item["raw_entry"], str):
                            raise ValueError("unresolved citation diagnostic candidate is invalid")
                        matches = [ref_id for ref_id, number in ref_numbers.items() if number == item["ref_number"] and self._conn.execute("SELECT raw_entry FROM reference_entries WHERE ref_id=?", (ref_id,)).fetchone()[0][:160] == item["raw_entry"]]
                        if len(matches) != 1:
                            raise ValueError("unresolved citation diagnostic candidate is unavailable or ambiguous")
                        candidate_ids.append(matches[0])
                if not isinstance(candidate_ids, list) or not all(isinstance(x, str) and x in ref_numbers for x in candidate_ids):
                    raise ValueError("unresolved citation candidates are invalid")
                kind = "ambiguity" if candidate_ids else "orphan"
                claim_id = citation.get("claim_id")
                if claim_id is not None:
                    claim_id = _validate_identifier("claim_id", str(claim_id))
                raw = _sanitize_json_value(citation)
                occurrence_id = hashlib.sha256(json.dumps({"claim_id": claim_id, "marker_raw": citation.get("marker_raw"), "citation": raw, "position": pos}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                raw_present = not bool(citation.get("_diagnostic_only"))
                self._conn.execute("INSERT INTO unresolved_citations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    occurrence_id, pos, claim_id, _clean_text(citation.get("marker_raw")) or "?", kind,
                    _nullable_nonnegative_int(citation.get("ref_number"), "unresolved ref_number"),
                    _clean_text(citation.get("surname") or diagnostic.get("surname")), citation.get("year") or diagnostic.get("year"),
                    _clean_text(citation.get("canonical") or diagnostic.get("canonical")),
                    json.dumps(_sanitize_json_value(citation.get("provenance") or diagnostic.get("provenance") or {}), sort_keys=True, separators=(",", ":")),
                    int(raw_present),
                    json.dumps(raw, sort_keys=True, separators=(",", ":")),
                ))
                for order, ref_id in enumerate(candidate_ids):
                    self._conn.execute("INSERT INTO unresolved_citation_candidates VALUES(?,?,?)", (occurrence_id, order, ref_id))

            self._replace_footnote_provenance(
                [] if footnote_notes is None else footnote_notes,
                [] if footnote_note_sources is None else footnote_note_sources,
                [] if claim_footnotes is None else claim_footnotes,
                [] if footnote_note_parents is None else footnote_note_parents,
            )

            self._replace_parse_link_silent_citations(link_silent_citations)
            self._replace_parse_table_citations(table_only_citations or [])
            self._replace_parse_coverage_inputs(coverage or {}, parse_extract or {})
            if coverage is not None and self.parse_coverage() != coverage:
                raise ValueError("parse coverage is not a coherent projection")

            self._conn.execute(
                "UPDATE run SET updated_at = ?, phase = ?",
                (now, "resolve"),
            )

    def _replace_cited_coordinates(
        self, ref_id: str, raw_entry: str, coordinates: object,
    ) -> None:
        """Store closed-vocabulary Parse coordinate facts and their raw spans."""
        if not isinstance(coordinates, list):
            raise ValueError("cited coordinates must be a list")
        allowed = {
            "container", "repository", "volume", "issue", "article_page_range",
            "chapter_page_range", "elocator", "article_number", "article_locator",
        }
        seen: set[str] = set()
        for coordinate in coordinates:
            if not isinstance(coordinate, dict):
                raise ValueError("cited coordinate is invalid")
            required = {"kind", "raw_value", "normalized_value", "rule_id", "extractor_version", "spans"}
            if set(coordinate) != required:
                raise ValueError("cited coordinate has invalid fields")
            kind = coordinate["kind"]
            if not isinstance(kind, str) or kind not in allowed or kind in seen:
                raise ValueError("cited coordinate kind is invalid")
            seen.add(kind)
            values = [coordinate["raw_value"], coordinate["normalized_value"], coordinate["rule_id"]]
            if not all(isinstance(value, str) and value and "\x00" not in value for value in values):
                raise ValueError("cited coordinate value is invalid")
            if coordinate["normalized_value"] != _normalize_cited_coordinate_value(
                coordinate["raw_value"]
            ):
                raise ValueError("cited coordinate normalized value is invalid")
            if coordinate["extractor_version"] != "cited-coordinates/v1":
                raise ValueError("cited coordinate extractor version is invalid")
            spans = coordinate["spans"]
            if not isinstance(spans, list) or not spans:
                raise ValueError("cited coordinate spans are invalid")
            last_end = 0
            raw_parts: list[str] = []
            for span in spans:
                if not isinstance(span, dict) or set(span) != {"raw_start", "raw_end"}:
                    raise ValueError("cited coordinate span is invalid")
                start, end = span["raw_start"], span["raw_end"]
                if type(start) is not int or type(end) is not int or start < last_end or end <= start or end > len(raw_entry):
                    raise ValueError("cited coordinate span is invalid")
                last_end = end
                raw_parts.append(raw_entry[start:end])
            if "".join(raw_parts) != coordinate["raw_value"]:
                raise ValueError("cited coordinate raw value does not match spans")
            cursor = self._conn.execute(
                """
                INSERT INTO cited_bibliographic_coordinates(
                  ref_id,coordinate_kind,raw_value,normalized_value,rule_id,extractor_version
                ) VALUES(?,?,?,?,?,?)
                """,
                (ref_id, kind, coordinate["raw_value"], coordinate["normalized_value"],
                 coordinate["rule_id"], coordinate["extractor_version"]),
            )
            coordinate_id = int(cursor.lastrowid)
            for order, span in enumerate(spans):
                self._conn.execute(
                    """INSERT INTO cited_bibliographic_coordinate_spans(
                      coordinate_id,span_order,raw_start,raw_end
                    ) VALUES(?,?,?,?)""",
                    (coordinate_id, order, span["raw_start"], span["raw_end"]),
                )

    def parse_payload(self) -> JsonDict:
        run = self.get_run()
        manuscript_identity = self.manuscript_identity_payload()
        payload = {
            "manuscript": {
                "id": run.run_id,
                "filename": os.path.basename(run.input_path),
                "input_path": run.input_path,
                "sha256": run.input_sha256,
                "accuracy": run.accuracy,
                "parser_version": "db-only",
                "citation_mode": None,
            },
            "claims": [
                _claim_dict(row, self._claim_marker_numbers(row.claim_id))
                for row in self.list_claims()
            ],
            "references": [_reference_dict(row) for row in self.list_references()],
            "citations": (
                [_citation_dict(row) for row in self.list_citations()]
                + self._parse_link_silent_citations()
            ),
            "coverage": self.parse_coverage(),
            "table_only_citations": self.parse_table_citations(),
            "_debug": {},
        }
        unresolved = []
        for row in self._conn.execute("SELECT * FROM unresolved_citations ORDER BY occurrence_order"):
            raw = json.loads(row["raw_citation_json"])
            raw["occurrence_id"] = row["occurrence_id"]
            raw["claim_id"] = row["claim_id"]
            raw["marker_raw"] = row["marker_raw"]
            raw["candidate_ref_ids"] = [x["ref_id"] for x in self._conn.execute("SELECT ref_id FROM unresolved_citation_candidates WHERE occurrence_id=? ORDER BY candidate_order", (row["occurrence_id"],))]
            raw.update(surname=row["surname"], year=row["citation_year"], canonical=row["canonical"])
            raw["_raw_citation_present"] = bool(row["raw_citation_present"])
            unresolved.append(raw)
        payload["citations"].extend(x for x in unresolved if x.pop("_raw_citation_present"))
        payload["ambiguities"] = []
        for x in unresolved:
            if not x.get("candidate_ref_ids"):
                continue
            candidates = []
            for ref_id in x["candidate_ref_ids"]:
                ref = self.get_reference(ref_id)
                if ref is None:
                    raise RuntimeError("unresolved citation candidate is unavailable")
                candidates.append({"ref_number": ref.ref_number, "raw_entry": ref.raw_entry[:160]})
            payload["ambiguities"].append({"marker_raw": x.get("marker_raw"), "surname": x.get("surname"), "year": x.get("year"), "claim_id": x.get("claim_id"), "candidates": candidates, "occurrence_id": x.get("occurrence_id")})
        payload["_debug"]["orphans"] = [x for x in unresolved if not x.get("candidate_ref_ids")]
        payload["_debug"]["reference_coverage"] = payload["coverage"]
        if manuscript_identity is not None:
            payload["manuscript"]["title"] = manuscript_identity["final_title"]
            payload["manuscript"]["title_identity"] = manuscript_identity
        footnote_notes = self._footnote_notes()
        if footnote_notes:
            payload.update({
                "footnote_notes": footnote_notes,
                "footnote_note_sources": self._footnote_note_sources(),
                "claim_footnotes": self._claim_footnotes(),
            })
        return payload

    def effective_parse_payload(self) -> JsonDict:
        """Non-mutating Parse projection for operational Resolve/Fetch consumers."""
        payload = self.parse_payload()
        notes = {row["note_id"]: row for row in payload.get("footnote_notes", [])}
        parents = {row["note_id"]: row["ref_id"] for row in self._conn.execute("SELECT note_id,ref_id FROM footnote_note_parents")}
        if set(parents) != set(notes):
            raise RuntimeError("footnote Note parent projection is incomplete")
        sources = self._footnote_note_sources()
        overrides = self._manual_footnote_source_overrides()
        manual_sources = self._manual_footnote_override_sources()
        manual_ref_ids = {source["ref_id"] for source in manual_sources}
        for source in manual_sources:
            source["_manual_override"] = True
        manual_by_note: dict[str, list[JsonDict]] = {}
        for source in manual_sources:
            manual_by_note.setdefault(source["note_id"], []).append(source)
        for note_id, override in overrides.items():
            members = manual_by_note.get(note_id, [])
            if override["action"] == "split_sources":
                if len(members) < 2 or [item["source_order"] for item in members] != list(range(len(members))):
                    raise RuntimeError("manual footnote split overlay is incomplete")
            elif members:
                raise RuntimeError("manual footnote suppression overlay has sources")
        by_note: dict[str, list[JsonDict]] = {}
        for source in sources: by_note.setdefault(source["note_id"], []).append(source)
        for note_id, override in overrides.items():
            by_note[note_id] = (
                manual_by_note.get(note_id, [])
                if override["action"] == "split_sources" else []
            )
        # A deterministic one-source footnote already uses its parent reference.
        # Keep that raw projection in place; only an actual replacement by child
        # sources is a manual split and receives manual provenance downstream.
        passthrough_parents = {
            parent_id
            for note_id, parent_id in parents.items()
            if note_id not in overrides
            and notes.get(note_id, {}).get("extraction_status") == "sources_extracted"
            and len(by_note.get(note_id, [])) == 1
            and by_note[note_id][0]["ref_id"] == parent_id
        }
        suppressed = set(parents.values()) - passthrough_parents
        projected_child_ids = {
            source["ref_id"]
            for note_id, parent_id in parents.items()
            if parent_id not in passthrough_parents
            for source in by_note.get(note_id, [])
            if source["ref_id"] != parent_id and not source.get("_manual_override")
        }
        refs = {r["id"]: dict(r) for r in payload["references"] if r["id"] not in suppressed}
        parent_notes = {ref_id: note_id for note_id, ref_id in parents.items()}
        child_by_note: dict[str, list[JsonDict]] = {}
        claims = {c["claim_id"] for c in payload["claims"]}
        for note_id, parent_id in sorted(parents.items()):
            note = notes.get(note_id)
            members = by_note.get(note_id, []) if note and (
                note["extraction_status"] == "sources_extracted"
                or overrides.get(note_id, {}).get("action") == "split_sources"
            ) else []
            if parent_id in passthrough_parents:
                continue
            if not members: continue
            claim_ids = [r["claim_id"] for r in self._conn.execute("SELECT claim_id FROM claim_footnotes WHERE note_id=? ORDER BY claim_id", (note_id,))]
            for source in members:
                if source.get("_manual_override"):
                    item = {
                        "id": source["ref_id"], "ref_id": source["ref_id"],
                        "ref_number": source["ref_number"], "raw_entry": source["source_text"],
                        "title": None, "doi": None, "pmid": None, "isbn": None,
                        "url": None, "year": None, "ay_surname": None,
                        "ay_year": None, "ay_suffix": None, "source_type": "unknown",
                        "source_kind": "footnote_source", "indexability": "unknown",
                        "source_type_confidence": "manual_split",
                    }
                else:
                    ref = self.get_reference(source["ref_id"])
                    if ref is None: raise RuntimeError("effective footnote source is unavailable")
                    item = _reference_dict(ref)
                    identity = self.effective_reference_identity(source["ref_id"])
                    item.update(title=identity["title"], doi=identity["doi"])
                projection = {"note_number": note["note_number"], "source_order": source["source_order"], "source_count": len(members), "original_ref_id": parent_id}
                if source.get("_manual_override"):
                    item.update(_manual_parse=projection)
                else:
                    item.update(_footnote_projection=projection)
                refs[item["id"]] = item
                child_by_note.setdefault(note_id, []).append(item)
        for ref_id, item in list(refs.items()):
            if ref_id in manual_ref_ids:
                continue
            identity = self.effective_reference_identity(ref_id)
            if identity != {"title": item.get("title"), "doi": item.get("doi")}:
                audit = self._conn.execute("SELECT answer_id,applied_at FROM manual_reference_identity_overrides WHERE ref_id=?", (ref_id,)).fetchone()
                item.update(title=identity["title"], doi=identity["doi"], _manual_identity_overlay={"original_title": item.get("title"), "original_doi": item.get("doi"), "answer_id": audit["answer_id"], "applied_at": audit["applied_at"]})
        effective_refs = []
        for row in payload["references"]:
            if row["id"] in projected_child_ids:
                continue
            note_id = parent_notes.get(row["id"])
            if note_id is None or row["id"] in passthrough_parents:
                effective_refs.append(refs.pop(row["id"]))
            else:
                effective_refs.extend(child_by_note.get(note_id, []))
        unavailable_note_numbers = sorted(
            notes[note_id]["note_number"]
            for note_id, parent_id in parents.items()
            if parent_id not in passthrough_parents
        )
        if unavailable_note_numbers:
            for item in effective_refs:
                item["_footnote_unavailable_note_numbers"] = unavailable_note_numbers
        payload["references"] = effective_refs
        effective_citations = []
        seen_edges: set[tuple[str, str]] = set()
        for citation in payload["citations"]:
            note_id = parent_notes.get(citation.get("ref_id"))
            if note_id is None or citation.get("ref_id") in passthrough_parents:
                key = (citation.get("claim_id"), citation.get("ref_id"))
                if citation.get("ref_id") is None or key not in seen_edges:
                    effective_citations.append(citation)
                    seen_edges.add(key)
                continue
            members = child_by_note.get(note_id, [])
            for item in members:
                key = (citation.get("claim_id"), item["id"])
                if key in seen_edges: continue
                source = next(s for s in by_note[note_id] if s["ref_id"] == item["id"])
                effective_citations.append({**citation, "ref_id": item["id"], "ref_number": item["ref_number"], "provenance": "manual_footnote_split" if source.get("_manual_override") else "parsed_footnote_split", "note_number": notes[note_id]["note_number"], "source_order": source["source_order"], "source_count": len(members), "original_ref_id": citation["ref_id"], "original_ref_number": citation["ref_number"]})
                seen_edges.add(key)
        payload["citations"] = effective_citations
        # Applied attribution is an overlay; raw unresolved facts stay in parse_payload.
        attribution = list(self._conn.execute("""
            SELECT o.*, a.answer_id,a.actor_type,a.submitted_at FROM manual_citation_attribution_overrides o
            JOIN task_answers a ON a.answer_id=o.answer_id
            ORDER BY o.task_id
        """))
        unresolved_ids = {row["occurrence_id"] for row in attribution if row["occurrence_id"] is not None}
        payload["citations"] = [c for c in payload["citations"] if c.get("occurrence_id") not in unresolved_ids]
        seen = {(c.get("claim_id"), c.get("ref_id")) for c in payload["citations"]}
        for row in attribution:
            if row["direction"] == "citation_to_reference":
                raw = self._conn.execute("SELECT claim_id,marker_raw FROM unresolved_citations WHERE occurrence_id=?", (row["occurrence_id"],)).fetchone()
                claim_id, ref_id = raw["claim_id"], row["ref_id"]
                marker_raw = raw["marker_raw"]
            else:
                claim_id, ref_id, marker_raw = row["claim_id"], row["ref_id"], None
            if (claim_id, ref_id) in seen:
                # The human adjudicated a distinct unresolved occurrence which
                # happens to confirm an already-present edge.  Preserve that
                # decision in the audit projection, while keeping the effective
                # operational edge set unique.
                continue
            ref = self.get_reference(ref_id)
            reason_row = self._conn.execute("SELECT reason FROM task_manual_parse_review_answers WHERE answer_id=?", (row["answer_id"],)).fetchone()
            payload["citations"].append({"claim_id": claim_id, "ref_id": ref_id, "ref_number": ref.ref_number, "marker_raw": marker_raw, "provenance": "manual_citation_attribution", "manual_attribution": {"task_id": row["task_id"], "answer_id": row["answer_id"], "direction": row["direction"], "origin": row["candidate_origin"], "score": row["candidate_score"], "reason": reason_row["reason"], "actor_type": row["actor_type"], "submitted_at": row["submitted_at"], "applied_at": row["applied_at"]}})
            seen.add((claim_id, ref_id))
        return payload

    def manual_parse_review_projection(self) -> list[JsonDict]:
        """Audit projection of applied Parse adjudications and open note facts.

        This intentionally reads only accepted answers that were atomically
        applied.  Pending instructions and unaccepted answer attempts are not
        operational evidence and must not leak into a report projection.
        """
        applied = {
            row["task_id"]: dict(row)
            for row in self._conn.execute(
                """
                SELECT t.task_id,t.note_id,t.ref_id,d.review_kind,d.target_sha256,
                       a.answer_id,a.actor_type,a.submitted_at,x.action,x.reason,
                       q.producer_class,q.producer_identity,q.ingress_kind,q.authority_id,
                       p.applied_at
                FROM tasks t
                JOIN task_manual_parse_review_details d ON d.task_id=t.task_id
                JOIN manual_parse_review_applications p ON p.task_id=t.task_id
                JOIN task_answers a ON a.answer_id=p.answer_id
                JOIN task_manual_parse_review_answers x ON x.answer_id=a.answer_id
                LEFT JOIN task_answer_provenance q ON q.answer_id=a.answer_id
                WHERE t.task_kind='manual_parse_review' AND t.status='applied'
                  AND a.accepted_for_processing=1 AND a.generation=t.generation
                  AND p.generation=t.generation
                ORDER BY t.task_id
                """
            )
        }
        applied_by_note = {
            row["note_id"]: row for row in applied.values() if row["note_id"] is not None
        }
        rows: list[JsonDict] = []
        source_counts = {
            row["note_id"]: int(row["source_count"])
            for row in self._conn.execute(
                "SELECT note_id,COUNT(*) AS source_count FROM footnote_note_sources GROUP BY note_id"
            )
        }
        for note_id, override in self._manual_footnote_source_overrides().items():
            source_counts[note_id] = (
                int(self._conn.execute(
                    "SELECT COUNT(*) FROM manual_footnote_source_override_sources WHERE note_id=?",
                    (note_id,),
                ).fetchone()[0])
                if override["action"] == "split_sources" else 0
            )
        for note in self._conn.execute(
            "SELECT note_id,note_number,extraction_status FROM footnote_notes ORDER BY manuscript_id,note_number,note_id"
        ):
            decision = applied_by_note.get(note["note_id"])
            if note["extraction_status"] not in {"no_sources", "ambiguous"} and not (
                decision and decision["action"] == "split_sources"
            ):
                continue
            effective_status = note["extraction_status"]
            if decision and decision["action"] == "split_sources":
                effective_status = "sources_extracted"
            elif decision and decision["action"] == "no_sources":
                effective_status = "no_sources"
            elif decision and decision["action"] == "keep_ambiguous":
                effective_status = "ambiguous"
            rows.append({
                "subject_type": "footnote_note",
                "subject_id": note["note_id"],
                "note_number": note["note_number"],
                "status": effective_status,
                "action": decision["action"] if decision else None,
                "source_count": source_counts.get(note["note_id"], 0),
                "manual_review_required": effective_status == "ambiguous" and decision is None,
                "task_id": decision["task_id"] if decision else None,
                "answer_id": decision["answer_id"] if decision else None,
                "target_sha256": decision["target_sha256"] if decision else None,
                "reason": decision["reason"] if decision else None,
                "actor_type": decision["actor_type"] if decision else None,
                "producer_class": decision["producer_class"] if decision else None,
                "producer_identity": decision["producer_identity"] if decision else None,
                "ingress_kind": decision["ingress_kind"] if decision else None,
                "authority_id": decision["authority_id"] if decision else None,
                "submitted_at": decision["submitted_at"] if decision else None,
                "applied_at": decision["applied_at"] if decision else None,
            })
        for override in self._conn.execute(
            """
            SELECT e.ref_id,e.ref_number,e.title AS original_title,e.doi AS original_doi,
                   o.title_present,o.title AS override_title,o.doi_present,o.doi AS override_doi,
                   d.target_sha256,a.task_id,a.answer_id,a.actor_type,a.submitted_at,x.reason,
                   q.producer_class,q.producer_identity,q.ingress_kind,q.authority_id,p.applied_at
            FROM manual_reference_identity_overrides o
            JOIN reference_entries e ON e.ref_id=o.ref_id
            JOIN task_answers a ON a.answer_id=o.answer_id
            JOIN tasks t ON t.task_id=a.task_id
            JOIN task_manual_parse_review_details d ON d.task_id=t.task_id
            JOIN task_manual_parse_review_answers x ON x.answer_id=a.answer_id
            JOIN manual_parse_review_applications p ON p.task_id=t.task_id AND p.answer_id=a.answer_id
            LEFT JOIN task_answer_provenance q ON q.answer_id=a.answer_id
            WHERE t.task_kind='manual_parse_review' AND t.status='applied'
              AND a.accepted_for_processing=1 AND a.generation=t.generation
              AND p.generation=t.generation
            ORDER BY e.ref_number,e.ref_id
            """
        ):
            rows.append({
                "subject_type": "reference_identity",
                "subject_id": override["ref_id"],
                "ref_number": override["ref_number"],
                "status": "identity_overridden",
                "action": "correct_identity",
                "source_count": None,
                "manual_review_required": False,
                "task_id": override["task_id"],
                "answer_id": override["answer_id"],
                "target_sha256": override["target_sha256"],
                "reason": override["reason"],
                "actor_type": override["actor_type"],
                "producer_class": override["producer_class"],
                "producer_identity": override["producer_identity"],
                "ingress_kind": override["ingress_kind"],
                "authority_id": override["authority_id"],
                "submitted_at": override["submitted_at"],
                "applied_at": override["applied_at"],
                "original_title": override["original_title"],
                "original_doi": override["original_doi"],
                "effective_title": override["override_title"] if override["title_present"] else override["original_title"],
                "effective_doi": override["override_doi"] if override["doi_present"] else override["original_doi"],
            })
        for decision in sorted(applied.values(), key=lambda item: item["task_id"]):
            if decision["review_kind"] != "reference_identity_review" or decision["action"] != "keep_ambiguous":
                continue
            ref = self._conn.execute(
                "SELECT ref_id,ref_number,title,doi FROM reference_entries WHERE ref_id=?",
                (decision["ref_id"],),
            ).fetchone()
            if ref is None:
                raise RuntimeError("manual Parse identity target is unavailable")
            rows.append({
                "subject_type": "reference_identity",
                "subject_id": ref["ref_id"], "ref_number": ref["ref_number"],
                "status": "identity_ambiguous", "action": "keep_ambiguous",
                "source_count": None, "manual_review_required": False,
                "task_id": decision["task_id"], "answer_id": decision["answer_id"],
                "target_sha256": decision["target_sha256"], "reason": decision["reason"],
                "actor_type": decision["actor_type"],
                "producer_class": decision["producer_class"],
                "producer_identity": decision["producer_identity"],
                "ingress_kind": decision["ingress_kind"],
                "authority_id": decision["authority_id"],
                "submitted_at": decision["submitted_at"], "applied_at": decision["applied_at"],
                "original_title": ref["title"], "original_doi": ref["doi"],
                "effective_title": ref["title"], "effective_doi": ref["doi"],
            })
        for override in self._conn.execute("""
            SELECT o.*, d.target_sha256,x.reason,a.actor_type,a.submitted_at,
                   q.producer_class,q.producer_identity,q.ingress_kind,q.authority_id
            FROM manual_citation_attribution_overrides o
            JOIN tasks t ON t.task_id=o.task_id
            JOIN task_manual_parse_review_details d ON d.task_id=t.task_id
            JOIN task_answers a ON a.answer_id=o.answer_id
            JOIN task_manual_parse_review_answers x ON x.answer_id=a.answer_id
            LEFT JOIN task_answer_provenance q ON q.answer_id=a.answer_id
            ORDER BY o.task_id
        """):
            forward_claim = self._conn.execute("SELECT claim_id FROM unresolved_citations WHERE occurrence_id=?", (override["occurrence_id"],)).fetchone() if override["occurrence_id"] else None
            rows.append({"subject_type": "citation_attribution", "subject_id": override["occurrence_id"] or override["ref_id"], "claim_id": forward_claim["claim_id"] if forward_claim else override["claim_id"], "ref_id": override["ref_id"], "status": "attribution_overridden", "action": "select_reference" if override["direction"] == "citation_to_reference" else "select_claim", "task_id": override["task_id"], "answer_id": override["answer_id"], "target_sha256": override["target_sha256"], "reason": override["reason"], "actor_type": override["actor_type"], "producer_class": override["producer_class"], "producer_identity": override["producer_identity"], "ingress_kind": override["ingress_kind"], "authority_id": override["authority_id"], "submitted_at": override["submitted_at"], "applied_at": override["applied_at"], "direction": override["direction"], "candidate_origin": override["candidate_origin"], "candidate_score": override["candidate_score"]})
        for decision in applied.values():
            if decision["review_kind"] not in {"citation_reference_review", "reference_claim_review"} or decision["action"] != "keep_unresolved":
                continue
            task = self._conn.execute("SELECT claim_id,ref_id,scope FROM tasks WHERE task_id=?", (decision["task_id"],)).fetchone()
            rows.append({"subject_type": "citation_attribution", "subject_id": task["scope"] or task["ref_id"], "claim_id": task["claim_id"], "ref_id": task["ref_id"], "status": "kept_unresolved", "action": "keep_unresolved", "task_id": decision["task_id"], "answer_id": decision["answer_id"], "target_sha256": decision["target_sha256"], "reason": decision["reason"], "actor_type": decision["actor_type"], "producer_class": decision["producer_class"], "producer_identity": decision["producer_identity"], "ingress_kind": decision["ingress_kind"], "authority_id": decision["authority_id"], "submitted_at": decision["submitted_at"], "applied_at": decision["applied_at"]})
        return rows

    def _replace_footnote_provenance(self, notes, sources, claim_footnotes, parents=None) -> None:
        parents = [] if parents is None else parents
        if not all(type(rows) is list for rows in (notes, sources, claim_footnotes, parents)):
            raise ValueError("footnote provenance collections must be lists")
        note_ids = set()
        for note in notes:
            if type(note) is not dict or set(note) != {
                "note_id", "manuscript_id", "note_number", "raw_note",
                "extraction_status",
            }:
                raise ValueError("footnote note has invalid shape")
            note_id = _validate_identifier("note_id", str(note["note_id"]))
            if note_id in note_ids or type(note["note_number"]) is not int or note["note_number"] <= 0:
                raise ValueError("footnote note is invalid")
            if note["extraction_status"] not in {"no_sources", "sources_extracted", "ambiguous"}:
                raise ValueError("footnote extraction status is invalid")
            raw_note = note["raw_note"]
            if not isinstance(raw_note, str) or not raw_note or "\0" in raw_note:
                raise ValueError("footnote raw note is invalid")
            manuscript_id = note["manuscript_id"]
            if (
                not isinstance(manuscript_id, str)
                or not manuscript_id
                or "\0" in manuscript_id
            ):
                raise ValueError("footnote manuscript id is invalid")
            note_ids.add(note_id)
            self._conn.execute(
                "INSERT INTO footnote_notes VALUES(?,?,?,?,?)",
                (note_id, manuscript_id, note["note_number"], raw_note,
                 note["extraction_status"]),
            )
        ref_raw = {
            row["ref_id"]: row["raw_entry"]
            for row in self._conn.execute(
                "SELECT ref_id,raw_entry FROM reference_entries"
            )
        }
        by_note = {n["note_id"]: n for n in notes}
        orders = {}
        source_pairs = set()
        for source in sources:
            if type(source) is not dict or set(source) != {
                "note_id", "ref_id", "source_order", "raw_start", "raw_end",
            }:
                raise ValueError("footnote source has invalid shape")
            nid, rid = source["note_id"], source["ref_id"]
            if (
                nid not in note_ids
                or rid not in ref_raw
                or type(source["source_order"]) is not int
                or type(source["raw_start"]) is not int
                or type(source["raw_end"]) is not int
            ):
                raise ValueError("footnote source has invalid identifiers")
            if source["source_order"] < 0 or source["raw_start"] < 0 or source["raw_end"] <= source["raw_start"]:
                raise ValueError("footnote source span is invalid")
            note = by_note[nid]
            if ref_raw[rid] != note["raw_note"][source["raw_start"]:source["raw_end"]]:
                raise ValueError("footnote source span does not match reference raw_entry")
            if (nid, rid) in source_pairs:
                raise ValueError("footnote source is duplicated")
            source_pairs.add((nid, rid))
            orders.setdefault(nid, []).append(source["source_order"])
            self._conn.execute(
                "INSERT INTO footnote_note_sources VALUES(?,?,?,?,?)",
                (nid, rid, source["source_order"], source["raw_start"], source["raw_end"]),
            )
        for nid, note in by_note.items():
            actual = sorted(orders.get(nid, []))
            if (
                actual != list(range(len(actual)))
                or (note["extraction_status"] == "sources_extracted") != bool(actual)
            ):
                raise ValueError("footnote status/cardinality is inconsistent")
        for row in claim_footnotes:
            if type(row) is not dict or set(row) != {"claim_id", "note_id"}:
                raise ValueError("claim footnote has invalid shape")
            cid, nid = row["claim_id"], row["note_id"]
            if nid not in note_ids:
                raise ValueError("claim footnote has dangling note")
            if self._conn.execute("SELECT 1 FROM claims WHERE claim_id=?", (cid,)).fetchone() is None:
                raise ValueError("claim footnote has dangling claim")
            if self._conn.execute(
                "SELECT 1 FROM claim_marker_members WHERE claim_id=? AND marker_number=?",
                (cid, by_note[nid]["note_number"]),
            ).fetchone() is None:
                raise ValueError("claim footnote must have matching marker")
            self._conn.execute("INSERT INTO claim_footnotes VALUES(?,?)", (cid, nid))
        for row in parents:
            if type(row) is not dict or set(row) != {"note_id", "ref_id"} or row["note_id"] not in note_ids or row["ref_id"] not in ref_raw:
                raise ValueError("footnote parent has invalid shape")
            self._conn.execute("INSERT INTO footnote_note_parents VALUES(?,?)", (row["note_id"], row["ref_id"]))

    def _footnote_notes(self):
        return [dict(row) for row in self._conn.execute(
            "SELECT note_id,manuscript_id,note_number,raw_note,extraction_status "
            "FROM footnote_notes ORDER BY manuscript_id,note_number"
        )]

    def _footnote_note_sources(self):
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT note_id,ref_id,source_order,raw_start,raw_end "
                "FROM footnote_note_sources ORDER BY note_id,source_order"
            )
        ]

    def _manual_footnote_source_overrides(self) -> dict[str, JsonDict]:
        return {
            row["note_id"]: dict(row)
            for row in self._conn.execute(
                "SELECT note_id,action,original_raw_note_sha256,answer_id,applied_at "
                "FROM manual_footnote_source_overrides ORDER BY note_id"
            )
        }

    def _manual_footnote_override_sources(self) -> list[JsonDict]:
        return [
            dict(row)
            for row in self._conn.execute(
                """
                SELECT s.note_id,s.source_ref_id AS ref_id,o.ref_number,s.source_order,
                       s.source_text,s.raw_start,s.raw_end
                FROM manual_footnote_source_override_sources s
                JOIN operational_references o ON o.ref_id=s.source_ref_id
                JOIN footnote_notes n ON n.note_id=s.note_id
                ORDER BY n.manuscript_id,n.note_number,n.note_id,s.source_order
                """
            )
        ]

    def _claim_footnotes(self):
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT claim_id,note_id FROM claim_footnotes ORDER BY claim_id,note_id"
            )
        ]

    def get_manuscript_text(self) -> str | None:
        row = self._conn.execute(
            "SELECT content,sha256,char_count FROM manuscript_text WHERE singleton=1"
        ).fetchone()
        if row is None:
            return None
        content = row["content"]
        if (
            not isinstance(content, str)
            or len(content) != row["char_count"]
            or hashlib.sha256(content.encode("utf-8")).hexdigest() != row["sha256"]
        ):
            raise RuntimeError("persisted manuscript text is internally inconsistent")
        return content

    @staticmethod
    def _manuscript_title(value: Any, field: str) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value.strip() or "\x00" in value:
            raise ValueError(f"{field} must be nonempty NUL-free text or null")
        cleaned = " ".join(value.split())
        if len(cleaned) > 500:
            raise ValueError(f"{field} is too long")
        return cleaned

    def _replace_manuscript_identity(self, payload: JsonDict) -> None:
        expected = {
            "title", "title_status", "title_method", "metadata_title",
            "layout_title", "identifiers",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise ValueError("manuscript identity evidence has invalid shape")
        local_status = payload["title_status"]
        if local_status not in {"validated_local", "inferred_local", "unknown"}:
            raise ValueError("manuscript local title status is invalid")
        local_title = self._manuscript_title(payload["title"], "manuscript local title")
        if (local_status == "unknown") != (local_title is None):
            raise ValueError("manuscript local title status does not match its title")
        local_method = payload["title_method"]
        if type(local_method) is not str or not local_method.strip() or "\x00" in local_method:
            raise ValueError("manuscript local title method is invalid")
        metadata_title = self._manuscript_title(
            payload["metadata_title"], "manuscript metadata title"
        )
        layout_title = self._manuscript_title(
            payload["layout_title"], "manuscript layout title"
        )
        identifiers = payload["identifiers"]
        if type(identifiers) is not list:
            raise ValueError("manuscript identifiers must be a list")
        normalized_identifiers = []
        seen = set()
        for identifier in identifiers:
            if type(identifier) is not dict or set(identifier) != {"scheme", "value", "source"}:
                raise ValueError("manuscript identifier has invalid shape")
            scheme = identifier["scheme"]
            value = identifier["value"]
            source = identifier["source"]
            if scheme not in {"doi", "pmid", "isbn", "url", "arxiv_id"}:
                raise ValueError("manuscript identifier scheme is invalid")
            if (
                type(value) is not str or not value.strip() or "\x00" in value
                or type(source) is not str or not source.strip() or "\x00" in source
            ):
                raise ValueError("manuscript identifier value or source is invalid")
            key = (scheme, value.strip())
            if key in seen:
                raise ValueError("manuscript identifier is duplicated")
            seen.add(key)
            normalized_identifiers.append((scheme, value.strip(), source.strip()))
        run = self.get_run()
        final_status = local_status
        self._conn.execute(
            """
            INSERT INTO manuscript_identity(
              singleton,input_sha256,local_title,local_status,local_method,
              metadata_title,layout_title,resolution_status,final_title,
              final_status,final_method
            ) VALUES(1,?,?,?,?,?,?,'not_attempted',?,?,?)
            """,
            (
                run.input_sha256, local_title, local_status, local_method.strip(),
                metadata_title, layout_title, local_title, final_status,
                local_method.strip(),
            ),
        )
        self._conn.executemany(
            """
            INSERT INTO manuscript_identity_identifiers(
              identifier_order,scheme,value,source
            ) VALUES(?,?,?,?)
            """,
            (
                (order, scheme, value, source)
                for order, (scheme, value, source) in enumerate(normalized_identifiers)
            ),
        )

    def manuscript_identity_payload(self) -> JsonDict | None:
        row = self._conn.execute(
            "SELECT * FROM manuscript_identity WHERE singleton=1"
        ).fetchone()
        if row is None:
            return None
        run = self.get_run()
        if row["input_sha256"] != run.input_sha256:
            raise RuntimeError("manuscript identity is bound to a different input")
        identifiers = [
            {
                "scheme": item["scheme"],
                "value": item["value"],
                "source": item["source"],
            }
            for item in self._conn.execute(
                """
                SELECT identifier_order,scheme,value,source
                FROM manuscript_identity_identifiers ORDER BY identifier_order
                """
            )
        ]
        attempt_state = row["resolution_attempt_state"]
        attempts_json = row["resolution_attempts_json"]
        if attempt_state == "produced":
            try:
                attempts = json.loads(attempts_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("manuscript identity attempts are invalid") from exc
            _validate_resolve_attempts(attempts)
            canonical_attempts = json.dumps(
                attempts, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
            if canonical_attempts != attempts_json:
                raise RuntimeError("manuscript identity attempts are noncanonical")
        elif attempt_state == "attempts_not_produced" and attempts_json is None:
            attempts = None
        else:
            raise RuntimeError("manuscript identity attempt state is invalid")
        return {
            "input_sha256": row["input_sha256"],
            "local_title": row["local_title"],
            "local_status": row["local_status"],
            "local_method": row["local_method"],
            "metadata_title": row["metadata_title"],
            "layout_title": row["layout_title"],
            "identifiers": identifiers,
            "selected_identifier": (
                {
                    "scheme": row["selected_identifier_scheme"],
                    "value": row["selected_identifier_value"],
                }
                if row["selected_identifier_scheme"] is not None else None
            ),
            "resolution_status": row["resolution_status"],
            "resolved_identifier": (
                {
                    "scheme": row["resolved_identifier_scheme"],
                    "value": row["resolved_identifier_value"],
                }
                if row["resolved_identifier_scheme"] is not None else None
            ),
            "resolved_title": row["resolved_title"],
            "resolved_via": row["resolved_via"],
            "final_title": row["final_title"],
            "final_status": row["final_status"],
            "final_method": row["final_method"],
            "reason": row["reason"],
            "resolved_at": row["resolved_at"],
            "resolution_attempt_state": attempt_state,
            "attempts": attempts,
        }

    def set_manuscript_identity_resolution(self, payload: JsonDict) -> None:
        expected = {
            "selected_identifier", "resolution_status", "resolved_identifier",
            "resolved_title", "resolved_via", "final_title", "final_status",
            "final_method", "reason", "attempts",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise ValueError("manuscript identity resolution has invalid shape")
        identity_payload = self.manuscript_identity_payload()
        if identity_payload is None:
            raise RuntimeError("manuscript local identity evidence is missing")

        def identifier(value: Any, field: str) -> tuple[str | None, str | None]:
            if value is None:
                return None, None
            if type(value) is not dict or set(value) != {"scheme", "value"}:
                raise ValueError(f"{field} has invalid shape")
            scheme, raw = value["scheme"], value["value"]
            if scheme not in {"doi", "pmid", "isbn", "url", "arxiv_id"}:
                raise ValueError(f"{field} scheme is invalid")
            if type(raw) is not str or not raw.strip() or "\x00" in raw:
                raise ValueError(f"{field} value is invalid")
            return scheme, raw.strip()

        selected_scheme, selected_value = identifier(
            payload["selected_identifier"], "selected manuscript identifier"
        )
        if selected_scheme is None or selected_value is None:
            raise ValueError("selected manuscript identifier is required")
        declared = {
            (item["scheme"], item["value"])
            for item in identity_payload["identifiers"]
        }
        if (selected_scheme, selected_value) not in declared:
            raise ValueError("selected manuscript identifier was not declared by Parse")
        resolved_scheme, resolved_value = identifier(
            payload["resolved_identifier"], "resolved manuscript identifier"
        )
        resolution_status = payload["resolution_status"]
        if resolution_status not in {"resolved", "not_found", "unresolved", "error", "conflict"}:
            raise ValueError("manuscript resolution status is invalid")
        resolved_title = self._manuscript_title(
            payload["resolved_title"], "resolved manuscript title"
        )
        final_title = self._manuscript_title(payload["final_title"], "final manuscript title")
        final_status = payload["final_status"]
        if final_status not in {
            "validated_identifier", "validated_local", "inferred_local", "unknown"
        }:
            raise ValueError("final manuscript title status is invalid")
        if (final_status == "unknown") != (final_title is None):
            raise ValueError("final manuscript title status does not match its title")
        for field in ("resolved_via", "final_method", "reason"):
            value = payload[field]
            if value is not None and (
                type(value) is not str or not value.strip() or "\x00" in value
            ):
                raise ValueError(f"{field} is invalid")
        if type(payload["final_method"]) is not str:
            raise ValueError("final_method is required")
        attempts = payload["attempts"]
        _validate_resolve_attempts(attempts)
        attempts_json = (
            json.dumps(
                attempts, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
            if attempts is not None else None
        )
        attempt_state = "produced" if attempts is not None else "attempts_not_produced"
        with self._conn:
            self._conn.execute(
                """
                UPDATE manuscript_identity SET
                  selected_identifier_scheme=?,selected_identifier_value=?,
                  resolution_status=?,resolved_identifier_scheme=?,
                  resolved_identifier_value=?,resolved_title=?,resolved_via=?,
                  resolution_attempt_state=?,resolution_attempts_json=?,
                  final_title=?,final_status=?,final_method=?,reason=?,resolved_at=?
                WHERE singleton=1
                """,
                (
                    selected_scheme, selected_value, resolution_status,
                    resolved_scheme, resolved_value, resolved_title,
                    payload["resolved_via"].strip() if payload["resolved_via"] else None,
                    attempt_state, attempts_json,
                    final_title, final_status, payload["final_method"].strip(),
                    payload["reason"].strip() if payload["reason"] else None,
                    _now(),
                ),
            )

    def _replace_parse_link_silent_citations(self, markers: list[JsonDict]) -> None:
        if type(markers) is not list:
            raise ValueError("parse link-silent citations must be a list")
        refs = {
            row["ref_id"]: row["ref_number"]
            for row in self._conn.execute(
                "SELECT ref_id,ref_number FROM reference_entries"
            )
        }
        normalized = []
        expected_keys = {
            "claim_id", "ref_id", "ref_number", "provenance", "marker_raw",
        }
        for marker in markers:
            if type(marker) is not dict or set(marker) != expected_keys:
                raise ValueError("parse link-silent citation marker has invalid shape")
            if marker.get("claim_id") is not None:
                raise ValueError("parse link-silent citation marker must be claimless")
            ref_id = marker.get("ref_id")
            if type(ref_id) is not str or ref_id not in refs:
                raise ValueError(
                    "parse link-silent citation marker references an unknown reference"
                )
            if marker.get("ref_number") != refs[ref_id]:
                raise ValueError(
                    "parse link-silent citation marker reference metadata mismatch"
                )
            if marker.get("provenance") != "link_silent_resolved":
                raise ValueError("parse link-silent citation provenance is invalid")
            raw = marker.get("marker_raw")
            if type(raw) is not str or not raw or "\0" in raw:
                raise ValueError(
                    "parse link-silent citation marker_raw must be nonempty NUL-free text"
                )
            normalized.append((ref_id, raw, marker["provenance"]))
        self._conn.execute("DELETE FROM parse_link_silent_citation_markers")
        self._conn.execute(
            "UPDATE parse_link_silent_citation_state "
            "SET present=1,marker_count=? WHERE singleton=1",
            (len(normalized),),
        )
        self._conn.executemany(
            "INSERT INTO parse_link_silent_citation_markers"
            "(marker_order,ref_id,marker_raw,provenance) VALUES(?,?,?,?)",
            (
                (order, ref_id, raw, provenance)
                for order, (ref_id, raw, provenance) in enumerate(normalized)
            ),
        )

    def _parse_link_silent_citations(self) -> list[JsonDict]:
        state = self._conn.execute(
            "SELECT present,marker_count FROM parse_link_silent_citation_state "
            "WHERE singleton=1"
        ).fetchone()
        if state is None:
            raise RuntimeError("parse link-silent citation state is missing")
        rows = self._conn.execute(
            """
            SELECT m.marker_order,m.marker_raw,m.provenance,
                   r.ref_id,r.ref_number
            FROM parse_link_silent_citation_markers m
            JOIN reference_entries r ON r.ref_id=m.ref_id
            ORDER BY m.marker_order
            """
        ).fetchall()
        if (
            (not state["present"] and (state["marker_count"] != 0 or rows))
            or len(rows) != state["marker_count"]
            or [row["marker_order"] for row in rows] != list(range(len(rows)))
            or any(
                type(row["marker_raw"]) is not str
                or not row["marker_raw"]
                or "\0" in row["marker_raw"]
                or row["provenance"] != "link_silent_resolved"
                for row in rows
            )
        ):
            raise RuntimeError("parse link-silent citation storage is inconsistent")
        if not state["present"]:
            return []
        return [
            {
                "ref_id": row["ref_id"],
                "ref_number": row["ref_number"],
                "claim_id": None,
                "provenance": row["provenance"],
                "marker_raw": row["marker_raw"],
            }
            for row in rows
        ]

    def _replace_parse_table_citations(self, markers: list[JsonDict]) -> None:
        if type(markers) is not list:
            raise ValueError("parse_table_citations must be a list")
        refs = {
            row["ref_id"]: row["ref_number"]
            for row in self._conn.execute("SELECT ref_id,ref_number FROM reference_entries")
        }
        normalized = []
        for marker in markers:
            if type(marker) is not dict or set(marker) != {"ref_id", "ref_number", "marker_raw", "raw_entry"}:
                raise ValueError("parse table citation marker has invalid shape")
            ref_id = marker.get("ref_id")
            if type(ref_id) is not str or ref_id not in refs:
                raise ValueError("parse table citation marker references an unknown reference")
            if marker.get("ref_number") != refs[ref_id]:
                raise ValueError("parse table citation marker reference metadata mismatch")
            raw = marker.get("marker_raw")
            if type(raw) is not str or not raw or "\0" in raw:
                raise ValueError("parse table citation marker_raw must be nonempty NUL-free text")
            if marker.get("raw_entry") != self._conn.execute("SELECT raw_entry FROM reference_entries WHERE ref_id=?", (ref_id,)).fetchone()[0][:160]:
                raise ValueError("parse table citation marker raw_entry mismatch")
            normalized.append((ref_id, raw))
        self._conn.execute("DELETE FROM parse_table_citation_markers")
        self._conn.execute(
            "UPDATE parse_table_citation_state SET present=1,marker_count=? WHERE singleton=1",
            (len(normalized),),
        )
        self._conn.executemany(
            "INSERT INTO parse_table_citation_markers(marker_order,ref_id,marker_raw) VALUES(?,?,?)",
            ((order, ref_id, raw) for order, (ref_id, raw) in enumerate(normalized)),
        )

    def parse_table_citations(self) -> list[JsonDict]:
        state = self._conn.execute(
            "SELECT present,marker_count FROM parse_table_citation_state WHERE singleton=1"
        ).fetchone()
        if state is None:
            raise RuntimeError("parse table citation state is missing")
        rows = self._conn.execute("""
            SELECT m.marker_order,m.marker_raw,r.ref_id,r.ref_number,r.raw_entry
            FROM parse_table_citation_markers m JOIN reference_entries r ON r.ref_id=m.ref_id
            ORDER BY m.marker_order
        """).fetchall()
        if (not state["present"] and (state["marker_count"] != 0 or rows)) or len(rows) != state["marker_count"] or [row["marker_order"] for row in rows] != list(range(len(rows))) or any(type(row["marker_raw"]) is not str or not row["marker_raw"] or "\0" in row["marker_raw"] for row in rows):
            raise RuntimeError("parse table citation storage is inconsistent")
        if not state["present"]:
            return []
        return [{"ref_id": row["ref_id"], "ref_number": row["ref_number"],
                 "marker_raw": row["marker_raw"], "raw_entry": row["raw_entry"][:160]}
                for row in rows]

    def parse_coverage(self) -> JsonDict:
        state = self._conn.execute("SELECT present FROM parse_table_citation_state WHERE singleton=1").fetchone()
        if state is None:
            raise RuntimeError("parse table citation state is missing")
        if not state["present"]:
            return {}
        references = self.list_references()
        parent_ids = {
            row["ref_id"] for row in self._conn.execute(
                "SELECT ref_id FROM footnote_note_parents"
            )
        }
        projected_child_ids = {
            row["ref_id"] for row in self._conn.execute(
                "SELECT ref_id FROM footnote_note_sources"
            )
        } - parent_ids
        prose = {row.ref_id for row in self.list_citations()} | {
            row["ref_id"] for row in self._parse_link_silent_citations()
        }
        tables = {row["ref_id"] for row in self.parse_table_citations()}
        cited = prose | tables
        further = {row[0] for row in self._conn.execute("SELECT ref_id FROM parse_coverage_further_reading")}
        counted = [row for row in references
                   if row.ref_id not in projected_child_ids
                   and (row.ref_id not in further or row.ref_id in cited)]
        total = len(counted)
        pct = round(100.0 * len(cited) / total, 1) if total else 0.0
        out = {"cited": len(cited), "total": total, "pct": pct,
               "in_prose": len(prose), "in_tables": len(tables - prose),
               "uncited": [row.ref_number for row in counted if row.ref_id not in cited],
               "warning": None, "fatal": False}
        excluded = [row.ref_number for row in references if row.ref_id in further and row.ref_id not in cited]
        if excluded:
            out["further_reading"] = excluded
        table_only_count = len(tables - prose)
        if table_only_count >= 3 and (not prose or table_only_count * 4 > len(prose)):
            out["table_warning"] = (
                f"{table_only_count} references appear only in table-like regions versus "
                f"{len(prose)} in prose; inspect structural table suppression")
        source = self._conn.execute("SELECT superscript_source_kind,superscript_source,further_reading_count FROM parse_coverage_state WHERE singleton=1").fetchone()
        if source is None:
            raise RuntimeError("parse coverage state is missing")
        if source["further_reading_count"] != len(further):
            raise RuntimeError("parse coverage storage is inconsistent")
        if source["superscript_source_kind"] not in {"absent", "null", "text"} or (
            source["superscript_source_kind"] == "text" and (
                type(source["superscript_source"]) is not str or not source["superscript_source"] or "\0" in source["superscript_source"]
            )
        ) or (source["superscript_source_kind"] != "text" and source["superscript_source"] is not None):
            raise RuntimeError("parse coverage storage is inconsistent")
        if total >= _MIN_COVERAGE_REFS and pct < _MIN_COVERAGE_PCT:
            blind = source[0] == "null"
            cause = ("this format cannot represent a superscript, so a paper citing by superscript arrives with its markers already gone" if blind else "the body's citation markers may not have been recognised")
            out["warning"] = f"only {len(cited)} of {total} references are ever cited ({pct:.0f}%) — citation markers were probably lost: {cause}"
            out["fatal"] = blind
        return out

    def _replace_parse_coverage_inputs(self, coverage: JsonDict, extract: JsonDict) -> None:
        further = coverage.get("further_reading", [])
        if type(further) is not list or any(type(number) is not int for number in further):
            raise ValueError("parse coverage further_reading is invalid")
        refs = {row["ref_number"]: row["ref_id"] for row in self._conn.execute("SELECT ref_id,ref_number FROM reference_entries")}
        if len(set(further)) != len(further) or any(number not in refs for number in further):
            raise ValueError("parse coverage further_reading references an unknown reference")
        if type(extract) is not dict:
            raise ValueError("parse extract is invalid")
        value = extract.get("superscript_source")
        kind = "absent" if "superscript_source" not in extract else ("null" if value is None else "text")
        if kind == "text" and (type(value) is not str or not value or "\0" in value):
            raise ValueError("parse superscript source is invalid")
        self._conn.execute("DELETE FROM parse_coverage_further_reading")
        self._conn.execute("UPDATE parse_coverage_state SET superscript_source_kind=?,superscript_source=?,further_reading_count=? WHERE singleton=1", (kind, value if kind == "text" else None, len(further)))
        self._conn.executemany("INSERT INTO parse_coverage_further_reading(ref_id) VALUES(?)", ((refs[number],) for number in further))

    def list_claims(self) -> list[ClaimRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM claims
            ORDER BY claim_order, claim_id
            """
        ).fetchall()
        return [_claim_record(row) for row in rows]

    def _claim_marker_numbers(self, claim_id: str) -> list[object]:
        rows = self._conn.execute(
            """
            SELECT marker_number FROM claim_marker_members
            WHERE claim_id = ? ORDER BY member_order
            """,
            (claim_id,),
        ).fetchall()
        return [row["marker_number"] for row in rows]

    def list_references(
        self, *, offset: int = 0, limit: int | None = None,
    ) -> list[ReferenceRecord]:
        """Return Parse references in stable order, optionally as a DB page."""
        if type(offset) is not int or not 0 <= offset <= (1 << 63) - 1:
            raise ValueError("reference offset must be a non-negative SQLite integer")
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("reference limit must be a positive integer")
        if limit is None and offset == 0:
            rows = self._conn.execute(
                """
                SELECT * FROM reference_entries
                ORDER BY ref_number, ref_id
                """
            ).fetchall()
        elif limit is None:
            rows = self._conn.execute(
                """
                SELECT * FROM reference_entries
                ORDER BY ref_number, ref_id
                LIMIT -1 OFFSET ?
                """,
                (offset,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM reference_entries
                ORDER BY ref_number, ref_id
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [_reference_record(self._conn, row) for row in rows]

    def count_references(self) -> int:
        """Count Parse references without materializing their records."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM reference_entries"
        ).fetchone()
        return int(row[0])

    def list_citations(self) -> list[CitationRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM citations
            ORDER BY citation_id
            """
        ).fetchall()
        return [_citation_record(row) for row in rows]

    def get_reference(self, ref_id: str) -> ReferenceRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM reference_entries
            WHERE ref_id = ?
            """,
            (ref_id,),
        ).fetchone()
        return None if row is None else _reference_record(self._conn, row)

    def upsert_reference_identity(
        self,
        ref_id: str,
        *,
        identity_key: str,
        identity_scheme: str,
        canonical_doi: str | None = None,
        canonical_pmid: str | None = None,
        canonical_isbn: str | None = None,
        canonical_url: str | None = None,
        normalized_title: str | None = None,
        normalized_author_year: str | None = None,
        identity_status: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO reference_identity(
                  ref_id, identity_key, identity_scheme, canonical_doi, canonical_pmid,
                  canonical_isbn, canonical_url, normalized_title, normalized_author_year,
                  identity_status
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ref_id) DO UPDATE SET
                  identity_key = excluded.identity_key,
                  identity_scheme = excluded.identity_scheme,
                  canonical_doi = excluded.canonical_doi,
                  canonical_pmid = excluded.canonical_pmid,
                  canonical_isbn = excluded.canonical_isbn,
                  canonical_url = excluded.canonical_url,
                  normalized_title = excluded.normalized_title,
                  normalized_author_year = excluded.normalized_author_year,
                  identity_status = excluded.identity_status
                """,
                (
                    ref_id,
                    identity_key,
                    identity_scheme,
                    canonical_doi,
                    canonical_pmid,
                    canonical_isbn,
                    canonical_url,
                    normalized_title,
                    normalized_author_year,
                    identity_status,
                ),
            )
            self._conn.execute("UPDATE run SET updated_at = ?", (_now(),))

    def upsert_resolve_result(
        self,
        ref_id: str,
        payload: JsonDict,
        *,
        trace_state: str = "preserve",
    ) -> JsonDict | None:
        ref_id = _validate_identifier("ref_id", ref_id)
        if trace_state not in {"preserve", "produced", "trace_not_produced"}:
            raise ValueError("unknown resolution trace state")
        attempts = _sanitize_json_value(payload.get("attempts"))
        attempt_rows = _validate_resolve_attempts(attempts)
        trace_rows = None
        trace_sections = None
        if trace_state == "produced":
            if attempt_rows is None or "trace" not in payload:
                raise ValueError("produced resolution requires attempts and trace")
            trace_rows = _validate_resolution_trace(
                payload["trace"], expected_attempt_count=len(attempt_rows),
            )
            trace_sections = _split_resolution_trace(trace_rows)
        elif trace_state == "trace_not_produced":
            if "trace" in payload and payload["trace"] is not None:
                raise ValueError("trace-not-produced resolution contains a trace")
            trace_sections = {
                "trace": None,
                "resolver_attempts": None,
                "identifier_validations": None,
                "retraction_checks": None,
                "weak_corroboration_events": None,
            }
        fulltext_exists, link_sets = _validate_resolve_fulltext_payload(payload)
        evidence_profile = _validate_resolve_evidence_profile(payload.get("evidence_profile"))
        resolved_identifier = _validate_resolved_identifier(payload.get("resolved_identifier"))
        with self._write_transaction():
            self._conn.execute(
                """
                INSERT INTO resolve_results(
                  ref_id, status, via, matched_title, abstract, abstract_via, retracted,
                  fulltext_exists, oa_status, work_type, resolution_basis,
                  existence_confidence, reason, reference_status_tag, fabrication_risk,
                  resolved_identifier_type, resolved_identifier_value, resolved_identifier_validated_via,
                  updated_at, tag_reason
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ref_id) DO UPDATE SET
                  status = excluded.status,
                  via = excluded.via,
                  matched_title = excluded.matched_title,
                  abstract = excluded.abstract,
                  abstract_via = excluded.abstract_via,
                  retracted = excluded.retracted,
                  fulltext_exists = excluded.fulltext_exists,
                  oa_status = excluded.oa_status,
                  work_type = excluded.work_type,
                  resolution_basis = excluded.resolution_basis,
                  existence_confidence = excluded.existence_confidence,
                  reason = excluded.reason,
                  reference_status_tag = excluded.reference_status_tag,
                  fabrication_risk = excluded.fabrication_risk,
                  resolved_identifier_type = excluded.resolved_identifier_type,
                  resolved_identifier_value = excluded.resolved_identifier_value,
                  resolved_identifier_validated_via = excluded.resolved_identifier_validated_via,
                  updated_at = excluded.updated_at,
                  tag_reason = excluded.tag_reason
                """,
                (
                    ref_id,
                    _clean_text(payload["status"]) or "",
                    _clean_text(payload.get("via")),
                    _clean_text(payload.get("matched_title")),
                    _clean_text(payload.get("abstract")),
                    _clean_text(payload.get("abstract_via")),
                    1 if payload.get("retracted") else 0,
                    fulltext_exists,
                    _clean_text(payload.get("oa_status")),
                    _clean_text(payload.get("work_type")),
                    _clean_text(payload.get("resolution_basis")),
                    _clean_text(payload.get("existence_confidence")),
                    _clean_text(payload.get("reason")),
                    _clean_text(payload.get("reference_status_tag")),
                    _clean_text(payload.get("fabrication_risk")),
                    resolved_identifier["type"] if resolved_identifier is not None else None,
                    resolved_identifier["value"] if resolved_identifier is not None else None,
                    resolved_identifier.get("validated_via") if resolved_identifier is not None else None,
                    _clean_text(payload.get("checked_at")) or _now(),
                    _clean_text(payload.get("tag_reason")),
                ),
            )
            _replace_resolve_fulltext_links(self._conn, ref_id, link_sets)
            _replace_resolve_evidence_profile(self._conn, ref_id, evidence_profile)
            _replace_resolve_attempts(self._conn, ref_id, attempts)
            self._conn.execute(
                "INSERT OR IGNORE INTO resolve_trace_state(ref_id,state,stage_count) VALUES(?,?,0)",
                (ref_id, "trace_not_produced"),
            )
            if trace_state == "produced":
                _replace_resolution_trace(self._conn, ref_id, trace_rows)
            elif trace_state == "trace_not_produced":
                _replace_resolution_trace(self._conn, ref_id, None)
            self._conn.execute("UPDATE run SET updated_at = ?", (_now(),))
        return trace_sections

    def get_resolve_result(self, ref_id: str) -> ResolveResultRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM resolve_results
            WHERE ref_id = ?
            """,
            (ref_id,),
        ).fetchone()
        return None if row is None else _resolve_result_record(row, self._conn)

    def list_resolve_results(self) -> list[ResolveResultRecord]:
        rows = self._conn.execute(
            """
            SELECT rr.*
            FROM resolve_results rr
                JOIN operational_references re ON re.ref_id = rr.ref_id
            ORDER BY re.ref_number, rr.ref_id
            """
        ).fetchall()
        return [_resolve_result_record(row, self._conn) for row in rows]

    def resolve_payload_map(self) -> dict[str, JsonDict]:
        return {
            row.ref_id: _resolve_result_export_dict(row)
            for row in self.list_resolve_results()
        }

    def record_resolution_trace(self, ref_id: str, trace: list[JsonDict]) -> JsonDict:
        ref_id = _validate_identifier("ref_id", ref_id)
        attempts = _read_resolve_attempts(self._conn, ref_id)
        if type(attempts) is not list:
            raise ValueError("produced resolution trace requires an attempt list")
        rows = _validate_resolution_trace(
            trace, expected_attempt_count=len(attempts),
        )
        sections = _split_resolution_trace(rows)
        with self._write_transaction():
            _replace_resolution_trace(self._conn, ref_id, rows)
        return sections

    def record_resolution_trace_not_produced(self, ref_id: str) -> None:
        """Record intentional absence; never turn it into an empty timeline."""
        ref_id = _validate_identifier("ref_id", ref_id)
        with self._write_transaction():
            _replace_resolution_trace(self._conn, ref_id, None)

    def get_resolution_trace(self, ref_id: str) -> list[JsonDict] | None:
        row = self.get_resolve_result(ref_id)
        if row is None or not isinstance(row.trace, list):
            return None
        return list(row.trace)

    def list_resolver_attempts(self, ref_id: str) -> list[JsonDict]:
        row = self.get_resolve_result(ref_id)
        if row is None or not isinstance(row.resolver_attempts, list):
            return []
        return list(row.resolver_attempts)

    def list_identifier_validations(self, ref_id: str) -> list[JsonDict]:
        row = self.get_resolve_result(ref_id)
        if row is None or not isinstance(row.identifier_validations, list):
            return []
        return list(row.identifier_validations)

    def list_retraction_checks(self, ref_id: str) -> list[JsonDict]:
        row = self.get_resolve_result(ref_id)
        if row is None or not isinstance(row.retraction_checks, list):
            return []
        return list(row.retraction_checks)

    def list_weak_corroboration_events(self, ref_id: str) -> list[JsonDict]:
        row = self.get_resolve_result(ref_id)
        if row is None or not isinstance(row.weak_corroboration_events, list):
            return []
        return list(row.weak_corroboration_events)

    def append_fetch_attempt(
        self,
        ref_id: str,
        *,
        method: str | None,
        url: str | None,
        kind: str | None,
        final_url: str | None,
        status_code: int | None,
        content_type: str | None,
        outcome: str,
        reason: str | None = None,
        challenge_blocked: bool = False,
        paywalled: bool = False,
        trace: JsonDict | None = None,
        origin: str | None = None,
    ) -> int:
        with self._write_transaction():
            cur = self._conn.execute(
                """
                INSERT INTO fetch_attempts(
                  ref_id, method, url, kind, final_url, status_code, content_type,
                  outcome, reason, challenge_blocked, paywalled, origin, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref_id,
                    method,
                    url,
                    kind,
                    final_url,
                    status_code,
                    content_type,
                    outcome,
                    reason,
                    1 if challenge_blocked else 0,
                    1 if paywalled else 0,
                    origin,
                    _now(),
                ),
            )
            replace_fetch_trace(self._conn, int(cur.lastrowid), trace)
            self._conn.execute("UPDATE run SET updated_at = ?", (_now(),))
        return int(cur.lastrowid)

    def list_fetch_attempts(self, ref_id: str) -> list[JsonDict]:
        rows = self._conn.execute(
            """
            SELECT *
            FROM fetch_attempts
            WHERE ref_id = ?
            ORDER BY fetch_attempt_id
            """,
            (ref_id,),
        ).fetchall()
        return [
            _fetch_attempt_dict(row, self._conn)
            for row in rows
        ]

    def append_integrity_unit_completion(
        self,
        unit_group: str,
        unit_id: str,
        payload: JsonDict,
    ) -> None:
        with self._write_transaction():
            _append_integrity_unit_completion(
                self._conn,
                unit_group=unit_group,
                unit_id=unit_id,
                payload=_sanitize_json_value(payload),
                completed_at=_now(),
            )
            self._conn.execute("UPDATE run SET updated_at = ?", (_now(),))

    def list_integrity_unit_completions(
        self, unit_group: str
    ) -> list[JsonDict]:
        return _list_integrity_unit_completions(self._conn, unit_group)

    def store_source_text(
        self,
        *,
        source_text_id: str,
        ref_id: str,
        identity_key: str,
        tier: str,
        origin: str,
        stored_path: str,
        sha256: str,
        char_count: int,
        source_ref: str | None = None,
        mapping: str | None = None,
        match_signal: str | None = None,
        match_score: float | None = None,
        identity_status: str | None = None,
        identity_note: str | None = None,
        content_version: str | None = None,
        provenance_relation: str | None = None,
        supplied_by: str | None = None,
        supplied_via: str | None = None,
        file_format: str | None = None,
        extraction_flags: list[str] | None = None,
        extraction_method: str | None = None,
        ) -> None:
        source_text_id = _validate_identifier("source_text_id", source_text_id)
        ref_id = _validate_identifier("ref_id", ref_id)
        now = _now()
        with self._write_transaction():
            self._conn.execute(
                """
                DELETE FROM source_texts
                WHERE ref_id = ? AND tier = ? AND origin = ?
                """,
                (ref_id, tier, origin),
            )
            self._conn.execute(
                """
                INSERT INTO source_texts(
                  source_text_id, ref_id, identity_key, tier, origin, stored_path, sha256,
                  char_count, source_ref, mapping, match_signal, match_score,
                  identity_status, identity_note, content_version, provenance_relation,
                  supplied_by, supplied_via, file_format, extraction_method, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_text_id,
                    ref_id,
                    _clean_text(identity_key) or "",
                    _clean_text(tier) or "",
                    _clean_text(origin) or "",
                    _clean_text(stored_path) or "",
                    _clean_text(sha256) or "",
                    char_count,
                    _clean_text(source_ref),
                    _clean_text(mapping),
                    _clean_text(match_signal),
                    match_score,
                    _clean_text(identity_status),
                    _clean_text(identity_note),
                    _clean_text(content_version),
                    _clean_text(provenance_relation),
                    _clean_text(supplied_by),
                    _clean_text(supplied_via),
                    _clean_text(file_format),
                    _clean_text(extraction_method),
                    now,
                ),
            )
            replace_source_flags(self._conn, source_text_id, extraction_flags)
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))

    def prepare_source_text_materialization(self, *, source_text_id: str, ref_id: str,
        identity_key: str, tier: str, origin: str, stored_path: str, sha256: str,
        char_count: int, source_ref: str | None = None, mapping: str | None = None,
        match_signal: str | None = None, match_score: float | None = None,
        identity_status: str | None = None, identity_note: str | None = None,
        content_version: str | None = None, provenance_relation: str | None = None,
        supplied_by: str | None = None, supplied_via: str | None = None,
        file_format: str | None = None, extraction_flags: list[str] | None = None,
        extraction_method: str | None = None) -> str:
        """Persist the exact source row before publishing its run-local asset."""
        source_text_id = _validate_identifier("source_text_id", source_text_id)
        ref_id = _validate_identifier("ref_id", ref_id)
        if type(char_count) is not int or char_count < 0:
            raise ValueError("source text char_count must be a non-negative integer")
        sha256 = _clean_text(sha256) or ""
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("source text sha256 must be a lowercase SHA-256 hex digest")
        self._materialization_path(_clean_text(stored_path) or "")
        flags = normalize_source_flags(extraction_flags)
        intent_id = "intent-" + uuid.uuid4().hex
        now = _now()
        with self._write_transaction():
            self._conn.execute(
                """INSERT INTO source_text_materialization_intents(
                intent_id,source_text_id,ref_id,identity_key,tier,origin,stored_path,sha256,char_count,
                source_ref,mapping,match_signal,match_score,identity_status,identity_note,content_version,
                provenance_relation,supplied_by,supplied_via,file_format,extraction_method,prepared_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (intent_id, source_text_id, ref_id, _clean_text(identity_key) or "",
                 _clean_text(tier) or "", _clean_text(origin) or "", _clean_text(stored_path) or "",
                 sha256, char_count, _clean_text(source_ref), _clean_text(mapping), _clean_text(match_signal),
                 match_score, _clean_text(identity_status), _clean_text(identity_note), _clean_text(content_version),
                 _clean_text(provenance_relation), _clean_text(supplied_by), _clean_text(supplied_via),
                 _clean_text(file_format), _clean_text(extraction_method), now),
            )
            self._conn.execute(
                "INSERT INTO source_text_materialization_intent_flag_states(intent_id,is_null,flag_count) VALUES(?,?,?)",
                (intent_id, int(flags is None), 0 if flags is None else len(flags)),
            )
            for flag in flags or []:
                self._conn.execute(
                    "INSERT INTO source_text_materialization_intent_flags(intent_id,flag) VALUES(?,?)",
                    (intent_id, flag),
                )
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))
        return intent_id

    def reconcile_source_text_materializations(self) -> int:
        """Finalize only explicitly prepared source assets; never scan filenames."""
        rows = self._conn.execute(
            """SELECT i.* FROM source_text_materialization_intents i
            LEFT JOIN source_text_materialization_outcomes o ON o.intent_id=i.intent_id
            WHERE o.intent_id IS NULL ORDER BY i.prepared_at, i.intent_id"""
        ).fetchall()
        for row in rows:
            self._finalize_source_text_materialization(row)
        self._invalidate_invalid_materialized_source_texts()
        return len(rows)

    def finalize_source_text_materialization(self, intent_id: str) -> str:
        """Finalize one prepared asset and return its durable terminal outcome."""
        intent_id = _validate_identifier("materialization intent", intent_id)
        row = self._conn.execute(
            "SELECT * FROM source_text_materialization_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("source text materialization intent is missing")
        return self._finalize_source_text_materialization(row)

    def _finalize_source_text_materialization(self, row) -> str:
        with self._write_transaction():
            prior = self._conn.execute(
                "SELECT outcome FROM source_text_materialization_outcomes WHERE intent_id=?", (row["intent_id"],)
            ).fetchone()
            if prior is not None:
                return str(prior["outcome"])
            try:
                stored_path, absolute = self._materialization_path(str(row["stored_path"]))
            except RuntimeError:
                now = _now()
                self._conn.execute(
                    "INSERT INTO source_text_materialization_outcomes(intent_id,outcome,recorded_at) VALUES(?,?,?)",
                    (row["intent_id"], "file_invalid_path", now),
                )
                self._conn.execute("UPDATE run SET updated_at = ?", (now,))
                return "file_invalid_path"
            outcome, data = None, None
            if not os.path.lexists(absolute): outcome = "file_missing"
            elif os.path.islink(absolute) or not os.path.isfile(absolute): outcome = "file_not_regular"
            else:
                with open(absolute, "rb") as handle: data = handle.read()
                try: text = data.decode("utf-8")
                except UnicodeDecodeError: outcome = "file_not_utf8"
                else:
                    if hashlib.sha256(data).hexdigest() != row["sha256"]: outcome = "file_hash_mismatch"
                    elif len(text) != int(row["char_count"]): outcome = "char_count_mismatch"
            now = _now()
            if outcome is not None:
                existing = self._conn.execute(
                    "SELECT source_text_id,sha256,char_count FROM source_texts WHERE stored_path=?", (stored_path,)
                ).fetchone()
                valid_existing = False
                if existing is not None and data is not None:
                    try:
                        valid_existing = (hashlib.sha256(data).hexdigest() == existing["sha256"] and len(data.decode("utf-8")) == int(existing["char_count"]))
                    except UnicodeDecodeError: pass
                if existing is not None and not valid_existing:
                    self._conn.execute("DELETE FROM source_texts WHERE source_text_id=?", (existing["source_text_id"],))
                self._conn.execute(
                    "INSERT INTO source_text_materialization_outcomes(intent_id,outcome,recorded_at) VALUES(?,?,?)",
                    (row["intent_id"], outcome, now),
                )
                self._conn.execute("UPDATE run SET updated_at = ?", (now,))
                return outcome
            state = self._conn.execute(
                "SELECT is_null,flag_count FROM source_text_materialization_intent_flag_states WHERE intent_id=?", (row["intent_id"],)
            ).fetchone()
            if state is None:
                raise RuntimeError("materialization intent flag state is missing")
            flags = [item["flag"] for item in self._conn.execute(
                "SELECT flag FROM source_text_materialization_intent_flags WHERE intent_id=? ORDER BY flag", (row["intent_id"],)
            ).fetchall()]
            if len(flags) != int(state["flag_count"]):
                raise RuntimeError("materialization intent flag count mismatch")
            self._conn.execute("DELETE FROM source_texts WHERE ref_id=? AND tier=? AND origin=?", (row["ref_id"], row["tier"], row["origin"]))
            self._conn.execute(
                """INSERT INTO source_texts(source_text_id,ref_id,identity_key,tier,origin,stored_path,sha256,char_count,
                source_ref,mapping,match_signal,match_score,identity_status,identity_note,content_version,provenance_relation,
                supplied_by,supplied_via,file_format,extraction_method,recorded_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(row[name] for name in ("source_text_id", "ref_id", "identity_key", "tier", "origin", "stored_path", "sha256", "char_count", "source_ref", "mapping", "match_signal", "match_score", "identity_status", "identity_note", "content_version", "provenance_relation", "supplied_by", "supplied_via", "file_format", "extraction_method")) + (now,),
            )
            replace_source_flags(self._conn, row["source_text_id"], None if state["is_null"] else flags)
            self._conn.execute("INSERT INTO source_text_materialization_outcomes(intent_id,outcome,recorded_at) VALUES(?,?,?)", (row["intent_id"], "registered", now))
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))
            return "registered"

    def _invalidate_invalid_materialized_source_texts(self) -> None:
        """Remove only current materialized sources whose current asset is invalid."""
        rows = self._conn.execute(
            """SELECT st.* FROM source_texts st WHERE EXISTS (
              SELECT 1 FROM source_text_materialization_intents i
              JOIN source_text_materialization_outcomes o ON o.intent_id=i.intent_id
              WHERE o.outcome='registered' AND i.source_text_id=st.source_text_id
                AND i.ref_id=st.ref_id AND i.tier=st.tier AND i.origin=st.origin
                AND i.stored_path=st.stored_path AND i.sha256=st.sha256
                AND i.char_count=st.char_count
            ) ORDER BY st.source_text_id"""
        ).fetchall()
        for row in rows:
            self._invalidate_materialized_source_text(row)

    def _invalidate_materialized_source_text(self, row) -> None:
        with self._write_transaction():
            current = self._conn.execute(
                "SELECT * FROM source_texts WHERE source_text_id=?", (row["source_text_id"],)
            ).fetchone()
            if current is None:
                return
            try:
                _stored_path, absolute = self._materialization_path(str(current["stored_path"]))
            except RuntimeError:
                reason, data = "file_invalid_path", None
            else:
                reason, data = None, None
                if not os.path.lexists(absolute): reason = "file_missing"
                elif os.path.islink(absolute) or not os.path.isfile(absolute): reason = "file_not_regular"
                else:
                    with open(absolute, "rb") as handle: data = handle.read()
                    try: text = data.decode("utf-8")
                    except UnicodeDecodeError: reason = "file_not_utf8"
                    else:
                        if hashlib.sha256(data).hexdigest() != current["sha256"]: reason = "file_hash_mismatch"
                        elif len(text) != int(current["char_count"]): reason = "char_count_mismatch"
            if reason is None:
                return
            now = _now()
            self._conn.execute(
                """INSERT INTO source_text_materialization_invalidations(
                source_text_id,ref_id,tier,origin,stored_path,sha256,char_count,reason,recorded_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (current["source_text_id"], current["ref_id"], current["tier"], current["origin"],
                 current["stored_path"], current["sha256"], current["char_count"], reason, now),
            )
            self._conn.execute("DELETE FROM source_texts WHERE source_text_id=?", (current["source_text_id"],))
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))

    def _materialization_path(self, stored_path: str) -> tuple[str, str]:
        if not isinstance(stored_path, str) or "\\" in stored_path or re.match(r"^[A-Za-z]:", stored_path):
            raise RuntimeError("materialization intent path is not run-local")
        path = PurePosixPath(stored_path)
        if path.is_absolute() or len(path.parts) < 2 or path.parts[:1] != ("sources",) or any(part in (".", "..") for part in path.parts) or path.as_posix() != stored_path:
            raise RuntimeError("materialization intent path is not normalized")
        run_root = os.path.realpath(self._run_dir)
        absolute = os.path.realpath(os.path.join(run_root, stored_path))
        try:
            if os.path.commonpath((run_root, absolute)) != run_root:
                raise RuntimeError("materialization intent path escapes run directory")
        except ValueError as exc:
            raise RuntimeError("materialization intent path is invalid") from exc
        return stored_path, absolute

    def list_source_texts(self, ref_id: str | None = None) -> list[SourceTextRecord]:
        params: list[Any] = []
        where = ""
        if ref_id is not None:
            where = "WHERE st.ref_id = ?"
            params.append(ref_id)
        rows = self._conn.execute(
            f"""
            SELECT st.*
            FROM source_texts st
                JOIN operational_references re ON re.ref_id = st.ref_id
            {where}
            ORDER BY re.ref_number, {_tier_rank_sql('st.tier')} DESC, st.recorded_at DESC, st.source_text_id
            """,
            params,
        ).fetchall()
        return [
            _source_text_record(row, self._conn)
            for row in rows
        ]

    def list_source_texts_for_refs(
        self, ref_ids: list[str],
    ) -> list[SourceTextRecord]:
        """Return source records only for the supplied reference page."""
        ref_ids = _validated_ref_ids(ref_ids)
        if not ref_ids:
            return []
        placeholders = ",".join("?" for _ in ref_ids)
        rows = self._conn.execute(
            f"""
            SELECT st.*
            FROM source_texts st
                JOIN operational_references re ON re.ref_id = st.ref_id
            WHERE st.ref_id IN ({placeholders})
            ORDER BY re.ref_number, {_tier_rank_sql('st.tier')} DESC,
                     st.recorded_at DESC, st.source_text_id
            """,
            ref_ids,
        ).fetchall()
        return [_source_text_record(row, self._conn) for row in rows]

    def get_source_text(self, source_text_id: str) -> SourceTextRecord | None:
        source_text_id = _validate_identifier("source_text_id", source_text_id)
        row = self._conn.execute(
            "SELECT * FROM source_texts WHERE source_text_id = ?",
            (source_text_id,),
        ).fetchone()
        return (
            None
            if row is None
            else _source_text_record(row, self._conn)
        )

    def get_best_source_text(self, ref_id: str) -> SourceTextRecord | None:
        row = self._conn.execute(
            f"""
            SELECT st.*
            FROM source_texts st
            WHERE st.ref_id = ?
            ORDER BY {_tier_rank_sql('st.tier')} DESC, st.recorded_at DESC, st.source_text_id DESC
            LIMIT 1
            """,
            (ref_id,),
        ).fetchone()
        return (
            None
            if row is None
            else _source_text_record(row, self._conn)
        )

    def source_text_exists(self, ref_id: str, *, tier: str | None = None) -> bool:
        if tier is None:
            row = self._conn.execute(
                "SELECT 1 FROM source_texts WHERE ref_id = ? LIMIT 1",
                (ref_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM source_texts WHERE ref_id = ? AND tier = ? LIMIT 1",
                (ref_id, tier),
        ).fetchone()
        return row is not None

    def delete_source_texts(
        self,
        ref_id: str,
        *,
        tier: str | None = None,
        origin: str | None = None,
        source_ref: str | None = None,
    ) -> list[SourceTextRecord]:
        ref_id = _validate_identifier("ref_id", ref_id)
        clauses = ["ref_id = ?"]
        params: list[Any] = [ref_id]
        if tier is not None:
            clauses.append("tier = ?")
            params.append(_clean_text(tier) or "")
        if origin is not None:
            clauses.append("origin = ?")
            params.append(_clean_text(origin) or "")
        if source_ref is not None:
            clauses.append("source_ref = ?")
            params.append(_clean_text(source_ref) or "")
        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"SELECT * FROM source_texts WHERE {where} ORDER BY recorded_at DESC, source_text_id DESC",
            params,
        ).fetchall()
        deleted = [
            _source_text_record(row, self._conn)
            for row in rows
        ]
        if not deleted:
            return []
        now = _now()
        with self._write_transaction():
            self._conn.execute(f"DELETE FROM source_texts WHERE {where}", params)
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))
        return deleted

    def source_manifest_payload(self) -> JsonDict:
        ref_numbers = {
            row["ref_id"]: row["ref_number"]
            for row in self._conn.execute(
                "SELECT ref_id,ref_number FROM operational_references"
            )
        }
        entries = []
        for row in self.list_source_texts():
            entry = {
                "source_text_id": row.source_text_id,
                "ref_id": row.ref_id,
                "ref_number": ref_numbers.get(row.ref_id),
                "tier": row.tier,
                "origin": row.origin,
                "stored_as": row.stored_path.replace("\\", "/").removeprefix("sources/"),
                "source_ref": row.source_ref,
                "sha256": row.sha256,
                "char_count": row.char_count,
                "mapping": row.mapping,
                "match_signal": row.match_signal,
                "match_score": row.match_score,
                "recorded_at": row.recorded_at,
            }
            if row.identity_status is not None:
                entry["identity_status"] = row.identity_status
            if row.identity_note is not None:
                entry["identity_note"] = row.identity_note
            if row.content_version is not None:
                entry["content_version"] = row.content_version
            if row.provenance_relation is not None:
                entry["provenance_relation"] = row.provenance_relation
            if row.supplied_by is not None:
                entry["supplied_by"] = row.supplied_by
            if row.supplied_via is not None:
                entry["supplied_via"] = row.supplied_via
            if row.file_format is not None:
                entry["file_format"] = row.file_format
            if row.extraction_flags:
                entry["extraction_flags"] = row.extraction_flags
            if row.extraction_method is not None:
                entry["extraction_method"] = row.extraction_method
            entries.append(entry)
        return {"entries": entries}

    def park_unreadable_source(
        self,
        *,
        ref_id: str | None,
        ref_number: int | None,
        kept_path: str | None,
        source_ref: str | None,
        origin: str,
        reason: str,
    ) -> None:
        if ref_id is not None:
            ref_id = _validate_identifier("ref_id", ref_id)
        now = _now()
        with self._conn:
            if kept_path:
                self._conn.execute(
                    """
                    DELETE FROM unreadable_sources
                    WHERE kept_path = ?
                    """,
                    (kept_path,),
                )
            elif source_ref:
                self._conn.execute(
                    """
                    DELETE FROM unreadable_sources
                    WHERE kept_path IS NULL AND source_ref = ?
                    """,
                    (source_ref,),
                )
            self._conn.execute(
                """
                INSERT INTO unreadable_sources(
                  ref_id, ref_number, kept_path, source_ref, origin, reason,
                  ocr_status, recorded_at, resolved_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
                """,
                (
                    ref_id,
                    ref_number,
                    _clean_text(kept_path),
                    _clean_text(source_ref),
                    _clean_text(origin) or "",
                    _clean_text(reason) or "",
                    now,
                ),
            )
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))

    def resolve_unreadable_source(
        self,
        *,
        ref_id: str | None = None,
        kept_path: str | None = None,
        method: str | None = None,
    ) -> bool:
        if not ref_id and not kept_path:
            return False
        now = _now()
        if kept_path:
            rows = self._conn.execute(
                """
                SELECT unreadable_source_id
                FROM unreadable_sources
                WHERE ocr_status != 'done' AND kept_path = ?
                ORDER BY unreadable_source_id
                LIMIT 1
                """,
                (kept_path,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT unreadable_source_id
                FROM unreadable_sources
                WHERE ocr_status != 'done' AND ref_id = ?
                ORDER BY unreadable_source_id
                """,
                (ref_id,),
            ).fetchall()
        if not rows:
            return False
        with self._conn:
            for row in rows:
                self._conn.execute(
                    """
                    UPDATE unreadable_sources
                    SET ocr_status = 'done', ocr_method = ?, resolved_at = ?
                    WHERE unreadable_source_id = ?
                    """,
                    (method, now, int(row["unreadable_source_id"])),
                )
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))
        return True

    def unreadable_payload(self) -> JsonDict:
        rows = self._conn.execute(
            """
            SELECT *
            FROM unreadable_sources
            ORDER BY unreadable_source_id
            """
        ).fetchall()
        return {
            "entries": [
                {
                    "ref_id": row["ref_id"],
                    "ref_number": row["ref_number"],
                    "kept_as": row["kept_path"],
                    "source_ref": row["source_ref"],
                    "origin": row["origin"],
                    "reason": row["reason"],
                    "ocr_status": row["ocr_status"],
                    "ocr_method": row["ocr_method"],
                    "recorded_at": row["recorded_at"],
                    "resolved_at": row["resolved_at"],
                }
                for row in rows
            ]
        }

    def create_task(
        self,
        *,
        task_id: str,
        slot: str,
        ref_id: str | None = None,
        note_id: str | None = None,
        claim_id: str | None = None,
        scope: str | None = None,
        task_payload: JsonDict,
    ) -> None:
        task_id = _validate_identifier("task_id", task_id)
        if ref_id is not None:
            ref_id = _validate_identifier("ref_id", ref_id)
        if note_id is not None:
            note_id = _validate_identifier("note_id", note_id)
        if claim_id is not None:
            claim_id = _validate_identifier("claim_id", claim_id)
        now = _now()
        with self._write_transaction():
            existing = self._conn.execute(
                "SELECT task_id FROM tasks WHERE task_id=?", (task_id,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO tasks(
                      task_id, slot, ref_id, note_id, claim_id, scope, status,
                      created_at, answered_at, applied_at, task_kind, generation
                    ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, 'fetch', 0)
                    """,
                    (
                        task_id,
                        _clean_text(slot) or "",
                        ref_id,
                        note_id,
                        claim_id,
                        _clean_text(scope),
                        now,
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (task_id,),
                ).fetchone()
                normalized = normalize_task_payload(
                    self._conn, self._run_dir, row, task_payload,
                )
                replace_task_payload(self._conn, task_id, normalized)
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute(
            """
            SELECT *
            FROM tasks
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        return None if row is None else _task_record(
            row, self._conn, self._run_dir,
        )

    def update_task_payload(self, task_id: str, task_payload: JsonDict) -> None:
        task_id = _validate_identifier("task_id", task_id)
        now = _now()
        with self._write_transaction():
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"task not found: {task_id}")
            normalized = normalize_task_payload(
                self._conn, self._run_dir, row, task_payload,
            )
            if normalized["kind"] != row["task_kind"]:
                raise ValueError("task kind cannot change")
            replace_task_payload(self._conn, task_id, normalized)
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))

    def list_tasks(
        self,
        *,
        slot: str | None = None,
        status: str | None = None,
        ref_ids: list[str] | None = None,
    ) -> list[TaskRecord]:
        where = []
        params: list[Any] = []
        if slot is not None:
            where.append("t.slot = ?")
            params.append(slot)
        if status is not None:
            where.append("t.status = ?")
            params.append(status)
        if ref_ids is not None:
            ref_ids = _validated_ref_ids(ref_ids)
            if not ref_ids:
                return []
            placeholders = ",".join("?" for _ in ref_ids)
            where.append(
                "(t.ref_id IN (" + placeholders + ") OR ("
                "t.task_kind = 'browser_challenge' AND EXISTS ("
                "SELECT 1 FROM task_browser_challenge_references AS challenge_ref "
                "WHERE challenge_ref.task_id = t.task_id AND challenge_ref.ref_id IN ("
                + placeholders + "))))"
            )
            params.extend(ref_ids)
            params.extend(ref_ids)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._conn.execute(
            f"""
            SELECT t.*
            FROM tasks AS t
            {where_sql}
            ORDER BY t.created_at, t.task_id
            """,
            params,
        ).fetchall()
        return [
            _task_record(row, self._conn, self._run_dir)
            for row in rows
        ]

    def list_pending_tasks(
        self, *, slot: str | None = None, ref_ids: list[str] | None = None,
    ) -> list[TaskRecord]:
        return self.list_tasks(slot=slot, status="pending", ref_ids=ref_ids)

    def list_answered_tasks_waiting_apply(
        self, *, slot: str | None = None, ref_ids: list[str] | None = None,
    ) -> list[TaskRecord]:
        return self.list_tasks(slot=slot, status="answered", ref_ids=ref_ids)

    def store_task_answer(
        self,
        *,
        answer_id: str,
        task_id: str,
        actor_type: str,
        raw_payload: JsonDict,
        accepted_for_processing: bool = False,
    ) -> None:
        answer_id = _validate_identifier("answer_id", answer_id)
        task_id = _validate_identifier("task_id", task_id)
        now = _now()
        with self._write_transaction():
            self._insert_task_answer(
                answer_id=answer_id,
                task_id=task_id,
                actor_type=actor_type,
                raw_payload=raw_payload,
                accepted_for_processing=accepted_for_processing,
                submitted_at=now,
            )
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))

    def _insert_task_answer(
        self,
        *,
        answer_id: str,
        task_id: str,
        actor_type: str,
        raw_payload: JsonDict,
        accepted_for_processing: bool,
        submitted_at: str,
    ) -> sqlite3.Row:
        """Insert one validated answer inside the caller-owned transaction."""
        task = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        if task["task_kind"] == "source_identity_attestation" and actor_type != "user":
            raise ValueError("source identity attestation requires a user answer")
        if task["task_kind"] == "manual_parse_review":
            detail = self._conn.execute("SELECT review_kind FROM task_manual_parse_review_details WHERE task_id=?", (task_id,)).fetchone()
            if detail is not None and detail["review_kind"] in {"citation_reference_review", "reference_claim_review"} and actor_type != "user":
                raise ValueError("manual citation attribution requires a user answer")
        normalized = normalize_task_answer(self._conn, task, raw_payload)
        self._conn.execute(
            """
            INSERT INTO task_answers(
              answer_id, task_id, actor_type, submitted_at,
              accepted_for_processing, generation, answer_kind
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                answer_id, task_id, _clean_text(actor_type) or "", submitted_at,
                1 if accepted_for_processing else 0, int(task["generation"]),
                normalized["kind"],
            ),
        )
        replace_task_answer(self._conn, answer_id, normalized)
        return task

    def validate_task_answer(
        self,
        *,
        task_id: str,
        raw_payload: JsonDict,
    ) -> None:
        """Validate one answer against its persisted task without writing it."""
        task_id = _validate_identifier("task_id", task_id)
        task = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        normalize_task_answer(self._conn, task, raw_payload)

    def submit_task_answer(
        self,
        *,
        task_id: str,
        actor_type: str,
        raw_payload: JsonDict,
        answer_id: str | None = None,
        accepted_for_processing: bool = True,
    ) -> str:
        answer_id = _validate_identifier(
            "answer_id", answer_id or f"answer-{uuid.uuid4().hex[:12]}",
        )
        task_id = _validate_identifier("task_id", task_id)
        now = _now()
        with self._write_transaction():
            task = self._insert_task_answer(
                answer_id=answer_id,
                task_id=task_id,
                actor_type=actor_type,
                raw_payload=raw_payload,
                accepted_for_processing=accepted_for_processing,
                submitted_at=now,
            )
            if accepted_for_processing:
                if task["status"] != "pending":
                    raise ValueError("accepted task answers require a pending task")
                cursor = self._conn.execute(
                    """
                    UPDATE tasks
                    SET status='answered',answered_at=?,applied_at=NULL,
                        last_error_stage=NULL,last_error_type=NULL,
                        last_error_message=NULL,last_error_traceback=NULL
                    WHERE task_id=? AND status='pending' AND generation=?
                    """,
                    (now, task_id, int(task["generation"])),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("task lifecycle changed while accepting its answer")
            self._conn.execute("UPDATE run SET updated_at=?", (now,))
        return answer_id

    def append_task_answer_provenance(
        self, record: JsonDict, files: list[JsonDict]
    ) -> None:
        with self._write_transaction():
            _insert_task_answer_provenance(self._conn, record, files)

    def submit_task_answer_with_provenance(
        self, *, task_id: str, actor_type: str, raw_payload: JsonDict,
        answer_id: str, provenance: JsonDict, files: list[JsonDict],
        expected_assurance: ExecutionAssuranceRecord,
    ) -> str:
        """Atomically accept an answer and its immutable admission provenance."""
        answer_id = _validate_identifier("answer_id", answer_id)
        task_id = _validate_identifier("task_id", task_id)
        now = _now()
        with self._write_transaction():
            if _read_execution_assurance(self._conn) != expected_assurance:
                raise RuntimeError(
                    "local task admission assurance changed before persistence"
                )
            task = self._insert_task_answer(
                answer_id=answer_id, task_id=task_id, actor_type=actor_type,
                raw_payload=raw_payload, accepted_for_processing=True, submitted_at=now,
            )
            if task["status"] != "pending":
                raise ValueError("accepted task answers require pending task")
            cursor = self._conn.execute(
                """UPDATE tasks SET status='answered',answered_at=?,applied_at=NULL,
                                      last_error_stage=NULL,last_error_type=NULL,
                                      last_error_message=NULL,last_error_traceback=NULL
                   WHERE task_id=? AND status='pending' AND generation=?""",
                (now, task_id, int(task["generation"])),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("task lifecycle changed while accepting its answer")
            _insert_task_answer_provenance(self._conn, provenance, files)
            self._conn.execute("UPDATE run SET updated_at=?", (now,))
        return answer_id

    def get_task_answer_provenance(self, answer_id: str) -> JsonDict | None:
        return _read_task_answer_provenance(
            self._conn, _validate_identifier("answer_id", answer_id)
        )

    def get_latest_task_answer(self, task_id: str) -> TaskAnswerRecord | None:
        row = self._conn.execute(
            """
            SELECT answer.* FROM task_answers AS answer
            JOIN tasks AS task ON task.task_id = answer.task_id
            WHERE answer.task_id=? AND answer.accepted_for_processing=1
              AND answer.generation=task.generation
            ORDER BY answer.submitted_at DESC, answer.answer_id DESC LIMIT 1
            """, (task_id,),
        ).fetchone()
        return None if row is None else _task_answer_record(
            row, self._conn,
        )

    def list_task_answers(self, task_id: str) -> list[TaskAnswerRecord]:
        rows = self._conn.execute(
            """
            SELECT *
            FROM task_answers
            WHERE task_id = ?
            ORDER BY submitted_at, answer_id
            """,
            (task_id,),
        ).fetchall()
        return [
            _task_answer_record(row, self._conn)
            for row in rows
        ]

    def mark_task_answered(self, task_id: str) -> None:
        with self._conn:
            self._conn.execute(
                """
                UPDATE tasks
                SET status = 'answered', answered_at = ?, applied_at = NULL,
                    last_error_stage = NULL, last_error_type = NULL,
                    last_error_message = NULL, last_error_traceback = NULL
                WHERE task_id = ? AND status = 'pending'
                """,
                (_now(), task_id),
            )
            self._conn.execute("UPDATE run SET updated_at = ?", (_now(),))

    def reopen_task(self, task_id: str, *, task_payload: JsonDict) -> None:
        task_id = _validate_identifier("task_id", task_id)
        now = _now()
        with self._write_transaction():
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"task not found: {task_id}")
            prospective = dict(row)
            prospective.update(status="pending", answered_at=None, applied_at=None)
            normalized = normalize_task_payload(
                self._conn, self._run_dir, prospective, task_payload,
            )
            if normalized["kind"] != row["task_kind"]:
                raise ValueError("task kind cannot change")
            self._conn.execute(
                """
                UPDATE tasks
                SET status = 'pending', answered_at = NULL, applied_at = NULL,
                    generation = generation + 1
                WHERE task_id = ?
                """,
                (task_id,),
            )
            replace_task_payload(self._conn, task_id, normalized)
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))

    def apply_task(self, task_id: str, *, task_payload: JsonDict | None = None) -> None:
        task_id = _validate_identifier("task_id", task_id)
        now = _now()
        with self._write_transaction():
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"task not found: {task_id}")
            if task_payload is not None:
                prospective = dict(row)
                prospective.update(status="applied", applied_at=now)
                normalized = normalize_task_payload(
                    self._conn, self._run_dir, prospective, task_payload,
                )
                if normalized["kind"] != row["task_kind"]:
                    raise ValueError("task kind cannot change")
                replace_task_payload(self._conn, task_id, normalized)
            self._conn.execute(
                """
                UPDATE tasks
                SET status = 'applied', applied_at = ?,
                    last_error_stage=NULL,last_error_type=NULL,
                    last_error_message=NULL,last_error_traceback=NULL
                WHERE task_id = ?
                """,
                (now, task_id),
            )
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))

    def create_source_identity_attestation(
        self, *, task_id: str, ref_id: str, source_text_id: str,
        instructions: str, slot: str = "verify",
    ) -> None:
        target = self.source_identity_attestation_target(
            ref_id=ref_id, source_text_id=source_text_id,
        )
        self.create_task(
            task_id=task_id,
            slot=slot,
            ref_id=ref_id,
            task_payload={
                "kind": "source_identity_attestation",
                "status": "pending",
                "answer": None,
                **target,
                "instructions": instructions,
            },
        )

    def source_identity_attestation_target(
        self, *, ref_id: str, source_text_id: str,
    ) -> JsonDict:
        """Return the closed, current non-secret facts an operator would attest."""
        ref_id = _validate_identifier("ref_id", ref_id)
        source_text_id = _validate_identifier("source_text_id", source_text_id)
        snapshot = _source_identity_attestation_snapshot(
            self._conn, ref_id, source_text_id,
        )
        return {
            **snapshot,
            "target_sha256": _source_identity_attestation_sha(snapshot),
        }

    def apply_source_identity_attestation(self, task_id: str) -> None:
        """Apply one provenance-authorized, hash-bound identity decision."""
        task_id = _validate_identifier("task_id", task_id)
        now = _now()
        with self._write_transaction():
            task = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,),
            ).fetchone()
            if (
                task is None
                or task["task_kind"] != "source_identity_attestation"
                or task["slot"] not in {"fetch", "verify"}
                or task["ref_id"] is None
                or task["status"] != "answered"
            ):
                raise ValueError("source identity attestation is not awaiting application")
            task_payload = read_task_payload(self._conn, self._run_dir, task)
            answer = self._conn.execute(
                """
                SELECT * FROM task_answers
                WHERE task_id=? AND accepted_for_processing=1 AND generation=?
                ORDER BY submitted_at DESC, answer_id DESC LIMIT 1
                """,
                (task_id, task["generation"]),
            ).fetchone()
            if answer is None or answer["actor_type"] != "user":
                raise ValueError("source identity attestation answer is invalid")
            answer_payload = read_task_answer(self._conn, answer)
            detail = self._conn.execute(
                """
                SELECT source_text_id,source_text_sha256,target_sha256
                FROM task_source_identity_attestation_details WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
            if (
                detail is None
                or answer_payload["target_sha256"] != detail["target_sha256"]
                or task_payload.get("target_sha256") != detail["target_sha256"]
            ):
                raise ValueError("source identity attestation answer is stale")
            provenance = _read_task_answer_provenance(
                self._conn, answer["answer_id"],
            )
            if not provenance or provenance.get("producer_class") != "operator":
                raise ValueError("source identity attestation requires operator provenance")
            snapshot = _source_identity_attestation_snapshot(
                self._conn, task["ref_id"], detail["source_text_id"],
            )
            target_sha256 = _source_identity_attestation_sha(snapshot)
            if (
                target_sha256 != detail["target_sha256"]
                or snapshot["source_text_sha256"] != detail["source_text_sha256"]
            ):
                raise ValueError("source identity attestation target changed")
            source_text = _read_source_text(
                self._conn,
                self._run_dir,
                detail["source_text_id"],
                detail["source_text_sha256"],
                task["ref_id"],
            )
            if answer_payload["action"] == "attest_identity":
                from core.verify.identity_gate import (
                    source_identity_attestation_block_reason,
                )

                gate_task = {
                    "reference": snapshot["reference"],
                    "source_identity_evidence": {
                        **snapshot["source_identity"],
                        "resolve": snapshot["resolve_identity"],
                    },
                }
                if source_identity_attestation_block_reason(
                    gate_task, source_text,
                ) is not None:
                    raise ValueError("source identity attestation is hard-ineligible")
            if self._conn.execute(
                """
                SELECT 1 FROM source_identity_attestation_decisions
                WHERE task_id=? OR source_text_id=?
                """,
                (task_id, detail["source_text_id"]),
            ).fetchone() is not None:
                raise ValueError("source identity attestation is already applied")
            applied = self._conn.execute(
                """
                UPDATE tasks SET status='applied',applied_at=?
                WHERE task_id=? AND status='answered' AND generation=?
                """,
                (now, task_id, task["generation"]),
            ).rowcount
            if applied != 1:
                raise RuntimeError("source identity attestation lifecycle changed")
            self._conn.execute(
                """
                INSERT INTO source_identity_attestation_decisions(
                  task_id,ref_id,source_text_id,action,target_sha256,answer_id,applied_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    task["ref_id"],
                    detail["source_text_id"],
                    answer_payload["action"],
                    target_sha256,
                    answer["answer_id"],
                    now,
                ),
            )
            self._conn.execute("UPDATE run SET updated_at=?", (now,))

    def source_identity_attestation_for(self, source_text_id: str) -> JsonDict | None:
        source_text_id = _validate_identifier("source_text_id", source_text_id)
        row = self._conn.execute(
            """
            SELECT d.*,t.status,t.task_kind,t.slot,t.generation,
                   a.actor_type,a.answer_kind,a.generation AS answer_generation,
                   a.accepted_for_processing,a.submitted_at,
                   x.action AS answer_action,x.target_sha256 AS answer_target,
                   x.reason,td.source_text_sha256,
                   td.target_sha256 AS task_target
            FROM source_identity_attestation_decisions d
            JOIN tasks t ON t.task_id=d.task_id
            JOIN task_answers a ON a.answer_id=d.answer_id
            JOIN task_source_identity_attestation_answers x
              ON x.answer_id=a.answer_id
            JOIN task_source_identity_attestation_details td
              ON td.task_id=t.task_id
            WHERE d.source_text_id=?
            """,
            (source_text_id,),
        ).fetchone()
        if row is None:
            return None
        task_row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (row["task_id"],),
        ).fetchone()
        answer_row = self._conn.execute(
            "SELECT * FROM task_answers WHERE answer_id=?", (row["answer_id"],),
        ).fetchone()
        if task_row is None or answer_row is None:
            raise RuntimeError("source identity attestation binding is incomplete")
        task_payload = read_task_payload(self._conn, self._run_dir, task_row)
        answer_payload = read_task_answer(self._conn, answer_row)
        provenance = _read_task_answer_provenance(
            self._conn, row["answer_id"],
        )
        if (
            row["status"] != "applied"
            or row["task_kind"] != "source_identity_attestation"
            or row["slot"] not in {"fetch", "verify"}
            or row["actor_type"] != "user"
            or row["answer_kind"] != "source_identity_attestation"
            or not bool(row["accepted_for_processing"])
            or int(row["answer_generation"]) != int(row["generation"])
            or row["answer_action"] != row["action"]
            or row["answer_target"] != row["target_sha256"]
            or row["task_target"] != row["target_sha256"]
            or task_payload.get("target_sha256") != row["target_sha256"]
            or answer_payload.get("action") != row["action"]
            or answer_payload.get("target_sha256") != row["target_sha256"]
            or answer_payload.get("reason") != row["reason"]
            or provenance is None
            or provenance.get("producer_class") != "operator"
        ):
            raise RuntimeError("source identity attestation provenance is inconsistent")
        snapshot = _source_identity_attestation_snapshot(
            self._conn, row["ref_id"], source_text_id,
        )
        if (
            _source_identity_attestation_sha(snapshot) != row["target_sha256"]
            or snapshot["source_text_sha256"] != row["source_text_sha256"]
        ):
            raise RuntimeError("source identity attestation target is inconsistent")
        _read_source_text(
            self._conn,
            self._run_dir,
            source_text_id,
            row["source_text_sha256"],
            row["ref_id"],
        )
        authority_id = provenance["authority_id"]
        return {
            "task_id": row["task_id"],
            "answer_id": row["answer_id"],
            "ref_id": row["ref_id"],
            "source_text_id": row["source_text_id"],
            "action": row["action"],
            "target_sha256": row["target_sha256"],
            "reason": row["reason"],
            "applied_at": row["applied_at"],
            "submitted_at": row["submitted_at"],
            "provenance": {
                "producer_class": provenance["producer_class"],
                "producer_identity": provenance["producer_identity"],
                "authenticated_uid": provenance["authenticated_uid"],
                "ingress_kind": provenance["ingress_kind"],
                "authority_id": authority_id,
                "classification": (
                    "local_operator_unattested"
                    if authority_id == "local-unattested"
                    else "authority_authenticated_operator"
                ),
            },
        }

    def create_manual_parse_review(
        self, *, task_id: str, review_kind: str, target_id: str,
        instructions: str, candidates: list[JsonDict] | None = None,
    ) -> None:
        """Create one hash-bound Parse review task from immutable Parse facts."""
        if review_kind == "footnote_source_review":
            row = self._conn.execute("SELECT raw_note FROM footnote_notes WHERE note_id=?", (target_id,)).fetchone()
            kwargs = {"note_id": target_id}
        elif review_kind == "reference_identity_review":
            row = self._conn.execute("SELECT raw_entry FROM reference_entries WHERE ref_id=?", (target_id,)).fetchone()
            kwargs = {"ref_id": target_id}
        elif review_kind == "citation_reference_review":
            row = self._conn.execute("SELECT raw_citation_json FROM unresolved_citations WHERE occurrence_id=?", (target_id,)).fetchone()
            claim = self._conn.execute("SELECT claim_id FROM unresolved_citations WHERE occurrence_id=?", (target_id,)).fetchone()
            if claim is None or claim["claim_id"] is None:
                raise ValueError("manual citation review target has no claim")
            kwargs = {"claim_id": claim["claim_id"], "scope": target_id}
        elif review_kind == "reference_claim_review":
            row = self._conn.execute("SELECT raw_entry FROM reference_entries WHERE ref_id=?", (target_id,)).fetchone()
            kwargs = {"ref_id": target_id}
        else:
            raise ValueError("manual Parse review kind is invalid")
        if row is None:
            raise ValueError("manual Parse review target is unavailable")
        target_material = row[0]
        if review_kind in {"citation_reference_review", "reference_claim_review"}:
            if not candidates:
                raise ValueError("manual citation review requires candidates")
            try:
                candidates = [
                    {"id": item["id"], "origin": item["origin"], "score": float(item["score"])}
                    for item in candidates
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("manual citation review candidate is invalid") from exc
            if review_kind == "citation_reference_review":
                claim_row = self._conn.execute("SELECT claim_id,sentence,context_window,marker_raw FROM claims WHERE claim_id=?", (kwargs["claim_id"],)).fetchone()
                target_material += json.dumps(dict(claim_row), sort_keys=True, separators=(",", ":"))
            else:
                claim_rows = [dict(x) for item in candidates for x in self._conn.execute("SELECT claim_id,sentence,context_window,marker_raw FROM claims WHERE claim_id=?", (item["id"],))]
                target_material += json.dumps(claim_rows, sort_keys=True, separators=(",", ":"))
            target_material += json.dumps(candidates, sort_keys=True, separators=(",", ":"))
        target_sha256 = hashlib.sha256(target_material.encode("utf-8")).hexdigest()
        self.create_task(task_id=task_id, slot="parse_review", task_payload={
            "kind": "manual_parse_review", "status": "pending", "answer": None,
            "review_kind": review_kind, "target_sha256": target_sha256,
            "instructions": instructions,
            **({"candidates": candidates} if candidates is not None else {}),
        }, **kwargs)

    def apply_manual_parse_review(self, task_id: str) -> None:
        """Atomically apply the current accepted, hash-bound Parse review once."""
        task_id = _validate_identifier("task_id", task_id)
        now = _now()
        with self._write_transaction():
            task = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None or task["task_kind"] != "manual_parse_review" or task["status"] != "answered":
                raise ValueError("manual Parse review is not awaiting application")
            answer = self._conn.execute("""
                SELECT a.*, x.action, x.target_sha256 AS answer_hash, x.title_present, x.title, COALESCE(x.selected_ref_id,x.selected_claim_id) AS selected_id,
                       x.doi_present, x.doi, d.review_kind, d.target_sha256 AS task_hash
                FROM task_answers a JOIN task_manual_parse_review_answers x ON x.answer_id=a.answer_id
                JOIN task_manual_parse_review_details d ON d.task_id=a.task_id
                WHERE a.task_id=? AND a.accepted_for_processing=1 AND a.generation=?
                ORDER BY a.submitted_at DESC,a.answer_id DESC LIMIT 1
            """, (task_id, task["generation"])).fetchone()
            if answer is None or answer["answer_hash"] != answer["task_hash"]:
                raise ValueError("manual Parse review answer is unavailable or stale")
            if answer["review_kind"] in {"citation_reference_review", "reference_claim_review"}:
                provenance = _read_task_answer_provenance(
                    self._conn, answer["answer_id"],
                )
                if (
                    answer["actor_type"] != "user"
                    or provenance is None
                    or provenance.get("producer_class") != "operator"
                ):
                    raise ValueError(
                        "manual citation attribution requires operator provenance"
                    )
                task_candidates = [{"id": x["candidate_ref_id"] or x["candidate_claim_id"], "origin": x["candidate_origin"], "score": x["candidate_score"]} for x in self._conn.execute("SELECT candidate_ref_id,candidate_claim_id,candidate_origin,candidate_score FROM task_manual_parse_review_candidates WHERE task_id=? ORDER BY candidate_order", (task_id,))]
                if answer["review_kind"] == "citation_reference_review":
                    occurrence = self._conn.execute("SELECT claim_id,raw_citation_json FROM unresolved_citations WHERE occurrence_id=?", (task["scope"],)).fetchone()
                    claim_snapshot = self._conn.execute("SELECT claim_id,sentence,context_window,marker_raw FROM claims WHERE claim_id=?", (occurrence["claim_id"],)).fetchone() if occurrence is not None else None
                    current_hash = hashlib.sha256((occurrence["raw_citation_json"] + json.dumps(dict(claim_snapshot), sort_keys=True, separators=(",", ":")) + json.dumps(task_candidates, sort_keys=True, separators=(",", ":"))).encode("utf-8")).hexdigest() if occurrence is not None and claim_snapshot is not None else None
                    if occurrence is None or occurrence["claim_id"] != task["claim_id"] or current_hash != answer["task_hash"]:
                        raise ValueError("manual citation target changed")
                else:
                    target = self.get_reference(task["ref_id"])
                    claim_snapshots = [dict(x) for item in task_candidates for x in self._conn.execute("SELECT claim_id,sentence,context_window,marker_raw FROM claims WHERE claim_id=?", (item["id"],))]
                    current_hash = hashlib.sha256((target.raw_entry + json.dumps(claim_snapshots, sort_keys=True, separators=(",", ":")) + json.dumps(task_candidates, sort_keys=True, separators=(",", ":"))).encode("utf-8")).hexdigest() if target is not None else None
                    if current_hash != answer["task_hash"]:
                        raise ValueError("manual inverse attribution target is unavailable")
                if answer["action"] == "keep_unresolved":
                    pass
                else:
                    selected = answer["selected_id"]
                    column = "candidate_ref_id" if answer["review_kind"] == "citation_reference_review" else "candidate_claim_id"
                    candidate = self._conn.execute(f"SELECT candidate_origin,candidate_score FROM task_manual_parse_review_candidates WHERE task_id=? AND {column}=?", (task_id, selected)).fetchone()
                    if candidate is None:
                        raise ValueError("manual citation attribution candidate is unavailable")
                    if answer["review_kind"] == "citation_reference_review":
                        occurrence = self._conn.execute("SELECT claim_id,raw_citation_json FROM unresolved_citations WHERE occurrence_id=?", (task["scope"],)).fetchone()
                        task_candidates = [{"id": x["candidate_ref_id"] or x["candidate_claim_id"], "origin": x["candidate_origin"], "score": x["candidate_score"]} for x in self._conn.execute("SELECT candidate_ref_id,candidate_claim_id,candidate_origin,candidate_score FROM task_manual_parse_review_candidates WHERE task_id=? ORDER BY candidate_order", (task_id,))]
                        claim_snapshot = self._conn.execute("SELECT claim_id,sentence,context_window,marker_raw FROM claims WHERE claim_id=?", (occurrence["claim_id"],)).fetchone() if occurrence is not None else None
                        if occurrence is None or occurrence["claim_id"] != task["claim_id"] or claim_snapshot is None or candidate["candidate_origin"] not in {"parser", "orphan_match"} or hashlib.sha256((occurrence["raw_citation_json"] + json.dumps(dict(claim_snapshot), sort_keys=True, separators=(",", ":")) + json.dumps(task_candidates, sort_keys=True, separators=(",", ":"))).encode("utf-8")).hexdigest() != answer["task_hash"]:
                            raise ValueError("manual citation target changed")
                        if occurrence["claim_id"] is None or self.get_reference(selected) is None:
                            raise ValueError("manual citation attribution target is dangling")
                        self._conn.execute("INSERT INTO manual_citation_attribution_overrides VALUES(?,?,?,?,?,?,?,?,?)", (task_id, task["scope"], None, selected, "citation_to_reference", candidate["candidate_origin"], candidate["candidate_score"], answer["answer_id"], now))
                    else:
                        target = self.get_reference(task["ref_id"])
                        task_candidates = [{"id": x["candidate_ref_id"] or x["candidate_claim_id"], "origin": x["candidate_origin"], "score": x["candidate_score"]} for x in self._conn.execute("SELECT candidate_ref_id,candidate_claim_id,candidate_origin,candidate_score FROM task_manual_parse_review_candidates WHERE task_id=? ORDER BY candidate_order", (task_id,))]
                        claim_snapshots = [dict(x) for item in task_candidates for x in self._conn.execute("SELECT claim_id,sentence,context_window,marker_raw FROM claims WHERE claim_id=?", (item["id"],))]
                        if target is None or candidate["candidate_origin"] != "orphan_match_inverse" or hashlib.sha256((target.raw_entry + json.dumps(claim_snapshots, sort_keys=True, separators=(",", ":")) + json.dumps(task_candidates, sort_keys=True, separators=(",", ":"))).encode("utf-8")).hexdigest() != answer["task_hash"] or self._conn.execute("SELECT 1 FROM claims WHERE claim_id=?", (selected,)).fetchone() is None:
                            raise ValueError("manual inverse attribution target is unavailable")
                        self._conn.execute("INSERT INTO manual_citation_attribution_overrides VALUES(?,?,?,?,?,?,?,?,?)", (task_id, None, selected, task["ref_id"], "reference_to_claim", candidate["candidate_origin"], candidate["candidate_score"], answer["answer_id"], now))
            elif answer["review_kind"] == "footnote_source_review":
                target = self._conn.execute("SELECT * FROM footnote_notes WHERE note_id=?", (task["note_id"],)).fetchone()
                if target is None or hashlib.sha256(target["raw_note"].encode("utf-8")).hexdigest() != answer["task_hash"]:
                    raise ValueError("manual Parse footnote target changed")
                if answer["action"] not in {"no_sources", "split_sources", "keep_ambiguous"}:
                    raise ValueError("manual Parse footnote action is invalid")
                if answer["action"] == "split_sources":
                    pieces = list(self._conn.execute("SELECT source_order,source_text FROM task_manual_parse_review_split_sources WHERE answer_id=? ORDER BY source_order", (answer["answer_id"],)))
                    if len(pieces) < 2 or [r["source_order"] for r in pieces] != list(range(len(pieces))):
                        raise ValueError("manual Parse split sources are incomplete")
                    spans = []
                    for piece in pieces:
                        text = piece["source_text"]
                        start = target["raw_note"].find(text)
                        if start < 0 or target["raw_note"].find(text, start + 1) >= 0:
                            raise ValueError("manual Parse split source must occur exactly once")
                        spans.append((start, start + len(text), text))
                    if spans != sorted(spans) or any(spans[i][1] > spans[i + 1][0] for i in range(len(spans)-1)):
                        raise ValueError("manual Parse split sources overlap or are out of order")
                else:
                    spans = []
                if self._conn.execute(
                    "SELECT 1 FROM manual_footnote_source_overrides WHERE note_id=?",
                    (task["note_id"],),
                ).fetchone() is not None:
                    raise ValueError("manual Parse footnote decision is already applied")
                self._conn.execute(
                    "INSERT INTO manual_footnote_source_overrides VALUES(?,?,?,?,?)",
                    (task["note_id"], answer["action"], answer["task_hash"], answer["answer_id"], now),
                )
                next_ref_number = int(self._conn.execute(
                    "SELECT COALESCE(MAX(ref_number),0)+1 FROM operational_references"
                ).fetchone()[0])
                for order, (start, end, text) in enumerate(spans):
                    ref_id = "footnote-source-" + hashlib.sha256(
                        f"{task['note_id']}:{order}:{text}".encode()
                    ).hexdigest()[:24]
                    self._conn.execute(
                        "INSERT INTO operational_references VALUES(?,?,?,?,?,?,?)",
                        (ref_id, next_ref_number + order, "manual_footnote_split",
                         self._conn.execute(
                             "SELECT ref_id FROM footnote_note_parents WHERE note_id=?",
                             (task["note_id"],),
                         ).fetchone()[0], task["note_id"], answer["answer_id"], now),
                    )
                    self._conn.execute(
                        "INSERT INTO manual_footnote_source_override_sources VALUES(?,?,?,?,?,?)",
                        (task["note_id"], ref_id, order, text, start, end),
                    )
            else:
                target = self._conn.execute("SELECT raw_entry FROM reference_entries WHERE ref_id=?", (task["ref_id"],)).fetchone()
                if target is None or hashlib.sha256(target[0].encode("utf-8")).hexdigest() != answer["task_hash"]:
                    raise ValueError("manual Parse identity target changed")
                if answer["action"] == "correct_identity":
                    self._conn.execute("INSERT INTO manual_reference_identity_overrides VALUES(?,?,?,?,?,?,?,?)", (task["ref_id"], answer["task_hash"], answer["title_present"], answer["title"], answer["doi_present"], answer["doi"], answer["answer_id"], now))
                elif answer["action"] != "keep_ambiguous":
                    raise ValueError("manual Parse identity action is invalid")
            applied = self._conn.execute("UPDATE tasks SET status='applied',applied_at=? WHERE task_id=? AND status='answered' AND generation=?", (now, task_id, task["generation"])).rowcount
            if applied != 1:
                raise RuntimeError("manual Parse review lifecycle changed")
            self._conn.execute("INSERT INTO manual_parse_review_applications VALUES(?,?,?,?)", (task_id, answer["answer_id"], task["generation"], now))
            self._conn.execute("UPDATE run SET updated_at=?", (now,))

    def effective_reference_identity(self, ref_id: str) -> JsonDict:
        row = self._conn.execute("SELECT e.title,e.doi,o.title_present,o.title AS override_title,o.doi_present,o.doi AS override_doi FROM reference_entries e LEFT JOIN manual_reference_identity_overrides o ON o.ref_id=e.ref_id WHERE e.ref_id=?", (ref_id,)).fetchone()
        if row is None: raise ValueError("reference is unavailable")
        return {"title": row["override_title"] if row["title_present"] else row["title"], "doi": row["override_doi"] if row["doi_present"] else row["doi"]}

    def effective_footnote_sources(self, note_id: str) -> list[JsonDict]:
        """Resolved-only footnote sources; raw parse citations are untouched."""
        row = self._conn.execute("SELECT extraction_status FROM footnote_notes WHERE note_id=?", (note_id,)).fetchone()
        if row is None: raise ValueError("footnote note is unavailable")
        override = self._manual_footnote_source_overrides().get(note_id)
        if override is not None:
            if override["action"] != "split_sources":
                return []
            return [
                source for source in self._manual_footnote_override_sources()
                if source["note_id"] == note_id
            ]
        if row["extraction_status"] != "sources_extracted": return []
        return [dict(item) for item in self._conn.execute("SELECT ref_id,source_order,raw_start,raw_end FROM footnote_note_sources WHERE note_id=? ORDER BY source_order", (note_id,))]

    def mark_task_applied(self, task_id: str) -> None:
        with self._conn:
            self._conn.execute(
                """
                UPDATE tasks
                SET status = 'applied', applied_at = ?,
                    last_error_stage=NULL,last_error_type=NULL,
                    last_error_message=NULL,last_error_traceback=NULL
                WHERE task_id = ? AND status IN ('answered', 'pending')
                """,
                (_now(), task_id),
            )
            self._conn.execute("UPDATE run SET updated_at = ?", (_now(),))

    def task_view(self, task_id: str) -> JsonDict | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        answer = self.get_latest_task_answer(task_id)
        view = _task_view(task, answer)
        if task.task_kind == "manual_parse_review":
            view.update(self._manual_parse_review_target_view(task))
        return view

    def _manual_parse_review_target_view(self, task: TaskRecord) -> JsonDict:
        """Expose only the immutable raw target of a manual Parse review."""
        payload = task.task_payload
        kind = payload.get("review_kind")
        target_hash = payload.get("target_sha256")
        parent = self._conn.execute(
            "SELECT ref_id,note_id,scope FROM tasks WHERE task_id=?",
            (task.task_id,),
        ).fetchone()
        if parent is None:
            raise ValueError("manual Parse review task unavailable")
        if kind == "footnote_source_review":
            note_id = parent["note_id"]
            row = self._conn.execute(
                "SELECT note_id,note_number,raw_note FROM footnote_notes WHERE note_id=?",
                (note_id,),
            ).fetchone()
            if row is None:
                raise ValueError("manual Parse review footnote target unavailable")
            return {
                "review_kind": kind,
                "target_sha256": target_hash,
                "note_id": row["note_id"],
                "note_number": row["note_number"],
                "raw_note": row["raw_note"],
            }
        if kind == "reference_identity_review":
            ref_id = parent["ref_id"]
            row = self._conn.execute(
                "SELECT ref_id,ref_number,raw_entry,title,doi FROM reference_entries WHERE ref_id=?",
                (ref_id,),
            ).fetchone()
            if row is None:
                raise ValueError("manual Parse review identity target unavailable")
            return {
                "review_kind": kind,
                "target_sha256": target_hash,
                "ref_id": row["ref_id"],
                "ref_number": row["ref_number"],
                "raw_entry": row["raw_entry"],
                "title": row["title"],
                "doi": row["doi"],
            }
        if kind == "citation_reference_review":
            row = self._conn.execute("SELECT * FROM unresolved_citations WHERE occurrence_id=?", (parent["scope"],)).fetchone()
            if row is None:
                raise ValueError("manual citation review target unavailable")
            candidates = list(self._conn.execute("SELECT candidate_ref_id,candidate_claim_id,candidate_origin,candidate_score FROM task_manual_parse_review_candidates WHERE task_id=? ORDER BY candidate_order", (task.task_id,)))
            return {"review_kind": kind, "target_sha256": target_hash, "occurrence_id": row["occurrence_id"], "claim_id": row["claim_id"], "marker_raw": row["marker_raw"], "candidates": [{"id": x["candidate_ref_id"] or x["candidate_claim_id"], "origin": x["candidate_origin"], "score": x["candidate_score"]} for x in candidates]}
        if kind == "reference_claim_review":
            row = self._conn.execute("SELECT ref_id,ref_number,raw_entry FROM reference_entries WHERE ref_id=?", (parent["ref_id"],)).fetchone()
            if row is None:
                raise ValueError("manual inverse citation review target unavailable")
            candidates = list(self._conn.execute("SELECT candidate_ref_id,candidate_claim_id,candidate_origin,candidate_score FROM task_manual_parse_review_candidates WHERE task_id=? ORDER BY candidate_order", (task.task_id,)))
            return {"review_kind": kind, "target_sha256": target_hash, "ref_id": row["ref_id"], "ref_number": row["ref_number"], "raw_entry": row["raw_entry"], "candidates": [{"id": x["candidate_ref_id"] or x["candidate_claim_id"], "origin": x["candidate_origin"], "score": x["candidate_score"]} for x in candidates]}
        raise ValueError("manual Parse review kind invalid")

    def list_task_views(
        self,
        *,
        slot: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> list[JsonDict]:
        tasks = self.list_tasks(slot=slot)
        out = []
        for task in tasks:
            if statuses is not None and task.status not in statuses:
                continue
            out.append(self.task_view(task.task_id) or {})
        return out

    def cancel_task(self, task_id: str) -> None:
        task_id = _validate_identifier("task_id", task_id)
        with self._conn:
            self._conn.execute(
                """
                UPDATE tasks
                SET status = 'cancelled',
                    last_error_stage=NULL,last_error_type=NULL,
                    last_error_message=NULL,last_error_traceback=NULL
                WHERE task_id = ? AND status != 'applied'
                """,
                (task_id,),
            )
            self._conn.execute("UPDATE run SET updated_at = ?", (_now(),))

    def ensure_verification_pair(
        self, *, claim_id: str, ref_id: str, scope: str,
    ) -> None:
        """Create the mutable OPEN state for a jury pair if it is absent."""
        claim_id = _validate_identifier("claim_id", claim_id)
        ref_id = _validate_identifier("ref_id", ref_id)
        now = _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO verification_pair_state(
                  claim_id, ref_id, scope, status, opened_at
                ) VALUES(?, ?, ?, 'open', ?)
                ON CONFLICT(claim_id, ref_id, scope) DO NOTHING
                """,
                (claim_id, ref_id, _clean_text(scope) or "", now),
            )

    def get_verification_pair_state(
        self, *, claim_id: str, ref_id: str, scope: str
    ) -> dict | None:
        """Return the authoritative lifecycle state for one verification pair."""
        row = self._conn.execute(
            """
            SELECT * FROM verification_pair_state
            WHERE claim_id = ? AND ref_id = ? AND scope = ?
            """,
            (claim_id, ref_id, scope),
        ).fetchone()
        return dict(row) if row is not None else None

    def verification_pair_state_payloads(
        self, *, ref_ids: list[str] | None = None,
    ) -> list[JsonDict]:
        """Return authoritative pair states, optionally limited to references."""
        if ref_ids is not None:
            ref_ids = _validated_ref_ids(ref_ids)
            if not ref_ids:
                return []
            placeholders = ",".join("?" for _ in ref_ids)
            rows = self._conn.execute(
                f"""
                SELECT * FROM verification_pair_state
                WHERE ref_id IN ({placeholders})
                ORDER BY claim_id, ref_id, scope
                """,
                ref_ids,
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM verification_pair_state
                ORDER BY claim_id, ref_id, scope
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def run_progress_counts(self) -> JsonDict:
        """Return durable desktop progress aggregates without projecting rows."""
        reference_rows = self._conn.execute(
            "SELECT ref_id FROM reference_entries"
        ).fetchall()
        reference_ids = {row[0] for row in reference_rows}
        resolve_completed = int(self._conn.execute(
            """
            SELECT COUNT(*) FROM reference_entries AS reference
            JOIN resolve_results AS result ON result.ref_id = reference.ref_id
            """
        ).fetchone()[0])
        citation_pairs_total = int(self._conn.execute(
            "SELECT COUNT(*) FROM citations"
        ).fetchone()[0])
        verify_total, verify_completed = self._conn.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN status != 'open' THEN 1 ELSE 0 END), 0)
            FROM verification_pair_state
            """
        ).fetchone()

        # Fetch completions are append-only but a later cycle uses ref_id#N.
        # Count only the same consecutive, reference-bound identities used by
        # the desktop's full projection.
        completion_counts: dict[str, int] = {}
        completed_fetch_ids: set[str] = set()
        for row in _list_integrity_unit_completions(self._conn, "fetch_auto"):
            payload = row.get("payload")
            ref_id = payload.get("ref_id") if isinstance(payload, dict) else None
            unit_id = row.get("unit_id")
            if not isinstance(ref_id, str) or not ref_id or not isinstance(unit_id, str):
                continue
            prior_count = completion_counts.get(ref_id, 0)
            expected_unit_id = ref_id if prior_count == 0 else f"{ref_id}#{prior_count + 1}"
            if unit_id != expected_unit_id:
                continue
            completion_counts[ref_id] = prior_count + 1
            if ref_id in reference_ids:
                completed_fetch_ids.add(ref_id)

        return {
            "sources_total": len(reference_ids),
            "resolve_completed": resolve_completed,
            "fetch_completed": len(completed_fetch_ids),
            "citation_pairs_total": citation_pairs_total,
            "verify_total": int(verify_total),
            "verify_completed": int(verify_completed),
        }

    def verification_raw_payloads(self) -> JsonDict:
        """Export immutable claim-evidence facts for report-only projections."""
        snapshot = self.claim_evidence_adapter().resume_snapshot()
        snapshot["pair_states"] = self.verification_pair_state_payloads()
        return snapshot

    def verification_pair_projection_payloads(
        self, *, ref_ids: list[str],
    ) -> JsonDict:
        """Read and validate only candidate facts for a bounded pair projection."""
        from core.verify.claim_evidence.domain.fingerprint import (
            candidate_fingerprint,
            payload_fingerprint,
        )
        from . import jury1_rejections, llm_dispatches, verification_candidates

        ref_ids = _validated_ref_ids(ref_ids)
        pair_states = self.verification_pair_state_payloads(ref_ids=ref_ids)
        if not ref_ids:
            return {"pair_states": [], "candidates": [], "candidate_events": []}

        ref_placeholders = ",".join("?" for _ in ref_ids)
        candidate_rows = self._conn.execute(
            f"""
            SELECT * FROM verification_candidates
            WHERE ref_id IN ({ref_placeholders})
            ORDER BY candidate_cycle, candidate_id
            """,
            ref_ids,
        ).fetchall()
        candidates = {}
        adapter = self.claim_evidence_adapter()
        fingerprint_version = adapter.fingerprint_version()
        for row in candidate_rows:
            candidate = verification_candidates.decode_candidate(
                dict(row), conn=self._conn,
            )
            grounded = tuple(
                {
                    name: item[name]
                    for name in ("span_id", "raw_start", "raw_end", "text", "source_hash")
                    if name in item
                }
                for item in candidate.get("grounded", [])
            )
            expected_fingerprint = candidate_fingerprint(
                outcome=candidate.get("outcome"),
                evidence=tuple(candidate.get("evidence", [])),
                outcome_fields=candidate.get("outcome_fields", {}),
                grounded=grounded,
                version=fingerprint_version,
            )
            if candidate.get("fingerprint") != expected_fingerprint:
                raise ValueError("stored candidate fingerprint mismatch")
            candidates[candidate["candidate_id"]] = candidate

        if not candidates:
            return {
                "pair_states": pair_states,
                "candidates": [],
                "candidate_events": [],
            }

        candidate_ids = list(candidates)
        origin_request_ids = {
            candidate["origin_logical_request_id"]
            for candidate in candidates.values()
        }
        request_rows: dict[str, Any] = {}
        for request_ids in _identifier_batches(sorted(origin_request_ids)):
            placeholders = ",".join("?" for _ in request_ids)
            for row in self._conn.execute(
                "SELECT * FROM llm_logical_requests WHERE logical_request_id IN ("
                + placeholders + ")",
                request_ids,
            ):
                request_rows[row["logical_request_id"]] = row
        for candidate_batch in _identifier_batches(candidate_ids):
            placeholders = ",".join("?" for _ in candidate_batch)
            for row in self._conn.execute(
                "SELECT * FROM llm_logical_requests WHERE stage='jury2' "
                "AND candidate_id IN (" + placeholders + ")",
                candidate_batch,
            ):
                request_rows[row["logical_request_id"]] = row

        requests = {}
        for request_id, row in request_rows.items():
            request = llm_dispatches.decode_request(dict(row), conn=self._conn)
            if request.get("payload_hash") != payload_fingerprint(
                request["payload"], version=fingerprint_version,
            ):
                raise ValueError("logical request payload hash mismatch")
            adapter._require_prompt_hash(request)
            requests[request_id] = request
        verification_candidates.validate_links(requests, candidates)

        candidate_events = []
        for candidate_batch in _identifier_batches(candidate_ids):
            placeholders = ",".join("?" for _ in candidate_batch)
            rows = self._conn.execute(
                "SELECT * FROM verification_candidate_events WHERE candidate_id IN ("
                + placeholders + ") ORDER BY created_at, event_id",
                candidate_batch,
            ).fetchall()
            candidate_events.extend(
                verification_candidates.decode_candidate_event(
                    dict(row), conn=self._conn,
                )
                for row in rows
            )
        verification_candidates.validate_event_links(candidate_events, requests)
        verification_candidates.candidate_state(candidate_events, candidates)

        for request_id in origin_request_ids:
            row = self._conn.execute(
                "SELECT * FROM jury1_rejection_events WHERE logical_request_id=?",
                (request_id,),
            ).fetchone()
            if row is not None:
                jury1_rejections.validate_input(
                    event_id=row["event_id"],
                    request=requests.get(request_id),
                    state_cause=row["state_cause"],
                    cause=row["cause"],
                )
                raise ValueError("persisted Jury1 rejection conflicts with candidate")

        return {
            "pair_states": pair_states,
            "candidates": list(candidates.values()),
            "candidate_events": candidate_events,
        }

    def add_verification_pair_active_elapsed(
        self, *, claim_id: str, ref_id: str, scope: str, elapsed_ms: float,
    ) -> None:
        """Charge active LLM work to a pair, never queue/backoff time."""
        if elapsed_ms <= 0:
            return
        claim_id = _validate_identifier("claim_id", claim_id)
        ref_id = _validate_identifier("ref_id", ref_id)
        with self._conn:
            self._conn.execute(
                """
                UPDATE verification_pair_state
                SET active_elapsed_ms = active_elapsed_ms + ?
                WHERE claim_id = ? AND ref_id = ? AND scope = ? AND status = 'open'
                """,
                (float(elapsed_ms), claim_id, ref_id, _clean_text(scope) or ""),
            )

    def claim_verification_pair_terminal(
        self,
        *,
        claim_id: str,
        ref_id: str,
        scope: str,
        status: str,
        outcome: str | None,
        cause: str | None,
        call_id: str | None = None,
    ) -> bool:
        """Atomically move one OPEN pair to a single terminal state.

        The append-only transition is written in the same transaction as the
        compare-and-set update.  A false return means another worker won.
        """
        if status not in {
            "accepted", "uncertain", "exhausted", "deadline_exceeded",
            "cancelled", "infrastructure_error",
        }:
            raise ValueError(f"invalid verification terminal status: {status}")
        claim_id = _validate_identifier("claim_id", claim_id)
        ref_id = _validate_identifier("ref_id", ref_id)
        scope = _clean_text(scope) or ""
        now = _now()
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE verification_pair_state
                SET status = ?, terminal_outcome = ?, terminal_cause = ?,
                    winner_call_id = ?, terminal_at = ?
                WHERE claim_id = ? AND ref_id = ? AND scope = ? AND status = 'open'
                """,
                (status, _clean_text(outcome), _clean_text(cause),
                 _clean_text(call_id), now, claim_id, ref_id, scope),
            )
            if cursor.rowcount != 1:
                state = self._conn.execute(
                    """
                    SELECT status FROM verification_pair_state
                    WHERE claim_id = ? AND ref_id = ? AND scope = ?
                    """,
                    (claim_id, ref_id, scope),
                ).fetchone()
                if state is None:
                    raise RuntimeError(
                        "verification pair state missing before terminal persistence")
                if state["status"] == "open":
                    raise RuntimeError(
                        "verification pair CAS failed while pair remained open")
                return False
            self._conn.execute(
                """
                INSERT INTO verification_pair_transitions(
                  claim_id, ref_id, scope, from_status, to_status, call_id, cause, created_at
                ) VALUES(?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                (claim_id, ref_id, scope, status, _clean_text(call_id),
                 _clean_text(cause), now),
            )
            self._conn.execute("UPDATE run SET updated_at = ?", (now,))
        return True

    def list_verification_lifecycle_violations(self) -> list[JsonDict]:
        """Return deterministic diagnostics from authoritative typed facts."""
        rows = self._conn.execute(
            """
            SELECT
              'terminal_pair_verify_task_not_applied' AS code,
              state.claim_id AS claim_id,
              state.ref_id AS ref_id,
              state.scope AS scope,
              task.task_id AS task_id,
              task.status AS task_status
            FROM verification_pair_state AS state
            JOIN tasks AS task
             ON task.slot = 'verify'
             AND task.claim_id = state.claim_id
             AND task.ref_id = state.ref_id
             AND COALESCE(task.scope, '') = state.scope
            WHERE state.status != 'open'
              AND COALESCE(task.scope, '') != 'composition'
              AND task.status != 'applied'
            ORDER BY claim_id, ref_id, scope, code, task_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def verify_run_gate_readiness(self) -> JsonDict:
        citations = self.list_citations()
        expected_pairs = {(row.claim_id, row.ref_id) for row in citations}
        refs_with_text = {
            row.ref_id for row in self.list_source_texts()
            if row.tier in {"fulltext", "abstract"}
            or (row.tier == "web" and row.origin == "googlebooks")
        }
        unreadable_refs = {
            row["ref_id"]
            for row in self._conn.execute(
                "SELECT DISTINCT ref_id FROM unreadable_sources "
                "WHERE ref_id IS NOT NULL AND ocr_status != 'done'"
            ).fetchall()
        }
        verification_facts = self.verification_raw_payloads()
        terminals = {
            (row["claim_id"], row["ref_id"])
            for row in verification_facts["pair_states"]
            if row["status"] != "open"
        }
        accepted_pairs = {
            (row["claim_id"], row["ref_id"])
            for row in verification_facts["pair_states"]
            if row["status"] == "accepted"
        }
        skipped = sorted(
            pair for pair in expected_pairs
            if pair[1] in refs_with_text and pair[1] not in unreadable_refs
            and pair not in terminals
        )
        no_text = sorted(
            pair for pair in expected_pairs
            if pair[1] not in refs_with_text and pair[1] not in unreadable_refs
        )
        pending_ocr = sorted(pair for pair in expected_pairs if pair[1] in unreadable_refs)
        ref_map = {row.ref_id: row for row in self.list_references()}

        def _label(pair: tuple[str, str]) -> str:
            ref = ref_map.get(pair[1])
            return f"{pair[0]}.[{ref.ref_number if ref else '?'}]"

        failures = []
        warnings = []
        if skipped:
            failures.append(
                f"{len(skipped)} (claim,source) pair(s) have source text but no terminal verification fact"
            )
        if no_text:
            warnings.append(f"{len(no_text)} pair(s) have no retrievable text")
        if pending_ocr:
            warnings.append(f"{len(pending_ocr)} pair(s) are pending OCR")
        return {
            "ok": not failures,
            "failures": failures,
            "warnings": warnings,
            "info": {
                "expected_pairs": len(expected_pairs),
                "pairs_with_text_verified": len(
                    {pair for pair in expected_pairs if pair[1] in refs_with_text}
                    & terminals
                ),
                "pairs_skipped": len(skipped),
                "pairs_skipped_labels": [_label(pair) for pair in skipped],
                "pairs_no_text": len(no_text),
                "pairs_no_text_labels": [_label(pair) for pair in no_text],
                "pairs_pending_ocr": len(pending_ocr),
                "pairs_pending_ocr_labels": [_label(pair) for pair in pending_ocr],
                "terminal_pairs": len(terminals),
                "accepted_pairs": len(accepted_pairs),
            },
        }

    def export_report_inputs(self) -> JsonDict:
        return {
            "run": _run_dict(self.get_run()),
            "parse": {
                "claims": [
                    _claim_dict(row, self._claim_marker_numbers(row.claim_id))
                    for row in self.list_claims()
                ],
                "references": [_reference_dict(row) for row in self.list_references()],
                "citations": [_citation_dict(row) for row in self.list_citations()],
            },
            "resolve": {
                row.ref_id: _resolve_result_export_dict(row)
                for row in self.list_resolve_results()
            },
            "source_texts": [_source_text_dict(row) for row in self.list_source_texts()],
            "unreadable_sources": [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM unreadable_sources ORDER BY unreadable_source_id"
                ).fetchall()
            ],
            "tasks": [_task_dict(row) for row in self.list_tasks()],
            "task_answers": [
                _task_answer_dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM task_answers ORDER BY submitted_at, answer_id"
                ).fetchall()
            ],
            "verification_raw": self.verification_raw_payloads(),
            "gate": self.verify_run_gate_readiness(),
        }

    def _count_tasks(self, *, status: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = ?",
            (status,),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def _count_tasks_by_slot(self, *, status: str) -> JsonDict:
        rows = self._conn.execute(
            """
            SELECT slot, COUNT(*) AS n
            FROM tasks
            WHERE status = ?
            GROUP BY slot
            ORDER BY slot
            """,
            (status,),
        ).fetchall()
        return {row["slot"]: int(row["n"]) for row in rows}

    def _next_action(self, phase: str) -> str:
        pending = self._count_tasks(status="pending")
        answered = self._count_tasks(status="answered")
        if phase == "done":
            return "none"
        if pending:
            if phase == "verify":
                kinds = {row[0] for row in self._conn.execute(
                    "SELECT DISTINCT task_kind FROM tasks WHERE status='pending'"
                )}
                if kinds == {"claim_evidence"}:
                    return "resume automatic claim-evidence verification; no manual verdict answers"
                if "source_identity_attestation" in kinds:
                    return "review source identity: tasks show, then tasks answer-review"
            return "fill pending task answers"
        if answered:
            return "resume to ingest completed task answers"
        return "resume"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value))
    if "\x00" in text:
        text = text.replace("\x00", "\uFFFD")
    return text


def _normalize_cited_coordinate_value(value: str) -> str:
    return " ".join(
        value.replace("‐", "-").replace("‑", "-").replace("‒", "-")
        .replace("–", "-").replace("—", "-").replace("−", "-").split()
    )


def _nullable_nonnegative_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or null")
    return value


_LINK_KEYS = {"url", "availability", "site", "content_type", "content_version", "intended_application", "discovered_via", "provenance", "identity_context", "identity_contexts", "identity_context_conflict"}
_CONTEXT_KEYS = {
    "title", "authors", "year", "identifiers", "provider", "provider_record_id",
    "first_author", "source_confidence", "canonical_host", "canonical_url",
    "landing_page_url", "expected_document_title", "official",
    "official_document_relation",
}

_EVIDENCE_BASE_KEYS = {
    "has_identifier", "has_searchable_title", "has_author", "has_year",
    "has_venue", "source_kind", "source_type_confidence",
    "source_type_evidence", "indexability", "checks_completed",
    "minimum_checks_completed", "best_candidate", "resolution_basis",
    "existence_confidence", "title_overlap", "metadata_match",
    "identifier_fallback", "synthetic_reference_risk",
}
_EVIDENCE_OPTIONAL_KEYS = {
    "fulltext_availability", "repair_exception", "repair_failed", "fetch_repair",
    "journal_authority", "issue_attestations",
    "bibliographic_adjudication",
    "resolver_coverage",
    "journal_alias_assessment", "bibliographic_suspicion",
}
_EVIDENCE_EXCEPTION_KEYS = {
    "resolution_basis", "exception", "has_identifier",
    "repair_exception", "repair_failed", "fetch_repair",
}
_METADATA_MINIMAL_KEYS = {"title_overlap", "matched_year"}
_METADATA_ORDINARY_KEYS = {
    "score", "title_overlap", "author_match", "cited_first_author",
    "matched_first_author", "year_match", "matched_year", "venue_overlap",
    "matched_venue",
}
_METADATA_ORDINAL_KEYS = {"ordinal_conflict", "cited_ordinals", "matched_ordinals"}
_METADATA_HARD_CONFLICT_KEYS = {
    "author_conflict", "year_conflict", "venue_conflict",
    "metadata_conflict", "hard_conflicts",
}
_METADATA_COORDINATE_COMPARISONS_KEY = "coordinate_comparisons"
_METADATA_COORDINATE_KINDS = {
    "container", "volume", "issue", "article_page_range", "chapter_page_range",
    "elocator", "article_number", "article_locator",
}
_METADATA_COORDINATE_STATUSES = {"match", "mismatch", "inconclusive"}
_METADATA_JOURNAL_AUTHORITY_KEYS = {
    "status", "registry", "registry_version", "record_id", "cited_venue",
    "canonical_title", "matched_alias", "match_basis", "snapshot_sha256",
}
_BIBLIOGRAPHIC_ADJUDICATION_KEYS = {
    "rule_version", "outcome", "identity_status", "check_status",
    "correction_status", "refutations",
}
_BIBLIOGRAPHIC_REFUTATION_KEYS = {
    "kind", "field", "cited_value", "observed_value", "source", "basis",
}
_FALLBACK_COMMON_KEYS = {"status", "via", "reason"}
_PROFILE_SOURCE_KINDS = {
    "article_like", "book_like", "chapter_like", "report_like",
    "webpage_like", "legal_instrument", "unknown", "article", "book", "webpage",
}
_PROFILE_RESOLUTION_BASES = {
    "doi", "pmid", "isbn", "identifier", "metadata_search",
    "book_title_search", "canonical_source", "institutional_web", "url", "none",
    "jstor_stable_identifier", "un_doc_symbol", "us_reporter_citation",
}
_RISK_SIGNALS = {
    "strong_identifier_corroborated", "hard_identifier_error",
    "high_index_article_like", "complete_core_bibliography",
    "venue_or_journal_shape", "expected_checks_completed",
    "plausible_candidate_bibliographically_incoherent",
    "best_candidate_below_borderline", "metadata_corroborated",
    "identifier_fallback_corroborated", "low_indexability",
    "pre_index_era",
}
_REPAIR_TRIGGERS = {"status=unresolved", "via=crossref_metadata"}
_CORROBORATE_SIGNALS = {"doi", "pmid", "title", "tokens", "url"}


def _exact_object(
    value: Any,
    *,
    required: set[str],
    optional: set[str] | None = None,
    name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"evidence_profile.{name} must be an object")
    optional = optional or set()
    keys = set(value)
    if not required <= keys or keys - required - optional:
        raise ValueError(f"evidence_profile.{name} has an invalid field set")
    return value


def _optional_text(value: Any, name: str, *, nonempty: bool = False) -> None:
    if value is None:
        return
    if type(value) is not str or (nonempty and not value.strip()):
        raise ValueError(f"evidence_profile.{name} must be text or null")


def _validate_resolved_identifier(value: Any) -> JsonDict | None:
    """Validate the single resolver-confirmed identifier for Fetch rehydration."""
    if value is None:
        return None
    if (
        type(value) is not dict
        or not {"type", "value"} <= set(value)
        or set(value) - {"type", "value", "validated_via"}
    ):
        raise ValueError("resolved_identifier must contain only type, value, and optional validated_via")
    identifier_type = value["type"]
    identifier_value = value["value"]
    if (
        type(identifier_type) is not str
        or type(identifier_value) is not str
        or not identifier_type.strip()
        or not identifier_value.strip()
        or "\x00" in identifier_type
        or "\x00" in identifier_value
    ):
        raise ValueError("resolved_identifier.type and resolved_identifier.value must be nonempty text")
    validated_via = value.get("validated_via")
    if validated_via is not None and (
        type(validated_via) is not str or not validated_via.strip() or "\x00" in validated_via
    ):
        raise ValueError("resolved_identifier.validated_via must be nonempty text when present")
    return {
        "type": identifier_type,
        "value": identifier_value,
        **({"validated_via": validated_via} if validated_via is not None else {}),
    }


def _fraction(value: Any, name: str, *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    if type(value) is not float or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"evidence_profile.{name} must be a finite float in [0,1]")


def _ordered_texts(
    value: Any,
    name: str,
    *,
    unique: bool,
) -> None:
    if not isinstance(value, list) or any(type(item) is not str or not item.strip() for item in value):
        raise ValueError(f"evidence_profile.{name} must be a list of non-empty strings")
    if unique and len(value) != len(set(value)):
        raise ValueError(f"evidence_profile.{name} must not contain duplicates")


def _validate_metadata_match(value: Any, name: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"evidence_profile.{name} must be an object or null")
    keys = set(value)
    if keys == _METADATA_MINIMAL_KEYS:
        _fraction(value["title_overlap"], f"{name}.title_overlap")
        if value["matched_year"] is not None and type(value["matched_year"]) is not int:
            raise ValueError(f"evidence_profile.{name}.matched_year must be an integer or null")
        return
    allowed = (
        _METADATA_ORDINARY_KEYS
        | _METADATA_ORDINAL_KEYS
        | _METADATA_HARD_CONFLICT_KEYS
        | {"year_mismatch_plausible", _METADATA_COORDINATE_COMPARISONS_KEY}
    )
    if not _METADATA_ORDINARY_KEYS <= keys or keys - allowed:
        raise ValueError(f"evidence_profile.{name} is not a supported metadata variant")
    _fraction(value["score"], f"{name}.score", nullable=False)
    _fraction(value["title_overlap"], f"{name}.title_overlap")
    _fraction(value["venue_overlap"], f"{name}.venue_overlap")
    for key in ("author_match", "year_match"):
        if type(value[key]) is not bool:
            raise ValueError(f"evidence_profile.{name}.{key} must be boolean")
    for key in ("cited_first_author", "matched_first_author", "matched_venue"):
        _optional_text(value[key], f"{name}.{key}")
    if value["matched_year"] is not None and type(value["matched_year"]) is not int:
        raise ValueError(f"evidence_profile.{name}.matched_year must be an integer or null")

    ordinal_keys = keys & _METADATA_ORDINAL_KEYS
    if ordinal_keys:
        if ordinal_keys != _METADATA_ORDINAL_KEYS or value["ordinal_conflict"] is not True:
            raise ValueError(f"evidence_profile.{name} has an incomplete ordinal-conflict bundle")
        for key in ("cited_ordinals", "matched_ordinals"):
            items = value[key]
            if (
                not isinstance(items, list)
                or not items
                or any(type(item) is not int for item in items)
                or items != sorted(set(items))
            ):
                raise ValueError(f"evidence_profile.{name}.{key} must be sorted unique integers")
    if "year_mismatch_plausible" in value and value["year_mismatch_plausible"] is not True:
        raise ValueError(f"evidence_profile.{name}.year_mismatch_plausible must be true")

    comparisons = value.get(_METADATA_COORDINATE_COMPARISONS_KEY)
    if comparisons is not None:
        if not isinstance(comparisons, list) or not comparisons:
            raise ValueError(f"evidence_profile.{name}.coordinate_comparisons is invalid")
        seen_kinds: set[str] = set()
        for comparison in comparisons:
            if not isinstance(comparison, dict) or set(comparison) != {
                "kind", "cited_value", "matched_value", "status",
            }:
                raise ValueError(f"evidence_profile.{name}.coordinate_comparisons has an invalid field set")
            kind = comparison["kind"]
            if kind not in _METADATA_COORDINATE_KINDS or kind in seen_kinds:
                raise ValueError(f"evidence_profile.{name}.coordinate_comparisons kind is invalid")
            seen_kinds.add(kind)
            _optional_text(comparison["cited_value"], f"{name}.coordinate_comparisons.cited_value", nonempty=True)
            matched_value = comparison["matched_value"]
            _optional_text(matched_value, f"{name}.coordinate_comparisons.matched_value", nonempty=True)
            status = comparison["status"]
            if status not in _METADATA_COORDINATE_STATUSES:
                raise ValueError(f"evidence_profile.{name}.coordinate_comparisons status is invalid")
            if status in {"match", "mismatch"} and matched_value is None:
                raise ValueError(f"evidence_profile.{name}.coordinate_comparisons values are inconsistent")

    conflict_keys = keys & _METADATA_HARD_CONFLICT_KEYS
    if conflict_keys:
        if conflict_keys != _METADATA_HARD_CONFLICT_KEYS or value["metadata_conflict"] is not True:
            raise ValueError(f"evidence_profile.{name} has an incomplete hard-conflict bundle")
        conflicts = value["hard_conflicts"]
        if (
            not isinstance(conflicts, list)
            or not conflicts
            or any(item not in {"author", "year", "venue"} for item in conflicts)
            or len(conflicts) != len(set(conflicts))
        ):
            raise ValueError(f"evidence_profile.{name}.hard_conflicts is invalid")
        for key, label in (
            ("author_conflict", "author"),
            ("year_conflict", "year"),
            ("venue_conflict", "venue"),
        ):
            if type(value[key]) is not bool or value[key] != (label in conflicts):
                raise ValueError(f"evidence_profile.{name}.{key} contradicts hard_conflicts")


def _validate_journal_authority(value: Any, name: str) -> None:
    if not isinstance(value, dict) or set(value) != _METADATA_JOURNAL_AUTHORITY_KEYS:
        raise ValueError(f"evidence_profile.{name} has an invalid field set")
    for key in (
        "status", "registry", "registry_version", "record_id", "cited_venue",
        "canonical_title",
    ):
        if value[key] is None:
            raise ValueError(f"evidence_profile.{name}.{key} is required")
        _optional_text(value[key], f"{name}.{key}", nonempty=True)
    status = value["status"]
    if status not in {"recognized", "unrecognized"}:
        raise ValueError(f"evidence_profile.{name}.status is invalid")
    for key in ("matched_alias", "match_basis"):
        _optional_text(value[key], f"{name}.{key}", nonempty=True)
    snapshot_sha256 = value["snapshot_sha256"]
    if (
        type(snapshot_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256) is None
    ):
        raise ValueError(f"evidence_profile.{name} snapshot hash is invalid")
    recognized = status == "recognized"
    if recognized != (value["matched_alias"] is not None):
        raise ValueError(f"evidence_profile.{name} alias is inconsistent")
    if recognized != (
        value["match_basis"] in {
            "canonical_title", "registered_alias",
        }
    ):
        raise ValueError(f"evidence_profile.{name} basis is inconsistent")


def _validate_bibliographic_adjudication(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _BIBLIOGRAPHIC_ADJUDICATION_KEYS:
        raise ValueError("evidence_profile.bibliographic_adjudication has an invalid field set")
    _optional_text(value["rule_version"], "bibliographic_adjudication.rule_version", nonempty=True)
    if value["outcome"] not in {
        "identified", "identified_with_errors", "refuted", "not_corroborated",
        "checks_incomplete",
    }:
        raise ValueError("evidence_profile.bibliographic_adjudication outcome is invalid")
    if value["identity_status"] not in {
        "identified", "identified_with_errors", "not_identified", "ambiguous",
    }:
        raise ValueError("evidence_profile.bibliographic_adjudication identity status is invalid")
    if value["check_status"] not in {"complete", "incomplete"}:
        raise ValueError("evidence_profile.bibliographic_adjudication check status is invalid")
    if value["correction_status"] not in {
        "not_needed", "identified", "ambiguous", "not_found", "not_attempted",
    }:
        raise ValueError("evidence_profile.bibliographic_adjudication correction status is invalid")
    refutations = value["refutations"]
    if not isinstance(refutations, list):
        raise ValueError("evidence_profile.bibliographic_adjudication refutations must be a list")
    for item in refutations:
        if not isinstance(item, dict) or set(item) != _BIBLIOGRAPHIC_REFUTATION_KEYS:
            raise ValueError("evidence_profile.bibliographic_adjudication refutation has an invalid field set")
        if item["kind"] not in {
            "identifier_not_found", "identifier_targets_other_work",
            "coordinate_mismatch", "coordinate_occupied_by_other_work", "year_mismatch",
            "author_mismatch", "absent_from_complete_issue",
        }:
            raise ValueError("evidence_profile.bibliographic_adjudication refutation kind is invalid")
        for key in ("field", "cited_value", "source", "basis"):
            _optional_text(item[key], f"bibliographic_adjudication.refutations[].{key}", nonempty=True)
        _optional_text(
            item["observed_value"],
            "bibliographic_adjudication.refutations[].observed_value",
            nonempty=True,
        )


def _validate_issue_attestations(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError("evidence_profile.issue_attestations must be a non-empty list")
    providers: set[str] = set()
    required = {"provider", "rule_version", "status", "reason", "scope", "target_status",
                "target_member_order", "cited_container", "cited_volume", "cited_issue",
                "journal_title", "completeness_basis", "members", "sources", "observations"}
    for item in value:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("evidence_profile.issue_attestations has an invalid field set")
        provider = item["provider"]
        if type(provider) is not str or not provider.strip() or provider in providers:
            raise ValueError("evidence_profile.issue_attestations provider is invalid")
        providers.add(provider)
        _optional_text(item["rule_version"], "issue_attestations.rule_version", nonempty=True)
        _optional_text(item["reason"], "issue_attestations.reason", nonempty=True)
        if item["status"] not in {"complete", "enumerated", "incomplete", "not_applicable"}:
            raise ValueError("evidence_profile.issue_attestations status is invalid")
        if item["target_status"] not in {"present", "absent", "inconclusive"}:
            raise ValueError("evidence_profile.issue_attestations target status is invalid")
        order = item["target_member_order"]
        if order is not None and (type(order) is not int or order < 0):
            raise ValueError("evidence_profile.issue_attestations target member order is invalid")
        for key in ("scope", "cited_container", "cited_volume", "cited_issue", "journal_title", "completeness_basis"):
            _optional_text(item[key], f"issue_attestations.{key}")
        members = item["members"]
        if not isinstance(members, list) or len({str(m.get("record_id")) for m in members if isinstance(m, dict)}) != len(members):
            raise ValueError("evidence_profile.issue_attestations members are invalid")
        for member in members:
            if not isinstance(member, dict) or set(member) != {"record_id", "title", "first_author", "year", "journal", "volume", "issue", "locator", "pmid", "pmcid", "doi", "url"}:
                raise ValueError("evidence_profile.issue_attestations member shape is invalid")
            _optional_text(member["record_id"], "issue_attestations.members.record_id", nonempty=True)
            _optional_text(member["title"], "issue_attestations.members.title", nonempty=True)
            for key in ("first_author", "journal", "volume", "issue", "locator", "pmid", "pmcid", "doi", "url"):
                _optional_text(member[key], f"issue_attestations.members.{key}")
            if member["year"] is not None and type(member["year"]) is not int:
                raise ValueError("evidence_profile.issue_attestations member year is invalid")
        for source in item["sources"]:
            if not isinstance(source, dict) or set(source) != {"role", "url", "response_sha256"}:
                raise ValueError("evidence_profile.issue_attestations source shape is invalid")
            _optional_text(source["role"], "issue_attestations.sources.role", nonempty=True)
            _optional_text(source["url"], "issue_attestations.sources.url")
            if source["response_sha256"] is not None and (type(source["response_sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", source["response_sha256"]) is None):
                raise ValueError("evidence_profile.issue_attestations source hash is invalid")
        for observation in item["observations"]:
            if not isinstance(observation, dict) or set(observation) != {"key", "value_type", "text_value", "integer_value"}:
                raise ValueError("evidence_profile.issue_attestations observation shape is invalid")
            _optional_text(observation["key"], "issue_attestations.observations.key", nonempty=True)
            if observation["value_type"] == "text" and isinstance(observation["text_value"], str) and observation["integer_value"] is None:
                continue
            if observation["value_type"] == "integer" and type(observation["integer_value"]) is int and observation["text_value"] is None:
                continue
            raise ValueError("evidence_profile.issue_attestations observation value is invalid")
        materialized = item["status"] in {"complete", "enumerated"}
        if materialized and (
            item["scope"] != "issue"
            or not item["completeness_basis"]
            or not members
            or not item["sources"]
            or any(not source["url"] or source["response_sha256"] is None for source in item["sources"])
        ):
            raise ValueError("evidence_profile.issue_attestations materialized state is incomplete")
        if item["status"] in {"incomplete", "not_applicable"} and (item["target_status"] != "inconclusive" or order is not None or members):
            raise ValueError("evidence_profile.issue_attestations incomplete state asserts evidence")
        if item["status"] == "enumerated" and item["target_status"] == "absent":
            raise ValueError("evidence_profile.issue_attestations enumeration cannot prove absence")
        if item["target_status"] == "present" and (order is None or order >= len(members)):
            raise ValueError("evidence_profile.issue_attestations present target is invalid")
        if item["target_status"] != "present" and order is not None:
            raise ValueError("evidence_profile.issue_attestations non-present target is invalid")


def _validate_identifier_fallback(value: Any) -> None:
    item = _exact_object(
        value,
        required=_FALLBACK_COMMON_KEYS,
        optional={
            "matched_title", "matched_authors", "abstract", "retracted",
            "fulltext_exists", "oa_status", "work_type", "resolution_basis",
            "existence_confidence", "metadata_match", "oa_declared_status",
            "oa_license_urls", "fulltext_links", "book_availability",
            "availability_note", "existence_corroboration",
        },
        name="identifier_fallback",
    )
    status = item["status"]
    via = item["via"]
    if type(status) is not str or type(via) is not str:
        raise ValueError("evidence_profile.identifier_fallback status/via must be text")
    _optional_text(item["reason"], "identifier_fallback.reason")

    if via == "crossref_metadata":
        if status == "unverified" and set(item) == _FALLBACK_COMMON_KEYS:
            _optional_text(item["reason"], "identifier_fallback.reason", nonempty=True)
            return
        weak_keys = _FALLBACK_COMMON_KEYS | {
            "matched_title", "resolution_basis", "existence_confidence", "metadata_match",
        }
        if status == "unverified" and set(item) == weak_keys:
            _optional_text(item["reason"], "identifier_fallback.reason", nonempty=True)
            _optional_text(item["matched_title"], "identifier_fallback.matched_title")
            if item["resolution_basis"] != "metadata_search" or item["existence_confidence"] != "low":
                raise ValueError("evidence_profile.identifier_fallback weak Crossref policy is invalid")
            _validate_metadata_match(item["metadata_match"], "identifier_fallback.metadata_match")
            if set(item["metadata_match"]) == _METADATA_MINIMAL_KEYS:
                raise ValueError("evidence_profile.identifier_fallback weak Crossref metadata must be ordinary")
            return
        required = _FALLBACK_COMMON_KEYS | {
            "matched_title", "abstract", "retracted", "resolution_basis",
            "existence_confidence", "metadata_match", "fulltext_exists",
            "oa_status", "work_type", "fulltext_links",
        }
        optional = {"matched_authors", "oa_declared_status", "oa_license_urls"}
        if status != "resolved" or not required <= set(item) or set(item) - required - optional:
            raise ValueError("evidence_profile.identifier_fallback Crossref variant is invalid")
        _optional_text(item["reason"], "identifier_fallback.reason", nonempty=True)
        _optional_text(item["matched_title"], "identifier_fallback.matched_title")
        if "matched_authors" in item:
            _ordered_texts(item["matched_authors"], "identifier_fallback.matched_authors", unique=False)
        _optional_text(item["abstract"], "identifier_fallback.abstract")
        _optional_text(item["work_type"], "identifier_fallback.work_type")
        if item["retracted"] is not False:
            raise ValueError("evidence_profile.identifier_fallback Crossref retracted must be false")
        if item["resolution_basis"] != "metadata_search" or item["existence_confidence"] != "medium":
            raise ValueError("evidence_profile.identifier_fallback resolved Crossref policy is invalid")
        if item["fulltext_exists"] not in (True, "unknown") or item["oa_status"] != "unknown":
            raise ValueError("evidence_profile.identifier_fallback Crossref availability is invalid")
        _validate_metadata_match(item["metadata_match"], "identifier_fallback.metadata_match")
        if set(item["metadata_match"]) == _METADATA_MINIMAL_KEYS:
            raise ValueError("evidence_profile.identifier_fallback Crossref metadata must be ordinary")
        if "oa_declared_status" in item and item["oa_declared_status"] not in {"open", "licensed"}:
            raise ValueError("evidence_profile.identifier_fallback.oa_declared_status is invalid")
        if "oa_license_urls" in item:
            _ordered_texts(item["oa_license_urls"], "identifier_fallback.oa_license_urls", unique=False)
        links = item["fulltext_links"]
        if not isinstance(links, list):
            raise ValueError("evidence_profile.identifier_fallback.fulltext_links must be a list")
        for link in links:
            _exact_object(
                link,
                required={"url"},
                optional={"content_type", "intended_application"},
                name="identifier_fallback.fulltext_links[]",
            )
            _optional_text(link["url"], "identifier_fallback.fulltext_links[].url", nonempty=True)
            for key in ("content_type", "intended_application"):
                if key in link:
                    _optional_text(link[key], f"identifier_fallback.fulltext_links[].{key}")
        return

    if via == "pubmed_citation_match":
        if status in {"unresolved", "not_found", "ambiguous"} and set(item) == _FALLBACK_COMMON_KEYS:
            _optional_text(item["reason"], "identifier_fallback.reason", nonempty=True)
            return
        required = _FALLBACK_COMMON_KEYS | {
            "matched_title", "matched_authors", "abstract", "retracted",
            "fulltext_exists", "oa_status", "work_type", "resolution_basis",
            "existence_confidence", "metadata_match", "fulltext_links",
        }
        if status not in {"resolved", "unverified"} or set(item) != required:
            raise ValueError("evidence_profile.identifier_fallback PubMed citation-match variant is invalid")
        _optional_text(item["reason"], "identifier_fallback.reason", nonempty=True)
        _optional_text(item["matched_title"], "identifier_fallback.matched_title")
        _ordered_texts(item["matched_authors"], "identifier_fallback.matched_authors", unique=False)
        _optional_text(item["abstract"], "identifier_fallback.abstract")
        if (
            item["retracted"] is not False
            or item["fulltext_exists"] != "unknown"
            or item["oa_status"] != "unknown"
            or item["work_type"] != "article"
            or item["resolution_basis"] != "metadata_search"
            or item["existence_confidence"] != ("medium" if status == "resolved" else "low")
            or item["fulltext_links"] != []
        ):
            raise ValueError("evidence_profile.identifier_fallback PubMed citation-match facts are invalid")
        _validate_metadata_match(item["metadata_match"], "identifier_fallback.metadata_match")
        return

    book_vias = {"openlibrary", "googlebooks", "openlibrary/googlebooks", "openlibrary+googlebooks"}
    if via not in book_vias:
        raise ValueError("evidence_profile.identifier_fallback via is unsupported")
    if status == "resolved":
        required = _FALLBACK_COMMON_KEYS | {
            "matched_title", "matched_authors", "abstract", "retracted",
            "fulltext_exists", "oa_status", "work_type", "book_availability",
            "availability_note", "existence_corroboration",
        }
        if set(item) != required or via not in {"openlibrary", "googlebooks"}:
            raise ValueError("evidence_profile.identifier_fallback resolved-book variant is invalid")
        _optional_text(item["matched_title"], "identifier_fallback.matched_title")
        _ordered_texts(item["matched_authors"], "identifier_fallback.matched_authors", unique=False)
        _optional_text(item["abstract"], "identifier_fallback.abstract")
        _optional_text(item["availability_note"], "identifier_fallback.availability_note")
        if (
            item["retracted"] is not False
            or item["fulltext_exists"] != "unknown"
            or item["oa_status"] != "unknown"
            or item["work_type"] != "book"
            or item["book_availability"] not in {"full", "partial", "none", "unknown"}
            or item["existence_corroboration"] != "corroborated"
        ):
            raise ValueError("evidence_profile.identifier_fallback resolved-book facts are invalid")
        return
    if status not in {"unresolved", "not_found", "unverified"}:
        raise ValueError("evidence_profile.identifier_fallback book status is invalid")
    _optional_text(item["reason"], "identifier_fallback.reason", nonempty=True)
    if set(item) - (_FALLBACK_COMMON_KEYS | {"existence_corroboration"}):
        raise ValueError("evidence_profile.identifier_fallback book failure has extra fields")
    if "existence_corroboration" in item and item["existence_corroboration"] not in {"searched_not_found", "none"}:
        raise ValueError("evidence_profile.identifier_fallback existence corroboration is invalid")


def _validate_exception_payload(value: Any, name: str) -> None:
    item = _exact_object(
        value,
        required={"type", "message", "traceback"},
        name=name,
    )
    for key in ("type", "message", "traceback"):
        if type(item[key]) is not str:
            raise ValueError(f"evidence_profile.{name}.{key} must be text")


def _validate_repair_overlays(value: dict[str, Any]) -> None:
    repair_exception = value.get("repair_exception")
    if repair_exception is not None:
        item = _exact_object(
            repair_exception,
            required={"trigger", "exception"},
            name="repair_exception",
        )
        if item["trigger"] not in _REPAIR_TRIGGERS:
            raise ValueError("evidence_profile.repair_exception.trigger is invalid")
        _validate_exception_payload(item["exception"], "repair_exception.exception")

    repair_failed = value.get("repair_failed")
    if repair_failed is not None:
        item = _exact_object(
            repair_failed,
            required={"trigger", "fetch_status", "fetch_method", "fetch_reason"},
            name="repair_failed",
        )
        if item["trigger"] not in _REPAIR_TRIGGERS:
            raise ValueError("evidence_profile.repair_failed.trigger is invalid")
        for key in ("fetch_status", "fetch_method", "fetch_reason"):
            _optional_text(item[key], f"repair_failed.{key}")

    fetch_repair = value.get("fetch_repair")
    if fetch_repair is None:
        return
    item = _exact_object(
        fetch_repair,
        required={
            "trigger", "original_status", "original_via", "stored_via",
            "stored_source_ref", "content_version", "corroborate_signal",
            "corroborate_score", "abstract_disposition",
        },
        name="fetch_repair",
    )
    if item["trigger"] not in _REPAIR_TRIGGERS:
        raise ValueError("evidence_profile.fetch_repair.trigger is invalid")
    for key in ("original_status", "original_via", "stored_via"):
        _optional_text(item[key], f"fetch_repair.{key}", nonempty=True)
    _optional_text(item["stored_source_ref"], "fetch_repair.stored_source_ref")
    if item["content_version"] not in {None, "published", "preprint", "accepted_manuscript"}:
        raise ValueError("evidence_profile.fetch_repair.content_version is invalid")
    if item["corroborate_signal"] is not None and item["corroborate_signal"] not in _CORROBORATE_SIGNALS:
        raise ValueError("evidence_profile.fetch_repair.corroborate_signal is invalid")
    _fraction(item["corroborate_score"], "fetch_repair.corroborate_score")

    disposition = item["abstract_disposition"]
    if not isinstance(disposition, dict) or disposition.get("action") not in {"absent", "kept", "suppressed"}:
        raise ValueError("evidence_profile.fetch_repair.abstract_disposition is invalid")
    if disposition["action"] == "absent":
        if set(disposition) != {"action"}:
            raise ValueError("evidence_profile.fetch_repair absent disposition has extra fields")
        return
    required = {"action", "corroborate_signal", "corroborate_score", "source_ref", "origin"}
    if disposition["action"] == "suppressed":
        required.add("reason")
    if set(disposition) != required:
        raise ValueError("evidence_profile.fetch_repair abstract disposition field set is invalid")
    if disposition["corroborate_signal"] is not None and disposition["corroborate_signal"] not in _CORROBORATE_SIGNALS:
        raise ValueError("evidence_profile.fetch_repair abstract signal is invalid")
    _fraction(disposition["corroborate_score"], "fetch_repair.abstract_disposition.corroborate_score", nullable=False)
    for key in ("source_ref", "origin"):
        _optional_text(disposition[key], f"fetch_repair.abstract_disposition.{key}", nonempty=True)
    if disposition["action"] == "suppressed" and disposition["reason"] != (
        "weak metadata abstract did not corroborate the fetch-repaired canonical source"
    ):
        raise ValueError("evidence_profile.fetch_repair suppression reason is invalid")


def _validate_resolve_evidence_profile(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("evidence_profile must be an object")
    value = _sanitize_json_value(value)
    if not isinstance(value, dict):  # for type checkers; sanitizer preserves mappings
        raise ValueError("evidence_profile must be an object")

    if value.get("resolution_basis") == "resolver_exception":
        _exact_object(
            value,
            required={"resolution_basis", "exception", "has_identifier"},
            optional={"repair_exception", "repair_failed", "fetch_repair"},
            name="resolver_exception",
        )
        if type(value["has_identifier"]) is not bool:
            raise ValueError("evidence_profile.has_identifier must be boolean")
        _validate_exception_payload(value["exception"], "exception")
        _validate_repair_overlays(value)
        return value

    _exact_object(
        value,
        required=_EVIDENCE_BASE_KEYS,
        optional=_EVIDENCE_OPTIONAL_KEYS,
        name="normal",
    )
    for key in (
        "has_identifier", "has_searchable_title", "has_author", "has_year",
        "has_venue", "minimum_checks_completed",
    ):
        if type(value[key]) is not bool:
            raise ValueError(f"evidence_profile.{key} must be boolean")
    if value["source_kind"] not in _PROFILE_SOURCE_KINDS:
        raise ValueError("evidence_profile.source_kind is invalid")
    if value["source_type_confidence"] not in {"high", "medium", "low", "unknown"}:
        raise ValueError("evidence_profile.source_type_confidence is invalid")
    if value["indexability"] not in {"high", "medium", "low"}:
        raise ValueError("evidence_profile.indexability is invalid")
    if value["resolution_basis"] not in _PROFILE_RESOLUTION_BASES:
        raise ValueError("evidence_profile.resolution_basis is invalid")
    if value["existence_confidence"] not in {"high", "medium", "low", "unknown"}:
        raise ValueError("evidence_profile.existence_confidence is invalid")
    _fraction(value["title_overlap"], "title_overlap")
    _ordered_texts(value["source_type_evidence"], "source_type_evidence", unique=False)
    _ordered_texts(value["checks_completed"], "checks_completed", unique=True)

    best = value["best_candidate"]
    if best is not None:
        best = _exact_object(
            best,
            required={"source", "title", "title_overlap", "metadata_match", "status", "reason"},
            optional={"authors"},
            name="best_candidate",
        )
        for key in ("source", "title", "status", "reason"):
            _optional_text(best[key], f"best_candidate.{key}")
        _fraction(best["title_overlap"], "best_candidate.title_overlap")
        if best["metadata_match"] is not None:
            _validate_metadata_match(best["metadata_match"], "best_candidate.metadata_match")
        if "authors" in best and best["authors"] is not None:
            _ordered_texts(best["authors"], "best_candidate.authors", unique=False)

    if value["metadata_match"] is not None:
        _validate_metadata_match(value["metadata_match"], "metadata_match")
    if value["identifier_fallback"] is not None:
        _validate_identifier_fallback(value["identifier_fallback"])
    if "journal_authority" in value:
        _validate_journal_authority(value["journal_authority"], "journal_authority")
        if value["journal_authority"]["status"] != "recognized":
            raise ValueError("evidence_profile.journal_authority must be recognized")
    if "journal_alias_assessment" in value:
        assessment = _exact_object(value["journal_alias_assessment"], required={
            "status", "registry", "registry_version", "cited_venue", "snapshot_sha256", "candidates",
        }, name="journal_alias_assessment")
        if assessment["status"] not in {"exact", "near_unconfirmed", "ambiguous", "unrecognized"}:
            raise ValueError("evidence_profile.journal_alias_assessment status is invalid")
        for key in ("registry", "registry_version", "cited_venue"):
            _optional_text(assessment[key], f"journal_alias_assessment.{key}", nonempty=True)
        if not isinstance(assessment["snapshot_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", assessment["snapshot_sha256"]) is None:
            raise ValueError("evidence_profile.journal_alias_assessment snapshot is invalid")
        candidates = assessment["candidates"]
        if not isinstance(candidates, list):
            raise ValueError("evidence_profile.journal_alias_assessment candidates are invalid")
        for candidate in candidates:
            _exact_object(candidate, required={"record_id", "canonical_title", "matched_alias", "distance"}, name="journal_alias_assessment.candidates[]")
            if any(not isinstance(candidate[key], str) or not candidate[key].strip() for key in ("record_id", "canonical_title", "matched_alias")) or candidate["distance"] not in {0, 1}:
                raise ValueError("evidence_profile.journal_alias_assessment candidate is invalid")
        status = assessment["status"]
        distances = {candidate["distance"] for candidate in candidates}
        record_ids = {candidate["record_id"] for candidate in candidates}
        if (
            (status == "exact" and (len(candidates) != 1 or distances != {0}))
            or (status == "near_unconfirmed" and (len(candidates) != 1 or distances != {1}))
            or (status == "ambiguous" and len(record_ids) < 2)
            or (status == "unrecognized" and candidates)
        ):
            raise ValueError("evidence_profile.journal_alias_assessment candidates contradict status")
    if "bibliographic_suspicion" in value:
        suspicion = _exact_object(value["bibliographic_suspicion"], required={"suspicion_level", "conclusion", "providers"}, name="bibliographic_suspicion")
        if suspicion["suspicion_level"] != "elevated" or not isinstance(suspicion["conclusion"], str) or not suspicion["conclusion"].strip():
            raise ValueError("evidence_profile.bibliographic_suspicion is invalid")
        _ordered_texts(suspicion["providers"], "bibliographic_suspicion.providers", unique=True)
        if len(suspicion["providers"]) < 2:
            raise ValueError("evidence_profile.bibliographic_suspicion needs independent providers")
    if "issue_attestations" in value:
        _validate_issue_attestations(value["issue_attestations"])
    if "bibliographic_adjudication" in value:
        _validate_bibliographic_adjudication(value["bibliographic_adjudication"])
    if "resolver_coverage" in value:
        coverage = _exact_object(value["resolver_coverage"], required={
            "authority", "suspicion_level", "conclusion", "article_lookups", "observations", "catalog", "payloads",
        }, name="resolver_coverage")
        authority = _exact_object(coverage["authority"], required={
            "record_id", "snapshot_sha256", "canonical_title", "issns", "authority_hash",
        }, name="resolver_coverage.authority")
        if (not isinstance(authority["issns"], list) or not authority["issns"]
                or any(not isinstance(item, str) or not item for item in authority["issns"])
                or any(not isinstance(authority[key], str) or not authority[key] for key in ("record_id", "snapshot_sha256", "canonical_title", "authority_hash"))):
            raise ValueError("evidence_profile.resolver_coverage authority is invalid")
        expected_authority_hash = hashlib.sha256(json.dumps({"record_id": authority["record_id"], "snapshot_sha256": authority["snapshot_sha256"], "canonical_title": authority["canonical_title"], "issns": authority["issns"]}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if authority["authority_hash"] != expected_authority_hash:
            raise ValueError("evidence_profile.resolver_coverage authority hash is invalid")
        if coverage["suspicion_level"] not in {"none", "elevated", "high"} or not isinstance(coverage["conclusion"], str):
            raise ValueError("evidence_profile.resolver_coverage is invalid")
        if not isinstance(coverage["observations"], list):
            raise ValueError("evidence_profile.resolver_coverage observations is invalid")
        required_observation = {"snapshot_id", "resolver", "rule_version", "status", "fresh", "checked_at", "expires_at", "query_contract", "completion", "source_url", "http_status", "response_sha256", "provider_journal_id", "work_count", "reason", "refresh_mode"}
        for observation in coverage["observations"]:
            _exact_object(observation, required=required_observation, name="resolver_coverage.observations[]")
            if observation["status"] not in {"covered", "not_covered", "incomplete"} or observation["completion"] not in {"complete", "partial", "incomplete"} or observation["refresh_mode"] not in {"auto", "manual"} or type(observation["fresh"]) is not bool or not isinstance(observation["resolver"], str) or not isinstance(observation["rule_version"], str) or not isinstance(observation["query_contract"], str) or not isinstance(observation["reason"], str):
                raise ValueError("evidence_profile.resolver_coverage observation is invalid")
        if not isinstance(coverage["article_lookups"], list):
            raise ValueError("evidence_profile.resolver_coverage article lookups is invalid")
        for lookup in coverage["article_lookups"]:
            _exact_object(lookup, required={"resolver", "query_contract", "scope", "completion", "match_status", "source_url", "http_status", "response_sha256", "reason"}, name="resolver_coverage.article_lookups[]")
            if lookup["completion"] not in {"complete", "partial", "incomplete"} or lookup["match_status"] not in {"no_compatible_article", "compatible", "ambiguous", "incomplete"} or not isinstance(lookup["reason"], str) or not lookup["reason"].strip():
                raise ValueError("evidence_profile.resolver_coverage article lookup is invalid")
        _exact_object(coverage["catalog"], required={"schema_version", "created_at", "catalog_sha256"}, name="resolver_coverage.catalog")
        if type(coverage["catalog"]["schema_version"]) is not int or coverage["catalog"]["schema_version"] < 1 or not isinstance(coverage["catalog"]["created_at"], str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T.*Z", coverage["catalog"]["created_at"]) or not re.fullmatch(r"[0-9a-f]{64}", coverage["catalog"]["catalog_sha256"]):
            raise ValueError("evidence_profile.resolver_coverage catalog is invalid")
        expected_catalog_hash = hashlib.sha256(json.dumps({"created_at": coverage["catalog"]["created_at"], "schema_version": coverage["catalog"]["schema_version"]}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if coverage["catalog"]["catalog_sha256"] != expected_catalog_hash:
            raise ValueError("evidence_profile.resolver_coverage catalog hash is invalid")
        if not isinstance(coverage["payloads"], list):
            raise ValueError("evidence_profile.resolver_coverage payloads is invalid")
        for payload in coverage["payloads"]:
            _exact_object(payload, required={"sha256", "media_type", "body"}, name="resolver_coverage.payloads[]")
            if not isinstance(payload["body"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["sha256"]) or hashlib.sha256(payload["body"].encode()).hexdigest() != payload["sha256"]:
                raise ValueError("evidence_profile.resolver_coverage payload hash is invalid")
        payload_hashes = {payload["sha256"] for payload in coverage["payloads"]}
        for item in [*coverage["observations"], *coverage["article_lookups"]]:
            response_sha = item["response_sha256"]
            if response_sha is not None and response_sha not in payload_hashes:
                raise ValueError("evidence_profile.resolver_coverage response payload is missing")

    risk = _exact_object(
        value["synthetic_reference_risk"],
        required={"score", "band", "signals"},
        optional={"reason"},
        name="synthetic_reference_risk",
    )
    signals = risk["signals"]
    if (
        not isinstance(signals, list)
        or any(signal not in _RISK_SIGNALS for signal in signals)
        or len(signals) != len(set(signals))
    ):
        raise ValueError("evidence_profile.synthetic_reference_risk.signals is invalid")
    if risk.get("reason") == "checks_incomplete":
        if risk != {"score": None, "band": "not_scored", "reason": "checks_incomplete", "signals": []}:
            raise ValueError("evidence_profile.synthetic_reference_risk incomplete variant is invalid")
    elif risk.get("reason") == "strong_identifier_corroborated":
        if risk != {
            "score": 0,
            "band": "none",
            "reason": "strong_identifier_corroborated",
            "signals": ["strong_identifier_corroborated"],
        }:
            raise ValueError("evidence_profile.synthetic_reference_risk strong-identifier variant is invalid")
    else:
        if "reason" in risk or type(risk["score"]) is not int or risk["score"] < 0:
            raise ValueError("evidence_profile.synthetic_reference_risk ordinary variant is invalid")
        expected_band = (
            "high" if risk["score"] >= 5
            else "medium" if risk["score"] >= 3
            else "low" if risk["score"] > 0
            else "none"
        )
        if risk["band"] != expected_band:
            raise ValueError("evidence_profile.synthetic_reference_risk band contradicts score")

    availability = value.get("fulltext_availability")
    if availability is not None:
        availability = _exact_object(
            availability,
            required={"status", "scope", "observed_by"},
            optional={"reason"},
            name="fulltext_availability",
        )
        if availability["status"] not in {"available", "unknown"} or availability["scope"] not in {"provider", "location"}:
            raise ValueError("evidence_profile.fulltext_availability enum is invalid")
        _optional_text(availability["observed_by"], "fulltext_availability.observed_by", nonempty=True)
        if "reason" in availability:
            if type(availability["reason"]) is not str:
                raise ValueError("evidence_profile.fulltext_availability.reason must be text")

    _validate_repair_overlays(value)
    return value


def _replace_resolve_evidence_profile(conn: sqlite3.Connection, ref_id: str, profile: dict[str, Any] | None) -> None:
    profile = _validate_resolve_evidence_profile(profile)
    tables = (
        "resolve_evidence_bibliographic_suspicion_providers", "resolve_evidence_bibliographic_suspicions",
        "resolve_evidence_journal_alias_candidates", "resolve_evidence_journal_alias_assessments",
        "resolve_evidence_resolver_coverage_article_lookups", "resolve_evidence_resolver_coverage_observations", "resolve_evidence_resolver_coverage_payloads", "resolve_evidence_resolver_coverage_catalogs", "resolve_evidence_resolver_coverage_issns", "resolve_evidence_resolver_coverages",
        "resolve_evidence_abstract_dispositions", "resolve_evidence_fetch_repairs",
        "resolve_evidence_repair_failed", "resolve_evidence_exceptions",
        "resolve_evidence_fulltext_availability", "resolve_evidence_risks",
        "resolve_evidence_issue_attestation_observations",
        "resolve_evidence_issue_attestation_sources",
        "resolve_evidence_issue_attestation_members",
        "resolve_evidence_issue_attestations",
        "resolve_evidence_bibliographic_refutations",
        "resolve_evidence_bibliographic_adjudications",
        "resolve_evidence_journal_authorities",
        "resolve_evidence_identifier_fallback_links",
        "resolve_evidence_identifier_fallback_licenses",
        "resolve_evidence_identifier_fallback_authors",
        "resolve_evidence_identifier_fallbacks", "resolve_evidence_best_candidates",
        "resolve_evidence_metadata_ordinals",
        "resolve_evidence_metadata_hard_conflicts",
        "resolve_evidence_metadata_coordinate_comparisons",
        "resolve_evidence_metadata_conflicts", "resolve_evidence_metadata_matches",
        "resolve_evidence_best_candidate_authors", "resolve_evidence_risk_signals",
        "resolve_evidence_source_type_evidence", "resolve_evidence_checks",
        "resolve_evidence_profiles", "resolve_evidence_profile_states",
    )
    for table in tables:
        conn.execute(f"DELETE FROM {table} WHERE ref_id=?", (ref_id,))
    conn.execute(
        "INSERT INTO resolve_evidence_profile_states(ref_id,is_null) VALUES(?,?)",
        (ref_id, int(profile is None)),
    )
    if profile is None:
        return

    def present_value(item: dict[str, Any], key: str) -> tuple[Any, int]:
        return item.get(key), int(key in item)

    kind = "resolver_exception" if profile["resolution_basis"] == "resolver_exception" else "normal"
    checks = list(profile.get("checks_completed", []))
    source_evidence = list(profile.get("source_type_evidence", []))
    conn.execute(
        """
        INSERT INTO resolve_evidence_profiles(
          ref_id,profile_kind,has_identifier,
          has_searchable_title,has_searchable_title_present,
          has_author,has_author_present,has_year,has_year_present,
          has_venue,has_venue_present,source_kind,source_kind_present,
          source_type_confidence,source_type_confidence_present,
          indexability,indexability_present,
          minimum_checks_completed,minimum_checks_completed_present,
          resolution_basis,existence_confidence,existence_confidence_present,
          title_overlap,title_overlap_present,checks_count,
          source_type_evidence_count,best_candidate_is_null,
          metadata_match_is_null,identifier_fallback_is_null,
          fulltext_availability_present,exception_present,
          repair_exception_present,repair_failed_present,fetch_repair_present,
          journal_authority_present,journal_alias_assessment_present,bibliographic_suspicion_present,issue_attestations_present,issue_attestations_count,
          bibliographic_adjudication_present
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ref_id, kind, int(profile["has_identifier"]),
            *present_value(profile, "has_searchable_title"),
            *present_value(profile, "has_author"),
            *present_value(profile, "has_year"),
            *present_value(profile, "has_venue"),
            *present_value(profile, "source_kind"),
            *present_value(profile, "source_type_confidence"),
            *present_value(profile, "indexability"),
            *present_value(profile, "minimum_checks_completed"),
            profile["resolution_basis"],
            *present_value(profile, "existence_confidence"),
            *present_value(profile, "title_overlap"),
            len(checks), len(source_evidence),
            int(profile.get("best_candidate") is None),
            int(profile.get("metadata_match") is None),
            int(profile.get("identifier_fallback") is None),
            int("fulltext_availability" in profile), int("exception" in profile),
            int("repair_exception" in profile), int("repair_failed" in profile),
            int("fetch_repair" in profile),
            int("journal_authority" in profile),
            int("journal_alias_assessment" in profile),
            int("bibliographic_suspicion" in profile),
            int("issue_attestations" in profile),
            len(profile.get("issue_attestations") or []),
            int("bibliographic_adjudication" in profile),
        ),
    )
    authority = profile.get("journal_authority")
    if authority is not None:
        conn.execute(
            "INSERT INTO resolve_evidence_journal_authorities VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                ref_id, authority["status"], authority["registry"],
                authority["registry_version"], authority["record_id"],
                authority["cited_venue"], authority["canonical_title"],
                authority["matched_alias"], authority["match_basis"],
                authority["snapshot_sha256"],
            ),
        )
    alias_assessment = profile.get("journal_alias_assessment")
    if alias_assessment is not None:
        conn.execute("INSERT INTO resolve_evidence_journal_alias_assessments VALUES(?,?,?,?,?,?)", (
            ref_id, alias_assessment["status"], alias_assessment["registry"],
            alias_assessment["registry_version"], alias_assessment["cited_venue"],
            alias_assessment["snapshot_sha256"],
        ))
        for order, candidate in enumerate(alias_assessment["candidates"]):
            conn.execute("INSERT INTO resolve_evidence_journal_alias_candidates VALUES(?,?,?,?,?,?)", (
                ref_id, order, candidate["record_id"], candidate["canonical_title"],
                candidate["matched_alias"], candidate["distance"],
            ))
    bibliographic_suspicion = profile.get("bibliographic_suspicion")
    if bibliographic_suspicion is not None:
        conn.execute("INSERT INTO resolve_evidence_bibliographic_suspicions VALUES(?,?,?)", (
            ref_id, bibliographic_suspicion["suspicion_level"], bibliographic_suspicion["conclusion"],
        ))
        for order, provider in enumerate(bibliographic_suspicion["providers"]):
            conn.execute("INSERT INTO resolve_evidence_bibliographic_suspicion_providers VALUES(?,?,?)", (ref_id, order, provider))
    coverage = profile.get("resolver_coverage")
    if coverage is not None:
        item = coverage["authority"]
        conn.execute("INSERT INTO resolve_evidence_resolver_coverages VALUES(?,?,?,?,?,?,?,?,?)", (
            ref_id, item["record_id"], item["snapshot_sha256"], item["canonical_title"], item["authority_hash"],
            coverage["suspicion_level"], coverage["conclusion"], int(any(item["completion"] == "complete" for item in coverage["article_lookups"])), "no_compatible_article" if any(item["match_status"] == "no_compatible_article" for item in coverage["article_lookups"]) else "incomplete",
        ))
        for issn_order, issn in enumerate(item["issns"]):
            conn.execute("INSERT INTO resolve_evidence_resolver_coverage_issns VALUES(?,?,?)", (ref_id, issn_order, issn))
        catalog = coverage["catalog"]
        conn.execute("INSERT INTO resolve_evidence_resolver_coverage_catalogs VALUES(?,?,?,?)", (ref_id, catalog["schema_version"], catalog["created_at"], catalog["catalog_sha256"]))
        for payload in coverage["payloads"]:
            conn.execute("INSERT INTO resolve_evidence_resolver_coverage_payloads VALUES(?,?,?,?)", (ref_id, payload["sha256"], payload["media_type"], payload["body"].encode()))
        for order, observation in enumerate(coverage["observations"]):
            conn.execute("INSERT INTO resolve_evidence_resolver_coverage_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                ref_id, order, observation["resolver"], observation["rule_version"], observation["snapshot_id"], observation["status"], int(observation["fresh"]), observation["checked_at"], observation["expires_at"], observation["query_contract"], observation["completion"], observation["source_url"], observation["http_status"], observation["response_sha256"], observation["provider_journal_id"], observation["work_count"], observation["reason"], observation["refresh_mode"],
            ))
        for order, lookup in enumerate(coverage["article_lookups"]):
            conn.execute("INSERT INTO resolve_evidence_resolver_coverage_article_lookups VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ref_id, order, lookup["resolver"], lookup["query_contract"], lookup["scope"], lookup["completion"], lookup["match_status"], lookup["source_url"], lookup["http_status"], lookup["response_sha256"], lookup["reason"]))
    for attestation_order, attestation in enumerate(profile.get("issue_attestations") or []):
        conn.execute("""INSERT INTO resolve_evidence_issue_attestations(
          ref_id,attestation_order,rule_version,provider,status,reason,scope,target_status,
          target_member_order,cited_container,cited_volume,cited_issue,journal_title,
          completeness_basis,members_count,sources_count,observations_count)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (ref_id, attestation_order, attestation["rule_version"], attestation["provider"],
           attestation["status"], attestation["reason"], attestation["scope"],
           attestation["target_status"], attestation["target_member_order"],
           attestation["cited_container"], attestation["cited_volume"], attestation["cited_issue"],
           attestation["journal_title"], attestation["completeness_basis"],
           len(attestation["members"]), len(attestation["sources"]),
           len(attestation["observations"])))
        for member_order, member in enumerate(attestation["members"]):
            conn.execute("""INSERT INTO resolve_evidence_issue_attestation_members(
              ref_id,attestation_order,member_order,record_id,pmcid,pmid,doi,title,first_author,year,journal,volume,issue,locator,url)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (ref_id, attestation_order, member_order, member["record_id"], member["pmcid"], member["pmid"], member["doi"], member["title"], member["first_author"], member["year"], member["journal"], member["volume"], member["issue"], member["locator"], member["url"]))
        for source_order, source in enumerate(attestation["sources"]):
            conn.execute("INSERT INTO resolve_evidence_issue_attestation_sources VALUES(?,?,?,?,?,?)",
                         (ref_id, attestation_order, source_order, source["role"], source["url"], source["response_sha256"]))
        for observation_order, observation in enumerate(attestation["observations"]):
            conn.execute("INSERT INTO resolve_evidence_issue_attestation_observations VALUES(?,?,?,?,?,?,?)",
                         (ref_id, attestation_order, observation_order, observation["key"], observation["value_type"], observation["text_value"], observation["integer_value"]))
    adjudication = profile.get("bibliographic_adjudication")
    if adjudication is not None:
        refutations = adjudication["refutations"]
        conn.execute(
            "INSERT INTO resolve_evidence_bibliographic_adjudications "
            "VALUES(?,?,?,?,?,?,?)",
            (
                ref_id, adjudication["rule_version"], adjudication["outcome"],
                adjudication["identity_status"], adjudication["check_status"],
                adjudication["correction_status"], len(refutations),
            ),
        )
        for order, item in enumerate(refutations):
            conn.execute(
                "INSERT INTO resolve_evidence_bibliographic_refutations "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    ref_id, order, item["kind"], item["field"],
                    item["cited_value"], item["observed_value"], item["source"],
                    item["basis"],
                ),
            )
    for order, text in enumerate(checks):
        conn.execute(
            "INSERT INTO resolve_evidence_checks(ref_id,check_order,check_name) VALUES(?,?,?)",
            (ref_id, order, text),
        )
    for order, text in enumerate(source_evidence):
        conn.execute(
            "INSERT INTO resolve_evidence_source_type_evidence(ref_id,evidence_order,evidence) VALUES(?,?,?)",
            (ref_id, order, text),
        )

    meta_scalar_keys = (
        "score", "title_overlap", "author_match", "cited_first_author",
        "matched_first_author", "year_match", "matched_year", "venue_overlap",
        "matched_venue",
    )

    def write_metadata(role: str, item: dict[str, Any] | None) -> None:
        if item is None:
            return
        values = tuple(
            value
            for key in meta_scalar_keys
            for value in (item.get(key), int(key in item))
        )
        conn.execute(
            "INSERT INTO resolve_evidence_metadata_matches VALUES("
            + ",".join("?" for _ in range(22)) + ")",
            (
                ref_id, role, *values,
                int(_METADATA_COORDINATE_COMPARISONS_KEY in item),
                len(item.get(_METADATA_COORDINATE_COMPARISONS_KEY, [])),
            ),
        )
        for order, comparison in enumerate(item.get(_METADATA_COORDINATE_COMPARISONS_KEY, [])):
            conn.execute(
                "INSERT INTO resolve_evidence_metadata_coordinate_comparisons VALUES(?,?,?,?,?,?,?)",
                (
                    ref_id, role, order, comparison["kind"], comparison["cited_value"],
                    comparison["matched_value"], comparison["status"],
                ),
            )
        conflict_keys = (
            "author_conflict", "metadata_conflict", "venue_conflict", "year_conflict",
            "ordinal_conflict", "year_mismatch_plausible",
        )
        conflict_values = tuple(
            value
            for key in conflict_keys
            for value in (
                None if key not in item else int(item[key]),
                int(key in item),
            )
        )
        hard_conflicts = list(item.get("hard_conflicts", []))
        cited_ordinals = list(item.get("cited_ordinals", []))
        matched_ordinals = list(item.get("matched_ordinals", []))
        conn.execute(
            """
            INSERT INTO resolve_evidence_metadata_conflicts(
              ref_id,role,author_conflict,author_conflict_present,
              metadata_conflict,metadata_conflict_present,
              venue_conflict,venue_conflict_present,year_conflict,year_conflict_present,
              ordinal_conflict,ordinal_conflict_present,
              year_mismatch_plausible,year_mismatch_plausible_present,
              hard_conflicts_present,hard_conflicts_count,
              cited_ordinals_count,matched_ordinals_count
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref_id, role, *conflict_values, int("hard_conflicts" in item),
                len(hard_conflicts), len(cited_ordinals), len(matched_ordinals),
            ),
        )
        for key, ordinal_kind in (
            ("cited_ordinals", "cited"), ("matched_ordinals", "matched")
        ):
            for order, ordinal in enumerate(item.get(key, [])):
                conn.execute(
                    "INSERT INTO resolve_evidence_metadata_ordinals VALUES(?,?,?,?,?)",
                    (ref_id, role, ordinal_kind, order, ordinal),
                )
        for order, conflict in enumerate(hard_conflicts):
            conn.execute(
                "INSERT INTO resolve_evidence_metadata_hard_conflicts VALUES(?,?,?,?)",
                (ref_id, role, order, conflict),
            )

    best = profile.get("best_candidate")
    fallback = profile.get("identifier_fallback")
    write_metadata("top", profile.get("metadata_match"))
    write_metadata("best", best.get("metadata_match") if best else None)
    write_metadata("fallback", fallback.get("metadata_match") if fallback else None)

    if best is not None:
        authors = list(best.get("authors") or [])
        scalar_values = tuple(
            value
            for key in ("source", "title", "title_overlap", "status", "reason")
            for value in present_value(best, key)
        )
        conn.execute(
            """
            INSERT INTO resolve_evidence_best_candidates(
              ref_id,source,source_present,title,title_present,
              title_overlap,title_overlap_present,status,status_present,
              reason,reason_present,metadata_match_present,metadata_match_is_null,
              authors_present,authors_is_null,authors_count
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref_id, *scalar_values, int("metadata_match" in best),
                int(best.get("metadata_match") is None), int("authors" in best),
                int("authors" in best and best["authors"] is None), len(authors),
            ),
        )
        for order, author in enumerate(authors):
            conn.execute(
                "INSERT INTO resolve_evidence_best_candidate_authors VALUES(?,?,?)",
                (ref_id, order, author),
            )

    if fallback is not None:
        scalar_keys = (
            "status", "via", "reason", "matched_title", "abstract", "retracted",
            "oa_status", "work_type", "resolution_basis", "existence_confidence",
            "oa_declared_status", "book_availability", "availability_note",
            "existence_corroboration",
        )
        scalar_values: list[Any] = []
        for key in scalar_keys:
            raw = fallback.get(key)
            if key == "retracted" and key in fallback:
                raw = int(raw)
            scalar_values.extend((raw, int(key in fallback)))
        fulltext_exists = fallback.get("fulltext_exists")
        fulltext_state = {
            True: "true", False: "false", None: None, "unknown": "unknown",
        }.get(fulltext_exists)
        authors = list(fallback.get("matched_authors") or [])
        licenses = list(fallback.get("oa_license_urls") or [])
        links = list(fallback.get("fulltext_links") or [])

        def list_kind(key: str) -> str:
            if key not in fallback:
                return "absent"
            return "null" if fallback[key] is None else "list"

        conn.execute(
            """
            INSERT INTO resolve_evidence_identifier_fallbacks(
              ref_id,status,status_present,via,via_present,reason,reason_present,
              metadata_match_present,matched_title,matched_title_present,
              abstract,abstract_present,retracted,retracted_present,
              fulltext_exists,fulltext_exists_present,oa_status,oa_status_present,
              work_type,work_type_present,resolution_basis,resolution_basis_present,
              existence_confidence,existence_confidence_present,
              oa_declared_status,oa_declared_status_present,
              book_availability,book_availability_present,
              availability_note,availability_note_present,
              existence_corroboration,existence_corroboration_present,
              matched_authors_kind,matched_authors_count,
              oa_license_urls_kind,oa_license_urls_count,
              fulltext_links_kind,fulltext_links_count
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref_id,
                *scalar_values[:6], int("metadata_match" in fallback),
                *scalar_values[6:10], *scalar_values[10:12],
                fulltext_state, int("fulltext_exists" in fallback),
                *scalar_values[12:],
                list_kind("matched_authors"), len(authors),
                list_kind("oa_license_urls"), len(licenses),
                list_kind("fulltext_links"), len(links),
            ),
        )
        for order, author in enumerate(authors):
            conn.execute(
                "INSERT INTO resolve_evidence_identifier_fallback_authors VALUES(?,?,?)",
                (ref_id, order, author),
            )
        for order, url in enumerate(licenses):
            conn.execute(
                "INSERT INTO resolve_evidence_identifier_fallback_licenses VALUES(?,?,?)",
                (ref_id, order, url),
            )
        for order, link in enumerate(links):
            conn.execute(
                "INSERT INTO resolve_evidence_identifier_fallback_links VALUES(?,?,?,?,?,?,?)",
                (
                    ref_id, order, link["url"], link.get("content_type"),
                    int("content_type" in link), link.get("intended_application"),
                    int("intended_application" in link),
                ),
            )

    risk = profile.get("synthetic_reference_risk")
    if risk is not None:
        conn.execute(
            """
            INSERT INTO resolve_evidence_risks(
              ref_id,score,score_present,band,band_present,reason,reason_present,signals_count
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                ref_id, risk["score"], 1, risk["band"], 1,
                risk.get("reason"), int("reason" in risk), len(risk["signals"]),
            ),
        )
        for order, signal in enumerate(risk["signals"]):
            conn.execute(
                "INSERT INTO resolve_evidence_risk_signals VALUES(?,?,?)",
                (ref_id, order, signal),
            )

    availability = profile.get("fulltext_availability")
    if availability is not None:
        values = tuple(
            value
            for key in ("status", "scope", "observed_by", "reason")
            for value in present_value(availability, key)
        )
        conn.execute(
            "INSERT INTO resolve_evidence_fulltext_availability VALUES(?,?,?,?,?,?,?,?,?)",
            (ref_id, *values),
        )

    def write_exception(role: str, overlay: dict[str, Any] | None) -> None:
        if overlay is None:
            return
        detail = overlay["exception"] if role == "repair" else overlay
        trigger = overlay.get("trigger") if role == "repair" else None
        conn.execute(
            """
            INSERT INTO resolve_evidence_exceptions(
              ref_id,role,trigger,trigger_present,type,type_present,
              message,message_present,traceback,traceback_present
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref_id, role, trigger, int(role == "repair"),
                detail["type"], 1, detail["message"], 1, detail["traceback"], 1,
            ),
        )

    write_exception("resolver", profile.get("exception"))
    write_exception("repair", profile.get("repair_exception"))

    failed = profile.get("repair_failed")
    if failed is not None:
        values = tuple(
            value
            for key in ("trigger", "fetch_status", "fetch_method", "fetch_reason")
            for value in present_value(failed, key)
        )
        conn.execute(
            "INSERT INTO resolve_evidence_repair_failed VALUES(?,?,?,?,?,?,?,?,?)",
            (ref_id, *values),
        )

    repair = profile.get("fetch_repair")
    if repair is not None:
        values = tuple(
            value
            for key in (
                "trigger", "original_status", "original_via", "stored_via",
                "stored_source_ref", "content_version", "corroborate_signal",
                "corroborate_score",
            )
            for value in present_value(repair, key)
        )
        conn.execute(
            "INSERT INTO resolve_evidence_fetch_repairs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ref_id, *values, 1),
        )
        disposition = repair["abstract_disposition"]
        conn.execute(
            """
            INSERT INTO resolve_evidence_abstract_dispositions(
              ref_id,action,reason,corroborate_signal,corroborate_score,source_ref,origin
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                ref_id, disposition["action"], disposition.get("reason"),
                disposition.get("corroborate_signal"),
                disposition.get("corroborate_score"), disposition.get("source_ref"),
                disposition.get("origin"),
            ),
        )


def _validate_resolve_fulltext_payload(payload: JsonDict) -> tuple[str, dict[str, tuple[bool, list[dict[str, Any]]]]]:
    state = payload.get("fulltext_exists")
    if state is True: stored = "true"
    elif state is False: stored = "false"
    elif state is None: stored = "null"
    elif state == "unknown": stored = "unknown"
    else: raise ValueError("fulltext_exists must be true, false, or unknown")
    result = {}
    for role, key in (("primary", "fulltext_links"), ("auxiliary", "auxiliary_fulltext_links")):
        value = payload.get(key)
        if value is None:
            result[role] = (True, [])
            continue
        if not isinstance(value, list): raise ValueError(f"{key} must be null or a list")
        normalized = []
        urls = set()
        for link in value:
            if not isinstance(link, dict) or set(link) - _LINK_KEYS: raise ValueError(f"{key} has unknown or invalid link fields")
            # Normalize text before writing each value into its typed relation.
            link = _sanitize_json_value(link)
            if not isinstance(link.get("url"), str) or not link["url"].strip() or link["url"] in urls: raise ValueError(f"{key} URL is invalid or duplicated")
            urls.add(link["url"])
            for name in ("availability", "site", "content_type", "content_version", "intended_application", "discovered_via"):
                if name in link and link[name] is not None and not isinstance(link[name], str): raise ValueError(f"{key}.{name} must be text")
            if "identity_context_conflict" in link and not isinstance(link["identity_context_conflict"], bool): raise ValueError(f"{key}.identity_context_conflict must be boolean")
            provenance = link.get("provenance", [])
            if not isinstance(provenance, list) or any(not isinstance(v, str) or not v for v in provenance): raise ValueError(f"{key}.provenance is invalid")
            if "identity_context" in link and not isinstance(link["identity_context"], dict):
                raise ValueError(f"{key}.identity_context is invalid")
            if "identity_contexts" in link and not isinstance(link["identity_contexts"], list):
                raise ValueError(f"{key}.identity_contexts is invalid")
            contexts = ([link["identity_context"]] if "identity_context" in link else []) + link.get("identity_contexts", [])
            if any(not isinstance(c, dict) or set(c) - _CONTEXT_KEYS for c in contexts): raise ValueError(f"{key}.identity context is invalid")
            for context in contexts:
                if "authors" in context and (not isinstance(context["authors"], list) or any(not isinstance(a, str) or not a for a in context["authors"])): raise ValueError(f"{key}.authors is invalid")
                if "identifiers" in context and (not isinstance(context["identifiers"], dict) or any(not isinstance(k, str) or not isinstance(v, str) or not k or not v for k,v in context["identifiers"].items())): raise ValueError(f"{key}.identifiers is invalid")
                for name in (
                    "title", "provider", "provider_record_id", "first_author",
                    "canonical_url", "landing_page_url", "expected_document_title",
                    "official_document_relation",
                ):
                    if name in context and context[name] is not None and not isinstance(context[name], str): raise ValueError(f"{key}.{name} is invalid")
                if "year" in context and context["year"] is not None and (isinstance(context["year"], bool) or not isinstance(context["year"], (int, str))): raise ValueError(f"{key}.year is invalid")
                if "source_confidence" in context and context["source_confidence"] is not None and (isinstance(context["source_confidence"], bool) or not isinstance(context["source_confidence"], (int,float)) or not math.isfinite(context["source_confidence"])): raise ValueError(f"{key}.source_confidence is invalid")
                if "canonical_host" in context and not isinstance(context["canonical_host"], bool): raise ValueError(f"{key}.canonical_host is invalid")
                if "official" in context and not isinstance(context["official"], bool): raise ValueError(f"{key}.official is invalid")
            normalized.append(dict(link))
        result[role] = (False, normalized)
    return stored, result


def _replace_resolve_fulltext_links(conn: sqlite3.Connection, ref_id: str, link_sets: dict[str, tuple[bool, list[dict[str, Any]]]]) -> None:
    for table in ("resolve_fulltext_link_context_identifiers", "resolve_fulltext_link_context_authors", "resolve_fulltext_link_contexts", "resolve_fulltext_link_provenance", "resolve_fulltext_links", "resolve_fulltext_link_sets"):
        conn.execute(f"DELETE FROM {table} WHERE ref_id = ?", (ref_id,))
    for role, (is_null, links) in link_sets.items():
        conn.execute("INSERT INTO resolve_fulltext_link_sets VALUES(?,?,?)", (ref_id, role, int(is_null)))
        for order, link in enumerate(links):
            conn.execute(
                """INSERT INTO resolve_fulltext_links(
                  ref_id,role,link_order,url,availability,availability_present,
                  site,site_present,content_type,content_type_present,
                  content_version,content_version_present,intended_application,
                  intended_application_present,discovered_via,discovered_via_present,
                  identity_context_conflict,provenance_present,
                  identity_context_present,identity_contexts_present
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ref_id, role, order, link["url"], link.get("availability"),
                    int("availability" in link), link.get("site"), int("site" in link),
                    link.get("content_type"), int("content_type" in link),
                    link.get("content_version"), int("content_version" in link),
                    link.get("intended_application"), int("intended_application" in link),
                    link.get("discovered_via"), int("discovered_via" in link),
                    None if "identity_context_conflict" not in link else int(link["identity_context_conflict"]),
                    int("provenance" in link), int("identity_context" in link),
                    int("identity_contexts" in link),
                ),
            )
            for po, value in enumerate(link.get("provenance", [])):
                conn.execute("INSERT INTO resolve_fulltext_link_provenance VALUES(?,?,?,?,?)", (ref_id,role,order,po,value))
            families = (("primary", [link["identity_context"]] if "identity_context" in link else []), ("accumulated", list(link.get("identity_contexts", []))))
            for family, contexts in families:
              for co, context in enumerate(contexts):
                year = context.get("year")
                year_kind = (
                    "absent" if "year" not in context else
                    "null" if year is None else
                    "integer" if isinstance(year, int) and not isinstance(year, bool) else
                    "text"
                )
                conn.execute(
                    """INSERT INTO resolve_fulltext_link_contexts(
                      ref_id,role,link_order,context_kind,context_order,
                      title,title_present,year_kind,year_integer,year_text,
            provider,provider_present,provider_record_id,
            provider_record_id_present,first_author,first_author_present,
            source_confidence,source_confidence_present,canonical_host,
            canonical_host_present,canonical_url,canonical_url_present,
            landing_page_url,landing_page_url_present,
            expected_document_title,expected_document_title_present,
            official,official_present,
            official_document_relation,official_document_relation_present,
            authors_present,identifiers_present
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        ref_id, role, order, family, co, context.get("title"),
                        int("title" in context), year_kind,
                        year if year_kind == "integer" else None,
                        year if year_kind == "text" else None,
            context.get("provider"), int("provider" in context),
            context.get("provider_record_id"), int("provider_record_id" in context),
            context.get("first_author"), int("first_author" in context),
            context.get("source_confidence"), int("source_confidence" in context),
            None if "canonical_host" not in context else int(context["canonical_host"]),
            int("canonical_host" in context),
            context.get("canonical_url"), int("canonical_url" in context),
            context.get("landing_page_url"), int("landing_page_url" in context),
            context.get("expected_document_title"), int("expected_document_title" in context),
            None if "official" not in context else int(context["official"]),
            int("official" in context), context.get("official_document_relation"),
            int("official_document_relation" in context), int("authors" in context),
                        int("identifiers" in context),
                    ),
                )
                for ao, author in enumerate(context.get("authors", [])): conn.execute("INSERT INTO resolve_fulltext_link_context_authors VALUES(?,?,?,?,?,?,?)", (ref_id,role,order,family,co,ao,author))
                for kind, value in context.get("identifiers", {}).items(): conn.execute("INSERT INTO resolve_fulltext_link_context_identifiers VALUES(?,?,?,?,?,?,?)", (ref_id,role,order,family,co,kind,value))


def _read_resolve_fulltext_links(conn: sqlite3.Connection, ref_id: str) -> tuple[Any, Any]:
    output = []
    for role in ("primary", "auxiliary"):
        marker = conn.execute("SELECT is_null FROM resolve_fulltext_link_sets WHERE ref_id=? AND role=?", (ref_id,role)).fetchone()
        if marker is None: raise RuntimeError("typed resolve fulltext link set is missing")
        link_rows = list(conn.execute("SELECT * FROM resolve_fulltext_links WHERE ref_id=? AND role=? ORDER BY link_order", (ref_id,role)))
        if marker["is_null"]:
            if link_rows: raise RuntimeError("null typed resolve link set has children")
            output.append(None); continue
        _require_dense_ordinals(link_rows, "link_order", "typed resolve links")
        links=[]
        for row in link_rows:
            link={"url":row["url"]}
            for key in ("availability","site","content_type","content_version","intended_application","discovered_via"):
                present = bool(row[f"{key}_present"])
                if not present and row[key] is not None:
                    raise RuntimeError("typed resolve link scalar presence is inconsistent")
                if present: link[key]=row[key]
            if row["identity_context_conflict"] is not None: link["identity_context_conflict"]=bool(row["identity_context_conflict"])
            provenance=[r["value"] for r in conn.execute("SELECT value FROM resolve_fulltext_link_provenance WHERE ref_id=? AND role=? AND link_order=? ORDER BY provenance_order", (ref_id,role,row["link_order"]))]
            _require_dense_ordinals(list(conn.execute("SELECT provenance_order FROM resolve_fulltext_link_provenance WHERE ref_id=? AND role=? AND link_order=? ORDER BY provenance_order", (ref_id,role,row["link_order"]))), "provenance_order", "typed resolve provenance")
            if not row["provenance_present"] and provenance: raise RuntimeError("typed resolve provenance is unexpected")
            if row["provenance_present"]: link["provenance"] = provenance
            families={"primary":[],"accumulated":[]}
            for ctx in conn.execute("SELECT * FROM resolve_fulltext_link_contexts WHERE ref_id=? AND role=? AND link_order=? ORDER BY context_kind,context_order", (ref_id,role,row["link_order"])):
                c={};
                for k in (
                    "title", "provider", "provider_record_id", "first_author",
                    "source_confidence", "canonical_url", "landing_page_url",
                    "expected_document_title", "official_document_relation",
                ):
                    present = bool(ctx[f"{k}_present"])
                    if not present and ctx[k] is not None:
                        raise RuntimeError("typed resolve context scalar presence is inconsistent")
                    if present: c[k]=ctx[k]
                year_kind = ctx["year_kind"]
                if year_kind == "null": c["year"] = None
                elif year_kind == "integer": c["year"] = ctx["year_integer"]
                elif year_kind == "text": c["year"] = ctx["year_text"]
                elif year_kind != "absent": raise RuntimeError("typed resolve context year kind is invalid")
                author_rows=list(conn.execute("SELECT author_order,author FROM resolve_fulltext_link_context_authors WHERE ref_id=? AND role=? AND link_order=? AND context_kind=? AND context_order=? ORDER BY author_order",(ref_id,role,row["link_order"],ctx["context_kind"],ctx["context_order"]))); _require_dense_ordinals(author_rows,"author_order","typed resolve authors"); authors=[r["author"] for r in author_rows]; identifiers={r["identifier_type"]:r["identifier_value"] for r in conn.execute("SELECT identifier_type,identifier_value FROM resolve_fulltext_link_context_identifiers WHERE ref_id=? AND role=? AND link_order=? AND context_kind=? AND context_order=? ORDER BY identifier_type",(ref_id,role,row["link_order"],ctx["context_kind"],ctx["context_order"]))};
                if ctx["authors_present"]: c["authors"] = authors
                if ctx["identifiers_present"]: c["identifiers"] = identifiers
                if not ctx["authors_present"] and authors: raise RuntimeError("typed resolve authors are unexpected")
                if not ctx["identifiers_present"] and identifiers: raise RuntimeError("typed resolve identifiers are unexpected")
                canonical_present = bool(ctx["canonical_host_present"])
                if canonical_present != (ctx["canonical_host"] is not None):
                    raise RuntimeError("typed resolve canonical-host presence is inconsistent")
                if canonical_present:c["canonical_host"]=bool(ctx["canonical_host"])
                official_present = bool(ctx["official_present"])
                if official_present != (ctx["official"] is not None):
                    raise RuntimeError("typed resolve official presence inconsistent")
                if official_present:
                    c["official"] = bool(ctx["official"])
                families[ctx["context_kind"]].append(c)
            for family in families:
                _require_dense_ordinals(list(conn.execute("SELECT context_order FROM resolve_fulltext_link_contexts WHERE ref_id=? AND role=? AND link_order=? AND context_kind=? ORDER BY context_order", (ref_id,role,row["link_order"],family))), "context_order", "typed resolve contexts")
            expected_primary = 1 if row["identity_context_present"] else 0
            if len(families["primary"]) != expected_primary: raise RuntimeError("typed resolve primary context presence is inconsistent")
            if not row["identity_contexts_present"] and families["accumulated"]: raise RuntimeError("typed resolve accumulated context is unexpected")
            if row["identity_context_present"]: link["identity_context"]=families["primary"][0]
            if row["identity_contexts_present"]: link["identity_contexts"]=families["accumulated"]
            links.append(link)
        output.append(links)
    _validate_resolve_fulltext_payload({
        "fulltext_exists": "unknown",
        "fulltext_links": output[0],
        "auxiliary_fulltext_links": output[1],
    })
    return tuple(output)


def _require_dense_ordinals(rows: list[Any], key: str, label: str) -> None:
    if [int(row[key]) for row in rows] != list(range(len(rows))):
        raise RuntimeError(f"{label} ordinals are not contiguous")


def _nullable_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer or null")
    return value


def _sanitize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[_clean_text(key) or ""] = _sanitize_json_value(item)
        return out
    return _clean_text(value)


def _claim_structural_provenance_json(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("claim structural_provenance must be an object or null")
    _validate_claim_structural_provenance(value, ValueError)
    sanitized = _sanitize_json_value(value)
    _validate_claim_structural_provenance(sanitized, ValueError)
    try:
        return json.dumps(
            sanitized,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("claim structural_provenance must be canonical JSON") from exc


def _read_claim_structural_provenance(value: Any) -> JsonDict | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("claim structural_provenance storage is invalid")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("claim structural_provenance JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("claim structural_provenance must decode to an object")
    _validate_claim_structural_provenance(decoded, RuntimeError)
    try:
        canonical = json.dumps(
            decoded, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("claim structural_provenance JSON is invalid") from exc
    if value != canonical:
        raise RuntimeError("claim structural_provenance JSON is not canonical")
    return decoded


def _validate_claim_structural_provenance(
    value: dict[str, Any], error_type: type[Exception],
) -> None:
    boundary_kinds = value.get("boundary_kinds")
    if "boundary_kinds" in value and (
        not isinstance(boundary_kinds, list)
        or not all(isinstance(kind, str) for kind in boundary_kinds)
    ):
        raise error_type(
            "claim structural_provenance boundary_kinds must be a list of strings"
        )


def _validate_setting_key(key: str) -> str:
    cleaned = _clean_text(key) or ""
    if not _SETTING_KEY_RE.fullmatch(cleaned):
        raise ValueError(f"invalid run setting key: {key!r}")
    return cleaned


def _validate_identifier(kind: str, value: str) -> str:
    cleaned = _clean_text(value) or ""
    if not _IDENT_RE.fullmatch(cleaned):
        raise ValueError(f"invalid {kind}: {value!r}")
    return cleaned


def _validated_ref_ids(ref_ids: list[str]) -> list[str]:
    if type(ref_ids) is not list:
        raise ValueError("reference ids must be a list")
    if any(type(ref_id) is not str for ref_id in ref_ids):
        raise ValueError("reference ids must be strings")
    normalized = [_validate_identifier("ref_id", ref_id) for ref_id in ref_ids]
    if len(set(normalized)) != len(normalized):
        raise ValueError("reference ids must be unique")
    return normalized


def _identifier_batches(values: list[str], *, size: int = 500):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _run_record(row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=row["status"],
        phase=row["phase"],
        input_path=row["input_path"],
        input_sha256=row["input_sha256"],
        accuracy=row["accuracy"],
        style=row["style"],
        model_id=row["model_id"],
        http_profile=row["http_profile"],
        challenge_mode=row["challenge_mode"],
        fixture_fingerprint=row["fixture_fingerprint"],
        parent_run_id=row["parent_run_id"],
        run_origin=row["run_origin"],
    )


def _run_session_record(row) -> RunSessionRecord:
    return RunSessionRecord(
        session_id=row["session_id"],
        started_at=row["started_at"],
        heartbeat_at=row["heartbeat_at"],
        ended_at=row["ended_at"],
        status=row["status"],
        pid=row["pid"],
        host=row["host"],
    )


def _phase_event_record(conn, row) -> PhaseEventRecord:
    payload = read_phase_event(conn, row)
    return PhaseEventRecord(
        event_id=int(row["event_id"]),
        phase=row["phase"],
        event_type=row["event_type"],
        created_at=row["created_at"],
        session_id=row["session_id"],
        payload=payload,
    )


def _claim_record(row) -> ClaimRecord:
    return ClaimRecord(
        claim_id=row["claim_id"],
        sentence=row["sentence"],
        context_window=row["context_window"],
        marker_raw=row["marker_raw"],
        claim_scope=row["claim_scope"],
        parser_sentence_index=row["parser_sentence_index"],
        marker_group_index=row["marker_group_index"],
        marker_group_count=row["marker_group_count"],
        marker_start=row["marker_start"],
        marker_end=row["marker_end"],
        structural_provenance=_read_claim_structural_provenance(
            row["structural_provenance_json"]
        ),
        claim_order=int(row["claim_order"]),
    )


def _reference_record(conn: sqlite3.Connection, row) -> ReferenceRecord:
    coordinates = []
    for coordinate in conn.execute(
        """SELECT coordinate_id,coordinate_kind,raw_value,normalized_value,rule_id,extractor_version
           FROM cited_bibliographic_coordinates WHERE ref_id=? ORDER BY coordinate_id""",
        (row["ref_id"],),
    ):
        span_rows = list(conn.execute(
                """SELECT span_order,raw_start,raw_end FROM cited_bibliographic_coordinate_spans
                   WHERE coordinate_id=? ORDER BY span_order""",
                (coordinate["coordinate_id"],),
            ))
        if [span["span_order"] for span in span_rows] != list(range(len(span_rows))):
            raise RuntimeError("cited coordinate raw spans are sparse")
        spans = [
            {"raw_start": span["raw_start"], "raw_end": span["raw_end"]}
            for span in span_rows
        ]
        try:
            raw_value = "".join(
                row["raw_entry"][span["raw_start"]:span["raw_end"]]
                for span in spans
            )
        except (IndexError, TypeError):
            raw_value = None
        if not spans or raw_value != coordinate["raw_value"]:
            raise RuntimeError("cited coordinate raw spans are inconsistent")
        if coordinate["normalized_value"] != _normalize_cited_coordinate_value(
            coordinate["raw_value"]
        ):
            raise RuntimeError("cited coordinate normalized value is inconsistent")
        coordinates.append({
            "kind": coordinate["coordinate_kind"],
            "raw_value": coordinate["raw_value"],
            "normalized_value": coordinate["normalized_value"],
            "rule_id": coordinate["rule_id"],
            "extractor_version": coordinate["extractor_version"],
            "spans": spans,
        })
    return ReferenceRecord(
        ref_id=row["ref_id"],
        ref_number=int(row["ref_number"]),
        raw_entry=row["raw_entry"],
        title=row["title"],
        doi=row["doi"],
        pmid=row["pmid"],
        isbn=row["isbn"],
        url=row["url"],
        year=row["year"],
        ay_surname=row["ay_surname"],
        ay_year=row["ay_year"],
        ay_suffix=row["ay_suffix"],
        source_type=row["source_type"],
        source_kind=row["source_kind"],
        indexability=row["indexability"],
        source_type_confidence=row["source_type_confidence"],
        cited_coordinates=tuple(coordinates),
    )


def _read_resolve_evidence_profile(conn: sqlite3.Connection, ref_id: str) -> dict[str, Any] | None:
    state = conn.execute(
        "SELECT is_null FROM resolve_evidence_profile_states WHERE ref_id=?", (ref_id,)
    ).fetchone()
    if state is None:
        raise RuntimeError("typed resolve evidence profile state is missing")
    row = conn.execute(
        "SELECT * FROM resolve_evidence_profiles WHERE ref_id=?", (ref_id,)
    ).fetchone()
    if bool(state["is_null"]):
        if row is not None:
            raise RuntimeError("null typed resolve evidence profile has payload")
        return None
    if row is None:
        raise RuntimeError("typed resolve evidence profile payload is missing")

    def read_fields(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        boolean_keys = {
            "author_match", "year_match", "author_conflict", "metadata_conflict",
            "venue_conflict", "year_conflict", "ordinal_conflict",
            "year_mismatch_plausible",
        }
        for key in keys:
            present = bool(source[f"{key}_present"])
            raw = source[key]
            if not present:
                if raw is not None:
                    raise RuntimeError(f"typed resolve evidence {key} presence is inconsistent")
                continue
            result[key] = bool(raw) if key in boolean_keys else raw
        return result

    def ordered_rows(
        sql: str,
        parameters: tuple[Any, ...],
        *,
        order_key: str,
        value_key: str,
        expected: int,
        label: str,
    ) -> list[Any]:
        rows = list(conn.execute(sql, parameters))
        _require_dense_ordinals(rows, order_key, label)
        if len(rows) != expected:
            raise RuntimeError(f"{label} count does not match its parent")
        return [item[value_key] for item in rows]

    out: dict[str, Any] = {
        "has_identifier": bool(row["has_identifier"]),
        "resolution_basis": row["resolution_basis"],
    }
    for key in (
        "has_searchable_title", "has_author", "has_year", "has_venue",
        "source_kind", "source_type_confidence", "indexability",
        "minimum_checks_completed", "existence_confidence", "title_overlap",
    ):
        values = read_fields(row, (key,))
        if key in values:
            out[key] = (
                bool(values[key])
                if key.startswith("has_") or key == "minimum_checks_completed"
                else values[key]
            )

    authority = conn.execute(
        "SELECT * FROM resolve_evidence_journal_authorities WHERE ref_id=?",
        (ref_id,),
    ).fetchone()
    if bool(row["journal_authority_present"]):
        if authority is None:
            raise RuntimeError("typed resolve journal authority is missing")
        out["journal_authority"] = {
            "status": authority["status"],
            "registry": authority["registry"],
            "registry_version": authority["registry_version"],
            "record_id": authority["record_id"],
            "cited_venue": authority["cited_venue"],
            "canonical_title": authority["canonical_title"],
            "matched_alias": authority["matched_alias"],
            "match_basis": authority["match_basis"],
            "snapshot_sha256": authority["snapshot_sha256"],
        }
        try:
            _validate_journal_authority(out["journal_authority"], "journal_authority")
        except ValueError as exc:
            raise RuntimeError("typed resolve journal authority is inconsistent") from exc
    elif authority is not None:
        raise RuntimeError("typed resolve journal authority is unexpected")

    alias = conn.execute(
        "SELECT * FROM resolve_evidence_journal_alias_assessments WHERE ref_id=?", (ref_id,),
    ).fetchone()
    alias_candidates = list(conn.execute(
        "SELECT * FROM resolve_evidence_journal_alias_candidates WHERE ref_id=? ORDER BY candidate_order", (ref_id,),
    ))
    _require_dense_ordinals(alias_candidates, "candidate_order", "typed journal alias candidates")
    if bool(row["journal_alias_assessment_present"]):
        if alias is None:
            raise RuntimeError("typed resolve journal alias assessment is missing")
        out["journal_alias_assessment"] = {
            "status": alias["status"], "registry": alias["registry"],
            "registry_version": alias["registry_version"], "cited_venue": alias["cited_venue"],
            "snapshot_sha256": alias["snapshot_sha256"], "candidates": [
                {key: item[key] for key in ("record_id", "canonical_title", "matched_alias", "distance")}
                for item in alias_candidates
            ],
        }
    elif alias is not None or alias_candidates:
        raise RuntimeError("typed resolve journal alias assessment is unexpected")

    suspicion = conn.execute(
        "SELECT * FROM resolve_evidence_bibliographic_suspicions WHERE ref_id=?", (ref_id,),
    ).fetchone()
    suspicion_providers = list(conn.execute(
        "SELECT * FROM resolve_evidence_bibliographic_suspicion_providers WHERE ref_id=? ORDER BY provider_order", (ref_id,),
    ))
    _require_dense_ordinals(suspicion_providers, "provider_order", "typed bibliographic suspicion providers")
    if bool(row["bibliographic_suspicion_present"]):
        if suspicion is None:
            raise RuntimeError("typed bibliographic suspicion is missing")
        out["bibliographic_suspicion"] = {
            "suspicion_level": suspicion["suspicion_level"], "conclusion": suspicion["conclusion"],
            "providers": [item["provider"] for item in suspicion_providers],
        }
    elif suspicion is not None or suspicion_providers:
        raise RuntimeError("typed bibliographic suspicion is unexpected")

    coverage = conn.execute(
        "SELECT * FROM resolve_evidence_resolver_coverages WHERE ref_id=?", (ref_id,)
    ).fetchone()
    coverage_rows = list(conn.execute(
        "SELECT * FROM resolve_evidence_resolver_coverage_observations WHERE ref_id=? ORDER BY observation_order", (ref_id,)
    ))
    _require_dense_ordinals(coverage_rows, "observation_order", "typed resolver coverage observations")
    coverage_issns = list(conn.execute("SELECT issn_order,issn FROM resolve_evidence_resolver_coverage_issns WHERE ref_id=? ORDER BY issn_order", (ref_id,)))
    coverage_lookups = list(conn.execute("SELECT * FROM resolve_evidence_resolver_coverage_article_lookups WHERE ref_id=? ORDER BY lookup_order", (ref_id,)))
    coverage_catalog = conn.execute("SELECT * FROM resolve_evidence_resolver_coverage_catalogs WHERE ref_id=?", (ref_id,)).fetchone()
    coverage_payloads = list(conn.execute("SELECT * FROM resolve_evidence_resolver_coverage_payloads WHERE ref_id=? ORDER BY payload_sha256", (ref_id,)))
    _require_dense_ordinals(coverage_issns, "issn_order", "typed resolver coverage ISSNs")
    _require_dense_ordinals(coverage_lookups, "lookup_order", "typed resolver coverage article lookups")
    if coverage is not None:
        if coverage_catalog is None:
            raise RuntimeError("typed resolver coverage catalog is missing")
        payloads = []
        for payload in coverage_payloads:
            body = bytes(payload["payload_body"])
            if hashlib.sha256(body).hexdigest() != payload["payload_sha256"]:
                raise RuntimeError("typed resolver coverage payload hash is inconsistent")
            payloads.append({"sha256": payload["payload_sha256"], "media_type": payload["media_type"], "body": body.decode()})
        out["resolver_coverage"] = {
            "authority": {
                "record_id": coverage["authority_record_id"],
                "snapshot_sha256": coverage["authority_snapshot_sha256"],
                "canonical_title": coverage["canonical_title"],
                "issns": [item["issn"] for item in coverage_issns], "authority_hash": coverage["authority_hash"],
            },
            "suspicion_level": coverage["suspicion_level"], "conclusion": coverage["conclusion"],
            "catalog": {"schema_version": coverage_catalog["schema_version"], "created_at": coverage_catalog["created_at"], "catalog_sha256": coverage_catalog["catalog_sha256"]},
            "payloads": payloads,
            "article_lookups": [{key: item[key] for key in ("resolver", "query_contract", "scope", "completion", "match_status", "source_url", "http_status", "response_sha256", "reason")} for item in coverage_lookups],
            "observations": [{
                "snapshot_id": item["catalog_snapshot_id"], "resolver": item["resolver"],
                "rule_version": item["rule_version"], "status": item["coverage_status"],
                "fresh": bool(item["fresh"]), "checked_at": item["checked_at"],
                "expires_at": item["expires_at"], "query_contract": item["query_contract"],
                "completion": item["completion"], "source_url": item["source_url"],
                "http_status": item["http_status"], "response_sha256": item["response_sha256"],
                "provider_journal_id": item["provider_journal_id"], "work_count": item["work_count"],
                "reason": item["reason"], "refresh_mode": item["refresh_mode"],
            } for item in coverage_rows],
        }
    elif coverage_rows or coverage_issns or coverage_lookups or coverage_catalog is not None or coverage_payloads:
        raise RuntimeError("typed resolver coverage observations are unexpected")

    attestations = list(conn.execute("SELECT * FROM resolve_evidence_issue_attestations WHERE ref_id=? ORDER BY attestation_order", (ref_id,)))
    _require_dense_ordinals(
        attestations, "attestation_order", "typed issue attestations",
    )
    if len(attestations) != int(row["issue_attestations_count"]):
        raise RuntimeError("typed issue attestation count does not match profile")
    if bool(row["issue_attestations_present"]):
        out["issue_attestations"] = []
        for parent in attestations:
            order = parent["attestation_order"]
            members = list(conn.execute("SELECT * FROM resolve_evidence_issue_attestation_members WHERE ref_id=? AND attestation_order=? ORDER BY member_order", (ref_id, order)))
            sources = list(conn.execute("SELECT * FROM resolve_evidence_issue_attestation_sources WHERE ref_id=? AND attestation_order=? ORDER BY source_order", (ref_id, order)))
            observations = list(conn.execute("SELECT * FROM resolve_evidence_issue_attestation_observations WHERE ref_id=? AND attestation_order=? ORDER BY observation_order", (ref_id, order)))
            _require_dense_ordinals(members, "member_order", "typed issue attestation members")
            _require_dense_ordinals(sources, "source_order", "typed issue attestation sources")
            _require_dense_ordinals(observations, "observation_order", "typed issue attestation observations")
            if len(members) != int(parent["members_count"]):
                raise RuntimeError("typed issue attestation member count does not match parent")
            if len(sources) != int(parent["sources_count"]):
                raise RuntimeError("typed issue attestation source count does not match parent")
            if len(observations) != int(parent["observations_count"]):
                raise RuntimeError("typed issue attestation observation count does not match parent")
            out["issue_attestations"].append({
                **{key: parent[key] for key in ("rule_version", "provider", "status", "reason", "scope", "target_status", "target_member_order", "cited_container", "cited_volume", "cited_issue", "journal_title", "completeness_basis")},
                "members": [{key: member[key] for key in ("record_id", "title", "first_author", "year", "journal", "volume", "issue", "locator", "pmid", "pmcid", "doi", "url")} for member in members],
                "sources": [{key: source[key] for key in ("role", "url", "response_sha256")} for source in sources],
                "observations": [{"key": observation["observation_key"], "value_type": observation["value_type"], "text_value": observation["text_value"], "integer_value": observation["integer_value"]} for observation in observations],
            })
        _validate_issue_attestations(out["issue_attestations"])
    elif attestations:
        raise RuntimeError("typed issue attestations are unexpected")

    adjudication = conn.execute(
        "SELECT * FROM resolve_evidence_bibliographic_adjudications WHERE ref_id=?",
        (ref_id,),
    ).fetchone()
    refutation_rows = list(conn.execute(
        "SELECT * FROM resolve_evidence_bibliographic_refutations "
        "WHERE ref_id=? ORDER BY refutation_order",
        (ref_id,),
    ))
    _require_dense_ordinals(
        refutation_rows, "refutation_order", "typed bibliographic refutations",
    )
    if bool(row["bibliographic_adjudication_present"]):
        if adjudication is None:
            raise RuntimeError("typed bibliographic adjudication is missing")
        if len(refutation_rows) != int(adjudication["refutations_count"]):
            raise RuntimeError("typed bibliographic refutation count does not match parent")
        out["bibliographic_adjudication"] = {
            "rule_version": adjudication["rule_version"],
            "outcome": adjudication["outcome"],
            "identity_status": adjudication["identity_status"],
            "check_status": adjudication["check_status"],
            "correction_status": adjudication["correction_status"],
            "refutations": [
                {
                    "kind": item["kind"], "field": item["field"],
                    "cited_value": item["cited_value"],
                    "observed_value": item["observed_value"],
                    "source": item["source"], "basis": item["basis"],
                }
                for item in refutation_rows
            ],
        }
        try:
            _validate_bibliographic_adjudication(out["bibliographic_adjudication"])
        except ValueError as exc:
            raise RuntimeError("typed bibliographic adjudication is inconsistent") from exc
    elif adjudication is not None or refutation_rows:
        raise RuntimeError("typed bibliographic adjudication is unexpected")

    checks = ordered_rows(
        "SELECT check_order,check_name FROM resolve_evidence_checks WHERE ref_id=? ORDER BY check_order",
        (ref_id,), order_key="check_order", value_key="check_name",
        expected=int(row["checks_count"]), label="typed resolve evidence checks",
    )
    source_evidence = ordered_rows(
        "SELECT evidence_order,evidence FROM resolve_evidence_source_type_evidence WHERE ref_id=? ORDER BY evidence_order",
        (ref_id,), order_key="evidence_order", value_key="evidence",
        expected=int(row["source_type_evidence_count"]),
        label="typed resolve source-type evidence",
    )
    if row["profile_kind"] == "normal":
        out["checks_completed"] = checks
        out["source_type_evidence"] = source_evidence
    elif checks or source_evidence:
        raise RuntimeError("resolver-exception profile has normal-profile children")

    meta_scalar_keys = (
        "score", "title_overlap", "author_match", "cited_first_author",
        "matched_first_author", "year_match", "matched_year", "venue_overlap",
        "matched_venue",
    )
    meta_rows = list(
        conn.execute(
            "SELECT * FROM resolve_evidence_metadata_matches WHERE ref_id=? ORDER BY role",
            (ref_id,),
        )
    )
    conflict_rows = {
        item["role"]: item
        for item in conn.execute(
            "SELECT * FROM resolve_evidence_metadata_conflicts WHERE ref_id=? ORDER BY role",
            (ref_id,),
        )
    }
    if set(conflict_rows) != {item["role"] for item in meta_rows}:
        raise RuntimeError("typed resolve metadata child state is incomplete")
    metas: dict[str, dict[str, Any]] = {}
    for meta_row in meta_rows:
        role = meta_row["role"]
        target = read_fields(meta_row, meta_scalar_keys)
        comparisons = list(conn.execute(
            """
            SELECT comparison_order,coordinate_kind,cited_value,matched_value,status
            FROM resolve_evidence_metadata_coordinate_comparisons
            WHERE ref_id=? AND role=? ORDER BY comparison_order
            """,
            (ref_id, role),
        ))
        _require_dense_ordinals(
            comparisons, "comparison_order", "typed resolve metadata coordinate comparisons",
        )
        comparisons_present = bool(meta_row["coordinate_comparisons_present"])
        if len(comparisons) != meta_row["coordinate_comparisons_count"] or (
            comparisons_present != bool(comparisons)
        ):
            raise RuntimeError(
                "typed resolve metadata coordinate comparison count does not match its parent"
            )
        if comparisons_present:
            target[_METADATA_COORDINATE_COMPARISONS_KEY] = [
                {
                    "kind": comparison["coordinate_kind"],
                    "cited_value": comparison["cited_value"],
                    "matched_value": comparison["matched_value"],
                    "status": comparison["status"],
                }
                for comparison in comparisons
            ]
        conflict = conflict_rows[role]
        target.update(
            read_fields(
                conflict,
                (
                    "author_conflict", "metadata_conflict", "venue_conflict",
                    "year_conflict", "ordinal_conflict", "year_mismatch_plausible",
                ),
            )
        )
        for key, ordinal_kind, expected_key in (
            ("cited_ordinals", "cited", "cited_ordinals_count"),
            ("matched_ordinals", "matched", "matched_ordinals_count"),
        ):
            ordinals = ordered_rows(
                """
                SELECT ordinal_order,ordinal_value
                FROM resolve_evidence_metadata_ordinals
                WHERE ref_id=? AND role=? AND ordinal_kind=?
                ORDER BY ordinal_order
                """,
                (ref_id, role, ordinal_kind), order_key="ordinal_order",
                value_key="ordinal_value", expected=int(conflict[expected_key]),
                label="typed resolve metadata ordinals",
            )
            if ordinals:
                target[key] = ordinals
        hard_conflicts = ordered_rows(
            """
            SELECT conflict_order,conflict
            FROM resolve_evidence_metadata_hard_conflicts
            WHERE ref_id=? AND role=? ORDER BY conflict_order
            """,
            (ref_id, role), order_key="conflict_order", value_key="conflict",
            expected=int(conflict["hard_conflicts_count"]),
            label="typed resolve metadata hard conflicts",
        )
        if bool(conflict["hard_conflicts_present"]):
            target["hard_conflicts"] = hard_conflicts
        elif hard_conflicts:
            raise RuntimeError("typed resolve metadata hard-conflict presence is inconsistent")
        try:
            _validate_metadata_match(target, f"{role}.metadata_match")
        except ValueError as exc:
            raise RuntimeError("typed resolve metadata match is inconsistent") from exc
        metas[role] = target

    if row["profile_kind"] == "normal":
        if bool(row["metadata_match_is_null"]):
            if "top" in metas:
                raise RuntimeError("null typed resolve metadata match has payload")
            out["metadata_match"] = None
        else:
            if "top" not in metas:
                raise RuntimeError("typed resolve metadata match is missing")
            out["metadata_match"] = metas["top"]
    elif "top" in metas:
        raise RuntimeError("resolver-exception profile has top metadata")

    best = conn.execute(
        "SELECT * FROM resolve_evidence_best_candidates WHERE ref_id=?", (ref_id,)
    ).fetchone()
    best_authors = list(
        conn.execute(
            """
            SELECT author_order,author FROM resolve_evidence_best_candidate_authors
            WHERE ref_id=? ORDER BY author_order
            """,
            (ref_id,),
        )
    )
    _require_dense_ordinals(best_authors, "author_order", "typed best-candidate authors")
    if row["profile_kind"] == "normal":
        if bool(row["best_candidate_is_null"]):
            if best is not None or best_authors or "best" in metas:
                raise RuntimeError("null typed best candidate has payload")
            out["best_candidate"] = None
        else:
            if best is None:
                raise RuntimeError("typed best candidate is missing")
            if len(best_authors) != int(best["authors_count"]):
                raise RuntimeError("typed best-candidate author count does not match parent")
            item = read_fields(best, ("source", "title", "title_overlap", "status", "reason"))
            if not bool(best["metadata_match_present"]):
                raise RuntimeError("typed best candidate metadata presence is missing")
            if bool(best["metadata_match_is_null"]):
                if "best" in metas:
                    raise RuntimeError("null typed best metadata has payload")
                item["metadata_match"] = None
            else:
                if "best" not in metas:
                    raise RuntimeError("typed best metadata is missing")
                item["metadata_match"] = metas["best"]
            if bool(best["authors_present"]):
                if bool(best["authors_is_null"]):
                    if best_authors:
                        raise RuntimeError("null typed best authors have children")
                    item["authors"] = None
                else:
                    item["authors"] = [author["author"] for author in best_authors]
            elif best_authors or int(best["authors_count"]):
                raise RuntimeError("absent typed best authors have children")
            out["best_candidate"] = item
    elif best is not None or best_authors or "best" in metas:
        raise RuntimeError("resolver-exception profile has best-candidate payload")

    fallback = conn.execute(
        "SELECT * FROM resolve_evidence_identifier_fallbacks WHERE ref_id=?", (ref_id,)
    ).fetchone()
    if row["profile_kind"] == "normal":
        if bool(row["identifier_fallback_is_null"]):
            if fallback is not None or "fallback" in metas:
                raise RuntimeError("null typed identifier fallback has payload")
            out["identifier_fallback"] = None
        else:
            if fallback is None:
                raise RuntimeError("typed identifier fallback is missing")
            item = read_fields(
                fallback,
                (
                    "status", "via", "reason", "matched_title", "abstract",
                    "retracted", "oa_status", "work_type", "resolution_basis",
                    "existence_confidence", "oa_declared_status",
                    "book_availability", "availability_note", "existence_corroboration",
                ),
            )
            if "retracted" in item:
                item["retracted"] = bool(item["retracted"])
            if bool(fallback["fulltext_exists_present"]):
                state_value = fallback["fulltext_exists"]
                if state_value not in {"true", "false", "unknown", None}:
                    raise RuntimeError("typed fallback fulltext state is invalid")
                item["fulltext_exists"] = {
                    "true": True, "false": False, "unknown": "unknown", None: None,
                }[state_value]
            elif fallback["fulltext_exists"] is not None:
                raise RuntimeError("typed fallback fulltext presence is inconsistent")
            if bool(fallback["metadata_match_present"]):
                if "fallback" not in metas:
                    raise RuntimeError("typed fallback metadata is missing")
                item["metadata_match"] = metas["fallback"]
            elif "fallback" in metas:
                raise RuntimeError("absent typed fallback metadata has payload")

            for key, table, order_key, value_key, count_key in (
                (
                    "matched_authors", "resolve_evidence_identifier_fallback_authors",
                    "author_order", "author", "matched_authors_count",
                ),
                (
                    "oa_license_urls", "resolve_evidence_identifier_fallback_licenses",
                    "license_order", "license_url", "oa_license_urls_count",
                ),
            ):
                values = ordered_rows(
                    f"SELECT {order_key},{value_key} FROM {table} WHERE ref_id=? ORDER BY {order_key}",
                    (ref_id,), order_key=order_key, value_key=value_key,
                    expected=int(fallback[count_key]), label=f"typed fallback {key}",
                )
                kind = fallback[f"{key}_kind"]
                if kind == "null":
                    if values:
                        raise RuntimeError(f"null typed fallback {key} has children")
                    item[key] = None
                elif kind == "list":
                    item[key] = values
                elif kind != "absent" or values:
                    raise RuntimeError(f"typed fallback {key} presence is inconsistent")

            link_rows = list(
                conn.execute(
                    """
                    SELECT * FROM resolve_evidence_identifier_fallback_links
                    WHERE ref_id=? ORDER BY link_order
                    """,
                    (ref_id,),
                )
            )
            _require_dense_ordinals(link_rows, "link_order", "typed fallback links")
            if len(link_rows) != int(fallback["fulltext_links_count"]):
                raise RuntimeError("typed fallback link count does not match parent")
            link_kind = fallback["fulltext_links_kind"]
            if link_kind == "null":
                if link_rows:
                    raise RuntimeError("null typed fallback links have children")
                item["fulltext_links"] = None
            elif link_kind == "list":
                links: list[dict[str, Any]] = []
                for link in link_rows:
                    link_item = {"url": link["url"]}
                    link_item.update(read_fields(link, ("content_type", "intended_application")))
                    links.append(link_item)
                item["fulltext_links"] = links
            elif link_kind != "absent" or link_rows:
                raise RuntimeError("typed fallback links presence is inconsistent")
            out["identifier_fallback"] = item
    elif fallback is not None or "fallback" in metas:
        raise RuntimeError("resolver-exception profile has identifier fallback payload")

    risk = conn.execute(
        "SELECT * FROM resolve_evidence_risks WHERE ref_id=?", (ref_id,)
    ).fetchone()
    if row["profile_kind"] == "normal":
        if risk is None:
            raise RuntimeError("typed synthetic-reference risk is missing")
        signals = ordered_rows(
            "SELECT signal_order,signal FROM resolve_evidence_risk_signals WHERE ref_id=? ORDER BY signal_order",
            (ref_id,), order_key="signal_order", value_key="signal",
            expected=int(risk["signals_count"]), label="typed synthetic-risk signals",
        )
        risk_item = read_fields(risk, ("score", "band", "reason"))
        risk_item["signals"] = signals
        out["synthetic_reference_risk"] = risk_item
    elif risk is not None:
        raise RuntimeError("resolver-exception profile has synthetic-risk payload")

    availability = conn.execute(
        "SELECT * FROM resolve_evidence_fulltext_availability WHERE ref_id=?", (ref_id,)
    ).fetchone()
    if bool(row["fulltext_availability_present"]):
        if availability is None:
            raise RuntimeError("typed fulltext availability is missing")
        out["fulltext_availability"] = read_fields(
            availability, ("status", "scope", "observed_by", "reason")
        )
    elif availability is not None:
        raise RuntimeError("absent typed fulltext availability has payload")

    exception_rows = {
        item["role"]: item
        for item in conn.execute(
            "SELECT * FROM resolve_evidence_exceptions WHERE ref_id=? ORDER BY role", (ref_id,)
        )
    }
    if bool(row["exception_present"]):
        resolver_exception = exception_rows.get("resolver")
        if resolver_exception is None:
            raise RuntimeError("typed resolver exception is missing")
        out["exception"] = read_fields(
            resolver_exception, ("type", "message", "traceback")
        )
    elif "resolver" in exception_rows:
        raise RuntimeError("absent typed resolver exception has payload")
    if bool(row["repair_exception_present"]):
        repair_exception = exception_rows.get("repair")
        if repair_exception is None:
            raise RuntimeError("typed repair exception is missing")
        out["repair_exception"] = {
            **read_fields(repair_exception, ("trigger",)),
            "exception": read_fields(repair_exception, ("type", "message", "traceback")),
        }
    elif "repair" in exception_rows:
        raise RuntimeError("absent typed repair exception has payload")

    failed = conn.execute(
        "SELECT * FROM resolve_evidence_repair_failed WHERE ref_id=?", (ref_id,)
    ).fetchone()
    if bool(row["repair_failed_present"]):
        if failed is None:
            raise RuntimeError("typed repair-failed payload is missing")
        out["repair_failed"] = read_fields(
            failed, ("trigger", "fetch_status", "fetch_method", "fetch_reason")
        )
    elif failed is not None:
        raise RuntimeError("absent typed repair-failed state has payload")

    repair = conn.execute(
        "SELECT * FROM resolve_evidence_fetch_repairs WHERE ref_id=?", (ref_id,)
    ).fetchone()
    disposition = conn.execute(
        "SELECT * FROM resolve_evidence_abstract_dispositions WHERE ref_id=?", (ref_id,)
    ).fetchone()
    if bool(row["fetch_repair_present"]):
        if repair is None:
            raise RuntimeError("typed fetch-repair payload is missing")
        repair_item = read_fields(
            repair,
            (
                "trigger", "original_status", "original_via", "stored_via",
                "stored_source_ref", "content_version", "corroborate_signal",
                "corroborate_score",
            ),
        )
        if not bool(repair["abstract_disposition_present"]) or disposition is None:
            raise RuntimeError("typed abstract disposition is missing")
        disposition_item: dict[str, Any] = {"action": disposition["action"]}
        if disposition["action"] == "absent" and any(
            disposition[key] is not None
            for key in (
                "reason", "corroborate_signal", "corroborate_score", "source_ref", "origin"
            )
        ):
            raise RuntimeError("typed absent abstract disposition has detail fields")
        if disposition["action"] == "kept" and disposition["reason"] is not None:
            raise RuntimeError("typed kept abstract disposition has a suppression reason")
        if disposition["action"] in {"kept", "suppressed"}:
            disposition_item.update(
                {
                    "corroborate_signal": disposition["corroborate_signal"],
                    "corroborate_score": disposition["corroborate_score"],
                    "source_ref": disposition["source_ref"],
                    "origin": disposition["origin"],
                }
            )
        if disposition["action"] == "suppressed":
            disposition_item["reason"] = disposition["reason"]
        repair_item["abstract_disposition"] = disposition_item
        out["fetch_repair"] = repair_item
    elif repair is not None or disposition is not None:
        raise RuntimeError("absent typed fetch-repair state has payload")

    try:
        validated = _validate_resolve_evidence_profile(out)
    except ValueError as exc:
        raise RuntimeError("typed resolve evidence profile is internally inconsistent") from exc
    if validated is None:
        raise RuntimeError("typed resolve evidence profile unexpectedly decoded as null")
    return validated


def _citation_record(row) -> CitationRecord:
    return CitationRecord(
        citation_id=int(row["citation_id"]),
        claim_id=row["claim_id"],
        ref_id=row["ref_id"],
        ref_number=int(row["ref_number"]),
    )


def _resolve_result_record(row, conn: sqlite3.Connection) -> ResolveResultRecord:
    fulltext_links, auxiliary_links = _read_resolve_fulltext_links(conn, row["ref_id"])
    attempts = _read_resolve_attempts(conn, row["ref_id"])
    trace = None
    trace_sections = None
    trace = _read_resolution_trace(
        conn,
        row["ref_id"],
        expected_attempt_count=(len(attempts) if type(attempts) is list else None),
    )
    if trace is not None:
        if type(attempts) is not list:
            raise RuntimeError("produced resolution trace has no attempt list")
        trace_sections = _split_resolution_trace(trace)
    return ResolveResultRecord(
        ref_id=row["ref_id"],
        status=row["status"],
        via=row["via"],
        matched_title=row["matched_title"],
        abstract=row["abstract"],
        abstract_via=row["abstract_via"],
        retracted=bool(row["retracted"]),
        fulltext_exists={"true": True, "false": False, "unknown": "unknown", "null": None}[row["fulltext_exists"]],
        oa_status=row["oa_status"],
        work_type=row["work_type"],
        resolution_basis=row["resolution_basis"],
        existence_confidence=row["existence_confidence"],
        reason=row["reason"],
        reference_status_tag=row["reference_status_tag"],
        fabrication_risk=row["fabrication_risk"],
        resolved_identifier=(
            {
                "type": row["resolved_identifier_type"],
                "value": row["resolved_identifier_value"],
                **(
                    {"validated_via": row["resolved_identifier_validated_via"]}
                    if row["resolved_identifier_validated_via"] is not None
                    else {}
                ),
            }
            if row["resolved_identifier_type"] is not None
            else None
        ),
        tag_reason=row["tag_reason"],
        fulltext_links=fulltext_links,
        auxiliary_fulltext_links=auxiliary_links,
        evidence_profile=_read_resolve_evidence_profile(conn, row["ref_id"]),
        attempts=attempts,
        trace=trace,
        resolver_attempts=(
            trace_sections["resolver_attempts"] if trace_sections is not None
            else None
        ),
        identifier_validations=(
            trace_sections["identifier_validations"] if trace_sections is not None
            else None
        ),
        retraction_checks=(
            trace_sections["retraction_checks"] if trace_sections is not None
            else None
        ),
        weak_corroboration_events=(
            trace_sections["weak_corroboration_events"] if trace_sections is not None
            else None
        ),
        updated_at=row["updated_at"],
    )


def _fetch_attempt_dict(row, conn: sqlite3.Connection) -> JsonDict:
    return {
        "fetch_attempt_id": int(row["fetch_attempt_id"]),
        "ref_id": row["ref_id"],
        "method": row["method"],
        "url": row["url"],
        "kind": row["kind"],
        "final_url": row["final_url"],
        "status_code": row["status_code"],
        "content_type": row["content_type"],
        "outcome": row["outcome"],
        "reason": row["reason"],
        "challenge_blocked": bool(row["challenge_blocked"]),
        "paywalled": bool(row["paywalled"]),
        "trace": read_fetch_trace(conn, int(row["fetch_attempt_id"])),
        "origin": row["origin"],
        "created_at": row["created_at"],
    }


def _source_text_record(row, conn: sqlite3.Connection) -> SourceTextRecord:
    return SourceTextRecord(
        source_text_id=row["source_text_id"],
        ref_id=row["ref_id"],
        identity_key=row["identity_key"],
        tier=row["tier"],
        origin=row["origin"],
        stored_path=row["stored_path"],
        sha256=row["sha256"],
        char_count=int(row["char_count"]),
        source_ref=row["source_ref"],
        mapping=row["mapping"],
        match_signal=row["match_signal"],
        match_score=row["match_score"],
        identity_status=row["identity_status"],
        identity_note=row["identity_note"],
        content_version=row["content_version"],
        provenance_relation=row["provenance_relation"],
        supplied_by=row["supplied_by"],
        supplied_via=row["supplied_via"],
        file_format=row["file_format"],
        extraction_flags=read_source_flags(conn, row["source_text_id"]),
        extraction_method=row["extraction_method"],
        recorded_at=row["recorded_at"],
    )


def _task_record(row, conn: sqlite3.Connection, run_dir: str) -> TaskRecord:
    try:
        payload = read_task_payload(conn, run_dir, row)
    except ValueError as exc:
        raise RuntimeError("typed task projection is internally inconsistent") from exc
    return TaskRecord(
        task_id=row["task_id"],
        slot=row["slot"],
        ref_id=row["ref_id"],
        claim_id=row["claim_id"],
        scope=row["scope"],
        status=row["status"],
        task_kind=row["task_kind"], generation=int(row["generation"]),
        task_payload=payload,
        created_at=row["created_at"],
        answered_at=row["answered_at"],
        applied_at=row["applied_at"],
    )


def _task_answer_record(row, conn: sqlite3.Connection) -> TaskAnswerRecord:
    return TaskAnswerRecord(
        answer_id=row["answer_id"],
        task_id=row["task_id"],
        actor_type=row["actor_type"],
        generation=int(row["generation"]), answer_kind=row["answer_kind"],
        raw_payload=read_task_answer(conn, row),
        submitted_at=row["submitted_at"],
        accepted_for_processing=bool(row["accepted_for_processing"]),
    )


def _run_dict(row: RunRecord) -> JsonDict:
    return {
        "run_id": row.run_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "status": row.status,
        "phase": row.phase,
        "input_path": row.input_path,
        "input_sha256": row.input_sha256,
        "accuracy": row.accuracy,
        "style": row.style,
        "model_id": row.model_id,
        "http_profile": row.http_profile,
        "challenge_mode": row.challenge_mode,
        "fixture_fingerprint": row.fixture_fingerprint,
        "parent_run_id": row.parent_run_id,
        "run_origin": row.run_origin,
    }


def _claim_dict(row: ClaimRecord, marker_numbers: list[object]) -> JsonDict:
    out: JsonDict = {
        "id": row.claim_id,
        "claim_id": row.claim_id,
        "sentence": row.sentence,
        "context_window": row.context_window,
        "marker_raw": row.marker_raw,
        "claim_scope": row.claim_scope,
        "parser_sentence_index": row.parser_sentence_index,
        "marker_group_index": row.marker_group_index,
        "marker_group_count": row.marker_group_count,
        "marker_start": row.marker_start,
        "marker_end": row.marker_end,
        "marker_numbers": marker_numbers,
        "claim_order": row.claim_order,
    }
    if row.structural_provenance is not None:
        out["structural_provenance"] = row.structural_provenance
    return out


def _reference_dict(row: ReferenceRecord) -> JsonDict:
    out = {
        "id": row.ref_id,
        "ref_id": row.ref_id,
        "ref_number": row.ref_number,
        "raw_entry": row.raw_entry,
        "title": row.title,
        "doi": row.doi,
        "pmid": row.pmid,
        "isbn": row.isbn,
        "url": row.url,
        "year": row.year,
        "ay_surname": row.ay_surname,
        "ay_year": row.ay_year,
        "ay_suffix": row.ay_suffix,
        "source_type": row.source_type,
        "source_kind": row.source_kind,
        "indexability": row.indexability,
        "source_type_confidence": row.source_type_confidence,
    }
    if row.cited_coordinates:
        out["cited_coordinates"] = [dict(coordinate) for coordinate in row.cited_coordinates]
    return out


def _citation_dict(row: CitationRecord) -> JsonDict:
    return {
        "citation_id": row.citation_id,
        "claim_id": row.claim_id,
        "ref_id": row.ref_id,
        "ref_number": row.ref_number,
    }


def _resolve_result_export_dict(row: ResolveResultRecord) -> JsonDict:
    evidence_profile = row.evidence_profile or {}
    payload = {
        "ref_id": row.ref_id,
        "status": row.status,
        "via": row.via,
        "matched_title": row.matched_title,
        "abstract": row.abstract,
        "abstract_via": row.abstract_via,
        "retracted": row.retracted,
        "fulltext_exists": row.fulltext_exists,
        "oa_status": row.oa_status,
        "work_type": row.work_type,
        "resolution_basis": row.resolution_basis,
        "existence_confidence": row.existence_confidence,
        "reason": row.reason,
        "reference_status_tag": row.reference_status_tag,
        "fabrication_risk": row.fabrication_risk,
        "tag_reason": row.tag_reason,
        "fulltext_links": row.fulltext_links,
        "auxiliary_fulltext_links": row.auxiliary_fulltext_links,
        "evidence_profile": evidence_profile,
        "attempts": row.attempts,
        "trace": row.trace,
        "resolver_attempts": row.resolver_attempts,
        "identifier_validations": row.identifier_validations,
        "retraction_checks": row.retraction_checks,
        "weak_corroboration_events": row.weak_corroboration_events,
        "checked_at": row.updated_at,
    }
    if row.resolved_identifier is not None:
        payload["resolved_identifier"] = row.resolved_identifier
    if evidence_profile.get("fulltext_availability") is not None:
        payload["fulltext_availability"] = evidence_profile["fulltext_availability"]
    strong_identity = _persisted_strong_doi_identity(row)
    if strong_identity is not None:
        identity_state, identity = strong_identity
        payload["identity_state"] = identity_state
        payload["identity"] = identity
    return payload


def _persisted_strong_doi_identity(
    row: ResolveResultRecord,
) -> tuple[str, JsonDict] | None:
    """Project a DOI identity only from a self-consistent typed trace."""
    trace = row.trace
    if not isinstance(trace, list):
        return None

    final_rows = [
        item for item in trace
        if isinstance(item, dict) and item.get("stage") == "final_resolution"
    ]
    if len(final_rows) != 1:
        return None
    final = final_rows[0]
    final_detail = final.get("detail")
    identity_state = final.get("outcome")
    if (
        not isinstance(final_detail, dict)
        or identity_state not in {
            "resolved_strong_declared", "resolved_strong_discovered",
        }
        or final_detail.get("status") != row.status
        or final_detail.get("via") != row.via
    ):
        return None

    confirmations = [
        item for item in trace
        if isinstance(item, dict) and item.get("stage") == "strong_confirmed"
    ]
    if len(confirmations) != 1:
        return None
    confirmation = confirmations[0]
    confirmation_detail = confirmation.get("detail")
    if (
        confirmation.get("outcome") != "confirmed"
        or not isinstance(confirmation_detail, dict)
        or confirmation_detail.get("identity_state") != identity_state
        or confirmation_detail.get("resolution_basis") != row.resolution_basis
    ):
        return None

    identity_stage = (
        "declared_present"
        if identity_state == "resolved_strong_declared"
        else "strong_discovery"
    )
    identity_rows = [
        item for item in trace
        if isinstance(item, dict) and item.get("stage") == identity_stage
    ]
    if len(identity_rows) != 1:
        return None
    identity = identity_rows[0].get("detail")
    if (
        not isinstance(identity, dict)
        or identity.get("scheme") != "doi"
        or identity.get("is_strong") is not True
        or identity.get("class_") != "global"
        or not isinstance(identity.get("value"), str)
        or not identity["value"].strip()
    ):
        return None
    return identity_state, {
        "scheme": "doi",
        "value": identity["value"],
        "is_strong": True,
        "class_": "global",
    }


def _source_text_dict(row: SourceTextRecord | None) -> JsonDict | None:
    if row is None:
        return None
    return {
        "source_text_id": row.source_text_id,
        "ref_id": row.ref_id,
        "identity_key": row.identity_key,
        "tier": row.tier,
        "origin": row.origin,
        "stored_path": row.stored_path,
        "sha256": row.sha256,
        "char_count": row.char_count,
        "source_ref": row.source_ref,
        "mapping": row.mapping,
        "match_signal": row.match_signal,
        "match_score": row.match_score,
        "identity_status": row.identity_status,
        "identity_note": row.identity_note,
        "content_version": row.content_version,
        "extraction_flags": row.extraction_flags,
        "extraction_method": row.extraction_method,
        "recorded_at": row.recorded_at,
    }


def _task_dict(row: TaskRecord) -> JsonDict:
    return {
        "task_id": row.task_id,
        "slot": row.slot,
        "ref_id": row.ref_id,
        "claim_id": row.claim_id,
        "scope": row.scope,
        "status": row.status,
        "task_payload": row.task_payload,
        "created_at": row.created_at,
        "answered_at": row.answered_at,
        "applied_at": row.applied_at,
    }


def _task_view(row: TaskRecord, answer: TaskAnswerRecord | None) -> JsonDict:
    payload = dict(row.task_payload or {})
    payload["task_id"] = row.task_id
    payload["slot"] = row.slot
    payload.setdefault("ref_id", row.ref_id)
    payload.setdefault("claim_id", row.claim_id)
    payload.setdefault("scope", row.scope)
    if row.status == "applied":
        payload["status"] = payload.get("status") or "done"
    else:
        payload["status"] = "pending"
    payload["db_status"] = row.status
    payload["created_at"] = row.created_at
    payload["answered_at"] = row.answered_at
    payload["applied_at"] = row.applied_at
    if answer is not None and row.status in ("answered", "applied"):
        payload["answer"] = answer.raw_payload
        payload["answer_meta"] = {
            "answer_id": answer.answer_id,
            "actor_type": answer.actor_type,
            "submitted_at": answer.submitted_at,
            "accepted_for_processing": answer.accepted_for_processing,
        }
    else:
        payload.setdefault("answer", None)
    return payload


def _task_answer_dict(row: TaskAnswerRecord) -> JsonDict:
    return {
        "answer_id": row.answer_id,
        "task_id": row.task_id,
        "actor_type": row.actor_type,
        "raw_payload": row.raw_payload,
        "submitted_at": row.submitted_at,
        "accepted_for_processing": row.accepted_for_processing,
    }


def _is_stale(timestamp: str, stale_after_seconds: int) -> bool:
    heartbeat = datetime.fromisoformat(timestamp)
    return (datetime.now(timezone.utc) - heartbeat).total_seconds() > stale_after_seconds


def _tier_rank_sql(column: str) -> str:
    return (
        f"CASE {column} "
        "WHEN 'fulltext' THEN 3 "
        "WHEN 'abstract' THEN 2 "
        "WHEN 'web' THEN 1 "
        "ELSE 0 END"
    )
