#!/usr/bin/env python3
# core/fetch/extraction/validate.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Content validation helpers — text quality and HTML fulltext classification."""

from __future__ import annotations

try:
    from core.fetch.extraction import pdf as _pdf
    from core.fetch.extraction import fetch_html as _fetch_html
except ImportError:
    import pdf as _pdf
    import fetch_html as _fetch_html


def _text_ok(text: str) -> bool:
    return _pdf._quality(text)


def _html_fulltext_ok(text: str) -> bool:
    return _fetch_html.html_fulltext_ok(text)
