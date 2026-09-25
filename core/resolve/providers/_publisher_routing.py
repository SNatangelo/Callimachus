# core/resolve/providers/_publisher_routing.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic publisher routing from a versioned, data-only catalog."""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path


DEFAULT_CATALOG_PATH = Path(__file__).with_name("publisher_routes.json")
_DOI_PREFIX_RE = re.compile(r"10\.\d{4,9}")
_HOST_SUFFIX_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")


def _string_list(value: object, *, validator) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    values = tuple(str(item).strip().lower() for item in value)
    if any(not item or not validator(item) for item in values) or len(set(values)) != len(values):
        return None
    return values


def _valid_alias(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value))


def _valid_host_suffix(value: str) -> bool:
    return bool(_HOST_SUFFIX_RE.fullmatch(value))


def _validated_catalog(data: object) -> dict[str, dict[str, tuple[str, ...]]] | None:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return None
    publishers = data.get("publishers")
    if not isinstance(publishers, list) or not publishers:
        return None

    catalog: dict[str, dict[str, tuple[str, ...]]] = {}
    prefixes: set[str] = set()
    hosts: set[str] = set()
    for entry in publishers:
        if not isinstance(entry, dict) or set(entry) != {"id", "doi_prefixes", "host_suffixes"}:
            return None
        publisher_id = str(entry.get("id") or "").strip().lower()
        doi_prefixes = _string_list(entry.get("doi_prefixes"), validator=_DOI_PREFIX_RE.fullmatch)
        host_suffixes = _string_list(entry.get("host_suffixes"), validator=_valid_host_suffix)
        if (
            not _valid_alias(publisher_id)
            or doi_prefixes is None
            or host_suffixes is None
            or publisher_id in catalog
            or prefixes.intersection(doi_prefixes)
            or hosts.intersection(host_suffixes)
        ):
            return None
        catalog[publisher_id] = {
            "doi_prefixes": doi_prefixes,
            "host_suffixes": host_suffixes,
        }
        prefixes.update(doi_prefixes)
        hosts.update(host_suffixes)
    return catalog


def load_catalog(path: Path | None = None) -> dict[str, dict[str, tuple[str, ...]]] | None:
    """Return a fully validated catalog, or ``None`` when it is unsafe to use."""
    try:
        payload = json.loads((path or DEFAULT_CATALOG_PATH).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return _validated_catalog(payload)


def _host_matches(url: object, suffixes: tuple[str, ...]) -> bool:
    try:
        host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return False
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def matches_publisher(publisher_id: str, *, doi: object = None, url: object = None) -> bool:
    """Return true only for an unambiguous catalog match.

    Routing signals only choose a provider; callers must still validate the
    returned document identity independently.
    """
    catalog = load_catalog()
    publisher_key = str(publisher_id).strip().lower()
    publisher = catalog.get(publisher_key) if catalog else None
    if publisher is None:
        return False
    normalized_doi = str(doi or "").strip().lower()
    doi_matches = any(normalized_doi.startswith(f"{prefix}/") for prefix in publisher["doi_prefixes"])
    host_matches = _host_matches(url, publisher["host_suffixes"])
    if not doi_matches and not host_matches:
        return False
    matched_publishers = [
        key for key, candidate in catalog.items()
        if any(normalized_doi.startswith(f"{prefix}/") for prefix in candidate["doi_prefixes"])
        or _host_matches(url, candidate["host_suffixes"])
    ]
    return matched_publishers == [publisher_key]
