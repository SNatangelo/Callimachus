#!/usr/bin/env python3
# core/parse/format_handlers/html.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Format handler for HTML manuscripts.

``core.parse.html_text`` already normalizes an HTML page to clean prose plus
an (optional) numbered ``References`` block in the same shape Markdown's
extractor produces — no residual ``#`` markers, no link syntax, a self-
contained bibliography with no post-reference prose bleed.  That means the
plain-text defaults in ``parsing_common`` (table-row detection, sentence
segmentation, body/bibliography split, reference parsing) already work; this
module exists as an explicit registry entry rather than relying on the
``select()`` unknown-format fallback, so ``fmt == "html"`` is a documented,
intentional choice and not an accident of "we forgot to write a handler".

Add overrides here only if a golden/parse test reveals HTML needs one.
"""

from __future__ import annotations

NAME = "html"
