#!/usr/bin/env python3
# core/parse/extractors/txt.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Plain-text manuscript extractor."""

from __future__ import annotations

try:
    from core.parse.extractors import superscript
except ImportError:
    import superscript


EXTENSIONS = (".txt",)


def extract(path: str, *, ocr_lang: str, ocr_notice, manuscript_ocr: bool):
    del ocr_lang, ocr_notice, manuscript_ocr
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    # Plain text has no way to RAISE a digit, but it can still hold one that was already
    # raised when it was written: the Unicode superscripts (¹²³) are ordinary characters
    # and survive here like any other.  So ask the file rather than the extension — a
    # .txt that cites "in all cases.¹²" has its markers, and only a .txt that shows us
    # none of them is blind.
    #
    # Blind is the dangerous state, and it is dangerous silently: a paper whose
    # superscripts were flattened to "in all cases.12" reads the same as "P = .12", and
    # its citations are not obscured but destroyed.  Declaring the incapacity (rather
    # than staying quiet) is what lets the parser refuse such a document instead of
    # reporting a clean parse of 40% of it.
    text, has_superscripts = superscript.convert(text)
    return text, "txt", {"superscript_source": "markup" if has_superscripts else None}
