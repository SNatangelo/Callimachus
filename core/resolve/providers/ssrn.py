#!/usr/bin/env python3
# core/resolve/providers/ssrn.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""SSRN-specific deterministic fetch helpers."""

from __future__ import annotations

import re

NAME = "ssrn"
MANIFEST = {
    "doi_prefixes": ["10.2139/ssrn."],
    "preprint_host": True,
    "canonical_hosts": ["ssrn.com", "papers.ssrn.com"],
    "host_markers": ["ssrn"],
}


def landing_url(doi: str | None) -> str | None:
    if not doi:
        return None
    m = re.match(r"^10\.2139/ssrn\.(\d+)$", doi, flags=re.IGNORECASE)
    if not m:
        return None
    return f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={m.group(1)}"


def candidate_items(ref: dict, **kwargs) -> list[dict]:
    normalize_doi = kwargs.get("normalize_doi") or (lambda x: x)
    url = landing_url(normalize_doi(ref.get("doi")))
    if not url:
        return []
    return [{"method": NAME, "url": url, "kind": "landing"}]
