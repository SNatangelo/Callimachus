# core/resolve/providers/issue_pmc.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-neutral adapter for PMC's bounded issue holdings inventory."""
from __future__ import annotations

from .. import pmc_complete_issue as _pmc
from ._issue_common import coordinate

ISSUE_ATTESTATION = {"provider": _pmc.NAME, "rule_version": "issue-attestation/v1"}


def supports_issue_attestation(ref: dict) -> bool:
    # Keep the established article routing unchanged.  A parser can however
    # leave an otherwise journal-shaped reference as ``unknown``.  Permit that
    # narrow shape through to PMC, whose official-holdings check remains the
    # authoritative, fail-closed decision boundary.
    source_kind = ref.get("source_kind") or ref.get("source_type")
    if source_kind in {"article_like", "article"}:
        return True
    if source_kind != "unknown":
        return False
    if (
        ref.get("isbn")
        or ref.get("source_kind") == "book_like"
        or ref.get("source_type") == "book"
        or not str(ref.get("title") or "").strip()
        or not str(ref.get("year") or "").strip()
        or not coordinate(ref, "container")
        or not coordinate(ref, "volume")
        or not str(coordinate(ref, "issue") or "").isdigit()
    ):
        return False
    return True


def attest_issue(ref: dict) -> dict:
    raw = _pmc.complete_issue_inventory(ref)
    return {
        "provider": ISSUE_ATTESTATION["provider"], "rule_version": ISSUE_ATTESTATION["rule_version"],
        "status": raw["status"], "reason": raw["reason"], "scope": raw["scope"],
        "target_status": raw["target_status"], "target_member_order": raw["target_member_order"],
        "cited_container": raw["cited_container"], "cited_volume": raw["cited_volume"],
        "cited_issue": raw["cited_issue"], "journal_title": raw["journal_title"],
        "completeness_basis": raw["completeness_basis"],
        "members": [{key: member.get(key) for key in ("record_id", "title", "first_author", "year", "journal", "volume", "issue", "locator", "pmid", "pmcid", "doi", "url")} for member in raw["members"]],
        "sources": [{"role": "journal_list", "url": raw["journal_list_url"], "response_sha256": raw["journal_list_sha256"]}, {"role": "query", "url": raw["query_url"], "response_sha256": raw["query_sha256"]}],
        "observations": [{"key": key, "value_type": "text", "text_value": str(raw[key]), "integer_value": None} for key in ("nlm_unique_id", "issn", "agreement_status", "agreement_to_deposit") if raw.get(key) is not None] + ([{"key": "reported_count", "value_type": "integer", "text_value": None, "integer_value": raw["reported_count"]}] if raw.get("reported_count") is not None else []),
    }
