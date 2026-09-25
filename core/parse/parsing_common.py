#!/usr/bin/env python3
# core/parse/parsing_common.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
parsing_common.py — Pure, format-independent parsing helpers.

These functions operate on text regardless of the source format (PDF, TXT,
DOCX, LaTeX, Markdown).  They are shared by format_handlers families and
by citation_schemes — neither should depend on parse_manuscript directly.
"""

import re
import unicodedata
import urllib.parse
import uuid
import warnings

try:
    import core.parse.parser_text as parser_text
except ImportError:
    import parser_text

try:
    from core.parse import reference_readers
except ImportError:  # direct execution fallback
    import reference_readers

try:
    from core.parse.citation_coordinates import extract_cited_coordinates
except ImportError:  # direct execution fallback
    from citation_coordinates import extract_cited_coordinates

# Year pattern used across author-year detection, biblio splitting, and
# reference parsing.  Exported so citation_schemes can use it without
# importing parse_manuscript.
#
# The same span of centuries the in-text side reads (authoryear.YEAR), and it has to
# be: a paper that cites "(Blackwell 1875)" — or Darwin, or Mendel — must be able to
# hold Blackwell in its bibliography.  Read as 19xx-or-20xx only, the entry carries no
# year at all, is taken for a fragment of the entry above it, and is folded back into
# it; the work then exists nowhere, and the citation of it is an orphan with no cause.
_AY_YEAR = r"(?:1[5-9]|20)\d{2}[a-z]?"

# Every dash a typesetter uses to join two citation numbers into a range.  A house style
# picks one and never says which: ACS sets "11−18" with a MINUS SIGN, Nature an en dash,
# a submitted manuscript a plain hyphen — and all three mean "11 through 18".  A dash
# missing from this class does not raise a range we cannot read; it CUTS the range at the
# first number, and the works behind the dash are lost in silence.  One class, so the
# next dash is added once rather than in the dozen regexes that read a marker.
# (The hyphen is last: a character class needs no escaping there.)
RANGE_DASHES = "–—−‐‑-"
# The number run inside a marker: "5", "4,5", "11−18", "8,13,28".
NUMBER_RUN = rf"\d{{1,3}}(?:\s*[,{RANGE_DASHES}]\s*\d{{1,3}})*"

_NUMBERED_HEADING_RE = re.compile(
    r"^\s*(?!(?:19|20)\d{2}\b)\d+(?:\.\d+)*\.?\s+[A-Z][^\n]{0,80}$"
)
_HEADING_NUMBER_ONLY_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s*$")
# This vocabulary is used only by manuscript layout segmentation, never by
# verification or verdict logic. It deliberately preserves the certified
# two-line-heading behavior: widening it to every capitalized line turns
# numeric table cells into headings and changes real claim boundaries.
_HEADING_TITLE_HINT_RE = re.compile(
    r"\b(?:abstract|introduction|background|related|method(?:s|ology)?|"
    r"model|architecture|encoder|decoder|training|regularization|"
    r"experiment(?:s|al)?|evaluation|results?|discussion|conclusion|"
    r"appendix|references?|pre-?training|fine-?tuning|"
    r"feature(?:-based)?|data(?:set)?s?)\b",
    re.IGNORECASE,
)
_FLOAT_LABEL_RE = re.compile(r"^\s*(?:Figure|Table|Fig\.|Tab\.)\s*\d+", re.I)
_FLOAT_PROSE_SHOWS_RE = re.compile(
    r"^\s*(?:Figure|Table|Fig\.|Tab\.)\s*\d+\s+shows\b", re.I
)
_SHORT_UNPUNCT_RE = re.compile(r"^\s*\S+(?:\s+\S+){0,3}\s*$")
_LAYOUT_CONNECTORS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on",
    "or", "the", "to", "vs", "with",
})
_TABLE_VALUE_LINE_RE = re.compile(
    r"^\s*[\d\s.,%+\-–—−·×/()]+(?:[A-Za-z]{0,3})?\s*$"
)


def _is_float_label(value: str) -> bool:
    """Return whether a line is layout furniture rather than prose.

    ``Table/Figure N shows …`` is an authored sentence form in certified
    manuscripts, not a caption label.
    """
    return bool(
        _FLOAT_LABEL_RE.match(value)
        and not _FLOAT_PROSE_SHOWS_RE.match(value)
    )

# Internal, zero-width-to-readers tag for a region whose *layout* establishes
# that it is a checklist/table rather than prose.  It is stripped at the
# parser's presentation boundary; citation schemes may use it while deciding
# whether an in-region marker is a verification target.
STRUCTURAL_TABLE_SENTINEL = "\ue000cv-structural-table\ue001"
# A checklist-shaped range whose row ownership cannot be proven from flattened
# text. It remains suppressed by default to preserve the certified pair
# universe, but carries a separate debug/provenance label and can be promoted
# through the existing explicit table-verification mode.
STRUCTURAL_AMBIGUOUS_TABLE_SENTINEL = (
    "\ue000cv-structural-ambiguous-table\ue001"
)
# A PDF-only, geometry-proven page seam.  It is internal metadata, never claim
# text: the parser removes it while retaining an observable lineage boundary.
PAGE_LAYOUT_INTERRUPTION_SENTINEL = "\ue000cv-page-layout-interruption\ue001"
PAGE_LAYOUT_REPAIRED_SENTINEL = "\ue000cv-page-layout-repaired\ue001"

# A checklist item label is deliberately structural: ``1``, ``1a`` and ``12b``
# are emitted in the narrow label column of checklists across disciplines.  A
# cluster (or an explicit checklist header) is still required, so a single
# numbered prose line or figure panel cannot create table context.  The suffix
# is intentionally case-sensitive: checklist sub-items are lower-case by
# construction, whereas ``16S``, ``2D`` and similar upper-case tokens are
# routinely scientific identifiers in ordinary prose.
_CHECKLIST_ITEM_LINE_RE = re.compile(
    r"^\s*(?P<number>\d{1,2})(?P<suffix>[a-z]?)(?:\s+|$)")
_CHECKLIST_ALPHA_LABEL_RE = re.compile(
    r"^\s*(?P<number>\d{1,2})(?P<suffix>[a-z])(?:\s+|$)")
_CHECKLIST_TERMINATORS = ".?!。！？"
_COMPLETE_PROSE_END_RE = re.compile(r"[.!?。！？][\"'’”)\]]*\s*$")
_PAGE_NUMBER_CELL_RE = re.compile(r"^\s*\d{1,3}\s*$")


def _checklist_label(line: str) -> tuple[int, str] | None:
    """Return a structural checklist label, retaining its ordered components."""
    match = _CHECKLIST_ITEM_LINE_RE.match(line.strip())
    if not match:
        return None
    return int(match.group("number")), match.group("suffix")


def _coherent_checklist_label_sequence(
    indexes: list[int], lines: list[str],
) -> bool:
    """Whether a label cluster follows a checklist-like local progression.

    Repetition alone is not enough: flattened text can contain unrelated values
    such as ``16S``, ``2D`` and ``3D``.  Numeric rows must increase one by one;
    sub-items may advance within a number (``1a``, ``1b``) or reset to ``a`` at
    the next number (``1b``, ``2a``).  Standalone items (``5``) may appear among
    sub-items; gaps and invalid sub-item jumps fail open as prose.
    """
    values = [_checklist_label(lines[index]) for index in indexes]
    if len(values) < 2 or any(value is None for value in values):
        return False
    labels = [value for value in values if value is not None]
    for (number, suffix), (next_number, next_suffix) in zip(labels, labels[1:]):
        if next_number == number:
            # Only sub-items can share a number and they must advance locally.
            if not suffix or not next_suffix or ord(next_suffix) != ord(suffix) + 1:
                return False
        elif next_number != number + 1:
            return False
        # A checklist can mix a standalone item (``5``) with sub-items
        # (``4a``, ``4b``, ``6a``).  Crossing to the next item may therefore
        # either reset to ``a`` or remain unsuffixed, but never jump to ``b``.
        elif next_suffix not in ("", "a"):
            return False
    return True


def _is_structural_checklist_header(line: str) -> bool:
    """Recognize short, marker-free header furniture by layout only."""
    stripped = line.strip()
    return bool(
        stripped
        and len(stripped.split()) <= 4
        and not stripped.endswith(tuple(_CHECKLIST_TERMINATORS))
        and not re.search(r"\[[^\]]+\]|\b(?:19|20)\d{2}[a-z]?\b", stripped)
    )


def _contains_complete_prose(lines: list[str]) -> bool:
    """Whether physical lines contain a complete prose-shaped sentence.

    This is deliberately citation-style blind: bracket, author-year,
    superscript, footnote and future marker encodings all receive the same
    treatment. The checklist detector may isolate repeated label rows, but it
    must not suppress a punctuated prose island merely because PDF columns
    placed that island between or immediately after labels.
    """
    joined = " ".join(line.strip() for line in lines if line.strip())
    return bool(
        any(char.isalpha() for char in joined)
        and _COMPLETE_PROSE_END_RE.search(joined)
    )


def _contains_complete_physical_sentence(lines: list[str]) -> bool:
    """Whether any one observed line is already a complete prose sentence.

    This narrower helper is used at a checklist's outer edges, where unrelated
    prose can be followed by unpunctuated column furniture. Checking only the
    concatenated gap would let that furniture hide the sentence terminator.
    """
    return any(
        any(char.isalpha() for char in line)
        and _COMPLETE_PROSE_END_RE.search(line.strip())
        for line in lines
    )


def _split_numbered_heading(lines: list[str], index: int) -> int:
    """Return the end of a two-line numbered heading, or ``index``.

    Scholarly PDFs frequently extract ``3.1`` and ``Pre-training BERT`` on
    separate lines. Restrict the second line to the established parser-layout
    heading vocabulary: accepting every capitalized line also accepts numeric
    result-table cells and silently changes certified claim boundaries. This
    classifier inserts a boundary only; it never affects semantic verification.
    """
    if index + 1 >= len(lines):
        return index
    number = lines[index].strip()
    title = lines[index + 1].strip()
    if not _HEADING_NUMBER_ONLY_RE.fullmatch(number):
        return index
    if not title or len(title) > 80 or title.endswith((".", "!", "?", ":", ";")):
        return index
    if re.search(r"\[[^\]]+\]|\b(?:19|20)\d{2}[a-z]?\b", title):
        return index
    return index + 2 if _HEADING_TITLE_HINT_RE.search(title) else index


def _looks_like_layout_label(line: str) -> bool:
    """True for compact figure/diagram labels, not wrapped prose."""
    stripped = line.strip()
    if not stripped or len(stripped) > 80:
        return False
    if stripped.endswith((".", "!", "?", ":", ";")):
        return False
    if re.search(r"\b(?:19|20)\d{2}[a-z]?\b|\[[^\]]+\]", stripped):
        return False
    tokens = re.findall(r"[A-Za-zÀ-ÿ0-9]+|\.{3}|\[[A-Za-z]+\]", stripped)
    if not tokens or len(tokens) > 8:
        return False
    words = re.findall(r"[A-Za-zÀ-ÿ]+", stripped)
    if not words:
        return True
    title_like = all(
        word.lower() in _LAYOUT_CONNECTORS
        or word[:1].isupper()
        or word.isupper()
        for word in words
    )
    symbolic = bool(re.search(r"\[[A-Za-z]+\]|\b[A-Z]\d+\b|\.{3}", stripped))
    return title_like or symbolic


def _is_post_table_heading(lines: list[str], index: int) -> bool:
    """Recognize an unnumbered prose heading immediately after numeric rows."""
    if index <= 0 or index + 1 >= len(lines):
        return False
    heading = lines[index].strip()
    if not _looks_like_layout_label(heading):
        return False
    if len(re.findall(r"[A-Za-zÀ-ÿ]+", heading)) > 5:
        return False
    before = lines[index - 1].strip()
    after = lines[index + 1].strip()
    return bool(
        _TABLE_VALUE_LINE_RE.fullmatch(before)
        and len(re.findall(r"[A-Za-zÀ-ÿ]+", after)) >= 5
        and after[:1].isupper()
    )


_POSTFIX_MARKER_LINE_RE = re.compile(
    rf"[.!?]\s*(?:\[(?:{NUMBER_RUN})\]|"
    rf"⟦SUP:[\d,{RANGE_DASHES}]+⟧)\s*[.!?]?\s*$"
)


def _is_heading_after_postfix_marker(lines: list[str], index: int) -> bool:
    """Recognize a short physical heading after a completed cited line.

    The marker remains on the preceding physical line; isolating the following
    title before newline folding lets the existing marker-only repair preserve
    that ownership.  This uses only layout and grammar: no paper title,
    bibliography identity, or source content participates.
    """
    if index <= 0 or index + 1 >= len(lines):
        return False
    previous = lines[index - 1].strip()
    heading = lines[index].strip()
    following = lines[index + 1].strip()
    if (
        not _POSTFIX_MARKER_LINE_RE.search(previous)
        or len(re.findall(r"[A-Za-zÀ-ÿ]+", previous)) < 2
        or not heading
        or len(heading) > 80
        or heading.endswith((".", "!", "?", ":", ";"))
        or re.search(r"\[[^\]]+\]", heading)
    ):
        return False
    tokens = re.findall(r"[A-Za-zÀ-ÿ]+|\d{4}", heading)
    if not 2 <= len(tokens) <= 7:
        return False
    if any(
        token.isalpha()
        and token.lower() not in _LAYOUT_CONNECTORS
        and not token[:1].isupper()
        for token in tokens
    ):
        return False
    following_words = re.findall(r"[A-Za-zÀ-ÿ]+", following)
    return bool(
        len(following_words) >= 5
        and following[:1].isupper()
        and not _TABLE_VALUE_LINE_RE.fullmatch(following)
    )


def _interleaved_page_continuation_starts(lines: list[str]) -> set[int]:
    """Locate a delayed left-column continuation after right-column content.

    Some PDF text layers end one page with an unfinished left-column line, then
    emit the next page's right-column section (several numbered headings and
    complete prose lines) before returning to the lower-case continuation of
    that unfinished sentence.  Inserting one boundary before the continuation
    prevents the two columns from becoming one claim.  The detector deliberately
    requires every part of that shape; ordinary page-wrap continuations begin
    immediately and are untouched.
    """
    starts: set[int] = set()
    for page_start, raw in enumerate(lines):
        if "\f" not in raw:
            continue
        prev_index = page_start - 1
        while prev_index >= 0 and not lines[prev_index].strip():
            prev_index -= 1
        if prev_index < 0:
            continue
        previous = lines[prev_index].strip()
        if previous.endswith((".", "!", "?", ":", ";")):
            continue
        if len(re.findall(r"[A-Za-zÀ-ÿ]+", previous)) < 4:
            continue

        numbered_headings = 0
        complete_prefix_lines = 0
        for index in range(page_start, min(len(lines), page_start + 24)):
            line = lines[index].replace("\f", "").strip()
            if not line:
                continue
            if _HEADING_NUMBER_ONLY_RE.fullmatch(line):
                numbered_headings += 1
            words = re.findall(r"[A-Za-zÀ-ÿ]+", line)
            if (
                line[:1].islower()
                and len(words) >= 5
                and index - page_start >= 6
                and numbered_headings >= 2
                and complete_prefix_lines >= 2
            ):
                starts.add(index)
                break
            if len(words) >= 5 and line.endswith((".", "!", "?", ":")):
                complete_prefix_lines += 1
    return starts


def isolate_interleaved_page_continuations(text: str) -> str:
    """Insert paragraph boundaries at certified cross-column page seams."""
    lines = text.split("\n")
    starts = _interleaved_page_continuation_starts(lines)
    if not starts:
        return text
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in starts:
            out.append("")
        out.append(line)
    return "\n".join(out)


def _structural_checklist_regions(
    lines: list[str],
) -> tuple[
    list[tuple[int, int]],
    list[tuple[int, int]],
    list[tuple[int, int]],
]:
    """Return certain-table, visible-ambiguous and suppressed-ambiguous ranges.

    PDF text layers often emit a checklist as alternating short label and
    description lines.  The words are domain-specific, but the repeated label
    spine (``1a``, ``1b``, ``2a`` ...) is not.

    A blank-delimited PDF block is *not* a safe table boundary: two-column
    extraction can interleave the end of a table with ordinary prose without
    inserting a blank line. Each region therefore remains locally anchored to
    a label cluster. Only a bare label followed by exactly one physical row and
    then another label proves row ownership. A complete prose-shaped island is
    retained as an ambiguous claim candidate. An unpunctuated ambiguous range
    stays outside the default pair universe but receives a distinct sentinel,
    making the uncertainty visible and opt-in verifiable instead of silent.
    """
    blocks: list[tuple[int, int]] = []
    ambiguous_visible: list[tuple[int, int]] = []
    ambiguous_suppressed: list[tuple[int, int]] = []
    physical_start = 0
    while physical_start < len(lines):
        while physical_start < len(lines) and not lines[physical_start].strip():
            physical_start += 1
        physical_end = physical_start
        while physical_end < len(lines) and lines[physical_end].strip():
            physical_end += 1
        if physical_start == physical_end:
            physical_start += 1
            continue
        labels = [
            index for index in range(physical_start, physical_end)
            if _CHECKLIST_ITEM_LINE_RE.match(lines[index].strip())
        ]
        run_start = 0
        last_certified_label: tuple[int, str] | None = None
        while run_start < len(labels):
            run_end = run_start + 1
            while (
                run_end < len(labels)
                and labels[run_end] - labels[run_end - 1] <= 6
            ):
                run_end += 1
            cluster = labels[run_start:run_end]
            first_label = cluster[0]
            last_label = cluster[-1]
            header_floor = max(physical_start, first_label - 12)
            alpha_label_indexes = [
                index for index in cluster
                if _CHECKLIST_ALPHA_LABEL_RE.match(lines[index])
            ]
            header_indexes = [
                index for index in range(header_floor, first_label)
                if _is_structural_checklist_header(lines[index])
            ]
            # Preserve legitimate standalone checklist items (``5``, ``9``)
            # when the complete spine is coherent.  Fall back to the suffixed
            # subsequence only when interposed page-number cells invalidate the
            # complete sequence; bare numbers cannot authorize a table alone.
            coherent = _coherent_checklist_label_sequence(cluster, lines)
            if not coherent:
                coherent = _coherent_checklist_label_sequence(
                    alpha_label_indexes, lines
                )
            enough_labels = len(alpha_label_indexes) >= 3 and coherent
            # A visible header can lower the count, never the progression
            # requirement.  Otherwise arbitrary numeric-looking prose becomes
            # a structural table merely because it happens to be nearby.
            continuation = (
                len(alpha_label_indexes) >= 2
                and bool(header_indexes)
                and coherent
            )
            first_value = _checklist_label(lines[first_label])
            continued_bare_cluster = bool(
                not alpha_label_indexes
                and coherent
                and header_indexes
                and last_certified_label is not None
                and first_value is not None
                and first_value[0] == last_certified_label[0] + 1
                and first_value[1] in ("", "a")
            )
            if enough_labels or continuation or continued_bare_cluster:
                header_owned_end = None
                if header_indexes:
                    header_group = [header_indexes[-1]]
                    for index in reversed(header_indexes[:-1]):
                        if header_group[0] - index != 1:
                            break
                        header_group.insert(0, index)
                    region_start = header_group[0]
                    header_owned_end = header_group[-1] + 1
                    caption_floor = max(physical_start, region_start - 3)
                    for index in range(
                        region_start - 1, caption_floor - 1, -1
                    ):
                        if _is_float_label(lines[index].strip()):
                            region_start = index
                            break
                else:
                    region_start = first_label

                # Preserve the historical region exactly unless an interior
                # gap forms complete prose. Split around the whole gap:
                # labels remain table rows, while every citation encoding in
                # the prose remains observable.
                cursor = region_start
                if header_owned_end is not None:
                    prefix = lines[header_owned_end:first_label]
                    prefix_is_prose = (
                        _contains_complete_prose(
                            prefix
                        )
                        or _contains_complete_physical_sentence(
                            prefix
                        )
                    )
                    if prefix:
                        if cursor < header_owned_end:
                            blocks.append((cursor, header_owned_end))
                        target = (
                            ambiguous_visible
                            if prefix_is_prose else ambiguous_suppressed
                        )
                        target.append((header_owned_end, first_label))
                        cursor = first_label
                for left, right in zip(cluster, cluster[1:]):
                    label_match = _CHECKLIST_ITEM_LINE_RE.match(
                        lines[left].strip()
                    )
                    label_has_inline_text = bool(
                        label_match
                        and lines[left].strip()[
                            label_match.end():
                        ].strip()
                    )
                    gap = lines[left + 1:right]
                    page_cell_indexes = {
                        index for index in range(left + 1, right)
                        if _PAGE_NUMBER_CELL_RE.fullmatch(lines[index])
                    }
                    row_gap = [
                        line for index, line in enumerate(gap, start=left + 1)
                        if index not in page_cell_indexes
                    ]
                    # A bare label followed by exactly one physical line and
                    # then the next label is the only unambiguous ownership
                    # shape available in flattened text. Longer gaps fail open:
                    # they may be interleaved prose from another PDF column,
                    # and suppressing them would be a silent citation miss.
                    bare_single_line_row = (
                        not label_has_inline_text and len(row_gap) == 1
                    )
                    if row_gap and not bare_single_line_row:
                        if cursor < left + 1:
                            blocks.append((cursor, left + 1))
                        target = (
                            ambiguous_visible
                            if _contains_complete_prose(row_gap)
                            else ambiguous_suppressed
                        )
                        target.append((left + 1, right))
                        cursor = right
                last_label_match = _CHECKLIST_ITEM_LINE_RE.match(
                    lines[last_label].strip()
                )
                last_label_has_inline_text = bool(
                    last_label_match
                    and lines[last_label].strip()[
                        last_label_match.end():
                    ].strip()
                )
                region_end = min(physical_end, last_label + 1)
                if (
                    not last_label_has_inline_text
                    and last_label + 1 < physical_end
                ):
                    # With no successor label, no following line has proven
                    # row ownership.  Keep at most the immediate physical row
                    # ambiguous; extending across wrapped-looking text here
                    # silently eats adjacent prose and its citations.
                    tail_end = min(last_label + 2, physical_end)
                    target = (
                        ambiguous_visible
                        if _contains_complete_prose(
                            lines[last_label + 1:tail_end]
                        )
                        else ambiguous_suppressed
                    )
                    target.append((last_label + 1, tail_end))
                if cursor < region_end:
                    blocks.append((cursor, region_end))
                last_certified_label = _checklist_label(lines[last_label])
            run_start = run_end
        physical_start = physical_end + 1

    # Nearby header/label discoveries can overlap.  Coalesce only overlapping
    # ranges; a prose interval between two table continuations must remain prose.
    merged: list[tuple[int, int]] = []
    for start, end in sorted(blocks):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    def merge_ranges(
        ranges: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if start >= end:
                continue
            if result and start <= result[-1][1]:
                result[-1] = (
                    result[-1][0],
                    max(result[-1][1], end),
                )
            else:
                result.append((start, end))
        return result

    return (
        merged,
        merge_ranges(ambiguous_visible),
        merge_ranges(ambiguous_suppressed),
    )


def _isolate_layout_lines(text: str, *, with_trace: bool = False):
    """Isolate headings and figure-label runs before visual lines are folded.

    Text is retained verbatim apart from surrounding blank boundaries.  The
    parser therefore stops layout furniture from contaminating adjacent prose
    without trying to reconstruct or delete document content.
    """
    # Keep physical source positions alongside the layout-only rewrite.  This
    # is deliberately not a classifier of prose meaning: a record says only
    # which *observed layout transformation* contributed a line to a segment.
    # The public default remains the historical string return; provenance is an
    # opt-in internal companion for sentence lineage.
    raw_lines = text.split("\n")
    page_layout_interruptions: dict[int, list[int]] = {}
    page_layout_repairs: dict[int, list[int]] = {}
    for raw_index, line in enumerate(raw_lines):
        cleaned: list[str] = []
        cursor = 0
        while cursor < len(line):
            if line.startswith(PAGE_LAYOUT_INTERRUPTION_SENTINEL, cursor):
                page_layout_interruptions.setdefault(raw_index, []).append(len(cleaned))
                cursor += len(PAGE_LAYOUT_INTERRUPTION_SENTINEL)
            elif line.startswith(PAGE_LAYOUT_REPAIRED_SENTINEL, cursor):
                page_layout_repairs.setdefault(raw_index, []).append(len(cleaned))
                cursor += len(PAGE_LAYOUT_REPAIRED_SENTINEL)
            else:
                cleaned.append(line[cursor])
                cursor += 1
        raw_lines[raw_index] = "".join(cleaned)
    interleaved_starts = _interleaved_page_continuation_starts(raw_lines)
    lines: list[str] = []
    line_numbers: list[int | None] = []
    for raw_index, line in enumerate(raw_lines):
        if raw_index in interleaved_starts:
            lines.append("")
            line_numbers.append(None)
        lines.append(line)
        line_numbers.append(raw_index)
    raw_offsets: list[int] = []
    cursor = 0
    for line in raw_lines:
        raw_offsets.append(cursor)
        cursor += len(line) + 1

    def trace_for(index: int, kind: str) -> dict | None:
        raw_index = line_numbers[index]
        if raw_index is None:
            return None
        return {
            "line_start": raw_index + 1,
            "line_end": raw_index + 1,
            "raw_start": raw_offsets[raw_index],
            "raw_end": raw_offsets[raw_index] + len(raw_lines[raw_index]),
            "kind": kind,
            "page_layout_interruption_offsets": page_layout_interruptions.get(raw_index, ()),
            "page_layout_repaired_offsets": page_layout_repairs.get(raw_index, ()),
        }

    (
        checklist_blocks,
        checklist_ambiguous,
        checklist_ambiguous_suppressed,
    ) = _structural_checklist_regions(lines)
    checklist_end_by_start = {start: end for start, end in checklist_blocks}
    ambiguous_end_by_start = {
        start: end for start, end in checklist_ambiguous
    }
    ambiguous_suppressed_end_by_start = {
        start: end for start, end in checklist_ambiguous_suppressed
    }
    out: list[str] = []
    trace: list[dict | None] = []

    def emit(value: str, item_trace: dict | None = None) -> None:
        out.append(value)
        trace.append(item_trace)

    i = 0
    while i < len(lines):
        checklist_end = checklist_end_by_start.get(i)
        if checklist_end is not None:
            # Keep the table's physical block separate from surrounding prose
            # before newline folding, and tag only its own lines.
            emit("")
            for index in range(i, checklist_end):
                emit(STRUCTURAL_TABLE_SENTINEL + lines[index],
                     trace_for(index, "table"))
            emit("")
            i = checklist_end
            continue
        ambiguous_end = ambiguous_end_by_start.get(i)
        if ambiguous_end is not None:
            # This text lies next to a checklist spine but has no structurally
            # proven row owner. Keep it visible, isolate it from both table and
            # prose, and carry the uncertainty into any emitted claim.
            emit("")
            for index in range(i, ambiguous_end):
                emit(lines[index], trace_for(index, "ambiguous_layout"))
            emit("")
            i = ambiguous_end
            continue
        ambiguous_suppressed_end = ambiguous_suppressed_end_by_start.get(i)
        if ambiguous_suppressed_end is not None:
            # Preserve the certified default pair universe, but distinguish an
            # uncertain checklist boundary from a proven table row. The
            # explicit table-verification mode can still promote these lines.
            emit("")
            for index in range(i, ambiguous_suppressed_end):
                emit(
                    STRUCTURAL_TABLE_SENTINEL
                    + STRUCTURAL_AMBIGUOUS_TABLE_SENTINEL
                    + lines[index],
                    trace_for(index, "ambiguous_table"),
                )
            emit("")
            i = ambiguous_suppressed_end
            continue
        stripped = lines[i].strip()
        split_heading_end = _split_numbered_heading(lines, i)
        if split_heading_end > i:
            emit("")
            for index in range(i, split_heading_end):
                emit(lines[index].strip(), trace_for(index, "heading"))
            emit("")
            i = split_heading_end
            continue
        if _is_post_table_heading(lines, i):
            emit("")
            emit(stripped, trace_for(i, "heading"))
            emit("")
            i += 1
            continue
        if (_is_heading_after_postfix_marker(lines, i)
                or _NUMBERED_HEADING_RE.match(stripped)
                or _is_float_label(stripped)):
            emit("")
            emit(stripped, trace_for(i, "float" if _is_float_label(stripped)
                 else "heading"))
            emit("")
            i += 1
            continue
        j = i
        while (
            j < len(lines)
            and _SHORT_UNPUNCT_RE.match(lines[j] or " ")
            and not re.search(r"[()&\d]", lines[j])
            and not re.search(r"\b(?:19|20)\d{2}[a-z]?\b", lines[j])
            and not re.search(r"\[[^\]]+\]", lines[j])
            and not lines[j].rstrip().endswith((".", "!", "?", ":", ";"))
        ):
            j += 1
        if j - i >= 3:
            # A real label run may end with one wider title-like label
            # ("Unlabeled Sentence A and B Pair").  Extend only an already
            # established compact run; starting from every title-cased line
            # split numeric benchmark tables into false prose claims.
            while j < len(lines) and _looks_like_layout_label(lines[j]):
                j += 1
            emit("")
            for index in range(i, j):
                emit(lines[index], trace_for(index, "layout"))
            emit("")
            i = j
            continue
        # Even a short run below the isolation threshold remains observable as
        # layout-like furniture.  We do not split or discard it here; carrying
        # the kind lets a later claim record that it was fused with prose.
        emit(lines[i], trace_for(
            i, "layout" if _looks_like_layout_label(lines[i]) else "prose"))
        i += 1
    isolated = "\n".join(out)
    return (isolated, trace) if with_trace else isolated


def _expand_numbers(raw: str) -> list[int]:
    """'12-18' -> [12..18]; '1,3,5' -> [1,3,5]; mixed supported.
    Emits a UserWarning (and skips the token) for reverse-order or
    suspiciously wide ranges — both are silent data-loss without this."""
    nums: list[int] = []
    for part in re.split(r"\s*,\s*", raw.strip()):
        m = re.match(rf"^(\d+)\s*[{RANGE_DASHES}]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                warnings.warn(
                    f"marker range skipped (reverse order): {part!r}",
                    UserWarning, stacklevel=3)
            elif b - a >= 200:
                warnings.warn(
                    f"marker range skipped (suspiciously wide, {b - a + 1} items): {part!r}",
                    UserWarning, stacklevel=3)
            else:
                nums.extend(range(a, b + 1))
        elif part.isdigit():
            nums.append(int(part))
    return nums


_INDEXABILITY_RANK = {"low": 0, "medium": 1, "high": 2}


def apply_reference_readers(refs: list[dict]) -> list[dict]:
    """Re-type a manuscript's references through the readers its list elected.

    Typing runs twice by necessity, not by accident: `_make_reference` builds one
    entry at a time and cannot see the list, while election is a property OF the
    list. So entries are first typed from raw text alone, and re-typed here once
    the whole bibliography exists and has shown which format it is written in.

    A manuscript that elects nothing keeps its heuristic typing untouched, which
    is every paper in the corpus but the law-review one.
    """
    readers = reference_readers.elect([r.get("raw_entry") or "" for r in refs])
    if not readers:
        return refs
    for ref in refs:
        classification = _classify_reference(
            ref.get("raw_entry") or "", doi=ref.get("doi"), pmid=ref.get("pmid"),
            isbn=ref.get("isbn"), url=ref.get("url"), readers=readers)
        ref.update({
            "source_type": classification["source_type"],
            "source_kind": classification["source_kind"],
            "source_type_confidence": classification["source_type_confidence"],
            "source_type_evidence": classification["source_type_evidence"],
            "indexability": classification["indexability"],
        })
    return refs


def _classify_reference(raw: str, doi=None, pmid=None, isbn=None, url=None,
                        readers=()) -> dict:
    """The reference's type, from a reader that recognised its format where one
    does, and from raw-text heuristics otherwise.

    `readers` are the ones this manuscript elected. It defaults to none, so an
    entry typed outside a manuscript's context — one reference at a time, before
    the list exists — is read by the heuristics alone.

    A reader outranks the heuristics on the TYPE: it identified the entry's format
    and read the anchor positionally, which is stronger evidence than "the text
    contains the word 'report'" or a publisher's name.  It does not outrank a
    strong identifier — a DOI or an ISBN settles the type and how findable the
    work is, which position cannot.

    It never LOWERS indexability.  A reporter anchor and the journal heuristic
    agree that a work is an article, but the heuristic additionally saw
    volume/issue/pages; the reader knowing less about findability is no reason to
    forget what the heuristic knew.
    """
    heuristic = _classify_by_text(raw, doi, pmid, isbn, url)
    if doi or pmid or isbn or not readers:
        return heuristic
    record = reference_readers.read(raw, readers=readers)
    reader = (record or {}).get("classification")
    if not reader:
        return heuristic
    evidence = list(heuristic.get("source_type_evidence") or [])
    evidence.append(f"{record['reader']}_{record['anchor']}_anchor")
    indexability = max(
        (reader["indexability"], heuristic["indexability"]),
        key=lambda level: _INDEXABILITY_RANK.get(level, 0))
    return {**reader, "indexability": indexability, "source_type_evidence": evidence}


# OUP reference-work DOI namespaces: Max Planck Encyclopedia of Public
# International Law (`law:epil`), Oxford Research Encyclopedias (`acref`,
# `acrefore`), Oxford DNB (`ref:odnb`).  These are encyclopedia entries, not
# journal articles, and are unevenly covered by Crossref, so they must not be
# read as high-index articles whose absence looks like fabrication.
_REFERENCE_WORK_DOI_RE = re.compile(r"10\.1093/(?:acref|law:epil|ref:)", re.IGNORECASE)


def _classify_by_text(raw: str, doi=None, pmid=None, isbn=None, url=None) -> dict:
    # Date-only publication markers are metadata, not publisher evidence.  In
    # particular, ``(in press)`` must not make an article citation book-like.
    classification_raw = re.sub(
        r"\(\s*(?:in\s+press|forthcoming|n\.\s*d\.)\s*\)", "", raw,
        flags=re.IGNORECASE,
    )
    evidence = []
    has_accessed = bool(re.search(r"accessed|consultato|retrieved", raw, re.IGNORECASE))
    journal_like = bool(
        re.search(r"(?:19|20)\d{2}\s*;\s*\d+", raw)
        or re.search(r"\b\d+\s*\(\s*\d+\s*\)\s*[:,]\s*\d", raw)
        # ``volume, start-end``. The volume must not itself be a 19xx/20xx year,
        # otherwise a bare "2018, 345-678" in prose is misread as journal evidence.
        or re.search(r"\b(?!(?:19|20)\d{2}\b)\d{1,4}\s*,\s*\d{1,6}\s*[–—-]\s*\d+", raw)
        # PNAS and a few other journals use an electronic article locator in
        # place of pages: ``122, e2401231121 (2025)``.
        or re.search(r"\b\d{1,4}\s*,\s*e\d{6,}\b", raw, re.IGNORECASE)
        or re.search(r"\bvol\.\s*\d+", raw, re.IGNORECASE)
    )
    if journal_like:
        evidence.append("journal_volume_issue_pages")
    edition_like = bool(re.search(r"\(\s*\d+(?:st|nd|rd|th)\s+ed\.\s*\)", raw, re.IGNORECASE))
    if edition_like:
        evidence.append("edition_marker")
    pub_like = bool(re.search(
        r"\b(press|publish|publisher|editore|edizioni|wiley|springer|elsevier|"
        r"routledge|norton|basic books|mit press|sage|university press)\b",
        classification_raw, re.IGNORECASE))
    if pub_like:
        evidence.append("publisher_name")
    institution_like = bool(re.search(
        r"\b(ministry|department|organization|organisation|international|who|oecd|"
        r"unesco|world bank|transparency international|government|annual report|"
        r"technical report|report)\b",
        raw, re.IGNORECASE))
    if institution_like:
        evidence.append("institution_or_report_marker")
    chapter_like = bool(re.search(r"\bIn\s+.+\b(?:ed\.|eds\.|edited by)\b.+\bpp\.\s*\d+", raw, re.IGNORECASE))
    if chapter_like:
        evidence.append("chapter_marker")
    if doi and _REFERENCE_WORK_DOI_RE.search(str(doi)):
        # An OUP reference-work entry (encyclopedia), not a journal article.
        # Treat as a reference-work chapter with medium findability so a Crossref
        # miss is not read as a missing high-index article.
        evidence.append("reference_work_doi")
        return {"source_type": "book", "source_kind": "chapter_like",
                "source_type_confidence": "high", "source_type_evidence": evidence,
                "indexability": "medium"}
    if doi or pmid:
        evidence.append("strong_article_identifier")
        return {"source_type": "article", "source_kind": "article_like",
                "source_type_confidence": "high", "source_type_evidence": evidence,
                "indexability": "high"}
    if isbn:
        evidence.append("isbn")
        return {"source_type": "book", "source_kind": "book_like",
                "source_type_confidence": "high", "source_type_evidence": evidence,
                "indexability": "medium"}
    if chapter_like:
        return {"source_type": "book", "source_kind": "chapter_like",
                "source_type_confidence": "medium", "source_type_evidence": evidence,
                "indexability": "medium"}
    if institution_like and (has_accessed or url or not journal_like):
        return {"source_type": "webpage" if url else "unknown", "source_kind": "report_like",
                "source_type_confidence": "medium", "source_type_evidence": evidence,
                "indexability": "low" if not url else "medium"}
    if (edition_like or pub_like) and not journal_like:
        return {"source_type": "book", "source_kind": "book_like",
                "source_type_confidence": "medium", "source_type_evidence": evidence,
                "indexability": "low" if not isbn else "medium"}
    if url and (has_accessed or not journal_like):
        evidence.append("url")
        return {"source_type": "webpage", "source_kind": "webpage_like",
                "source_type_confidence": "medium", "source_type_evidence": evidence,
                "indexability": "medium"}
    if journal_like:
        return {"source_type": "article", "source_kind": "article_like",
                "source_type_confidence": "high", "source_type_evidence": evidence,
                "indexability": "high"}
    return {"source_type": "unknown", "source_kind": "unknown",
            "source_type_confidence": "low", "source_type_evidence": evidence,
            "indexability": "low"}


def _extract_arxiv_doi(raw: str) -> str | None:
    """Extract an arXiv ID from the raw entry and return it as a DOI."""
    # Standard arXiv: arXiv:XXXX.XXXXX or arXiv preprint arXiv:XXXX.XXXXX
    m = re.search(r"(?:arXiv preprint\s*)?arXiv[:\s]*(\d{4}\.\d{4,5}(?:v\d+)?)", raw, re.IGNORECASE)
    if m:
        aid = re.sub(r"v\d+$", "", m.group(1))
        return f"10.48550/arXiv.{aid}"
    # Legacy CoRR format: CoRR, abs/XXXX.XXXX (pre-2015 arXiv CS papers)
    m = re.search(r"CoRR,\s*abs/(\d{4}\.\d{4,5})", raw, re.IGNORECASE)
    if m:
        return f"10.48550/arXiv.{m.group(1)}"
    # Also match bare abs/XXXX.XXXX
    m = re.search(r"\babs/(\d{4}\.\d{4,5})\b", raw, re.IGNORECASE)
    if m:
        return f"10.48550/arXiv.{m.group(1)}"
    return None


def _split_prose_entry_parts(raw: str) -> list[str]:
    """Split a prose reference into sentence-like parts without breaking on initials.

    Author blocks often contain initials such as "Quoc V. Le" or "Yann N. Dauphin".
    Those periods are not end-of-clause markers and must not shift title extraction
    onto the tail of the author list.
    """
    clean = re.sub(r"[{}]", "", raw or "")
    if not clean:
        return []
    sentinel = "__CV_INITIAL_DOT__"
    masked = re.sub(
        # Keep initials inside an author list intact.  The next surname can begin
        # with an accented lowercase letter (``J. Hénaff``); treating that period
        # as a sentence boundary makes the remaining authors look like the title.
        r"\b([A-ZÀ-Þ])\.(?=\s+(?:[A-ZÀ-Þ]\b|[A-ZÀ-Þ][a-zà-ÿ]))",
        lambda m: m.group(1) + sentinel,
        clean,
    )
    return [
        part.replace(sentinel, ".").strip(" ,;")
        for part in re.split(r"\.\s+", masked)
        if part.strip(" ,;")
    ]


def _split_before_year(text: str) -> str | None:
    """Extract the portion before the first year, then clean trailing venue markers."""
    m = re.search(r"\b(?:19|20)\d{2}\b", text)
    if not m:
        return text.strip(" ,;")
    # Take everything before the year
    before = text[:m.start()].rstrip(" ,;([]{")
    if not before:
        return None
    # Try the trailing "In" / "in" markers.  Use the *rightmost* match
    # Strip venue markers iteratively from the rightmost match, so that
    # nested "In X" prepositions are handled correctly.  Example:
    # "Can active memory replace attention? In Advances in Neural
    # Information Processing Systems, ..." has two levels of "In" —
    # the outer venue marker and the inner preposition.  We keep
    # stripping rightmost matches until no more are found, then return
    # the longest candidate with at least 3 words (the guard prevents
    # over-stripping into title prepositions like "A Study in Scarlet").
    candidates: list[str] = []
    working = before
    while True:
        best = None
        best_start = -1
        for marker in (r",?\s*[Ii]n\s*$", r",?\s*[Ii]n\s*\{?[A-Z0-9]"):
            matches = list(re.finditer(marker, working))
            if matches:
                start = matches[-1].start()
                if start > best_start:
                    best_start = start
                    best = working[:start].rstrip(" ,;")
        if best is None or best_start == -1:
            break
        if len(best.split()) >= 2:
            candidates.append(best)
        working = best
    if candidates:
        # Prefer the candidate with ≥3 words (safe from over-stripping),
        # choosing the *shortest* one — the most aggressively stripped
        # candidate that still passes the word-count guard.
        safe = [c for c in candidates if len(c.split()) >= 3]
        return min(safe if safe else candidates, key=len)
    # Fallback: " In {" / ": In " / ", in " before a capital-letter or
    # digit venue word (e.g. "in Proceedings", "in 2021 NeurIPS").
    matches = list(re.finditer(r",?\s+[Ii]n\s+\{?[A-Z0-9]", before))
    if matches:
        candidate = before[:matches[-1].start()].rstrip(" ,;")
        if len(candidate.split()) >= 2:
            return candidate
    return before.strip(" ,;")


# A title candidate sometimes swallows the venue tail because the venue has no
# leading period to split on (e.g. "Title? arXiv preprint arXiv:2102.05095").
# Trim a trailing preprint/venue marker so the title ends at the real boundary.
# A standalone "arXiv"/"CoRR" token starts the venue, whatever follows it
# ("preprint arXiv:...", ":1903.10520", or a bare ", 2020").  A real title
# essentially never contains these as words, so cut at the first one.
_TITLE_TAIL_RE = re.compile(r"\s+(?:arXiv|CoRR)\b", re.IGNORECASE)


def _trim_title_tail(title: str) -> str:
    title = _TITLE_TAIL_RE.split(title, maxsplit=1)[0]
    # Collapse the line-wrap whitespace PDF extraction leaves inside a title
    # ("Are\nwe done" -> "Are we done").
    title = re.sub(r"\s+", " ", title).strip()
    return title.rstrip(" ,;.")


def _title_after_initials(raw: str) -> str | None:
    """Recover a title glued to a Vancouver-style initial block.

    PDF text often leaves no sentence boundary between ``H. A.`` and a title.
    This is deliberately restricted to a title-like capital word (or A/An/The),
    so an ordinary ``J. Smith`` author continuation is not promoted to a title.
    """
    pattern = re.compile(
        r"(?:\b[A-Z]\.\s*){2,4}(?:et al\.\s*)?"
        r"(?P<title>(?:(?:A|An|The)(?:\s+[^.!?]+)?|[A-Z][a-z][^.!?]*?))\."
    )
    for match in pattern.finditer(raw):
        candidate = match.group("title").strip(" ,;")
        # ``V. Le, Ilya Sutskever, ...`` is still inside a long author
        # block, not a title boundary.  Surname-first text after an initial is
        # sufficiently distinct from the anonymous tranche titles to reject.
        if (len(candidate.split()) >= 3
                # ``B. T. Polyak and A. B. Juditsky`` can otherwise be
                # split after ``B. T.`` and promote ``Polyak and A`` to a
                # title. A trailing conjunction + initial is author-list
                # syntax, not a title-shaped phrase.
                and not re.search(r"\s+(?:and|&)\s+[A-Z]$", candidate)
                and not re.match(r"^[A-Z][a-zÀ-ÿ'’-]+,\s+[A-Z][a-zÀ-ÿ'’-]+", candidate)
                # ``S. M. Ali Eslami, and Aaron ...`` is an author-list
                # continuation, including when a PDF inserted a newline.
                and not re.match(
                    r"^[A-Z][a-zÀ-ÿ'’-]+\s+[A-Z][a-zÀ-ÿ'’-]+,\s+(?:and|&)",
                    candidate,
                    re.I,
                )):
            return _trim_title_tail(candidate)
    return None


def _is_title_fragment(part: str) -> bool:
    """Reject author/abbreviation debris before considering a prose title."""
    clean = part.strip(" ,;")
    if not clean or clean.lower() in {"et al", "et al."}:
        return False
    if clean.startswith(("&", ".", ",")):
        return False
    # A volume plus page range/e-locator is a venue tail, not a title.  It can
    # look title-like in abbreviated Nature footnotes, where keeping it would
    # prevent enrichment from the fuller end bibliography.
    if re.search(r"\b\d{1,4}\s*,\s*(?:e?\d+)(?:\s*[–—-]\s*\d+)?\b(?!\s*\+)", clean):
        return False
    if re.search(r"\bproject,?\s+(?:19|20)\d{2}\b", clean, re.I):
        return False
    return len(clean.split()) >= 2


# Nature-style lists join the last co-author with "&": "... Kumar, A. & Evans, B.
# Title...".  ``_split_prose_entry_parts`` masks the period after an initial only
# when a capital follows, and "&" is not one, so the split lands there and the
# part carrying the real title begins with the trailing co-author.  Dropping that
# co-author leaves the title, which is why this is stripped rather than the whole
# fragment being rejected as author debris.  The initial is required: "& Sons,
# Publishers" is debris and must stay rejected.
_TRAILING_COAUTHOR_RE = re.compile(r"^&\s+[A-ZÀ-Þ][\w'’\-]+,?\s+(?:[A-ZÀ-Þ]\.\s*)+")


def _strip_trailing_coauthor(part: str) -> str:
    stripped = _TRAILING_COAUTHOR_RE.sub("", part, count=1).strip(" ,;")
    return stripped or part


_PDF_RIGHTS_NOTICE_LINE_RE = re.compile(
    r"^[ \t]*All[ \t]+rights,[ \t]+including[ \t]+for[ \t]+text[ \t]+and[ \t]+"
    r"data[ \t]+mining,[ \t]+AI[ \t]+training,[ \t]+and[ \t]+similar[ \t]+"
    r"technologies,[ \t]+are[ \t]+reserved\.[ \t]*(?:\r?\n|$)",
    re.MULTILINE,
)


def _strip_pdf_rights_notice_for_title(raw: str) -> str:
    """Remove the exact standalone PDF notice from the title working copy."""
    return _PDF_RIGHTS_NOTICE_LINE_RE.sub("", raw)


_SOFT_HYPHEN_BREAK_RE = re.compile(r"([a-zà-ÿ])-(?: |\r?\n)([a-zà-ÿ])")


def _join_soft_hyphen_breaks(raw: str) -> str:
    """Rejoin a word the PDF broke across lines: "classi- fiers" -> "classifiers".

    Text extraction turns an end-of-line hyphen into "hyphen space", and the
    fragment survives into the title, where it defeats every lookup: no index holds
    "Regularization strategy to train strong classi- fiers".  73 of the corpus's 941
    extracted titles carry one.

    Only lowercase-to-lowercase joins.  A hyphen that falls at a line break but
    genuinely belongs to the word keeps its capital ("Third- Party"), so requiring
    lowercase on both sides leaves real compounds alone.  The trade is deliberate
    and not free: a lowercase compound broken exactly at its own hyphen
    ("public- trust") is joined wrongly.  That is rarer than the artefact, and it
    costs a lookup that the unjoined form was going to fail anyway.

    Applies to the working copy used for extraction only — ``raw_entry`` keeps what
    the document prints, because it is the evidence the audit rests on.
    """
    return _SOFT_HYPHEN_BREAK_RE.sub(r"\1\2", raw)


_STRICT_AIP_QUOTED_TITLE_RE = re.compile(
    r"^\s*(?P<authors>[A-Z]\.\s*[^\"“”\r\n]+?),\s*"
    r"(?:“(?P<curly_title>[^\"“”\r\n]+?),”|\"(?P<ascii_title>[^\"“”\r\n]+?),\")\s*"
    r"(?P<venue>[A-Za-z][A-Za-z .,:;()&'’\-]*?)\s+"
    r"\d+\s*,\s*\d+\s*\((?:19|20)\d{2}\)\.?\s*$"
)

_VANCOUVER_UNDOTTED_INITIAL_TITLE_RE = re.compile(
    r"^\s*(?:[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\s+[A-Z]{1,4},\s+){1,20}"
    r"[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\s+[A-Z]{1,4}\.\s+"
    r"(?P<title>.+?)\.\s+"
    r"(?P<venue>[A-Z][A-Za-z& ]{2,80})\.\s+"
    r"\((?:19|20)\d{2}[a-z]?\)\s+\d{1,4}\s*:\s*"
    r"[A-Za-z]?\d+(?:\s*[–—-]\s*\d+)?\.?\s*$"
)


def _extract_vancouver_undotted_initial_title(raw: str) -> str | None:
    """Extract a compound title after a comma-separated undotted author list."""
    match = _VANCOUVER_UNDOTTED_INITIAL_TITLE_RE.match(raw or "")
    return _trim_title_tail(match.group("title")) if match else None


_VANCOUVER_TERMINAL_INITIAL_AUTHOR_RE = (
    r"[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]*"
    r"(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]*)*\s+[A-Z]{1,4}"
    r"(?:\s+(?:Jr|Sr|II|III))?"
)
_VANCOUVER_TERMINAL_INITIAL_HEAD_RE = re.compile(
    r"^\s*(?:" + _VANCOUVER_TERMINAL_INITIAL_AUTHOR_RE + r",\s+)+"
    r"(?:et\s+al\.\s+|" + _VANCOUVER_TERMINAL_INITIAL_AUTHOR_RE + r"\.\s+)"
)


def _extract_vancouver_terminal_initial_title(raw: str) -> str | None:
    """Recover a title after a Vancouver author block ending in an initial.

    The prose splitter deliberately protects ``J. Smith``-style author
    continuations.  In ``Smith J, Doe R. Title. Journal.``, that protection
    also masks the terminal author initial and combines it with the title,
    making the journal appear to be the first title-shaped part.  This narrow
    complete-shape extractor retains the title directly from the raw entry.
    """
    match = _VANCOUVER_TERMINAL_INITIAL_HEAD_RE.match(raw or "")
    if not match:
        return None
    tail = raw[match.end():]
    multi_sentence = re.match(
        r"(?P<title>.+)\.\s+[A-Z][A-Za-z .&'’()\-]{1,100}?\.\s+"
        r"(?:19|20)\d{2};\d{1,4}(?:\([^)]*\))?:[A-Za-z]?\d+(?:[-–—]\d+)?\.?\s*$",
        tail,
    )
    if multi_sentence:
        title = multi_sentence.group("title").strip()
        return _trim_title_tail(title) if len(title.split()) >= 2 else None
    question = re.match(r"(?P<title>[^.\r\n]*?[?!])(?:\s+|$)", tail)
    if question:
        title = question.group("title").strip()
        return title if len(title.split()) >= 2 else None
    sentence = re.match(r"(?P<title>[^.\r\n]+?)\.\s+", tail)
    if not sentence:
        return None
    title = sentence.group("title").strip()
    return _trim_title_tail(title) if len(title.split()) >= 2 else None


_CUREUS_AUTHOR_TITLE_RE = re.compile(
    r"^\s*[^:\r\n]+:\s+(?P<title>[\s\S]+?)\.\s+"
    r"[A-Z][A-Za-z .&'’()\-]{1,100}?\.\s+(?:19|20)\d{2},\s+"
    r"\d{1,4}(?:\s+Suppl\s+\d+)?:[A-Za-z]?\d+"
    r"(?:\s*[-–—]\s*\d+)?\."
)


def _extract_cureus_title(raw: str) -> str | None:
    """Extract a title from Cureus' closed ``Authors: Title. Journal.`` shape."""
    match = _CUREUS_AUTHOR_TITLE_RE.match(raw or "")
    if not match:
        return None
    title = match.group("title").strip()
    return _trim_title_tail(title) if len(title.split()) >= 2 else None


_APA_QUESTION_JOURNAL_TAIL_RE = re.compile(
    r"^\s*[\s\S]*?\((?:19|20)\d{2}[a-z]?\)\.\s+"
    r"(?P<title>[\s\S]*?[?!])\s+[A-Z][A-Za-z .&'’()\-\r\n]{1,100}?,\s+"
    r"\d{1,4}(?:\([^)]*\))?,\s+\d+(?:[-–—]\d+)?\.\s*$"
)


def _extract_apa_question_journal_title(raw: str) -> str | None:
    """Extract an APA question title only with its complete journal tail."""
    match = _APA_QUESTION_JOURNAL_TAIL_RE.match(raw or "")
    if not match:
        return None
    title = match.group("title").strip()
    return _trim_title_tail(title) if len(title.split()) >= 2 else None


def _extract_strict_aip_quoted_title(raw: str) -> str | None:
    """Extract a quoted AIP title only from its complete citation shape."""
    match = _STRICT_AIP_QUOTED_TITLE_RE.match(raw)
    if not match:
        return None
    title = (match.group("curly_title") or match.group("ascii_title")).strip()
    return title if len(title.split()) >= 2 else None


_EDITED_ORIGINAL_WORK_TITLE_RE = re.compile(
    r"^\s*[^\r\n]+?\.\s*\((?:19|20)\d{2}[a-z]?\),\s*"
    r"in\s+[^\r\n]+?\s+\(Ed\.\),\s*"
    r"(?P<title>[^,\r\n]+?)\s*,\s*"
    r"\d+(?:st|nd|rd|th)\s+anniversary\s+ed\.\s*,\s*"
    r"[^,\r\n]+\s*,\s*[^()\r\n]+?\s*"
    r"\(Original work published (?:19|20)\d{2}\)\.\s*$"
)


def _extract_edited_original_work_title(raw: str) -> str | None:
    """Extract the title from a complete edited-original-work book shape."""
    match = _EDITED_ORIGINAL_WORK_TITLE_RE.match(raw)
    if not match:
        return None
    title = match.group("title").strip()
    return title if len(title.split()) >= 2 else None


_CHICAGO_HANGING_HEAD_RE = (
    r"^\s*[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*)*,\s+[A-Z][^0-9:]*?\s+"
    r"(?:\[(?:19|20)\d{2}\]\s*)?(?:19|20)\d{2}\s+"
)
_CHICAGO_HANGING_QUOTED_CHAPTER_RE = re.compile(
    _CHICAGO_HANGING_HEAD_RE
    + r"(?:“(?P<curly_inside>[^\"“”\r\n]+?)\.”|“(?P<curly_outside>[^\"“”\r\n]+?)”\."
    r"|\"(?P<ascii_inside>[^\"“”\r\n]+?)\.\"|\"(?P<ascii_outside>[^\"“”\r\n]+?)\"\.)\s+"
    r"In\s+.+?,\s*\d+\s*[–-]\s*\d+\.\s+[A-Z][^:]*:\s+[^.]+\.?\s*$"
)
_CHICAGO_HANGING_QUOTED_DATE_RE = re.compile(
    _CHICAGO_HANGING_HEAD_RE
    + r"(?:“(?P<curly_inside>[^\"“”\r\n]+?)\.”|“(?P<curly_outside>[^\"“”\r\n]+?)”\."
    r"|\"(?P<ascii_inside>[^\"“”\r\n]+?)\.\"|\"(?P<ascii_outside>[^\"“”\r\n]+?)\"\.)\s+"
    r"[A-Za-z][A-Za-z .&'’-]*,\s+"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"\d{1,2}\.(?:\s+https?://\S+)?\s*$"
)
_CHICAGO_HANGING_BOOK_RE = re.compile(
    _CHICAGO_HANGING_HEAD_RE
    + r"(?P<title>[^.]+?)\.\s+[A-Z][^:]*:\s+[^.]+\.?\s*$"
)
_CHICAGO_HANGING_TRANSLATED_BOOK_RE = re.compile(
    _CHICAGO_HANGING_HEAD_RE
    + r"(?P<title>[^.]+?)\.\s+Translated by\s+[A-Z][^.]*\.\s+"
    r"[A-Z][^:]*:\s+[^.]+\.?\s*$"
)


def _extract_chicago_hanging_title(raw: str) -> str | None:
    """Extract titles only from complete Chicago hanging-author citation shapes."""
    for pattern in (
        _CHICAGO_HANGING_QUOTED_CHAPTER_RE,
        _CHICAGO_HANGING_QUOTED_DATE_RE,
    ):
        match = pattern.match(raw)
        if match:
            title = next(
                (match.group(name) for name in (
                    "curly_inside", "curly_outside", "ascii_inside", "ascii_outside",
                ) if match.group(name)),
                "",
            ).strip()
            return title if len(title.split()) >= 2 else None
    for pattern in (
        _CHICAGO_HANGING_TRANSLATED_BOOK_RE,
        _CHICAGO_HANGING_BOOK_RE,
    ):
        match = pattern.match(raw)
        if match:
            title = match.group("title").strip()
            if ", edited by" not in title.lower() and len(title.split()) >= 2:
                return title
    return None


def _extract_title(raw: str, *, chicago_hanging: bool = False) -> str | None:
    """Best-effort title extraction from a prose bibliography entry."""
    if not raw:
        return None
    raw = _strip_pdf_rights_notice_for_title(raw)
    raw = _join_soft_hyphen_breaks(raw)
    if chicago_hanging:
        chicago_title = _extract_chicago_hanging_title(raw)
        if chicago_title:
            return chicago_title
    aip_title = _extract_strict_aip_quoted_title(raw)
    if aip_title:
        return aip_title
    edited_original_work_title = _extract_edited_original_work_title(raw)
    if edited_original_work_title:
        return edited_original_work_title
    vancouver_title = _extract_vancouver_undotted_initial_title(raw)
    if vancouver_title:
        return vancouver_title
    vancouver_terminal_initial_title = _extract_vancouver_terminal_initial_title(raw)
    if vancouver_terminal_initial_title:
        return vancouver_terminal_initial_title
    cureus_title = _extract_cureus_title(raw)
    if cureus_title:
        return cureus_title
    apa_question_title = _extract_apa_question_journal_title(raw)
    if apa_question_title:
        return apa_question_title
    # PLOS / numbered-Vancouver: "Authors (YYYY) Title. Venue Vol: pp."  Unlike
    # APA ("Authors (YYYY). Title.") there is NO period after the year, so the
    # author block and the title share one dotted segment and the generic path
    # below would mistake the venue for the title.  A ")" followed directly by
    # whitespace + a capital (never a period, which would be APA) marks this
    # style; the title is the sentence right after the year.
    m = re.search(r"\((?:19|20)\d{2}[a-z]?\)\s+(?=[A-Z])", raw)
    if m:
        tail_parts = _split_prose_entry_parts(raw[m.end():])
        if tail_parts:
            candidate = tail_parts[0].strip()
            if (
                len(candidate.split()) >= 2
                and not re.fullmatch(
                    r"[A-Za-z][\w.-]*\s+\d{1,4}\s*:\s*[A-Za-z]?\d+(?:[-–—]\d+)?\.?",
                    candidate,
                )
            ):
                return _trim_title_tail(candidate)
    initial_title = _title_after_initials(raw)
    if initial_title:
        return initial_title
    # Corporate entries can omit the author/title period entirely, e.g.
    # ``Clarivate, Journal Citation Reports ... (Philadelphia: ..., 2025)``.
    # Restrict this to a single-word corporate author to avoid mistaking a
    # Chicago personal name for the title.
    m = re.match(
        r"^\s*[A-Z][\w&'-]+,\s+(?P<title>[^()]{8,}?)\s*\([^)]*\b(?:19|20)\d{2}\b",
        raw,
    )
    if m:
        candidate = m.group("title").strip()
        if len(candidate.split()) >= 3 and not re.match(r"^[A-Z]\.", candidate):
            return _trim_title_tail(candidate)
    # Strip LaTeX formatting to get clean tokens
    parts = _split_prose_entry_parts(raw)
    # The typical pattern is: Authors. Title. Venue, Year.
    # Skip the first segment (author block), check the next 1-2 segments.
    for part in parts[1:5]:
        part = _strip_trailing_coauthor(part)
        # ``(in press)``/``(forthcoming)``/``(n.d.)`` can be the first
        # segment after the author block.  They are dates, never titles.
        if re.fullmatch(
            r"\(?\s*(?:in\s+press|forthcoming|n\.\s*d\.)\s*\)?[.]?",
            part, re.IGNORECASE,
        ):
            continue
        if not _is_title_fragment(part):
            continue
        toks = part.split()
        if len(toks) >= 2 and not re.search(r"\b(?:19|20)\d{2}\b", part):
            return _trim_title_tail(part)
        # Part contains a year but may also contain the title before the year.
        # Try to extract the title portion before the first year occurrence.
        if len(toks) >= 4:
            title_part = _split_before_year(part)
            if title_part and len(title_part.split()) >= 2:
                return _trim_title_tail(title_part)
    return None


# A journal's submission history — "Received June 13, 2005 … Accepted March 28,
# 2006" — is printed at the foot of the article, and the extractor can leave it
# hanging past the last reference, where the splitter reads it as one more entry
# ("San Diego, CA: Academic Press. Received …", filed under "diego") that then
# shows up as a reference nobody cites.  No real reference carries a
# Received-then-Accepted date pair, so an entry that does is the footer, not a
# source.
_SUBMISSION_FOOTER_RE = re.compile(
    r"\bReceived\b[\s\S]{0,80}?\b(?:1[6-9]|20)\d{2}\b[\s\S]{0,140}?"
    r"\b(?:Accepted|Revision\s+received)\b",
    re.IGNORECASE)


def _is_submission_footer(raw: str) -> bool:
    return bool(_SUBMISSION_FOOTER_RE.search(raw or ""))


# A line wrap inside a DOI leaves a space the extractor cannot see past, so the
# DOI comes out truncated ("10.1093/be" for "10.1093/beheco/art050") or missing
# altogether when the break falls right after the slash ("10.1525/ tran...").
# Both are unresolvable, and the reference is then reported as unverified even
# though it is an ordinary indexed article.
#
# Re-joining is only safe where the continuation cannot be something else, because
# gluing a following token onto a complete DOI would forge an identifier that may
# itself resolve — to the wrong work. Two things follow a finished DOI in practice:
# prose ("... 104454 SUPPLEMENT.", "... 0330046. URL"), always capitalised, and a
# page range or year ("10.1234/abcxyz 12-34", "... 2013 study of"). Both are
# excluded below, and how much structure the continuation must show depends on
# whether the head itself already looks finished.
_DOI_HEAD_RE = re.compile(r"10\.\d{4,9}/\S*")
# A continuation may also begin with the DOI-internal "." when the wrap fell
# just before it ("10.1016/j.clgc .2017.10.006"); ``_doi_continuation`` gates it.
_DOI_CONT_RE = re.compile(r"[.a-z0-9(][^\s]*")
_DOI_REGISTRANT_WRAP_RE = re.compile(
    r"(?P<head>(?:\bdoi\s*:\s*|https?://(?:(?:www|dx)\.)?doi\.org/)10\.)"
    r"(?P<left>\d{1,8})[ \t\r\n]+(?P<right>\d{1,8})(?=/)",
    re.IGNORECASE,
)
_DOI_RESOLVER_HOSTS = frozenset(
    {"doi.org", "www.doi.org", "dx.doi.org", "www.dx.doi.org"}
)

# A PDF wrap can also split a short word inside an ordinary URL path
# (".../how-wo mens-body" -> ".../how-womens-body").  Keep this deliberately
# narrower than a general whitespace join: both fragments must be lowercase and
# short, and the continuation must immediately retain path structure.  The
# combined-word length gate rejects ordinary prose following a short URL path.
_SHORT_MIDPATH_WRAP_RE = re.compile(
    r"(?P<prefix>https?://[^\s)]*(?:/|-))"
    r"(?P<left>[a-z]{1,3})[ \t\r\n]+"
    r"(?P<right>[a-z]{1,5})(?P<tail>[-/][\w./#?=&%~+\-]+)"
)


def _rejoin_short_midpath_wrap(text: str) -> str:
    """Repair only tightly bounded, short-word URL path wraps."""
    def replace(match: re.Match) -> str:
        word = match.group("left") + match.group("right")
        if not 3 <= len(word) <= 8:
            return match.group(0)
        return match.group("prefix") + word + match.group("tail")

    return _SHORT_MIDPATH_WRAP_RE.sub(replace, text)


def _rejoin_wrapped_doi_registrant(text: str) -> str:
    """Repair one line wrap inside a DOI registrant in explicit DOI context."""
    def replace(match: re.Match) -> str:
        registrant = match.group("left") + match.group("right")
        if 4 <= len(registrant) <= 9:
            return match.group("head") + registrant
        return match.group(0)

    return _DOI_REGISTRANT_WRAP_RE.sub(replace, text)


def _complete_truncated_doi_url(url: str, doi: str | None, citation_context: str) -> str:
    """Complete a DOI URL only when its path is a prefix of the same DOI."""
    if not doi or doi.count("(") != doi.count(")"):
        return url
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return url
    if parsed.scheme.lower() not in {"http", "https"} or host not in _DOI_RESOLVER_HOSTS:
        return url
    observed = urllib.parse.unquote(parsed.path.lstrip("/")).rstrip(".,;>")
    if not observed or observed.casefold() == doi.casefold():
        return url
    if not doi.casefold().startswith(observed.casefold()):
        return url
    if urllib.parse.unquote(citation_context).casefold().count(observed.casefold()) != 1:
        return url
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            "/" + urllib.parse.quote(doi, safe="/"),
            parsed.query,
            parsed.fragment,
        )
    )


def _doi_continuation(head: str, cont: str) -> bool:
    """Whether ``cont`` is the rest of a DOI broken by a line wrap."""
    if cont.startswith("."):
        # The wrap split immediately before a DOI-internal period, so the head
        # ("…/j.clgc", "…/jmor", "…/rsbl") is unfinished and the period is its
        # separator.  Accept a multi-group numeric suffix (".2017.10.006",
        # ".2015.0694", ".20533-5") or a pure-numeric run too long to be a year
        # (".20272"); a bare wrapped year (". 2013 study of …") carries neither
        # and is left alone.
        return bool(re.search(r"\d[.\-]\d", cont) or re.search(r"\.\d{5,}", cont))
    if not re.search(r"\d", cont):
        return False                      # prose after a DOI carries no digits
    if head.endswith("/"):
        return True                       # a DOI suffix is never empty
    if "/" in cont:
        return True                       # prose words and page ranges have no slash
    if re.search(r"\d{7,}", cont):
        return True                       # no page range runs to seven digits
    if re.match(r"[a-z]", cont):
        return True                       # wrap inside a word: "heco/art050", "beh.2023..."
    if head.endswith((")", ".", "-", ":")):
        # The head breaks off on a DOI-internal separator, so it is unfinished
        # whatever follows: accept a continuation carrying digit structure
        # ("10.1016/S1470-2045(17) 30891-4"). A bare year still fails this, which
        # is what keeps "...10.x/y. 2013 study of ..." intact.
        return bool(re.search(r"\d[.\-]\d", cont))
    # Head ends on an alphanumeric, so it may well be complete. Require an
    # internal period ("8.022") — that separates a broken suffix from a page
    # range, whose separator is a hyphen ("10.1234/abcxyz 12-34").
    return bool(re.search(r"\d\.\d", cont))


def _rejoin_wrapped_doi(text: str) -> str:
    """Undo a line wrap that split a DOI, leaving anything after a DOI untouched."""
    out = text
    for _ in range(4):  # a DOI can be broken more than once; converges quickly
        joined = _rejoin_wrapped_doi_once(out)
        if joined == out:
            break
        out = joined
    return out


def _rejoin_wrapped_doi_once(text: str) -> str:
    match = _DOI_HEAD_RE.search(text)
    while match:
        end = match.end()
        rest = text[end:]
        space = len(rest) - len(rest.lstrip(" \t"))
        if space:
            cont = _DOI_CONT_RE.match(rest[space:])
            head = match.group(0)
            if cont and not head.endswith((",", ";")) and _doi_continuation(head, cont.group(0)):
                return text[:end] + rest[space:]
        match = _DOI_HEAD_RE.search(text, match.start() + 1)
    return text


def _make_reference(num: int, raw: str, *, chicago_hanging: bool = False) -> dict:
    # Run storage canonicalises text to NFC. Coordinate offsets must therefore
    # be derived from that same representation or a decomposed character before
    # the coordinate shifts every persisted raw span.
    raw = unicodedata.normalize("NFC", raw)
    doi = None
    # PDF extraction often inserts spaces around hyphens within DOIs that
    # span lines: "10.1038/s41586 - 022 - 05543 - x" → "10.1038/s41586-022-05543-x"
    _clean = re.sub(r"\s*[–—-]\s*", "-", raw)
    # Some editors insert invisible Unicode format controls into DOI URLs to
    # suppress automatic linking.  Remove those controls only in the extraction
    # view: ``raw_entry`` remains the exact citation supplied by the user.
    _clean = "".join(char for char in _clean if unicodedata.category(char) != "Cf")
    # A Wiley SICI DOI carries "<"/">"; PDF extraction can mangle them into the
    # look-alike "≤"/"≥" ("…257:2≤50::AID-AR4≥3.0.CO;2-W"), leaving a DOI that
    # doi.org cannot resolve.  Restore them before the DOI is read.  Confined to
    # _clean (which only feeds DOI extraction), so a genuine "p ≤ 0.05" elsewhere
    # in the entry is untouched.
    _clean = _clean.replace("≤", "<").replace("≥", ">")
    _clean = _rejoin_wrapped_doi_registrant(_clean)
    _clean = _rejoin_wrapped_doi(_clean)
    m = re.search(r"10\.\d{4,9}/\S+", _clean)
    if m:
        # ">" closes an OSCOLA "<https://doi.org/…>" wrap and is never DOI-final.
        doi = m.group(0).rstrip(".,;>")
    # Fallback: extract arXiv ID as DOI (e.g. "arXiv:1607.06450" → "10.48550/arXiv.1607.06450")
    if doi is None:
        doi = _extract_arxiv_doi(raw)
    title = _extract_title(raw, chicago_hanging=chicago_hanging)
    pmid = None
    m = re.search(r"PMID:?\s*(\d+)", raw, re.IGNORECASE)
    if m:
        pmid = m.group(1)
    isbn = None
    m = re.search(r"ISBN[:\s]*([\d\-Xx]{10,17})", raw)
    if m:
        isbn = m.group(1)
    url = None
    # Use cleaned text for URL too (same PDF line-break artifacts in URLs).
    # A wrap can split a URL after a path separator, e.g.
    # "github.com/rwightman/ pytorch-image-models": the extractor stops at the
    # space and keeps the wrong page ("…/rwightman/"). Re-join the space only when
    # the URL ends on "/" or "-" AND the continuation is a lowercase path-like
    # token, so a URL trailed by prose ("… /foo. Accessed 2020") is left intact.
    # A hyphen the typesetter inserted to justify the line, falling inside the
    # HOST: "https://www.ac- cessnow.org" is accessnow.org, not ac-cessnow.org, and
    # the second does not resolve.  Confined to the host ("[^\s)/]*" cannot cross
    # the first "/") because in a path the hyphen is usually part of the slug —
    # "…/northern-ontario- 1101895" is one identifier and must keep its hyphen.
    # Read from *raw*, not from _clean: _clean has already collapsed "- " to "-",
    # which is exactly the evidence that the break was there.
    _url_src = re.sub(r"(https?://[^\s)/]*)-\s+([a-z0-9][^\s)]*)", r"\1\2", raw)
    # Join only a scheme-to-host wrap when the next token is a dotted DNS host
    # with an alphabetic TLD; this excludes prose, localhost, and IPv4.
    _url_src = re.sub(
        r"(https?://)[ \t]+(?=[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}"
        r"(?=[:/?#\s)]|$))",
        r"\1",
        _url_src,
    )
    _url_src = re.sub(r"\s*[–—-]\s*", "-", _url_src)  # as _clean, for path hyphens
    _url_clean = re.sub(
        r"(https?://[^\s)]*[/-])\s+([a-z0-9][\w./#?=&%~+-]*)", r"\1\2", _url_src)
    # A wrap may instead fall immediately before the next slash-delimited path
    # segment (".../postwar-world /681745").  Requiring the continuation itself
    # to start with "/" avoids folding ordinary prose following a URL into it.
    _url_clean = re.sub(
        r"(https?://[^\s)]*[a-z0-9])\s+(/[a-z0-9][\w./#?=&%~+-]*)",
        r"\1\2",
        _url_clean,
        flags=re.IGNORECASE,
    )
    _url_clean = _rejoin_short_midpath_wrap(_url_clean)
    # The same wrap also lands either side of a dot in the HOST, which the rule
    # above cannot see because the URL then ends on "." or on a letter:
    # "https://www. tbnewswatch.com/…" and "https://avalon.law.yale .edu/…" both
    # truncate to a bare "https://www" / "https://avalon.law.yale" and the fetch
    # fails on a host that does not exist.  Each continuation must carry a dotted
    # TLD of its own, so prose following a URL-final full stop ("…/foo. Accessed
    # 2020", "…/foo. see also") still fails the test and is left alone.
    _url_clean = re.sub(
        r"(https?://[^\s)]*\.)\s+([a-z0-9-]+\.[a-z]{2,}[\w./#?=&%~+-]*)", r"\1\2", _url_clean)
    _url_clean = re.sub(
        r"(https?://[^\s)]*[a-z0-9])\s+(\.[a-z]{2,6}/[\w./#?=&%~+-]*)", r"\1\2", _url_clean)
    # The wrap can also fall mid-path, where neither host rule sees it: the break
    # is not around a dot and the continuation may be capitalised
    # ("…/LON/PA RTII-16.en.pdf" -> truncated "…/LON/PA") or a bare TLD
    # ("search.itu. int/…/x.pdf" -> "https://search.itu").  When the tail is
    # unmistakably a filename — no space and a document extension — it is the rest
    # of the link, not the next sentence: prose never ends in ".pdf".  A URL-final
    # full stop followed by prose ("…/foo. Accessed 2020") carries no such tail and
    # is still left alone.
    _url_clean = re.sub(
        r"(https?://[^\s)]*)\s+([^\s)]*\.(?:pdf|html?|aspx?|php|docx?))\b",
        r"\1\2", _url_clean)
    m = re.search(r"https?://[^\s)]+", _url_clean)
    if m:
        # Bluebook wraps an archival mirror in brackets ("[https://perma.cc/AB12-CD34]");
        # the closing one is not part of the link.  ")" cannot appear here — the
        # pattern above already stops before it.
        url = m.group(0).rstrip(".,;]>»")
        url = _complete_truncated_doi_url(url, doi, raw)
    year = None
    # An arXiv identifier ("arXiv:1903.10520") leads with a YYYY-shaped group that
    # is NOT a year; drop such id tokens before scanning so we don't read 1903 as
    # the year of a 2019 paper.  The YYYY.NNNNN shape is unique to arXiv ids —
    # real page ranges use "-"/":" separators, never a dot between 4 and 5 digits.
    _year_src = re.sub(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b", " ", raw)
    # A project/initiative number is not a publication date.  Keep this narrow:
    # it handles the known ``Project, 2050`` corporate contributor without
    # changing ordinary author-year citations.
    _year_src = re.sub(r"\bProject,?\s+(?:19|20)\d{2}\b", "Project", _year_src, flags=re.I)
    for pat in (
        r"\b((?:19|20)\d{2})[a-z]?\s*;",
        r"\b((?:19|20)\d{2})[a-z]?\s*,\s*(?:vol\.|volume)\b",
    ):
        m = re.search(pat, _year_src, re.IGNORECASE)
        if m:
            year = int(m.group(1))
            break
    if year is None:
        # The optional author-year disambiguator is metadata, not part of the
        # numeric publication year.  Do not require a word boundary immediately
        # after the digits: it would discard ``2020a`` altogether.
        years = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})[a-z]?\b", _year_src)]
        if years:
            year = years[0]  # first year in the entry, before PDF bleed appends unrelated years

    classification = _classify_reference(raw, doi=doi, pmid=pmid, isbn=isbn, url=url)

    reference = {
        "id": f"ref-{uuid.uuid4().hex[:10]}",
        "ref_number": num,
        "raw_entry": raw,
        "source_type": classification["source_type"],
        "source_kind": classification["source_kind"],
        "source_type_confidence": classification["source_type_confidence"],
        "source_type_evidence": classification["source_type_evidence"],
        "indexability": classification["indexability"],
        "doi": doi,
        "pmid": pmid,
        "isbn": isbn,
        "issn": None,
        "url": url,
        "title": title,
        "year": year,
        "fulltext_status": "pending",
        "provided_source": None,
        "accessed_at": None,
    }
    cited_coordinates = extract_cited_coordinates(raw, title)
    if cited_coordinates:
        reference["cited_coordinates"] = cited_coordinates
    return reference


# ---------------------------------------------------------------------------
# Default format-handler hooks (shared by all format families unless
# overridden).  These are the current implementations from parse_manuscript,
# moved verbatim so that the format_handlers.Family facade can route to them.
#
# Each hook accepts an optional *meta* dict (extract metadata) so that
# format-specific overrides can inspect the source format, OCR status, etc.
# The default implementations ignore it.
# ---------------------------------------------------------------------------


def _clean_biblio_line(line: str) -> str:
    line = " ".join((line or "").split())
    line = re.sub(r"\s+([,.;:])", r"\1", line)
    line = re.sub(r"\(\s+", "(", line)
    line = re.sub(r"\s+\)", ")", line)
    # PDF extraction sometimes spaces out URLs character by character.
    line = re.sub(r"h\s*t\s*t\s*p\s*s?\s*:\s*/\s*/", "https://", line, flags=re.I)
    line = re.sub(r"w\s*w\s*w\s*\.", "www.", line, flags=re.I)
    line = re.sub(r"\b(Publisher's Note|Publisher.s Note)\b.*$", "", line).strip()
    return line


# A running header a two-column PDF interleaves between reference entries, e.g.
# "Published as a conference paper at ICLR 2021".  No real reference starts this
# way, so treating it as noise lets the boundary splitter see the period → next
# author transition it would otherwise straddle.
_RUNNING_HEADER_RE = re.compile(
    r"(?:published|accepted|submitted|under\s+review)\s+as\s+a\s+"
    r"(?:conference|workshop)\s+paper\b",
    re.IGNORECASE,
)

# "Further reading" and its kin: a heading, so no entry may swallow it.
_FURTHER_READING_RE = parser_text.further_reading_heading_re()


def _is_biblio_noise_line(line: str) -> bool:
    if not line:
        return True
    if _FURTHER_READING_RE.match(line):
        return True
    low = line.lower()
    if low in {"retracted article", "1 3", "institutional affiliations."}:
        return True
    # A standalone number is a page-footer artefact (1-2 digits for short papers,
    # 3-5 for longer ones); a real reference is never reduced to bare digits.
    if re.fullmatch(r"\d{1,5}", line):
        return True
    if _RUNNING_HEADER_RE.match(line):
        return True
    if low.startswith("publisher's note") or low.startswith("publisher.s note"):
        return True
    if low.startswith("springer nature or its licensor"):
        return True
    if low == "authors and affiliations":
        return True
    return False


def _is_biblio_tail_line(line: str) -> bool:
    low = line.lower()
    return (
        low.startswith("publisher's note")
        or low.startswith("publisher’s note")
        or low.startswith("publisher.s note")
        or low.startswith("springer nature or its licensor")
        or low == "authors and affiliations"
        or low == "how to cite this article"
        or low.startswith("how to cite this article:")
    )


# The bare initials CSE and Vancouver set after a surname — "Bedford JM", "Lachance
# M-A".  A compound forename keeps its hyphen there, so the cluster is not simply a run
# of capitals.
_INITIAL_CLUSTER = r"[A-Z]{1,4}(?:-[A-Z]{1,4})*"


def _looks_authoryear_ref_start(line: str, next_line: str = "") -> bool:
    """APA-ish reference start for non-numbered bibliographies.

    *next_line* (when provided) allows year lookahead for PDF text where the
    author names and year land on different lines."""
    if _is_biblio_noise_line(line):
        return False
    # Chicago NB entry start: "Surname, Firstname" — may not have the year
    # on the same line due to PDF line wrapping.
    if re.match(r"^[A-Z][a-zà-ÿ]+(?:[-'][A-Z][a-zà-ÿ]+)?,\s+(?:[A-Z]\.\s+)*[A-Z][a-zà-ÿ]", line):
        return True
    # Organizational author with period: "Clarivate. Journal Title..." or
    # "European Research Council. "Grants..."  — must contain a year.
    if re.match(r"^[A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+)*\.\s+[A-Z\"'(]", line):
        if re.search(rf"\b{_AY_YEAR}\b", line):
            return True
    # Skip publisher-location continuation lines: "Cambridge, MA: MIT Press, 1996."
    if re.match(r"^[A-Z][a-zÀ-ÿ]+,\s*[A-Z]{2}\b", line):
        return False
    # Year can be on the current line OR the next line (PDF line wrapping).
    year_here = re.search(rf"\({_AY_YEAR}\)|\b{_AY_YEAR}\b", line[:180])
    year_next = bool(next_line) and bool(
        re.search(rf"\b{_AY_YEAR}\b", next_line[:80]))
    if not year_here and not year_next:
        # Even without a year nearby, a strong author-list pattern on this
        # line signals a reference start: "Firstname Surname, Othername..."
        if re.match(r"^[A-Z][a-zà-ÿ]+ [A-Z][a-zà-ÿ'\-]+, [A-Z]", line):
            return True
        return False
    if re.match(r"^[A-Z](?:'[A-Z])?[^\W\d_][\w'’.-]*(?:\s+[A-Z]\.)?,", line):
        return True
    if re.match(r"^[A-Z][^\W\d_][\w'’.-]*(?:\s+[A-Z][^\W\d_][\w'’.-]*){1,3},\s*[A-Z]\.", line):
        return True
    if re.match(r"^[A-Z][^\W\d_][\w'’.-]*(?:\s+[A-Z][^\W\d_][\w'’.-]*){0,4}\s*\(", line):
        return True
    if re.match(
        r"^[A-Z][^\W\d_][\w'’.-]*(?:\s+(?:of|and|the|for|in|on|[A-Z][^\W\d_][\w'’.-]*)){1,7}"
        rf"\.?\s*\({_AY_YEAR}\)",
        line,
    ):
        return True
    if re.match(r"^[A-Z][^\W\d_][\w'’.-]*(?:\s+&\s+|\s+and\s+)[A-Z]", line):
        return True
    if re.match(r"^[A-Z][^\W\d_][\w'’.-]*(?:,\s*[A-Z]\.)+(?:,\s*&\s*[A-Z]|,\s*[A-Z])", line):
        return True
    # CSE / Vancouver name-year, where the initials follow the surname BARE — no
    # period between them, no comma before them: "Adkins-Regan E. 1990.", "Bedford
    # JM, Mock OB, Goodman SM. 2004."  Every rule above expects the initials to be
    # punctuated ("Bedford, J. M.") and so reads that entry as prose: the whole
    # bibliography then has one recognisable start in 290 lines, and its references
    # merge into each other by the dozen.  The bare cluster is the signature — a
    # title or a venue never stands a run of capitals where a forename would go.
    # The initial of a compound forename is compound too: Marc-André is "M-A", and
    # "Lachance M-A, Burke C, …" opens a reference exactly as "Bedford JM," does.
    # Read without the hyphen, the cluster is no initial, the line is no start, and the
    # whole reference is swallowed by the entry above it.
    if re.match(r"^[A-Z][^\W\d_][\w'’-]*(?:[- ][A-Z][^\W\d_][\w'’-]*)?\s+" + _INITIAL_CLUSTER + r"[,.]", line):
        return True
    return False


def _merge_hyphen_wraps(text: str) -> str:
    return re.sub(r"([A-Za-z])-\s+([A-Za-z])", r"\1\2", text)


# A numbered reference-entry start, across publisher styles:
#   "[12] " | "[12]. " | "12. " | "12) "  (IEEE / Vancouver / AMA / NLM),
#   "(12) "                     (ACS),
#   "12W."                      (AIP/APS: the number is glued to the first
#                                author's initial, with no separator).
# Each alternative carries its own trailing so the entry text starts cleanly.
# A physical newline is a separator only when it opens the entry itself. PDF
# extraction can put a terminal page number ("454.", "2024.") on a line of
# its own just before the next marker; accepting every ``\s+`` there consumes
# that next marker and silently loses the reference.
_NUM_SEPARATOR = r"(?:[^\S\r\n]+|[^\S\r\n]*\r?\n(?=[^\S\r\n]*[^\W\d_]))"
_NUM_MARKER = (
    rf"(?:\[(\d+)\](?:\.)?{_NUM_SEPARATOR}"
    rf"|\((\d+)\){_NUM_SEPARATOR}"
    rf"|(\d+)[.)]{_NUM_SEPARATOR}"
    r"|(\d+)(?=[A-Z]\.))"
)
_REF_ENTRY_NUM_RE = re.compile(r"^\s*" + _NUM_MARKER)


def _entry_num(m) -> int:
    """The reference number from a numbered-entry match, whichever alternative
    (bracket / paren / dot / glued-initial) captured it."""
    for g in m.groups():
        if g is not None:
            return int(g)
    return 0


# Nature puts the entry number on a line of its own, with the entry below it:
#   "1."
#   "Murray, C. J. et al. Global burden of bacterial antimicrobial resistance…"
# The trailing period is required: a bare "12" on its own line is a page number.
_LONE_NUM_RE = re.compile(r"^\s*(\d{1,3})[.)]\s*$")


def _entry_start_num(line: str) -> int | None:
    """The number opening a reference entry, whether it sits with the entry text
    ("[12] Author…", "12. Author…") or alone on its line (Nature).  None when the
    line does not open an entry.

    Only the boundary detectors use this: knowing *where the numbering runs* is
    what tells the reference list apart from whatever the PDF interleaves with
    it.  Splitting the entries themselves is a different job, done elsewhere."""
    m = _REF_ENTRY_NUM_RE.match(line)
    if m:
        return _entry_num(m)
    m = _LONE_NUM_RE.match(line)
    return int(m.group(1)) if m else None


_PUBLISHER_RE = re.compile(
    r"\b(?:Press|Wiley|Springer|Elsevier|Routledge|MIT\s+Press|SAGE|University\s+Press)\b",
    re.IGNORECASE,
)
_REF_MARKER_RE = re.compile(r"^\s*(?:\[?\d+\]?|[\d]+[.)])\s")


def _has_biblio_signal(line: str) -> bool:
    """Anything that marks the line as part of a reference entry: a year, a DOI, a
    page range, a volume(issue), a publisher, a URL."""
    if re.search(rf"\b{_AY_YEAR}\b", line):
        return True
    if re.search(r"10\.\d{4,9}/", line):
        return True
    if re.search(r"\bpp?\.\s*\d+", line):
        return True
    if re.search(r"\b\d+\s*\(\s*\d+\s*\)\s*[:,]", line):
        return True
    if _PUBLISHER_RE.search(line):
        return True
    if re.search(r"https?://", line):
        return True
    return False


def _is_prose_like(line: str) -> bool:
    """A line is prose-like when it is long and lacks ALL bibliographic structure."""
    return len(line) > 80 and not _has_biblio_signal(line)


def _line_has_ref_marker(line: str) -> bool:
    """True when the line opens a reference: a numbered marker, or an author-year start."""
    return bool(_REF_MARKER_RE.match(line)) or _looks_authoryear_ref_start(line)


def _numbered_refs_resume(lines: list[str], start_i: int, max_ref_seen: int) -> bool:
    """True when numbered reference entries resume at/after *start_i*, continuing
    the sequence already seen (``max_ref_seen``).

    Used to recognise blocks (Acknowledgments, Author Contributions, ...) that a
    two-column PDF interposes *between* references: the reference list is not
    over, it merely pauses.  Requires a run of >=2 consecutive-ish entries so a
    stray year like ``2010)`` or a one-off numbered list item cannot trigger it.
    """
    seq: list[int] = []
    for j in range(start_i, len(lines)):
        n = _entry_start_num(lines[j].strip())
        if n is None or not (1 <= n <= 999):
            continue
        if not seq:
            # The first resumed entry must continue the pre-block sequence.
            if max_ref_seen and max_ref_seen < n <= max_ref_seen + 3:
                seq.append(n)
            continue
        if n in (seq[-1] + 1, seq[-1] + 2):
            seq.append(n)
            if len(seq) >= 2:
                return True
        elif max_ref_seen and max_ref_seen < n <= max_ref_seen + 3:
            seq = [n]
    return False


def _strong_authoryear_spine(lines: list[str], end: int) -> bool:
    """Whether the lines before *end* independently establish an author-year list.

    An ``Appendix`` token can occur in a title or a venue, so an unnumbered list
    must not be cut merely because that word lands on a line of its own.  Five
    author-and-date starts are deliberately more evidence than a single wrapped
    reference can supply.
    """
    starts = 0
    for i in range(end):
        line = _clean_biblio_line(lines[i])
        nxt = _clean_biblio_line(lines[i + 1]) if i + 1 < end else ""
        if (_looks_authoryear_ref_start(line, nxt)
                and re.search(rf"\b{_AY_YEAR}\b", f"{line} {nxt}")):
            starts += 1
            if starts >= 5:
                return True
    return False


def _authoryear_list_resumes(lines: list[str], start_i: int) -> bool:
    """True when an author-year bibliography RESUMES at/after *start_i*.

    The unnumbered mirror of :func:`_numbered_refs_resume`: an ``Appendix`` token
    can fall inside a reference's own title or venue, and there the list is not
    over — it goes on below.  It reuses the same five-start spine the head uses,
    so a handful of prose lines that happen to look like a name cannot revive it,
    but a genuine run of references does.  When nothing resumes (a real appendix,
    prose — BERT's GLUE-task descriptions), the heading is the true end.
    """
    tail = lines[start_i:]
    return _strong_authoryear_spine(tail, len(tail))


_APPENDIX_FOR_QUOTED_TITLE_START_RE = re.compile(
    r'^\s*appendix\s+for\s+(?P<quote>[“"])[^”"]+\s*$',
    re.IGNORECASE,
)


def _is_short_multiline_quoted_appendix_heading(lines: list[str], heading_i: int) -> bool:
    """Whether *heading_i* begins BERT's short, wrapped appendix title.

    BERT extracts its heading as ``Appendix for “BERT: Pre-training of`` / ``Deep
    Bidirectional Transformers for`` / ``Language Understanding”``.  A title in a
    bibliography can use the same words, so require the closing matching quote
    within two short continuation lines and reject every bibliographic signal.
    """
    first = _clean_biblio_line(lines[heading_i])
    opening = _APPENDIX_FOR_QUOTED_TITLE_START_RE.match(first)
    if not opening or _has_biblio_signal(first) or _looks_authoryear_ref_start(first):
        return False
    closing = "”" if opening.group("quote") == "“" else '"'
    for offset in range(1, 3):  # BERT wraps the title across at most two more lines.
        i = heading_i + offset
        if i >= len(lines):
            return False
        line = _clean_biblio_line(lines[i])
        nxt = _clean_biblio_line(lines[i + 1]) if i + 1 < len(lines) else ""
        if (not line or len(line) > 80 or len(line.split()) > 10
                or _has_biblio_signal(line) or _looks_authoryear_ref_start(line, nxt)):
            return False
        if re.search(rf"{re.escape(closing)}\s*[:.]?\s*$", line):
            return True
    return False


def _authoryear_refs_resume(lines: list[str], start_i: int) -> bool:
    """Whether another author-year reference begins below a proposed boundary."""
    for i in range(start_i, len(lines)):
        line = _clean_biblio_line(lines[i])
        nxt = _clean_biblio_line(lines[i + 1]) if i + 1 < len(lines) else ""
        if not (_looks_authoryear_ref_start(line, nxt)
                and re.search(rf"\b{_AY_YEAR}\b", f"{line} {nxt}")):
            continue
        # ``Dataset (Rajpurkar et al., 2016)`` is an appendix sentence, not a
        # reference start.  Keep a genuine ``Author (2021)`` APA entry eligible,
        # but reject a capitalised subject followed by an in-text citation.
        if re.match(rf"^[A-Z][\w'’.-]*\s*\((?!{_AY_YEAR}\b)", line):
            continue
        return True
    return False


def _appendix_table_boundary(lines: list[str], heading_i: int) -> int | None:
    """Return the first table line adjacent to an Appendix heading, if proven.

    This is intentionally not a generic prose test: the author-year end boundary
    is only safe for the ViT shape when an explicit Appendix heading introduces a
    table.  In extracted ViT text the table comes *before* ``APPENDIX``; clean
    text can put it after.  Two tabular rows prevent a caption mentioning "Table"
    on its own from cutting a bibliography.
    """
    table_re = re.compile(r"^(?:table|tab\.)\s*[A-Z]?\d+\b", re.IGNORECASE)
    # A dense extracted table can be much taller than its final reference; keep
    # enough look-behind to reach that reference without treating arbitrary
    # document-wide prose as table context.
    lo = max(0, heading_i - 200)
    hi = min(len(lines), heading_i + 25)
    # The nearest caption is the one belonging to this appendix; an earlier table
    # elsewhere in the (long) reference tail is not evidence for this boundary.
    caption = next((i for i in range(hi - 1, lo - 1, -1)
                    if table_re.match(lines[i].strip())), None)
    if caption is None:
        return None
    window = [line.strip() for line in lines[lo:hi] if line.strip()]
    rows = sum("|" in line or "\t" in line for line in window)
    rows += sum(bool(re.match(r"^[A-Za-z][A-Za-z _-]{1,30}\s+[-+]?\d+(?:\.\d+)?(?:\s+[-+]?\d+(?:\.\d+)?)+\s*$", line))
                for line in window)
    # PDF column extraction may put every cell on a separate line (ViT's
    # ``Models / Dataset / Epochs / …`` table).  Its compact, signal-free run
    # before the caption is still a table, but a lone caption is not.
    stacked = sum(
        bool(line.strip()) and len(line.strip()) <= 50 and not _has_biblio_signal(line)
        for line in lines[max(lo, caption - 40):caption]
    )
    if rows < 2 and stacked < 10:
        return None
    # Table before Appendix: cut before its first line, immediately after the
    # final bibliographic signal.  Table after Appendix: the heading is the cut.
    if caption < heading_i:
        signals = [i for i in range(lo, caption) if _has_biblio_signal(lines[i])]
        return signals[-1] + 1 if signals else None
    return heading_i


def _find_biblio_end_index(lines: list[str]) -> int | None:
    """Detect where the reference list ends and trailing prose/appendices begin.

    Two detection methods, returns the earliest (lowest index) match:
      A. Section heading: exact _POST_BIBLIO_HEADINGS match (short line, < 10 words)
         plus appendix-variant prefix match (\"Appendix A\", \"Appendix B\", etc.).
      B. Prose run: ≥4 consecutive prose-like lines, reference-boundary-aware,
         anchored to the last 30% of the biblio.

    Both methods are suppressed when numbered reference entries resume after the
    candidate boundary (a two-column PDF interposing Acknowledgments / Author
    Contributions blocks in the middle of the reference list).

    Returns None if no end boundary is detected."""
    if len(lines) < 4:
        return None

    total = len(lines)
    scan_start = max(0, int(total * 0.7))  # only scan last 30% for prose runs

    # --- Method A: section heading match ---
    max_ref_seen = 0
    last_entry_idx = None
    entry_count = 0
    for i, line in enumerate(lines):
        # Track the running reference number so an interposed heading can be told
        # apart from the true end: only accept a marker that continues the
        # sequence (guards against a year like "2010)" at a line start).
        n = _entry_start_num(line)
        if n is not None:
            if 1 <= n <= 999 and (max_ref_seen == 0 and n <= 3 or n in (max_ref_seen + 1, max_ref_seen + 2)):
                max_ref_seen = n
                last_entry_idx = i
                entry_count += 1
        stripped = line.strip().lower().rstrip(":.")
        if not stripped:
            continue
        # BERT ends its unnumbered bibliography with the standalone heading
        # ``Appendix for “…”`` (some extractors use straight quotes).  Unlike a
        # generic ``Appendix A``, this short wrapped title-shaped heading is safe
        # once the author-year spine is independently proven — unless references
        # resume below it, in which case it is text embedded in the list.
        if (max_ref_seen == 0
                and _is_short_multiline_quoted_appendix_heading(lines, i)
                and _strong_authoryear_spine(lines, i)
                and not _authoryear_refs_resume(lines, i + 1)):
            return i
        word_count = len(stripped.split())
        if word_count >= 10:
            continue
        is_appendix = bool(parser_text._APPENDIX_PREFIX_RE.match(stripped))
        if stripped in parser_text._POST_BIBLIO_HEADINGS:
            if _numbered_refs_resume(lines, i + 1, max_ref_seen):
                continue  # interposed block; references resume below
            # Numbered lists have their own spine.  An unnumbered author-year
            # bibliography needs both a strong spine and the explicit table shape
            # that follows ViT's Appendix; otherwise keep the text in the list.
            if is_appendix and max_ref_seen == 0:
                boundary = (_appendix_table_boundary(lines, i)
                            if _strong_authoryear_spine(lines, i) else None)
                if boundary is not None:
                    return boundary
                # No ViT-style table below the heading: it ends the list unless an
                # author-year bibliography resumes past it.  Restoring the old cut
                # here is what BERT needs — its "Appendix B" is prose (GLUE-task
                # descriptions carrying citations found nowhere else), so leaving it
                # trapped in the discarded bibliography loses those citations.
                if _authoryear_list_resumes(lines, i + 1):
                    continue
                return i
            return i
        # Watch-item 1: prefix-match appendix variants ("Appendix A", etc.)
        if is_appendix:
            if _numbered_refs_resume(lines, i + 1, max_ref_seen):
                continue
            if max_ref_seen == 0:
                boundary = (_appendix_table_boundary(lines, i)
                            if _strong_authoryear_spine(lines, i) else None)
                if boundary is not None:
                    return boundary
                # An ``Appendix for "<title>"`` heading is the other check's
                # territory, not this one.  The early check above accepts the true
                # end-heading (closed quote, spine proven, nothing resuming) and
                # returns; where it DECLINES — the quote never closes, or references
                # resume below — the line is a title-shaped entry, not a boundary,
                # so this generic-appendix cut must not fire on it.
                if _APPENDIX_FOR_QUOTED_TITLE_START_RE.match(_clean_biblio_line(lines[i])):
                    continue
                # No ViT-style table below the heading: it ends the list unless an
                # author-year bibliography resumes past it.  Restoring the old cut
                # here is what BERT needs — its "Appendix B" is prose (GLUE-task
                # descriptions carrying citations found nowhere else), so leaving it
                # trapped in the discarded bibliography loses those citations.
                if _authoryear_list_resumes(lines, i + 1):
                    continue
                return i
            return i

    # --- Method B: prose run in last 30% ---
    consecutive = 0
    run_start = None
    b_max_ref = 0
    for i in range(scan_start, total):
        line = lines[i].strip()
        if not line:
            # Empty lines don't break a prose run but don't count toward it.
            continue
        rm = _REF_ENTRY_NUM_RE.match(line)
        if rm:
            n = _entry_num(rm)
            if 1 <= n <= 999 and (b_max_ref == 0 or n in (b_max_ref + 1, b_max_ref + 2)):
                b_max_ref = n
        if _is_prose_like(line) and not _line_has_ref_marker(line):
            if consecutive == 0:
                run_start = i
            consecutive += 1
            if consecutive >= 4:
                # Interposed block, not the end, if numbered refs resume below.
                if _numbered_refs_resume(lines, i + 1, b_max_ref):
                    consecutive = 0
                    run_start = None
                    continue
                return run_start
        else:
            consecutive = 0
            run_start = None

    # --- Method C: the numbering runs out ---
    # A numbered reference list ends where its numbering ends.  Whatever follows
    # the last entry — an appendix, a table dump, attention visualisations — is
    # not a reference, and while it sits inside the bibliography every citation
    # it makes is invisible: the references it cites read as "never cited".
    # Methods A and B miss it when the appendix opens with a heading that is not
    # in the list ("A. Variants") and its two-column lines are too short to read
    # as prose.
    #
    # The cut is anchored to the blank line that closes the last entry, so a
    # wrapped entry is never cut in half; and it is refused when anything below
    # could still be an entry — if the number tracker lost the sequence, the rest
    # of the list is down there and dropping it would lose real references.
    if entry_count >= 5 and last_entry_idx is not None:
        end = last_entry_idx + 1
        while end < total and lines[end].strip():          # the entry's own wrap
            end += 1
        while end < total and not lines[end].strip():       # the blank run
            end += 1
        if end < total and not _numbered_refs_resume(lines, end, max_ref_seen):
            below = (_entry_start_num(ln) for ln in lines[end:])
            if not any(n > max_ref_seen for n in below if n is not None):
                return end

    return None


def _entry_spine(lines: list[str]) -> list[tuple[int, int]]:
    """Every line that continues the reference list's numbering, as (index, number).

    The spine is the list's skeleton.  Everything else on those pages either hangs
    off it — a wrapped title, a venue line — or does not belong to the list at all,
    and the spine is the only thing that can tell the two apart.
    """
    spine: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        n = _entry_start_num(line.strip())
        if n is None or not (1 <= n <= 999):
            continue
        if not spine:
            if n <= 3:
                spine.append((i, n))
        elif n in (spine[-1][1] + 1, spine[-1][1] + 2):
            spine.append((i, n))
    return spine


def _wraps_previous_line(lines: list[str], i: int) -> bool:
    """True when line *i* completes a word the line above broke with a hyphen.

    Typesetting, not guesswork: "Castillo-" / "Ramírez, S. …" is one word split
    across two lines, so the second line belongs to whatever the first belongs to
    and can never be the first line of something else.
    """
    for j in range(i - 1, -1, -1):
        prev = lines[j].strip()
        if prev:
            return prev.endswith(("-", "‐", "‑"))
    return False


def _interposed_body_spans(lines: list[str]) -> list[tuple[int, int]]:
    """Body text that the PDF's reading order dropped INSIDE the reference list.

    A two-column back matter can interleave its columns: in Nature's, the tail of
    the Methods section lands between reference [22]'s first lines and its last
    two.  Eight references are then cited only from prose that sits inside the
    bibliography — prose the parser never reads as prose.  Those citations are
    lost in silence (worse than an orphan: nothing reports them) and the eight
    references come out as "never cited".

    Returns the spans to hand back to the body, as inclusive (first, last) line
    indices.  The rule is the numbering: a run of lines is foreign when the
    numbering *resumes below it* — a run past the last entry is the list ending,
    which is _find_biblio_end_index's job — and when it is far longer than this
    list's own entries, a length the list itself supplies.  Cutting a reference
    list is the dangerous direction, so every doubt leaves the lines where they are.
    """
    spine = _entry_spine(lines)
    if len(spine) < 5:
        return []
    # How long is an entry here?  The list says so: the gaps between its own
    # consecutive entries.  The median ignores the interposed block itself, which
    # is exactly the outlier we are looking for.
    gaps = sorted(spine[k + 1][0] - spine[k][0] for k in range(len(spine) - 1))
    typical = gaps[len(gaps) // 2]
    floor = max(6, 3 * typical)

    spans: list[tuple[int, int]] = []
    for k in range(len(spine) - 1):
        lo, hi = spine[k][0] + 1, spine[k + 1][0]
        if hi - lo < floor:
            continue  # an ordinary entry, however it wraps
        # The entry above owns the lines right below its number — that is what
        # *typical* measures — so a foreign run may not open in them.  An author
        # line reads like prose (no year, no DOI, no pages: those come further
        # down the entry), and taking it would cut a reference in half.  Erring
        # here costs a line or two of body text; erring the other way costs a
        # reference.
        spans.extend(_foreign_run(lines, lo, hi, floor, spine[k][0] + typical))
    return spans


def _foreign_run(
    lines: list[str], lo: int, hi: int, floor: int, open_from: int
) -> list[tuple[int, int]]:
    """The runs of non-bibliographic lines in ``lines[lo:hi]``, at least *floor* long.

    A run may not open before *open_from* (those lines are the previous entry's)
    nor on a line that completes a hyphenated word (same reason, one line at a
    time).  What ends a run is what says the reference list is back: a line that
    opens a reference — its number, or the authors an entry starts with — or two
    lines carrying bibliographic signal with nothing but fragments between them.
    A single such line does not: a URL sits in Methods prose as readily as in an
    entry, and a page footer carries the year of the journal.  Short lines (a page
    number, a subheading broken in two) neither extend a run nor break it.
    """
    runs: list[tuple[int, int]] = []
    start = last = None
    signalled = False

    def close():
        nonlocal start, last, signalled
        if start is not None and last is not None and last - start + 1 >= floor:
            runs.append((start, last))
        start = last = None
        signalled = False

    for i in range(lo, hi):
        line = lines[i].strip()
        if len(line) < 25:
            continue  # a fragment, a page number, half a heading: neither way
        if _line_has_ref_marker(line):
            close()  # an entry's number, or the authors it opens with
            continue
        if _has_biblio_signal(line):
            if signalled:
                close()  # two signals running: this is the list again
            else:
                signalled = True  # tolerated inside the run, never opens one
            continue
        signalled = False
        if start is None:
            if i < open_from or _wraps_previous_line(lines, i):
                continue  # still the entry above
            start = i
        last = i
    close()
    return runs


def _cut_at_last_biblio_heading(text: str) -> tuple[str, str, dict, int | None]:
    """Scan *text* for the last bibliography heading and split into (body, biblio).

    Returns ``(body, biblio, debug, cut_idx)`` where *cut_idx* is the line
    index of the heading, or ``None`` when no heading is found.

    Shared by all format handlers:
    * ``default_split_body_bibliography`` adds ``_find_biblio_end_index``
      on top of this result to detect trailing prose/appendix runs.
    * ``latex`` / ``markdown`` return ``end_idx=None`` directly — their
      bibliographies are structurally clean and never have post-biblio bleed.
    """
    lines = text.split("\n")
    cut_idx = None
    for i, line in enumerate(lines):
        # Strip a leading section-marker glyph ("■References", ACS) before match.
        stripped = re.sub(r"^[\s■▪●•◆□▶*·]+", "",
                          line).strip().lower().rstrip(":.")
        if stripped in parser_text.BIBLIOGRAPHY_HEADINGS:
            cut_idx = i  # keep the LAST occurrence (fix for bug §11)
    debug = {"cut_line_index": cut_idx, "total_lines": len(lines)}
    if cut_idx is None:
        debug["context"] = "(no bibliography heading found at start of line)"
        return text, "", debug, None
    body = "\n".join(lines[:cut_idx])
    biblio = "\n".join(lines[cut_idx + 1:])
    # ~100 words around the cut, for debug
    around = " ".join(lines[max(0, cut_idx - 2): cut_idx + 3])
    debug["context"] = around[:800]
    return body, biblio, debug, cut_idx


def default_split_body_bibliography(text: str, meta=None) -> tuple[str, str, dict, int | None]:
    """Splits body from bibliography at the LAST occurrence of a heading and
    trims trailing post-biblio content (appendix, acknowledgments, prose runs).
    """
    body, biblio, debug, cut_idx = _cut_at_last_biblio_heading(text)
    if cut_idx is None:
        return body, biblio, debug, None
    biblio_lines = biblio.split("\n")
    end_idx = _find_biblio_end_index(biblio_lines)
    if end_idx is not None:
        biblio = "\n".join(biblio_lines[:end_idx])
        # Re-include post-biblio content (appendices, acknowledgments, etc.)
        # back into the body so citations in those sections are resolved.
        if end_idx < len(biblio_lines):
            post_biblio = "\n".join(biblio_lines[end_idx:])
            body = body + "\n" + post_biblio
    return body, biblio, debug, end_idx


# A "(n)" right after one of these words is a structural cross-reference
# (equation/figure/table number), never a bibliography citation.
_PAR_STRUCTURE_RE = re.compile(
    r"\b(?:eq|eqs|eqn|eqns|equation|equations|formula|formulae|formulas|"
    r"expression|expressions|fig|figs|figure|figures|table|tables|scheme|"
    r"step|steps|item|items|section|sect|sec|chapter|chap|appendix|annex|"
    r"algorithm|algorithms|lemma|theorem|corollary|proposition|definition|"
    r"remark|exercise|problem|box|panel|note|notes)\s*\.?\s*$",
    re.IGNORECASE,
)


def _par_in_equation_context(lead: str) -> bool:
    """True when '(n)' directly follows an equation: 'E = mc2 (1)'.
    A relational operator shortly before the marker, with no clause punctuation
    in between, marks the parenthesised number as an equation label."""
    tail = lead[-40:]
    idx = max(tail.rfind(op) for op in "=≈≤≥")
    if idx == -1:
        return False
    return not re.search(r"[,.;:!?)]", tail[idx + 1:])


def _brk_in_formula_context(sentence: str, start: int) -> bool:
    """True when '[n]' is attached to a multi-letter identifier in formula context.

    The AND gate requires BOTH:
    1. A multi-letter identifier adjacent to the bracket (zero spaces), OR a
       closing parenthesis right before the bracket; AND
    2. Mathematical operators, function names, or subscripts in the ~60-char
       window around the bracket (before AND after — formula context often
       follows the indexed expression, e.g. ``head[1] + bias``).

    A single signal alone (e.g. ``model[1]`` without formula context, or ``=``
    followed by ``transformer [1]`` with a space) is NOT enough.  This mirrors
    ``_par_in_equation_context`` for the bracket citation style.

    An operator BEHIND the marker only counts when it applies to the marked
    expression — "head[1] + bias" — and clause punctuation says it does not:
    "RAxML v8⁴², considering a GTR + G4 model" is a sentence that happens to
    contain a plus sign, and the comma is where the formula would have to end.
    Same reading as ``_par_in_equation_context`` makes on the other side.
    """
    end_pos = min(len(sentence), start + 60)
    window_before = sentence[max(0, start - 60):start]
    window_after = sentence[start:end_pos]
    # The marker is not its own context: its sentinel form carries a ':'.
    tail = re.sub(rf"^⟦SUP:[\d,{RANGE_DASHES}]+⟧", "", window_after)
    op_after = re.search(r"[=+÷×⋅∑]", tail)
    has_formula = bool(
        re.search(r"[=+÷×⋅∑]", window_before)
        or (op_after and not re.search(r"[,;:!?]", tail[:op_after.start()]))
        or re.search(
            r"\b(Concat|Softmax|sum|max|min|argmax|argmin|mean|avg|prod|norm|"
            r"ReLU|GELU|LayerNorm)\b",
            window_before + " " + window_after,
            re.IGNORECASE,
        )
        or re.search(r"\b[a-zA-Z]_\{?\d", window_before)
        or re.search(r"\b[a-zA-Z]_\{?\d", window_after)
    )
    if not has_formula:
        return False
    before = sentence[:start]
    # Closing-paren right before bracket:  f(x)[i], Softmax(x)[1]
    if before.rstrip().endswith(")"):
        return True
    # Multi-letter identifier adjacent to bracket: head[1], W_O[1]
    mb = re.search(r"([A-Za-z_][A-Za-z0-9_]*)$", before)
    if not mb:
        return False
    return len(mb.group(1)) > 1


# An initial ("J.", "M. A.") that opens a name running to a YEAR — see
# default_segment_sentences, where those periods are protected from the split.
#
# The year is the discriminator, and there is no other: "J. Smith and K. Jones (2020)"
# and "the answer is A. The next question" are the same shape — a capital, a period, a
# space, a capital — and only one of them is a citation.  So the lookahead walks what
# follows (surnames, further initials, "and"/"&", a co-author run) and protects the
# initial ONLY where that walk reaches a year.  Prose keeps its sentence ends; a
# citation keeps its first author.
_INITIAL_IN_CITE_RE = re.compile(
    r"(?<![A-Za-zÀ-ÿ0-9])((?:[A-ZÀ-Þ]\.\s*)+)"
    r"(?=(?:[A-ZÀ-Þ]\.\s*|[A-ZÀ-Þ][\w'’‐-]*[\s,]*|(?:and|&|e)\s+){0,8}"
    r"\(?(?:19|20)\d{2})")


class SegmentedSentences(list):
    """A normal sentence list with optional, layout-only per-sentence lineage.

    It intentionally remains a ``list[str]`` for every existing citation
    scheme.  Consumers that do not know about lineage therefore retain their
    exact API and behaviour; claim construction can opt in through the public
    ``lineage`` attribute.
    """

    def __init__(self, values=(), *, lineage=None):
        super().__init__(values)
        self.lineage = list(lineage or [])


def _canonical_with_origins(text: str, trace: list[dict | None]):
    """Whitespace-fold *text* while preserving the physical-line contributor.

    This mirrors the only lossy sentence-segmentation transformation relevant
    to lineage (visual newline -> space).  It does not inspect words, titles,
    citation syntax, or paper-specific vocabulary.
    """
    chars: list[str] = []
    origins: list[dict | None] = []
    pending_space = False
    for line, item_trace in zip(text.split("\n"), trace):
        line = line.replace(STRUCTURAL_TABLE_SENTINEL, "").replace(
            STRUCTURAL_AMBIGUOUS_TABLE_SENTINEL, ""
        )
        interruption_offsets = iter(
            item_trace.get("page_layout_interruption_offsets", ())
        ) if item_trace is not None else iter(())
        repaired_offsets = iter(
            item_trace.get("page_layout_repaired_offsets", ())
        ) if item_trace is not None else iter(())
        next_interruption = next(interruption_offsets, None)
        next_repaired = next(repaired_offsets, None)
        for source_offset, char in enumerate(line):
            if char.isspace():
                pending_space = bool(chars)
                continue
            if pending_space and chars:
                chars.append(" ")
                origins.append(None)
            pending_space = False
            chars.append(char)
            if item_trace is not None and (
                (next_interruption is not None and source_offset >= next_interruption)
                or (next_repaired is not None and source_offset >= next_repaired)
            ):
                origin = dict(item_trace)
                if next_interruption is not None and source_offset >= next_interruption:
                    origin["page_layout_interruption"] = True
                    next_interruption = next(interruption_offsets, None)
                if next_repaired is not None and source_offset >= next_repaired:
                    origin["page_layout_repaired"] = True
                    next_repaired = next(repaired_offsets, None)
                origins.append(origin)
            else:
                origins.append(item_trace)
        pending_space = True
    return "".join(chars).strip(), origins


def _lineage_for_sentences(sentences: list[str], isolated: str,
                           trace: list[dict | None]) -> list[dict]:
    """Attach observable layout contributors to each emitted sentence.

    The matching is ordered and character-exact after whitespace folding.  A
    failure is represented as ``unavailable`` rather than guessed from text.
    """
    flat, origins = _canonical_with_origins(isolated, trace)
    cursor = 0
    lineage: list[dict] = []
    incompatible = frozenset({
        "heading", "float", "table", "layout", "ambiguous_layout",
        "ambiguous_table",
    })
    kind_by_line = {
        item["line_start"]: item["kind"]
        for item in trace
        if isinstance(item, dict) and item.get("line_start") is not None
    }
    for sentence in sentences:
        canonical = " ".join(
            sentence.replace(STRUCTURAL_TABLE_SENTINEL, "").replace(
                STRUCTURAL_AMBIGUOUS_TABLE_SENTINEL, ""
            ).split()
        )
        start = flat.find(canonical, cursor) if canonical else -1
        if start < 0:
            lineage.append({
                "version": 1,
                "status": "unavailable",
                "segment_kinds": [],
                "boundary_kinds": [],
                "crosses_incompatible_segments": False,
                "recovered": False,
            })
            continue
        end = start + len(canonical)
        cursor = end
        contributors: list[dict] = []
        contributor_ids: set[int] = set()
        for item_trace in origins[start:end]:
            item_trace_id = id(item_trace)
            if item_trace is not None and item_trace_id not in contributor_ids:
                contributors.append(item_trace)
                contributor_ids.add(item_trace_id)
        kinds = [item["kind"] for item in contributors]
        unique_kinds = list(dict.fromkeys(kinds))
        line_numbers = [item["line_start"] for item in contributors]
        first_line = min(line_numbers) if line_numbers else None
        after_layout_lowercase = bool(
            canonical
            and canonical[0].islower()
            and first_line is not None
            and kind_by_line.get(first_line - 1) in incompatible
        )
        explicitly_ambiguous = bool({
            "ambiguous_layout", "ambiguous_table",
        }.intersection(unique_kinds))
        crosses_incompatible = explicitly_ambiguous or (
            "prose" in unique_kinds
            and any(kind in incompatible for kind in unique_kinds)
        ) or after_layout_lowercase
        page_layout_repaired = any(
            item.get("page_layout_repaired") for item in contributors
        )
        recovered = (
            (len(set(line_numbers)) > 1 or page_layout_repaired)
            and not crosses_incompatible
        )
        status = "ambiguous" if crosses_incompatible else (
            "recovered" if recovered else "clean")
        lineage.append({
            "version": 1,
            "status": status,
            "segment_kinds": unique_kinds,
            "boundary_kinds": (
                (["line_fold"] if len(set(line_numbers)) > 1 else [])
                + (
                    ["lowercase_continuation_after_layout"]
                    if after_layout_lowercase else []
                )
                + (
                    ["checklist_boundary_ambiguous"]
                    if explicitly_ambiguous else []
                )
                + (
                    ["page_layout_interruption"]
                    if any(item.get("page_layout_interruption") for item in contributors)
                    else []
                )
                + (["page_layout_repaired"] if page_layout_repaired else [])
            ),
            "crosses_incompatible_segments": crosses_incompatible,
            "recovered": recovered,
            "line_start": first_line,
            "line_end": max(line_numbers) if line_numbers else None,
            "raw_start": min((item["raw_start"] for item in contributors), default=None),
            "raw_end": max((item["raw_end"] for item in contributors), default=None),
        })
    return lineage


def default_segment_sentences(text: str, meta=None) -> list[str]:
    """Sentence segmentation that is aware of protected abbreviations.

    The returned object is list-compatible and carries ``lineage`` derived
    exclusively from physical layout and the deterministic rewrites below.
    """
    # Protect abbreviations by replacing the period with a sentinel.
    # Mandatory word boundary before: without it, "p." would match inside
    # "relationship." and "no." inside "Marino.", merging adjacent sentences.
    protected, layout_trace = _isolate_layout_lines(text, with_trace=True)
    lineage_text = protected
    for abbr in sorted(parser_text.PROTECTED_ABBREVIATIONS, key=len, reverse=True):
        protected = re.sub(
            r"(?<![A-Za-zÀ-ÿ0-9])" + re.escape(abbr),
            lambda m: m.group(0).replace(".", "․"),
            protected,
            flags=re.IGNORECASE,
        )
    # An author's initials are a name, not a sentence end.  APA names its authors in
    # full in an abstract — "M. A. Conway and C. W. Pleydell-Pearce's (2000) model" —
    # and a capital, a period, a space, a capital is exactly the shape this splits on.
    # Cut there, the citation loses its first author and the fragment left behind
    # ("Pleydell-Pearce's (2000) …") keys on the SECOND: an orphan no bibliography can
    # answer, and the tool reporting a fault of its own as the manuscript's.
    #
    # Only where a year follows (see _INITIAL_IN_CITE_RE): that is what separates the
    # name from prose that merely looks like one.  The error it can still make is to
    # join two real sentences when a year happens to sit just past the boundary ("the
    # answer is A. The 2020 election …") — a worse sentence, but every citation in it
    # still resolves, where the other way round loses one outright.
    protected = _INITIAL_IN_CITE_RE.sub(
        lambda m: m.group(1).replace(".", "․") + m.group(0)[len(m.group(1)):], protected)
    # PDF extractors preserve visual line wraps. A single newline inside a paragraph
    # is usually not a sentence boundary; blank lines still separate blocks/headings.
    protected = re.sub(r"[ \t]*\n[ \t]*", "\n", protected)
    protected = re.sub(r"(?<!\n)\n(?!\n)", " ", protected)
    # Split on .?! followed by space + uppercase/opening, on the observed
    # numeric-citation sentence followed by a PDF Unicode bullet, or on
    # paragraph breaks. Keep the bullet rule narrow so ordinary list layout
    # does not silently redefine unrelated claim boundaries.
    pieces = re.split(
        r"(?<=[.!?])[\s]+(?=[A-ZÀ-ſ\"'(\[])|(?<=\][.!?])[\s]+(?=•)|\n{2,}",
        protected,
    )
    out = []
    for piece in pieces:
        sub = piece.replace("․", ".").strip()
        if sub:
            out.append(sub)
    return SegmentedSentences(
        out,
        lineage=_lineage_for_sentences(out, lineage_text, layout_trace),
    )


def _coalesce_yearless_splits(splits: list[str]) -> list[str]:
    """Rejoin split fragments that carry no year instead of dropping them.

    A real author-year reference always has a year, so a yearless fragment is a
    mis-split.  It can be either half of a wrongly-cut entry, and the two need
    opposite treatment:

      * an author-list *head* whose year the splitter left in the next fragment
        (a journal name like "Neural Computation," was mistaken for an author
        boundary and cut before the year) -> merge it FORWARD onto that year;
      * a venue *tail* whose year sits in the previous fragment (e.g. a trailing
        "Curran Associates, Inc.") -> merge it BACKWARD onto that entry.

    Discriminate on author initials ("Y. LeCun", "B. Boser"): a real reference
    head carries them, a venue tail does not.  Merging a tail forward would
    overwrite the next reference's first author and orphan it (regression guard:
    the fingerprint on BERT catches exactly that)."""
    # A source not yet published dates itself the same breath it names its authors —
    # "(in press)", "(forthcoming)", "(n.d.)" — and that IS its date, so a fragment
    # carrying one is a whole entry, not a yearless head waiting to steal the next
    # fragment's year.  Without this, "Crane, C., … (in press). … Memory." reads as
    # yearless, grabs the reference that follows it, and that reference — Croll (2000),
    # Henderson (2002) — vanishes into the merge and its citation is orphaned.
    has_year = lambda s: re.search(rf"\b{_AY_YEAR}\b", s) or _PAREN_YEAR_RE.search(s)  # noqa: E731
    is_head = lambda s: bool(re.search(r"\b[A-Z]\.\s*[A-Z]", s))  # noqa: E731
    out: list[str] = []
    carry = ""  # a yearless author-list head waiting for the fragment with its year
    for s in splits:
        if carry:
            s = f"{carry} {s}".strip()
            carry = ""
        if has_year(s):
            out.append(s)
        elif is_head(s):
            carry = s
        elif out:
            out[-1] = f"{out[-1]} {s}".strip()
        # else: a leading yearless non-head with no prior entry -> dropped, as before
    if carry:
        if out:
            out[-1] = f"{out[-1]} {carry}".strip()
        else:
            out.append(carry)
    return out


# Chicago author-date prints the author once, on its own line, and hangs each of
# their works below it on a line that opens with the year in a hanging column,
# separated from the text column by a tab:
#
#     Agamben, Giorgio
#     1995\t Homo Sacer: Sovereign Power and Bare Life. ...
#     Stanford, Calif.: Stanford University Press.
#     2004\t State of Exception. ...
#     Agier, Michel
#     2008\t On the Margins of the World. ...
#
# Every year line opens its OWN reference and inherits the author line above it,
# so "(Agamben 2004)" resolves even though the surname is printed only once.  The
# passes below split on author names instead, which folds the second work into the
# previous entry's publisher line and hands it that line's bogus surname.
_HANGING_YEAR_RE = re.compile(rf"^\s*(?:\[{_AY_YEAR}\]\s*)?{_AY_YEAR}\s*\t")

# The author line itself: a surname-first name list ("Agamben, Giorgio"; "Barker,
# Joshua, Erik Harms, and Johan A. Lindquist") or an organisation named with a
# bracketed acronym ("United Nations High Commission for Refugees (UNHCR)").
_HANGING_AUTHOR_RE = re.compile(
    r"^[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*)*,\s+[A-Z]"
    r"|^[A-Z].*\([A-Z]{2,}\)$"
)


def _is_hanging_author_line(line: str) -> bool:
    """Tell an author line from a wrapped continuation line.  Position alone cannot:
    "Stanford, Calif.: Stanford University Press." and "University of California
    Press." both sit directly above a year line, exactly where an author line sits.
    What separates them is content — an author line carries names only, so no digit,
    no colon and no URL."""
    if not line or ":" in line or any(ch.isdigit() for ch in line):
        return False
    return bool(_HANGING_AUTHOR_RE.match(line))


def _split_hanging_authoryear_references(biblio: str) -> list[str] | None:
    """Split the hanging-author layout described above.  Returns None when the
    bibliography is not in that layout, so the caller keeps its normal passes."""
    raw_lines = biblio.splitlines()
    if sum(1 for ln in raw_lines if _HANGING_YEAR_RE.match(ln)) < 5:
        return None
    refs: list[str] = []
    author = ""
    buf: list[str] = []

    def _flush() -> None:
        if buf:
            refs.append(_merge_hyphen_wraps(" ".join(buf)))
            buf.clear()

    for raw in raw_lines:
        is_year = bool(_HANGING_YEAR_RE.match(raw))
        line = _clean_biblio_line(raw)
        if _is_biblio_tail_line(line):
            break
        # A year-only line ("[1951] 1973", with the title wrapped below) cleans down
        # to bare digits, which the noise test reads as a page footer — so ask the
        # year test first.
        if is_year:
            _flush()
            buf.append(f"{author} {line}".strip())
        elif _is_biblio_noise_line(line):
            continue
        elif _is_hanging_author_line(line):
            _flush()
            author = line
        elif buf:
            buf.append(line)
    _flush()
    return refs or None


def _page_furniture_lines(biblio: str) -> set[str]:
    """The page furniture the PDF printed INSIDE the reference list.

    Emerald stamps every page with "Downloaded from https://… by guest on …", and
    the extractor hands the stamp back where it fell — in the middle of an entry,
    which then swallows it.  Two signals together, never one: the line REPEATS
    inside the list (a reference does not repeat) *and* it matches a watermark
    pattern from the external config.  Whole lines only: an entry that wraps its
    DOI onto a line of its own matches those same patterns, and dropping that line
    would take the DOI with it."""
    try:
        from core.parse import boilerplate
    except ImportError:  # pragma: no cover - direct-execution import path
        import boilerplate  # type: ignore
    return boilerplate.boilerplate_lines(biblio)


# An author-date entry dates itself right after naming its authors:
# "Winkelman, P. (2012),", "North Carolina State Board of Education (2006),".
# Nothing may come between but the names — no digits (a volume or a page range),
# no other bracket (an "(Ed.)" belongs further in).  A source with no year says so
# in the same breath ("(n.d.)"), and that is still an entry dating itself.
_AY_ENTRY_DATE = rf"(?:{_AY_YEAR}|n\.\s?d\.|in press|forthcoming)"
_DATED_START_RE = re.compile(rf"^[^()\d]{{1,200}}?\({_AY_ENTRY_DATE}\)", re.IGNORECASE)
_PAREN_YEAR_RE = re.compile(rf"\({_AY_ENTRY_DATE}\)", re.IGNORECASE)


def _dated_entry_start(line: str, next_line: str = "") -> bool:
    # The year lands on the next line when the author list fills this one.
    probe = line if _PAREN_YEAR_RE.search(line) else f"{line} {next_line}"
    return bool(_DATED_START_RE.match(probe))


# An author list that runs off the end of its line leaves it hanging on the last
# author's bare initials — "… Takeshima H, Yamazaki A,".  The line below carries the
# rest of the list and then the year ("Munehara H. 2019. Host selection …"), which is
# the very shape of an entry start, and reading it as one cuts the reference in half:
# the head loses its year, the tail becomes a work nobody cited.  A comma after a
# punctuated initial ("Williams, J. M. G.,") is a different thing and stays untouched.
_HANGING_AUTHORS_RE = re.compile(r"(?:^|\s)[A-Z]{1,4},\s*$")

# The column can break a name in half, between the surname and its own initials:
#
#     Guedes P, Alves-Martins F, Arribas JM, Chatterjee S, Santos
#     AMC, Lewin A, Bako L, Webala PW, Correia RA, Rocha R
#     et al. 2023. Eponyms have no place in 21st-century …
#
# and the damage is done twice over.  The line below carries the year, so it reads as
# an entry start and the reference is cut through its author list — the tail becomes a
# work by "AMC", the head is left yearless and glues itself to the entry above.
#
# No reference opens with its author's initials: the name they belong to is the one
# above them.  Two signals, because an acronym CAN be an author in its own right
# ("WHO. 2024. …") — the line begins with a bare cluster of capitals, AND the line
# above it ends in the middle of a word-run, with no full stop to close the entry.
_CONTINUES_AUTHORS_RE = re.compile(_INITIAL_CLUSTER + r"[,.]\s")
_LINE_LEFT_OPEN_RE = re.compile(r"[A-Za-zÀ-ÿ]$")
_SPLIT_SURNAME_LINE_RE = re.compile(
    r"^[A-Z][^\W\d_][\w'’-]*(?:[- ][A-Z][^\W\d_][\w'’-]*)?$")
_SPLIT_INITIAL_LINE_RE = re.compile(rf"^{_INITIAL_CLUSTER}\.$")
_SPLIT_YEAR_LINE_RE = re.compile(rf"^{_AY_YEAR}\.(?:\s|$)")


def _continues_author_list(line: str, prev_line: str) -> bool:
    return bool(_CONTINUES_AUTHORS_RE.match(line)
                and _LINE_LEFT_OPEN_RE.search(prev_line.strip()))


def _split_surname_initial_year_start(
        surname_line: str, initial_line: str, year_line: str) -> bool:
    """Recognize a reference split across surname, initial, and year lines.

    This is deliberately narrower than author-list continuation: PDF extraction can
    place ``Lund``, ``N.``, and ``2024.`` on separate physical lines.  The existing
    start validator still decides whether their joined form is a bibliography entry.
    """
    if not (_SPLIT_SURNAME_LINE_RE.fullmatch(surname_line)
            and _SPLIT_INITIAL_LINE_RE.fullmatch(initial_line)
            and _SPLIT_YEAR_LINE_RE.match(year_line)):
        return False
    return _looks_authoryear_ref_start(
        f"{surname_line} {initial_line} {year_line}")


def _authoryear_entry_starts(raw_lines: list[str]) -> set[int]:
    """Which lines open a reference — with the wrapped ones taken back out.

    A journal name wrapping across the column ("… Administrative Issues Journal:
    Connecting" / "Education, Practice, and Research, Vol. 8 No. 1, pp. 18-27…")
    reads exactly like a surname followed by a forename, so the entry-start test
    fires on it and cuts the reference in two: the head loses its venue, and the
    tail becomes a reference nobody ever cited — a phantom that inflates the list
    and shows up as a coverage hole.

    The list itself says which of the two a line is.  Where the entries date
    themselves — "Winkelman, P. (2012)," — a candidate that does not carry its year
    is a wrap, not an entry.  Only a list that overwhelmingly dates its entries this
    way opens the gate: a bibliography that dates them some other way (Chicago's
    "Smith, John. 2020. …") never does, and keeps the old behaviour untouched."""
    cand: list[tuple[int, bool]] = []
    prev_line = ""
    for i, raw in enumerate(raw_lines):
        line = _clean_biblio_line(raw)
        if _is_biblio_noise_line(line):
            continue
        nxt = _clean_biblio_line(raw_lines[i + 1]) if i + 1 < len(raw_lines) else ""
        third = (_clean_biblio_line(raw_lines[i + 2])
                 if i + 2 < len(raw_lines) else "")
        if _split_surname_initial_year_start(line, nxt, third):
            # A three-line author/year shape may still be the final co-author
            # of an author list that the column wrapped after a trailing comma.
            # In that case it cannot open a new entry.
            if not _HANGING_AUTHORS_RE.search(prev_line):
                joined = f"{line} {nxt} {third}"
                cand.append((i, _dated_entry_start(joined)))
            prev_line = line
            continue
        if _continues_author_list(line, prev_line):
            prev_line = line
            continue
        # An author list that wraps twice pushes the year out of reach: the entry's own
        # line ends on a surname, the next carries that surname's initials, and only the
        # third says 1875 or 2023.  The line that continues the list is where the list
        # goes on, so that is where the year is still being looked for.
        if _continues_author_list(nxt, line) and i + 2 < len(raw_lines):
            nxt = f"{nxt} {_clean_biblio_line(raw_lines[i + 2])}"
        if _looks_authoryear_ref_start(line, nxt) and not _HANGING_AUTHORS_RE.search(prev_line):
            cand.append((i, _dated_entry_start(line, nxt)))
        prev_line = line
    dated = [i for i, ok in cand if ok]
    # A simple majority: the two kinds of list sit far apart — one dates nearly every
    # entry this way (the wraps are the minority), the other dates none of them at
    # all (Chicago's bare year, MLA's year at the end), so the middle is empty and
    # the line can be drawn there without straining either side.
    if len(cand) >= 5 and len(dated) > len(cand) / 2:
        return set(dated)
    return {i for i, _ in cand}


def mark_further_reading(references: list[dict], biblio: str) -> list[dict]:
    """Flag the entries of a "Further reading" list — works the paper recommends and
    never cites.

    They are not references, so reporting them as references nobody cited announces a
    hole in a bibliography that has none.  The heading alone cannot say WHICH entries
    are theirs: this back matter is set in two columns, and the extractor drops the
    heading beside an entry from the *other* column — "Further reading" lands right
    above O'Malley, a reference the body does cite.

    The list's own order says it instead.  An alphabetical bibliography runs A→Z once;
    a second list restarts the alphabet.  So three signals together: the document
    PRINTS such a heading, the list is alphabetical, and it restarts exactly once, near
    the end — and the flag reaches only the short tail past that restart.  Any doubt
    leaves every entry a reference, which is the safe direction, and the flag itself
    is bounded: an entry that turns out to be cited stays counted (see
    ``_reference_coverage``), so it can only ever excuse an entry nobody cites."""
    if not references or not biblio or not _FURTHER_READING_RE.search(biblio):
        return []
    keys = [(r.get("ay_surname") or "").lower() for r in references]
    if len(keys) < 15 or not all(keys):
        return []
    breaks = [i for i in range(1, len(keys)) if keys[i] < keys[i - 1]]
    if len(breaks) != 1:
        return []
    cut = breaks[0]
    tail = references[cut:]
    if not 1 <= len(tail) <= 5 or cut < 0.75 * len(keys):
        return []
    for r in tail:
        r["further_reading"] = True
    return tail


def _split_authoryear_references(biblio: str) -> list[str]:
    """Split an author-year bibliography into individual references.

    Uses a two-pass strategy: first tries to split the full text at reference
    boundaries (period + author-list start), falling back to the line-based
    approach when that produces too few entries."""
    # --- Pass 0: hanging-author layout (Chicago author-date) ---
    # Only fires on the year-in-a-hanging-column shape, which the two passes below
    # cannot split correctly; every other bibliography falls straight through.
    hanging = _split_hanging_authoryear_references(biblio)
    if hanging and len(hanging) >= 5:
        return hanging

    # --- Pass 1: full-text boundary split ---
    # A reference boundary: period, whitespace, then an author-list start
    # pattern: 2+ capitalized name-tokens followed by a comma, "and", or "&".
    # Exclude common sentence-start words that are NOT name starts.
    # Allow an O'/D' apostrophe prefix so "O'Malley"/"D'Angelo" read as one name
    # token (apostrophes are already folded to ASCII "'" upstream).
    _NAME_TOK = r"[A-Z](?:'[A-Z])?[a-zà-ÿ][a-zA-Zà-ÿ'\-]*"
    # An initial is a capital letter STANDING ALONE.  Without that, the four capitals of
    # an acronym read as four initials — "NIST." and "ICLR" become an author list, the
    # run leaps over the venue of one reference and swallows the authors of the next,
    # and the boundary lands a whole entry early.
    _INIT_TOK = r"(?<![A-Za-z])[A-Z]\."
    _BARE_INIT = r"(?<![A-Za-z])[A-Z](?![A-Za-z])"   # "William B Dolan"
    _TOK = rf"(?:{_NAME_TOK}|{_INIT_TOK}|{_BARE_INIT})"
    _NOT_NAME_START = (
        r"(?!In\s|The\s|This\s|These\s|We\s|It\s|They\s|Our\s|"
        r"A\s|An\s|For\s|With\s|Using\s|Both\s|All\s|Each\s|"
        r"Some\s|Many\s|More\s|Most\s|No\s|New\s|Two\s|One\s|"
        r"arXiv\s|Also\s|Its\s|Their\s|Our\s|"
        r"Journal\s|Computational\s|International\s|Conference\s|"
        r"Proceedings\s|Association\s|Technical\s)"
    )
    # An author list ends in one of two ways, and the bibliography's style decides
    # which — so both are boundaries, and nothing else is.
    #
    # (a) The entry DATES ITSELF right after the names: "Ajzen, I. (1998).",
    #     "American Psychiatric Association. (1994).", "Kristina Toutanova. 2019.",
    #     "Crane, C. (in press).".
    #
    # (b) The style dates the entry at the END instead ("… In ICLR, 2021."), so the
    #     names are followed by nothing but the title: "Samira Abnar and Willem
    #     Zuidema. Quantifying attention flow…".  Capitalised words plus a comma are
    #     not enough to call that an author list: "Behaviour Research and Therapy, 39,
    #     373–393." has exactly that shape, and an APA bibliography ends nearly every
    #     entry with one — so the boundary landed in the middle of the references
    #     instead of between them, and each "entry" came out as the tail of one
    #     reference glued to the head of the next.  What tells them apart is the
    #     VOLUME: a journal puts a number where the rest of the names would stand.
    #
    # Neither rule spells the author list out.  Spelling it out is what breaks: the
    # names carry every alphabet there is, and PDF extraction breaks them further —
    # "Łukasz Kaiser", "Lo¨ıc Barrault", "van der Maaten", "deLeon, V.", "et al.".  A
    # run that has to recognise each of those loses the boundary on the first one it
    # has never seen.  So the run only has to be free of the two things an author list
    # cannot contain: a DIGIT (that is the volume, the year, the page), and a period
    # that is not an INITIAL's — an initial's period follows a single letter, a
    # sentence's period follows a word.  That one distinction is what stops the run
    # from crossing out of the entry: in "Cognition & Emotion. Smith, J. (2001)" the
    # period after the venue follows "on", so the run dies there instead of reading the
    # venue and the next author as one list.
    #
    # What closes the author list is only ever LOOKED at, never eaten: the period that
    # ends one entry is the period that opens the next boundary, and a match that
    # consumed it took the following reference's boundary down with it — ". Curran
    # Associates, Inc." reads like an author list, and the entry standing behind it was
    # never even tried.
    #
    # And the period that OPENS a boundary is never an initial's own period, or a
    # wrapped "Williams, J.\nM. G., Golden, A.-M." is split down the middle of a name.
    _LETTER = r"[^\W\d_]"                                    # a letter, any script
    _INIT_DOT = rf"(?<!{_LETTER}{_LETTER})\."                # the period of an initial, not of a sentence
    # The run crosses a line break: an author list is where a bibliography wraps.
    _RUN_CHAR = r"[^\W\d_]|[\s,&'\-¨´`^~]"
    _AUTHOR_RUN = _TOK + rf"(?:{_RUN_CHAR}|{_INIT_DOT})*"
    _NAME_DELIM = r"\s*(?:,| and | & )"
    # An author list may open on a lower-case name particle — "de Decker, A. (2001)",
    # "van Minnen, N. (2005)", "von Restorff" — where the capital that _TOK demands
    # sits on the SECOND word.  Without this the boundary before such a name is
    # invisible: the entry merges into the one above it and, dated by the year that
    # follows, files itself under the wrong surname (a phantom "(Dalgleish, 2001)"
    # built from Dalgleish's "(in press)" glued to de Decker's 2001).  Only the known
    # particles, only in lower case, and only when a capitalised name follows — ". de
    # facto results" cannot match it, because the word after the particle is not a
    # _TOK.  Longer particles first so the alternation prefers "van der" over "van".
    _LEAD = (r"(?:(?:van der|van den|van de|de la|de|del|della|van|von|der|den|"
             r"du|da|di|dos|ter|ten|bin|ibn)\s+){1,2}")
    _BOUNDARY_RE = re.compile(
        r"(?<!\b[A-Z])\.\s+"
        + _NOT_NAME_START
        + r"(?:"
        + rf"(?:{_LEAD})?" + _AUTHOR_RUN
        # (a) the names, then the date.  A dash never introduces a date, it glues a
        # number to a title word — without this, "Semeval-2017 task 1" is an author
        # dated 2017 and the title becomes a reference of its own.
        + rf"(?<![-‐-―−])(?=\.?\s*\(?\s*(?i:{_AY_ENTRY_DATE}))"
        + r"|"
        + rf"(?:{_LEAD})?" + _TOK + r"(?:\s+" + _TOK + r")+" + _NAME_DELIM
        + r"(?=[^.\d]*\.)"                                   # (b) the names, then a period, with no volume in between
        + r")"
    )

    # Drop running-header / page-number lines the PDF interleaves between
    # entries before the boundary scan; otherwise the period → next-author
    # transition is hidden behind them and two references merge into one.
    furniture = _page_furniture_lines(biblio)
    _kept = [ln for ln in biblio.splitlines()
             if not _is_biblio_noise_line(_clean_biblio_line(ln))
             and ln.strip() not in furniture]
    text = "\n".join(_kept).strip() if _kept else biblio.strip()
    boundary_refs: list[str] = []
    if _BOUNDARY_RE.search(text):
        # Split at each boundary.  The boundary period belongs to the
        # preceding reference; everything after it (whitespace + author
        # list) starts the next reference.
        splits: list[str] = []
        prev_end = 0
        for m in _BOUNDARY_RE.finditer(text):
            # Capture from the previous boundary (or start of text) to the
            # period that ends the current reference.
            ref_end = m.start() + 1  # include the period
            splits.append(text[prev_end:ref_end].strip())
            prev_end = ref_end
        # Last reference: from the last period to end of text.
        splits.append(text[prev_end:].strip())
        # Rejoin yearless mis-splits rather than dropping them, then keep the
        # references (each now carries a year).
        year_splits = _coalesce_yearless_splits(splits)
        if len(year_splits) >= 3:
            boundary_refs = year_splits

    # --- Pass 2: line-based, where a reference starts a line ---
    refs: list[str] = []
    buf: list[str] = []
    raw_lines = biblio.splitlines()
    starts = _authoryear_entry_starts(raw_lines)
    for i, raw_line in enumerate(raw_lines):
        line = _clean_biblio_line(raw_line)
        if _is_biblio_tail_line(line):
            break
        if _is_biblio_noise_line(line) or raw_line.strip() in furniture:
            continue
        if i in starts and buf:
            refs.append(_merge_hyphen_wraps(" ".join(buf)))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        refs.append(_merge_hyphen_wraps(" ".join(buf)))
    line_refs = _coalesce_yearless_splits(refs)

    # The two passes read the same bibliography through different windows, and each is
    # blind where the other sees.  Pass 1 only ever cuts at a PERIOD, so a reference
    # whose predecessor does not end in one — the entry the extractor truncated at
    # "SUNY Press,", the one behind a running header, the one after a bare URL — is
    # invisible to it, and merges into its neighbour without a sound.  Pass 2 only ever
    # cuts at a LINE START, so it cannot see two entries sharing a line at all.
    # Whichever pass separated more references saw boundaries the other one missed, and
    # a boundary missed is a reference that nothing can ever cite.  A boundary imagined
    # would be the worse error — but a cut inside an entry leaves a fragment carrying no
    # year, and those have already been rejoined above, in both passes alike.
    if len(boundary_refs) > len(line_refs):
        return boundary_refs
    return line_refs


def _truncate_bleed(raw_entries: list[str]) -> list[str]:
    """Truncate outlier entries that have a large prose tail after the last
    bibliographic structural element.

    Watch-item 3: preserves list length and order — entries ≤ threshold are
    returned unchanged, elements are never dropped or merged, only truncated
    in-place.  The numbered path in _parse_references depends on this invariant
    for its num↔raw alignment via zip()."""

    if len(raw_entries) < 3:
        return list(raw_entries)  # too few entries to establish a median

    lengths = [len(e) for e in raw_entries]
    median = sorted(lengths)[len(lengths) // 2]
    threshold = max(700, 3 * median)

    # Combined structural-element pattern — find ALL bib markers in one pass.
    # Anchored to STRONG elements only: DOI, URL, page-range, vol(issue):pages,
    # publisher.  These do not occur in ordinary body prose, unlike bare
    # punctuated years which are everywhere in prose tails and would shift
    # last_bib_end to the wrong end of the entry.
    _BIB_ELEMENT_RE = re.compile(
        r"10\.\d{4,9}/\S+"              # DOI
        r"|https?://[^\s)]+"             # URL
        r"|\b(?:pp?\.|pages?)\s*\d+\s*[–\-]\s*\d+"  # page range pp. 123-145 or pages 434-443
        r"|\b\d+\s*\(\s*\d+\s*\)\s*[:,]\s*\d+"  # volume(issue):pages
        r"|\b(?:Press|Wiley|Springer|Elsevier|Routledge|MIT\s+Press|SAGE|University\s+Press)\b"  # publisher
    )

    result: list[str] = []
    for entry in raw_entries:
        if len(entry) <= threshold:
            result.append(entry)
            continue

        # Find the last structural element position.
        last_bib_end = 0
        for m in _BIB_ELEMENT_RE.finditer(entry):
            last_bib_end = max(last_bib_end, m.end())

        if last_bib_end == 0:
            # No bib element found — leave unchanged (watch-item 2: fails safe).
            result.append(entry)
            continue

        tail_len = len(entry) - last_bib_end
        if tail_len > len(entry) * 0.5:
            # More than half the entry is prose after the last structural element.
            # Truncate at last_bib_end + small buffer for trailing punctuation.
            result.append(entry[: last_bib_end + 20])
        else:
            result.append(entry)

    return result


def _merge_standalone_doi_url(refs: list[dict]) -> list[dict]:
    """Fold entries that are just a bare DOI/URL (split off by PDF text
    extraction) back into the preceding reference, and discard empty entries.

    Applies to every parsing path: numbered, author-year, and the one-line-per
    entry fallback (the fallback is where standalone DOI lines actually surface).
    """
    merged: list[dict] = []
    for ref in refs:
        raw = (ref.get("raw_entry") or "").strip()
        is_doi_url = (
            raw.startswith("https://doi.org/")
            or raw.startswith("http://doi.org/")
            or re.match(r"^10\.\d{4,9}/", raw)
        )
        if is_doi_url and merged:
            prev = merged[-1]
            _clean_merge = re.sub(r"\s*[–—-]\s*", "-", raw)
            prev["raw_entry"] = (prev["raw_entry"] or "") + "  " + raw
            gained_id = False
            if not prev.get("doi"):
                m = re.search(r"(10\.\d{4,9}/\S+)", _clean_merge)
                if m:
                    prev["doi"] = m.group(1).rstrip(".,;")
                    gained_id = True
            if not prev.get("url") and raw.startswith("http"):
                prev["url"] = _clean_merge.rstrip(".")
                gained_id = True
            # A newly merged DOI/URL can upgrade the source classification (e.g.
            # 'unknown' -> 'article'); re-run it on the combined entry.
            if gained_id:
                cls = _classify_reference(
                    prev["raw_entry"], doi=prev.get("doi"), pmid=prev.get("pmid"),
                    isbn=prev.get("isbn"), url=prev.get("url"))
                prev["source_type"] = cls["source_type"]
                prev["source_kind"] = cls["source_kind"]
                prev["source_type_confidence"] = cls["source_type_confidence"]
                prev["source_type_evidence"] = cls["source_type_evidence"]
                prev["indexability"] = cls["indexability"]
        elif not raw:
            continue  # discard completely empty entries
        else:
            merged.append(ref)
    return merged


def _trim_interposed_block(chunk: str) -> str:
    """Drop a post-biblio block (Acknowledgments, Author Contributions, ...) that a
    two-column PDF interposes inside a reference entry.

    When the reference list is paused by such a block and resumes afterwards, the
    block text lands between one numbered entry and the next, so it would pollute
    the raw text of the preceding reference.  We cut the chunk at the first line
    that is exactly a post-biblio heading or an explicit publisher tail label.
    The trim is local to this entry chunk: a two-column layout may interpose the
    label before later numbered references, which must remain independently
    parseable.
    """
    lines = chunk.split("\n")
    for k, ln in enumerate(lines):
        normalized = " ".join(ln.split())
        stripped = normalized.lower().rstrip(":.")
        if (
            stripped in parser_text._POST_BIBLIO_HEADINGS
            or _is_biblio_tail_line(normalized)
        ):
            return "\n".join(lines[:k])
    return chunk


def default_parse_references(biblio: str, meta=None) -> list[dict]:
    """Splits the bibliography into numbered entries and extracts readable fields.

    source_type detection is heuristic and declared as such: article if there is a DOI
    or a journal-like pattern; webpage if there is a URL with 'accessed'/date; book if
    there is an ISBN or a publisher-like pattern. Otherwise 'unknown'. Actual existence
    verification is done by resolve.py; style conformance by the style/ modules.
    """
    # Chicago Notes-Bibliography and law reviews: the numbered references live in per-page
    # footnotes (recovered by the extractor into meta), and the superscript in-text numbers
    # resolve against them — not against any unnumbered end bibliography.  The footnote
    # extractor only sets these for a genuine citation apparatus (see footnotes.py), so an
    # author-year article's explanatory footnotes never reach here.
    if isinstance(meta, dict) and meta.get("footnote_references"):
        try:
            from core.parse import footnotes as _footnotes
        except ImportError:  # pragma: no cover - import path fallback
            import footnotes as _footnotes
        # The alphabetical end bibliography (this `biblio`) lists the same
        # sources in fuller Chicago form; pass it so abbreviated footnotes can
        # borrow a clean quoted title / DOI.
        note_refs = _footnotes.build_note_references(
            meta["footnote_references"], end_bibliography=biblio)
        if note_refs:
            return note_refs
    text = biblio.strip()
    if not text:
        return []
    # Entry marker: [n] | n. | n) | (n) | nX. at the start of a line (see
    # _NUM_MARKER for the per-style variants).
    entry_re = re.compile(r"(?m)^\s*" + _NUM_MARKER)
    matches = list(entry_re.finditer(text))
    refs = []
    # If the matched numbers don't look like a real reference-numbering
    # sequence (e.g. they are page numbers or years split across PDF lines),
    # fall through to the author-year path.
    _numbered_ref_ok = False
    if matches:
        first_num = _entry_num(matches[0])
        if first_num <= 20:
            _numbered_ref_ok = True
        elif len(matches) >= 5:
            # Check for a consecutive-ish sequence
            nums = [_entry_num(m) for m in matches[:10]]
            gaps = sum(1 for a, b in zip(nums, nums[1:]) if b - a not in (1, 2))
            if gaps <= 1:
                _numbered_ref_ok = True
    if not _numbered_ref_ok:
        matches = []
    if not matches:
        hanging_refs = _split_hanging_authoryear_references(text)
        chicago_hanging = bool(hanging_refs and len(hanging_refs) >= 5)
        ay_refs = hanging_refs if chicago_hanging else _split_authoryear_references(text)
        if len(ay_refs) >= 2:
            ay_refs = _truncate_bleed(ay_refs)
            ay_refs = [r for r in ay_refs if not _is_submission_footer(r)]
            for i, raw in enumerate(ay_refs, 1):
                refs.append(_make_reference(i, raw, chicago_hanging=chicago_hanging))
            return _merge_standalone_doi_url(refs)
        # Last-resort fallback: one entry per non-empty line, numbered in order.
        lines = [l for l in text.split("\n") if l.strip()]
        lines = _truncate_bleed(lines)
        lines = [l for l in lines if not _is_submission_footer(l)]
        for i, line in enumerate(lines, 1):
            refs.append(_make_reference(i, line.strip()))
        return _merge_standalone_doi_url(refs)
    # Bibliography numbering is strictly increasing, so drop entry markers whose
    # number leaps outside the sequence: a page range wrapped across a PDF line
    # ("293 (2203-\n2209) ...") exposes a bare "2209)" at line start that the
    # entry regex would otherwise treat as reference #2209, splitting the real
    # reference in two.  Dropping it keeps the fragment merged into its entry.
    kept = [matches[0]]
    _last_num = _entry_num(matches[0])
    for m in matches[1:]:
        n = _entry_num(m)
        if _last_num < n <= _last_num + 3:
            kept.append(m)
            _last_num = n
    matches = kept
    # Numbered path: extract raw entries, truncate outlier entries, then build refs.
    raw_entries = []
    for i, m in enumerate(matches):
        num = _entry_num(m)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = _trim_interposed_block(text[start:end])
        raw_entries.append((num, " ".join(chunk.split())))
    cleaned = _truncate_bleed([r[1] for r in raw_entries])
    for (num, _), raw in zip(raw_entries, cleaned):
        if _is_submission_footer(raw):
            continue
        refs.append(_make_reference(num, raw))
    return _merge_standalone_doi_url(refs)
