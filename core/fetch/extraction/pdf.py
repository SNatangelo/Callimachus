#!/usr/bin/env python3
# core/fetch/extraction/pdf.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
pdf.py - PDF text extraction with two-column layout detection.

pdfminer.six gives us bounding-box coordinates for each text block, which lets us
detect two-column layouts and merge them in correct reading order (left column
top-to-bottom, then right column top-to-bottom) rather than reading across rows.

Fallback chain:
  1. pdfminer.six  (coordinate-aware, handles most scholarly PDFs)
  2. pdftotext -layout  (poppler, if installed)
  3. stdlib zlib+BT/ET extractor  (last resort, best-effort)

Used by:
  - fetch.py (OA downloads via Unpaywall)
  - extract.py (manuscript PDF input - replaces the old two-step fallback)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import zlib
from collections import Counter


# Minimum text quality to trust the extraction result.
_MIN_CHARS = 200
_MIN_ALPHA_RATIO = 0.35
_MIN_ALPHA_TOKENS = 50
_CID_MARKER_RE = re.compile(r"\(cid:\d+\)")
_WORD_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ]{3,}")
_STRUCT_REPEATED_LINE_MIN = 5
_STRUCT_CHARS_PER_TOKEN_MAX = 10.0
_STRUCT_ALPHA_RATIO_MIN = 0.55
_STRUCT_FRAGMENTED_LINE_MIN_LINES = 200
_STRUCT_FRAGMENTED_LINE_RATIO_MIN = 0.80
_STRUCT_FRAGMENTED_LINE_RUN_MIN = 50
# ``fragmented_lines`` fires for at least 200 alphabetic lines when >=80% are
# one/two-token fragments and a run of >=50 such lines occurs consecutively.
_COMMON_WORDS = {
    "the", "and", "of", "to", "in", "for", "with", "that", "is",
    "are", "on", "by", "this", "from", "as", "or", "an", "be",
}
_FALLBACK_WARNING_LOCK = threading.Lock()
_FALLBACK_WARNING_EMITTED = False


def fulltext_material_issue(text: str, ref: dict | None = None) -> str | None:
    """Return a deterministic reason when readable text is not the cited body.

    Text quality alone cannot distinguish an article from a repository cover
    sheet or a review that merely discusses the cited book.  These checks use
    document structure, and deliberately require multiple signals so that an
    article preceded by a repository cover page remains eligible.
    """
    text = text or ""
    normalized = " ".join(text.casefold().split())
    tail = " ".join(text[-1800:].casefold().split())
    cover_markers = (
        "citation for published version",
        "document status and date",
        "please check the document version of this publication",
        "general rights",
    )
    if (
        all(marker in normalized for marker in cover_markers)
        and "take down policy" in tail
        and "download date" in tail
    ):
        return "repository_cover_sheet_only"

    front_matter = re.split(r"\babstract\b", text[:3000], maxsplit=1, flags=re.IGNORECASE)[0]
    leading_lines = [
        " ".join(line.casefold().split())
        for line in front_matter.splitlines()[:25]
        if line.strip()
    ]
    has_isbn_marker = any(
        re.search(r"\bisbn(?:-?(?:10|13))?\b", line)
        for line in leading_lines
    )
    is_book_review_section = any(
        line in {"book review", "book reviews"}
        or re.search(r"\brezension\s+über:", line)
        for line in leading_lines
    ) or (
        has_isbn_marker
        and any(line.startswith("reviewed by:") for line in leading_lines)
    )
    cited_text = " ".join(
        str((ref or {}).get(key) or "") for key in ("title", "raw_entry")
    ).casefold()
    citation_identifies_review = bool(
        re.search(
            r"\bbook\s+reviews?\b|\breview\s+of\b|\brezension\s+über\b",
            cited_text,
        )
    )
    if is_book_review_section and not citation_identifies_review:
        return "book_review_not_cited_work"

    # Some publisher review pages do not expose a ``Book review`` heading in
    # extracted text.  Treat this as incompatible only when both the review
    # byline and the publisher's republication notice establish the document
    # type.  A bare reviewer byline is common enough in article front matter
    # that it is not a signal on its own.
    has_reviewer_byline = bool(re.search(
        r"(?<![A-Za-z])reviewer\s*:", front_matter, re.IGNORECASE
    ))
    has_review_republication_notice = (
        "this review was originally published" in tail
    )
    if (
        has_reviewer_byline
        and has_review_republication_notice
        and not citation_identifies_review
    ):
        return "book_review_not_cited_work"

    has_supplementary_heading = "supplementary information" in leading_lines
    has_supplementary_information_relation = any(
        line == "supplementary information for"
        or line.startswith("supplementary information for ")
        for line in leading_lines
    )
    has_supplementary_methods_heading = "supplementary methods" in leading_lines
    has_supplementary_material_heading = "supplementary material:" in leading_lines
    citation_identifies_supplement = bool(
        re.search(
            r"\b(?:supplementary|supporting)\s+information\b"
            r"|\bsupplementary\s+methods?\b"
            r"|\bsupplementary\s+material\b",
            cited_text,
        )
    )
    if (
        (
            (has_supplementary_heading and has_supplementary_information_relation)
            or has_supplementary_methods_heading
            or has_supplementary_material_heading
        )
        and not citation_identifies_supplement
    ):
        return "supplementary_information_not_cited_work"

    # This exact end-of-page New York Times access-verification notice can
    # contain enough readable boilerplate to pass text quality.  Keep all
    # clauses together so ordinary prose mentioning access, reader mode, or a
    # subscription remains eligible.
    has_access_verification = (
        "thank you for your patience while we verify access" in tail
    )
    has_reader_mode = (
        "if you are in reader mode please exit and log into your times account"
        in tail
    )
    has_subscription_control = "or subscribe for all of the times" in tail
    if (
        has_access_verification
        and has_reader_mode
        and has_subscription_control
    ):
        return "access_verification_shell_not_cited_work"

    # A rendered viewer's first page is not a complete cited document.  The
    # page counter, normal-view switch, and terminal paragraph/save controls
    # must all be present before rejecting it; each appears alone in genuine
    # document interfaces and source text.
    page_match = re.search(
        r"\bprevious\s+1\s*/\s*(\d+)\s+next\b", text[:3000], re.IGNORECASE
    )
    has_multiple_pages = bool(page_match and int(page_match.group(1)) > 1)
    has_normal_view = bool(re.search(r"\bnormal\s+view\b", text, re.IGNORECASE))
    terminal_controls = text[-1800:]
    has_terminal_controls = (
        bool(re.search(
            r"\bselect\s+target\s+paragraph(?:\d+)?\b",
            terminal_controls,
            re.IGNORECASE,
        ))
        and bool(re.search(r"\bsave\b", terminal_controls, re.IGNORECASE))
    )
    if has_multiple_pages and has_normal_view and has_terminal_controls:
        return "paginated_viewer_first_page_only"
    return None


def _quality(text: str) -> bool:
    normalized = _CID_MARKER_RE.sub(" ", text or "")
    if len(normalized) < _MIN_CHARS:
        return False
    alpha = sum(c.isalpha() for c in normalized)
    if alpha / len(normalized) < _MIN_ALPHA_RATIO:
        return False
    # Reject header-only / page-number extracts that can look alphabetic enough
    # to pass the coarse ratio gate while containing far too little real text.
    tokens = _WORD_TOKEN_RE.findall(normalized)
    word_tokens = [tok.lower() for tok in tokens if tok.lower() != "cid"]
    if len(word_tokens) < _MIN_ALPHA_TOKENS:
        return False
    common_hits = sum(1 for tok in word_tokens if tok in _COMMON_WORDS)
    if (common_hits / len(word_tokens)) >= 0.02:
        return True
    vowel_counts = [sum(ch in "aeiouy" for ch in tok) for tok in word_tokens]
    two_vowel_ratio = sum(1 for count in vowel_counts if count >= 2) / len(vowel_counts)
    no_vowel_ratio = sum(1 for count in vowel_counts if count == 0) / len(vowel_counts)
    # English-centric word hits are a positive signal, but low English hit-rate alone
    # is not enough to reject legitimate full text in other Latin-alphabet languages.
    if two_vowel_ratio >= 0.35 and no_vowel_ratio <= 0.20:
        return True
    # Reject garbage that happens to meet the token-count minimum but has no
    # real readability signal.  A text that fails both the common-word path
    # and the vowel-distribution path is not trustworthy text regardless of
    # how many word-shaped tokens it contains.
    return False


def _quality_metrics(text: str) -> dict:
    """Small, serialisable metrics used to compare extraction variants."""
    normalized = _CID_MARKER_RE.sub(" ", text or "")
    chars = len(normalized)
    alpha = sum(c.isalpha() for c in normalized)
    word_tokens = [
        token for token in _WORD_TOKEN_RE.findall(normalized)
        if token.lower() != "cid"
    ]
    return {
        "chars": chars,
        "alpha_ratio": round(alpha / max(chars, 1), 4),
        "word_tokens": len(word_tokens),
    }


def structure_flags(text: str) -> list[str]:
    """Return deterministic signs that extracted PDF layout is damaged.

    ``_quality`` answers whether text is readable prose at all.  These signals
    instead catch structurally damaged yet readable-looking output, such as
    interleaved columns, repeated running headers, or whitespace bloat.
    """
    flags = set()
    normalized = _CID_MARKER_RE.sub(" ", text or "")
    chars = len(normalized)
    if not chars:
        return []
    alpha = sum(c.isalpha() for c in normalized)
    if alpha / chars < _STRUCT_ALPHA_RATIO_MIN:
        flags.add("low_alpha_ratio")
    word_tokens = [
        tok for tok in _WORD_TOKEN_RE.findall(normalized)
        if tok.lower() != "cid"
    ]
    if word_tokens and chars / len(word_tokens) > _STRUCT_CHARS_PER_TOKEN_MAX:
        flags.add("whitespace_bloat")
    lines = []
    # When the caller preserved page boundaries, a repeated line is suspicious
    # only when it recurs on several distinct pages.  Counting raw occurrences
    # made legitimate table rows repeated within one page look like running
    # headers (Attention/Vilt regression audit, 2026-07-23).
    page_lines: dict[str, set[int]] = {}
    has_page_boundaries = "\f" in (text or "")
    alpha_lines = []
    for page_index, page in enumerate((text or "").split("\f")):
        for raw_line in page.splitlines():
            line = _CID_MARKER_RE.sub(" ", raw_line).strip()
            if not any(c.isalpha() for c in line):
                continue
            tokens = _WORD_TOKEN_RE.findall(line)
            tokens = [tok for tok in tokens if tok.lower() != "cid"]
            if tokens:
                alpha_lines.append(len(tokens))
            # Repeated-line detection is for prose/header lines, not formula rows.
            if len(line) > 15 and len(tokens) >= 3:
                lines.append(line)
                page_lines.setdefault(line, set()).add(page_index)
    if len(alpha_lines) >= _STRUCT_FRAGMENTED_LINE_MIN_LINES:
        short = [count <= 2 for count in alpha_lines]
        longest_run = current_run = 0
        for is_short in short:
            current_run = current_run + 1 if is_short else 0
            longest_run = max(longest_run, current_run)
        short_ratio = sum(short) / len(short)
        if (short_ratio >= _STRUCT_FRAGMENTED_LINE_RATIO_MIN
                and longest_run >= _STRUCT_FRAGMENTED_LINE_RUN_MIN):
            flags.add("fragmented_lines")
    if lines:
        if has_page_boundaries:
            repeated = max((len(pages) for pages in page_lines.values()), default=0)
        else:
            # Page-less text still needs repeated-line detection, using the
            # same 15-occurrence threshold as page-aware extraction.
            repeated = Counter(lines).most_common(1)[0][1]
        if repeated >= _STRUCT_REPEATED_LINE_MIN:
            flags.add("repeated_line")
    return sorted(flags)


# --------------------------------------------------------------------------- #
#  pdfminer.six extractor                                                     #
# --------------------------------------------------------------------------- #

def _pdfminer_extract(path: str, page_sep: str = "\n\n") -> str:
    """Extract text via pdfminer.six with two-column layout detection.
    Raises ImportError if the library is not installed."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextBox

    page_texts: list[str] = []
    for page_layout in extract_pages(path):
        boxes: list[tuple[float, float, float, float, str]] = []
        for element in page_layout:
            if isinstance(element, LTTextBox):
                text = element.get_text().strip()
                if text:
                    # (x0, y0, x1, y1, text) - y0 is bottom in PDF coordinates
                    boxes.append((element.x0, element.y0, element.x1, element.y1, text))

        if not boxes:
            continue

        # Two-column heuristic: look for a gap in the horizontal centre.
        # We measure how many boxes have their horizontal centre in the middle
        # third of the page. A genuine gap there -> two-column layout.
        x_centres = [(b[0] + b[2]) / 2.0 for b in boxes]
        x_min, x_max = min(x_centres), max(x_centres)
        page_w = x_max - x_min

        two_col = False
        if page_w > 200:
            lo = x_min + page_w * 0.35
            hi = x_min + page_w * 0.65
            mid_count = sum(1 for x in x_centres if lo <= x <= hi)
            two_col = mid_count < len(boxes) * 0.15

        if two_col:
            mid_x = (x_min + x_max) / 2.0
            left = sorted(
                [b for b in boxes if (b[0] + b[2]) / 2.0 < mid_x],
                key=lambda b: -b[1],   # descending y (top first in PDF coords)
            )
            right = sorted(
                [b for b in boxes if (b[0] + b[2]) / 2.0 >= mid_x],
                key=lambda b: -b[1],
            )
            ordered = left + right
        else:
            ordered = sorted(boxes, key=lambda b: (-b[1], b[0]))

        page_texts.append("\n".join(b[4] for b in ordered))

    return page_sep.join(p for p in page_texts if p)


# --------------------------------------------------------------------------- #
#  pymupdf (fitz) extractor                                                    #
# --------------------------------------------------------------------------- #

def _line_chars(page) -> "list[list[dict]]":
    """The glyphs of the page, line by line, in reading order."""
    lines = []
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            chars = [ch for span in line.get("spans", []) for ch in span.get("chars", [])]
            if chars:
                lines.append(chars)
    return lines


def _spaces_are_phantom(doc, sample_pages: int = 4) -> bool:
    """True when this PDF's space characters are not spaces at all.

    Some producers set every word with the glyphs individually placed and then
    scatter space characters through the middle of them — a different point size,
    no advance of their own, drawn ON TOP of the letter that follows.  The
    extractor believes them, and the paper comes out shattered: "Adkins-Rega n
    1990", "Obstet Gyn ecol". Nothing downstream survives that — a name broken in
    two matches no reference, and the entry it belonged to is cited by nobody.

    A space that a typesetter meant is followed by the next letter; a phantom one
    OVERLAPS it.  That is the whole test, and it costs nothing to ask."""
    phantom = total = 0
    for i in range(min(sample_pages, doc.page_count)):
        for chars in _line_chars(doc[i]):
            for cur, nxt in zip(chars, chars[1:]):
                if not cur["c"].isspace():
                    continue
                total += 1
                if nxt["bbox"][0] < cur["bbox"][2] - 0.5:
                    phantom += 1
    return total >= 50 and phantom > total / 2


def page_blocks_reader(doc):
    """The way this document's blocks must be read, as a callable page -> blocks.

    A producer that scatters phantom spaces through its words breaks every reader
    that believes the text stream, not just the one that happens to be measured.
    The repair therefore belongs to the document: ask once here, and every route
    into the same PDF - body extraction, bibliography re-extraction, the hyperlink
    layer that slices reference entries out of the page - reads it the same way.

    Returns ``page.get_text("blocks")`` unchanged for a normal PDF, so a caller can
    use it unconditionally.
    """
    repair = False
    try:
        repair = _spaces_are_phantom(doc)
    except Exception:
        repair = False
    if not repair:
        return lambda page: page.get_text("blocks")
    def _read(page):
        try:
            return _blocks_from_glyphs(page)
        except Exception:
            return page.get_text("blocks")
    return _read


def _is_two_column_editorial_apparatus(block) -> bool:
    """Whether a block is page apparatus rather than column body text.

    Publishers place correspondence/affiliation notes below the left column and
    may overlay an editorial status label across the page.  Column-major sorting
    otherwise puts those blocks between the unfinished left-column tail and its
    right-column continuation.  Keep the apparatus in the extracted text, but
    move it behind the body with a paragraph boundary.
    """
    try:
        text = " ".join(str(block[4] or "").split())
    except (IndexError, TypeError):
        return False
    folded = text.casefold()
    if folded == "retracted article":
        return True
    return bool(
        re.match(r"^\*?\s*correspondence\s*:", text, re.IGNORECASE)
        and ("@" in text or "full author information" in folded)
    )


# A gap between two letters is a space when it is this much of the point size.
# Measured on the glyphs themselves, where the two populations sit far apart:
# inside a word the letters touch (gap ≈ 0.01 em), between two words they stand
# at 0.19 em and up.  The threshold is laid in the empty middle.
_WORD_GAP_RATIO = 0.12


def _blocks_from_glyphs(page) -> list[tuple]:
    """Rebuild the page's blocks from where the letters actually sit.

    The PDF's own spaces are dropped and the words are read back out of the
    geometry, so a phantom space cannot break a word in half.  The block boxes
    are pymupdf's own, so the two-column ordering below reads them unchanged."""
    out: list[tuple] = []
    for block in page.get_text("rawdict").get("blocks", []):
        if block.get("type", 0) != 0:      # an image block carries no text
            continue
        lines: list[str] = []
        for line in block.get("lines", []):
            buf: list[str] = []
            prev_x1 = prev_size = None
            for span in line.get("spans", []):
                size = span.get("size") or 10.0
                for ch in span.get("chars", []):
                    if ch["c"].isspace():
                        continue
                    x0, _, x1, _ = ch["bbox"]
                    if prev_x1 is not None and (x0 - prev_x1) > _WORD_GAP_RATIO * prev_size:
                        buf.append(" ")
                    buf.append(ch["c"])
                    prev_x1, prev_size = x1, size
            if buf:
                lines.append("".join(buf))
        if lines:
            x0, y0, x1, y1 = block["bbox"]
            out.append((x0, y0, x1, y1, "\n".join(lines), block.get("number", 0)))
    return out


# ----- rotated (landscape) text ------------------------------------------- #
#
# A landscape table is printed rotated 90 degrees: every one of its lines
# declares a writing direction of (0, -1) — the PDF says so per line, in
# page.get_text("dict").  Read those lines in the page's upright order and the
# table comes out shredded: the reading axis is -y, the lines stack along +x,
# so a row-major read walks ACROSS the table's cells and a cell wrapped onto a
# second line ("Kuyken & Dalgleish" / "(1995)") is split apart with a
# neighbouring column's text in between.  The citation inside that cell then
# matches nothing, and the work it names is reported uncited.
#
# The repair is geometric, not heuristic.  For each rotated direction, a
# visual table COLUMN is a band of lines whose reading axis STARTS at the same
# coordinate (y1 for dir=(0,-1): the start, because the END y0 moves with the
# line's length).  Cluster the lines into those bands and sort each band along
# the stacking axis (+x for dir=(0,-1)): that walks DOWN the column, and a
# wrapped cell's halves land on adjacent lines again — the same column-major
# trick the bibliography recovery plays for two-column back matter.  A rotated
# paragraph costs nothing extra: its lines all start at the same margin, so
# they are one band, and the band's stacking order IS the paragraph.
#
# Only the three cardinal rotations are read this way.  Upright text is left
# to the block machinery, and a diagonal direction (a watermark, a slanted
# figure label) stays in its block: it is decoration, not a region with a
# reading order of its own.  A page with no rotated lines is untouched by
# construction — the direction is the PDF's own declaration, not a guess.

_ROTATED_DIRS = {(0.0, -1.0), (0.0, 1.0), (-1.0, 0.0)}

# Two rotated lines whose reading axes start within this many points of each
# other sit in the same column band.  Table columns stand tens of points
# apart; a wrapped cell's continuation starts back at its column's margin.
_STRIP_BAND_TOL = 8.0

# For each rotated direction: where a line's reading axis STARTS (constant
# down a visual column), and the coordinate that advances from one stacked
# line to the next (ascending = reading down the column).
_STRIP_GEOMETRY = {
    (0.0, -1.0): (lambda b: b[3], lambda b: b[0]),     # start y1, stack +x
    (0.0, 1.0): (lambda b: b[1], lambda b: -b[2]),     # start y0, stack -x
    (-1.0, 0.0): (lambda b: b[2], lambda b: -b[3]),    # start x1, stack -y
}

# Sentinel block number marking a rotated column strip: the strips are their
# own region and must never be re-ordered by the upright page layout logic.
# A strip's seventh field is its ANCHOR — the smallest block number its lines
# came from — so the strip can rejoin the page where the producer put the
# text: a figure's rotated axis label stays with its figure instead of
# drifting to the end of the page, between two bibliography entries.
_STRIP_BLOCK_NO = -1


def _rotated_strip_blocks(rotated: dict) -> list[tuple]:
    """One synthetic block per column band of rotated lines, in band order."""
    strips: list[tuple] = []
    for direction in sorted(rotated):
        start_of, stack_of = _STRIP_GEOMETRY[direction]
        lines = sorted(rotated[direction], key=lambda it: start_of(it[0]))
        bands: list[list[tuple]] = []
        prev_start = None
        for item in lines:
            s = start_of(item[0])
            if prev_start is None or s - prev_start > _STRIP_BAND_TOL:
                bands.append([])
            bands[-1].append(item)
            prev_start = s
        for band in bands:
            band.sort(key=lambda it: stack_of(it[0]))
            x0 = min(b[0] for b, _, _ in band)
            y0 = min(b[1] for b, _, _ in band)
            x1 = max(b[2] for b, _, _ in band)
            y1 = max(b[3] for b, _, _ in band)
            strips.append((x0, y0, x1, y1,
                           "\n".join(t for _, t, _ in band), _STRIP_BLOCK_NO,
                           min(n for _, _, n in band)))
    return strips


def _blocks_with_rotated_strips(page) -> list[tuple] | None:
    """The page's blocks with its rotated lines re-read along their own axis.

    Returns None when the page has no cardinally-rotated text — the caller
    keeps pymupdf's own blocks, so the common page is not re-built at all.
    Otherwise the upright lines keep their blocks (bbox recomputed from the
    lines that stay) and the rotated lines come back as column-strip blocks,
    appended after the upright ones as a region of their own."""
    upright: list[tuple] = []
    rotated: dict[tuple, list[tuple]] = {}
    for block in page.get_text("dict").get("blocks", ()):
        if block.get("type", 0) != 0:      # an image block carries no text
            continue
        kept: list[tuple] = []
        for line in block.get("lines", ()):
            text = "".join(s.get("text", "") for s in line.get("spans", ()))
            if not text.strip():
                continue
            d = line.get("dir") or (1.0, 0.0)
            direction = (round(d[0], 2), round(d[1], 2))
            if direction in _ROTATED_DIRS:
                rotated.setdefault(direction, []).append(
                    (line["bbox"], text.strip(), block.get("number", 0)))
            else:
                kept.append((line["bbox"], text))
        if kept:
            x0 = min(b[0] for b, _ in kept)
            y0 = min(b[1] for b, _ in kept)
            x1 = max(b[2] for b, _ in kept)
            y1 = max(b[3] for b, _ in kept)
            upright.append((x0, y0, x1, y1,
                            "\n".join(t for _, t in kept), block.get("number", 0)))
    if not rotated:
        return None
    return upright + _rotated_strip_blocks(rotated)


def _furniture_key(text: str) -> str:
    """What a block says, with the numbers taken out.

    The page number changes on every page, the download stamp carries a date, the
    footer a volume — the running head underneath them does not."""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text or "")).strip().lower()


def _strip_page_furniture(page_blocks: list[list[tuple]]) -> list[list[tuple]]:
    """Drop the running heads, page numbers and download stamps: what the page
    carries and the paper does not say.

    They are not merely noise.  A sentence that crosses a page break is cut in half
    by them, and the halves are read with the furniture in between: "(e.g. Jacobs /
    Downloaded from http://... / Journal of Educational Administration / et al.,
    2013)".  The citation no longer exists — the work it named is orphaned, and the
    running head is left standing inside the claim.  Worse, the furniture is read as
    if the paper had written it: "JAMA Network Open. 2026;9(7):e2621705" cites, in
    the issue number, a seventh reference that nobody ever cited.

    Two signals, and BOTH are asked of the same block: furniture REPEATS across
    pages, and it sits OUTSIDE the text.  Repetition alone would take a table cell
    or a numbered equation with it — once the digits are masked those repeat too,
    and they look exactly like a page number.  What tells them apart is where they
    sit: inside the text, where the paper is speaking.  The text is measured from
    the blocks that do NOT repeat, page by page, so nothing has to be known in
    advance about margins, or about this publisher."""
    pages = len(page_blocks)
    if pages < 3:                     # too few pages for repetition to mean anything
        return page_blocks

    seen: dict[str, int] = {}
    for blocks in page_blocks:
        for key in {_furniture_key(b[4]) for b in blocks}:
            seen[key] = seen.get(key, 0) + 1
    # A head that alternates by parity ("JEA 63,5" on the left page, the journal's
    # name on the right) is on a quarter of the pages, not on half.
    repeated = {k for k, n in seen.items() if n >= 3 and n >= 0.25 * pages}
    if not repeated:
        return page_blocks

    out: list[list[tuple]] = []
    for blocks in page_blocks:
        body = [b for b in blocks if _furniture_key(b[4]) not in repeated]
        if not body:
            out.append(blocks)
            continue
        x0 = min(b[0] for b in body)
        y0 = min(b[1] for b in body)
        x1 = max(b[2] for b in body)
        y1 = max(b[3] for b in body)
        out.append([
            b for b in blocks
            if _furniture_key(b[4]) not in repeated
            or not (b[3] <= y0 + 2 or b[1] >= y1 - 2       # above the text, below it,
                    or b[2] <= x0 + 2 or b[0] >= x1 - 2)   # or beside it in the margin
        ])
    return out


def _pymupdf_extract(path: str, page_sep: str = "\n\n") -> str:
    """Extract text via pymupdf (fitz) with block-level two-column detection.

    BLOCKS mode groups text by layout blocks, correctly separating columns
    and preserving internal line order within each block.  This handles
    two-column LaTeX PDFs better than pdfminer.six because pymupdf's block
    detection groups lines within the same visual block together, keeping
    words like ``Col-\\nlobert`` on adjacent lines within one block."""
    import pymupdf as fitz  # pymupdf

    _silence_pymupdf_native_errors(fitz)
    doc = fitz.open(path)
    page_texts: list[str] = []
    phantom_spaces = _spaces_are_phantom(doc)

    # (x0, y0, x1, y1, text, block_no, ...) — the whole document first, because what
    # is furniture on one page can only be told by looking at the others.  The
    # rotated column strips are built here, before the furniture pass: a rotated
    # margin stamp repeats on every page exactly like an upright one, and it can
    # only be recognised where the repetition is visible.
    def _raw_blocks(page):
        if phantom_spaces:
            return _blocks_from_glyphs(page)
        return _blocks_with_rotated_strips(page) or page.get_text("blocks")

    page_blocks = _strip_page_furniture([
        [b for b in _raw_blocks(page) if (b[4] or "").strip()]
        for page in doc
    ])

    for blocks in page_blocks:
        if not blocks:
            continue

        # The rotated strips are a region of their own: they already carry their
        # reading order and must not be re-sorted by the upright layout logic
        # (their bboxes span the page the wrong way and would poison the
        # two-column midline test).  They rejoin the page at their anchor.
        strips = [b for b in blocks if b[5] == _STRIP_BLOCK_NO]
        if strips:
            blocks = [b for b in blocks if b[5] != _STRIP_BLOCK_NO]
        if not blocks:
            page_texts.append("\n".join(b[4].strip() for b in strips if b[4].strip()))
            continue

        # Collect block midpoints for two-column heuristic.
        # pymupdf y0 is top, y1 is bottom — same as PDF coords but inverted
        # from pdfminer (where y0=bottom).  Since we only use relative
        # ordering it doesn't matter, but for clarity: pymupdf y0 < y1.
        block_centres: list[float] = []
        for b in blocks:
            x0, y0, x1, y1 = b[0], b[1], b[2], b[3]
            block_centres.append((x0 + x1) / 2.0)

        x_min = min(block_centres)
        x_max = max(block_centres)
        page_w = x_max - x_min

        two_col = False
        if page_w > 200 and len(blocks) >= 4:
            lo = x_min + page_w * 0.35
            hi = x_min + page_w * 0.65
            mid_count = sum(1 for x in block_centres if lo <= x <= hi)
            two_col = mid_count < len(blocks) * 0.15

        apparatus = []
        if two_col:
            apparatus = [
                block for block in blocks
                if _is_two_column_editorial_apparatus(block)
            ]
            column_blocks = [block for block in blocks if block not in apparatus]
            mid_x = (x_min + x_max) / 2.0
            left = sorted(
                [b for b in column_blocks if (b[0] + b[2]) / 2.0 < mid_x],
                key=lambda b: b[1],  # ascending y (top-to-bottom)
            )
            right = sorted(
                [b for b in column_blocks if (b[0] + b[2]) / 2.0 >= mid_x],
                key=lambda b: b[1],
            )
            ordered = left + right
        else:
            # Keep pymupdf's natural block order.  A (y0, x0) sort looks harmless
            # but reads ACROSS a table: two cells that start at the same height
            # are interleaved, and a cell wrapped onto a second line is torn in
            # half with its neighbour's text in between ("Raes, Hermans,
            # Williams, & | 27 (27) | Eelen (2005)") — the severed tail then
            # matches the wrong reference.  Natural order is what the producer
            # wrote and what get_text("text") reads back; it keeps each block —
            # and each wrapped cell — intact.
            ordered = blocks

        if strips:
            if two_col:
                # The column ordering has already given up the stream order, so
                # the anchors mean nothing here; the strips close the page.
                ordered = ordered + strips
            else:
                # Natural order is ascending block number, so each strip slides
                # in right after the block its lines were declared in.
                ordered = sorted(
                    ordered + strips,
                    key=lambda b: (b[6] if b[5] == _STRIP_BLOCK_NO else b[5],
                                   b[5] == _STRIP_BLOCK_NO))
        body_text = "\n".join(b[4].strip() for b in ordered if b[4].strip())
        apparatus_text = "\n".join(
            b[4].strip() for b in apparatus if b[4].strip()
        )
        page_texts.append(
            "\n\n".join(part for part in (body_text, apparatus_text) if part)
        )

    doc.close()
    return page_sep.join(p for p in page_texts if p)


def _silence_pymupdf_native_errors(fitz) -> None:
    """Suppress native stderr noise while retaining structured extraction failures."""
    tools = getattr(fitz, "TOOLS", None)
    display_errors = getattr(tools, "mupdf_display_errors", None)
    if callable(display_errors):
        display_errors(False)


# --------------------------------------------------------------------------- #
#  Fallback: pdftotext (poppler)                                              #
# --------------------------------------------------------------------------- #

def _pdftotext_extract(path: str) -> str | None:
    if not shutil.which("pdftotext"):
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
    tmp.close()
    try:
        subprocess.run(
            ["pdftotext", "-layout", path, tmp.name],
            check=True,
            capture_output=True,
            timeout=60,
        )
        with open(tmp.name, encoding="utf-8", errors="replace") as f:
            text = f.read()
        return text if text.strip() else None
    except Exception:
        return None
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


# --------------------------------------------------------------------------- #
#  Fallback: stdlib BT/ET stream extractor                                    #
# --------------------------------------------------------------------------- #

def _unescape_pdf_string(b: bytes) -> str:
    s = b[1:-1]
    s = re.sub(rb"\\([0-7]{1,3})", lambda m: bytes([int(m.group(1), 8) & 0xFF]), s)
    for a, c in (
        (rb"\\(", b"("),
        (rb"\\)", b")"),
        (rb"\\n", b" "),
        (rb"\\r", b""),
        (rb"\\t", b" "),
        (rb"\\\\", b"\\"),
    ):
        s = s.replace(a, c)
    return s.decode("latin-1", errors="replace")


def _stdlib_extract(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    lines: list[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.DOTALL):
        data = m.group(1)
        try:
            data = zlib.decompress(data)
        except Exception:
            pass
        if b"BT" not in data:
            continue
        for bt in re.finditer(rb"BT(.*?)ET", data, re.DOTALL):
            parts = [
                _unescape_pdf_string(sm.group(0))
                for sm in re.finditer(rb"\((?:\\.|[^()\\])*\)", bt.group(1))
            ]
            if parts:
                lines.append("".join(parts))
    return "\n".join(lines)


def _warn_fallback_once(path: str) -> None:
    """Report native extraction degradation once per process, even with fetch workers."""
    global _FALLBACK_WARNING_EMITTED
    with _FALLBACK_WARNING_LOCK:
        if _FALLBACK_WARNING_EMITTED:
            return
        _FALLBACK_WARNING_EMITTED = True
    import sys

    source = os.fspath(path)
    deps = check_deps()
    if not deps.get("ok"):
        message = (
            "[pdf] WARNING: no quality PDF text-extraction backend is available "
            f"for {source!r} (pymupdf / pdfminer.six not installed, pdftotext not "
            "on PATH). Falling back to stdlib — table "
            "detection, references and sentence segmentation will be severely degraded. "
            "Install pymupdf (`pip install pymupdf`) or pdfminer.six (`pip install pdfminer.six`) "
            "for best results."
        )
    else:
        message = (
            "[pdf] WARNING: installed PDF text-extraction backends returned no text "
            f"passing the quality gate for {source!r}. The source may be a scan or use "
            "unsupported encoding. Falling back to stdlib as a last resort. This does "
            "not indicate that OCR is missing; eligible OCR is handled separately."
        )
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
#  Dependency check                                                           #
# --------------------------------------------------------------------------- #

def check_deps() -> dict:
    """Check which PDF extraction backends are available.

    Returns a dict with keys ``pdfminer``, ``pdftotext``, ``ok`` and a
    human-readable ``message``.  Call this once at startup so the user knows
    whether extraction quality will degrade before any work begins.
    """
    has_miner = False
    try:
        from pdfminer.high_level import extract_pages  # noqa: F401
        has_miner = True
    except ImportError:
        pass
    except Exception as e:
        # pdfminer.six drags in `cryptography` (for encrypted PDFs), whose optional
        # Rust extension can PANIC at import time — pyo3_runtime.PanicException,
        # not a plain ImportError — when the installed cryptography build is
        # broken/ABI-mismatched. That must not crash startup: treat pdfminer as
        # unavailable and warn, and fall through to the other backends (pymupdf /
        # pdftotext); the explicit "no backend" error below still fires if none
        # of them are usable either.
        import sys
        print(
            f"[pdf] WARNING: pdfminer.six unavailable ({type(e).__name__}: {e}) — "
            "disabling this backend instead of crashing. If pymupdf is also "
            "missing, extraction quality will degrade; consider reinstalling "
            "cryptography (`pip install --force-reinstall cryptography`).",
            file=sys.stderr,
        )

    has_pymupdf = False
    try:
        import pymupdf as fitz  # noqa: F401
        has_pymupdf = True
    except ImportError:
        pass

    has_pdftotext = bool(shutil.which("pdftotext"))

    ok = has_pymupdf or has_miner or has_pdftotext
    if has_pymupdf:
        msg = "pymupdf available"
        if has_miner:
            msg += ", pdfminer.six available"
        if has_pdftotext:
            msg += ", pdftotext available (fallback)"
    elif has_miner:
        msg = "pdfminer.six available"
        if has_pdftotext:
            msg += ", pdftotext available (fallback)"
        else:
            msg += " (pymupdf NOT installed)"
    elif has_pdftotext:
        msg = "pdftotext available (pymupdf / pdfminer.six NOT installed — install one for best results)"
    else:
        msg = (
            "⚠ NO quality PDF backend available — install pymupdf "
            "(`pip install pymupdf`), pdfminer.six (`pip install pdfminer.six`), "
            "or poppler-utils (`pdftotext`). "
            "Falling back to stdlib: table detection, references, and "
            "sentence segmentation will be severely degraded."
        )

    return {
        "pdfminer": has_miner,
        "pymupdf": has_pymupdf,
        "pdftotext": has_pdftotext,
        "ok": ok,
        "message": msg,
    }


# --------------------------------------------------------------------------- #
#  Public API                                                                 #
# --------------------------------------------------------------------------- #

def extract_variants(path: str, page_sep: str = "\n\n") -> list[dict]:
    """Return all native extraction variants for identity-aware arbitration."""
    variants: list[dict] = []
    for method, backend in (
        ("pymupdf", _pymupdf_extract),
        ("pdfminer", _pdfminer_extract),
        ("pdftotext", _pdftotext_extract),
    ):
        try:
            if method == "pdftotext" or page_sep == "\n\n":
                extracted = backend(path)
            else:
                extracted = backend(path, page_sep=page_sep)
            text = extracted if isinstance(extracted, str) else ""
            variants.append({
                "method": method,
                "text": text,
                "quality_ok": _quality(text),
                "quality_metrics": _quality_metrics(text),
                "structure_flags": structure_flags(text),
                "error": None if text else "backend returned no text",
            })
        except ImportError as exc:
            variants.append({
                "method": method, "text": "", "quality_ok": False,
                "quality_metrics": _quality_metrics(""), "structure_flags": [],
                "error": f"backend unavailable: {exc}",
            })
        except Exception as exc:
            variants.append({
                "method": method, "text": "", "quality_ok": False,
                "quality_metrics": _quality_metrics(""), "structure_flags": [],
                "error": f"{type(exc).__name__}: {exc}",
            })
    # stdlib output is the final extraction fallback, never negative identity evidence.
    if not any(item["quality_ok"] for item in variants):
        try:
            text = _stdlib_extract(path)
            variants.append({
                "method": "stdlib", "text": text, "quality_ok": False,
                "quality_metrics": _quality_metrics(text),
                "structure_flags": structure_flags(text), "error": None,
            })
        except Exception as exc:
            variants.append({
                "method": "stdlib", "text": "", "quality_ok": False,
                "quality_metrics": _quality_metrics(""), "structure_flags": [],
                "error": f"{type(exc).__name__}: {exc}",
            })
    return variants


def _select_extraction_variant(variants: list[dict]) -> tuple[dict, bool]:
    """Return ``(winner, quality_passed)`` using the stable backend order."""
    quality_variants = [item for item in variants if item.get("quality_ok")]
    if quality_variants:
        return (
            min(
                quality_variants,
                key=lambda item: (
                    len(item.get("structure_flags") or []),
                    variants.index(item),
                ),
            ),
            True,
        )
    return (
        variants[-1] if variants else {"text": "", "method": "stdlib"},
        False,
    )


def _variant_selection_audit(
    variants: list[dict],
    winner: dict,
    quality_passed: bool,
) -> dict:
    """Serializable extraction arbitration record (never includes full text)."""
    return {
        "selected_method": str(winner.get("method") or "stdlib"),
        "selected_quality_ok": bool(quality_passed),
        "selection_rule": "fewest_structure_flags_then_backend_order",
        "candidates": [
            {
                "method": item.get("method"),
                "quality_ok": bool(item.get("quality_ok")),
                "quality_metrics": dict(item.get("quality_metrics") or {}),
                "structure_flags": list(item.get("structure_flags") or []),
                "error": item.get("error"),
            }
            for item in variants
        ],
    }


def extract_with_quality_audit(
    path: str,
    page_sep: str = "\n\n",
) -> tuple[str, str, list[str], dict]:
    """Structure-aware extraction plus its backend-selection audit record."""
    variants = (
        extract_variants(path)
        if page_sep == "\n\n"
        else extract_variants(path, page_sep=page_sep)
    )
    winner, quality_passed = _select_extraction_variant(variants)
    if not quality_passed:
        _warn_fallback_once(path)
    text = str(winner.get("text") or "")
    flags = (
        list(winner.get("structure_flags") or [])
        if quality_passed
        else structure_flags(text)
    )
    return (
        text,
        str(winner.get("method") or "stdlib"),
        flags,
        _variant_selection_audit(variants, winner, quality_passed),
    )


def extract_with_quality(path: str, page_sep: str = "\n\n") -> tuple[str, str, list[str]]:
    """Like :func:`extract`, but prefer structurally sound PDF text.

    The same backend chain is evaluated in order.  A readable variant without
    structural flags wins; when all readable variants are flagged, preserve the
    least-damaged one and return its flags for downstream reporting.
    """
    text, method, flags, _audit = extract_with_quality_audit(path, page_sep=page_sep)
    return text, method, flags


def extract(path: str, page_sep: str = "\n\n") -> tuple[str, str]:
    """Extract text from a PDF.

    Returns (text, method) where method is one of:
      'pymupdf', 'pdfminer', 'pdftotext', 'stdlib'

    Tries each method in order and returns the first result that passes the
    quality gate. If all fail, returns whatever the last method produced
    (caller is responsible for checking quality).

    *page_sep* joins consecutive pages.  The default preserves existing
    behaviour (and fetch/identity text); the manuscript parser passes a form-feed
    so it can recover per-page footnote apparatus, then strips it again.
    """
    import sys

    # 1. pymupdf (fitz) — BLOCKS mode handles two-column layouts best
    try:
        text = _pymupdf_extract(path, page_sep=page_sep)
        if _quality(text):
            return text, "pymupdf"
    except ImportError:
        pass
    except Exception:
        pass

    # 2. pdfminer.six — also coordinate-aware, fallback when pymupdf not installed
    try:
        text = _pdfminer_extract(path, page_sep=page_sep)
        if _quality(text):
            return text, "pdfminer"
    except ImportError:
        pass
    except Exception:
        pass

    # 3. pdftotext — poppler, if installed
    try:
        text = _pdftotext_extract(path)
        if text and _quality(text):
            return text, "pdftotext"
    except Exception:
        pass

    # 4. stdlib — last resort, quality will be poor
    _warn_fallback_once(path)
    text = _stdlib_extract(path)
    return text, "stdlib"
