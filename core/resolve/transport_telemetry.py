# core/resolve/transport_telemetry.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Best-effort, non-secret Resolve transport persistence."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import uuid

_LOCAL = threading.local()
_LOG = logging.getLogger(__name__)


@contextlib.contextmanager
def credential_scope(*, provider: str, env_name: str):
    """Bind a non-secret credential identity to the exact outbound call.

    Callers enter this only after they have elected to inject that credential.
    It is deliberately thread-local and never derives identity from a URL or
    request headers.
    """
    previous = getattr(_LOCAL, "credential", None)
    _LOCAL.credential = {"provider": provider, "env_name": env_name}
    try:
        yield
    finally:
        _LOCAL.credential = previous


def _failure(stage: str, error: BaseException) -> None:
    repo = getattr(_LOCAL, "repo", None)
    if repo is None:
        return
    try:
        target_sha256 = getattr(_LOCAL, "manuscript_input_sha256", None)
        payload = {
            "failure_id": str(uuid.uuid4()),
            "stage": stage,
            "error_type": type(error).__name__,
        }
        if target_sha256 is not None:
            payload.update({
                "target_kind": "manuscript_identity",
                "manuscript_input_sha256": target_sha256,
            })
        elif getattr(_LOCAL, "ref_id", None) is not None:
            payload.update({
                "target_kind": "reference",
                "ref_id": _LOCAL.ref_id,
            })
        else:
            payload["target_kind"] = "run"
        repo.append_resolve_transport_failure(payload)
    except Exception:
        pass


@contextlib.contextmanager
def bind_run(
    run_dir: str, *, ref_id: str | None = None,
    manuscript_input_sha256: str | None = None,
):
    if manuscript_input_sha256 is not None and (
        ref_id is not None
        or not isinstance(manuscript_input_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", manuscript_input_sha256) is None
    ):
        raise ValueError("invalid manuscript transport target")
    previous = (
        getattr(_LOCAL, "repo", None), getattr(_LOCAL, "ref_id", None),
        getattr(_LOCAL, "manuscript_input_sha256", None),
    )
    repo = None
    try:
        from core.infra.db import RunRepository

        repo = RunRepository.open(run_dir)
    except Exception as error:
        _LOG.warning(
            "Resolve transport telemetry unavailable: %s", type(error).__name__
        )
    _LOCAL.repo, _LOCAL.ref_id = repo, ref_id
    _LOCAL.manuscript_input_sha256 = manuscript_input_sha256
    try:
        yield
    finally:
        if repo is not None:
            try:
                repo.close()
            except Exception:
                pass
        _LOCAL.repo, _LOCAL.ref_id, _LOCAL.manuscript_input_sha256 = previous


@contextlib.contextmanager
def operation(
    *,
    provider: str,
    operation: str,
    mode: str,
    ref_ids: list[str],
    item_count: int | None = None,
    chunk_index: int | None = None,
    predecessor_operation_id: str | None = None,
):
    previous = getattr(_LOCAL, "operation", None)
    record = None
    repo = getattr(_LOCAL, "repo", None)
    if repo is not None:
        target_sha256 = getattr(_LOCAL, "manuscript_input_sha256", None)
        record = {
            "operation_id": str(uuid.uuid4()),
            "provider": provider,
            "operation": operation,
            "mode": mode,
            "chunk_index": chunk_index,
            "predecessor_operation_id": predecessor_operation_id,
        }
        if target_sha256 is not None:
            record.update({
                "target_kind": "manuscript_identity",
                "manuscript_input_sha256": target_sha256,
                "item_count": 1,
            })
        else:
            record.update({
                "ref_ids": sorted(set(ref_ids)),
                "item_count": len(ref_ids) if item_count is None else item_count,
            })
        try:
            repo.append_resolve_transport_operation(record)
        except Exception as error:
            _failure("operation", error)
            record = None
    _LOCAL.operation = record
    try:
        yield record["operation_id"] if record else None
    finally:
        _LOCAL.operation = previous


def append_ref_mapping(operation_id: str | None, ref_id: str | None) -> None:
    repo = getattr(_LOCAL, "repo", None)
    if getattr(_LOCAL, "manuscript_input_sha256", None) is not None:
        return
    if repo is not None and operation_id and ref_id:
        try:
            repo.append_resolve_transport_ref_mapping(operation_id, ref_id)
        except Exception as error:
            _failure("mapping", error)


def record_institutional_search(query: str, provenance: dict, candidates: list[dict]) -> None:
    """Append institutional search evidence without retaining query text."""
    repo, ref_id = getattr(_LOCAL, "repo", None), getattr(_LOCAL, "ref_id", None)
    if repo is None or ref_id is None:
        return
    try:
        repo.append_institutional_search_provenance({
            "search_id": str(uuid.uuid4()),
            "ref_id": ref_id,
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "outcome": provenance["outcome"],
            "selected_backend": provenance.get("selected_backend"),
            "attempts": provenance["attempts"],
            "candidates": [
                {"title": str(item.get("title") or ""), "url": str(item.get("url") or "")}
                for item in candidates
            ],
        })
    except Exception as error:
        _failure("search_provenance", error)


@contextlib.contextmanager
def physical_request(*, method: str, url: str):
    previous_request = getattr(_LOCAL, "request_id", None)
    previous_operation = getattr(_LOCAL, "operation", None)
    if previous_operation is None and (
        getattr(_LOCAL, "ref_id", None)
        or getattr(_LOCAL, "manuscript_input_sha256", None)
    ):
        with operation(
            provider="resolve_http",
            operation="request",
            mode="scalar",
            ref_ids=[_LOCAL.ref_id] if getattr(_LOCAL, "ref_id", None) else [],
        ):
            _LOCAL.request_id = str(uuid.uuid4())
            try:
                yield _LOCAL.request_id
            finally:
                _LOCAL.request_id, _LOCAL.operation = (
                    previous_request,
                    previous_operation,
                )
        return
    _LOCAL.request_id = str(uuid.uuid4())
    try:
        yield _LOCAL.request_id
    finally:
        _LOCAL.request_id, _LOCAL.operation = previous_request, previous_operation


def _endpoint(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def record_attempt(
    *,
    method: str,
    url: str,
    attempt_number: int,
    started: float,
    started_at_ms: int | None = None,
    status: int | None = None,
    error: BaseException | None = None,
    retry_after_seconds: float | None = None,
) -> None:
    record = getattr(_LOCAL, "operation", None)
    request_id = getattr(_LOCAL, "request_id", None)
    repo = getattr(_LOCAL, "repo", None)
    if not record or not request_id or repo is None:
        return
    if error is None:
        outcome = "response"
    elif isinstance(error, urllib.error.HTTPError):
        outcome = "http_error"
    else:
        outcome = "network_error"
    try:
        repo.append_resolve_transport_attempt(
            {
                "attempt_id": str(uuid.uuid4()),
                "operation_id": record["operation_id"],
                "request_id": request_id,
                "attempt_number": attempt_number,
                "method": method,
                "endpoint": _endpoint(url),
                "status": status,
                "error_type": type(error).__name__ if error else None,
                "started_at_ms": int(time.time() * 1000) if started_at_ms is None else started_at_ms,
                "retry_after_seconds": retry_after_seconds,
                "duration_ms": max(0.0, (time.monotonic() - started) * 1000),
                "outcome": outcome,
            }, credential=getattr(_LOCAL, "credential", None)
        )
    except Exception as exc:
        _failure("attempt", exc)
