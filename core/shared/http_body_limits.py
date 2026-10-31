#!/usr/bin/env python3
# core/shared/http_body_limits.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Bound HTTP response reads before a body reaches parsers or caches."""

from __future__ import annotations

HTTP_METADATA_BODY_LIMIT = 32 * 1024 * 1024
HTTP_DOCUMENT_BODY_LIMIT = 256 * 1024 * 1024
HTTP_READ_CHUNK_SIZE = 64 * 1024


class HTTPBodyLimitError(RuntimeError):
    """An HTTP response exceeded its allowed encoded or decoded body size."""

    reason_code = "http_body_limit_exceeded"

    def __init__(self, limit_bytes: int, *, observed_bytes: int | None = None,
                 declared_bytes: int | None = None) -> None:
        self.limit_bytes = int(limit_bytes)
        self.observed_bytes = observed_bytes
        self.declared_bytes = declared_bytes
        if declared_bytes is not None:
            detail = f"declared {declared_bytes} bytes"
        elif observed_bytes is not None:
            detail = f"at least {observed_bytes} bytes"
        else:
            detail = "response body"
        super().__init__(
            f"HTTP {detail} exceeds the {self.limit_bytes}-byte body limit"
        )


def _header(headers, name: str):
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is None:
            value = getter(name.lower())
        if value is None:
            value = getter(name.title())
        return value
    try:
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return value
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def _mime_tokens(value: str | None) -> list[str]:
    return [
        item.split(";", 1)[0].strip().lower()
        for item in str(value or "").split(",")
        if item.strip()
    ]


def _document_mime(mime: str) -> bool:
    return (
        mime.startswith("text/")
        or mime in {
            "application/pdf",
            "application/xhtml+xml",
            "application/epub+zip",
            "application/rtf",
            "application/postscript",
            "application/msword",
        }
        or mime.startswith("application/vnd.openxmlformats-officedocument.")
    )


def _metadata_mime(mime: str) -> bool:
    return (
        mime in {
            "application/json",
            "application/ld+json",
            "application/xml",
            "text/xml",
            "text/csv",
            "application/csv",
            "application/x-ndjson",
        }
        or mime.endswith("+json")
        or mime.endswith("+xml")
    )


def response_body_limit(
    headers=None,
    *,
    profile: str | None = None,
    accept: str | None = None,
    force_document: bool = False,
) -> int:
    """Choose a metadata or full-document cap from request and response hints.

    API JSON/XML and other structured metadata use the smaller cap. PDF, HTML,
    plain text, and explicit full-document requests retain room for large files.
    """
    accept_mimes = _mime_tokens(accept)
    response_mime = _mime_tokens(_header(headers, "Content-Type"))

    if force_document or str(profile or "").strip().lower() == "pdf":
        return HTTP_DOCUMENT_BODY_LIMIT
    if any(_document_mime(mime) and not _metadata_mime(mime) for mime in accept_mimes):
        return HTTP_DOCUMENT_BODY_LIMIT
    if any(_metadata_mime(mime) for mime in accept_mimes):
        return HTTP_METADATA_BODY_LIMIT
    if any(_document_mime(mime) and not _metadata_mime(mime) for mime in response_mime):
        return HTTP_DOCUMENT_BODY_LIMIT
    if any(_metadata_mime(mime) for mime in response_mime):
        return HTTP_METADATA_BODY_LIMIT
    if str(profile or "").strip().lower() == "api":
        return HTTP_METADATA_BODY_LIMIT
    return HTTP_DOCUMENT_BODY_LIMIT


def read_limited_body(response, *, limit_bytes: int, headers=None) -> bytes:
    """Read at most ``limit_bytes + 1`` bytes, rejecting oversized responses.

    A trustworthy Content-Length is checked before reading. Streaming and
    chunked responses are consumed in bounded pieces and fail as soon as the
    first byte beyond the limit arrives.
    """
    limit_bytes = max(0, int(limit_bytes))
    if headers is None:
        headers = getattr(response, "headers", None)
    raw_length = _header(headers, "Content-Length")
    try:
        declared_bytes = int(str(raw_length).strip())
    except (TypeError, ValueError):
        declared_bytes = None
    if declared_bytes is not None and declared_bytes >= 0 and declared_bytes > limit_bytes:
        raise HTTPBodyLimitError(limit_bytes, declared_bytes=declared_bytes)

    read = getattr(response, "read", None)
    if not callable(read):
        raise TypeError("HTTP response has no readable body")

    chunks: list[bytes] = []
    total = 0
    while True:
        size = min(HTTP_READ_CHUNK_SIZE, limit_bytes - total + 1)
        chunk = read(size)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        total += len(chunk)
        if total > limit_bytes:
            raise HTTPBodyLimitError(limit_bytes, observed_bytes=total)
        chunks.append(chunk)
    return b"".join(chunks)
