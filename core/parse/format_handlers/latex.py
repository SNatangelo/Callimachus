#!/usr/bin/env python3
# core/parse/format_handlers/latex.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Format handler for LaTeX manuscripts.

LaTeX output from the extractor is already clean and structured:
``\\bibitem`` entries become ``[n] text``, headings are deterministic, and
there are no PDF artifacts (line-break hyphens, prose bleed, orphan DOIs).
This family **trusts** the extractor and disables the PDF-oriented heuristics
that the plain_text default applies.
"""

from __future__ import annotations

import re

try:
    from core.parse.parsing_common import (
        _cut_at_last_biblio_heading,
        _make_reference,
        _split_authoryear_references,
        _merge_standalone_doi_url,
    )
    import core.parse.parser_text as parser_text
except ImportError:
    from parsing_common import (
        _make_reference,
        _split_authoryear_references,
        _merge_standalone_doi_url,
    )
    import parser_text

NAME = "latex"


def is_table_row(sentence: str, markers: list) -> bool:
    """LaTeX table rows: ``&`` column separators + ``\\\\`` row endings.

    After ``tex.py`` strips ``\\begin{table}/\\end{tabular}`` etc., the
    cell content remains with ``&`` between columns and ``\\\\`` at row
    boundaries.  These artifacts never appear in natural-language prose
    extracted from LaTeX.

    Two-tier detection:

    * **< 30 % alpha** — catches sparse rows where ``\\\\`` was stripped
      during extraction (Signal B, formerly in the numeric scheme).
    * **< 60 % alpha** — requires explicit ``\\\\`` row boundaries.
      Even text-heavy result tables stay below this threshold while
      any genuine prose sentence with a stray ``\\\\`` (e.g. a LaTeX
      code snippet) exceeds it.
    """
    if sentence.count("&") >= 2:
        alpha = sum(1 for c in sentence if c.isalpha())
        total = len(sentence) or 1
        ratio = alpha / total
        # Very sparse — & separators alone are enough signal
        if ratio < 0.30:
            return True
        # Moderate text load — require explicit \\\\ row boundaries
        if "\\\\" in sentence and ratio < 0.60:
            return True
    return False


def split_body_bibliography(text: str, meta=None) -> tuple[str, str, dict, int | None]:
    """LaTeX body/bibliography split — no post-biblio bleed detection.

    The extractor always places the ``References`` heading at the end of
    the document, immediately before the bibliography entries.  There are
    no appendices, acknowledgments, or prose sections after the reference
    list that could leak into the body, so ``_find_biblio_end_index`` is
    unnecessary.
    """
    body, biblio, debug, _cut_idx = _cut_at_last_biblio_heading(text)
    return body, biblio, debug, None


def parse_references(biblio: str, meta=None) -> list[dict]:
    """LaTeX reference parsing — clean numbered path without PDF heuristics.

    Skips ``_truncate_bleed`` (prose-bleed detection is a PDF artifact that
    never appears in LaTeX output) and relies on the fact that the extractor
    already converted ``\\bibitem{key}`` to ``[n]`` entries.

    The author-year fallback path is preserved for mixed-style bibliographies.
    """
    text = biblio.strip()
    if not text:
        return []

    # --- Numbered entry detection (same logic as the default) ---
    entry_re = re.compile(r"(?m)^\s*(?:\[(\d+)\]|(\d+)[.\)])\s+")
    matches = list(entry_re.finditer(text))
    refs: list[dict] = []

    _numbered_ref_ok = False
    if matches:
        first_num = int(matches[0].group(1) or matches[0].group(2))
        if first_num <= 20:
            _numbered_ref_ok = True
        elif len(matches) >= 5:
            nums = [int(m.group(1) or m.group(2)) for m in matches[:10]]
            gaps = sum(1 for a, b in zip(nums, nums[1:]) if b - a not in (1, 2))
            if gaps <= 1:
                _numbered_ref_ok = True
    if not _numbered_ref_ok:
        matches = []

    if not matches:
        # --- Author-year fallback ---
        ay_refs = _split_authoryear_references(text)
        if len(ay_refs) >= 2:
            # No _truncate_bleed — LaTeX references don't have prose tails.
            for i, raw in enumerate(ay_refs, 1):
                refs.append(_make_reference(i, raw))
            return _merge_standalone_doi_url(refs)
        # Last-resort: one entry per non-empty line.
        lines = [l for l in text.split("\n") if l.strip()]
        for i, line in enumerate(lines, 1):
            refs.append(_make_reference(i, line.strip()))
        return _merge_standalone_doi_url(refs)

    # --- Numbered path ---
    raw_entries: list[tuple[int, str]] = []
    for i, m in enumerate(matches):
        num = int(m.group(1) or m.group(2))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        raw_entries.append((num, " ".join(text[start:end].split())))

    # No _truncate_bleed — LaTeX entries are clean, no prose bleed to strip.
    for num, raw in raw_entries:
        refs.append(_make_reference(num, raw))
    return _merge_standalone_doi_url(refs)
