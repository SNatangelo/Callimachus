#!/usr/bin/env python3
# core/parse/format_handlers/markdown.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Format handler for Markdown manuscripts.

Markdown has explicit structure (``##`` headings, list markers).  The
extractor strips formatting markers (``#``, ``**``, links) but leaves
the underlying text clean, with ``[1]`` entries that match the numbered
parsing path directly.

This family disables the PDF-oriented heuristics (``_find_biblio_end_index``,
``_truncate_bleed``) that the plain_text default applies, because markdown
bibliographies are self-contained sections with no prose bleed.
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
    from core.parse.format_handlers import plain_text
    import core.parse.parser_text as parser_text
except ImportError:
    from parsing_common import (
        _make_reference,
        _split_authoryear_references,
        _merge_standalone_doi_url,
    )
    import plain_text
    import parser_text

NAME = "markdown"


def is_table_row(sentence: str, markers: list) -> bool:
    """Markdown table rows: pipe ``|`` column separators, or a plain-text table.

    After ``markdown.py`` strips images, links, headings and bold markers,
    table pipes survive::

        Model | BLEU | Params
        Transformer [1] | 28.4 | 65M

    Two or more pipes strongly suggest a table row.  The 40 % alphabetic
    ceiling is intentionally tighter than LaTeX's 60 % — pipe characters
    also appear in prose (e.g. "See [1] | also [2]") but such sentences
    are always >50 % alpha.

    Pipes are how markdown writes a table, not how a markdown FILE always
    carries one: a paper converted out of a PDF brings its tables along as
    the aligned columns of numbers they were, with no pipe in sight.  Every
    plain-text table is also a valid markdown document, so the plain-text
    signal (decimal density) applies here too — markdown is a superset, and
    the handler for it cannot recognise less than the one it extends.
    """
    if not markers:
        return False
    if sentence.count("|") >= 2:
        alpha = sum(1 for c in sentence if c.isalpha())
        total = len(sentence) or 1
        if alpha / total < 0.40:
            return True
    return plain_text.is_table_row(sentence, markers)


def split_body_bibliography(text: str, meta=None) -> tuple[str, str, dict, int | None]:
    """Markdown body/bibliography split — no post-biblio bleed detection.

    After the extractor strips ``##`` markers, the ``References`` heading
    is a plain line.  Markdown manuscripts don't have appendices,
    acknowledgments, or prose sections after the reference list that could
    leak into the body, so ``_find_biblio_end_index`` is unnecessary.
    """
    body, biblio, debug, _cut_idx = _cut_at_last_biblio_heading(text)
    return body, biblio, debug, None


def parse_references(biblio: str, meta=None) -> list[dict]:
    """Markdown reference parsing with explicit bullet-list detection.

    Three parsing paths in priority order:

    1. **Numbered** — ``[1]``, ``1.`` markers (same logic as the default).
    2. **Bullet list** — ``-`` / ``*`` / ``+`` markers → structural split
       at each bullet boundary; one reference per list item.  This path
       fires **before** the author-year heuristic because markdown list
       items are unambiguous reference boundaries.
    3. **Author-year** — heuristic fallback when neither numbered nor
       bullet markers are present.
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
        # --- Bullet-list detection (before author-year fallback) ---
        bullet_matches = list(re.finditer(r"(?m)^[ \t]{0,3}[-*+][ \t]+", text))
        if len(bullet_matches) >= 2:
            for i, m in enumerate(bullet_matches):
                start = m.end()
                end = bullet_matches[i + 1].start() if i + 1 < len(bullet_matches) else len(text)
                raw = " ".join(text[start:end].split())
                refs.append(_make_reference(i + 1, raw))
            return _merge_standalone_doi_url(refs)

        # --- Author-year fallback ---
        ay_refs = _split_authoryear_references(text)
        if len(ay_refs) >= 2:
            # No _truncate_bleed — markdown references don't have prose tails.
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

    # No _truncate_bleed — markdown entries are clean, no prose bleed to strip.
    for num, raw in raw_entries:
        refs.append(_make_reference(num, raw))
    return _merge_standalone_doi_url(refs)
