"""APA-style author-date journal references with a closed volume/page tail."""

from __future__ import annotations

import re

from ._field_utils import author_fields, field, title_journal_boundaries

NAME = "apa-journal"
VERSION = "1"
ORDER = 20
STRUCTURAL = True
_NAME = r"[A-Z][A-Za-zÀ-ÿ'’-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'’-]+)*,\s*(?:[A-Z]\.\s*){1,4}"
_ENTRY = re.compile(
    rf"^\s*(?P<authors>{_NAME}(?:,\s*(?:&\s*)?{_NAME})*)"
    r"\s*\((?P<year>(?:18|19|20)\d{2})\)\.\s*"
    r"(?P<title>[A-Z][^\r\n.]*?)\.\s*(?P<journal>.+),\s*"
    r"(?P<volume>\d{1,4})(?:\((?P<issue>[^)]+)\))?,\s*"
    r"(?P<pages>(?:e?\d+|[A-Z]\d+)(?:\s*[–—−‐‑-]\s*(?:e?\d+|[A-Z]\d+))?)\.?"
    r"(?:\s+(?:https?://\S+|(?:doi\s*:\s*)?10\.\d{4,9}/\S+))?\s*$"
)
_START = re.compile(rf"^\s*{_NAME}(?:,\s*(?:&\s*)?{_NAME})*\s*\((?:18|19|20)\d{{2}}\)\.\s*")


def starts_entry(line: str) -> bool:
    """Whether a physical line starts this reader's author-date structure."""
    return bool(_START.match(line))


def parse_fields(entry: str) -> dict | None:
    match = _ENTRY.match(entry)
    if not match:
        return None
    block = match.group("authors")
    names = list(re.finditer(_NAME, block))
    if (not names or block[:names[0].start()].strip()
            or block[names[-1].end():].strip()
            or any(not re.fullmatch(r"\s*,\s*&?\s*", block[left.end():right.start()])
                   for left, right in zip(names, names[1:]))):
        return None
    fields = {
        "authors": author_fields(entry, [(m.group(0).strip(), match.start("authors") + m.start() + len(m.group(0)) - len(m.group(0).lstrip()),
                                           match.start("authors") + m.end() - (len(m.group(0)) - len(m.group(0).rstrip()))) for m in names], NAME),
        "year": field(int(match.group("year")), entry, *match.span("year"), NAME),
        "volume": field(match.group("volume"), entry, *match.span("volume"), NAME),
        "pages": field(match.group("pages"), entry, *match.span("pages"), NAME),
    }
    if match.group("issue"):
        fields["issue"] = field(match.group("issue"), entry, *match.span("issue"), NAME)
    first_name = names[0]
    surname = first_name.group(0).split(",", 1)[0]
    fields["first_author"] = field(surname, entry, match.start("authors") + first_name.start(),
                                    match.start("authors") + first_name.start() + len(surname),
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
