#!/usr/bin/env python3
# core/fetch/fallbacks/perma.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Perma.cc backup-link fallback for the fetch pipeline.

Law reviews require authors to perma-archive every web citation, so a Bluebook
footnote carries its own backup link in brackets: ``[https://perma.cc/B2HM-PVPE]``.
When the live fetch of such a citation dies of link rot or a bot wall, that code
is a second, author-designated address for the same source — already sitting in
the reference's raw text, costing no lookup to discover.

What this module does *not* do is read the capture itself. A perma viewer page is
a JavaScript playback shell (``<div id="iframe-target">``, an ``about:blank``
iframe filled at runtime); ``/download`` and ``/capture`` are 404 and the public
API answers 429 from here, so the archived bytes are out of reach without a
browser. What the shell does state, in a ``<meta name="description">``, is the
original URL and the capture date. That is the useful part: it recovers the
*canonical* source URL, which is frequently not the URL our own parse holds —
PDF extraction breaks long URLs across lines — and hands it to the normal fetch
path, and to Wayback after it, in a form worth trying again.

So the chain is: dead/mangled citation URL → perma states the true original →
refetch that → failing which, an archived snapshot of that. Recovery, not
substitution: nothing here is ever stored as source text.
"""

from __future__ import annotations

import datetime
import html
import os
import re
import urllib.parse

ENV_PERMA = "CITATION_VERIFIER_PERMA"
_VIEWER = "https://perma.cc/{}"
_PERMA_HOSTS = ("perma.cc", "perma-archives.org")

# The details tray, rendered server-side on every viewer: an input labelled
# "Source page URL" holding the captured address.
_SOURCE_URL_RE = re.compile(
    r'id=["\']source_url["\'][^>]*\svalue=["\'](?P<url>[^"\']+)["\']', re.I)
# Read second only because it is not always the archive statement: when the
# captured page carried its own description, perma serves that instead, which is
# how a title-only meta silently passed for an archive-of line.
_ARCHIVE_OF_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']\s*'
    r'This is an archive of\s+(?P<url>\S+?)\s+from\s+(?P<captured>[^"\']+?)\s*["\']',
    re.I | re.S,
)
_TRAY_TITLE_RE = re.compile(
    r'<dt[^>]*>\s*Title\s*</dt>\s*<dd[^>]*>(?P<title>.*?)</dd>', re.I | re.S)
_TITLE_RE = re.compile(r"<title>\s*(?:Perma\s*\|\s*)?(.*?)\s*</title>", re.I | re.S)
# The capture instant, given to the page's clock script as a Unix timestamp.
_CAPTURED_TS_RE = re.compile(r"insertLocalDateTime\(\s*'[^']*'\s*,\s*(?P<ts>\d{9,11})\s*,")
_CAPTURED_TEXT_RE = re.compile(
    r'class=["\']creation["\'].*?<noscript>\s*(?P<captured>[^<]+?)\s*</noscript>', re.I | re.S)


def _text(raw: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", raw or "")).strip()

# A perma code is two short alphanumeric groups: B2HM-PVPE.
_CODE_RE = re.compile(r"(?:https?://)?perma\.cc/([A-Za-z0-9]{2,8}-[A-Za-z0-9]{2,8})$", re.I)
_BRACKET_RE = re.compile(r"\[([^\[\]]{8,120})\]")
_BARE_RE = re.compile(r"https?://perma\.cc/([A-Za-z0-9]{2,8}-[A-Za-z0-9]{2,8})\b", re.I)


def enabled(environ: dict | None = None) -> bool:
    value = str((environ or os.environ).get(ENV_PERMA, "1")).strip().lower()
    return value not in ("0", "false", "no", "off")


def is_perma_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url or "").netloc or "").lower()
    return any(host == h or host.endswith("." + h) for h in _PERMA_HOSTS)


def codes(raw_entry: str) -> list[str]:
    """Perma codes cited in a reference's raw text, in order, deduplicated.

    Reading the bracket first rather than the URL is what makes this work on real
    PDF text. Extraction breaks a code across a line — ``perma.cc/QWN2-`` then
    ``7NZ2]`` — and a URL-shaped regex stops at the whitespace, silently yielding
    a truncated code that 404s. The closing bracket is the true delimiter, so the
    content is joined before it is matched: over the paper at hand that is the
    difference between 52 and all 65 links.
    """
    found: list[str] = []
    seen: set[str] = set()

    def keep(code: str) -> None:
        code = code.upper()
        if code not in seen:
            seen.add(code)
            found.append(code)

    for match in _BRACKET_RE.finditer(raw_entry or ""):
        joined = re.sub(r"\s+", "", match.group(1))
        code_match = _CODE_RE.match(joined)
        if code_match:
            keep(code_match.group(1))
    for match in _BARE_RE.finditer(raw_entry or ""):
        keep(match.group(1))
    return found


def archived_original(code: str, *, fetch_url) -> dict | None:
    """Return ``{"url", "title", "captured"}`` for a perma code, or None.

    ``fetch_url`` is the pipeline's fetcher. Only a viewer page that states an
    archive of a non-perma URL counts; anything else fails closed.
    """
    if not code:
        return None
    try:
        result = fetch_url(_VIEWER.format(code), accept="text/html", profile="document")
    except Exception:
        return None
    if not isinstance(result, dict) or str(result.get("status") or "") != "200":
        return None
    body = result.get("body") or b""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    stated = _ARCHIVE_OF_RE.search(body)
    source = _SOURCE_URL_RE.search(body)
    url = _text(source.group("url")) if source else (
        (stated.group("url") or "").strip() if stated else "")
    if not url.lower().startswith(("http://", "https://")) or is_perma_url(url):
        return None

    tray_title = _TRAY_TITLE_RE.search(body)
    page_title = _TITLE_RE.search(body)
    title = _text(tray_title.group("title")) if tray_title else (
        _text(page_title.group(1)) if page_title else "")

    captured = ""
    timestamp = _CAPTURED_TS_RE.search(body)
    if timestamp:
        captured = datetime.datetime.fromtimestamp(
            int(timestamp.group("ts")), datetime.timezone.utc).date().isoformat()
    elif stated:
        captured = (stated.group("captured") or "").strip()
    else:
        text_date = _CAPTURED_TEXT_RE.search(body)
        captured = _text(text_date.group("captured")) if text_date else ""

    return {"url": url, "title": title, "captured": captured}


def build_candidates(raw_entry, *, fetch_url, seen=None, max_lookups: int = 2) -> list[dict]:
    """Turn a reference's perma backup links into fetch candidates.

    Each candidate points at the *original* URL the archive names, not at perma
    itself — the viewer page holds no readable text. At most ``max_lookups``
    codes are resolved per reference; originals already tried are skipped.
    """
    seen = seen if seen is not None else set()
    out: list[dict] = []
    looked = 0
    for code in codes(raw_entry):
        if looked >= max_lookups:
            break
        looked += 1
        original = archived_original(code, fetch_url=fetch_url)
        if not original:
            continue
        url = original["url"]
        if url in seen:
            continue
        seen.add(url)
        captured = original.get("captured") or "an unstated date"
        out.append({
            "url": url,
            "kind": "pdf" if url.split("?", 1)[0].rstrip("/").lower().endswith(".pdf") else "html",
            "method": "perma",
            "fallback_stage": "perma",
            "referer": None,
            "discovery_reason": (
                f"perma.cc/{code} archives this URL as of {captured}"
                + (f": {original['title']}" if original.get("title") else "")
            ),
        })
    return out
