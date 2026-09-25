# core/style/ieee.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""IEEE — numeric citation style with distinctive format: initials before
surname, article title in double quotes, labeled volume/issue/pages.

Format (article):
  [1] F. Surname and G. Surname, "Article title," Abbrev. J. Name,
  vol. 12, no. 3, pp. 200-210, Mar. 2020, doi: 10.xxxx/xxxx.

Format (book):
  [1] A. Author, Book Title. Publisher, Year.

Format (webpage):
  [1] A. Author. "Page Title." Website. Accessed: Jan. 1, 2024. [Online].
  Available: URL
"""
import re
from . import common as c

# IEEE author format: "F. Surname" — one or more initial-dot pairs before surname
IEEE_AUTHOR = re.compile(r"^(?:\[\d+\]\s*)?(?:[A-Z]\.\s+)+[A-Z][a-z]+")
IEEE_VOL = re.compile(r"\bvol\.\s*\d+", re.IGNORECASE)
IEEE_NO = re.compile(r"\bno\.\s*\d+", re.IGNORECASE)
IEEE_PP = re.compile(r"\bpp?\.\s*\d+", re.IGNORECASE)


def check(entry: str, source_type: str) -> dict:
    d = []
    if not c.has_year(entry):
        d.append(c.dev("no_year", "Year not detected.", "error"))

    if source_type == "article":
        # Author format: "F. Surname" (initials before surname)
        if not IEEE_AUTHOR.search(entry):
            d.append(c.dev("authors_format",
                           "Expected 'F. Surname' (initial-dot + surname) at the start."))
        # Article title in double quotes
        if '"' not in entry and "“" not in entry:
            d.append(c.dev("title_quotes",
                           "Article title expected in quotation marks (IEEE)."))
        # vol. N label
        if not IEEE_VOL.search(entry):
            d.append(c.dev("vol_label", "Expected 'vol. N' volume label (IEEE)."))
        # no. N label
        if not IEEE_NO.search(entry):
            d.append(c.dev("no_label", "Expected 'no. N' issue label (IEEE)."))
        # pp. N pages
        if not IEEE_PP.search(entry):
            d.append(c.dev("pp_label", "Expected 'pp. N' page label (IEEE)."))
        # DOI with 'doi:' prefix
        if c.DOI.search(entry) and not re.search(r"\bdoi:\s*10\.", entry, re.I):
            d.append(c.dev("doi_prefix",
                           "DOI should be prefixed 'doi:' (IEEE)."))
    elif source_type == "book":
        d.append(c.dev("italics_unverifiable",
                       "Book title italics cannot be verified from plain text.", "note"))
    elif source_type == "webpage":
        if not c.has_url(entry):
            d.append(c.dev("no_url", "Webpage without URL.", "error"))
        if not c.ACCESSED.search(entry):
            d.append(c.dev("no_accessed", "Access date missing ('accessed'/'cited')."))
    else:
        d.append(c.dev("unknown_type",
                       "source_type undetermined: style cannot be fully checked.", "warning"))

    return c.result(d)
