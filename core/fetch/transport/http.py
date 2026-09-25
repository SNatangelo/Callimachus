#!/usr/bin/env python3
# core/fetch/transport/http.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Low-level HTTP fetch primitives — GET, download, header snapshot, body inspection."""

from __future__ import annotations

import brotli as _brotli
import contextlib
import gzip
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

try:
    from core.fetch.extraction import fetch_html as _fetch_html
    from core.fetch.transport import host_limiter as _host_limiter
    from core.fetch.transport import semantic_scholar_pacing as _semantic_scholar_pacing
    import core.fetch.hosts as _hosts_mod
    import core.fetch.transport.http_headers as _http_headers_mod
    from core.fetch.transport import transport_telemetry as _transport_telemetry
    from core.infra import perf as _perf
except ImportError:
    import fetch_html as _fetch_html
    import host_limiter as _host_limiter
    import semantic_scholar_pacing as _semantic_scholar_pacing
    import hosts as _hosts_mod
    import http_headers as _http_headers_mod
    import transport_telemetry as _transport_telemetry
    import perf as _perf


class FetchAdmissionDeferred(RuntimeError):
    """A Fetch request was not admitted because its host is cooling down.

    This is intentionally distinct from a network failure: no request was sent
    and the queue scheduler may retry the identical frozen candidate at the
    authoritative not-before time.
    """

    def __init__(self, host: str, wait_seconds: float) -> None:
        self.host = host
        self.wait_seconds = max(0.0, float(wait_seconds))
        self.not_before = time.monotonic() + self.wait_seconds
        super().__init__(f"host cooldown for {host} requires {self.wait_seconds:g}s")


def _acquire_for_fetch(limiter, url: str) -> int | None:
    host = _host_limiter.host_for_url(url)
    try:
        with _perf.span("fetch_wait", host or None):
            return limiter.acquire_for_fetch_url(url)
    except _host_limiter.HostCooldownExceeded as exc:
        raise FetchAdmissionDeferred(exc.host, exc.wait_seconds) from exc


# Cookie-handshake fallback: when a fetch is bounced to an Atypon `cookieAbsent`
# gate, retry once through a cookie-jar session (warm up on the origin, then
# replay the request with the server-issued session cookie). Set to 0/off/false
# to disable — e.g. for deterministic single-request test fixtures.
ENV_COOKIE_HANDSHAKE = "CITATION_VERIFIER_COOKIE_HANDSHAKE"

TIMEOUT_API = 12
# Upper bound on a 429 Retry-After pause. The host limiter is SHARED across the
# resolve and fetch paths, so an unbounded cooldown here would stall every later
# request to the same host (e.g. api.openalex.org) for the whole run. Mirrors the
# cap in core/resolve/http.py and the host limiter's escalation ceiling. A malicious
# or misconfigured Retry-After can be days.
MAX_RETRY_AFTER = 300
TRACE_BODY_HEAD_LIMIT = 500
TRACE_HEADER_VALUE_LIMIT = 500
TRACE_HEADER_EXCLUDE = {
    "set-cookie",
    "cookie",
    "authorization",
    "proxy-authorization",
}


def _retry_after_seconds(err) -> float:
    try:
        value = float(err.headers.get("Retry-After", "2"))
    except (AttributeError, TypeError, ValueError):
        value = 2.0
    return max(0.0, min(value, MAX_RETRY_AFTER))


def _decompress_deflate(body: bytes) -> bytes:
    try:
        return zlib.decompress(body)
    except zlib.error:
        return zlib.decompress(body, -zlib.MAX_WBITS)  # raw deflate, no zlib header


class ContentDecodingError(ValueError):
    """A declared HTTP content encoding could not be deterministically undone."""

    def __init__(self, encoding: str, reason_code: str):
        self.encoding = encoding
        self.reason_code = reason_code
        message = (
            f"unsupported declared Content-Encoding {encoding!r}"
            if reason_code == "content_encoding_unsupported"
            else f"failed to decode declared Content-Encoding {encoding!r}"
        )
        super().__init__(message)


_BODY_DECODERS = {
    "br": _brotli.decompress,
    "gzip": gzip.decompress,
    "x-gzip": gzip.decompress,
    "deflate": _decompress_deflate,
}


def _decode_body(body: bytes, headers) -> bytes:
    """Undo any ``Content-Encoding`` the response declares.

    We never send an ``Accept-Encoding``, so a live server answers identity and
    this is a no-op. Wayback's raw (``id_``) snapshots are the exception: they
    replay the ORIGINAL captured response, headers included, so a page archived
    with ``Content-Encoding: gzip`` arrives compressed regardless. Nothing
    downstream decodes, so the extractor would read binary noise and discard a
    perfectly good archived full text.

    Keyed on the declared encoding only — never on sniffed magic bytes — so a
    genuine ``.gz`` download (which states its type in Content-Type, not here)
    is passed through untouched. Unknown, corrupt, or partially decoded stacks
    raise a typed error so encoded bytes can never reach a content parser.
    """
    if not body:
        return body
    try:
        declared = str(headers.get("Content-Encoding") or "")
    except AttributeError:
        return body
    for encoding in reversed([tok.strip().lower() for tok in declared.split(",") if tok.strip()]):
        if encoding == "identity":
            continue
        decoder = _BODY_DECODERS.get(encoding)
        if decoder is None:
            raise ContentDecodingError(encoding, "content_encoding_unsupported")
        try:
            body = decoder(body)
        except Exception as exc:
            raise ContentDecodingError(
                encoding,
                "content_encoding_decode_failed",
            ) from exc
    return body


def _response_headers_snapshot(headers) -> dict[str, str]:
    if not headers:
        return {}
    out = {}
    for key, value in headers.items():
        low_key = str(key).lower()
        if low_key in TRACE_HEADER_EXCLUDE:
            continue
        text = str(value)
        if len(text) > TRACE_HEADER_VALUE_LIMIT:
            text = text[:TRACE_HEADER_VALUE_LIMIT] + "..."
        out[str(key)] = text
    return out


def _body_head_text(body: bytes, content_type: str | None = None) -> str:
    body = body or b""
    if not body:
        return ""
    text = _fetch_html.decode_textual_body(body[:TRACE_BODY_HEAD_LIMIT], content_type)
    if text is None:
        text = body[:TRACE_BODY_HEAD_LIMIT].decode("utf-8", errors="replace")
    return text[:TRACE_BODY_HEAD_LIMIT]


def _challenge_markers(body: bytes, content_type: str | None = None) -> list[str]:
    text = _fetch_html.decode_textual_body(body[:4096], content_type)
    if not text:
        return []
    return _fetch_html.challenge_markers_in_html(text)


def _get(
    url: str,
    accept: str = "*/*",
    timeout: int = TIMEOUT_API,
    *,
    profile: str = "document",
    referer: str | None = None,
    headers_extra: dict[str, str] | None = None,
    _return_effective_url: bool = False,
) -> tuple[int, bytes] | tuple[int, bytes, str]:
    limiter = _hosts_mod._shared_host_limiter()
    host = _host_limiter.host_for_url(url)
    admission_started_at_ms = time.time_ns() // 1_000_000
    admission_token = _acquire_for_fetch(limiter, url)
    admitted_at_ms = max(admission_started_at_ms, time.time_ns() // 1_000_000)
    headers = _http_headers_mod.request_headers(url=url, accept=accept, profile=profile, referer=referer)
    if headers_extra:
        headers.update(headers_extra)
    req = urllib.request.Request(
        url,
        headers=headers,
    )
    sent_at_ms = max(admitted_at_ms, time.time_ns() // 1_000_000)
    started = time.monotonic()
    status = None
    error = None
    try:
        with _perf.span("fetch_download", host or None):
            with _http_headers_mod.open_request(req, timeout=timeout) as r:
                raw_body = r.read()
        body = _decode_body(raw_body, r.headers)
        status = r.status
        limiter.report_success_for_url(url)
        if _return_effective_url:
            response_url = r.geturl() if callable(getattr(r, "geturl", None)) else url
            return r.status, body, response_url
        return r.status, body
    except urllib.error.HTTPError as e:
        status = e.code
        error = e
        if e.code == 429:
            limiter.cooldown_for_url(
                url, _retry_after_seconds(e), admission_token=admission_token
            )
        raise
    except Exception as exc:
        error = exc
        raise
    finally:
        _transport_telemetry.record_attempt(
            attempt_kind="primary", method="GET", url=url, started=started,
            status=status, error=error,
            admission_started_at_ms=admission_started_at_ms,
            admitted_at_ms=admitted_at_ms,
            sent_at_ms=sent_at_ms,
        )


def _result_dict(
    status,
    body,
    content_type,
    url,
    headers,
    *,
    http_error=False,
    redirect_chain=None,
    content_decoding_error: ContentDecodingError | None = None,
) -> dict:
    result = {
        "status": status,
        "body": body,
        "content_type": content_type,
        "url": url,
        "headers": _response_headers_snapshot(headers),
        "body_head": _body_head_text(body, content_type),
        "challenge_markers": _challenge_markers(body, content_type),
        "redirect_chain": list(redirect_chain) if redirect_chain else [],
    }
    if http_error:
        result["http_error"] = True
    if content_decoding_error is not None:
        result["content_decoding_error"] = {
            "encoding": content_decoding_error.encoding,
            "reason_code": content_decoding_error.reason_code,
        }
    return result


def _transport_error_result(exc: Exception) -> dict:
    """Represent a request that started but failed before a response arrived."""
    return {
        "status": None,
        "body": b"",
        "content_type": None,
        "url": None,
        "transport_error": {
            "reason_code": "transport_error",
            "exception_type": type(exc).__name__,
        },
    }


def _open_and_read(
    opener_open,
    req: urllib.request.Request,
    timeout: int,
    url: str,
    *,
    admission_token: int | None = None,
    limiter=None,
    attempt_kind: str = "primary",
    admission_started_at_ms: int | None = None,
    admitted_at_ms: int | None = None,
    sent_at_ms: int | None = None,
) -> dict | None:
    if limiter is None:
        limiter = _hosts_mod._shared_host_limiter()
    host = _host_limiter.host_for_url(url)
    started = time.monotonic()
    status = None
    error = None
    try:
        response_error = None
        with _perf.span("fetch_download", host or None):
            try:
                with opener_open(req, timeout=timeout) as r:
                    raw_body = r.read()
            except urllib.error.HTTPError as exc:
                response_error = exc
                try:
                    raw_body = exc.read()
                except Exception:
                    raw_body = b""
        if response_error is not None:
            e = response_error
            try:
                status = e.code
                error = e
                if e.code == 429:
                    limiter.cooldown_for_url(
                        url, _retry_after_seconds(e), admission_token=admission_token
                    )
                try:
                    body = _decode_body(raw_body, e.headers)
                    decoding_error = None
                except ContentDecodingError as exc:
                    body = b""
                    decoding_error = exc
                except Exception:
                    body = b""
                    decoding_error = None
                content_type = e.headers.get("Content-Type") or ""
                return _result_dict(
                    e.code, body, content_type, e.geturl(), e.headers, http_error=True,
                    redirect_chain=getattr(req, "_redirect_chain", None),
                    content_decoding_error=decoding_error,
                )
            finally:
                with contextlib.suppress(Exception):
                    e.close()
        try:
            body = _decode_body(raw_body, r.headers)
            decoding_error = None
        except ContentDecodingError as exc:
            body = b""
            decoding_error = exc
        content_type = r.headers.get("Content-Type") or ""
        limiter.report_success_for_url(url)
        status = getattr(r, "status", None)
        return _result_dict(
            getattr(r, "status", None), body, content_type, r.geturl(), r.headers,
            redirect_chain=getattr(req, "_redirect_chain", None),
            content_decoding_error=decoding_error,
        )
    except Exception as exc:
        error = exc
        return _transport_error_result(exc)
    finally:
        _transport_telemetry.record_attempt(
            attempt_kind=attempt_kind, method="GET", url=url, started=started,
            status=status, error=error,
            admission_started_at_ms=admission_started_at_ms,
            admitted_at_ms=admitted_at_ms,
            sent_at_ms=sent_at_ms,
        )


def _cookie_handshake_enabled() -> bool:
    return os.environ.get(ENV_COOKIE_HANDSHAKE, "1").strip().lower() not in ("0", "off", "false", "no")


def _looks_like_cookie_gate(result: dict | None) -> bool:
    if not result:
        return False
    return _fetch_html.is_cookie_gate(
        result.get("url"), markers=result.get("challenge_markers") or ()
    )


def _fetch_url_with_session(
    url: str,
    accept: str = "*/*",
    timeout: int = TIMEOUT_API,
    *,
    profile: str = "document",
    referer: str | None = None,
    headers_extra: dict[str, str] | None = None,
) -> dict | None:
    """Retry a fetch through a cookie-jar session: warm up on the origin to collect
    a server-issued session cookie, then replay the request reusing that cookie.
    Clears Atypon ``cookieAbsent`` gates (SIAM, SagePub, ...) that a stateless
    request cannot pass. Cloudflare/CAPTCHA challenges are out of scope here."""
    opener = _http_headers_mod.build_session_opener()
    limiter = _hosts_mod._shared_host_limiter()

    # 1) Warm up on the origin so the platform hands us a session cookie.
    parts = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    warm_status = None
    warm_error = None
    attempted = False
    warm_admission_started_at_ms = None
    warm_admitted_at_ms = None
    warm_sent_at_ms = None
    # The warm-up has no provider-specific headers; it must not inherit the
    # surrounding primary request's credential attribution.
    with _transport_telemetry.suspend_credential_attribution():
        try:
            warm_admission_started_at_ms = time.time_ns() // 1_000_000
            _acquire_for_fetch(limiter, origin)
            warm_admitted_at_ms = max(warm_admission_started_at_ms, time.time_ns() // 1_000_000)
            warm_headers = _http_headers_mod.request_headers(
                url=origin, accept="text/html,application/xhtml+xml,*/*;q=0.8", profile="document"
            )
            warm_request = urllib.request.Request(origin, headers=warm_headers)
            warm_sent_at_ms = max(warm_admitted_at_ms, time.time_ns() // 1_000_000)
            started = time.monotonic()
            attempted = True
            with _perf.span("fetch_download", _host_limiter.host_for_url(origin) or None):
                warm_response = opener.open(warm_request, timeout=timeout)
                try:
                    warm_response.read()
                    warm_status = getattr(warm_response, "status", None)
                finally:
                    with contextlib.suppress(Exception):
                        warm_response.close()
        except FetchAdmissionDeferred:
            raise
        except Exception as error:
            warm_error = error
            if isinstance(error, urllib.error.HTTPError):
                warm_status = error.code
                with contextlib.suppress(Exception):
                    error.close()
            # A failed warm-up is non-fatal: the replay below may still succeed, and
            # if it doesn't we simply fall back to the original blocked result.
            pass
        finally:
            if attempted:
                _transport_telemetry.record_attempt(
                    attempt_kind="cookie_warmup", method="GET", url=origin,
                    started=started, status=warm_status, error=warm_error,
                    admission_started_at_ms=warm_admission_started_at_ms,
                    admitted_at_ms=warm_admitted_at_ms,
                    sent_at_ms=warm_sent_at_ms,
                )

    # 2) Replay the real request with the jar now populated.
    admission_started_at_ms = time.time_ns() // 1_000_000
    admission_token = _acquire_for_fetch(limiter, url)
    admitted_at_ms = max(admission_started_at_ms, time.time_ns() // 1_000_000)
    headers = _http_headers_mod.request_headers(
        url=url, accept=accept, profile=profile, referer=referer or origin
    )
    if headers_extra:
        headers.update(headers_extra)
    sent_at_ms = max(admitted_at_ms, time.time_ns() // 1_000_000)
    return _open_and_read(
        opener.open,
        urllib.request.Request(url, headers=headers),
        timeout,
        url,
        admission_token=admission_token,
        limiter=limiter,
        attempt_kind="cookie_replay",
        admission_started_at_ms=admission_started_at_ms,
        admitted_at_ms=admitted_at_ms,
        sent_at_ms=sent_at_ms,
    )


def _fetch_url(
    url: str,
    accept: str = "*/*",
    timeout: int = TIMEOUT_API,
    *,
    profile: str = "document",
    referer: str | None = None,
    headers_extra: dict[str, str] | None = None,
) -> dict | None:
    limiter = _hosts_mod._shared_host_limiter()
    admission_started_at_ms = time.time_ns() // 1_000_000
    admission_token = _acquire_for_fetch(limiter, url)
    admitted_at_ms = max(admission_started_at_ms, time.time_ns() // 1_000_000)
    headers = _http_headers_mod.request_headers(url=url, accept=accept, profile=profile, referer=referer)
    if headers_extra:
        headers.update(headers_extra)
    req = urllib.request.Request(
        url,
        headers=headers,
    )
    sent_at_ms = max(admitted_at_ms, time.time_ns() // 1_000_000)
    result = _open_and_read(
        _http_headers_mod.open_request,
        req,
        timeout,
        url,
        admission_token=admission_token,
        limiter=limiter,
        attempt_kind="primary",
        admission_started_at_ms=admission_started_at_ms,
        admitted_at_ms=admitted_at_ms,
        sent_at_ms=sent_at_ms,
    )

    # Atypon cookie-gate fallback: one cookie-handshake retry when the response is
    # a `cookieAbsent` bounce. Bounded to that signal so ordinary fetches keep a
    # single round-trip and JS/Cloudflare challenges aren't retried pointlessly.
    if _looks_like_cookie_gate(result) and _cookie_handshake_enabled():
        retried = _fetch_url_with_session(
            url, accept, timeout, profile=profile, referer=referer, headers_extra=headers_extra
        )
        if retried is not None and not _looks_like_cookie_gate(retried):
            return retried
    return result


def _download(url: str, dest: str) -> bool:
    try:
        _status, body = _get(url, timeout=_hosts_mod._pdf_timeout(), profile="pdf")
        with open(dest, "wb") as f:
            f.write(body)
        return True
    except FetchAdmissionDeferred:
        raise
    except Exception:
        return False


def _body_looks_html(body: bytes, content_type: str | None = None) -> bool:
    if content_type and "html" in content_type.lower():
        return True
    head = (body or b"")[:256].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")
