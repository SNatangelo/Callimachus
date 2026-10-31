#!/usr/bin/env python3
# tests/test_http_body_limits.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded response reads and content decoding stay deterministic offline."""

from __future__ import annotations

import gzip
import io
import urllib.error
import urllib.request
import zlib

import brotli
import pytest

from core.fetch.transport import http as fetch_http
from core.fetch.storage.fetch_cache import FetchRunContext
from core.resolve import http as resolve_http
from core.shared import http_body_limits


class _Response:
    def __init__(self, body: bytes, headers: dict[str, str], *, status: int = 200):
        self._body = body
        self._offset = 0
        self.headers = headers
        self.status = status
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            size = len(self._body) - self._offset
        end = min(len(self._body), self._offset + size)
        chunk = self._body[self._offset:end]
        self._offset = end
        return chunk

    def geturl(self) -> str:
        return "https://fixture.example/item"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Limiter:
    def __init__(self):
        self.requests = 0

    def acquire_for_fetch_url(self, _url):
        self.requests += 1
        return 1

    def acquire(self, _host, *, max_wait_seconds=None):
        self.requests += 1
        return 1

    def report_success_for_url(self, _url):
        return None

    def report_success(self, _host):
        return None

    def cooldown(self, *_args, **_kwargs):
        return None

    def cooldown_for_url(self, *_args, **_kwargs):
        return None


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def test_response_limits_distinguish_metadata_from_full_documents():
    assert http_body_limits.response_body_limit(
        {"Content-Type": "application/json"}, profile="api", accept="application/json"
    ) == http_body_limits.HTTP_METADATA_BODY_LIMIT
    assert http_body_limits.response_body_limit(
        {"Content-Type": "application/pdf"}, profile="api", accept="application/pdf"
    ) == http_body_limits.HTTP_DOCUMENT_BODY_LIMIT
    assert http_body_limits.response_body_limit(
        {"Content-Type": "text/html"}, profile="document", accept="*/*"
    ) == http_body_limits.HTTP_DOCUMENT_BODY_LIMIT


def test_content_length_over_limit_fails_before_reading():
    response = _Response(b"should not read", {"Content-Length": "9"})
    with pytest.raises(http_body_limits.HTTPBodyLimitError) as raised:
        http_body_limits.read_limited_body(response, limit_bytes=8)
    assert raised.value.declared_bytes == 9
    assert response.read_sizes == []


def test_streaming_body_is_read_in_bounded_pieces_and_rejected_without_returning_prefix():
    response = _Response(b"0123456789", {})
    with pytest.raises(http_body_limits.HTTPBodyLimitError) as raised:
        http_body_limits.read_limited_body(response, limit_bytes=8)
    assert raised.value.observed_bytes == 9
    assert response.read_sizes == [9]


def test_gzip_supports_concatenated_members_with_bounded_output():
    payload = b"first member" + b" and second"
    compressed = (
        gzip.compress(b"first member")
        + b"\x00\x00"
        + gzip.compress(b" and second")
        + b"\x00\x00\x00\x00"
    )
    assert fetch_http._decode_body(
        compressed, {"Content-Encoding": "gzip"}, max_output_bytes=64
    ) == payload


@pytest.mark.parametrize(
    "encoding,compressed",
    [
        ("gzip", gzip.compress(b"bounded expansion")),
        ("deflate", zlib.compress(b"bounded expansion")),
        ("deflate", _raw_deflate(b"bounded expansion")),
        ("br", brotli.compress(b"bounded expansion")),
    ],
    ids=["gzip", "deflate-zlib", "deflate-raw", "br"],
)
def test_supported_compression_codecs_decode_under_the_limit(encoding, compressed):
    assert fetch_http._decode_body(
        compressed, {"Content-Encoding": encoding}, max_output_bytes=128
    ) == b"bounded expansion"


@pytest.mark.parametrize(
    "encoding,compressed",
    [
        ("gzip", gzip.compress(b"x" * 1024)),
        ("deflate", zlib.compress(b"x" * 1024)),
        ("deflate", _raw_deflate(b"x" * 1024)),
        ("br", brotli.compress(b"x" * 1024)),
    ],
    ids=["gzip", "deflate-zlib", "deflate-raw", "br"],
)
def test_compression_expansion_is_rejected_at_the_output_limit(encoding, compressed):
    with pytest.raises(http_body_limits.HTTPBodyLimitError):
        fetch_http._decode_body(
            compressed, {"Content-Encoding": encoding}, max_output_bytes=32
        )


def test_fetch_oversize_response_returns_controlled_empty_transport_error(monkeypatch):
    monkeypatch.setattr(http_body_limits, "HTTP_DOCUMENT_BODY_LIMIT", 8)
    response = _Response(b"0123456789", {"Content-Type": "text/html"})
    limiter = _Limiter()
    req = urllib.request.Request("https://fixture.example/item")

    result = fetch_http._open_and_read(
        lambda *_args, **_kwargs: response,
        req,
        1,
        "https://fixture.example/item",
        profile="document",
        accept="text/html",
        limiter=limiter,
    )

    assert result["body"] == b""
    assert result["transport_error"] == {
        "reason_code": "http_body_limit_exceeded",
        "exception_type": "HTTPBodyLimitError",
    }


def test_fetch_oversize_response_is_not_cached(monkeypatch):
    monkeypatch.setattr(http_body_limits, "HTTP_DOCUMENT_BODY_LIMIT", 8)
    monkeypatch.setattr(fetch_http._hosts_mod, "_shared_host_limiter", _Limiter)
    calls = []

    def open_request(*_args, **_kwargs):
        calls.append(1)
        return _Response(b"0123456789", {"Content-Type": "text/html"})

    monkeypatch.setattr(fetch_http._http_headers_mod, "open_request", open_request)
    context = FetchRunContext()
    url = "https://fixture.example/item"

    first = context.cached_fetch(fetch_http._fetch_url, url)
    second = context.cached_fetch(fetch_http._fetch_url, url)

    assert first["body"] == second["body"] == b""
    assert first["transport_error"]["reason_code"] == "http_body_limit_exceeded"
    assert len(calls) == 2


def test_fetch_compressed_expansion_is_reported_without_partial_body(monkeypatch):
    monkeypatch.setattr(http_body_limits, "HTTP_DOCUMENT_BODY_LIMIT", 32)
    compressed = gzip.compress(b"x" * 2048)
    response = _Response(
        compressed,
        {
            "Content-Type": "text/html",
            "Content-Encoding": "gzip",
            "Content-Length": str(len(compressed)),
        },
    )
    result = fetch_http._open_and_read(
        lambda *_args, **_kwargs: response,
        urllib.request.Request("https://fixture.example/item"),
        1,
        "https://fixture.example/item",
        profile="document",
        accept="text/html",
        limiter=_Limiter(),
    )
    assert result["body"] == b""
    assert result["transport_error"]["reason_code"] == "http_body_limit_exceeded"


def test_oversized_fetch_429_still_applies_host_cooldown(monkeypatch):
    monkeypatch.setattr(http_body_limits, "HTTP_METADATA_BODY_LIMIT", 8)
    limiter = _Limiter()
    cooled = []
    limiter.cooldown_for_url = lambda *args, **kwargs: cooled.append((args, kwargs))
    attempts = []
    monkeypatch.setattr(
        fetch_http._transport_telemetry,
        "record_attempt",
        lambda **kwargs: attempts.append(kwargs),
    )
    response_error = urllib.error.HTTPError(
        "https://fixture.example/item",
        429,
        "rate limited",
        {"Content-Type": "application/json", "Content-Length": "10", "Retry-After": "3"},
        io.BytesIO(b"0123456789"),
    )
    result = fetch_http._open_and_read(
        lambda *_args, **_kwargs: (_ for _ in ()).throw(response_error),
        urllib.request.Request("https://fixture.example/item"),
        1,
        "https://fixture.example/item",
        profile="api",
        accept="application/json",
        admission_token=7,
        limiter=limiter,
    )
    assert result["body"] == b""
    assert result["transport_error"]["reason_code"] == "http_body_limit_exceeded"
    assert len(cooled) == 1
    assert cooled[0][1]["admission_token"] == 7
    assert attempts[0]["status"] == 429


def test_resolver_metadata_overflow_is_not_retried_or_memoized(monkeypatch):
    monkeypatch.setattr(http_body_limits, "HTTP_METADATA_BODY_LIMIT", 8)
    monkeypatch.setenv(resolve_http.ENV_HTTP_MEMO, "1")
    resolve_http.reset_response_memo()
    limiter = _Limiter()
    response = _Response(
        b"0123456789",
        {"Content-Type": "application/json", "Content-Length": "10"},
    )
    monkeypatch.setattr(resolve_http, "_shared_host_limiter", lambda: limiter)
    monkeypatch.setattr(resolve_http, "open_request", lambda *_args, **_kwargs: response)
    url = "https://api.fixture.example/works"

    with pytest.raises(http_body_limits.HTTPBodyLimitError):
        resolve_http._get(url)

    assert resolve_http.error_category(http_body_limits.HTTPBodyLimitError(8)) == "resource_exhausted"
    assert limiter.requests == 1
    assert (url, "application/json", True) not in resolve_http._response_memo
    resolve_http.reset_response_memo()


def test_resolver_allows_pdf_under_document_cap_while_api_json_uses_metadata_cap(monkeypatch):
    monkeypatch.setattr(http_body_limits, "HTTP_METADATA_BODY_LIMIT", 8)
    monkeypatch.setattr(http_body_limits, "HTTP_DOCUMENT_BODY_LIMIT", 16)
    response = _Response(
        b"%PDF-1.7 ok!",
        {"Content-Type": "application/pdf", "Content-Length": "12"},
    )
    monkeypatch.setattr(resolve_http, "_shared_host_limiter", _Limiter)
    monkeypatch.setattr(resolve_http, "open_request", lambda *_args, **_kwargs: response)

    status, body = resolve_http._get_bytes(
        "https://fixture.example/item.pdf", accept="application/pdf"
    )

    assert status == 200
    assert body == b"%PDF-1.7 ok!"


def test_resolver_post_json_response_is_bounded(monkeypatch):
    monkeypatch.setattr(http_body_limits, "HTTP_METADATA_BODY_LIMIT", 8)
    monkeypatch.setattr(resolve_http, "_shared_host_limiter", _Limiter)
    monkeypatch.setattr(
        resolve_http,
        "open_request",
        lambda *_args, **_kwargs: _Response(
            b"0123456789",
            {"Content-Type": "application/json", "Content-Length": "10"},
        ),
    )

    with pytest.raises(http_body_limits.HTTPBodyLimitError):
        resolve_http._post_json_recorded(
            "https://api.fixture.example/query",
            {},
            accept="application/json",
            headers_extra=None,
        )
