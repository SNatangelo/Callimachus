#!/usr/bin/env python3
# core/app/pipeline.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Per-source serial pipeline helpers for resolve -> cache gate -> fetch."""

from __future__ import annotations

from dataclasses import dataclass

from core.fetch.storage import content_store
from core.resolve import sources


FETCH_SKIPPED_IDENTITY_STATES = {"fabricated"}
FETCH_SKIPPED_STATUSES = {"not_found", "identifier_mismatch"}


@dataclass(frozen=True)
class ProcessSourceDeps:
    resolve_ref: object
    fetch_fulltext: object
    mailto: str | None = None
    fetch_context: object | None = None
    allow_inline_fulltext: bool = True


@dataclass(frozen=True)
class ProcessSourceResult:
    resolution: dict
    fetched: dict | None
    cached_source: dict | None


def _resolution_identity(resolution: dict | None) -> dict:
    ident = (resolution or {}).get("identity") or {}
    return ident if isinstance(ident, dict) else {}


def _ref_with_confirmed_identity(ref: dict, resolution: dict) -> dict:
    effective = dict(ref)
    identity = _resolution_identity(resolution)
    scheme = str(identity.get("scheme") or "").strip().lower()
    value = str(identity.get("value") or "").strip()
    if not scheme or not value:
        return effective
    if scheme == "doi":
        effective["doi"] = value
    elif scheme == "pmid":
        effective["pmid"] = value
    elif scheme == "isbn":
        effective["isbn"] = value
    elif scheme == "url":
        effective["url"] = value
    elif scheme == "arxiv_id" and not effective.get("doi"):
        effective["doi"] = f"10.48550/arXiv.{value}"
    return effective


def _cache_tiers_for_resolution(resolution: dict) -> tuple[str, ...]:
    availability = resolution.get("fulltext_availability") or {}
    unavailable_for_work = (
        availability.get("status") == "not_available"
        and availability.get("scope") == "work"
    )
    # Some current resolver paths produce only the unscoped summary. Treat an
    # explicit negative as work-scoped unless scoped evidence says otherwise.
    unscoped_work_negative = resolution.get("fulltext_exists") is False and not availability
    if unavailable_for_work or unscoped_work_negative:
        return ("abstract",)
    return ("fulltext", "abstract")


def _should_fetch(ref: dict, resolution: dict) -> bool:
    status = str(resolution.get("status") or "")
    if status in FETCH_SKIPPED_STATUSES:
        return False
    identity_state = str(resolution.get("identity_state") or "")
    if identity_state in FETCH_SKIPPED_IDENTITY_STATES:
        return False
    identity = _resolution_identity(resolution)
    if (
        identity_state == "unverified"
        and identity.get("scheme") == "none"
        and not ref.get("url")
    ):
        return False
    return True


def _materialize_cached_source(run_dir: str, ref: dict, cached: dict) -> dict:
    with open(cached["stored_path"], encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    entry = sources.store_text(
        run_dir,
        ref,
        cached["tier"],
        cached["origin"],
        text,
        source_ref=cached.get("source_ref") or cached.get("stored_relpath"),
        mapping=cached.get("mapping") or "content_store_reuse",
        signal=cached.get("match_signal"),
        score=cached.get("match_score"),
        identity_status=cached.get("identity_status"),
        identity_note=cached.get("identity_note"),
        content_version=cached.get("content_version"),
        provenance_relation=cached.get("provenance_relation"),
        supplied_by=cached.get("supplied_by"),
        supplied_via=cached.get("supplied_via"),
        file_format=cached.get("file_format"),
        extraction_flags=cached.get("extraction_flags"),
        extraction_method=cached.get("extraction_method"),
    )
    result = {
        "status": "cached",
        "tier": cached.get("tier"),
        "origin": cached.get("origin"),
        "source_ref": cached.get("source_ref"),
        "stored_as": entry.get("stored_as"),
        "stored_path": cached.get("stored_path"),
        "stored_relpath": cached.get("stored_relpath"),
        "parsed_text_id": cached.get("parsed_text_id"),
        "match_signal": cached.get("match_signal"),
        "match_score": cached.get("match_score"),
        "identity_status": cached.get("identity_status"),
        "identity_note": cached.get("identity_note"),
        "provenance_relation": cached.get("provenance_relation"),
        "extraction_flags": cached.get("extraction_flags") or [],
        "extraction_method": cached.get("extraction_method"),
        "preparation_version": cached.get("preparation_version"),
        "preparation_flags": cached.get("preparation_flags") or [],
        "preparation_before_chars": cached.get("preparation_before_chars"),
        "preparation_after_chars": cached.get("preparation_after_chars"),
    }
    if "document_relation" in cached:
        result["document_relation"] = cached["document_relation"]
    return result


def _cache_gate(run_dir: str, ref: dict, resolution: dict) -> dict | None:
    effective_ref = _ref_with_confirmed_identity(ref, resolution)
    tiers = _cache_tiers_for_resolution(resolution)
    cached = content_store.find_reusable_parsed_text(
        run_dir,
        effective_ref,
        tiers=tiers,
        resolve_result=resolution,
    )
    if not cached:
        return None
    # Fulltext is not possible (e.g. conference abstract with fulltext_exists=False)
    # — accept whatever tier is already cached.
    if "fulltext" not in tiers:
        return _materialize_cached_source(run_dir, ref, cached)
    # Fulltext is possible but the cached result is only an abstract (or lower).
    # Bypass the cache so the pipeline re-fetches: conditions may have changed
    # since the last run (OCR installed, new OA copies available, etc.).
    if cached.get("tier") != "fulltext":
        return None
    return _materialize_cached_source(run_dir, ref, cached)


def process_source(ref: dict, run_dir: str, *, deps: ProcessSourceDeps) -> ProcessSourceResult:
    resolution = deps.resolve_ref(ref)
    if not _should_fetch(ref, resolution):
        return ProcessSourceResult(resolution=resolution, fetched=None, cached_source=None)
    if not isinstance(resolution.get("evidence_payload"), dict):
        return ProcessSourceResult(resolution=resolution, fetched=None, cached_source=None)
    if not deps.allow_inline_fulltext:
        return ProcessSourceResult(resolution=resolution, fetched=None, cached_source=None)

    cached = _cache_gate(run_dir, ref, resolution)
    if cached is not None:
        return ProcessSourceResult(resolution=resolution, fetched=None, cached_source=cached)

    fetched = deps.fetch_fulltext(
        ref,
        run_dir,
        resolution,
        mailto=deps.mailto,
        fetch_context=deps.fetch_context,
        evidence_payload=resolution.get("evidence_payload"),
    )
    return ProcessSourceResult(resolution=resolution, fetched=fetched, cached_source=None)
