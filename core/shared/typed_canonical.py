# core/shared/typed_canonical.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Versioned typed canonical bytes for new immutable records.

This format is intentionally small and encode-only.  It is not a general
serialization format: accepting only a closed set of built-in values keeps
hash inputs deterministic and makes unsupported values fail closed.
"""
from __future__ import annotations

import math
import struct
from typing import Any


TYPED_CANONICAL_MARKER = b"callimachus:typed-canonical:v2\x00"


class TypedCanonicalError(ValueError):
    """A value cannot be represented by the typed canonical format."""


def encode(value: Any) -> bytes:
    """Return the versioned, tagged, length-prefixed canonical bytes for *value*.

    Dict keys are limited to valid UTF-8 strings and sorted by their encoded
    bytes.  Lists and tuples deliberately have distinct tags.
    """
    return TYPED_CANONICAL_MARKER + _encode(value)


def _frame(tag: bytes, payload: bytes) -> bytes:
    return tag + str(len(payload)).encode("ascii") + b":" + payload


def _utf8(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TypedCanonicalError("string is not valid UTF-8") from exc


def _encode(value: Any) -> bytes:
    value_type = type(value)
    if value is None:
        return _frame(b"n", b"")
    if value_type is bool:
        return _frame(b"b", b"1" if value else b"0")
    if value_type is int:
        return _frame(b"i", str(value).encode("ascii"))
    if value_type is float:
        if not math.isfinite(value):
            raise TypedCanonicalError("float must be finite")
        return _frame(b"f", struct.pack(">d", value))
    if value_type is str:
        return _frame(b"s", _utf8(value))
    if value_type is list:
        return _frame(b"l", b"".join(_encode(item) for item in value))
    if value_type is tuple:
        return _frame(b"t", b"".join(_encode(item) for item in value))
    if value_type is dict:
        entries: list[tuple[bytes, Any]] = []
        for key, item in value.items():
            if type(key) is not str:
                raise TypedCanonicalError("dict keys must be strings")
            entries.append((_utf8(key), item))
        entries.sort(key=lambda entry: entry[0])
        payload = b"".join(
            _frame(b"k", key_bytes) + _encode(item)
            for key_bytes, item in entries
        )
        return _frame(b"d", payload)
    if value_type is bytes:
        raise TypedCanonicalError("bytes are not supported")
    if value_type is set or value_type is frozenset:
        raise TypedCanonicalError("sets are not supported")
    raise TypedCanonicalError(f"unsupported canonical value type: {value_type.__name__}")
