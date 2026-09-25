#!/usr/bin/env python3
# core/parse/footnotes.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Chicago Notes-Bibliography footnote references.

Some manuscripts cite with superscript numbers whose *references* live in
numbered footnotes at the bottom of each page (with a separate, unnumbered
alphabetical bibliography at the end).  The end bibliography cannot resolve the
superscript numbers; the numbered footnotes can.

This module recovers those footnotes as a numbered reference list.  It operates
on extracted text where page boundaries are marked with a form-feed (``\\f``);
the PDF extractor inserts them for the manuscript path only, and strips them
again after this runs, so the rest of the pipeline is unaffected.

Detection is deliberately strict — a *sequential* run 1, 2, 3, … of
number-plus-citation lines — so ordinary documents (whose page footers are line
numbers or nothing) never trigger it.
"""

from __future__ import annotations

import collections
import importlib
import re
import unicodedata

try:
    from core.parse import reference_readers
except ImportError:  # direct execution fallback
    import reference_readers

PAGE_BREAK = "\f"

# A footnote line: the note number, whitespace, then citation text.  A bare page
# footer / manuscript line number ("81") is just digits and never matches this.
_NOTE_START_RE = re.compile(r"^(\d{1,3})\s+(\S.*)$")
_BARE_NUMBER_RE = re.compile(r"^\d{1,3}$")
# Minimum run length before we trust that this really is a footnote apparatus.
_MIN_NOTES = 3

# --- font-aware footnote apparatus (law-review OSCOLA / Bluebook) ---------------
#
# A law review carries no end bibliography: its sources ARE its numbered footnotes, and
# the in-text marker is the note number (a tiny raised digit).  The text-based scan above
# does not lift them — krishnamurthy numbers notes "230." (a period the note-start regex
# rejects) and GroJIL drops a note number onto its own line — and relaxing it to "N." on
# the raw text would swallow section headings ("1. Introduction") whole.
#
# The footnote apparatus is set in a smaller font than the body (body 11pt, note text
# ~9pt, note number ~6pt), so reading it off the small-font spans isolates it cleanly:
# section headings are body-size and never appear.  A note starts at "N", "N." or "N)"
# (the text may wrap to the next small-font line); the run must be sequential from 1 and
# citation-like, so a stray small-font run (captions, affiliations) never triggers it.
_FONT_MIN_NOTES = 8
_NOTE_START_RELAXED_RE = re.compile(r"^(\d{1,3})[.)]?\s*(.*)$")
# The signals of a legal citation apparatus — cross-references between the notes
# (id./ibid/supra/(n N)), case names (v.), pinpoints and reporter forms.  An author-year
# article's occasional explanatory footnote carries none of these, so requiring a good
# fraction of the notes to show one tells a real footnote apparatus (GroJIL 55%,
# krishnamurthy 74%) from a handful of explanatory notes (icae138 18%).
_APPARATUS_SIGNAL_RE = re.compile(
    r"\bsupra\b|\bibid\b|\bid\.|\(n\s*\d|\bv\.\s|§|\bat\s+\d|\bparas?\b|\bart\b|\bcf\b"
    r"|\breprinted\b|,\s*\d{1,4}\s*\(", re.I)
_APPARATUS_MIN_FRACTION = 0.4


def _resolve_parsing_common():
    try:
        return importlib.import_module("core.parse.parsing_common")
    except ImportError:
        return importlib.import_module("parsing_common")


def extract_footnote_references(text: str) -> dict[int, str] | None:
    """Recover numbered footnotes from ``\\f``-delimited page text.

    Returns ``{n: citation_text}`` for a sequential run starting at 1, or
    ``None`` when no such apparatus is present.  Continuation lines (a wrapped
    footnote) are joined; the current note is closed at each page boundary so it
    can never absorb the next page's body text."""
    if PAGE_BREAK not in text:
        return None
    notes: dict[int, str] = {}
    expected = 1
    cur: list | None = None
    for page in text.split(PAGE_BREAK):
        for raw in page.splitlines():
            line = raw.strip()
            m = _NOTE_START_RE.match(line)
            if m and int(m.group(1)) == expected:
                if cur:
                    notes[cur[0]] = cur[1].strip()
                cur = [expected, m.group(2)]
                expected += 1
            elif cur is not None and line and not _BARE_NUMBER_RE.match(line):
                cur[1] += " " + line
        if cur:  # a footnote never wraps across a page boundary
            notes[cur[0]] = cur[1].strip()
            cur = None
    if len(notes) < _MIN_NOTES:
        return None
    return notes


def _page_footnote_lines(page_dict, body_size: float) -> list[str]:
    """The small-font (footnote) lines of one page, in reading order.

    A line is footnote material when its dominant font size sits a step below the body
    size — which excludes the body and its section headings.  The tiny raised note
    number and the note text are different spans of the same line, so joining the line's
    spans keeps "230" together with the citation it opens.

    A page with essentially no body text is front matter — a small-font table of
    contents or abstract, whose numbered TOC entries ("1. Physical Disconnection 2386")
    would otherwise be read as the first footnotes.  Footnotes only ever sit beneath a
    page's body, so a page that is all small font carries no footnotes and is skipped."""
    small_lines: list[str] = []
    small_chars = big_chars = 0
    for block in page_dict.get("blocks", ()):
        for line in block.get("lines", ()):
            by_size: dict[float, int] = collections.defaultdict(int)
            texts: list[str] = []
            for span in line.get("spans", ()):
                t = span.get("text", "")
                texts.append(t)
                if t.strip():
                    by_size[round(span.get("size", 0.0), 1)] += len(t.strip())
            if not by_size:
                continue
            dominant = max(by_size, key=lambda z: by_size[z])
            n = sum(by_size.values())
            if dominant <= body_size - 0.75:
                small_chars += n
                joined = "".join(texts).strip()
                if joined:
                    small_lines.append(joined)
            else:
                big_chars += n
    if big_chars < 0.08 * (small_chars + big_chars):
        return []
    return small_lines


def _segment_font_notes(page_lines: list[list[str]]) -> dict[int, str] | None:
    """Reconstruct ``{n: text}`` from per-page small-font lines.

    The run must be sequential from 1 (a note number that wraps onto its own line opens
    an empty note the next lines fill), a note never wraps across a page boundary, and a
    majority of the notes must read like citations — a year or a legal signal — so a
    small-font run that is not a footnote apparatus is rejected."""
    notes: dict[int, str] = {}
    expected = 1
    cur: list | None = None
    for lines in page_lines:
        for line in lines:
            m = _NOTE_START_RELAXED_RE.match(line)
            if m and int(m.group(1)) == expected:
                if cur:
                    notes[cur[0]] = cur[1].strip()
                cur = [expected, m.group(2)]
                expected += 1
            elif cur is not None and not _BARE_NUMBER_RE.match(line):
                cur[1] += " " + line
        if cur:  # a footnote never wraps across a page boundary
            notes[cur[0]] = cur[1].strip()
            cur = None
    if len(notes) < _FONT_MIN_NOTES:
        return None
    apparatus = sum(1 for t in notes.values() if _APPARATUS_SIGNAL_RE.search(t))
    if apparatus < _APPARATUS_MIN_FRACTION * len(notes):
        return None
    return notes


def extract_footnote_references_from_pdf(path: str) -> dict[int, str] | None:
    """Recover a law review's numbered footnote apparatus off the PDF's font sizes.

    A last resort for the footnote papers the text scan cannot lift: it reads the
    small-font footnote text directly, so the note format (``73`` vs ``230.``) and a note
    number wrapped onto its own line stop mattering, and section headings — set in the
    body font — never intrude.  Returns ``{n: citation_text}`` or ``None``."""
    try:
        import pymupdf as fitz
    except ImportError:
        return None
    try:
        doc = fitz.open(path)
    except Exception:
        return None
    try:
        sizes: dict[float, int] = collections.defaultdict(int)
        page_dicts = []
        for page in doc:
            try:
                pd = page.get_text("dict")
            except Exception:
                pd = {"blocks": ()}
            page_dicts.append(pd)
            for block in pd.get("blocks", ()):
                for line in block.get("lines", ()):
                    for span in line.get("spans", ()):
                        t = span.get("text", "")
                        if t.strip():
                            sizes[round(span.get("size", 0.0), 1)] += len(t.strip())
        if not sizes:
            return None
        body_size = max(sizes, key=lambda z: sizes[z])
        page_lines = [_page_footnote_lines(pd, body_size) for pd in page_dicts]
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return {n: _normalize_pdf_note_text(t)
            for n, t in (_segment_font_notes(page_lines) or {}).items()}


def _normalize_pdf_note_text(text: str) -> str:
    """Give font-read notes the cleanup the text extractor already performs.

    `extract_footnote_references_from_pdf` reads glyphs straight off the page, so
    it never passes through `extractors.pdf.postprocess` and gets none of what
    lives there.  Two artefacts survive into the notes and then into everything
    derived from them: typographic ligatures ("ﬁles", "brieﬂy", "reﬂects"), and a
    word split across two lines keeping both the justification hyphen and the
    space that replaced the newline ("trans- boundary", "Fragmenta- tion",
    ".../re- source/blob/...").  A title carrying either does not match on
    lookup, and a URL carrying either resolves to a host or path that does not
    exist — which is how one law-review paper produced 39 damaged references and
    a fistful of 404s.

    Requiring a letter on both sides is the rule `_dehyphenate` already uses for
    body text: it rejoins a split word while leaving "…/northern-ontario- 1101895"
    alone, where the hyphen belongs to the slug and the continuation is a number.
    """
    from core.parse.extractors.pdf import _normalize_ligatures, _strip_soft_hyphens
    out = _strip_soft_hyphens(unicodedata.normalize("NFC", text or ""))
    return re.sub(r"([A-Za-z])-\s+([a-z])", r"\1\2", _normalize_ligatures(out))


# --- note -> reference metadata ------------------------------------------------

_QUOTED_TITLE_RE = re.compile(r"[\"“]([^\"”]{6,})[\"”]")
# OSCOLA uses single quotes for a work title, but apostrophes also occur in
# ordinary titles and journal abbreviations.  Require the author, quoted title,
# and publication-year sequence at the beginning of the source before treating
# single quotes as bibliographic evidence.
_OSCOLA_QUOTED_TITLE_RE = re.compile(
    r"^\s*[A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){0,5},\s+"
    r"[‘']([^’']{6,})[’']\s+\((?:19|20)\d{2}[a-z]?\)"
)
_PERSONAL_AUTHOR_BOOK_TITLE_RE = re.compile(
    r"^[A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,5}"
    r"(?:\s+(?:and|&)\s+[A-Z][A-Za-z'’.-]+"
    r"(?:\s+[A-Z][A-Za-z'’.-]+){1,5})?,\s+"
    r"(?P<title>[^()]{4,220}?)\s+"
    r"\((?:(?:\d+(?:st|nd|rd|th)\s+ed\.)|(?:19|20)\d{2})[^)]*\)\.?$",
    re.I,
)
_LEGAL_REPORTER_TAIL_IN_TITLE_RE = re.compile(
    r",\s*\d{1,4}\s+[A-Z][A-Za-z.&'’\s]{2,100}\s+\d{1,5}"
    r"(?:,\s*\d{1,5}(?:[–‐-]\d{1,5})?)?$"
)
_PAREN_YEAR_RE = re.compile(r"\((19|20)\d{2}[a-z]?\)")

# An institutional web citation can have no personal-author block for the
# generic readers to anchor.  Keep this grammar deliberately closed: it needs
# a colon-bearing initial title, an abbreviated institutional source, a page
# pinpoint, a complete calendar date, and the primary URL.  Each component is
# needed to distinguish the citation from ordinary explanatory prose or a
# partial note.
_INSTITUTIONAL_NOTE_TITLE_RE = re.compile(
    r"^\s*"
    r"(?P<title>[A-Z][^,\n:]{0,80}:\s*[A-Z][^,\n]{4,160})"
    r",\s+"
    r"(?P<institution>[A-Z][A-Za-z'’]{1,24}"
    r"(?:\s+[A-Z][A-Za-z'’]{1,24}){0,2}\.)"
    r"\s+"
    r"(?P<pinpoint>\d{1,4}(?:\s*[-–]\s*\d{1,4})?)"
    r"\s+\("
    r"(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
    r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"(?P<day>[1-9]|[12]\d|3[01]),\s+(?P<year>(?:19|20)\d{2})"
    r"\),\s*<?https?://[^\s>\]]+>?"
    r"(?:\s+\[[^\]]+\])?\s*\.?\s*$"
)

# This is deliberately narrower than the general note parsers. It identifies
# only a complete, single law-journal citation embedded after explanatory
# prose; callers must opt in explicitly rather than changing note extraction.
_EMBEDDED_LEGAL_SOURCE_RE = re.compile(
    r"\b[A-Z][A-Za-z'’.-]+(?:\s+[A-Z]\.)+\s+[A-Z][A-Za-z'’.-]+,\s+"
    r"[^;\n]{6,}?,\s+"
    r"\d{1,4}\s+(?:[A-Z][A-Za-z.]*\s+){1,4}L\.\s*(?:J\.|Rev\.)\s+"
    r"\d{1,5}(?:,\s*\d{1,5}(?:[–‐-]\d{1,5})?)?\s+"
    r"\((?:19|20)\d{2}[a-z]?\)\."
)
_EMBEDDED_BLUEBOOK_ARTICLE_RE = re.compile(
    r"\b[A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,4}"
    r"(?:\s+(?:&|and)\s+[A-Z][A-Za-z'’.-]+"
    r"(?:\s+[A-Z][A-Za-z'’.-]+){1,4})?,\s+"
    r"[A-Z][^;,\n]{4,180},\s+\d{1,4}\s+"
    r"[A-Z][A-Za-z.&'’\s]{2,100}\s+\d{1,5}"
    r"(?:,\s*\d{1,5}(?:[–‐-]\d{1,5})?)?\s+"
    r"\((?:19|20)\d{2}[a-z]?\)\.\s*$"
)
# A non-Bluebook article can be embedded after explanatory prose in an OSCOLA
# footnote.  Keep this separate from the legal-reporter form above: it requires
# a named author, a quoted title, a parenthesised year, and a volume/journal/page
# tail all the way to the end of the note.  That positional evidence is what
# distinguishes it from a quoted treaty passage in the surrounding prose.
_EMBEDDED_AUTHORYEAR_ARTICLE_RE = re.compile(
    r"\b[A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,4},\s+"
    r"[‘'][^’']{6,240}[’']\s+"
    r"\((?:19|20)\d{2}[a-z]?\)\s+\d{1,3}(?:\(\d+\))?\s+"
    r"[A-Z][A-Za-z& ]{2,80}\s+\d{1,5}(?:,\s*\d{1,5}(?:[–‐-]\d{1,5})?)?"
    r"\.\s*\.?\s*$"
)
_ALPHABETIC_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+")


def _single_embedded_legal_source_span(text: str) -> tuple[int, int] | None:
    """Return the strict embedded legal-source span, or ``None``.

    The only production caller is ``build_note_references`` in the footnote
    branch, which is itself enabled only by a document-level apparatus gate.
    Fail closed for ambiguity, incomplete citations, and citation-only notes.
    """
    text = text or ""
    if len(list(_PAREN_YEAR_RE.finditer(text))) != 1:
        return None

    matches = list(_EMBEDDED_LEGAL_SOURCE_RE.finditer(text))
    if len(matches) != 1:
        return None

    match = matches[0]
    prefix = text[: match.start()]
    if match.start() == 0 or len(_ALPHABETIC_WORD_RE.findall(prefix)) < 4:
        return None
    return match.span()


def _single_embedded_source_span(text: str) -> tuple[int, int] | None:
    """Return one proven citation embedded after explanatory note prose.

    Legal-reporter notes retain their established grammar.  The second grammar
    is deliberately limited to a complete quoted author-year journal article;
    anything with zero or multiple candidate citations remains on the existing
    whole-note path.
    """
    legal = _single_embedded_legal_source_span(text)
    if legal is not None:
        return legal

    text = text or ""
    if len(list(_PAREN_YEAR_RE.finditer(text))) != 1:
        return None
    matches = [
        *list(_EMBEDDED_AUTHORYEAR_ARTICLE_RE.finditer(text)),
        *list(_EMBEDDED_BLUEBOOK_ARTICLE_RE.finditer(text)),
    ]
    if len(matches) != 1:
        return None
    match = matches[0]
    if (match.start() == 0
            or len(_ALPHABETIC_WORD_RE.findall(text[:match.start()])) < 4):
        return None
    start, end = match.span()
    signal = re.match(r"(?:see(?:\s+also|,\s*e\.g\.,?)?)\s+", text[start:end], re.I)
    if signal is not None:
        start += signal.end()
    return start, end


def _note_year(text: str) -> int | None:
    """The *last* parenthesised year — the publication year — not the first,
    which may be a title date range ("Scopus 1900–2020 … (2022)")."""
    years = _PAREN_YEAR_RE.findall(text)  # findall returns the "19"/"20" groups
    if not years:
        return None
    last = list(_PAREN_YEAR_RE.finditer(text))[-1].group(0)  # e.g. "(2022)"
    return int(last[1:5])


def _institutional_note_title(text: str) -> str | None:
    """Return the literal title in the one complete institutional-note form."""
    match = _INSTITUTIONAL_NOTE_TITLE_RE.match(text or "")
    return match.group("title").strip() if match else None


def _note_title(text: str, pc) -> str | None:
    """The work a footnote names: a registered reader first, then the quoted
    reading (Chicago), then the shared extractor.

    The last two are heuristics over an entry whose format nobody identified, so
    both reject an author-block fragment ("& Sud, P.", "et al", a lone initial):
    for a citation verifier a wrong title misleads the fetch more than a missing
    one, so this returns None rather than guess.
    """
    # A reader outranks BOTH heuristics, because it recognised the entry's format
    # and they did not.
    #
    # Quotation marks mean opposite things in the two styles this function serves:
    # in Chicago they enclose the title, in Bluebook they enclose the explanatory
    # parenthetical that FOLLOWS it — so reading the quotes first named Rose's
    # article "inherently public property" and Frischmann's book "infrastructural
    # resources".  Across the whole corpus the readings disagree on 20 references,
    # all in the one Bluebook paper, and on every one the quoted text is a gloss.
    #
    # The generic extractor was written for author-year and numbered styles and on
    # a Bluebook footnote returns whatever fragment it can find — "tampered" for
    # "A Global Assessment of Third-Party Connection Tampering".  35 references in
    # one paper were named by such a fragment.
    oscola = _OSCOLA_QUOTED_TITLE_RE.search(text)
    if oscola:
        return oscola.group(1).strip().rstrip(".,")
    book = _PERSONAL_AUTHOR_BOOK_TITLE_RE.match(text.strip())
    if book:
        book_title = book.group("title").strip().rstrip(".,")
        if not _LEGAL_REPORTER_TAIL_IN_TITLE_RE.search(book_title):
            return book_title
    record = reference_readers.read(text)
    if record:
        return record["title"]
    institutional_title = _institutional_note_title(text)
    if institutional_title:
        return institutional_title
    m = _QUOTED_TITLE_RE.search(text)
    if m:
        return m.group(1).strip().rstrip(".,")
    cand = pc._extract_title(text)
    words = cand.split() if cand else []
    first = words[0] if words else ""
    if (cand
            and len(words) >= 3
            and first not in ("&", "and")
            and not cand.lower().startswith("et al")
            and not re.fullmatch(r"[A-Z]\.?", first)):
        return cand
    return None


_BIB_ENTRY_SPLIT_RE = re.compile(r"\n(?=[A-ZÀ-Þ][a-zà-ÿ]+,\s+[A-ZÀ-Þ])")
_DOI_RE = re.compile(r"10\.\d{4,9}/\S+")
_COMPARE_NOTE_RE = re.compile(r"^compare\s+", re.I)
_COMPARE_WITH_RE = re.compile(r", with ", re.I)
_REPORTER_YEAR_RE = re.compile(
    r"\d{1,4}\s+(?:[A-Z][A-Za-z.]*\s+){1,5}(?:L\.\s*)?(?:J\.|Rev\.)\s+\d{1,5}[^()]{0,80}\((?:19|20)\d{2}[a-z]?\)",
)
_SEMICOLON_SOURCE_SIGNAL_RE = re.compile(
    r"^(?:(?:see(?:\s+also|,\s*e\.g\.,?)?|and)\s+)?", re.I,
)
_SEMICOLON_SOURCE_AUTHOR_RE = re.compile(
    r"[A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,5}"
    r"(?:\s+(?:and|&)\s+[A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,5})?\s*,"
)
_SEMICOLON_SOURCE_YEAR_RE = re.compile(r"\([^)]*\b(?:1[6-9]|20)\d{2}\b[^)]*\)")
_SEMICOLON_SOURCE_END_RE = re.compile(r"(?:\)|\d{1,5})$")
_TRAILING_CITATION_COMMENT_RE = re.compile(
    r"\([^)]*\b(?:1[6-9]|20)\d{2}\b[^)]*\)(?=\s+\()"
)


def _semicolon_note_source_spans(text: str) -> list[tuple[int, int]] | None:
    """Return source spans for a closed semicolon-separated bibliography list.

    Each clause must independently begin with a personal-author block and end
    in a parenthesised publication year.  Leading ``and``/``see also`` signals
    are excluded from child spans.  A semicolon in commentary therefore cannot
    split a note merely because it looks like punctuation.
    """
    text = text or ""
    separators = list(re.finditer(r";", text))
    if not separators:
        return None
    starts = [0, *(match.end() for match in separators)]
    ends = [*(match.start() for match in separators), len(text)]
    spans: list[tuple[int, int]] = []
    for start, end in zip(starts, ends):
        source_start = start
        source_end = end
        while source_start < source_end and text[source_start].isspace():
            source_start += 1
        signal = _SEMICOLON_SOURCE_SIGNAL_RE.match(text[source_start:source_end])
        source_start += signal.end() if signal else 0
        while source_end > source_start and text[source_end - 1].isspace():
            source_end -= 1
        if source_end > source_start and text[source_end - 1] == ".":
            source_end -= 1
        candidate = text[source_start:source_end]
        trailing_citation = _TRAILING_CITATION_COMMENT_RE.search(candidate)
        if trailing_citation is not None:
            source_end = source_start + trailing_citation.end()
            candidate = text[source_start:source_end]
        year_matches = list(_SEMICOLON_SOURCE_YEAR_RE.finditer(candidate))
        if (not _SEMICOLON_SOURCE_AUTHOR_RE.match(candidate)
                or len(year_matches) != 1
                or not _SEMICOLON_SOURCE_END_RE.search(candidate)):
            return None
        spans.append((source_start, source_end))
    return spans if len(spans) >= 2 else None


def _first_surname(text: str) -> str | None:
    """The first author's surname, for either ordering: "Thelwall, M. …" ->
    thelwall; "Julian Ziegler et al." -> ziegler (word before "et al.")."""
    m = re.match(r"\s*([A-ZÀ-Þ][A-Za-zà-ÿ'’-]+),", text)  # Surname, Initials (CamelCase ok)
    if m:
        return m.group(1).lower()
    m = re.match(r"\s*[A-ZÀ-Þ][A-Za-zà-ÿ'’-]+\s+([A-ZÀ-Þ][A-Za-zà-ÿ'’-]+)\s+et al", text)
    if m:  # Firstname Surname et al.
        return m.group(1).lower()
    return None


def _parse_end_bibliography(biblio: str) -> list[dict]:
    """Split the alphabetical end bibliography into Chicago entries and index the
    fields useful for matching a footnote to its fuller entry."""
    entries = []
    for chunk in _BIB_ENTRY_SPLIT_RE.split(biblio or ""):
        raw = re.sub(r"\s+", " ", chunk).strip()
        if len(raw) < 20:
            continue
        # Publication year: the first parenthesised year (a Chicago entry that
        # merged a following institutional "Name. …" entry would otherwise take
        # that neighbour's later year from a raw trailing scan).
        pm = re.search(r"\((19|20)\d{2}\)", raw)
        years = re.findall(r"\b((?:19|20)\d{2})\b", raw)
        doi = _DOI_RE.search(raw)
        quoted = _QUOTED_TITLE_RE.search(raw)
        entries.append({
            "raw": raw,
            "surname": _first_surname(raw),
            "year": int(pm.group(0)[1:5]) if pm else (int(years[-1]) if years else None),
            "doi": doi.group(0).rstrip(".,;").lower() if doi else None,
            "title": quoted.group(1).strip().rstrip(".,") if quoted else None,
        })
    return entries


def _match_end_bib(ref: dict, note_text: str, entries: list[dict]) -> dict | None:
    """Find the end-bibliography entry for a footnote: by DOI (strong), else by
    publication year plus the first-author surname appearing in the entry."""
    ref_doi = (ref.get("doi") or "").lower() or None
    sur = _first_surname(note_text)
    yr = ref.get("year")
    for e in entries:
        if ref_doi and e["doi"] and e["doi"] == ref_doi:
            return e
    if sur is None or yr is None:
        return None
    for e in entries:
        if e["year"] == yr and re.search(r"\b" + re.escape(sur) + r"\b", e["raw"], re.I):
            return e
    return None


def _compare_source_has_independent_citation_form(text: str) -> bool:
    """Whether one side of a leading ``Compare A, with B`` is a source.

    This is intentionally a closed, structural gate.  It does not use the
    reference constructor as evidence, because that constructor can return a
    partial record for arbitrary prose.
    """
    first_comma = text.find(",")
    if first_comma < 0:
        return False
    contributor_prefix = text[:first_comma]
    contributors = re.findall(r"\b[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\b", contributor_prefix)
    bibliographic_tail = text[first_comma + 1:]
    if (len(contributors) < 2 or len(contributors) > 12
            or re.search(r"\d|https?://", contributor_prefix, re.I)
            or len(_ALPHABETIC_WORD_RE.findall(bibliographic_tail)) < 3):
        return False
    has_doi = bool(_DOI_RE.search(text))
    has_url_and_year = bool(
        re.search(r"https?://\S+", text, re.I)
        and re.search(r"\b(?:19|20)\d{2}\b", text)
    )
    return has_doi or has_url_and_year or bool(_REPORTER_YEAR_RE.search(text))


def _compare_note_source_spans(text: str) -> tuple[str, list[tuple[int, int]] | None]:
    """Classify the one supported comparative-note grammar.

    A leading ``Compare`` retains its explicit ambiguous state when incomplete.
    An embedded ``Compare`` is comparative only when its complete closed grammar
    is present; a title's ordinary use of the word otherwise keeps the
    established whole-note path.
    """
    text = text or ""
    offset = 0
    segment = text
    head = _COMPARE_NOTE_RE.match(segment)
    embedded_compare = head is None
    if head is None:
        embedded = list(re.finditer(r"\bcompare\s+", text, flags=re.I))
        if (len(embedded) != 1
                or len(_ALPHABETIC_WORD_RE.findall(text[:embedded[0].start()])) < 4):
            return "not_comparative", None
        offset = embedded[0].start()
        segment = text[offset:]
        head = _COMPARE_NOTE_RE.match(segment)
    connectors = list(_COMPARE_WITH_RE.finditer(segment))
    if len(connectors) != 1:
        return ("not_comparative", None) if embedded_compare else ("ambiguous", None)
    connector = connectors[0]
    spans = [
        (offset + head.end(), offset + connector.start()),
        (offset + connector.end(), len(text)),
    ]
    if any(start >= end or not _compare_source_has_independent_citation_form(text[start:end])
           for start, end in spans):
        return ("not_comparative", None) if embedded_compare else ("ambiguous", None)
    return "split", spans


_PINPOINT_RE = re.compile(r"\bat\s+(\d+(?:[–‐-]\d+)?(?:\s*,\s*\d+(?:[–‐-]\d+)?)*)", re.I)
_PARA_PINPOINT_RE = re.compile(r"¶\s*(\d+(?:[–‐-]\d+)?)")

# "Id." / "Id. at 96" / "Ibid." / "Ibid. at 96", optionally preceded by "See "/"See also ".
# A trailing "at N" (the pinpoint) is optional and captured separately by _pinpoint().
# "Id." carries a pinpoint in whatever form the citation style uses, and is very
# often followed by the author's own commentary.  Anchoring the whole entry on
# "Id. at <page>" recognises only the barest form: it misses "Id. art. 34(2).",
# "Id. at 12, 14 (emphasis added)." and every "Id. at 13. The Manual clarifies
# that ..." — 44 notes in one law-review paper alone, each then resolved as if it
# were a source of its own and failing.  Match the HEAD instead, and let
# _ID_TAIL_NEW_SOURCE_RE decide whether what follows is mere commentary.
_ID_HEAD_RE = re.compile(
    r"^(?:see\s+also\s+|see\s+)?(?:id|ibid)\.?"
    r"(?:\s*,?\s*(?:at|arts?\.|§{1,2}|¶{1,2}|pt\.|ch\.|tbl\.|fig\.|nn?\.)\s*"
    r"[\dIVXLC][^.;]{0,24})*"
    r"\s*[.;]?", re.I)
# A note may cite a NEW source after its back-reference ("Ibid. See also UNCTAD,
# ... (United Nations, 2000) 1."), and that source must still be resolved on its
# own.  A year inside parentheses is the signature of a full citation; a year in
# running prose ("the Crimean War of 1853-1856") is not, so the year has to be
# bracketed to count.  Any doubt leaves the note a source of its own, which is the
# safe direction and the behaviour that predates this check.
_ID_TAIL_NEW_SOURCE_RE = re.compile(
    r"\([^)]{0,60}(?:1[6-9]|20)\d{2}[^)]{0,10}\)|https?://|\bsupra\s+note\b", re.I)
# "<anything> supra note K[, at N]" - the target note number is the load-bearing capture.
_SUPRA_RE = re.compile(r"\bsupra\s+note\s+(\d+)", re.I)
# A note whose ENTIRE content points at a SPAN of the apparatus: "See supra notes
# 291-294 and accompanying text.", "See infra notes 130-137 ...".  Unlike "Author,
# supra note K", these name no source of their own, and the plural "notes" is what
# _SUPRA_RE (singular) never matched — so they were resolved as if each were a
# standalone work, and failed.  The span's FIRST note is the antecedent: following
# it reaches the real sources the span is pointing at.  "infra" works through the
# same map as "supra" because the lookup is by note number, not by direction.
#
# Anchored on the whole entry on purpose.  A real citation that merely MENTIONS a
# pointer in its trailing commentary ("...591, 612 (2020). By and large, the
# literature ... supra note 40") must keep its own resolution, so a mid-entry
# match must not reach this rule.
_NOTE_SPAN_RE = re.compile(
    r"^(?:see\s+(?:also\s+|generally\s+)?)?(?:supra|infra)\s+notes?\s+(\d+)"
    r"(?:\s*[–‐-]\s*\d+)?(?:\s+and\s+accompanying\s+text)?\s*\.?$", re.I)


def _pinpoint(text: str) -> str | None:
    """The locator this note's own raw_entry carries ("at 96" -> "96"), or None."""
    m = _PINPOINT_RE.search(text)
    if m:
        return re.sub(r"\s+", "", m.group(1)).replace(",", ", ")
    m = _PARA_PINPOINT_RE.search(text)
    if m:
        return m.group(1)
    return None


def _classify_cross_reference(raw_entry: str) -> tuple[str, int | None] | None:
    """Classify *raw_entry* as a cross-reference: ("id", None) for Id./Ibid. (points
    at the immediately preceding note) or ("supra", K) for "supra note K" and for a
    whole-entry span ("See supra notes K-M and accompanying text", "infra notes ...")
    — a span resolves to K, the first note it covers.  Returns None when the note is
    a real source (matches no form)."""
    text = (raw_entry or "").strip()
    m = _NOTE_SPAN_RE.match(text)
    if m:
        return ("supra", int(m.group(1)))
    m = _SUPRA_RE.search(text)
    if m:
        return ("supra", int(m.group(1)))
    m = _ID_HEAD_RE.match(text)
    # end() >= 3 keeps the match honest: the head must have consumed "id."/"ibid",
    # not merely the optional "see " prefix.
    if m and m.end() >= 3 and not _ID_TAIL_NEW_SOURCE_RE.search(text[m.end():]):
        return ("id", None)
    return None


# A pointer at a SECTION of the manuscript ("See supra Section II.B.3.a."), as
# opposed to a pointer at another note.  There is no antecedent note to inherit
# from: the author is pointing at their own earlier prose.
_MANUSCRIPT_SECTION_RE = re.compile(
    r"\b(?:supra|infra)\s+(?:sections?|parts?|ch\.|chapters?)\b", re.I)
# Anything that would identify an EXTERNAL work: a parenthesised year, a URL, a
# DOI, a "VOLUME Reporter PAGE" span, a quoted title, a treaty series.  One of
# these present means the note cites something, whatever else it also says.
_CITATION_SIGNATURE_RE = re.compile(
    r"\((?:1[6-9]|20)\d{2}\)|https?://|10\.\d{4,9}/"
    r"|\d+\s+[A-Z][A-Za-z.'&]*(?:\s+[A-Za-z.'&]+){0,4}\s+\d+"
    r"|“[^”]{8,}”|\bU\.N\.T\.S\.|\bStat\.\s")


def manuscript_pointer_ids(references: list[dict]) -> set[str]:
    """Ids of notes that point at the manuscript's own sections and cite nothing.

    "See supra Section II.B.3.a." names no work.  Sent to resolution it becomes a
    web search for a string that cannot exist, which costs a lookup, returns
    nothing, and leaves the note tagged as if a real source had gone missing —
    sixteen such notes in one law-review paper, each also adding to the request
    volume that rate-limits the providers the REAL references need.

    Conservative: a note carrying any citation signature is excluded even when it
    also points at a section, because then it does cite something.  Callers must
    treat membership as "not an external source", never as "verified".
    """
    out: set[str] = set()
    inheriting = cross_reference_map(references)
    for ref in references:
        if ref["id"] in inheriting:
            continue
        raw = ref.get("raw_entry") or ""
        if _MANUSCRIPT_SECTION_RE.search(raw) and not _CITATION_SIGNATURE_RE.search(raw):
            out.add(ref["id"])
    return out


def cross_reference_map(references: list[dict]) -> dict[str, tuple[str, str | None]]:
    """Map each back-reference note's ref id to (antecedent ref id, pinpoint).

    A law-review footnote apparatus is full of back-references that are not
    sources themselves: "Id. at 96." means "the immediately preceding note,
    pinpoint 96"; "Lauterpacht, supra note 1, at 331." means "the note numbered
    1, pinpoint 331".  Sent to web resolution as if they were standalone
    sources, these fail and show up as "unverified" — the fix is for them to
    INHERIT the antecedent's resolution instead.  This function computes that
    mapping; it does not change what happens to the notes.

    Returns ``{ref_id: (target_ref_id, pinpoint)}`` for every note classified as
    a cross-reference, where *target_ref_id* is the id of the final REAL
    (non-cross-reference) note reached by following the chain — an "Id." that
    points at another "Id." that points at a real source resolves to that real
    source's id, not the intermediate "Id."'s id.  *pinpoint* is the locator
    carried by the CITING note itself (e.g. "96" or "90-91"), or None.

    A note that is not a cross-reference, or whose chain cannot be resolved to
    a real note (missing "supra note K" target, a cycle, or "Id." as the very
    first note), is omitted from the map entirely — it is left for the normal
    pipeline to treat as its own source.

    Pure: reads *references*, never mutates any element, and returns a plain
    dict.
    """
    unavailable_note_numbers: set[int] = set()
    logical_references: list[dict] = []
    for ref in references:
        unavailable_note_numbers.update(
            number for number in ref.get("_footnote_unavailable_note_numbers", ())
            if isinstance(number, int)
        )
        projection = ref.get("_footnote_projection") or ref.get("_manual_parse")
        if isinstance(projection, dict) and projection.get("source_count", 0) > 1:
            note_number = projection.get("note_number")
            if isinstance(note_number, int):
                unavailable_note_numbers.add(note_number)
            # A raw parent and its effective children are both implementation
            # details, never logical note antecedents.
            continue
        logical_references.append(ref)
    ordered = sorted(logical_references, key=lambda r: r.get("ref_number", 0))
    by_number: dict[int, dict] = {
        r["ref_number"]: r for r in ordered if r.get("ref_number") is not None
    }
    kinds: dict[str, tuple[str, int | None]] = {}
    for i, ref in enumerate(ordered):
        kind = _classify_cross_reference(ref.get("raw_entry") or "")
        if kind is None:
            continue
        if kind[0] == "id":
            current_number = ref.get("ref_number")
            previous_numbers = [
                number for number in by_number
                if isinstance(current_number, int) and number < current_number
            ]
            previous_number = max(previous_numbers) if previous_numbers else None
            prev = by_number.get(previous_number)
            kinds[ref["id"]] = (
                "id", prev["id"] if prev is not None and not any(
                    previous_number < number < current_number
                    for number in unavailable_note_numbers
                ) else None
            )
        else:
            target = None if kind[1] in unavailable_note_numbers else by_number.get(kind[1])
            kinds[ref["id"]] = ("supra", target["id"] if target is not None else None)

    result: dict[str, tuple[str, str | None]] = {}
    by_id = {r["id"]: r for r in ordered}
    cap = len(ordered) + 1
    for ref in ordered:
        rid = ref["id"]
        if rid not in kinds:
            continue
        cur = rid
        seen: set[str] = set()
        target_id: str | None = None
        for _ in range(cap):
            if cur in seen:  # cycle
                cur = None
                break
            seen.add(cur)
            entry = kinds.get(cur)
            if entry is None:
                target_id = cur  # a real note - chain terminates here
                cur = None
                break
            _, nxt = entry
            if nxt is None:  # no valid antecedent (missing supra target / Id. at start)
                cur = None
                break
            cur = nxt
        if target_id is not None and target_id in by_id:
            result[rid] = (target_id, _pinpoint(ref.get("raw_entry") or ""))
    return result


def build_note_references(notes: dict[int, str], end_bibliography: str = "") -> list[dict]:
    """Turn ``{n: citation_text}`` into numbered reference dicts with note-aware
    metadata (publication year = last parenthesised year, quoted title when
    present), tagged ``provenance="footnote"``.

    The abbreviated Nature-style footnotes often have no clean title, but the
    separate alphabetical end bibliography lists the same sources in fuller
    Chicago form with quoted titles and DOIs.  When supplied, it is used to
    *fill* (never overwrite) a missing title or DOI on the matched entry."""
    pc = _resolve_parsing_common()
    bib_entries = _parse_end_bibliography(end_bibliography)
    refs: list[dict] = []
    next_child_number = max(notes, default=0) + 1

    def make_note_ref(number: int, source_text: str) -> dict:
        ref = pc._make_reference(number, source_text)
        year = _note_year(source_text)
        if year is not None:
            ref["year"] = year
        # Assign unconditionally: _note_title returns None for an author-block
        # fragment, which must *clear* the weak title _make_reference guessed so
        # the end-bibliography enrichment below can fill it.
        ref["title"] = _note_title(source_text, pc)
        ref["provenance"] = "footnote"
        if bib_entries and (not ref.get("title") or not ref.get("doi")):
            match = _match_end_bib(ref, source_text, bib_entries)
            if match:
                if not ref.get("title") and match["title"]:
                    ref["title"] = match["title"]
                    ref["title_source"] = "end_bibliography"
                if not ref.get("doi") and match["doi"]:
                    ref["doi"] = match["doi"]
        return ref

    for n in sorted(notes):
        text = notes[n]
        compare_status, compare_spans = _compare_note_source_spans(text)
        if compare_status != "not_comparative":
            parent = make_note_ref(n, text)
            source_count = len(compare_spans or ())
            parent["_footnote_projection"] = {
                "role": "parent", "note_number": n, "raw_note": text,
                "extraction_status": "sources_extracted" if compare_spans else "ambiguous",
                "source_count": source_count,
            }
            refs.append(parent)
            if compare_spans is None:
                continue
            for source_order, (start, end) in enumerate(compare_spans):
                child = make_note_ref(next_child_number, text[start:end])
                next_child_number += 1
                child["_footnote_projection"] = {
                    "role": "source", "note_number": n, "raw_note": text,
                    "raw_start": start, "raw_end": end,
                    "source_order": source_order, "source_count": source_count,
                    "parent_ref_id": parent["id"],
                }
                refs.append(child)
            continue
        semicolon_spans = _semicolon_note_source_spans(text)
        if semicolon_spans is not None:
            parent = make_note_ref(n, text)
            parent["_footnote_projection"] = {
                "role": "parent", "note_number": n, "raw_note": text,
                "extraction_status": "sources_extracted",
                "source_count": len(semicolon_spans),
            }
            refs.append(parent)
            for source_order, (start, end) in enumerate(semicolon_spans):
                child = make_note_ref(next_child_number, text[start:end])
                next_child_number += 1
                child["_footnote_projection"] = {
                    "role": "source", "note_number": n, "raw_note": text,
                    "raw_start": start, "raw_end": end,
                    "source_order": source_order,
                    "source_count": len(semicolon_spans),
                    "parent_ref_id": parent["id"],
                }
                refs.append(child)
            continue
        span = _single_embedded_source_span(text)
        source_text = text[span[0]:span[1]] if span is not None else text
        ref = make_note_ref(n, source_text)
        # The footnote parser has already made one deterministic
        # note-to-reference projection. Preserve that whole-note source unless
        # the narrow embedded-source rule proves a more precise span. Failure
        # of the private splitter is not evidence that the note is ambiguous.
        source_start, source_end = span if span is not None else (0, len(text))
        ref["_footnote_source"] = {
            "note_number": n,
            "raw_note": text,
            "raw_start": source_start,
            "raw_end": source_end,
            "extraction_status": "sources_extracted",
            "source_order": 0,
        }
        refs.append(ref)
    return refs
