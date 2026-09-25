# core/resolve/providers/zenodo.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Conservative Zenodo publication-copy fallback."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse


NAME = "zenodo"
API_URL = "https://zenodo.org/api/records"
PREPRINT_RESOLVER = True
CREDENTIAL_SPECS = ({
    "provider": NAME,
    "env_name": "ZENODO_ACCESS_TOKEN",
    "channels": ("fetch",),
    "label": "Zenodo",
},)
MANIFEST = {
    "canonical_hosts": ["zenodo.org"],
    "host_markers": ["zenodo"],
}

_DOI = re.compile(r"10\.\d{4,9}/\S+", re.I)
_UNSUITABLE = re.compile(
    r"(?:supplement|appendix|dataset|data[._ -]|slides?|poster)", re.I
)
_ALLOWED_HOSTS = {"zenodo.org", "www.zenodo.org"}


class _Items(list):
    def __init__(self, items=(), *, error=None):
        super().__init__(items)
        self._provider_error = error


def _token(environ):
    value = str((environ or {}).get("ZENODO_ACCESS_TOKEN") or "").strip()
    return value if value and "\n" not in value and "\r" not in value else None


def enabled(*, environ=None, **_kwargs):
    return bool(_token(environ))


def disabled_reason(*, environ=None, **_kwargs):
    return None if _token(environ) else "missing ZENODO_ACCESS_TOKEN"


def _doi(value):
    text = urllib.parse.unquote(str(value or "")).strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi:\s*", "", text, flags=re.I)
    return text.casefold() if _DOI.fullmatch(text) else None


def _title(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _surname(value):
    if isinstance(value, dict):
        value = value.get("family") or value.get("name")
    text = str(value or "")
    if "," in text:
        text = text.split(",", 1)[0]
    words = re.findall(r"[a-z]+", text.casefold())
    return words[-1] if words else None


def _ref_author(ref):
    for key in ("ay_surname", "surname", "first_author_surname"):
        value = _surname(ref.get(key))
        if value:
            return value
    return None


def _year(value):
    match = re.search(r"(?:18|19|20)\d{2}", str(value or ""))
    return int(match.group()) if match else None


def _identifiers(record):
    values = []
    sources = (
        record,
        record.get("metadata") if isinstance(record, dict) else {},
    )
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("doi", "conceptdoi", "concept_doi", "alternate_identifiers"):
            raw = source.get(key)
            if isinstance(raw, list):
                values.extend(
                    _doi(item.get("identifier") if isinstance(item, dict) else item)
                    for item in raw
                )
            else:
                values.append(_doi(raw))
        for item in source.get("related_identifiers") or []:
            if isinstance(item, dict):
                values.append(_doi(item.get("identifier")))
    return {item for item in values if item}


def _identity_matches(ref, record, cited_doi):
    if cited_doi and cited_doi in _identifiers(record):
        return True
    meta = record.get("metadata") if isinstance(record, dict) else {}
    cited_title = _title(ref.get("title"))
    if (
        not isinstance(meta, dict)
        or len(cited_title.split()) < 3
        or cited_title != _title(meta.get("title"))
    ):
        return False
    record_author = _surname((meta.get("creators") or [None])[0])
    cited_author = _ref_author(ref)
    if cited_author and record_author and cited_author != record_author:
        return False
    cited_year = _year(ref.get("year"))
    record_year = _year(meta.get("publication_date") or record.get("created"))
    if cited_year and record_year and abs(cited_year - record_year) > 2:
        return False
    return bool(
        (cited_author and record_author) or (cited_year and record_year)
    )


def _pdf_candidates(record):
    files = record.get("files") if isinstance(record, dict) else None
    if not isinstance(files, list):
        return []
    out = []
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("key") or item.get("filename") or "")
        links = item.get("links") if isinstance(item.get("links"), dict) else {}
        url = links.get("content") or links.get("self")
        parsed = urllib.parse.urlsplit(str(url or ""))
        if (
            not filename.casefold().endswith(".pdf")
            or _UNSUITABLE.search(filename)
            or parsed.scheme != "https"
            or parsed.hostname not in _ALLOWED_HOSTS
        ):
            continue
        out.append((filename.casefold(), str(url), str(item.get("checksum") or "")))
    return sorted(out)


def _provider_error(error_type, reason, *, retryable, http_status=None):
    return {
        "status": "error",
        "error_type": error_type,
        "error_reason": reason,
        "retryable": retryable,
        "http_status": http_status,
    }


def _transport_error(exc):
    status = getattr(exc, "code", None)
    if status in (401, 403):
        return _provider_error(
            "auth", f"auth (HTTP {status})", retryable=False, http_status=status
        )
    if status == 429:
        return _provider_error(
            "rate_limit", "rate_limit (HTTP 429)", retryable=True, http_status=429
        )
    if isinstance(status, int) and 500 <= status < 600:
        return _provider_error(
            "transient_server",
            f"transient_server (HTTP {status})",
            retryable=True,
            http_status=status,
        )
    if isinstance(status, int):
        return _provider_error(
            "http_error",
            f"http_error (HTTP {status})",
            retryable=False,
            http_status=status,
        )
    return _provider_error(
        "network", f"network: {type(exc).__name__}", retryable=True
    )


def _malformed_response():
    return _provider_error(
        "malformed_response",
        "malformed Zenodo API response",
        retryable=False,
    )


def _landing_page(record):
    links = record.get("links") if isinstance(record, dict) else None
    value = links.get("self_html") if isinstance(links, dict) else None
    parsed = urllib.parse.urlsplit(str(value or ""))
    if parsed.scheme == "https" and parsed.hostname in _ALLOWED_HOSTS:
        return str(value)
    return None


def _record_order(record):
    if not isinstance(record, dict):
        return ("", -1, "")
    meta = record.get("metadata")
    date = meta.get("publication_date") if isinstance(meta, dict) else ""
    record_id = record.get("id")
    numeric_id = record_id if type(record_id) is int else -1
    return (str(date or ""), numeric_id, str(record_id or ""))


def _context(record, identifiers, cited_doi, landing, checksum):
    meta = record["metadata"]
    names = [
        str(creator.get("name") or creator.get("family") or "").strip()
        for creator in meta.get("creators") or []
        if isinstance(creator, dict)
    ]
    public_identifiers = {}
    if cited_doi and cited_doi in identifiers:
        public_identifiers["doi"] = cited_doi
    elif identifiers:
        public_identifiers["zenodo_doi"] = identifiers[0]
    if checksum:
        public_identifiers["file_checksum"] = checksum
    return {
        "title": meta.get("title"),
        "authors": [name for name in names if name],
        "year": _year(meta.get("publication_date") or record.get("created")),
        "provider": NAME,
        "provider_record_id": str(record["id"]),
        "first_author": _surname((meta.get("creators") or [None])[0]),
        "source_confidence": (
            1.0 if cited_doi and cited_doi in identifiers else 0.8
        ),
        "canonical_host": True,
        "canonical_url": landing,
        "landing_page_url": landing,
        "identifiers": public_identifiers,
    }


def candidate_items(ref, *, get_fn, normalize_doi, environ=None, **_kwargs):
    token = _token(environ)
    if not token:
        return []
    cited_doi = _doi(normalize_doi(ref.get("doi")))
    title = str(ref.get("title") or "").strip()
    queries = []
    if cited_doi:
        queries.append((f'doi:"{cited_doi}"', True))
    if (
        not cited_doi
        and len(_title(title).split()) >= 3
        and (_ref_author(ref) or _year(ref.get("year")))
    ):
        safe_title = title.replace('"', " ")
        queries.append((f'title:"{safe_title}"', False))

    for query, all_versions in queries:
        params = {"q": query, "size": "25", "type": "publication"}
        if all_versions:
            params["all_versions"] = "true"
        url = API_URL + "?" + urllib.parse.urlencode(params)
        try:
            status, body = get_fn(
                url,
                accept="application/json",
                profile="api",
                headers_extra={"Authorization": f"Bearer {token}"},
            )
        except Exception as exc:
            return _Items(error=_transport_error(exc))
        if type(status) is not int:
            return _Items(error=_malformed_response())
        if not 200 <= status < 300:
            error = urllib.error.HTTPError(
                API_URL, status, "Zenodo API error", {}, None
            )
            return _Items(error=_transport_error(error))
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except (AttributeError, TypeError, ValueError):
            return _Items(error=_malformed_response())
        hits = payload.get("hits", {}).get("hits") if isinstance(payload, dict) else None
        if not isinstance(hits, list):
            return _Items(error=_malformed_response())

        out_by_url = {}
        for record in sorted(hits, key=_record_order, reverse=True):
            meta = record.get("metadata") if isinstance(record, dict) else None
            resource_type = meta.get("resource_type") if isinstance(meta, dict) else None
            if (
                not isinstance(meta, dict)
                or meta.get("access_right") != "open"
                or not isinstance(resource_type, dict)
                or resource_type.get("type") != "publication"
                or not isinstance(meta.get("title"), str)
                or not meta["title"].strip()
                or not isinstance(meta.get("creators"), list)
                or not meta["creators"]
                or not all(isinstance(item, dict) for item in meta["creators"])
                or record.get("id") is None
                or not _identity_matches(ref, record, cited_doi)
            ):
                continue
            identifiers = sorted(_identifiers(record))
            landing = _landing_page(record)
            for _name, pdf_url, checksum in _pdf_candidates(record):
                out_by_url.setdefault(pdf_url, {
                    "method": NAME,
                    "url": pdf_url,
                    "kind": "pdf",
                    "content_version": "preprint",
                    "discovery_reason": "identity-matched Zenodo publication copy",
                    "identity_context": _context(
                        record, identifiers, cited_doi, landing, checksum
                    ),
                })
        if out_by_url:
            return _Items(list(out_by_url.values())[:3])
    return _Items()
