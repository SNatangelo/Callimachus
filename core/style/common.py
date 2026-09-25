# core/style/common.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
common.py - helpers shared by all style modules.

Each style module exposes: check(entry: str, source_type: str) -> dict
with shape: {"conforms": bool, "deviations": [{"code","message","severity"}]}.

Severity levels:
  "error"   — definite format violation; always blocks conforms.
  "warning" — likely format issue that can be verified from plain text; blocks conforms.
  "note"    — structural limitation (e.g. italics not verifiable from plain text);
              does NOT block conforms, shown as informational in the report.

Checks are heuristic and declared as such: they catch obvious deviations
(missing year, malformed DOI, absent quotes where required, author-year order)
but do not replace a full citation validator. A labelled false-positive warning
is better than a wrongly declared conformance.
"""
import re

YEAR = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
DOI = re.compile(r"10\.\d{4,9}/\S+")
DOI_URL = re.compile(r"https?://(?:dx\.)?doi\.org/10\.\d{4,9}/", re.IGNORECASE)
URL = re.compile(r"https?://[^\s)]+")
PAGES = re.compile(r"\b(?:\d+\s*[-\u2013]\s*\d+|e\d{4,}|[A-Za-z]\d{4,})\b")
# Initials like "AB" or "A.B." after a surname (Vancouver/AMA).
INITIALS_VANC = re.compile(r"[A-Z][a-zA-ZÀ-ÿ]+\s+[A-Z]{1,3}\b")
# "Surname, A. B." (APA/Chicago/MLA author-date)
INITIALS_DOTTED = re.compile(r"[A-Z][a-zA-ZÀ-ÿ]+,\s+[A-Z]\.")
ACCESSED = re.compile(r"accessed|consultato|retrieved|visitato", re.IGNORECASE)


def dev(code: str, message: str, severity: str = "warning") -> dict:
    return {"code": code, "message": message, "severity": severity}


def result(deviations: list[dict]) -> dict:
    blocking = [d for d in deviations if d["severity"] != "note"]
    return {"conforms": len(blocking) == 0,
            "deviations": deviations}


def has_year(entry: str) -> bool:
    return bool(YEAR.search(entry))


def has_doi(entry: str) -> bool:
    return bool(DOI.search(entry) or DOI_URL.search(entry))


def has_url(entry: str) -> bool:
    return bool(URL.search(entry))
