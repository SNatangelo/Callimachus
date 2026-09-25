# core/verify/source_spans.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable, raw-offset source-evidence spans.

This module deliberately operates on the *raw* prepared source text.  It does
not normalize whitespace, typography, Unicode, or formulas: a materialized
span is always exactly ``source_text[raw_start:raw_end]``.  The verifier can
therefore select code-owned IDs instead of retyping passages from memory.

The public surface is intentionally small:

``build_catalog(text)``
    deterministically partitions a source into contiguous, covering spans;
``SourceSpanCatalog.compact()``
    produces the compact, JSON-ready catalogue supplied to a model;
``materialize_passages(ids)``
    returns verbatim quoted passages for an ordered ID list; and
``materialize_contiguous(selections)``
    materializes one raw contiguous range, including optional subspan offsets.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence


_SCHEMA = "source-span-v1"
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])(?=\s+)")
_PARAGRAPH_BOUNDARY_RE = re.compile(r"\n{2,}")
_NUL_BOUNDARY_RE = re.compile(r"\x00+")
_SPAN_ID_RE = re.compile(
    r"^ss1:([0-9a-f]{16}):(\d+):(\d+):([0-9a-f]{20})$"
)
_MAX_SPAN_CHARS = 1200
_MAX_COLLAPSED_WHITESPACE_EXPANSION = 16


class SourceSpanError(ValueError):
    """Base class for invalid source-span input."""


class SourceHashMismatch(SourceSpanError):
    """A caller attempted to use a catalogue for a different source text."""


class UnknownSpanID(SourceSpanError):
    """A requested span ID is absent from the catalogue."""


class InvalidSpanRange(SourceSpanError):
    """A subspan offset is malformed or lies outside its parent span."""


class NonContiguousSpanSelection(SourceSpanError):
    """Multiple selected raw ranges cannot form one contiguous quotation."""


def source_text_hash(source_text: str) -> str:
    """Return the SHA-256 identity of raw source text (UTF-8, unnormalised)."""
    if not isinstance(source_text, str):
        raise TypeError("source_text must be a str")
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


def _span_digest(source_hash: str, raw_start: int, raw_end: int, text: str) -> str:
    """Bind an ID to source identity, raw offsets, and the raw selected bytes."""
    payload = f"{_SCHEMA}\0{source_hash}\0{raw_start}\0{raw_end}\0".encode("ascii")
    return hashlib.sha256(payload + text.encode("utf-8")).hexdigest()[:20]


def _span_id(source_hash: str, raw_start: int, raw_end: int, text: str) -> str:
    return f"ss1:{source_hash[:16]}:{raw_start}:{raw_end}:{_span_digest(source_hash, raw_start, raw_end, text)}"


@dataclass(frozen=True)
class SourceSpan:
    """One immutable, half-open raw range in a prepared source text."""

    id: str
    raw_start: int
    raw_end: int
    text: str

    def __post_init__(self) -> None:
        if self.raw_start < 0 or self.raw_end <= self.raw_start:
            raise InvalidSpanRange("a source span must have a non-empty half-open range")

    def compact(self) -> dict[str, Any]:
        """JSON-ready evidence record; text remains verbatim, never previewed."""
        return {"id": self.id, "raw_start": self.raw_start, "raw_end": self.raw_end, "text": self.text}


@dataclass(frozen=True)
class SpanSelection:
    """A code-owned optional subrange of one span.

    Offsets are relative to the parent span and use ordinary half-open Python
    slicing.  The model may name an ID; the application decides any subspan
    offsets and validates them here.
    """

    span_id: str
    start_offset: int = 0
    end_offset: int | None = None
    source_hash: str | None = None


def _partition_source(source_text: str) -> list[tuple[int, int]]:
    """Partition raw text into sentence-like spans while covering every char.

    Paragraph boundaries are preferred.  Within a paragraph terminal prose
    punctuation introduces a boundary; everything else (formulae, labels,
    whitespace, repeated text) remains represented in its exact raw slice.
    No content is dropped or normalized.
    """
    if not source_text:
        return []
    boundaries = {0, len(source_text)}
    for match in _PARAGRAPH_BOUNDARY_RE.finditer(source_text):
        # Separators are their own exact spans.  This keeps prose spans clean
        # while retaining every raw newline for evidence that needs it.
        boundaries.add(match.start())
        boundaries.add(match.end())
    for match in _NUL_BOUNDARY_RE.finditer(source_text):
        # NUL bytes occur as extraction delimiters in some prepared sources.
        # Preserve them in the immutable catalogue, but isolate them so they
        # cannot prefix prompt-visible evidence or reach SQLite TEXT APIs.
        boundaries.add(match.start())
        boundaries.add(match.end())
    for match in _SENTENCE_BOUNDARY_RE.finditer(source_text):
        # Keep following whitespace as an independent span, rather than
        # attaching it to either prose sentence.  That makes duplicate prose
        # spans deterministically distinguishable by offsets, not formatting.
        boundaries.add(match.start())
        whitespace = re.match(r"\s+", source_text[match.start():])
        if whitespace:
            boundaries.add(match.start() + whitespace.end())
    ordered = sorted(boundaries)
    ranges = [
        (start, end)
        for start, end in zip(ordered, ordered[1:])
        if start < end
    ]
    bounded: list[tuple[int, int]] = []
    for start, end in ranges:
        cursor = start
        while end - cursor > _MAX_SPAN_CHARS:
            hard = cursor + _MAX_SPAN_CHARS
            soft_floor = cursor + (_MAX_SPAN_CHARS // 2)
            whitespace = [
                match.end()
                for match in re.finditer(r"\s+", source_text[soft_floor:hard])
            ]
            cut = (
                soft_floor + whitespace[-1]
                if whitespace
                else hard
            )
            bounded.append((cursor, cut))
            cursor = cut
        if cursor < end:
            bounded.append((cursor, end))
    return bounded


def _collapsed_whitespace_with_offsets(
    text: str,
) -> tuple[str, list[int], list[int]]:
    """Collapse whitespace while retaining raw bounds for every output char."""
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            end = index + 1
            while end < len(text) and text[end].isspace():
                end += 1
            chars.append(" ")
            starts.append(index)
            ends.append(end)
            index = end
            continue
        chars.append(text[index])
        starts.append(index)
        ends.append(index + 1)
        index += 1
    return "".join(chars), starts, ends


def _collapsed_whitespace(text: str) -> str:
    """Return prompt-comparison coordinates for a raw source range."""
    return " ".join(text.split())


class SourceSpanCatalog:
    """Verified span index for exactly one immutable prepared source text."""

    def __init__(self, source_text: str, spans: Sequence[SourceSpan]):
        self._source_text = source_text
        self.source_hash = source_text_hash(source_text)
        self._spans = tuple(spans)
        self._by_id = {span.id: span for span in self._spans}
        if len(self._by_id) != len(self._spans):
            raise SourceSpanError("duplicate source span IDs")
        self._validate_catalogue()

    @property
    def spans(self) -> tuple[SourceSpan, ...]:
        return self._spans

    def _validate_catalogue(self) -> None:
        if not self._source_text:
            if self._spans:
                raise InvalidSpanRange("empty source text cannot contain spans")
            return
        cursor = 0
        for span in self._spans:
            if span.raw_start != cursor or span.raw_end > len(self._source_text):
                raise InvalidSpanRange("catalogue spans must be contiguous and in bounds")
            expected_text = self._source_text[span.raw_start:span.raw_end]
            if span.text != expected_text:
                raise SourceSpanError("span text does not equal its raw source range")
            expected_id = _span_id(self.source_hash, span.raw_start, span.raw_end, expected_text)
            if span.id != expected_id:
                raise SourceSpanError("span ID failed source/offset integrity verification")
            cursor = span.raw_end
        if cursor != len(self._source_text):
            raise InvalidSpanRange("catalogue does not cover the complete source text")

    def compact(self) -> dict[str, Any]:
        """Return the full, compact JSON transport without duplicating source text.

        Span text is necessary evidence for a caller/model; the original source
        itself is intentionally not emitted again as a separate field.
        """
        return {
            "schema": _SCHEMA,
            "source_hash": self.source_hash,
            "spans": [span.compact() for span in self._spans],
        }

    def selection_aliases(
        self, spans: Sequence[SourceSpan],
    ) -> dict[str, str]:
        """Return deterministic prompt-local aliases for exact span IDs.

        Aliases are a model transport only: callers must materialize the
        returned canonical IDs, never persist the aliases as provenance.
        """
        aliases: dict[str, str] = {}
        seen_ids: set[str] = set()
        for index, span in enumerate(spans, start=1):
            if span.id in seen_ids:
                raise SourceSpanError("duplicate source spans cannot share aliases")
            # Visible prompt spans can be clipped, so validate their canonical
            # ID against raw source rather than requiring catalogue membership.
            if (
                span.raw_end > len(self._source_text)
                or self._source_text[span.raw_start:span.raw_end] != span.text
                or span.id != _span_id(
                    self.source_hash, span.raw_start, span.raw_end, span.text
                )
            ):
                raise SourceSpanError("source span alias target is invalid")
            seen_ids.add(span.id)
            aliases[f"S{index}"] = span.id
        return aliases

    def visible_spans(self, visible_text: str) -> tuple[SourceSpan, ...]:
        """Return materializable spans overlapping the prompt-visible text.

        RAG joins exact source chunks with new paragraph separators, and an
        explicit character cap may cut through one catalogue span. Requiring a
        whole span to occur inside that derived prompt can therefore expose no
        selectable ID. This maps exact or whitespace-collapsed prompt chunks
        back to raw source ranges, then clips canonical spans to the exact
        visible ranges. No hidden character beyond a cap is exposed and no
        text is reconstructed or accepted from the prompt.
        """
        if not isinstance(visible_text, str) or not visible_text.strip():
            return ()
        visible_budget = len(_collapsed_whitespace(visible_text))
        source_collapsed, source_starts, source_ends = (
            _collapsed_whitespace_with_offsets(self._source_text)
        )
        raw_ranges: list[tuple[int, int, bool]] = []
        if self._source_text.startswith(visible_text):
            raw_ranges.append((0, len(visible_text), False))
        chunks = [
            chunk for chunk in re.split(r"\n{2,}", visible_text)
            if chunk.strip()
        ] or [visible_text]
        for chunk in (() if raw_ranges else chunks):
            exact_at = self._source_text.find(chunk)
            if exact_at >= 0:
                # One prompt occurrence maps to one source occurrence. Exposing
                # every duplicate would multiply catalogue text beyond the
                # visible RAG/cap budget without showing the model more source.
                raw_ranges.append((exact_at, exact_at + len(chunk), False))
                continue
            collapsed_chunk = _collapsed_whitespace(chunk)
            if not collapsed_chunk:
                continue
            at = source_collapsed.find(collapsed_chunk)
            if at >= 0:
                end = at + len(collapsed_chunk)
                raw_start = source_starts[at]
                raw_end = source_ends[end - 1]
                raw_text = self._source_text[raw_start:raw_end]
                # A collapsed-string match alone is insufficient: retain the
                # identity and order of every visible character explicitly.
                if (
                    _collapsed_whitespace(raw_text) != collapsed_chunk
                    or "".join(char for char in raw_text if not char.isspace())
                    != "".join(char for char in chunk if not char.isspace())
                ):
                    continue
                # Collapsed matching may map one visible space to an arbitrarily
                # long raw whitespace run. Permit ordinary formatting variance,
                # but fail closed before exposing a pathological invisible run.
                if raw_end - raw_start > len(collapsed_chunk) + _MAX_COLLAPSED_WHITESPACE_EXPANSION:
                    continue
                raw_ranges.append((raw_start, raw_end, True))

        selected: list[SourceSpan] = []
        seen: set[str] = set()
        selected_chars = 0
        for raw_start, raw_end, require_full_range in raw_ranges:
            range_budget = len(_collapsed_whitespace(self._source_text[raw_start:raw_end]))
            remaining = visible_budget - selected_chars
            if remaining <= 0 or (require_full_range and range_budget > remaining):
                continue
            for span in self._spans:
                remaining = visible_budget - selected_chars
                if remaining <= 0:
                    break
                start = max(span.raw_start, raw_start)
                end = min(span.raw_end, raw_end)
                if not require_full_range:
                    end = min(end, start + remaining)
                if start >= end:
                    continue
                text = self._source_text[start:end]
                if not text.replace("\x00", "").strip():
                    continue
                span_id = _span_id(self.source_hash, start, end, text)
                if span_id in seen:
                    continue
                seen.add(span_id)
                selected.append(SourceSpan(span_id, start, end, text))
            selected_chars += range_budget
        return tuple(selected)

    def _check_source_hash(self, expected_source_hash: str | None) -> None:
        if expected_source_hash is not None and expected_source_hash != self.source_hash:
            raise SourceHashMismatch("source hash does not match this span catalogue")

    def get(self, span_id: str, *, expected_source_hash: str | None = None) -> SourceSpan:
        self._check_source_hash(expected_source_hash)
        if not isinstance(span_id, str):
            raise UnknownSpanID(f"unknown source span ID: {span_id!r}")
        known = self._by_id.get(span_id)
        if known is not None:
            return known

        # Prompt caps and retrieved chunks may expose only part of a canonical
        # span. Reconstruct that clipped range only after re-validating the
        # source hash, raw bounds and content-bound ID against the full source.
        match = _SPAN_ID_RE.fullmatch(span_id)
        if not match or match.group(1) != self.source_hash[:16]:
            raise UnknownSpanID(f"unknown source span ID: {span_id!r}")
        raw_start = int(match.group(2))
        raw_end = int(match.group(3))
        if raw_start < 0 or raw_end <= raw_start or raw_end > len(self._source_text):
            raise UnknownSpanID(f"unknown source span ID: {span_id!r}")
        text = self._source_text[raw_start:raw_end]
        if _span_id(self.source_hash, raw_start, raw_end, text) != span_id:
            raise UnknownSpanID(f"unknown source span ID: {span_id!r}")
        return SourceSpan(span_id, raw_start, raw_end, text)

    def span_for_range(
        self, raw_start: int, raw_end: int, *, expected_source_hash: str | None = None
    ) -> SourceSpan:
        """Return the canonical ID for one non-empty in-bounds raw range."""
        self._check_source_hash(expected_source_hash)
        if (
            isinstance(raw_start, bool)
            or isinstance(raw_end, bool)
            or not isinstance(raw_start, int)
            or not isinstance(raw_end, int)
            or raw_start < 0
            or raw_end <= raw_start
            or raw_end > len(self._source_text)
        ):
            raise InvalidSpanRange("raw range must be a non-empty in-bounds half-open range")
        text = self._source_text[raw_start:raw_end]
        return SourceSpan(_span_id(self.source_hash, raw_start, raw_end, text), raw_start, raw_end, text)

    def materialize_passages(
        self, span_ids: Iterable[str], *, expected_source_hash: str | None = None
    ) -> list[str]:
        """Materialize ordered ID references as exact, separate quote passages."""
        self._check_source_hash(expected_source_hash)
        return [self.get(span_id).text for span_id in span_ids]

    def _coerce_selection(self, selection: SpanSelection | Mapping[str, Any]) -> SpanSelection:
        if isinstance(selection, SpanSelection):
            return selection
        if not isinstance(selection, Mapping):
            raise InvalidSpanRange("selection must be a SpanSelection or mapping")
        try:
            return SpanSelection(
                span_id=selection["span_id"],
                start_offset=selection.get("start_offset", 0),
                end_offset=selection.get("end_offset"),
                source_hash=selection.get("source_hash"),
            )
        except (KeyError, TypeError) as exc:
            raise InvalidSpanRange("invalid source span selection") from exc

    def materialize_selection(self, selection: SpanSelection | Mapping[str, Any]) -> str:
        """Return an exact code-owned subspan after strict offset validation."""
        chosen = self._coerce_selection(selection)
        self._check_source_hash(chosen.source_hash)
        span = self.get(chosen.span_id)
        start = chosen.start_offset
        end = len(span.text) if chosen.end_offset is None else chosen.end_offset
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            raise InvalidSpanRange("subspan offsets must be integers")
        if start < 0 or end <= start or end > len(span.text):
            raise InvalidSpanRange("subspan offsets are outside the selected span")
        return span.text[start:end]

    def _raw_selection_range(self, selection: SpanSelection | Mapping[str, Any]) -> tuple[int, int]:
        chosen = self._coerce_selection(selection)
        # Reuse materialize_selection for all type/bounds/hash checks.
        self.materialize_selection(chosen)
        span = self.get(chosen.span_id)
        end = len(span.text) if chosen.end_offset is None else chosen.end_offset
        return span.raw_start + chosen.start_offset, span.raw_start + end

    def materialize_contiguous(
        self, selections: Iterable[SpanSelection | Mapping[str, Any]], *, expected_source_hash: str | None = None
    ) -> str:
        """Materialize one exact quotation from adjacent, ordered selected ranges.

        Passing non-adjacent selections would silently create an ellipsis-like
        reconstructed quote, so it is rejected rather than concatenated.
        """
        self._check_source_hash(expected_source_hash)
        ranges = [self._raw_selection_range(selection) for selection in selections]
        if not ranges:
            raise InvalidSpanRange("a contiguous quotation needs at least one selection")
        previous_end = ranges[0][1]
        for start, end in ranges[1:]:
            if start != previous_end:
                raise NonContiguousSpanSelection("selected source spans are not raw-contiguous")
            previous_end = end
        return self._source_text[ranges[0][0]:ranges[-1][1]]


def build_catalog(source_text: str) -> SourceSpanCatalog:
    """Build a deterministic, complete raw-offset source evidence catalogue."""
    digest = source_text_hash(source_text)
    spans = []
    for raw_start, raw_end in _partition_source(source_text):
        text = source_text[raw_start:raw_end]
        spans.append(SourceSpan(_span_id(digest, raw_start, raw_end, text), raw_start, raw_end, text))
    return SourceSpanCatalog(source_text, spans)


def materialize_passages(
    catalog: SourceSpanCatalog, span_ids: Iterable[str], *, expected_source_hash: str | None = None
) -> list[str]:
    """Convenience API for callers that store the catalogue separately."""
    return catalog.materialize_passages(span_ids, expected_source_hash=expected_source_hash)


def validate_materialized_answer(source_text: str, answer: Mapping[str, Any]) -> str | None:
    """Validate that an answer's passages came only from its immutable IDs."""
    ids = answer.get("source_span_ids")
    if not isinstance(ids, list) or not ids:
        return "evidence requires persisted source_span_ids"
    if any(not isinstance(item, str) or not item for item in ids):
        return "source_span_ids must be non-empty strings"
    if len(set(ids)) != len(ids):
        return "source_span_ids must be distinct"
    catalog = build_catalog(source_text)
    expected_hash = answer.get("source_span_source_hash")
    if expected_hash != catalog.source_hash:
        return "source span hash does not match the guard source"
    try:
        materialized = catalog.materialize_passages(
            ids, expected_source_hash=expected_hash
        )
    except SourceSpanError as exc:
        return str(exc)
    if any(not passage.strip() for passage in materialized):
        return "materialized quoted passages must not be whitespace-only"
    passages = answer.get("quoted_passages")
    if not isinstance(passages, list) or passages != materialized:
        return "quoted_passages were not materialized exactly from source_span_ids"
    return None
