#!/usr/bin/env python3
# core/fetch/fallbacks/fetch_modes/interactive_fallback.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in headed-browser fallback for evidenced, still-missing full texts.

This mode deliberately limits browser navigation to HTTP(S) routes already
present in a fetch failure/trace, the parsed reference, or trusted resolver
metadata.  It does not construct search targets or turn identifiers into new
URLs.  The default variant excludes routes explicitly reported as closed;
``interactive_fallback_closed`` opts into those routes for a user who has
legitimate publisher or institutional access.
"""

from __future__ import annotations

from urllib.parse import urlparse

from core.fetch.fallbacks.fetch_modes import interactive_browser

NAME = "interactive_fallback"
MODE_NAME = "interactive_fallback"
ALIASES = ("interactive_recovery", "browser_fallback")
# This mode needs resolver evidence without opting into closed-access routes.
USES_RESOLVE_MAP = True

_CLOSED_TEXT = (
    "access denied",
    "access_denied",
    "paywall",
    "paywalled",
    "subscription",
    "purchase",
    "institutional access",
    "sign in",
    "login",
    "log in",
    "closed access",
    "closed resource",
)
_CLOSED_OUTCOMES = {"access_denied", "paywall", "paywalled", "closed"}


def _http_url(value) -> str | None:
    """Keep only explicit browser-navigable HTTP(S) routes."""
    url = str(value or "").strip()
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return url


def _is_closed(row: dict | None) -> bool:
    row = row or {}
    outcome = str(row.get("outcome") or "").strip().lower()
    if outcome in _CLOSED_OUTCOMES:
        return True
    text = " ".join(
        str(row.get(key) or "").lower()
        for key in (
            "reason", "status", "oa_status", "access", "availability",
            "fulltext_availability",
        )
    )
    return any(marker in text for marker in _CLOSED_TEXT)


def _append(routes: list[tuple[str, bool]], seen: set[str], value, *, closed: bool) -> None:
    url = _http_url(value)
    if url and url not in seen:
        seen.add(url)
        routes.append((url, closed))


def _result_routes(result: dict) -> list[tuple[str, bool]]:
    """Extract only URLs actually attempted or reported by deterministic fetch."""
    routes, seen = [], set()
    for row in result.get("failures") or []:
        if not isinstance(row, dict):
            continue
        for key in ("url", "final_url"):
            _append(routes, seen, row.get(key), closed=_is_closed(row))
    attempts = ((result.get("fetch_trace") or {}).get("execution") or {}).get("attempts") or []
    for row in attempts:
        if not isinstance(row, dict):
            continue
        for key in ("url", "final_url"):
            _append(routes, seen, row.get(key), closed=_is_closed(row))
    # Some older results retain the final route but not a failure row.
    _append(routes, seen, result.get("url"), closed=_is_closed(result))
    return routes


def _candidate_generation_routes(result: dict) -> list[tuple[str, bool]]:
    """Retain explicit pipeline candidates left untried by a budget or deadline."""
    routes, seen = [], set()
    generated = ((result.get("fetch_trace") or {}).get("candidate_generation") or {})
    # The deterministic pipeline has already materialized these routes.  Reuse
    # them verbatim; the fallback still does not derive a URL from a DOI itself.
    _append(routes, seen, generated.get("reference_url"), closed=_is_closed(result))
    _append(routes, seen, generated.get("doi_landing"), closed=_is_closed(result))
    for key in ("final_queue", "oa_alternate_queue", "preprint_queue", "metadata_links"):
        for item in generated.get(key) or []:
            if isinstance(item, dict):
                _append(routes, seen, item.get("url"), closed=_is_closed(item))
    # These rows are serialized only from this fetch pipeline's provider
    # candidate results.  Do not derive targets from provider names or errors:
    # an explicit HTTP(S) item is the sole admissible provider route.
    for provider_row in generated.get("provider_candidates") or []:
        if not isinstance(provider_row, dict):
            continue
        for item in provider_row.get("items") or []:
            if isinstance(item, dict):
                _append(routes, seen, item.get("url"), closed=_is_closed(item))
    return routes


def _reference_routes(ref: dict, resolution: dict, *, result_closed: bool) -> list[tuple[str, bool]]:
    """Extract explicit citation URLs and resolver-provided full-text routes."""
    routes, seen = [], set()
    # A final closed result applies to the cited route it identifies, not to
    # every resolver location for the work (an auxiliary repository copy may
    # still be open).
    _append(routes, seen, (ref or {}).get("url"), closed=result_closed)
    for key in ("fulltext_links", "auxiliary_fulltext_links"):
        for link in resolution.get(key) or []:
            if isinstance(link, dict):
                _append(routes, seen, link.get("url"), closed=_is_closed(link))
    return routes


def _domain(url: str) -> str | None:
    host = (urlparse(url).netloc or "").lower()
    return host[4:] if host.startswith("www.") else (host or None)


def groups(need, refs, fetch_results, *, resolve_map=None, include_closed=False):
    """Group incomplete references by host without adding speculative routes."""
    grouped, ref_ids = {}, set()
    results = {
        item.get("ref_id"): item for item in (fetch_results or [])
        if isinstance(item, dict) and item.get("ref_id")
    }
    for item in need or []:
        ref_id = item.get("ref_id") if isinstance(item, dict) else item
        ref = (refs or {}).get(ref_id)
        if not ref:
            continue
        result = results.get(ref_id) or {}
        resolution = ((resolve_map or {}).get(ref_id) or {})
        route_closed, route_order = {}, []
        for url, closed in (
            _result_routes(result)
            + _candidate_generation_routes(result)
            + _reference_routes(ref, resolution, result_closed=_is_closed(result))
        ):
            if url not in route_closed:
                route_order.append(url)
                route_closed[url] = closed
            else:
                # A later trace/resolver row explicitly closing the same route
                # must take precedence over a generic earlier failure.
                route_closed[url] = route_closed[url] or closed
        routes = [
            (url, route_closed[url]) for url in route_order
            if include_closed or not route_closed[url]
        ]
        by_domain = {}
        for url, closed in routes:
            host = _domain(url)
            if host:
                by_domain.setdefault(host, []).append((url, closed))
        for host, host_routes in by_domain.items():
            ref_ids.add(ref_id)
            bucket = grouped.setdefault(host, {
                "domain": host,
                "references": [],
                "candidate_urls": [],
                "requires_legitimate_access": False,
            })
            urls = [url for url, _closed in host_routes]
            closed = any(is_closed for _url, is_closed in host_routes)
            bucket["references"].append({
                "ref_id": ref_id,
                "ref_number": ref.get("ref_number"),
                "raw_entry": ref.get("raw_entry"),
                "doi": ref.get("doi"),
                "url": ref.get("url"),
                "candidate_urls": urls,
            })
            bucket["requires_legitimate_access"] |= closed
            for url in urls:
                if url not in bucket["candidate_urls"]:
                    bucket["candidate_urls"].append(url)
    return list(grouped.values()), ref_ids


def recover(grouped, run_dir: str) -> dict:
    """Use the shared headed-browser capture; normal fetch gates remain downstream."""
    return interactive_browser.recover(grouped, run_dir)
