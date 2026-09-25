#!/usr/bin/env python3
# core/parse/extractors/docx.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""DOCX manuscript extractor."""

from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree as ET

try:
    from core.parse.parsing_common import RANGE_DASHES
except ImportError:  # standalone use, as the other extractors allow
    from parsing_common import RANGE_DASHES


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
EXTENSIONS = (".docx",)


def _docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    paragraphs = []
    for p in root.iter(f"{W}p"):
        buf = []
        for r in p.iter(f"{W}r"):
            rpr = r.find(f"{W}rPr")
            is_sup = False
            if rpr is not None:
                va = rpr.find(f"{W}vertAlign")
                if va is not None and va.get(f"{W}val") == "superscript":
                    is_sup = True
            text = "".join(t.text or "" for t in r.iter(f"{W}t"))
            if not text:
                continue
            if is_sup and re.fullmatch(rf"[\d,{RANGE_DASHES}]+", text.strip()):
                buf.append(f"⟦SUP:{text.strip()}⟧")
            else:
                buf.append(text)
        paragraphs.append("".join(buf))
    return "\n".join(paragraphs)


def extract(path: str, *, ocr_lang: str, ocr_notice, manuscript_ocr: bool):
    del ocr_lang, ocr_notice, manuscript_ocr
    # DOCX states which runs are raised (w:vertAlign), so superscript markers are
    # read, not inferred — unlike plain text, which cannot carry the information.
    return _docx_text(path), "docx", {"superscript_source": "markup"}
