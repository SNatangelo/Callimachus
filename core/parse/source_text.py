# core/parse/source_text.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical text preparation for fetched sources consumed by ``verify``.

This is deliberately narrower than :mod:`parse_manuscript`: a cited source is
evidence, not a manuscript to segment into claims or references.  It shares the
safe document-cleanup parts of the parser while keeping identity checks on the
unmodified extraction in the fetch layer.
"""

from __future__ import annotations

import re
import unicodedata

try:
    from core.parse import boilerplate, parser_text, parsing_common
except ImportError:  # pragma: no cover - direct-execution import path
    import boilerplate  # type: ignore
    import parser_text  # type: ignore
    import parsing_common  # type: ignore


# Increment this whenever a preparation change makes a cached verifier text
# incomparable with newly prepared source text.  The content store uses this
# value to decline unversioned or outdated prepared text rather than reuse it.
PREPARATION_VERSION = "source_prepare_v2"
_CATASTROPHIC_INPUT_CHARS = 2_000
_CATASTROPHIC_MIN_RETAINED_CHARS = 1_000
_CATASTROPHIC_RETAINED_RATIO = 0.10
_BIBLIO_ENTRY_RE = re.compile(r"^\s*(?:\[\s*\d+\s*\]|\d+[.)])\s+\S")


def _is_pdf_extraction(format_hint: str | None) -> bool:
    hint = (format_hint or "").lower()
    return any(token in hint for token in ("pdf", "pymupdf", "pdfminer", "pdftotext"))


def _drop_bibliography(text: str) -> tuple[str, bool, dict]:
    """Remove a terminal reference list but retain a post-bibliography appendix.

    The shared parser recognises multilingual reference headings.  A heading must
    occupy a whole line, so prose that merely mentions "references" is retained.
    """
    lines = text.splitlines()
    heading_re = parser_text.bibliography_heading_re()
    index = next((i for i, line in enumerate(lines) if heading_re.match(line)), None)
    if index is None:
        return text, False, {"boundary_line": None, "bibliography_entry_lines": 0}

    bibliography = lines[index + 1:]
    end = parsing_common._find_biblio_end_index(bibliography)
    tail = bibliography[end:] if end is not None else []
    body = lines[:index]
    entry_lines = sum(1 for line in bibliography if _BIBLIO_ENTRY_RE.match(line))
    return "\n".join(body + tail).strip(), True, {
        "boundary_line": index,
        "bibliography_entry_lines": entry_lines,
    }


def _structural_quality(text: str) -> dict:
    """Small language-agnostic facts suitable for before/after comparison."""
    chars = len(text)
    alpha = sum(char.isalpha() for char in text)
    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    nonblank_lines = sum(1 for line in text.splitlines() if line.strip())
    return {
        "chars": chars,
        "alpha_ratio": round(alpha / max(chars, 1), 4),
        "word_tokens": len(tokens),
        "nonblank_lines": nonblank_lines,
    }


def _catastrophic_retention(before: str, after: str, stage: str) -> list[str]:
    """Flag a structural cleanup stage that discarded nearly all useful text."""
    before_chars = len(before)
    after_chars = len(after)
    if before_chars < _CATASTROPHIC_INPUT_CHARS:
        return []
    if (
        after_chars < _CATASTROPHIC_MIN_RETAINED_CHARS
        or after_chars / max(before_chars, 1) < _CATASTROPHIC_RETAINED_RATIO
    ):
        return [f"catastrophic_{stage}_truncation"]
    return []


def _unsafe_bibliography_removal(before: str, after: str, boundary: dict) -> list[str]:
    """Return structural reasons to retain ``before`` instead of a bad cut.

    This intentionally does not infer document meaning.  A heading-like token can
    occur in navigation or a layout artefact; removing almost an entire useful
    document is worse than leaving a bibliography in the verifier context.
    """
    flags: list[str] = []
    flags.extend(_catastrophic_retention(before, after, "bibliography"))
    # A very early heading with no numbered bibliography structure is usually a
    # navigation/layout boundary, not a terminal reference list.  The existing
    # heading detector remains the sole heading vocabulary; this is shape-only.
    line_count = max(len(before.splitlines()), 1)
    boundary_line = boundary.get("boundary_line")
    if (
        isinstance(boundary_line, int)
        and boundary_line / line_count < 0.08
        and not boundary.get("bibliography_entry_lines")
    ):
        flags.append("implausible_bibliography_boundary")
    return flags


def prepare_for_verify(text: str, *, format_hint: str | None = None) -> tuple[str, dict]:
    """Return readable source prose for the verifier and its cleanup metadata.

    The caller retains the original text for identity/provenance.  PDF cleanup
    reuses the manuscript parser's conservative dehyphenation, ligature repair,
    and page-break handling; all source types then share boilerplate and
    bibliography removal.
    """
    original = text or ""
    prepared = unicodedata.normalize("NFC", original)
    preparation_flags: list[str] = []
    before_pdf_postprocess = prepared
    pdf_postprocess_applied = False
    if _is_pdf_extraction(format_hint):
        try:
            from core.parse.extractors import pdf as pdf_extractor
            pdf_candidate = pdf_extractor.postprocess(prepared, {})
            pdf_flags = _catastrophic_retention(
                before_pdf_postprocess, pdf_candidate, "pdf_postprocess"
            )
            if pdf_flags:
                preparation_flags.extend(pdf_flags)
                preparation_flags.append("pdf_postprocess_reverted")
            else:
                prepared = pdf_candidate
                pdf_postprocess_applied = True
        except Exception:
            # Fetch must never reject a source merely because optional parser
            # enrichment is unavailable; its native extractor remains usable.
            pass

    before_boilerplate = prepared
    furniture = boilerplate.boilerplate_lines(prepared)
    boilerplate_lines_removed = 0
    if furniture:
        boilerplate_candidate = boilerplate.strip_page_breaks(prepared, furniture)
        boilerplate_flags = _catastrophic_retention(
            before_boilerplate, boilerplate_candidate, "boilerplate"
        )
        if boilerplate_flags:
            preparation_flags.extend(boilerplate_flags)
            preparation_flags.append("boilerplate_removal_reverted")
        else:
            prepared = boilerplate_candidate
            boilerplate_lines_removed = len(furniture)
    before_bibliography = prepared
    candidate, bibliography_removed, boundary = _drop_bibliography(prepared)
    bibliography_flags = _unsafe_bibliography_removal(
        before_bibliography, candidate, boundary
    ) if bibliography_removed else []
    if bibliography_flags:
        # Safe degradation: preserving a little trailing bibliography costs less
        # than passing a tiny fragment to the verifier as if it were full text.
        prepared = before_bibliography
        bibliography_removed = False
        preparation_flags.extend(bibliography_flags)
        preparation_flags.append("bibliography_removal_reverted")
    else:
        prepared = candidate
    prepared = re.sub(r"[ \t]+\n", "\n", prepared)
    prepared = re.sub(r"\n{3,}", "\n\n", prepared).strip()
    return prepared, {
        "preparation_version": PREPARATION_VERSION,
        "original_char_count": len(original),
        "before_pdf_postprocess_char_count": len(before_pdf_postprocess),
        "pdf_postprocess_applied": pdf_postprocess_applied,
        "before_boilerplate_char_count": len(before_boilerplate),
        "before_bibliography_char_count": len(before_bibliography),
        "candidate_prepared_char_count": len(candidate),
        "prepared_char_count": len(prepared),
        "format_hint": format_hint,
        "bibliography_removed": bibliography_removed,
        "boilerplate_lines_detected": len(furniture),
        "boilerplate_lines_removed": boilerplate_lines_removed,
        "preparation_flags": sorted(set(preparation_flags)),
        "quality_before": _structural_quality(before_bibliography),
        "quality_after": _structural_quality(prepared),
        "bibliography_boundary": boundary,
    }
