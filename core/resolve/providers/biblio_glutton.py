#!/usr/bin/env python3
# core/resolve/providers/biblio_glutton.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional local biblio-glutton metadata accelerator.

This provider is deliberately only an accelerator.  Its response is admitted
with the same metadata checks as a remote title search, and a returned DOI is
independently checked by the resolve orchestrator before it can freeze identity.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse


NAME = "biblio_glutton"
LOCAL_ACCELERATOR = True
MANIFEST = {"origin": NAME, "via_aliases": [NAME]}
ENV_URL = "BIBLIO_GLUTTON_URL"


def _base_url(environ: dict[str, str] | None = None) -> str | None:
    source = os.environ if environ is None else environ
    value = str(source.get(ENV_URL) or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        parsed.port
    except ValueError:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc}{parsed.path.rstrip('/')}"


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return bool(
        _base_url()
        and not ref.get("url")
        and resolve_mod._article_like_resolution_candidate(ref)
        and (ref.get("source_type") == "article" or resolve_mod._article_title_candidate(ref))
    )


def _text(value) -> str | None:
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, str) and item.strip()), None)
    value = str(value or "").strip()
    return value or None


def _crossref_like(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    return message if isinstance(message, dict) else payload


def _query_params(ref: dict) -> dict[str, str]:
    from core.resolve import service as resolve_mod

    title = resolve_mod._article_title_candidate(ref)
    raw = str(ref.get("raw_entry") or "").strip()
    values = {
        "doi": resolve_mod._normalize_doi_value(ref.get("doi")),
        "pmid": ref.get("pmid"),
        "atitle": title,
        "firstAuthor": resolve_mod._first_author_key(raw),
        "jtitle": ref.get("journal") or ref.get("venue"),
        "volume": ref.get("volume"),
        "firstPage": ref.get("first_page") or ref.get("page"),
        "year": ref.get("year"),
        "biblio": raw,
    }
    return {key: str(value).strip() for key, value in values.items() if str(value or "").strip()}


def _fulltext_meta(msg: dict, doi: str) -> dict:
    links: list[dict] = []
    seen: set[str] = set()
    oa_link = _text(msg.get("oaLink"))
    if oa_link:
        links.append({
            "url": oa_link,
            "content_type": "unknown",
            "discovered_via": NAME,
            "identity_context": {"provider": NAME, "identifiers": {"doi": doi}},
        })
        seen.add(oa_link)
    doi_url = f"https://doi.org/{doi}" if doi else None
    if doi_url and doi_url not in seen:
        links.append({
            "url": doi_url,
            "content_type": "doi",
            "discovered_via": NAME,
            "identity_context": {"provider": NAME, "identifiers": {"doi": doi}},
        })
    return {
        "fulltext_exists": "unknown",
        "oa_status": "unknown",
        "work_type": msg.get("type"),
        "fulltext_links": links,
    }


def _candidate_identifiers(msg: dict, doi: str) -> dict[str, str]:
    identifiers = {"doi": doi}
    pmid = str(msg.get("pmid") or "").strip()
    if pmid.isdigit():
        identifiers["pmid"] = pmid
    pmcid = str(msg.get("pmcid") or "").strip().upper()
    if re.fullmatch(r"PMC\d+", pmcid):
        identifiers["pmcid"] = pmcid
    return identifiers


def discover(ref: dict) -> dict | None:
    """Look up metadata locally, retaining remote Crossref as the authority."""
    from core.resolve import service as resolve_mod

    base = _base_url()
    if base is None:
        return None
    params = _query_params(ref)
    if not params:
        return {"status": "unverified", "via": NAME, "reason": "no lookup fields"}
    url = f"{base}/service/lookup?{urllib.parse.urlencode(params)}"
    try:
        status, body = resolve_mod._get(url)
        if int(status) != 200:
            if int(status) == 404:
                return {"status": "unverified", "via": NAME, "reason": "HTTP 404", "provider_outcome": "not_found"}
            return {"status": "unresolved", "via": NAME, "reason": f"HTTP {status}", "provider_outcome": "unavailable"}
        try:
            msg = _crossref_like(json.loads(body))
        except (TypeError, ValueError, json.JSONDecodeError):
            msg = None
        if msg is None:
            return {"status": "unresolved", "via": NAME, "reason": "invalid JSON response", "provider_outcome": "unavailable"}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": "unverified", "via": NAME, "reason": "HTTP 404", "provider_outcome": "not_found"}
        return {"status": "unresolved", "via": NAME, "reason": f"HTTP {exc.code}", "provider_outcome": "unavailable"}
    except Exception as exc:
        return {"status": "unresolved", "via": NAME, "reason": f"network: {type(exc).__name__}", "provider_outcome": "unavailable"}

    doi = resolve_mod._normalize_doi_value(msg.get("DOI"))
    title = _text(msg.get("title"))
    if not doi or not title:
        return {
            "status": "unverified", "via": NAME,
            "reason": "candidate missing required DOI or title",
            "provider_outcome": "candidate_rejected",
        }
    profile = resolve_mod._metadata_match_profile(ref, msg, title)
    hard_conflicts = [
        name for name, present in (
            ("author", bool(profile.get("cited_first_author") and profile.get("matched_first_author") and not profile.get("author_match"))),
            ("year", bool(ref.get("year") is not None and profile.get("matched_year") is not None and not profile.get("year_match") and not profile.get("year_mismatch_plausible"))),
            ("venue", isinstance(profile.get("venue_overlap"), (int, float)) and profile["venue_overlap"] < 0.50),
        ) if present
    ]
    overlap = profile.get("title_overlap")
    if len(hard_conflicts) >= 2 or overlap is None or (
        overlap < resolve_mod.TITLE_WARN_MAX and profile.get("score", 0) < resolve_mod.METADATA_VERIFY_MIN
    ):
        profile = dict(profile)
        if hard_conflicts:
            profile["hard_conflicts"] = hard_conflicts
        return {
            "status": "unverified", "via": NAME, "matched_title": title,
            "reason": "metadata candidate rejected by Callimachus admission checks",
            "resolution_basis": "metadata_search", "existence_confidence": "low",
            "metadata_match": profile, "provider_outcome": "candidate_rejected",
        }
    out = {
        "status": "resolved", "via": NAME, "doi": doi, "matched_title": title,
        "abstract": msg.get("abstract"), "retracted": False,
        "reason": "local biblio-glutton metadata candidate",
        "resolution_basis": "metadata_search", "existence_confidence": "medium",
        "metadata_match": profile, "provider_outcome": "matched",
        # These are claims from the local accelerator, not active identity.
        # The orchestration stage admits them only after DOI validation.
        "candidate_identifiers": _candidate_identifiers(msg, doi),
    }
    out.update(_fulltext_meta(msg, doi))
    return out
