#!/usr/bin/env python3
# core/parse/reference_readers/bluebook.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Bluebook law-review citations.

"Author(s), Title, VOLUME Reporter PAGE (Year)".  Bluebook italicises the title
instead of quoting it, and italics do not survive PDF text extraction, so the
quoted reader never sees one and the generic extractor returns nothing: 12
resolvable articles in one law-review paper had no title at all, which leaves the
resolver nothing to search on.

Two anchors, because Bluebook cites two kinds of source:

- `reporter` — a journal article, delimited on the right by the
  volume-reporter-page span;
- `date` — a book, news story, think-tank piece or government paper, where a
  parenthesised date takes the anchor's place.

Both delimit the title by the author block on its LEFT and the anchor on its
RIGHT, rather than as "the text between two commas", because law-review titles
routinely CONTAIN commas ("Bargaining for Free Speech: Common Carriage, Network
Neutrality, and Section 230") and a comma-delimited read truncates them mid-title.

Conservative by construction throughout: a treaty, a decided case, a missing
author block or a non-journal reporter all yield None rather than a guess. For a
citation verifier a wrong title misleads the fetch more than a missing one.
"""

from __future__ import annotations

import re

NAME = "bluebook"
# Ahead of any reader keying on weaker evidence: an author block plus a
# reporter-or-date anchor is the strongest positional structure a footnote offers.
ORDER = 10

# A conference-proceedings reporter name routinely runs past the plain journal
# cap of 6 tokens ("Int'l Conf. on Software Sec. & Assurance" is 7), so the
# first branch's cap is widened to admit them.  The second branch is a distinct
# anchor shape a conference proceeding also uses: no leading volume number at
# all, the reporter carrying its own truncated year instead ("Compass '18:",
# "SOSP '19:") — "Compass" is the reporter, "'18" is the year the first
# branch expects to find IN FRONT of the reporter, not after it.
_VOL_REPORTER_RE = re.compile(
    r",\s*(?:"
    r"(?P<vol>\d{1,4})\s+(?P<rep>[A-Z][A-Za-z.'’&]*(?:\s+[A-Za-z.'’&]+){0,7}?)\s+\d{1,5}\b"
    r"|(?P<rep2>[A-Z][A-Za-z.'’&]*(?:\s+[A-Za-z.'’&]+){0,3})\s+['’]\d{2}:"
    r")")
# A name token may itself carry a parenthetical nickname ("Tae (Tom) Oh"),
# which the plain capitalised-word repetition below cannot absorb on its own.
_NAME_RE = r"(?:[A-Z][\w.'’-]*\s+|\([A-Z][\w.'’-]*\)\s+){1,4}[A-Z][\w.'’-]+"
# Bluebook joins any number of authors with commas and closes the list on the
# LAST one with "&" ("A, B, C & D,"), the same shape _AUTHOR_LIST_RE already
# reads for the date anchor below.  The reporter anchor's author block used to
# accept only a single name or an "&"-joined pair, so a four-author list past
# the first comma was read as part of the title.
_AUTHOR_BLOCK_RE = re.compile(
    rf"{_NAME_RE}(?:\s*,\s*{_NAME_RE})*(?:\s*&\s*{_NAME_RE})?(?:\s+et\s+al\.?)?,\s*")
# An introductory signal may be a whole clause ("For a clear ... works, see"), so
# the author block is taken from the LAST signal, not from the start of the note.
_SIGNAL_RE = re.compile(
    r"\b(?:see\s+generally|see,\s*e\.g\.,|see\s+also|see|compare|accord|cf\.)\s+", re.I)
# Reporters that are not journals: a statute, treaty series or law report carries
# no article title, and the volume-reporter shape alone cannot tell them apart.
_NOT_A_JOURNAL_RE = re.compile(
    r"^(?:Stat|U\.N\.T\.S|T\.S|Consol\. T\.S|I\.C\.J|U\.S|S\. Ct|F\.\dd|L\. Ed)\b", re.I)
# Markers of a treaty or a decided case rather than an article.  The word boundary
# on "v." is load-bearing: without it the case-name test also fires on the tail of
# "Har\ *v.* J.L." and silently rejects three genuine articles.
#
# The plural "arts." is not a stylistic variant to be tidy about: a note that cites
# several articles of an instrument is a note about that instrument, and the
# singular-only marker let one through.  A footnote may carry a treaty citation and
# then discuss it, and the discussion cites articles of its own — so the reporter
# anchor of a LATER, unrelated citation was the first one found, and the note came
# out titled with somebody else's work ("Convention télégraphique internationale
# … arts. 20-21" read as "The ITU and the Internet's Titanic Moment", 496
# characters further on).  Refusing the note is the conservative half of the same
# rule the singular marker already states.
_TREATY_OR_CASE_RE = re.compile(
    r"U\.N\.T\.S\.|\bStat\.\s|\bT\.S\.\s|\[hereinafter|\barts?\.\s*\d|\bv\.\s+[A-Z]", re.I)

# The two anchors above, named apart, because refusing to give a treaty a title is
# not the same as failing to recognise it.
#
# A treaty series ("55 U.N.T.S. 194", "61 Stat. A-11") and a case name ("Reno v.
# ACLU") are as positional as a reporter span, and they identify the sources that
# have no title to extract — which is why both title readers refuse them.  Left
# unclaimed they fell through to the heuristics, and the heuristics gave them one
# anyway, out of whatever was in quotes: the GATT citation became "Convention on
# the Law of the Sea pts", the League of Nations Covenant became "freedom of
# communications and of transit", the Tallinn Manual became "physical network
# components".  Each was then searched as though it were a journal article, and
# Crossref duly returned a real and unrelated work -- "Rapid Transit Arithmetic"
# for the Covenant, "A Reevaluation of the International Patent Convention" for
# GATT, on which 43 back-references depend.
#
# Claiming them titleless is what stops that: 39 of the 45 references that carry
# no title draw no match at all, while 117 of the 124 that carry one do.
#
# `art.` and `[hereinafter` stay rejection-only markers.  They appear in 50
# entries here, far more loosely than the two below, and claiming a source is a
# statement about it -- one this reader should make only on evidence as
# unambiguous as a treaty series or a "v.".
_TREATY_SERIES_RE = re.compile(r"U\.N\.T\.S\.|\bStat\.\s|\bT\.S\.\s|Consol\. T\.S\.")
_CASE_RE = re.compile(r"\bv\.\s+[A-Z]")
# Law reports: a reporter span whose reporter is a court's, not a journal's.
_LAW_REPORT_RE = re.compile(
    r",\s*\d{1,4}\s+(?:I\.C\.J|U\.S|S\. Ct|F\.\dd|L\. Ed)\b")


def legal_instrument(entry: str) -> str | None:
    """Which kind of legal instrument the entry cites, or None if it is neither.

    These sources exist and are citable, but they are not articles and carry no
    title: the citation IS the identifier.
    """
    if _TREATY_SERIES_RE.search(entry):
        return "treaty"
    if _CASE_RE.search(entry) or _LAW_REPORT_RE.search(entry):
        return "case"
    return None


def reporter_anchored_title(text: str) -> str | None:
    """The article title from a Bluebook journal citation, or None."""
    if _TREATY_OR_CASE_RE.search(text):
        return None
    for anchor in _VOL_REPORTER_RE.finditer(text):
        rep = anchor.group("rep") or anchor.group("rep2")
        if _NOT_A_JOURNAL_RE.match(rep):
            continue
        head = text[:anchor.start()]
        signals = [s.end() for s in _SIGNAL_RE.finditer(head)]
        segment = head[signals[-1] if signals else 0:]
        authors = _AUTHOR_BLOCK_RE.match(segment)
        if not authors:
            continue
        # "559- 65" and similar line-break debris: rejoin before normalising.
        title = re.sub(r"\s+", " ", segment[authors.end():]).strip(" ,")
        # An unbalanced "(" means the anchor matched past the citation's own date
        # and the title swallowed the publication block ("… Borderless World 73
        # (2006) (noting how …").  Same guard the dated reader applies.
        if "(" in title:
            continue
        if len(title.split()) < 2 or not title[:1].isupper():
            continue
        return title
    return None


# The shape is the journal one with the volume-reporter-page anchor replaced by a
# parenthesised date, which is why the reporter reader alone left 24 of these
# without a title in one paper.
#
# A book citation's parenthetical often names the edition before the year
# ("2d ed. 2018", "3d ed. 2023", "rev. ed. 2020"); the plain month/day form
# above does not admit that token, so those parentheticals never matched at
# all and the entry got no title.  The `edition` group is also the book-shaped
# signal `read()` uses below -- it fires only on this explicit marker, never on
# a bare "(YEAR)", which is exactly as likely to be a webpage or a news story.
_DATE_PAREN_RE = re.compile(
    r"\((?:[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s*)?(?:[A-Z][a-z]{2,8}\.?\s+)?"
    r"(?P<edition>\d+(?:st|nd|rd|th|d)\.?\s+ed\.\s+|rev\.\s+ed\.\s+)?"
    r"(?:1[6-9]|20)\d{2}")
# Bluebook joins the last of several authors with "&" or ", and".  A bare comma
# list is NOT accepted: "S. Yokoyama, R. Ukai, S. C. Armstrong…" is how physics
# and Vancouver styles write authors, and treating the tail as a title invented
# 21 titles out of author names across the corpus.
_AUTHOR_LIST_RE = re.compile(
    rf"(?:{_NAME_RE}(?:\s*,\s*{_NAME_RE})*\s*(?:&|,\s*and)\s*{_NAME_RE}"
    rf"|{_NAME_RE}(?:\s+et\s+al\.?)?),\s*")
# Vancouver writes an author as surname-then-bare-initials — "Chan AW",
# "Scholten-Peeters WGM" — and separates authors with plain commas.  The author
# regex above then matches the FIRST author only, and everything after it reads as
# a title: "Chan AW, Altman DG (2005) Epidemiology and reporting…" yielded the
# title "Altman DG".  Bluebook never writes a name this way; it writes "Brett M.
# Frischmann", initials carrying periods and the surname last.  So a trailing run
# of bare capitals is proof the entry belongs to another style.
#
# This guard is what lets the reader be trusted by any caller.  It was harmless
# while _note_title, which runs only on footnote manuscripts, was the sole caller
# — a safety the reader inherited from its caller rather than holding itself.
_VANCOUVER_AUTHOR_RE = re.compile(r"\b[A-Z]{1,4}\s*(?:,|$)")
# A lone initial ("R.", "A.") never occurs inside a real title but is the
# signature of a continued author list, which is what survives when the author
# block matched only the first name.  This single test removed every one of
# those 21 inventions without costing a single genuine title.
_BARE_INITIAL_RE = re.compile(r"(?:^|\s)[A-Z]\.(?:\s|$)")
# Trailing pinpoint of a book citation: "… Non-State Actors 193-95 (2010)".
_TRAILING_PINPOINT_RE = re.compile(
    r"[\s,]*(?:at\s+)?[\dxivl]+(?:\s*[–‐-]\s*[\dxivl]+)?$", re.I)


def date_anchored_title(text: str) -> str | None:
    """Title of a Bluebook citation anchored on a date rather than a reporter."""
    if _TREATY_OR_CASE_RE.search(text):
        return None
    anchor = _DATE_PAREN_RE.search(text)
    if anchor is None:
        return None
    head = text[:anchor.start()]
    signals = [s.end() for s in _SIGNAL_RE.finditer(head)]
    segment = head[signals[-1] if signals else 0:]
    authors = _AUTHOR_LIST_RE.match(segment)
    if not authors:
        return None
    if _VANCOUVER_AUTHOR_RE.search(authors.group(0)):
        return None
    rest = segment[authors.end():].strip()
    # The outlet, when present, is the last comma-separated segment — but only
    # when it reads like one.  "N.Y. Times" is an outlet; the ", and Disease" of
    # "The Story of the Human Body: Evolution, Health, and Disease" is the tail
    # of a title, and stripping it truncated the title mid-list.
    if "," in rest:
        body, tail = rest[:rest.rfind(",")], rest[rest.rfind(",") + 1:].strip()
        if tail[:1].isupper() and len(tail.split()) <= 6:
            rest = body
    title = re.sub(r"\s+", " ", _TRAILING_PINPOINT_RE.sub("", rest)
                   .replace("- ", "")).strip(" ,.")
    # An unbalanced "(" or an editor's credit means the split landed inside the
    # publication block, not at the end of the title.
    if "(" in title or re.search(r"\bed\.\s*$|\bed\.,", title):
        return None
    if _BARE_INITIAL_RE.search(title):
        return None
    if len(title.split()) < 2 or not title[:1].isupper():
        return None
    return title


# What each anchor proves about the SOURCE, not just about the title.
#
# A reporter anchor types the entry outright: "89 Tex. L. Rev. 1" is a volume, a
# journal and a first page, which is what a journal article is, and the readers
# above have already refused the reporters that are statutes, treaty series or law
# reports.  On the law-review paper this types 22 references that the raw-text
# heuristics leave `unknown` and one they call a webpage.
#
# `indexability` is medium, deliberately between the two: 24 of the 37
# reporter-anchored references resolve, so "low" understates them — but "high"
# would earn the `high_index_article_like` synthetic-risk signal, and a genuine
# law-review article that simply is not in Crossref would start reading as a
# suspected fabrication.
#
# A date anchor proves nothing about the type.  The same shape carries books, news
# stories, think-tank pieces and government papers, and 33 of the 48 here are
# already correctly typed `webpage` off their URL.  Guessing would be worse than
# the heuristic, so the reader declines to type them and only names the work.
#
# A treaty and a case are typed through `source_kind` alone.  `source_type` stays
# "unknown", which is the truth in its vocabulary -- these are not articles, books
# or webpages -- and adding a value there would reach eight report styles that
# switch on it, for no gain today.  `source_kind` is where resolve reads the kind,
# and it already carries the wider vocabulary.
#
# Naming the kind changes nothing downstream on its own: every consumer compares
# against known values, so `legal_instrument` falls through each branch exactly as
# `unknown` does.  That is the point for now -- these sources stop being carried
# down the article-shaped path -- and it leaves a name for the minimum checks a
# legal source should get, which is where a resolver for them would start.
_ANCHOR_CLASSIFICATION = {
    "reporter": {
        "source_type": "article",
        "source_kind": "article_like",
        "source_type_confidence": "high",
        "indexability": "medium",
    },
    "treaty": {
        "source_type": "unknown",
        "source_kind": "legal_instrument",
        "source_type_confidence": "high",
        "indexability": "low",
    },
    "case": {
        "source_type": "unknown",
        "source_kind": "legal_instrument",
        "source_type_confidence": "high",
        "indexability": "low",
    },
}
# A date anchor with an edition marker in its own parenthetical ("2d ed. 2018")
# is the one date-anchor shape that is unambiguously a book -- a journal, a
# webpage or a news story is never cited by "edition".  `source_type` stays
# "unknown" (the general date anchor still proves nothing about type, per the
# note above `_ANCHOR_CLASSIFICATION`); `source_kind` alone carries the signal,
# matching the vocabulary `resolve` already reads elsewhere (`book_like`), so
# the book resolver can be tried without mistyping the entry outright.
_BOOK_SHAPED_DATE_CLASSIFICATION = {
    "source_type": "unknown",
    "source_kind": "book_like",
    "source_type_confidence": "medium",
    "indexability": "low",
}

# A bare date does not establish a book: Bluebook uses it for news, reports and
# webpages too.  This stricter shape is the additional no-ISBN book route.  It
# is intentionally closed: each part is citation-owned positional evidence,
# rather than a guess based on words such as "report" or "press".
_IDENTIFIER_OR_URL_RE = re.compile(
    r"(?:https?://|\b(?:doi\s*:\s*)?10\.\d{4,9}/|\bPMID\s*[:#]?\s*\d+)",
    re.I,
)
_ORGANISATION_AUTHOR_RE = re.compile(
    r"\b(?:administration|agency|association|bank|centre|center|commission|"
    r"committee|council|department|foundation|government|institute|"
    r"ministry|office|organisation|organization|university)\b",
    re.I,
)
_SECOND_BIBLIOGRAPHIC_UNIT_RE = re.compile(
    r"(?:;\s*|,\s*with\s+|\bcompare\b.*\bwith\b)", re.I,
)


def personal_book_route(entry: str) -> dict | None:
    """Return a closed, citation-owned no-ISBN personal-book shape.

    The title is read before any catalog is contacted.  A dated Bluebook entry
    qualifies only with a personal author block, a complete year parenthetical,
    and either an edition or a pinpoint immediately before that parenthetical.
    """
    if (legal_instrument(entry) or _IDENTIFIER_OR_URL_RE.search(entry)
            or _SECOND_BIBLIOGRAPHIC_UNIT_RE.search(entry)):
        return None
    anchor = _DATE_PAREN_RE.search(entry)
    if anchor is None or not entry[anchor.end():].lstrip().startswith(")"):
        return None
    head = entry[:anchor.start()]
    signals = [s.end() for s in _SIGNAL_RE.finditer(head)]
    segment = head[signals[-1] if signals else 0:]
    authors = _AUTHOR_LIST_RE.match(segment)
    if not authors or _VANCOUVER_AUTHOR_RE.search(authors.group(0)):
        return None
    author_block = authors.group(0).strip(" ,")
    if _ORGANISATION_AUTHOR_RE.search(author_block):
        return None
    title = date_anchored_title(entry)
    if not title:
        return None
    # Keep the exact text between author and date; it may only be the title and
    # an optional immediate pinpoint.  This rejects outlet/date citations.
    before_date = segment[authors.end():].rstrip()
    pinpoint = _TRAILING_PINPOINT_RE.search(before_date)
    title_portion = before_date[:pinpoint.start()].strip(" ,.") if pinpoint else before_date.strip(" ,.")
    if re.sub(r"\W+", "", title_portion.casefold()) != re.sub(r"\W+", "", title.casefold()):
        return None
    if not anchor.group("edition") and pinpoint is None:
        return None
    surname_tokens = re.findall(r"[A-Za-zÀ-ÿ][\w'’-]*", author_block)
    if not surname_tokens:
        return None
    # The first person's surname is the final token of the first comma-delimited
    # personal name.  It is used only to reject catalog candidates, never to
    # supply metadata absent from the citation.
    first_person = re.split(r"\s*&\s*|,\s*and\s+|,\s*", author_block,
                            maxsplit=1)[0]
    first_tokens = re.findall(r"[A-Za-zÀ-ÿ][\w'’-]*", first_person)
    if len(first_tokens) < 2:
        return None
    return {"title": title, "first_author_surname": first_tokens[-1]}


def _date_anchor_is_book_shaped(entry: str) -> bool:
    """Whether the date anchor matched an edition marker in its parenthetical."""
    anchor = _DATE_PAREN_RE.search(entry)
    return bool(anchor and anchor.group("edition")) or personal_book_route(entry) is not None


def read(entry: str) -> dict | None:
    """The work a Bluebook entry names, or None if the entry is not Bluebook.

    The reporter anchor is tried first: it is the more discriminating of the two,
    and a journal citation also carries a parenthesised year that the dated reader
    would otherwise anchor on.
    """
    instrument = legal_instrument(entry)
    if instrument:
        # Claimed, and deliberately titleless: the citation is the identifier.
        # Both title readers already refuse these, so this only decides whether
        # the heuristics get to invent one.
        return {"reader": NAME, "title": None, "anchor": instrument,
                "classification": dict(_ANCHOR_CLASSIFICATION[instrument])}
    for anchor, extract in (("reporter", reporter_anchored_title),
                            ("date", date_anchored_title)):
        title = extract(entry)
        if title:
            record = {"reader": NAME, "title": title, "anchor": anchor}
            classification = _ANCHOR_CLASSIFICATION.get(anchor)
            if (classification is None and anchor == "date"
                    and _date_anchor_is_book_shaped(entry)):
                classification = _BOOK_SHAPED_DATE_CLASSIFICATION
            if classification:
                record["classification"] = dict(classification)
            return record
    return None
