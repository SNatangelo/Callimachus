#!/usr/bin/env python3
# core/resolve/providers/core.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""CORE resolver + fetch provider."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse

from core.fetch.transport.http import FetchAdmissionDeferred

NAME = "core"
CREDENTIAL_SPECS = ({
    "provider": "core",
    "env_name": "CORE_API_KEY",
    "channels": ("resolve", "fetch"),
    "label": "CORE",
},)
RESOLVE_NAME = "core_search"
MANIFEST = {}
ENV_CORE_API_KEY = "CORE_API_KEY"
CORE_SEARCH_WORKS_URL = "https://api.core.ac.uk/v3/search/works"
PUBLIC_FULLTEXT_PLACEHOLDER = "Not available for public API users."


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return (
        not ref.get("url")
        and resolve_mod._article_like_resolution_candidate(ref)
    )


def api_key(environ: dict[str, str] | None = None) -> str | None:
    env = environ or os.environ
    key = (env.get(ENV_CORE_API_KEY) or "").strip()
    return key or None


def enabled(*, environ=None, **kwargs) -> bool:
    return bool(api_key(environ))


def disabled_reason(*, environ=None, **kwargs) -> str | None:
    return None if api_key(environ) else f"missing {ENV_CORE_API_KEY}"


def query_json(params: dict[str, str], *, get_fn, environ: dict[str, str] | None = None) -> dict | None:
    key = api_key(environ)
    if not key:
        return None
    query = urllib.parse.urlencode(params)
    url = f"{CORE_SEARCH_WORKS_URL}?{query}"
    try:
        _status, body = get_fn(
            url,
            accept="application/json",
            profile="api",
            headers_extra={"Authorization": f"Bearer {key}"},
        )
        return json.loads(body.decode("utf-8", errors="replace"))
    except FetchAdmissionDeferred:
        raise
    except Exception:
        return None


def work(
    ref: dict,
    *,
    get_fn,
    normalize_doi,
    environ: dict[str, str] | None = None,
    **kwargs,
) -> dict | None:
    doi = normalize_doi(ref.get("doi"))
    memo = getattr(get_fn, "provider_work_lookup", None)
    if callable(memo):
        return memo(NAME, _work_lookup_key(ref, doi), lambda: _work_uncached(
            ref, doi=doi, get_fn=get_fn, normalize_doi=normalize_doi, environ=environ,
        ))
    return _work_uncached(
        ref, doi=doi, get_fn=get_fn, normalize_doi=normalize_doi, environ=environ,
    )


def _work_lookup_key(ref: dict, doi: str | None) -> str:
    """Stable identity for one CORE work lookup within a Fetch run."""
    return "\x1f".join(str(value or "").strip() for value in (
        doi, ref.get("title"), ref.get("year"), ref.get("surname"),
        ref.get("ay_surname"), ref.get("first_author_surname"),
    ))


def _work_uncached(
    ref: dict, *, doi: str | None, get_fn, normalize_doi, environ: dict[str, str] | None,
) -> dict | None:
    for query in _queries(ref, doi):
        data = query_json(
            {"q": query, "limit": "5"},
            get_fn=get_fn,
            environ=environ,
        )
        if not isinstance(data, dict):
            continue
        results = [item for item in (data.get("results") or []) if isinstance(item, dict)]
        if not results:
            continue
        candidate = _best_hit(ref, results, normalize_doi)
        if candidate is not None:
            return candidate
    return None


def _queries(ref: dict, doi: str | None) -> list[str]:
    out = []
    if doi:
        out.append(f"doi:{doi}")
    title = str(ref.get("title") or "").strip()
    if title:
        out.append(title)
        year = str(ref.get("year") or "").strip()
        if year and year.isdigit():
            out.append(f"{title} {year}")
    return out


def _norm(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _title_score(ref: dict, hit: dict) -> float:
    ref_title = _norm(ref.get("title"))
    hit_title = _norm(hit.get("title"))
    if not ref_title or not hit_title:
        return 0.0
    ref_tokens = set(ref_title.split())
    hit_tokens = set(hit_title.split())
    if not ref_tokens or not hit_tokens:
        return 0.0
    return len(ref_tokens & hit_tokens) / len(ref_tokens)


def _surname_from_name(name: str | None) -> str | None:
    if not name:
        return None
    clean = re.sub(r"[{}]", "", str(name))
    tokens = re.findall(r"[A-Za-z][A-Za-z'â€™.-]*", clean)
    if not tokens:
        return None
    if len(tokens) >= 2 and re.fullmatch(r"[A-Z]{2,4}", tokens[1]):
        return tokens[0].lower()
    return tokens[-1].lower()


def _ref_first_author_surname(ref: dict) -> str | None:
    for key in ("ay_surname", "surname", "first_author_surname"):
        surname = str(ref.get(key) or "").strip()
        if surname:
            return surname.lower()
    raw_entry = ref.get("raw_entry") or ""
    if not raw_entry:
        return None
    clean = re.sub(r"[{}]", "", str(raw_entry))
    clean = re.sub(r"\\[a-zA-Z]+\s*\{\}", "", clean)
    clean = re.sub(r"\\[a-zA-Z]+", "", clean)
    title = str(ref.get("title") or "").strip()
    if title:
        title_pattern = re.escape(title)
        title_pattern = re.sub(r"\\\s+", r"\\s+", title_pattern)
        match = re.search(title_pattern, clean, flags=re.IGNORECASE)
        if match and match.start() > 0:
            clean = clean[:match.start()].strip(" .")
    first_author = re.split(r"\s+(?:and|&)\s+|,|;", clean, maxsplit=1)[0].strip()
    return _surname_from_name(first_author)


def _hit_first_author_surname(hit: dict) -> str | None:
    authors = hit.get("authors") or []
    first = None
    if isinstance(authors, list) and authors:
        first = authors[0]
    elif isinstance(authors, str):
        first = authors
    if isinstance(first, dict):
        for key in ("family", "lastName", "last_name", "surname", "name"):
            surname = _surname_from_name(first.get(key))
            if surname:
                return surname
    return _surname_from_name(first)


def _hit_year(hit: dict) -> int | None:
    for key in ("year", "publicationYear", "publishedYear"):
        value = hit.get(key)
        if isinstance(value, int):
            return value
        text = str(value or "")
        match = re.search(r"\b(18|19|20)\d{2}\b", text)
        if match:
            return int(match.group(0))
    for key in ("publishedDate", "createdDate", "updatedDate"):
        text = str(hit.get(key) or "")
        match = re.search(r"\b(18|19|20)\d{2}\b", text)
        if match:
            return int(match.group(0))
    return None


def _hit_doi(hit: dict, normalize_doi) -> str | None:
    doi = normalize_doi(hit.get("doi"))
    if doi:
        return doi
    for key in ("identifiers", "doiIdentifiers"):
        vals = hit.get(key) or []
        for val in vals:
            norm = normalize_doi(val)
            if norm:
                return norm
    return None


def _bibliographic_match(ref: dict, hit: dict, normalize_doi) -> bool:
    doi = normalize_doi(ref.get("doi"))
    hit_doi = _hit_doi(hit, normalize_doi)
    if doi and hit_doi == doi:
        return True
    title_score = _title_score(ref, hit)
    if title_score < 0.55:
        return False
    ref_year = ref.get("year")
    try:
        ref_year_int = int(ref_year) if ref_year else None
    except Exception:
        ref_year_int = None
    hit_year = _hit_year(hit)
    if ref_year_int and hit_year and abs(ref_year_int - hit_year) > 1:
        return False
    ref_author = _ref_first_author_surname(ref)
    hit_author = _hit_first_author_surname(hit)
    if ref_author and hit_author and ref_author != hit_author:
        return False
    return True


def _best_hit(ref: dict, results: list[dict], normalize_doi) -> dict | None:
    doi = normalize_doi(ref.get("doi"))
    best = None
    best_score = -1.0
    for hit in results:
        if not _bibliographic_match(ref, hit, normalize_doi):
            continue
        score = 0.0
        hit_doi = _hit_doi(hit, normalize_doi)
        if doi and hit_doi == doi:
            score += 100.0
        score += _title_score(ref, hit) * 20.0
        if hit.get("downloadUrl"):
            score += 3.0
        if hit.get("sourceFulltextUrls"):
            score += 2.0
        if _clean_fulltext(hit.get("fullText")):
            score += 4.0
        if score > best_score:
            best = hit
            best_score = score
    return best


def _display_url(hit: dict) -> str | None:
    for item in hit.get("links") or []:
        if isinstance(item, dict) and item.get("type") == "display" and item.get("url"):
            return item["url"]
    return None


def _clean_fulltext(text: str | None) -> str | None:
    if not text:
        return None
    text = str(text).strip()
    if not text or text == PUBLIC_FULLTEXT_PLACEHOLDER:
        return None
    return text


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    """Title search on CORE API - good for CS papers with OA full text.

    CORE indexes millions of OA papers and its keyword search is surprisingly
    accurate for paper titles. Uses the CORE API key from the environment.
    """
    via = RESOLVE_NAME
    title = resolve_mod._article_title_candidate(ref)
    if not title:
        return {
            "status": "unverified",
            "via": via,
            "reason": "no usable title for CORE search",
        }
    if not resolve_mod._CORE_API_KEY:
        return {
            "status": "unverified",
            "via": via,
            "reason": "no CORE API key configured",
        }

    raw_entry = resolve_mod._tex_strip_braces(ref.get("raw_entry") or "")
    ref_title = (ref.get("title") or "").strip()
    year = ref.get("year")

    clean_title = re.sub(r'[{}"\']', "", title[:200])
    params = {"q": clean_title, "limit": "5"}
    url = "https://api.core.ac.uk/v3/search/works?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {resolve_mod._CORE_API_KEY}"}
    try:
        _status, body = resolve_mod._get(url, headers_extra=headers)
        data = json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return {
                "status": "unresolved",
                "via": via,
                "reason": "rate_limited (HTTP 429)",
            }
        return {
            "status": "unresolved",
            "via": via,
            "reason": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "status": "unresolved",
            "via": via,
            "reason": f"network: {type(exc).__name__}",
        }

    results = data.get("results") or []
    if not results:
        return {
            "status": "unverified",
            "via": via,
            "reason": "no results on CORE",
        }

    best = None
    best_overlap = -1.0
    for rec in results:
        entry_title = (rec.get("title") or "").strip()
        entry_title = re.sub(r"\s+", " ", entry_title)
        overlap = resolve_mod.title_overlap(entry_title, raw_entry)
        if overlap is None and ref_title:
            overlap = resolve_mod.title_overlap(entry_title, ref_title)
        if overlap is not None and overlap > best_overlap:
            best_overlap = overlap
            best = rec

    if best is None or best_overlap < 0.30:
        return {
            "status": "unverified",
            "via": via,
            "reason": "no CORE match met the confidence threshold",
        }

    matched_title = (best.get("title") or "").strip()
    matched_title = re.sub(r"\s+", " ", matched_title)
    matched_year = best.get("yearPublished")
    doi = best.get("doi")
    download_url = best.get("downloadUrl")
    fl_links = []
    if doi:
        fl_links.append({"url": f"https://doi.org/{doi}", "content_type": "doi"})
    if download_url:
        fl_links.append({"url": download_url, "content_type": "pdf"})

    if (
        year is not None
        and matched_year is not None
        and abs(int(year) - int(matched_year)) > 2
        and best_overlap < 0.70
    ):
        return {
            "status": "unverified",
            "via": via,
            "matched_title": matched_title,
            "reason": (
                f"year mismatch (cited {year}, "
                f"CORE {matched_year}, overlap {best_overlap:.3f})"
            ),
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
        }

    return {
        "status": "resolved",
        "via": via,
        "matched_title": matched_title,
        "doi": doi,
        "retracted": False,
        "reason": f"CORE title search match (overlap {best_overlap:.3f})",
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium" if best_overlap >= 0.70 else "low",
        "fulltext_exists": bool(download_url),
        "oa_status": "open" if download_url else "unknown",
        "fulltext_links": fl_links,
        "metadata_match": {
            "title_overlap": best_overlap,
            "matched_year": matched_year,
        },
    }


def direct_text_items(
    ref: dict,
    *,
    get_fn,
    normalize_doi,
    environ: dict[str, str] | None = None,
    **kwargs,
) -> list[dict]:
    hit = work(
        ref,
        get_fn=get_fn,
        normalize_doi=normalize_doi,
        environ=environ,
    )
    if not hit:
        return []
    text = _clean_fulltext(hit.get("fullText"))
    if not text:
        return []
    source_ref = hit.get("downloadUrl") or _display_url(hit) or CORE_SEARCH_WORKS_URL
    return [
        {
            "method": NAME,
            "text": text,
            "source_ref": source_ref,
            "extract_method": "api_fulltext",
        }
    ]


def candidate_items(
    ref: dict,
    *,
    get_fn,
    normalize_doi,
    kind_from_url,
    environ: dict[str, str] | None = None,
    **kwargs,
) -> list[dict]:
    hit = work(
        ref,
        get_fn=get_fn,
        normalize_doi=normalize_doi,
        environ=environ,
    )
    if not hit:
        return []
    out = []
    seen = set()

    def add(url: str | None):
        if not url or url in seen:
            return
        seen.add(url)
        out.append({"method": NAME, "url": url, "kind": kind_from_url(url)})

    add(hit.get("downloadUrl"))
    add(_display_url(hit))
    for url in hit.get("sourceFulltextUrls") or []:
        if isinstance(url, str):
            add(url)
    return out
