#!/usr/bin/env python3
# core/resolve/http.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP helpers for the resolve package."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar

from . import transport_telemetry as _transport_telemetry

try:
    from core.fetch.transport import host_limiter as _host_limiter
    from core.infra import perf as _perf
    from core.fetch.transport.http_headers import (
        configured_user_agent,
        open_request,
        request_headers,
        user_agent_with_mailto,
    )
except ImportError:  # direct execution
    import host_limiter as _host_limiter
    import perf as _perf
    from http_headers import configured_user_agent, open_request, request_headers, user_agent_with_mailto


BASE_UA = configured_user_agent()
_UA = BASE_UA          # default: no email => polite pool inactive
_CONTACT_EMAIL = None
TIMEOUT = 12
MAX_RETRY_AFTER = 300  # honour a server Retry-After up to the limiter's escalation
                       # ceiling; the host limiter caps and escalates from there
MAX_ATTEMPTS = 3
MAX_LIMITER_WAIT_SECONDS = 5.0
RETRYABLE_SERVER_CODES = {502, 503, 504}

ENV_HTTP_MEMO = "CITATION_VERIFIER_HTTP_MEMO"
_response_memo: dict[tuple[str, str, bool], tuple[int, str | bytes]] = {}
_memo_lock = threading.Lock()
_provider_failure_scope: ContextVar[dict | None] = ContextVar(
    "provider_failure_scope", default=None
)


class ProviderCooldownDeferred(RuntimeError):
    """Provider-local admission deferred before a second physical request."""

    provider = ""

    def __init__(self, *, not_before: float, provider: str | None = None,
                 physical_429_count: int = 0,
                 physical_429_tokens: tuple[object, ...] = ()) -> None:
        self.provider = provider or self.provider
        self.not_before = float(not_before)
        self.physical_429_count = max(0, int(physical_429_count))
        # These opaque, process-local identities distinguish one physical HTTP
        # 429 from multiple references observing its shared provider cooldown.
        self.physical_429_tokens = tuple(physical_429_tokens)
        super().__init__(f"{self.provider} admission deferred by active cooldown")


class SemanticScholarCooldownDeferred(ProviderCooldownDeferred):
    """Semantic Scholar work declined before I/O because a shared cooldown is live.

    This is intentionally distinct from a received HTTP 429: the latter is a
    completed provider attempt, while this signal carries no network response
    and lets the resolve phase resume the same in-memory continuation later.
    """

    provider = "semantic_scholar"


@contextmanager
def provider_failure_scope():
    """Capture the last HTTP failure swallowed by a provider invocation.

    The registry enables this only around provider calls.  ``ContextVar`` keeps
    concurrent provider workers isolated and leaves ordinary resolver HTTP use
    untouched.
    """
    state: dict = {}
    token = _provider_failure_scope.set(state)
    try:
        yield state
    finally:
        _provider_failure_scope.reset(token)


def _record_provider_failure(exc: BaseException) -> None:
    state = _provider_failure_scope.get()
    if state is not None:
        state["exception"] = exc


def error_category(exc: BaseException) -> str:
    """Return the conservative provider/fetch category for *exc*.

    This deliberately classifies only signals that are safe to act on.  An
    unknown exception remains ``provider_error`` rather than being guessed to
    be transient.
    """
    if isinstance(exc, MemoryError):
        return "resource_exhausted"
    if isinstance(exc, _host_limiter.HostCooldownExceeded):
        return "rate_limit"
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "auth"
        if exc.code == 429:
            return "rate_limit"
        if 500 <= exc.code <= 599:
            return "transient_server"
        return "http_error"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout"
        return "network"
    if isinstance(exc, (ConnectionError, OSError)):
        return "network"
    return "provider_error"


def error_reason(exc: BaseException) -> str:
    """Produce a short readable diagnostic while retaining the exception detail."""
    category = error_category(exc)
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            wait = _retry_after_seconds(exc)
            return f"{category} (HTTP 429; Retry-After {wait:g}s)"
        return f"{category} (HTTP {exc.code})"
    if isinstance(exc, _host_limiter.HostCooldownExceeded):
        return f"{category}: host cooldown requires {exc.wait_seconds:g}s"
    detail = str(exc).strip()
    return f"{category}: {detail}" if detail else category


def is_retryable(exc: BaseException) -> bool:
    """Only retry bounded transient failures; auth and resource failures stop."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code in RETRYABLE_SERVER_CODES
    if isinstance(exc, _host_limiter.HostCooldownExceeded):
        return True
    return error_category(exc) in {"network", "timeout"}


def _memo_enabled() -> bool:
    return str(os.environ.get(ENV_HTTP_MEMO) or "").strip().lower() in ("1", "true", "yes", "on")


def reset_response_memo() -> None:
    """Clear the in-process GET response memo (used by tests / between runs)."""
    with _memo_lock:
        _response_memo.clear()


def set_contact(mailto: str | None):
    """Set the User-Agent with the polite-pool contact email (or without one, if None)."""
    global _UA, _CONTACT_EMAIL
    _UA = user_agent_with_mailto(mailto)
    _CONTACT_EMAIL = (mailto or "").strip() or None


def _shared_host_limiter():
    return _host_limiter.get_shared_limiter()


def _get(
    url: str,
    accept: str = "application/json",
    headers_extra: dict[str, str] | None = None,
    *,
    preserve_cooldown_after_429: bool = False,
):
    binding = getattr(url, "credential_binding", None)
    scope = (
        _transport_telemetry.credential_scope(
            provider=binding[0], env_name=binding[1]
        ) if binding else contextlib.nullcontext()
    )
    with scope:
        with _transport_telemetry.physical_request(method="GET", url=url):
            return _get_recorded(url, accept, headers_extra, preserve_cooldown_after_429)


def _get_with_final_url(
    url: str,
    accept: str = "application/json",
    headers_extra: dict[str, str] | None = None,
    *,
    preserve_cooldown_after_429: bool = False,
):
    """GET while retaining the final URL after redirects for authority checks."""
    binding = getattr(url, "credential_binding", None)
    scope = (
        _transport_telemetry.credential_scope(
            provider=binding[0], env_name=binding[1]
        ) if binding else contextlib.nullcontext()
    )
    with scope:
        with _transport_telemetry.physical_request(method="GET", url=url):
            return _get_recorded(
                url, accept, headers_extra, preserve_cooldown_after_429,
                include_final_url=True,
            )


def _get_bytes(
    url: str,
    accept: str = "application/octet-stream",
    headers_extra: dict[str, str] | None = None,
    *,
    preserve_cooldown_after_429: bool = False,
):
    """GET a binary authority artefact through the audited resolve transport."""
    binding = getattr(url, "credential_binding", None)
    scope = (
        _transport_telemetry.credential_scope(
            provider=binding[0], env_name=binding[1]
        ) if binding else contextlib.nullcontext()
    )
    with scope:
        with _transport_telemetry.physical_request(method="GET", url=url):
            return _get_recorded(
                url,
                accept,
                headers_extra,
                preserve_cooldown_after_429,
                decode=False,
            )


def _get_bytes_with_final_url(
    url: str,
    accept: str = "application/octet-stream",
    headers_extra: dict[str, str] | None = None,
    *,
    preserve_cooldown_after_429: bool = False,
):
    """Binary GET variant retaining the redirect target for authority checks."""
    binding = getattr(url, "credential_binding", None)
    scope = (
        _transport_telemetry.credential_scope(
            provider=binding[0], env_name=binding[1]
        ) if binding else contextlib.nullcontext()
    )
    with scope:
        with _transport_telemetry.physical_request(method="GET", url=url):
            return _get_recorded(
                url,
                accept,
                headers_extra,
                preserve_cooldown_after_429,
                include_final_url=True,
                decode=False,
            )


def _get_recorded(
    url, accept, headers_extra, preserve_cooldown_after_429=False,
    *, include_final_url=False, decode=True,
):
    headers = request_headers(url=url, accept=accept, profile="api")
    # `_UA` is applied before the caller's extras so a provider can override the
    # User-Agent when a host demands one (the same order `_post_json` already
    # uses). No caller overrode it before; the default `_UA` is unchanged.
    headers["User-Agent"] = _UA
    if headers_extra:
        headers.update(headers_extra)
    req = urllib.request.Request(url, headers=headers)
    host = (urllib.parse.urlparse(url).netloc or "").lower()

    memo_key = None
    if _memo_enabled() and not headers_extra and not include_final_url:
        memo_key = (url, accept, bool(decode))
        with _memo_lock:
            cached = _response_memo.get(memo_key)
        if cached is not None:
            return cached

    last_exc: Exception | None = None
    physical_429_count = 0
    physical_429_tokens: list[object] = []
    limiter_wait_deadline = time.monotonic() + MAX_LIMITER_WAIT_SECONDS
    limiter = _shared_host_limiter()
    for attempt in range(MAX_ATTEMPTS):
        try:
            with _perf.span("resolve_wait", host):
                admission_token = limiter.acquire(
                    host,
                    max_wait_seconds=max(0.0, limiter_wait_deadline - time.monotonic()),
                )
        except _host_limiter.HostCooldownExceeded as e:
            failure = (
                e if preserve_cooldown_after_429 else last_exc
                if isinstance(last_exc, urllib.error.HTTPError) and last_exc.code == 429
                else e
            )
            failure.physical_429_count = physical_429_count
            failure.physical_429_tokens = tuple(physical_429_tokens)
            _record_provider_failure(failure)
            raise failure
        try:
            started = time.monotonic()
            started_at_ms = int(time.time() * 1000)
            response_status = None
            with _perf.span("http", host):
                with open_request(req, timeout=TIMEOUT) as r:
                    response_status = r.status
                    final_url = r.geturl() if include_final_url else None
                    raw_body = r.read()
                    body = raw_body.decode("utf-8", errors="replace") if decode else raw_body
                    if memo_key is not None and 200 <= response_status < 300:
                        with _memo_lock:
                            _response_memo[memo_key] = (response_status, body)
            limiter.report_success(host)
            _transport_telemetry.record_attempt(
                method="GET",
                url=url,
                attempt_number=attempt + 1,
                started=started,
                started_at_ms=started_at_ms,
                status=response_status,
            )
            if include_final_url:
                return response_status, body, final_url
            return response_status, body
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = _retry_after_seconds(e)
                limiter.cooldown(host, wait, admission_token=admission_token)
                physical_429_count += 1
                physical_429_tokens.append(object())
                last_exc = e
            elif e.code in RETRYABLE_SERVER_CODES:
                last_exc = e
            else:
                _record_provider_failure(e)
            _transport_telemetry.record_attempt(
                method="GET",
                url=url,
                attempt_number=attempt + 1,
                started=started,
                started_at_ms=started_at_ms,
                status=e.code,
                error=e,
                retry_after_seconds=_retry_after_header_seconds(e) if e.code == 429 else None,
            )
            if e.code == 429:
                continue
            if e.code in RETRYABLE_SERVER_CODES:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as e:
            last_exc = e
            _transport_telemetry.record_attempt(
                method="GET",
                url=url,
                attempt_number=attempt + 1,
                started=started,
                started_at_ms=started_at_ms,
                error=e,
            )
            time.sleep(1.0 * (attempt + 1))
            continue
        except Exception as e:
            _transport_telemetry.record_attempt(
                method="GET",
                url=url,
                attempt_number=attempt + 1,
                started=started,
                started_at_ms=started_at_ms,
                status=response_status,
                error=e,
            )
            raise
    # Out of retries: re-raise the actual last failure (preserving its detail)
    # rather than masking it behind a generic error.
    failure = last_exc if last_exc is not None else OSError("_get failed after 3 attempts")
    _record_provider_failure(failure)
    raise failure


def _ncbi_url(base: str, params: dict[str, str]) -> str:
    query = dict(params)
    selected_env_name = None
    query.setdefault("tool", "CitationVerifier")
    if _CONTACT_EMAIL:
        query.setdefault("email", _CONTACT_EMAIL)
    if (
        (urllib.parse.urlparse(base).hostname or "").lower()
        == "eutils.ncbi.nlm.nih.gov"
        and "api_key" not in query
    ):
        for env_name in ("NCBI_API_KEY", "ENTREZ_API_KEY"):
            api_key = (os.environ.get(env_name) or "").strip()
            if api_key:
                query["api_key"] = api_key
                selected_env_name = env_name
                break
    url = base + urllib.parse.urlencode(query)
    # Carry only the selected environment variable to the immediate HTTP
    # boundary.  ``str`` compatibility keeps this private carrier out of
    # persisted request data; the telemetry endpoint strips the query anyway.
    if selected_env_name is not None:
        class _CredentialURL(str):
            pass
        value = _CredentialURL(url)
        value.credential_binding = ("ncbi", selected_env_name)
        return value
    return url


def _retry_after_seconds(err) -> float:
    """Read Retry-After from a 429 error (integer seconds); default 2s, capped at MAX."""
    try:
        val = float((getattr(err, "headers", None) or {}).get("Retry-After", "2"))
    except (TypeError, ValueError):
        val = 2.0
    return max(0.0, min(val, MAX_RETRY_AFTER))


def _retry_after_header_seconds(err) -> float | None:
    """Return a parseable Retry-After only when the server actually supplied it."""
    headers = getattr(err, "headers", None) or {}
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, min(float(raw), MAX_RETRY_AFTER))
    except (TypeError, ValueError):
        return None


def _post_json(
    url: str,
    payload: dict,
    *,
    accept: str = "application/json",
    headers_extra: dict[str, str] | None = None,
    preserve_cooldown_after_429: bool = False,
):
    with _transport_telemetry.physical_request(method="POST", url=url):
        return _post_json_recorded(
            url, payload, accept=accept, headers_extra=headers_extra,
            preserve_cooldown_after_429=preserve_cooldown_after_429,
        )


def _post_json_recorded(url, payload, *, accept, headers_extra, preserve_cooldown_after_429=False):
    headers = request_headers(url=url, accept=accept, profile="api")
    headers["User-Agent"] = _UA
    headers["Content-Type"] = "application/json"
    if headers_extra:
        headers.update(headers_extra)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    host = (urllib.parse.urlparse(url).netloc or "").lower()
    last_exc: Exception | None = None
    physical_429_count = 0
    physical_429_tokens: list[object] = []
    limiter_wait_deadline = time.monotonic() + MAX_LIMITER_WAIT_SECONDS
    limiter = _shared_host_limiter()
    for attempt in range(MAX_ATTEMPTS):
        try:
            with _perf.span("resolve_wait", host):
                admission_token = limiter.acquire(
                    host,
                    max_wait_seconds=max(0.0, limiter_wait_deadline - time.monotonic()),
                )
        except _host_limiter.HostCooldownExceeded as e:
            failure = (
                e if preserve_cooldown_after_429 else last_exc
                if isinstance(last_exc, urllib.error.HTTPError) and last_exc.code == 429
                else e
            )
            failure.physical_429_count = physical_429_count
            failure.physical_429_tokens = tuple(physical_429_tokens)
            _record_provider_failure(failure)
            raise failure
        try:
            started = time.monotonic()
            started_at_ms = int(time.time() * 1000)
            response_status = None
            with _perf.span("http", host):
                with open_request(req, timeout=TIMEOUT) as r:
                    response_status = r.status
                    body = r.read().decode("utf-8", errors="replace")
            limiter.report_success(host)
            _transport_telemetry.record_attempt(
                method="POST",
                url=url,
                attempt_number=attempt + 1,
                started=started,
                started_at_ms=started_at_ms,
                status=response_status,
            )
            return response_status, body
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = _retry_after_seconds(e)
                limiter.cooldown(host, wait, admission_token=admission_token)
                physical_429_count += 1
                physical_429_tokens.append(object())
                last_exc = e
            elif e.code in RETRYABLE_SERVER_CODES:
                last_exc = e
            else:
                _record_provider_failure(e)
            _transport_telemetry.record_attempt(
                method="POST",
                url=url,
                attempt_number=attempt + 1,
                started=started,
                started_at_ms=started_at_ms,
                status=e.code,
                error=e,
                retry_after_seconds=_retry_after_header_seconds(e) if e.code == 429 else None,
            )
            if e.code == 429:
                continue
            if e.code in RETRYABLE_SERVER_CODES:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as e:
            last_exc = e
            _transport_telemetry.record_attempt(
                method="POST",
                url=url,
                attempt_number=attempt + 1,
                started=started,
                started_at_ms=started_at_ms,
                error=e,
            )
            time.sleep(1.0 * (attempt + 1))
            continue
        except Exception as e:
            _transport_telemetry.record_attempt(
                method="POST",
                url=url,
                attempt_number=attempt + 1,
                started=started,
                started_at_ms=started_at_ms,
                status=response_status,
                error=e,
            )
            raise
    failure = last_exc if last_exc is not None else OSError("_post_json failed after 3 attempts")
    _record_provider_failure(failure)
    raise failure
