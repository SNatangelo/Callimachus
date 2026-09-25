#!/usr/bin/env python3
# core/app/phases/web_research.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Retired post-Verify third-party web-research phase."""

from __future__ import annotations

from urllib.parse import urlparse

from core.app.runtime.repository import _save_state


_RESEARCH_STANCES = {"supports", "contradicts"}


def _validate_research_answer(answer):
    """Validate legacy research task payloads without admitting their pages."""
    if not isinstance(answer, dict) or set(answer) != {"found", "findings"}:
        raise ValueError("research answer must be exactly {found, findings}")
    if type(answer["found"]) is not bool or not isinstance(answer["findings"], list):
        raise ValueError("research answer has invalid found/findings types")
    if answer["found"] is not bool(answer["findings"]):
        raise ValueError("research answer found must agree with findings")
    for finding in answer["findings"]:
        if not isinstance(finding, dict) or set(finding) != {"url", "stance", "quote"}:
            raise ValueError("research finding has invalid shape")
        url, stance, quote = finding["url"], finding["stance"], finding["quote"]
        if not isinstance(url, str) or not url.strip() or urlparse(url).scheme not in {"http", "https"} or not urlparse(url).netloc:
            raise ValueError("research finding URL must be nonempty http(s)")
        if stance not in _RESEARCH_STANCES or not isinstance(quote, str) or not quote.strip():
            raise ValueError("research finding stance or quote is invalid")
    return answer


def _has_tier(manifest, ref_id, tier):
    return any(
        entry.get("ref_id") == ref_id and entry.get("tier") == tier
        for entry in manifest.get("entries", [])
    )


def _verification_projection(run):
    """Compatibility seam retained for callers that inspect the retired phase."""
    return []


def _web_research_triggers(st):
    """Compatibility seam: generic web pages are no longer evidence candidates."""
    return {}


def _emit_web_verify_tasks(st):
    """Compatibility seam: generic third-party pages never create Verify tasks."""
    return None


def phase_web_research(st):
    """Finish the retired phase without collecting third-party page evidence."""
    st["research_done"] = True
    _save_state(st)
    return "report"
