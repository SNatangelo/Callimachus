#!/usr/bin/env python3
# core/verify/outcomes.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
outcomes.py - canonical outcome taxonomy for the verifier/composer judge (Contract A).

Single source of truth for the persisted outcome labels accepted by
verification and reporting, so those boundaries do not each maintain their own
copy that can drift.

Taxonomy (graded relevance):
    supports   - the source substantiates the whole claim
    partial    - the source substantiates PART of the claim
    related    - on-topic but does NOT substantiate this claim
    off_topic  - extraneous, about a different subject altogether
    contradicts - the source argues against the claim

``uncertain`` is a second-stage ("judge 2") terminal outcome — it carries no
relevance grade and is never produced by the first-stage judge itself.
"""
from __future__ import annotations

VALID_OUTCOMES = frozenset({"supports", "partial", "related", "off_topic", "contradicts"})

CLAIM_EVIDENCE_OUTCOMES = VALID_OUTCOMES | frozenset({"non_decidable"})

# Second-stage ("judge 2") terminal outcome. Never produced by the verifier/composer
# judge itself — set upstream after a guard-accept when isolated evidence alone
# cannot confirm the claim. Kept here so every module names it the same way.
UNCERTAIN_OUTCOME = "uncertain"


def is_valid_outcome(outcome) -> bool:
    """Return whether *outcome* is in the current canonical taxonomy."""
    return outcome in VALID_OUTCOMES
