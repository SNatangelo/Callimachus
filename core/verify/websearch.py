#!/usr/bin/env python3
# core/verify/websearch.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Legacy deterministic web-search helpers retained for parsing and diagnostics.

Why this module exists
----------------------
Earlier versions admitted THIRD-PARTY pages that cite or describe a source under
the `web_secondhand` scope. Generic web pages are no longer citation evidence and
this module is not connected to Fetch or Verify.

The helpers remain deterministic and are retained for focused parser/search tests
and possible non-evidentiary diagnostics.

Scope / honesty
---------------
Outputs from these helpers must not be stored as admissible Verify text or used to
produce a citation verdict.

Backend
-------
Searches go through :mod:`core.search`, which falls through its configured
backends while preserving the difference between an answered empty result and an
unavailable search service.  The local HTML parser remains available only through
the injected ``get_fn`` seam used by offline tests.
"""

from __future__ import annotations

import html
import os
import re
import urllib.parse
import urllib.request

from core import search as _search_router
from core.fetch.extraction import fetch_html
from core.fetch.transport.http_headers import open_request, request_headers

ENV_SEARCH_URL = "CITATION_VERIFIER_SEARCH_URL"
DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/?q={q}"

# A web page is worth keeping as evidence only if it carries some real prose.
_MIN_PAGE_CHARS = 200
# Cap the stored evidence so a runaway page cannot bloat the source text.
_MAX_PAGE_CHARS = 6000
_MAX_DOC_CHARS = 20000

_RESULT_A = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_RESULT_SNIPPET = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _clean(fragment: str) -> str:
    """HTML fragment -> readable text (strip tags + unescape entities)."""
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _real_url(href: str) -> str:
    """DuckDuckGo wraps result links as `//.../l/?uddg=<encoded>`; unwrap to the target."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.endswith("/l/"):
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("uddg"):
            return qs["uddg"][0]
    return href


def build_query(ref: dict) -> str:
    """A search query from the most identifying bits of the reference."""
    parts = []
    title = (ref.get("title") or "").strip()
    if title:
        parts.append(f'"{title}"')
    if not title:
        # No title to anchor on: fall back to the raw entry (trimmed).
        raw = (ref.get("raw_entry") or "").strip()
        if raw:
            parts.append(raw[:160])
    year = ref.get("year")
    if year and not title:
        parts.append(str(year))
    return " ".join(parts).strip()


def search(query: str, *, max_results: int = 5, mailto: str | None = None,
           timeout: int = 20, get_fn=None, run_dir: str | None = None) -> list[dict] | None:
    """Run one web search.

    The default path delegates to the shared backend chain.  It returns a list
    when a backend answered (including ``[]``), or ``None`` when no backend was
    available.  ``get_fn`` retains the direct-HTML parser seam for offline tests.
    """
    if not query:
        return []
    if get_fn is None:
        if run_dir:
            return _search_router.search(
                query, max_results=max_results, run_dir=run_dir
            )
        return _search_router.search(query, max_results=max_results)
    template = (os.environ.get(ENV_SEARCH_URL) or DEFAULT_SEARCH_URL)
    url = template.format(q=urllib.parse.quote(query))
    body = get_fn(url, mailto=mailto, timeout=timeout)
    if not body:
        return []
    text = body.decode("utf-8", errors="replace")
    titles = _RESULT_A.findall(text)
    snippets = [_clean(s) for s in _RESULT_SNIPPET.findall(text)]
    out: list[dict] = []
    seen: set[str] = set()
    for i, (href, title_frag) in enumerate(titles):
        real = _real_url(href)
        if not real or real in seen:
            continue
        seen.add(real)
        out.append({
            "url": real,
            "title": _clean(title_frag),
            "snippet": snippets[i] if i < len(snippets) else "",
        })
        if len(out) >= max_results:
            break
    return out


def _http_get(url: str, *, mailto: str | None = None, timeout: int = 20) -> bytes | None:
    """Minimal GET that honours the configured HTTP profile. Returns body or None."""
    try:
        headers = request_headers(url=url, accept="text/html,*/*", profile="document",
                                  mailto=mailto)
        req = urllib.request.Request(url, headers=headers)
        with open_request(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _page_text(url: str, *, mailto: str | None = None, timeout: int = 20,
               get_fn=None) -> str:
    """Fetch a result page and reduce it to readable prose (best-effort)."""
    body = (get_fn or _http_get)(url, mailto=mailto, timeout=timeout)
    if not body:
        return ""
    decoded = fetch_html.decode_textual_body(body, "text/html")
    if not decoded:
        return ""
    text = fetch_html.extract_page_text(decoded)
    return (text or "").strip()


def gather(ref: dict, *, max_results: int = 5, max_pages: int = 3,
           mailto: str | None = None, search_fn=None, page_fn=None,
           run_dir: str | None = None) -> dict:
    """Search for third-party pages about ``ref`` for non-evidentiary diagnostics.

    Returns ``stored`` when usable evidence was assembled, ``not_found`` when a
    backend answered with no usable results, or ``unavailable`` when every search
    backend refused or was unavailable.  The latter is retryable and must never
    be interpreted as an absence.
    `search_fn(query)->results` and `page_fn(url)->text` are injectable for offline tests.
    """
    query = build_query(ref)
    results = (search_fn or (lambda q: search(
        q, max_results=max_results, mailto=mailto, run_dir=run_dir
    )))(query)
    if results is None:
        return {"status": "unavailable", "retryable": True,
                "text": "", "urls": [], "results": []}
    if not results:
        return {"status": "not_found", "text": "", "urls": [], "results": []}

    blocks: list[str] = []
    urls: list[str] = []
    fetched_pages = 0
    for r in results:
        url = r.get("url") or ""
        snippet = (r.get("snippet") or "").strip()
        title = (r.get("title") or "").strip()
        page = ""
        if fetched_pages < max_pages:
            page = (page_fn or (lambda u: _page_text(u, mailto=mailto)))(url)
            if page:
                fetched_pages += 1
        excerpt = page[:_MAX_PAGE_CHARS] if page else snippet
        if len(excerpt) < _MIN_PAGE_CHARS and snippet:
            # Page too thin to quote from: keep the snippet, which at least exists verbatim.
            excerpt = snippet
        if not excerpt:
            continue
        header = f"SOURCE (third-party page): {title}\nURL: {url}"
        blocks.append(f"{header}\n{excerpt}")
        urls.append(url)

    if not blocks:
        return {"status": "not_found", "text": "", "urls": [], "results": results}
    doc = "\n\n---\n\n".join(blocks)[:_MAX_DOC_CHARS]
    return {"status": "stored", "text": doc, "urls": urls, "results": results}
