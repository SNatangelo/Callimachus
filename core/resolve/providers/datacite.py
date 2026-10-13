#!/usr/bin/env python3
# core/resolve/providers/datacite.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""DataCite title-search resolver module."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse

NAME = "datacite_search"
MANIFEST = {}
DATACITE_NATIVE_DOI_PROOF_FIELD = "_datacite_native_doi_proof"
_DATACITE_NATIVE_DOI_PROOF_FIELDS = {
    "version", "requested_doi", "request_url", "http_status",
    "response_body", "response_sha256",
}
_DATACITE_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s]+", re.IGNORECASE)

_QUERY_LITERAL_RE = re.compile(
    r'(?<!\w)(?:AND|OR|NOT)(?!\w)|[+\-=&|!(){}\[\]^"~*?:\\/]'
)
_SOFTWARE_TYPE_MARKER_RE = re.compile(
    r"\[\s*(?:software|computer\s+software)\s*\]", re.IGNORECASE
)
_VERSIONED_SOFTWARE_TITLE_RE = re.compile(
    r"(?P<work>.+?)\s*\(\s*(?:v|version)\s*"
    r"(?P<release>\d+(?:\.\d+)+)\s*\)\s*$",
    re.IGNORECASE,
)


def _escape_query_title(title: str) -> str:
    """Escape query-string syntax while keeping free-term title matching."""
    return _QUERY_LITERAL_RE.sub(lambda match: "\\" + match.group(0), title)


def _explicit_software_version(ref: dict, title: str) -> tuple[str, str] | None:
    raw_entry = ref.get("raw_entry")
    if (
        not isinstance(raw_entry, str)
        or not _SOFTWARE_TYPE_MARKER_RE.search(raw_entry)
    ):
        return None
    match = _VERSIONED_SOFTWARE_TITLE_RE.fullmatch(title)
    if not match:
        return None
    work = match.group("work").strip()
    release = match.group("release")
    if not work:
        return None
    return work, release


def _matches_cited_version(cited_release: str, metadata_version: object) -> bool:
    if not isinstance(metadata_version, str):
        return False
    version = metadata_version.strip()
    if version[:1].casefold() == "v":
        version = version[1:].strip()
    return version == cited_release


def _native_doi_request_url(doi: str) -> str:
    return "https://api.datacite.org/dois/" + urllib.parse.quote(doi)


def _validate_native_doi_proof(proof: object, *, expected_doi: str | None = None) -> dict:
    if type(proof) is not dict or set(proof) != _DATACITE_NATIVE_DOI_PROOF_FIELDS:
        raise ValueError("DataCite native DOI proof has an invalid field set")
    if type(proof["version"]) is not int or proof["version"] != 1:
        raise ValueError("DataCite native DOI proof version is invalid")
    requested_doi = proof["requested_doi"]
    if (
        not isinstance(requested_doi, str)
        or _DATACITE_DOI_RE.fullmatch(requested_doi) is None
        or requested_doi != requested_doi.strip()
    ):
        raise ValueError("DataCite native DOI proof requested DOI is invalid")
    if expected_doi is not None and requested_doi.casefold() != expected_doi.casefold():
        raise ValueError("DataCite native DOI proof is bound to a different DOI")
    if proof["request_url"] != _native_doi_request_url(requested_doi):
        raise ValueError("DataCite native DOI proof request URL is invalid")
    if type(proof["http_status"]) is not int or proof["http_status"] != 200:
        raise ValueError("DataCite native DOI proof HTTP status is invalid")
    body = proof["response_body"]
    digest = proof["response_sha256"]
    if type(body) is not str or not body:
        raise ValueError("DataCite native DOI proof body is invalid")
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("DataCite native DOI proof response digest is invalid")
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
        raise ValueError("DataCite native DOI proof body digest does not match")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ValueError("DataCite native DOI proof body is malformed") from exc
    record = payload.get("data") if isinstance(payload, dict) else None
    attributes = record.get("attributes") if isinstance(record, dict) else None
    record_id = record.get("id") if isinstance(record, dict) else None
    record_doi = attributes.get("doi") if isinstance(attributes, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("type") != "dois"
        or not isinstance(record_id, str)
        or record_id.strip().casefold() != requested_doi.casefold()
        or not isinstance(attributes, dict)
        or not isinstance(record_doi, str)
        or record_doi.strip().casefold() != requested_doi.casefold()
    ):
        raise ValueError("DataCite native DOI proof body is not the requested DOI record")
    return json.loads(json.dumps(proof, ensure_ascii=False))


def _native_doi_record_from_proof(
    proof: object, *, expected_doi: str | None = None,
) -> dict:
    validated = _validate_native_doi_proof(proof, expected_doi=expected_doi)
    payload = json.loads(validated["response_body"])
    return payload["data"]


def validate_doi(doi: str) -> dict:
    """Retrieve one DOI's authoritative DataCite record for central admission."""
    from core.resolve import service as resolve_mod

    via = "identifier_validation:datacite"
    if not isinstance(doi, str) or _DATACITE_DOI_RE.fullmatch(doi) is None:
        return {
            "status": "unverified",
            "via": via,
            "reason": "invalid DOI for DataCite direct lookup",
        }
    url = _native_doi_request_url(doi)
    try:
        status, body = resolve_mod._get(url, accept="application/vnd.api+json")
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = ""
        if status == 404:
            return {
                "status": "not_found",
                "via": via,
                "http_status": status,
                "reason": "DOI not found on DataCite (404)",
            }
        return {
            "status": "unresolved",
            "via": via,
            "http_status": status,
            "error_type": "network",
            "retryable": status == 429 or status >= 500,
            "reason": f"DataCite DOI lookup HTTP {status}",
        }
    except Exception as exc:
        return {
            "status": "unresolved",
            "via": via,
            "error_type": "network",
            "retryable": True,
            "reason": f"DataCite DOI lookup network: {type(exc).__name__}",
        }
    if status != 200:
        if status == 404:
            return {
                "status": "not_found",
                "via": via,
                "http_status": status,
                "reason": "DOI not found on DataCite (404)",
            }
        return {
            "status": "unresolved",
            "via": via,
            "http_status": status,
            "error_type": "network",
            "retryable": status == 429 or status >= 500,
            "reason": f"DataCite DOI lookup HTTP {status}",
        }
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            body = None
    if not isinstance(body, str):
        return {
            "status": "unverified",
            "via": via,
            "http_status": status,
            "reason": "DataCite DOI response is not UTF-8 text",
        }
    proof = {
        "version": 1,
        "requested_doi": doi,
        "request_url": url,
        "http_status": status,
        "response_body": body,
        "response_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    try:
        proof = _validate_native_doi_proof(proof, expected_doi=doi)
    except ValueError as exc:
        return {
            "status": "unverified",
            "via": via,
            "http_status": status,
            "reason": f"DataCite DOI response did not attest the requested DOI: {exc}",
        }
    record = _native_doi_record_from_proof(proof, expected_doi=doi)
    titles = (record.get("attributes") or {}).get("titles") or []
    title = titles[0].get("title") if titles and isinstance(titles[0], dict) else None
    result = {
        "status": "resolved",
        "via": via,
        "doi": doi,
        "http_status": status,
        "reason": "DataCite returned the requested DOI record",
        DATACITE_NATIVE_DOI_PROOF_FIELD: proof,
    }
    if isinstance(title, str) and title.strip():
        result["matched_title"] = re.sub(r"\s+", " ", title).strip()
    return result


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return (
        not ref.get("url")
        and resolve_mod._article_like_resolution_candidate(ref)
    )


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    """Title search on DataCite - covers datasets, preprints, and grey literature.

    DataCite is the DOI registration agency for datasets, software, and
    many preprints. Its title search is less precise than Crossref/OpenAlex
    but covers material those services miss. Used as a deep fallback after
    CORE and arXiv have been tried.
    """
    title = resolve_mod._article_title_candidate(ref)
    if not title:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no usable title for DataCite search",
        }

    raw_entry = resolve_mod._tex_strip_braces(ref.get("raw_entry") or "")
    ref_title = resolve_mod._article_title_candidate(ref)
    year = ref.get("year")

    software_version = _explicit_software_version(ref, title)
    if software_version is None:
        query_title = _escape_query_title(re.sub(r"\s*/+\s*", " ", title[:200]))
        query = f"titles.title:{query_title}"
    else:
        work_name, cited_release = software_version
        query_title = _escape_query_title(
            re.sub(r"\s*/+\s*", " ", work_name[:200])
        )
        query = (
            f"titles.title:({query_title}) AND "
            f"version:(v{cited_release} OR {cited_release})"
        )
    params = {
        "query": query,
        "page[size]": "5",
    }
    url = "https://api.datacite.org/dois?" + urllib.parse.urlencode(params)
    try:
        _status, body = resolve_mod._get(url, accept="application/vnd.api+json")
        data = json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return {
                "status": "unresolved",
                "via": NAME,
                "reason": "rate_limited (HTTP 429)",
            }
        return {
            "status": "unresolved",
            "via": NAME,
            "reason": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "status": "unresolved",
            "via": NAME,
            "reason": f"network: {type(exc).__name__}",
        }

    items = data.get("data") or []
    if not items:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no results on DataCite",
        }

    best = None
    best_overlap = -1.0
    scoring_title = ref_title
    raw_match_context = raw_entry
    if software_version is not None:
        scoring_title, _cited_release = software_version
        raw_match_context = scoring_title
    for item in items:
        attr = item.get("attributes") or {}
        if software_version is not None:
            native_types = attr.get("types")
            if (
                not isinstance(native_types, dict)
                or native_types.get("resourceTypeGeneral") != "Software"
                or not _matches_cited_version(cited_release, attr.get("version"))
            ):
                continue
        titles = attr.get("titles") or []
        entry_title = (titles[0].get("title", "") if titles else "").strip()
        entry_title = re.sub(r"\s+", " ", entry_title)
        overlap = resolve_mod._title_match_score(scoring_title, entry_title)
        if (
            overlap == 0.0
            and len(resolve_mod._sources._tokens(scoring_title))
            < resolve_mod.TITLE_MIN_TOKENS
        ):
            raw_overlap = resolve_mod.title_overlap(entry_title, raw_match_context)
            if raw_overlap is not None and raw_overlap >= 0.95:
                overlap = raw_overlap
        if overlap is not None and overlap > best_overlap:
            best_overlap = overlap
            best = (item, attr)

    if best is None or best_overlap < 0.30:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no DataCite match met the confidence threshold",
        }

    _item, attr = best
    matched_title = ""
    titles = attr.get("titles") or []
    if titles:
        matched_title = (titles[0].get("title", "") or "").strip()
        matched_title = re.sub(r"\s+", " ", matched_title)
    doi = attr.get("doi")
    matched_year_str = attr.get("publicationYear")
    try:
        matched_year = int(matched_year_str) if matched_year_str else None
    except (ValueError, TypeError):
        matched_year = None
    fl_links = []
    if doi:
        fl_links.append({"url": f"https://doi.org/{doi}", "content_type": "doi"})

    if (
        year is not None
        and matched_year is not None
        and abs(int(year) - matched_year) > 2
        and best_overlap < 0.70
    ):
        return {
            "status": "unverified",
            "via": NAME,
            "matched_title": matched_title,
            "reason": (
                f"year mismatch (cited {year}, "
                f"DataCite {matched_year}, overlap {best_overlap:.3f})"
            ),
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
        }

    return {
        "status": "resolved",
        "via": NAME,
        "matched_title": matched_title,
        "doi": doi,
        "retracted": False,
        "reason": f"DataCite title search match (overlap {best_overlap:.3f})",
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium" if best_overlap >= 0.70 else "low",
        "fulltext_exists": True if doi else False,
        "oa_status": "unknown",
        "fulltext_links": fl_links,
        "metadata_match": {
            "title_overlap": best_overlap,
            "matched_year": matched_year,
        },
    }
