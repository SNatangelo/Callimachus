# core/verify/claim_evidence/evidence/grounding.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure, fail-closed quotation grounding; it never interprets an outcome."""
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata
from typing import Callable

from core.verify.source_spans import SourceSpanError, build_catalog, source_text_hash
from ..domain.types import (
    EVIDENCE_OUTCOMES,
    Jury1Decision,
    Jury1DecisionValidationError,
    validate_jury1_decision,
)
from .context import ContextError, EffectiveContext, validate_effective_context


class GroundingError(ValueError):
    """A Jury1 quotation has no unique mechanically valid raw location."""

    def __init__(self, message: str, code: str = "decision_or_context_invalid") -> None:
        super().__init__(message)
        self.code = code


_TOKEN_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])([+-]?(?:\d+(?:[.,]\d+)*|\.\d+)"
    r"(?:e[+-]?\d+)?)([A-Za-z%]*)",
    re.I,
)
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|neither|nor|cannot|can't|didn't|"
    r"doesn't|isn't|won't)\b",
    re.I,
)
_ELLIPSIS_RE = re.compile(r"\[\s*(?:\.{3,}|…)\s*\]|\.{3,}|…")
_SUP_MARKER_RE = re.compile(r"⟦SUP:(\d+)⟧")
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "ﬅ": "st", "ﬆ": "st",
}
_DASHES = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-"}
_MATCHING_SEQUENCE_REPLACEMENTS = (
    # Common UTF-8/Windows mojibake copied from academic PDFs.
    ("\u00e2\u20ac\u02dc", "'"),
    ("\u00e2\u20ac\u2122", "'"),
    ("\u00e2\u20ac\u0161", "'"),
    ("\u00e2\u20ac\u203a", "'"),
    ("\u00e2\u20ac\u00b2", "'"),
    ("\u00e2\u20ac\u00b9", "'"),
    ("\u00e2\u20ac\u00ba", "'"),
    ("\u00e2\u20ac\u0153", '"'),
    ("\u00e2\u20ac\x9d", '"'),
    ("\u00e2\u20ac\u017e", '"'),
    ("\u00e2\u20ac\u0178", '"'),
    ("\u00c2\u00ab", '"'),
    ("\u00c2\u00bb", '"'),
    ("\u00e2\u20ac\u00b3", '"'),
    ("\u00e2\u20ac\u2039", ""),
    ("\u00e2\u20ac\u0152", ""),
    ("\u00e2\u20ac\x8d", ""),
    ("\u00ef\u00bb\u00bf", ""),
)
_MATCHING_CHAR_REPLACEMENTS = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u2032": "'", "\u2039": "'", "\u203a": "'", "`": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u00ab": '"', "\u00bb": '"', "\u2033": '"',
    "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
    "\u00ad": "", "\u00a0": " ",
})


@dataclass(frozen=True, slots=True)
class GroundingPolicy:
    version: str = "grounding-v2"
    normalization: str = "nfc-whitespace-dehyphenation-superscript-v2"
    fuzzy_min_chars: int = 40
    fuzzy_min_tokens: int = 8
    fuzzy_min_anchors: int = 3
    fuzzy_ratio: float = 0.92
    fuzzy_margin: float = 0.02

    def snapshot(self) -> dict[str, object]:
        return {
            "version": self.version,
            "normalization": self.normalization,
            "fuzzy_min_chars": self.fuzzy_min_chars,
            "fuzzy_min_tokens": self.fuzzy_min_tokens,
            "fuzzy_min_anchors": self.fuzzy_min_anchors,
            "fuzzy_ratio": self.fuzzy_ratio,
            "fuzzy_margin": self.fuzzy_margin,
        }


POLICY = GroundingPolicy()


@dataclass(frozen=True, slots=True)
class GroundedQuote:
    text: str
    raw_start: int
    raw_end: int
    span_id: str
    source_hash: str
    match_mode: str
    score: float


@dataclass(frozen=True, slots=True)
class GroundedDecision:
    decision: Jury1Decision
    quotes: tuple[GroundedQuote, ...]
    policy: GroundingPolicy


def _normal_with_map(text: str) -> tuple[str, list[int], list[int]]:
    units: list[tuple[str, int, int]] = []
    index = 0
    while index < len(text):
        superscript = _SUP_MARKER_RE.match(text, index)
        if superscript is not None:
            raw_end = superscript.end()
            units.extend((char, index, raw_end) for char in f"[{superscript.group(1)}]")
            index = raw_end
            continue
        char = text[index]
        mapped_dash = _DASHES.get(char, char)
        if mapped_dash == "-" and index and text[index - 1].isalpha():
            end = index + 1
            while end < len(text) and text[end].isspace():
                end += 1
            if end > index + 1 and end < len(text) and text[end].isalpha():
                index = end
                continue
        if char.isspace():
            end = index + 1
            while end < len(text) and text[end].isspace():
                end += 1
            if units and units[-1][0] != " ":
                units.append((" ", index, end))
            index = end
            continue
        normalized = unicodedata.normalize("NFC", mapped_dash)
        normalized = _LIGATURES.get(normalized, normalized)
        normalized = normalized.replace("’", "'").casefold()
        units.extend((item, index, index + 1) for item in normalized)
        index += 1
    compact: list[tuple[str, int, int]] = []
    for position, unit in enumerate(units):
        char = unit[0]
        previous = compact[-1][0] if compact else ""
        following = units[position + 1][0] if position + 1 < len(units) else ""
        if char == " " and (previous in "([{" or following in ")]},.;:!?%"):
            continue
        compact.append(unit)
    while compact and compact[-1][0] == " ":
        compact.pop()
    return (
        "".join(item[0] for item in compact),
        [item[1] for item in compact],
        [item[2] for item in compact],
    )


def _normal(text: str) -> str:
    return _normal_with_map(text)[0]


def normalize_for_matching(text: str) -> str:
    """Return the deterministic compatibility form used by report matching."""
    normalized = text or ""
    for old, new in _MATCHING_SEQUENCE_REPLACEMENTS:
        normalized = normalized.replace(old, new)
    return _normal(normalized.translate(_MATCHING_CHAR_REPLACEMENTS))


def _signature(text: str) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    normalized = _normal(text)
    numbers = tuple(
        (number.replace(",", "").lower(), suffix.lower())
        for number, suffix in _NUMBER_RE.findall(normalized)
    )
    return numbers, tuple(item.lower() for item in _NEGATION_RE.findall(normalized))


def _raw_matches(quote: str, source: str) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    start = source.find(quote)
    while start >= 0:
        matches.append((start, start + len(quote)))
        start = source.find(quote, start + 1)
    return matches


def _normal_matches(quote: str, source: str) -> list[tuple[int, int]]:
    target = _normal(quote)
    normalized, starts, ends = _normal_with_map(source)
    signature = _signature(quote)
    matches: list[tuple[int, int]] = []
    index = normalized.find(target)
    while index >= 0:
        raw_range = (starts[index], ends[index + len(target) - 1])
        if _signature(source[raw_range[0]:raw_range[1]]) == signature:
            matches.append(raw_range)
        index = normalized.find(target, index + 1)
    return matches


def _anchors(target: str, source_tokens: list[tuple[int, str]], count: int) -> tuple[tuple[int, str], ...]:
    frequencies = Counter(token for _, token in source_tokens)
    negations = set(_NEGATION_RE.findall(target))
    candidates = [
        (frequencies[token], offset, token)
        for offset, token in ((match.start(), match.group()) for match in _TOKEN_RE.finditer(target))
        if len(token) >= 3 and frequencies[token] and token not in negations
    ]
    result: list[tuple[int, str]] = []
    seen: set[str] = set()
    for _, offset, token in sorted(candidates):
        if token not in seen:
            seen.add(token)
            result.append((offset, token))
        if len(result) == count:
            break
    return tuple(result)


def _fuzzy_matches(
    quote: str, source: str, policy: GroundingPolicy = POLICY
) -> list[tuple[float, int, int]]:
    target = _normal(quote)
    target_tokens = list(_TOKEN_RE.finditer(target))
    if len(target) < policy.fuzzy_min_chars or len(target_tokens) < policy.fuzzy_min_tokens:
        return []
    normalized, raw_starts, raw_ends = _normal_with_map(source)
    source_tokens = [(match.start(), match.group()) for match in _TOKEN_RE.finditer(normalized)]
    anchors = _anchors(target, source_tokens, policy.fuzzy_min_anchors)
    if len(anchors) < policy.fuzzy_min_anchors:
        return []
    starts_by_token: dict[str, list[int]] = {}
    for offset, token in source_tokens:
        starts_by_token.setdefault(token, []).append(offset)
    candidate_starts = sorted({
        source_offset - target_offset
        for target_offset, token in anchors
        for source_offset in starts_by_token[token]
        if source_offset >= target_offset
    })
    signature = _signature(quote)
    scored: list[tuple[int, float]] = []
    anchor_tokens = {token for _, token in anchors}
    for start in candidate_starts:
        end = start + len(target)
        if end > len(normalized):
            continue
        candidate = normalized[start:end]
        if _signature(candidate) != signature:
            continue
        if len(anchor_tokens & {match.group() for match in _TOKEN_RE.finditer(candidate)}) < policy.fuzzy_min_anchors:
            continue
        score = SequenceMatcher(None, target, candidate, autojunk=False).ratio()
        if score >= policy.fuzzy_ratio:
            scored.append((start, score))
    clusters: list[list[tuple[int, float]]] = []
    for item in scored:
        if not clusters or item[0] - clusters[-1][0][0] > 4:
            clusters.append([])
        clusters[-1].append(item)
    champions = [max(cluster, key=lambda item: (item[1], -item[0])) for cluster in clusters]
    return sorted(
        (
            score,
            raw_starts[start],
            raw_ends[start + len(target) - 1],
        )
        for start, score in champions
        if _signature(source[raw_starts[start]:raw_ends[start + len(target) - 1]]) == signature
    )[::-1]


def _locate(quote: str, source: str) -> tuple[int, int, str, float]:
    if not isinstance(quote, str) or not quote.strip():
        raise GroundingError("quotation is empty or invalid", "empty_quotation")
    matches = _raw_matches(quote, source)
    if len(matches) == 1:
        return (*matches[0], "exact_raw", 1.0)
    if len(matches) > 1:
        raise GroundingError("quotation has ambiguous exact raw matches", "ambiguous_exact_raw_match")
    matches = _normal_matches(quote, source)
    if len(matches) == 1:
        return (*matches[0], "normalized", 1.0)
    if len(matches) > 1:
        raise GroundingError("quotation has ambiguous normalized matches", "ambiguous_normalized_match")
    if _ELLIPSIS_RE.search(_normal(quote)):
        raise GroundingError("quotation contains an ungrounded omission marker", "ungrounded_omission_marker")
    candidates = _fuzzy_matches(quote, source)
    if not candidates:
        raise GroundingError("quotation has no guarded fuzzy match", "no_guarded_fuzzy_match")
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] <= POLICY.fuzzy_margin:
        raise GroundingError("quotation has ambiguous guarded fuzzy matches", "ambiguous_guarded_fuzzy_match")
    score, start, end = candidates[0]
    return start, end, "fuzzy", score


def _validated_location(
    located: object, quote: str, catalog: object
) -> tuple[object, str, float]:
    if not isinstance(located, tuple) or len(located) != 4:
        raise GroundingError("locator result is malformed", "malformed_locator_result")
    start, end, mode, score = located
    if (
        isinstance(start, bool) or isinstance(end, bool)
        or not isinstance(start, int) or not isinstance(end, int)
        or mode not in {"exact_raw", "normalized", "fuzzy"}
        or isinstance(score, bool) or not isinstance(score, (int, float))
        or not 0 <= score <= 1
    ):
        raise GroundingError("locator result is malformed", "malformed_locator_result")
    try:
        span = catalog.span_for_range(start, end)
    except SourceSpanError as exc:
        raise GroundingError("locator raw range is invalid", "invalid_locator_range") from exc
    if mode == "exact_raw" and span.text != quote:
        raise GroundingError("exact locator result does not match the quotation", "exact_locator_mismatch")
    if mode == "normalized" and _normal(span.text) != _normal(quote):
        raise GroundingError("normalized locator result does not match the quotation", "normalized_locator_mismatch")
    if mode == "fuzzy" and (score < POLICY.fuzzy_ratio or _signature(span.text) != _signature(quote)):
        raise GroundingError("fuzzy locator result violates the frozen policy", "fuzzy_locator_policy_violation")
    return span, mode, float(score)


def ground_jury1_decision(
    decision: Jury1Decision,
    source_text: str,
    context: EffectiveContext,
    *,
    locator: Callable[[str, str], tuple[int, int, str, float]] = _locate,
) -> GroundedDecision:
    """Ground required evidence without changing or assigning its outcome."""
    try:
        decision = validate_jury1_decision(decision)
        validate_effective_context(context, source_text)
    except (Jury1DecisionValidationError, ContextError) as exc:
        raise GroundingError(str(exc)) from exc
    if decision.outcome not in EVIDENCE_OUTCOMES:
        return GroundedDecision(decision, (), POLICY)
    catalog = build_catalog(source_text)
    source_hash = source_text_hash(source_text)
    quoted: list[GroundedQuote] = []
    used: set[tuple[int, int]] = set()
    for quote in decision.evidence:
        span, mode, score = _validated_location(locator(quote, source_text), quote, catalog)
        raw_range = (span.raw_start, span.raw_end)
        if raw_range in used:
            raise GroundingError("evidence quotations ground to the same raw range", "duplicate_evidence_range")
        if context.mode == "extractive_rag" and not any(
            item.raw_start <= span.raw_start and span.raw_end <= item.raw_end
            for item in context.ranges
        ):
            raise GroundingError("evidence lies outside the retrieved context", "evidence_outside_context")
        quoted.append(GroundedQuote(
            span.text, span.raw_start, span.raw_end, span.id, source_hash, mode, score
        ))
        used.add(raw_range)
    return GroundedDecision(decision, tuple(quoted), POLICY)


def ground_jury1_span_decision(
    decision: Jury1Decision,
    evidence_aliases: tuple[str, ...],
    source_text: str,
    context: EffectiveContext,
) -> GroundedDecision:
    """Resolve prompt-local aliases and materialize canonical source spans."""
    try:
        decision = validate_jury1_decision(decision)
        validate_effective_context(context, source_text)
    except (Jury1DecisionValidationError, ContextError) as exc:
        raise GroundingError(str(exc)) from exc
    if (
        not isinstance(evidence_aliases, tuple)
        or any(not isinstance(alias, str) or not alias for alias in evidence_aliases)
        or len(set(evidence_aliases)) != len(evidence_aliases)
        or len(evidence_aliases) != len(decision.evidence)
    ):
        raise GroundingError(
            "source span selection does not match decision evidence",
            "span_selection_invalid",
        )
    if decision.outcome not in EVIDENCE_OUTCOMES:
        return GroundedDecision(decision, (), POLICY)
    catalog = build_catalog(source_text)
    quoted: list[GroundedQuote] = []
    try:
        visible_spans = catalog.visible_spans(context.text)
        aliases = catalog.selection_aliases(visible_spans)
        alias_map = dict(zip(aliases, visible_spans))
        for alias, evidence in zip(evidence_aliases, decision.evidence):
            span = alias_map.get(alias)
            if span is None:
                raise GroundingError(
                    "evidence alias is not present in visible source spans",
                    "span_selection_invalid",
                )
            if span.text != evidence:
                raise GroundingError(
                    "materialized source span differs from decision evidence",
                    "span_text_mismatch",
                )
            if any(item.text == span.text for item in quoted):
                raise GroundingError("evidence selections contain duplicate text", "duplicate_evidence_text")
            if context.mode == "extractive_rag" and not any(
                item.raw_start <= span.raw_start and span.raw_end <= item.raw_end
                for item in context.ranges
            ):
                raise GroundingError(
                    "selected source span lies outside retrieved context",
                    "evidence_outside_context",
                )
            quoted.append(
                GroundedQuote(
                    span.text,
                    span.raw_start,
                    span.raw_end,
                    span.id,
                    catalog.source_hash,
                    "exact_raw",
                    1.0,
                )
            )
    except SourceSpanError as exc:
        raise GroundingError(str(exc), "unknown_source_span") from exc
    return GroundedDecision(decision, tuple(quoted), POLICY)
