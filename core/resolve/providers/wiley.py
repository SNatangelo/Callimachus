#!/usr/bin/env python3
# core/resolve/providers/wiley.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Wiley TDM PDF candidate provider.

The API token is deliberately resolved only at request time through
``request_headers`` so it cannot enter candidate plans, traces, or storage.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Mapping

from ._publisher_routing import matches_publisher


NAME = "wiley"
MANIFEST = {"origin": NAME, "via_aliases": [NAME]}
ENV_TDM_API_TOKEN = "TDM_API_TOKEN"
CREDENTIAL_SPECS = ({
    "provider": "wiley_tdm",
    "env_name": ENV_TDM_API_TOKEN,
    "channels": ("fetch",),
    "label": "Wiley TDM",
},)
ARTICLE_URL = "https://api.wiley.com/onlinelibrary/tdm/v1/articles"
_DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)


def _normalized_doi(value: object) -> str | None:
    text = urllib.parse.unquote(str(value or "")).strip()
    text = re.sub(r"^https?://(?:www\.)?(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi:\s*", "", text, flags=re.I).strip()
    return text.lower() if _DOI_RE.fullmatch(text) else None


def api_token(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    token = str(env.get(ENV_TDM_API_TOKEN) or "").strip()
    if not token or "\r" in token or "\n" in token:
        return None
    return token


def _request_url(doi: str) -> str:
    return f"{ARTICLE_URL}/{urllib.parse.quote(doi, safe='')}"


def _supports_ref(ref: Mapping[str, object] | None) -> bool:
    if not isinstance(ref, Mapping):
        return False
    doi = _normalized_doi(ref.get("doi"))
    return bool(doi and matches_publisher(NAME, doi=doi, url=ref.get("url")))


def enabled(*, ref=None, environ=None, **kwargs) -> bool:
    return bool(api_token(environ) and _supports_ref(ref))


def disabled_reason(*, ref=None, environ=None, **kwargs) -> str | None:
    if not api_token(environ):
        return f"missing {ENV_TDM_API_TOKEN}"
    if not _supports_ref(ref):
        return "reference is not identified as Wiley content with an exact DOI"
    return None


def candidate_items(ref: dict, *, normalize_doi, environ=None, **kwargs) -> list[dict]:
    token = api_token(environ)
    doi = normalize_doi(ref.get("doi")) if isinstance(ref, dict) else None
    doi = _normalized_doi(doi)
    if not token or not doi or not matches_publisher(NAME, doi=doi, url=ref.get("url")):
        return []
    return [{"method": NAME, "url": _request_url(doi), "kind": "pdf"}]


def request_headers(candidate: Mapping[str, object], *, environ=None) -> dict[str, str]:
    """Return the Wiley token only for a canonical, routed Wiley API candidate."""
    if not isinstance(candidate, Mapping) or candidate.get("method") != NAME:
        return {}
    url = str(candidate.get("url") or "")
    parsed = urllib.parse.urlsplit(url)
    base = urllib.parse.urlsplit(ARTICLE_URL)
    if (
        parsed.scheme != base.scheme
        or parsed.hostname != base.hostname
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(base.path + "/")
    ):
        return {}
    encoded_doi = parsed.path[len(base.path) + 1:]
    doi = _normalized_doi(encoded_doi)
    if not doi or url != _request_url(doi) or not matches_publisher(NAME, doi=doi):
        return {}
    token = api_token(environ)
    return {"Wiley-TDM-Client-Token": token} if token else {}


def owns_request_url(candidate: Mapping[str, object]) -> bool:
    parsed = urllib.parse.urlsplit(str(candidate.get("url") or ""))
    endpoint = urllib.parse.urlsplit(ARTICLE_URL)
    return (
        parsed.scheme.lower() == endpoint.scheme
        and (parsed.hostname or "").lower() == endpoint.hostname
        and parsed.port in (None, 443)
        and (
            parsed.path == endpoint.path
            or parsed.path.startswith(endpoint.path + "/")
        )
    )
