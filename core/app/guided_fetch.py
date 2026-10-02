# core/app/guided_fetch.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed application payloads for user-guided Fetch recovery.

The GUI passes these payloads to the existing integrity admission boundary; it
never writes task or source tables itself.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Callable

from core.app.runtime.settings import DEFAULT_OCR_LANG
from core.fetch.admission import provided_fulltext
from core.fetch.extraction import ocr as fetch_ocr
from core.fetch.extraction import pdf as fetch_pdf
from core.infra.db.repository import RunRepository
from core.resolve.resolver_coverage import (
    bibliographic_concern,
    bibliographic_review_labels,
)
from core.report.source_availability import usable_source_refs


_MAX_SOURCE_PAGE_SIZE = 100
_MAX_SOURCE_PAGE_OFFSET = (1 << 63) - 1


def source_payload(
    *,
    file_path: str | None = None,
    text: str | None = None,
    tier: str = "fulltext",
    source_ref: str | None = None,
) -> dict:
    """Build one guided source answer for the persisted Fetch contract."""
    if tier not in {"fulltext", "abstract"}:
        raise ValueError("guided Fetch tier must be fulltext or abstract")
    if (file_path is None) == (text is None):
        raise ValueError("guided Fetch source requires exactly one file_path or text")
    if tier == "abstract" and file_path is None:
        raise ValueError("guided Fetch abstracts require a captured file")
    value = file_path if file_path is not None else text
    if not isinstance(value, str) or not value.strip():
        raise ValueError("guided Fetch source value must be non-empty")
    payload = {
        "found": True,
        "guided_fetch": True,
        "identity_attested": True,
        "source_tier": tier,
        "file_path" if file_path is not None else "text": value,
    }
    if source_ref is not None:
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise ValueError("guided Fetch source_ref must be non-empty")
        payload["url"] = source_ref
    return payload


def waived_payload() -> dict:
    """Build the explicit user disposition used by guided Fetch proceed."""
    return {"found": False, "disposition": "user_waived", "guided_fetch": True}


def browser_waived_payload(ref_ids: list[str]) -> dict:
    """Build complete ordered browser-group waiver coverage."""
    if not ref_ids or any(not isinstance(ref_id, str) or not ref_id.strip() for ref_id in ref_ids):
        raise ValueError("guided Fetch browser group requires ordered reference ids")
    if len(set(ref_ids)) != len(ref_ids):
        raise ValueError("guided Fetch browser group reference ids must be unique")
    return {"items": [{"ref_id": ref_id, **waived_payload()} for ref_id in ref_ids]}


class GuidedFetchController:
    """Stage human-selected source artifacts, then atomically preflight proceed.

    Staging is deliberately process-local.  The only persistent mutation is an
    individual task admission after every pending Fetch task has been checked
    for a closed, complete answer.  A later admission failure leaves prior
    admissions auditable and the remaining staged inputs resumable.
    """

    def __init__(
        self,
        run_dir: str,
        *,
        agent_identity: str | None = None,
        debug_override_artifact_integrity: bool = False,
        debug_override_reason: str | None = None,
        repository_opener: Callable[[str], Any] = RunRepository.open_readonly,
        admit: Callable[..., dict] | None = None,
        admit_identity_review: Callable[..., dict] | None = None,
    ) -> None:
        self.run_dir = run_dir
        self._agent_identity = agent_identity
        self._debug_override_artifact_integrity = debug_override_artifact_integrity
        self._debug_override_reason = debug_override_reason
        self._repository_opener = repository_opener
        self._admit = admit or _admit_fetch_payload
        self._admit_identity_review = admit_identity_review or _admit_identity_review
        self._staged: dict[str, dict] = {}
        self._restored_ocr_artifacts = _load_guided_ocr_artifacts(run_dir)
        self._proceeded = False
        self._summary: dict[str, Any] = {
            "proceeded": False,
            "submitted_task_ids": [],
            "waived_ref_ids": [],
        }
        self._restore_completed_ocr()

    @property
    def proceeded(self) -> bool:
        return self._proceeded

    @property
    def summary(self) -> dict[str, Any]:
        return deepcopy(self._summary)

    @property
    def identity_review_allowed(self) -> bool:
        """Whether this process is eligible to submit an operator attestation."""
        return self._agent_identity is None

    def stage_source(self, ref_id: str, payload: dict) -> None:
        """Associate one file-only guided source with exactly one pending task."""
        if isinstance(payload, dict) and "ocr_scan_file_path" in payload:
            raise ValueError("guided OCR evidence can only be staged by the OCR action")
        _validate_staged_source(ref_id, payload)
        tasks = self._pending_tasks_by_ref()
        matches = [
            task for task in tasks.get(ref_id, [])
            if task.get("kind") in {"fetch", "browser_challenge"}
        ]
        if len(matches) != 1:
            raise ValueError(
                "guided Fetch source must match exactly one pending Fetch task"
            )
        self._mark_staged_ocr_discarded(ref_id)
        self._staged[ref_id] = deepcopy(payload)
        self._proceeded = False

    def submit_identity_decision(
        self, ref_id: str, action: str, target_sha256: str, reason: str,
    ) -> dict:
        """Submit one hash-bound identity decision through its admission boundary."""
        if not self.identity_review_allowed:
            raise ValueError("source identity attestation requires the human operator")
        matches = [
            task for task in self._pending_tasks_by_ref().get(ref_id, [])
            if task.get("kind") == "source_identity_attestation"
        ]
        if len(matches) != 1:
            raise ValueError(
                "guided Fetch identity decision must match exactly one pending identity review"
            )
        task = matches[0]
        if task.get("target_sha256") != target_sha256:
            raise ValueError("guided Fetch identity decision target hash does not match")
        try:
            admitted = self._admit_identity_review(
                self.run_dir,
                task["task_id"],
                action=action,
                target_sha256=target_sha256,
                reason=reason,
                agent_identity=self._agent_identity,
                debug_override_artifact_integrity=self._debug_override_artifact_integrity,
                debug_override_reason=self._debug_override_reason,
            )
        except SystemExit as exc:
            raise ValueError(str(exc) or "guided Fetch identity review admission failed") from exc
        self._proceeded = False
        return admitted

    def discard_source(self, ref_id: str) -> bool:
        """Remove one process-local staged answer without changing audit evidence.

        Returns whether an answer was staged for ``ref_id``.  A discarded
        reference is deliberately left pending, so ``proceed`` records its
        normal explicit user waiver if the operator does not provide another
        source first.
        """
        if not isinstance(ref_id, str) or not ref_id.strip():
            raise ValueError("Guided Fetch reference id must be non-empty")
        self._mark_staged_ocr_discarded(ref_id)
        discarded = self._staged.pop(ref_id, None) is not None
        self._proceeded = False
        return discarded

    def run_ocr(
        self,
        jobs: list[dict[str, Any]],
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Run confirmed OCR jobs serially and stage hash-bound text answers.

        Scan and result files live in a signed run-owned directory. Each PDF is
        processed one at a time, and every job produces a structured done or
        failed result even if another job fails.
        """
        if not isinstance(jobs, list):
            return []
        total = len(jobs)
        results: list[dict[str, Any]] = []
        for index, job in enumerate(jobs, start=1):
            ref_id = job.get("ref_id") if isinstance(job, dict) else None
            scan_id = job.get("scan_id") if isinstance(job, dict) else None
            if not isinstance(ref_id, str) or not ref_id:
                results.append({
                    "ref_id": str(ref_id or ""),
                    "scan_id": scan_id,
                    "status": "failed",
                    "reason": "OCR job is missing its reference identity.",
                })
                continue
            effective_scan_id = scan_id if isinstance(scan_id, str) else None
            self._emit_ocr_progress(on_progress, {
                "ref_id": ref_id,
                "scan_id": effective_scan_id,
                "status": "queued",
                "index": index,
                "total": total,
            })
            self._emit_ocr_progress(on_progress, {
                "ref_id": ref_id,
                "scan_id": effective_scan_id,
                "status": "running",
                "index": index,
                "total": total,
            })
            try:
                artifact = self._run_one_ocr(ref_id, scan_id)
                result = {
                    "ref_id": ref_id,
                    "scan_id": artifact["scan_id"],
                    "status": "done",
                    "reason": None,
                }
            except Exception as exc:
                reason = _ocr_failure_reason(exc)
                result = {
                    "ref_id": ref_id,
                    "scan_id": effective_scan_id,
                    "status": "failed",
                    "reason": reason,
                }
            results.append(result)
            self._emit_ocr_progress(on_progress, {
                **result,
                "index": index,
                "total": total,
            })
        return results

    def _emit_ocr_progress(
        self,
        callback: Callable[[dict[str, Any]], None] | None,
        event: dict[str, Any],
    ) -> None:
        if callable(callback):
            try:
                callback(event)
            except Exception:
                pass

    def _run_one_ocr(self, ref_id: str, requested_scan_id: object) -> dict[str, Any]:
        retrieval_matches = [
            task for task in self._pending_tasks_by_ref().get(ref_id, [])
            if task.get("kind") in {"fetch", "browser_challenge"}
            or task.get("task_kind") in {"fetch", "browser_challenge"}
        ]
        if len(retrieval_matches) > 1:
            raise ValueError("OCR job matches multiple pending retrieval tasks")
        can_stage_answer = len(retrieval_matches) == 1

        artifact: dict[str, Any] | None = None
        if requested_scan_id is None:
            staged = self._staged.get(ref_id)
            if staged is None or not isinstance(staged.get("file_path"), str):
                raise ValueError("the confirmed staged PDF is unavailable")
            source_path = Path(staged["file_path"])
            if source_path.suffix.lower() != ".pdf" or not source_path.is_file():
                raise ValueError("the confirmed source is not an available PDF")
            try:
                provided_fulltext.parse_extract.probe_file(str(source_path))
            except Exception as exc:
                raise ValueError("the confirmed source is not a valid PDF") from exc
            digest = _sha256_path(source_path)
            token = f"staged:{source_path.resolve()}"
            artifact_id = provided_fulltext.ocr_scan_id(ref_id, token, digest)
            artifact = self._restored_ocr_artifacts.get(artifact_id)
            source_ref = str(staged.get("url") or source_path.resolve())
            display_name = source_path.name
            if artifact is None:
                from core.app.commands.tasks import store_guided_ocr_artifact
                artifact = store_guided_ocr_artifact(
                    self.run_dir,
                    scan_id=artifact_id,
                    ref_id=ref_id,
                    ref_number=self._reference_number(ref_id),
                    scan_path=str(source_path),
                    scan_sha256=digest,
                    scan_token=token,
                    source_ref=source_ref,
                    display_name=display_name,
                    status="pending",
                    identity_attested=True,
                    debug_override_artifact_integrity=self._debug_override_artifact_integrity,
                    debug_override_reason=self._debug_override_reason,
                )
                self._restored_ocr_artifacts[artifact_id] = artifact
        else:
            if not isinstance(requested_scan_id, str) or not re.fullmatch(
                r"[0-9a-f]{64}", requested_scan_id,
            ):
                raise ValueError("OCR scan identity is invalid")
            artifact = self._restored_ocr_artifacts.get(requested_scan_id)
            if artifact is None:
                queued = [
                    item for item in provided_fulltext.list_pending_ocr_scans(
                        self.run_dir, ref_id=ref_id,
                    ) if item["scan_id"] == requested_scan_id
                ]
                if len(queued) != 1:
                    raise ValueError("queued OCR scan is missing or ambiguous")
                scan = queued[0]
                artifact = {
                    **scan,
                    "scan_id": requested_scan_id,
                    "scan_sha256": scan["sha256"],
                    "scan_token": scan["source_ref"],
                    "scan_path": scan["path"],
                    "text_file_path": None,
                    "status": "pending",
                    "reason": scan["reason"],
                    "identity_attested": False,
                }

        if artifact.get("ref_id") != ref_id:
            raise ValueError("OCR scan belongs to a different reference")
        if artifact.get("status") == "done":
            if artifact.get("discarded"):
                artifact = self._persist_discarded_state(artifact, discarded=False)
            if can_stage_answer:
                payload = _ocr_artifact_payload(artifact)
                _validate_staged_source(ref_id, payload, allow_ocr_scan=True)
                self._staged[ref_id] = payload
                self._proceeded = False
            return artifact

        scan_path = artifact.get("scan_path") or artifact.get("path")
        digest = artifact.get("scan_sha256") or artifact.get("sha256")
        token = artifact.get("scan_token") or artifact.get("source_ref")
        source_ref = artifact.get("source_ref") or token
        if not isinstance(scan_path, str) or not isinstance(digest, str):
            raise ValueError("OCR scan metadata is incomplete")
        if _sha256_path(Path(scan_path)) != digest:
            raise ValueError("OCR PDF bytes changed before OCR")
        if artifact.get("status") != "pending" or artifact["scan_id"] not in self._restored_ocr_artifacts:
            from core.app.commands.tasks import store_guided_ocr_artifact
            artifact = store_guided_ocr_artifact(
                self.run_dir,
                scan_id=artifact["scan_id"],
                ref_id=ref_id,
                ref_number=self._reference_number(ref_id),
                scan_path=scan_path,
                scan_sha256=digest,
                scan_token=token,
                source_ref=str(source_ref),
                display_name=artifact.get("display_name") or Path(scan_path).name,
                status="pending",
                identity_attested=artifact["identity_attested"],
                debug_override_artifact_integrity=self._debug_override_artifact_integrity,
                debug_override_reason=self._debug_override_reason,
            )
            self._restored_ocr_artifacts[artifact["scan_id"]] = artifact
            can_stage_answer = True
        try:
            text, method = fetch_ocr.ocr_pdf(scan_path, lang=DEFAULT_OCR_LANG)
            if not fetch_pdf._quality(text):
                raise ValueError("OCR output failed the deterministic text quality gate")
            from core.app.commands.tasks import store_guided_ocr_artifact
            completed = store_guided_ocr_artifact(
                self.run_dir,
                scan_id=artifact["scan_id"],
                ref_id=ref_id,
                ref_number=self._reference_number(ref_id),
                scan_path=scan_path,
                scan_sha256=digest,
                scan_token=token,
                source_ref=str(source_ref),
                display_name=artifact.get("display_name") or Path(scan_path).name,
                status="done",
                text=text,
                method=method,
                identity_attested=artifact["identity_attested"],
                debug_override_artifact_integrity=self._debug_override_artifact_integrity,
                debug_override_reason=self._debug_override_reason,
            )
        except Exception as exc:
            reason = _ocr_failure_reason(exc)
            try:
                from core.app.commands.tasks import store_guided_ocr_artifact
                failed = store_guided_ocr_artifact(
                    self.run_dir,
                    scan_id=artifact["scan_id"],
                    ref_id=ref_id,
                    ref_number=self._reference_number(ref_id),
                    scan_path=scan_path,
                    scan_sha256=digest,
                    scan_token=token,
                    source_ref=str(source_ref),
                    display_name=artifact.get("display_name") or Path(scan_path).name,
                    status="failed",
                    reason=reason,
                    identity_attested=artifact["identity_attested"],
                    debug_override_artifact_integrity=self._debug_override_artifact_integrity,
                    debug_override_reason=self._debug_override_reason,
                )
                self._restored_ocr_artifacts[failed["scan_id"]] = failed
            except Exception:
                pass
            raise
        if can_stage_answer:
            payload = _ocr_artifact_payload(completed)
            _validate_staged_source(ref_id, payload, allow_ocr_scan=True)
            self._staged[ref_id] = payload
            self._proceeded = False
        self._restored_ocr_artifacts[completed["scan_id"]] = completed
        return completed

    def _reference_number(self, ref_id: str) -> int | None:
        repo = self._repository_opener(self.run_dir)
        try:
            reference = repo.get_reference(ref_id)
            return None if reference is None else reference.ref_number
        finally:
            repo.close()

    def _mark_staged_ocr_discarded(self, ref_id: str) -> None:
        staged = self._staged.get(ref_id)
        scan_path = staged.get("ocr_scan_file_path") if isinstance(staged, dict) else None
        if not isinstance(scan_path, str):
            return
        try:
            target = Path(scan_path).resolve(strict=True)
        except OSError:
            return
        for artifact in self._restored_ocr_artifacts.values():
            if (
                artifact.get("ref_id") == ref_id
                and artifact.get("status") == "done"
                and Path(artifact["scan_path"]).resolve() == target
                and not artifact.get("discarded")
            ):
                self._persist_discarded_state(artifact, discarded=True)

    def _persist_discarded_state(
        self, artifact: dict[str, Any], *, discarded: bool,
    ) -> dict[str, Any]:
        from core.app.commands.tasks import store_guided_ocr_artifact
        text_path = artifact.get("text_file_path")
        if not isinstance(text_path, str):
            raise ValueError("completed OCR text is unavailable")
        updated = store_guided_ocr_artifact(
            self.run_dir,
            scan_id=artifact["scan_id"],
            ref_id=artifact["ref_id"],
            ref_number=artifact.get("ref_number"),
            scan_path=artifact["scan_path"],
            scan_sha256=artifact["scan_sha256"],
            scan_token=artifact["scan_token"],
            source_ref=artifact["source_ref"],
            display_name=artifact["display_name"],
            status="done",
            text=Path(text_path).read_text(encoding="utf-8"),
            method=artifact["ocr_method"],
            discarded=discarded,
            identity_attested=artifact["identity_attested"],
            debug_override_artifact_integrity=self._debug_override_artifact_integrity,
            debug_override_reason=self._debug_override_reason,
        )
        self._restored_ocr_artifacts[updated["scan_id"]] = updated
        return updated

    def _restore_completed_ocr(self) -> None:
        if not self._restored_ocr_artifacts:
            return
        try:
            pending = self._pending_tasks_by_ref()
        except Exception:
            return
        for artifact in self._restored_ocr_artifacts.values():
            if artifact.get("status") != "done" or artifact.get("discarded"):
                continue
            ref_id = artifact.get("ref_id")
            if not isinstance(ref_id, str):
                continue
            eligible = [
                task for task in pending.get(ref_id, [])
                if task.get("kind") in {"fetch", "browser_challenge"}
                or task.get("task_kind") in {"fetch", "browser_challenge"}
            ]
            if len(eligible) != 1:
                continue
            try:
                payload = _ocr_artifact_payload(artifact)
                _validate_staged_source(ref_id, payload, allow_ocr_scan=True)
            except (OSError, ValueError, KeyError):
                continue
            self._staged[ref_id] = payload

    def proceed(self) -> dict[str, Any]:
        """Admit staged sources and explicit waivers for every pending Fetch task."""
        self._prepare_ocr_only_queue()
        pending = self._pending_task_views()
        planned, staged_refs, waived_refs = self._preflight_proceed(pending)
        submitted: list[str] = []
        try:
            for task_id, payload, task_staged_refs in planned:
                self._admit(
                    self.run_dir,
                    task_id,
                    payload,
                    agent_identity=self._agent_identity,
                    debug_override_artifact_integrity=self._debug_override_artifact_integrity,
                    debug_override_reason=self._debug_override_reason,
                )
                submitted.append(task_id)
                for ref_id in task_staged_refs:
                    self._staged.pop(ref_id, None)
        except Exception:
            self._summary = {
                "proceeded": False,
                "submitted_task_ids": submitted,
                "waived_ref_ids": waived_refs,
                "staged_ref_ids": staged_refs,
            }
            raise
        self._proceeded = True
        self._summary = {
            "proceeded": True,
            "submitted_task_ids": submitted,
            "waived_ref_ids": waived_refs,
            "staged_ref_ids": staged_refs,
        }
        return self.summary

    def _prepare_ocr_only_queue(self) -> None:
        """Make a queued scan answerable before an explicit Proceed/skip."""
        queued_scans = provided_fulltext.list_pending_ocr_scans(self.run_dir)
        if not queued_scans:
            return
        actionable = {
            item["scan_id"] for item in build_source_inventory(self.run_dir)["ocr_queue"]
            if item["status"] in {"pending", "failed"}
        }
        for scan in queued_scans:
            if scan["scan_id"] not in actionable:
                continue
            if any(
                task.get("kind") in {"fetch", "browser_challenge"}
                for task in self._pending_tasks_by_ref().get(scan["ref_id"], [])
            ):
                continue
            from core.app.commands.tasks import store_guided_ocr_artifact
            artifact = store_guided_ocr_artifact(
                self.run_dir,
                scan_id=scan["scan_id"], ref_id=scan["ref_id"],
                ref_number=self._reference_number(scan["ref_id"]),
                scan_path=scan["path"], scan_sha256=scan["sha256"],
                scan_token=scan["source_ref"], source_ref=scan["source_ref"],
                display_name=scan["display_name"], status="pending",
                identity_attested=False,
                debug_override_artifact_integrity=self._debug_override_artifact_integrity,
                debug_override_reason=self._debug_override_reason,
            )
            self._restored_ocr_artifacts[scan["scan_id"]] = artifact

    def _ocr_waiver(self, ref_id: str) -> dict[str, Any]:
        scans = sorted(
            (
                artifact for artifact in self._restored_ocr_artifacts.values()
                if artifact.get("ref_id") == ref_id and not artifact.get("discarded")
            ),
            key=lambda artifact: artifact["scan_id"],
        )
        if not scans:
            raise ValueError("guided OCR waiver is missing its original PDF")
        return {**waived_payload(), "ocr_scan_file_path": scans[0]["scan_path"]}

    def _pending_tasks_by_ref(self) -> dict[str, list[dict]]:
        by_ref: dict[str, list[dict]] = {}
        for task in self._pending_task_views():
            for ref_id in _task_ref_ids(task):
                by_ref.setdefault(ref_id, []).append(task)
        return by_ref

    def _pending_task_views(self) -> list[dict]:
        repo = self._repository_opener(self.run_dir)
        try:
            views: list[dict] = []
            for task in repo.list_pending_tasks(slot="fetch"):
                view = repo.task_view(task.task_id)
                if not isinstance(view, dict):
                    raise ValueError(f"pending Fetch task {task.task_id} has no task view")
                views.append(view)
            return sorted(views, key=lambda task: task.get("task_id", ""))
        finally:
            repo.close()

    def _preflight_proceed(
        self, pending: list[dict]
    ) -> tuple[list[tuple[str, dict, list[str]]], list[str], list[str]]:
        task_refs: dict[str, list[str]] = {}
        planned: list[tuple[str, dict, list[str]]] = []
        staged_refs: list[str] = []
        waived_refs: list[str] = []
        for task in pending:
            task_id = task.get("task_id")
            kind = task.get("kind")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("pending Fetch task is missing task_id")
            if kind == "source_identity_attestation":
                raise ValueError(
                    "guided Fetch cannot proceed while a source identity review is pending"
                )
            if kind not in {"fetch", "browser_challenge"}:
                raise ValueError("guided Fetch proceed cannot waive non-retrieval tasks")
            ref_ids = _task_ref_ids(task)
            if not ref_ids or len(set(ref_ids)) != len(ref_ids):
                raise ValueError("pending Fetch task has invalid reference coverage")
            task_refs[task_id] = ref_ids

        ref_matches: dict[str, list[str]] = {}
        for task_id, ref_ids in task_refs.items():
            for ref_id in ref_ids:
                ref_matches.setdefault(ref_id, []).append(task_id)
        for ref_id in self._staged:
            if len(ref_matches.get(ref_id, [])) != 1:
                raise ValueError(
                    "staged guided source does not match exactly one pending Fetch task"
                )

        for task in pending:
            task_id = task["task_id"]
            ref_ids = task_refs[task_id]
            if task["kind"] == "fetch":
                ref_id = ref_ids[0]
                payload = self._staged.get(ref_id)
                task_staged_refs = []
                if payload is None:
                    payload = (
                        self._ocr_waiver(ref_id)
                        if task_id.startswith("fetch:guided-ocr:") else waived_payload()
                    )
                    waived_refs.append(ref_id)
                else:
                    payload, was_waived = _proceed_payload(ref_id, payload)
                    if payload.get("found") is True:
                        _validate_staged_source(
                            ref_id, payload,
                            allow_ocr_scan="ocr_scan_file_path" in payload,
                        )
                    if was_waived:
                        waived_refs.append(ref_id)
                    else:
                        staged_refs.append(ref_id)
                    task_staged_refs.append(ref_id)
                planned.append((
                    task_id,
                    deepcopy(payload),
                    task_staged_refs,
                ))
                continue
            items = []
            task_staged_refs: list[str] = []
            for ref_id in ref_ids:
                payload = self._staged.get(ref_id)
                if payload is None:
                    items.append({"ref_id": ref_id, **waived_payload()})
                    waived_refs.append(ref_id)
                else:
                    payload, was_waived = _proceed_payload(ref_id, payload)
                    if payload.get("found") is True:
                        _validate_staged_source(
                            ref_id, payload,
                            allow_ocr_scan="ocr_scan_file_path" in payload,
                        )
                    items.append({"ref_id": ref_id, **deepcopy(payload)})
                    if was_waived:
                        waived_refs.append(ref_id)
                    else:
                        staged_refs.append(ref_id)
                    task_staged_refs.append(ref_id)
            planned.append((task_id, {"items": items}, task_staged_refs))
        return planned, staged_refs, waived_refs


def _task_ref_ids(task: dict) -> list[str]:
    if task.get("kind") in {"fetch", "source_identity_attestation"}:
        ref_id = task.get("ref_id")
        return [ref_id] if isinstance(ref_id, str) and ref_id else []
    if task.get("kind") == "browser_challenge":
        references = task.get("references")
        if not isinstance(references, list):
            return []
        return [
            item["ref_id"] for item in references
            if isinstance(item, dict) and isinstance(item.get("ref_id"), str)
            and item["ref_id"]
        ]
    return []


def _validate_staged_source(
    ref_id: str, payload: dict, *, allow_ocr_scan: bool = False,
) -> None:
    if not isinstance(ref_id, str) or not ref_id:
        raise ValueError("guided Fetch ref_id must be non-empty")
    if not isinstance(payload, dict):
        raise ValueError("guided Fetch source payload must be an object")
    allowed = {
        "found", "guided_fetch", "identity_attested", "source_tier", "file_path", "url",
        "ocr_scan_file_path",
    }
    if set(payload) - allowed or payload.get("found") is not True:
        raise ValueError("guided Fetch staged source has an invalid shape")
    if (
        payload.get("guided_fetch") is not True
        or (
            payload.get("identity_attested") is not True
            and not (
                allow_ocr_scan
                and "ocr_scan_file_path" in payload
                and payload.get("identity_attested") is False
            )
        )
        or "text" in payload
    ):
        raise ValueError("guided Fetch staged source must be file-only")
    if not isinstance(payload.get("file_path"), str) or not payload["file_path"].strip():
        raise ValueError("guided Fetch staged source requires file_path")
    if payload.get("source_tier", "fulltext") not in {"fulltext", "abstract"}:
        raise ValueError("guided Fetch staged source tier is invalid")
    if "url" in payload and (
        not isinstance(payload["url"], str) or not payload["url"].strip()
    ):
        raise ValueError("guided Fetch source_ref must be non-empty")
    if "ocr_scan_file_path" in payload:
        if not allow_ocr_scan:
            raise ValueError("guided OCR evidence can only be staged by the OCR action")
        if (
            payload.get("source_tier", "fulltext") != "fulltext"
            or Path(payload["file_path"]).suffix.lower() != ".txt"
            or not isinstance(payload.get("ocr_scan_file_path"), str)
            or Path(payload["ocr_scan_file_path"]).suffix.lower() != ".pdf"
        ):
            raise ValueError("guided OCR source requires text and its original PDF")
        source_ref = payload.get("url")
        if not isinstance(source_ref, str) or not re.fullmatch(
            r"urn:callimachus:ocr-scan:sha256:[0-9a-f]{64}", source_ref,
        ):
            raise ValueError("guided OCR source requires a hash-bound scan reference")
        expected = source_ref.rsplit(":", 1)[-1]
        if _sha256_path(Path(payload["ocr_scan_file_path"])) != expected:
            raise ValueError("guided OCR source PDF hash does not match its reference")


def _proceed_payload(ref_id: str, payload: dict) -> tuple[dict, bool]:
    """Turn an unprocessed scanned PDF into an explicit waiver with its PDF."""
    if (
        payload.get("found") is True
        and "ocr_scan_file_path" not in payload
        and isinstance(payload.get("file_path"), str)
        and Path(payload["file_path"]).suffix.lower() == ".pdf"
        and Path(payload["file_path"]).is_file()
        and provided_fulltext.pdf_requires_ocr(payload["file_path"])
    ):
        return ({
            **waived_payload(),
            "ocr_scan_file_path": payload["file_path"],
        }, True)
    return deepcopy(payload), False


def _admit_fetch_payload(*args, **kwargs) -> dict:
    """Late import keeps the app payload module independent from CLI parsing."""
    from core.app.commands.tasks import admit_fetch_payload
    try:
        return admit_fetch_payload(*args, **kwargs)
    except SystemExit as exc:
        message = str(exc) or "guided Fetch answer admission failed"
        raise ValueError(message) from exc


def _admit_identity_review(*args, **kwargs) -> dict:
    """Late import keeps the GUI controller independent from CLI parsing."""
    from core.app.commands.tasks import admit_source_identity_review
    return admit_source_identity_review(*args, **kwargs)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ocr_artifact_payload(artifact: dict[str, Any]) -> dict:
    text_path = artifact.get("text_file_path")
    scan_path = artifact.get("scan_path")
    digest = artifact.get("scan_sha256")
    if (
        not isinstance(text_path, str)
        or not isinstance(scan_path, str)
        or not isinstance(digest, str)
    ):
        raise ValueError("completed OCR artifact is incomplete")
    if _sha256_path(Path(scan_path)) != digest:
        raise ValueError("completed OCR artifact scan hash changed")
    return {
        "found": True,
        "guided_fetch": True,
        "identity_attested": artifact["identity_attested"],
        "source_tier": "fulltext",
        "file_path": text_path,
        "url": provided_fulltext.ocr_scan_source_ref(digest),
        "ocr_scan_file_path": scan_path,
    }


def _load_guided_ocr_artifacts(run_dir: str) -> dict[str, dict[str, Any]]:
    """Load only hash-validated, run-contained Guided Fetch OCR sidecars."""
    root = Path(run_dir).resolve()
    sources_dir = root / "sources"
    artifact_dir = sources_dir / "guided_ocr"
    if (
        not artifact_dir.exists()
        or sources_dir.is_symlink()
        or artifact_dir.is_symlink()
        or not artifact_dir.is_dir()
    ):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for metadata_path in artifact_dir.glob("*.json"):
        if metadata_path.is_symlink():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                continue
            scan_id = metadata.get("scan_id")
            ref_id = metadata.get("ref_id")
            scan_token = metadata.get("scan_token")
            scan_sha256 = metadata.get("scan_sha256")
            status = metadata.get("status")
            if (
                not isinstance(scan_id, str)
                or not re.fullmatch(r"[0-9a-f]{64}", scan_id)
                or not isinstance(ref_id, str)
                or not ref_id
                or not isinstance(scan_token, str)
                or not isinstance(scan_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", scan_sha256)
                or provided_fulltext.ocr_scan_id(ref_id, scan_token, scan_sha256) != scan_id
                or status not in {"pending", "failed", "done"}
                or metadata_path.stem != scan_id
            ):
                continue
            scan_path = artifact_dir / f"{scan_id}.pdf"
            if scan_path.is_symlink() or not scan_path.is_file():
                continue
            resolved_scan = scan_path.resolve(strict=True)
            resolved_scan.relative_to(root)
            if _sha256_path(resolved_scan) != scan_sha256:
                continue
            display_name = metadata.get("display_name")
            source_ref = metadata.get("source_ref")
            reason = metadata.get("reason")
            ref_number = metadata.get("ref_number")
            if (
                not isinstance(display_name, str)
                or Path(display_name).name != display_name
                or not isinstance(source_ref, str)
                or (reason is not None and not isinstance(reason, str))
                or (ref_number is not None and type(ref_number) is not int)
                or type(metadata.get("discarded", False)) is not bool
                or type(metadata.get("identity_attested")) is not bool
            ):
                continue
            text_path: Path | None = None
            if status == "done":
                relative = metadata.get("text_path")
                text_sha256 = metadata.get("text_sha256")
                method = metadata.get("ocr_method")
                if (
                    not isinstance(relative, str)
                    or not re.fullmatch(
                        rf"sources/guided_ocr/{re.escape(scan_id)}-[0-9a-f]{{64}}\.txt",
                        relative,
                    )
                    or not isinstance(text_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", text_sha256)
                    or not isinstance(method, str)
                    or not method
                ):
                    continue
                text_path = root / Path(*PurePosixPath(relative).parts)
                if text_path.is_symlink() or not text_path.is_file():
                    continue
                text_path.resolve(strict=True).relative_to(root)
                if _sha256_path(text_path) != text_sha256:
                    continue
                metadata["text_file_path"] = str(text_path.resolve())
            elif (
                metadata.get("text_path") is not None
                or metadata.get("text_sha256") is not None
                or metadata.get("ocr_method") is not None
            ):
                continue
            metadata["scan_path"] = str(resolved_scan)
            metadata["path"] = str(resolved_scan)
            metadata["sha256"] = scan_sha256
            output[scan_id] = metadata
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return output


def _ocr_failure_reason(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "The selected PDF is unavailable. Replace it and retry OCR."
    if isinstance(exc, ValueError) and "quality gate" in str(exc):
        return "OCR did not produce text that passed the quality check. Replace the PDF or provide readable text."
    if isinstance(exc, RuntimeError):
        return "The OCR backend could not process this PDF. Check OCR availability or replace the scan."
    return f"OCR failed ({type(exc).__name__}). Replace the PDF or retry."


def build_source_inventory(
    run_dir: str,
    *,
    focus_ref_id: str | None = None,
    include_fetch_attempts: bool = True,
    offset: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return the read-only, audit-backed source view for guided Fetch.

    The projection deliberately keeps acquisition state separate from the
    deterministic suspected-fabrication tag.  It uses one read-only repository
    handle so a GUI refresh cannot create or mutate run state.  Desktop callers
    may focus the projection on one reference and omit transport attempts when
    they only need the compact main-table rows.
    """
    paged = offset is not None or limit is not None
    if paged:
        _validate_source_page_bounds(offset, limit)
        if focus_ref_id is not None:
            raise ValueError("a source inventory cannot be focused and paged")

    repo = RunRepository.open_readonly(run_dir)
    try:
        page_ref_ids: list[str] | None = None
        if paged:
            references = repo.list_references(offset=offset, limit=limit)
            page_ref_ids = [item.ref_id for item in references]
            source_records = repo.list_source_texts_for_refs(page_ref_ids)
            ref_numbers = {item.ref_id: item.ref_number for item in references}
            manifest_by_source = {
                source.source_text_id: _source_manifest_entry(
                    source, ref_numbers.get(source.ref_id),
                )
                for source in source_records
            }
        elif focus_ref_id is None:
            references = repo.list_references()
            source_records = repo.list_source_texts()
            manifest_by_source = {
                item.get("source_text_id"): item
                for item in repo.source_manifest_payload().get("entries", [])
                if isinstance(item, dict)
            }
        else:
            reference = repo.get_reference(focus_ref_id)
            references = [] if reference is None else [reference]
            page_ref_ids = [focus_ref_id]
            source_records = repo.list_source_texts(focus_ref_id)
            ref_numbers = {
                item.ref_id: item.ref_number for item in references
            }
            manifest_by_source = {
                source.source_text_id: _source_manifest_entry(
                    source, ref_numbers.get(source.ref_id),
                )
                for source in source_records
            }
        pending_by_ref = _task_views_by_ref(
            repo, repo.list_pending_tasks(slot="fetch", ref_ids=page_ref_ids)
        )
        active_by_ref = _task_views_by_ref(repo, [
            *repo.list_pending_tasks(slot="fetch", ref_ids=page_ref_ids),
            *repo.list_answered_tasks_waiting_apply(
                slot="fetch", ref_ids=page_ref_ids,
            ),
        ])
        source_by_ref: dict[str, list[dict[str, Any]]] = {}
        for source in source_records:
            item = asdict(source)
            manifest_entry = manifest_by_source.get(source.source_text_id)
            usable, diagnostics = usable_source_refs(
                run_dir,
                {"entries": [manifest_entry]} if manifest_entry is not None else {},
            )
            item["available"] = source.ref_id in usable
            item["availability_diagnostics"] = list(diagnostics)
            item["preview_path"] = (
                _preview_path(run_dir, source.stored_path)
                if item["available"]
                else None
            )
            source_by_ref.setdefault(source.ref_id, []).append(item)

        guided_ocr_artifacts = _load_guided_ocr_artifacts(run_dir)
        guided_ocr_by_ref: dict[str, list[dict[str, Any]]] = {}
        for artifact in guided_ocr_artifacts.values():
            ref_id = artifact.get("ref_id")
            if isinstance(ref_id, str):
                guided_ocr_by_ref.setdefault(ref_id, []).append(artifact)

        entries = []
        for reference in references:
            resolved = repo.get_resolve_result(reference.ref_id)
            resolve = None if resolved is None else asdict(resolved)
            evidence = (resolve or {}).get("evidence_profile") or {}
            if resolve is not None:
                resolve["journal_authority"] = evidence.get("journal_authority")
                resolve["coordinate_comparisons"] = _coordinate_comparisons(evidence)
            sources = source_by_ref.get(reference.ref_id, [])
            best_source = next(
                (source for source in sources if source["available"]),
                None,
            )
            has_available_fulltext = any(
                source.get("available") and source.get("tier") == "fulltext"
                for source in sources
            )
            staged_ocr_preview = (
                None
                if has_available_fulltext
                else _staged_guided_ocr_preview_path(
                    run_dir, guided_ocr_by_ref.get(reference.ref_id, [])
                )
            )
            best_preview_path = (
                None if best_source is None else best_source["preview_path"]
            )
            preview_path = staged_ocr_preview or best_preview_path
            preview_kind = (
                "guided_ocr_staged"
                if staged_ocr_preview is not None
                else "acquired"
                if best_preview_path is not None
                else None
            )
            resolved_abstract = (resolve or {}).get("abstract")
            best_tier = (
                best_source["tier"]
                if best_source is not None
                else "abstract"
                if isinstance(resolved_abstract, str) and resolved_abstract.strip()
                else None
            )
            parsed = asdict(reference)
            parsed["cited_coordinates"] = list(reference.cited_coordinates)
            profile = evidence if isinstance(evidence, dict) else {}
            entries.append({
                "ref_id": reference.ref_id,
                "ref_number": reference.ref_number,
                "parsed": parsed,
                "resolve": resolve,
                "bibliographic_concern": bibliographic_concern(resolve),
                "bibliographic_review_labels": bibliographic_review_labels(
                    reference=parsed,
                    status=(resolve or {}).get("status"),
                    attempts=(resolve or {}).get("attempts") or [],
                    adjudication=profile.get("bibliographic_adjudication") or {},
                    coverage=profile.get("resolver_coverage") or {},
                ),
                "fetch": {
                    "sources": sources,
                    "best_source": best_source,
                    "tier": best_tier,
                    "preview_path": preview_path,
                    "preview_kind": preview_kind,
                    "attempts": (
                        repo.list_fetch_attempts(reference.ref_id)
                        if include_fetch_attempts
                        else []
                    ),
                    "pending_tasks": pending_by_ref.get(reference.ref_id, []),
                    "tasks": active_by_ref.get(reference.ref_id, []),
                },
                "fabrication_suspicion": {
                    "suspected": bool(
                        resolve
                        and resolve.get("reference_status_tag")
                        == "suspected_fabricated"
                    ),
                    "reference_status_tag": (resolve or {}).get("reference_status_tag"),
                    "fabrication_risk": (resolve or {}).get("fabrication_risk"),
                    "reason": (resolve or {}).get("tag_reason"),
                },
            })
        ocr_queue = _ocr_queue_projection(
            run_dir,
            [reference.ref_id for reference in references],
            source_by_ref,
            guided_ocr_artifacts,
        )
        return {
            "run": asdict(repo.get_run()),
            "references": entries,
            "ocr_queue": ocr_queue,
        }
    finally:
        repo.close()


def _validate_source_page_bounds(
    offset: int | None, limit: int | None,
) -> None:
    if type(offset) is not int or not 0 <= offset <= _MAX_SOURCE_PAGE_OFFSET:
        raise ValueError(
            "source page offset must be a non-negative SQLite integer"
        )
    if (
        type(limit) is not int
        or limit < 1
        or limit > _MAX_SOURCE_PAGE_SIZE
    ):
        raise ValueError(
            f"source page limit must be between 1 and {_MAX_SOURCE_PAGE_SIZE}"
        )


def _source_manifest_entry(source, ref_number: int | None) -> dict[str, Any]:
    """Project one source row into the fields used by availability checks."""
    return {
        "source_text_id": source.source_text_id,
        "ref_id": source.ref_id,
        "ref_number": ref_number,
        "tier": source.tier,
        "origin": source.origin,
        "stored_as": source.stored_path.replace("\\", "/").removeprefix("sources/"),
        "sha256": source.sha256,
        "char_count": source.char_count,
    }


def _coordinate_comparisons(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the resolver's typed metadata comparison facts without inference."""
    metadata = evidence.get("metadata_match")
    if not isinstance(metadata, dict):
        return []
    comparisons = metadata.get("coordinate_comparisons")
    return comparisons if isinstance(comparisons, list) else []


def _task_views_by_ref(
    repo: RunRepository, tasks: list[Any],
) -> dict[str, list[dict[str, Any]]]:
    """Project typed task views only; source bodies and paths stay out of tasks."""
    views = []
    for task in tasks:
        view = repo.task_view(task.task_id)
        if isinstance(view, dict):
            view["task_kind"] = task.task_kind
            views.append(view)
    return _tasks_by_ref(views)


def _task_is_retrieval(task: dict[str, Any]) -> bool:
    return task.get("kind") in {"fetch", "browser_challenge"} or task.get(
        "task_kind"
    ) in {"fetch", "browser_challenge"}


def _ocr_queue_projection(
    run_dir: str,
    ref_ids: list[str],
    source_by_ref: dict[str, list[dict[str, Any]]],
    guided_ocr_artifacts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    allowed_refs = set(ref_ids)
    eligible_refs = {
        ref_id for ref_id in ref_ids
        if not any(
            source.get("available") and source.get("tier") == "fulltext"
            for source in source_by_ref.get(ref_id, [])
        )
    }
    scans: dict[str, dict[str, Any]] = {}
    try:
        for scan in provided_fulltext.list_pending_ocr_scans(run_dir):
            if scan.get("ref_id") not in eligible_refs:
                continue
            scans[scan["scan_id"]] = {
                "scan_id": scan["scan_id"],
                "ref_id": scan["ref_id"],
                "ref_number": scan.get("ref_number"),
                "display_name": scan.get("display_name") or "scan.pdf",
                "status": "pending",
                "reason": "Previously fetched PDF is queued for OCR.",
            }
    except Exception:
        pass
    for artifact in guided_ocr_artifacts.values():
        if artifact.get("ref_id") not in eligible_refs or artifact.get("discarded"):
            continue
        scans[artifact["scan_id"]] = {
            "scan_id": artifact["scan_id"],
            "ref_id": artifact["ref_id"],
            "ref_number": artifact.get("ref_number"),
            "display_name": artifact["display_name"],
            "status": artifact["status"],
            "reason": artifact.get("reason"),
        }
    return sorted(
        (item for item in scans.values() if item["ref_id"] in allowed_refs),
        key=lambda item: (item.get("ref_number") or 0, item["scan_id"]),
    )


def _staged_guided_ocr_preview_path(
    run_dir: str, artifacts: list[dict[str, Any]],
) -> str | None:
    """Expose one completed, undiscarded OCR sidecar as an unadmitted preview."""
    if len(artifacts) != 1:
        return None
    artifact = artifacts[0]
    if artifact.get("status") != "done" or artifact.get("discarded"):
        return None
    text_path = artifact.get("text_path")
    if not isinstance(text_path, str):
        return None
    return _preview_path(run_dir, text_path)


def pending_ocr_count(run_dir: str) -> int:
    """Count actionable pending OCR scans without building the full inventory."""
    repo = RunRepository.open_readonly(run_dir)
    try:
        references = repo.list_references()
        ref_ids = [reference.ref_id for reference in references]
        manifest = repo.source_manifest_payload()
        manifest = {
            "entries": [
                item for item in manifest.get("entries", [])
                if isinstance(item, dict) and item.get("tier") == "fulltext"
            ]
        }
        usable, _diagnostics = usable_source_refs(run_dir, manifest)
    finally:
        repo.close()
    eligible = {ref_id for ref_id in ref_ids if ref_id not in usable}
    artifacts = _load_guided_ocr_artifacts(run_dir)
    artifact_by_id = {item["scan_id"]: item for item in artifacts.values()}
    scan_ids = set()
    for item in provided_fulltext.list_pending_ocr_scans(run_dir):
        if item["ref_id"] not in eligible:
            continue
        artifact = artifact_by_id.get(item["scan_id"])
        if artifact is None or artifact.get("status") != "done" or artifact.get("discarded"):
            scan_ids.add(item["scan_id"])
    scan_ids.update(
        item["scan_id"]
        for item in artifacts.values()
        if item["ref_id"] in eligible
        and item["status"] != "done"
        and not item.get("discarded")
    )
    return len(scan_ids)


def _tasks_by_ref(tasks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_ref: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        ref_ids: list[str] = []
        ref_id = task.get("ref_id")
        if isinstance(ref_id, str) and ref_id:
            ref_ids.append(ref_id)
        if task.get("kind") == "browser_challenge":
            references = task.get("references")
            if isinstance(references, list):
                ref_ids.extend(
                    reference["ref_id"]
                    for reference in references
                    if isinstance(reference, dict)
                    and isinstance(reference.get("ref_id"), str)
                )
        for ref_id in dict.fromkeys(ref_ids):
            by_ref.setdefault(ref_id, []).append(task)
    return by_ref


def _preview_path(run_dir: str, stored_path: str) -> str | None:
    """Expose only a normalized source path that remains inside this run."""
    if not isinstance(stored_path, str) or "\\" in stored_path:
        return None
    if re.match(r"^[A-Za-z]:", stored_path):
        return None
    relative = PurePosixPath(stored_path)
    if (
        relative.is_absolute()
        or len(relative.parts) < 2
        or relative.parts[0] != "sources"
        or any(part in {".", ".."} for part in relative.parts)
        or relative.as_posix() != stored_path
    ):
        return None
    root = Path(run_dir).resolve()
    candidate = (root / relative).resolve()
    try:
        if os.path.commonpath((str(root), str(candidate))) != str(root):
            return None
    except ValueError:
        return None
    return str(candidate)
