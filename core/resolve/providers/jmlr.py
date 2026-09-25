#!/usr/bin/env python3
# core/resolve/providers/jmlr.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical Journal of Machine Learning Research companion resolver."""

from __future__ import annotations

import html
from html.parser import HTMLParser
import re
import urllib.parse

NAME = "jmlr_search"
RESOLVE_NAME = NAME
OPTIONAL_STAGE = True
CANONICAL_COMPANION = True
RECOVER_NON_RECORD = True
MANIFEST = {"origin": "jmlr", "canonical_hosts": ["jmlr.org", "jmlr.csail.mit.edu"]}

_INDEX_CACHE: dict[str, str] = {}
_JMLR_MARKERS = ("journal of machine learning research", "jmlr")


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    text = re.sub(
        r"\s+",
        " ",
        " ".join(str(ref.get(k) or "") for k in ("venue", "journal", "raw_entry", "url")),
    ).lower()
    return bool(
        resolve_mod._article_like_resolution_candidate(ref)
        and any(marker in text for marker in _JMLR_MARKERS)
    )


def _volume(ref: dict) -> int | None:
    for value in (ref.get("volume"), ref.get("journal_volume")):
        try:
            if value:
                return int(value)
        except (TypeError, ValueError):
            pass
    raw = re.sub(r"\s+", " ", str(ref.get("raw_entry") or ""))
    patterns = (
        r"(?:JMLR|Journal of Machine Learning Research)\s*,?\s*(\d{1,3})\b",
        r"\bvolume\s+(\d{1,3})\b",
        r"\b(\d{1,3})\s*\(\s*\d+\s*\)\s*[:,]",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if match:
            return int(match.group(1))
    return None


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _observed_year(value: str | None) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def _index_entries(body: str, base_url: str) -> list[dict]:
    """Parse JMLR volume entries, whose title lives in <dt> beside abs/pdf links."""
    out = []
    seen = set()
    for block in re.findall(r"<dl\b[^>]*>(.*?)</dl>", body or "", re.I | re.S):
        title_match = re.search(r"<dt\b[^>]*>(.*?)</dt>", block, re.I | re.S)
        landing_match = re.search(
            r'<a\b[^>]*href=["\']([^"\']+\.html)["\'][^>]*>\s*abs\s*</a>',
            block,
            re.I | re.S,
        )
        if not title_match or not landing_match:
            continue
        title = _clean_text(title_match.group(1))
        url = urllib.parse.urljoin(base_url, html.unescape(landing_match.group(1)))
        pdf_match = re.search(
            r'<a\b[^>]*href=["\']([^"\']+\.pdf)["\']', block, re.I | re.S
        )
        authors_match = re.search(r"<dd\b[^>]*>.*?<i\b[^>]*>(.*?)</i>", block, re.I | re.S)
        year_match = re.search(r"\b(19|20)\d{2}\b", _clean_text(block))
        if title and url not in seen:
            seen.add(url)
            out.append({
                "title": title,
                "url": url,
                "pdf_url": (
                    urllib.parse.urljoin(base_url, html.unescape(pdf_match.group(1)))
                    if pdf_match else None
                ),
                "authors": [
                    part.strip()
                    for part in _clean_text(authors_match.group(1) if authors_match else "").split(",")
                    if part.strip()
                ],
                "year": int(year_match.group(0)) if year_match else None,
            })
    return out


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        row = {str(k).lower(): v for k, v in attrs}
        key = str(row.get("name") or row.get("property") or "").lower()
        value = row.get("content")
        if key and value:
            self.meta.setdefault(key, []).append(html.unescape(value).strip())


def _landing_metadata(body: str, landing_url: str) -> dict:
    parser = _MetaParser()
    parser.feed(body)
    meta = parser.meta
    pdf_meta = (meta.get("citation_pdf_url") or [None])[0]
    return {
        "title": (meta.get("citation_title") or meta.get("dc.title") or [None])[0],
        "authors": meta.get("citation_author") or meta.get("dc.creator") or [],
        "year": _observed_year((meta.get("citation_publication_date") or meta.get("dc.date") or [None])[0]),
        # An empty relative ref would urljoin back to the landing page itself and
        # make the HTML masquerade as a PDF; keep it None when there is no meta.
        "pdf_url": urllib.parse.urljoin(landing_url, pdf_meta) if pdf_meta else None,
    }


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    title = resolve_mod._article_title_candidate(ref)
    volume = _volume(ref)
    if not title or volume is None:
        return {"status": "unverified", "via": NAME, "reason": "JMLR title or volume unavailable"}
    index_url = f"https://www.jmlr.org/papers/v{volume}/"
    try:
        body = _INDEX_CACHE.get(index_url)
        if body is None:
            _status, body = resolve_mod._get(index_url, accept="text/html,application/xhtml+xml")
            _INDEX_CACHE[index_url] = body
        candidates = _index_entries(body, index_url)
        best = None
        best_profile = None
        for candidate in candidates:
            profile = resolve_mod._metadata_match_profile(
                ref,
                {
                    "title": [candidate["title"]],
                    "author": [{"family": name.split()[-1]} for name in candidate.get("authors") or []],
                    "published": {"date-parts": [[candidate.get("year")]]},
                    "container-title": ["Journal of Machine Learning Research"],
                },
                candidate["title"],
            )
            if resolve_mod._title_key(candidate["title"]) == resolve_mod._title_key(title):
                profile = dict(profile, title_overlap=1.0, score=max(0.80, profile.get("score", 0)))
            if best_profile is None or profile.get("score", 0) > best_profile.get("score", 0):
                best, best_profile = candidate, profile
        if best is None or (best_profile or {}).get("title_overlap", 0) < 0.80:
            return {"status": "unverified", "via": NAME, "reason": "no faithful JMLR index match"}
        _status, landing = resolve_mod._get(best["url"], accept="text/html,application/xhtml+xml")
        metadata = _landing_metadata(landing, best["url"])
        matched_title = metadata.get("title") or best["title"]
        pdf_url = metadata.get("pdf_url") or best.get("pdf_url")
        if not pdf_url:
            return {"status": "unverified", "via": NAME, "reason": "JMLR landing exposed no PDF URL"}
        profile = resolve_mod._metadata_match_profile(
            ref,
            {
                "title": [matched_title],
                "author": [{"family": name.split()[-1]} for name in metadata.get("authors") or []],
                "published": {"date-parts": [[metadata["year"]]]} if metadata.get("year") else {},
                "container-title": ["Journal of Machine Learning Research"],
            },
            matched_title,
        )
        if resolve_mod._title_key(matched_title) == resolve_mod._title_key(title):
            profile = dict(profile, title_overlap=1.0, score=max(0.90, profile.get("score", 0)))
        slug = urllib.parse.urlparse(best["url"]).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return {
            "status": "resolved", "via": NAME, "matched_title": matched_title,
            "matched_authors": metadata.get("authors") or [], "record_id": slug,
            "reason": "JMLR canonical index and landing metadata match",
            "resolution_basis": "metadata_search", "existence_confidence": "high",
            "metadata_match": profile, "retracted": False, "fulltext_exists": True,
            "fulltext_availability": {"status": "available", "scope": "location", "observed_by": NAME},
            "oa_status": "open", "work_type": "journal article",
            "fulltext_links": [
                {"url": pdf_url, "content_type": "application/pdf", "content_version": "published"},
                {"url": best["url"], "content_type": "html", "content_version": "published"},
            ],
        }
    except Exception as exc:
        return {"status": "unresolved", "via": NAME, "reason": f"network: {type(exc).__name__}"}
