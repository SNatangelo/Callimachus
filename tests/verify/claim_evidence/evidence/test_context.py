# tests/verify/claim_evidence/evidence/test_context.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from tests._bootstrap import *  # noqa: F401,F403

import pytest

from core.verify.claim_evidence.evidence.context import ContextError, build_effective_context, validate_effective_context


SOURCE = "First evidence sentence. Second evidence sentence. Third evidence sentence."


def test_full_text_is_exact_and_never_falls_back_when_over_budget():
    context = build_effective_context(SOURCE, mode="full_text", budget=len(SOURCE))
    assert context.text == SOURCE
    assert context.ranges == ()
    with pytest.raises(ContextError, match="exceeds"):
        build_effective_context(SOURCE, mode="full_text", budget=len(SOURCE) - 1)


def test_rag_snapshot_is_deterministic_and_has_verbatim_ordered_ranges():
    ranges = ((24, 49),)
    first = build_effective_context(SOURCE, mode="extractive_rag", budget=25, retrieved_ranges=ranges, retrieval_algorithm="fixture-rag-v1", retrieval_config={"window": 45, "version": "1"})
    second = build_effective_context(SOURCE, mode="extractive_rag", budget=25, retrieved_ranges=ranges, retrieval_algorithm="fixture-rag-v1", retrieval_config={"version": "1", "window": 45})
    assert first == second
    assert first.context_hash == second.context_hash
    assert first.retrieval_algorithm == "fixture-rag-v1"
    assert first.text == "\n\n".join(item.text for item in first.ranges)
    assert [(item.raw_start, item.raw_end) for item in first.ranges] == list(ranges)
    assert all(SOURCE[item.raw_start:item.raw_end] == item.text for item in first.ranges)
    assert first.snapshot()["retrieval_config"] == {"version": "1", "window": 45}


def test_context_rejects_tampered_source_and_range_provenance():
    context = build_effective_context(SOURCE, mode="extractive_rag", budget=30, retrieved_ranges=((0, 20),), retrieval_algorithm="fixture-v1")
    with pytest.raises(ContextError, match="source hash"):
        validate_effective_context(context, SOURCE + " changed")
    object.__setattr__(context.ranges[0], "text", "tampered")
    with pytest.raises(ContextError):
        validate_effective_context(context, SOURCE)


def test_context_rejects_overlapping_and_out_of_order_ranges():
    context = build_effective_context(SOURCE, mode="extractive_rag", budget=45, retrieved_ranges=((0, 22), (24, 45)), retrieval_algorithm="fixture-v1")
    object.__setattr__(context.ranges[0], "raw_start", 2)
    with pytest.raises(ContextError, match="tampered"):
        validate_effective_context(context, SOURCE)


def test_full_text_forbids_and_rag_requires_retrieval_metadata():
    with pytest.raises(ContextError, match="forbids"):
        build_effective_context(SOURCE, mode="full_text", budget=len(SOURCE), retrieval_algorithm="wrong")
    with pytest.raises(ContextError, match="requires"):
        build_effective_context(SOURCE, mode="extractive_rag", budget=20, retrieved_ranges=(), retrieval_algorithm="")
    with pytest.raises(ContextError, match="algorithm"):
        build_effective_context(
            SOURCE,
            mode="extractive_rag",
            budget=20,
            retrieved_ranges=((0, 20),),
            retrieval_algorithm=" padded ",
        )


def test_rag_budget_counts_separators_and_empty_persisted_ranges_fail():
    ranges = ((0, 5), (24, 29))
    with pytest.raises(ContextError, match="budget"):
        build_effective_context(
            SOURCE,
            mode="extractive_rag",
            budget=11,
            retrieved_ranges=ranges,
            retrieval_algorithm="fixture-v1",
        )
    context = build_effective_context(
        SOURCE,
        mode="extractive_rag",
        budget=12,
        retrieved_ranges=ranges,
        retrieval_algorithm="fixture-v1",
    )
    object.__setattr__(context, "ranges", ())
    with pytest.raises(ContextError, match="non-empty"):
        validate_effective_context(context, SOURCE)


def test_retrieval_configuration_keys_are_closed_and_unambiguous():
    for config in ({"": 1}, {" padded ": 1}, {1: "not-a-string-key"}):
        with pytest.raises(ContextError, match="configuration"):
            build_effective_context(
                SOURCE,
                mode="extractive_rag",
                budget=20,
                retrieved_ranges=((0, 20),),
                retrieval_algorithm="fixture-v1",
                retrieval_config=config,
            )
