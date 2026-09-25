#!/usr/bin/env python3
# core/resolve/providers/openreview.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Official OpenReview forum resolver for ICLR-style references."""

from __future__ import annotations

import json
import re
import urllib.parse

NAME = "openreview_search"
MANIFEST = {}
OPTIONAL_STAGE = True
RECOVER_NON_RECORD = True

_VENUE_MARKERS = (
    "iclr",
    "international conference on learning representations",
    "openreview",
)


def _field_value(value):
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())).strip()


def _likely_openreview(ref: dict) -> bool:
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
        and _likely_openreview(ref)
    )


def _official_note(note: dict) -> bool:
    content = note.get("content") or {}
    invitation = str(note.get("invitation") or "")
    venueid = str(_field_value(content.get("venueid")) or "")
    venue = str(_field_value(content.get("venue")) or "")
    if invitation.startswith("ICLR.cc/") or venueid.startswith("ICLR.cc/"):
        return True
    return "openreview" in venue.lower() and "iclr" in venue.lower()


def _score_note(ref: dict, note: dict) -> tuple[float, dict]:
    from core.resolve import service as resolve_mod

    content = note.get("content") or {}
    title = _field_value(content.get("title"))
    authors = _field_value(content.get("authors")) or []
    venue = _field_value(content.get("venue")) or "ICLR"
    year = ref.get("year")
    msg = {
        "title": [title] if title else None,
        "author": [{"family": str(author).split()[-1]} for author in authors if author],
        "published-print": {"date-parts": [[year]]} if year else {},
        "container-title": [venue],
    }
    profile = resolve_mod._metadata_match_profile(ref, msg, title)
    return float(profile.get("score") or 0.0), profile


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    title = resolve_mod._article_title_candidate(ref)
    if not title:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no usable title for OpenReview search",
        }

    params = {
        "term": title[:250],
        "type": "terms",
        "content": "all",
        "group": "all",
        "source": "forum",
        "offset": "0",
        "limit": "10",
    }
    url = "https://api.openreview.net/notes/search?" + urllib.parse.urlencode(params)
    try:
        _status, body = resolve_mod._get(url, accept="application/json")
        notes = (json.loads(body).get("notes") or [])
    except Exception as exc:
        return {
            "status": "unresolved",
            "via": NAME,
            "reason": f"network: {type(exc).__name__}",
        }

    best_note = None
    best_profile = None
    best_score = -1.0
    for note in notes:
        if not isinstance(note, dict) or not _official_note(note):
            continue
        score, profile = _score_note(ref, note)
        if score > best_score:
            best_note = note
            best_profile = profile
            best_score = score

    if best_note is None:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no official OpenReview forum match",
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
        }

    content = best_note.get("content") or {}
    matched_title = _field_value(content.get("title"))
    overlap = (best_profile or {}).get("title_overlap")
    if overlap is not None and overlap < resolve_mod.TITLE_MISMATCH_MAX:
        return {
            "status": "unverified",
            "via": NAME,
            "matched_title": matched_title,
            "reason": (
                f"title too dissimilar (overlap {overlap:.3f} < {resolve_mod.TITLE_MISMATCH_MAX}); "
                "author/year match is not enough to identify the cited work"
            ),
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
            "metadata_match": best_profile,
        }
    if best_score < 0.30:
        return {
            "status": "unverified",
            "via": NAME,
            "matched_title": matched_title,
            "reason": (
                f"best match below confidence threshold "
                f"(score {best_score:.3f}, overlap {overlap})"
            ),
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
            "metadata_match": best_profile,
        }

    note_id = best_note.get("forum") or best_note.get("id")
    pdf_links = []
    if note_id:
        pdf_links.append({"url": f"https://openreview.net/pdf?id={note_id}", "content_type": "pdf"})
        pdf_links.append({"url": f"https://openreview.net/forum?id={note_id}", "content_type": "html"})
    raw_pdf = _field_value(content.get("pdf"))
    if raw_pdf:
        pdf_links.append(
            {
                "url": urllib.parse.urljoin("https://openreview.net/", str(raw_pdf).lstrip("/")),
                "content_type": "pdf",
            }
        )
    seen = set()
    links = []
    for item in pdf_links:
        url_text = item.get("url")
        if not url_text or url_text in seen:
            continue
        seen.add(url_text)
        links.append(item)
    return {
        "status": "resolved",
        "via": NAME,
        "matched_title": matched_title,
        "abstract": _field_value(content.get("abstract")),
        "reason": "OpenReview forum title search match",
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium",
        "metadata_match": best_profile,
        "retracted": False,
        "fulltext_exists": True,
        "oa_status": "open",
        "work_type": "conference paper",
        "fulltext_links": links,
    }
