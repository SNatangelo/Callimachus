# core/fetch/fallbacks/internet_archive_items.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Internet Archive *item* fallback for deterministic full-text Fetch.

This is deliberately distinct from Wayback: it searches the Archive.org item
catalogue for a bibliographically confirmed work, then hands one public PDF to
the ordinary Fetch queue.  It does not use IAS3 credentials and never treats a
catalogue miss or transport failure as proof that the work is absent.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse

from core.fetch import hosts as _hosts
from core.fetch.refdata import _normalize_doi, _title_key
from core.fetch.transport import host_limiter as _host_limiter
from core.fetch.transport.http import FetchAdmissionDeferred
from core.resolve import sources as _sources

_SEARCH = "https://archive.org/advancedsearch.php"
_METADATA = "https://archive.org/metadata/{}"
_DOWNLOAD = "https://archive.org/download/{}/{}"
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")


class LookupUnresolved(RuntimeError):
    """A catalogue/metadata response was not usable, rather than a true miss."""

    def __init__(self, url: str, reason: str, *, status: int | None = None) -> None:
        self.url = url
        self.reason = reason
        self.status = status
        super().__init__(reason)


class CandidateResult(list):
    """List-compatible candidate result carrying the closed no-candidate fact."""

    def __init__(self, candidates=(), *, reason_code: str | None = None,
                 reason: str | None = None, lookup_url: str | None = None,
                 http_status: int | None = None) -> None:
        super().__init__(candidates)
        self.reason_code = reason_code
        self.reason = reason
        self.lookup_url = lookup_url
        self.http_status = http_status


def _text(value) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value if item is not None)
    return str(value or "").strip()


def _dois(value) -> set[str]:
    values = value if isinstance(value, (list, tuple)) else [value]
    return {
        normalized.lower()
        for raw in values
        for normalized in [_normalize_doi(_text(raw))]
        if normalized
    }


def _year(value) -> int | None:
    match = _YEAR_RE.search(_text(value))
    return int(match.group(1)) if match else None


def _true(value) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes"}


def _identity(ref: dict) -> dict | None:
    doi = _normalize_doi(ref.get("doi"))
    title = _text(ref.get("title"))
    title_key = _title_key(title)
    author = _sources._first_author_surname(ref)
    year = _year(ref.get("year"))
    if str(ref.get("source_type") or "").lower() in {"webpage", "web", "website"}:
        return None
    if doi:
        return {"doi": doi, "title": title, "title_key": title_key, "author": author, "year": year}
    # A title-only catalogue query is safe only when it has the author anchor and
    # enough words to make an exact normalized comparison meaningful.
    if author and len(title_key.split()) >= 3:
        return {"doi": None, "title": title, "title_key": title_key, "author": author, "year": year}
    return None


def lookup_key(ref: dict) -> str | None:
    """Stable per-reference memo key; does not claim that a request was sent."""
    identity = _identity(ref)
    if identity is None:
        return None
    if identity["doi"]:
        return "doi:" + identity["doi"].casefold()
    return "title:" + "|".join((
        identity["title_key"].casefold(),
        _text(identity["author"]).casefold(),
        str(identity["year"] or ""),
    ))


def _json_response(url: str, *, fetch_url) -> dict:
    try:
        result = fetch_url(url, accept="application/json", profile="document")
    except FetchAdmissionDeferred as exc:
        exc.url = url
        raise
    except Exception as exc:
        raise LookupUnresolved(
            url, f"Internet Archive transport failure: {type(exc).__name__}",
        ) from exc
    try:
        status = int(result.get("status", 0)) if isinstance(result, dict) else 0
    except (TypeError, ValueError):
        status = 0
    if status == 429:
        host = _host_limiter.host_for_url(url)
        until = _hosts._shared_host_limiter().cooldown_until(host)
        if (
            isinstance(until, bool)
            or not isinstance(until, (int, float))
            or not math.isfinite(until)
            or until <= 0
        ):
            raise RuntimeError("Internet Archive HTTP 429 did not produce a finite host cooldown")
        deferred = FetchAdmissionDeferred(host, max(0.0, until - time.monotonic()))
        deferred.reason_code = "rate_limit_response"
        deferred.url = url
        deferred.http_status = 429
        raise deferred
    if not 200 <= status < 300:
        display_status = status or "no response"
        raise LookupUnresolved(url, f"Internet Archive returned HTTP {display_status}", status=status or None)
    body = result.get("body")
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise LookupUnresolved(url, "Internet Archive returned invalid JSON", status=status) from exc
    if not isinstance(data, dict) or data.get("error"):
        raise LookupUnresolved(url, "Internet Archive returned an error payload", status=status)
    return data


def _search_url(identity: dict) -> str:
    def solr_quote(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    query = (
        (
            f'(doi:"{solr_quote(identity["doi"])}" OR '
            f'external-identifier:"doi:{solr_quote(identity["doi"])}" OR '
            f'external-identifier:"https://doi.org/{solr_quote(identity["doi"])}") '
            "AND mediatype:texts"
        )
        if identity["doi"] else f'title:"{solr_quote(identity["title"])}" AND mediatype:texts'
    )
    params = [
        ("q", query), ("fl[]", "identifier"), ("fl[]", "title"),
        ("fl[]", "doi"), ("fl[]", "external-identifier"),
        ("fl[]", "creator"), ("fl[]", "year"),
        ("rows", "5"), ("output", "json"),
    ]
    return _SEARCH + "?" + urllib.parse.urlencode(params)


def _document_matches(document: dict, identity: dict) -> bool:
    if identity["doi"]:
        stated_dois = _dois(document.get("doi")) | _dois(document.get("external-identifier"))
        return identity["doi"].lower() in stated_dois
    if _title_key(document.get("title")) != identity["title_key"]:
        return False
    creators = document.get("creator")
    creators = creators if isinstance(creators, list) else [creators]
    first_creator = _text(creators[0]).lower() if creators else ""
    if not identity["author"] or not re.search(
        rf"(?<![a-z0-9]){re.escape(identity['author'].lower())}(?![a-z0-9])", first_creator,
    ):
        return False
    document_year, cited_year = _year(document.get("year")), identity["year"]
    return not (document_year and cited_year and document_year != cited_year)


def _public_pdf(metadata: dict) -> str | None:
    item = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
    if _true(item.get("access-restricted-item")) or _true(item.get("private")):
        return None
    choices = []
    for entry in metadata.get("files") or []:
        if not isinstance(entry, dict):
            continue
        name = _text(entry.get("name"))
        if not name or _true(entry.get("private")) or _true(entry.get("access-restricted")):
            continue
        if name.lower().endswith(".pdf"):
            choices.append((0 if _text(entry.get("source")).lower() == "original" else 1, name.casefold(), name))
    return min(choices)[2] if choices else None


def _metadata_matches(metadata: dict, identifier: str, identity: dict) -> bool:
    item = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
    stated_identifier = _text(item.get("identifier"))
    if stated_identifier and stated_identifier != identifier:
        return False
    stated_dois = _dois(item.get("doi")) | _dois(item.get("external-identifier"))
    if identity["doi"] and stated_dois and identity["doi"].lower() not in stated_dois:
        return False
    title = _text(item.get("title"))
    if identity["title_key"] and title and _title_key(title) != identity["title_key"]:
        return False
    creators = item.get("creator")
    creators = creators if isinstance(creators, list) else [creators]
    first_creator = _text(creators[0]).lower() if creators else ""
    if identity["author"] and first_creator and not re.search(
        rf"(?<![a-z0-9]){re.escape(identity['author'].lower())}(?![a-z0-9])", first_creator,
    ):
        return False
    stated_year = _year(item.get("year"))
    return not (identity["year"] and stated_year and identity["year"] != stated_year)


def build_candidates(ref: dict, *, fetch_url) -> CandidateResult:
    """Return at most one public, uniquely matched Archive.org item PDF.

    There is one advanced search, then one metadata request only for a unique
    bibliographic match. ``FetchAdmissionDeferred`` deliberately escapes this
    function so the scheduler retains its cooldown semantics.
    """
    identity = _identity(ref)
    if identity is None:
        return CandidateResult(
            reason_code="ineligible_identity",
            reason="Internet Archive item lookup requires a DOI or a distinctive title with first author",
        )
    search_url = _search_url(identity)
    payload = _json_response(search_url, fetch_url=fetch_url)
    response = payload.get("response")
    docs = response.get("docs") if isinstance(response, dict) else None
    if not isinstance(docs, list):
        raise LookupUnresolved(
            search_url, "Internet Archive search returned an invalid response payload",
            status=200,
        )
    matches = [doc for doc in docs if isinstance(doc, dict) and _document_matches(doc, identity)]
    identifiers = {_text(doc.get("identifier")) for doc in matches if _text(doc.get("identifier"))}
    if len(identifiers) != 1:
        return CandidateResult(
            reason_code="no_unique_match",
            reason="Internet Archive search did not yield one uniquely matching item",
            lookup_url=search_url, http_status=200,
        )
    identifier = next(iter(identifiers))
    metadata_url = _METADATA.format(urllib.parse.quote(identifier, safe=""))
    metadata = _json_response(metadata_url, fetch_url=fetch_url)
    if not _metadata_matches(metadata, identifier, identity):
        return CandidateResult(
            reason_code="metadata_identity_conflict",
            reason="Internet Archive item metadata conflicts with the matched citation identity",
            lookup_url=metadata_url, http_status=200,
        )
    item = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
    if _true(item.get("access-restricted-item")) or _true(item.get("private")):
        return CandidateResult(
            reason_code="restricted_item",
            reason="Internet Archive item is access restricted or private",
            lookup_url=metadata_url, http_status=200,
        )
    filename = _public_pdf(metadata)
    if not filename:
        return CandidateResult(
            reason_code="no_public_pdf",
            reason="Internet Archive item has no public PDF file",
            lookup_url=metadata_url, http_status=200,
        )
    context = {
        "provider": "internet_archive_items",
        "title": identity["title"] or _text(matches[0].get("title")),
        "first_author": identity["author"],
        "year": identity["year"],
        "identifiers": {"doi": identity["doi"]} if identity["doi"] else {},
    }
    return CandidateResult([{
        "url": _DOWNLOAD.format(urllib.parse.quote(identifier, safe=""), urllib.parse.quote(filename, safe="")),
        "kind": "pdf",
        "method": "internet_archive_item",
        "fallback_stage": "internet_archive_item",
        "referer": None,
        "identity_context": context,
        "provenance": ["internet_archive_items", identifier],
        "discovery_reason": f"public PDF {filename} in uniquely matched Internet Archive item {identifier}",
    }])
