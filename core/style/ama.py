# core/style/ama.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""AMA (American Medical Association) — numeric, like Vancouver but with key
differences: year after the journal preceded by ';' not '.', and DOI with
'doi:' prefix rather than a URL.

Format (article):
  Surname AB, Surname CD. Article title in sentence case. Abbrev J Name.
  2020;12(3):200-210. doi:10.xxxx/xxxx

Format (book):
  Surname AB. Book Title. Publisher; Year.

Format (webpage):
  Surname AB. Page Title. Website Name. Published/Updated Date. Accessed
  Date. URL.
"""
import re
from . import common as c


def check(entry: str, source_type: str) -> dict:
    d = []
    if not c.has_year(entry):
        d.append(c.dev("no_year", "Year not detected.", "error"))

    if source_type == "article":
        # Authors "Surname AB, Surname CD." without dots between initials
        if not c.INITIALS_VANC.search(entry):
            d.append(c.dev("authors_format",
                           "Expected 'Surname AB' (initials without dots) at the start."))
        # Volume-issue-pages: Year;Volume(Issue):Pages
        if not re.search(r"\d{4};\d+\s*\(\s*\d+\s*\)\s*:\s*\d+", entry):
            d.append(c.dev("vol_issue_pages",
                           "Expected 'Year;Volume(Issue):Pages' format, e.g. '2020;12(3):200-210'."))
        # DOI must use 'doi:' prefix, not a bare DOI or doi.org URL
        if c.DOI.search(entry) and not re.search(r"\bdoi:\s*10\.", entry, re.I):
            d.append(c.dev("doi_prefix",
                           "DOI should be prefixed 'doi:' (e.g. 'doi:10.xxxx/xxxx')."))
    elif source_type == "book":
        pass
    elif source_type == "webpage":
        if not c.has_url(entry):
            d.append(c.dev("no_url", "Webpage without URL.", "error"))
        if not c.ACCESSED.search(entry):
            d.append(c.dev("no_accessed", "Access date missing ('cited'/'accessed')."))
    else:
        d.append(c.dev("unknown_type",
                       "source_type undetermined: style cannot be fully checked.", "warning"))

    return c.result(d)
