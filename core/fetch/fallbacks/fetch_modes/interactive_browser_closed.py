#!/usr/bin/env python3
# core/fetch/fallbacks/fetch_modes/interactive_browser_closed.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in headed browser retrieval for challenge and closed-access candidates.

This mode is for a user who can legitimately authenticate to a publisher or
institution.  It does not supply credentials, solve challenges, or relax any
content or identity checks; it merely preserves browser-session responses for
the normal fetch pipeline.
"""

from __future__ import annotations

import urllib.parse

from core.fetch.fallbacks.fetch_modes import browser_challenge
from core.fetch.fallbacks.fetch_modes import interactive_browser

NAME = "interactive_browser_closed"
MODE_NAME = "interactive_closed"
ALIASES = ("interactive_access", "interactive_browser_closed")
CLOSED_ACCESS_MODE = True

_CLOSED_MARKERS = (
    "paywall",
    "paywalled",
    "access denied",
    "access_denied",
    "subscription",
    "purchase",
    "institutional access",
    "sign in",
    "login",
    "log in",
    "closed access",
    "closed",
)


def _closed_failure_items(fetch_result: dict) -> list[dict]:
    """Return only failures that explicitly describe a closed resource."""
    items = list(browser_challenge.failure_items(fetch_result))
    for failure in fetch_result.get("failures") or []:
        reason = str(failure.get("reason") or "").lower()
        if any(marker in reason for marker in _CLOSED_MARKERS) and failure not in items:
            items.append(failure)
    return items


def _closed_trace_items(fetch_result: dict) -> list[dict]:
    """Trace rows retain access-denied/paywall outcomes without guessing URLs."""
    attempts = ((fetch_result.get("fetch_trace") or {}).get("execution") or {}).get("attempts") or []
    return [
        attempt for attempt in attempts
        if str(attempt.get("outcome") or "").lower()
        in {"challenge_blocked", "access_denied", "paywall", "paywalled", "closed"}
    ]


def _resolver_closed_urls(ref: dict, resolution: dict) -> list[str]:
    """Trusted resolver routes for a work explicitly labelled closed/paywalled."""
    state = " ".join(str(resolution.get(key) or "").lower()
                     for key in ("oa_status", "reason", "fulltext_exists"))
    if not any(marker in state for marker in _CLOSED_MARKERS):
        return []
    urls = []
    dois = [ref.get("doi")]
    for key in ("fulltext_links", "auxiliary_fulltext_links"):
        for item in resolution.get(key) or []:
            if not isinstance(item, dict):
                continue
            if item.get("url") and item["url"] not in urls:
                urls.append(item["url"])
            context = item.get("identity_context") or {}
            identifiers = context.get("identifiers") if isinstance(context, dict) else {}
            if isinstance(identifiers, dict):
                dois.append(identifiers.get("doi"))
    if ref.get("url") and ref["url"] not in urls:
        urls.append(ref["url"])
    for doi in dois:
        doi = str(doi or "").strip()
        if not doi:
            continue
        url = "https://doi.org/" + urllib.parse.quote(doi, safe="")
        if url not in urls:
            urls.append(url)
    return urls


def groups(need, refs, fetch_results, *, resolve_map=None):
    """Group challenge plus explicit closed-access URLs by publisher domain."""
    grouped = {}
    ref_ids = set()
    by_ref_id = {result.get("ref_id"): result for result in (fetch_results or []) if result.get("ref_id")}
    for item in need:
        ref_id = item.get("ref_id") if isinstance(item, dict) else item
        ref = refs.get(ref_id)
        result = by_ref_id.get(ref_id) or {}
        resolution = (resolve_map or {}).get(ref_id) or {}
        failures = _closed_failure_items(result)
        trace_items = _closed_trace_items(result)
        urls = []
        for entry in [*failures, *trace_items]:
            for key in ("url", "final_url"):
                url = entry.get(key)
                if url and url not in urls:
                    urls.append(url)
        for url in _resolver_closed_urls(ref or {}, resolution):
            if url not in urls:
                urls.append(url)
        # A final paywall summary has no failure URL in older pipeline rows.
        # Only use the explicitly cited URL; never invent a publisher target.
        reason = str(result.get("reason") or "").lower()
        if not urls and ref and ref.get("url") and any(marker in reason for marker in _CLOSED_MARKERS):
            urls.append(ref["url"])
        if not ref or not urls:
            continue
        by_domain = {}
        for url in urls:
            host = browser_challenge.domain(url)
            if host:
                by_domain.setdefault(host, []).append(url)
        for host, host_urls in by_domain.items():
            ref_ids.add(ref_id)
            bucket = grouped.setdefault(host, {
                "domain": host,
                "references": [],
                "candidate_urls": [],
                "requires_legitimate_access": True,
            })
            bucket["references"].append({
                "ref_id": ref_id,
                "ref_number": ref.get("ref_number"),
                "raw_entry": ref.get("raw_entry"),
                "doi": ref.get("doi"),
                "url": ref.get("url"),
                "candidate_urls": host_urls,
            })
            for url in host_urls:
                if url not in bucket["candidate_urls"]:
                    bucket["candidate_urls"].append(url)
    return list(grouped.values()), ref_ids


def recover(grouped, run_dir: str) -> dict:
    """Delegate session capture to the shared headed Playwright implementation."""
    return interactive_browser.recover(grouped, run_dir)
