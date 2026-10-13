"""Personal author-year book entries with an explicit place-publisher tail."""

from __future__ import annotations

import re

from ._field_utils import author_fields, field

NAME = "dated-book"
VERSION = "1"
ORDER = 30
STRUCTURAL = True
_ENTRY = re.compile(
    r"^\s*(?P<authors>[A-ZÀ-Þ][\w'’-]+,\s*[A-ZÀ-Þ][\w'’.-]+(?:"
    r"(?:\s+[A-ZÀ-Þ][\w'’.-]+)?(?:;\s*[A-ZÀ-Þ][\w'’-]+,\s*[A-ZÀ-Þ][\w'’.-]+)*)?)"
    r"\s+(?P<year>(?:18|19|20)\d{2})\s+(?P<title>[A-Z][^\r\n]*?)\.\s+"
    r"(?P<place>[A-Z][^:\r\n]+):\s*(?P<publisher>[^.\r\n]+)\.?"
    r"(?:\s+(?:https?://\S+|(?:doi\s*:\s*)?10\.\d{4,9}/\S+))?\s*$"
)
_AUTHOR = re.compile(r"[A-ZÀ-Þ][\w'’-]+,\s*[A-ZÀ-Þ][\w'’.-]+(?:\s+[A-ZÀ-Þ][\w'’.-]+)?")


def parse_fields(entry: str) -> dict | None:
    match = _ENTRY.match(entry)
    if not match:
        return None
    authors_text = match.group("authors").strip()
    authors = list(_AUTHOR.finditer(authors_text))
    if not authors or "; ".join(m.group(0) for m in authors).casefold() != authors_text.casefold():
        return None
    offset = match.start("authors") + len(match.group("authors")) - len(match.group("authors").lstrip())
    fields = {
        "authors": author_fields(entry, [(m.group(0), offset + m.start(), offset + m.end()) for m in authors], NAME),
        "title": field(match.group("title"), entry, *match.span("title"), NAME),
        "year": field(int(match.group("year")), entry, *match.span("year"), NAME),
        "place": field(match.group("place"), entry, *match.span("place"), NAME),
        "publisher": field(match.group("publisher"), entry, *match.span("publisher"), NAME),
    }
    # A chapter/contribution citation can carry an author-adjacent year and a
    # terminal imprint, while the intervening text includes its containing
    # volume, editor credit, and page range. The book reader cannot separate
    # those titles safely, but the author, year, and imprint anchors remain
    # independently source-spanned.
    title_text = match.group("title")
    if (re.search(r"\bedited by\b", title_text, re.I)
            and re.search(r"\b\d+\s*[–—−‐‑-]\s*\d+\s*$", title_text)):
        fields.pop("title", None)
    # The comma before the given name makes the surname boundary explicit.
    first = authors[0]
    surname = first.group(0).split(",", 1)[0]
    fields["first_author"] = field(surname, entry, offset + first.start(),
                                    offset + first.start() + len(surname), NAME + ":surname-before-comma")
    return {"reader": NAME, "version": VERSION, "status": "parsed", "fields": fields, "conflicts": []}
