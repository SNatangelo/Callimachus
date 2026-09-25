# core/style/chicago_nb.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Chicago NB (Notes & Bibliography) — numeric superscript in-text citations,
full bibliography entries at the end.

Format (journal article):
  Surname, Firstname. "Article Title." Journal Volume, no. Issue (Year): Pages. https://doi.org/...  # noqa: E501

Format (book):
  Surname, Firstname. Book Title. Publisher, Year.

Format (webpage):
  Surname, Firstname. "Page Title." Website Name. Accessed Month Day, Year. URL.

Note: Unlike Chicago Author-Date, the year appears AFTER the journal info
and NOT in parentheses right after the author name.
"""
import re
from . import common as c


def check(entry: str, source_type: str) -> dict:
    d = []

    # ── Year ──
    if not c.has_year(entry):
        d.append(c.dev("no_year", "Year not detected.", "error"))

    # ── Author format: "Surname, Firstname" at the start ──
    if not re.search(r"[A-Z][a-zà-ÿ]+,\s+[A-Z][a-zà-ÿ]", entry):
        d.append(c.dev(
            "authors_format",
            "Expected 'Surname, Firstname' with full given name at the start (Chicago NB)."  # noqa: E501
        ))

    if source_type == "article":
        # Article title in double quotes
        if '"' not in entry and "“" not in entry and "„" not in entry:
            d.append(c.dev(
                "title_quotes",
                "Article title expected in quotation marks (Chicago NB)."
            ))
        # Volume and issue: "99, no. 394" (canonical NB) or "12 (3)"
        if not (re.search(r"\b\d+\s*,\s*no\.\s+\d+", entry, re.I)
                or re.search(r"\b\d+\s*\(\s*\d+\s*\)", entry)):
            d.append(c.dev(
                "vol_issue",
                "Expected 'Volume, no. Issue' format, e.g. '99, no. 394'.",
                "warning"
            ))
        # Pages
        if not c.PAGES.search(entry):
            d.append(c.dev(
                "no_pages",
                "Page range not detected.",
                "warning"
            ))
        # DOI as URL if DOI present
        if c.DOI.search(entry) and not c.DOI_URL.search(entry):
            d.append(c.dev(
                "doi_as_url",
                "DOI should be presented as https://doi.org/... URL (Chicago NB).",
                "warning"
            ))

    elif source_type == "book":
        # Title italics not verifiable from plain text
        d.append(c.dev(
            "italics_unverifiable",
            "Book title italics cannot be verified from plain text.",
            "note"
        ))
        # Publisher + year at end of entry
        if not re.search(r"(?:Press|Books|Publishing|University|Inc\.|Editore)\s*,\s*(?:19|20)\d{2}", entry, re.I):
            if c.has_year(entry):
                d.append(c.dev(
                    "publisher",
                    "Publisher name not detected before the year.",
                    "warning"
                ))

    elif source_type == "webpage":
        if not c.has_url(entry):
            d.append(c.dev("no_url", "Webpage without URL.", "error"))
        if not c.ACCESSED.search(entry):
            d.append(c.dev(
                "no_accessed",
                "Access date expected, e.g. 'Accessed January 1, 2024'."
            ))

    else:
        d.append(c.dev(
            "unknown_type",
            "source_type undetermined: style cannot be fully checked.",
            "warning"
        ))

    return c.result(d)
