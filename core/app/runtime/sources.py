# core/app/runtime/sources.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Source materialization and verification-scope helpers."""

from __future__ import annotations

import os

from core.fetch import service as _fetch
from core.app.runtime.repository import (
    _load_parse_payload,
    _load_resolve_map,
    _load_run_refs,
    _repo_open,
    _run_ref_by_id,
)
from core.fetch.extraction.pdf import extract_with_quality
from core.parse.extract import extract_text
from core.resolve import sources as _sources

TIER_TO_SCOPE = {
    "fulltext": "fulltext_complete",
}
ABSTRACT_ONLY_SCOPE = "abstract_only"
ABSTRACT_FALLBACK_SCOPE = "abstract_fallback"

# ``abstract_only`` means full text genuinely does not exist; an
# ``abstract_fallback`` remains provisional because fuller text may exist.
ABSTRACT_SCOPES = {ABSTRACT_ONLY_SCOPE, ABSTRACT_FALLBACK_SCOPE}


def _ref_has_source_tier(run_dir: str, ref_id: str, tier: str) -> bool:
    manifest = _load_manifest_payload(run_dir)
    return any(
        entry.get("ref_id") == ref_id and entry.get("tier") == tier
        for entry in manifest.get("entries", [])
    )


def _ref_has_materialized_source(run_dir: str, ref_id: str) -> bool:
    manifest = _load_manifest_payload(run_dir)
    return any(
        entry.get("ref_id") == ref_id
        and (
            entry.get("tier") in ("fulltext", "abstract")
            or (
                entry.get("tier") == "web"
                and entry.get("origin") == "googlebooks"
            )
        )
        for entry in manifest.get("entries", [])
    )


def _load_manifest_payload(run_dir):
    repo = _repo_open(run_dir)
    if repo is not None:
        try:
            return repo.source_manifest_payload()
        finally:
            repo.close()
    return _sources.load_manifest(run_dir)


def _load_unreadable_payload(run_dir):
    repo = _repo_open(run_dir)
    if repo is not None:
        try:
            return repo.unreadable_payload()
        finally:
            repo.close()
    return _sources.load_unreadable(run_dir)


def _resolve_abstract_origin(resolve_result: dict) -> str:
    via = str(resolve_result.get("abstract_via") or resolve_result.get("via") or "").lower()
    provider_origin = _fetch._provider_registry.provider_name_for_via(via)
    if provider_origin:
        return provider_origin
    if "pubmed" in via:
        return "pubmed"
    if "openlibrary" in via:
        return "openlibrary"
    if "googlebooks" in via:
        return "googlebooks"
    return "webfetch"


def _resolve_abstract_source_ref(resolve_result: dict) -> str:
    origin = _resolve_abstract_origin(resolve_result)
    via = resolve_result.get("abstract_via") or resolve_result.get("via") or origin
    return resolve_result.get("url") or f"resolve:{via}"


def _fetch_evidence_payload(resolve_result: dict | None) -> dict | None:
    if not isinstance(resolve_result, dict):
        return None
    return {
        "fulltext_links": list(resolve_result.get("fulltext_links") or []),
        "auxiliary_fulltext_links": list(resolve_result.get("auxiliary_fulltext_links") or []),
        "oa_status": resolve_result.get("oa_status"),
        "fulltext_exists": resolve_result.get("fulltext_exists", "unknown"),
        "abstract": resolve_result.get("abstract"),
        "matched_title": resolve_result.get("matched_title"),
        "work_type": resolve_result.get("work_type"),
        "via": resolve_result.get("via"),
        "fulltext_availability": resolve_result.get("fulltext_availability"),
    }


def _repair_abstract_payload(ref: dict, resolve_result: dict, *, trigger: str) -> tuple[str | None, str | None, dict]:
    abstract = resolve_result.get("abstract")
    abstract_via = resolve_result.get("abstract_via")
    if not abstract:
        return None, None, {"action": "absent"}
    sig, score = _sources.corroborate(ref, abstract, resolve_result)
    via_text = str(abstract_via or resolve_result.get("via") or "").lower()
    weak_abstract_via = (
        _fetch._provider_registry.is_weak_abstract_origin(via_text)
        or "webfetch" in via_text
    )
    weak_resolution = (
        trigger in ("status=unresolved", "via=crossref_metadata")
        or resolve_result.get("resolution_basis") == "metadata_search"
        or resolve_result.get("reference_status_tag") == "weak_metadata_match"
    )
    if weak_abstract_via and weak_resolution and sig not in ("doi", "pmid") and score < _sources.CORROBORATE_THRESHOLD:
        return None, None, {
            "action": "suppressed",
            "reason": "weak metadata abstract did not corroborate the fetch-repaired canonical source",
            "corroborate_signal": sig,
            "corroborate_score": score,
            "source_ref": _resolve_abstract_source_ref(resolve_result),
            "origin": _resolve_abstract_origin(resolve_result),
        }
    return abstract, abstract_via, {
        "action": "kept",
        "corroborate_signal": sig,
        "corroborate_score": score,
        "source_ref": _resolve_abstract_source_ref(resolve_result),
        "origin": _resolve_abstract_origin(resolve_result),
    }


def _suppress_repair_abstract_entries(run_dir: str, ref_id: str, disposition: dict) -> list[dict]:
    if disposition.get("action") != "suppressed":
        return []
    removed = _sources.delete_text(
        run_dir,
        ref_id,
        tier="abstract",
        origin=disposition.get("origin"),
        source_ref=disposition.get("source_ref"),
    )
    return removed


def _materialize_resolve_abstracts(run_dir):
    parse = _load_parse_payload(run_dir)
    refs = {r["id"]: r for r in parse.get("references", [])}
    resolve_map = _load_resolve_map(run_dir)
    manifest = _load_manifest_payload(run_dir)
    tiers_by_ref = {}
    for entry in manifest.get("entries", []):
        tiers_by_ref.setdefault(entry.get("ref_id"), set()).add(entry.get("tier"))
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        fetch_attempts_by_ref = {
            ref_id: repo.list_fetch_attempts(ref_id)
            for ref_id, result in resolve_map.items()
            if result.get("abstract")
        }
    finally:
        repo.close()
    stored = []
    for ref_id, ex in resolve_map.items():
        if not ex.get("abstract"):
            continue
        ref = refs.get(ref_id)
        if not ref:
            continue
        if _sources.weak_metadata_abstract_invalidated(
            ref, ex, fetch_attempts_by_ref.get(ref_id, [])
        ):
            _sources.delete_text(
                run_dir,
                ref_id,
                tier="abstract",
                origin=_resolve_abstract_origin(ex),
                source_ref=_resolve_abstract_source_ref(ex),
            )
            continue
        if "fulltext" in tiers_by_ref.get(ref_id, set()) or "abstract" in tiers_by_ref.get(ref_id, set()):
            continue
        text = ex.get("abstract")
        sig, score = _sources.corroborate(ref, text)
        origin = _resolve_abstract_origin(ex)
        stored.append(_sources.store_text(
            run_dir,
            ref,
            "abstract",
            origin,
            text,
            source_ref=_resolve_abstract_source_ref(ex),
            mapping="auto" if sig in ("doi", "pmid") else "tokens",
            signal=sig,
            score=score,
        ))
    return stored


def _store_resolve_abstract_if_needed(
    run_dir: str,
    ref: dict,
    resolve_result: dict,
) -> tuple[bool, str | None, str | None, float | None]:
    if not resolve_result.get("abstract"):
        return False, None, None, None
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        fetch_attempts = repo.list_fetch_attempts(ref["id"])
    finally:
        repo.close()
    if _sources.weak_metadata_abstract_invalidated(
        ref, resolve_result, fetch_attempts
    ):
        return False, None, None, None
    if _ref_has_materialized_source(run_dir, ref["id"]):
        return False, None, None, None
    origin = _resolve_abstract_origin(resolve_result)
    sig, score = _sources.corroborate(ref, resolve_result["abstract"])
    _sources.store_text(
        run_dir,
        ref,
        "abstract",
        origin,
        resolve_result["abstract"],
        source_ref=_resolve_abstract_source_ref(resolve_result),
        mapping="auto" if sig in ("doi", "pmid") else "tokens",
        signal=sig,
        score=score,
    )
    return True, origin, sig, score


def _provide_identity_for_origin(
    origin: str,
    *,
    traceable_url: bool,
    forced: bool,
) -> tuple[str | None, str | None]:
    if origin == "browser_session":
        return (
            "browser_session_cleared",
            ("retrieved in a visible browser session after the user cleared a "
             "publisher security challenge; the script recorded the resulting text"),
        )
    if forced:
        return (
            "externally_corroborated_text",
            ("mapped from a traceable supplied file and usable for claim "
             "verification, but separate from bibliographic resolve"),
        )
    return None, None


def _register_run_source_text(
    run_dir,
    *,
    ref_id,
    tier,
    origin,
    file_path=None,
    text=None,
    url=None,
    force=False,
    mode="record",
    supplied_by=None,
    supplied_via=None,
):
    ref = _run_ref_by_id(run_dir, ref_id)
    extraction_flags = None
    extraction_method = None
    if file_path:
        file_format = os.path.splitext(file_path)[1].lower().lstrip(".")
        if file_format == "pdf":
            payload_text, extraction_method, extraction_flags = extract_with_quality(file_path)
        else:
            payload_text, file_format, _meta = extract_text(file_path)
        source_ref = url or os.path.abspath(file_path)
    else:
        payload_text = text or ""
        file_format = "txt"
        source_ref = url
    signal, score = _sources.corroborate(ref, payload_text)
    if mode == "map":
        corroborated = signal in ("doi", "pmid") or score >= _sources.CORROBORATE_THRESHOLD
        if not corroborated and not force:
            raise ValueError(
                f"not_corroborated: signal={signal!r} score={score!r} for {ref_id}"
            )
        mapping = "deterministic" if signal in ("doi", "pmid") else (
            "manual" if force else "model_corroborated"
        )
        identity_status, identity_note = _provide_identity_for_origin(
            origin,
            traceable_url=bool(url),
            forced=force,
        )
        return _sources.store_text(
            run_dir,
            ref,
            tier,
            origin,
            payload_text,
            source_ref=source_ref,
            mapping=mapping,
            signal=signal,
            score=score,
            identity_status=identity_status,
            identity_note=identity_note,
            supplied_by=(
                supplied_by
                if supplied_by is not None
                else ("user" if origin in ("user", "ocr") else "script")
            ),
            supplied_via=(
                supplied_via
                if supplied_via is not None
                else ("user_ocr" if origin == "ocr" else f"{origin}_{file_format}")
            ),
            file_format=file_format,
            extraction_flags=extraction_flags,
            extraction_method=extraction_method,
        )
    identity_status, identity_note = _provide_identity_for_origin(
        origin,
        traceable_url=bool(url),
        forced=False,
    )
    return _sources.store_text(
        run_dir,
        ref,
        tier,
        origin,
        payload_text,
        source_ref=source_ref,
        mapping="web",
        signal=signal,
        score=score,
        identity_status=identity_status,
        identity_note=identity_note,
        supplied_by=supplied_by if supplied_by is not None else "script",
        supplied_via=(
            supplied_via if supplied_via is not None else f"{origin}_{tier}"
        ),
        file_format=file_format,
        extraction_flags=extraction_flags,
        extraction_method=extraction_method,
    )


def _resolve_map(run_dir):
    return _load_resolve_map(run_dir)


def _scope_for_source(entry: dict, resolve_map: dict[str, dict]) -> str | None:
    """Map a stored source entry's tier to a verdict scope.

    An ``abstract`` tier splits into ``abstract_only`` (the source genuinely has
    no full text, so the abstract is final) versus ``abstract_fallback`` (only an
    abstract was reachable but fuller text may still exist), based on the
    resolver's ``fulltext_exists`` signal. Attributed Google Books text maps to
    ``preview_snippet``; generic web tiers are not admitted and return ``None``.
    """
    tier = entry.get("tier")
    if tier == "abstract":
        ref_id = entry.get("ref_id")
        fulltext_exists = (resolve_map.get(ref_id) or {}).get("fulltext_exists")
        return ABSTRACT_ONLY_SCOPE if fulltext_exists is False else ABSTRACT_FALLBACK_SCOPE
    if tier == "web" and entry.get("origin") == "googlebooks":
        return "preview_snippet"
    return TIER_TO_SCOPE.get(tier)
