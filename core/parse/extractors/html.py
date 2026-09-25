#!/usr/bin/env python3
# core/parse/extractors/html.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""HTML manuscript extractor.

An HTML article page is treated like Markdown, not like plain text: it CAN
raise a digit (``<sup>1</sup>``, or a numeric link to a bibliography entry,
both preserved verbatim by :mod:`core.parse.html_text`), so whether THIS file
did is a question for the document, not the extension — see
:mod:`core.parse.extractors.superscript`.

The heavy lifting — finding the article inside the page, dropping chrome,
detecting a login/challenge wall or an empty JS-rendered body, and keeping a
structured bibliography when the markup has one — is Phase 1's job
(:func:`core.parse.html_text.to_canonical_text`).  This module only adapts
that result to the extractor registry's ``(text, fmt, meta)`` contract.
"""

from __future__ import annotations

import re

try:
    from core.parse import html_text
except ImportError:  # pragma: no cover - standalone execution
    import html_text  # type: ignore

try:
    from core.parse.extractors import superscript
except ImportError:  # pragma: no cover - standalone execution
    import superscript  # type: ignore


EXTENSIONS = (".html", ".htm", ".xhtml")


def extract(path: str, *, ocr_lang: str, ocr_notice, manuscript_ocr: bool):
    del ocr_lang, ocr_notice, manuscript_ocr
    with open(path, "rb") as f:
        raw = f.read()
    result = html_text.to_canonical_text(raw)
    outcome = result["outcome"]

    if outcome == "challenge_or_login_page":
        raise ValueError(
            "HTML input is a login/challenge page, not the article — supply "
            "the article HTML or a PDF")
    if outcome == "empty_body":
        raise ValueError(
            "HTML body is empty (JS-rendered page?) — save the fully-rendered "
            "HTML or supply a PDF")

    # HTML, like Markdown, CAN raise a digit (<sup>, or a numeric anchor that
    # html_text already rebracketed to "[n]"): ask the document rather than
    # assume from the extension.
    text, has_superscripts = superscript.convert(result["text"])
    # Superscripts of something other than a number ("21<sup>st</sup>") are left
    # behind by the conversion; they are not citations, and the tags are not prose.
    text = re.sub(r"</?sup>", "", text, flags=re.I)

    meta = {
        "superscript_source": "markup" if has_superscripts else None,
        "html_container": result["container"],
    }
    if outcome == "abstract_only":
        # Not fatal here: the manuscript path wants whatever text there is, and
        # the reference-coverage gate (which sees the whole document, not just
        # this flag) will catch a genuinely truncated parse on its own.
        meta["html_outcome"] = outcome
    return text, "html", meta
