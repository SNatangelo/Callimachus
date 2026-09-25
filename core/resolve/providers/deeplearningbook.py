#!/usr/bin/env python3
# core/resolve/providers/deeplearningbook.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Official web edition of Goodfellow, Bengio, and Courville's Deep Learning."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import re
import urllib.parse

NAME = "deeplearningbook"
RESOLVE_NAME = "deeplearningbook_search"
OPTIONAL_STAGE = True
CANONICAL_COMPANION = True
MANIFEST = {"canonical_hosts": ["deeplearningbook.org", "www.deeplearningbook.org"]}

TITLE = "Deep Learning"
AUTHORS = ["Ian Goodfellow", "Yoshua Bengio", "Aaron Courville"]
YEAR = 2016
LANDING_URL = "https://www.deeplearningbook.org/"
TOC_URL = "https://www.deeplearningbook.org/contents/TOC.html"


def _norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def supports(ref: dict) -> bool:
    # Fetch uses a resolved discovery view for identifiers and candidate URLs.
    # This exact canonical provider must still validate the identity stated in
    # the citation, rather than a weak catalog title selected during discovery.
    identity_ref = getattr(ref, "_provider_callback_cited_ref", None) or ref
    title = _norm(identity_ref.get("title"))
    raw = _norm(identity_ref.get("raw_entry"))
    haystack = f"{title} {raw}"
    try:
        year = int(identity_ref.get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    return (
        title == "deep learning"
        and all(author in haystack for author in ("goodfellow", "bengio", "courville"))
        and year in (0, YEAR)
    )


def _identity_context() -> dict:
    return {
        "provider": RESOLVE_NAME,
        "canonical_host": True,
        "title": TITLE,
        "authors": AUTHORS,
        "year": YEAR,
    }


def discover(ref: dict) -> dict:
    if not supports(ref):
        return {"status": "unverified", "via": RESOLVE_NAME, "reason": "not the supported Deep Learning book"}
    return {
        "status": "resolved",
        "via": RESOLVE_NAME,
        "matched_title": TITLE,
        "matched_authors": AUTHORS,
        "abstract": None,
        "retracted": False,
        "fulltext_exists": True,
        "oa_status": "open",
        "work_type": "book",
        "resolution_basis": "canonical_source",
        "existence_confidence": "high",
        "reason": "official author-hosted web edition matched by title, authors, and year",
        "resolved_identifier": {
            "type": "canonical_url",
            "value": TOC_URL,
            "validated_via": RESOLVE_NAME,
        },
        "fulltext_links": [
            {"url": LANDING_URL, "content_type": "text/html", "identity_context": _identity_context()}
        ],
    }


def _decode(body: object) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body or "")


def _page_text(html_text: str) -> str:
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html_text)
    body = re.sub(r"(?is)<!--.*?-->", " ", body)
    body = re.sub(r"(?i)<br\s*/?>|</(?:p|div|section|article|h[1-6]|li|tr)>", "\n", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    return re.sub(r"\n\s*\n+", "\n\n", body).strip()


def _chapter_urls(toc_html: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for href in re.findall(r"(?is)<a\b[^>]*\bhref\s*=\s*['\"]([^'\"]+)['\"]", toc_html):
        url = urllib.parse.urljoin(LANDING_URL, html.unescape(href).strip())
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.hostname not in {"deeplearningbook.org", "www.deeplearningbook.org"}
            or not parsed.path.startswith("/contents/")
            or not parsed.path.lower().endswith(".html")
            or parsed.path.lower().endswith("/toc.html")
        ):
            continue
        canonical = urllib.parse.urlunparse(("https", "www.deeplearningbook.org", parsed.path, "", "", ""))
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


def _fetch_page(url: str, get_fn) -> tuple[str, str] | None:
    try:
        status, body = get_fn(
            url,
            accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            profile="document",
        )
        if not 200 <= int(status) < 300:
            return None
    except (OSError, TypeError, ValueError):
        return None
    text = _page_text(_decode(body))
    return (url, text) if len(text) >= 500 else None


def direct_text_items(ref: dict, *, get_fn, **kwargs) -> list[dict]:
    if not supports(ref):
        return []
    try:
        status, body = get_fn(
            LANDING_URL,
            accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            profile="document",
        )
        if not 200 <= int(status) < 300:
            return []
    except (OSError, TypeError, ValueError):
        return []
    landing_html = _decode(body)
    urls = _chapter_urls(landing_html)
    if len(urls) < 3:
        return []

    fetched: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(urls))) as pool:
        futures = {pool.submit(_fetch_page, url, get_fn): url for url in urls}
        for future in as_completed(futures):
            try:
                item = future.result()
            except Exception:
                item = None
            if item:
                fetched[item[0]] = item[1]

    # Reconstruct TOC order, independent of network completion order.
    chapters = [fetched[url] for url in urls if url in fetched]
    if len(chapters) < 3:
        return []
    identity_header = f"{TITLE}\nIan Goodfellow, Yoshua Bengio, Aaron Courville\nMIT Press, {YEAR}"
    text = "\n\n".join([identity_header, _page_text(landing_html), *chapters])
    if len(text) < 10_000:
        return []
    return [{
        "method": NAME,
        "text": text,
        "source_ref": LANDING_URL,
        "extract_method": "official_web_edition",
    }]
