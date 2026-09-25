# core/resolve/providers/_issue_common.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Small deterministic helpers shared by official issue adapters."""

from __future__ import annotations

import re
import unicodedata


def coordinate(ref: dict, kind: str) -> str | None:
    for item in ref.get("cited_coordinates") or ():
        if isinstance(item, dict) and item.get("kind") == kind:
            value = item.get("normalized_value") or item.get("raw_value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def text_key(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def target_status(ref: dict, members: list[dict]) -> tuple[str, int | None]:
    """Find a unique member while keeping unreliable parsed titles non-negative.

    Exact normalized containment in the original citation is positive evidence.
    Absence is allowed only when the parser supplied a substantial title that is
    itself visibly present in the original citation.  Locator-only and malformed
    title fragments therefore remain inconclusive.
    """
    raw_key = text_key(ref.get("raw_entry"))
    cited_title_key = text_key(ref.get("title"))
    cited_year = str(ref.get("year") or "").strip()
    present: list[int] = []
    borderline = False
    for order, member in enumerate(members):
        title_key = text_key(member.get("title"))
        if not title_key:
            continue
        member_year = str(member.get("year") or "").strip()
        year_ok = not cited_year or not member_year or cited_year == member_year
        exact_in_raw = len(title_key.split()) >= 3 and title_key in raw_key
        exact_parsed = bool(cited_title_key and cited_title_key == title_key)
        if year_ok and (exact_in_raw or exact_parsed):
            present.append(order)
            continue
        cited_tokens = set(cited_title_key.split())
        member_tokens = set(title_key.split())
        if cited_tokens and member_tokens:
            overlap = len(cited_tokens & member_tokens) / len(cited_tokens | member_tokens)
            borderline = borderline or overlap >= 0.50
    if len(present) == 1:
        return "present", present[0]
    if present or borderline:
        return "inconclusive", None
    title_tokens = cited_title_key.split()
    title_reliable = (
        len(title_tokens) >= 4
        and cited_title_key in raw_key
        and sum(token.isalpha() for token in title_tokens) >= 3
    )
    return ("absent", None) if title_reliable else ("inconclusive", None)
