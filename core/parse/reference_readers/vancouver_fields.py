"""Conservative Vancouver author-year journal entry reader."""

from __future__ import annotations

import re

from core.parse.citation_coordinates import extract_cited_coordinates

from ._field_utils import author_fields, field

NAME = "vancouver-fields"
VERSION = "1"
ORDER = 20
STRUCTURAL = True
_ENTRY = re.compile(
    r"^\s*(?P<authors>[A-Z][A-Za-zÀ-ÿ'’-]+\s+[A-Z]{1,4}"
    r"(?:,\s*[A-Z][A-Za-zÀ-ÿ'’-]+\s+[A-Z]{1,4}){0,12})"
    r"\s*\((?P<year>(?:18|19|20)\d{2})\)\s*"
    # Prefer the last sentence boundary that leaves a closed journal/volume/
    # page tuple. This preserves compound titles whose early sentence ends in
    # a word that could otherwise begin a permissive multiword journal match.
    r"(?P<title>[^\r\n]+)\.\s+"
    r"(?P<journal>[A-Z][A-Za-z0-9'’&-]*\.?(?:\s+[A-Za-z][A-Za-z0-9'’&-]*\.?){0,6})"
    r"\s+(?P<volume>\d{1,4})"
    r"(?:\((?P<issue>[^)]+)\))?\s*:\s*"
    r"(?P<pages>(?:e?\d+|[A-Za-z]\d+)(?:\s*[–—−‐‑-]\s*(?:e?\d+|[A-Za-z]\d+))?)\.?"
    r"(?:\s+(?:https?://\S+|(?:doi\s*:\s*)?10\.\d{4,9}/\S+))?\s*$"
)
_AUTHOR = re.compile(r"[A-Z][A-Za-zÀ-ÿ'’-]+\s+[A-Z]{1,4}")
_START = re.compile(
    r"^\s*[A-Z][A-Za-zÀ-ÿ'’-]+\s+[A-Z]{1,4}"
    r"(?:,\s*[A-Z][A-Za-zÀ-ÿ'’-]+\s+[A-Z]{1,4}){0,12}"
    r"\s*\((?:18|19|20)\d{2}\)\s+"
)


def starts_entry(line: str) -> bool:
    """Whether a physical line starts a Vancouver author-year entry."""
    return bool(_START.match(line))


def parse_fields(entry: str) -> dict | None:
    match = _ENTRY.match(entry)
    if not match:
        return None
    # A syntactic split is not enough: an abbreviation period can be mistaken
    # for the title/journal boundary ("J. Clin. Oncol." -> journal "Oncol.").
    # Require the journal, volume, and locator spans to match one independently
    # recognized closed coordinate tuple in the raw entry.
    coordinates = extract_cited_coordinates(entry)
    by_kind = {}
    for item in coordinates:
        by_kind.setdefault(item.get("kind"), []).append(item)
    locator_kinds = ("article_page_range", "article_locator", "article_number", "elocator")
    containers = by_kind.get("container", [])
    volumes = by_kind.get("volume", [])
    locators = [item for kind in locator_kinds for item in by_kind.get(kind, [])]
    if len(containers) != 1 or len(volumes) != 1 or len(locators) != 1:
        return {"reader": NAME, "version": VERSION, "status": "ambiguous",
                "fields": {}, "conflicts": [{"field": "journal", "reason": "no_unique_closed_coordinate_tuple"}]}
    container, volume, locator = containers[0], volumes[0], locators[0]
    if not (container.get("rule_id") == volume.get("rule_id") == locator.get("rule_id")):
        return {"reader": NAME, "version": VERSION, "status": "ambiguous",
                "fields": {}, "conflicts": [{"field": "journal", "reason": "competing_coordinate_rules"}]}
    def coordinate_span(item):
        spans = item.get("spans") or []
        if len(spans) != 1:
            return None
        return spans[0].get("raw_start"), spans[0].get("raw_end")
    if (coordinate_span(container) != match.span("journal")
            or coordinate_span(volume) != match.span("volume")
            or coordinate_span(locator) != match.span("pages")):
        return {"reader": NAME, "version": VERSION, "status": "ambiguous",
                "fields": {}, "conflicts": [{"field": "journal", "reason": "coordinate_span_mismatch"}]}
    authors_text = match.group("authors")
    author_matches = list(_AUTHOR.finditer(authors_text))
    if not author_matches or ",".join(m.group(0) for m in author_matches).casefold() != re.sub(r"\s*,\s*", ",", authors_text).casefold():
        return None
    fields = {
        "authors": author_fields(entry, [(m.group(0), match.start("authors") + m.start(),
                                           match.start("authors") + m.end()) for m in author_matches], NAME),
        "title": field(match.group("title"), entry, *match.span("title"), NAME),
        "journal": field(match.group("journal"), entry, *match.span("journal"), NAME),
        "year": field(int(match.group("year")), entry, *match.span("year"), NAME),
        "volume": field(match.group("volume"), entry, *match.span("volume"), NAME),
        "pages": field(match.group("pages"), entry, *match.span("pages"), NAME),
    }
    first = author_matches[0]
    surname = first.group(0).split()[0]
    fields["first_author"] = field(surname, entry, match.start("authors") + first.start(),
                                    match.start("authors") + first.start() + len(surname),
                                    NAME + ":surname-before-initials")
    if match.group("issue"):
        fields["issue"] = field(match.group("issue"), entry, *match.span("issue"), NAME)
    return {"reader": NAME, "version": VERSION, "status": "parsed", "fields": fields, "conflicts": []}
