#!/usr/bin/env python3
# core/resolve/providers/biorxiv.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""bioRxiv / medRxiv deterministic preprint helpers.

When a manuscript cites a preprint directly by its bioRxiv/medRxiv DOI
(``10.1101/...``), this provider resolves the canonical full-text PDF for the
latest version. The DOI namespace makes the match deterministic — no fuzzy
title search — and the retrieved text is, by construction, a preprint (NOT the
version of record), so candidates carry ``content_version="preprint"``.

The bioRxiv API needs no key or contact email and degrades gracefully when
unreachable (like the other deterministic providers).
"""

from __future__ import annotations

import json
import urllib.parse

NAME = "biorxiv"
MANIFEST = {
    "doi_prefixes": ["10.1101/"],
    "preprint_host": True,
    "canonical_hosts": ["biorxiv.org", "medrxiv.org"],
    "host_markers": ["biorxiv", "medrxiv"],
}
# Pure preprint resolver: the core consults this group only as a distinct fallback
# stage, after the version of record could not be retrieved.
PREPRINT_RESOLVER = True
SERVERS = ("biorxiv", "medrxiv")
DETAILS_URL = "https://api.biorxiv.org/details/{server}/{doi}/na/json"
CONTENT_HOST = {"biorxiv": "www.biorxiv.org", "medrxiv": "www.medrxiv.org"}


def _is_biorxiv_doi(doi: str | None) -> bool:
    return bool(doi) and str(doi).strip().lower().startswith("10.1101/")


def _details(doi: str, *, get_fn):
    """Return (server, collection) from the first server that knows the DOI."""
    for server in SERVERS:
        url = DETAILS_URL.format(server=server, doi=urllib.parse.quote(doi, safe="/"))
        try:
            _status, body = get_fn(url, accept="application/json", profile="api")
            data = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            continue
        collection = [c for c in (data.get("collection") or []) if isinstance(c, dict)]
        if collection:
            return server, collection
    return None, []


def _latest(collection: list[dict]) -> dict:
    def _vnum(entry: dict) -> int:
        try:
            return int(str(entry.get("version") or "0"))
        except (TypeError, ValueError):
            return 0

    return max(collection, key=_vnum)


def candidate_items(
    ref: dict,
    *,
    get_fn,
    normalize_doi,
    kind_from_url,
    **kwargs,
) -> list[dict]:
    doi = normalize_doi(ref.get("doi"))
    if not _is_biorxiv_doi(doi):
        return []
    server, collection = _details(doi, get_fn=get_fn)
    if not collection:
        return []
    latest = _latest(collection)
    version = str(latest.get("version") or "1").strip() or "1"
    host = CONTENT_HOST.get(server, "www.biorxiv.org")
    url = f"https://{host}/content/{doi}v{version}.full.pdf"
    return [{
        "method": NAME,
        "url": url,
        "kind": "pdf",
        "content_version": "preprint",
    }]
