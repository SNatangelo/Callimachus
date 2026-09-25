# core/parse/citation_coordinates.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Conservative extraction of bibliographic facts asserted by a citation.

Every emitted value is tied to raw-entry spans.  These are citation claims,
never canonical metadata returned by a resolver.
"""

from __future__ import annotations

import re


EXTRACTOR_VERSION = "cited-coordinates/v1"
_DASHES = "-‐‑‒–—−"
_LOCATOR = rf"(?:[A-Za-z]\d{{2,}}|\d+\s*[{_DASHES}]\s*\d+)"
_ISSUE = rf"[A-Za-z0-9/]+(?:\s*[{_DASHES}]\s*[A-Za-z0-9/]+)?"


def _normal(value: str) -> str:
    return " ".join(
        value.replace("‐", "-").replace("‑", "-").replace("‒", "-")
        .replace("–", "-").replace("—", "-").replace("−", "-").split()
    )


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


def _title_boundary(prefix: str, cited_title: str | None) -> int | None:
    boundaries: list[int] = []
    if cited_title:
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


def _container_before(
    raw: str,
    coordinate_start: int,
    rule: str,
    cited_title: str | None,
) -> dict | None:
    prefix = raw[:coordinate_start].rstrip(" ,;.")
    if not prefix:
        return None
    trim = " \t\r\n,.;:\"'“”‘’()"

    def candidate(boundary: int) -> tuple[int, str] | None:
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
        if value[0].islower():
            return None
        if len(value.split()) > 16 or re.search(r"\b(?:supra|infra)\s+note\b", value, re.I):
            return None
        return value_start, value

    boundaries: list[int] = []
    strong = []
    for match in re.finditer(r"([A-Za-zÀ-ÿ]+|\d+)[.!?]\s+", prefix):
        word = match.group(1)
        if word.isdigit() or word[:1].islower() or len(word) > 5:
            strong.append(match.end())
    boundaries.extend(reversed(strong))
    title_boundary = _title_boundary(prefix, cited_title)
    if title_boundary is not None:
        boundaries.append(title_boundary)
    boundaries.append(0)
    seen: set[int] = set()
    for boundary in boundaries:
        if boundary in seen:
            continue
        seen.add(boundary)
        found = candidate(boundary)
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
        if locator[:1].isalpha():
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
        } and (
            locator.startswith("0") or len(locator) >= 6
        ):
            kind = "article_number"
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
    if re.search(rf"[{_DASHES}]", locator):
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
    if re.search(rf"[{_DASHES}]", locator):
        start, end = match.span("locator")
        _append_unique(out, _field("article_page_range", raw, start, end, rule))


def _emit_legal_reporter(out: list[dict], raw: str) -> bool:
    """Read a legal journal/reporter coordinate without treating pinpoints as pages."""
    pattern = re.compile(
        r"\b(?P<volume>\d{1,3})\s+"
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
