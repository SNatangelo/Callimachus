# core/infra/integrity/gate.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed application gate backed by the external integrity authority."""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from core.infra.db import RunRepository

from .authority import AuthorityError
from .client import IntegrityAuthorityClient


ENV_INTEGRITY_SOCKET = "CITATION_VERIFIER_INTEGRITY_SOCKET"
DEBUG_INTEGRITY_LABEL = "artifact_integrity_debug"
DEBUG_OVERRIDE_LABEL = "artifact_integrity_override"


class IntegrityGateError(RuntimeError):
    """The operation cannot continue under the trusted integrity contract."""


class ArtifactIntegrityViolation(IntegrityGateError):
    """The authority observed artifacts different from the trusted checkpoint."""


class ContentStoreIntegrityViolation(IntegrityGateError):
    """The shared content store differs from its global trusted checkpoint."""


@dataclass(frozen=True)
class PipelineIntegrityLease:
    transition_id: str
    run_lease_id: str
    content_store_lease_id: str | None


def _clean_reason(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise IntegrityGateError("artifact integrity debug override requires a reason")
        return None
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise IntegrityGateError(
            "artifact integrity debug override reason must be non-empty NUL-free text"
        )
    return value.strip()


@dataclass(frozen=True)
class RunIntegrityGate:
    """Coordinates authority operations with their run-local audit mirror."""

    client: IntegrityAuthorityClient

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "RunIntegrityGate":
        values = os.environ if environ is None else environ
        socket_path = str(values.get(ENV_INTEGRITY_SOCKET) or "").strip()
        if not socket_path:
            raise IntegrityGateError(
                f"{ENV_INTEGRITY_SOCKET} is required for every mutating run operation"
            )
        return cls(IntegrityAuthorityClient(socket_path))

    @staticmethod
    def _open_repo(run_dir: str) -> RunRepository:
        try:
            return RunRepository.open(run_dir)
        except Exception as exc:
            raise IntegrityGateError(
                f"cannot open run database for integrity mirror: {run_dir}"
            ) from exc

    @staticmethod
    def _checkpoint_parts(row: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(row, dict) or "files" not in row:
            raise IntegrityGateError("authority checkpoint history is malformed")
        record = {key: value for key, value in row.items() if key != "files"}
        files = row["files"]
        if not isinstance(files, list):
            raise IntegrityGateError("authority checkpoint file history is malformed")
        return record, files

    @staticmethod
    def _violation_parts(row: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(row, dict) or "differences" not in row:
            raise IntegrityGateError("authority violation history is malformed")
        record = {key: value for key, value in row.items() if key != "differences"}
        differences = row["differences"]
        if not isinstance(differences, list):
            raise IntegrityGateError("authority violation difference history is malformed")
        return record, differences

    @staticmethod
    def _recovery_parts(
        row: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(row, dict) or "entries" not in row:
            raise IntegrityGateError("authority crash recovery history is malformed")
        record = {key: value for key, value in row.items() if key != "entries"}
        entries = row["entries"]
        if not isinstance(entries, list):
            raise IntegrityGateError("authority crash recovery entries are malformed")
        return record, entries

    def _mirror_audit_records(
        self,
        run_dir: str,
        audit_records: Any,
        *,
        expected_state: str,
    ) -> None:
        if not isinstance(audit_records, dict) or set(audit_records) != {
            "checkpoints",
            "violations",
            "overrides",
            "recoveries",
        }:
            raise IntegrityGateError("authority audit history is malformed")
        checkpoints = audit_records["checkpoints"]
        violations = audit_records["violations"]
        overrides = audit_records["overrides"]
        recoveries = audit_records["recoveries"]
        if not all(
            isinstance(rows, list)
            for rows in (checkpoints, violations, overrides, recoveries)
        ):
            raise IntegrityGateError("authority audit history collections are malformed")

        repo = self._open_repo(run_dir)
        try:
            for row in checkpoints:
                record, files = self._checkpoint_parts(row)
                repo.append_artifact_checkpoint(record, files)
            for row in violations:
                record, differences = self._violation_parts(row)
                repo.append_artifact_integrity_violation(record, differences)
            for record in overrides:
                if not isinstance(record, dict):
                    raise IntegrityGateError("authority override history is malformed")
                repo.append_artifact_integrity_override(record)
            for row in recoveries:
                record, entries = self._recovery_parts(row)
                repo.append_artifact_crash_recovery(record, entries)

            mirrored_checkpoints = repo.list_artifact_checkpoints()
            summary = repo.artifact_integrity_summary()
        except IntegrityGateError:
            raise
        except Exception as exc:
            raise IntegrityGateError(
                "run-local integrity mirror differs from the signed authority history"
            ) from exc
        finally:
            repo.close()

        if (
            mirrored_checkpoints != checkpoints
            or summary["violations"] != violations
            or summary["overrides"] != overrides
            or summary["crash_recoveries"] != recoveries
            or summary["state"] != expected_state
        ):
            raise IntegrityGateError(
                "run-local integrity mirror differs from the signed authority history"
            )

    def _mirror_checkpoint(self, run_dir: str, result: dict[str, Any]) -> None:
        checkpoint = result.get("checkpoint")
        files = result.get("files")
        if not isinstance(checkpoint, dict) or not isinstance(files, list):
            raise IntegrityGateError("authority checkpoint response is malformed")
        repo = self._open_repo(run_dir)
        try:
            repo.append_artifact_checkpoint(checkpoint, files)
        except Exception as exc:
            raise IntegrityGateError(
                "could not mirror the signed artifact checkpoint"
            ) from exc
        finally:
            repo.close()

    def _mirror_override(self, run_dir: str, result: dict[str, Any]) -> None:
        required = {"violation", "differences", "checkpoint", "files", "override"}
        if not required.issubset(result):
            raise IntegrityGateError("authority override response is malformed")
        repo = self._open_repo(run_dir)
        try:
            repo.append_artifact_integrity_override_bundle(
                violation=result["violation"],
                differences=result["differences"],
                checkpoint=result["checkpoint"],
                files=result["files"],
                override=result["override"],
            )
        except Exception as exc:
            raise IntegrityGateError(
                "could not mirror the signed artifact integrity override"
            ) from exc
        finally:
            repo.close()

    def _set_debug_settings(self, run_dir: str, *, overridden: bool) -> None:
        repo = self._open_repo(run_dir)
        try:
            labels = set(repo.get_run_setting("debug_labels") or [])
            labels.add(DEBUG_INTEGRITY_LABEL)
            if overridden:
                labels.add(DEBUG_OVERRIDE_LABEL)
            desired = sorted(labels)
            if repo.get_run_setting("debug_mode") is True and (
                repo.get_run_setting("debug_labels") or []
            ) == desired:
                return
        finally:
            repo.close()

        lease_id = self.begin_transition(run_dir, checkpoint_kind="phase_boundary")
        repo = self._open_repo(run_dir)
        try:
            repo.update_run_settings({"debug_mode": True, "debug_labels": desired})
        finally:
            repo.close()
        self.commit_transition(run_dir, lease_id)

    def enroll(self, run_dir: str) -> dict[str, Any]:
        try:
            result = self.client.enroll(run_dir)
        except AuthorityError as exc:
            raise IntegrityGateError(f"integrity authority enrollment failed: {exc}") from exc
        self._mirror_checkpoint(run_dir, result)
        return result

    def activate_worker(self, run_dir: str) -> dict[str, Any]:
        try:
            result = self.client.activate_worker(run_dir)
        except AuthorityError as exc:
            raise IntegrityGateError(
                f"Callimachus worker activation failed: {exc}"
            ) from exc
        expected_run = os.path.realpath(os.path.abspath(run_dir))
        if result.get("scope") != "worker" or result.get("run_dir") != expected_run:
            raise IntegrityGateError(
                "integrity authority returned a malformed worker activation"
            )
        return result

    def admit_task_answer(
        self,
        run_dir: str,
        task_id: str,
        raw_payload: dict[str, Any],
        *,
        mirror_checkpoint: bool = True,
    ) -> dict[str, Any]:
        try:
            result = self.client.admit_task_answer(
                run_dir, task_id, raw_payload
            )
        except AuthorityError as exc:
            raise IntegrityGateError(f"task answer admission failed: {exc}") from exc
        if not isinstance(result.get("answer_id"), str) or not isinstance(
            result.get("provenance"), dict
        ):
            raise IntegrityGateError("task answer authority response is malformed")
        if mirror_checkpoint:
            self._mirror_checkpoint(run_dir, result)
        return result

    def preflight(
        self,
        run_dir: str,
        *,
        debug_override: bool = False,
        override_reason: str | None = None,
        mirror_audit_records: bool = True,
    ) -> dict[str, Any]:
        reason = _clean_reason(override_reason, required=debug_override)
        if reason is not None and not debug_override:
            raise IntegrityGateError(
                "artifact integrity override reason requires the debug override flag"
            )
        if not mirror_audit_records and debug_override:
            raise IntegrityGateError(
                "artifact integrity debug override requires a local audit mirror"
            )
        try:
            checked = self.client.check(run_dir)
        except AuthorityError as exc:
            raise IntegrityGateError(f"integrity authority preflight failed: {exc}") from exc
        status = checked.get("status")
        if status not in {"clean", "violated", "debug_overridden"}:
            raise IntegrityGateError("integrity authority returned an invalid state")
        if mirror_audit_records:
            self._mirror_audit_records(
                run_dir,
                checked.get("audit_records"),
                expected_state=status,
            )

        overridden = False
        if status == "violated":
            if not debug_override:
                raise ArtifactIntegrityViolation(
                    "artifact integrity mismatch; run stopped before domain mutation"
                )
            try:
                checked = self.client.override(run_dir, reason or "")
            except AuthorityError as exc:
                raise IntegrityGateError(
                    f"integrity authority debug override failed: {exc}"
                ) from exc
            self._mirror_override(run_dir, checked)
            status = "debug_overridden"
            overridden = True

        if debug_override:
            self._set_debug_settings(run_dir, overridden=overridden)
            try:
                checked = self.client.check(run_dir)
            except AuthorityError as exc:
                raise IntegrityGateError(
                    f"integrity authority post-debug check failed: {exc}"
                ) from exc
            status = checked.get("status")
            if status not in {"clean", "debug_overridden"}:
                raise IntegrityGateError("debug settings did not reach a trusted checkpoint")
            self._mirror_audit_records(
                run_dir,
                checked.get("audit_records"),
                expected_state=status,
            )
            checked = {**checked, "audit_ready": False, "debug_mode": True}
        return checked

    def preflight_content_store(
        self,
        run_dir: str,
        *,
        debug_override: bool = False,
        override_reason: str | None = None,
    ) -> dict[str, Any]:
        reason = _clean_reason(override_reason, required=debug_override)
        if reason is not None and not debug_override:
            raise IntegrityGateError(
                "content store override reason requires the debug override flag"
            )
        try:
            checked = self.client.check_content_store()
        except AuthorityError as exc:
            raise IntegrityGateError(
                f"content store authority preflight failed: {exc}"
            ) from exc
        status = checked.get("status")
        if status not in {"clean", "violated", "debug_overridden"}:
            raise IntegrityGateError("content store authority returned an invalid state")
        overridden = status == "debug_overridden"
        if status == "violated":
            if not debug_override:
                raise ContentStoreIntegrityViolation(
                    "content store integrity mismatch; run stopped before domain mutation"
                )
            try:
                checked = self.client.override_content_store(run_dir, reason or "")
            except AuthorityError as exc:
                raise IntegrityGateError(
                    f"content store debug override failed: {exc}"
                ) from exc
            status = "debug_overridden"
            overridden = True
        if overridden or debug_override:
            self._set_debug_settings(run_dir, overridden=overridden)
            checked = {**checked, "audit_ready": False, "debug_mode": True}
        return checked

    def begin_pipeline_transition(
        self,
        run_dir: str,
        *,
        checkpoint_kind: str,
        mutates_content_store: bool = True,
    ) -> PipelineIntegrityLease:
        transition_id = f"transition-{uuid.uuid4().hex}"
        content_lease_id = None
        if mutates_content_store:
            try:
                content = self.client.begin_content_store_transition(
                    run_dir, transition_id
                )
            except AuthorityError as exc:
                raise IntegrityGateError(
                    f"content store integrity transition could not begin: {exc}"
                ) from exc
            content_lease = content.get("lease")
            content_lease_id = (
                content_lease.get("lease_id")
                if isinstance(content_lease, dict)
                else None
            )
            if not isinstance(content_lease_id, str) or not content_lease_id:
                raise IntegrityGateError(
                    "content store authority returned a malformed lease"
                )
        try:
            result = self.client.begin_transition(
                run_dir,
                checkpoint_kind,
                transition_id=transition_id,
                content_store_required=mutates_content_store,
            )
        except BaseException as exc:
            if content_lease_id is not None:
                try:
                    self.abort_content_store_transition(
                        run_dir,
                        content_lease_id,
                        reason="run integrity transition could not begin",
                    )
                except IntegrityGateError as abort_exc:
                    raise IntegrityGateError(
                        f"content store transition could not be released: {abort_exc}"
                    ) from exc
            raise
        lease = result.get("lease")
        run_lease_id = lease.get("lease_id") if isinstance(lease, dict) else None
        if not isinstance(run_lease_id, str) or not run_lease_id:
            raise IntegrityGateError("integrity authority returned a malformed lease")
        return PipelineIntegrityLease(
            transition_id, run_lease_id, content_lease_id
        )

    def commit_pipeline_transition(
        self, run_dir: str, lease: PipelineIntegrityLease
    ) -> None:
        # Commit the authoritative run first.  If the caller dies before the
        # shared-store commit, recovery keeps the completed run unit and rolls
        # the still-open cache lease back.  The reverse order could leave a
        # committed store checkpoint paired with an orphaned run lease.
        self.commit_transition(run_dir, lease.run_lease_id)
        if lease.content_store_lease_id is not None:
            try:
                self.client.commit_content_store_transition(
                    run_dir, lease.content_store_lease_id
                )
            except AuthorityError as exc:
                raise IntegrityGateError(
                    f"content store integrity transition could not commit: {exc}"
                ) from exc

    def abort_pipeline_transition(
        self,
        run_dir: str,
        lease: PipelineIntegrityLease,
        *,
        reason: str,
    ) -> None:
        errors: list[str] = []
        try:
            self.abort_transition(run_dir, lease.run_lease_id, reason=reason)
        except IntegrityGateError as exc:
            errors.append(str(exc))
        if lease.content_store_lease_id is not None:
            try:
                self.abort_content_store_transition(
                    run_dir, lease.content_store_lease_id, reason=reason
                )
            except IntegrityGateError as exc:
                errors.append(str(exc))
        if errors:
            raise IntegrityGateError("; ".join(errors))

    def begin_transition(self, run_dir: str, *, checkpoint_kind: str) -> str:
        try:
            result = self.client.begin_transition(
                run_dir,
                checkpoint_kind,
                transition_id=f"transition-{uuid.uuid4().hex}",
                content_store_required=False,
            )
        except AuthorityError as exc:
            raise IntegrityGateError(f"integrity transition could not begin: {exc}") from exc
        lease = result.get("lease")
        lease_id = lease.get("lease_id") if isinstance(lease, dict) else None
        if not isinstance(lease_id, str) or not lease_id:
            raise IntegrityGateError("integrity authority returned a malformed lease")
        return lease_id

    def heartbeat_pipeline_transition(
        self, run_dir: str, lease: PipelineIntegrityLease
    ) -> None:
        try:
            self.client.heartbeat_transition(run_dir, lease.run_lease_id)
            if lease.content_store_lease_id is not None:
                self.client.heartbeat_content_store_transition(
                    run_dir, lease.content_store_lease_id
                )
        except AuthorityError as exc:
            raise IntegrityGateError(
                f"integrity transition heartbeat failed: {exc}"
            ) from exc

    def recover_pipeline_transition(self, run_dir: str) -> dict[str, Any]:
        try:
            result = self.client.recover_pipeline_transition(run_dir)
        except AuthorityError as exc:
            raise IntegrityGateError(
                f"interrupted integrity transition could not be recovered: {exc}"
            ) from exc
        if result.get("status") not in {"none", "recovered"} or not isinstance(
            result.get("recoveries"), list
        ):
            raise IntegrityGateError(
                "integrity authority returned a malformed recovery result"
            )
        run_recoveries = []
        for event in result["recoveries"]:
            if not isinstance(event, dict):
                raise IntegrityGateError(
                    "integrity authority returned a malformed recovery event"
                )
            record = event.get("record")
            entries = event.get("children")
            if not isinstance(record, dict) or not isinstance(entries, list):
                raise IntegrityGateError(
                    "integrity authority returned a malformed recovery event"
                )
            if record.get("subject_scope") == "run":
                run_recoveries.append((record, entries))
        if run_recoveries:
            repo = self._open_repo(run_dir)
            try:
                for record, entries in run_recoveries:
                    repo.append_artifact_crash_recovery(record, entries)
            except Exception as exc:
                raise IntegrityGateError(
                    "run-local crash recovery mirror rejected signed authority event"
                ) from exc
            finally:
                repo.close()
        return result

    def commit_transition(self, run_dir: str, lease_id: str) -> dict[str, Any]:
        try:
            result = self.client.commit_transition(run_dir, lease_id)
        except AuthorityError as exc:
            raise IntegrityGateError(f"integrity transition could not commit: {exc}") from exc
        self._mirror_checkpoint(run_dir, result)
        return result

    def abort_transition(self, run_dir: str, lease_id: str, *, reason: str) -> None:
        try:
            result = self.client.abort_transition(run_dir, lease_id, reason)
        except AuthorityError as exc:
            raise IntegrityGateError(f"integrity transition could not abort: {exc}") from exc
        if result.get("status") != "aborted" or result.get("lease_id") != lease_id:
            raise IntegrityGateError("integrity authority returned a malformed abort result")

    def abort_content_store_transition(
        self, run_dir: str, lease_id: str, *, reason: str
    ) -> None:
        try:
            result = self.client.abort_content_store_transition(
                run_dir, lease_id, reason
            )
        except AuthorityError as exc:
            raise IntegrityGateError(
                f"content store integrity transition could not abort: {exc}"
            ) from exc
        if result.get("status") != "aborted" or result.get("lease_id") != lease_id:
            raise IntegrityGateError(
                "content store authority returned a malformed abort result"
            )
