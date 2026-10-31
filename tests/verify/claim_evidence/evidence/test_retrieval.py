# tests/verify/claim_evidence/evidence/test_retrieval.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from core.verify.claim_evidence.evidence import retrieval
from core.verify.source_spans import build_catalog


SOURCE = "Alpha beta.\n\nNeedle result.\n\nGamma delta.\n\nNeedle appendix."


def test_select_bm25_ranges_merges_neighbours_packs_budget_and_records_config(
    monkeypatch,
):
    catalog = build_catalog(SOURCE)
    scores = tuple(2.0 if "Needle" in span.text else 0.1 for span in catalog.spans)
    monkeypatch.setattr(retrieval, "_score_bm25", lambda documents, query: scores)

    result = retrieval.select_bm25_ranges(catalog, "Needle", budget=len(SOURCE))

    assert result.algorithm == "bm25s-0.3.10-callimachus-v1"
    assert result.raw_ranges == ((0, len(SOURCE)),)
    assert result.config == (
        ("library", "bm25s-0.3.10"),
        ("neighbor_window", 1),
        ("tokenizer", "unicode-word-casefold-v1"),
        ("top_k", 6),
    )
    assert result.snapshot()["config"] == dict(result.config)


def test_select_bm25_ranges_is_deterministic_on_score_ties(monkeypatch):
    catalog = build_catalog(SOURCE)
    monkeypatch.setattr(
        retrieval, "_score_bm25", lambda documents, query: (1.0,) * len(documents)
    )

    result = retrieval.select_bm25_ranges(
        catalog, "needle", budget=13, top_k=1, neighbor_window=0
    )

    assert result.raw_ranges == ((0, 11),)


def test_select_bm25_ranges_admits_best_late_seed_before_earlier_weaker_span(monkeypatch):
    source = "Weak prior.\n\nBest final."
    catalog = build_catalog(source)
    monkeypatch.setattr(
        retrieval,
        "_score_bm25",
        lambda documents, query: tuple(
            2.0 if "Best" in span.text else 0.1 if "Weak" in span.text else 0.0
            for span in catalog.spans
        ),
    )

    result = retrieval.select_bm25_ranges(
        catalog,
        "needle",
        budget=len("Best final."),
        top_k=2,
        neighbor_window=0,
    )

    assert result.raw_ranges == ((len("Weak prior.\n\n"), len(source)),)


@pytest.mark.parametrize("query", ("", "  — …  "))
def test_select_bm25_ranges_fails_closed_for_noninformative_query(query):
    with pytest.raises(retrieval.Bm25RetrievalLimitError, match="informative"):
        retrieval.select_bm25_ranges(build_catalog(SOURCE), query, budget=20)


def test_select_bm25_ranges_fails_closed_without_positive_scores(monkeypatch):
    catalog = build_catalog(SOURCE)
    monkeypatch.setattr(
        retrieval, "_score_bm25", lambda documents, query: (0.0,) * len(documents)
    )

    with pytest.raises(retrieval.Bm25RetrievalLimitError, match="positive"):
        retrieval.select_bm25_ranges(catalog, "needle", budget=20)


def test_require_bm25s_rejects_absent_dependency(monkeypatch):
    def missing(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(retrieval.importlib, "import_module", missing)

    with pytest.raises(retrieval.Bm25DependencyUnavailable, match="requirements-rag.txt"):
        retrieval.require_bm25s()


def test_require_bm25s_rejects_wrong_dependency_version(monkeypatch):
    monkeypatch.setattr(retrieval.importlib, "import_module", lambda name: SimpleNamespace())
    monkeypatch.setattr(retrieval.importlib.metadata, "version", lambda name: "0.3.9")

    with pytest.raises(retrieval.Bm25DependencyUnavailable, match="0.3.10"):
        retrieval.require_bm25s()


def test_bm25_import_smoke():
    if importlib.util.find_spec("bm25s") is None:
        pytest.skip("bm25s is not installed in this environment")
    assert retrieval.require_bm25s().__name__ == "bm25s"
    result = retrieval._score_bm25((("needle",), ("other",)), ("needle",))
    assert len(result) == 2
