# core/parse/citation_coordinates.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Conservative extraction of bibliographic facts asserted by a citation.

Every emitted value is tied to raw-entry spans.  These are citation claims,
never canonical metadata returned by a resolver.
"""

from __future__ import annotations

import re


EXTRACTOR_VERSION = "cited-coordinates/v2"
_DASHES = "-‐‑‒–—−"
_ELOCATOR_TOKEN = r"[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*"
_ELOCATOR_RANGE = rf"{_ELOCATOR_TOKEN}\s*[{_DASHES}]\s*{_ELOCATOR_TOKEN}"
_LOCATOR = (
    rf"(?:{_ELOCATOR_RANGE}|{_ELOCATOR_TOKEN}|"
    rf"\d+\s*[{_DASHES}]\s*\d+)"
)
_ISSUE = rf"[A-Za-z0-9/]+(?:\s*[{_DASHES}]\s*[A-Za-z0-9/]+)?"
_PARENTHESIZED_YEAR_TAIL_RE = re.compile(
    rf"(?P<volume>\d{{1,4}})\s*\(\s*(?P<year>(?:19|20)\d{{2}})\s*\)\s*:?\s*"
    rf"(?P<locator>{_LOCATOR}|\d{{1,9}})"
    r"(?=\s*[.;]?\s*(?:\[[A-Za-z0-9][A-Za-z0-9:./_-]{0,79}\]\s*)*"
    r"[.;]?\s*$)",
    re.I,
)
_REPEATED_VOLUME_COLON_ISSUE_PAGES_RE = re.compile(
    rf"\b(?P<volume>\d{{1,4}})\s*:\s*(?P<issue>{_ISSUE})\s+"
    rf"(?P=volume)\s*,\s*(?P<locator>\d+\s*[{_DASHES}]\s*\d+)"
    rf"\s*\((?:19|20)\d{{2}}\)\s*[.]?\s*$",
    re.I,
)
_ATTACHED_VENUE_VOLUME_PAGE_RANGE_YEAR_RE = re.compile(
    rf"(?<=[^\W\d_])(?P<volume>\d{{1,4}})\s*,\s*"
    rf"(?P<pages>\d+\s*[{_DASHES}]\s*\d+)\s*"
    r"\(\s*(?P<year>(?:19|20)\d{2})\s*\)\s*[.]?\s*$"
)
_POST_LOCATOR_YEAR_RE = re.compile(
    r"(?P<volume>\d{1,4})\s*,\s*(?P<locator>\d{2,9})\s*"
    r"\(\s*(?P<year>(?:19|20)\d{2})\s*\)",
    re.I,
)
_COMPACT_INITIAL_AUTHOR = (
    r"[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\s+[A-Z]{1,4}"
)
_COMPACT_INITIAL_AUTHOR_HEAD_RE = re.compile(
    r"^\s*(?:" + _COMPACT_INITIAL_AUTHOR + r",\s+)+"
    + _COMPACT_INITIAL_AUTHOR + r"\.\s+"
)
_COMPACT_INITIAL_CONTAINER_YEAR_RE = re.compile(
    r"\.(?P<container>[A-Z][A-Za-zÀ-ÿ .&'’()\-]{1,100}?)"
    r"(?P<year>(?:19|20)\d{2});"
)
_COMPACT_INITIAL_ISSUE_PAGE_RANGE_RE = re.compile(
    rf"\d{{1,4}}\(\s*{_ISSUE}\s*\)\s*:\s*"
    rf"\d{{1,9}}\s*[{_DASHES}]\s*\d{{1,9}}\.?\s*$"
)

_AUTHOR_UPPER = "A-ZÀ-ÞĀ-Ž"
_AUTHOR_NAME_TOKEN = (
    r"(?:(?:[a-zà-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’‐‑‒–—\-]*\s+){0,2}"
    rf"[{_AUTHOR_UPPER}][A-Za-zÀ-ÖØ-öø-ÿĀ-ž'’‐‑‒–—\-]*)"
)
_AUTHOR_SURNAME = (
    rf"{_AUTHOR_NAME_TOKEN}(?:\s+{_AUTHOR_NAME_TOKEN}){{0,2}}"
)
_AUTHOR_INITIAL_ATOM = (
    rf"[{_AUTHOR_UPPER}]\.(?:-\s*[{_AUTHOR_UPPER}]\.)?"
    rf"(?!\s+(?!(?i:et\s+al\.))[a-zà-ÿā-ž])"
)
_AUTHOR_INITIAL_SEQUENCE = (
    rf"{_AUTHOR_INITIAL_ATOM}"
    rf"(?:(?:\s+|(?=[{_AUTHOR_UPPER}]\.)){_AUTHOR_INITIAL_ATOM}){{0,3}}"
    rf"(?:\s+[{_AUTHOR_UPPER}](?=\s*(?:,|&|(?i:and)\b)))?"
)
_SURNAME_FIRST_AUTHOR = re.compile(
    rf"{_AUTHOR_SURNAME},\s*{_AUTHOR_INITIAL_SEQUENCE}"
    r"(?:\s+(?i:Jr|Sr|II|III|IV))?"
)
_AUTHOR_LIST_SEPARATOR = re.compile(
    r"\s*(?:,\s*(?:&\s*|(?i:and)\s+)?|&\s*|(?i:and)\s+)"
)
_AUTHOR_ET_AL = re.compile(r"\s*(?i:et\s+al\.)")
_ABBREVIATED_CONTAINER_TAIL = re.compile(
    r"(?:[A-ZÀ-Þ]\.|[A-ZÀ-Þ][A-Za-zÀ-ÿ0-9&'’/-]{1,}\.)\s*"
    r"(?:[A-ZÀ-Þ][A-Za-zÀ-ÿ0-9&'’/-]*\.?\s*){1,10}"
)
_BARE_CONTAINER_INITIALISM = re.compile(r"[A-Z]{2,10}")
_CAMELCASE_CONTAINER_TAIL = re.compile(r"(?=[a-z])(?=.*[A-Z])[A-Za-z0-9]+$")


def _normal(value: str) -> str:
    return " ".join(
        value.replace("‐", "-").replace("‑", "-").replace("‒", "-")
        .replace("–", "-").replace("—", "-").replace("−", "-").split()
    )


def _prefixed_locator_token_parts(value: str) -> tuple[str, str] | None:
    """Split a complete alphanumeric locator into its prefix and numeric tail."""
    match = re.fullmatch(
        r"(?P<prefix>[A-Za-z][A-Za-z0-9]*?)(?P<endpoint>\d+)",
        _normal(value), re.I,
    )
    if match is None:
        return None
    return match.group("prefix").casefold(), match.group("endpoint")


def _uniform_prefixed_locator_range_parts(
    value: str,
) -> tuple[str, str, str] | None:
    """Return prefix and endpoints only for a complete, distinct uniform range."""
    match = re.fullmatch(
        rf"(?P<start>{_ELOCATOR_TOKEN})\s*-\s*(?P<end>{_ELOCATOR_TOKEN})",
        _normal(value), re.I,
    )
    if match is None:
        return None
    start = _prefixed_locator_token_parts(match.group("start"))
    end = _prefixed_locator_token_parts(match.group("end"))
    if (
        start is None or end is None
        or start[0] != end[0]
        or start[1] == end[1]
    ):
        return None
    return start[0], start[1], end[1]


def _literal_coordinate_span(raw: str, row: dict) -> tuple[int, int] | None:
    """Return one exact source span for a coordinate row."""
    spans = row.get("spans")
    if not isinstance(spans, list) or len(spans) != 1 or not isinstance(spans[0], dict):
        return None
    start, end = spans[0].get("raw_start"), spans[0].get("raw_end")
    if (
        not isinstance(start, int) or isinstance(start, bool)
        or not isinstance(end, int) or isinstance(end, bool)
        or not 0 <= start < end <= len(raw)
        or raw[start:end] != row.get("raw_value")
        or not isinstance(row.get("normalized_value"), str)
        or _normal(raw[start:end]) != row.get("normalized_value")
    ):
        return None
    return start, end


def _field(
    kind: str,
    raw: str,
    start: int,
    end: int,
    rule: str,
    *,
    spans: list[tuple[int, int]] | None = None,
) -> dict:
    source_spans = spans or [(start, end)]
    raw_value = "".join(raw[left:right] for left, right in source_spans)
    return {
        "kind": kind,
        "raw_value": raw_value,
        "normalized_value": _normal(raw_value),
        "rule_id": rule,
        "extractor_version": EXTRACTOR_VERSION,
        "spans": [
            {"raw_start": left, "raw_end": right} for left, right in source_spans
        ],
    }


def _append_unique(out: list[dict], item: dict | None) -> None:
    if item is not None and item["kind"] not in {row["kind"] for row in out}:
        out.append(item)


def _surname_first_author_list_end(prefix: str) -> int | None:
    """Return the end of a closed surname-first initialized author list.

    This is deliberately a source-text grammar, not an author-name lookup.
    Callers can require the returned boundary to align with a later source
    span before treating a title as structurally absent.
    """
    first = _SURNAME_FIRST_AUTHOR.match(prefix)
    if first is None:
        return None
    cursor = first.end()
    author_count = 1
    final_separator = ""
    while True:
        et_al = _AUTHOR_ET_AL.match(prefix, cursor)
        if et_al is not None:
            return et_al.end()
        separator = _AUTHOR_LIST_SEPARATOR.match(prefix, cursor)
        if separator is None:
            if cursor == len(prefix):
                return cursor
            if not prefix[cursor:].strip(" \t\r\n,.;:!?\"'“”‘’()"):
                return cursor
            if author_count > 1 and re.search(r"&|(?i:and)", final_separator):
                return cursor
            return None
        next_author = _SURNAME_FIRST_AUTHOR.match(prefix, separator.end())
        if next_author is None:
            # A comma after the final author closes the list before the
            # container. A dangling conjunction is ambiguous and fails closed.
            if (
                re.fullmatch(r"\s*,\s*", separator.group(0))
                and not re.match(r"\s*(?:&|(?i:and)\b)", prefix[separator.end():])
            ):
                return separator.end()
            return None
        cursor = next_author.end()
        author_count += 1
        final_separator = separator.group(0)


def _surname_first_author_container_boundary(prefix: str) -> int | None:
    """Find a closed initialized author head before a compact venue tail."""
    complete_end = _surname_first_author_list_end(prefix)
    if complete_end is not None:
        suffix = prefix[complete_end:].strip(" \t\r\n,.;:!?\"'“”‘’()")
        if _container_suffix_is_closed(suffix):
            return complete_end

    # A single surname-first author followed by a compact abbreviated venue
    # is also a closed head. The longest initials match keeps forms such as
    # ``Astumian, R. D. Phys. Chem.`` source-aligned.
    author = _SURNAME_FIRST_AUTHOR.match(prefix)
    if author is not None:
        suffix = prefix[author.end():].strip(" \t\r\n,.;:!?\"'“”‘’()")
        if _container_suffix_is_closed(suffix):
            return author.end()
    return None


def _strict_surname_first_author_container_boundary(prefix: str) -> int | None:
    """Prove a complete surname-first list before a strict abbreviated venue."""
    complete_end = _surname_first_author_list_end(prefix)
    if complete_end is not None and _strict_abbreviated_container_tail(
        prefix[complete_end:],
    ):
        return complete_end
    author = _SURNAME_FIRST_AUTHOR.match(prefix)
    if author is not None and _strict_abbreviated_container_tail(prefix[author.end():]):
        return author.end()
    return None


def _lowercase_prefix_abbreviated_container_tail(suffix: str) -> bool:
    """Recognize a short lowercase prefix and multiple dotted word atoms.

    The final period may already have been stripped from a coordinate span.
    Single-letter chains and ordinary undotted names do not prove this shape.
    """
    return re.fullmatch(
        r"[a-z]{2,5}\s+(?:[A-Z][a-z]{1,4}\.\s+){1,5}[A-Z][a-z]{1,4}\.?",
        suffix,
    ) is not None


def _container_suffix_is_closed(suffix: str) -> bool:
    """Require a titleless author head to lead directly to a venue-shaped tail."""
    if not suffix:
        return False
    if not re.search(r"[.!?]", suffix):
        return True
    return (
        _ABBREVIATED_CONTAINER_TAIL.fullmatch(suffix) is not None
        or _lowercase_prefix_abbreviated_container_tail(suffix)
    )


def _strict_abbreviated_container_tail(suffix: str) -> bool:
    """Require a compact abbreviation chain with at least one word token."""
    value = suffix.strip(" \t\r\n,;")
    if re.fullmatch(
        r"[A-ZÀ-Þ][A-Za-zÀ-ÿ0-9&'’/-]*\.?"
        r"(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÿ0-9&'’/-]*\.?){1,9}",
        value,
    ) is None:
        return False
    tokens = re.findall(r"[A-ZÀ-Þ][A-Za-zÀ-ÿ0-9&'’/-]*\.?", value)
    word_tokens = [token.rstrip(".") for token in tokens if len(token.rstrip(".")) > 1]
    return (
        len(word_tokens) >= 2
        and any(
            len(token.rstrip(".")) > 1 and token.endswith(".")
            for token in tokens[:-1]
        )
    )


def _title_boundary(prefix: str, cited_title: str | None) -> int | None:
    boundaries: list[int] = []
    # A very short unquoted parsed title can be a journal fragment (e.g.
    # ``Light: Sci`` in ``Light: Sci. Appl``). Do not promote that ambiguous
    # fragment over punctuation evidence. Explicit quotation remains usable.
    if cited_title and len(re.findall(r"\w+", cited_title)) >= 3:
        matches = list(re.finditer(re.escape(cited_title), prefix, re.I))
        if matches:
            end = matches[-1].end()
            tail = prefix[end:]
            # A truncated title ending before ``No. 200: ...`` is not a safe
            # boundary from which to read a journal name.
            if not re.match(r"\s*\.?\s*(?:no\.?\s*)?\d+\s*:", tail, re.I):
                boundaries.append(end)
    quoted = list(re.finditer(r"[\"“‘][^\"”’]{2,300}[\"”’]", prefix))
    if quoted:
        boundaries.append(quoted[-1].end())
    return max(boundaries) if boundaries else None


def _compact_initial_author_container_before(
    raw: str, coordinate_start: int, rule: str, cited_title: str | None,
) -> dict | None:
    """Read a glued container/year only in a closed author/title/page tuple."""
    if not cited_title:
        return None
    author_head = _COMPACT_INITIAL_AUTHOR_HEAD_RE.match(raw)
    if not author_head:
        return None
    title_tokens = re.split(r"\s+", cited_title.strip())
    if not title_tokens:
        return None
    title_pattern = r"(?<!\w)" + r"\s+".join(
        re.escape(token) for token in title_tokens
    ) + r"(?!\w)"
    prefix = raw[author_head.end():coordinate_start]
    title_matches = list(re.finditer(title_pattern, prefix, re.I))
    if len(title_matches) != 1:
        return None
    title_end = author_head.end() + title_matches[0].end()
    container_year = _COMPACT_INITIAL_CONTAINER_YEAR_RE.fullmatch(
        raw[title_end:coordinate_start]
    )
    if (
        container_year is None
        or _COMPACT_INITIAL_ISSUE_PAGE_RANGE_RE.fullmatch(raw[coordinate_start:]) is None
    ):
        return None
    start = title_end + container_year.start("container")
    end = title_end + container_year.end("container")
    return _field("container", raw, start, end, rule)


def _closed_colon_parenthesized_container_before(
    raw: str, coordinate_start: int, rule: str, boundary: int,
) -> dict | None:
    """Read a closed colon-author container with a terminal numeric year.

    This applies only when a completed source-title sentence ends before the
    container and one separate publication tuple closes the entry.
    """
    if rule not in {
        "volume_locator", "volume_issue_locator", "volume_colon_single",
    }:
        return None
    try:
        from core.parse.parsing_common import (
            _COLON_AUTHOR_PUBLICATION_TUPLE_RE,
            _COLON_INITIAL_AUTHOR_HEAD_RE,
            _closed_colon_author_entry,
        )
    except ImportError:  # direct execution fallback
        from parsing_common import (
            _COLON_AUTHOR_PUBLICATION_TUPLE_RE,
            _COLON_INITIAL_AUTHOR_HEAD_RE,
            _closed_colon_author_entry,
        )

    head = _COLON_INITIAL_AUTHOR_HEAD_RE.match(raw)
    if head is None or not _closed_colon_author_entry(raw):
        return None

    identifiers = [
        match.span()
        for pattern in (
            r"\b10\.\d{4,9}/\S+",
            r"https?://\S+",
            r"\barxiv[:\s]*\d{4}\.\d{4,5}(?:v\d+)?\b",
        )
        for match in re.finditer(pattern, raw, re.IGNORECASE)
    ]

    def overlaps_identifier(span: tuple[int, int]) -> bool:
        return any(span[0] < end and span[1] > start for start, end in identifiers)

    tuples = [
        match for match in _COLON_AUTHOR_PUBLICATION_TUPLE_RE.finditer(raw)
        if not overlaps_identifier(match.span())
    ]
    if len(tuples) != 1:
        return None
    publication = tuples[0]
    volume = re.search(
        r";\s*(?P<volume>\d{1,4})(?:\s*\(\s*\d+\s*\))?\s*:",
        publication.group(0),
    )
    if (
        volume is None
        or publication.start() + volume.start("volume") != coordinate_start
        or boundary <= head.end()
        or boundary >= publication.start("year")
    ):
        return None

    title_source = raw[head.end():boundary].strip()
    if not title_source.endswith("."):
        return None
    title = title_source[:-1].strip()
    if not title:
        return None
    quote_pairs = (("“", "”"), ('"', '"'), ("‘", "’"), ("«", "»"))
    if any(
        title.startswith(opening) != title.endswith(closing)
        for opening, closing in quote_pairs
    ):
        return None
    if re.search(r"[.!?][\"”’)]?\s+[\"“‘(]*[A-Z]", title):
        return None

    qualifier = re.fullmatch(
        r"\s*(?P<container>[A-ZÀ-Þ][^\r\n;]{0,119}?"
        r"\((?P<qualifier_year>(?:1[5-9]|20)\d{2})\))\.\s*",
        raw[boundary:publication.start("year")],
    )
    if qualifier is None:
        return None
    container_value = qualifier.group("container")
    years = list(re.finditer(r"\b(?:1[5-9]|20)\d{2}\b", container_value))
    if (
        len(years) != 1
        or years[0].group(0) != qualifier.group("qualifier_year")
        or not container_value.endswith(f"({qualifier.group('qualifier_year')})")
        or container_value.count("(") != 1
        or container_value.count(")") != 1
        or len(container_value.split()) < 2
        or len(container_value.split()) > 16
        or overlaps_identifier((
            boundary + qualifier.start("container"),
            boundary + qualifier.end("container"),
        ))
    ):
        return None
    container_name = container_value[:-(len(qualifier.group("qualifier_year")) + 2)].rstrip()
    if not container_name or not re.search(r"[^\W\d_]", container_name):
        return None
    if (
        re.search(r"[.!?][\"”’)]?\s+[A-ZÀ-Þ]", container_name)
        and _ABBREVIATED_CONTAINER_TAIL.fullmatch(container_name) is None
    ):
        return None

    start = boundary + qualifier.start("container")
    end = boundary + qualifier.end("container")
    return _field("container", raw, start, end, rule)


def _container_before(
    raw: str,
    coordinate_start: int,
    rule: str,
    cited_title: str | None,
) -> dict | None:
    source_prefix = raw[:coordinate_start]
    prefix = source_prefix.rstrip(" ,;.")
    if not prefix:
        return None
    compact_container = _compact_initial_author_container_before(
        raw, coordinate_start, rule, cited_title,
    )
    if compact_container is not None:
        return compact_container
    trim = " \t\r\n,.;:!?\"'“”‘’()"

    def candidate(boundary: int, *, allow_camelcase: bool = False) -> tuple[int, str] | None:
        value_start = boundary
        while value_start < len(prefix) and prefix[value_start] in trim:
            value_start += 1
        value = prefix[value_start:].strip(" \t\r\n,;.")
        leading_year = re.match(r"\(?\b(?:19|20)\d{2}\b\)?\s*,?\s*", value)
        if leading_year:
            value_start += leading_year.end()
            value = value[leading_year.end():]
        year_suffix = re.search(r"\s*[.(]?\b(?:19|20)\d{2}\b\)?\s*$", value)
        if year_suffix:
            value = value[:year_suffix.start()].rstrip(" ,;.")
        value_start = prefix.find(value, value_start) if value else -1
        if value_start < 0 or not value or len(value) < 2:
            return None
        if re.match(r"(?:no\.?\s*)?\d+\s*:", value, re.I):
            return None
        # These are report/prose labels immediately before their own numbers,
        # not serial containers. The generic ``volume:locator`` shape must not
        # turn them into journal coordinates.
        if re.fullmatch(
            r"(?:this\s+is|(?:technical\s+|working\s+)?report\s+(?:no|number)\.?)",
            value,
            re.I,
        ):
            return None
        if re.search(r"\b(?:19|20)\d{2}\b", value):
            return None
        if value[0].islower() and not (
            (allow_camelcase and _CAMELCASE_CONTAINER_TAIL.fullmatch(value))
            or _lowercase_prefix_abbreviated_container_tail(value)
        ):
            return None
        if len(value.split()) > 16 or re.search(r"\b(?:supra|infra)\s+note\b", value, re.I):
            return None
        return value_start, value

    boundaries: list[int] = []
    title_boundary = _title_boundary(prefix, cited_title)
    try:
        from core.parse.parsing_common import _collective_personal_author_head
    except ImportError:  # direct execution fallback
        from parsing_common import _collective_personal_author_head
    collective_head = _collective_personal_author_head(source_prefix)
    if collective_head is not None:
        author_end, _first_family = collective_head
        if _strict_abbreviated_container_tail(raw[author_end:coordinate_start]):
            boundaries.append(author_end)
    if title_boundary is None:
        # A coauthor's initial and surname can also look like a journal
        # abbreviation. Prefer the complete shared author head when the
        # remaining prefix is itself a closed venue-shaped tail.
        try:
            from core.parse.parsing_common import (
                _COMMA_INITIAL_AUTHOR_HEAD_RE, _COMMA_INITIAL_GIVEN_AUTHOR,
            )
        except ImportError:  # direct execution fallback
            from parsing_common import (
                _COMMA_INITIAL_AUTHOR_HEAD_RE, _COMMA_INITIAL_GIVEN_AUTHOR,
            )
        author_head = (
            _COMMA_INITIAL_AUTHOR_HEAD_RE.match(prefix)
            if re.match(r"\s*" + _COMMA_INITIAL_GIVEN_AUTHOR, prefix)
            else None
        )
        if author_head is not None:
            author_tail = prefix[author_head.end():].strip(trim)
            if (
                author_tail
                and not re.search(r"[,;]", author_tail)
                and _container_suffix_is_closed(author_tail)
            ):
                boundaries.append(author_head.end())
    if not cited_title and title_boundary is None:
        author_boundary = _initialed_author_container_boundary(prefix)
        if author_boundary is not None:
            boundaries.append(author_boundary)
        author_boundary = _surname_first_author_container_boundary(prefix)
        if author_boundary is not None:
            boundaries.append(author_boundary)
        else:
            author_boundary = _strict_surname_first_author_container_boundary(source_prefix)
            if author_boundary is not None:
                boundaries.append(author_boundary)
    strong = []
    for match in re.finditer(r"([A-Za-zÀ-ÿ]+|\d+)[.!?]\s+", prefix):
        word = match.group(1)
        if word.isdigit() or word[:1].islower() or len(word) > 5:
            strong.append(match.end())
    lowercase_boundaries: set[int] = set()
    if title_boundary is not None:
        # A title-aware boundary can safely keep a compact capitalized journal
        # abbreviation chain together (for example ``J. Exp. Methods`` or
        # ``Astron. Astrophys.``). Requiring a compact, terminal chain avoids
        # applying this to ordinary prose after a truncated title.
        title_tail = prefix[title_boundary:].strip(" \t\r\n,.;:!?\"'“”‘’()")
        if (
            _ABBREVIATED_CONTAINER_TAIL.fullmatch(title_tail)
            or _lowercase_prefix_abbreviated_container_tail(title_tail)
        ):
            boundaries.append(title_boundary)
        elif _CAMELCASE_CONTAINER_TAIL.fullmatch(title_tail):
            boundaries.append(title_boundary)
            lowercase_boundaries.add(title_boundary)
        # A parsed title may stop before a subtitle. When the remaining
        # terminal segment is itself a compact journal abbreviation chain,
        # start the container at that segment rather than retaining the
        # subtitle as part of the venue.
        title_remainder = prefix[title_boundary:]
        for separator in re.finditer(r"[.!?]\s+", title_remainder):
            suffix_start = title_boundary + separator.end()
            intervening = prefix[title_boundary:suffix_start].strip(
                " \t\r\n,.;:!?\"'“”‘’()"
            )
            # Do not split inside a venue whose own abbreviated words end in
            # periods (for example ``Proc. Natl Acad. Sci. USA``). A subtitle
            # boundary is usable only when the intervening sentence is a clean
            # phrase without internal sentence punctuation.
            if not intervening or re.search(r"[.!?]", intervening):
                continue
            suffix = prefix[suffix_start:].strip(" \t\r\n,.;:!?\"'“”‘’()")
            if (
                _ABBREVIATED_CONTAINER_TAIL.fullmatch(suffix)
                or _lowercase_prefix_abbreviated_container_tail(suffix)
            ):
                boundaries.append(suffix_start)
                break
        # The extracted title can end before a subtitle or even be an author
        # fragment. A later full sentence ending remains stronger evidence
        # than that truncated title; short journal abbreviations do not count.
        boundaries.extend(reversed([
            boundary for boundary in strong if boundary > title_boundary
        ]))
        boundaries.append(title_boundary)
    boundaries.extend(reversed(strong))
    boundaries.append(0)
    strong_boundaries = set(strong)
    seen: set[int] = set()
    for boundary in boundaries:
        if boundary in seen:
            continue
        seen.add(boundary)
        if boundary in strong_boundaries:
            qualified_container = _closed_colon_parenthesized_container_before(
                raw, coordinate_start, rule, boundary,
            )
            if qualified_container is not None:
                return qualified_container
        found = candidate(
            boundary,
            allow_camelcase=(
                boundary in lowercase_boundaries
            ),
        )
        if found:
            value_start, value = found
            return _field("container", raw, value_start, value_start + len(value), rule)
    return None


def _repository_url(raw: str) -> dict | None:
    """Read a complete arXiv/SSRN URL, including a wrapped path."""
    match = re.search(r"https?://(?:www\.)?(?:arxiv\.org|ssrn\.com)/", raw, re.I)
    if not match:
        return None
    spans = [(match.start(), match.end())]
    cursor = match.end()
    token = re.match(r"[^\s)\],;]+", raw[cursor:])
    if token:
        end = cursor + token.end()
        while end > cursor and raw[end - 1] == ".":
            end -= 1
        if end > cursor:
            spans.append((cursor, end))
    else:
        gap = re.match(r"\s+", raw[cursor:])
        next_cursor = cursor + gap.end() if gap else cursor
        continuation = re.match(r"(?:abs/|pdf/|abstract_id=)\S+", raw[next_cursor:], re.I)
        if continuation:
            end = next_cursor + continuation.end()
            while end > next_cursor and raw[end - 1] == ".":
                end -= 1
            spans.append((next_cursor, end))
    return _field(
        "repository", raw, spans[0][0], spans[-1][1], "repository_host", spans=spans,
    )


def _initialed_author_container_boundary(prefix: str) -> int | None:
    """Find a closed initialized author list followed by a comma boundary."""
    initialed_name = re.compile(
        r"(?:[A-Z]\.\s*){1,3}[A-Z][^\W\d_][\w'’\-]*"
    )
    author_separator = re.compile(r"\s*(?:,|\s+(?:and|&)\s+)\s*", re.I)
    first = initialed_name.match(prefix)
    if first is None:
        return None
    cursor = first.end()
    while True:
        separator = author_separator.match(prefix, cursor)
        if separator is None:
            return None
        next_name = initialed_name.match(prefix, separator.end())
        if next_name is None:
            return separator.end() if "," in separator.group(0) else None
        if re.fullmatch(r"\s*,\s*", separator.group(0)):
            # A comma can introduce another initialed author or the title.
            following_separator = author_separator.match(prefix, next_name.end())
            if following_separator is None:
                return separator.end()
        cursor = next_name.end()


def _emit_article_match(
    out: list[dict], raw: str, match: re.Match, rule: str, cited_title: str | None,
) -> None:
    if re.search(r"\barts?\.?\s*$", raw[:match.start()], re.I):
        return
    present = [
        name for name in ("volume", "issue", "locator")
        if name in match.groupdict() and match.groupdict().get(name) is not None
    ]
    first_coordinate = (
        match.start() if rule == "explicit_vol_issue_pages"
        else min(match.start(name) for name in present)
    )
    container = _container_before(raw, first_coordinate, rule, cited_title)
    if rule == "volume_colon_single" and not cited_title:
        # The final comma bounds the complete abbreviated venue; punctuation
        # inside that venue must not select only its trailing fragment.
        terminal_container = _terminal_abbreviated_container_before(
            raw, first_coordinate, rule,
        )
        if terminal_container is not None:
            container = terminal_container
    elif container is None and rule == "volume_single_locator_year":
        container = _terminal_abbreviated_container_before(
            raw, first_coordinate, rule,
        )
    if container is None:
        return
    _append_unique(out, container)
    for group, kind in (("volume", "volume"), ("issue", "issue")):
        if match.groupdict().get(group) is not None:
            start, end = match.span(group)
            _append_unique(out, _field(kind, raw, start, end, rule))
    locator = match.groupdict().get("locator")
    if locator is not None:
        start, end = match.span("locator")
        if _uniform_prefixed_locator_range_parts(locator) is not None:
            kind = "article_locator"
        elif locator[:1].isalpha():
            kind = "elocator"
        elif re.search(rf"[{_DASHES}]", locator):
            kind = "article_page_range"
        elif rule == "volume_issue_article_number":
            # ``volume(issue):number`` is ambiguous: the terminal number can
            # be either an article number or a starting page.  Keep the fact
            # without claiming either interpretation.
            kind = "article_locator"
        elif rule in {
            "year_volume_locator", "volume_single_locator_year", "volume_colon_single",
        }:
            kind = (
                "article_number"
                if locator.startswith("0") or len(locator) >= 6
                else "article_locator"
            )
        else:
            kind = None
        if kind:
            _append_unique(out, _field(kind, raw, start, end, rule))


def _emit_after_issue_container(
    out: list[dict], raw: str, match: re.Match, rule: str,
) -> None:
    """Emit the `(year) volume(issue) Journal locator` family."""
    cstart, cend = match.span("container")
    container = raw[cstart:cend].strip(" \t\r\n,;.")
    cstart = raw.find(container, cstart, cend) if container else -1
    if cstart < 0 or len(container) < 2 or len(container.split()) > 16:
        return
    _append_unique(out, _field("container", raw, cstart, cstart + len(container), rule))
    for group, kind in (("volume", "volume"), ("issue", "issue")):
        start, end = match.span(group)
        _append_unique(out, _field(kind, raw, start, end, rule))
    locator = match.group("locator")
    if _uniform_prefixed_locator_range_parts(locator) is not None:
        start, end = match.span("locator")
        _append_unique(out, _field("article_locator", raw, start, end, rule))
    elif locator[:1].isalpha():
        start, end = match.span("locator")
        _append_unique(out, _field("elocator", raw, start, end, rule))
    elif re.search(rf"[{_DASHES}]", locator):
        start, end = match.span("locator")
        _append_unique(out, _field("article_page_range", raw, start, end, rule))


def _emit_after_year_container(
    out: list[dict], raw: str, match: re.Match, rule: str,
) -> None:
    cstart, cend = match.span("container")
    container = raw[cstart:cend].strip(" \t\r\n,;.")
    cstart = raw.find(container, cstart, cend) if container else -1
    if cstart < 0 or len(container) < 2 or len(container.split()) > 16:
        return
    _append_unique(out, _field("container", raw, cstart, cstart + len(container), rule))
    start, end = match.span("volume")
    _append_unique(out, _field("volume", raw, start, end, rule))
    locator = match.group("locator")
    if _uniform_prefixed_locator_range_parts(locator) is not None:
        start, end = match.span("locator")
        _append_unique(out, _field("article_locator", raw, start, end, rule))
    elif locator[:1].isalpha():
        start, end = match.span("locator")
        _append_unique(out, _field("elocator", raw, start, end, rule))
    elif re.search(rf"[{_DASHES}]", locator):
        start, end = match.span("locator")
        _append_unique(out, _field("article_page_range", raw, start, end, rule))


def _terminal_abbreviated_container_before(
    raw: str, coordinate_start: int, rule: str,
) -> dict | None:
    """Find a compact abbreviated venue after the final comma before a tuple."""
    prefix = raw[:coordinate_start].rstrip(" \t\r\n,")
    separators = list(re.finditer(r",\s*", prefix))
    if not separators:
        return None
    start = separators[-1].end()
    value = prefix[start:].strip(" \t\r\n,;.")
    if not value:
        return None
    if not _ABBREVIATED_CONTAINER_TAIL.fullmatch(value):
        if not _BARE_CONTAINER_INITIALISM.fullmatch(value):
            return None
        source_before_venue = prefix[:separators[-1].start()]
        author_boundary = _initialed_author_container_boundary(source_before_venue)
        if author_boundary is None:
            return None
        title_words = re.findall(r"\w+", source_before_venue[author_boundary:])
        if len(title_words) < 3:
            return None
    value_start = prefix.find(value, start)
    if value_start < 0:
        return None
    end = value_start + len(value)
    return _field("container", raw, value_start, end, rule)


def _title_bounded_container_before(
    raw: str, coordinate_start: int, cited_title: str | None, rule: str,
) -> dict | None:
    """Use a literal title only when it cleanly separates a full venue name."""
    if not cited_title:
        return None
    prefix = raw[:coordinate_start]
    title_boundary = _title_boundary(prefix, cited_title)
    if title_boundary is None:
        return None
    container = _container_before(raw, coordinate_start, rule, cited_title)
    if container is None:
        return None
    if re.match(r"in\b", container["raw_value"], re.I):
        # An explicit chapter container is not a journal publication tuple.
        return None
    span = container["spans"][0]
    start, end = span["raw_start"], span["raw_end"]
    value = container["raw_value"]
    if re.search(r"[,;]", value):
        return None
    if ("." in value or (len(value.split()) == 1 and raw[end:coordinate_start].lstrip().startswith("."))):
        if (
            _ABBREVIATED_CONTAINER_TAIL.fullmatch(value) is None
            and _BARE_CONTAINER_INITIALISM.fullmatch(value) is None
        ):
            return None
    matches = list(re.finditer(re.escape(cited_title), prefix, re.I))
    if not matches:
        return None
    title_match = matches[-1]
    title_end = title_match.end()
    separator = raw[title_end:start]
    plain_separator = re.fullmatch(r"\s*[.,;:]\s*", separator)
    quoted_separator = re.fullmatch(
        r"\s*[.,;:]?\s*(?P<closing>[\"”])\s*", separator,
    )
    balanced_quoted_separator = False
    if quoted_separator:
        opening = '“' if quoted_separator.group("closing") == "”" else '"'
        balanced_quoted_separator = raw.rfind(opening, 0, title_match.start()) >= 0
    if title_end > start or not (plain_separator or balanced_quoted_separator):
        return None
    if not re.fullmatch(r"\s*[,;.]?\s*", raw[end:coordinate_start]):
        return None
    return container


def _emit_parenthesized_year_tuple(
    out: list[dict], raw: str, match: re.Match, cited_title: str | None,
) -> bool:
    """Emit coordinates for ``venue volume (year) locator`` before source IDs."""
    rule = "volume_parenthesized_year_locator"
    volume_start, volume_end = match.span("volume")
    container = _terminal_abbreviated_container_before(raw, volume_start, rule)
    if container is None:
        container = _title_bounded_container_before(
            raw, volume_start, cited_title, rule,
        )
    if container is None:
        return False
    _append_unique(out, container)
    _append_unique(out, _field("volume", raw, volume_start, volume_end, rule))

    locator = match.group("locator")
    start, end = match.span("locator")
    if _uniform_prefixed_locator_range_parts(locator) is not None:
        kind = "article_locator"
    elif locator[:1].isalpha():
        kind = "elocator"
    elif re.search(rf"[{_DASHES}]", locator):
        kind = "article_page_range"
    else:
        # A bare number does not establish whether the publisher uses pages or
        # an article number. Preserve the printed value without choosing.
        kind = "article_locator"
    _append_unique(out, _field(kind, raw, start, end, rule))
    return True


def _emit_legal_reporter(out: list[dict], raw: str) -> bool:
    """Read a legal journal/reporter coordinate without treating pinpoints as pages."""
    pattern = re.compile(
        rf"(?<![{_DASHES}\d.])(?<!\d,)\b(?P<volume>\d{{1,3}})\s+"
        r"(?P<container>[A-Z][A-Za-zÀ-ÿ0-9.’'-]*"
        r"(?:\s+(?:[A-Z][A-Za-zÀ-ÿ0-9.’'-]*|&|of|on|the|and)){0,9})\s+"
        r"\d{1,5}(?=\s*(?:,|\[|\((?:19|20)\d{2}\)))"
    )
    hints = re.compile(
        r"(?:\b(?:Law|Journal|Review|Reports?|Transactions)\b|"
        r"\b(?:L\.?\s*J\.?|Rev\.?|Soc[’']?y|L\.?N\.?T\.?S\.?|"
        r"Pol[’']?y|Internet|Tech\.?|Affs\.?|Org\.?|L\.?Q\.?|Recs\.?)\b)",
        re.I,
    )
    for match in pattern.finditer(raw):
        container = match.group("container")
        if container.casefold() in {"no", "no."}:
            continue
        dotted_token = re.search(r"(?:^|\s)[A-Z][A-Za-z’']{0,5}\.", container)
        if not hints.search(container) and dotted_token is None:
            continue
        for group, kind in (("container", "container"), ("volume", "volume")):
            start, end = match.span(group)
            _append_unique(out, _field(kind, raw, start, end, "legal_reporter"))
        return True
    return False


def _emit_attached_venue_volume_page_range_year(
    out: list[dict], raw: str, cited_title: str | None,
) -> bool:
    """Read an attached venue-volume only from one closed author/title tuple."""
    if not isinstance(cited_title, str) or not cited_title.strip():
        return False
    match = _ATTACHED_VENUE_VOLUME_PAGE_RANGE_YEAR_RE.search(raw)
    if match is None:
        return False

    try:
        from core.parse.parsing_common import _COMMA_INITIAL_AUTHOR_HEAD_RE
    except ImportError:  # direct execution fallback
        from parsing_common import _COMMA_INITIAL_AUTHOR_HEAD_RE
    author_head = _COMMA_INITIAL_AUTHOR_HEAD_RE.match(raw)
    if author_head is None:
        return False

    title_tokens = re.split(r"\s+", cited_title.strip())
    title_pattern = r"(?<!\w)" + r"\s+".join(
        re.escape(token) for token in title_tokens
    ) + r"(?!\w)"
    title_matches = list(re.finditer(
        title_pattern, raw[author_head.end():match.start("volume")], re.I,
    ))
    if len(title_matches) != 1:
        return False

    rule = "attached_venue_volume_page_range_year"
    container = _title_bounded_container_before(
        raw, match.start("volume"), cited_title, rule,
    )
    if container is None:
        return False
    spans = container.get("spans")
    if (
        not isinstance(spans, list) or len(spans) != 1
        or not isinstance(spans[0], dict)
        or spans[0].get("raw_end") != match.start("volume")
        or not isinstance(container.get("raw_value"), str)
        or not container["raw_value"].strip()
    ):
        return False

    _append_unique(out, container)
    for group, kind in (
        ("volume", "volume"), ("pages", "article_page_range"),
    ):
        start, end = match.span(group)
        _append_unique(out, _field(kind, raw, start, end, rule))
    return True


def extract_cited_coordinates(raw: str, cited_title: str | None = None) -> list[dict]:
    """Return high-precision coordinate facts from one bibliography entry."""
    if not isinstance(raw, str) or not raw:
        return []
    out: list[dict] = []

    arxiv = re.search(r"\barXiv(?:\s+preprint)?\s*:\s*\d{4}\.\d{4,5}(?:v\d+)?\b", raw, re.I)
    if arxiv:
        _append_unique(out, _field("repository", raw, arxiv.start(), arxiv.end(), "explicit_arxiv"))
    ssrn = re.search(r"\bSSRN\b(?:\s+(?:working\s+paper|abstract))?", raw, re.I)
    if ssrn:
        _append_unique(out, _field("repository", raw, ssrn.start(), ssrn.end(), "explicit_ssrn"))
    _append_unique(out, _repository_url(raw))

    if _emit_legal_reporter(out, raw):
        return out

    # Some publisher citations put the volume and year after the journal, then
    # append repository/catalogue annotations (for example arXiv + INSPIRE).
    # Keep this path closed at the entry tail so unrelated numbers in a title
    # cannot be promoted to journal coordinates.
    parenthesized_year_match = _PARENTHESIZED_YEAR_TAIL_RE.search(raw)
    if parenthesized_year_match and _emit_parenthesized_year_tuple(
        out, raw, parenthesized_year_match, cited_title,
    ):
        return out

    after_issue = re.search(
        rf"\((?:19|20)\d{{2}}\)\s*(?P<volume>\d{{1,4}})"
        rf"\((?P<issue>[A-Za-z0-9/]+)\)\s+"
        rf"(?P<container>[A-Z][^.;]{{1,120}}?)\s*,?\s*"
        rf"(?P<locator>{_LOCATOR}|\d{{1,7}})(?=\s*[,.;])",
        raw,
    )
    if after_issue:
        _emit_after_issue_container(out, raw, after_issue, "year_volume_issue_container")
        return out

    after_year = re.search(
        rf"\((?:19|20)\d{{2}}\)\s*(?P<volume>\d{{1,4}})\s+"
        rf"(?P<container>[A-Z][^.;]{{1,120}}?)\s*,?\s*"
        rf"(?P<locator>{_LOCATOR}|\d{{1,7}})(?=\s*[,.;])",
        raw,
    )
    if after_year:
        _emit_after_year_container(out, raw, after_year, "year_volume_container")
        return out

    repeated_volume_issue_pages = _REPEATED_VOLUME_COLON_ISSUE_PAGES_RE.search(raw)
    if repeated_volume_issue_pages:
        before = len(out)
        _emit_article_match(
            out, raw, repeated_volume_issue_pages,
            "repeated_volume_colon_issue_pages", cited_title,
        )
        if len(out) > before:
            return out

    if _emit_attached_venue_volume_page_range_year(out, raw, cited_title):
        return out

    patterns: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "explicit_vol_issue_pages",
            re.compile(
                rf"\bvol(?:ume)?\.?\s*(?P<volume>[A-Za-z0-9/]+)\s*,?\s*"
                rf"(?:no\.?\s*(?P<issue>[A-Za-z0-9/]+)\s*,?\s*)?"
                rf"pp?\.?\s*(?P<locator>{_LOCATOR})\b", re.I,
            ),
        ),
        (
            "chicago_no_locator",
            re.compile(
                rf"\b(?P<volume>\d+)\s*,?\s+no\.?\s*(?P<issue>{_ISSUE})"
                rf"(?:\s*\((?:19|20)\d{{2}}\))?\s*:\s*(?P<locator>{_LOCATOR})\b",
                re.I,
            ),
        ),
        (
            "volume_issue_locator",
            re.compile(
                rf"\b(?P<volume>\d+)\((?P<issue>{_ISSUE})\)\s*[,;:]\s*"
                rf"(?P<locator>{_LOCATOR})\b", re.I,
            ),
        ),
        (
            "volume_issue_article_number",
            re.compile(
                rf"\b(?P<volume>\d+)\((?P<issue>{_ISSUE})\)\s*[,;:]\s*"
                r"(?P<locator>\d{1,9})\b", re.I,
            ),
        ),
        (
            "volume_locator",
            re.compile(
                rf"\b(?P<volume>\d{{1,4}})\s*[,;:]\s*(?P<locator>{_LOCATOR})\b",
                re.I,
            ),
        ),
        (
            "volume_colon_single",
            re.compile(
                r"\b(?P<volume>\d{1,4})\s*:\s*(?P<locator>\d{1,9})\b",
                re.I,
            ),
        ),
        (
            "year_volume_locator",
            re.compile(
                rf"\b(?:19|20)\d{{2}}\s*,\s*(?P<volume>\d{{1,4}})\s*,\s*"
                rf"(?:No\.\s*)?(?P<locator>{_LOCATOR}|\d+)\b", re.I,
            ),
        ),
        (
            "volume_single_locator_year",
            re.compile(
                r"\b(?P<volume>\d{1,4})\s*,\s*(?P<locator>\d{2,9})\s*"
                r"\((?:19|20)\d{2}\)", re.I,
            ),
        ),
    )
    matched = False
    for rule, pattern in patterns:
        match = pattern.search(raw)
        if match:
            before = len(out)
            _emit_article_match(out, raw, match, rule, cited_title)
            matched = len(out) > before
            if matched:
                break

    if not matched and isinstance(cited_title, str) and cited_title:
        # Author-year styles can close with ``Journal volume, number.``. A
        # bare scalar has no page/article-number type, so retain it as a generic
        # locator only when a unique literal title bounds a separate container
        # and one unambiguous publication year occurs outside that title.
        terminal = re.search(
            r"\b(?P<volume>\d{1,4})\s*,\s*(?P<locator>\d{1,9})\s*\.?\s*$",
            raw,
        )
        if terminal is not None:
            prefix = raw[:terminal.start("volume")]
            titles = list(re.finditer(re.escape(cited_title), prefix, re.I))
            if len(titles) == 1:
                title_span = titles[0].span()
                source_years = [
                    year
                    for year in re.finditer(r"\b(?:19|20)\d{2}\b", prefix)
                    if not title_span[0] <= year.start() < title_span[1]
                ]
                if len(source_years) == 1:
                    rule = "title_bounded_terminal_scalar"
                    container = _title_bounded_container_before(
                        raw, terminal.start("volume"), cited_title, rule,
                    )
                    if container is not None:
                        _append_unique(out, container)
                        for kind, group in (("volume", "volume"), ("article_locator", "locator")):
                            start, end = terminal.span(group)
                            _append_unique(out, _field(kind, raw, start, end, rule))
                        matched = True

    if not matched:
        match = re.search(
            rf"\b(?P<volume>\d+)\((?P<issue>{_ISSUE})\)", raw,
        )
        if match:
            _emit_article_match(out, raw, match, "volume_issue_only", cited_title)

    proceeding = re.search(
        rf"\bIn\s+(?P<container>[^.;]{{3,180}}?),\s+pages?\s+"
        rf"(?P<pages>\d+\s*[{_DASHES}]\s*\d+)\b",
        raw,
        re.I,
    )
    if proceeding and "container" not in {row["kind"] for row in out}:
        cstart, cend = proceeding.span("container")
        pstart, pend = proceeding.span("pages")
        _append_unique(out, _field("container", raw, cstart, cend, "proceedings_pages"))
        _append_unique(out, _field("chapter_page_range", raw, pstart, pend, "proceedings_pages"))

    article_number = re.search(
        r"\bArticle\s+(?:number\s*|No\.?\s*)(?P<number>[A-Za-z]?\d[\w.-]*)\b",
        raw,
        re.I,
    )
    if article_number:
        start, end = article_number.span("number")
        _append_unique(out, _field("article_number", raw, start, end, "explicit_article_number"))

    return out


def _has_closed_parenthesized_journal_tuple(
    raw: str, coordinates: list[dict], year: object, *,
    allow_generic_scalar: bool = False,
) -> bool:
    """Validate one closed, source-spanned journal coordinate tuple."""
    if not isinstance(raw, str) or not raw or type(year) is not int:
        return False
    if not isinstance(coordinates, list):
        return False
    tuple_matches = [
        ("parenthesized", match)
        for match in _PARENTHESIZED_YEAR_TAIL_RE.finditer(raw)
    ] + [
        ("post_locator", match)
        for match in _POST_LOCATOR_YEAR_RE.finditer(raw)
    ]
    if len(tuple_matches) != 1:
        return False
    tuple_style, tuple_match = tuple_matches[0]
    if int(tuple_match.group("year")) != year:
        return False

    tuple_kinds = {
        "container", "volume", "article_page_range", "article_locator",
        "article_number", "elocator",
    }
    selected = [
        row for row in coordinates
        if isinstance(row, dict)
        and row.get("kind") in tuple_kinds
    ]
    expected_rule = (
        "volume_parenthesized_year_locator"
        if tuple_style == "parenthesized" else "volume_single_locator_year"
    )
    if any(row.get("rule_id") != expected_rule for row in selected):
        return False
    by_kind: dict[str, list[dict]] = {}
    for row in selected:
        by_kind.setdefault(row["kind"], []).append(row)
    locator_kinds = (
        "article_page_range", "article_locator", "article_number", "elocator",
    )
    if (
        len(by_kind.get("container", [])) != 1
        or len(by_kind.get("volume", [])) != 1
        or sum(len(by_kind.get(kind, [])) for kind in locator_kinds) != 1
    ):
        return False

    container = by_kind["container"][0]
    volume = by_kind["volume"][0]
    locator = next(by_kind[kind][0] for kind in locator_kinds if by_kind.get(kind))

    def literal_span(row: dict) -> tuple[int, int] | None:
        spans = row.get("spans")
        if not isinstance(spans, list) or len(spans) != 1 or not isinstance(spans[0], dict):
            return None
        start, end = spans[0].get("raw_start"), spans[0].get("raw_end")
        if (
            not isinstance(start, int) or isinstance(start, bool)
            or not isinstance(end, int) or isinstance(end, bool)
            or not 0 <= start < end <= len(raw)
            or raw[start:end] != row.get("raw_value")
            or not isinstance(row.get("normalized_value"), str)
            or _normal(raw[start:end]) != row.get("normalized_value")
        ):
            return None
        return start, end

    container_span = literal_span(container)
    volume_span = literal_span(volume)
    locator_span = literal_span(locator)
    if not container_span or not volume_span or not locator_span:
        return False
    container_text = container["raw_value"]
    if not isinstance(container_text, str) or not container_text.strip():
        return False
    if re.match(r"\s*in\b", container_text, re.I):
        return False
    if not re.fullmatch(r"\d{1,4}", volume["raw_value"]):
        return False
    locator_kind = locator["kind"]
    locator_value = locator["raw_value"]
    if tuple_style == "post_locator" and locator_kind != "article_number":
        if not (allow_generic_scalar and locator_kind == "article_locator"):
            return False
    if locator_kind == "article_locator":
        locator_is_valid = isinstance(locator_value, str) and (
            re.fullmatch(r"\d{1,9}", locator_value) is not None
            or _uniform_prefixed_locator_range_parts(locator_value) is not None
        )
    else:
        locator_pattern = {
            "article_page_range": rf"\d+\s*[{_DASHES}]\s*\d+",
            "article_number": r"\d{1,9}",
            "elocator": r"[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*",
        }.get(locator_kind)
        locator_is_valid = (
            isinstance(locator_value, str)
            and locator_pattern is not None
            and re.fullmatch(locator_pattern, locator_value) is not None
        )
    if not locator_is_valid:
        return False

    if (
        not container_span[1] <= volume_span[0] < volume_span[1] <= locator_span[0]
        or volume_span != tuple_match.span("volume")
        or locator_span != tuple_match.span("locator")
        or not re.fullmatch(
            r"\s*[.,;:]?\s*", raw[container_span[1]:volume_span[0]],
        )
    ):
        return False
    if re.search(r"\bin\s*:\s*$", raw[:container_span[0]], re.I):
        return False

    year_start, year_end = tuple_match.span("year")
    if tuple_style == "parenthesized":
        if not (
            re.fullmatch(r"\s*\(\s*", raw[volume_span[1]:year_start])
            and re.fullmatch(r"\s*\)\s*:?\s*", raw[year_end:locator_span[0]])
        ):
            return False
    elif not (
        re.fullmatch(r"\s*,\s*", raw[volume_span[1]:locator_span[0]])
        and re.fullmatch(r"\s*\(\s*", raw[locator_span[1]:year_start])
        and re.fullmatch(r"\s*\)", raw[year_end:tuple_match.end()])
    ):
        return False

    suffix = list(raw[tuple_match.end():])
    for item in coordinates:
        if not isinstance(item, dict) or item.get("kind") != "repository":
            continue
        spans = item.get("spans")
        if not isinstance(spans, list) or not spans:
            return False
        span_values: list[str] = []
        parsed_spans: list[tuple[int, int]] = []
        for span in spans:
            if not isinstance(span, dict):
                return False
            start, end = span.get("raw_start"), span.get("raw_end")
            if (
                not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or not 0 <= start < end <= len(raw)
            ):
                return False
            parsed_spans.append((start, end))
            span_values.append(raw[start:end])
        if "".join(span_values) != item.get("raw_value"):
            return False
        for start, end in parsed_spans:
            if end <= tuple_match.end():
                continue
            if start < tuple_match.end():
                return False
            suffix[start - tuple_match.end():end - tuple_match.end()] = " " * (end - start)

    suffix_text = "".join(suffix)
    suffix_text = re.sub(r"\[[A-Za-z0-9][A-Za-z0-9:./_-]{0,79}\]", "", suffix_text)
    return bool(re.fullmatch(r"[\s\[\](){}.,;:]*", suffix_text))


def _has_closed_pre_year_colon_journal_tuple(
    raw: str, coordinates: list[dict], year: object,
) -> bool:
    """Validate ``author, year, abbreviated journal, volume:scalar`` as closed."""
    if not isinstance(raw, str) or not raw or type(year) is not int:
        return False
    if not isinstance(coordinates, list) or len(coordinates) != 3:
        return False
    by_kind: dict[str, list[dict]] = {}
    for row in coordinates:
        if not isinstance(row, dict):
            return False
        by_kind.setdefault(row.get("kind"), []).append(row)
    if set(by_kind) != {"container", "volume", "article_locator"}:
        return False
    container = by_kind["container"][0]
    volume = by_kind["volume"][0]
    locator = by_kind["article_locator"][0]
    if any(row.get("rule_id") != "volume_colon_single" for row in coordinates):
        return False
    container_span = _literal_coordinate_span(raw, container)
    volume_span = _literal_coordinate_span(raw, volume)
    locator_span = _literal_coordinate_span(raw, locator)
    if not container_span or not volume_span or not locator_span:
        return False
    container_text = container.get("raw_value")
    if (
        not isinstance(container_text, str)
        or not container_text.strip()
        or re.search(r"[,;]", container_text)
        or re.match(r"\s*in\b", container_text, re.I)
        or not _container_suffix_is_closed(container_text)
        or not re.fullmatch(r"\d{1,4}", volume.get("raw_value", ""))
        or not re.fullmatch(r"\d{1,9}", locator.get("raw_value", ""))
    ):
        return False
    matches = list(re.finditer(
        r"\b(?P<volume>\d{1,4})\s*:\s*(?P<locator>\d{1,9})\b", raw,
    ))
    years = list(re.finditer(r"\b(?P<year>(?:19|20)\d{2})\b", raw))
    if (
        len(matches) != 1 or len(years) != 1
        or int(years[0].group("year")) != year
        or not container_span[1] <= volume_span[0] < volume_span[1] <= locator_span[0]
        or volume_span != matches[0].span("volume")
        or locator_span != matches[0].span("locator")
        or years[0].end() > container_span[0]
        or not re.fullmatch(r"\s*[,;:]\s*", raw[container_span[1]:volume_span[0]])
        or not re.fullmatch(r"\s*:\s*", raw[volume_span[1]:locator_span[0]])
        or not re.fullmatch(r"\s*[.]?\s*", raw[locator_span[1]:])
    ):
        return False
    return True


def _has_closed_issue_scalar_postyear_tuple(
    raw: str, coordinates: list[dict], year: object,
) -> bool:
    """Validate a source-spanned ``volume(issue), scalar (year)`` journal tuple."""
    if not isinstance(raw, str) or not raw or type(year) is not int:
        return False
    if not isinstance(coordinates, list) or len(coordinates) != 4:
        return False
    by_kind: dict[str, list[dict]] = {}
    for row in coordinates:
        if not isinstance(row, dict):
            return False
        by_kind.setdefault(row.get("kind"), []).append(row)
    if set(by_kind) != {"container", "volume", "issue", "article_locator"}:
        return False
    container = by_kind["container"][0]
    volume = by_kind["volume"][0]
    issue = by_kind["issue"][0]
    locator = by_kind["article_locator"][0]
    if any(row.get("rule_id") != "volume_issue_article_number" for row in coordinates):
        return False
    container_span = _literal_coordinate_span(raw, container)
    volume_span = _literal_coordinate_span(raw, volume)
    issue_span = _literal_coordinate_span(raw, issue)
    locator_span = _literal_coordinate_span(raw, locator)
    if not container_span or not volume_span or not issue_span or not locator_span:
        return False
    container_text = container.get("raw_value")
    if (
        not isinstance(container_text, str)
        or not container_text.strip()
        or re.search(r"[,;]", container_text)
        or re.match(r"\s*in\b", container_text, re.I)
        or not _container_suffix_is_closed(container_text)
        or not re.fullmatch(r"\d{1,4}", volume.get("raw_value", ""))
        or not re.fullmatch(r"[A-Za-z0-9/]+", issue.get("raw_value", ""))
        or not re.fullmatch(r"\d{1,9}", locator.get("raw_value", ""))
    ):
        return False
    pattern = re.compile(
        r"(?P<volume>\d{1,4})\s*\(\s*(?P<issue>[A-Za-z0-9/]+)\s*\)"
        r"\s*[,;:]\s*(?P<locator>\d{1,9})\s*\(\s*"
        r"(?P<year>(?:19|20)\d{2})\s*\)\s*[.]?\s*$"
    )
    matches = list(pattern.finditer(raw))
    years = list(re.finditer(r"\b(?P<year>(?:19|20)\d{2})\b", raw))
    if len(matches) != 1 or len(years) != 1:
        return False
    match = matches[0]
    if (
        int(match.group("year")) != year
        or int(years[0].group("year")) != year
        or not container_span[1] <= volume_span[0] < volume_span[1] <= issue_span[0]
        or not issue_span[1] <= locator_span[0] < locator_span[1]
        or volume_span != match.span("volume")
        or issue_span != match.span("issue")
        or locator_span != match.span("locator")
        or not re.fullmatch(r"\s*[.,;:]?\s*", raw[container_span[1]:volume_span[0]])
        or not re.fullmatch(r"\s*\(\s*", raw[volume_span[1]:issue_span[0]])
        or not re.fullmatch(r"\s*\)\s*[,;:]\s*", raw[issue_span[1]:locator_span[0]])
        or not re.fullmatch(r"\s*\(\s*", raw[locator_span[1]:years[0].start()])
        or not re.fullmatch(r"\s*\)\s*[.]?\s*", raw[years[0].end():])
    ):
        return False
    return True


def _has_source_spanned_title_bounded_terminal_scalar(
    raw: str, title: str | None, coordinates: list[dict] | None = None,
) -> bool:
    """Validate the exact title/container/volume/generic-scalar extraction."""
    if not isinstance(raw, str) or not raw or not isinstance(title, str) or not title.strip():
        return False
    expected = extract_cited_coordinates(raw, title)
    if len(expected) != 3:
        return False
    by_kind = {row.get("kind"): row for row in expected if isinstance(row, dict)}
    if set(by_kind) != {"container", "volume", "article_locator"}:
        return False
    if any(row.get("rule_id") != "title_bounded_terminal_scalar" for row in expected):
        return False
    if coordinates is not None and coordinates != expected:
        return False
    spans = {kind: _literal_coordinate_span(raw, row) for kind, row in by_kind.items()}
    if any(span is None for span in spans.values()):
        return False
    container_span = spans["container"]
    volume_span = spans["volume"]
    locator_span = spans["article_locator"]
    if container_span is None or volume_span is None or locator_span is None:
        return False
    title_matches = list(re.finditer(re.escape(title), raw, re.I))
    return bool(
        len(title_matches) == 1
        and title_matches[0].end() <= container_span[0]
        and container_span[1] <= volume_span[0] < volume_span[1] <= locator_span[0] < locator_span[1]
        and re.fullmatch(r"\s*[.;:]?\s*", raw[title_matches[0].end():container_span[0]])
        and re.fullmatch(r"\s*[.,;:]?\s*", raw[container_span[1]:volume_span[0]])
        and re.fullmatch(r"\s*,\s*", raw[volume_span[1]:locator_span[0]])
        and re.fullmatch(r"\d{1,4}", by_kind["volume"].get("raw_value", ""))
        and re.fullmatch(r"\d{1,9}", by_kind["article_locator"].get("raw_value", ""))
        and re.fullmatch(r"\s*[.]?\s*", raw[locator_span[1]:])
    )
