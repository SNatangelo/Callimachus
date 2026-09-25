# core/verify/claim_evidence/evidence/context.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Frozen, caller-resolved effective contexts for Jury1."""
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from core.verify.source_spans import SourceSpanError, build_catalog, source_text_hash


class ContextError(ValueError):
    """A context is invalid, incomplete, or cannot fit its frozen budget."""


@dataclass(frozen=True, slots=True)
class RetrievedRange:
    span_id: str
    raw_start: int
    raw_end: int
    text: str


@dataclass(frozen=True, slots=True)
class EffectiveContext:
    mode: str
    budget: int
    text: str
    source_hash: str
    context_hash: str
    retrieval_algorithm: str | None
    retrieval_config: tuple[tuple[str, str | int | bool], ...]
    ranges: tuple[RetrievedRange, ...]

    def snapshot(self) -> dict[str, Any]:
        return {"mode": self.mode, "budget": self.budget, "text": self.text, "source_hash": self.source_hash, "context_hash": self.context_hash, "retrieval_algorithm": self.retrieval_algorithm, "retrieval_config": dict(self.retrieval_config), "ranges": [{"span_id": item.span_id, "raw_start": item.raw_start, "raw_end": item.raw_end, "text": item.text} for item in self.ranges]}


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _budget(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContextError("a positive context budget is required")
    return value


def _config(value: Mapping[str, Any] | None, *, persisted: bool = False) -> tuple[tuple[str, str | int | bool], ...]:
    if value is None:
        return ()
    if persisted and not isinstance(value, tuple):
        raise ContextError("persisted retrieval configuration is malformed")
    entries = value if persisted else value.items() if isinstance(value, Mapping) else ()
    if not isinstance(value, Mapping) and not persisted:
        raise ContextError("retrieval configuration must be a mapping")
    if any(
        not isinstance(item, tuple)
        or len(item) != 2
        or not isinstance(item[0], str)
        or not item[0].strip()
        or item[0] != item[0].strip()
        or not isinstance(item[1], (str, int, bool))
        for item in entries
    ):
        raise ContextError("retrieval configuration is malformed")
    result = tuple(entries) if persisted else tuple(sorted(entries))
    if len({key for key, _ in result}) != len(result) or result != tuple(sorted(result)):
        raise ContextError("retrieval configuration keys are invalid")
    return result


def _body(mode: str, budget: int, text: str, source_hash: str, algorithm: str | None, config: tuple[tuple[str, str | int | bool], ...], ranges: tuple[RetrievedRange, ...]) -> dict[str, Any]:
    return {"mode": mode, "budget": budget, "text": text, "source_hash": source_hash, "retrieval_algorithm": algorithm, "retrieval_config": dict(config), "ranges": [(item.span_id, item.raw_start, item.raw_end, item.text) for item in ranges]}


def _freeze(source_text: str, mode: str, budget: int, algorithm: str | None, config: tuple[tuple[str, str | int | bool], ...], ranges: tuple[RetrievedRange, ...]) -> EffectiveContext:
    text = source_text if mode == "full_text" else "\n\n".join(item.text for item in ranges)
    if len(text) > budget:
        raise ContextError("effective context exceeds its frozen budget")
    source_hash = source_text_hash(source_text)
    body = _body(mode, budget, text, source_hash, algorithm, config, ranges)
    return EffectiveContext(mode, budget, text, source_hash, _hash(body), algorithm, config, ranges)


def _ranges(source_text: str, raw_ranges: Sequence[tuple[int, int]], source_hash: str) -> tuple[RetrievedRange, ...]:
    if not isinstance(raw_ranges, Sequence) or isinstance(raw_ranges, (str, bytes)) or not raw_ranges:
        raise ContextError("extractive_rag requires non-empty retrieved raw ranges")
    catalog = build_catalog(source_text)
    previous = 0
    result: list[RetrievedRange] = []
    for raw_range in raw_ranges:
        if not isinstance(raw_range, tuple) or len(raw_range) != 2:
            raise ContextError("retrieved raw range is malformed")
        try:
            span = catalog.span_for_range(*raw_range, expected_source_hash=source_hash)
        except (SourceSpanError, TypeError) as exc:
            raise ContextError("retrieved raw range is invalid") from exc
        if result and span.raw_start < previous:
            raise ContextError("retrieved raw ranges overlap or are out of order")
        result.append(RetrievedRange(span.id, span.raw_start, span.raw_end, span.text)); previous = span.raw_end
    return tuple(result)


def build_effective_context(source_text: str, *, mode: str, budget: int, retrieved_ranges: Sequence[tuple[int, int]] | None = None, retrieval_algorithm: str | None = None, retrieval_config: Mapping[str, Any] | None = None) -> EffectiveContext:
    """Freeze a full source or caller-selected verbatim RAG horizon; never retrieve."""
    if not isinstance(source_text, str) or not source_text:
        raise ContextError("source text must be non-empty")
    budget = _budget(budget)
    if mode == "full_text":
        if retrieved_ranges is not None or retrieval_algorithm is not None or retrieval_config is not None:
            raise ContextError("full_text forbids retrieval metadata")
        return _freeze(source_text, mode, budget, None, (), ())
    if (
        mode != "extractive_rag"
        or not isinstance(retrieval_algorithm, str)
        or not retrieval_algorithm.strip()
        or retrieval_algorithm != retrieval_algorithm.strip()
    ):
        raise ContextError("extractive_rag requires a non-empty retrieval algorithm")
    source_hash = source_text_hash(source_text)
    return _freeze(source_text, mode, budget, retrieval_algorithm, _config(retrieval_config), _ranges(source_text, retrieved_ranges, source_hash))


def validate_effective_context(context: EffectiveContext, source_text: str) -> EffectiveContext:
    """Rebuild and compare every persisted field, raw range and provenance hash."""
    if not isinstance(context, EffectiveContext) or context.source_hash != source_text_hash(source_text):
        raise ContextError("context source hash is invalid")
    budget = _budget(context.budget)
    config = _config(context.retrieval_config, persisted=True)
    if context.mode == "full_text":
        if context.retrieval_algorithm is not None or config or context.ranges:
            raise ContextError("full_text retrieval metadata is invalid")
        expected = _freeze(source_text, context.mode, budget, None, (), ())
    elif context.mode == "extractive_rag":
        if (
            not isinstance(context.retrieval_algorithm, str)
            or not context.retrieval_algorithm.strip()
            or context.retrieval_algorithm != context.retrieval_algorithm.strip()
        ):
            raise ContextError("retrieval algorithm is invalid")
        raw_ranges = tuple((item.raw_start, item.raw_end) for item in context.ranges) if all(isinstance(item, RetrievedRange) for item in context.ranges) else ()
        ranges = _ranges(source_text, raw_ranges, context.source_hash)
        if ranges != context.ranges:
            raise ContextError("retrieved ranges are tampered")
        expected = _freeze(source_text, context.mode, budget, context.retrieval_algorithm, config, ranges)
    else:
        raise ContextError("context mode is invalid")
    if expected != context:
        raise ContextError("context snapshot is tampered")
    return context


def context_from_snapshot(
    snapshot: Mapping[str, Any],
    source_text: str,
) -> EffectiveContext:
    """Rebuild every frozen context field before a resumed dispatch."""
    try:
        context = EffectiveContext(
            snapshot["mode"],
            snapshot["budget"],
            snapshot["text"],
            snapshot["source_hash"],
            snapshot["context_hash"],
            snapshot["retrieval_algorithm"],
            tuple(sorted(snapshot["retrieval_config"].items())),
            tuple(RetrievedRange(**row) for row in snapshot["ranges"]),
        )
        return validate_effective_context(context, source_text)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContextError("persisted effective context is invalid") from exc
