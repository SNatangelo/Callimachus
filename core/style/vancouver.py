# core/style/vancouver.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Vancouver (ICMJE/AMA-like, numeric). Consistent with [n] citations."""
import re
from . import common as c


def check(entry: str, source_type: str) -> dict:
    d = []
    if not c.has_year(entry):
        d.append(c.dev("no_year", "Year not detected.", "error"))

    if source_type == "article":
        # Authors "Surname AB, Surname CD." without dots between initials.
        if not c.INITIALS_VANC.search(entry):
            d.append(c.dev("authors_format",
                           "Expected 'Surname AB' (initials without dots) at the start."))
        if re.search(r"[A-Z][a-zà-ÿ]+,\s+[A-Z]\.", entry):
            d.append(c.dev("authors_dotted",
                           "Dotted initials: Vancouver wants them without dots (e.g. 'Smith AB')."))
        if not c.PAGES.search(entry):
            d.append(c.dev("no_pages", "Page range not detected.", "warning"))
        if c.DOI.search(entry) and not re.search(r"doi:|https?://doi\.org", entry, re.I):
            d.append(c.dev("doi_prefix", "DOI present but without 'doi:' prefix or doi.org URL."))
    elif source_type == "book":
        if not re.search(r"\b(ed|edition|edizione)\b", entry, re.I) and not c.has_year(entry):
            d.append(c.dev("book_fields", "For a book, publisher/place/year expected."))
    elif source_type == "webpage":
        if not c.has_url(entry):
            d.append(c.dev("no_url", "Webpage without URL.", "error"))
        if not c.ACCESSED.search(entry):
            d.append(c.dev("no_accessed", "Access date missing ('cited'/'accessed')."))
    else:
        d.append(c.dev("unknown_type", "source_type undetermined: style cannot be fully checked.", "warning"))

    return c.result(d)
