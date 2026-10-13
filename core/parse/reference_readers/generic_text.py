"""Explicit last-resort field reader backed by current deterministic heuristics."""

from __future__ import annotations

import re

NAME = "generic-text"
VERSION = "1"
ORDER = 1000
GENERIC = True
STRUCTURAL = False


def _field(value, entry, start, end, rule):
    return {"value": value, "spans": [{"start": start, "end": end,
                                       "raw_value": entry[start:end]}], "rule": rule}


def parse_fields(entry: str, *, chicago_hanging: bool = False) -> dict | None:
    try:
        from core.parse import parsing_common as pc
    except ImportError:
        import parsing_common as pc
    fields = {}
    # Call the heuristic directly: the public title extractor first consults
    # structural readers, and routing back through it would recurse here.
    title = pc._extract_title_heuristic(entry, chicago_hanging=chicago_hanging)
    if title:
        start = entry.find(title)
        if start >= 0 and entry.find(title, start + 1) < 0:
            fields["title"] = _field(title, entry, start, start + len(title),
                                     "generic-title-heuristic")
    doi_match = re.search(r"10\.\d{4,9}/\S+", entry)
    if doi_match:
        doi = doi_match.group(0).rstrip(".,;)]}>")
        end = doi_match.start() + len(doi)
        if end > doi_match.start():
            fields["doi"] = _field(doi, entry, doi_match.start(), end,
                                   "generic-doi-token")
    url_match = re.search(r"https?://[^\s)]+", entry, re.I)
    if url_match:
        url = url_match.group(0).rstrip(".,;]>»")
        end = url_match.start() + len(url)
        if end > url_match.start():
            fields["url"] = _field(url, entry, url_match.start(), end,
                                   "generic-url-token")
    if not fields:
        return None
    return {"reader": NAME, "version": VERSION, "status": "parsed",
            "fields": fields, "conflicts": []}
