#!/usr/bin/env python3
# core/resolve/providers/lens.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Lens resolver module."""

from __future__ import annotations

import html
import json
import urllib.error

NAME = "lens_search"
CREDENTIAL_SPECS = ({
    "provider": "lens",
    "env_name": "LENS_API_KEY",
    "channels": ("resolve",),
    "label": "Lens",
},)
MANIFEST = {
    "origin": "lens",
}
OPTIONAL_STAGE = True


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return (
        not ref.get("url")
        and resolve_mod._article_like_resolution_candidate(ref)
    )


def _headers(resolve_mod) -> dict[str, str] | None:
    key = resolve_mod._configured_key(resolve_mod.ENV_LENS_API_KEY)
    if not key:
        return None
    return {"Authorization": f"Bearer {key}"}


def _title_query(title: str, year: int | None) -> dict:
    must = [{"match_phrase": {"title": title[:250]}}]
    if year is not None:
        must.append({"range": {"year_published": {"gte": year - 1, "lte": year + 1}}})
    return {
        "query": {"bool": {"must": must}},
        "size": 3,
        "include": [
            "title",
            "abstract",
            "external_ids",
            "authors",
            "year_published",
            "source",
            "open_access",
            "publication_type",
            "source_urls",
        ],
    }


def _to_crossref_like(rec: dict) -> dict:
    authors = rec.get("authors") or []
    author_list = []
    if authors:
        first = authors[0] or {}
        family = first.get("last_name") or first.get("name")
        if family:
            author_list.append({"family": str(family).split()[-1]})
    source = rec.get("source") or {}
    venue = source.get("title") or rec.get("source_title")
    year = rec.get("year_published")
    title = rec.get("title")
    return {
        "title": [title] if title else None,
        "author": author_list,
        "published-print": {"date-parts": [[year]]} if year else {},
        "container-title": [venue] if venue else [],
    }


def _doi(resolve_mod, rec: dict) -> str | None:
    for item in rec.get("external_ids") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").lower() == "doi":
            return resolve_mod._normalize_doi_value(item.get("value"))
    return resolve_mod._normalize_doi_value(rec.get("doi"))


def _open_access_links(resolve_mod, rec: dict) -> list[dict]:
    links = []
    seen = set()
    doi = _doi(resolve_mod, rec)
    if doi:
        doi_url = f"https://doi.org/{doi}"
        links.append({"url": doi_url, "content_type": "doi"})
        seen.add(doi_url)
    open_access = rec.get("open_access") or {}
    locations = open_access.get("locations") or []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        for key in ("pdf_urls", "landing_page_urls"):
            for url in loc.get(key) or []:
                if not url or url in seen:
                    continue
                seen.add(url)
                links.append({"url": url, "content_type": resolve_mod._content_type_hint(url, key)})
    for item in rec.get("source_urls") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        links.append({"url": url, "content_type": resolve_mod._content_type_hint(url, item.get("type"))})
    return links


def _fulltext_meta(resolve_mod, rec: dict) -> dict:
    out_links = _open_access_links(resolve_mod, rec)
    open_access = rec.get("open_access") or {}
    colour = str(open_access.get("colour") or "").strip().lower()
    publication_type = rec.get("publication_type")
    oa_status = "open" if out_links and (colour or any(l.get("content_type") == "pdf" for l in out_links)) else "unknown"
    return {
        "fulltext_exists": True if out_links else "unknown",
        "oa_status": oa_status,
        "work_type": publication_type,
        "fulltext_links": out_links,
    }


def enrich(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    headers = _headers(resolve_mod)
    if headers is None:
        return None
    doi = resolve_mod._normalize_doi_value(ref.get("doi"))
    if not doi:
        return None
    payload = {
        "query": {"match": {"doi": doi}},
        "size": 1,
        "include": [
            "title",
            "abstract",
            "external_ids",
            "authors",
            "year_published",
            "source",
            "open_access",
            "publication_type",
            "source_urls",
        ],
    }
    try:
        _status, body = resolve_mod._post_json(
            "https://api.lens.org/scholarly/search",
            payload,
            headers_extra=headers,
        )
        results = (json.loads(body).get("data") or [])
        if not results:
            return {"status": "unverified", "via": "lens", "reason": "no match on Lens enrichment"}
        rec = results[0] or {}
        out = {
            "status": "resolved",
            "via": "lens",
            "matched_title": rec.get("title"),
            "abstract": html.unescape(rec.get("abstract") or "") or None,
            "reason": None,
        }
        out.update(_fulltext_meta(resolve_mod, rec))
        return out
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": "unverified", "via": "lens", "reason": "no match on Lens enrichment"}
        if exc.code == 429:
            return {
                "status": "unresolved",
                "via": "lens",
                "reason": "rate_limited (HTTP 429) - NOT fabrication",
            }
        return {"status": "unresolved", "via": "lens", "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"status": "unresolved", "via": "lens", "reason": f"network: {type(exc).__name__}"}


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    headers = _headers(resolve_mod)
    if headers is None:
        return None
    title = resolve_mod._article_title_candidate(ref)
    if not title:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no usable title for Lens search",
        }
    payload = _title_query(title, ref.get("year"))
    try:
        _status, body = resolve_mod._post_json(
            "https://api.lens.org/scholarly/search",
            payload,
            headers_extra=headers,
        )
        results = (json.loads(body).get("data") or [])
        if not results:
            return {"status": "unverified", "via": NAME, "reason": "no results on Lens"}
        best_rec = None
        best_profile = None
        best_score = -1.0
        for rec in results:
            if not isinstance(rec, dict):
                continue
            msg = _to_crossref_like(rec)
            profile = resolve_mod._metadata_match_profile(ref, msg, rec.get("title"))
            if profile["score"] > best_score:
                best_score = profile["score"]
                best_rec = rec
                best_profile = profile
        matched_title = (best_rec or {}).get("title")
        overlap = best_profile["title_overlap"] if best_profile else None
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
        if best_rec is None or best_score < 0.30:
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
        out = {
            "status": "resolved",
            "via": NAME,
            "matched_title": matched_title,
            "abstract": html.unescape(best_rec.get("abstract") or "") or None,
            "retracted": False,
            "reason": "Lens title search match",
            "resolution_basis": "metadata_search",
            "existence_confidence": "medium",
            "metadata_match": best_profile,
        }
        out.update(_fulltext_meta(resolve_mod, best_rec))
        return out
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": "unverified", "via": NAME, "reason": "no results on Lens"}
        if exc.code == 429:
            return {
                "status": "unresolved",
                "via": NAME,
                "reason": "rate_limited (HTTP 429) - NOT fabrication",
            }
        return {"status": "unresolved", "via": NAME, "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"status": "unresolved", "via": NAME, "reason": f"network: {type(exc).__name__}"}
