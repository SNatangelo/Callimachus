# core/resolve/providers/issue_gwilr.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-neutral adapter for the GWILR official issue TOC."""
from __future__ import annotations

from .. import publisher_issue_attestation as _gwilr

ISSUE_ATTESTATION = {"provider": _gwilr.NAME, "rule_version": "issue-attestation/v1"}


def supports_issue_attestation(ref: dict) -> bool:
    return bool(ref.get("source_kind") in {"article_like", "article"})


def attest_issue(ref: dict) -> dict:
    raw = _gwilr.attest_issue(ref)
    return {
        "provider": ISSUE_ATTESTATION["provider"], "rule_version": ISSUE_ATTESTATION["rule_version"],
        "status": raw["status"], "reason": raw["reason"], "scope": raw["scope"],
        "target_status": raw["target_status"], "target_member_order": raw["target_member_order"],
        "cited_container": raw["cited_container"], "cited_volume": raw["cited_volume"],
        "cited_issue": raw["cited_issue"], "journal_title": raw["journal_title"],
        "completeness_basis": raw["attestation_basis"],
        "members": [{key: member.get(key) for key in ("record_id", "title", "first_author", "year", "journal", "volume", "issue", "locator", "pmid", "pmcid", "doi", "url")} for member in raw["members"]],
        "sources": [{"role": "issue_toc", "url": raw["issue_url"], "response_sha256": raw["response_sha256"]}],
        "observations": ([{"key": "publisher_host", "value_type": "text", "text_value": raw["publisher_host"], "integer_value": None}]
                         + ([{"key": "section_count", "value_type": "integer", "text_value": None, "integer_value": raw["section_count"]}]
                            if raw["section_count"] is not None else [])),
    }
