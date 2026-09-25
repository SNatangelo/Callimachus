#!/usr/bin/env python3
# core/resolve/identifiers.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Identifier-driven resolver helpers."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse

try:
    from .providers.europepmc import (
        _epmc_fulltext_meta,
        _europepmc,
        _pmc_links_from_ids,
        _pubmed_enrich,
        _pubmed_citation_match,
        _pubmed_fetch_metadata,
        _pubmed_search_pmid,
        _xml_text,
        pubmed_exists,
    )
except ImportError:
    from resolve.providers.europepmc import (
        _epmc_fulltext_meta,
        _europepmc,
        _pmc_links_from_ids,
        _pubmed_enrich,
        _pubmed_citation_match,
        _pubmed_fetch_metadata,
        _pubmed_search_pmid,
        _xml_text,
        pubmed_exists,
    )

_CONFERENCE_TYPES = ("proceedings", "conference")


def _resolve_module():
    try:
        from core.resolve import service as resolve_mod
    except ImportError:
        import service as resolve_mod
    return resolve_mod


def _is_conference_type(work_type) -> bool:
    wt = (work_type or "").lower()
    return any(key in wt for key in _CONFERENCE_TYPES)


def _doi_handle(doi: str) -> dict:
    """Check the DOI Foundation handle resolver."""
    resolve_mod = _resolve_module()
    url = f"https://doi.org/api/handles/{urllib.parse.quote(doi)}"
    try:
        _status, body = resolve_mod._get(url)
        data = json.loads(body)
        response_code = data.get("responseCode")
        if response_code in {1, 200}:
            return {
                "status": "resolved",
                "via": "doi.org",
                "matched_title": None,
                "abstract": None,
                "retracted": False,
                "fulltext_exists": "unknown",
                "oa_status": "unknown",
                "work_type": "doi",
                "reason": (
                    None if response_code == 1
                    else "DOI handle exists; requested Handle values were not present"
                ),
            }
        if response_code == 100:
            return {
                "status": "not_found",
                "via": "doi.org",
                "reason": "DOI not found by DOI resolver (Handle responseCode 100)",
            }
        return {
            "status": "unresolved",
            "via": "doi.org",
            "reason": f"inconclusive Handle responseCode {response_code!r} - NOT fabrication",
        }
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "status": "not_found",
                "via": "doi.org",
                "reason": "DOI not found by DOI resolver (HTTP 404)",
            }
        if exc.code == 429:
            return {
                "status": "unresolved",
                "via": "doi.org",
                "reason": "rate_limited (HTTP 429) - NOT fabrication",
            }
        return {"status": "unresolved", "via": "doi.org", "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"status": "unresolved", "via": "doi.org", "reason": f"network: {type(exc).__name__}"}
