# core/style/apa7.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""APA 7th (author-date)."""
import re
from . import common as c


def check(entry: str, source_type: str) -> dict:
    d = []
    # APA wants year in parentheses near authors: "Author, A. (2020)."
    if not re.search(r"\(\s*(1[5-9]\d{2}|20\d{2})[a-z]?\s*\)", entry):
        d.append(c.dev("year_parens", "Year not in parentheses as required by APA: '(2020).'", "error"))
    if not c.INITIALS_DOTTED.search(entry):
        d.append(c.dev("authors_format",
                       "Expected 'Surname, A. B.' with dotted initials (APA)."))

    if source_type == "article":
        if not c.has_doi(entry):
            d.append(c.dev("no_doi", "APA 7 requires the DOI as URL (https://doi.org/...) if available."))
        elif not c.DOI_URL.search(entry):
            d.append(c.dev("doi_as_url", "DOI present but not as https://doi.org/... URL.", "warning"))
        if not re.search(r"\b\d+\s*\(\s*\d+\s*\)", entry):
            d.append(c.dev("vol_issue", "Volume(issue) not detected, e.g. '12(3)'.", "warning"))
    elif source_type == "book":
        if not re.search(r"[A-Z][a-zà-ÿ].*\.\s*$", entry):
            d.append(c.dev("publisher", "Final publisher not detected (APA: '... Publisher.')."))
    elif source_type == "webpage":
        if not c.has_url(entry):
            d.append(c.dev("no_url", "Webpage without URL.", "error"))
    else:
        d.append(c.dev("unknown_type", "source_type undetermined: style cannot be fully checked.", "warning"))

    return c.result(d)
