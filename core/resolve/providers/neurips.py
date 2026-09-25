#!/usr/bin/env python3
# core/resolve/providers/neurips.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Official NeurIPS proceedings resolver."""

from __future__ import annotations

import html
import re
import urllib.parse

NAME = "neurips_search"
MANIFEST = {
    "canonical_hosts": ["proceedings.neurips.cc", "papers.nips.cc"],
}
OPTIONAL_STAGE = True
RECOVER_NON_RECORD = True

_VENUE_MARKERS = (
    "neurips",
    "nips",
    "advances in neural information processing systems",
)


def _norm(text: str | None) -> str:
    if not text:
        return ""
    cleaned = html.unescape(str(text))
    cleaned = re.sub(r"[\u2010-\u2015]", "-", cleaned)
    cleaned = re.sub(r"(\w)-\s+(\w)", r"\1\2", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _year(ref: dict) -> int | None:
    try:
        year = int(ref.get("year") or 0)
    except (TypeError, ValueError):
        return None
    return year or None


def _likely_neurips(ref: dict) -> bool:
    raw = " ".join(
        str(ref.get(key) or "")
        for key in ("title", "raw_entry", "url", "doi")
    ).lower()
    return any(marker in raw for marker in _VENUE_MARKERS)


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return (
        not ref.get("url")
        and resolve_mod._article_like_resolution_candidate(ref)
        and _year(ref) is not None
        and _likely_neurips(ref)
    )


def _entry_matches(
    ref: dict, title: str, authors: str, *, record_year: int | None = None,
) -> bool:
    from core.resolve import service as resolve_mod

    cited_title = resolve_mod._article_title_candidate(ref)
    if not cited_title:
        return False
    profile = resolve_mod._metadata_match_profile(
        ref,
        {
            "title": [title],
            "author": ([{"family": _first_author_surname(authors)}]
                       if _first_author_surname(authors) else []),
            "published-print": {"date-parts": [[record_year or _year(ref)]]}
            if (record_year or _year(ref)) else {},
            "container-title": ["Neural Information Processing Systems"],
        },
        title,
    )
    if _norm(title) == _norm(cited_title):
        return True
    # Some parsed bibliography entries append the proceedings container and
    # page range.  This narrow exception retains the entire official title as
    # an exact prefix and requires independent author, year, and venue checks.
    if _exact_title_prefix_with_proceedings_tail(cited_title, title):
        return bool(
            _first_author_matches_citation(ref, authors)
            and profile.get("year_match") is True
            and (profile.get("venue_overlap") or 0.0) >= 0.50
        )
    return False


def _exact_title_prefix_with_proceedings_tail(
    cited_title: str, official_title: str,
) -> bool:
    """Allow only an exact official title followed by a NeurIPS tail."""
    official = _norm(official_title).split()
    cited = _norm(cited_title).split()
    if cited[:len(official)] != official:
        return False
    tail = cited[len(official):]
    proceedings = ["in", "advances", "in", "neural", "information", "processing", "systems"]
    if tail[:len(proceedings)] != proceedings:
        return False
    return all(token == "pages" or token.isdigit() for token in tail[len(proceedings):])


def _first_author_matches_citation(ref: dict, authors: str) -> bool:
    """Compare the official surname with the citation's author segment."""
    surname = _first_author_surname(authors)
    if not surname:
        return False
    raw = str(ref.get("raw_entry") or "")
    title = str(ref.get("title") or "")
    if title:
        match = re.search(re.escape(title), raw, flags=re.IGNORECASE)
        if match:
            raw = raw[:match.start()]
    return surname in _norm(raw).split()


def _first_author_surname(authors: str) -> str | None:
    first_author = str(authors or "").split(",", 1)[0]
    tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", first_author)
    return _norm(tokens[-1]) if tokens else None


def _extract_entries(index_html: str) -> list[dict]:
    pattern = re.compile(
        r'<a[^>]+title="paper title"[^>]+href="([^"]+Abstract\.html)"[^>]*>([^<]+)</a>\s*'
        r'<span[^>]+class="paper-authors"[^>]*>([^<]*)</span>',
        flags=re.IGNORECASE,
    )
    out = []
    seen = set()
    for href, title, authors in pattern.findall(index_html):
        clean_href = html.unescape(href).strip()
        clean_title = html.unescape(title).strip()
        clean_authors = html.unescape(authors).strip()
        if not clean_href or not clean_title or clean_href in seen:
            continue
        seen.add(clean_href)
        out.append({"href": clean_href, "title": clean_title, "authors": clean_authors})
    return out


def _choose_entry(
    ref: dict, entries: list[dict], *, record_year: int | None = None,
) -> dict | None:
    exact = [
        entry for entry in entries
        if _entry_matches(
            ref, entry.get("title") or "", entry.get("authors") or "",
            record_year=record_year,
        )
    ]
    return exact[0] if exact else None


def _profile(
    ref: dict, title: str, authors: str, *, record_year: int | None = None,
) -> dict:
    from core.resolve import service as resolve_mod

    msg = {
        "title": [title],
        "author": ([{"family": _first_author_surname(authors)}]
                   if _first_author_surname(authors) else []),
        "published-print": {"date-parts": [[record_year or _year(ref)]]}
        if (record_year or _year(ref)) else {},
        "container-title": ["Neural Information Processing Systems"],
    }
    return resolve_mod._metadata_match_profile(ref, msg, title)


def _paper_url(detail_url: str) -> str:
    return (
        detail_url
        .replace("/hash/", "/file/")
        .replace("-Abstract.html", "-Paper.pdf")
    )


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    year = _year(ref)
    title = resolve_mod._article_title_candidate(ref)
    if year is None or not title:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "missing year or usable title for NeurIPS search",
        }

    # Older NIPS volumes are occasionally cited with their publication year,
    # while the official index is filed under the preceding conference year.
    # Identity remains strict: adjacent indices can only win on title/author.
    entry = None
    index_url = None
    network_errors = []
    for candidate_year in (year, year - 1, year + 1):
        candidate_url = f"https://proceedings.neurips.cc/paper/{candidate_year}"
        try:
            _status, body = resolve_mod._get(
                candidate_url,
                accept="text/html,application/xhtml+xml",
            )
        except Exception as exc:
            network_errors.append(type(exc).__name__)
            continue
        candidate = _choose_entry(
            ref, _extract_entries(body), record_year=candidate_year,
        )
        if candidate is not None:
            entry = candidate
            index_url = candidate_url
            break
    if entry is None:
        if len(network_errors) == 3:
            return {
                "status": "unresolved",
                "via": NAME,
                "reason": f"network: {network_errors[-1]}",
            }
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no official NeurIPS proceedings match",
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
        }

    detail_url = urllib.parse.urljoin(index_url or "", entry["href"])
    pdf_url = _paper_url(detail_url)
    profile = _profile(
        ref, entry["title"], entry.get("authors") or "",
        record_year=int(index_url.rsplit("/", 1)[-1]) if index_url else None,
    )
    return {
        "status": "resolved",
        "via": NAME,
        "matched_title": entry["title"],
        "matched_authors": [entry.get("authors") or ""],
        "reason": "NeurIPS proceedings title search match",
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium",
        "metadata_match": profile,
        "retracted": False,
        "fulltext_exists": True,
        "oa_status": "open",
        "work_type": "conference paper",
        "fulltext_links": [
            {"url": pdf_url, "content_type": "pdf", "identity_context": {
                "provider": NAME,
                "official": True,
                "official_document_relation": "official_landing_page_links_exact_document",
                "canonical_host": True,
                "landing_page_url": detail_url,
                "canonical_url": pdf_url,
                "title": entry["title"],
                "expected_document_title": entry["title"],
                "first_author": (entry.get("authors") or "").split(",", 1)[0],
                "year": year,
            }},
            {"url": detail_url, "content_type": "html"},
        ],
    }
