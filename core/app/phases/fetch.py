#!/usr/bin/env python3
# core/app/phases/fetch.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Phase: fetch — source retrieval pipeline with concurrency control.

LLM/agent web tools with optional pause for missing full text. Handles:
- Deterministic OA fetch before asking the agent
- Content store reuse
- Browser challenge groups
- OCR for scanned PDFs
- Apply provenance-bound task answers on resume
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import heapq
import math
import os
import re
import sys
import threading
import time
import urllib.parse
from pathlib import Path, PurePosixPath, PureWindowsPath

from core.app.runtime.credentials import _print_phase_credential_warnings
from core.app.runtime.fetch_audit import _record_fetch_attempts, record_identity_anomaly
from core.app.runtime.repository import (
    _load_parse_payload,
    _load_resolve_map,
    _now,
    _repo_open,
    _run_ref_by_id,
    _save_state,
)
from core.app.runtime.settings import (
    DEFAULT_CHALLENGE_MODE,
    DEFAULT_OCR_LANG,
    ENV_CHALLENGE_MODE,
    _blocking_fetch_need_items,
    _fetch_worker_count,
    _progress,
)
from core.app.runtime.sources import (
    _fetch_evidence_payload,
    _load_manifest_payload,
    _load_unreadable_payload,
    _materialize_resolve_abstracts,
    _register_run_source_text,
    _resolve_map,
)
from core.app.runtime.tasks import (
    _answered_tasks,
    _create_task,
    _pause,
    _pending_tasks,
    _reopen_task_with_error,
    _update_task,
)

from core.fetch import service as _fetch
from core.fetch.queue import FetchQueuePersistenceHooks
from core.fetch.fallbacks import fetch_modes as _fetch_modes
from core.fetch.storage import content_store
from core.fetch.transport.http import FetchAdmissionDeferred
from core.fetch.transport import host_limiter as _host_limiter
from core.infra import perf as _perf
from core.fetch.admission import provided_fulltext as _provided_fulltext
from core.resolve import sources as _sources
from core.resolve import user_sources as _user_sources
from core.resolve.resolver_coverage import bibliographic_review_labels
from core.parse.footnotes import cross_reference_map
from core.verify.identity_gate import (
    bibliographic_identity_admitted,
    source_identity_attestation_block_reason,
)

ENV_OCR_AUTO = "CITATION_VERIFIER_OCR_AUTO"
ENV_OCR_AUTO_MAX_PAGES = "CITATION_VERIFIER_OCR_AUTO_MAX_PAGES"
AUTO_OCR_MAX_PAGES = 50
_OCR_REGISTRATION_LOCK = threading.Lock()
_FETCH_DEFERRED_WALL_SECONDS = 10 * 60
_FETCH_DEFERRED_RETRY_LIMIT = 2
_FETCH_DEFERRED_MAX_PHYSICAL_429S = 1 + _FETCH_DEFERRED_RETRY_LIMIT
_GUIDED_FETCH_REJECTION_OUTCOMES = frozenset({
    "abstract_section_missing",
    "challenge_or_login_page",
    "identity_mismatch",
    "insufficient_identity",
    "needs_manual_confirmation",
    "unreadable",
})


def _integrity_completed_unit_rows(st: dict, group: str) -> list[dict]:
    """Return crash-resumable unit markers only under an integrity lease."""
    if not callable(st.get("_integrity_unit_checkpoint")):
        return []
    repo = _repo_open(st["run_dir"])
    if repo is None:
        raise RuntimeError("integrity unit progress repository is unavailable")
    try:
        return repo.list_integrity_unit_completions(group)
    finally:
        repo.close()


def _integrity_completed_units(st: dict, group: str) -> set[str]:
    return {
        row["unit_id"] for row in _integrity_completed_unit_rows(st, group)
    }


def _checkpoint_integrity_unit(
    st: dict, group: str, unit_id: str, *, payload: dict | None = None
) -> None:
    """Persist a completed Fetch unit, then rotate the signed checkpoint."""
    callback = st.get("_integrity_unit_checkpoint")
    if not callable(callback):
        return
    if not isinstance(unit_id, str) or not unit_id:
        raise RuntimeError("integrity unit identity is unavailable")
    repo = _repo_open(st["run_dir"])
    if repo is None:
        raise RuntimeError("integrity unit progress repository is unavailable")
    try:
        repo.append_integrity_unit_completion(
            group, unit_id, payload or {}
        )
    finally:
        repo.close()
    callback(group, unit_id)


def _validated_fetch_auto_completion_payloads(rows: list[dict]) -> tuple[dict[str, dict], dict[str, int]]:
    """Return latest automatic-Fetch conclusions and their append counts.

    A reference's first completion is its ref id.  Later completion cycles use
    ``<ref_id>#<ordinal>``.  Checking every row makes a corrupted ledger fail
    closed rather than silently selecting an arbitrary prior conclusion.
    """
    completed: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for row in rows:
        unit_id = row.get("unit_id")
        payload = row.get("payload")
        ref_id = payload.get("ref_id") if isinstance(payload, dict) else None
        if not isinstance(ref_id, str) or not ref_id:
            raise RuntimeError("fetch automatic completion identity is invalid")
        prior_count = counts.get(ref_id, 0)
        expected_unit_id = ref_id if prior_count == 0 else f"{ref_id}#{prior_count + 1}"
        if not isinstance(unit_id, str) or unit_id != expected_unit_id:
            raise RuntimeError("fetch automatic completion identity is invalid")
        counts[ref_id] = prior_count + 1
        completed[ref_id] = payload
    return completed, counts


def _fetch_auto_source_binding(source) -> dict:
    return {
        "source_text_id": source.source_text_id,
        "ref_id": source.ref_id,
        "stored_path": source.stored_path,
        "sha256": source.sha256,
        "char_count": source.char_count,
        "recorded_at": source.recorded_at,
        "origin": source.origin,
    }


def _stored_fetch_auto_payload_is_current(payload: dict, sources_by_ref: dict[str, list]) -> bool:
    """True only for a stored completion bound to its exact current source."""
    binding = payload.get("_fetch_auto_source")
    if binding is None:
        return False
    expected_keys = {
        "source_text_id", "ref_id", "stored_path", "sha256", "char_count",
        "recorded_at", "origin",
    }
    if not isinstance(binding, dict) or set(binding) != expected_keys:
        raise RuntimeError("fetch automatic completion source binding is invalid")
    ref_id = payload.get("ref_id")
    stored_as = payload.get("stored_as")
    if (
        not isinstance(ref_id, str) or not ref_id
        or not isinstance(stored_as, str) or not stored_as
        or binding.get("ref_id") != ref_id
        or binding.get("stored_path") != f"sources/{stored_as}"
    ):
        raise RuntimeError("fetch automatic completion source binding is invalid")
    if (
        not all(isinstance(binding[key], str) and binding[key] for key in expected_keys - {"char_count"})
        or not isinstance(binding["char_count"], int)
        or isinstance(binding["char_count"], bool)
        or binding["char_count"] < 0
    ):
        raise RuntimeError("fetch automatic completion source binding is invalid")
    return any(
        row.tier == "fulltext" and _fetch_auto_source_binding(row) == binding
        for row in sources_by_ref.get(binding["ref_id"], [])
    )


def _bind_stored_fetch_auto_payload(run_dir: str, payload: dict) -> None:
    if payload.get("status") not in {"stored", "already_stored"}:
        return
    ref_id = payload.get("ref_id")
    stored_as = payload.get("stored_as")
    if not isinstance(ref_id, str) or not ref_id or not isinstance(stored_as, str) or not stored_as:
        raise RuntimeError("stored fetch result cannot bind a registered fulltext")
    expected_path = f"sources/{stored_as}"
    repo = _repo_open(run_dir)
    if repo is None:
        raise RuntimeError("fetch automatic completion repository is unavailable")
    try:
        matches = [
            row for row in repo.list_source_texts(ref_id)
            if row.tier == "fulltext" and row.stored_path == expected_path
        ]
    finally:
        repo.close()
    if len(matches) != 1:
        raise RuntimeError("stored fetch result cannot bind a registered fulltext")
    payload["_fetch_auto_source"] = _fetch_auto_source_binding(matches[0])


def _fetch_auto_completion_payloads(st: dict) -> tuple[dict[str, dict], dict[str, int]]:
    """Load durable automatic-Fetch conclusions, independent of integrity signing."""
    repo = _repo_open(st["run_dir"])
    if repo is None:
        raise RuntimeError("fetch automatic completion repository is unavailable")
    try:
        rows = repo.list_integrity_unit_completions("fetch_auto")
        completed, counts = _validated_fetch_auto_completion_payloads(rows)
        stored_refs = {
            ref_id for ref_id, payload in completed.items()
            if payload.get("status") in {"stored", "already_stored"}
        }
        sources_by_ref: dict[str, list] = {}
        if stored_refs:
            for row in repo.list_source_texts():
                if row.ref_id in stored_refs:
                    sources_by_ref.setdefault(row.ref_id, []).append(row)
    finally:
        repo.close()
    for ref_id in stored_refs:
        if not _stored_fetch_auto_payload_is_current(completed[ref_id], sources_by_ref):
            del completed[ref_id]
    return completed, counts


def _append_fetch_auto_completion(
    st: dict, ref_id: str, payload: dict, prior_count: int
) -> None:
    """Append an audited automatic-Fetch conclusion, then rotate integrity state."""
    if (
        not isinstance(ref_id, str) or not ref_id
        or payload.get("ref_id") != ref_id
        or not isinstance(prior_count, int) or isinstance(prior_count, bool)
        or prior_count < 0
    ):
        raise RuntimeError("fetch automatic completion identity is invalid")
    unit_id = ref_id if prior_count == 0 else f"{ref_id}#{prior_count + 1}"
    repo = _repo_open(st["run_dir"])
    if repo is None:
        raise RuntimeError("fetch automatic completion repository is unavailable")
    try:
        repo.append_integrity_unit_completion("fetch_auto", unit_id, payload)
        callback = st.get("_integrity_unit_checkpoint")
        if callable(callback):
            callback("fetch_auto", unit_id)
    finally:
        repo.close()


def _checkpoint_applied_fetch_answer(st: dict, handle: str) -> None:
    callback = st.get("_integrity_unit_checkpoint")
    if callable(callback):
        callback("fetch_answer", handle)


def _automatic_ocr_enabled() -> bool:
    return (os.environ.get(ENV_OCR_AUTO) or "1").strip().lower() in ("1", "true", "yes", "on")


def _automatic_ocr_page_limit() -> int:
    try:
        return max(1, int(os.environ.get(ENV_OCR_AUTO_MAX_PAGES) or AUTO_OCR_MAX_PAGES))
    except ValueError:
        return AUTO_OCR_MAX_PAGES


# --------------------------------------------------------------------------- #
#  Phase: fetch (LLM/agent web tools — opt pause for missing full text)        #
# --------------------------------------------------------------------------- #

def _auto_fetch_fulltexts(st, need, refs, *, resolve_fetch_context=None):
    """Best-effort deterministic OA fetch before asking the agent/human."""
    run = st["run_dir"]
    checkpointing = callable(st.get("_integrity_unit_checkpoint"))
    fetch_context = resolve_fetch_context
    if fetch_context is None:
        fetch_context = _fetch.new_fetch_run_context()
    else:
        # Resolve may have recorded host challenges or transient entries.  Only
        # exact completed 2xx responses are safe to replay in formal Fetch.
        begin_fetch_phase = getattr(fetch_context, "begin_fetch_phase", None)
        if callable(begin_fetch_phase):
            begin_fetch_phase()
    resolve_map = _load_resolve_map(run)
    jobs = []
    seen_ref_ids = set()
    for idx, item in enumerate(need):
        ref_id = item.get("ref_id") if isinstance(item, dict) else item
        if ref_id in seen_ref_ids:
            continue
        seen_ref_ids.add(ref_id)
        ref = refs.get(ref_id)
        if not ref:
            continue
        resolve_result = resolve_map.get(ref_id)
        if not resolve_result:
            continue
        jobs.append((idx, ref_id, ref, resolve_result))

    budget = _fetch._fetch_budget_seconds()
    deadlines = {}
    # A typed provider cooldown can be reported after Fetch has started its
    # per-reference active-time budget.  Keep the unused portion process-local:
    # wall-clock time spent waiting in the coordinator must not consume it.
    paused_active_deadline_remaining = {}
    admission_deadlines = {}
    # Candidate stage freezes give a cooldown continuation an immutable, exact
    # candidate to replay.  Rebuilding the full ladder after a no-request
    # deferral reissued already-completed (and deliberately non-cacheable)
    # candidates such as DOI landing pages.
    deferred_candidate_ids = {}
    deferred_started_at = {}
    deferred_retry_counts = {}

    def _deferred_frozen_candidate_ids(fetched):
        trace = fetched.get("fetch_trace") if isinstance(fetched, dict) else None
        execution = trace.get("execution") if isinstance(trace, dict) else None
        attempts = execution.get("attempts") if isinstance(execution, dict) else None
        if not isinstance(attempts, list):
            return []
        ids = []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            if not (
                attempt.get("outcome") == "rate_limit_deferred"
                and attempt.get("reason_code") in {"host_cooldown", "rate_limit_response"}
            ):
                continue
            candidate_id = attempt.get("frozen_candidate_id")
            if type(candidate_id) is int and candidate_id > 0 and candidate_id not in ids:
                ids.append(candidate_id)
        return ids

    def _rate_limited_candidate_ids(fetched):
        trace = fetched.get("fetch_trace") if isinstance(fetched, dict) else None
        execution = trace.get("execution") if isinstance(trace, dict) else None
        attempts = execution.get("attempts") if isinstance(execution, dict) else None
        if not isinstance(attempts, list):
            return []
        ids = []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            candidate_id = attempt.get("frozen_candidate_id")
            if (
                type(candidate_id) is int and candidate_id > 0
                and attempt.get("status") == 429 and candidate_id not in ids
            ):
                ids.append(candidate_id)
        return ids

    def _archive_circuit_frozen_candidate_ids(fetched):
        trace = fetched.get("fetch_trace") if isinstance(fetched, dict) else None
        execution = trace.get("execution") if isinstance(trace, dict) else None
        attempts = execution.get("attempts") if isinstance(execution, dict) else None
        if not isinstance(attempts, list):
            return []
        ids = []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            candidate_id = attempt.get("frozen_candidate_id")
            if (
                attempt.get("reason_code") == "archive_circuit_open"
                and type(candidate_id) is int
                and candidate_id > 0
                and candidate_id not in ids
            ):
                ids.append(candidate_id)
        return ids

    def _incomplete_rate_limit_result(fetched, reason):
        result = dict(fetched)
        result.update({
            # ``download_error`` is the established terminal Fetch outcome for
            # an unavailable transport.  The reason is explicit so reporting
            # cannot mistake the bounded rate-limit conclusion for not_found.
            "status": "download_error",
            "reason": reason,
            "rate_limit_exhausted": True,
        })
        return result

    def _invalidate_rate_limited_continuations(ref_id, reason_code, candidate_ids=None):
        candidate_ids = (
            list(candidate_ids)
            if candidate_ids is not None
            else deferred_candidate_ids.get(ref_id) or []
        )
        if not candidate_ids:
            return
        repo = _repo_open(run)
        if repo is None:
            raise RuntimeError("fetch continuation lifecycle repository is unavailable")
        try:
            for candidate_id in candidate_ids:
                repo.record_frozen_fetch_candidate_event(
                    candidate_id,
                    "retry_invalidated",
                    reason_code=reason_code,
                    reason_detail="automatic Fetch rate-limit bound exhausted",
                )
        finally:
            repo.close()

    def run_one(job):
        idx, ref_id, ref, resolve_result = job
        deadline = deadlines.get(ref_id)
        if deadline is None:
            remaining = paused_active_deadline_remaining.pop(ref_id, None)
            if remaining is not None:
                deadline = time.monotonic() + remaining
                deadlines[ref_id] = deadline
        worker_repo = None
        worker_repo = _repo_open(run)
        if worker_repo is None:
            raise RuntimeError("fetch queue persistence repository is unavailable")
        lifecycle_actions = []
        claimed_candidate_ids = set()
        try:
            evidence_payload = _fetch_evidence_payload(resolve_result)
            continuation_ids = deferred_candidate_ids.get(ref_id)
            if continuation_ids:
                replayed = []
                for candidate_id in continuation_ids:
                    record = worker_repo.get_frozen_fetch_candidate_record(candidate_id)
                    if record.get("ref_id") != ref_id:
                        raise RuntimeError("deferred fetch candidate belongs to another reference")
                    if not worker_repo.claim_frozen_fetch_candidate(candidate_id):
                        raise RuntimeError("deferred fetch candidate is no longer eligible")
                    claimed_candidate_ids.add(candidate_id)
                    frozen_context = _fetch._frozen_fetch_context(
                        ref, resolve_result, evidence_payload=evidence_payload,
                    )
                    hooks = FetchQueuePersistenceHooks(
                        freeze_stage_segment=lambda stage, candidates: worker_repo.freeze_fetch_candidate_stage(
                            ref_id, stage, frozen_context, candidates,
                        ),
                        admit_batch=worker_repo.admit_frozen_fetch_candidate_batch,
                    )
                    fetched = _fetch._fetch_frozen_candidate(
                        ref, run, resolve_result, record["candidate"], candidate_id,
                        mailto=st.get("mailto"), fetch_context=fetch_context,
                        evidence_payload=evidence_payload,
                        queue_persistence_hooks=hooks,
                        stage=record["stage"],
                    )
                    replayed.append(fetched)
                    deferred_ids = _deferred_frozen_candidate_ids(fetched)
                    circuit_skipped_ids = _archive_circuit_frozen_candidate_ids(fetched)
                    if candidate_id in circuit_skipped_ids:
                        pass
                    elif candidate_id in deferred_ids:
                        lifecycle_actions.append(("defer", candidate_id))
                    else:
                        lifecycle_actions.append(("complete", candidate_id))
                    # Landing parsing may deterministically discover a new
                    # frozen child.  It was admitted and physically attempted
                    # in this automatic continuation; claim it now so the
                    # same persisted 429/cooldown trace can keep only that
                    # child pending on the next wake.
                    for deferred_id in deferred_ids:
                        if deferred_id == candidate_id:
                            continue
                        if not worker_repo.claim_frozen_fetch_candidate(deferred_id):
                            raise RuntimeError("new frozen landing child is not eligible")
                        lifecycle_actions.append(("defer", deferred_id))
                    if fetched.get("status") == "stored":
                        break
                if not replayed:
                    raise RuntimeError("deferred fetch continuation is empty")
                execution = []
                for replay in replayed:
                    trace = replay.get("fetch_trace") if isinstance(replay, dict) else None
                    execution.extend(
                        ((trace.get("execution") or {}).get("attempts") or [])
                        if isinstance(trace, dict) else []
                    )
                # A successful sibling is conclusive; do not replay pending
                # candidates. Host cooldown state is owned by the limiter and is
                # intentionally left intact for later references.
                if any(item.get("status") == "stored" for item in replayed):
                    fetched = next(item for item in replayed if item.get("status") == "stored")
                    fetched = dict(fetched)
                    fetched["fetch_trace"] = {"execution": {"batches": [], "attempts": execution}}
                    completed_ids = {
                        candidate_id for action, candidate_id in lifecycle_actions
                        if action == "complete"
                    }
                    pending_ids = {
                        candidate_id for action, candidate_id in lifecycle_actions
                        if action == "defer"
                    }
                    pending_ids.update(continuation_ids)
                    for candidate_id in pending_ids:
                        if candidate_id not in completed_ids:
                            lifecycle_actions.append(("invalidate", candidate_id))
                else:
                    pending = [
                        item for item in replayed
                        if item.get("status") == "rate_limit_deferred"
                    ]
                    if pending:
                        fetched = min(
                            pending,
                            key=lambda item: item.get("deferred_until", float("inf")),
                        )
                        fetched = dict(fetched)
                    else:
                        fetched = dict(replayed[-1])
                    fetched["fetch_trace"] = {"execution": {"batches": [], "attempts": execution}}
                candidate_results = {}
                for item in replayed:
                    if item.get("status") != "rate_limit_deferred":
                        continue
                    detail = {
                        key: item.get(key)
                        for key in ("deferred_until", "deferred_host", "method", "reason")
                    }
                    for candidate_id in _deferred_frozen_candidate_ids(item):
                        candidate_results[candidate_id] = detail
                fetched["_deferred_candidate_results"] = candidate_results
            else:
                frozen_context = _fetch._frozen_fetch_context(
                    ref, resolve_result, evidence_payload=evidence_payload
                )
                hooks = FetchQueuePersistenceHooks(
                    freeze_stage_segment=lambda stage, candidates: worker_repo.freeze_fetch_candidate_stage(
                        ref_id, stage, frozen_context, candidates
                    ),
                    admit_batch=worker_repo.admit_frozen_fetch_candidate_batch,
                )
                try:
                    fetched = _fetch.fetch_fulltext(
                        ref,
                        run,
                        resolve_result,
                        mailto=st.get("mailto"),
                        fetch_context=fetch_context,
                        evidence_payload=evidence_payload,
                        queue_persistence_hooks=hooks,
                        **({"deadline": deadline} if deadline is not None else {}),
                    )
                except FetchAdmissionDeferred as e:
                    fetched = {
                        "status": "rate_limit_deferred",
                        "method": "auto",
                        "reason": "host cooldown deferred provider admission",
                        "deferred_host": e.host,
                        "deferred_until": e.not_before,
                        "fetch_trace": {"execution": {"batches": [], "attempts": [{
                            "queue_index": 0, "batch_index": 0,
                            "method": "provider_callback", "url": f"https://{e.host}/",
                            "kind": "document",
                            "outcome": "rate_limit_deferred", "request": "none",
                            "reason_code": "host_cooldown",
                            "reason": f"host cooldown deferred {e.host} not-before {e.not_before:g}",
                        }]}},
                    }
                for candidate_id in _deferred_frozen_candidate_ids(fetched):
                    # Claim before the trace is persisted: after the no-request
                    # trace is bound, the durable protocol correctly refuses a
                    # new claim until it has a retry_deferred lifecycle event.
                    if not worker_repo.claim_frozen_fetch_candidate(candidate_id):
                        raise RuntimeError("initial deferred fetch candidate is not eligible")
                    lifecycle_actions.append(("defer", candidate_id))
            for candidate_id in _archive_circuit_frozen_candidate_ids(fetched):
                if candidate_id not in claimed_candidate_ids:
                    if not worker_repo.claim_frozen_fetch_candidate(candidate_id):
                        raise RuntimeError("circuit-open fetch candidate is not eligible")
                    claimed_candidate_ids.add(candidate_id)
                lifecycle_actions.append(("invalidate_archive", candidate_id))
        finally:
            worker_repo.close()
        fetched["ref_id"] = ref_id
        fetched["ref_number"] = ref.get("ref_number")
        frozen_attempt_ids = _record_fetch_attempts(run, ref_id, fetched, resolve_result)
        if lifecycle_actions:
            lifecycle_repo = _repo_open(run)
            if lifecycle_repo is None:
                raise RuntimeError("fetch continuation lifecycle repository is unavailable")
            try:
                for action, candidate_id in lifecycle_actions:
                    if action == "invalidate":
                        lifecycle_repo.record_frozen_fetch_candidate_event(
                            candidate_id, "retry_invalidated",
                            reason_code="sibling_fulltext_stored",
                            reason_detail="full text stored by another deferred candidate",
                        )
                        continue
                    if action == "invalidate_archive":
                        # A replay was claimed before it was evaluated.  Its
                        # circuit-open trace proves no request escaped; the
                        # lifecycle protocol requires that exact attempt.
                        attempt_id = frozen_attempt_ids.get(candidate_id)
                        if attempt_id is None:
                            raise RuntimeError("claimed circuit-open candidate trace was not persisted")
                        lifecycle_repo.record_frozen_fetch_candidate_event(
                            candidate_id, "retry_invalidated",
                            fetch_attempt_id=attempt_id,
                            reason_code="archive_circuit_open",
                            reason_detail="Archive.org circuit opened before candidate admission",
                        )
                        continue
                    attempt_id = frozen_attempt_ids.get(candidate_id)
                    if attempt_id is None:
                        raise RuntimeError("deferred fetch candidate trace was not persisted")
                    lifecycle_repo.record_frozen_fetch_candidate_event(
                        candidate_id,
                        "retry_deferred" if action == "defer" else "retry_completed",
                        fetch_attempt_id=attempt_id,
                        **({
                            "reason_code": (
                                "rate_limit_response"
                                if any(
                                    attempt.get("frozen_candidate_id") == candidate_id
                                    and attempt.get("status") == 429
                                    for attempt in (
                                        (fetched.get("fetch_trace") or {}).get("execution", {}).get("attempts", [])
                                    )
                                    if isinstance(attempt, dict)
                                ) else "host_cooldown"
                            ),
                            "reason_detail": (
                                "HTTP 429 requeued at limiter cooldown expiry"
                                if any(
                                    attempt.get("frozen_candidate_id") == candidate_id
                                    and attempt.get("status") == 429
                                    for attempt in (
                                        (fetched.get("fetch_trace") or {}).get("execution", {}).get("attempts", [])
                                    )
                                    if isinstance(attempt, dict)
                                ) else "candidate admission remained in host cooldown"
                            ),
                        } if action == "defer" else {}),
                    )
            finally:
                lifecycle_repo.close()
        return idx, fetched

    all_jobs = list(jobs)
    results_by_index = {}
    persisted_by_ref, completion_counts = _fetch_auto_completion_payloads(st)
    jobs = []
    for job in all_jobs:
        idx, ref_id, _ref, _resolve_result = job
        prior = persisted_by_ref.get(ref_id)
        if prior is not None:
            results_by_index[idx] = persisted_by_ref[ref_id]
        else:
            jobs.append(job)
    worker_count = (
        1 if jobs and checkpointing
        else min(len(jobs), 16, max(_fetch_worker_count(), (len(jobs) + 3) // 4)) if jobs
        else 0
    )
    ready = list(jobs)
    deferred = []
    deferred_sequence = 0
    completed_progress = 0
    started_ref_ids: set[str] = set()
    if jobs:
        _progress(
            "automatic full-text retrieval: "
            f"0/{len(jobs)} references completed "
            f"({worker_count} {'worker' if worker_count == 1 else 'workers'})"
        )

    def report_completion(fetched):
        nonlocal completed_progress
        completed_progress += 1
        status = fetched.get("status") or "unknown"
        if status == "stored":
            outcome = f"stored full text via {fetched.get('method') or 'unknown'}"
        else:
            outcome = f"no full text stored ({status})"
        _progress(
            "automatic full-text retrieval "
            f"{completed_progress}/{len(jobs)}: reference "
            f"[{fetched.get('ref_number')}] {outcome}"
        )

    def report_started(job):
        _idx, ref_id, ref, _resolve_result = job
        if ref_id in started_ref_ids:
            return
        started_ref_ids.add(ref_id)
        _progress(
            "automatic full-text retrieval: starting reference "
            f"[{ref.get('ref_number')}]"
        )

    def classify_no_request_provider_defer(fetched):
        trace = fetched.get("fetch_trace")
        if trace is None:
            return False, False
        if not isinstance(trace, dict):
            return False, True
        execution = trace.get("execution")
        if execution is None:
            return False, False
        if not isinstance(execution, dict):
            return False, True
        attempts = execution.get("attempts")
        if attempts is None:
            return False, False
        if not isinstance(attempts, list) or any(
            not isinstance(attempt, dict) for attempt in attempts
        ):
            return False, True
        return any(
            isinstance(attempt, dict)
            and attempt.get("request") == "none"
            and attempt.get("outcome") == "rate_limit_deferred"
            and attempt.get("reason_code") == "host_cooldown"
            for attempt in attempts
        ), False

    def finish_one(idx, fetched, job):
        nonlocal deferred_sequence
        if fetched.get("status") == "rate_limit_deferred":
            ref_id = job[1]
            started_at = deferred_started_at.setdefault(ref_id, time.monotonic())
            continuation_ids = _deferred_frozen_candidate_ids(fetched)
            if continuation_ids:
                deferred_candidate_ids[ref_id] = continuation_ids
            retry_counts = deferred_retry_counts.setdefault(ref_id, {})
            # Count physical responses per candidate. A no-I/O preflight
            # cooldown consumes nothing; the first physical 429 plus two
            # physical retry 429s exhaust the candidate.
            for candidate_id in _rate_limited_candidate_ids(fetched):
                retry_counts[candidate_id] = retry_counts.get(candidate_id, 0) + 1
            exhausted_ids = [
                candidate_id for candidate_id, count in retry_counts.items()
                if count >= _FETCH_DEFERRED_MAX_PHYSICAL_429S
            ]
            if exhausted_ids and continuation_ids:
                _invalidate_rate_limited_continuations(
                    ref_id, "rate_limit_retry_exhausted", exhausted_ids
                )
                continuation_ids = [
                    candidate_id for candidate_id in continuation_ids
                    if candidate_id not in exhausted_ids
                ]
                if continuation_ids:
                    deferred_candidate_ids[ref_id] = continuation_ids
                    results_by_candidate = fetched.get("_deferred_candidate_results")
                    if isinstance(results_by_candidate, dict):
                        remaining = [
                            results_by_candidate.get(candidate_id)
                            for candidate_id in continuation_ids
                            if isinstance(results_by_candidate.get(candidate_id), dict)
                            and isinstance(
                                results_by_candidate[candidate_id].get("deferred_until"),
                                (int, float),
                            )
                        ]
                        if remaining:
                            next_defer = min(
                                remaining, key=lambda item: item["deferred_until"]
                            )
                            fetched = dict(fetched)
                            fetched.update(next_defer)
                else:
                    deferred_candidate_ids[ref_id] = []
                    fetched = _incomplete_rate_limit_result(
                        fetched,
                        "fetch candidate rate-limit retry limit exhausted",
                    )
            elif exhausted_ids:
                fetched = _incomplete_rate_limit_result(
                    fetched,
                    "fetch candidate rate-limit retry limit exhausted",
                )
            elif time.monotonic() - started_at >= _FETCH_DEFERRED_WALL_SECONDS:
                fetched = _incomplete_rate_limit_result(
                    fetched,
                    "per-reference rate-limit wait limit exhausted",
                )
            if fetched.get("status") != "rate_limit_deferred":
                _invalidate_rate_limited_continuations(
                    ref_id,
                    "rate_limit_retry_exhausted"
                    if "retry limit" in str(fetched.get("reason") or "")
                    else "rate_limit_wait_exhausted",
                )
                deferred_candidate_ids.pop(ref_id, None)
                deferred_started_at.pop(ref_id, None)
                deferred_retry_counts.pop(ref_id, None)
                results_by_index[idx] = fetched
                admission_deadlines.pop(ref_id, None)
                paused_active_deadline_remaining.pop(ref_id, None)
                fetched.pop("fetch_deadline", None)
                fetched.pop("_deferred_candidate_results", None)
                fetched["ref_id"] = ref_id
                fetched["ref_number"] = job[2].get("ref_number")
                _bind_stored_fetch_auto_payload(run, fetched)
                _append_fetch_auto_completion(st, ref_id, fetched, completion_counts.get(ref_id, 0))
                completion_counts[ref_id] = completion_counts.get(ref_id, 0) + 1
                persisted_by_ref[ref_id] = fetched
                report_completion(fetched)
                return
            due = fetched.get("deferred_until")
            active_deadline = fetched.get("fetch_deadline")
            if active_deadline is None:
                active_deadline = deadlines.get(job[1])
            typed_no_request_defer, malformed_defer_trace = (
                classify_no_request_provider_defer(fetched)
            )
            deadline = active_deadline
            if deadline is not None:
                deadlines[job[1]] = deadline
                admission_deadlines.pop(job[1], None)
            else:
                deadline = admission_deadlines.get(job[1])
                if (
                    deadline is None
                    and isinstance(budget, (int, float))
                    and math.isfinite(budget)
                    and budget > 0
                ):
                    deadline = time.monotonic() + budget
                    admission_deadlines[job[1]] = deadline
            if (
                not isinstance(due, (int, float))
                or isinstance(due, bool)
                or not math.isfinite(due)
            ):
                fetched = dict(fetched)
                fetched.update({
                    "status": "download_error",
                    "reason": "candidate admission deferred without a finite not-before time",
                })
            elif due >= started_at + _FETCH_DEFERRED_WALL_SECONDS:
                fetched = _incomplete_rate_limit_result(
                    fetched, "per-reference rate-limit wait limit exhausted"
                )
            elif malformed_defer_trace and active_deadline is not None:
                fetched = dict(fetched)
                fetched.update({
                    "status": "download_error",
                    "reason": "candidate admission defer has a malformed fetch trace",
                })
            elif (
                isinstance(active_deadline, (int, float))
                and not isinstance(active_deadline, bool)
                and math.isfinite(active_deadline)
                and typed_no_request_defer
            ):
                paused_active_deadline_remaining[job[1]] = max(
                    0.0, active_deadline - time.monotonic()
                )
                deadlines.pop(job[1], None)
                admission_deadlines.pop(job[1], None)
                host = fetched.get("deferred_host")
                canonical_host = (
                    _host_limiter.host_for_url("//" + host)
                    if isinstance(host, str) and host.strip()
                    else ""
                )
                if not canonical_host:
                    fetched = dict(fetched)
                    fetched.update({
                        "status": "download_error",
                        "reason": "candidate admission deferred without a host",
                    })
                else:
                    deferred_sequence += 1
                    heapq.heappush(
                        deferred, (float(due), deferred_sequence, canonical_host, job)
                    )
                    return
            elif deadline is not None and due >= deadline:
                fetched = dict(fetched)
                fetched.update({
                    "status": "download_error",
                    "reason": (
                        "per-reference fetch deadline expired while candidate admission was deferred"
                        if job[1] in deadlines
                        else "provider admission deadline expired while candidate admission was deferred"
                    ),
                })
            else:
                host = fetched.get("deferred_host")
                canonical_host = (
                    _host_limiter.host_for_url("//" + host)
                    if isinstance(host, str) and host.strip()
                    else ""
                )
                if not canonical_host:
                    fetched = dict(fetched)
                    fetched.update({
                        "status": "download_error",
                        "reason": "candidate admission deferred without a host",
                    })
                else:
                    deferred_sequence += 1
                    heapq.heappush(
                        deferred, (float(due), deferred_sequence, canonical_host, job)
                    )
                    return
        if fetched.get("rate_limit_exhausted"):
            _invalidate_rate_limited_continuations(
                job[1],
                "rate_limit_retry_exhausted"
                if "retry limit" in str(fetched.get("reason") or "")
                else "rate_limit_wait_exhausted",
            )
            deferred_candidate_ids.pop(job[1], None)
            deferred_started_at.pop(job[1], None)
            deferred_retry_counts.pop(job[1], None)
        results_by_index[idx] = fetched
        admission_deadlines.pop(job[1], None)
        paused_active_deadline_remaining.pop(job[1], None)
        # ``fetch_deadline`` is monotonic process-local scheduler state, never
        # a run payload or checkpoint fact. The persisted trace/reason records
        # the observable defer/deadline outcome instead.
        fetched.pop("fetch_deadline", None)
        fetched.pop("_deferred_candidate_results", None)
        ref_id = fetched["ref_id"]
        _bind_stored_fetch_auto_payload(run, fetched)
        _append_fetch_auto_completion(st, ref_id, fetched, completion_counts.get(ref_id, 0))
        completion_counts[ref_id] = completion_counts.get(ref_id, 0) + 1
        persisted_by_ref[ref_id] = fetched
        report_completion(fetched)

    while ready or deferred:
        now = time.monotonic()
        while deferred and deferred[0][0] <= now:
            _due, _sequence, _host, job = heapq.heappop(deferred)
            ready.append(job)
        if not ready:
            # The coordinator (never a Fetch worker) waits for the earliest
            # authoritative cooldown expiry. No request is sent before re-admission.
            due, _sequence, host, deferred_job = deferred[0]
            wait_seconds = max(0.0, due - time.monotonic())
            _progress(
                "automatic full-text retrieval: reference "
                f"[{deferred_job[2].get('ref_number')}] waiting "
                f"{max(1, math.ceil(wait_seconds))}s for {host or 'provider'} cooldown"
            )
            with _perf.span("fetch_wait", (host or "").lower()):
                time.sleep(max(0.0, due - time.monotonic()))
            continue
        batch, ready = ready, []
        if checkpointing or worker_count <= 1:
            for job in batch:
                report_started(job)
                idx, fetched = run_one(job)
                finish_one(idx, fetched, job)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
                future_map = {}
                for job in batch:
                    report_started(job)
                    future_map[pool.submit(run_one, job)] = job
                for fut in concurrent.futures.as_completed(future_map):
                    job = future_map[fut]
                    idx, fetched = fut.result()
                    finish_one(idx, fetched, job)
    results = [
        results_by_index[idx]
        for idx, *_rest in all_jobs
        if idx in results_by_index
    ]
    if all(ref_id in persisted_by_ref for _idx, ref_id, *_rest in all_jobs):
        st["auto_fetch_attempted"] = True
    _save_state(st)
    if checkpointing:
        st["_integrity_unit_checkpoint"]("fetch_auto_complete", "all")
    return results


def _seed_reusable_sources(
    run_dir: str, refs: dict[str, dict], *, state: dict | None = None
) -> dict[str, int]:
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        existing: dict[str, set[str]] = {}
        for row in repo.list_source_texts():
            existing.setdefault(row.ref_id, set()).add(row.tier)
        resolve_map = repo.resolve_payload_map()
    finally:
        repo.close()
    seeded = {"fulltext": 0, "abstract": 0}
    for ref_id, ref in refs.items():
        present = existing.get(ref_id, set())
        seeded_for_ref = 0
        seeded_document_relation = None
        for tier in ("fulltext", "abstract"):
            if tier in present:
                continue
            # When fulltext is possible, don't seed an abstract from the
            # content store — it would block re-fetch on resume (conditions
            # may have changed: OCR installed, new OA copies, etc.).
            if tier == "abstract":
                resolution = resolve_map.get(ref_id) or {}
                fulltext_exists = resolution.get("fulltext_exists")
                if fulltext_exists is not False:
                    continue
            cached = content_store.find_reusable_parsed_text(
                run_dir,
                ref,
                tiers=(tier,),
                resolve_result=resolve_map.get(ref_id),
            )
            if not cached:
                continue
            with open(cached["stored_path"], encoding="utf-8", errors="replace") as f:
                text = f.read()
            _sources.store_text(
                run_dir,
                ref,
                tier,
                cached["origin"],
                text,
                source_ref=cached.get("source_ref") or cached.get("stored_relpath"),
                mapping=cached.get("mapping") or "content_store_reuse",
                signal=cached.get("match_signal"),
                score=cached.get("match_score"),
                identity_status=cached.get("identity_status"),
                identity_note=cached.get("identity_note"),
                content_version=cached.get("content_version"),
                supplied_by=cached.get("supplied_by"),
                supplied_via=cached.get("supplied_via"),
                file_format=cached.get("file_format"),
                extraction_flags=cached.get("extraction_flags"),
                extraction_method=cached.get("extraction_method"),
            )
            seeded[tier] += 1
            seeded_for_ref += 1
            present.add(tier)
            if tier == "fulltext" and "document_relation" in cached:
                seeded_document_relation = cached["document_relation"]
        if seeded_for_ref and state is not None:
            payload = {"seeded_source_count": seeded_for_ref}
            if seeded_document_relation is not None:
                payload["document_relation"] = seeded_document_relation
            _checkpoint_integrity_unit(
                state,
                "fetch_cache_seed",
                ref_id,
                payload=payload,
            )
    return seeded


def _fetchable_need_items(need, resolve_map):
    """Drop what must not be chased: fabrications, and inherited back-references.

    A back-reference note ("Id. at 96.", "X, supra note 10, at 331.") has no text
    to retrieve of its own — resolve marks it resolution_basis="cross_reference"
    and verify reads its antecedent's stored text.  Chasing it anyway fetches and
    re-extracts the antecedent's document a second time, and puts a manual FETCH
    task on the queue whose retrieval target is the string "Id. at 96." — for a
    document the antecedent's own task is already asking for.  On the law review
    in the corpus that is 191 of 397 gap rows.
    """
    out = []
    for item in need:
        ref_id = item.get("ref_id") if isinstance(item, dict) else item
        resolution = resolve_map.get(ref_id) or {}
        if resolution.get("reference_status_tag") == "suspected_fabricated":
            continue
        if resolution.get("resolution_basis") == "cross_reference":
            continue
        out.append(item)
    return out


_RESOLVER_DISPLAY_NAMES = {
    "crossref": "Crossref",
    "datacite": "DataCite",
    "europepmc": "Europe PMC",
    "openalex": "OpenAlex",
    "springer_complete_issue": "Springer official issue archive",
    "pubmed": "PubMed",
    "pubmed_coordinate_occupancy": "PubMed",
}


def _resolver_display_name(value) -> str:
    text = str(value or "").strip()
    return _RESOLVER_DISPLAY_NAMES.get(text.lower(), text or "The resolver")


def _joined_display_names(values) -> str:
    names = [_resolver_display_name(value) for value in values if value]
    names = list(dict.fromkeys(names))
    if not names:
        return "The configured resolvers"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _structured_refutation_explanation(profile: dict) -> str | None:
    adjudication = profile.get("bibliographic_adjudication") or {}
    explanations = []
    for refutation in adjudication.get("refutations") or []:
        if not isinstance(refutation, dict):
            continue
        source = _resolver_display_name(refutation.get("source"))
        cited = str(refutation.get("cited_value") or "").strip()
        observed = str(refutation.get("observed_value") or "").strip()
        field = str(refutation.get("field") or "bibliographic field").strip()
        kind = refutation.get("kind")
        if kind == "coordinate_occupied_by_other_work" and cited and observed:
            explanations.append(
                f'{source} positively matched the cited coordinates "{cited}" '
                f'to a different work: "{observed}"'
            )
        elif kind == "identifier_targets_other_work" and cited:
            suffix = f': "{observed}"' if observed else ""
            explanations.append(
                f'{source} resolved the cited identifier "{cited}" to a different work{suffix}'
            )
        elif kind == "identifier_not_found" and cited:
            explanations.append(
                f'{source} authoritatively reported the cited identifier "{cited}" as not found'
            )
        elif kind == "absent_from_complete_issue" and cited:
            explanations.append(
                f'{source} completed the issue inventory and did not find the cited work at "{cited}"'
            )
        elif kind in {"coordinate_mismatch", "year_mismatch", "author_mismatch"} and cited:
            suffix = f', but observed "{observed}"' if observed else ""
            explanations.append(
                f'{source} checked {field} and found cited "{cited}"{suffix}'
            )
        else:
            basis = str(refutation.get("basis") or "").strip()
            if basis:
                explanations.append(f"{source}: {basis}")
    return "; ".join(explanations) or None


def _resolver_coverage_explanation(coverage: dict) -> str | None:
    observations = coverage.get("observations") or []
    lookups = coverage.get("article_lookups") or []
    for observation in observations:
        if not isinstance(observation, dict) or not (
            observation.get("fresh")
            and observation.get("status") == "covered"
            and observation.get("completion") == "complete"
            and observation.get("http_status") == 200
        ):
            continue
        lookup = next((
            item for item in lookups
            if isinstance(item, dict)
            and item.get("resolver") == observation.get("resolver")
            and item.get("completion") == "complete"
            and item.get("match_status") == "no_compatible_article"
            and item.get("http_status") == 200
        ), None)
        if lookup is None:
            continue
        resolver = _resolver_display_name(observation.get("resolver"))
        authority = coverage.get("authority") or {}
        journal = str(authority.get("canonical_title") or "").strip()
        issns = [str(value).strip() for value in authority.get("issns") or [] if value]
        journal_detail = f' "{journal}"' if journal else " the cited journal"
        journal_facts = []
        if issns:
            journal_facts.append(f"ISSN {', '.join(issns)}")
        work_count = observation.get("work_count")
        if isinstance(work_count, int) and work_count > 0:
            journal_facts.append(f"{work_count:,} indexed works")
        if journal_facts:
            journal_detail += f" ({'; '.join(journal_facts)})"
        scope = str(lookup.get("scope") or "the supplied bibliographic coordinates")
        if scope == "canonical journal/year/volume/first locator":
            scope = "journal, year, volume, and first page or article number"
        return (
            f"{resolver} indexes{journal_detail}, but its complete search by {scope} "
            "found no matching paper"
        )
    return None


def _suspicion_explanation(profile: dict, suspicion: dict, fallback: str) -> str:
    if (
        suspicion.get("conclusion")
        == "complete_issue_absence_with_incomplete_identity_checks"
    ):
        adjudication = profile.get("bibliographic_adjudication") or {}
        for refutation in adjudication.get("refutations") or []:
            if (
                not isinstance(refutation, dict)
                or refutation.get("kind") != "absent_from_complete_issue"
            ):
                continue
            resolver = _resolver_display_name(refutation.get("source"))
            cited = str(refutation.get("cited_value") or "").strip()
            if cited:
                return (
                    f'{resolver} completed the official issue inventory and excluded '
                    f'the cited work at "{cited}", but identity checks remain incomplete; '
                    "this does not establish that the work does not exist"
                )
        return fallback
    refutation = _structured_refutation_explanation(profile)
    if refutation:
        return refutation
    coverage = profile.get("resolver_coverage") or {}
    if suspicion.get("suspicion_level") == "high":
        concrete = _resolver_coverage_explanation(coverage)
        if concrete:
            return concrete
    providers = suspicion.get("providers") or []
    if providers:
        authority = coverage.get("authority") or profile.get("journal_authority") or {}
        journal = str(authority.get("canonical_title") or "").strip()
        journal_note = f' for the recognized journal "{journal}"' if journal else ""
        return (
            f"{_joined_display_names(providers)} completed independent bibliographic "
            f"searches{journal_note} without finding a compatible source"
        )
    return fallback


def _print_automatic_fetch_summary(run: str, refs: dict[str, dict], resolve_map: dict) -> None:
    """Report materialized automatic-Fetch coverage without metadata fallbacks."""
    tiers_by_ref: dict[str, set[str]] = {}
    for entry in _load_manifest_payload(run).get("entries", []):
        if not isinstance(entry, dict):
            continue
        ref_id = entry.get("ref_id")
        tier = entry.get("tier")
        if ref_id in refs and tier in ("fulltext", "abstract"):
            tiers_by_ref.setdefault(ref_id, set()).add(tier)

    fulltext_ids = {
        ref_id for ref_id, tiers in tiers_by_ref.items() if "fulltext" in tiers
    }
    abstract_only_ids = {
        ref_id for ref_id, tiers in tiers_by_ref.items()
        if ref_id not in fulltext_ids and "abstract" in tiers
    }
    total = len(refs)
    fulltext_count = len(fulltext_ids)
    abstract_only_count = len(abstract_only_ids)
    neither_count = total - fulltext_count - abstract_only_count
    _progress(
        "automatic fetch summary: "
        f"full text found {fulltext_count}/{total}; "
        "abstract-only among references without full text "
        f"{abstract_only_count}/{total - fulltext_count}; "
        f"neither full text nor abstract {neither_count}/{total}"
    )

    outcomes = {
        ref_id: ((resolution.get("evidence_profile") or {}).get(
            "bibliographic_adjudication"
        ) or {}).get("outcome")
        for ref_id, resolution in resolve_map.items()
        if ref_id in refs and isinstance(resolution, dict)
    }
    identified = sum(outcome == "identified" for outcome in outcomes.values())
    identified_with_errors = sum(
        outcome == "identified_with_errors" for outcome in outcomes.values()
    )
    refuted = sum(outcome == "refuted" for outcome in outcomes.values())
    not_corroborated = sum(
        outcome == "not_corroborated" for outcome in outcomes.values()
    )
    checks_incomplete = sum(
        outcome == "checks_incomplete" for outcome in outcomes.values()
    )
    adjudicated = (
        identified + identified_with_errors + refuted
        + not_corroborated + checks_incomplete
    )
    _progress(
        "bibliographic adjudication: "
        f"identified {identified}/{total}; "
        f"identified with errors {identified_with_errors}/{total}; "
        f"refuted {refuted}/{total}; "
        f"not corroborated {not_corroborated}/{total}; "
        f"checks incomplete {checks_incomplete}/{total}; "
        f"not adjudicated {total - adjudicated}/{total}"
    )

    review_labels_by_ref = {
        str(ref.get("id")): bibliographic_review_labels(
            reference=ref,
            status=resolution.get("status"),
            attempts=resolution.get("attempts") or [],
            adjudication=((resolution.get("evidence_profile") or {}).get(
                "bibliographic_adjudication"
            ) or {}),
            coverage=((resolution.get("evidence_profile") or {}).get(
                "resolver_coverage"
            ) or {}),
        )
        for ref in refs.values()
        for resolution in [resolve_map.get(ref.get("id")) or {}]
        if isinstance(resolution, dict)
    }
    for code, text in (
        (
            "not_found_after_completed_searches",
            "completed independent searches",
        ),
        (
            "no_compatible_article_at_cited_coordinates",
            "completed coordinate lookups",
        ),
        (
            "incomplete_bibliographic_source",
            "incomplete bibliographic sources",
        ),
        (
            "author_list_discrepancy",
            "returned author-list discrepancies",
        ),
    ):
        matching = [
            ref for ref in refs.values()
            if any(label.get("code") == code for label in review_labels_by_ref.get(str(ref.get("id")), []))
        ]
        _progress(
            f"bibliographic review labels: {text} {len(matching)}"
            + ("" if matching else " (none)")
        )
        for ref in sorted(matching, key=lambda item: (item.get("ref_number") is None, item.get("ref_number") or 0)):
            _progress(
                f"bibliographic review [{ref.get('ref_number')}] "
                f"{ref.get('title') or ref.get('raw_entry') or ref.get('id')} — "
                + (
                    "the citation lacks an article page, elocator, or number and automatic "
                    "checks could neither identify nor refute it; informational only, not proof "
                    "that the reference is fabricated"
                    if code == "incomplete_bibliographic_source" else
                    "an explicitly cited author is absent from returned provider metadata; "
                    "the provider does not attest list completeness, so review is required "
                    "without refuting the identified work"
                    if code == "author_list_discrepancy" else
                    f"{text} did not identify a compatible source; informational only, "
                    "not proof that the reference is fabricated"
                )
            )

    suspicion_by_ref: dict[str, tuple[str, str]] = {}
    explanations = {
        "fresh_covered_same_resolver_complete_article_miss": (
            "a fresh resolver catalog confirms journal coverage, but the same "
            "resolver's complete coordinate lookup found no compatible article"
        ),
        "two_independent_exact_resolver_absences": (
            "two independent exact resolver probes do not index the registered journal"
        ),
        "recognized_journal_with_fresh_coverage_and_completed_bibliographic_misses": (
            "fresh coverage and completed independent searches did not identify a compatible source"
        ),
        "unconfirmed_or_unrecognized_journal_with_completed_bibliographic_misses": (
            "completed independent searches did not identify a compatible source for an unconfirmed journal"
        ),
        "complete_issue_absence_with_incomplete_identity_checks": (
            "the official complete issue inventory excludes the cited work, "
            "but identity checks remain incomplete"
        ),
    }
    ordered_refs = sorted(
        refs.values(),
        key=lambda ref: (
            ref.get("ref_number") is None,
            ref.get("ref_number") if isinstance(ref.get("ref_number"), int) else 0,
            str(ref.get("ref_number") or ref.get("id") or ""),
        ),
    )
    for ref in ordered_refs:
        ref_id = ref.get("id")
        resolution = resolve_map.get(ref_id) or {}
        profile = resolution.get("evidence_profile") or {}
        signals = (
            profile.get("resolver_coverage") or {},
            profile.get("bibliographic_suspicion") or {},
        )
        suspicion = next(
            (signal for signal in signals if signal.get("suspicion_level") == "high"),
            None,
        ) or next(
            (signal for signal in signals if signal.get("suspicion_level") == "elevated"),
            None,
        )
        if suspicion is None:
            continue
        conclusion = suspicion.get("conclusion")
        fallback = explanations.get(
            conclusion, conclusion or "reason unavailable",
        )
        suspicion_by_ref[str(ref_id)] = (
            suspicion["suspicion_level"],
            _suspicion_explanation(profile, suspicion, fallback),
        )

    suspects = [
        ref for ref in ordered_refs
        if (
            (resolve_map.get(ref.get("id")) or {}).get("reference_status_tag")
            == "suspected_fabricated"
            or suspicion_by_ref.get(str(ref.get("id")), (None,))[0] == "high"
        )
    ]
    suspect_ids = {str(ref.get("id")) for ref in suspects}
    elevated = [
        ref for ref in ordered_refs
        if (
            str(ref.get("id")) not in suspect_ids
            and suspicion_by_ref.get(str(ref.get("id")), (None,))[0] == "elevated"
        )
    ]
    _progress(
        f"elevated bibliographic suspicion: {len(elevated)}"
        + ("" if elevated else " (none)")
    )
    for ref in elevated:
        reason = suspicion_by_ref[str(ref.get("id"))][1]
        _progress(
            f"elevated bibliographic suspicion [{ref.get('ref_number')}] "
            f"{ref.get('title') or ref.get('raw_entry') or ref.get('id')} — {reason}"
        )

    _progress(
        f"suspected fabricated references: {len(suspects)}"
        + ("" if suspects else " (none)")
    )
    for ref in suspects:
        resolution = resolve_map.get(ref.get("id")) or {}
        signal = suspicion_by_ref.get(str(ref.get("id")))
        reason = None
        if resolution.get("reference_status_tag") == "suspected_fabricated":
            reason = _structured_refutation_explanation(
                resolution.get("evidence_profile") or {},
            ) or resolution.get("tag_reason")
        if not reason and signal and signal[0] == "high":
            reason = signal[1]
        if not reason:
            reason = "deterministic bibliographic evidence refuted the declared reference"
        _progress(
            f"suspected fabricated reference [{ref.get('ref_number')}] "
            f"{ref.get('title') or ref.get('raw_entry') or ref.get('id')} — {reason}"
        )


def _include_table_only_need(need, parse, refs, run_dir):
    """Add table-only references to full-text retrieval without making claims.

    The parser deliberately suppresses these citations unless the explicit
    verification flag is enabled.  Retrieval is a separate concern: a source
    named only in a table still belongs in the full-text evidence pool.  Re-read
    the DB-backed manifest on every call because the preceding automatic fetch
    may have stored the source since ``need`` was first computed.
    """
    out = list(need)
    fulltext_ref_ids = {
        entry.get("ref_id")
        for entry in _load_manifest_payload(run_dir).get("entries", [])
        if entry.get("tier") == "fulltext"
    }
    seen = {
        item.get("ref_id") if isinstance(item, dict) else item
        for item in out
    }
    for item in parse.get("table_only_citations") or []:
        ref_id = item.get("ref_id") if isinstance(item, dict) else item
        if (ref_id and ref_id in refs and ref_id not in seen
                and ref_id not in fulltext_ref_ids):
            out.append({"ref_id": ref_id, "table_only": True})
            seen.add(ref_id)
    return out


def _manual_fetch_dois(ref: dict, resolve_result: dict | None) -> list[str]:
    # Only consume identifier fields.  A DOI-shaped substring in a landing/PDF
    # URL is not an identity assertion: Springer URLs commonly append
    # ``.pdf`` or ``/fulltext.html`` to the DOI path.
    resolution = resolve_result or {}
    values = [
        ref.get("doi"),
        resolution.get("canonical_doi"),
        resolution.get("doi"),
    ]
    resolved_identifier = resolution.get("resolved_identifier") or {}
    if (
        isinstance(resolved_identifier, dict)
        and str(resolved_identifier.get("type") or "").lower() == "doi"
    ):
        values.append(resolved_identifier.get("value"))
    for key in ("fulltext_links", "auxiliary_fulltext_links"):
        for item in resolution.get(key) or []:
            if not isinstance(item, dict):
                continue
            context = item.get("identity_context") or {}
            identifiers = context.get("identifiers") if isinstance(context, dict) else {}
            if isinstance(identifiers, dict):
                values.append(identifiers.get("doi"))
    out = []
    for value in values:
        text = urllib.parse.unquote(str(value or "")).strip()
        text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
        text = re.sub(r"^doi:\s*", "", text, flags=re.I)
        # DOI fields are already identity-scoped.  Accept the complete
        # non-whitespace suffix, including legacy Wiley ``<...>`` syntax, and
        # do not trim punctuation that may be part of the identifier.
        if not re.fullmatch(r"10\.\d{4,9}/[^\s]+", text, re.I):
            continue
        doi = text
        if doi.lower() not in {item.lower() for item in out}:
            out.append(doi)
    return out


def _no_external_fulltext_expected(ref: dict, resolution: dict) -> bool:
    """True when fetch deliberately expects no external full text for this ref,
    so the manual-sources report must not flag it as actionable.

    Two by-design cases, mirroring the skips in ``core.fetch`` proper:

    * a pointer to the manuscript's OWN sections (``manuscript_pointer``) — there
      is no external document to supply;
    * a work-scoped availability negative with no contradictory candidate — the
      resolver established that the work has no retrievable full text.

    A reference URL, an auxiliary link, or a non-DOI full-text link is a
    contradictory candidate: fetch would still try it, so the ref stays
    actionable and must keep ``manual_action_required: yes``.
    """
    from core.fetch.refdata import _is_doi_url

    if resolution.get("reference_status_tag") == "manuscript_pointer":
        return True
    availability = resolution.get("fulltext_availability") or {}
    unavailable_for_work = (
        availability.get("status") == "not_available"
        and availability.get("scope") == "work"
    )
    if not unavailable_for_work:
        return False
    if str(ref.get("url") or "").strip():
        return False
    if any(
        isinstance(item, dict) and item.get("url")
        for item in (resolution.get("auxiliary_fulltext_links") or [])
    ):
        return False
    if any(
        isinstance(item, dict) and item.get("url") and not _is_doi_url(item.get("url"))
        for item in (resolution.get("fulltext_links") or [])
    ):
        return False
    return True


def _write_manual_fetch_report(run: str, refs: dict[str, dict], resolve_map: dict) -> None:
    """Emit clickable DOI/source links for references still lacking full text."""
    def _attempt_value(attempt, key: str):
        if isinstance(attempt, dict):
            return attempt.get(key)
        return getattr(attempt, key, None)

    repo = _repo_open(run)
    if repo is None:
        return
    try:
        # Load manifest once to build fulltext_ref_ids set (avoid O(N²) reloads)
        manifest = _load_manifest_payload(run)
        fulltext_ref_ids = {entry.get("ref_id") for entry in manifest.get("entries", []) if entry.get("tier") == "fulltext"}
        task_state = {}
        list_task_views = getattr(repo, "list_task_views", None)
        if list_task_views is not None:
            for task in list_task_views(slot="fetch", statuses=("pending", "answered", "applied")):
                refs_for_task = [task.get("ref_id")] if task.get("ref_id") else []
                if task.get("kind") == "browser_challenge":
                    refs_for_task.extend(item.get("ref_id") for item in (task.get("answer") or {}).get("items", []))
                    refs_for_task.extend(item.get("ref_id") for item in task.get("references", []))
                terminal = _fetch_task_answer_is_terminal(task)
                for task_ref_id in {ref_id for ref_id in refs_for_task if ref_id}:
                    state = task_state.setdefault(task_ref_id, {"terminal": False, "pending": False})
                    state["terminal"] |= terminal
                    state["pending"] |= not terminal
        frozen_retry_audit = []
        list_lifecycles = getattr(
            repo, "list_frozen_fetch_candidate_lifecycle_summaries", None
        )
        if callable(list_lifecycles):
            for ref_id, ref in sorted(
                refs.items(), key=lambda item: item[1].get("ref_number") or 0
            ):
                for summary in list_lifecycles(ref_id):
                    lifecycle = summary["lifecycle"]
                    if lifecycle == "retry_claimed":
                        frozen_retry_audit.append(
                            (ref, summary, "frozen_retry_claimed_without_completion")
                        )
                    elif lifecycle == "retry_deferred":
                        frozen_retry_audit.append(
                            (
                                ref,
                                summary,
                                f"frozen_retry_deferred:{summary['reason_code']}",
                            )
                        )
                    elif lifecycle == "retry_invalidated":
                        frozen_retry_audit.append(
                            (
                                ref,
                                summary,
                                f"frozen_retry_invalidated:{summary['reason_code']}",
                            )
                        )
        rows = []
        for ref_id, ref in sorted(refs.items(), key=lambda item: item[1].get("ref_number") or 0):
            if ref_id in fulltext_ref_ids:
                continue
            resolution = resolve_map.get(ref_id) or {}
            abstract_entry = next(
                (entry for entry in manifest.get("entries", [])
                 if entry.get("ref_id") == ref_id and entry.get("tier") == "abstract"),
                None,
            )
            attempts = repo.list_fetch_attempts(ref_id)
            if _sources.weak_metadata_abstract_invalidated(ref, resolution, attempts):
                abstract_entry = None
                resolution = {
                    key: value for key, value in resolution.items() if key != "abstract"
                }
            labels, reasons, urls, attempted, deadline_not_attempted = [], [], [], [], []
            for attempt in attempts:
                outcome = _attempt_value(attempt, "outcome")
                reason = _attempt_value(attempt, "reason")
                url = _attempt_value(attempt, "url") or _attempt_value(attempt, "final_url")
                record = (_attempt_value(attempt, "method"), url, outcome, reason)
                target = deadline_not_attempted if outcome == "deadline_exceeded" else attempted
                if record not in target:
                    target.append(record)
                if outcome and outcome not in labels:
                    labels.append(outcome)
                if reason and reason not in reasons:
                    reasons.append(reason)
                for candidate_url in (_attempt_value(attempt, "url"), _attempt_value(attempt, "final_url")):
                    if candidate_url and candidate_url not in urls:
                        urls.append(candidate_url)
            state = task_state.get(ref_id, {})
            rows.append((ref, resolution, abstract_entry, labels, reasons, urls,
                         attempted, deadline_not_attempted, state))
    finally:
        repo.close()
    with_abstract = sum(
        1 for _ref, resolution, abstract_entry, *_rest in rows
        if abstract_entry or resolution.get("abstract")
    )
    without_text = len(rows) - with_abstract
    lines = [
        "# Sources requiring manual retrieval",
        "",
        "Generated after automatic Fetch.",
        "",
        f"References without full text: **{len(rows)}**",
        f"- With an available abstract: **{with_abstract}**",
        f"- With neither full text nor abstract: **{without_text}**",
        f"- fulltext_missing: **{len(rows)}**",
        f"- abstract_available: **{with_abstract}**",
        f"- manual_action_required: **{sum(1 for ref, resolution, *_rest, state in rows if not state.get('terminal') and not _no_external_fulltext_expected(ref, resolution))}**",
        f"- manual_action_available: **{sum(1 for ref, resolution, *_rest, state in rows if not state.get('terminal') and not _no_external_fulltext_expected(ref, resolution))}**",
        "",
    ]
    for ref, resolution, abstract_entry, labels, reasons, urls, attempted, deadline_not_attempted, task_state in rows:
        abstract_dir = os.path.join(run, "user_sources", "abstracts")
        manual_required = (
            not task_state.get("terminal", False)
            and not _no_external_fulltext_expected(ref, resolution)
        )
        lines.extend([f"## Ref. {ref.get('ref_number')} — {ref.get('title') or ref.get('raw_entry')}", "", "- fulltext_missing: **yes**", f"- manual_action_required: **{'yes' if manual_required else 'no'}**", f"- manual_action_available: **{'yes' if manual_required else 'no'}**", f"- Resolver: `{resolution.get('status') or 'unknown'}` via `{resolution.get('via') or 'unknown'}`", f"- Label fetch: {', '.join(f'`{label}`' for label in labels) or '`not_attempted`'}"])
        declared_oa = resolution.get("oa_declared_status") or "unknown"
        observed_access = "not_observed"
        for observed_label, aliases in (
            ("challenge_blocked", {"challenge_blocked"}),
            ("paywalled", {"paywall", "paywalled"}),
            ("access_denied", {"access_denied"}),
            ("abstract_only", {"abstract_fallback", "abstract_only"}),
            ("landing_only", {"landing_no_store", "landing_page", "metadata_shell"}),
            ("identity_inconclusive", {"identity_inconclusive"}),
            ("not_found", {"not_found"}),
        ):
            if any(label in aliases for label in labels):
                observed_access = observed_label
                break
        lines.append(f"- Declared OA status (metadata): `{declared_oa}`")
        lines.append(f"- Observed access (Fetch): `{observed_access}`")
        if abstract_entry:
            lines.append("- abstract_available: **yes**")
            abstract_origin = abstract_entry.get("origin") or resolution.get("abstract_via") or "user"
            lines.append(f"- Materialized abstract (origin: `{abstract_origin}`); path: `{abstract_dir}`")
        elif resolution.get("abstract"):
            lines.append("- abstract_available: **yes**")
            lines.append(f"- Abstract available in metadata (origin: `{resolution.get('abstract_via') or 'resolver metadata'}`, not materialized); path: `{abstract_dir}`")
        else:
            lines.append("- abstract_available: **no**")
            lines.append(f"- Abstract missing or not indexed; path: `{abstract_dir}`")
        reason = resolution.get("reason") or "; ".join(reasons)
        lines.append(f"- Reason: {reason or 'no reason recorded'}")
        dois = _manual_fetch_dois(ref, resolution)
        lines.append("- DOI: " + ", ".join(f"[{doi}](https://doi.org/{urllib.parse.quote(doi, safe='')})" for doi in dois) if dois else "- DOI: absent from the available metadata")
        lines.append(f"- Attempts: {len(attempted)} made; {len(deadline_not_attempted)} skipped because of the deadline")
        for method, url, outcome, attempt_reason in attempted + deadline_not_attempted:
            detail = ", ".join(part for part in (method, outcome, attempt_reason) if part)
            lines.append(f"  - {detail or 'recorded attempt'}" + (f": <{url}>" if url else ""))
        if urls:
            lines.append("- Candidate URLs:")
            lines.extend(f"  - <{url}>" for url in urls)
        lines.append("")
    if frozen_retry_audit:
        lines.extend(["## Frozen retry audit", ""])
        for ref, summary, state in frozen_retry_audit:
            lines.append(
                f"- Ref. {ref.get('ref_number')} — `{state}` "
                f"(candidate {summary['candidate_id']}, stage `{summary['stage']}`)"
            )
        lines.append("")
    with open(os.path.join(run, "fetch_manual_sources.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _browser_challenge_answer_is_valid(task: dict) -> bool:
    """Return whether a browser task answer accounts for its declared group.

    Browser challenges are group tasks: accepting a partial or misaddressed
    result would let unhandled references disappear from the fetch queue.
    Keep the accepted shape deliberately closed so the same predicate can gate
    both task completion and source ingestion.
    """
    answer = (task or {}).get("answer")
    if not isinstance(answer, dict) or set(answer) != {"items"}:
        return False
    items = answer.get("items")
    references = (task or {}).get("references")
    if not isinstance(items, list) or not isinstance(references, list):
        return False

    ref_ids = []
    for reference in references:
        if not isinstance(reference, dict):
            return False
        ref_id = reference.get("ref_id")
        if not isinstance(ref_id, str) or not ref_id.strip() or ref_id in ref_ids:
            return False
        ref_ids.append(ref_id)
    if not ref_ids or len(items) != len(ref_ids):
        return False

    allowed_fields = {
        "ref_id", "found", "file_path", "text", "url", "source_tier",
        "disposition", "guided_fetch", "identity_attested",
    }
    for expected_ref_id, item in zip(ref_ids, items):
        if not isinstance(item, dict) or set(item) - allowed_fields:
            return False
        if item.get("ref_id") != expected_ref_id or not isinstance(item.get("found"), bool):
            return False
        if item.get("source_tier", "fulltext") not in {"fulltext", "abstract"}:
            return False
        if item.get("disposition", "provided" if item["found"] else "not_found") not in {
            "provided", "not_found", "user_waived",
        }:
            return False
        if "guided_fetch" in item and type(item["guided_fetch"]) is not bool:
            return False
        guided = item.get("guided_fetch", False)
        identity_attested = item.get("identity_attested", False)
        if (
            type(identity_attested) is not bool
            or identity_attested != bool(guided and item["found"])
        ):
            return False
        disposition = item.get(
            "disposition", "provided" if item["found"] else "not_found"
        )
        if (disposition == "user_waived") != (guided and not item["found"]):
            return False
        if "url" in item and (not isinstance(item["url"], str) or not item["url"].strip()):
            return False

        has_file_path = "file_path" in item
        has_text = "text" in item
        if item["found"] is False:
            if (has_file_path or has_text or item.get("source_tier", "fulltext") != "fulltext"
                    or item.get("disposition", "not_found") == "provided"):
                return False
            continue
        if item.get("disposition", "provided") != "provided":
            return False
        if has_file_path == has_text:
            return False
        source_field = "file_path" if has_file_path else "text"
        if not isinstance(item[source_field], str) or not item[source_field].strip():
            return False
    return True


def _fetch_task_answer_is_terminal(task: dict) -> bool:
    """True when a fetch task has an explicit terminal answer.

    A task counts as complete only when it either supplied source text
    (`file_path`/`text`/browser-challenge items/`proceed=true` for OCR) or it
    explicitly recorded that nothing was found (`found=false` / `proceed=false`).
    Missing answers must keep the run in the fetch phase.
    """
    answer = (task or {}).get("answer") or {}
    kind = (task or {}).get("kind")
    if kind == "browser_challenge":
        return _browser_challenge_answer_is_valid(task)
    if kind == "ocr":
        return "proceed" in answer or answer.get("found") is False
    if answer.get("found") is False:
        return True
    return bool(answer.get("file_path") or answer.get("text"))


def _fetch_resume_pending_tasks(run_dir: str) -> list[tuple[str, dict]]:
    """Fetch tasks that still require an explicit answer before fetch can finish."""
    pending = [
        (handle, task)
        for handle, task in _pending_tasks(run_dir, "fetch")
        if task.get("kind") in {"fetch", "browser_challenge", "ocr"}
        and not _fetch_task_answer_is_terminal(task)
    ]
    repo = _repo_open(run_dir)
    if repo is None:
        return pending
    try:
        auto_completed, _completion_counts = _validated_fetch_auto_completion_payloads(
            repo.list_integrity_unit_completions("fetch_auto")
        )
        sources_by_ref: dict[str, list] = {}
        for _handle, task in pending:
            ref_id = task.get("ref_id")
            if isinstance(ref_id, str) and ref_id and ref_id not in sources_by_ref:
                sources_by_ref[ref_id] = repo.list_source_texts(ref_id)
        waived_ref_ids: set[str] = set()
        for _handle, task in pending:
            # Only an untouched ordinary fetch task can be discharged by the
            # provenance-bound retry.  Malformed/non-terminal answers remain
            # blocking and browser/OCR tasks keep their prior semantics.
            ref_id = task.get("ref_id")
            if (
                task.get("kind") != "fetch"
                or task.get("answer") is not None
                or not isinstance(ref_id, str)
                or not ref_id
            ):
                continue
            auto_result = auto_completed.get(ref_id) or {}
            if (
                auto_result.get("status") in {"stored", "already_stored"}
                and _stored_fetch_auto_payload_is_current(auto_result, sources_by_ref)
            ):
                waived_ref_ids.add(ref_id)
                continue
            summaries = repo.list_frozen_fetch_candidate_lifecycle_summaries(ref_id)
            attempt_outcomes = {
                row["fetch_attempt_id"]: row["outcome"]
                for row in repo.list_fetch_attempts(ref_id)
            }
            has_ambiguous_or_invalidated_retry = any(
                summary["lifecycle"] == "retry_claimed"
                or (
                    summary["lifecycle"] == "retry_invalidated"
                    and summary["reason_code"] != "fulltext_present"
                )
                for summary in summaries
            )
            has_audited_fulltext_resolution = any(
                (
                    summary["lifecycle"] == "retry_completed"
                    and attempt_outcomes.get(summary["fetch_attempt_id"]) == "stored"
                )
                or (
                    summary["lifecycle"] == "retry_invalidated"
                    and summary["reason_code"] == "fulltext_present"
                )
                for summary in summaries
            )
            if (
                not has_ambiguous_or_invalidated_retry
                and has_audited_fulltext_resolution
            ):
                waived_ref_ids.add(ref_id)
        return [
            (handle, task)
            for handle, task in pending
            if not (
                task.get("kind") == "fetch"
                and task.get("answer") is None
                and task.get("ref_id") in waived_ref_ids
            )
        ]
    finally:
        repo.close()


def _ingest_user_source_directories(run: str, refs: dict[str, dict], resolve_map: dict) -> dict:
    """Best-effort ingest of files dropped in the user-facing tier folders."""
    try:
        dirs = _user_sources.ensure_user_source_dirs(run)
    except OSError:
        return {"fulltext": [], "abstract": []}
    identity_refs = {
        ref_id: _user_sources.identity_view(ref, resolve_map.get(ref_id))
        for ref_id, ref in refs.items()
    }
    audit = {"fulltext": [], "abstract": []}
    try:
        audit["fulltext"] = _provided_fulltext.ingest_directory(
            run, refs, resolve_map, dirs["fulltext"]
        )
    except Exception as exc:
        audit["fulltext"] = [
            {"outcome": "unreadable", "error": f"{type(exc).__name__}: {exc}"}
        ]
    try:
        audit["abstract"] = _user_sources.ingest_directory(
            run, identity_refs, dirs["abstract"], tier="abstract"
        )
    except Exception as exc:
        audit["abstract"] = [
            {"outcome": "unreadable", "error": f"{type(exc).__name__}: {exc}"}
        ]
    repo = _repo_open(run)
    if repo is None:
        raise RuntimeError("user source ingest audit repository is unavailable")
    try:
        repo.set_run_setting("user_source_ingest", audit)
    finally:
        repo.close()
    counts = {"accepted_fulltext": 0, "accepted_abstract": 0, "duplicate": 0,
              "review_rejected": 0}
    for result in audit["fulltext"] + audit["abstract"]:
        outcome = result.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
        elif outcome:
            counts["review_rejected"] += 1
    if any(audit.values()):
        _progress(
            "user sources: "
            f"accepted fulltext/abstract {counts['accepted_fulltext']}/{counts['accepted_abstract']}, "
            f"duplicates {counts['duplicate']}, review/rejected {counts['review_rejected']}"
        )
        for tier in ("fulltext", "abstract"):
            for result in audit[tier]:
                outcome = result.get("outcome") or "unknown"
                if outcome.startswith("accepted_") or outcome == "duplicate":
                    continue
                ref = result.get("ref_number") or result.get("ref_id") or "-"
                detail = result.get("reason") or result.get("error")
                if (
                    not detail
                    and tier == "fulltext"
                    and outcome == "needs_manual_confirmation"
                ):
                    detail = f"add an explicit mapping to {_provided_fulltext.INDEX_FILENAME}"
                suffix = f" · {detail}" if detail else ""
                _progress(
                    f"user source audit: {tier} {os.path.basename(result.get('file') or '-') }"
                    f" · {outcome} · ref {ref}{suffix}"
                )
    return audit


def _fetch_trace_ref_ids(run_dir: str) -> set[str]:
    """References with any recorded resolve/fetch/source evidence."""
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        traced = {row.ref_id for row in repo.list_resolve_results()}
        with repo._connection_lock:
            for ref in repo._conn.execute(
                "SELECT ref_id FROM operational_references ORDER BY ref_number"
            ):
                if repo.list_fetch_attempts(ref["ref_id"]):
                    traced.add(ref["ref_id"])
        for row in repo.list_source_texts():
            traced.add(row.ref_id)
        for task in repo.list_task_views(slot="fetch", statuses=("pending", "answered", "applied")):
            if not _fetch_task_answer_is_terminal(task):
                continue
            answer = task.get("answer") or {}
            ref_id = task.get("ref_id")
            if ref_id:
                traced.add(ref_id)
            if task.get("kind") == "browser_challenge":
                for item in answer.get("items") or []:
                    if item.get("ref_id"):
                        traced.add(item["ref_id"])
        return traced
    finally:
        repo.close()


def _blocking_fetch_refs_without_trace(st: dict, refs: dict[str, dict]) -> list[dict]:
    """Blocking refs that still have no evidence of being checked at all."""
    run = st["run_dir"]
    try:
        from core.fetch.diagnostics import gaps as _gaps
    except ImportError:
        import gaps as _gaps
    gaps = _gaps.build_gap_report(run, accuracy=st["accuracy"])
    resolve_map = _resolve_map(run)
    parse = _load_parse_payload(run)
    need = gaps.get("need_fulltext") or gaps.get("missing") or gaps.get("gaps") or []
    need = _include_table_only_need(need, parse, refs, run)
    need = _fetchable_need_items(need, resolve_map)
    need = _blocking_fetch_need_items(need, st["accuracy"], refs=refs)
    traced = _fetch_trace_ref_ids(run)
    missing = []
    for item in need:
        ref_id = item.get("ref_id") if isinstance(item, dict) else item
        if ref_id in traced:
            continue
        ref = refs.get(ref_id) or {}
        missing.append({
            "ref_id": ref_id,
            "ref_number": ref.get("ref_number"),
            "title": ref.get("title"),
        })
    return missing


def _frozen_replay_attempt_without_request(repo, ref_id, attempt_id):
    """Return the persisted no-I/O trace facts for one replay attempt, if any."""
    for attempt in repo.list_fetch_attempts(ref_id):
        if attempt.get("fetch_attempt_id") != attempt_id:
            continue
        trace = attempt.get("trace") or {}
        if trace.get("request") != "none":
            return None
        reason = trace.get("reason")
        return {
            "outcome": trace.get("outcome"),
            "reason_code": trace.get("reason_code") or "request_not_issued",
            "reason_detail": (
                reason
                if isinstance(reason, str) and reason
                else "frozen candidate replay did not issue a request"
            ),
        }
    raise RuntimeError("claimed frozen candidate persisted attempt is unavailable")


def _resume_one_frozen_fetch_candidate(st, refs, resolve_map):
    """Claim and replay one globally eligible deadline candidate, fail closed."""
    repo = _repo_open(st["run_dir"])
    if repo is None:
        return None
    candidate_id = None
    try:
        eligible = repo.list_eligible_frozen_fetch_candidates()
        if not eligible:
            return None
        candidate_id = eligible[0]
        record = repo.get_frozen_fetch_candidate_record(candidate_id)
        ref_id = record["ref_id"]
        ref, resolved = refs.get(ref_id), resolve_map.get(ref_id)
        if not ref or not resolved:
            repo.record_frozen_fetch_candidate_event(
                candidate_id,
                "retry_invalidated",
                reason_code="missing_context",
                reason_detail="reference or resolution unavailable",
            )
            return None
        evidence = _fetch_evidence_payload(resolved)
        context = _fetch._frozen_fetch_context(ref, resolved, evidence)
        from core.infra.db.fetch_candidates import fingerprints

        context_fp, plan_fp = fingerprints(ref_id, context)
        if (
            context_fp != record["context_fingerprint"]
            or plan_fp != record["plan_fingerprint"]
        ):
            repo.record_frozen_fetch_candidate_event(
                candidate_id,
                "retry_invalidated",
                reason_code="context_mismatch",
                reason_detail="frozen fetch context no longer matches",
            )
            return None
        if any(row.tier == "fulltext" for row in repo.list_source_texts(ref_id)):
            repo.record_frozen_fetch_candidate_event(
                candidate_id,
                "retry_invalidated",
                reason_code="fulltext_present",
                reason_detail="full text already materialized",
            )
            return None
        if not repo.claim_frozen_fetch_candidate(candidate_id):
            return None
    except Exception:
        if candidate_id is not None:
            try:
                repo.record_frozen_fetch_candidate_event(
                    candidate_id,
                    "retry_invalidated",
                    reason_code="record_invalid",
                    reason_detail="frozen record could not be validated",
                )
            except Exception:
                pass
        return None
    finally:
        repo.close()
    try:
        # A claim is retried only when its persisted trace proves host cooldown
        # prevented the request from being issued.
        fetched = _fetch._fetch_frozen_candidate(
            ref,
            st["run_dir"],
            resolved,
            record["candidate"],
            candidate_id,
            mailto=st.get("mailto"),
            fetch_context=_fetch.new_fetch_run_context(),
            evidence_payload=evidence,
        )
        mapping = _record_fetch_attempts(st["run_dir"], ref_id, fetched, resolved)
        attempt_id = mapping.get(candidate_id)
        if attempt_id is None:
            raise RuntimeError("claimed frozen candidate produced no persisted attempt")
        repo = _repo_open(st["run_dir"])
        if repo is None:
            raise RuntimeError("frozen completion repository unavailable")
        try:
            deferred = _frozen_replay_attempt_without_request(
                repo, ref_id, attempt_id
            )
            if deferred is not None:
                if (
                    fetched.get("status") == "rate_limit_deferred"
                    and deferred["outcome"] == "rate_limit_deferred"
                    and deferred["reason_code"] == "host_cooldown"
                ):
                    repo.record_frozen_fetch_candidate_event(
                        candidate_id,
                        "retry_deferred",
                        fetch_attempt_id=attempt_id,
                        reason_code="host_cooldown",
                        reason_detail=deferred["reason_detail"],
                    )
                    _progress(
                        "frozen fetch candidate "
                        f"[{ref.get('ref_number')}] deferred before issuing a request"
                    )
                    return {
                        "status": "rate_limit_deferred",
                        "candidate_id": candidate_id,
                        "ref_id": ref_id,
                        "fetched": fetched,
                    }
                repo.record_frozen_fetch_candidate_event(
                    candidate_id,
                    "retry_invalidated",
                    fetch_attempt_id=attempt_id,
                    reason_code="replay_request_not_issued",
                    reason_detail=deferred["reason_detail"],
                )
                return {
                    "status": "invalidated",
                    "candidate_id": candidate_id,
                    "ref_id": ref_id,
                    "fetched": fetched,
                }
            repo.record_frozen_fetch_candidate_event(
                candidate_id,
                "retry_completed",
                fetch_attempt_id=attempt_id,
            )
        finally:
            repo.close()
        _progress(f"resumed frozen fetch candidate for reference [{ref.get('ref_number')}]")
        return {
            "status": "completed",
            "candidate_id": candidate_id,
            "ref_id": ref_id,
            "fetched": fetched,
        }
    except Exception as exc:
        _progress(
            f"frozen fetch candidate [{ref.get('ref_number')}] remains claimed: "
            f"{type(exc).__name__}"
        )
        return {
            "status": "ambiguous_claim",
            "candidate_id": candidate_id,
            "ref_id": ref_id,
            "error": type(exc).__name__,
        }


def _source_identity_attestation_task_id(source_text_id: str) -> str:
    digest = hashlib.sha256(source_text_id.encode("utf-8")).hexdigest()
    return f"verify-identity:{digest[:24]}"


def _read_fetch_identity_source(repository, run: str, source_text_id: str, sha256: str) -> str:
    """Read the exact ledger-bound source that Verify would later consume."""
    source = repository.get_source_text(source_text_id)
    if source is None or source.sha256 != sha256:
        raise RuntimeError("source identity ledger binding is unavailable")
    stored_path = source.stored_path
    if not isinstance(stored_path, str) or not stored_path:
        raise RuntimeError("source identity persisted path is not run-local")
    path = PurePosixPath(stored_path)
    if (
        "\\" in stored_path
        or path.is_absolute() or path.parts[:1] != ("sources",)
        or ".." in path.parts or PureWindowsPath(stored_path).is_absolute()
    ):
        raise RuntimeError("source identity persisted path is not run-local")
    root = Path(run).resolve()
    try:
        source_path = (root / Path(*path.parts)).resolve(strict=True)
        source_path.relative_to(root)
        data = source_path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("source identity persisted source is unavailable") from exc
    if hashlib.sha256(data).hexdigest() != sha256 or len(text) != source.char_count:
        raise RuntimeError("source identity persisted source does not match its ledger")
    return text


def _emit_fetch_identity_tasks(st: dict, parse: dict) -> int:
    """Queue only admissible operator reviews for selected full-text sources.

    This mirrors Verify's source selection and gates the immutable source/ref
    target before Fetch completes.  Verify deliberately repeats the check.
    """
    run = st["run_dir"]
    refs = {ref.get("id"): ref for ref in parse.get("references", []) if ref.get("id")}
    manifest = _load_manifest_payload(run)
    owner_by_ref = cross_reference_map(parse.get("references", []))
    owner_ids: set[str] = set()
    for citation in parse.get("citations", []):
        ref_id = citation.get("ref_id") if isinstance(citation, dict) else None
        if not isinstance(ref_id, str) or not ref_id:
            continue
        owner_ids.add(owner_by_ref.get(ref_id, (ref_id, None))[0])

    if not owner_ids:
        return 0

    repository = _repo_open(run)
    if repository is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    created = 0
    try:
        for owner_ref_id in sorted(owner_ids):
            reference = refs.get(owner_ref_id)
            entry = _sources.best_for(manifest, owner_ref_id)
            if reference is None or not isinstance(entry, dict) or entry.get("tier") != "fulltext":
                continue
            source_text_id = entry.get("source_text_id")
            source_sha256 = entry.get("sha256")
            if not isinstance(source_text_id, str) or not source_text_id or not isinstance(source_sha256, str):
                record_identity_anomaly(
                    repository,
                    owner_ref_id,
                    phase="fetch",
                    stage="identity_target",
                    error=RuntimeError(
                        "source-identity attestation target is unavailable"
                    ),
                )
                continue
            try:
                target = repository.source_identity_attestation_target(
                    ref_id=owner_ref_id, source_text_id=source_text_id,
                )
            except (RuntimeError, ValueError) as exc:
                record_identity_anomaly(
                    repository,
                    owner_ref_id,
                    phase="fetch",
                    stage="identity_target",
                    error=exc,
                )
                continue
            try:
                source_text = _read_fetch_identity_source(
                    repository, run, source_text_id, target["source_text_sha256"],
                )
            except RuntimeError as exc:
                record_identity_anomaly(
                    repository,
                    owner_ref_id,
                    phase="fetch",
                    stage="source_context",
                    error=exc,
                )
                continue
            if target["source_text_sha256"] != source_sha256:
                record_identity_anomaly(
                    repository,
                    owner_ref_id,
                    phase="fetch",
                    stage="identity_target_binding",
                    error=RuntimeError(
                        "source-identity attestation source binding changed"
                    ),
                )
                continue
            identity_task = {
                "reference": target["reference"],
                "source_identity_evidence": {
                    **target["source_identity"], "resolve": target["resolve_identity"],
                },
            }
            try:
                identity_admitted = bibliographic_identity_admitted(
                    identity_task, source_text,
                )
            except Exception as exc:
                record_identity_anomaly(
                    repository,
                    owner_ref_id,
                    phase="fetch",
                    stage="identity_admission",
                    error=exc,
                )
                # The anomaly is not a negative identity verdict. Verify repeats
                # the check and decides later whether this source can proceed.
                continue
            if identity_admitted:
                continue
            task_id = _source_identity_attestation_task_id(source_text_id)
            existing = repository.get_task(task_id)
            if existing is not None:
                # The same stable id prevents a later Verify pass from creating
                # a second review for this exact source.
                from core.app.phases.verify import (
                    _require_identity_attestation_task,
                )
                _require_identity_attestation_task(existing, target)
                continue
            try:
                block_reason = source_identity_attestation_block_reason(
                    identity_task, source_text,
                )
            except Exception as exc:
                record_identity_anomaly(
                    repository,
                    owner_ref_id,
                    phase="fetch",
                    stage="identity_attestation_eligibility",
                    error=exc,
                )
                continue
            if block_reason is not None:
                # A hard conflict remains fail-closed; Verify records its
                # nonsemantic terminal rather than asking an operator to waive it.
                continue
            repository.create_source_identity_attestation(
                task_id=task_id,
                ref_id=owner_ref_id,
                source_text_id=source_text_id,
                slot="fetch",
                instructions=(
                    "Confirm whether this exact source text is the cited work. "
                    "Answer attest_identity or keep_unverified, binding the answer "
                    "to target_sha256 and giving a reason."
                ),
            )
            created += 1
    finally:
        repository.close()
    return created


def _pending_fetch_identity_attestations(run: str) -> int:
    repository = _repo_open(run)
    if repository is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        return sum(
            task.task_kind == "source_identity_attestation"
            and task.status == "pending"
            for task in repository.list_tasks(slot="fetch")
        )
    finally:
        repository.close()


def phase_fetch(st):
    """If the accuracy grade wants full text and some required sources have none,
    pause ONCE and let the agent fetch them with its web tools. Best-effort: on
    resume the driver proceeds with whatever was provided (the rest drop a tier or
    become honestly uncheckable 'no_text')."""
    # Consume process-local state before any operation that can raise.  It must
    # never survive a failed/resumed invocation through the persisted run state.
    resolve_fetch_context = st.pop("_fetch_run_context", None)
    _progress("fetching full texts (OA repositories, publisher sites)")
    phase_started_at = _now()
    run = st["run_dir"]
    _sources.reconcile_materialized_texts(run)

    def finish(result):
        _print_phase_credential_warnings(
            run,
            "fetch",
            since=phase_started_at,
        )
        return result

    parse = _load_parse_payload(run)
    refs = {r["id"]: r for r in parse.get("references", [])}
    resolve_map = _resolve_map(run)
    _ingest_user_source_directories(run, refs, resolve_map)
    _materialize_resolve_abstracts(run)
    if st.get("no_fetch"):
        # --skip-fetch waives retrieval and ordinary Fetch tasks only.  It must
        # not waive the independent source/ref identity decision for text that
        # is already materialized in the run.
        if parse.get("citations"):
            from core.app.phases.verify import _apply_answered_source_identity_attestations
            _apply_answered_source_identity_attestations(run)
            _emit_fetch_identity_tasks(st, parse)
            pending_identity = _pending_fetch_identity_attestations(run)
            if pending_identity:
                return finish(_pause(
                    "fetch",
                    run,
                    pending_identity,
                    "Fetch source identity requires an operator decision. Inspect each "
                    "task and answer attest_identity or keep_unverified; skip-fetch and "
                    "guided Proceed do not waive identity reviews.",
                ))
        return finish("gaps")
    if st["accuracy"] == "abstract":
        return finish("gaps")
    if st.get("fetch_paused"):
        # Answers are applied before checking whether the same source still
        # requires a Fetch-stage identity decision.
        if parse.get("citations"):
            from core.app.phases.verify import _apply_answered_source_identity_attestations
            _apply_answered_source_identity_attestations(run)
        try:
            from core.fetch.diagnostics import gaps as _gaps
        except ImportError:
            import gaps as _gaps
        gaps = _gaps.build_gap_report(run, accuracy=st["accuracy"])
        need = gaps.get("need_fulltext") or gaps.get("missing") or gaps.get("gaps") or []
        need = _include_table_only_need(need, parse, refs, run)
        resolve_map = _resolve_map(run)
        need = _fetchable_need_items(need, resolve_map)
        _auto_fetch_fulltexts(
            st, need, refs, resolve_fetch_context=resolve_fetch_context,
        )
        _resume_one_frozen_fetch_candidate(st, refs, resolve_map)
        _print_automatic_fetch_summary(run, refs, _resolve_map(run))
        unanswered = _fetch_resume_pending_tasks(run)
        if unanswered:
            _write_manual_fetch_report(run, refs, resolve_map)
            return finish(_pause(
                "fetch",
                run,
                len(unanswered),
                "Fetch is still incomplete: answer every pending FETCH task with "
                "{file_path|text, found=false} before verify can start.",
            ))
        unchecked = _blocking_fetch_refs_without_trace(st, refs)
        if unchecked:
            details = ", ".join(
                f"[{row.get('ref_number')}] {row.get('title') or row.get('ref_id')}"
                for row in unchecked[:5]
            )
            if len(unchecked) > 5:
                details += f", +{len(unchecked) - 5} more"
            _write_manual_fetch_report(run, refs, resolve_map)
            print(
                "Fetch incomplete: some blocking references have no resolve/fetch/source "
                f"trace at all ({details}). Staying in FETCH instead of entering verify.",
                file=sys.stderr,
            )
            return finish(_pause(
                "fetch",
                run,
                len(unchecked),
                "Fetch is still incomplete: some blocking references were never checked. "
                "Resolve or answer the FETCH tasks before verify can start.",
            ))
        resolve_map = _resolve_map(run)
        _write_manual_fetch_report(run, refs, resolve_map)
        pending_identity = 0
        if parse.get("citations"):
            _emit_fetch_identity_tasks(st, parse)
            pending_identity = _pending_fetch_identity_attestations(run)
        if pending_identity:
            return finish(_pause(
                "fetch",
                run,
                pending_identity,
                "Fetch source identity requires an operator decision. Inspect each "
                "task and answer attest_identity or keep_unverified; skip-fetch and "
                "guided Proceed do not waive identity reviews.",
            ))
        return finish("gaps")

    try:
        from core.fetch.diagnostics import gaps as _gaps
    except ImportError:
        import gaps as _gaps
    seeded = _seed_reusable_sources(run, refs, state=st)
    if seeded["fulltext"] or seeded["abstract"]:
        _progress(
            "reused cached sources from content store: "
            f"{seeded['fulltext']} full text, {seeded['abstract']} abstract"
        )
    gaps = _gaps.build_gap_report(run, accuracy=st["accuracy"])
    if gaps.get("network_blocked"):
        print("Network blocked and nothing usable retrieved — cannot proceed offline. "
              "Answer the generated FETCH tasks through run.py tasks, then --resume.",
              file=sys.stderr)
        # Not fatal to the state machine: still emit tasks below so the agent can act.
    need = gaps.get("need_fulltext") or gaps.get("missing") or gaps.get("gaps") or []
    need = _include_table_only_need(need, parse, refs, run)
    resolve_map = _resolve_map(run)
    need = _fetchable_need_items(need, resolve_map)
    fetch_results = _auto_fetch_fulltexts(
        st, need, refs, resolve_fetch_context=resolve_fetch_context,
    )
    _resume_one_frozen_fetch_candidate(st, refs, resolve_map)
    ocr_result = _auto_run_source_ocr(st, refs, accuracy=st["accuracy"])
    # Generic third-party web search is not source-attributed evidence and is
    # therefore not part of Fetch or Verify.
    _print_automatic_fetch_summary(run, refs, _resolve_map(run))
    gaps = _gaps.build_gap_report(run, accuracy=st["accuracy"])
    need = gaps.get("need_fulltext") or gaps.get("missing") or gaps.get("gaps") or []
    need = _include_table_only_need(need, parse, refs, run)
    resolve_map = _resolve_map(run)
    need = _fetchable_need_items(need, resolve_map)
    need = _blocking_fetch_need_items(need, st["accuracy"], refs=refs)
    challenge_groups, challenge_ref_ids = ([], set())
    selected_mode = _fetch_modes.selected_module(
        {ENV_CHALLENGE_MODE: st.get("challenge_mode") or DEFAULT_CHALLENGE_MODE}
    )
    selected_mode_name = getattr(selected_mode, "MODE_NAME", None)
    if selected_mode is not None and hasattr(selected_mode, "groups"):
        group_kwargs = (
            {"resolve_map": resolve_map}
            if (
                getattr(selected_mode, "USES_RESOLVE_MAP", False)
                or getattr(selected_mode, "CLOSED_ACCESS_MODE", False)
            )
            else {}
        )
        challenge_groups, challenge_ref_ids = selected_mode.groups(
            need, refs, fetch_results, **group_kwargs
        )
    # ``interactive`` supplies browser-captured responses to the same HTML/PDF
    # pipeline used by deterministic fetches. Anything that does not produce a
    # real manifest fulltext (including an abstract or metadata shell) remains a
    # normal queue task.
    if challenge_groups and selected_mode is not None and hasattr(selected_mode, "recover"):
        interactive = selected_mode.recover(challenge_groups, run)
        if not interactive.get("available"):
            _progress(f"{selected_mode_name or 'interactive'} browser unavailable; "
                      "falling back to browser queue: "
                      f"{interactive.get('reason') or 'unknown reason'}")
        else:
            responses_by_ref = {}
            for item in interactive.get("items") or []:
                ref_id = item.get("ref_id")
                if not ref_id or item.get("found") is False or not item.get("body"):
                    continue
                responses_by_ref.setdefault(ref_id, []).append(item)
            for ref_id, responses in responses_by_ref.items():
                ref = refs.get(ref_id)
                resolve_result = resolve_map.get(ref_id) or {}
                if ref is None:
                    continue
                try:
                    result = _fetch.process_browser_responses(
                        ref,
                        run,
                        resolve_result,
                        responses,
                    )
                except Exception as exc:
                    _progress(f"interactive browser response for {ref_id} failed in "
                              f"the fetch pipeline; queuing it: {type(exc).__name__}: {exc}")
                    continue
                _record_fetch_attempts(run, ref_id, result, resolve_result)
                _progress(f"interactive browser result for {ref_id}: "
                          f"{result.get('outcome') or result.get('status')}")
            pending_interactive = set(challenge_ref_ids) - _fulltext_ref_ids(run)
            if pending_interactive != challenge_ref_ids:
                filtered_groups = []
                for group in challenge_groups:
                    remaining = [r for r in group["references"]
                                 if r.get("ref_id") in pending_interactive]
                    if not remaining:
                        continue
                    urls = []
                    for reference in remaining:
                        for url in reference.get("candidate_urls") or []:
                            if url not in urls:
                                urls.append(url)
                    filtered_groups.append({
                        **group,
                        "references": remaining,
                        "candidate_urls": urls,
                    })
                challenge_groups = filtered_groups
                challenge_ref_ids = pending_interactive
    emitted = 0
    for group in challenge_groups:
        requires_access = bool(group.get("requires_legitimate_access"))
        instructions = (
            "Open one visible, isolated browser session for this domain and sign in only "
            "with the user's legitimate publisher or institutional access. In that SAME "
            "browser session, visit the listed URLs and retrieve the source text for as "
            "many references as you can. For each recovered source, save a .txt and use "
            "`run.py tasks answer-fetch` with --item-file REF_ID=PATH (or "
            "--item-text-file REF_ID=PATH_TO_TEXT). Use --item-not-found REF_ID "
            "when no usable full text "
            "is reachable. Never bypass access controls or invent text; record only what "
            "that authenticated browser session actually exposed."
            if requires_access else
            "Open one visible browser session for this domain. Let the user clear the "
            "publisher security challenge if one appears. In that SAME browser session, "
            "visit the listed URLs and retrieve the source text for as many references as "
            "you can. For each recovered source, save a .txt and use `run.py tasks "
            "answer-fetch` with --item-file REF_ID=PATH (or --item-text-file "
            "REF_ID=PATH_TO_TEXT). Use --item-not-found REF_ID when the challenge "
            "clears but no usable full text "
            "is reachable. Never invent text; record only what the browser session "
            "actually exposed after the user interaction."
        )
        task = {
            "kind": "browser_challenge",
            "status": "pending",
            "domain": group["domain"],
            "references": group["references"],
            "candidate_urls": group["candidate_urls"],
            "instructions": instructions,
            "answer": None,
        }
        _create_task(run, "fetch", f"challenge:{group['domain']}", task)
        emitted += 1
    for item in need:
        ref_id = item.get("ref_id") if isinstance(item, dict) else item
        if ref_id in challenge_ref_ids:
            continue
        ref = refs.get(ref_id)
        if not ref:
            continue
        evidence = resolve_map.get(ref_id, {})
        task = {
            "kind": "fetch", "status": "pending", "ref_id": ref_id,
            "ref_number": ref.get("ref_number"),
            "reference": {k: ref.get(k) for k in
                          ("ref_number", "raw_entry", "title", "doi", "pmid", "url",
                           "isbn", "year", "source_type", "source_kind", "indexability",
                           "source_type_confidence")},
            "source_identity": {
                "reference_status_tag": evidence.get("reference_status_tag"),
                "fabrication_risk": evidence.get("fabrication_risk"),
                "tag_reason": evidence.get("tag_reason"),
                "matched_title": evidence.get("matched_title"),
                "metadata_match": (evidence.get("evidence_profile") or {}).get("metadata_match"),
            },
            "instructions": (
                "Use your web tools to retrieve the FULL TEXT of this source (open-access "
                "PDF, publisher HTML, or author copy). Save it as a .txt, then submit it "
                "with `run.py tasks answer-fetch --run <run> --task <task_id> "
                "--file-path <path>`. If you truly cannot find it, use --not-found "
                "(the source will drop a tier or be marked "
                "uncheckable -- never invent text). If source_identity is weak or uncertain, "
                "retrieve only traceable candidate text and do not rewrite the citation."),
            "answer": None,
        }
        _create_task(run, "fetch", f"fetch:{ref_id}", task)
        emitted += 1
    # At the END of the fetch queue: scanned source PDFs (provided or downloaded) that
    # have no text layer. OCR is slow, so it is offered as an explicit opt-in per scan
    # rather than run automatically — the user answers proceed=true to spend the time.
    _write_manual_fetch_report(run, refs, resolve_map)
    # Identity review remains a distinct, hash-bound task, but it belongs in the
    # same Fetch checkpoint as any manual retrieval work.  Otherwise an operator
    # can finish every retrieval task and only then discover a second required
    # Fetch decision for text that was already selected.
    pending_identity = 0
    if parse.get("citations"):
        _emit_fetch_identity_tasks(st, parse)
        pending_identity = _pending_fetch_identity_attestations(run)
    if emitted == 0:
        if pending_identity:
            st["fetch_paused"] = True
            _save_state(st)
            return finish(_pause(
                "fetch",
                run,
                pending_identity,
                "Fetch source identity requires an operator decision. Inspect each "
                "task and answer attest_identity or keep_unverified; skip-fetch and "
                "guided Proceed do not waive identity reviews.",
            ))
        return finish("gaps")
    st["fetch_paused"] = True
    _save_state(st)
    return finish(_pause(
        "fetch",
        run,
        emitted + pending_identity,
        "For retrieval tasks: inspect with `run.py tasks show`; submit with "
        "`run.py tasks answer-fetch`, using only text retrieved from a real source "
        "or record not found. For source-identity tasks: answer attest_identity or "
        "keep_unverified for the exact hash-bound source; skip-fetch does not waive "
        "identity review.",
    ))


def _fulltext_ref_ids(run_dir: str) -> set:
    """Return the set of ref_ids that already have a stored fulltext source."""
    manifest = _load_manifest_payload(run_dir)
    ids = set()
    for entry in manifest.get("entries", []):
        if entry.get("tier") == "fulltext":
            ids.add(entry.get("ref_id"))
    return ids


def _abstract_ref_ids(run_dir: str) -> set:
    """Return the set of ref_ids that have a stored abstract source."""
    manifest = _load_manifest_payload(run_dir)
    ids = set()
    for entry in manifest.get("entries", []):
        if entry.get("tier") == "abstract":
            ids.add(entry.get("ref_id"))
    return ids


def _safe_task_stem(kept_rel: str) -> str:
    base = os.path.splitext(os.path.basename(kept_rel))[0]
    return _sources._safe(base) if hasattr(_sources, "_safe") else base


def _corroborate_ocr_text(refs, preferred_ref_id, text):
    """Return a corroborated reference without treating OCR probe text as source text."""
    def confirmed(candidate):
        signal, score = _sources.corroborate(candidate, text)
        probe = _sources.document_identity_probe(candidate, text)
        ok = (
            (signal in ("doi", "pmid") or score >= _sources.CORROBORATE_THRESHOLD)
            and probe.get("decision") == "confirmed"
        )
        return ok, signal, score

    preferred = refs.get(preferred_ref_id) if preferred_ref_id else None
    if preferred is not None:
        ok, signal, score = confirmed(preferred)
        if ok:
            return preferred, signal, score
    best = None
    for ref in refs.values():
        ok, signal, score = confirmed(ref)
        if not ok:
            continue
        if best is None or score > best[2]:
            best = (ref, signal, score)
    if best:
        return best
    return None, None, None


def _park_identity_confirmed_ocr_pending(run, task, reason):
    """Persist an explicit pending state without promoting a probe to full text."""
    with _OCR_REGISTRATION_LOCK:
        repo = _repo_open(run)
        if repo is None:
            return
        try:
            repo.park_unreadable_source(
                ref_id=task.get("ref_id"), ref_number=task.get("ref_number"),
                kept_path=task.get("kept_as"), source_ref=None,
                origin=task.get("origin") or "user", reason=reason,
            )
        finally:
            repo.close()


def _run_source_ocr(st, task, answer, *, automatic: bool = False) -> None:
    """Process an answered OCR task: if the user opted in, OCR the parked scan, register
    the text as the source full text (origin 'ocr') and retire the queued PDF. A negative
    or empty answer leaves the scan parked (an honest, recorded non-action)."""
    run = st["run_dir"]
    proceed = bool(answer.get("proceed")) and answer.get("found") is not False
    if not proceed:
        _progress(f"OCR skipped for parked scan {task.get('kept_as')} "
                  "(left in the queue; source stays uncheckable at this tier)")
        return
    kept_rel = task.get("kept_as")
    kept_abs = os.path.join(run, kept_rel) if kept_rel else None
    if not kept_abs or not os.path.exists(kept_abs):
        _progress(f"OCR requested but parked scan is missing: {kept_rel}")
        return
    try:
        from core.fetch.extraction import ocr as _ocr
    except ImportError:
        import ocr as _ocr
    _progress(f"OCR running on parked scan {kept_rel} (this can take a while)")
    lang = st.get("ocr_lang") or DEFAULT_OCR_LANG
    parse = _load_parse_payload(run)
    refs = {r["id"]: r for r in parse.get("references", [])}
    ref_id = task.get("ref_id")
    ref = refs.get(ref_id) if ref_id else None
    identity_method = None
    if automatic:
        # Probe just enough of a scan to establish identity. These bytes are
        # deliberately never registered: only a subsequent full OCR can be fulltext.
        try:
            probe, probe_method = _ocr.ocr_pdf(kept_abs, lang=lang, pages=range(1, 4))
            ref, signal, _score = _corroborate_ocr_text(refs, ref_id, probe)
            if ref is None:
                extra, probe_method = _ocr.ocr_pdf(kept_abs, lang=lang, pages=range(4, 7))
                probe = f"{probe}\n\n{extra}"
                ref, signal, _score = _corroborate_ocr_text(refs, ref_id, probe)
            if ref is not None:
                ref_id = ref["id"]
                identity_method = f"ocr-probe:{probe_method}:{signal}"
        except (FileNotFoundError, RuntimeError) as e:
            _progress(f"OCR identity probe failed for {kept_rel}: {e}")
            return
        if ref is None:
            _progress(f"OCR probe could not corroborate {kept_rel}; left parked")
            return
        page_count = _ocr.pdf_page_count(kept_abs)
        if not _automatic_ocr_enabled() or (page_count is not None and page_count > _automatic_ocr_page_limit()):
            reason = "automatic OCR disabled" if not _automatic_ocr_enabled() else (
                f"PDF has {page_count} pages (automatic limit {_automatic_ocr_page_limit()})")
            pending_task = dict(task, ref_id=ref_id)
            _park_identity_confirmed_ocr_pending(
                run, pending_task, f"identity-confirmed; OCR-pending ({identity_method}): {reason}")
            _progress(f"OCR identity confirmed for {kept_rel}; full OCR pending ({reason})")
            return
    try:
        # A deliberate user opt-in (non-automatic) must force a fresh run: an earlier
        # automatic attempt may have cached a failed Future for these exact bytes, and
        # the point of answering the OCR task is to try again. The automatic path keeps
        # retry=False so unattended re-runs don't repeatedly spend OCR time.
        text, method = _ocr.ocr_pdf(kept_abs, lang=lang, retry=not automatic)
    except (FileNotFoundError, RuntimeError) as e:
        _progress(f"OCR failed for {kept_rel}: {e}")
        return
    try:
        from core.fetch.extraction import pdf as _pdf
    except ImportError:
        import pdf as _pdf
    if not _pdf._quality(text):
        _progress(f"OCR output failed the quality gate for {kept_rel} (poor scan); left parked")
        return
    if ref is None:
        # Provided scan that could not be associated before it had text: corroborate now.
        best = None
        for r in parse.get("references", []):
            sig, score = _sources.corroborate(r, text)
            if best is None or score > best[1]:
                best = (r, score, sig)
        if best and (best[2] in ("doi", "pmid") or best[1] >= _sources.CORROBORATE_THRESHOLD):
            ref, _score, _sig = best
            ref_id = ref["id"]
    if ref is None:
        _progress(f"OCR done for {kept_rel} but no reference matched the text; left parked")
        return
    resolve_result = _load_resolve_map(run).get(ref_id) or {}
    storage_provenance = {
        "supplied_by": "ocr",
        "supplied_via": f"ocr_queue:{kept_rel}",
        "file_format": "pdf",
    }
    # OCR inference may run concurrently, but source/queue registration is kept
    # serial so separate workers cannot race SQLite state or the source manifest.
    with _OCR_REGISTRATION_LOCK:
        from core.fetch.storage import fetch_store as _fetch_store
        stored = _fetch_store.try_store_fulltext(
            run,
            ref,
            resolve_result,
            text,
            method="ocr",
            source_ref=str(task.get("source_ref") or kept_abs),
            extract_method="ocr",
            storage_provenance=storage_provenance,
        )
        if stored.get("status") != "stored":
            _progress(
                f"OCR output for {kept_rel} was not admitted: "
                f"{stored.get('reason') or stored.get('status')} (left parked)"
            )
            return
        # Belt-and-suspenders: retire the queued scan even if it was parked unassociated
        # (provide map retires by ref_id; an unassociated entry is keyed by its kept path).
        _sources.resolve_unreadable(run, ref_id=ref_id, kept_as=kept_rel, method=f"ocr:{method}")
    _progress(f"OCR stored full text for reference [{task.get('ref_number') or ref_id}] "
              f"via {method}; scan retired from the queue")


def _auto_run_source_ocr(st, refs, accuracy: str = "standard") -> dict:
    """Attempt OCR automatically for parked unreadable PDFs that still need full text."""
    run = st["run_dir"]
    payload = _load_unreadable_payload(run)
    attempted = 0
    stored = 0
    failed = 0
    skipped = 0
    errors = []
    fulltext_refs = _fulltext_ref_ids(run)

    completed = _integrity_completed_units(st, "fetch_ocr")
    checkpointing = callable(st.get("_integrity_unit_checkpoint"))
    jobs = []
    for entry in payload.get("entries", []):
        if entry.get("ocr_status") == "done":
            continue
        kept = entry.get("kept_as")
        if not kept or not os.path.exists(os.path.join(run, kept)):
            continue
        if kept in completed:
            continue
        ref_id = entry.get("ref_id")
        if ref_id and ref_id in fulltext_refs:
            continue
        task = {
            "kind": "ocr",
            "ref_id": ref_id,
            "ref_number": entry.get("ref_number"),
            "kept_as": kept,
            "origin": entry.get("origin") or "user",
            "source_ref": entry.get("source_ref"),
            "reference": refs.get(ref_id) if ref_id else None,
        }
        jobs.append((entry, task))

    def run_one(entry, task):
        before = set(_fulltext_ref_ids(run))
        try:
            _run_source_ocr(st, task, {"proceed": True, "found": True}, automatic=True)
        except Exception as exc:
            return "failed", {"ref_id": task.get("ref_id"), "kept_as": entry.get("kept_as"),
                              "error": f"{type(exc).__name__}: {exc}"}
        after = set(_fulltext_ref_ids(run))
        ref_id = task.get("ref_id")
        return ("stored" if (ref_id in after if ref_id else len(after) > len(before)) else "skipped"), None

    def record_outcome(entry, outcome, error):
        nonlocal attempted, stored, failed, skipped
        attempted += 1
        if outcome == "stored":
            stored += 1
        elif outcome == "failed":
            failed += 1
            errors.append(error)
        else:
            skipped += 1
        if checkpointing:
            _checkpoint_integrity_unit(
                st,
                "fetch_ocr",
                entry["kept_as"],
                payload={"outcome": outcome, "error": error},
            )

    # Integrity checkpoints are per scan, so guarded execution is serial.  The
    # original parallel coordinator remains unchanged outside the guarded driver.
    if checkpointing:
        for entry, task in jobs:
            outcome, error = run_one(entry, task)
            record_outcome(entry, outcome, error)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(jobs), 16) or 1) as pool:
            future_map = {
                pool.submit(run_one, entry, task): entry
                for entry, task in jobs
            }
            for future in concurrent.futures.as_completed(future_map):
                entry = future_map[future]
                outcome, error = future.result()
                record_outcome(entry, outcome, error)

    return {
        "attempted": attempted,
        "stored": stored,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
    }


def _controlled_answer_source_provenance(
    run_dir: str, task_id: str
) -> tuple[str, str]:
    repo = _repo_open(run_dir)
    if repo is None:
        raise RuntimeError("task answer provenance repository is unavailable")
    try:
        answer = repo.get_latest_task_answer(task_id)
        if answer is None:
            raise RuntimeError("answered task has no persisted answer")
        provenance = repo.get_task_answer_provenance(answer.answer_id)
    finally:
        repo.close()
    if provenance is None:
        raise RuntimeError("external fetch task answer lacks trusted provenance")
    producer = (
        f"{provenance['producer_class']}:{provenance['producer_identity']}"
    )
    ingress = f"controlled_task_answer:{answer.answer_id}"
    return producer, ingress


def _source_provenance_for_answer(
    answer: dict,
    authenticated_supplier: str,
    controlled_ingress: str,
) -> tuple[str, str]:
    """Attribute guided evidence only when the answer producer is an operator."""
    if not answer.get("guided_fetch"):
        return authenticated_supplier, controlled_ingress
    if not authenticated_supplier.startswith("operator:"):
        raise ValueError("guided Fetch answers require authenticated operator provenance")
    return "user", controlled_ingress


def _is_guided_fetch_source_rejection(answer: dict, result: dict) -> bool:
    outcome = result.get("outcome") or result.get("status")
    return bool(answer.get("guided_fetch")) and outcome in _GUIDED_FETCH_REJECTION_OUTCOMES


def _guided_fetch_rejection_error(result: dict, answer: dict) -> ValueError:
    outcome = result.get("outcome") or result.get("status") or "rejected"
    diagnostics = []
    source_path = answer.get("file_path") or result.get("file")
    source_name = Path(source_path).name if isinstance(source_path, str) else ""
    for key in (
        "reason", "error", "reason_code", "document_relation", "signal", "score",
    ):
        value = result.get(key)
        if value is None:
            continue
        rendered = str(value).replace("\u2014", ":")
        if source_path and source_name:
            rendered = rendered.replace(str(source_path), source_name)
        diagnostics.append(f"{key}={rendered}")
    detail = "; ".join([f"outcome={outcome}", *diagnostics])
    return ValueError(f"guided Fetch source rejected: {detail}")


def _admit_controlled_fetch_answer(
    run_dir: str,
    *,
    ref_id: str,
    answer: dict,
    origin: str,
    supplied_by: str,
    supplied_via: str,
) -> dict:
    """Apply a controlled answer through its declared deterministic tier gate."""
    ref = _run_ref_by_id(run_dir, ref_id)
    resolve_result = _load_resolve_map(run_dir).get(ref_id) or {}
    source_ref = answer.get("url")
    origin = (
        "ocr"
        if _provided_fulltext.is_precomputed_ocr_source_ref(source_ref)
        else origin
    )
    tier = answer.get("source_tier", "fulltext")
    identity_attested = bool(answer.get("identity_attested"))
    if tier == "abstract":
        if not answer.get("file_path"):
            raise ValueError("abstract Fetch answers require a captured file")
        result = _user_sources.ingest_file(
            run_dir,
            _user_sources.identity_view(ref, resolve_result),
            answer["file_path"],
            tier="abstract",
            supplied_by="user" if supplied_via == "guided_fetch" else supplied_by,
            supplied_via=supplied_via,
            source_ref=source_ref,
            identity_attested=identity_attested,
        )
        if result.get("outcome") not in {"accepted_abstract", "duplicate"}:
            if _is_guided_fetch_source_rejection(answer, result):
                return {**result, "status": "rejected"}
            raise ValueError(
                "fetch task answer was not admitted as abstract: "
                f"{result.get('outcome')}: {result.get('reason') or ''}"
            )
        return {**result, "status": "stored"}
    if tier != "fulltext":
        raise ValueError("fetch task answer has an unsupported source tier")
    result = _provided_fulltext.ingest_task_answer(
        run_dir,
        ref,
        resolve_result,
        file_path=answer.get("file_path"),
        text=answer.get("text"),
        source_ref=source_ref,
        origin=origin,
        supplied_by=supplied_by,
        supplied_via=supplied_via,
        identity_attested=identity_attested,
    )
    if result.get("status") not in {"stored", "ocr_pending"}:
        if _is_guided_fetch_source_rejection(answer, result):
            return {**result, "status": "rejected"}
        raise ValueError(
            "fetch task answer was not admitted as full text: "
            f"{result.get('outcome') or result.get('status')}: {result.get('reason') or ''}"
        )
    return result


def _ingest_fetch_answers(st):
    """Called at the start of resume after a fetch pause: register provided texts."""
    run = st["run_dir"]

    def run_newly_parked_ocr() -> None:
        parse = _load_parse_payload(run)
        refs = {ref["id"]: ref for ref in parse.get("references", [])}
        _auto_run_source_ocr(st, refs, accuracy=st.get("accuracy") or "standard")

    has_newly_parked_scan = False
    for handle, t in _answered_tasks(run, "fetch"):
        a = t.get("answer") or {}
        if t.get("kind") == "source_identity_attestation":
            # Identity reviews now share the Fetch checkpoint with retrieval
            # tasks, but their closed answer contract is applied separately by
            # ``_apply_answered_source_identity_attestations`` in phase_fetch.
            # Do not reinterpret that metadata answer as fetched source text.
            continue
        if t.get("kind") == "ocr":
            try:
                _controlled_answer_source_provenance(run, handle)
                _run_source_ocr(st, t, a)
            except Exception as exc:
                _reopen_task_with_error(run, "fetch", handle, t, stage="fetch_ocr_ingest", exc=exc)
                raise
            t["status"] = "done"
            t.pop("last_error", None)
            _update_task(run, "fetch", handle, t, status="applied")
            _checkpoint_applied_fetch_answer(st, handle)
            continue
        if t.get("kind") == "browser_challenge":
            task_parked_scan = False
            rejection = None
            try:
                if not _browser_challenge_answer_is_valid(t):
                    raise ValueError("browser challenge answer does not cover its declared references")
                source_supplied_by, source_supplied_via = (
                    _controlled_answer_source_provenance(run, handle)
                )
                for item in a.get("items") or []:
                    ref_id = item.get("ref_id")
                    supplied_by, supplied_via = _source_provenance_for_answer(
                        item, source_supplied_by, source_supplied_via
                    )
                    if item.get("found") is False or not ref_id:
                        continue
                    if item.get("file_path"):
                        result = _admit_controlled_fetch_answer(
                            run,
                            ref_id=ref_id,
                            answer=item,
                            origin="browser_session",
                            supplied_by=supplied_by,
                            supplied_via=supplied_via,
                        )
                    elif item.get("text"):
                        result = _admit_controlled_fetch_answer(
                            run,
                            ref_id=ref_id,
                            answer=item,
                            origin="browser_session",
                            supplied_by=supplied_by,
                            supplied_via=supplied_via,
                        )
                    else:
                        raise ValueError(
                            f"browser challenge item for {ref_id} is missing file_path/text"
                        )
                    if result.get("status") == "rejected":
                        rejection = _guided_fetch_rejection_error(result, item)
                        break
                    task_parked_scan = (
                        task_parked_scan or result.get("status") == "ocr_pending"
                    )
            except Exception as exc:
                _reopen_task_with_error(
                    run,
                    "fetch",
                    handle,
                    t,
                    stage="browser_challenge_ingest",
                    exc=exc,
                )
                raise
            if rejection is not None:
                has_newly_parked_scan = has_newly_parked_scan or task_parked_scan
                _reopen_task_with_error(
                    run,
                    "fetch",
                    handle,
                    t,
                    stage="browser_challenge_ingest",
                    exc=rejection,
                )
                continue
            t["status"] = "done"
            t.pop("last_error", None)
            _update_task(run, "fetch", handle, t, status="applied")
            _checkpoint_applied_fetch_answer(st, handle)
            has_newly_parked_scan = has_newly_parked_scan or task_parked_scan
            continue
        if a.get("found") is False:
            source_supplied_by, source_supplied_via = (
                _controlled_answer_source_provenance(run, handle)
            )
            _source_provenance_for_answer(a, source_supplied_by, source_supplied_via)
            t["status"] = "done"
            t.pop("last_error", None)
            _update_task(run, "fetch", handle, t, status="applied")
            _checkpoint_applied_fetch_answer(st, handle)
            continue
        ref_id = t["ref_id"]
        try:
            source_supplied_by, source_supplied_via = (
                _controlled_answer_source_provenance(run, handle)
            )
            supplied_by, supplied_via = _source_provenance_for_answer(
                a, source_supplied_by, source_supplied_via
            )
            if a.get("file_path"):
                result = _admit_controlled_fetch_answer(
                    run,
                    ref_id=ref_id,
                    answer=a,
                    origin="webfetch",
                    supplied_by=supplied_by,
                    supplied_via=supplied_via,
                )
            elif a.get("text"):
                result = _admit_controlled_fetch_answer(
                    run,
                    ref_id=ref_id,
                    answer=a,
                    origin="webfetch",
                    supplied_by=supplied_by,
                    supplied_via=supplied_via,
                )
            else:
                raise ValueError(
                    f"fetch task answer for {ref_id} must include file_path or text when found=true"
                )
            if result.get("status") == "rejected":
                _reopen_task_with_error(
                    run,
                    "fetch",
                    handle,
                    t,
                    stage="fetch_answer_ingest",
                    exc=_guided_fetch_rejection_error(result, a),
                )
                continue
            task_parked_scan = result.get("status") == "ocr_pending"
        except Exception as exc:
            _reopen_task_with_error(run, "fetch", handle, t, stage="fetch_answer_ingest", exc=exc)
            raise
        t["status"] = "done"
        t.pop("last_error", None)
        _update_task(run, "fetch", handle, t, status="applied")
        _checkpoint_applied_fetch_answer(st, handle)
        has_newly_parked_scan = has_newly_parked_scan or task_parked_scan
    if has_newly_parked_scan:
        run_newly_parked_ocr()
