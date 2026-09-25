#!/usr/bin/env python3
# core/fetch/fallbacks/fetch_modes/browser_challenge.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional browser-session challenge queue for blocked publisher domains."""

from __future__ import annotations

import os
import urllib.parse

try:
    from core.fetch.fallbacks import fetch_modes as _registry
except ImportError:
    import fetch_modes as _registry

NAME = "browser_challenge"
MODE_NAME = "queue"
ALIASES = ("browser_challenge",)
ENV_CHALLENGE_MODE = _registry.ENV_CHALLENGE_MODE
CHALLENGE_MODE_CHOICES = ("off", MODE_NAME) + ALIASES
DEFAULT_CHALLENGE_MODE = _registry.DEFAULT_CHALLENGE_MODE
CHALLENGE_REASON = "publisher challenge page blocked automated retrieval"


def configured_challenge_mode() -> str:
    return _registry.configured_mode_name()


def enabled() -> bool:
    mod = _registry.selected_module()
    return bool(mod and getattr(mod, "MODE_NAME", None) == MODE_NAME)


def failure_items(fetch_result: dict) -> list[dict]:
    return [
        f for f in (fetch_result.get("failures") or [])
        if CHALLENGE_REASON in str(f.get("reason") or "")
    ]


def domain(url: str | None) -> str | None:
    if not url:
        return None
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def groups(need, refs, fetch_results):
    grouped = {}
    challenge_ref_ids = set()
    by_ref_id = {
        r.get("ref_id"): r for r in (fetch_results or [])
        if r.get("ref_id")
    }
    for item in need:
        ref_id = item.get("ref_id") if isinstance(item, dict) else item
        ref = refs.get(ref_id)
        fetched = by_ref_id.get(ref_id) or {}
        failures = failure_items(fetched)
        if not ref or not failures:
            continue
        domains = []
        urls = []
        seen_urls = set()
        for failure in failures:
            url = failure.get("url")
            host = domain(url)
            if url and url not in seen_urls:
                seen_urls.add(url)
                urls.append(url)
            if host and host not in domains:
                domains.append(host)
        if not domains:
            continue
        challenge_ref_ids.add(ref_id)
        host = domains[0]
        bucket = grouped.setdefault(host, {
            "domain": host,
            "references": [],
            "candidate_urls": [],
        })
        bucket["references"].append({
            "ref_id": ref_id,
            "ref_number": ref.get("ref_number"),
            "raw_entry": ref.get("raw_entry"),
            "doi": ref.get("doi"),
            "url": ref.get("url"),
            "candidate_urls": urls,
        })
        for url in urls:
            if url not in bucket["candidate_urls"]:
                bucket["candidate_urls"].append(url)
    return list(grouped.values()), challenge_ref_ids
