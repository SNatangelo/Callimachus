# core/app/runtime/resolve_failures.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Structured fail-closed payloads for resolver and repair exceptions."""

from __future__ import annotations

import traceback

from core.app.runtime.repository import _now


def _exception_payload(exc: Exception) -> dict:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }


def _resolve_exception_result(ref: dict, exc: Exception) -> dict:
    details = _exception_payload(exc)
    reason = f"resolver exception: {details['type']}: {details['message']}"
    return {
        "ref_id": ref["id"],
        "ref_number": ref.get("ref_number"),
        "status": "unresolved",
        "via": "resolver_exception",
        "matched_title": ref.get("title"),
        "abstract": None,
        "retracted": False,
        "fulltext_exists": "unknown",
        "oa_status": "unknown",
        "work_type": None,
        "title_overlap": None,
        "title_flag": None,
        "resolution_basis": "resolver_exception",
        "existence_confidence": "unknown",
        "reason": reason,
        "reference_status_tag": "unverified",
        "fabrication_risk": "low",
        "tag_reason": "resolver failed before producing a bibliographic verdict",
        "evidence_profile": {
            "resolution_basis": "resolver_exception",
            "exception": details,
            "has_identifier": bool(ref.get("doi") or ref.get("pmid") or ref.get("isbn") or ref.get("url")),
        },
        "attempts": [{
            "status": "unresolved",
            "via": "resolver_exception",
            "reason": reason,
            "exception": details,
        }],
        "checked_at": _now(),
    }


def _repair_exception_result(ref: dict, resolve_result: dict, exc: Exception, trigger: str) -> dict:
    """Build a structured resolve_result when inline repair raises.

    Mirrors _resolve_exception_result: the exception is persisted in both
    evidence_profile and attempts so it's visible in the DB without debug mode.
    """
    details = _exception_payload(exc)
    reason = f"repair exception: {details['type']}: {details['message']}"
    attempts = list(resolve_result.get("attempts") or [])
    attempts.append({
        "status": "repair_exception",
        "via": "repair_exception",
        "reason": reason,
        "exception": details,
    })
    evidence = dict(resolve_result.get("evidence_profile") or {})
    evidence["repair_exception"] = {
        "trigger": trigger,
        "exception": details,
    }
    return {
        "ref_id": ref["id"],
        "ref_number": ref.get("ref_number"),
        "status": resolve_result.get("status", "unresolved"),
        "via": "repair_exception",
        "matched_title": resolve_result.get("matched_title") or ref.get("title"),
        "abstract": resolve_result.get("abstract"),
        "abstract_via": resolve_result.get("abstract_via"),
        "retracted": bool(resolve_result.get("retracted")),
        "fulltext_exists": resolve_result.get("fulltext_exists", "unknown"),
        "oa_status": resolve_result.get("oa_status"),
        "work_type": resolve_result.get("work_type"),
        "resolution_basis": "repair_exception",
        "existence_confidence": resolve_result.get("existence_confidence", "unknown"),
        "reason": reason,
        "reference_status_tag": resolve_result.get("reference_status_tag", "unverified"),
        "fabrication_risk": resolve_result.get("fabrication_risk", "low"),
        "evidence_profile": evidence,
        "attempts": attempts,
        "checked_at": _now(),
        "resolved_identifier": resolve_result.get("resolved_identifier"),
    }


def _repair_failed_result(resolve_result: dict, fetched: dict, trigger: str) -> dict:
    """Augment *resolve_result* with a repair_failed attempt so the DB shows
    that repair was tried and why it didn't improve the result."""
    attempts = list(resolve_result.get("attempts") or [])
    attempts.append({
        "status": "repair_failed",
        "via": "repair_failed",
        "reason": (
            f"repair attempted via {fetched.get('method', 'unknown')} but "
            f"status was {fetched.get('status', 'unknown')}"
            + (f": {fetched['reason']}" if fetched.get("reason") else "")
        ),
    })
    evidence = dict(resolve_result.get("evidence_profile") or {})
    evidence["repair_failed"] = {
        "trigger": trigger,
        "fetch_status": fetched.get("status"),
        "fetch_method": fetched.get("method"),
        "fetch_reason": fetched.get("reason"),
    }
    payload = {
        **resolve_result,
        "attempts": attempts,
        "evidence_profile": evidence,
        "checked_at": _now(),
    }
    # The attempt stream changed, so the resolver's terminal attempt_count no
    # longer describes this overlay.  Do not persist a stale decision trace.
    payload.pop("trace", None)
    return payload
