# core/verify/claim_evidence/application/task.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic task preparation at the application boundary."""
from typing import Any

from core.verify.source_spans import build_catalog

from ..context_config import ContextSettings
from ..evidence.context import build_effective_context
from ..evidence.retrieval import select_bm25_ranges


class RuntimeError(ValueError):
    """Runtime state or task input cannot be executed safely."""


def prepare_task(
    claim: dict[str, Any],
    source_text: str,
    *,
    context_settings: ContextSettings | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Freeze exact claim input and its provenance-bearing source context."""
    settings = context_settings or ContextSettings("auto", "large", None)
    claim_text = _nonempty_text(claim.get("sentence"), "claim sentence")
    claim_context = _nonempty_text(
        claim.get("context_window"),
        "claim context_window",
    )
    citation_marker = _nonempty_text(
        claim.get("marker_raw"),
        "claim marker_raw",
    )
    budget = settings.max_source_chars or len(source_text)
    mode = settings.mode
    if mode == "auto":
        if settings.profile == "large":
            mode = "full_text"
        elif settings.profile == "medium":
            mode = "full_text" if len(source_text) <= budget else "extractive_rag"
        else:
            mode = "extractive_rag"

    if mode == "full_text":
        context = build_effective_context(
            source_text, mode="full_text", budget=budget
        )
    else:
        if settings.max_source_chars is None:
            raise RuntimeError("extractive RAG requires a frozen context budget")
        retrieval = select_bm25_ranges(
            build_catalog(source_text), claim_text, budget=budget
        )
        context = build_effective_context(
            source_text,
            mode="extractive_rag",
            budget=budget,
            retrieved_ranges=retrieval.raw_ranges,
            retrieval_algorithm=retrieval.algorithm,
            retrieval_config=dict(retrieval.config),
        )
    return (
        {
            "claim": claim_text,
            "claim_context": claim_context,
            "citation_marker": citation_marker,
        },
        context.snapshot(),
    )


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise RuntimeError(f"{label} must be non-empty text")
    return value
