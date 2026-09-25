#!/usr/bin/env python3
# core/parse/format_handlers/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Format-handler registry — plug-and-play dispatch for format-specific parser
behaviour (table-row detection, etc.).

Each module in this package exports:

* ``NAME`` or ``FORMATS`` — which format label(s) it handles
  (``"latex"``, ``"pdf"``, ``"txt"``, ``"docx"``, ``"markdown"``).
* ``is_table_row(sentence: str, markers: list) -> bool`` —
  format-specific gate for table rows that should be suppressed.

The registry auto-discovers modules and builds a ``fmt → module`` map at
import time, so adding a new format handler is a single new file — the
parser core never needs to change.
"""

from __future__ import annotations

import importlib
import pkgutil

_REGISTRY: dict[str, object] | None = None


def _discover() -> dict[str, object]:
    registry: dict[str, object] = {}
    for module_info in pkgutil.iter_modules(__path__):
        name = module_info.name
        if name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{name}")
        formats = getattr(module, "FORMATS", None)
        if formats is None:
            single = getattr(module, "NAME", None)
            if single is not None:
                formats = [single]
        if not formats:
            continue
        for fmt in formats:
            registry[str(fmt)] = module
    return registry


def _get_registry() -> dict[str, object]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _discover()
    return _REGISTRY


def is_table_row(sentence: str, markers: list, fmt: str) -> bool:
    """Format-aware table-row detection.

    Returns ``True`` when the sentence is recognised as a table row (not a
    prose citation) and its markers should be suppressed.

    *fmt* is the format label from the extractor (``"latex"``, ``"pdf"``,
    ``"txt"``, ``"docx"``, ``"markdown"``).  Unknown formats fall back to
    the ``"txt"`` handler.
    """
    handler = _get_registry().get(fmt)
    if handler is None:
        # Unknown format → fall back to plain-text handler
        handler = _get_registry().get("txt")
        if handler is None:
            return False
    fn = getattr(handler, "is_table_row", None)
    if callable(fn):
        return bool(fn(sentence, markers))
    return False


def reset_for_tests() -> None:
    global _REGISTRY
    _REGISTRY = None


# ---------------------------------------------------------------------------
# Family facade — pluggable dispatch
# ---------------------------------------------------------------------------

class Family:
    """Lightweight container for the three parsing hooks + one detection hook.

    Each attribute is a callable.  Families are built by ``select(fmt)``; the
    defaults come from ``parsing_common`` and individual format-handler modules
    can override one or more hooks.
    """
    __slots__ = ("split_body_bibliography", "segment_sentences",
                 "parse_references", "is_table_row")

    def __init__(self, *, split_body_bibliography, segment_sentences,
                 parse_references, is_table_row):
        self.split_body_bibliography = split_body_bibliography
        self.segment_sentences = segment_sentences
        self.parse_references = parse_references
        self.is_table_row = is_table_row


def select(fmt: str) -> Family:
    """Return the ``Family`` of parsing hooks for *fmt*.

    *fmt* is the format label from the extractor (``"latex"``, ``"pdf"``,
    ``"txt"``, ``"docx"``, ``"markdown"``).  Unknown formats fall back to the
    ``"txt"`` handler.

    Each hook defaults to the shared implementation in ``parsing_common``;
    format-handler modules can override whichever hooks they need.
    """
    # Lazy import to avoid circular imports at package-load time.
    try:
        from core.parse.parsing_common import (
            default_parse_references,
            default_segment_sentences,
            default_split_body_bibliography,
        )
    except ImportError:
        from parsing_common import (
            default_parse_references,
            default_segment_sentences,
            default_split_body_bibliography,
        )

    handler = _get_registry().get(fmt)
    if handler is None:
        handler = _get_registry().get("txt")  # fallback

    def _resolve(attr_name: str, default_fn):
        """Pick handler's override or fall back to *default_fn*."""
        if handler is not None:
            override = getattr(handler, attr_name, None)
            if callable(override):
                return override
        return default_fn

    return Family(
        split_body_bibliography=_resolve("split_body_bibliography",
                                         default_split_body_bibliography),
        segment_sentences=_resolve("segment_sentences",
                                   default_segment_sentences),
        parse_references=_resolve("parse_references",
                                  default_parse_references),
        is_table_row=_resolve("is_table_row",
                              lambda s, m, f=fmt: is_table_row(s, m, f)),
    )
