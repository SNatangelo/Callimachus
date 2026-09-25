#!/usr/bin/env python3
# core/parse/extractors/markdown.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Markdown manuscript extractor."""

from __future__ import annotations

import re

try:
    from core.parse.extractors import superscript
except ImportError:
    import superscript


EXTENSIONS = (".md", ".markdown")

# A citation marker is very often a link to its entry in the reference list:
# "[1](#ref-1)".  Unwrapping that to the link TEXT alone would leave a bare "1" and
# throw the marker away with the brackets, so a numeric link keeps them.
_CITATION_LINK_TEXT_RE = re.compile(superscript.NUMBER_RUN)


def _unwrap_link(m: "re.Match") -> str:
    text = m.group(1)
    return f"[{text}]" if _CITATION_LINK_TEXT_RE.fullmatch(text.strip()) else text


def _md_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    txt = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", txt)
    txt = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _unwrap_link, txt)
    txt = re.sub(r"(?m)^#{1,6}\s*", "", txt)
    txt = re.sub(r"(?m)^(?:-{3,}|\*{3,})\s*$", "", txt)
    txt = txt.replace("**", "").replace("__", "")
    return txt


def extract(path: str, *, ocr_lang: str, ocr_notice, manuscript_ocr: bool):
    del ocr_lang, ocr_notice, manuscript_ocr
    # Markdown, unlike plain text, CAN raise a digit — three ways, and the converters
    # use all three (see superscript.py).  Whether this particular file did is a
    # question for the file, so we ask it instead of assuming from the extension.
    text, has_superscripts = superscript.convert(_md_text(path))
    # Superscripts of something other than a number ("21<sup>st</sup>") are left behind
    # by the conversion; they are not citations, and the tags are not prose.
    text = re.sub(r"</?sup>", "", text, flags=re.I)
    return text, "markdown", {
        "superscript_source": "markup" if has_superscripts else None}
