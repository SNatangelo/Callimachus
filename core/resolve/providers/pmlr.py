#!/usr/bin/env python3
# core/resolve/providers/pmlr.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Official Proceedings of Machine Learning Research (PMLR) fetch provider.

The PMLR catalogue is the authority for mapping a conference/year to its
volume.  Paper URLs are only emitted after a title match on that volume and,
when both sides expose it, a first-author match.
"""

from __future__ import annotations

import html
import re
import unicodedata
import urllib.parse

NAME = "pmlr"
MANIFEST = {"canonical_hosts": ["proceedings.mlr.press", "www.pmlr.org"]}
CATALOG_URL = "https://proceedings.mlr.press/"

_VENUES = {
    "icml": ("international conference on machine learning", "icml"),
    "aistats": ("international conference on artificial intelligence and statistics", "aistats"),
    "colt": ("conference on learning theory", "colt"),
    "uai": ("conference on uncertainty in artificial intelligence", "uai"),
    "acml": ("asian conference on machine learning", "acml"),
    "alt": ("algorithmic learning theory", "alt"),
    "corl": ("conference on robot learning", "corl"),
    "l4dc": ("learning for dynamics and control", "l4dc"),
}


def _norm(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _year(ref: dict) -> int | None:
    try:
        value = int(ref.get("year") or 0)
    except (TypeError, ValueError):
        return None
    return value if 1900 <= value <= 2100 else None


def _venue(ref: dict) -> str | None:
    raw = " ".join(str(ref.get(key) or "") for key in ("venue", "journal", "raw_entry", "url"))
    normalized = _norm(raw)
    for name, markers in _VENUES.items():
        if any(_norm(marker) in normalized for marker in markers):
            return name
    return "pmlr" if "proceedings machine learning research" in normalized else None


def _first_author(ref: dict) -> str | None:
    for key in ("ay_surname", "surname", "first_author_surname"):
        value = _norm(ref.get(key))
        if value:
            return value.split()[-1]
    raw = str(ref.get("raw_entry") or "")
    title = str(ref.get("title") or "")
    if title:
        match = re.search(re.escape(title), raw, re.I)
        if match:
            raw = raw[:match.start()]
    first = re.split(r"\s+(?:and|&)\s+|,|;", raw, maxsplit=1)[0]
    words = _norm(first).split()
    return words[-1] if len(words) >= 2 else None


def _author_surname(value: object) -> str | None:
    first = re.split(r"\s+(?:and|&)\s+|,|;", str(value or ""), maxsplit=1)[0]
    words = _norm(first).split()
    return words[-1] if words else None


def _volume_from_ref(ref: dict) -> int | None:
    for key in ("volume", "journal_volume"):
        try:
            value = int(ref.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    raw = str(ref.get("raw_entry") or "")
    match = re.search(
        r"(?:PMLR|Proceedings of Machine Learning Research)\s*"
        r"(?:,?\s*(?:volume|vol\.?))?\s*(\d{1,4})\b",
        raw,
        re.I,
    )
    if not match:
        return None
    value = int(match.group(1))
    # ``PMLR, 2020`` is publisher + publication year, not volume 2020.
    return None if 1900 <= value <= 2100 else value


def _catalog_volumes(body: str) -> list[dict]:
    out, seen = [], set()
    # Venue/year text is commonly outside the anchor, in the surrounding <li>.
    pattern = re.compile(
        r'<li\b[^>]*>(?P<block>.*?<a\b[^>]*href=["\'](?P<href>[^"\']*(?:/v|v)(?P<volume>\d+)/?)["\'][^>]*>.*?</a>.*?)</li>',
        re.I | re.S,
    )
    for match in pattern.finditer(body or ""):
        href, volume = match.group("href"), match.group("volume")
        label = re.sub(r"<[^>]+>", " ", match.group("block"))
        key = int(volume)
        if key in seen:
            continue
        seen.add(key)
        out.append({"volume": key, "label": html.unescape(re.sub(r"<[^>]+>", " ", label)), "url": urllib.parse.urljoin(CATALOG_URL, href)})
    return out


def _volume_matches(volume: dict, venue: str | None, year: int | None) -> bool:
    label = _norm(volume.get("label"))
    if year is not None and str(year) not in label:
        return False
    if venue and venue != "pmlr" and not any(_norm(marker) in label for marker in _VENUES[venue]):
        return False
    return True


def _paper_entries(body: str, base_url: str, year: int | None) -> list[dict]:
    out, seen = [], set()
    blocks = re.findall(r'<(?:div|li|article)\b[^>]*(?:paper|container)[^>]*>(.*?)</(?:div|li|article)>', body or "", re.I | re.S)
    if not blocks:
        blocks = re.findall(r'(<p\b[^>]*class=["\'][^"\']*title[^"\']*["\'][^>]*>.*?</p>.*?(?:<p\b[^>]*class=["\'][^"\']*authors[^"\']*["\'][^>]*>.*?</p>)?)', body or "", re.I | re.S)
    for block in blocks:
        title_match = re.search(
            r'<(?:p|h\d)\b[^>]*class=["\'][^"\']*title[^"\']*["\'][^>]*>(.*?)</(?:p|h\d)>',
            block, re.I | re.S,
        )
        if not title_match:
            continue
        title = title_match.group(1)
        authors_match = re.search(r'<(?:p|span)\b[^>]*class=["\'][^"\']*authors[^"\']*["\'][^>]*>(.*?)</(?:p|span)>', block, re.I | re.S)
        hrefs = [html.unescape(value) for value in re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\']', block, re.I)]
        landing_href = next((value for value in hrefs if re.search(r"\.html?(?:$|[?#])", value, re.I)), None)
        if not landing_href:
            continue
        landing = urllib.parse.urljoin(base_url, landing_href)
        pdf_href = next((value for value in hrefs if re.search(r"\.pdf(?:$|[?#])", value, re.I)
                         and not re.search(r"(?:supp|appendix)", value, re.I)), None)
        if landing in seen:
            continue
        seen.add(landing)
        out.append({"title": html.unescape(re.sub(r"<[^>]+>", " ", title)).strip(), "authors": html.unescape(re.sub(r"<[^>]+>", " ", authors_match.group(1) if authors_match else "")).strip(), "landing": landing, "pdf": urllib.parse.urljoin(base_url, pdf_href) if pdf_href else None, "year": year})
    return out


def _matches(ref: dict, entry: dict) -> bool:
    if _norm(ref.get("title")) != _norm(entry.get("title")):
        return False
    cited_year, entry_year = _year(ref), entry.get("year")
    if cited_year is not None and entry_year is not None and cited_year != entry_year:
        return False
    cited_author = _first_author(ref)
    entry_author = _author_surname(entry.get("authors"))
    return not (cited_author and entry_author and cited_author != entry_author)


def _pdf_url(landing: str) -> str:
    parsed = urllib.parse.urlparse(landing)
    path = re.sub(r"\.html?$", ".pdf", parsed.path, flags=re.I)
    return urllib.parse.urlunparse(parsed._replace(path=path))


def candidate_items(ref: dict, **kwargs) -> list[dict]:
    """Find a canonical PMLR landing page and PDF using only official indices."""
    get_fn = kwargs.get("get_fn")
    year, venue, title = _year(ref), _venue(ref), _norm(ref.get("title"))
    if not callable(get_fn) or not title or (not year and not _volume_from_ref(ref)) or not venue:
        return []
    try:
        _status, catalog_body = get_fn(CATALOG_URL, profile="document", timeout=30)
    except Exception:
        return []
    volumes = _catalog_volumes(catalog_body.decode("utf-8", errors="replace"))
    explicit_volume = _volume_from_ref(ref)
    selected = [item for item in volumes if item["volume"] == explicit_volume] if explicit_volume else [item for item in volumes if _volume_matches(item, venue, year)]
    for volume in selected:
        index_url = volume["url"].rstrip("/") + "/"
        try:
            _status, body = get_fn(index_url, profile="document", timeout=30)
        except Exception:
            continue
        for entry in _paper_entries(body.decode("utf-8", errors="replace"), index_url, year):
            if not _matches(ref, entry):
                continue
            context = {"provider": NAME, "canonical_host": True, "provider_record_id": f"v{volume['volume']}", "title": entry["title"], "year": year}
            return [
                {"method": NAME, "url": entry.get("pdf") or _pdf_url(entry["landing"]), "kind": "pdf", "identity_context": context},
                {"method": NAME, "url": entry["landing"], "kind": "landing", "identity_context": context},
            ]
    return []
