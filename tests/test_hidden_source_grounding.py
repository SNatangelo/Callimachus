# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest

from core.verify.claim_evidence.domain.types import Jury1Decision
from core.verify.claim_evidence.evidence.context import build_effective_context
from core.verify.claim_evidence.evidence.grounding import (
    GroundingError,
    ground_jury1_decision,
)


def test_rag_cannot_ground_hidden_source_evidence():
    source = "Visible retrieved sentence. Hidden evidence follows."
    context = build_effective_context(
        source,
        mode="extractive_rag",
        budget=20,
        retrieved_ranges=((0, 20),),
        retrieval_algorithm="fixture-v1",
    )
    decision = Jury1Decision(
        "supports",
        ("Hidden evidence follows.",),
        "The hidden sentence appears to support the claim.",
        "the source-backed role",
        None,
        None,
    )

    with pytest.raises(GroundingError, match="outside"):
        ground_jury1_decision(decision, source, context)
