"""Semicolon-separated author list with year-volume-pages journal tail."""

from __future__ import annotations

import re

from ._field_utils import author_fields, field, title_journal_boundaries

NAME = "semicolon-journal"
VERSION = "1"
ORDER = 20
STRUCTURAL = True
_AUTHOR = re.compile(r"[^;]+")
_INITIALS = r"(?:[A-Z]\.\s*)+"
_AUTHOR_SHAPE = re.compile(
    r"\s*(?:[A-Z][\w'’-]+|[a-z][\w'’-]+(?:\s+[A-Z]?[a-z][\w'’-]+)*),"
    rf"\s*{_INITIALS}"
)
_ENTRY = re.compile(
    rf"^\s*(?P<authors>[A-Z][\w'’-]+,\s*{_INITIALS}"
    rf"(?:;\s*[A-Za-z][\w'’ -]+,\s*{_INITIALS})+)\s+"
    r"(?P<title>[A-Z][^\r\n.]*?)\.\s+"
    r"(?P<journal>[A-Z][^\r\n]*?)\s+(?P<year>(?:18|19|20)\d{2}),\s*"
    r"(?P<volume>\d{1,4}),\s*"
    r"(?P<pages>(?:e?\d+|[A-Z]\d+)(?:\s*[–—−‐‑-]\s*(?:e?\d+|[A-Z]\d+))?)\.?"
    r"(?:\s+(?:https?://\S+|(?:doi\s*:\s*)?10\.\d{4,9}/\S+))?\s*$"
)
_START = re.compile(
    rf"^\s*[A-Z][\w'’-]+,\s*{_INITIALS}(?:;\s*"
    rf"[A-Za-z][\w'’ -]+,\s*{_INITIALS})+"
)


def starts_entry(line: str) -> bool:
    """Whether a physical line starts a semicolon-author journal entry."""
    return bool(_START.match(line))


def parse_fields(entry: str) -> dict | None:
    match = _ENTRY.match(entry)
    if not match:
        return None
    authors_text = match.group("authors").strip()
    authors = list(_AUTHOR.finditer(authors_text))
    if len(authors) < 2 or any(not _AUTHOR_SHAPE.fullmatch(item.group(0)) for item in authors):
        return None
    author_offset = match.start("authors") + (len(match.group("authors")) - len(match.group("authors").lstrip()))
    fields = {
        "authors": author_fields(entry, [(item.group(0).strip(), author_offset + item.start() + len(item.group(0)) - len(item.group(0).lstrip()),
                                           author_offset + item.end() - (len(item.group(0)) - len(item.group(0).rstrip()))) for item in authors], NAME),
        "year": field(int(match.group("year")), entry, *match.span("year"), NAME),
        "volume": field(match.group("volume"), entry, *match.span("volume"), NAME),
        "pages": field(match.group("pages"), entry, *match.span("pages"), NAME),
    }
    first_name = authors[0].group(0).strip()
    surname = first_name.split(",", 1)[0]
    fields["first_author"] = field(surname, entry, author_offset + authors[0].start(),
                                    author_offset + authors[0].start() + len(surname),
                                    NAME + ":surname-before-comma")
    title_start = match.start("title")
    journal_end = match.end("journal")
    boundaries = title_journal_boundaries(entry, title_start, journal_end)
    if len(boundaries) != 1:
        if len(boundaries) > 1:
            return {"reader": NAME, "version": VERSION, "status": "parsed",
                    "fields": fields,
                    "conflicts": [
                        {"field": "title", "reason": "ambiguous_title_journal_boundary"},
                        {"field": "journal", "reason": "ambiguous_title_journal_boundary"},
                    ]}
        return {"reader": NAME, "version": VERSION, "status": "ambiguous",
                "fields": {},
                "conflicts": [{"field": "title",
                               "reason": "unproven_title_journal_boundary"}]}
    title_end, journal_start = boundaries[0]
    title_text = entry[title_start:title_end].rstrip()
    title_end = title_start + len(title_text)
    journal_raw = entry[journal_start:journal_end]
    journal_left_space = len(journal_raw) - len(journal_raw.lstrip())
    journal_text = journal_raw.strip()
    journal_start += journal_left_space
    journal_end = journal_start + len(journal_text)
    fields["title"] = field(title_text, entry, title_start, title_end, NAME)
    fields["journal"] = field(journal_text, entry, journal_start, journal_end, NAME)
    return {"reader": NAME, "version": VERSION, "status": "parsed", "fields": fields, "conflicts": []}
