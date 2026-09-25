#!/usr/bin/env python3
# core/parse/file_probe.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic content/extension probes for user-provided documents.

Normal manuscript parsing remains extension-dispatched.  The stricter probe is
used at trust boundaries where a user-controlled filename must not select an
unrelated parser merely because its suffix was changed.  Future extractor
modules can implement ``probe_file(path) -> content_family`` and optionally set
``CONTENT_FAMILY``; built-in extractors use the dependency-free probes below.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path


class FileProbeError(ValueError):
    """Base class for deterministic file-probe rejection."""


class FormatMismatchError(FileProbeError):
    """The bytes identify a different format family than the extension."""


class UnreadableFileError(FileProbeError):
    """The file cannot be read or has no safely identifiable content."""


_SAMPLE_BYTES = 64 * 1024
_BUILTIN_FAMILIES = {
    "docx": "docx",
    "html": "html",
    "markdown": "text",
    "pdf": "pdf",
    "tex": "text",
    "txt": "text",
}
_HTML_DOCUMENT_RE = re.compile(
    br"(?is)^(?:(?:<!--.*?-->)\s*)*(?:<!doctype\s+html\b|<html\b)"
)
_XHTML_DOCUMENT_RE = re.compile(br"(?is)^<\?xml[^>]*\?>\s*<html\b")
_HTML_FRAGMENT_RE = re.compile(
    br"(?is)^</?(?:article|body|div|h[1-6]|head|html|main|meta|p|section|table|title)\b"
)


def _extractor_name(module: object) -> str:
    return str(getattr(module, "__name__", "extractor")).rsplit(".", 1)[-1]


def _read_sample(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            sample = handle.read(_SAMPLE_BYTES)
    except OSError as exc:
        raise UnreadableFileError(f"cannot read file: {exc}") from exc
    if not sample:
        raise UnreadableFileError("file is empty")
    return sample


def _looks_like_html(sample: bytes) -> bool:
    head = sample[:8192].lstrip(b"\xef\xbb\xbf \t\r\n")
    lowered = head.lower()
    if (
        _HTML_DOCUMENT_RE.match(head)
        or _XHTML_DOCUMENT_RE.match(head)
        or _HTML_FRAGMENT_RE.match(head)
    ):
        return True
    return (
        b"<head" in lowered
        and b"<body" in lowered
    ) or (
        b"<article" in lowered
        and b"</article" in lowered
    )


def _looks_like_text(sample: bytes) -> bool:
    if b"\x00" in sample:
        return False
    decoded = sample.decode("utf-8", errors="replace")
    if not decoded.strip():
        raise UnreadableFileError("file contains no readable content")
    replacement_count = decoded.count("\ufffd")
    if replacement_count > max(2, len(decoded) // 100):
        return False
    control_count = sum(
        ord(char) < 32 and char not in "\t\n\r\f"
        for char in decoded
    )
    return control_count <= max(2, len(decoded) // 100)


def _detect_builtin(path: str) -> str:
    sample = _read_sample(path)
    if sample[:1024].lstrip().startswith(b"%PDF"):
        return "pdf"

    zip_magic = sample.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    try:
        is_zip = zipfile.is_zipfile(path)
    except OSError as exc:
        raise UnreadableFileError(f"cannot inspect ZIP container: {exc}") from exc
    if is_zip:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise UnreadableFileError(f"corrupt ZIP container: {exc}") from exc
        if {"[Content_Types].xml", "word/document.xml"} <= names:
            return "docx"
        return "zip"
    if zip_magic:
        raise UnreadableFileError("corrupt ZIP container")

    if _looks_like_html(sample):
        return "html"
    if _looks_like_text(sample):
        return "text"
    return "binary"


def probe_extractor(path: str, module: object) -> dict[str, str]:
    """Validate ``path`` against its selected extractor and return its family.

    A future extractor participates by exposing ``probe_file``.  It must return
    the detected content-family name and may set ``CONTENT_FAMILY`` when that
    name differs from its module name.  Missing future probes fail closed.
    """
    name = _extractor_name(module)
    expected = str(
        getattr(module, "CONTENT_FAMILY", None)
        or _BUILTIN_FAMILIES.get(name)
        or name
    ).strip().casefold()
    custom_probe = getattr(module, "probe_file", None)
    if callable(custom_probe):
        try:
            detected = custom_probe(path)
        except FileProbeError:
            raise
        except (OSError, RuntimeError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
            raise UnreadableFileError(
                f"{expected} content probe failed: {exc}"
            ) from exc
    elif name in _BUILTIN_FAMILIES:
        detected = _detect_builtin(path)
    else:
        raise UnreadableFileError(
            f"extractor {name!r} has no deterministic probe_file contract"
        )

    if not isinstance(detected, str) or not detected.strip():
        raise UnreadableFileError(
            f"extractor {name!r} returned an invalid content probe result"
        )
    detected = detected.strip().casefold()
    if detected != expected:
        suffix = Path(path).suffix.lower() or "<none>"
        raise FormatMismatchError(
            f"format mismatch: extension {suffix} selects {expected}, "
            f"but content looks like {detected}"
        )
    return {"declared_format": expected, "detected_format": detected}
