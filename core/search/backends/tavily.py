# core/search/backends/tavily.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Tavily search backend."""

from __future__ import annotations

import json
import os

from .. import transport

ENV_TAVILY_KEY = "TAVILY_API_KEY"
TAVILY_URL = "https://api.tavily.com/search"


def search(query: str, max_results: int, *, run_dir: str | None = None) -> list[dict] | None:
    """Return Tavily results, or None when Tavily cannot answer."""
    key = (os.environ.get(ENV_TAVILY_KEY) or "").strip()
    if not key:
        return None
    payload = json.dumps({
        "query": query,
        "max_results": max(1, min(int(max_results), 20)),
        "search_depth": "basic",
    }).encode("utf-8")
    got = transport._request(TAVILY_URL, data=payload, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }, run_dir=run_dir, credential=("tavily", ENV_TAVILY_KEY))
    if got is None or not transport._answered(got[0]):
        return None
    try:
        data = json.loads(got[1].decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None
    if not isinstance(data.get("results"), list):
        return None
    return [{"url": item.get("url"), "title": item.get("title") or ""}
            for item in data["results"] if isinstance(item, dict) and item.get("url")]
