# core/parse/boilerplate.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Watermark / running-header / footer detection for reference recovery.

A publisher watermark or running header printed on every page ("Downloaded from
… by guest on …") gets extracted inline and can glue two references together,
hiding one from the segmenter.

Detection uses two signals TOGETHER, never repetition alone:

  * the line **repeats** across the document, and
  * it matches a **watermark-characteristic pattern** (loaded from the external
    ``boilerplate_patterns.txt`` — editable without touching code).

Body content that merely recurs (table labels, dataset names, hyperparameter
values) fails the pattern gate, so it is kept.  This is text-based, so it works
for ``.txt`` inputs too — no PDF coordinates required.

The cleanup is meant to be applied **demand-driven** (see the author-year
scheme's rescue): parse normally first, then re-segment a boilerplate-cleaned
copy only to recover references still orphaned — so a false-positive removal can
never lose text the normal pipeline already parsed.
"""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
import json
import os
import re

_PATTERNS_FILE = os.path.join(
    os.path.dirname(__file__), "config", "watermark_patterns.json")


@lru_cache(maxsize=1)
def _patterns() -> tuple[re.Pattern, ...]:
    try:
        with open(_PATTERNS_FILE, encoding="utf-8") as f:
            raw = json.load(f).get("watermark_patterns", [])
    except (OSError, ValueError):
        raw = []
    pats: list[re.Pattern] = []
    for p in raw:
        try:
            pats.append(re.compile(p))
        except re.error:
            continue  # skip a malformed pattern rather than fail
    return tuple(pats)


def _looks_like_watermark(line: str) -> bool:
    return any(p.search(line) for p in _patterns())


def boilerplate_lines(text: str, min_repeat: int = 2) -> set[str]:
    """Set of stripped lines that repeat ``>= min_repeat`` and look like a
    watermark/header/footer.  Empty when the document has no such boilerplate."""
    if not text:
        return set()
    freq = Counter(ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 8)
    return {ln for ln, n in freq.items()
            if n >= min_repeat and _looks_like_watermark(ln)}


# A journal's running head is its own title, set in caps across the top of every page
# ("AUTOBIOGRAPHICAL MEMORY SPECIFICITY", "WILLIAMS ET AL.").  No publisher vocabulary
# names it, so _patterns() cannot: it is a SHAPE, not a phrase.  Several words, all
# capitals, no digit — a digit is what tells a table cell or a dataset name ("JFT-300M")
# from a title, and a single word is too weak a signal to act on.
_RUNNING_HEAD_RE = re.compile(
    r"^(?=.{8,60}$)(?!.*\d)[A-ZÀ-Þ][A-ZÀ-Þ'’.&-]*(?:\s+[A-ZÀ-Þ'’.&-]+)+$")

# These rules are used solely on the column-aware bibliography extraction. A
# title-cased source line qualifies only after exact repetition establishes it
# as page furniture, and only with a journal-layout signature of its own.
_MIXED_CASE_RUNNING_HEAD_RE = re.compile(
    r"^(?=.{16,140}$)[A-Z][A-Za-zÀ-ÿ'’.-]*(?:\s+(?:[A-Z][A-Za-zÀ-ÿ'’.-]*|and|van|von|de|del|et\s+al\.)){1,8}"
    r"\s+[A-Z][A-Za-zÀ-ÿ.&-]*(?:\s+[A-Z][A-Za-zÀ-ÿ.&-]*){0,3}"
    r"\s+\((?:19|20)\d{2}\)\s+\d+\s*:\s*(?:e?\d+)\.?$"
)
_CUREUS_RUNNING_HEAD_RE = re.compile(
    r"^(?:19|20)\d{2}\s+[A-Z][A-Za-zÀ-ÿ'’.-]*(?:\s+et\s+al\.)?\s+"
    r"Cureus\s+\d+\(\d+\):\s*e\d+\.\s+DOI\s+10\.7759/cureus\.\d+$",
    re.IGNORECASE,
)
_PAGE_FURNITURE_RE = re.compile(
    r"^Page\s+\d{1,4}\s+of\s+\d{1,4}$", re.IGNORECASE)
_BARE_PAGE_FURNITURE_RE = re.compile(
    r"^\d{1,4}\s+of\s+\d{1,4}$", re.IGNORECASE)


def _running_head_shape(line: str) -> str:
    """Normalize layout-only characters for running-head recognition."""
    return re.sub(
        r"\s+", " ", line.replace("\ufeff", " ").replace("\xa0", " ")
    ).strip()


def journal_running_head_lines(text: str, min_repeat: int = 2) -> set[str]:
    """Repeated journal-coordinate heads safe to remove from manuscript body.

    Unlike the broad all-caps bibliography rule, these lines identify themselves
    through an author/journal/year/volume/locator layout signature.
    """
    if not text:
        return set()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    freq = Counter(ln for ln in lines if len(ln) >= 8)
    repeated = {
        line for line, count in freq.items()
        if count >= min_repeat and (
            _MIXED_CASE_RUNNING_HEAD_RE.match(_running_head_shape(line))
            or _CUREUS_RUNNING_HEAD_RE.match(_running_head_shape(line))
        )
    }
    # A first-page self-header may occur only once.  Admit that one-off shape
    # only at the document start and only when the next line is the work's DOI.
    # A normal source sentence elsewhere, or the same shape without a DOI, stays.
    if len(lines) >= 2 and re.match(r"^https?://doi\.org/10\.", lines[1], re.I):
        first = lines[0]
        if (
            _MIXED_CASE_RUNNING_HEAD_RE.match(_running_head_shape(first))
            or _CUREUS_RUNNING_HEAD_RE.match(_running_head_shape(first))
        ):
            repeated.add(first)
    return repeated


_CORRESPONDING_AUTHOR_RE = re.compile(r"^\s*\*\s*[A-Z][A-Za-zÀ-ÿ'’.-]+")
_EMAIL_LINE_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
)
_AFFILIATION_INDEX_RE = re.compile(r"^\s*\d{1,2}\s*$")
_PAGE_LABEL_RE = re.compile(r"^(?=.{5,60}$)[A-Z][A-Z ]+[A-Z]$")


def strip_interposed_affiliation_blocks(body: str) -> str:
    """Remove a bounded corresponding-author footer inserted inside body prose.

    PDF reading order can place the page-one author footer between two halves of
    a sentence.  The block is removed only when a starred personal-name line is
    followed by an email, at least two numbered affiliations, and a page label.
    """
    if not body:
        return body
    lines = body.splitlines()
    drop: set[int] = set()
    for start, line in enumerate(lines):
        if not _CORRESPONDING_AUTHOR_RE.match(_running_head_shape(line)):
            continue
        email_at = next((
            index for index in range(start + 1, min(len(lines), start + 5))
            if _EMAIL_LINE_RE.match(lines[index].strip())
        ), None)
        if email_at is None:
            continue
        end = next((
            index for index in range(email_at + 1, min(len(lines), email_at + 18))
            if _PAGE_LABEL_RE.match(_running_head_shape(lines[index]))
        ), None)
        if end is None:
            continue
        affiliation_indices = sum(
            bool(_AFFILIATION_INDEX_RE.match(lines[index]))
            for index in range(email_at + 1, end)
        )
        if affiliation_indices < 2:
            continue
        drop.update(range(start, end + 1))
    return "\n".join(line for index, line in enumerate(lines) if index not in drop)


def running_head_lines(text: str, min_repeat: int = 2) -> set[str]:
    """Repeated all-caps running heads in *text*.

    Deliberately NOT folded into :func:`boilerplate_lines`: that gate gets a whole
    document, where an all-caps line that repeats may well be content — a table label,
    a caps dataset name, an acronym on its own row — and this rule would eat it.  This
    one is for the column-aware bibliography re-extraction, which reads the reference
    pages as blocks and so keeps the page furniture the default extraction drops.  A
    bibliography has no all-caps prose, so within those pages the shape is decisive.
    """
    if not text:
        return set()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    freq = Counter(ln for ln in lines if len(ln) >= 8)
    repeated = {
        ln for ln, n in freq.items()
        if n >= min_repeat and (
            _RUNNING_HEAD_RE.match(_running_head_shape(ln))
            or ln in journal_running_head_lines(text, min_repeat=min_repeat)
        )
    }
    # Counters differ on each page, so exact repetition cannot detect them.
    # ``Page N of M`` identifies itself.  The bare ``N of M`` form is removed
    # only when this bibliography also contains a recognised repeated running
    # head: in isolation it can be legitimate source text.
    page_furniture = {
        ln for ln in lines if _PAGE_FURNITURE_RE.match(_running_head_shape(ln))
    }
    if repeated:
        page_furniture.update(
            ln for ln in lines
            if _BARE_PAGE_FURNITURE_RE.match(_running_head_shape(ln)))
    return repeated | page_furniture


_PAGE_NUMBER_RE = re.compile(r"^\W{0,2}\d{1,4}\W{0,2}$")


def strip_page_breaks(body: str, lines: set[str]) -> str:
    """Drop the page's own lines from the body: the running head, and the page number
    printed with it.

    A page break falls where it falls, and it can fall INSIDE a sentence — even inside
    a citation: "… migration management (IOM" ends a page, and the next one opens with
    its number and running head before the year, so the marker the reader sees as
    "(IOM 2024)" reaches us as "(IOM 375 CULTURAL ANTHROPOLOGY 39:3 2024)" and matches
    no citation pattern at all.  That is the silent failure — no orphan is raised for a
    marker never seen — and only the page's furniture standing between the author and
    its year is in the way.

    Removal is line-based (unlike :func:`strip_boilerplate`, which cuts the watermark
    out of the middle of a reference): the furniture arrives on lines of its own, and a
    line is what we can drop without touching a word the author wrote.

    A bare number is only furniture NEXT TO a running head — two signals, as in
    :func:`boilerplate_lines`.  Alone it is data ("in 2019, 375 refugees…" wrapping
    onto its own line), and dropping it would silently edit the manuscript."""
    if not lines or not body:
        return body
    out = body.splitlines()
    marked = [i for i, ln in enumerate(out) if ln.strip() in lines]
    if not marked:
        return body
    drop = set(marked)
    for i in marked:
        for j in _neighbours(out, i):
            if _PAGE_NUMBER_RE.match(out[j].strip()):
                drop.add(j)
    # The blank lines the page break left around the furniture go with it.  Leaving
    # them behind would leave the sentence cut in two all the same: a blank line is a
    # paragraph break to the segmenter, so "(IOM" and "2024)" would still never meet.
    for i in sorted(drop):
        for step in (-1, 1):
            j = i + step
            while 0 <= j < len(out) and not out[j].strip():
                drop.add(j)
                j += step
    return "\n".join(ln for i, ln in enumerate(out) if i not in drop)


def _neighbours(lines: list[str], i: int) -> list[int]:
    """The nearest non-blank line each side of *i* — a page break may print the number
    above the running head or below it, and blank lines fall between."""
    out = []
    for step in (-1, 1):
        j = i + step
        while 0 <= j < len(lines) and not lines[j].strip():
            j += step
        if 0 <= j < len(lines):
            out.append(j)
    return out


def strip_boilerplate(text: str, lines: set[str]) -> str:
    """Remove the given boilerplate strings from *text* wherever they occur.

    Substring removal (not whole-line) is deliberate: a watermark often lands
    *inline*, joined to the reference that follows it ("… by guest on 2026 Zed,
    Z. (2021). …"), and only removing it there frees that reference to segment.
    Longest strings first so a shorter boilerplate line cannot pre-empt a longer
    one that contains it.
    """
    if not lines or not text:
        return text
    for b in sorted(lines, key=len, reverse=True):
        if b:
            text = text.replace(b, " ")
    return text
