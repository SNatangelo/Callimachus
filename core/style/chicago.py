# core/style/chicago.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Chicago (author-date variant)."""
import re
from . import common as c


def check(entry: str, source_type: str) -> dict:
    d = []
    if not c.has_year(entry):
        d.append(c.dev("no_year", "Year not detected.", "error"))
    # Chicago author-date: "Surname, Firstname. Year."
    if not re.search(r"[A-Z][a-zà-ÿ]+,\s+[A-Z][a-zà-ÿ]+", entry):
        d.append(c.dev("authors_format",
                       "Expected 'Surname, Firstname' in full (Chicago)."))

    if source_type == "article":
        # Article title in double quotes.
        if '"' not in entry and "“" not in entry:
            d.append(c.dev("title_quotes", "Article title expected in quotation marks (Chicago)."))
        if not re.search(r"\b\d+\s*\(\s*\d+\s*\)", entry):
            d.append(c.dev("vol_issue", "Volume(issue) not detected.", "warning"))
    elif source_type == "book":
        # Book title in italics: not detectable from plain text -> note only.
        d.append(c.dev("italics_unverifiable",
                       "Title italics cannot be verified from plain text.", "note"))
    elif source_type == "webpage":
        if not c.has_url(entry):
            d.append(c.dev("no_url", "Webpage without URL.", "error"))
        if not c.ACCESSED.search(entry):
            d.append(c.dev("no_accessed", "Access date not detected."))
    else:
        d.append(c.dev("unknown_type", "source_type undetermined: style cannot be fully checked.", "warning"))

    return c.result(d)
