# tests/_resolve_fixtures.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Builders for resolver fakes that satisfy the current typed contract."""

from __future__ import annotations


def current_resolution(ref: dict, result: dict) -> dict:
    from core.resolve.service import _reference_evidence_profile, _resolution_provenance
    from core.resolve.decision import _build_resolution_decision

    current = dict(result)
    attempts = list(current.get("attempts") or [])
    provenance = _resolution_provenance(ref, current)
    current.update(provenance)
    current["evidence_profile"] = _reference_evidence_profile(
        ref,
        current,
        current["status"],
        attempts,
        None,
        provenance,
    )
    decision = _build_resolution_decision(
        ref,
        current,
        attempts,
        status=current["status"],
        via=current.get("via"),
        reference_status_tag=current.get("reference_status_tag"),
        fabrication_risk=current.get("fabrication_risk"),
        resolution_basis=provenance["resolution_basis"],
        meta=current,
    )
    current.update({
        "identity": decision.identity.to_dict(),
        "identity_state": decision.identity_state,
        "evidence_payload": decision.evidence_payload,
        "retraction": decision.retraction,
        "trace": decision.trace,
    })
    return current
