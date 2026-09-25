# core/verify/claim_evidence/evidence/retrieval.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic BM25 selection of raw source-span ranges.

This module ranks only the immutable spans supplied by ``SourceSpanCatalog``.
It never rewrites text: returned ranges are half-open offsets in that catalog's
source and are suitable for ``build_effective_context(..., mode="extractive_rag")``.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import importlib.metadata
import re
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.verify.source_spans import SourceSpanCatalog


ALGORITHM_ID = "bm25s-0.3.10-callimachus-v1"
_TOKENIZER_ID = "unicode-word-casefold-v1"
_LIBRARY_ID = "bm25s-0.3.10"
_BM25S_VERSION = "0.3.10"
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)


class Bm25RetrievalError(ValueError):
    """Base class for deterministic extractive retrieval failures."""


class Bm25DependencyUnavailable(Bm25RetrievalError):
    """The pinned BM25 dependency is not importable."""


class Bm25RetrievalLimitError(Bm25RetrievalError):
    """No audit-safe, budget-fitting extractive context can be selected."""


@dataclass(frozen=True, slots=True)
class Bm25RetrievalResult:
    """Immutable selection plus the frozen, JSON-safe retrieval parameters."""

    raw_ranges: tuple[tuple[int, int], ...]
    algorithm: str
    config: tuple[tuple[str, str | int], ...]

    def snapshot(self) -> dict[str, object]:
        return {
            "raw_ranges": list(self.raw_ranges),
            "algorithm": self.algorithm,
            "config": dict(self.config),
        }


def require_bm25s() -> ModuleType:
    """Import the exact optional BM25 implementation required for this algorithm."""
    try:
        module = importlib.import_module("bm25s")
        version = importlib.metadata.version("bm25s")
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise Bm25DependencyUnavailable(
            "bm25s==0.3.10 is required; install it with pip install -r requirements-rag.txt"
        ) from exc
    if version != _BM25S_VERSION:
        raise Bm25DependencyUnavailable(
            "bm25s==0.3.10 is required; install it with pip install -r requirements-rag.txt"
        )
    return module


def _tokenize(text: str) -> tuple[str, ...]:
    """Tokenize with code-owned Unicode word semantics, independent of bm25s."""
    if not isinstance(text, str):
        return ()
    return tuple(match.group(0).casefold() for match in _TOKEN_RE.finditer(text))


def _score_bm25(
    documents: tuple[tuple[str, ...], ...], query_tokens: tuple[str, ...]
) -> tuple[float, ...]:
    """Score one tokenized query against tokenized documents via pinned bm25s."""
    bm25s = require_bm25s()

    retriever = bm25s.BM25()
    retriever.index([list(document) for document in documents], show_progress=False)
    indices, scores = retriever.retrieve(
        [list(query_tokens)], k=len(documents), show_progress=False
    )
    ranked_indices = indices[0]
    ranked_scores = scores[0]
    result = [0.0] * len(documents)
    for index, score in zip(ranked_indices, ranked_scores):
        result[int(index)] = float(score)
    return tuple(result)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Bm25RetrievalLimitError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Bm25RetrievalLimitError(f"{name} must be a non-negative integer")
    return value


def _merge_neighbor_ranges(
    spans: tuple[object, ...], selected_indices: set[int]
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for index in sorted(selected_indices):
        span = spans[index]
        raw_range = (span.raw_start, span.raw_end)
        if ranges and raw_range[0] == ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], raw_range[1])
        else:
            ranges.append(raw_range)
    return tuple(ranges)


def _joined_length(ranges: tuple[tuple[int, int], ...]) -> int:
    return sum(raw_end - raw_start for raw_start, raw_end in ranges) + 2 * max(
        0, len(ranges) - 1
    )


def select_bm25_ranges(
    catalog: SourceSpanCatalog,
    query: str,
    *,
    budget: int,
    top_k: int = 6,
    neighbor_window: int = 1,
) -> Bm25RetrievalResult:
    """Return budget-fitting, merged raw ranges selected from ``catalog``.

    Candidates are ranked by descending positive BM25 score, then raw source
    position.  The best ``top_k`` seed spans add their immediate catalog
    neighbours before contiguous selections are merged and packed in source
    order.  Failure to obtain a positive, fitting selection is explicit.
    """
    budget = _positive_int(budget, "budget")
    top_k = _positive_int(top_k, "top_k")
    neighbor_window = _nonnegative_int(neighbor_window, "neighbor_window")
    query_tokens = _tokenize(query)
    if not query_tokens:
        raise Bm25RetrievalLimitError("query has no informative Unicode tokens")

    spans = catalog.spans
    if not spans:
        raise Bm25RetrievalLimitError("source span catalog is empty")
    documents = tuple(_tokenize(span.text) for span in spans)
    scores = _score_bm25(documents, query_tokens)
    if len(scores) != len(spans):
        raise Bm25RetrievalError("BM25 scorer returned an invalid score count")
    ranked = sorted(
        (
            (float(score), index)
            for index, score in enumerate(scores)
            if float(score) > 0.0
        ),
        key=lambda item: (-item[0], spans[item[1]].raw_start),
    )
    if not ranked:
        raise Bm25RetrievalLimitError("BM25 retrieval produced no positive scores")

    selected_indices: set[int] = set()
    for _, index in ranked[:top_k]:
        with_neighbors = set(
            range(
                max(0, index - neighbor_window),
                min(len(spans), index + neighbor_window + 1),
            )
        )
        candidate = selected_indices | with_neighbors
        if _joined_length(_merge_neighbor_ranges(spans, candidate)) <= budget:
            selected_indices = candidate
            continue
        candidate = selected_indices | {index}
        if _joined_length(_merge_neighbor_ranges(spans, candidate)) <= budget:
            selected_indices = candidate
    ranges = _merge_neighbor_ranges(spans, selected_indices)
    if not ranges:
        raise Bm25RetrievalLimitError("BM25 retrieval cannot fit a selected range in budget")
    return Bm25RetrievalResult(
        raw_ranges=ranges,
        algorithm=ALGORITHM_ID,
        config=(
            ("library", _LIBRARY_ID),
            ("neighbor_window", neighbor_window),
            ("tokenizer", _TOKENIZER_ID),
            ("top_k", top_k),
        ),
    )
