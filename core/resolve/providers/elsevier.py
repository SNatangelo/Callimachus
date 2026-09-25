#!/usr/bin/env python3
# core/resolve/providers/elsevier.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Elsevier Article Retrieval API full-text provider."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
from xml.etree import ElementTree as ET

from ._publisher_routing import matches_publisher


NAME = "elsevier"
CREDENTIAL_SPECS = ({
    "provider": "elsevier",
    "env_name": "ELSEVIER_API_KEY",
    "channels": ("fetch",),
    "label": "Elsevier",
},)
MANIFEST = {
    "origin": "elsevier",
    "via_aliases": ["elsevier"],
}
ENV_ELSEVIER_API_KEY = "ELSEVIER_API_KEY"
ARTICLE_RETRIEVAL_URL = "https://api.elsevier.com/content/article"


def owns_request_url(candidate) -> bool:
    parsed = urllib.parse.urlsplit(str(candidate.get("url") or ""))
    endpoint = urllib.parse.urlsplit(ARTICLE_RETRIEVAL_URL)
    return (
        parsed.scheme.lower() == endpoint.scheme
        and (parsed.hostname or "").lower() == endpoint.hostname
        and parsed.port in (None, 443)
        and (
            parsed.path == endpoint.path
            or parsed.path.startswith(endpoint.path + "/")
        )
    )

_PII_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9().-]{5,63}")
_RETRYABLE_SERVER_CODES = {502, 503, 504}


def api_key(environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    value = str(env.get(ENV_ELSEVIER_API_KEY) or "").strip()
    return value or None


def _local_name(tag: object) -> str:
    return str(tag or "").rsplit("}", 1)[-1].lower()


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _text_of(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return _clean_text(" ".join(node.itertext()))


def _first_element(root: ET.Element | None, *names: str) -> ET.Element | None:
    if root is None:
        return None
    wanted = {name.lower() for name in names}
    return next((node for node in root.iter() if _local_name(node.tag) in wanted), None)


def _first_text(root: ET.Element | None, *names: str) -> str:
    return _text_of(_first_element(root, *names))


def _normalized_pii(value: object) -> str | None:
    text = urllib.parse.unquote(str(value or "")).strip()
    if not text or _PII_PATTERN.fullmatch(text) is None:
        return None
    return text.upper()


def _pii_from_ref(ref: dict | None) -> str | None:
    if not isinstance(ref, dict):
        return None
    explicit = _normalized_pii(ref.get("pii"))
    if explicit:
        return explicit
    url = str(ref.get("url") or "").strip()
    if not url:
        return None
    match = re.search(r"/(?:article/)?pii/([^/?#]+)", url, re.I)
    return _normalized_pii(match.group(1)) if match else None


def _raw_normalized_doi(value: object) -> str | None:
    text = urllib.parse.unquote(str(value or "")).strip()
    text = re.sub(r"^https?://(?:www\.)?(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi:\s*", "", text, flags=re.I).strip()
    return text.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", text) else None


def _supports_ref(ref: dict | None) -> bool:
    if not isinstance(ref, dict):
        return True
    doi = _raw_normalized_doi(ref.get("doi"))
    return bool(
        matches_publisher(NAME, doi=doi, url=ref.get("url"))
        or _pii_from_ref(ref)
    )


def enabled(*, ref=None, environ=None, **kwargs) -> bool:
    return bool(api_key(environ) and _supports_ref(ref))


def disabled_reason(*, ref=None, environ=None, **kwargs) -> str | None:
    if not api_key(environ):
        return f"missing {ENV_ELSEVIER_API_KEY}"
    if not _supports_ref(ref):
        return "reference is not identified as Elsevier content"
    return None


def _request_identity(ref: dict, normalize_doi) -> tuple[str, str] | None:
    doi = normalize_doi(ref.get("doi"))
    if doi and matches_publisher(NAME, doi=doi, url=ref.get("url")):
        return "doi", str(doi)
    pii = _pii_from_ref(ref)
    if pii:
        return "pii", pii
    return None


def _request_url(kind: str, identifier: str) -> str:
    safe = "/" if kind == "doi" else ""
    encoded = urllib.parse.quote(identifier, safe=safe)
    return f"{ARTICLE_RETRIEVAL_URL}/{kind}/{encoded}"


def _failure(status: int) -> dict | list:
    if status == 404:
        return []
    if status == 401:
        category, retryable = "auth", False
    elif status == 403:
        # Elsevier documents 403 for authorization/entitlement failures.  It is
        # not safe to diagnose the configured API key as expired from this signal.
        category, retryable = "http_error", False
    elif status == 429:
        category, retryable = "rate_limit", True
    elif 500 <= status <= 599:
        category, retryable = "transient_server", status in _RETRYABLE_SERVER_CODES
    else:
        category, retryable = "http_error", False
    reason = (
        "Elsevier entitlement/access denied (HTTP 403)"
        if status == 403
        else f"{category} (HTTP {status})"
    )
    return {
        "items": [],
        "status": "error",
        "error_type": category,
        "error_reason": reason,
        "retryable": retryable,
        "http_status": status,
    }


def _response_identifiers(coredata: ET.Element | None, normalize_doi) -> tuple[str | None, str | None]:
    doi = normalize_doi(_first_text(coredata, "doi"))
    if not doi:
        identifier = _first_text(coredata, "identifier")
        doi = normalize_doi(identifier)
    return doi, _normalized_pii(_first_text(coredata, "pii"))


def _doi_key(value: object, normalize_doi) -> str | None:
    normalized = normalize_doi(value)
    return str(normalized).lower() if normalized else None


def _body_text(body: ET.Element) -> str:
    chunks: list[str] = []
    pending_headings: list[str] = []
    for node in body.iter():
        tag = _local_name(node.tag)
        value = _text_of(node)
        if tag == "section-title" and value:
            pending_headings.append(value)
        elif tag in {"para", "simple-para"} and value:
            for heading in pending_headings:
                if not chunks or chunks[-1] != heading:
                    chunks.append(heading)
            pending_headings.clear()
            if not chunks or chunks[-1] != value:
                chunks.append(value)
    return "\n\n".join(chunks)


def _article_text(
    coredata: ET.Element | None,
    body: ET.Element,
) -> tuple[str, str | None, str | None]:
    title = _first_text(coredata, "title") or None
    body_text = _body_text(body)
    cover_date = _first_text(coredata, "coverdate", "coverdisplaydate")
    year_match = re.search(r"\b(18|19|20)\d{2}\b", cover_date)
    return body_text, title, year_match.group(0) if year_match else None


def direct_text_items(
    ref: dict,
    *,
    get_fn,
    normalize_doi,
    environ: dict[str, str] | None = None,
    **kwargs,
) -> list[dict] | dict:
    key = api_key(environ)
    request_identity = _request_identity(ref, normalize_doi)
    if not key or request_identity is None:
        return []
    requested_kind, requested_identifier = request_identity
    url = _request_url(requested_kind, requested_identifier)
    try:
        status, payload = get_fn(
            url,
            accept="text/xml",
            profile="api",
            headers_extra={"X-ELS-APIKey": key},
        )
    except urllib.error.HTTPError as exc:
        return _failure(exc.code)

    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 0
    if not 200 <= status_code < 300:
        return _failure(status_code)
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, TypeError, ValueError):
        return {
            "items": [],
            "status": "error",
            "error_type": "provider_error",
            "error_reason": "Elsevier returned malformed article XML",
            "retryable": False,
            "http_status": status_code,
        }

    coredata = _first_element(root, "coredata")
    response_doi, response_pii = _response_identifiers(coredata, normalize_doi)
    identity_matches = (
        _doi_key(response_doi, normalize_doi) == _doi_key(requested_identifier, normalize_doi)
        if requested_kind == "doi"
        else response_pii == _normalized_pii(requested_identifier)
    )
    if not identity_matches:
        return {
            "items": [],
            "status": "rejected",
            "error_reason": "Elsevier response identifier did not match the requested article",
            "retryable": False,
            "http_status": status_code,
        }

    original_text = _first_element(root, "originaltext")
    body = _first_element(original_text, "body")
    if body is None:
        return {
            "items": [],
            "status": "unavailable",
            "error_reason": "Elsevier response contained metadata/abstract only; no article body",
            "retryable": False,
            "http_status": status_code,
        }

    text, title, year = _article_text(coredata, body)
    if not text:
        return {
            "items": [],
            "status": "unavailable",
            "error_reason": "Elsevier article body was empty after XML extraction",
            "retryable": False,
            "http_status": status_code,
        }
    identifiers = {}
    if response_doi:
        identifiers["doi"] = response_doi
    if response_pii:
        identifiers["pii"] = response_pii
    identity_context = {"provider": NAME, "identifiers": identifiers}
    if title:
        identity_context["title"] = title
    if year:
        identity_context["year"] = year
    return [{
        "method": NAME,
        "text": text,
        "source_ref": url,
        "extract_method": "api_xml",
        "identity_context": identity_context,
    }]
