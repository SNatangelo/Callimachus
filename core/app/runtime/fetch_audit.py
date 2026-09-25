# core/app/runtime/fetch_audit.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Persistence and deduplication of fetch audit observations."""

from __future__ import annotations

import threading

from core.app.runtime.repository import _repo_open
from core.app.runtime.sources import ABSTRACT_SCOPES
from core.fetch.storage.fetch_store import origin_for_method

_FETCH_ATTEMPT_RECORD_LOCK = threading.Lock()


def record_identity_anomaly(
    repository,
    ref_id: str,
    *,
    phase: str,
    stage: str,
    error: Exception,
) -> int:
    """Persist a non-verdict identity anomaly without stopping sibling work."""
    message = str(error).replace("\x00", "\ufffd").replace("\r", " ").replace("\n", " ")
    reason = f"{phase}:{stage}:{type(error).__name__}: {message[:400]}"
    for row in repository.list_fetch_attempts(ref_id):
        if (
            row.get("method") == "identity_gate"
            and row.get("kind") == "identity_anomaly"
            and row.get("outcome") == "identity_anomaly"
            and row.get("reason") == reason
        ):
            return int(row["fetch_attempt_id"])
    return repository.append_fetch_attempt(
        ref_id,
        method="identity_gate",
        url=None,
        kind="identity_anomaly",
        final_url=None,
        status_code=None,
        content_type=None,
        outcome="identity_anomaly",
        reason=reason,
        origin="runtime",
    )


def _fetch_attempt_fingerprint(
    *,
    method,
    url,
    kind,
    final_url,
    status_code,
    content_type,
    outcome,
    reason,
    trace,
):
    """Stable identity for one persisted fetch observation.

    Fetch traces can be handed to ``_record_fetch_attempts`` by both the
    resolve and fetch phases.  Persisting the same observation twice makes the
    audit look as though another network request happened.  Keep genuinely
    different stages/retries (or changed outcomes) while making replay of the
    same trace idempotent.
    """
    trace = trace if isinstance(trace, dict) else {}
    stage = trace.get("stage") or trace.get("phase") or trace.get("scope")
    position = (
        trace.get("batch_index"),
        trace.get("queue_index"),
        trace.get("attempt_index"),
        trace.get("retry_index"),
        trace.get("strategy"),
    )
    return (
        str(method or ""),
        str(url or ""),
        str(kind or ""),
        str(final_url or ""),
        status_code,
        str(content_type or ""),
        str(outcome or ""),
        str(reason or ""),
        str(stage or ""),
        position,
        trace.get("frozen_candidate_id"),
    )


def _record_fetch_attempts(run_dir: str, ref_id: str, fetched: dict, resolve_result: dict | None = None) -> dict[int, int]:
    trace = fetched.get("fetch_trace") or {}
    execution_attempts = list((trace.get("execution") or {}).get("attempts") or [])
    direct_attempts = list((trace.get("direct_text") or {}).get("attempts") or [])
    provider_diagnostics = []
    provider_diagnostics.extend((trace.get("direct_text") or {}).get("providers") or [])
    provider_diagnostics.extend(
        (trace.get("candidate_generation") or {}).get("provider_candidates") or []
    )
    provider_diagnostics = [
        {key: value for key, value in row.items() if key != "execution_attempts"}
        for row in provider_diagnostics
        if (
            isinstance(row, dict)
            and not row.get("execution_attempts")
            and row.get("status") != "rate_limit_deferred"
            and (row.get("error_type") or row.get("status") in {"error", "partial"})
        )
    ]
    _resolve_result = resolve_result or {}
    # Resolve and fetch workers can finish the same reference close together.
    # Keep the read-existing/append transaction process-local and atomic so
    # idempotence is not defeated by both threads observing the same old rows.
    with _FETCH_ATTEMPT_RECORD_LOCK:
        repo = _repo_open(run_dir)
        if repo is None:
            return {}
        try:
            seen = {
                _fetch_attempt_fingerprint(
                    method=row.get("method"),
                    url=row.get("url"),
                    kind=row.get("kind"),
                    final_url=row.get("final_url"),
                    status_code=row.get("status_code"),
                    content_type=row.get("content_type"),
                    outcome=row.get("outcome"),
                    reason=row.get("reason"),
                    trace=row.get("trace"),
                ): row["fetch_attempt_id"]
                for row in repo.list_fetch_attempts(ref_id)
            }

            def append_once(**values):
                fingerprint = _fetch_attempt_fingerprint(
                    method=values.get("method"),
                    url=values.get("url"),
                    kind=values.get("kind"),
                    final_url=values.get("final_url"),
                    status_code=values.get("status_code"),
                    content_type=values.get("content_type"),
                    outcome=values.get("outcome"),
                    reason=values.get("reason"),
                    trace=values.get("trace"),
                )
                if fingerprint in seen:
                    return int(seen[fingerprint])
                attempt_id = repo.append_fetch_attempt(ref_id, **values)
                seen[fingerprint] = attempt_id
                return attempt_id

            for row in provider_diagnostics:
                provider = row.get("provider")
                append_once(
                    method=provider,
                    url=None,
                    kind="provider_diagnostic",
                    final_url=None,
                    status_code=row.get("http_status"),
                    content_type=None,
                    outcome=(
                        "provider_partial" if row.get("status") == "partial"
                        else "provider_error"
                    ),
                    reason=row.get("error_reason") or row.get("reason") or row.get("error"),
                    challenge_blocked=False,
                    paywalled=False,
                    trace=row,
                    origin=origin_for_method(provider, _resolve_result),
                )
            for row in direct_attempts:
                outcome = str(row.get("outcome") or "direct_text")
                source_ref = row.get("source_ref")
                origin = origin_for_method(row.get("method"), _resolve_result)
                append_once(
                    method=row.get("method"),
                    url=source_ref,
                    kind="direct_text",
                    final_url=source_ref,
                    status_code=None,
                    content_type="text/plain",
                    outcome=outcome,
                    reason=row.get("reason"),
                    challenge_blocked=False,
                    paywalled=False,
                    trace=row,
                    origin=origin,
                )
            frozen_attempt_ids: dict[int, int] = {}
            for row in execution_attempts:
                outcome = str(row.get("outcome") or "attempted")
                reason = row.get("reason")
                challenge_blocked = outcome == "challenge_blocked"
                paywalled = "paywall" in outcome or outcome in ABSTRACT_SCOPES
                origin = origin_for_method(row.get("method"), _resolve_result)
                attempt_id = append_once(
                    method=row.get("method"),
                    url=row.get("url"),
                    kind=row.get("kind"),
                    final_url=row.get("final_url"),
                    status_code=row.get("status"),
                    content_type=row.get("content_type"),
                    outcome=outcome,
                    reason=reason,
                    challenge_blocked=challenge_blocked,
                    paywalled=paywalled,
                    trace=row,
                    origin=origin,
                )
                frozen_candidate_id = row.get("frozen_candidate_id")
                if frozen_candidate_id is not None:
                    if type(frozen_candidate_id) is not int or frozen_candidate_id <= 0:
                        raise ValueError("invalid frozen candidate trace ID")
                    frozen_attempt_ids[frozen_candidate_id] = attempt_id
            if (
                not execution_attempts
                and not direct_attempts
                and not provider_diagnostics
                and fetched.get("status") in (
                    "error",
                    "skipped",
                    "not_found",
                    "download_error",
                    "quality_error",
                    "identity_inconclusive",
                    "wrong_document",
                    "identity_mismatch",
                    "metadata_only",
                    "abstract_only",
                    "abstract_fallback",
                    "stored",
                )
            ):
                origin = origin_for_method(fetched.get("method"), _resolve_result)
                append_once(
                    method=fetched.get("method"),
                    url=fetched.get("pdf_url") or fetched.get("url"),
                    kind="summary",
                    final_url=fetched.get("pdf_url") or fetched.get("url"),
                    status_code=None,
                    content_type=fetched.get("content_type"),
                    outcome=fetched.get("status"),
                    reason=fetched.get("reason"),
                    challenge_blocked=False,
                    paywalled="paywall" in str(fetched.get("reason") or "").lower(),
                    trace={"summary_only": True},
                    origin=origin,
                )
            for frozen_candidate_id, attempt_id in frozen_attempt_ids.items():
                for row in execution_attempts:
                    if (
                        row.get("frozen_candidate_id") == frozen_candidate_id
                        and row.get("outcome") == "deadline_exceeded"
                    ):
                        repo.record_frozen_fetch_candidate_event(
                            frozen_candidate_id,
                            "deadline_skipped",
                            fetch_attempt_id=attempt_id,
                            reason_code="deadline_exceeded",
                            reason_detail=row.get("reason"),
                        )
                        break
            return frozen_attempt_ids
        finally:
            repo.close()
