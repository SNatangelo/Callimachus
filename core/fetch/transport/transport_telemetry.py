# core/fetch/transport/transport_telemetry.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Best-effort, thread-local Fetch request accounting."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import uuid

_LOCAL = threading.local()
_LOG = logging.getLogger(__name__)


@contextlib.contextmanager
def credential_scope(*, provider: str, env_name: str):
    """Bind an explicitly inserted provider credential to physical attempts."""
    previous = getattr(_LOCAL, "credential", None)
    _LOCAL.credential = {"provider": provider, "env_name": env_name}
    try:
        yield
    finally:
        _LOCAL.credential = previous


@contextlib.contextmanager
def suspend_credential_attribution():
    """Temporarily suppress credential attribution for a public subrequest."""
    previous = getattr(_LOCAL, "credential", None)
    _LOCAL.credential = None
    try:
        yield
    finally:
        _LOCAL.credential = previous


def _endpoint(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except Exception:
        return ""


def _request_url_identity(url: str) -> str:
    """Normalize only URL parts that cannot alter a GET representation."""
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port and not (
        (parsed.scheme.lower() == "https" and port == 443)
        or (parsed.scheme.lower() == "http" and port == 80)
    ):
        port_text = f":{port}"
    else:
        port_text = ""
    if ":" in host:
        host = f"[{host}]"
    userinfo, separator, _ = parsed.netloc.rpartition("@")
    netloc = f"{userinfo}@" if separator else ""
    netloc += host + port_text
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
    )


def _request_fingerprint(*, method: str, url: str, profile: str, strategy: str,
                         accept: str = "*/*", referer: str | None = None,
                         headers_extra: dict[str, str] | None = None) -> str:
    """Hash exact request inputs without persisting URLs, headers, or secrets."""
    try:
        extras = {
            str(name).lower(): str(value)
            for name, value in (headers_extra or {}).items()
        }
        encoded = json.dumps(
            {
                "method": method,
                "url": _request_url_identity(url),
                "profile": profile,
                "strategy": strategy,
                "accept": accept,
                "referer": referer,
                "headers_extra": extras,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    except Exception:
        encoded = b"invalid-request-input"
    return hashlib.sha256(encoded).hexdigest()


def _failure(repo, stage: str, ref_id: str | None, error: BaseException) -> None:
    if repo is None:
        return
    try:
        repo.append_fetch_transport_failure({
            "failure_id": str(uuid.uuid4()), "stage": stage,
            "ref_id": ref_id, "error_type": type(error).__name__,
        })
    except Exception:
        pass


def _diagnostic(stage: str, error: BaseException) -> str:
    parts = [f"stage={stage}", f"error_type={type(error).__name__}"]
    for name in ("sqlite_errorname", "sqlite_errorcode"):
        value = getattr(error, name, None)
        if value is not None:
            parts.append(f"{name}={value}")
    return " ".join(parts)


@contextlib.contextmanager
def logical_request(*, run_dir: str | None, ref_id: str | None, url: str,
                    profile: str, strategy: str, cache_outcome: str,
                    method: str = "GET", accept: str = "*/*",
                    referer: str | None = None,
                    headers_extra: dict[str, str] | None = None):
    previous = getattr(_LOCAL, "request", None)
    previous_credential = getattr(_LOCAL, "credential", None)
    repo = None
    record = None
    if run_dir and ref_id:
        try:
            from core.infra.db import RunRepository
            repo = RunRepository.open(run_dir)
        except Exception as error:
            _LOG.warning(
                "Fetch transport telemetry unavailable: %s",
                _diagnostic("open", error),
            )
            _failure(repo, "open", ref_id, error)
        else:
            try:
                record = {
                    "request_id": str(uuid.uuid4()), "ref_id": ref_id,
                    "requested_endpoint": _endpoint(url), "profile": profile,
                    "strategy": strategy, "cache_outcome": cache_outcome,
                    "request_fingerprint_sha256": _request_fingerprint(
                        method=method, url=url, profile=profile, accept=accept,
                        referer=referer, headers_extra=headers_extra, strategy=strategy,
                    ),
                }
                repo.append_fetch_transport_request(record)
            except Exception as error:
                _LOG.warning(
                    "Fetch transport telemetry unavailable: %s",
                    _diagnostic("logical_request", error),
                )
                _failure(repo, "logical_request", ref_id, error)
                record = None
    binding = getattr(headers_extra, "credential_binding", None)
    if binding is not None:
        _LOCAL.credential = {"provider": binding[0], "env_name": binding[1]}
    _LOCAL.request = (repo, record)
    try:
        yield record
    finally:
        _LOCAL.request, _LOCAL.credential = previous, previous_credential
        if repo is not None:
            try:
                repo.close()
            except Exception:
                pass


@contextlib.contextmanager
def provider_callback_request(*, url: str, profile: str, accept: str,
                              referer: str | None,
                              headers_extra: dict[str, str] | None,
                              run_dir: str | None = None,
                              ref_id: str | None = None):
    """Create a provider-callback parent for an explicitly scoped callback."""
    if not run_dir or not ref_id:
        yield
        return
    with logical_request(
        run_dir=run_dir,
        ref_id=ref_id,
        url=url,
        profile=profile,
        strategy="provider_callback",
        cache_outcome="not_applicable",
        method="GET",
        accept=accept,
        referer=referer,
        headers_extra=headers_extra,
    ):
        yield


def record_attempt(*, attempt_kind: str, method: str, url: str, started: float,
                   status: int | None = None, error: BaseException | None = None,
                   admission_started_at_ms: int | None = None,
                   admitted_at_ms: int | None = None,
                   sent_at_ms: int | None = None) -> None:
    repo = None
    record = None
    try:
        repo, record = getattr(_LOCAL, "request", (None, None))
        if repo is None or record is None:
            return
        if error is None:
            outcome = "response"
        elif isinstance(error, urllib.error.HTTPError):
            outcome = "http_error"
        else:
            outcome = "network_error"
        if record.get("strategy") == "provider_callback":
            attempt_kind = "provider_callback"
        record["attempt_index"] = record.get("attempt_index", 0) + 1
        payload = {
            "attempt_id": str(uuid.uuid4()), "request_id": record["request_id"],
            "attempt_index": record["attempt_index"], "attempt_kind": attempt_kind,
            "method": method, "endpoint": _endpoint(url), "status": status,
            "error_type": type(error).__name__ if error else None,
            "duration_ms": max(0.0, (time.monotonic() - started) * 1000),
            "outcome": outcome,
        }
        if any(value is not None for value in (
            admission_started_at_ms, admitted_at_ms, sent_at_ms,
        )):
            payload.update({
                "admission_started_at_ms": admission_started_at_ms,
                "admitted_at_ms": admitted_at_ms,
                "sent_at_ms": sent_at_ms,
            })
        repo.append_fetch_transport_attempt(
            payload, credential=getattr(_LOCAL, "credential", None)
        )
    except Exception as exc:
        _failure(repo, "attempt", record.get("ref_id") if record else None, exc)
