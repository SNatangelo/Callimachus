#!/usr/bin/env python3
# core/resolve/policy.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared policy helpers for modular resolver orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable

try:
    from . import provider_config
    from . import sources as _sources
except ImportError:  # direct execution
    from resolve import provider_config
    from resolve import sources as _sources


def _normalize_content_version(value: str | None) -> str | None:
    normalize = getattr(_sources, "normalize_content_version", None)
    if callable(normalize):
        return normalize(value)
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return raw or None


def _host_is_preprint(url: str | None) -> bool:
    checker = getattr(_sources, "host_is_preprint", None)
    if callable(checker):
        return checker(url)
    markers = provider_config.preprint_markers().get("host_suffixes") or ()
    url_text = str(url or "").lower()
    return any(marker in url_text for marker in markers)


def _ref_is_preprint(ref: dict, result: dict | None = None) -> bool:
    checker = getattr(_sources, "ref_is_preprint", None)
    if callable(checker):
        return checker(ref, result)
    return False


def _acl_doi_prefixes() -> tuple[str, ...]:
    return provider_config.acl_doi_prefixes()


def fulltext_link_is_non_record(
    link: dict | None,
    *,
    normalize_doi: Callable[[str | None], str],
    normalize_content_version: Callable[[str | None], str | None] | None = None,
    host_is_preprint: Callable[[str | None], bool] | None = None,
    ref_is_preprint: Callable[[dict, dict | None], bool] | None = None,
    non_record_versions: tuple[str, ...] | list[str] = (),
) -> bool:
    if not isinstance(link, dict):
        return False
    url = str(link.get("url") or "").strip()
    if not url:
        return False
    version = (
        normalize_content_version(link.get("content_version"))
        if callable(normalize_content_version)
        else _normalize_content_version(link.get("content_version"))
    )
    if version in tuple(non_record_versions or getattr(_sources, "NON_RECORD_VERSIONS", ())):
        return True
    if callable(host_is_preprint):
        if host_is_preprint(url):
            return True
    elif _host_is_preprint(url):
        return True
    doi = normalize_doi(url)
    if not doi:
        return False
    if callable(ref_is_preprint):
        return bool(ref_is_preprint({"doi": doi}, None))
    return bool(_ref_is_preprint({"doi": doi}))


def result_only_non_record_fulltext(
    result: dict | None,
    *,
    normalize_doi: Callable[[str | None], str],
    normalize_content_version: Callable[[str | None], str | None] | None = None,
    host_is_preprint: Callable[[str | None], bool] | None = None,
    ref_is_preprint: Callable[[dict, dict | None], bool] | None = None,
    non_record_versions: tuple[str, ...] | list[str] = (),
) -> bool:
    if not result:
        return False
    links = [
        link for link in (result.get("fulltext_links") or [])
        if isinstance(link, dict) and link.get("url")
    ]
    if not links:
        return False
    return all(
        fulltext_link_is_non_record(
            link,
            normalize_doi=normalize_doi,
            normalize_content_version=normalize_content_version,
            host_is_preprint=host_is_preprint,
            ref_is_preprint=ref_is_preprint,
            non_record_versions=non_record_versions,
        )
        for link in links
    )


def optional_stage_reason(
    ref: dict,
    result: dict | None,
    *,
    article_title_candidate: Callable[[dict], str],
    result_title_overlap: Callable[[dict | None], float],
    result_has_fulltext: Callable[[dict | None], bool],
    result_doi: Callable[[dict | None], str],
    normalize_doi: Callable[[str | None], str],
    normalize_content_version: Callable[[str | None], str | None] | None = None,
    host_is_preprint: Callable[[str | None], bool] | None = None,
    ref_is_preprint: Callable[[dict, dict | None], bool] | None = None,
    article_like_resolution_candidate: Callable[[dict], bool] | None = None,
    non_record_versions: tuple[str, ...] | list[str] = (),
) -> bool:
    eligible = (
        article_like_resolution_candidate(ref)
        if callable(article_like_resolution_candidate)
        else ref.get("source_type") != "book" and bool(article_title_candidate(ref))
    )
    if ref.get("url") or not eligible:
        return None
    if result is None:
        return "missing"

    status = result.get("status")
    confidence = result.get("existence_confidence")
    if status == "unverified" and confidence == "low":
        return "weak_metadata"
    if (
        status == "resolved"
        and result_only_non_record_fulltext(
            result,
            normalize_doi=normalize_doi,
            normalize_content_version=normalize_content_version,
            host_is_preprint=host_is_preprint,
            ref_is_preprint=ref_is_preprint,
            non_record_versions=non_record_versions,
        )
        and not (ref_is_preprint(ref, result) if callable(ref_is_preprint) else _ref_is_preprint(ref, result))
    ):
        return "non_record_only"
    if status == "resolved" and isinstance(result.get("resolved_identifier"), dict):
        # A registry-validated identifier freezes bibliographic identity even
        # when it was discovered by metadata search.  Provenance remains
        # ``metadata_search``; optional title resolvers are not a second identity vote.
        return None
    if (
        status == "resolved"
        and result.get("resolution_basis") == "metadata_search"
        and confidence == "medium"
    ):
        if result_title_overlap(result) < 0.85:
            return "weak_metadata"
        if (
            not result_has_fulltext(result)
            and not any(result_doi(result).lower().startswith(prefix) for prefix in _acl_doi_prefixes())
        ):
            return "weak_metadata"
        if result_only_non_record_fulltext(
            result,
            normalize_doi=normalize_doi,
            normalize_content_version=normalize_content_version,
            host_is_preprint=host_is_preprint,
            ref_is_preprint=ref_is_preprint,
            non_record_versions=non_record_versions,
        ):
            return "non_record_only"
    return None


def optional_resolver_modules(
    attempts: list[dict],
    *,
    load_order: Callable[[], list[str]],
    registry: Callable[[], dict[str, object]],
    reason: str | None = None,
) -> list[object]:
    available = registry()
    tried_vias = {
        str(item.get("via") or "").strip()
        for item in (attempts or [])
        if isinstance(item, dict) and item.get("via")
    }
    modules = []
    for name in load_order():
        module = available.get(name)
        if module is None or not getattr(module, "OPTIONAL_STAGE", False):
            continue
        if reason == "non_record_only" and not getattr(module, "RECOVER_NON_RECORD", False):
            continue
        if name in tried_vias:
            continue
        modules.append(module)
    return modules


def local_accelerator_modules(
    attempts: list[dict],
    *,
    load_order: Callable[[], list[str]],
    registry: Callable[[], dict[str, object]],
) -> list[object]:
    """Configured local accelerators, once each and outside optional fallback."""
    available = registry()
    tried_vias = {
        str(item.get("via") or "").strip()
        for item in (attempts or [])
        if isinstance(item, dict) and item.get("via")
    }
    modules = []
    for name in load_order():
        module = available.get(name)
        if module is None or not getattr(module, "LOCAL_ACCELERATOR", False):
            continue
        if name in tried_vias or str(getattr(module, "NAME", "")) in tried_vias:
            continue
        modules.append(module)
    return modules


def canonical_companion_modules(
    attempts: list[dict],
    *,
    load_order: Callable[[], list[str]],
    registry: Callable[[], dict[str, object]],
) -> list[object]:
    """Canonical repositories remain discoverable after identity is frozen."""
    available = registry()
    tried = {
        str(item.get("via") or "").strip()
        for item in attempts or [] if isinstance(item, dict)
    }
    out = []
    for name in load_order():
        module = available.get(name)
        if module is None or not getattr(module, "CANONICAL_COMPANION", False):
            continue
        if name in tried or str(getattr(module, "NAME", "")) in tried:
            continue
        out.append(module)
    return out


def result_title_faithful(
    ref: dict,
    candidate: dict | None,
    *,
    article_title_candidate: Callable[[dict], str],
    title_key: Callable[[str | None], str],
    title_key_contains: Callable[[str | None, str | None], bool],
) -> bool:
    if not candidate:
        return False
    cited_title = article_title_candidate(ref)
    matched_title = candidate.get("matched_title")
    metadata_match = candidate.get("metadata_match") or {}
    if metadata_match.get("ordinal_conflict"):
        return False
    if cited_title and title_key(cited_title) == title_key(matched_title):
        return True
    if cited_title and title_key_contains(matched_title, cited_title):
        return metadata_match.get("author_match") is True
    overlap = metadata_match.get("title_overlap")
    return bool(
        isinstance(overlap, (int, float))
        and overlap >= 0.60
        and metadata_match.get("author_match") is True
    )


def capture_same_work_companion_links(
    ref: dict,
    current: dict | None,
    candidate: dict | None,
    *,
    article_title_candidate: Callable[[dict], str],
    title_key: Callable[[str | None], str],
    title_key_contains: Callable[[str | None, str | None], bool],
    metadata_has_canonical_host: Callable[[dict | None], bool],
    capture_auxiliary_fulltext_links: Callable[[dict, str, Iterable[dict]], None],
) -> dict | None:
    if not current or not candidate:
        return current
    if current.get("status") != "resolved" or candidate.get("status") != "resolved":
        return current
    if not candidate.get("fulltext_links"):
        return current
    metadata_match = candidate.get("metadata_match") or {}
    if not result_title_faithful(
        ref,
        candidate,
        article_title_candidate=article_title_candidate,
        title_key=title_key,
        title_key_contains=title_key_contains,
    ):
        return current
    if not (
        metadata_match.get("author_match") is True
        or (metadata_match.get("venue_overlap") or 0.0) >= 0.50
        or metadata_has_canonical_host(candidate)
    ):
        return current
    enriched = dict(current)
    capture_auxiliary_fulltext_links(
        enriched,
        via=str(candidate.get("via") or "resolver"),
        links=candidate.get("fulltext_links") or [],
        candidate=candidate,
    )
    return enriched
