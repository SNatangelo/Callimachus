#!/usr/bin/env python3
# core/resolve/providers/arxiv.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""arXiv resolver + fetch provider."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
import urllib.error
import urllib.parse
from xml.etree import ElementTree as ET

NAME = "arxiv"
RESOLVE_NAME = "arxiv_search"
PREPRINT_RESOLVER = True
MANIFEST = {
    "doi_prefixes": ["10.48550/arxiv."],
    "preprint_host": True,
    "canonical_hosts": ["arxiv.org", "export.arxiv.org"],
    "host_markers": ["arxiv"],
}
_PUBLISHED_RELATION_QUERY_CONTRACT = "arxiv-id-list-crossref-journal-unit/v1"
_PUBLISHED_RELATION_CERTIFICATE = "crossref:arxiv_published_journal_unit_certificate"
_ARXIV_ID_VALUE = r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?"


class _CandidateItems(list):
    """Private carrier for one issued title-lookup observation.

    ``candidate_items`` remains a list API.  The registry consumes these
    attributes only while it builds the per-provider trace row.
    """

    def __init__(self, items=(), *, attempt: dict | None = None, error: dict | None = None):
        super().__init__(items)
        self._fetch_execution_attempts = [attempt] if attempt else []
        self._provider_error = error


def _identity_title(value: object) -> str:
    """Normalise only the historically split ``self-attention`` spelling.

    This is intentionally not a general title normaliser: joining arbitrary
    hyphenated words makes distinct works look alike.  arXiv itself contains
    both spellings of this one established term.
    """
    text = str(value or "").casefold()
    text = re.sub(r"\bself[\s-]*attention\b", "selfattention", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _first_author(ref: dict) -> str | None:
    for key in ("ay_surname", "surname", "first_author_surname"):
        value = re.sub(r"[^a-z]", "", str(ref.get(key) or "").casefold())
        if len(value) >= 2:
            return value
    raw = str(ref.get("raw_entry") or "")
    title = str(ref.get("title") or "")
    if title:
        match = re.search(re.escape(title), raw, re.IGNORECASE)
        if match:
            raw = raw[:match.start()]
    first = re.split(r",|\s+(?:and|&)\s+", raw, maxsplit=1)[0]
    tokens = re.findall(r"[a-z]+", first.casefold())
    if tokens[-2:] == ["et", "al"]:
        tokens = tokens[:-2]
    if tokens:
        return tokens[-1]
    return None


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return (
        not ref.get("url")
        and resolve_mod._article_like_resolution_candidate(ref)
    )


def _entry_authors(entry, ns: dict[str, str]) -> list[str]:
    return [
        (node.findtext("atom:name", default="", namespaces=ns) or "").strip()
        for node in entry.findall("atom:author", ns)
    ]


def _text_sha256(value: bytes | str) -> str:
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _name_tokens(value: object) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.findall(r"[^\W\d_]+", text, re.UNICODE)


def _source_first_author(raw: str, container_start: int) -> dict | None:
    """Read one initialed first-author name before the source container span."""
    prefix = raw[:container_start]
    first = prefix.split(",", 1)[0].strip()
    initial_first = re.fullmatch(
        r"(?P<initials>(?:[A-ZÀ-Þ]\.(?:\s*-\s*[A-ZÀ-Þ]\.)?)"
        r"(?:\s*[A-ZÀ-Þ]\.){0,3})\s+(?P<family>[^,;.!?]+?)\s*",
        first,
        re.UNICODE,
    )
    if initial_first is not None:
        initials_text = initial_first.group("initials")
        family_text = initial_first.group("family").strip(" .")
    else:
        # Surname-first bibliographies put the initial sequence after the first
        # comma. Accept only a closed initialized head; uninitialized names do
        # not provide enough source evidence for this relation certificate.
        surname_first = re.match(
            r"\s*(?P<family>[^,;.!?]+?)\s*,\s*"
            r"(?P<initials>(?:[A-ZÀ-Þ](?:\s*-\s*[A-ZÀ-Þ])?\.?)"
            r"(?:\s+[A-ZÀ-Þ](?:\s*-\s*[A-ZÀ-Þ])?\.?) {0,3})"
            r"(?=\s*(?:[,;]|\band\b|&|$))",
            prefix,
            re.UNICODE | re.IGNORECASE,
        )
        if surname_first is None:
            return None
        initials_text = surname_first.group("initials")
        family_text = surname_first.group("family").strip(" .")
    family_tokens = _name_tokens(family_text)
    initials = "".join(token[0] for token in _name_tokens(initials_text) if token)
    if not family_tokens or not initials:
        return None
    return {
        "family": family_text,
        "family_tokens": family_tokens,
        "initials": initials,
        "literal": first,
    }


def _native_name_matches_source(name: object, author: dict) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    family_tokens = author.get("family_tokens")
    if not isinstance(family_tokens, list) or not family_tokens:
        return False
    if "," in name:
        family_text, given_text = name.split(",", 1)
        native_family = _name_tokens(family_text)
        given_tokens = _name_tokens(given_text)
    else:
        tokens = _name_tokens(name)
        if len(tokens) <= len(family_tokens):
            return False
        native_family = tokens[-len(family_tokens):]
        given_tokens = tokens[:-len(family_tokens)]
    return bool(
        native_family == family_tokens
        and given_tokens
        and "".join(token[0] for token in given_tokens)
        == author.get("initials")
    )


def _source_published_relation_unit(ref: dict) -> dict | None:
    """Rebuild one complete, closed, source-spanned journal unit and arXiv ID."""
    raw = ref.get("raw_entry")
    if (
        not isinstance(raw, str) or not raw
        or ref.get("source_type") != "article"
        or ref.get("source_kind") != "article_like"
        or ref.get("source_type_confidence") != "high"
        or type(ref.get("year")) is not int
    ):
        return None
    if ref.get("pmid") or ref.get("isbn"):
        return None
    literal_dois = list(re.finditer(r"\b10\.\d{4,9}/[^\s\]}>]+", raw, re.I))
    if literal_dois:
        return None

    try:
        from core.parse.citation_coordinates import (
            _has_closed_parenthesized_journal_tuple,
            _initialed_author_container_boundary,
            _literal_coordinate_span,
            _surname_first_author_list_end,
            extract_cited_coordinates,
        )
    except ImportError:  # pragma: no cover - direct execution fallback
        from parse.citation_coordinates import (
            _has_closed_parenthesized_journal_tuple,
            _initialed_author_container_boundary,
            _literal_coordinate_span,
            _surname_first_author_list_end,
            extract_cited_coordinates,
        )

    title = ref.get("title")
    fresh = extract_cited_coordinates(raw, title if isinstance(title, str) else None)
    coordinates = ref.get("cited_coordinates")
    if not isinstance(coordinates, list):
        return None
    relevant_kinds = {
        "repository", "container", "volume", "issue", "article_page_range",
        "article_locator", "article_number", "elocator",
    }
    if any(
        [row for row in coordinates if isinstance(row, dict) and row.get("kind") == kind]
        != [row for row in fresh if isinstance(row, dict) and row.get("kind") == kind]
        for kind in relevant_kinds
    ):
        return None

    repository_rows = [
        row for row in coordinates
        if isinstance(row, dict) and row.get("kind") == "repository"
    ]
    if len(repository_rows) != 1:
        return None
    repository = repository_rows[0]
    repository_span = _literal_coordinate_span(raw, repository)
    if repository_span is None:
        return None
    id_match = re.fullmatch(
        rf"arXiv(?:\s+preprint)?\s*:\s*(?P<id>{_ARXIV_ID_VALUE})",
        str(repository.get("raw_value") or ""),
        re.I,
    )
    all_ids = list(re.finditer(rf"\barXiv(?:\s+preprint)?\s*:\s*({_ARXIV_ID_VALUE})\b", raw, re.I))
    if id_match is None or len(all_ids) != 1:
        return None
    arxiv_id = id_match.group("id")
    id_span = (
        repository_span[0] + id_match.start("id"),
        repository_span[0] + id_match.end("id"),
    )
    if raw[id_span[0]:id_span[1]] != arxiv_id:
        return None
    expected_preprint_doi = f"10.48550/arxiv.{arxiv_id}".casefold()
    cited_doi = str(ref.get("doi") or "").strip()
    if cited_doi and cited_doi.casefold() != expected_preprint_doi:
        return None

    if not _has_closed_parenthesized_journal_tuple(
        raw, coordinates, ref["year"], allow_generic_scalar=True,
    ):
        return None
    by_kind: dict[str, list[dict]] = {}
    for row in coordinates:
        if isinstance(row, dict) and row.get("kind") in relevant_kinds:
            by_kind.setdefault(row["kind"], []).append(row)
    locator_kinds = ("article_page_range", "article_locator", "article_number", "elocator")
    if (
        len(by_kind.get("container", [])) != 1
        or len(by_kind.get("volume", [])) != 1
        or len(by_kind.get("issue", [])) > 1
        or sum(len(by_kind.get(kind, [])) for kind in locator_kinds) != 1
    ):
        return None
    container = by_kind["container"][0]
    volume = by_kind["volume"][0]
    locator_kind = next(kind for kind in locator_kinds if by_kind.get(kind))
    locator = by_kind[locator_kind][0]
    container_span = _literal_coordinate_span(raw, container)
    volume_span = _literal_coordinate_span(raw, volume)
    locator_span = _literal_coordinate_span(raw, locator)
    if not container_span or not volume_span or not locator_span:
        return None

    title_span = None
    if isinstance(title, str) and title:
        title_matches = list(re.finditer(re.escape(title), raw))
        if len(title_matches) != 1:
            return None
        title_span = title_matches[0].span()
        if (
            title_span[0] >= container_span[0]
            or title_span[1] > container_span[0]
            or not re.fullmatch(r"\s*[.,;:]\s*", raw[title_span[1]:container_span[0]])
        ):
            return None
    else:
        prefix = raw[:container_span[0]]
        boundaries = {
            boundary
            for boundary in (
                _initialed_author_container_boundary(prefix),
                _surname_first_author_list_end(prefix),
            )
            if isinstance(boundary, int)
            and re.fullmatch(r"\s*[,;.]?\s*", prefix[boundary:])
        }
        if len(boundaries) != 1:
            return None

    author = _source_first_author(raw, container_span[0])
    if author is None:
        return None
    issue = by_kind.get("issue", [None])[0]
    issue_span = _literal_coordinate_span(raw, issue) if issue else None
    if issue is not None and issue_span is None:
        return None
    return {
        "raw_sha256": _text_sha256(raw),
        "arxiv_id": arxiv_id,
        "repository_raw_value": repository["raw_value"],
        "repository_span": list(repository_span),
        "arxiv_id_span": list(id_span),
        "year": ref["year"],
        "title": title if isinstance(title, str) else None,
        "title_span": list(title_span) if title_span else None,
        "author": {
            "family": author["family"],
            "family_tokens": author["family_tokens"],
            "initials": author["initials"],
            "literal": author["literal"],
        },
        "container": container["raw_value"],
        "container_span": list(container_span),
        "volume": volume["raw_value"],
        "volume_span": list(volume_span),
        "issue": issue["raw_value"] if issue else None,
        "issue_span": list(issue_span) if issue_span else None,
        "locator_kind": locator_kind,
        "locator": locator["raw_value"],
        "locator_span": list(locator_span),
    }


def _atom_id_matches(source_id: str, entry_id: object) -> bool:
    if not isinstance(entry_id, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(entry_id.strip())
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or (parsed.hostname or "").casefold() not in {"arxiv.org", "export.arxiv.org"}
        or parsed.query or parsed.fragment
    ):
        return False
    match = re.fullmatch(r"/abs/([^/]+)", parsed.path)
    if match is None:
        return False
    observed = match.group(1)
    requested_version = re.search(r"v\d+$", source_id, re.I)
    if requested_version:
        return observed.casefold() == source_id.casefold()
    return re.sub(r"v\d+$", "", observed, flags=re.I).casefold() == source_id.casefold()


def _coordinate_key(kind: str, value: object) -> str:
    try:
        from core.resolve.matching import _coordinate_comparison_value
    except ImportError:  # pragma: no cover - direct execution fallback
        from resolve.matching import _coordinate_comparison_value
    return _coordinate_comparison_value(kind, str(value or ""))


def _closed_journal_ref_matches(source: dict, journal_ref: object) -> bool:
    if not isinstance(journal_ref, str) or not journal_ref.strip():
        return False
    try:
        from core.parse.citation_coordinates import (
            _has_closed_parenthesized_journal_tuple,
            extract_cited_coordinates,
        )
    except ImportError:  # pragma: no cover - direct execution fallback
        from parse.citation_coordinates import (
            _has_closed_parenthesized_journal_tuple,
            extract_cited_coordinates,
        )
    coordinates = extract_cited_coordinates(journal_ref)
    if not _has_closed_parenthesized_journal_tuple(
        journal_ref, coordinates, source["year"], allow_generic_scalar=True,
    ):
        return False
    by_kind: dict[str, list[dict]] = {}
    for row in coordinates:
        if isinstance(row, dict):
            by_kind.setdefault(row.get("kind"), []).append(row)
    locator_kinds = ("article_page_range", "article_locator", "article_number", "elocator")
    if (
        len(by_kind.get("container", [])) != 1
        or len(by_kind.get("volume", [])) != 1
        or sum(len(by_kind.get(kind, [])) for kind in locator_kinds) != 1
    ):
        return False
    observed_locator_kind = next(kind for kind in locator_kinds if by_kind.get(kind))
    return bool(
        _coordinate_key("container", by_kind["container"][0].get("raw_value"))
        == _coordinate_key("container", source["container"])
        and _coordinate_key("volume", by_kind["volume"][0].get("raw_value"))
        == _coordinate_key("volume", source["volume"])
        and _coordinate_key("page", by_kind[observed_locator_kind][0].get("raw_value"))
        == _coordinate_key("page", source["locator"])
        and (
            source.get("issue") is None
            or (
                len(by_kind.get("issue", [])) == 1
                and _coordinate_key("issue", by_kind["issue"][0].get("raw_value"))
                == _coordinate_key("issue", source["issue"])
            )
        )
    )


def _crossref_publication_year(message: dict) -> tuple[int | None, dict | None]:
    values: dict[str, int] = {}
    for field in ("published-print", "published-online", "published", "issued"):
        item = message.get(field)
        if item is None:
            continue
        if not isinstance(item, dict):
            return None, None
        parts = item.get("date-parts")
        if not isinstance(parts, list):
            return None, None
        if not parts:
            continue
        if not isinstance(parts[0], list) or not parts[0]:
            return None, None
        year = parts[0][0]
        if isinstance(year, bool) or not re.fullmatch(r"(?:19|20)\d{2}", str(year)):
            return None, None
        values[field] = int(year)
    if not values or len(set(values.values())) != 1:
        return None, None
    for preferred in ("published-print", "published-online", "published", "issued"):
        if preferred in values:
            return values[preferred], values
    return None, None


def _crossref_observation(message: dict, request_url: str, response_sha256: str) -> dict | None:
    authors = message.get("author")
    if not isinstance(authors, list):
        return None
    authors_observed = []
    for author in authors:
        if not isinstance(author, dict):
            return None
        authors_observed.append({
            "family": author.get("family"),
            "given": author.get("given"),
            "sequence": author.get("sequence"),
        })
    year, year_fields = _crossref_publication_year(message)
    if year is None or year_fields is None:
        return None
    titles = message.get("title")
    containers = message.get("container-title")
    short_containers = message.get("short-container-title") or []
    if not isinstance(titles, list) or not isinstance(containers, list) or not isinstance(short_containers, list):
        return None
    return {
        "request_url": request_url,
        "response_sha256": response_sha256,
        "doi": message.get("DOI"),
        "type": message.get("type"),
        "titles": titles,
        "authors": authors_observed,
        "year": year,
        "year_fields": year_fields,
        "containers": containers,
        "short_containers": short_containers,
        "volume": message.get("volume"),
        "issue": message.get("issue"),
        "page": message.get("page"),
        "article_number": message.get("article-number"),
    }


def _crossref_observation_matches(source: dict, atom: dict, observation: dict) -> tuple[bool, str, dict | None]:
    if not isinstance(observation, dict):
        return False, "missing direct Crossref observation", None
    try:
        from core.resolve.providers.crossref_metadata import _coordinate_venue_key
    except ImportError:  # pragma: no cover - direct execution fallback
        from resolve.providers.crossref_metadata import _coordinate_venue_key
    expected_url = "https://api.crossref.org/works/" + urllib.parse.quote(
        str(atom.get("doi") or ""), safe="",
    )
    if (
        observation.get("request_url") != expected_url
        or not re.fullmatch(r"[a-f0-9]{64}", str(observation.get("response_sha256") or ""))
        or str(observation.get("type") or "").casefold() != "journal-article"
        or str(observation.get("doi") or "").casefold() != str(atom.get("doi") or "").casefold()
        or observation.get("year") != source.get("year")
    ):
        return False, "Crossref DOI, type, or publication year conflicts with the source unit", None
    year_fields = observation.get("year_fields")
    if (
        not isinstance(year_fields, dict) or not year_fields
        or any(value != source.get("year") for value in year_fields.values())
    ):
        return False, "Crossref publication dates conflict with the cited year", None
    titles = observation.get("titles")
    if (
        not isinstance(titles, list) or len(titles) != 1
        or not isinstance(titles[0], str) or not titles[0].strip()
    ):
        return False, "Crossref direct record has no unique native title", None
    authors = observation.get("authors")
    if (
        not isinstance(authors, list) or not authors
        or any(not isinstance(author, dict) for author in authors)
    ):
        return False, "Crossref direct record has no first author", None
    sequenced = [a for a in authors if a.get("sequence") is not None]
    if sequenced:
        first_authors = [a for a in sequenced if str(a.get("sequence")).casefold() == "first"]
        if len(first_authors) != 1:
            return False, "Crossref direct record has an ambiguous first author", None
        first_author = first_authors[0]
    else:
        first_author = authors[0]
    if (
        not isinstance(first_author.get("family"), str)
        or not isinstance(first_author.get("given"), str)
        or not _native_name_matches_source(
            f"{first_author['given']} {first_author['family']}", source["author"],
        )
    ):
        return False, "Crossref first author conflicts with the literal author", None

    containers = observation.get("containers")
    short_containers = observation.get("short_containers")
    if (
        not isinstance(containers, list) or len(containers) != 1
        or not isinstance(containers[0], str) or not containers[0].strip()
        or not isinstance(short_containers, list)
        or any(not isinstance(item, str) for item in short_containers)
    ):
        return False, "Crossref direct record has an ambiguous journal container", None
    source_venue_key = _coordinate_venue_key(source["container"])
    if _coordinate_venue_key(containers[0]) == source_venue_key:
        matched_venue = containers[0]
    else:
        venue_matches = [
            item for item in short_containers
            if item.strip() and _coordinate_venue_key(item) == source_venue_key
        ]
        if len(venue_matches) != 1:
            return False, "Crossref primary/short journal container conflicts with the source", None
        matched_venue = venue_matches[0]
    if (
        not isinstance(observation.get("volume"), str)
        or _coordinate_key("volume", observation["volume"])
        != _coordinate_key("volume", source["volume"])
    ):
        return False, "Crossref volume conflicts with the source", None
    if source.get("issue") is not None and (
        not isinstance(observation.get("issue"), str)
        or _coordinate_key("issue", observation["issue"])
        != _coordinate_key("issue", source["issue"])
    ):
        return False, "Crossref issue conflicts with the source", None

    native_locators = []
    for field, value in (("article-number", observation.get("article_number")), ("page", observation.get("page"))):
        if isinstance(value, str) and value.strip():
            native_locators.append((field, value))
    locator_kind = source.get("locator_kind")
    source_locator = source.get("locator")
    if locator_kind == "article_number":
        matched_locator = ("article-number", observation.get("article_number"))
    elif locator_kind == "article_page_range":
        matched_locator = ("page", observation.get("page"))
    elif len(native_locators) == 1:
        matched_locator = native_locators[0]
    else:
        return False, "Crossref direct record has no unique comparable locator", None
    if (
        len(native_locators) != 1
        or not isinstance(matched_locator[1], str)
        or _coordinate_key("page", matched_locator[1]) != _coordinate_key("page", source_locator)
    ):
        return False, "Crossref locator conflicts with the source", None
    comparisons = [
        {"kind": "container", "cited_value": source["container"], "matched_value": matched_venue, "status": "match"},
        {"kind": "volume", "cited_value": source["volume"], "matched_value": observation["volume"], "status": "match"},
    ]
    if source.get("issue") is not None:
        comparisons.append({"kind": "issue", "cited_value": source["issue"], "matched_value": observation["issue"], "status": "match"})
    comparisons.append({
        "kind": locator_kind,
        "cited_value": source_locator,
        "matched_value": matched_locator[1],
        "matched_field": matched_locator[0],
        "status": "match",
    })
    return True, "", {
        "title": titles[0],
        "first_author": first_author,
        "year": observation["year"],
        "primary_container": containers[0],
        "matched_container_alias": matched_venue,
        "volume": observation["volume"],
        "issue": observation.get("issue"),
        "locator_field": matched_locator[0],
        "locator": matched_locator[1],
        "coordinate_comparisons": comparisons,
    }


def _atom_observation_matches(source: dict, observation: dict) -> tuple[bool, str]:
    if not isinstance(observation, dict):
        return False, "missing native arXiv Atom relation"
    expected_query = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"id_list": source["arxiv_id"]},
    )
    if (
        observation.get("request_url") != expected_query
        or observation.get("entry_count") != 1
        or observation.get("doi_count") != 1
        or observation.get("journal_ref_count") != 1
        or not isinstance(observation.get("author_count"), int)
        or observation.get("author_count", 0) < 1
        or not re.fullmatch(r"[a-f0-9]{64}", str(observation.get("response_sha256") or ""))
        or not _atom_id_matches(source["arxiv_id"], observation.get("entry_id"))
    ):
        return False, "native arXiv response does not uniquely match the literal ID"
    doi = str(observation.get("doi") or "").strip()
    if not re.fullmatch(r"10\.\d{4,9}/[^\s]+", doi, re.I):
        return False, "native arXiv entry has no unique published DOI"
    if not _native_name_matches_source(observation.get("first_author"), source["author"]):
        return False, "native arXiv first author conflicts with the literal author"
    if not _closed_journal_ref_matches(source, observation.get("journal_ref")):
        return False, "native arXiv journal_ref conflicts with the source journal unit"
    return True, ""


def _published_relation_proof_is_valid(
    ref: dict, result: dict | None, attempts: list[dict] | None = None,
) -> bool:
    """Recheck the private source-ID → Atom relation → Crossref unit proof."""
    if not isinstance(ref, dict) or not isinstance(result, dict):
        return False
    resolved_identifier = result.get("resolved_identifier")
    metadata = result.get("metadata_match")
    proof = metadata.get("arxiv_published_relation_certificate") if isinstance(metadata, dict) else None
    if (
        result.get("status") != "resolved"
        or result.get("via") != "crossref_metadata"
        or result.get("resolution_basis") != "metadata_search"
        or not isinstance(resolved_identifier, dict)
        or resolved_identifier.get("type") != "doi"
        or resolved_identifier.get("validated_via") != _PUBLISHED_RELATION_CERTIFICATE
        or not isinstance(proof, dict)
        or proof.get("marker") != _PUBLISHED_RELATION_CERTIFICATE
        or not isinstance(result.get("doi"), str)
        or result.get("doi", "").casefold() != str(resolved_identifier.get("value") or "").casefold()
    ):
        return False
    source = _source_published_relation_unit(ref)
    if source is None or proof.get("source") != source:
        return False
    atom = proof.get("atom")
    crossref = proof.get("crossref")
    atom_ok, _atom_reason = _atom_observation_matches(source, atom)
    if not atom_ok:
        return False
    crossref_ok, _crossref_reason, compared = _crossref_observation_matches(source, atom, crossref)
    if not crossref_ok or compared is None:
        return False
    try:
        from core.resolve import service as resolve_mod
    except ImportError:  # pragma: no cover - direct execution fallback
        import resolve as resolve_mod
    title_overlap = resolve_mod.title_overlap(compared["title"], ref.get("raw_entry"))
    if (
        str(atom.get("doi") or "").casefold() != str(crossref.get("doi") or "").casefold()
        or result.get("matched_title") != compared["title"]
        or result.get("matched_authors") != [compared["first_author"]["family"]]
        or result.get("matched_year") != source["year"]
        or result.get("work_type") != crossref.get("type")
        or not isinstance(metadata, dict)
        or metadata.get("ordinal_conflict") is not False
        or metadata.get("metadata_conflict") is not False
        or result.get("ordinal_conflict") is True
        or result.get("metadata_conflict") is True
        or metadata.get("title_overlap") != title_overlap
        or metadata.get("matched_year") != source["year"]
        or metadata.get("matched_venue") != compared["primary_container"]
        or metadata.get("matched_first_author") != compared["first_author"].get("family")
        or metadata.get("cited_first_author") != source["author"]["family"]
        or metadata.get("author_match") is not True
        or metadata.get("year_match") is not True
        or metadata.get("coordinate_comparisons") != compared["coordinate_comparisons"]
        or str(resolved_identifier.get("value") or "").casefold() != str(crossref.get("doi") or "").casefold()
    ):
        return False
    identity_search = result.get("identity_search")
    if (
        not isinstance(identity_search, dict)
        or identity_search.get("query_contract") != _PUBLISHED_RELATION_QUERY_CONTRACT
        or identity_search.get("outcome") != "compatible_identity"
    ):
        return False
    matching_attempt = any(
        isinstance(attempt, dict)
        and attempt.get("via") == "crossref_metadata"
        and isinstance(attempt.get("metadata_match"), dict)
        and attempt["metadata_match"].get("arxiv_published_relation_certificate") == proof
        and attempt.get("resolved_identifier") == resolved_identifier
        for attempt in (attempts or ())
    )
    return matching_attempt


def _published_relation_failure(
    source: dict, reason: str, *, outcome: str = "candidate_incompatible",
    completion: str = "complete", atom: dict | None = None,
    crossref: dict | None = None,
) -> dict:
    audit = {
        "resolver": "arxiv",
        "query_contract": _PUBLISHED_RELATION_QUERY_CONTRACT,
        "completion": completion,
        "outcome": outcome,
        "reason": reason,
        "source": source,
    }
    if atom is not None:
        audit["atom"] = atom
    if crossref is not None:
        audit["crossref"] = crossref
    return {
        "status": "unverified" if completion == "complete" else "unresolved",
        "via": RESOLVE_NAME,
        "reason": reason,
        "resolution_basis": "metadata_search",
        "existence_confidence": "low",
        "identity_search": audit,
    }


def _discover_published_relation(ref: dict, source: dict, resolve_mod) -> dict:
    atom_url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"id_list": source["arxiv_id"]},
    )
    try:
        atom_status, atom_body = resolve_mod._get(atom_url, accept="application/atom+xml")
    except Exception as exc:
        return _published_relation_failure(
            source, f"arXiv id_list lookup network: {type(exc).__name__}",
            outcome="inconclusive", completion="incomplete",
        )
    if not isinstance(atom_body, (bytes, str)):
        return _published_relation_failure(
            source, "arXiv id_list lookup returned no decodable response body",
            outcome="inconclusive", completion="incomplete",
        )
    atom_bytes = atom_body if isinstance(atom_body, bytes) else atom_body.encode("utf-8")
    atom_sha = _text_sha256(atom_bytes)
    if atom_status != 200:
        return _published_relation_failure(
            source, f"arXiv id_list lookup HTTP {atom_status}",
            outcome="inconclusive", completion="incomplete",
        )
    try:
        root = ET.fromstring(atom_bytes)
    except ET.ParseError:
        return _published_relation_failure(
            source, "arXiv id_list response was malformed XML", outcome="inconclusive",
        )
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    entries = root.findall("atom:entry", ns)
    if len(entries) != 1:
        return _published_relation_failure(
            source, "arXiv id_list response did not contain exactly one entry",
        )
    entry = entries[0]
    entry_ids = entry.findall("atom:id", ns)
    doi_nodes = entry.findall("arxiv:doi", ns)
    journal_nodes = entry.findall("arxiv:journal_ref", ns)
    authors = _entry_authors(entry, ns)
    atom = {
        "request_url": atom_url,
        "response_sha256": atom_sha,
        "entry_count": len(entries),
        "entry_id": (entry_ids[0].text or "").strip() if len(entry_ids) == 1 else None,
        "doi_count": len(doi_nodes),
        "doi": (doi_nodes[0].text or "").strip() if len(doi_nodes) == 1 else None,
        "journal_ref_count": len(journal_nodes),
        "journal_ref": (journal_nodes[0].text or "").strip() if len(journal_nodes) == 1 else None,
        "first_author": authors[0] if authors else None,
        "author_count": len(authors),
    }
    atom_ok, atom_reason = _atom_observation_matches(source, atom)
    if not atom_ok:
        return _published_relation_failure(source, atom_reason, atom=atom)
    crossref_url = "https://api.crossref.org/works/" + urllib.parse.quote(atom["doi"], safe="")
    try:
        crossref_status, crossref_body = resolve_mod._get(crossref_url)
    except Exception as exc:
        return _published_relation_failure(
            source, f"Crossref direct lookup network: {type(exc).__name__}",
            outcome="inconclusive", completion="incomplete", atom=atom,
        )
    if not isinstance(crossref_body, (bytes, str)):
        return _published_relation_failure(
            source, "Crossref direct lookup returned no decodable response body",
            outcome="inconclusive", completion="incomplete", atom=atom,
        )
    crossref_bytes = crossref_body if isinstance(crossref_body, bytes) else crossref_body.encode("utf-8")
    crossref_sha = _text_sha256(crossref_bytes)
    if crossref_status != 200:
        return _published_relation_failure(
            source, f"Crossref direct lookup HTTP {crossref_status}",
            outcome="inconclusive", completion="incomplete", atom=atom,
        )
    try:
        payload = json.loads(crossref_bytes)
    except (TypeError, ValueError):
        return _published_relation_failure(
            source, "Crossref direct response was malformed JSON", atom=atom,
        )
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        return _published_relation_failure(
            source, "Crossref direct response has no work record", atom=atom,
        )
    crossref = _crossref_observation(message, crossref_url, crossref_sha)
    if crossref is None:
        return _published_relation_failure(
            source, "Crossref direct work record lacks a complete publication observation",
            atom=atom,
        )
    crossref_ok, crossref_reason, compared = _crossref_observation_matches(source, atom, crossref)
    if not crossref_ok or compared is None:
        return _published_relation_failure(
            source, crossref_reason, atom=atom, crossref=crossref,
        )

    proof = {
        "marker": _PUBLISHED_RELATION_CERTIFICATE,
        "source": source,
        "atom": atom,
        "crossref": crossref,
    }
    title_overlap = resolve_mod.title_overlap(compared["title"], ref.get("raw_entry"))
    metadata_match = {
        "title_overlap": title_overlap,
        "matched_year": compared["year"],
        "matched_venue": compared["primary_container"],
        "matched_first_author": compared["first_author"]["family"],
        "cited_first_author": source["author"]["family"],
        "author_match": True,
        "year_match": True,
        "ordinal_conflict": False,
        "metadata_conflict": False,
        "coordinate_comparisons": compared["coordinate_comparisons"],
        "arxiv_published_relation_certificate": proof,
    }
    result = {
        "status": "resolved",
        "via": "crossref_metadata",
        "matched_title": compared["title"],
        "matched_authors": [compared["first_author"]["family"]],
        "matched_year": compared["year"],
        "doi": crossref["doi"],
        "work_type": crossref["type"],
        "reason": "native arXiv published relation and direct Crossref journal unit agree",
        "resolution_basis": "metadata_search",
        "existence_confidence": "high",
        "fulltext_exists": "unknown",
        "oa_status": "unknown",
        "fulltext_links": [],
        "retracted": False,
        "metadata_match": metadata_match,
        "resolved_identifier": {
            "type": "doi",
            "value": crossref["doi"],
            "validated_via": _PUBLISHED_RELATION_CERTIFICATE,
        },
        "identity_search": {
            "resolver": "arxiv",
            "query_contract": _PUBLISHED_RELATION_QUERY_CONTRACT,
            "completion": "complete",
            "outcome": "compatible_identity",
        },
    }
    if not _published_relation_proof_is_valid(ref, result, [result]):
        return _published_relation_failure(
            source, "serialized native relation proof failed its own recheck",
            atom=atom, crossref=crossref,
        )
    return result


def discover(ref: dict) -> dict | None:
    """Title-based search on arXiv API - universal last-resort fallback.

    When Crossref and OpenAlex both come up empty for a paper that is likely on
    arXiv, this search uses arXiv's native API to find it by title. Papers
    cited without a DOI (common in older LaTeX bibliographies) are often
    discoverable here, especially ML/CS papers.

    Tries ``ti:`` (title prefix) first; falls back to ``all:`` (full-text
    keyword) when the title search yields no usable hit.

    Returns a resolve-like dict with ``status``, ``matched_title``, and a DOI
    in the ``10.48550/arXiv.ID`` form when found.
    """
    from core.resolve import service as resolve_mod

    source_unit = _source_published_relation_unit(ref)
    if source_unit is not None:
        return _discover_published_relation(ref, source_unit, resolve_mod)

    title = resolve_mod._article_title_candidate(ref)
    via = RESOLVE_NAME
    if not title:
        return {
            "status": "unverified",
            "via": via,
            "reason": "no usable title for arXiv search",
        }

    raw_entry = resolve_mod._tex_strip_braces(ref.get("raw_entry") or "")
    clean_title = re.sub(r'[{}"\']', "", title[:300])
    cited_author = _first_author(ref)

    def _arxiv_query(query: str) -> tuple[list, bytes | None]:
        params: dict[str, str] = {
            "search_query": query,
            "max_results": "5",
            "sortBy": "relevance",
        }
        url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        try:
            _status, body = resolve_mod._get(url, accept="application/atom+xml")
            return [], body
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                return [{
                    "status": "unresolved",
                    "via": via,
                    "reason": "rate_limited (HTTP 429) - NOT fabrication",
                }], None
            return [{
                "status": "unresolved",
                "via": via,
                "reason": f"HTTP {exc.code}",
            }], None
        except Exception as exc:
            return [{
                "status": "unresolved",
                "via": via,
                "reason": f"network: {type(exc).__name__}",
            }], None

    def _score_entries(body: bytes | None) -> dict | None:
        if body is None:
            return None
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return None
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom",
        }
        entries = root.findall("atom:entry", ns)
        if not entries:
            return None

        ref_title = resolve_mod._article_title_candidate(ref)
        best_overlap = -1.0
        best_entry = None
        best_arxiv_id = None
        for entry in entries:
            entry_title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            entry_title = re.sub(r"\s+", " ", entry_title)
            overlap = resolve_mod._title_match_score(ref_title, entry_title)
            # Apply the selfattention/self-attention equivalence only after
            # the ordinary matcher has failed to reach the resolver threshold.
            if (overlap is None or overlap < 0.30) and _identity_title(ref_title) == _identity_title(entry_title):
                overlap = 1.0
            if overlap == 0.0 and len(resolve_mod._sources._tokens(ref_title)) < resolve_mod.TITLE_MIN_TOKENS:
                raw_overlap = resolve_mod.title_overlap(entry_title, raw_entry)
                if raw_overlap is not None and raw_overlap >= 0.95:
                    overlap = raw_overlap

            # Author metadata is corroborative when both sides provide it;
            # legacy title-only references and sparse Atom records still work.
            entry_authors = _entry_authors(entry, ns)
            if cited_author and entry_authors:
                surnames = {
                    re.sub(r"[^a-z]", "", name.casefold().split()[-1])
                    for name in entry_authors if name.split()
                }
                if cited_author not in surnames:
                    continue
            if overlap is not None and overlap > best_overlap:
                best_overlap = overlap
                best_entry = entry
                full_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
                match = re.search(r"arxiv\.org/abs/([^v]+)", full_id)
                if match:
                    best_arxiv_id = match.group(1)

        if best_entry is None or best_overlap < 0.30:
            return None
        return {
            "entry": best_entry,
            "arxiv_id": best_arxiv_id,
            "overlap": best_overlap,
            "ns": ns,
        }

    early_error, body = _arxiv_query(f'ti:"{clean_title}"')
    if early_error:
        return early_error[0]
    scored = _score_entries(body)

    if scored is None:
        words = [
            word for word in re.findall(r"[a-zA-Z]{4,}", clean_title.lower())
            if word not in resolve_mod._STOP_TITLE
        ]
        if len(words) >= 3:
            all_query = " AND ".join(f"all:{word}" for word in words[:8])
            early_error2, body2 = _arxiv_query(all_query)
            if early_error2:
                return early_error2[0]
            scored = _score_entries(body2)

    if scored is None:
        return {
            "status": "unverified",
            "via": via,
            "reason": "no arXiv match met the confidence threshold",
        }

    best_entry = scored["entry"]
    best_arxiv_id = scored["arxiv_id"]
    best_overlap = scored["overlap"]
    ns = scored["ns"]

    matched_title = (best_entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
    matched_title = re.sub(r"\s+", " ", matched_title)
    doi = f"10.48550/arXiv.{best_arxiv_id}" if best_arxiv_id else None
    abstract = (best_entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()

    published = (best_entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
    matched_year = None
    if published:
        try:
            matched_year = int(published[:4])
        except (ValueError, IndexError):
            pass

    if (
        ref.get("year") is not None
        and matched_year is not None
        and abs(int(ref["year"]) - matched_year) > 2
        and best_overlap < 0.70
    ):
        return {
            "status": "unverified",
            "via": via,
            "matched_title": matched_title,
            "reason": (
                f"year mismatch (cited {ref['year']}, "
                f"arXiv {matched_year}, overlap {best_overlap:.3f})"
            ),
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
        }

    return {
        "status": "resolved",
        "via": via,
        "matched_title": matched_title,
        "abstract": abstract or None,
        "retracted": False,
        "reason": f"arXiv title search match (overlap {best_overlap:.3f})",
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium" if best_overlap >= 0.70 else "low",
        "fulltext_exists": True if doi else False,
        "oa_status": "open",
        "fulltext_links": [
            {
                "url": item["url"],
                "content_type": item["kind"],
                "content_version": item["content_version"],
                "identity_context": item["identity_context"],
            }
            for item in _candidate_pair(best_arxiv_id, identity_context={
                "provider": RESOLVE_NAME,
                "title": matched_title,
                "authors": _entry_authors(best_entry, ns),
                "year": matched_year,
                "identifiers": {"arxiv_id": best_arxiv_id},
                "canonical_host": True,
                "source_confidence": best_overlap,
            })
        ] if doi else [],
        "metadata_match": {
            "title_overlap": best_overlap,
            "matched_year": matched_year,
        },
    }


def pdf_url(doi: str | None) -> str | None:
    if not doi:
        return None
    match = re.match(r"^10\.48550/arxiv\.(.+)$", doi, flags=re.IGNORECASE)
    if not match:
        return None
    return f"https://arxiv.org/pdf/{match.group(1)}.pdf"


def pdf_url_from_doi(doi: str | None, **kwargs) -> str | None:
    return pdf_url(doi)


def _arxiv_id_from_doi(doi: str | None) -> str | None:
    match = re.fullmatch(
        r"10\.48550/arxiv\.((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?)",
        str(doi or "").strip(),
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def _candidate_pair(arxiv_id: str, *, identity_context: dict | None = None) -> list[dict]:
    context = identity_context or {
        "provider": RESOLVE_NAME,
        "identifiers": {"arxiv_id": arxiv_id},
        "canonical_host": True,
    }
    return [
        {
            "method": NAME,
            "url": f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            "kind": "pdf",
            "content_version": "preprint",
            "identity_context": copy.deepcopy(context),
        },
        {
            "method": NAME,
            "url": f"https://arxiv.org/html/{arxiv_id}",
            "kind": "html",
            "content_version": "preprint",
            "official_arxiv_html": True,
            "identity_context": copy.deepcopy(context),
        },
    ]


def candidate_items(ref: dict, **kwargs) -> list[dict]:
    normalize_doi = kwargs.get("normalize_doi") or (lambda x: x)
    arxiv_id = _arxiv_id_from_doi(normalize_doi(ref.get("doi")))
    if arxiv_id:
        return _candidate_pair(arxiv_id)

    # Stage 2 is reached only after the version-of-record candidates failed.
    # Search once and accept only an exact identity-title match, corroborated by
    # author/year whenever arXiv and the citation both expose those fields.
    get_fn = kwargs.get("get_fn")
    title = str(ref.get("title") or "").strip()
    if not callable(get_fn) or len(_identity_title(title).split()) < 4:
        return []
    # Strip characters that would break the arXiv ``ti:"..."`` query. Kept out of
    # the f-string expression because a backslash there is a SyntaxError before
    # Python 3.12 (and the CI matrix still includes 3.11).
    # arXiv's title index does not reliably retrieve the concatenated spelling
    # ``selfattention`` used by some bibliographies.  Query the indexed spelling;
    # the separate exact identity normalizer below still admits only that one
    # documented typography variant.
    query_title = re.sub(
        r"\bself[\s-]*attention\b", "self-attention", title[:300],
        flags=re.IGNORECASE,
    )
    cleaned_title = re.sub(r'[{}"\']', "", query_title)
    query_url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
        "search_query": f'ti:"{cleaned_title}"',
        "max_results": "5",
        "sortBy": "relevance",
    })
    def _result(
        items=(),
        *,
        outcome: str,
        status: int | None = None,
        reason: str | None = None,
        error_type: str | None = None,
        retryable: bool | None = None,
    ) -> _CandidateItems:
        attempt = {
            "method": NAME,
            "url": query_url,
            "kind": "api",
            "stage": "candidate_generation",
            "outcome": outcome,
            "status": status,
            # The provider transport exposes only ``(status, body)``.  Preserve
            # the required response-state keys without inventing redirect or
            # response-header observations.
            "final_url": None,
            "content_type": None,
        }
        if reason:
            attempt["reason"] = reason
        error = None
        if error_type:
            error = {
                "status": "error",
                "error_type": error_type,
                "error_reason": reason,
                "retryable": retryable,
                "http_status": status,
            }
        return _CandidateItems(items, attempt=attempt, error=error)

    try:
        status, body = get_fn(query_url, profile="api", timeout=30)
    except urllib.error.HTTPError as exc:
        return _result(
            outcome="unavailable", status=exc.code, reason=f"HTTP {exc.code}",
            error_type="rate_limit" if exc.code == 429 else "http_error",
            retryable=exc.code == 429 or 500 <= exc.code <= 599,
        )
    except Exception as exc:
        return _result(
            outcome="unavailable", reason=f"network: {type(exc).__name__}",
            error_type="network", retryable=True,
        )
    if not isinstance(status, int) or status < 200 or status >= 300:
        rate_limited = status == 429
        return _result(
            outcome="unavailable", status=status if isinstance(status, int) else None,
            reason=f"HTTP {status}" if isinstance(status, int) else "invalid HTTP status",
            error_type="rate_limit" if rate_limited else "http_error",
            retryable=rate_limited or (isinstance(status, int) and 500 <= status <= 599),
        )
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, TypeError) as exc:
        return _result(
            outcome="error", status=status, reason=f"malformed Atom: {type(exc).__name__}",
            error_type="provider_error", retryable=False,
        )
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    cited_author = _first_author(ref)
    try:
        cited_year = int(ref.get("year")) if ref.get("year") is not None else None
    except (TypeError, ValueError):
        cited_year = None
    for entry in root.findall("atom:entry", ns):
        matched_title = re.sub(
            r"\s+", " ",
            (entry.findtext("atom:title", default="", namespaces=ns) or "").strip(),
        )
        if _identity_title(title) != _identity_title(matched_title):
            continue
        authors = _entry_authors(entry, ns)
        surnames = {
            re.sub(r"[^a-z]", "", name.casefold().split()[-1])
            for name in authors if name.split()
        }
        if cited_author and surnames and cited_author not in surnames:
            continue
        published = entry.findtext("atom:published", default="", namespaces=ns) or ""
        try:
            matched_year = int(published[:4])
        except ValueError:
            matched_year = None
        if cited_year is not None and matched_year is not None and abs(cited_year - matched_year) > 2:
            continue
        entry_id = entry.findtext("atom:id", default="", namespaces=ns) or ""
        match = re.search(r"arxiv\.org/abs/([^v/?#]+)(?:v\d+)?", entry_id, re.IGNORECASE)
        if not match:
            continue
        arxiv_id = match.group(1)
        context = {
                "provider": RESOLVE_NAME,
                "title": matched_title,
                "authors": authors,
                "year": matched_year,
                "identifiers": {"arxiv_id": arxiv_id},
                "canonical_host": True,
                "source_confidence": 0.95,
        }
        items = _candidate_pair(arxiv_id, identity_context=context)
        for item in items:
            item["discovery_reason"] = "lazy exact arXiv title/author/year fallback"
        return _result(items, outcome="success", status=status)
    return _result(outcome="no_match", status=status)
