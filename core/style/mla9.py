# core/style/mla9.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""MLA 9th."""
import re
from . import common as c


def check(entry: str, source_type: str) -> dict:
    d = []
    # MLA: "Surname, Firstname." at opening.
    if not re.search(r"^[A-Z][a-zà-ÿ]+,\s+[A-Z][a-zà-ÿ]+", entry.strip()):
        d.append(c.dev("authors_format", "Expected 'Surname, Firstname.' at opening (MLA)."))

    if source_type == "article":
        if '"' not in entry and "“" not in entry:
            d.append(c.dev("title_quotes", "Article title expected in quotation marks (MLA)."))
        # MLA uses textual vol./no.
        if not re.search(r"\bvol\.\s*\d+", entry, re.I):
            d.append(c.dev("vol_label", "Expected 'vol. N' (MLA).", "warning"))
        if not re.search(r"\bno\.\s*\d+", entry, re.I):
            d.append(c.dev("no_label", "Expected 'no. N' (MLA).", "warning"))
        if not c.has_year(entry):
            d.append(c.dev("no_year", "Year not detected.", "error"))
    elif source_type == "book":
        if not c.has_year(entry):
            d.append(c.dev("no_year", "Year not detected.", "error"))
    elif source_type == "webpage":
        if not c.has_url(entry):
            d.append(c.dev("no_url", "Webpage without URL.", "error"))
        if not c.ACCESSED.search(entry):
            d.append(c.dev("no_accessed", "Expected 'Accessed <date>' (MLA)."))
    else:
        d.append(c.dev("unknown_type", "source_type undetermined: style cannot be fully checked.", "warning"))

    return c.result(d)
