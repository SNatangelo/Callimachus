# core/report/reason_codes.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Authoritative human-report assessment reason codes."""

from __future__ import annotations

from typing import Final


ASSESSMENT_REASON_CODES: Final = frozenset({
    "hard.claim.orphan_citation",
    "hard.claim.uncontested_contradiction",
    "hard.claim.all_off_topic",
    "review.claim.off_topic",
    "review.claim.related_only",
    "review.claim.non_decidable",
    "review.manuscript.low_reference_coverage",
    "technical.claim.no_semantic_outcome",
    "technical.claim.incomplete_pair_coverage",
    "technical.claim.contested_assurance",
    "technical.claim.ambiguous_citation",
    "review.claim.partial_support",
    "minor.claim.partial_support",
    "hard.source.not_found",
    "hard.source.identifier_mismatch",
    "hard.source.suspected_fabricated",
    "hard.source.suspected_fabricated_tag",
    "hard.source.reference_refuted",
    "hard.source.high_fabrication_suspicion",
    "hard.source.retracted",
    "review.source.title_flag_warn",
    "review.source.author_list_discrepancy",
    "review.source.searched_not_found",
    "review.source.elevated_bibliographic_suspicion",
})
