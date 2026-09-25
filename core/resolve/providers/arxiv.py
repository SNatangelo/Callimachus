#!/usr/bin/env python3
# core/resolve/providers/arxiv.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""arXiv resolver + fetch provider."""

from __future__ import annotations

import copy
import re
import urllib.error
import urllib.parse
from xml.etree import ElementTree as ET

NAME = "arxiv"
RESOLVE_NAME = "arxiv_search"
PREPRINT_RESOLVER = True
MANIFEST = {
    "doi_prefixes": ["10.48550/arxiv."],
    "preprint_host": True,
    "canonical_hosts": ["arxiv.org", "export.arxiv.org"],
    "host_markers": ["arxiv"],
}


class _CandidateItems(list):
    """Private carrier for one issued title-lookup observation.

    ``candidate_items`` remains a list API.  The registry consumes these
    attributes only while it builds the per-provider trace row.
    """

    def __init__(self, items=(), *, attempt: dict | None = None, error: dict | None = None):
        super().__init__(items)
        self._fetch_execution_attempts = [attempt] if attempt else []
        self._provider_error = error


def _identity_title(value: object) -> str:
    """Normalise only the historically split ``self-attention`` spelling.

    This is intentionally not a general title normaliser: joining arbitrary
    hyphenated words makes distinct works look alike.  arXiv itself contains
    both spellings of this one established term.
    """
    text = str(value or "").casefold()
    text = re.sub(r"\bself[\s-]*attention\b", "selfattention", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _first_author(ref: dict) -> str | None:
    for key in ("ay_surname", "surname", "first_author_surname"):
        value = re.sub(r"[^a-z]", "", str(ref.get(key) or "").casefold())
        if len(value) >= 2:
            return value
    raw = str(ref.get("raw_entry") or "")
    title = str(ref.get("title") or "")
    if title:
        match = re.search(re.escape(title), raw, re.IGNORECASE)
        if match:
            raw = raw[:match.start()]
    first = re.split(r",|\s+(?:and|&)\s+", raw, maxsplit=1)[0]
    tokens = re.findall(r"[a-z]+", first.casefold())
    if tokens[-2:] == ["et", "al"]:
        tokens = tokens[:-2]
    if tokens:
        return tokens[-1]
    return None


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return (
        not ref.get("url")
        and resolve_mod._article_like_resolution_candidate(ref)
    )


def _entry_authors(entry, ns: dict[str, str]) -> list[str]:
    return [
        (node.findtext("atom:name", default="", namespaces=ns) or "").strip()
        for node in entry.findall("atom:author", ns)
    ]


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    """Title-based search on arXiv API - universal last-resort fallback.

    When Crossref and OpenAlex both come up empty for a paper that is likely on
    arXiv, this search uses arXiv's native API to find it by title. Papers
    cited without a DOI (common in older LaTeX bibliographies) are often
    discoverable here, especially ML/CS papers.

    Tries ``ti:`` (title prefix) first; falls back to ``all:`` (full-text
    keyword) when the title search yields no usable hit.

    Returns a resolve-like dict with ``status``, ``matched_title``, and a DOI
    in the ``10.48550/arXiv.ID`` form when found.
    """
    title = resolve_mod._article_title_candidate(ref)
    via = RESOLVE_NAME
    if not title:
        return {
            "status": "unverified",
            "via": via,
            "reason": "no usable title for arXiv search",
        }

    raw_entry = resolve_mod._tex_strip_braces(ref.get("raw_entry") or "")
    clean_title = re.sub(r'[{}"\']', "", title[:300])
    cited_author = _first_author(ref)

    def _arxiv_query(query: str) -> tuple[list, bytes | None]:
        params: dict[str, str] = {
            "search_query": query,
            "max_results": "5",
            "sortBy": "relevance",
        }
        url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        try:
            _status, body = resolve_mod._get(url, accept="application/atom+xml")
            return [], body
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                return [{
                    "status": "unresolved",
                    "via": via,
                    "reason": "rate_limited (HTTP 429) - NOT fabrication",
                }], None
            return [{
                "status": "unresolved",
                "via": via,
                "reason": f"HTTP {exc.code}",
            }], None
        except Exception as exc:
            return [{
                "status": "unresolved",
                "via": via,
                "reason": f"network: {type(exc).__name__}",
            }], None

    def _score_entries(body: bytes | None) -> dict | None:
        if body is None:
            return None
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return None
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom",
        }
        entries = root.findall("atom:entry", ns)
        if not entries:
            return None

        ref_title = resolve_mod._article_title_candidate(ref)
        best_overlap = -1.0
        best_entry = None
        best_arxiv_id = None
        for entry in entries:
            entry_title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            entry_title = re.sub(r"\s+", " ", entry_title)
            overlap = resolve_mod._title_match_score(ref_title, entry_title)
            # Apply the selfattention/self-attention equivalence only after
            # the ordinary matcher has failed to reach the resolver threshold.
            if (overlap is None or overlap < 0.30) and _identity_title(ref_title) == _identity_title(entry_title):
                overlap = 1.0
            if overlap == 0.0 and len(resolve_mod._sources._tokens(ref_title)) < resolve_mod.TITLE_MIN_TOKENS:
                raw_overlap = resolve_mod.title_overlap(entry_title, raw_entry)
                if raw_overlap is not None and raw_overlap >= 0.95:
                    overlap = raw_overlap

            # Author metadata is corroborative when both sides provide it;
            # legacy title-only references and sparse Atom records still work.
            entry_authors = _entry_authors(entry, ns)
            if cited_author and entry_authors:
                surnames = {
                    re.sub(r"[^a-z]", "", name.casefold().split()[-1])
                    for name in entry_authors if name.split()
                }
                if cited_author not in surnames:
                    continue
            if overlap is not None and overlap > best_overlap:
                best_overlap = overlap
                best_entry = entry
                full_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
                match = re.search(r"arxiv\.org/abs/([^v]+)", full_id)
                if match:
                    best_arxiv_id = match.group(1)

        if best_entry is None or best_overlap < 0.30:
            return None
        return {
            "entry": best_entry,
            "arxiv_id": best_arxiv_id,
            "overlap": best_overlap,
            "ns": ns,
        }

    early_error, body = _arxiv_query(f'ti:"{clean_title}"')
    if early_error:
        return early_error[0]
    scored = _score_entries(body)

    if scored is None:
        words = [
            word for word in re.findall(r"[a-zA-Z]{4,}", clean_title.lower())
            if word not in resolve_mod._STOP_TITLE
        ]
        if len(words) >= 3:
            all_query = " AND ".join(f"all:{word}" for word in words[:8])
            early_error2, body2 = _arxiv_query(all_query)
            if early_error2:
                return early_error2[0]
            scored = _score_entries(body2)

    if scored is None:
        return {
            "status": "unverified",
            "via": via,
            "reason": "no arXiv match met the confidence threshold",
        }

    best_entry = scored["entry"]
    best_arxiv_id = scored["arxiv_id"]
    best_overlap = scored["overlap"]
    ns = scored["ns"]

    matched_title = (best_entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
    matched_title = re.sub(r"\s+", " ", matched_title)
    doi = f"10.48550/arXiv.{best_arxiv_id}" if best_arxiv_id else None
    abstract = (best_entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()

    published = (best_entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
    matched_year = None
    if published:
        try:
            matched_year = int(published[:4])
        except (ValueError, IndexError):
            pass

    if (
        ref.get("year") is not None
        and matched_year is not None
        and abs(int(ref["year"]) - matched_year) > 2
        and best_overlap < 0.70
    ):
        return {
            "status": "unverified",
            "via": via,
            "matched_title": matched_title,
            "reason": (
                f"year mismatch (cited {ref['year']}, "
                f"arXiv {matched_year}, overlap {best_overlap:.3f})"
            ),
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
        }

    return {
        "status": "resolved",
        "via": via,
        "matched_title": matched_title,
        "abstract": abstract or None,
        "retracted": False,
        "reason": f"arXiv title search match (overlap {best_overlap:.3f})",
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium" if best_overlap >= 0.70 else "low",
        "fulltext_exists": True if doi else False,
        "oa_status": "open",
        "fulltext_links": [
            {
                "url": item["url"],
                "content_type": item["kind"],
                "content_version": item["content_version"],
                "identity_context": item["identity_context"],
            }
            for item in _candidate_pair(best_arxiv_id, identity_context={
                "provider": RESOLVE_NAME,
                "title": matched_title,
                "authors": _entry_authors(best_entry, ns),
                "year": matched_year,
                "identifiers": {"arxiv_id": best_arxiv_id},
                "canonical_host": True,
                "source_confidence": best_overlap,
            })
        ] if doi else [],
        "metadata_match": {
            "title_overlap": best_overlap,
            "matched_year": matched_year,
        },
    }


def pdf_url(doi: str | None) -> str | None:
    if not doi:
        return None
    match = re.match(r"^10\.48550/arxiv\.(.+)$", doi, flags=re.IGNORECASE)
    if not match:
        return None
    return f"https://arxiv.org/pdf/{match.group(1)}.pdf"


def pdf_url_from_doi(doi: str | None, **kwargs) -> str | None:
    return pdf_url(doi)


def _arxiv_id_from_doi(doi: str | None) -> str | None:
    match = re.fullmatch(
        r"10\.48550/arxiv\.((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?)",
        str(doi or "").strip(),
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def _candidate_pair(arxiv_id: str, *, identity_context: dict | None = None) -> list[dict]:
    context = identity_context or {
        "provider": RESOLVE_NAME,
        "identifiers": {"arxiv_id": arxiv_id},
        "canonical_host": True,
    }
    return [
        {
            "method": NAME,
            "url": f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            "kind": "pdf",
            "content_version": "preprint",
            "identity_context": copy.deepcopy(context),
        },
        {
            "method": NAME,
            "url": f"https://arxiv.org/html/{arxiv_id}",
            "kind": "html",
            "content_version": "preprint",
            "official_arxiv_html": True,
            "identity_context": copy.deepcopy(context),
        },
    ]


def candidate_items(ref: dict, **kwargs) -> list[dict]:
    normalize_doi = kwargs.get("normalize_doi") or (lambda x: x)
    arxiv_id = _arxiv_id_from_doi(normalize_doi(ref.get("doi")))
    if arxiv_id:
        return _candidate_pair(arxiv_id)

    # Stage 2 is reached only after the version-of-record candidates failed.
    # Search once and accept only an exact identity-title match, corroborated by
    # author/year whenever arXiv and the citation both expose those fields.
    get_fn = kwargs.get("get_fn")
    title = str(ref.get("title") or "").strip()
    if not callable(get_fn) or len(_identity_title(title).split()) < 4:
        return []
    # Strip characters that would break the arXiv ``ti:"..."`` query. Kept out of
    # the f-string expression because a backslash there is a SyntaxError before
    # Python 3.12 (and the CI matrix still includes 3.11).
    # arXiv's title index does not reliably retrieve the concatenated spelling
    # ``selfattention`` used by some bibliographies.  Query the indexed spelling;
    # the separate exact identity normalizer below still admits only that one
    # documented typography variant.
    query_title = re.sub(
        r"\bself[\s-]*attention\b", "self-attention", title[:300],
        flags=re.IGNORECASE,
    )
    cleaned_title = re.sub(r'[{}"\']', "", query_title)
    query_url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
        "search_query": f'ti:"{cleaned_title}"',
        "max_results": "5",
        "sortBy": "relevance",
    })
    def _result(
        items=(),
        *,
        outcome: str,
        status: int | None = None,
        reason: str | None = None,
        error_type: str | None = None,
        retryable: bool | None = None,
    ) -> _CandidateItems:
        attempt = {
            "method": NAME,
            "url": query_url,
            "kind": "api",
            "stage": "candidate_generation",
            "outcome": outcome,
            "status": status,
            # The provider transport exposes only ``(status, body)``.  Preserve
            # the required response-state keys without inventing redirect or
            # response-header observations.
            "final_url": None,
            "content_type": None,
        }
        if reason:
            attempt["reason"] = reason
        error = None
        if error_type:
            error = {
                "status": "error",
                "error_type": error_type,
                "error_reason": reason,
                "retryable": retryable,
                "http_status": status,
            }
        return _CandidateItems(items, attempt=attempt, error=error)

    try:
        status, body = get_fn(query_url, profile="api", timeout=30)
    except urllib.error.HTTPError as exc:
        return _result(
            outcome="unavailable", status=exc.code, reason=f"HTTP {exc.code}",
            error_type="rate_limit" if exc.code == 429 else "http_error",
            retryable=exc.code == 429 or 500 <= exc.code <= 599,
        )
    except Exception as exc:
        return _result(
            outcome="unavailable", reason=f"network: {type(exc).__name__}",
            error_type="network", retryable=True,
        )
    if not isinstance(status, int) or status < 200 or status >= 300:
        rate_limited = status == 429
        return _result(
            outcome="unavailable", status=status if isinstance(status, int) else None,
            reason=f"HTTP {status}" if isinstance(status, int) else "invalid HTTP status",
            error_type="rate_limit" if rate_limited else "http_error",
            retryable=rate_limited or (isinstance(status, int) and 500 <= status <= 599),
        )
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, TypeError) as exc:
        return _result(
            outcome="error", status=status, reason=f"malformed Atom: {type(exc).__name__}",
            error_type="provider_error", retryable=False,
        )
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    cited_author = _first_author(ref)
    try:
        cited_year = int(ref.get("year")) if ref.get("year") is not None else None
    except (TypeError, ValueError):
        cited_year = None
    for entry in root.findall("atom:entry", ns):
        matched_title = re.sub(
            r"\s+", " ",
            (entry.findtext("atom:title", default="", namespaces=ns) or "").strip(),
        )
        if _identity_title(title) != _identity_title(matched_title):
            continue
        authors = _entry_authors(entry, ns)
        surnames = {
            re.sub(r"[^a-z]", "", name.casefold().split()[-1])
            for name in authors if name.split()
        }
        if cited_author and surnames and cited_author not in surnames:
            continue
        published = entry.findtext("atom:published", default="", namespaces=ns) or ""
        try:
            matched_year = int(published[:4])
        except ValueError:
            matched_year = None
        if cited_year is not None and matched_year is not None and abs(cited_year - matched_year) > 2:
            continue
        entry_id = entry.findtext("atom:id", default="", namespaces=ns) or ""
        match = re.search(r"arxiv\.org/abs/([^v/?#]+)(?:v\d+)?", entry_id, re.IGNORECASE)
        if not match:
            continue
        arxiv_id = match.group(1)
        context = {
                "provider": RESOLVE_NAME,
                "title": matched_title,
                "authors": authors,
                "year": matched_year,
                "identifiers": {"arxiv_id": arxiv_id},
                "canonical_host": True,
                "source_confidence": 0.95,
        }
        items = _candidate_pair(arxiv_id, identity_context=context)
        for item in items:
            item["discovery_reason"] = "lazy exact arXiv title/author/year fallback"
        return _result(items, outcome="success", status=status)
    return _result(outcome="no_match", status=status)
