# core/resolve/providers/springer_openaccess.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Springer Nature Open Access API JATS full-text provider."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
from xml.etree import ElementTree as ET

from ._publisher_routing import matches_publisher


NAME = "springer_openaccess"
CREDENTIAL_SPECS = ({
    "provider": "springer_open_access",
    "env_name": "SPRINGER_NATURE_OPEN_ACCESS_API_KEY",
    "channels": ("fetch",),
    "label": "Springer Nature Open Access",
},)
MANIFEST = {"origin": "springer_nature", "via_aliases": ["springer_nature"]}
ENV_SPRINGER_NATURE_OPEN_ACCESS_API_KEY = "SPRINGER_NATURE_OPEN_ACCESS_API_KEY"
OPEN_ACCESS_JATS_URL = "https://api.springernature.com/openaccess/jats"


def owns_request_url(candidate) -> bool:
    parsed = urllib.parse.urlsplit(str(candidate.get("url") or ""))
    endpoint = urllib.parse.urlsplit(OPEN_ACCESS_JATS_URL)
    return (
        parsed.scheme.lower() == endpoint.scheme
        and (parsed.hostname or "").lower() == endpoint.hostname
        and parsed.port in (None, 443)
        and parsed.path == endpoint.path
    )


_SKIP_TAGS = {"ack", "app-group", "bio", "fn-group", "notes", "ref-list"}


def api_key(environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    value = str(env.get(ENV_SPRINGER_NATURE_OPEN_ACCESS_API_KEY) or "").strip()
    return value or None


def _local_name(tag: object) -> str:
    return str(tag or "").rsplit("}", 1)[-1].lower()


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _doi(value: object, normalize_doi) -> str | None:
    normalized = normalize_doi(value)
    return str(normalized).lower() if normalized else None


def _request_url(doi: str, key: str) -> str:
    return OPEN_ACCESS_JATS_URL + "?" + urllib.parse.urlencode({
        "q": f"doi:{doi}", "p": "1", "s": "1", "api_key": key,
    })


def _source_ref(doi: str) -> str:
    return OPEN_ACCESS_JATS_URL + "?" + urllib.parse.urlencode({
        "q": f"doi:{doi}", "p": "1", "s": "1",
    })


def enabled(*, ref=None, environ=None, **kwargs) -> bool:
    doi = _doi((ref or {}).get("doi"), lambda value: value)
    return bool(
        api_key(environ)
        and doi
        and matches_publisher("springer_nature", doi=doi, url=(ref or {}).get("url"))
    )


def disabled_reason(*, ref=None, environ=None, **kwargs) -> str | None:
    if not api_key(environ):
        return f"missing {ENV_SPRINGER_NATURE_OPEN_ACCESS_API_KEY}"
    if not enabled(ref=ref, environ=environ):
        return "reference is not identified as Springer Nature content with a DOI"
    return None


def _failure(status: int) -> dict:
    if status == 401:
        error_type, retryable = "auth", False
    elif status == 403:
        error_type, retryable = "http_error", False
    elif status == 429:
        error_type, retryable = "rate_limit", True
    else:
        error_type, retryable = "http_error", False
    return {
        "items": [], "status": "error", "error_type": error_type,
        "error_reason": f"{error_type} (HTTP {status})", "retryable": retryable,
        "http_status": status,
    }


def _node_text(node: ET.Element) -> str:
    return _clean_text(" ".join(node.itertext()))


def _article_doi(article: ET.Element, normalize_doi) -> str | None:
    for node in article.iter():
        tag = _local_name(node.tag)
        if tag == "doi" or (tag == "article-id" and str(node.attrib.get("pub-id-type") or "").lower() == "doi"):
            doi = _doi(_node_text(node), normalize_doi)
            if doi:
                return doi
    return None


def _article_title(article: ET.Element) -> str | None:
    for node in article.iter():
        if _local_name(node.tag) == "article-title":
            return _node_text(node) or None
    return None


def _body_text(body: ET.Element) -> str:
    chunks: list[str] = []

    def walk(node: ET.Element) -> None:
        tag = _local_name(node.tag)
        if tag in _SKIP_TAGS:
            return
        if tag in {"title", "p"}:
            text = _node_text(node)
            if text and (not chunks or chunks[-1] != text):
                chunks.append(text)
            return
        for child in node:
            walk(child)

    walk(body)
    return "\n\n".join(chunks)


def _has_body_paragraph(body: ET.Element | None) -> bool:
    def walk(node: ET.Element) -> bool:
        if _local_name(node.tag) in _SKIP_TAGS:
            return False
        if _local_name(node.tag) == "p" and _node_text(node):
            return True
        return any(walk(child) for child in node)

    return bool(body is not None and walk(body))


def _front_identity_text(article: ET.Element) -> str | None:
    """Return the native JATS front matter for identity checks only."""
    front = next((node for node in article if _local_name(node.tag) == "front"), None)
    if front is None:
        return None
    return _node_text(front) or None


def direct_text_items(ref: dict, *, get_fn, normalize_doi, environ: dict[str, str] | None = None, **kwargs) -> list[dict] | dict:
    key = api_key(environ)
    doi = _doi(ref.get("doi"), normalize_doi)
    if not key or not doi or not matches_publisher("springer_nature", doi=doi, url=ref.get("url")):
        return []
    try:
        status, payload = get_fn(_request_url(doi, key), accept="application/xml", profile="api")
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
        return {"items": [], "status": "error", "error_type": "provider_error", "error_reason": "Springer Open Access returned malformed JATS XML", "retryable": False, "http_status": status_code}
    articles = [node for node in root.iter() if _local_name(node.tag) == "article"]
    matches = [article for article in articles if _article_doi(article, normalize_doi) == doi]
    if len(matches) != 1 or len(articles) != 1:
        return {"items": [], "status": "rejected", "error_reason": "Springer Open Access response did not contain one exact DOI article", "retryable": False, "http_status": status_code}
    article = matches[0]
    body = next((node for node in article.iter() if _local_name(node.tag) == "body"), None)
    text = _body_text(body) if body is not None else ""
    if not _has_body_paragraph(body) or not text:
        return {"items": [], "status": "unavailable", "error_reason": "Springer Open Access response contained no article body paragraph", "retryable": False, "http_status": status_code}
    identity_context = {"provider": NAME, "identifiers": {"doi": doi}}
    title = _article_title(article)
    if title:
        identity_context["title"] = title
    item = {
        "method": NAME,
        "text": text,
        "source_ref": _source_ref(doi),
        "extract_method": "api_jats",
        "identity_context": identity_context,
    }
    identity_extract_text = _front_identity_text(article)
    if identity_extract_text:
        item["identity_extract_text"] = identity_extract_text
        item["identity_extract_method"] = "api_jats_front"
    return [item]
