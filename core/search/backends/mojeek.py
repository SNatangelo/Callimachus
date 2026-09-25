# core/search/backends/mojeek.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Mojeek search backend."""

from __future__ import annotations

import json
import os
import urllib.parse

from .. import transport

ENV_MOJEEK_KEY = "MOJEEK_API_KEY"
MOJEEK_URL = "https://api.mojeek.com/search"


def search(query: str, max_results: int, *, run_dir: str | None = None) -> list[dict] | None:
    """Return Mojeek results, or None when Mojeek cannot answer."""
    key = (os.environ.get(ENV_MOJEEK_KEY) or "").strip()
    if not key:
        return None
    url = MOJEEK_URL + "?" + urllib.parse.urlencode({
        "q": query, "api_key": key, "fmt": "json",
        "t": max(1, min(int(max_results), 50)),
    })
    got = transport._request(
        url, run_dir=run_dir, credential=("mojeek", ENV_MOJEEK_KEY)
    )
    if got is None or not transport._answered(got[0]):
        return None
    try:
        data = json.loads(got[1].decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None
    results = ((data or {}).get("response") or {}).get("results")
    if not isinstance(results, list):
        return None
    return [{"url": item.get("url"), "title": item.get("title") or ""}
            for item in results if isinstance(item, dict) and item.get("url")]
