#!/usr/bin/env python3
# core/resolve/providers/unpaywall.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Unpaywall-specific deterministic fetch helpers."""

from __future__ import annotations

import json
import urllib.parse

try:
    from .. import sources as _sources
except ImportError:
    from resolve import sources as _sources

NAME = "unpaywall"
MANIFEST = {}
UNPAYWALL_URL = "https://api.unpaywall.org/v2/{doi}?email={email}"
MAX_CANDIDATE_ITEMS = 4


def _record(doi: str, email: str, *, get_fn) -> dict | None:
    url = UNPAYWALL_URL.format(
        doi=urllib.parse.quote(doi, safe=""),
        email=urllib.parse.quote(email, safe="@."),
    )
    try:
        _status, body = get_fn(url, accept="application/json", profile="api")
        data = json.loads(body.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def best_oa_location(doi: str, email: str, *, get_fn) -> dict | None:
    data = _record(doi, email, get_fn=get_fn) or {}
    best = data.get("best_oa_location") or {}
    return best if isinstance(best, dict) else None


def oa_locations(doi: str, email: str, *, get_fn) -> list[dict]:
    """Return all Unpaywall OA locations, preserving API order."""
    data = _record(doi, email, get_fn=get_fn) or {}
    locations = data.get("oa_locations") or []
    return [location for location in locations if isinstance(location, dict)]


# Keep monkeypatch-based callers/tests of the public helpers working while the
# normal candidate path uses one shared record below.
_BEST_OA_LOCATION = best_oa_location
_OA_LOCATIONS = oa_locations


def best_oa_url(doi: str, email: str, *, get_fn) -> str | None:
    best = best_oa_location(doi, email, get_fn=get_fn) or {}
    return best.get("url_for_pdf") or best.get("url") or None


def pdf_url_from_doi(
    doi: str | None,
    *,
    email=None,
    get_fn,
    **kwargs,
) -> str | None:
    doi_text = str(doi or "").strip()
    if not doi_text or not email:
        return None
    return best_oa_url(doi_text, email, get_fn=get_fn)


def enabled(*, email=None, **kwargs) -> bool:
    return bool(email)


def disabled_reason(*, email=None, **kwargs) -> str | None:
    return None if email else "missing contact email"


def candidate_items(
    ref: dict,
    *,
    email=None,
    normalize_doi,
    kind_from_url,
    get_fn,
    **kwargs,
) -> list[dict]:
    doi = normalize_doi(ref.get("doi"))
    if not doi or not email:
        return []
    if best_oa_location is not _BEST_OA_LOCATION or oa_locations is not _OA_LOCATIONS:
        best = best_oa_location(doi, email, get_fn=get_fn)
        locations = ([best] if best else []) + oa_locations(doi, email, get_fn=get_fn)
    else:
        data = _record(doi, email, get_fn=get_fn) or {}
        best = data.get("best_oa_location")
        best = best if isinstance(best, dict) else None
        raw_locations = data.get("oa_locations") or []
        locations = ([best] if best else []) + [
            location for location in raw_locations if isinstance(location, dict)
        ]
    seen = set()
    out = []
    identity_context = {"provider": NAME, "identifiers": {"doi": doi}}

    def add(url: str | None, location: dict, source: str, *, alternate: bool = False):
        url = str(url or "").strip()
        if not url or url in seen or len(out) >= MAX_CANDIDATE_ITEMS:
            return
        out.append({
            "method": NAME,
            "url": url,
            "kind": kind_from_url(url),
            "content_version": _sources.content_version_for(url, location.get("version")),
            "discovered_via": NAME,
            "discovery_reason": f"Unpaywall location source: {source}",
            "provenance": [NAME],
            "identity_context": dict(identity_context),
            **({"fallback_stage": "oa_alternate"} if alternate else {}),
        })
        seen.add(url)

    pdf_urls = []
    landing_urls = []
    for index, location in enumerate(locations):
        if not isinstance(location, dict):
            continue
        is_best = index == 0 and location is best
        if is_best:
            # Pre-fallback-staging behaviour: the best location contributes a
            # single primary candidate (its PDF, or its landing page when it
            # has no PDF). When it exposes both, the landing is a same-source
            # mirror of the PDF — demote it to an alternate instead of paying
            # for it twice in the primary stage.
            pdf_url = location.get("url_for_pdf")
            landing_url = location.get("url")
            primary_url = pdf_url or landing_url
            if primary_url:
                target = pdf_urls if primary_url == pdf_url else landing_urls
                target.append((primary_url, location, "best_oa_location", False))
            if pdf_url and landing_url:
                landing_urls.append((landing_url, location, "best_oa_location", True))
            continue
        if location.get("url_for_pdf"):
            pdf_urls.append((location.get("url_for_pdf"), location, "oa_location", True))
        if location.get("url"):
            landing_urls.append((location.get("url"), location, "oa_location", True))
    for url, location, source, alternate in pdf_urls + landing_urls:
        add(url, location, source, alternate=alternate)
    return out
