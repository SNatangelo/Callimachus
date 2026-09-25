# core/report/human/privacy.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Local-path privacy boundary for the self-contained human HTML report."""

from __future__ import annotations

import re
from typing import Any, Mapping


_OMITTED = "[local path omitted]"
_PUBLIC_HTTP_URL = re.compile(r"(?i)\bhttps?://[^\s<>\"']+")
_MESSAGE_PLACEHOLDER_RATIO = re.compile(
    r"\{[A-Za-z_][A-Za-z0-9_]*\}/\{[A-Za-z_][A-Za-z0-9_]*\}"
)
_FILE_URI = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9_])file:(?=/{1,3}|[A-Za-z]:[\\/])"
)
_WINDOWS_DRIVE = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])(?:[A-Z]:[\\/])")
_UNC = re.compile(
    r"(?:^|[\s\"'=:(\[,;])(?:\\\\|//)[^\\/\s]+[\\/][^\\/\s]+"
)
# A path may follow punctuation in a diagnostic, but not a word character,
# another slash, or an HTML tag opener.  Leading slash runs are absolute-path
# candidates, so this preserves ``and//or`` and ``</section>`` while detecting
# ``diagnostic-///private/input.pdf``.
_POSIX = re.compile(r"(?<![A-Za-z0-9_</])/{1,}(?![>\s])[^\s\"'<>]*")


def contains_local_path(value: str) -> bool:
    """Return whether *value* contains a local absolute path or file URI.

    Complete HTTP(S) URLs are deliberately public identifiers, even when their
    URL path resembles a local path.  Other strings are checked conservatively
    because report data can contain free-form diagnostics.
    """
    # Remove public web URLs only from the detector probe.  This also handles
    # URLs embedded in prose while still finding a local path elsewhere in the
    # same string.
    probe = _PUBLIC_HTTP_URL.sub("", value)
    # Localized messages contain count ratios such as ``{success}/{calls}``.
    # Remove only that exact placeholder grammar; braces otherwise remain a
    # valid delimiter and cannot hide an absolute path.
    probe = _MESSAGE_PLACEHOLDER_RATIO.sub(
        lambda match: " " * len(match.group()), probe
    )
    return bool(
        _FILE_URI.search(probe)
        or _WINDOWS_DRIVE.search(probe)
        or _UNC.search(probe)
        or _POSIX.search(probe)
    )


def sanitize_local_paths(value: Any) -> Any:
    """Return a detached JSON-compatible value with local path values omitted.

    A path in an object key cannot be omitted without changing its audit shape,
    so it is rejected.  Values containing a path are replaced as a whole rather
    than attempting ambiguous partial rewrites.
    """
    if isinstance(value, str):
        return _OMITTED if contains_local_path(value) else value
    if isinstance(value, list):
        return [sanitize_local_paths(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_local_paths(item) for item in value]
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("human report privacy requires string object keys")
            if contains_local_path(key):
                raise ValueError("human report privacy cannot omit a local path object key")
            sanitized[key] = sanitize_local_paths(item)
        return sanitized
    return value


def reject_local_paths(value: Any) -> None:
    """Fail closed when caller-provided renderer data still contains a path."""
    if isinstance(value, str):
        if contains_local_path(value):
            raise ValueError("human report projection contains a local path")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            reject_local_paths(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("human report projection requires string object keys")
            if contains_local_path(key):
                raise ValueError("human report projection contains a local path object key")
            reject_local_paths(item)
