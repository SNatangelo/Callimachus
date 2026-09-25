#!/usr/bin/env python3
# core/parse/extract.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
extract.py — text extraction from the manuscript, by format. Deterministic.

All format-specific readers live in core.parse.extractors/ and are dispatched through
the extractor registry. This module keeps only the shared PDF quality gate and
the manuscript OCR fallback used by the PDF extractor.
"""

import os

try:
    from core.parse import extractors
except ImportError:
    import extractors


def supported_extensions() -> tuple[str, ...]:
    return extractors.supported_extensions()


def __getattr__(name: str):
    if name == "SUPPORTED":
        return supported_extensions()
    raise AttributeError(name)


def _pdf_quality_ok(text: str) -> bool:
    """Same gate used below: enough characters, mostly letters (not binary noise)."""
    alpha = sum(c.isalpha() for c in text)
    return len(text) >= 200 and not (len(text) > 0 and alpha / len(text) < 0.4)


def extract_text(path: str, *, manuscript_ocr: bool = False,
                 ocr_lang: str = "eng", ocr_notice=None) -> tuple[str, str, dict]:
    """Extract unified text from a manuscript/source file."""
    ext = os.path.splitext(path)[1].lower()
    supported = supported_extensions()
    if ext not in supported:
        raise ValueError(f"unsupported format: {ext} (valid: {', '.join(supported)})")
    return extractors.extract_for(
        path,
        manuscript_ocr=manuscript_ocr,
        ocr_lang=ocr_lang,
        ocr_notice=ocr_notice,
    )


def probe_file(path: str) -> dict[str, str]:
    """Validate that file bytes match the extension-selected extractor."""
    return extractors.probe_for(path)


def _manuscript_ocr(path: str, ocr_lang: str, meta: dict, ocr_notice) -> tuple[str, dict]:
    """Automatic OCR fallback for a scanned MANUSCRIPT (the document being verified).

    Unlike a cited source - which is parked for opt-in OCR - the manuscript is the
    primary input, so when it has no readable text layer we OCR it directly. Still a
    recorded degradation: the method is stamped ``ocr:<backend>`` in meta.
    """
    if callable(ocr_notice):
        ocr_notice(meta.get("pdf_method"))
    try:
        from core.fetch.extraction import ocr as _ocr
    except ImportError:
        import ocr as _ocr
    try:
        ocr_text, method = _ocr.ocr_pdf(path, lang=ocr_lang)
    except (FileNotFoundError, RuntimeError) as e:
        raise ValueError(
            "manuscript PDF has no readable text layer and OCR failed: "
            f"{e}. Install the OCR backend (pip install -r requirements-ocr.txt) "
            "or supply a transcribed .txt/.md.")
    if not _pdf_quality_ok(ocr_text):
        raise ValueError(
            "manuscript PDF was OCR'd but the result failed the text-quality gate "
            f"(method={method}, chars={len(ocr_text)}); the scan is likely too poor. "
            "Supply a transcribed .txt/.md instead.")
    meta = dict(meta)
    meta["pdf_method"] = f"ocr:{method}"
    meta["ocr"] = True
    meta["ocr_lang"] = ocr_lang
    return ocr_text, meta
