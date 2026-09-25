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
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Callable

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
        self._proceeded = False
        self._summary: dict[str, Any] = {
            "proceeded": False,
            "submitted_task_ids": [],
            "waived_ref_ids": [],
        }

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
        discarded = self._staged.pop(ref_id, None) is not None
        self._proceeded = False
        return discarded

    def proceed(self) -> dict[str, Any]:
        """Admit staged sources and explicit waivers for every pending Fetch task."""
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
                    payload = waived_payload()
                    waived_refs.append(ref_id)
                else:
                    _validate_staged_source(ref_id, payload)
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
                    _validate_staged_source(ref_id, payload)
                    items.append({"ref_id": ref_id, **deepcopy(payload)})
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


def _validate_staged_source(ref_id: str, payload: dict) -> None:
    if not isinstance(ref_id, str) or not ref_id:
        raise ValueError("guided Fetch ref_id must be non-empty")
    if not isinstance(payload, dict):
        raise ValueError("guided Fetch source payload must be an object")
    allowed = {
        "found", "guided_fetch", "identity_attested", "source_tier", "file_path", "url",
    }
    if set(payload) - allowed or payload.get("found") is not True:
        raise ValueError("guided Fetch staged source has an invalid shape")
    if (
        payload.get("guided_fetch") is not True
        or payload.get("identity_attested") is not True
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
                    "preview_path": (
                        None if best_source is None else best_source["preview_path"]
                    ),
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
        return {"run": asdict(repo.get_run()), "references": entries}
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
