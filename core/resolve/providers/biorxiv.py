#!/usr/bin/env python3
# core/resolve/providers/biorxiv.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""bioRxiv / medRxiv deterministic preprint helpers.

When a manuscript cites a preprint directly by its bioRxiv/medRxiv DOI
(``10.1101/...``), this provider resolves the canonical full-text PDF. An
explicit numbered content URL selects that revision; otherwise it selects the
latest version. The DOI namespace makes the match deterministic — no fuzzy
title search — and the retrieved text is, by construction, a preprint (NOT the
version of record), so candidates carry ``content_version="preprint"``.

The bioRxiv API needs no key or contact email and degrades gracefully when
unreachable (like the other deterministic providers).
"""

from __future__ import annotations

import json
import re
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
_PREPRINT_CONTENT_HOSTS = frozenset({
    "biorxiv.org", "www.biorxiv.org", "medrxiv.org", "www.medrxiv.org",
})
_PREPRINT_CONTENT_PATH_RE = re.compile(
    r"/content/(?P<doi>10\.1101/[A-Z0-9._()/:+-]+)v(?P<version>[1-9]\d*)"
    r"(?:\.full\.pdf)?/?\Z",
    re.IGNORECASE | re.ASCII,
)


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


def _explicit_preprint_content_identity(url: str | None) -> tuple[str, int] | None:
    """Parse only numbered bioRxiv/medRxiv public content URLs."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.hostname not in _PREPRINT_CONTENT_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return None
    match = _PREPRINT_CONTENT_PATH_RE.fullmatch(parsed.path)
    if match is None:
        return None
    return match.group("doi").lower(), int(match.group("version"))


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
    cited_content = _explicit_preprint_content_identity(ref.get("url"))
    if cited_content is not None and cited_content[0] != str(doi).strip().lower():
        return []
    server, collection = _details(doi, get_fn=get_fn)
    if not collection:
        return []
    if cited_content is None:
        latest = _latest(collection)
        version = str(latest.get("version") or "1").strip() or "1"
    else:
        requested_version = cited_content[1]
        matching_version = None
        for entry in collection:
            try:
                entry_version = int(str(entry.get("version") or "0").strip())
            except (TypeError, ValueError):
                continue
            if entry_version == requested_version:
                matching_version = entry_version
                break
        if matching_version is None:
            return []
        version = str(matching_version)
    host = CONTENT_HOST.get(server, "www.biorxiv.org")
    url = f"https://{host}/content/{doi}v{version}.full.pdf"
    return [{
        "method": NAME,
        "url": url,
        "kind": "pdf",
        "content_version": "preprint",
    }]
