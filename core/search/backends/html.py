# core/search/backends/html.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""HTML search-page fallback backend."""

from __future__ import annotations

import os
import urllib.parse

from .. import transport

ENV_SEARCH_URL = "CITATION_VERIFIER_SEARCH_URL"
DEFAULT_HTML_SEARCH_URL = "https://html.duckduckgo.com/html/?q={q}"


def search(query: str, max_results: int, *, run_dir: str | None = None) -> list[dict] | None:
    """Scrape an HTML result page as the final fallback.

    Parsing reuses the HTML result helpers owned by Verify. The default
    DuckDuckGo endpoint commonly refuses automated traffic; that remains
    ``None`` and never becomes a false empty result.
    """
    from core.verify import websearch as _websearch

    template = os.environ.get(ENV_SEARCH_URL) or DEFAULT_HTML_SEARCH_URL
    got = transport._request(template.format(q=urllib.parse.quote(query)))
    if got is None or not transport._answered(got[0]):
        return None
    text = got[1].decode("utf-8", "replace")
    out: list[dict] = []
    for href, title in _websearch._RESULT_A.findall(text):
        real = _websearch._real_url(href)
        if real:
            out.append({"url": real, "title": _websearch._clean(title)})
        if len(out) >= max_results:
            break
    return out
