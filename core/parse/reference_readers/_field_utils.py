"""Shared construction helpers for structural reference readers."""

from __future__ import annotations


def title_journal_boundaries(
    entry: str, title_start: int, journal_end: int
) -> list[tuple[int, int]]:
    """Return period boundaries between a title and its journal.

    The source grammar does not distinguish every sentence stop from a dotted
    journal abbreviation. Keep each period followed by a word as a possible
    cut so competing readings become explicit ambiguity instead of a guessed
    title or venue.
    """
    boundaries = []
    for period in range(title_start, journal_end):
        if entry[period] != "." or period + 1 >= journal_end or not entry[period + 1].isspace():
            continue
        journal_start = period + 1
        while journal_start < journal_end and entry[journal_start].isspace():
            journal_start += 1
        if journal_start >= journal_end or not entry[journal_start].isalpha():
            continue
        boundaries.append((period, journal_start))
    return boundaries


def field(value, entry: str, start: int, end: int, rule: str) -> dict:
    return {"value": value, "spans": [{"start": start, "end": end,
                                      "raw_value": entry[start:end]}],
            "rule": rule}


def author_fields(entry: str, spans: list[tuple[str, int, int]], rule: str) -> dict:
    return {"value": [name for name, _start, _end in spans],
            "spans": [{"start": start, "end": end, "raw_value": entry[start:end]}
                      for _name, start, end in spans],
            "rule": rule}
