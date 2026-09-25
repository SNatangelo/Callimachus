# core/fetch/extraction/document_relation.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic compatibility between a citation and retrieved document body."""

from __future__ import annotations

try:
    from core.fetch.extraction.pdf import fulltext_material_issue
except ImportError:  # pragma: no cover - direct execution
    from pdf import fulltext_material_issue


_INCOMPATIBLE_REASONS = {
    "repository_cover_sheet_only": (
        "retrieved text is only a repository cover sheet, without the cited body"
    ),
    "book_review_not_cited_work": (
        "retrieved text is a book-review section, not the cited work itself"
    ),
    "access_verification_shell_not_cited_work": (
        "retrieved text is an access-verification shell, not the cited work itself"
    ),
    "paginated_viewer_first_page_only": (
        "retrieved text is only the first page of a paginated viewer, not the cited work"
    ),
    "supplementary_information_not_cited_work": (
        "retrieved text is supplementary information, not the cited work itself"
    ),
}


def document_relation_probe(ref: dict, text: str) -> dict:
    """Classify whether text is positively incompatible with its citation.

    This is deliberately a fail-closed *negative* probe: it declares only the
    the established structural contradictions.  Absence of those signals does
    not establish that the document is the cited primary work.
    """
    issue = fulltext_material_issue(text, ref)
    if issue in _INCOMPATIBLE_REASONS:
        return {
            "decision": "incompatible",
            "reason_code": issue,
            "reason": _INCOMPATIBLE_REASONS[issue],
        }
    return {
        "decision": "inconclusive",
        "reason_code": "document_relation_inconclusive",
        "reason": "document relation could not be determined from closed structural signals",
    }
