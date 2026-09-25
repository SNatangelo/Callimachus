#!/usr/bin/env python3
# core/resolve/providers/cvf.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""CVF Open Access provider for CVPR, ICCV, and WACV proceedings.

CVF has used more than one directory convention over time.  This provider
discovers paper pages from the official conference index instead of guessing a
paper slug, then derives the sibling PDF path from the discovered page.
"""

from __future__ import annotations

import html
import re
import time
import unicodedata
import urllib.parse
import urllib.request

NAME = "cvf"
MANIFEST = {
    "doi_prefixes": ["10.1109/cvpr.", "10.1109/iccv.", "10.1109/wacv."],
    "canonical_hosts": ["openaccess.thecvf.com"],
}
API_BASE = "https://openaccess.thecvf.com"

_VENUE_SPECS = (
    {"name": "CVPR", "doi_prefixes": ("10.1109/cvpr.",), "markers": ("conference on computer vision and pattern recognition", " cvpr")},
    {"name": "ICCV", "doi_prefixes": ("10.1109/iccv.",), "markers": ("international conference on computer vision", " iccv")},
    {"name": "WACV", "doi_prefixes": ("10.1109/wacv.",), "markers": ("winter conference on applications of computer vision", " wacv")},
)


def _norm(text: object) -> str:
    text = html.unescape(str(text or ""))
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _ref_year(ref: dict) -> int | None:
    try:
        value = int(ref.get("year") or 0)
    except (TypeError, ValueError):
        return None
    return value if 1900 <= value <= 2100 else None


def _first_author(ref: dict) -> str | None:
    for key in ("ay_surname", "surname", "first_author_surname"):
        value = _norm(ref.get(key))
        if value:
            return value.split()[-1]
    raw, title = str(ref.get("raw_entry") or ""), str(ref.get("title") or "")
    if title:
        found = re.search(re.escape(title), raw, re.I)
        if found:
            raw = raw[:found.start()]
    tokens = _norm(re.split(r"\s+(?:and|&)\s+|,|;", raw, maxsplit=1)[0]).split()
    return tokens[-1] if len(tokens) >= 2 else None


def _author_surname(value: object) -> str | None:
    first = re.split(r"\s+(?:and|&)\s+|,|;", str(value or ""), maxsplit=1)[0]
    tokens = _norm(first).split()
    return tokens[-1] if tokens else None


def _likely_venues(ref: dict) -> list[dict]:
    if _ref_year(ref) is None:
        return []
    raw = " " + " ".join(str(ref.get(key) or "") for key in ("venue", "journal", "raw_entry", "doi", "url")).lower()
    return [spec for spec in _VENUE_SPECS if any(prefix in raw for prefix in spec["doi_prefixes"]) or any(marker in raw for marker in spec["markers"])]


def _index_urls(spec: dict, year: int, title: str | None = None) -> list[str]:
    # Both bare proceedings pages and ?day=all are official, and the latter is
    # needed by some newer CVF layouts to expose the complete index.
    base = f"{API_BASE}/{spec['name']}{year}"
    out = []
    if title:
        out.append(
            f"{API_BASE}/{spec['name']}{year}_search.py?"
            + urllib.parse.urlencode({"query": title})
        )
    return out + [base, f"{base}?day=all"]


def _extract_entries(index_html: str, index_url: str) -> list[dict]:
    """Extract paper detail links from legacy and current CVF index markup."""
    anchors = re.findall(r'<a\b[^>]*href=["\']([^"\']+\.html?(?:\?[^"\']*)?)["\'][^>]*>(.*?)</a>', index_html or "", re.I | re.S)
    out, seen = [], set()
    for href, label in anchors:
        clean_href = html.unescape(href).strip()
        title = html.unescape(re.sub(r"<[^>]+>", " ", label)).strip()
        if not clean_href or not title or re.search(r"\b(?:home|search|index)\b", title, re.I):
            continue
        detail = urllib.parse.urljoin(index_url, clean_href)
        parsed = urllib.parse.urlparse(detail)
        # The CVF paper detail page always lives in a content tree.  This guard
        # avoids treating conference navigation HTML as a paper.
        if "/content" not in parsed.path.lower() or detail in seen:
            continue
        seen.add(detail)
        pos = index_html.find(href)
        nearby = index_html[pos:pos + 1600] if pos >= 0 else ""
        author_match = re.search(r'<(?:i|span|p|dd)\b[^>]*class=["\'][^"\']*author[^"\']*["\'][^>]*>(.*?)</(?:i|span|p|dd)>', nearby, re.I | re.S)
        out.append({"href": detail, "title": title, "authors": html.unescape(re.sub(r"<[^>]+>", " ", author_match.group(1) if author_match else "")).strip()})
    return out


def _schedule_urls(index_html: str, index_url: str) -> list[str]:
    """Legacy CVF roots expose one ``.py?day=`` page per conference day."""
    out = []
    for href in re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\']', index_html or "", re.I):
        if not re.search(r"\.py\?day=\d{4}-\d{2}-\d{2}", href, re.I):
            continue
        url = urllib.parse.urljoin(index_url, html.unescape(href))
        if url not in out:
            out.append(url)
    return out


def _choose_entry(ref: dict, entries: list[dict]) -> dict | None:
    wanted = _norm(ref.get("title"))
    cited_author = _first_author(ref)
    if not wanted:
        return None
    for entry in entries:
        if _norm(entry.get("title")) != wanted:
            continue
        authors = _author_surname(entry.get("authors"))
        # If the official index gives authors, a first-author disagreement is a
        # hard rejection; title coincidence alone is not adequate evidence.
        if cited_author and authors and cited_author != authors:
            continue
        return entry
    return None


def _pdf_url_from_detail(detail_url: str) -> str | None:
    parsed = urllib.parse.urlparse(detail_url)
    path = parsed.path
    if not path.lower().endswith(".html"):
        return None
    if "/html/" not in path.lower():
        return None
    pdf_path = re.sub(r"/html/", "/papers/", path, count=1, flags=re.I)
    pdf_path = re.sub(r"\.html$", ".pdf", pdf_path, flags=re.I)
    return urllib.parse.urlunparse(parsed._replace(path=pdf_path, query="", fragment=""))


def _diagnostic(ref: dict, code: str, **details) -> dict:
    return {"method": NAME, "kind": "diagnostic", "diagnostic": {"provider": NAME, "code": code, "year": _ref_year(ref), **details}}


def _direct_fetch_html(url: str, timeout: int = 30) -> bytes | None:
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "CitationVerifier/1.0"}
        )
        from core.resolve import transport_telemetry

        started = time.monotonic()
        with transport_telemetry.physical_request(method="GET", url=url):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = response.read()
                    transport_telemetry.record_attempt(
                        method="GET",
                        url=url,
                        attempt_number=1,
                        started=started,
                        status=getattr(response, "status", 200),
                    )
                    return body
            except Exception as error:
                transport_telemetry.record_attempt(
                    method="GET",
                    url=url,
                    attempt_number=1,
                    started=started,
                    status=getattr(error, "code", None),
                    error=error,
                )
                raise
    except Exception:
        return None


def candidate_items(ref: dict, **kwargs) -> list[dict]:
    """Return canonical PDF/landing candidates, or an inspectable diagnostic."""
    get_fn = kwargs.get("get_fn")
    venues, year, title = _likely_venues(ref), _ref_year(ref), _norm(ref.get("title"))
    if not venues:
        return [_diagnostic(ref, "venue_or_year_not_recognized")]
    if not title:
        return [_diagnostic(ref, "missing_title")]

    attempted, fetched_any = [], False
    for spec in venues:
        for index_url in _index_urls(spec, year, str(ref.get("title") or "")):
            attempted.append(index_url)
            try:
                if callable(get_fn):
                    _status, body = get_fn(index_url, profile="document", timeout=30)
                else:
                    body = _direct_fetch_html(index_url, timeout=30)
            except Exception as exc:
                last_error = type(exc).__name__
                continue
            if body is None:
                continue
            fetched_any = True
            index_html = body.decode("utf-8", errors="replace")
            entry = _choose_entry(ref, _extract_entries(index_html, index_url))
            if entry is None:
                for schedule_url in _schedule_urls(index_html, index_url):
                    attempted.append(schedule_url)
                    try:
                        if callable(get_fn):
                            _child_status, child_body = get_fn(schedule_url, profile="document", timeout=30)
                        else:
                            child_body = _direct_fetch_html(schedule_url, timeout=30)
                    except Exception:
                        continue
                    if child_body is None:
                        continue
                    entry = _choose_entry(
                        ref,
                        _extract_entries(child_body.decode("utf-8", errors="replace"), schedule_url),
                    )
                    if entry is not None:
                        index_url = schedule_url
                        break
            if entry is None:
                continue
            pdf_url = _pdf_url_from_detail(entry["href"])
            # The persisted identity context is a closed contract.  Keep the
            # matched title in its canonical field; navigation details are
            # intentionally excluded from the persisted candidate contract.
            context = {"provider": NAME, "canonical_host": True, "year": year, "title": entry["title"]}
            return [
                {"method": NAME, "url": pdf_url, "kind": "pdf", "identity_context": context},
                {"method": NAME, "url": entry["href"], "kind": "landing", "identity_context": context},
            ]
    if not fetched_any:
        return [_diagnostic(ref, "official_index_unavailable", attempted=attempted, error=locals().get("last_error"))]
    return [_diagnostic(ref, "no_strict_official_match", attempted=attempted)]
