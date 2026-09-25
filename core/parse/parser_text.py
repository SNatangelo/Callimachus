#!/usr/bin/env python3
# core/parse/parser_text.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared prose-level parser knobs."""

from __future__ import annotations

import json
import os
import re


PROTECTED_ABBREVIATIONS = (
    "al.",
    "approx.",
    "ca.",
    "cf.",
    "co.",
    "dr.",
    "e.g.",
    "ed.",
    "eds.",
    "et al.",
    "etc.",
    "fig.",
    "figs.",
    "i.e.",
    "inc.",
    "ltd.",
    "max.",
    "min.",
    "mr.",
    "mrs.",
    "ms.",
    "no.",
    "p.",
    "pp.",
    "prof.",
    "sec.",
    "st.",
    "vol.",
    "vs.",
)

def _load_bibliography_headings() -> tuple[str, ...]:
    path = os.path.join(os.path.dirname(__file__), "config",
                        "bibliography_headings.json")
    try:
        with open(path, encoding="utf-8") as f:
            headings = json.load(f).get("bibliography_headings", [])
    except (OSError, ValueError):
        headings = []
    headings = tuple(h.lower() for h in headings if h)
    return headings or ("references", "bibliography", "works cited")


# Section-heading words that mark the start of a reference list (multilingual,
# loaded from config/bibliography_headings.json — edit there, not here).
BIBLIOGRAPHY_HEADINGS = _load_bibliography_headings()


def _heading_re(headings: tuple[str, ...]) -> re.Pattern:
    alt = "|".join(re.escape(h).replace(r"\ ", r"\s+") for h in headings)
    return re.compile(r"(?im)^\s*[\W_]{0,3}(?:" + alt + r")\s*[:.]?\s*$")


def bibliography_heading_re() -> re.Pattern:
    """A line that is a bibliography heading: any BIBLIOGRAPHY_HEADINGS entry,
    tolerating a leading section-marker glyph ("■References") and a trailing
    ':'/'.'.  Shared by the PDF link-layer and the column-aware re-extraction so
    heading recognition has one source of truth."""
    return _heading_re(BIBLIOGRAPHY_HEADINGS)


def _load_further_reading_headings() -> tuple[str, ...]:
    path = os.path.join(os.path.dirname(__file__), "config",
                        "bibliography_headings.json")
    try:
        with open(path, encoding="utf-8") as f:
            headings = json.load(f).get("further_reading_headings", [])
    except (OSError, ValueError):
        headings = []
    return tuple(h.lower() for h in headings if h) or ("further reading",)


# The heading of the SECOND list some publishers print after the references —
# recommended works the authors never cite (config/bibliography_headings.json).
FURTHER_READING_HEADINGS = _load_further_reading_headings()


def further_reading_heading_re() -> re.Pattern:
    return _heading_re(FURTHER_READING_HEADINGS)

# Section headings that signal the END of a bibliography region.
# Bilingual (English + Italian).  Used by _find_biblio_end_index().
_POST_BIBLIO_HEADINGS = frozenset({
    # English
    "appendix", "appendices", "acknowledgments", "acknowledgements",
    "author contributions", "author affiliations", "supplementary material",
    "supplemental information", "data availability", "data availability statement",
    "code availability", "competing interests", "conflict of interest",
    "conflict of interest statement", "funding", "disclosure", "declarations",
    "ethics statement", "additional information", "supporting information",
    # Italian
    "appendice", "appendici", "ringraziamenti", "contributi degli autori",
    "materiale supplementare", "informazioni supplementari",
    "disponibilità dei dati", "dichiarazione sui dati",
    "conflitto di interessi", "dichiarazione di conflitto di interessi",
    "finanziamenti", "fondi", "dichiarazioni", "dichiarazione etica",
})

# Regex for appendix-variant prefixes ("Appendix A", "Appendix B. Results", etc.).
# Used by _find_biblio_end_index() as a supplement to _POST_BIBLIO_HEADINGS exact match.
_APPENDIX_PREFIX_RE = re.compile(r"^appendix\b", re.IGNORECASE)
