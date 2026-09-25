#!/usr/bin/env python3
# core/resolve/providers/datacite.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""DataCite title-search resolver module."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse

NAME = "datacite_search"
MANIFEST = {}


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return (
        not ref.get("url")
        and resolve_mod._article_like_resolution_candidate(ref)
    )


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    """Title search on DataCite - covers datasets, preprints, and grey literature.

    DataCite is the DOI registration agency for datasets, software, and
    many preprints. Its title search is less precise than Crossref/OpenAlex
    but covers material those services miss. Used as a deep fallback after
    CORE and arXiv have been tried.
    """
    title = resolve_mod._article_title_candidate(ref)
    if not title:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no usable title for DataCite search",
        }

    raw_entry = resolve_mod._tex_strip_braces(ref.get("raw_entry") or "")
    ref_title = resolve_mod._article_title_candidate(ref)
    year = ref.get("year")

    clean_title = re.sub(r'[{}"\']', "", title[:200])
    params = {
        "query": f"titles.title:{clean_title}",
        "page[size]": "5",
    }
    url = "https://api.datacite.org/dois?" + urllib.parse.urlencode(params)
    try:
        _status, body = resolve_mod._get(url, accept="application/vnd.api+json")
        data = json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return {
                "status": "unresolved",
                "via": NAME,
                "reason": "rate_limited (HTTP 429)",
            }
        return {
            "status": "unresolved",
            "via": NAME,
            "reason": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "status": "unresolved",
            "via": NAME,
            "reason": f"network: {type(exc).__name__}",
        }

    items = data.get("data") or []
    if not items:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no results on DataCite",
        }

    best = None
    best_overlap = -1.0
    for item in items:
        attr = item.get("attributes") or {}
        titles = attr.get("titles") or []
        entry_title = (titles[0].get("title", "") if titles else "").strip()
        entry_title = re.sub(r"\s+", " ", entry_title)
        overlap = resolve_mod._title_match_score(ref_title, entry_title)
        if overlap == 0.0 and len(resolve_mod._sources._tokens(ref_title)) < resolve_mod.TITLE_MIN_TOKENS:
            raw_overlap = resolve_mod.title_overlap(entry_title, raw_entry)
            if raw_overlap is not None and raw_overlap >= 0.95:
                overlap = raw_overlap
        if overlap is not None and overlap > best_overlap:
            best_overlap = overlap
            best = (item, attr)

    if best is None or best_overlap < 0.30:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no DataCite match met the confidence threshold",
        }

    _item, attr = best
    matched_title = ""
    titles = attr.get("titles") or []
    if titles:
        matched_title = (titles[0].get("title", "") or "").strip()
        matched_title = re.sub(r"\s+", " ", matched_title)
    doi = attr.get("doi")
    matched_year_str = attr.get("publicationYear")
    try:
        matched_year = int(matched_year_str) if matched_year_str else None
    except (ValueError, TypeError):
        matched_year = None
    fl_links = []
    if doi:
        fl_links.append({"url": f"https://doi.org/{doi}", "content_type": "doi"})

    if (
        year is not None
        and matched_year is not None
        and abs(int(year) - matched_year) > 2
        and best_overlap < 0.70
    ):
        return {
            "status": "unverified",
            "via": NAME,
            "matched_title": matched_title,
            "reason": (
                f"year mismatch (cited {year}, "
                f"DataCite {matched_year}, overlap {best_overlap:.3f})"
            ),
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
        }

    return {
        "status": "resolved",
        "via": NAME,
        "matched_title": matched_title,
        "doi": doi,
        "retracted": False,
        "reason": f"DataCite title search match (overlap {best_overlap:.3f})",
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium" if best_overlap >= 0.70 else "low",
        "fulltext_exists": True if doi else False,
        "oa_status": "unknown",
        "fulltext_links": fl_links,
        "metadata_match": {
            "title_overlap": best_overlap,
            "matched_year": matched_year,
        },
    }
