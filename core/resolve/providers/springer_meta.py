# core/resolve/providers/springer_meta.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Springer Nature Metadata API enrichment for already-known DOI records."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse

from ._publisher_routing import matches_publisher


NAME = "springer_meta"
CREDENTIAL_SPECS = ({
    "provider": "springer_metadata",
    "env_name": "SPRINGER_NATURE_META_API_KEY",
    "channels": ("resolve",),
    "label": "Springer Nature Meta",
},)
RESOLVE_NAME = NAME
MANIFEST = {"origin": "springer_nature"}
ENV_SPRINGER_NATURE_META_API_KEY = "SPRINGER_NATURE_META_API_KEY"
METADATA_URL = "https://api.springernature.com/meta/v2/json"


def owns_request_url(candidate) -> bool:
    parsed = urllib.parse.urlsplit(str(candidate.get("url") or ""))
    endpoint = urllib.parse.urlsplit(METADATA_URL)
    return (
        parsed.scheme.lower() == endpoint.scheme
        and (parsed.hostname or "").lower() == endpoint.hostname
        and parsed.port in (None, 443)
        and parsed.path == endpoint.path
    )


def api_key(environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    value = str(env.get(ENV_SPRINGER_NATURE_META_API_KEY) or "").strip()
    return value or None


def _request_url(doi: str, key: str) -> str:
    return METADATA_URL + "?" + urllib.parse.urlencode({
        "q": f"doi:{doi}", "p": "1", "s": "1", "api_key": key,
    })


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
        "status": "unresolved", "via": NAME,
        "reason": f"{error_type} (HTTP {status})",
        "error_type": error_type, "error_reason": f"{error_type} (HTTP {status})",
        "retryable": retryable, "http_status": status,
    }


def supports(ref: dict) -> bool:
    """Keep this enrichment-only provider out of ordinary resolver discovery."""
    return False


def discover(ref: dict) -> None:
    """Resolver-registry compatibility hook; deliberately performs no request."""
    return None


def enrich(ref: dict) -> dict | None:
    """Return abstract metadata only after exact DOI confirmation by Springer."""
    from core.resolve import service as resolve_mod

    key = api_key()
    doi = resolve_mod._normalize_doi_value(ref.get("doi"))
    if not key or not doi or not matches_publisher("springer_nature", doi=doi, url=ref.get("url")):
        return None
    try:
        ref_id = ref.get("id")
        if ref_id:
            from core.resolve import transport_telemetry

            with transport_telemetry.operation(
                provider=NAME,
                operation="doi_lookup",
                mode="scalar",
                ref_ids=[ref_id],
            ):
                _status, body = resolve_mod._get(_request_url(str(doi), key))
        else:
            _status, body = resolve_mod._get(_request_url(str(doi), key))
    except urllib.error.HTTPError as exc:
        return _failure(exc.code)
    except Exception as exc:
        return {
            "status": "unresolved", "via": NAME,
            "reason": f"network: {type(exc).__name__}",
            "error_type": "network",
            "error_reason": f"network: {type(exc).__name__}",
            "retryable": True,
        }
    try:
        records = json.loads(body).get("records")
    except (AttributeError, TypeError, ValueError):
        return {"status": "unresolved", "via": NAME, "reason": "malformed Springer Metadata response"}
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        return {"status": "unverified", "via": NAME, "reason": "no unique Springer Metadata DOI match"}
    record = records[0]
    response_doi = resolve_mod._normalize_doi_value(record.get("doi"))
    if not response_doi or str(response_doi).lower() != str(doi).lower():
        return {"status": "unverified", "via": NAME, "reason": "Springer Metadata DOI did not match request"}
    abstract = str(record.get("abstract") or "").strip() or None
    return {
        "status": "resolved", "via": NAME,
        "matched_title": str(record.get("title") or "").strip() or None,
        "abstract": abstract,
        "reason": None,
    }
