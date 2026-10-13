#!/usr/bin/env python3
# core/resolve/providers/crossref_metadata.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Crossref DOI and metadata-search resolver module."""

from __future__ import annotations

from difflib import SequenceMatcher
import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import unicodedata

try:
    from .. import matching as _matching
except ImportError:  # direct execution fallback
    from resolve import matching as _matching


NAME = "crossref_metadata"


class _MalformedCrossrefResponse(ValueError):
    """A successful title-search response did not have a usable envelope."""


MANIFEST = {
    "origin": "crossref",
    "weak_abstract_origin": True,
    "via_aliases": [
        "crossref",
        "doi.org",
        "crossref+doi.org",
        "identifier_validation:crossref",
    ],
}

# This is deliberately separate from title discovery.  It can establish only a
# positive contradiction: a unique, incompatible Crossref work whose published
# page range contains the citation's claimed range.
COORDINATE_OCCUPANCY = {
    "provider": "crossref_coordinate_occupancy",
    "rule_version": "crossref-range-occupancy/v2",
}
JOURNAL_COVERAGE = {"resolver": "crossref", "rule_version": "crossref-journal/v1"}
_TITLELESS_NATIVE_VENUE_COMPOSITION_KEY = "_titleless_native_venue_composition"
_TITLELESS_NATIVE_VENUE_COMPOSITION_RULE = "ordered_native_primary_short_tokens_v1"
_GENERIC_ARTICLE_LOCATOR_CERTIFICATE_KEY = "_crossref_generic_article_locator_certificate"
_GENERIC_ARTICLE_LOCATOR_RULE = "crossref-generic-locator-role/v1"
_GENERIC_ARTICLE_LOCATOR_VALIDATED_VIA = "crossref:generic_article_locator_role_certificate"
_GENERIC_ARTICLE_LOCATOR_SEARCH_RULE = "crossref-complete-generic-locator-query/v1"
_REGISTERED_SOURCE_SCOPE_CERTIFICATE_KEY = "_registered_source_scope_certificate"
_REGISTERED_SOURCE_SCOPE_SEARCH_RULE = "crossref-nlm-issn-source-scope/v2"
_CROSSREF_NATIVE_WORK_PROOF_FIELD = "_crossref_native_work_proof"
_CROSSREF_NATIVE_WORK_PROOF_FIELDS = {
    "version", "requested_doi", "request_url", "http_status",
    "response_body", "response_sha256",
}
_AMBIGUOUS_TITLE_JOURNAL_VALIDATED_VIA = (
    "crossref:ambiguous_title_journal_boundary_certificate"
)
_AMBIGUOUS_TITLE_JOURNAL_RULE = "crossref-ambiguous-title-journal-boundary/v1"
_CROSSREF_NATIVE_ARTICLE_PROOF_FIELDS = _CROSSREF_NATIVE_WORK_PROOF_FIELDS | {
    "source_raw_sha256",
}


_COMPACT_TEX_COMMANDS = (
    "emph",
    "mathit",
    "mathbf",
    "mathrm",
    "textit",
    "textrm",
)


def _compact_citation_title(value: object) -> str | None:
    """Return a closed exact-comparison form for citation-owned titles.

    This is deliberately not a similarity measure: it admits only typography
    differences internal to a title word, such as hyphens and TeX subscript
    markup.  Other punctuation remains a word boundary.  A missing citation
    title never obtains this route.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    compact = unicodedata.normalize("NFKC", value).casefold()
    for command in _COMPACT_TEX_COMMANDS:
        compact = re.sub(
            rf"\\{command}\s*\{{([^{{}}]*)\}}", r"\1", compact,
        )
    compact = re.sub(r"\$\s*_\s*\{?([\w]+)\}?\s*\$", r"_\1", compact)
    compact = re.sub(r"_\s*\{([\w]+)\}", r"_\1", compact)
    compact = compact.replace(r"\_", "_")
    compact = re.sub(r"(?<=\w)[\-‐‑‒–—―](?=\w)", "", compact)
    compact = re.sub(r"(?<=\w)_(?=\w)", "", compact)
    compact = " ".join(re.sub(r"[\W_]+", " ", compact).split())
    return compact or None


def _titles_exactly_equivalent(cited_title: object, matched_title: object) -> bool:
    """Whether two explicit titles differ only in closed typography differences."""
    cited = _compact_citation_title(cited_title)
    matched = _compact_citation_title(matched_title)
    if cited is not None and matched is not None and cited == matched:
        return True
    if not isinstance(matched_title, str):
        return False
    unescaped = html.unescape(matched_title)
    if unescaped == matched_title:
        return False
    # Crossref may preserve escaped presentational wrappers in the title field.
    # Remove only paired outer tags from this closed comparison representation;
    # the original matched_title remains untouched in resolver provenance.
    wrapper = re.fullmatch(
        r"\s*<([A-Za-z][\w:-]*)(?:\s+[^<>]*)?>\s*(.*?)\s*</\1>\s*",
        unescaped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if wrapper is None:
        return False
    matched = _compact_citation_title(wrapper.group(2))
    return cited is not None and matched is not None and cited == matched


def _citation_title_exactly_equivalent(ref: dict, matched_title: object) -> bool:
    """Whether an explicit citation title and Crossref title differ only in typography."""
    return _titles_exactly_equivalent(ref.get("title"), matched_title)


def _crossref_primary_title(msg: object) -> str | None:
    """Return the primary Crossref title when its shape is usable."""
    if not isinstance(msg, dict):
        return None
    titles = msg.get("title")
    if (
        not isinstance(titles, list)
        or not titles
        or not isinstance(titles[0], str)
        or not titles[0].strip()
    ):
        return None
    return titles[0].strip()


def _crossref_complete_title(msg: object) -> str | None:
    """Return a Crossref work title with its optional subtitle composed once."""
    title = _crossref_primary_title(msg)
    if title is None:
        return None
    subtitles = msg.get("subtitle")
    if not isinstance(subtitles, list) or not subtitles or not isinstance(subtitles[0], str):
        return title
    subtitle = subtitles[0].strip()
    if not subtitle:
        return title

    normalized_title = _compact_citation_title(title)
    normalized_subtitle = _compact_citation_title(subtitle)
    if normalized_title and normalized_subtitle and (
        normalized_title == normalized_subtitle
        or f" {normalized_subtitle} " in f" {normalized_title} "
    ):
        return title
    return f"{title}: {subtitle}"


def _crossref_primary_title_match_profile(ref: dict, msg: dict) -> dict | None:
    """Return primary-title evidence only for an exact, independently corroborated citation."""
    from core.resolve import service as resolve_mod

    primary_title = _crossref_primary_title(msg)
    if (
        primary_title is None
        or _crossref_complete_title(msg) == primary_title
        or not _citation_title_exactly_equivalent(ref, primary_title)
    ):
        return None

    primary_profile = resolve_mod._metadata_match_profile(ref, msg, primary_title)
    comparisons = {
        item.get("kind"): item
        for item in primary_profile.get("coordinate_comparisons") or ()
        if isinstance(item, dict)
    }
    has_exact_article_identity = (
        comparisons.get("container", {}).get("status") == "match"
        and comparisons.get("volume", {}).get("status") == "match"
        and any(
            comparisons.get(kind, {}).get("status") == "match"
            for kind in ("article_page_range", "article_number")
        )
    )
    if (
        primary_profile.get("author_match") is True
        and primary_profile.get("year_match") is True
        and has_exact_article_identity
    ):
        return primary_profile
    return None


def _crossref_metadata_match_profile(ref: dict, msg: dict) -> dict:
    """Score the complete Crossref title, with a narrow corroborated primary-title path."""
    from core.resolve import service as resolve_mod

    complete_title = _crossref_complete_title(msg)
    profile = resolve_mod._metadata_match_profile(ref, msg, complete_title)
    if complete_title == _crossref_primary_title(msg):
        selected = profile
    else:
        selected = _crossref_primary_title_match_profile(ref, msg) or profile
    selected.pop(_TITLELESS_NATIVE_VENUE_COMPOSITION_KEY, None)
    witness = _titleless_native_venue_composition_witness(ref, msg)
    if witness is not None:
        selected[_TITLELESS_NATIVE_VENUE_COMPOSITION_KEY] = witness
    return selected


def _unsupported_title_fragment_with_year_conflict(
    ref: dict, msg: dict, profile: dict,
) -> bool:
    """Identify an unsupported Crossref title fragment with a conflicting year.

    Candidate-token coverage is directional. A short candidate title can score
    as a full match merely because its words occur throughout a longer cited
    title, so require independent identity support before admitting that shape.
    """
    from core.resolve import service as resolve_mod

    candidate_title = _crossref_complete_title(msg)
    cited_title = ref.get("title")
    if (
        not isinstance(cited_title, str)
        or not isinstance(candidate_title, str)
        or _citation_title_exactly_equivalent(ref, candidate_title)
        or _crossref_primary_title_match_profile(ref, msg) is not None
    ):
        return False

    cited_tokens = _matching._title_key(cited_title).split()
    candidate_tokens = _matching._title_key(candidate_title).split()
    min_tokens = getattr(resolve_mod, "TITLE_MIN_TOKENS", 3)
    if (
        len(cited_tokens) < min_tokens
        or len(candidate_tokens) < min_tokens
        or len(candidate_tokens) >= len(cited_tokens)
        or not set(candidate_tokens).issubset(set(cited_tokens))
    ):
        return False

    overlap = profile.get("title_overlap")
    if not isinstance(overlap, (int, float)) or overlap < 0.85:
        return False
    if profile.get("author_match") is True:
        return False

    try:
        cited_year = int(ref.get("year"))
        matched_year = int(profile.get("matched_year"))
    except (TypeError, ValueError):
        return False
    if cited_year == matched_year or profile.get("year_match") is not False:
        return False
    if profile.get("year_mismatch_plausible") is True:
        return False

    coordinate_matches = {
        "volume", "issue", "article_page_range", "article_locator",
        "elocator", "article_number",
    }
    if any(
        isinstance(item, dict)
        and item.get("kind") in coordinate_matches
        and item.get("status") == "match"
        for item in profile.get("coordinate_comparisons") or ()
    ):
        return False
    return True


def _crossref_has_conflicting_cited_subtitle(ref: dict, msg: dict) -> bool:
    """Reject an optional Crossref subtitle that conflicts with explicit cited title text."""
    primary_title = _crossref_primary_title(msg)
    complete_title = _crossref_complete_title(msg)
    cited_title_key = _compact_citation_title(ref.get("title"))
    primary_title_key = _compact_citation_title(primary_title)
    subtitles = msg.get("subtitle") if isinstance(msg, dict) else None
    subtitle = subtitles[0] if isinstance(subtitles, list) and subtitles else None
    if (
        not primary_title_key
        or not cited_title_key
        or not isinstance(subtitle, str)
        or not subtitle.strip()
        or complete_title is None
        or complete_title == primary_title
        or _citation_title_exactly_equivalent(ref, primary_title)
        or _citation_title_exactly_equivalent(ref, complete_title)
        or not cited_title_key.startswith(f"{primary_title_key} ")
    ):
        return False
    cited_suffix_tokens = _matching._sources._tokens(
        cited_title_key[len(primary_title_key):]
    )
    subtitle_tokens = _matching._sources._tokens(subtitle)
    if not cited_suffix_tokens or not subtitle_tokens:
        return False

    def token_coverage(source: set[str], target: set[str]) -> float:
        return sum(
            any(SequenceMatcher(None, token, other).ratio() >= 0.85 for other in target)
            for token in source
        ) / len(source)

    return min(
        token_coverage(cited_suffix_tokens, subtitle_tokens),
        token_coverage(subtitle_tokens, cited_suffix_tokens),
    ) < 0.85


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return (
        not ref.get("url")
        and (
            _titleless_coordinate_details(ref) is not None
            or _generic_article_locator_source_details(ref) is not None
            or _ambiguous_boundary_search_source(ref) is not None
            or (
                resolve_mod._article_like_resolution_candidate(ref)
                and (
                    ref.get("source_type") == "article"
                    or bool(resolve_mod._article_title_candidate(ref))
                )
            )
        )
    )


def _coordinate(ref: dict, kind: str) -> str | None:
    for item in ref.get("cited_coordinates") or ():
        if isinstance(item, dict) and item.get("kind") == kind:
            value = item.get("normalized_value") or item.get("raw_value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _http_error_body(exc: urllib.error.HTTPError) -> str | None:
    try:
        body = exc.read()
    except Exception:
        return None
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body) if body is not None else None


def probe_journal_coverage(authority: dict) -> dict:
    """Probe only Crossref's exact ISSN journal endpoint, never title search."""
    from core.resolve import service as resolve_mod
    issns = authority.get("issns") if isinstance(authority, dict) else ()
    if not isinstance(issns, tuple) or not issns:
        raise ValueError("coverage authority has no registered ISSN")
    responses = []
    for issn in issns:
        url = f"https://api.crossref.org/journals/{urllib.parse.quote(issn)}"
        try:
            status, body = resolve_mod._get(url)
            payload = json.loads(body).get("message")
            returned_issns = payload.get("ISSN") if isinstance(payload, dict) else None
            if isinstance(returned_issns, str):
                returned_issns = [returned_issns]
            if (
                not isinstance(payload, dict)
                or not isinstance(returned_issns, list)
                or issn.casefold() not in {
                    str(value).strip().casefold() for value in returned_issns
                    if isinstance(value, str) and value.strip()
                }
            ):
                raise ValueError("Crossref journal response is malformed")
            responses.append((issn, int(status), payload, url, None))
        except urllib.error.HTTPError as exc:
            responses.append((issn, exc.code, None, url, _http_error_body(exc)))
        except Exception as exc:
            return {"resolver": JOURNAL_COVERAGE["resolver"], "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "incomplete", "provider_journal_id": None, "work_count": None, "source_url": url, "http_status": None, "response": None, "query_contract": "GET /journals/{issn}", "completion": "incomplete", "reason": f"network: {type(exc).__name__}"}
    found = next((item for item in responses if item[1] == 200), None)
    if found:
        issn, status, payload, url, _error_body = found
        return {"resolver": "crossref", "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "covered", "provider_journal_id": issn, "work_count": None, "source_url": url, "http_status": status, "response": payload, "query_contract": "GET /journals/{issn}", "completion": "complete", "reason": "exact Crossref ISSN journal record exists"}
    if all(item[1] == 404 for item in responses):
        response = {"queries": [
            {"issn": issn, "http_status": status, "source_url": url, "body": error_body}
            for issn, status, _payload, url, error_body in responses
        ]}
        return {"resolver": "crossref", "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "not_covered", "provider_journal_id": None, "work_count": None, "source_url": responses[0][3], "http_status": 404, "response": response, "query_contract": "GET /journals/{issn}", "completion": "complete", "reason": "all registered ISSNs returned Crossref 404"}
    status = next((item[1] for item in responses if item[1] != 404), None)
    return {"resolver": "crossref", "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "incomplete", "provider_journal_id": None, "work_count": None, "source_url": responses[0][3], "http_status": status, "response": None, "query_contract": "GET /journals/{issn}", "completion": "incomplete", "reason": "exact Crossref ISSN coverage probe did not complete"}


def _page_span(value: object) -> tuple[int, int] | None:
    text = re.sub(r"[‐‑‒–—−]", "-", str(value or ""))
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", text)
    if match is None:
        return None
    first, last = int(match[1]), int(match[2])
    if len(match[2]) < len(match[1]):
        base = 10 ** len(match[2])
        last = (first // base) * base + last
        if last < first:
            last += base
    return (first, last) if first <= last else None


def _candidate_years(msg: dict) -> set[int]:
    years: set[int] = set()
    for key in ("published-print", "published-online", "published", "issued"):
        values = (msg.get(key) or {}).get("date-parts") if isinstance(msg.get(key), dict) else None
        if isinstance(values, list) and values and isinstance(values[0], list) and values[0]:
            year = values[0][0]
            if type(year) is int:
                years.add(year)
    return years


def _issue_matches(cited: str | None, observed: object) -> bool:
    if cited is None:
        return True
    found = str(observed or "").strip()
    if not found:
        return False
    if cited.isdigit() and found.isdigit():
        return int(cited) == int(found)
    return " ".join(cited.casefold().split()) == " ".join(found.casefold().split())


def _venue_matches(ref: dict, msg: dict) -> bool:
    """Require exact cited title, or local-authority canonical title, for a range claim."""
    from core.resolve.journal_authority import _text_key, assess_local_journal

    venue = (msg.get("container-title") or [None])[0]
    if not isinstance(venue, str) or not venue.strip():
        return False
    authority = assess_local_journal(ref)
    expected = authority.get("canonical_title") if authority else _coordinate(ref, "container")
    return bool(expected and _text_key(expected) == _text_key(venue))


def supports_coordinate_occupancy(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return bool(
        resolve_mod._article_coordinate_eligible(ref)
        and _coordinate(ref, "container")
        and _coordinate(ref, "volume")
        and _page_span(_coordinate(ref, "article_page_range"))
        and type(ref.get("year")) is int
    )


def occupy_coordinates(ref: dict) -> dict:
    """Find one incompatible Crossref work containing the asserted page span.

    Empty, partial, ambiguous and failed searches are all inconclusive.  The
    result is intentionally not an inventory claim.
    """
    from core.resolve import service as resolve_mod

    cited_page = _page_span(_coordinate(ref, "article_page_range"))
    journal = _coordinate(ref, "container")
    volume = _coordinate(ref, "volume")
    cited_issue = _coordinate(ref, "issue")
    year = ref.get("year")
    if not cited_page or not journal or not volume or type(year) is not int:
        return {
            "status": "unverified", "via": COORDINATE_OCCUPANCY["provider"],
            "reason": "citation lacks a closed Crossref range tuple",
        }
    query = {
        "query.container-title": journal,
        "filter": f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31",
        "rows": "1000",
    }
    issns: tuple[str, ...] = ()
    try:
        from core.resolve.journal_authority import assess_local_journal, issns_for_record

        authority = assess_local_journal(ref)
        if authority is not None:
            issns = tuple(sorted(issns_for_record(authority)))
    except Exception as exc:
        return {
            "status": "unresolved", "via": COORDINATE_OCCUPANCY["provider"],
            "reason": f"local journal authority: {type(exc).__name__}",
        }

    urls = (
        [
            "https://api.crossref.org/journals/"
            + urllib.parse.quote(issn)
            + "/works?"
            + urllib.parse.urlencode({"filter": query["filter"], "rows": query["rows"]})
            for issn in issns
        ]
        if issns
        else ["https://api.crossref.org/works?" + urllib.parse.urlencode(query)]
    )
    # An exact local authority lets Crossref scope the works inventory by a
    # registered ISSN.  Without one, retain the older title-search route as an
    # explicitly weaker, positive-occupancy-only fallback.
    results = []
    for url in urls:
        result = _coordinate_occupants_for_url(
            ref, url, cited_page, volume, cited_issue, year,
        )
        results.append(result)
    # A local authority can have more than one registered ISSN.  They form one
    # scope: any incomplete query can hide a second occupant, and a candidate
    # found under one ISSN cannot settle the result before every ISSN is read.
    if any(result["status"] == "unresolved" for result in results):
        return next(result for result in results if result["status"] == "unresolved")
    if not results:
        return {
            "status": "unresolved", "via": COORDINATE_OCCUPANCY["provider"],
            "reason": "Crossref coordinate query was not attempted",
        }
    occupants: dict[str, tuple[dict, dict]] = {}
    for result in results:
        occupants.update(result["occupants"])
    return _coordinate_occupancy_result(occupants, year=year, ref=ref)


def _coordinate_occupants_for_url(
    ref: dict, url: str, cited_page: tuple[int, int], volume: str,
    cited_issue: str | None, year: int,
) -> dict:
    from core.resolve import service as resolve_mod

    try:
        status, body = resolve_mod._get(url)
        message = json.loads(body).get("message", {})
        items = message.get("items") if isinstance(message, dict) else None
        total_results = message.get("total-results") if isinstance(message, dict) else None
        if not isinstance(items, list) or type(total_results) is not int:
            raise ValueError("Crossref works response has no bounded result set")
    except urllib.error.HTTPError as exc:
        return {
            "status": "unresolved", "via": COORDINATE_OCCUPANCY["provider"],
            "reason": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "status": "unresolved", "via": COORDINATE_OCCUPANCY["provider"],
            "reason": f"network: {type(exc).__name__}",
        }

    if type(status) is not int or status != 200:
        return {
            "status": "unresolved", "via": COORDINATE_OCCUPANCY["provider"],
            "reason": f"Crossref coordinate query returned HTTP {status}",
        }

    # Unseen records could contain a second occupant and invalidate uniqueness.
    # A truncated query is therefore operationally inconclusive even though
    # negative results are never used as absence evidence here.
    if total_results < 0 or total_results != len(items):
        return {
            "status": "unresolved", "via": COORDINATE_OCCUPANCY["provider"],
            "reason": "Crossref coordinate result cardinality is incomplete or inconsistent",
        }

    occupants: dict[str, tuple[dict, dict]] = {}
    for msg in items:
        if not isinstance(msg, dict) or str(msg.get("volume") or "").strip() != volume:
            continue
        if (
            year not in _candidate_years(msg)
            or not _venue_matches(ref, msg)
            or not _issue_matches(cited_issue, msg.get("issue"))
        ):
            continue
        span = _page_span(msg.get("page"))
        if span is None or not (span[0] <= cited_page[0] and cited_page[1] <= span[1]):
            continue
        title = _crossref_complete_title(msg)
        if not isinstance(title, str) or not title.strip():
            continue
        profile = _crossref_metadata_match_profile(ref, msg)
        key = str(msg.get("DOI") or "").strip().casefold()
        if not key:
            key = "\x1f".join((
                title.casefold(), str(msg.get("page") or ""), volume,
                str(msg.get("issue") or ""),
            ))
        occupants[key] = (msg, profile)
    return {"status": "complete", "occupants": occupants}


def _coordinate_occupancy_result(
    occupants: dict[str, tuple[dict, dict]], *, year: int, ref: dict,
) -> dict:
    from core.resolve import service as resolve_mod

    if len(occupants) != 1:
        return {
            "status": "ambiguous" if occupants else "unverified",
            "via": COORDINATE_OCCUPANCY["provider"],
            "reason": (
                "multiple Crossref works occupy the cited coordinates"
                if occupants else "no unique Crossref page-range occupant"
            ),
        }
    msg, profile = next(iter(occupants.values()))
    author_conflict = (
        profile.get("author_match") is False
        and profile.get("cited_first_author")
        and profile.get("matched_first_author")
    )
    raw_title_overlap = resolve_mod.title_overlap(
        _crossref_complete_title(msg),
        # This is used only to reject a positive contradiction.  It does not
        # promote the candidate to an identified work.
        ref.get("raw_entry"),
    )
    cited_title = ref.get("title")
    title_conflict = (
        isinstance(cited_title, str)
        and bool(cited_title.strip())
        and isinstance(profile.get("title_overlap"), (int, float))
        and profile["title_overlap"] < 0.50
        and not (
            raw_title_overlap is not None
            and raw_title_overlap >= 0.85
            and profile.get("author_match") is True
        )
    )
    if not (title_conflict or author_conflict):
        return {
            "status": "unverified", "via": COORDINATE_OCCUPANCY["provider"],
            "reason": "unique Crossref coordinate occupant is compatible with the citation",
        }
    title = _crossref_complete_title(msg)
    return {
        "status": "resolved", "via": COORDINATE_OCCUPANCY["provider"],
        "reason": "Crossref metadata positively confirms another work contains the cited page range",
        "matched_title": title,
        "matched_authors": _matched_authors(msg),
        "matched_year": year,
        "doi": msg.get("DOI"),
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium",
        "metadata_match": profile,
    }


def _fulltext_meta(msg: dict) -> dict:
    links = msg.get("link") or []
    licenses = msg.get("license") or []
    # Crossref licenses describe a declaration in metadata.  They do not prove
    # that this client can retrieve a readable full text, and many licenses are
    # not an OA grant.  Keep the declaration auditable without promoting it to
    # either observed access or full-text existence.
    license_urls = [item.get("URL") for item in licenses if item.get("URL")]
    declared_open = any(
        re.match(r"^https?://creativecommons\.org/(?:licenses|publicdomain)/", url, re.I)
        for url in license_urls
    )
    declared_oa = "open" if declared_open else "licensed" if licenses else "unknown"
    doi = msg.get("DOI")
    doi_url = f"https://doi.org/{doi}" if doi else None
    out_links = []
    seen_urls = set()
    for link in links:
        url = link.get("URL")
        if not url or url == doi_url or url in seen_urls:
            continue
        seen_urls.add(url)
        out_links.append({
            "url": url,
            "content_type": link.get("content-type"),
            "intended_application": link.get("intended-application"),
        })
    if doi_url:
        out_links.append({"url": doi_url, "content_type": "doi"})
    out = {
        # A DOI landing URL alone proves no retrievable text.  A publisher link
        # explicitly registered as full text is still useful existence evidence;
        # actual accessibility remains a fetch-time observation.
        "fulltext_exists": True if links else "unknown",
        "oa_status": "unknown",
        "work_type": msg.get("type"),
        "fulltext_links": out_links,
    }
    if declared_oa != "unknown":
        out["oa_declared_status"] = declared_oa
    if license_urls:
        out["oa_license_urls"] = license_urls
    return out


def _matched_authors(msg: dict) -> list[str]:
    return [
        str(author["family"]) for author in msg.get("author") or ()
        if isinstance(author, dict) and isinstance(author.get("family"), str)
        and author["family"].strip()
    ]


def _crossref_doi_key(value: object) -> str | None:
    from core.resolve.service import _normalize_doi_value

    normalized = _normalize_doi_value(value if isinstance(value, str) else None)
    return normalized if normalized and re.fullmatch(r"10\.\d{4,9}/[^\s]+", normalized) else None


def _crossref_native_work_proof_is_valid(
    proof: object,
    *,
    expected_doi: object,
) -> bool:
    """Revalidate a retained native Crossref preprint response for one DOI."""
    if type(proof) is not dict or set(proof) != _CROSSREF_NATIVE_WORK_PROOF_FIELDS:
        return False
    if type(proof.get("version")) is not int or proof["version"] != 1:
        return False
    requested = _crossref_doi_key(proof.get("requested_doi"))
    expected = _crossref_doi_key(expected_doi)
    if not requested or not expected or requested.casefold() != expected.casefold():
        return False
    expected_url = f"https://api.crossref.org/works/{urllib.parse.quote(requested)}"
    if proof.get("request_url") != expected_url:
        return False
    if type(proof.get("http_status")) is not int or proof["http_status"] != 200:
        return False
    body = proof.get("response_body")
    digest = proof.get("response_sha256")
    if type(body) is not str or not body or type(digest) is not str:
        return False
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return False
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
        return False
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        return False
    returned = message.get("DOI")
    return bool(
        isinstance(returned, str)
        and returned.strip().casefold() == requested.casefold()
        and message.get("type") == "posted-content"
        and message.get("subtype") == "preprint"
    )


def _mint_crossref_native_work_proof(
    *,
    doi: object,
    request_url: str,
    http_status: int,
    response_body: object,
) -> dict | None:
    """Retain only an exact successful direct response asserting preprint subtype."""
    requested = _crossref_doi_key(doi)
    if not requested or type(http_status) is not int or http_status != 200:
        return None
    proof = {
        "version": 1,
        "requested_doi": requested,
        "request_url": request_url,
        "http_status": http_status,
        "response_body": response_body,
        "response_sha256": (
            hashlib.sha256(response_body.encode("utf-8")).hexdigest()
            if type(response_body) is str else ""
        ),
    }
    return proof if _crossref_native_work_proof_is_valid(
        proof, expected_doi=requested,
    ) else None


_CROSSREF_WHOLE_BOOK_TYPES = frozenset({"book", "monograph", "edited-book"})


def _standard_isbn_key(value: object) -> str | None:
    """Canonicalize a valid ISBN after removing only standard spacing/hyphens."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not re.fullmatch(r"[0-9Xx]+(?:[ -][0-9Xx]+)*", raw):
        return None
    compact = raw.replace(" ", "").replace("-", "").upper()
    if len(compact) == 10:
        if "X" in compact[:-1]:
            return None
        digits = [10 if char == "X" else int(char) for char in compact]
        if sum((10 - index) * digit for index, digit in enumerate(digits)) % 11:
            return None
        prefix = "978" + compact[:9]
        check = sum(
            (1 if index % 2 == 0 else 3) * int(char)
            for index, char in enumerate(prefix)
        )
        return prefix + str((10 - check % 10) % 10)
    if (
        len(compact) == 13
        and compact.isdigit()
        and compact[:3] in {"978", "979"}
    ):
        check = sum(
            (1 if index % 2 == 0 else 3) * int(char)
            for index, char in enumerate(compact[:12])
        )
        return compact if str((10 - check % 10) % 10) == compact[-1] else None
    return None


def _has_matching_cited_crossref_isbn(ref: dict, msg: dict) -> bool:
    cited_values = ref.get("isbn")
    native_values = msg.get("ISBN")
    if isinstance(cited_values, str):
        cited_values = (cited_values,)
    if isinstance(native_values, str):
        native_values = (native_values,)
    if (
        not isinstance(cited_values, (list, tuple))
        or not isinstance(native_values, (list, tuple))
    ):
        return False
    cited = {key for value in cited_values if (key := _standard_isbn_key(value))}
    native = {key for value in native_values if (key := _standard_isbn_key(value))}
    return bool(cited & native)


def _crossref_discovered_book_edition_rejection_reason(
    ref: dict, msg: dict, profile: dict,
) -> str | None:
    """Withhold title-only discovery of a different, unsupported book edition."""
    work_type = msg.get("type")
    if (
        ref.get("source_type") != "book"
        or ref.get("source_kind") != "book_like"
        or str(ref.get("doi") or "").strip()
        or not isinstance(work_type, str)
        or work_type.strip().casefold() not in _CROSSREF_WHOLE_BOOK_TYPES
        or profile.get("year_match") is not False
        or profile.get("year_mismatch_plausible") is True
    ):
        return None
    cited_year = ref.get("year")
    matched_year = profile.get("matched_year")
    if (
        type(cited_year) is not int
        or type(matched_year) is not int
        or cited_year == matched_year
    ):
        return None
    if _has_matching_cited_crossref_isbn(ref, msg):
        return None
    return (
        "Crossref whole-book candidate has an implausible publication-year conflict and no "
        "matching cited ISBN; candidate identity withheld because the cited edition is unsupported"
    )


def _titleless_coordinate_details(ref: dict) -> tuple[str, int, str, str, str] | None:
    """Return the source-proved author/year/volume/full-locator tuple."""
    if (
        not isinstance(ref, dict)
        or ref.get("doi")
        or (isinstance(ref.get("title"), str) and ref["title"].strip())
        or ref.get("source_type") not in {"article", "unknown"}
        or ref.get("source_kind") not in {"article_like", "unknown"}
        or not _matching._has_structurally_titleless_article_coordinates(ref)
    ):
        return None
    raw = ref.get("raw_entry")
    year = ref.get("year")
    if not isinstance(raw, str) or type(year) is not int:
        return None
    coordinates = [
        row for row in ref.get("cited_coordinates") or ()
        if isinstance(row, dict)
    ]
    containers = [row for row in coordinates if row.get("kind") == "container"]
    volumes = [row for row in coordinates if row.get("kind") == "volume"]
    locators = [
        row for row in coordinates
        if row.get("kind") in {
            "article_page_range", "elocator", "article_number", "article_locator",
        }
    ]
    if (
        len(containers) != 1 or len(volumes) != 1 or len(locators) != 1
        or locators[0].get("kind") not in {
            "article_page_range", "elocator", "article_number",
        }
    ):
        return None
    for row in (containers[0], volumes[0], locators[0]):
        if _matching._source_spanned_coordinate(ref, row["kind"], raw) is None:
            return None
        if not isinstance(row.get("normalized_value"), str) or not row["normalized_value"].strip():
            return None
    author = _matching._first_author_key(raw)
    if not author:
        return None
    return (
        author,
        year,
        volumes[0]["normalized_value"],
        locators[0]["normalized_value"],
        locators[0]["kind"],
    )


_REGISTERED_TITLED_COORDINATE_VALIDATION = (
    "crossref:registered_coordinate_certificate"
)
_REGISTERED_TITLED_COORDINATE_RULE = "crossref-nlm-registered-coordinates/v1"


def _registered_titled_coordinate_details(
    ref: dict,
) -> tuple[str, int, str, str, str] | None:
    """Return a source-proved titled article tuple for the opt-in NLM route."""
    if (
        not isinstance(ref, dict)
        or ref.get("doi")
        or not isinstance(ref.get("title"), str)
        or not ref["title"].strip()
        or ref.get("source_type") not in {"article", "unknown"}
        or ref.get("source_kind") not in {"article_like", "unknown"}
        or not _matching._has_structurally_titled_article_coordinates(ref)
    ):
        return None
    raw = ref.get("raw_entry")
    year = ref.get("year")
    if not isinstance(raw, str) or type(year) is not int:
        return None
    coordinates = [
        row for row in ref.get("cited_coordinates") or ()
        if isinstance(row, dict)
    ]
    containers = [row for row in coordinates if row.get("kind") == "container"]
    volumes = [row for row in coordinates if row.get("kind") == "volume"]
    locators = [
        row for row in coordinates
        if row.get("kind") in {
            "article_page_range", "elocator", "article_number", "article_locator",
        }
    ]
    if (
        len(containers) != 1 or len(volumes) != 1 or len(locators) != 1
        or locators[0].get("kind") not in {
            "article_page_range", "elocator", "article_number",
        }
    ):
        return None
    for row in (containers[0], volumes[0], locators[0]):
        if _matching._source_spanned_coordinate(ref, row["kind"], raw) is None:
            return None
        if not isinstance(row.get("normalized_value"), str) or not row["normalized_value"].strip():
            return None
    author = _matching._first_author_key(raw)
    if not author:
        return None
    return (
        author,
        year,
        volumes[0]["normalized_value"],
        locators[0]["normalized_value"],
        locators[0]["kind"],
    )


def _normalized_crossref_issns(value: object) -> tuple[str, ...]:
    from core.resolve.journal_authority import normalize_issn

    if not isinstance(value, list):
        return ()
    return tuple(sorted({
        normalized for item in value
        if (normalized := normalize_issn(item)) is not None
    }))


def _ambiguous_boundary_page_span(value: object) -> tuple[int, int] | None:
    """Accept only a complete, explicit numeric page range."""
    text = re.sub(r"[‐‑‒–—−]", "-", str(value or ""))
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", text)
    if match is None or len(match[2]) < len(match[1]):
        return None
    first, last = int(match[1]), int(match[2])
    return (first, last) if first <= last else None


def _ambiguous_boundary_span(
    raw: str, field: object, expected: object,
) -> tuple[int, int] | None:
    if not isinstance(field, dict) or field.get("value") != expected:
        return None
    spans = field.get("spans")
    if not isinstance(spans, list) or len(spans) != 1 or not isinstance(spans[0], dict):
        return None
    span = spans[0]
    start, end, raw_value = span.get("start"), span.get("end"), span.get("raw_value")
    if (
        type(start) is not int or type(end) is not int or start < 0
        or end <= start or end > len(raw) or not isinstance(raw_value, str)
        or raw[start:end] != raw_value
    ):
        return None
    if type(expected) is int:
        if re.fullmatch(r"\d+", raw_value) is None or int(raw_value) != expected:
            return None
    elif not isinstance(expected, str) or raw_value != expected:
        return None
    return start, end


def _trim_source_punctuation(raw: str, start: int, end: int) -> tuple[int, int]:
    while start < end and (
        raw[start].isspace() or unicodedata.category(raw[start]).startswith("P")
    ):
        start += 1
    while end > start and (
        raw[end - 1].isspace() or unicodedata.category(raw[end - 1]).startswith("P")
    ):
        end -= 1
    return start, end


def _ambiguous_boundary_source_anchor(ref: object) -> dict | None:
    """Validate parser-owned authors and coordinates before considering a cut."""
    if (
        not isinstance(ref, dict) or ref.get("title") is not None
        or ref.get("source_type") not in {"article", "unknown"}
        or ref.get("source_kind") not in {"article_like", "unknown"}
    ):
        return None
    if any(ref.get(key) for key in ("journal", "venue", "container")):
        return None
    raw = ref.get("raw_entry")
    parsed = ref.get("bibliographic_parse")
    conflicts = parsed.get("conflicts") if isinstance(parsed, dict) else None
    if (
        not isinstance(raw, str) or not raw
        or not isinstance(parsed, dict) or parsed.get("status") != "parsed"
        or not isinstance(conflicts, list) or len(conflicts) != 2
        or {(item.get("field"), item.get("reason")) for item in conflicts if isinstance(item, dict)}
        != {
            ("title", "ambiguous_title_journal_boundary"),
            ("journal", "ambiguous_title_journal_boundary"),
        }
    ):
        return None
    fields = parsed.get("fields")
    authors = ref.get("authors")
    if (
        not isinstance(fields, dict) or not isinstance(authors, list) or not authors
        or fields.get("title") is not None or fields.get("journal") is not None
        or not isinstance(fields.get("authors"), dict)
        or fields["authors"].get("value") != authors
    ):
        return None
    author_spans = fields["authors"].get("spans")
    if not isinstance(author_spans, list) or len(author_spans) != len(authors):
        return None
    validated_author_spans: list[tuple[int, int]] = []
    for author, span in zip(authors, author_spans):
        if not isinstance(author, str) or not author.strip() or not isinstance(span, dict):
            return None
        start, end, raw_value = span.get("start"), span.get("end"), span.get("raw_value")
        if (
            type(start) is not int or type(end) is not int or start < 0
            or end <= start or end > len(raw) or raw[start:end] != raw_value
            or raw_value != author
        ):
            return None
        validated_author_spans.append((start, end))
    if any(
        left[1] > right[0]
        for left, right in zip(validated_author_spans, validated_author_spans[1:])
    ):
        return None
    first_author = ref.get("first_author")
    first_author_span = _ambiguous_boundary_span(
        raw, fields.get("first_author"), first_author,
    )
    if (
        not isinstance(first_author, str) or not first_author.strip()
        or first_author_span is None
        or first_author_span[0] < validated_author_spans[0][0]
        or first_author_span[1] > validated_author_spans[0][1]
    ):
        return None

    year = ref.get("year")
    if type(year) is not int:
        return None
    year_span = _ambiguous_boundary_span(raw, fields.get("year"), year)
    volume = ref.get("volume")
    volume_span = _ambiguous_boundary_span(raw, fields.get("volume"), volume)
    pages = ref.get("pages")
    pages_span = _ambiguous_boundary_span(raw, fields.get("pages"), pages)
    if (
        year_span is None or volume_span is None or pages_span is None
        or not isinstance(volume, str) or re.fullmatch(r"\d+", volume.strip()) is None
        or _ambiguous_boundary_page_span(pages) is None
    ):
        return None
    from core.resolve.matching import _source_spanned_coordinate

    volume_coordinate = _source_spanned_coordinate(ref, "volume", raw)
    pages_coordinate = _source_spanned_coordinate(ref, "article_page_range", raw)
    if volume_coordinate != volume_span or pages_coordinate != pages_span:
        return None
    coordinates = ref.get("cited_coordinates") or ()
    kinds = [
        item.get("kind") for item in coordinates
        if isinstance(item, dict) and isinstance(item.get("kind"), str)
    ]
    if (
        kinds.count("volume") != 1 or kinds.count("article_page_range") != 1
        or kinds.count("issue") > 1 or "container" in kinds
        or set(kinds) - {"volume", "issue", "article_page_range"}
    ):
        return None
    issue = _coordinate(ref, "issue")
    if kinds.count("issue") != (1 if issue else 0):
        return None
    issue_span = (
        _source_spanned_coordinate(ref, "issue", raw) if issue is not None else None
    )
    if issue is not None and issue_span is None:
        return None

    author_end = validated_author_spans[-1][1]
    windows = []
    if author_end < year_span[0]:
        windows.append((author_end, year_span[0]))
    if year_span[1] < volume_span[0]:
        windows.append((year_span[1], volume_span[0]))
    source_cuts: list[dict] = []
    for window_start, window_end in windows:
        start, end = _trim_source_punctuation(raw, window_start, window_end)
        if end <= start:
            continue
        segment = raw[start:end]
        for delimiter in re.finditer(r"\.(?=\s+)", segment):
            title_start, title_end = _trim_source_punctuation(
                raw, start, start + delimiter.start(),
            )
            venue_start, venue_end = _trim_source_punctuation(
                raw, start + delimiter.end(), end,
            )
            if title_end <= title_start or venue_end <= venue_start:
                continue
            source_cuts.append({
                "title": raw[title_start:title_end],
                "venue": raw[venue_start:venue_end],
                "venue_span": (venue_start, venue_end),
            })
    if not source_cuts:
        return None
    return {
        "raw_entry": raw,
        "year": year,
        "volume": volume.strip(),
        "issue": issue,
        "pages": _ambiguous_boundary_page_span(pages),
        "authors": authors,
        "first_author": first_author.strip(),
        "source_cuts": source_cuts,
    }


def _ambiguous_boundary_search_source(ref: object) -> dict | None:
    anchor = _ambiguous_boundary_source_anchor(ref)
    if anchor is None or any(
        ref.get(key) for key in ("doi", "pmid", "pmcid", "isbn", "url")
    ):
        return None
    if _matching._article_title_candidate(ref) is not None:
        return None
    return anchor


def _ambiguous_boundary_family_key(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _ambiguous_boundary_cited_families(authors: object) -> list[str] | None:
    if not isinstance(authors, list) or not authors:
        return None
    families = []
    for author in authors:
        if not isinstance(author, str) or not author.strip():
            return None
        family = author.split(",", 1)[0].strip() if "," in author else author.rsplit(" ", 1)[-1]
        key = _ambiguous_boundary_family_key(family)
        if not key:
            return None
        families.append(key)
    return families


def _ambiguous_boundary_source_venue_proof(
    ref: dict, cut: dict, msg: dict, anchor: dict,
) -> tuple[dict, dict] | None:
    from core.resolve.journal_authority import (
        _source_scoped_local_journal,
        _text_key,
        assess_local_journal_alias,
    )

    raw = anchor["raw_entry"]
    start, end = cut["venue_span"]
    value = cut["venue"]
    venue_coordinate = {
        "kind": "container",
        "raw_value": value,
        "normalized_value": " ".join(
            value.replace("‐", "-").replace("‑", "-").replace("‒", "-")
            .replace("–", "-").replace("—", "-").replace("−", "-").split()
        ),
        "spans": [{"raw_start": start, "raw_end": end}],
    }
    proof_ref = dict(ref)
    proof_ref["journal"] = value
    proof_ref["cited_coordinates"] = [
        item for item in (ref.get("cited_coordinates") or ())
        if isinstance(item, dict) and item.get("kind") != "container"
    ] + [venue_coordinate]
    try:
        authority = _source_scoped_local_journal(proof_ref)
        alias = assess_local_journal_alias(proof_ref)
    except Exception:
        return None
    containers = msg.get("container-title")
    primary = containers[0] if isinstance(containers, list) and len(containers) == 1 else None
    raw_issns = msg.get("ISSN")
    candidate_issns = _normalized_crossref_issns(raw_issns)
    registered_issns = authority.get("issns") if isinstance(authority, dict) else None
    if (
        not isinstance(authority, dict)
        or authority.get("status") != "recognized"
        or authority.get("registry") != "nlm-pubmed"
        or (authority.get("venue_witness") or {}).get("mode") != "exact_registered_name"
        or not isinstance(alias, dict)
        or alias.get("status") != "exact"
        or alias.get("registry") != "nlm-pubmed"
        or not isinstance(alias.get("candidates"), list)
        or len(alias["candidates"]) != 1
        or alias["candidates"][0].get("distance") != 0
        or alias["candidates"][0].get("record_id") != authority.get("record_id")
        or alias["candidates"][0].get("canonical_title") != authority.get("canonical_title")
        or not isinstance(primary, str) or not primary.strip()
        or _text_key(primary) != _text_key(authority.get("canonical_title"))
        or not isinstance(raw_issns, list) or not raw_issns
        or len(candidate_issns) != len(raw_issns)
        or not isinstance(registered_issns, list) or not registered_issns
        or not set(candidate_issns).issubset(set(registered_issns))
        or not set(candidate_issns).intersection(registered_issns)
    ):
        return None
    return authority, alias


def _ambiguous_boundary_article_proof(ref: object, msg: object) -> dict | None:
    """Prove one exact title/venue cut against native article data and NLM."""
    if not isinstance(ref, dict) or not isinstance(msg, dict):
        return None
    anchor = _ambiguous_boundary_source_anchor(ref)
    if anchor is None:
        return None
    if (
        msg.get("type") != "journal-article"
        or _crossref_doi_key(msg.get("DOI")) is None
        or _candidate_years(msg) != {anchor["year"]}
        or str(msg.get("volume") or "").strip() != anchor["volume"]
        or (anchor["issue"] is not None and not _issue_matches(anchor["issue"], msg.get("issue")))
        or _ambiguous_boundary_page_span(msg.get("page")) != anchor["pages"]
    ):
        return None
    full_title = _crossref_complete_title(msg)
    if not full_title:
        return None
    cited_families = _ambiguous_boundary_cited_families(anchor["authors"])
    native_authors = msg.get("author")
    if (
        not cited_families or not isinstance(native_authors, list) or not native_authors
        or not isinstance(native_authors[0], dict)
        or not isinstance(native_authors[0].get("family"), str)
        or not native_authors[0]["family"].strip()
    ):
        return None
    native_families = [
        _ambiguous_boundary_family_key(item.get("family"))
        for item in native_authors
        if isinstance(item, dict) and isinstance(item.get("family"), str)
        and item.get("family", "").strip()
    ]
    if (
        not native_families
        or native_families[0] != _ambiguous_boundary_family_key(anchor["first_author"])
        or any(native_families.count(family) < cited_families.count(family)
               for family in set(cited_families))
    ):
        return None
    compatible: list[dict] = []
    for cut in anchor["source_cuts"]:
        if not _titles_exactly_equivalent(cut["title"], full_title):
            continue
        venue_proof = _ambiguous_boundary_source_venue_proof(ref, cut, msg, anchor)
        if venue_proof is None:
            continue
        authority, alias = venue_proof
        compatible.append({
            "title": full_title,
            "source_title": cut["title"],
            "venue": cut["venue"],
            "venue_span": list(cut["venue_span"]),
            "authority": authority,
            "journal_alias_assessment": alias,
            "candidate_issns": _normalized_crossref_issns(msg.get("ISSN")),
        })
    return compatible[0] if len(compatible) == 1 else None


def _ambiguous_boundary_search_and_direct_proofs_agree(
    selected: object, direct: object,
) -> bool:
    """Bind direct DOI validation to the exact source cut selected by search."""
    if not isinstance(selected, dict) or not isinstance(direct, dict):
        return False
    selected_authority = selected.get("authority")
    direct_authority = direct.get("authority")
    selected_alias = selected.get("journal_alias_assessment")
    direct_alias = direct.get("journal_alias_assessment")
    if not all(isinstance(value, dict) for value in (
        selected_authority, direct_authority, selected_alias, direct_alias,
    )):
        return False
    authority_identity = ("registry", "registry_version", "record_id", "canonical_title")
    return bool(
        selected.get("source_title") == direct.get("source_title")
        and selected.get("venue") == direct.get("venue")
        and selected.get("venue_span") == direct.get("venue_span")
        and all(selected_authority.get(key) == direct_authority.get(key)
                for key in authority_identity)
        and selected_alias == direct_alias
        and selected.get("candidate_issns") == direct.get("candidate_issns")
    )


def _crossref_ambiguous_boundary_work_proof_is_valid(
    proof: object, *, expected_doi: object, raw_entry: object,
) -> bool:
    if type(proof) is not dict or set(proof) != _CROSSREF_NATIVE_ARTICLE_PROOF_FIELDS:
        return False
    if type(proof.get("version")) is not int or proof["version"] != 2:
        return False
    requested = _crossref_doi_key(proof.get("requested_doi"))
    expected = _crossref_doi_key(expected_doi)
    if not requested or not expected or requested.casefold() != expected.casefold():
        return False
    if not isinstance(raw_entry, str) or not raw_entry:
        return False
    if proof.get("source_raw_sha256") != hashlib.sha256(raw_entry.encode("utf-8")).hexdigest():
        return False
    if proof.get("request_url") != (
        "https://api.crossref.org/works/" + urllib.parse.quote(requested)
    ):
        return False
    if type(proof.get("http_status")) is not int or proof["http_status"] != 200:
        return False
    body = proof.get("response_body")
    digest = proof.get("response_sha256")
    if (
        type(body) is not str or not body or type(digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or hashlib.sha256(body.encode("utf-8")).hexdigest() != digest
    ):
        return False
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False
    message = payload.get("message") if isinstance(payload, dict) else None
    return bool(
        isinstance(message, dict)
        and _crossref_doi_key(message.get("DOI")) == requested
        and message.get("type") == "journal-article"
    )


def _crossref_ambiguous_boundary_work_proof(
    *, doi: object, request_url: str, http_status: int,
    response_body: object, raw_entry: object,
) -> dict | None:
    requested = _crossref_doi_key(doi)
    if not requested or type(http_status) is not int or http_status != 200:
        return None
    proof = {
        "version": 2,
        "requested_doi": requested,
        "request_url": request_url,
        "http_status": http_status,
        "response_body": response_body,
        "response_sha256": (
            hashlib.sha256(response_body.encode("utf-8")).hexdigest()
            if type(response_body) is str else ""
        ),
        "source_raw_sha256": (
            hashlib.sha256(raw_entry.encode("utf-8")).hexdigest()
            if isinstance(raw_entry, str) else ""
        ),
    }
    return proof if _crossref_ambiguous_boundary_work_proof_is_valid(
        proof, expected_doi=requested, raw_entry=raw_entry,
    ) else None


def _crossref_ambiguous_boundary_attempt_proof(
    ref: dict, attempt: object, expected_doi: str,
) -> tuple[dict, dict] | None:
    if (
        not isinstance(attempt, dict)
        or attempt.get("status") != "resolved"
        or attempt.get("via") != NAME
        or attempt.get("resolution_basis") != "metadata_search"
        or _crossref_doi_key(attempt.get("doi")) != expected_doi
        or attempt.get("work_type") != "journal-article"
    ):
        return None
    proof = attempt.get(_CROSSREF_NATIVE_WORK_PROOF_FIELD)
    if not _crossref_ambiguous_boundary_work_proof_is_valid(
        proof, expected_doi=expected_doi, raw_entry=ref.get("raw_entry"),
    ):
        return None
    try:
        message = json.loads(proof["response_body"]).get("message")
    except (TypeError, ValueError, AttributeError):
        return None
    details = _ambiguous_boundary_article_proof(ref, message)
    if details is None:
        return None
    resolved = attempt.get("resolved_identifier")
    profile = _crossref_metadata_match_profile(ref, message)
    if (
        not isinstance(resolved, dict)
        or resolved.get("type") != "doi"
        or _crossref_doi_key(resolved.get("value")) != expected_doi
        or resolved.get("validated_via") != _AMBIGUOUS_TITLE_JOURNAL_VALIDATED_VIA
        or attempt.get("matched_title") != details["title"]
        or attempt.get("matched_authors") != _matched_authors(message)
        or attempt.get("metadata_match") != profile
    ):
        return None
    return message, details


def _crossref_ambiguous_boundary_relock_details(
    ref: dict, result: dict, attempts: object,
) -> dict | None:
    resolved = result.get("resolved_identifier") if isinstance(result, dict) else None
    doi = _crossref_doi_key(resolved.get("value")) if isinstance(resolved, dict) else None
    result_doi = _crossref_doi_key(result.get("doi")) if isinstance(result, dict) else None
    if (
        _ambiguous_boundary_search_source(ref) is None
        or not isinstance(result, dict)
        or result.get("status") != "resolved"
        or result.get("via") != NAME
        or result.get("resolution_basis") != "metadata_search"
        or not doi
        or ("doi" in result and result_doi != doi)
        or not isinstance(resolved, dict)
        or resolved.get("type") != "doi"
        or resolved.get("validated_via") != _AMBIGUOUS_TITLE_JOURNAL_VALIDATED_VIA
        or not isinstance(attempts, (list, tuple))
    ):
        return None
    valid = [
        checked for attempt in attempts
        if (checked := _crossref_ambiguous_boundary_attempt_proof(ref, attempt, doi)) is not None
    ]
    if len(valid) != 1:
        return None
    message, details = valid[0]
    profile = _crossref_metadata_match_profile(ref, message)
    evidence_profile = result.get("evidence_profile")
    persisted_profile = (
        evidence_profile.get("metadata_match")
        if isinstance(evidence_profile, dict) else None
    )
    result_profile = result.get("metadata_match")
    if result_profile is None:
        result_profile = persisted_profile
    if (
        result.get("matched_title") != details["title"]
        or (
            "matched_authors" in result
            and result.get("matched_authors") != _matched_authors(message)
        )
        or result.get("work_type") != "journal-article"
        or not isinstance(result_profile, dict)
        or result_profile != profile
        or (
            "metadata_match" in result
            and result.get("metadata_match") != profile
        )
    ):
        return None
    return {"message": message, **details}


def _crossref_ambiguous_boundary_relock(
    ref: dict, result: dict, attempts: object,
) -> bool:
    return _crossref_ambiguous_boundary_relock_details(ref, result, attempts) is not None


def _crossref_ambiguous_boundary_journal_alias_assessment(
    ref: dict, result: dict, attempts: object,
) -> dict | None:
    details = _crossref_ambiguous_boundary_relock_details(ref, result, attempts)
    alias = details.get("journal_alias_assessment") if isinstance(details, dict) else None
    return alias if isinstance(alias, dict) else None


def _crossref_registered_titled_coordinate_certificate(
    ref: dict,
    msg: dict,
    profile: dict | None = None,
    *,
    stored_proof: dict | None = None,
    authority: dict | None = None,
    registered_issns: tuple[str, ...] | None = None,
    queried_issn: str | None = None,
) -> dict | None:
    """Return exact NLM/ISSN proof only for a complete titled article tuple."""
    from core.resolve import service as resolve_mod
    from core.resolve.journal_authority import (
        assess_local_journal,
        issns_for_record,
    )

    details = _registered_titled_coordinate_details(ref)
    if details is None or not isinstance(msg, dict):
        return None
    author, year, cited_volume, cited_locator, locator_kind = details
    if (
        msg.get("type") != "journal-article"
        or not _crossref_primary_title(msg)
        or not _crossref_doi_key(msg.get("DOI"))
    ):
        return None
    authors = msg.get("author")
    first_author = authors[0] if isinstance(authors, list) and authors else None
    family = first_author.get("family") if isinstance(first_author, dict) else None
    if not isinstance(family, str) or _matching._fold_author(author) != _matching._fold_author(family):
        return None
    if _matching._crossref_year(msg) != year:
        return None

    comparisons = profile
    if not isinstance(comparisons, dict):
        comparisons = _crossref_metadata_match_profile(ref, msg)
    if (
        comparisons.get("author_match") is not True
        or comparisons.get("year_match") is not True
        or comparisons.get("ordinal_conflict") is True
        or comparisons.get("metadata_conflict") is True
    ):
        return None
    rows_by_kind: dict[str, list[dict]] = {}
    for row in comparisons.get("coordinate_comparisons") or ():
        if isinstance(row, dict) and isinstance(row.get("kind"), str):
            rows_by_kind.setdefault(row["kind"], []).append(row)
    if (
        len(rows_by_kind.get("container", ())) != 1
        or len(rows_by_kind.get("volume", ())) != 1
        or len(rows_by_kind.get(locator_kind, ())) != 1
        or any(
            row.get("status") == "mismatch"
            for kind, rows in rows_by_kind.items() if kind != "container"
            for row in rows
        )
        or rows_by_kind["volume"][0].get("status") != "match"
        or rows_by_kind[locator_kind][0].get("status") != "match"
    ):
        return None
    metadata_volume = msg.get("volume")
    if not isinstance(metadata_volume, str) or _matching._coordinate_comparison_value(
        "volume", cited_volume,
    ) != _matching._coordinate_comparison_value("volume", metadata_volume):
        return None
    if locator_kind == "article_page_range":
        metadata_locator = msg.get("page")
    else:
        metadata_locator = msg.get("article-number") or msg.get("page")
    if not isinstance(metadata_locator, str) or _matching._coordinate_comparison_value(
        locator_kind, cited_locator,
    ) != _matching._coordinate_comparison_value(locator_kind, metadata_locator):
        return None

    matched_title = _crossref_complete_title(msg)
    if (
        _matching._metadata_candidate_rejection_reason(
            ref, matched_title, comparisons, candidate_type=msg.get("type"),
        )
        or _matching._metadata_candidate_is_dataset_for_journal_article(
            ref, _crossref_primary_title(msg), candidate_type=msg.get("type"),
        )
        or _crossref_has_conflicting_cited_subtitle(ref, msg)
    ):
        return None

    candidate_issns = _normalized_crossref_issns(msg.get("ISSN"))
    if not candidate_issns:
        return None
    if authority is None:
        authority = assess_local_journal(ref)
    if (
        not isinstance(authority, dict)
        or authority.get("status") != "recognized"
        or authority.get("registry") != "nlm-pubmed"
    ):
        return None
    if registered_issns is None:
        registered_issns = issns_for_record(authority)
    registered_set = set(registered_issns)
    if (
        not registered_set
        or not set(candidate_issns).intersection(registered_set)
        or (queried_issn is not None and queried_issn not in candidate_issns)
    ):
        return None
    proof = {
        "rule_version": _REGISTERED_TITLED_COORDINATE_RULE,
        "registry": authority.get("registry"),
        "registry_version": authority.get("registry_version"),
        "record_id": authority.get("record_id"),
        "snapshot_sha256": authority.get("snapshot_sha256"),
        "candidate_issns": list(candidate_issns),
    }
    if (
        not isinstance(proof["registry_version"], str)
        or not proof["registry_version"].strip()
        or not isinstance(proof["record_id"], str)
        or not proof["record_id"].strip()
        or not isinstance(proof["snapshot_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", proof["snapshot_sha256"])
        or (stored_proof is not None and stored_proof != proof)
    ):
        return None
    # Confirm the registry fields still describe the current catalog, including
    # when the caller supplied an authority object obtained earlier in discovery.
    current_authority = assess_local_journal(ref)
    if current_authority != authority:
        return None
    current_issns = issns_for_record(current_authority)
    if current_issns != registered_issns:
        return None
    if not set(candidate_issns).intersection(current_issns):
        return None
    if queried_issn is not None and queried_issn not in current_issns:
        return None
    return proof


_DOTTED_UPPER_INITIALISM = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z]\s*\.\s*){2,}(?![A-Za-z0-9])"
)


def _coordinate_venue_key(value: object) -> str:
    """Normalize native container keys, preserving closed dotted initialisms."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _DOTTED_UPPER_INITIALISM.sub(
        lambda match: "".join(re.findall(r"[A-Z]", match.group(0))), text,
    )
    text = unicodedata.normalize("NFKD", text).casefold()
    specialized_key = " ".join(
        "".join(char if char.isalnum() else " " for char in text).split()
    )
    return _matching._coordinate_comparison_value("container", specialized_key)


def _titleless_coordinate_venue_alias(ref: dict, msg: dict) -> str | None:
    """Return a literal primary/short-container venue match, or None."""
    cited = _coordinate(ref, "container")
    venues = msg.get("container-title")
    candidate = venues[0] if isinstance(venues, list) and venues else None
    if not isinstance(cited, str) or not cited.strip() or not isinstance(candidate, str) or not candidate.strip():
        return None
    if _coordinate_venue_key(cited) == _coordinate_venue_key(candidate):
        return ""
    short_titles = msg.get("short-container-title")
    if not isinstance(short_titles, list):
        return None
    for alias in short_titles:
        if isinstance(alias, str) and alias.strip() and (
            _coordinate_venue_key(cited) == _coordinate_venue_key(alias)
        ):
            return alias.strip()
    return None


def _titleless_coordinate_venue_matches(ref: dict, msg: dict) -> bool:
    return _titleless_coordinate_venue_alias(ref, msg) is not None


def _ordered_native_venue_composition_witness(
    ref: dict, msg: dict, source_span: tuple[int, int],
) -> dict | None:
    """Prove an ordered source venue from one native primary/short title pair."""
    if not isinstance(msg, dict):
        return None
    raw = ref.get("raw_entry")
    venues = msg.get("container-title")
    short_titles = msg.get("short-container-title")
    if (
        not isinstance(raw, str)
        or not isinstance(venues, list) or len(venues) != 1
        or not isinstance(short_titles, list) or len(short_titles) != 1
    ):
        return None
    primary, short = venues[0], short_titles[0]
    if (
        not isinstance(primary, str) or not primary.strip()
        or not isinstance(short, str) or not short.strip()
    ):
        return None
    source_start, source_end = source_span
    source_value = raw[source_start:source_end]
    cited_tokens = _coordinate_venue_key(source_value).split()
    primary_tokens = _coordinate_venue_key(primary).split()
    short_tokens = _coordinate_venue_key(short).split()
    if (
        len(cited_tokens) < 2
        or len(cited_tokens) != len(primary_tokens)
        or len(cited_tokens) != len(short_tokens)
    ):
        return None
    primary_contribution = False
    short_contribution = False
    for cited, native_primary, native_short in zip(
        cited_tokens, primary_tokens, short_tokens,
    ):
        if cited != native_primary and cited != native_short:
            return None
        if cited == native_primary and native_primary != native_short:
            primary_contribution = True
        if cited == native_short and native_short != native_primary:
            short_contribution = True
    if not primary_contribution or not short_contribution:
        return None
    cited_coordinate = _coordinate(ref, "container")
    if (
        not isinstance(cited_coordinate, str)
        or _coordinate_venue_key(cited_coordinate) != " ".join(cited_tokens)
    ):
        return None
    return {
        "rule": _TITLELESS_NATIVE_VENUE_COMPOSITION_RULE,
        "raw_entry_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "native_primary": primary.strip(),
        "native_short": short.strip(),
    }


def _titleless_native_venue_composition_witness(
    ref: dict, msg: dict,
) -> dict | None:
    """Prove a source venue for the existing typed titleless-coordinate route."""
    if (
        not isinstance(msg, dict)
        or _titleless_coordinate_details(ref) is None
        or _titleless_coordinate_venue_alias(ref, msg) is not None
    ):
        return None
    raw = ref.get("raw_entry")
    source_span = (
        _matching._source_spanned_coordinate(ref, "container", raw)
        if isinstance(raw, str) else None
    )
    if source_span is None:
        return None
    return _ordered_native_venue_composition_witness(ref, msg, source_span)


def _generic_article_locator_source_details(ref: dict) -> dict | None:
    """Return a closed, source-spanned numeric generic-locator journal tuple."""
    if (
        not isinstance(ref, dict)
        or any(ref.get(key) for key in ("doi", "pmid", "isbn", "url"))
        or ref.get("source_type") not in {"article", "unknown"}
        or ref.get("source_kind") not in {"article_like", "unknown"}
    ):
        return None
    raw = ref.get("raw_entry")
    year = ref.get("year")
    coordinates = ref.get("cited_coordinates")
    if not isinstance(raw, str) or type(year) is not int or not isinstance(coordinates, list):
        return None
    allowed = {"container", "volume", "issue", "article_locator"}
    if any(not isinstance(row, dict) or row.get("kind") not in allowed for row in coordinates):
        return None
    by_kind = {
        kind: [row for row in coordinates if row.get("kind") == kind]
        for kind in allowed
    }
    if (
        len(by_kind["container"]) != 1
        or len(by_kind["volume"]) != 1
        or len(by_kind["article_locator"]) != 1
        or len(by_kind["issue"]) > 1
    ):
        return None
    spans: dict[str, list[int]] = {}
    values: dict[str, str] = {}
    for kind, rows in by_kind.items():
        if not rows:
            continue
        row = rows[0]
        span = _matching._source_spanned_coordinate(ref, kind, raw)
        value = row.get("normalized_value")
        if span is None or not isinstance(value, str) or not value.strip():
            return None
        spans[kind] = [span[0], span[1]]
        values[kind] = value.strip()
    locator_span = spans.get("article_locator")
    locator = values.get("article_locator")
    volume = values.get("volume")
    container = values.get("container")
    issue = values.get("issue")
    if (
        not locator_span
        or not isinstance(locator, str)
        or not re.fullmatch(r"[0-9]+", locator)
        or not isinstance(volume, str)
        or not re.fullmatch(r"[0-9]{1,4}", volume)
        or not isinstance(container, str)
        or not container.strip()
        or (issue is not None and not re.fullmatch(r"[0-9]+", issue))
    ):
        return None
    try:
        from core.parse.parsing_common import _closed_journal_title_extent

        closed_title = _closed_journal_title_extent(raw, year, coordinates)
    except (ImportError, AttributeError, TypeError, ValueError):
        return None
    if closed_title is None:
        return None
    title, title_span = closed_title
    ref_title = ref.get("title")
    if title is None:
        if isinstance(ref_title, str) and ref_title.strip():
            return None
        source_titleless = True
    else:
        if (
            not isinstance(ref_title, str)
            or " ".join(ref_title.split()) != " ".join(title.split())
        ):
            return None
        source_titleless = False
    author = _matching._first_author_key(raw)
    if not author:
        return None
    return {
        "author": author,
        "year": year,
        "container": container,
        "volume": volume,
        "issue": issue,
        "locator": locator,
        "spans": spans,
        "title_span": [title_span[0], title_span[1]],
        "source_titleless": source_titleless,
        "raw_entry_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }


_GENERIC_NATIVE_RECORD_FIELDS = (
    "DOI", "type", "title", "subtitle", "author", "published-print",
    "published-online", "published", "issued", "container-title",
    "short-container-title", "volume", "issue", "page", "article-number",
)


def _generic_native_record_snapshot(msg: dict) -> dict | None:
    if not isinstance(msg, dict):
        return None
    snapshot = {
        key: msg[key] for key in _GENERIC_NATIVE_RECORD_FIELDS if key in msg
    }
    if snapshot.get("subtitle") is None or snapshot.get("subtitle") == []:
        snapshot.pop("subtitle", None)
    try:
        return json.loads(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        return None


def _generic_json_sha256(value: object) -> str | None:
    try:
        serialized = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _generic_native_locator_role(msg: dict, source_locator: str) -> tuple[str, str] | None:
    fields = ("article-number", "page")
    supplied = [
        (field, msg.get(field)) for field in fields
        if msg.get(field) is not None and msg.get(field) != ""
    ]
    if len(supplied) != 1:
        return None
    field, value = supplied[0]
    if not isinstance(value, str) or value != value.strip():
        return None
    if field == "article-number":
        return (field, value) if re.fullmatch(r"[0-9]+", value) and value == source_locator else None
    match = re.fullmatch(r"([0-9]+)\s*[-‐‑‒–—−]\s*([0-9]+)", value)
    if match is None:
        return None
    start, end = match.groups()
    if start != source_locator or int(end) < int(start):
        return None
    return field, value


def _generic_article_locator_profile(ref: dict, msg: dict, details: dict) -> dict:
    profile = _crossref_metadata_match_profile(ref, msg)
    if details.get("source_titleless"):
        profile["title_overlap"] = None
    source_span = tuple(details["spans"]["container"])
    if _titleless_coordinate_venue_alias(ref, msg) is None:
        witness = _ordered_native_venue_composition_witness(ref, msg, source_span)
        if witness is not None:
            profile[_TITLELESS_NATIVE_VENUE_COMPOSITION_KEY] = witness
    return profile


def _crossref_generic_article_locator_certificate(
    ref: dict,
    msg: dict,
    profile: dict | None = None,
    *,
    search_evidence: dict | None = None,
    stored_proof: dict | None = None,
) -> dict | None:
    """Prove one generic numeric source locator's native journal role."""
    details = _generic_article_locator_source_details(ref)
    if details is None or not isinstance(msg, dict):
        return None
    primary_values = msg.get("container-title")
    primary = primary_values[0] if isinstance(primary_values, list) and len(primary_values) == 1 else None
    if (
        msg.get("type") != "journal-article"
        or not _crossref_primary_title(msg)
        or not _crossref_doi_key(msg.get("DOI"))
        or not isinstance(primary, str)
        or not primary.strip()
        or _matching._fold_author(details["author"])
        != _matching._fold_author(_matching._crossref_first_author(msg))
        or _matching._crossref_year(msg) != details["year"]
    ):
        return None
    if profile is None or not isinstance(profile, dict):
        profile = _generic_article_locator_profile(ref, msg, details)
    if details["source_titleless"]:
        profile = dict(profile)
        profile["title_overlap"] = None
    if (
        profile.get("author_match") is not True
        or profile.get("year_match") is not True
        or profile.get("ordinal_conflict") is True
        or profile.get("metadata_conflict") is True
        or _matching._metadata_candidate_rejection_reason(
            ref, _crossref_complete_title(msg), profile,
            candidate_type=msg.get("type"),
        )
        or _matching._metadata_candidate_is_dataset_for_journal_article(
            ref, _crossref_primary_title(msg), candidate_type=msg.get("type"),
        )
        or _crossref_has_conflicting_cited_subtitle(ref, msg)
    ):
        return None
    rows_by_kind: dict[str, list[dict]] = {}
    for row in profile.get("coordinate_comparisons") or ():
        if isinstance(row, dict) and isinstance(row.get("kind"), str):
            rows_by_kind.setdefault(row["kind"], []).append(row)
    if (
        len(rows_by_kind.get("container", ())) != 1
        or len(rows_by_kind.get("volume", ())) != 1
        or len(rows_by_kind.get("article_locator", ())) != 1
        or (details["issue"] is not None and len(rows_by_kind.get("issue", ())) != 1)
        or rows_by_kind["volume"][0].get("status") != "match"
        or rows_by_kind["article_locator"][0].get("status") not in {"mismatch", "inconclusive"}
        or (details["issue"] is not None and rows_by_kind["issue"][0].get("status") != "match")
        or rows_by_kind["container"][0].get("cited_value") != details["container"]
        or profile.get("matched_venue") != primary.strip()
        or rows_by_kind["container"][0].get("matched_value") != primary.strip()
        or rows_by_kind["volume"][0].get("cited_value") != details["volume"]
        or rows_by_kind["volume"][0].get("matched_value") != msg.get("volume")
        or rows_by_kind["article_locator"][0].get("cited_value") != details["locator"]
        or rows_by_kind["article_locator"][0].get("matched_value")
        != (msg.get("page") if isinstance(msg.get("page"), str) and msg.get("page").strip() else None)
        or (
            details["issue"] is not None
            and (
                rows_by_kind["issue"][0].get("cited_value") != details["issue"]
                or rows_by_kind["issue"][0].get("matched_value") != msg.get("issue")
            )
        )
    ):
        return None
    native_volume = msg.get("volume")
    if (
        not isinstance(native_volume, str)
        or _matching._coordinate_comparison_value("volume", details["volume"])
        != _matching._coordinate_comparison_value("volume", native_volume)
    ):
        return None
    native_issue = msg.get("issue")
    if details["issue"] is not None and (
        not isinstance(native_issue, str)
        or _matching._coordinate_comparison_value("issue", details["issue"])
        != _matching._coordinate_comparison_value("issue", native_issue)
    ):
        return None
    role = _generic_native_locator_role(msg, details["locator"])
    if role is None:
        return None
    alias = _titleless_coordinate_venue_alias(ref, msg)
    venue_witness: dict
    if alias is not None:
        if alias:
            short_titles = msg.get("short-container-title")
            exact_aliases = [
                value for value in short_titles or ()
                if isinstance(value, str)
                and _coordinate_venue_key(value) == _coordinate_venue_key(alias)
            ]
            if (
                not isinstance(short_titles, list)
                or len(short_titles) != 1
                or len(exact_aliases) != 1
                or rows_by_kind["container"][0].get("status") == "mismatch"
            ):
                return None
        venue_witness = {"mode": "literal", "alias": alias}
    else:
        composition = _ordered_native_venue_composition_witness(
            ref, msg, tuple(details["spans"]["container"]),
        )
        container_row = rows_by_kind["container"][0]
        if (
            composition is None
            or profile.get(_TITLELESS_NATIVE_VENUE_COMPOSITION_KEY) != composition
        ):
            return None
        venue_witness = {"mode": "ordered_primary_short", **composition}
    snapshot = _generic_native_record_snapshot(msg)
    snapshot_digest = _generic_json_sha256(snapshot)
    if snapshot is None or snapshot_digest is None:
        return None
    proof = {
        "rule_version": _GENERIC_ARTICLE_LOCATOR_RULE,
        "raw_entry_sha256": details["raw_entry_sha256"],
        "source": {
            "author": details["author"],
            "year": details["year"],
            "container": details["container"],
            "volume": details["volume"],
            "issue": details["issue"],
            "locator": details["locator"],
            "spans": details["spans"],
            "title_span": details["title_span"],
            "source_titleless": details["source_titleless"],
        },
        "native_locator_field": role[0],
        "native_locator_value": role[1],
        "native_venue": {
            "primary": primary.strip(),
            "short": (
                msg["short-container-title"][0].strip()
                if isinstance(msg.get("short-container-title"), list)
                and len(msg["short-container-title"]) == 1
                and isinstance(msg["short-container-title"][0], str)
                else None
            ),
            **venue_witness,
        },
        "native_record_sha256": snapshot_digest,
        "native_record_snapshot": snapshot,
    }
    if search_evidence is not None:
        proof["search_evidence"] = search_evidence
    if stored_proof is not None and proof != stored_proof:
        return None
    return proof


def _generic_article_locator_query(details: dict) -> dict:
    return {
        "bibliographic": " ".join((
            str(details["year"]), details["container"], details["volume"],
            details["locator"],
        )),
        "author": details["author"],
        "year": details["year"],
    }


def _generic_article_locator_query_url(query: dict) -> str:
    params = {
        "query.bibliographic": query["bibliographic"],
        "rows": "1000",
        "query.author": query["author"],
        "filter": f"from-pub-date:{query['year']}-01-01,until-pub-date:{query['year']}-12-31",
    }
    return "https://api.crossref.org/works?" + urllib.parse.urlencode(params)


_REGISTERED_SCOPE_NATIVE_RECORD_FIELDS = (*_GENERIC_NATIVE_RECORD_FIELDS, "ISSN")


def _registered_titleless_scope_query(ref: dict, kind: str) -> dict | None:
    if kind == "generic":
        details = _generic_article_locator_source_details(ref)
        if details is None or details.get("source_titleless") is not True:
            return None
        author, year, volume, locator = (
            details["author"], details["year"], details["volume"], details["locator"],
        )
        locator_kind = "article_locator"
    elif kind == "coordinates":
        details = _titleless_coordinate_details(ref)
        if details is None:
            return None
        author, year, volume, locator, locator_kind = details
    else:
        return None
    return {
        "kind": kind,
        "author": author,
        "year": year,
        "volume": str(volume),
        "locator": str(locator),
        "locator_kind": locator_kind,
        "bibliographic": "",
    }


def _registered_titleless_scope_query_url(issn: str, query: dict) -> str:
    params: dict[str, str] = {}
    if query["bibliographic"]:
        params["query.bibliographic"] = query["bibliographic"]
    params.update({
        "rows": "1000",
        "query.author": query["author"],
        "filter": (
            f"from-pub-date:{query['year']}-01-01,"
            f"until-pub-date:{query['year']}-12-31"
        ),
    })
    path = "/journals/" + urllib.parse.quote(issn, safe="") + "/works?"
    return "https://api.crossref.org" + path + urllib.parse.urlencode(params)


def _registered_scope_native_record_snapshot(msg: dict) -> dict | None:
    if not isinstance(msg, dict):
        return None
    snapshot = {
        key: msg[key] for key in _REGISTERED_SCOPE_NATIVE_RECORD_FIELDS if key in msg
    }
    if snapshot.get("subtitle") is None or snapshot.get("subtitle") == []:
        snapshot.pop("subtitle", None)
    try:
        return json.loads(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        return None


def _registered_scope_native_identity_matches(
    msg: dict, source_scope: dict, queried_issn: str | None = None,
) -> bool:
    if not isinstance(msg, dict):
        return False
    containers = msg.get("container-title")
    primary = (
        containers[0] if isinstance(containers, list) and len(containers) == 1
        else None
    )
    raw_issns = msg.get("ISSN")
    candidate_issns = _normalized_crossref_issns(raw_issns)
    registered_issns = source_scope.get("issns")
    if not isinstance(raw_issns, list) or not raw_issns:
        return False
    if len(candidate_issns) != len(raw_issns):
        return False
    if not isinstance(registered_issns, list) or not registered_issns:
        return False
    from core.resolve.journal_authority import _text_key
    canonical = source_scope.get("canonical_title")
    return bool(
        isinstance(primary, str)
        and primary.strip()
        and isinstance(canonical, str)
        and _text_key(primary) == _text_key(canonical)
        and _crossref_doi_key(msg.get("DOI"))
        and isinstance(msg.get("type"), str)
        and _crossref_primary_title(msg)
        and set(candidate_issns).issubset(set(registered_issns))
        and set(candidate_issns).intersection(registered_issns)
        and (queried_issn is None or queried_issn in candidate_issns)
    )


def _revalidate_registered_titleless_scope_collection(
    ref: dict, evidence: dict, *, certificate_kind: str,
) -> tuple[list[dict], dict] | None:
    """Recheck a serialized all-ISSN source-scope collection and direct record."""
    required_keys = {
        "rule_version", "source_scope", "query", "scopes", "selected_doi",
        "selected_native_record_snapshot", "selected_native_record_sha256",
        "direct_native_record_snapshot", "direct_native_record_sha256",
    }
    if (
        not isinstance(evidence, dict)
        or set(evidence) != required_keys
        or evidence.get("rule_version") != _REGISTERED_SOURCE_SCOPE_SEARCH_RULE
        or certificate_kind not in {"coordinates", "generic"}
    ):
        return None
    query = _registered_titleless_scope_query(ref, certificate_kind)
    if query is None or evidence.get("query") != query:
        return None
    try:
        from core.resolve.journal_authority import _source_scoped_local_journal

        current_scope = _source_scoped_local_journal(ref)
    except Exception:
        return None
    if current_scope is None or evidence.get("source_scope") != current_scope:
        return None
    registered_issns = current_scope.get("issns")
    scopes = evidence.get("scopes")
    if (
        not isinstance(registered_issns, list)
        or not registered_issns
        or not isinstance(scopes, list)
        or len(scopes) != len(registered_issns)
    ):
        return None

    collection: list[tuple[str, dict]] = []
    certified_by_doi: dict[str, dict] = {}
    for expected_issn, scope in zip(registered_issns, scopes):
        if (
            not isinstance(scope, dict)
            or set(scope) != {
                "issn", "request_url", "complete", "result_total", "records",
                "records_sha256",
            }
            or scope.get("issn") != expected_issn
            or scope.get("request_url")
            != _registered_titleless_scope_query_url(expected_issn, query)
            or scope.get("complete") is not True
        ):
            return None
        records = scope.get("records")
        total = scope.get("result_total")
        if (
            not isinstance(records, list)
            or type(total) is not int
            or total != len(records)
            or total > 1000
            or _generic_json_sha256(records) != scope.get("records_sha256")
        ):
            return None
        for snapshot in records:
            if (
                not isinstance(snapshot, dict)
                or _registered_scope_native_record_snapshot(snapshot) != snapshot
                or not _registered_scope_native_identity_matches(
                    snapshot, current_scope, expected_issn,
                )
            ):
                return None
            doi_key = _crossref_doi_key(snapshot.get("DOI"))
            if not doi_key:
                return None
            collection.append((expected_issn, snapshot))
            if certificate_kind == "coordinates":
                profile = _crossref_metadata_match_profile(ref, snapshot)
                compatible = _crossref_titleless_coordinate_certificate(
                    ref, snapshot, profile,
                )
            else:
                details = _generic_article_locator_source_details(ref)
                if details is None:
                    return None
                profile = _generic_article_locator_profile(ref, snapshot, details)
                compatible = _crossref_generic_article_locator_certificate(
                    ref, snapshot, profile,
                ) is not None
            if compatible:
                previous = certified_by_doi.get(doi_key)
                if previous is not None and previous != snapshot:
                    return None
                certified_by_doi[doi_key] = snapshot

    if len(certified_by_doi) != 1:
        return None
    selected_doi, selected_snapshot = next(iter(certified_by_doi.items()))
    selected_record = evidence.get("selected_native_record_snapshot")
    selected_digest = evidence.get("selected_native_record_sha256")
    direct_record = evidence.get("direct_native_record_snapshot")
    direct_digest = evidence.get("direct_native_record_sha256")
    if (
        evidence.get("selected_doi") != selected_doi
        or selected_record != selected_snapshot
        or selected_digest != _generic_json_sha256(selected_record)
        or direct_record != selected_snapshot
        or direct_digest != _generic_json_sha256(direct_record)
        or not _registered_scope_native_identity_matches(direct_record, current_scope)
        or _crossref_doi_key(direct_record.get("DOI")) != selected_doi
    ):
        return None
    return [snapshot for _, snapshot in collection], current_scope


def _crossref_generic_article_locator_relock(
    ref: dict,
    evidence: dict,
    result: dict,
    attempts: list[dict] | None,
) -> bool:
    """Recompute the source/native generic-locator certificate after serialization."""
    resolved = result.get("resolved_identifier")
    result_profile = result.get("metadata_match")
    if (
        result.get("status") != "resolved"
        or result.get("via") != NAME
        or (evidence.get("resolution_basis") or result.get("resolution_basis")) != "metadata_search"
        or any(ref.get(key) for key in ("doi", "pmid", "isbn", "url"))
        or not isinstance(resolved, dict)
        or resolved.get("type") != "doi"
        or resolved.get("validated_via") != _GENERIC_ARTICLE_LOCATOR_VALIDATED_VIA
        or not isinstance(result_profile, dict)
    ):
        return False
    proof = result_profile.get(_GENERIC_ARTICLE_LOCATOR_CERTIFICATE_KEY)
    if not isinstance(proof, dict) or proof.get("rule_version") != _GENERIC_ARTICLE_LOCATOR_RULE:
        return False
    doi = _crossref_doi_key(result.get("doi"))
    if not doi or doi != _crossref_doi_key(resolved.get("value")):
        return False
    # The selected direct-validation attempt must retain the exact same proof.
    if not any(
        isinstance(attempt, dict)
        and attempt.get("status") == "resolved"
        and attempt.get("via") == NAME
        and attempt.get("resolution_basis") == "metadata_search"
        and _crossref_doi_key(attempt.get("doi")) == doi
        and isinstance(attempt.get("resolved_identifier"), dict)
        and attempt["resolved_identifier"].get("validated_via")
        == _GENERIC_ARTICLE_LOCATOR_VALIDATED_VIA
        and isinstance(attempt.get("metadata_match"), dict)
        and attempt["metadata_match"].get(_GENERIC_ARTICLE_LOCATOR_CERTIFICATE_KEY) == proof
        for attempt in attempts or ()
    ):
        return False
    details = _generic_article_locator_source_details(ref)
    search = proof.get("search_evidence")
    if details is None or not isinstance(search, dict):
        return False
    registered_scope_search = search.get("registered_source_scope")
    scoped_route = search.get("route")
    if registered_scope_search is not None or scoped_route is not None:
        if (
            set(search) != {"rule_version", "route", "registered_source_scope"}
            or search.get("rule_version") != _GENERIC_ARTICLE_LOCATOR_SEARCH_RULE
            or scoped_route != _REGISTERED_SOURCE_SCOPE_SEARCH_RULE
        ):
            return False
        revalidated = _revalidate_registered_titleless_scope_collection(
            ref, registered_scope_search, certificate_kind="generic",
        )
        if revalidated is None:
            return False
        records, _current_scope = revalidated
        selected_scope_record = registered_scope_search.get(
            "selected_native_record_snapshot",
        )
        if not isinstance(selected_scope_record, dict):
            return False
        projected_selected = _generic_native_record_snapshot(selected_scope_record)
        if projected_selected is None:
            return False
        selected_profile = _generic_article_locator_profile(
            ref, selected_scope_record, details,
        )
        selected_proof = _crossref_generic_article_locator_certificate(
            ref, selected_scope_record, selected_profile,
        )
        if (
            selected_proof is None
            or _crossref_doi_key(selected_scope_record.get("DOI")) != doi
        ):
            return False
        certified = [(projected_selected, selected_proof)]
    else:
        query = _generic_article_locator_query(details)
        records = search.get("records")
        if (
            search.get("rule_version") != _GENERIC_ARTICLE_LOCATOR_SEARCH_RULE
            or search.get("query") != query
            or search.get("request_url") != _generic_article_locator_query_url(query)
            or search.get("complete") is not True
            or not isinstance(records, list)
            or type(search.get("result_total")) is not int
            or search["result_total"] != len(records)
            or not 1 <= len(records) <= 1000
            or _generic_json_sha256(records) != search.get("records_sha256")
            or any(not isinstance(item, dict) for item in records)
        ):
            return False
        certified: list[tuple[dict, dict]] = []
        for snapshot in records:
            profile = _generic_article_locator_profile(ref, snapshot, details)
            candidate_proof = _crossref_generic_article_locator_certificate(
                ref, snapshot, profile,
            )
            if candidate_proof is not None:
                certified.append((snapshot, candidate_proof))
        if len(certified) != 1:
            return False
    if len(certified) != 1:
        return False
    direct_snapshot = proof.get("native_record_snapshot")
    direct_digest = proof.get("native_record_sha256")
    if (
        not isinstance(direct_snapshot, dict)
        or direct_snapshot != certified[0][0]
        or direct_digest != certified[0][1].get("native_record_sha256")
        or _generic_json_sha256(direct_snapshot) != direct_digest
        or _crossref_doi_key(direct_snapshot.get("DOI")) != doi
    ):
        return False
    profile = _generic_article_locator_profile(ref, direct_snapshot, details)
    profile_fields = (
        "title_overlap", "score", "author_match", "year_match",
        "year_mismatch_plausible", "matched_first_author", "cited_first_author",
        "matched_year", "matched_venue", "venue_overlap",
        "coordinate_comparisons", "ordinal_conflict", "metadata_conflict",
        _TITLELESS_NATIVE_VENUE_COMPOSITION_KEY,
    )
    if any(profile.get(field) != result_profile.get(field) for field in profile_fields):
        return False
    if (
        result.get("work_type") != direct_snapshot.get("type")
        or result.get("matched_title") != _crossref_complete_title(direct_snapshot)
        or result.get("matched_authors") != _matched_authors(direct_snapshot)
        or result.get("matched_venue") != profile.get("matched_venue")
    ):
        return False
    recomputed = _crossref_generic_article_locator_certificate(
        ref, direct_snapshot, profile,
        search_evidence=search,
        stored_proof=proof,
    )
    return recomputed is not None


def _resolve_generic_article_locator_doi(
    doi: str,
    ref: dict,
    search_evidence: dict,
    selected_search_proof: dict,
) -> dict:
    from core.resolve import service as resolve_mod

    expected = _crossref_doi_key(doi)
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi)
    try:
        status, body = resolve_mod._get(url)
        payload = json.loads(body)
        msg = payload.get("message") if isinstance(payload, dict) else None
        returned = _crossref_doi_key(msg.get("DOI")) if isinstance(msg, dict) else None
        details = _generic_article_locator_source_details(ref)
        if (
            status != 200 or not expected or returned != expected
            or details is None or not isinstance(msg, dict)
        ):
            return {
                "status": "unverified" if status == 200 else "unresolved",
                "via": "crossref",
                "reason": "direct Crossref record did not repeat the generic-locator identity certificate",
            }
        scoped_search_evidence = search_evidence
        registered_scope = search_evidence.get("registered_source_scope")
        if registered_scope is not None:
            scope_source = registered_scope.get("source_scope")
            direct_scope_snapshot = _registered_scope_native_record_snapshot(msg)
            selected_scope_snapshot = registered_scope.get(
                "selected_native_record_snapshot",
            )
            if (
                search_evidence.get("route") != _REGISTERED_SOURCE_SCOPE_SEARCH_RULE
                or not isinstance(scope_source, dict)
                or direct_scope_snapshot is None
                or not _registered_scope_native_identity_matches(msg, scope_source)
                or direct_scope_snapshot != selected_scope_snapshot
                or _crossref_doi_key(direct_scope_snapshot.get("DOI")) != expected
            ):
                return {
                    "status": "unverified",
                    "via": "crossref",
                    "reason": "direct Crossref record did not repeat the registered NLM ISSN and primary-title scope",
                }
            scoped_search_evidence = json.loads(
                json.dumps(search_evidence, ensure_ascii=False, sort_keys=True),
            )
            scoped_collection = scoped_search_evidence["registered_source_scope"]
            scoped_collection["direct_native_record_snapshot"] = direct_scope_snapshot
            scoped_collection["direct_native_record_sha256"] = _generic_json_sha256(
                direct_scope_snapshot,
            )
            if _revalidate_registered_titleless_scope_collection(
                ref, scoped_collection, certificate_kind="generic",
            ) is None:
                return {
                    "status": "unverified",
                    "via": "crossref",
                    "reason": "direct Crossref record failed the complete registered NLM scope proof",
                }
        profile = _generic_article_locator_profile(ref, msg, details)
        direct_proof = _crossref_generic_article_locator_certificate(ref, msg, profile)
        if (
            direct_proof is None
            or direct_proof.get("native_record_sha256")
            != selected_search_proof.get("native_record_sha256")
        ):
            return {
                "status": "unverified",
                "via": "crossref",
                "reason": "direct Crossref record differed from the unique generic-locator search candidate",
                "metadata_match": profile,
            }
        proof = dict(direct_proof)
        proof["search_evidence"] = scoped_search_evidence
        profile[_GENERIC_ARTICLE_LOCATOR_CERTIFICATE_KEY] = proof
        title = _crossref_complete_title(msg)
        relation = msg.get("relation") or {}
        retracted = "is-retracted-by" in relation or bool(
            title and title.strip().lower().startswith("retracted")
        )
        if not retracted:
            retracted = resolve_mod._rw.is_retracted(doi)
        out = {
            "status": "resolved",
            "via": NAME,
            "doi": returned,
            "matched_title": title,
            "matched_venue": profile.get("matched_venue"),
            "matched_authors": _matched_authors(msg),
            "work_type": msg.get("type"),
            "abstract": msg.get("abstract"),
            "retracted": retracted,
            "reason": "Crossref direct metadata repeated the unique generic-locator native-role certificate",
            "metadata_match": profile,
            "resolved_identifier": {
                "type": "doi",
                "value": returned,
                "validated_via": _GENERIC_ARTICLE_LOCATOR_VALIDATED_VIA,
            },
        }
        out.update(_fulltext_meta(msg))
        return out
    except urllib.error.HTTPError as exc:
        return {"status": "unresolved", "via": "crossref", "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {
            "status": "unresolved", "via": "crossref",
            "reason": f"network: {type(exc).__name__}",
        }


def _crossref_titleless_coordinate_certificate(
    ref: dict, msg: dict, profile: dict | None = None,
) -> bool:
    """Whether Crossref metadata proves a titleless cited article's full tuple."""
    from core.resolve import service as resolve_mod

    details = _titleless_coordinate_details(ref)
    if details is None or not isinstance(msg, dict):
        return False
    comparisons = profile
    if isinstance(comparisons, dict):
        if _REGISTERED_SOURCE_SCOPE_CERTIFICATE_KEY in comparisons:
            scope_proof = comparisons[_REGISTERED_SOURCE_SCOPE_CERTIFICATE_KEY]
            if not isinstance(scope_proof, dict):
                return False
            revalidated = _revalidate_registered_titleless_scope_collection(
                ref, scope_proof, certificate_kind="coordinates",
            )
            direct_snapshot = scope_proof.get("direct_native_record_snapshot")
            if (
                revalidated is None
                or not isinstance(direct_snapshot, dict)
                or _crossref_doi_key(msg.get("DOI"))
                != _crossref_doi_key(direct_snapshot.get("DOI"))
                or msg.get("type") != direct_snapshot.get("type")
                or _crossref_complete_title(msg)
                != _crossref_complete_title(direct_snapshot)
            ):
                return False
            direct_profile = _crossref_metadata_match_profile(ref, direct_snapshot)
            profile_fields = (
                "title_overlap", "score", "author_match", "year_match",
                "year_mismatch_plausible", "matched_first_author", "cited_first_author",
                "matched_year", "matched_venue", "venue_overlap",
                "coordinate_comparisons", "ordinal_conflict", "metadata_conflict",
                _TITLELESS_NATIVE_VENUE_COMPOSITION_KEY,
            )
            if any(
                direct_profile.get(field) != comparisons.get(field)
                for field in profile_fields
            ):
                return False
            msg = direct_snapshot
            comparisons = direct_profile
    author, year, cited_volume, cited_locator, locator_kind = details
    if (
        msg.get("type") != "journal-article"
        or not _crossref_primary_title(msg)
        or not _crossref_doi_key(msg.get("DOI"))
    ):
        return False
    authors = msg.get("author")
    first_author = authors[0] if isinstance(authors, list) and authors else None
    family = first_author.get("family") if isinstance(first_author, dict) else None
    if not isinstance(family, str) or _matching._fold_author(author) != _matching._fold_author(family):
        return False
    matched_year = _matching._crossref_year(msg)
    if matched_year != year:
        return False
    venue_alias = _titleless_coordinate_venue_alias(ref, msg)
    composition_witness = None
    if venue_alias is None:
        composition_witness = _titleless_native_venue_composition_witness(ref, msg)
        if composition_witness is None:
            return False

    if not isinstance(comparisons, dict):
        comparisons = _crossref_metadata_match_profile(ref, msg)
    if composition_witness is not None:
        container_rows = [
            row for row in comparisons.get("coordinate_comparisons") or ()
            if isinstance(row, dict) and row.get("kind") == "container"
        ]
        if (
            comparisons.get(_TITLELESS_NATIVE_VENUE_COMPOSITION_KEY)
            != composition_witness
            or comparisons.get("matched_venue") != composition_witness["native_primary"]
            or len(container_rows) != 1
            or container_rows[0].get("matched_value") != composition_witness["native_primary"]
            or container_rows[0].get("status") != "inconclusive"
        ):
            return False

    if venue_alias is None and composition_witness is None:
        return False
    if (
        comparisons.get("title_overlap") is not None
        or comparisons.get("author_match") is not True
        or comparisons.get("year_match") is not True
        or comparisons.get("ordinal_conflict") is True
        or comparisons.get("metadata_conflict") is True
    ):
        return False
    coordinate_rows = comparisons.get("coordinate_comparisons") or ()
    rows_by_kind: dict[str, list[dict]] = {}
    for row in coordinate_rows:
        if isinstance(row, dict) and isinstance(row.get("kind"), str):
            rows_by_kind.setdefault(row["kind"], []).append(row)
    if any(
        row.get("status") == "mismatch"
        for kind, rows in rows_by_kind.items() if kind != "container"
        for row in rows
    ):
        return False
    if (
        len(rows_by_kind.get("container", ())) != 1
        or len(rows_by_kind.get("volume", ())) != 1
        or len(rows_by_kind.get(locator_kind, ())) != 1
    ):
        return False
    if rows_by_kind["volume"][0].get("status") != "match":
        return False
    if rows_by_kind[locator_kind][0].get("status") != "match":
        return False

    metadata_volume = msg.get("volume")
    if not isinstance(metadata_volume, str) or _matching._coordinate_comparison_value(
        "volume", cited_volume,
    ) != _matching._coordinate_comparison_value("volume", metadata_volume):
        return False
    if locator_kind == "article_page_range":
        metadata_locator = msg.get("page")
    elif locator_kind == "elocator":
        metadata_locator = msg.get("article-number") or msg.get("page")
    else:
        metadata_locator = msg.get("article-number") or msg.get("page")
    if not isinstance(metadata_locator, str) or _matching._coordinate_comparison_value(
        locator_kind, cited_locator,
    ) != _matching._coordinate_comparison_value(locator_kind, metadata_locator):
        return False
    return True


def resolve_doi(doi: str, *, ref: dict | None = None) -> dict:
    """Look up a DOI while preserving the existing general lookup contract."""
    return _resolve_doi_impl(doi, ref=ref)


def _resolve_discovered_doi_exact(doi: str, *, ref: dict | None = None) -> dict:
    """Look up a metadata-discovered DOI and require its exact returned DOI."""
    return _resolve_doi_impl(
        doi, ref=ref, require_exact_returned_doi=True,
    )


def _resolve_doi_impl(
    doi: str,
    *,
    ref: dict | None = None,
    require_exact_returned_doi: bool = False,
) -> dict:
    from core.resolve import service as resolve_mod

    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
    titleless_coordinate_route = ref is not None and _titleless_coordinate_details(ref) is not None
    try:
        status, body = resolve_mod._get(url)
        payload = json.loads(body)
        msg = payload.get("message", {}) if isinstance(payload, dict) else {}
        if titleless_coordinate_route:
            requested_doi = resolve_mod._normalize_doi_value(doi)
            returned_doi = (
                resolve_mod._normalize_doi_value(msg.get("DOI"))
                if isinstance(msg, dict) else None
            )
            profile = (
                _crossref_metadata_match_profile(ref, msg)
                if isinstance(msg, dict) else None
            )
            if (
                status != 200
                or not requested_doi
                or returned_doi != requested_doi
                or profile is None
                or not _crossref_titleless_coordinate_certificate(ref, msg, profile)
            ):
                return {
                    "status": "unverified",
                    "via": "crossref",
                    "reason": "direct Crossref metadata did not repeat the complete titleless-coordinate certificate",
                    **({"metadata_match": profile} if profile is not None else {}),
                }
            title = _crossref_complete_title(msg)
            abstract = msg.get("abstract")
            venue_alias = _titleless_coordinate_venue_alias(ref, msg)
            if venue_alias:
                for comparison in profile.get("coordinate_comparisons") or ():
                    if isinstance(comparison, dict) and comparison.get("kind") == "container":
                        # Keep matched_venue as Crossref's primary container; this
                        # comparison row records the exact registered short title.
                        comparison["matched_value"] = venue_alias
                        comparison["status"] = "match"
            relation = msg.get("relation") or {}
            retracted = "is-retracted-by" in relation or bool(
                title and title.strip().lower().startswith("retracted")
            )
            if not retracted:
                retracted = resolve_mod._rw.is_retracted(doi)
            out = {
                "status": "resolved",
                "via": "crossref",
                "doi": returned_doi,
                "matched_title": title,
                "abstract": abstract,
                "retracted": retracted,
                "reason": "Crossref direct metadata repeated the complete titleless-coordinate certificate",
                "metadata_match": profile,
                "resolved_identifier": {
                    "type": "doi",
                    "value": returned_doi,
                    "validated_via": "crossref:complete_coordinate_certificate",
                },
            }
            matched_authors = _matched_authors(msg)
            if matched_authors:
                out["matched_authors"] = matched_authors
            out.update(_fulltext_meta(msg))
            return out
        ambiguous_boundary_route = (
            _ambiguous_boundary_search_source(ref) if ref is not None else None
        )
        if ambiguous_boundary_route is not None:
            requested_doi = _crossref_doi_key(doi)
            returned_doi = _crossref_doi_key(msg.get("DOI")) if isinstance(msg, dict) else None
            profile = _crossref_metadata_match_profile(ref, msg) if isinstance(msg, dict) else None
            source_proof = _ambiguous_boundary_article_proof(ref, msg)
            if (
                status != 200 or not requested_doi or returned_doi != requested_doi
                or source_proof is None
            ):
                return {
                    "status": "unverified" if status == 200 else "unresolved",
                    "via": "crossref",
                    "reason": (
                        "direct Crossref metadata did not repeat the complete ambiguous-title boundary proof"
                    ),
                    **({"metadata_match": profile} if profile is not None else {}),
                }
            native_work_proof = _crossref_ambiguous_boundary_work_proof(
                doi=requested_doi,
                request_url=url,
                http_status=status,
                response_body=body,
                raw_entry=ref.get("raw_entry"),
            )
            if native_work_proof is None:
                return {
                    "status": "unverified",
                    "via": "crossref",
                    "reason": "direct Crossref metadata could not be retained as a source-bound article proof",
                    "metadata_match": profile,
                }
            title = source_proof["title"]
            retracted = bool(
                "is-retracted-by" in (msg.get("relation") or {})
                or title.strip().lower().startswith("retracted")
            )
            if not retracted:
                retracted = resolve_mod._rw.is_retracted(requested_doi)
            out = {
                "status": "resolved",
                "via": "crossref",
                "doi": returned_doi,
                "matched_title": title,
                "matched_authors": _matched_authors(msg),
                "abstract": msg.get("abstract"),
                "retracted": retracted,
                "reason": "Crossref direct metadata repeated the complete source-bound ambiguous-title article proof",
                "metadata_match": profile,
                "resolved_identifier": {
                    "type": "doi",
                    "value": returned_doi,
                    "validated_via": _AMBIGUOUS_TITLE_JOURNAL_VALIDATED_VIA,
                },
                _CROSSREF_NATIVE_WORK_PROOF_FIELD: native_work_proof,
            }
            out.update(_fulltext_meta(msg))
            return out
        if require_exact_returned_doi and ref is not None and not ref.get("doi"):
            requested_doi = resolve_mod._normalize_doi_value(doi)
            returned_doi = (
                resolve_mod._normalize_doi_value(msg.get("DOI"))
                if isinstance(msg, dict) else None
            )
            if (
                status != 200
                or not requested_doi
                or not returned_doi
                or requested_doi.casefold() != returned_doi.casefold()
            ):
                return {
                    "status": "unverified",
                    "via": "crossref",
                    "reason": "direct Crossref response did not confirm the requested DOI",
                }
        title = _crossref_complete_title(msg)
        abstract = msg.get("abstract")
        relation = msg.get("relation") or {}
        retracted = "is-retracted-by" in relation or bool(
            title and title.strip().lower().startswith("retracted")
        )
        if not retracted:
            retracted = resolve_mod._rw.is_retracted(doi)
        primary_title_supported = bool(
            ref is not None and _crossref_primary_title_match_profile(ref, msg) is not None
        )
        out = {
            "status": "resolved",
            "via": "crossref",
            "matched_title": title,
            "abstract": abstract,
            "retracted": retracted,
            "reason": (
                "Crossref primary title matched with corroborating author, year, container, "
                "volume, and article locator"
                if primary_title_supported else None
            ),
        }
        matched_authors = _matched_authors(msg)
        if matched_authors:
            out["matched_authors"] = matched_authors
        if ref is not None:
            out["metadata_match"] = _crossref_metadata_match_profile(ref, msg)
        out.update(_fulltext_meta(msg))
        native_work_proof = _mint_crossref_native_work_proof(
            doi=doi,
            request_url=url,
            http_status=status,
            response_body=body,
        )
        if native_work_proof is not None:
            out[_CROSSREF_NATIVE_WORK_PROOF_FIELD] = native_work_proof
        return out
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "status": "not_found",
                "via": "crossref",
                "reason": "DOI not found on Crossref (404; hard identifier error)",
            }
        if exc.code == 429:
            return {
                "status": "unresolved",
                "via": "crossref",
                "reason": "rate_limited (HTTP 429) - NOT fabrication",
            }
        return {"status": "unresolved", "via": "crossref", "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {
            "status": "unresolved",
            "via": "crossref",
            "reason": f"network: {type(exc).__name__}",
        }


def _resolve_registered_titled_coordinate_doi(
    doi: str,
    ref: dict,
    authority: dict,
    registered_issns: tuple[str, ...],
) -> dict:
    """Fetch and independently repeat a registered titled-coordinate proof."""
    from core.resolve import service as resolve_mod

    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
    try:
        status, body = resolve_mod._get(url)
        payload = json.loads(body)
        msg = payload.get("message", {}) if isinstance(payload, dict) else {}
        requested_doi = resolve_mod._normalize_doi_value(doi)
        returned_doi = (
            resolve_mod._normalize_doi_value(msg.get("DOI"))
            if isinstance(msg, dict) else None
        )
        profile = (
            _crossref_metadata_match_profile(ref, msg)
            if isinstance(msg, dict) else None
        )
        proof = (
            _crossref_registered_titled_coordinate_certificate(
                ref, msg, profile, authority=authority,
                registered_issns=registered_issns,
            )
            if status == 200 and isinstance(msg, dict) and profile is not None
            and requested_doi and returned_doi == requested_doi else None
        )
        if proof is None or profile is None:
            return {
                "status": "unverified" if status == 200 else "unresolved",
                "via": "crossref",
                "reason": (
                    "direct Crossref metadata did not repeat the complete registered-coordinate certificate"
                    if status == 200 else f"direct Crossref metadata HTTP {status}"
                ),
                **({"metadata_match": profile} if profile is not None else {}),
            }
        profile = dict(profile)
        profile["registered_coordinate_certificate"] = proof
        title = _crossref_complete_title(msg)
        relation = msg.get("relation") or {}
        retracted = "is-retracted-by" in relation or bool(
            title and title.strip().lower().startswith("retracted")
        )
        if not retracted:
            retracted = resolve_mod._rw.is_retracted(doi)
        out = {
            "status": "resolved",
            "via": NAME,
            "doi": returned_doi,
            "matched_title": title,
            "matched_venue": profile.get("matched_venue"),
            "matched_authors": _matched_authors(msg),
            "work_type": msg.get("type"),
            "abstract": msg.get("abstract"),
            "retracted": retracted,
            "reason": "Crossref direct metadata repeated the complete registered-coordinate certificate",
            "metadata_match": profile,
            "resolved_identifier": {
                "type": "doi",
                "value": returned_doi,
                "validated_via": _REGISTERED_TITLED_COORDINATE_VALIDATION,
            },
        }
        out.update(_fulltext_meta(msg))
        return out
    except urllib.error.HTTPError as exc:
        return {"status": "unresolved", "via": "crossref", "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {
            "status": "unresolved",
            "via": "crossref",
            "reason": f"network: {type(exc).__name__}",
        }


def _resolve_registered_titleless_coordinate_doi(
    doi: str, ref: dict, scope_evidence: dict, selected_snapshot: dict,
) -> dict:
    """Fetch and repeat a titleless coordinate certificate inside its NLM scope."""
    from core.resolve import service as resolve_mod

    expected = _crossref_doi_key(doi)
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi)
    try:
        status, body = resolve_mod._get(url)
        payload = json.loads(body)
        msg = payload.get("message") if isinstance(payload, dict) else None
        returned = _crossref_doi_key(msg.get("DOI")) if isinstance(msg, dict) else None
        if status != 200 or not expected or returned != expected or not isinstance(msg, dict):
            return {
                "status": "unverified" if status == 200 else "unresolved",
                "via": "crossref",
                "reason": "direct Crossref metadata did not repeat the requested scoped DOI",
            }
        source_scope = scope_evidence.get("source_scope")
        direct_snapshot = _registered_scope_native_record_snapshot(msg)
        profile = _crossref_metadata_match_profile(ref, msg)
        if (
            not isinstance(source_scope, dict)
            or direct_snapshot is None
            or direct_snapshot != selected_snapshot
            or not _registered_scope_native_identity_matches(msg, source_scope)
            or not _crossref_titleless_coordinate_certificate(ref, msg, profile)
        ):
            return {
                "status": "unverified",
                "via": "crossref",
                "reason": "direct Crossref metadata did not repeat the registered titleless coordinate certificate",
                "metadata_match": profile,
            }
        proof = json.loads(json.dumps(scope_evidence, ensure_ascii=False, sort_keys=True))
        proof.update({
            "direct_native_record_snapshot": direct_snapshot,
            "direct_native_record_sha256": _generic_json_sha256(direct_snapshot),
        })
        if _revalidate_registered_titleless_scope_collection(
            ref, proof, certificate_kind="coordinates",
        ) is None:
            return {
                "status": "unverified",
                "via": "crossref",
                "reason": "direct Crossref metadata failed the complete registered NLM scope proof",
                "metadata_match": profile,
            }
        profile = dict(profile)
        profile[_REGISTERED_SOURCE_SCOPE_CERTIFICATE_KEY] = proof
        title = _crossref_complete_title(msg)
        relation = msg.get("relation") or {}
        retracted = "is-retracted-by" in relation or bool(
            title and title.strip().lower().startswith("retracted")
        )
        if not retracted:
            retracted = resolve_mod._rw.is_retracted(doi)
        result = {
            "status": "resolved",
            "via": NAME,
            "doi": returned,
            "matched_title": title,
            "matched_venue": profile.get("matched_venue"),
            "matched_authors": _matched_authors(msg),
            "work_type": msg.get("type"),
            "abstract": msg.get("abstract"),
            "retracted": retracted,
            "reason": "Crossref direct metadata repeated the complete titleless NLM-scoped coordinate certificate",
            "metadata_match": profile,
            "resolved_identifier": {
                "type": "doi",
                "value": returned,
                "validated_via": "crossref:complete_coordinate_certificate",
            },
        }
        result.update(_fulltext_meta(msg))
        return result
    except urllib.error.HTTPError as exc:
        return {"status": "unresolved", "via": "crossref", "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {
            "status": "unresolved",
            "via": "crossref",
            "reason": f"network: {type(exc).__name__}",
        }


def discover(ref: dict, *, identifier_fallback: bool = False) -> dict | None:
    from core.resolve import decision as decision_mod
    from core.resolve import service as resolve_mod

    title = resolve_mod._article_title_candidate(ref)
    raw = ref.get("raw_entry") or ""
    author = (ref.get("first_author") or "").casefold() or resolve_mod._first_author_key(raw)
    year = ref.get("year")
    search_urls: list[str] = []
    search_failures: list[str] = []
    search_result_complete: dict[str, bool] = {}
    search_result_total: dict[str, int | None] = {}
    registered_coordinate_search_used = False

    def _search(
        query_text: str, yr: int | None, *, rows: int = 5,
        author_filter: str | None = None, exact_year: bool = False,
        journal_issn: str | None = None,
    ) -> tuple[list[dict], str, bool, list[dict]]:
        nonlocal registered_coordinate_search_used
        params: dict[str, str] = {}
        if journal_issn is None or query_text:
            params["query.bibliographic"] = query_text
        params["rows"] = str(rows)
        if author_filter:
            params["query.author"] = author_filter
        year_applied = False
        if yr is not None:
            params["filter"] = (
                f"from-pub-date:{yr}-01-01,until-pub-date:{yr}-12-31"
                if exact_year else f"from-pub-date:{yr - 1},until-pub-date:{yr + 1}"
            )
            year_applied = True
        if journal_issn is not None:
            path = "/journals/" + urllib.parse.quote(journal_issn, safe="") + "/works?"
            registered_coordinate_search_used = True
        else:
            path = "/works?"
        url = "https://api.crossref.org" + path + urllib.parse.urlencode(params)
        search_urls.append(url)
        status, body = resolve_mod._get(url)
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            if status == 200:
                raise _MalformedCrossrefResponse(
                    "Crossref title search response is malformed"
                ) from exc
            raise
        message = payload.get("message") if isinstance(payload, dict) else None
        items = message.get("items") if isinstance(message, dict) else None
        if status != 200:
            raise urllib.error.HTTPError(
                url, status, f"Crossref title search HTTP {status}", None, None,
            )
        if not isinstance(message, dict) or not isinstance(items, list):
            raise _MalformedCrossrefResponse(
                "Crossref title search response is malformed"
            )
        total_results = message.get("total-results")
        search_result_total[url] = (
            total_results if type(total_results) is int and total_results >= 0 else None
        )
        search_result_complete[url] = (
            type(total_results) is int
            and total_results >= 0
            and total_results == len(items)
        )
        valid_items = []
        malformed_items = []
        for item in items:
            title_values = item.get("title") if isinstance(item, dict) else None
            if (
                isinstance(title_values, list)
                and title_values
                and isinstance(title_values[0], str)
                and title_values[0].strip()
            ):
                valid_items.append(item)
            else:
                malformed_items.append(item)
        return valid_items, url, year_applied, malformed_items

    query = (title or "")[:300]
    if author:
        query += f" {author}"
    year_filter_applied = False
    fallback_complete = True
    malformed_response_seen = False

    def identity_search(outcome: str) -> dict:
        contract = "Crossref bounded bibliographic title search"
        if registered_coordinate_search_used:
            contract += " plus complete registered NLM ISSN coordinate scopes"
        if search_urls:
            contract += "; issued request URLs: " + " | ".join(search_urls)
        if search_failures:
            contract += "; failed/incomplete requests: " + " | ".join(search_failures)
        incomplete = not fallback_complete or outcome == "inconclusive"
        return {
            "resolver": "crossref",
            "query_contract": contract,
            "completion": "incomplete" if incomplete else "complete",
            "outcome": "inconclusive" if incomplete else outcome,
        }

    def resolved_result_is_identity_locked(result: dict) -> bool:
        evidence = {
            "has_author": resolve_mod._has_author(ref),
            "has_year": bool(ref.get("year")),
        }
        return resolve_mod._resolved_identity_is_locked(ref, evidence, result, [])

    def resolved_result_needs_registered_coordinate_check(result: dict) -> bool:
        """Continue only when a provisional ordinary result is not identity-locked."""
        if (
            result.get("status") != "resolved"
            or _registered_titled_coordinate_details(ref) is None
        ):
            return False
        return not resolved_result_is_identity_locked(result)

    candidate_rejections: list[dict] = []
    malformed_candidate_rejections: list[dict] = []
    malformed_candidate_keys: set[str] = set()

    def record_malformed_candidates(items: list[dict]) -> None:
        nonlocal fallback_complete, malformed_response_seen
        if not items:
            return
        fallback_complete = False
        malformed_response_seen = True
        for item in items:
            doi_value = item.get("DOI") if isinstance(item, dict) else None
            key = str(doi_value or "").casefold()
            if key in malformed_candidate_keys:
                continue
            malformed_candidate_keys.add(key)
            rejected = {
                "status": "unverified",
                "via": NAME,
                "reason": "Crossref candidate record is malformed (missing or invalid title); candidate identity withheld",
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "identity_search": identity_search("inconclusive"),
            }
            if isinstance(doi_value, str) and doi_value.strip():
                rejected["doi"] = doi_value
            malformed_candidate_rejections.append(rejected)

    def malformed_envelope_result() -> dict:
        return {
            "status": "unresolved",
            "via": NAME,
            "reason": "Crossref title search response is malformed",
            "identity_search": identity_search("inconclusive"),
        }

    def candidate_rejection_reason(msg: dict, profile: dict) -> str | None:
        if not ref.get("doi") and msg.get("type") == "posted-content":
            # Reject a proven incompatible publication unit before ranking,
            # using the same withholding certificate as discovered admission.
            relation = decision_mod._discovered_doi_relation(
                ref, {"matched_title": _crossref_complete_title(msg)},
                {"work_type": msg.get("type"),
                 "matched_title": _crossref_complete_title(msg),
                 "metadata_match": profile},
            )
            if relation is not None and relation[0] == "incompatible":
                return relation[1] + "; candidate identity withheld"
        book_edition_reason = _crossref_discovered_book_edition_rejection_reason(
            ref, msg, profile,
        )
        if book_edition_reason:
            return book_edition_reason
        full_title_reason = _matching._metadata_candidate_rejection_reason(
            ref,
            _crossref_complete_title(msg),
            profile,
            candidate_type=msg.get("type"),
        )
        if full_title_reason:
            return full_title_reason
        if _matching._metadata_candidate_is_dataset_for_journal_article(
            ref,
            _crossref_primary_title(msg),
            candidate_type=msg.get("type"),
        ):
            return (
                "metadata candidate is a dataset record, not the cited journal article; "
                "candidate identity withheld"
            )
        return None

    def candidate_is_eligible(msg: dict, profile: dict) -> bool:
        return candidate_rejection_reason(msg, profile) is None

    def record_candidate_rejection(
        msg: dict, profile: dict, *, reason: str | None = None,
    ) -> None:
        if not ref.get("doi") and msg.get("type") == "posted-content":
            # The earlier generic-locator search can reject this same row
            # first. Retain its proven unit incompatibility in the ledger too.
            relation = decision_mod._discovered_doi_relation(
                ref, {"matched_title": _crossref_complete_title(msg)},
                {"work_type": msg.get("type"),
                 "matched_title": _crossref_complete_title(msg),
                 "metadata_match": profile},
            )
            if relation is not None and relation[0] == "incompatible":
                reason = relation[1] + "; candidate identity withheld"
        title_value = _crossref_complete_title(msg)
        doi_value = msg.get("DOI")
        rejection_key = (str(doi_value or "").casefold(), str(title_value or "").casefold())
        if any(
            (str(item.get("doi") or "").casefold(),
             str(item.get("matched_title") or "").casefold()) == rejection_key
            for item in candidate_rejections
        ):
            return
        rejected = {
            "status": "unverified",
            "via": NAME,
            "matched_title": title_value,
            "work_type": msg.get("type"),
            "reason": reason or candidate_rejection_reason(msg, profile)
            or "metadata candidate is incompatible with the cited work; candidate identity withheld",
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
            "metadata_match": profile,
            "identity_search": identity_search("candidate_incompatible"),
        }
        if isinstance(doi_value, str) and doi_value.strip():
            # This DOI names the rejected candidate in the attempt ledger only.
            rejected["doi"] = doi_value
        candidate_rejections.append(rejected)

    def with_candidate_rejections(result: dict) -> dict:
        all_rejections = candidate_rejections + malformed_candidate_rejections
        if all_rejections:
            # The service extracts these into ordinary typed attempts before
            # storing the selected result. Keep them on every post-filter exit.
            result["_candidate_rejections"] = all_rejections
        return result

    def search_registered_titleless_scopes(
        kind: str,
    ) -> tuple[dict | None, list[dict], bool]:
        """Collect every complete ISSN query for one source-spanned titleless tuple."""
        nonlocal fallback_complete, malformed_response_seen
        query = _registered_titleless_scope_query(ref, kind)
        if query is None:
            return None, [], False
        try:
            from core.resolve.journal_authority import _source_scoped_local_journal

            source_scope = _source_scoped_local_journal(ref)
        except Exception as exc:
            fallback_complete = False
            search_failures.append(
                f"registered NLM titleless source-scope check failed: {type(exc).__name__}"
            )
            return None, [], False
        if (
            not isinstance(source_scope, dict)
            or source_scope.get("status") != "recognized"
            or source_scope.get("registry") != "nlm-pubmed"
            or not isinstance(source_scope.get("issns"), list)
            or not source_scope["issns"]
        ):
            return None, [], False

        evidence = {
            "rule_version": _REGISTERED_SOURCE_SCOPE_SEARCH_RULE,
            "source_scope": source_scope,
            "query": query,
            "scopes": [],
        }
        all_items: list[dict] = []
        all_complete = True
        for issn in source_scope["issns"]:
            expected_url = _registered_titleless_scope_query_url(issn, query)
            scoped_items: list[dict] = []
            malformed_items: list[dict] = []
            query_error: str | None = None
            scoped_url: str | None = None
            try:
                scoped_items, scoped_url, _, malformed_items = _search(
                    query["bibliographic"],
                    query["year"],
                    rows=1000,
                    author_filter=query["author"],
                    exact_year=True,
                    journal_issn=issn,
                )
                record_malformed_candidates(malformed_items)
            except urllib.error.HTTPError as exc:
                query_error = f"HTTP {exc.code}"
            except _MalformedCrossrefResponse:
                query_error = "malformed response"
                malformed_response_seen = True
            except Exception as exc:
                query_error = type(exc).__name__
            if query_error:
                fallback_complete = False
                search_failures.append(
                    f"registered NLM ISSN titleless query failed for {issn}: {query_error}"
                )

            snapshots: list[dict] = []
            scope_wellformed = not malformed_items and query_error is None
            for item in scoped_items:
                snapshot = _registered_scope_native_record_snapshot(item)
                if (
                    snapshot is None
                    or not _registered_scope_native_identity_matches(
                        item, source_scope, issn,
                    )
                ):
                    scope_wellformed = False
                    record_candidate_rejection(
                        item,
                        _crossref_metadata_match_profile(ref, item),
                        reason=(
                            "registered NLM scope record did not repeat the same-entity "
                            "primary-title and ISSN evidence"
                        ),
                    )
                    continue
                snapshots.append(snapshot)
                all_items.append(item)
            total = search_result_total.get(expected_url)
            complete = bool(
                scoped_url == expected_url
                and search_result_complete.get(expected_url, False)
                and scope_wellformed
                and type(total) is int
                and total == len(scoped_items)
                and total <= 1000
            )
            if not complete:
                fallback_complete = False
                all_complete = False
                if not query_error:
                    search_failures.append(
                        f"registered NLM ISSN titleless query was incomplete or malformed: {issn}"
                    )
            evidence["scopes"].append({
                "issn": issn,
                "request_url": expected_url,
                "complete": complete,
                "result_total": total,
                "records": snapshots,
                "records_sha256": _generic_json_sha256(snapshots),
            })
        return evidence, all_items, all_complete

    def record_raw_candidates_without_title(items: list[dict]) -> None:
        for item in items:
            doi_value = item.get("DOI")
            matched_title = _crossref_complete_title(item)
            rejected = {
                "status": "unverified",
                "via": NAME,
                "reason": "raw-citation candidate withheld because no parsed cited title is available",
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "identity_search": identity_search("inconclusive"),
            }
            if matched_title:
                rejected["matched_title"] = matched_title
            if isinstance(doi_value, str) and doi_value.strip():
                rejected["doi"] = doi_value
            candidate_rejections.append(rejected)

    generic_details = _generic_article_locator_source_details(ref)
    if generic_details is not None:
        generic_query = _generic_article_locator_query(generic_details)
        items: list[dict] = []
        request_url: str | None = None
        query_error: str | None = None
        try:
            items, request_url, _, malformed_items = _search(
                generic_query["bibliographic"],
                generic_query["year"],
                rows=1000,
                author_filter=generic_query["author"],
                exact_year=True,
            )
            record_malformed_candidates(malformed_items)
        except urllib.error.HTTPError as exc:
            fallback_complete = False
            query_error = f"Crossref generic-locator query failed: HTTP {exc.code}"
        except _MalformedCrossrefResponse:
            fallback_complete = False
            malformed_response_seen = True
            query_error = "Crossref generic-locator query returned a malformed response"
        except Exception as exc:
            fallback_complete = False
            query_error = f"Crossref generic-locator query failed: {type(exc).__name__}"
        if query_error:
            search_failures.append(query_error)
        complete = bool(
            request_url
            and search_result_complete.get(request_url, False)
            and not malformed_response_seen
            and query_error is None
        )
        broad_total = search_result_total.get(request_url) if request_url else None
        broad_query_truncated = bool(
            request_url
            and query_error is None
            and not malformed_response_seen
            and type(broad_total) is int
            and broad_total > len(items)
        )
        snapshots = [
            _generic_native_record_snapshot(item) for item in items
        ] if complete else []
        if (
            complete
            and snapshots
            and all(isinstance(snapshot, dict) for snapshot in snapshots)
        ):
            search_evidence = {
                "rule_version": _GENERIC_ARTICLE_LOCATOR_SEARCH_RULE,
                "query": generic_query,
                "request_url": request_url,
                "complete": True,
                "result_total": len(items),
                "records": snapshots,
                "records_sha256": _generic_json_sha256(snapshots),
            }
            certified: list[tuple[dict, dict, dict]] = []
            for item in items:
                profile = _generic_article_locator_profile(
                    ref, item, generic_details,
                )
                proof = _crossref_generic_article_locator_certificate(
                    ref, item, profile,
                )
                if proof is not None:
                    certified.append((item, profile, proof))
                else:
                    record_candidate_rejection(
                        item,
                        profile,
                        reason=(
                            "Crossref result did not satisfy the unique generic-locator "
                            "native-role certificate"
                        ),
                    )
            if len(certified) == 1:
                selected_candidate, _selected_profile, selected_proof = certified[0]
                selected_doi = resolve_mod._normalize_doi_value(
                    selected_candidate.get("DOI"),
                )
                selected_snapshot = _generic_native_record_snapshot(selected_candidate)
                if (
                    selected_doi
                    and selected_snapshot == selected_proof.get("native_record_snapshot")
                ):
                    direct = _resolve_generic_article_locator_doi(
                        selected_doi,
                        ref,
                        search_evidence,
                        selected_proof,
                    )
                    if (
                        direct.get("status") == "resolved"
                        and (direct.get("resolved_identifier") or {}).get("validated_via")
                        == _GENERIC_ARTICLE_LOCATOR_VALIDATED_VIA
                        and _crossref_doi_key(direct.get("doi")) == _crossref_doi_key(selected_doi)
                    ):
                        fallback_complete = True
                        direct.update({
                            "via": NAME,
                            "resolution_basis": "metadata_search",
                            "existence_confidence": "medium",
                            "identity_search": identity_search("compatible_identity"),
                        })
                        return with_candidate_rejections(direct)
                    direct_reason = direct.get("reason")
                    if direct.get("status") == "unresolved":
                        fallback_complete = False
                    direct_failure_reason = (
                        "direct Crossref generic-locator validation failed: "
                        + str(direct_reason or "certificate was not repeated")
                    )
                    search_failures.append(direct_failure_reason)
                    if generic_details.get("source_titleless") is False:
                        record_candidate_rejection(
                            selected_candidate,
                            _selected_profile,
                            reason=direct_failure_reason,
                        )
            else:
                for item in items:
                    if not any(item is accepted[0] for accepted in certified):
                        profile = _generic_article_locator_profile(
                            ref, item, generic_details,
                        )
                        record_candidate_rejection(
                            item, profile,
                            reason=(
                                "Crossref result did not satisfy the unique generic-locator "
                                "native-role certificate"
                            ),
                        )
            fallback_complete = complete and fallback_complete
            generic_failure = {
                "status": "unverified",
                "via": NAME,
                "reason": (
                    "Crossref candidates did not yield one directly repeated generic-locator "
                    "native-role certificate"
                    + ("; " + "; ".join(search_failures) if search_failures else "")
                ),
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "identity_search": identity_search("candidate_incompatible"),
            }
            if generic_details.get("source_titleless") is not False:
                return with_candidate_rejections(generic_failure)
            # A titled source can still use the ordinary title matcher after the
            # stricter generic-locator certificate rejects its candidate. The
            # rejected generic attempt remains in candidate_rejections and in
            # the issued-search provenance attached to the eventual result.
        else:
            fallback_complete = False
            generic_failure = {
                "status": "unresolved" if query_error else "unverified",
                "via": NAME,
                "reason": query_error or (
                    "Crossref generic-locator query was incomplete or contained malformed records"
                ),
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "identity_search": identity_search("inconclusive"),
            }
            if (
                generic_details.get("source_titleless") is True
                and broad_query_truncated
            ):
                scope_collection, scope_items, scope_complete = (
                    search_registered_titleless_scopes("generic")
                )
                certified_by_doi: dict[str, tuple[dict, dict, dict, dict]] = {}
                conflicting_same_doi = False
                if scope_collection is not None and scope_complete:
                    for item in scope_items:
                        profile = _generic_article_locator_profile(
                            ref, item, generic_details,
                        )
                        proof = _crossref_generic_article_locator_certificate(
                            ref, item, profile,
                        )
                        if proof is None:
                            record_candidate_rejection(
                                item,
                                profile,
                                reason=(
                                    "Crossref registered NLM candidate did not satisfy the "
                                    "unique generic-locator native-role certificate"
                                ),
                            )
                            continue
                        doi_key = _crossref_doi_key(item.get("DOI"))
                        scope_snapshot = _registered_scope_native_record_snapshot(item)
                        if not doi_key or scope_snapshot is None:
                            conflicting_same_doi = True
                            continue
                        previous = certified_by_doi.get(doi_key)
                        if previous is not None and previous[3] != scope_snapshot:
                            conflicting_same_doi = True
                            continue
                        certified_by_doi.setdefault(
                            doi_key, (item, profile, proof, scope_snapshot),
                        )
                if (
                    scope_collection is not None
                    and scope_complete
                    and not conflicting_same_doi
                    and len(certified_by_doi) == 1
                ):
                    selected_doi, (
                        selected_candidate, selected_profile, selected_proof,
                        selected_scope_snapshot,
                    ) = next(iter(certified_by_doi.items()))
                    scope_collection.update({
                        "selected_doi": selected_doi,
                        "selected_native_record_snapshot": selected_scope_snapshot,
                        "selected_native_record_sha256": _generic_json_sha256(
                            selected_scope_snapshot,
                        ),
                    })
                    scoped_search_evidence = {
                        "rule_version": _GENERIC_ARTICLE_LOCATOR_SEARCH_RULE,
                        "route": _REGISTERED_SOURCE_SCOPE_SEARCH_RULE,
                        "registered_source_scope": scope_collection,
                    }
                    search_urls.append(
                        "https://api.crossref.org/works/"
                        + urllib.parse.quote(selected_doi)
                    )
                    direct = _resolve_generic_article_locator_doi(
                        selected_doi,
                        ref,
                        scoped_search_evidence,
                        selected_proof,
                    )
                    if (
                        direct.get("status") == "resolved"
                        and (direct.get("resolved_identifier") or {}).get("validated_via")
                        == _GENERIC_ARTICLE_LOCATOR_VALIDATED_VIA
                        and _crossref_doi_key(direct.get("doi")) == selected_doi
                    ):
                        fallback_complete = True
                        direct.update({
                            "via": NAME,
                            "resolution_basis": "metadata_search",
                            "existence_confidence": "medium",
                            "identity_search": identity_search("compatible_identity"),
                        })
                        return with_candidate_rejections(direct)
                    direct_reason = direct.get("reason")
                    search_failures.append(
                        "direct Crossref registered generic-locator validation failed: "
                        + str(direct_reason or "certificate was not repeated")
                    )
                    fallback_complete = False
                elif scope_complete and len(certified_by_doi) > 1:
                    search_failures.append(
                        "registered NLM ISSN generic-locator union returned multiple certified DOIs"
                    )
                elif conflicting_same_doi:
                    search_failures.append(
                        "registered NLM ISSN generic-locator union contained conflicting records for one DOI"
                    )
            if generic_details.get("source_titleless") is not False:
                generic_failure["reason"] += (
                    "; " + "; ".join(search_failures) if search_failures else ""
                )
                generic_failure["identity_search"] = identity_search("inconclusive")
                return with_candidate_rejections(generic_failure)
            # The failed query and its reason remain in search_failures; continue
            # only for the parsed title-bearing citation's ordinary search path.

    if not title:
        ambiguous_boundary_anchor = _ambiguous_boundary_search_source(ref)
        if ambiguous_boundary_anchor is not None:
            raw_query = ambiguous_boundary_anchor["raw_entry"][:600]
            if not raw_query:
                return with_candidate_rejections({
                    "status": "unverified",
                    "via": NAME,
                    "reason": "ambiguous title/journal source has no usable raw citation",
                    "identity_search": identity_search("inconclusive"),
                })
            raw_items: list[dict] = []
            raw_url: str | None = None
            query_error: str | None = None
            try:
                raw_items, raw_url, year_filter_applied, malformed_items = _search(
                    raw_query,
                    ambiguous_boundary_anchor["year"],
                    rows=1000,
                    author_filter=author or None,
                    exact_year=True,
                )
                record_malformed_candidates(malformed_items)
            except urllib.error.HTTPError as exc:
                fallback_complete = False
                query_error = f"ambiguous-boundary raw-citation query failed: HTTP {exc.code}"
                search_failures.append(query_error)
            except _MalformedCrossrefResponse:
                fallback_complete = False
                malformed_response_seen = True
                query_error = "ambiguous-boundary raw-citation query returned a malformed response"
                search_failures.append(query_error)
            except Exception as exc:
                fallback_complete = False
                query_error = f"ambiguous-boundary raw-citation query failed: {type(exc).__name__}"
                search_failures.append(query_error)

            raw_complete = bool(
                raw_url and search_result_complete.get(raw_url, False)
                and query_error is None and not malformed_response_seen
            )
            if not raw_complete:
                fallback_complete = False
                if raw_url and query_error is None and not malformed_response_seen:
                    search_failures.append(
                        "ambiguous-boundary raw-citation result set was incomplete"
                    )

            def ambiguous_boundary_identity_search(outcome: str) -> dict:
                item = identity_search(outcome)
                contract = "Crossref bounded exact-year raw-citation search"
                if search_urls:
                    contract += "; issued request URLs: " + " | ".join(search_urls)
                if search_failures:
                    contract += "; failed/incomplete requests: " + " | ".join(search_failures)
                item["query_contract"] = contract
                return item

            compatible_by_doi: dict[str, tuple[dict, dict]] = {}
            conflicting_search_proofs: set[str] = set()
            for candidate in raw_items:
                profile = _crossref_metadata_match_profile(ref, candidate)
                source_proof = _ambiguous_boundary_article_proof(ref, candidate)
                candidate_doi = _crossref_doi_key(candidate.get("DOI"))
                if source_proof is not None and candidate_doi:
                    previous = compatible_by_doi.get(candidate_doi)
                    if previous is None:
                        compatible_by_doi[candidate_doi] = (candidate, source_proof)
                    elif not _ambiguous_boundary_search_and_direct_proofs_agree(
                        previous[1], source_proof,
                    ):
                        conflicting_search_proofs.add(candidate_doi)
                else:
                    record_candidate_rejection(
                        candidate,
                        profile,
                        reason=(
                            "Crossref raw-citation candidate failed the exact source-bound "
                            "title, journal, author, year, volume, page, and ISSN proof"
                        ),
                    )

            if conflicting_search_proofs:
                fallback_complete = False
                search_failures.append(
                    "Crossref raw-citation search returned conflicting source proofs for one DOI"
                )
                for candidate in raw_items:
                    if _crossref_doi_key(candidate.get("DOI")) in conflicting_search_proofs:
                        record_candidate_rejection(
                            candidate,
                            _crossref_metadata_match_profile(ref, candidate),
                            reason=(
                                "Crossref returned conflicting native records for the same DOI; "
                                "source identity withheld"
                            ),
                        )

            if raw_complete and not conflicting_search_proofs and len(compatible_by_doi) == 1:
                selected_doi, (_selected_candidate, _selected_proof) = next(
                    iter(compatible_by_doi.items())
                )
                direct_url = (
                    "https://api.crossref.org/works/"
                    + urllib.parse.quote(selected_doi)
                )
                search_urls.append(direct_url)
                direct_result = _resolve_discovered_doi_exact(selected_doi, ref=ref)
                direct_source_proof = None
                direct_work_proof = direct_result.get(_CROSSREF_NATIVE_WORK_PROOF_FIELD)
                if _crossref_ambiguous_boundary_work_proof_is_valid(
                    direct_work_proof,
                    expected_doi=selected_doi,
                    raw_entry=ref.get("raw_entry"),
                ):
                    try:
                        direct_message = json.loads(
                            direct_work_proof["response_body"],
                        ).get("message")
                    except (TypeError, ValueError, AttributeError):
                        direct_message = None
                    direct_source_proof = _ambiguous_boundary_article_proof(
                        ref, direct_message,
                    )
                if (
                    direct_result.get("status") == "resolved"
                    and (direct_result.get("resolved_identifier") or {}).get("validated_via")
                    == _AMBIGUOUS_TITLE_JOURNAL_VALIDATED_VIA
                    and _crossref_doi_key(direct_result.get("doi")) == selected_doi
                    and _ambiguous_boundary_search_and_direct_proofs_agree(
                        _selected_proof, direct_source_proof,
                    )
                ):
                    direct_result.update({
                        "via": NAME,
                        "resolution_basis": "metadata_search",
                        "existence_confidence": "medium",
                        "identity_search": ambiguous_boundary_identity_search(
                            "compatible_identity",
                        ),
                    })
                    for candidate in raw_items:
                        if _crossref_doi_key(candidate.get("DOI")) == selected_doi:
                            continue
                        record_candidate_rejection(
                            candidate,
                            _crossref_metadata_match_profile(ref, candidate),
                            reason=(
                                "Crossref raw-citation candidate did not satisfy the unique "
                                "source-bound ambiguous-title article proof"
                            ),
                        )
                    return with_candidate_rejections(direct_result)
                if direct_result.get("status") == "unresolved":
                    fallback_complete = False
                    search_failures.append(
                        "direct ambiguous-boundary Crossref proof was inconclusive"
                    )
                elif direct_result.get("status") == "resolved":
                    search_failures.append(
                        "direct Crossref response proved a different source cut or journal identity"
                    )
                for candidate in raw_items:
                    if _crossref_doi_key(candidate.get("DOI")) != selected_doi:
                        continue
                    record_candidate_rejection(
                        candidate,
                        _crossref_metadata_match_profile(ref, candidate),
                        reason=(
                            "direct Crossref response failed the source-bound ambiguous-title "
                            "article proof; candidate identity withheld"
                        ),
                    )
            elif not conflicting_search_proofs and len(compatible_by_doi) > 1:
                for candidate, _proof in compatible_by_doi.values():
                    record_candidate_rejection(
                        candidate,
                        _crossref_metadata_match_profile(ref, candidate),
                        reason=(
                            "multiple distinct Crossref DOIs satisfy the ambiguous-title "
                            "source proof; candidate identity withheld"
                        ),
                    )
            elif compatible_by_doi and not conflicting_search_proofs:
                for candidate, _proof in compatible_by_doi.values():
                    record_candidate_rejection(
                        candidate,
                        _crossref_metadata_match_profile(ref, candidate),
                        reason=(
                            "Crossref raw-citation result set was incomplete; candidate "
                            "identity withheld"
                        ),
                    )

            outcome = "inconclusive" if not fallback_complete else "candidate_incompatible"
            reason = query_error or (
                "ambiguous-title raw-citation candidates failed unique direct Crossref proof"
                if raw_items else "ambiguous-title raw-citation search found no candidate"
            )
            if search_failures:
                reason += "; " + "; ".join(search_failures)
            failure_status = "unresolved" if query_error and "failed:" in query_error else "unverified"
            return with_candidate_rejections({
                "status": failure_status,
                "via": NAME,
                "reason": reason,
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "identity_search": ambiguous_boundary_identity_search(outcome),
            })

        raw_query = re.sub(r"\s+", " ", unicodedata.normalize("NFC", raw)).strip()[:600]
        if not raw_query:
            return {
                "status": "unverified",
                "via": NAME,
                "reason": "no usable title or raw citation for metadata search",
                "identity_search": identity_search("inconclusive"),
            }
        titleless_coordinate_route = _titleless_coordinate_details(ref)
        raw_items: list[dict] = []
        raw_url: str | None = None
        raw_query_error: str | None = None
        try:
            raw_items, raw_url, _, malformed_items = _search(raw_query, None)
            record_malformed_candidates(malformed_items)
        except urllib.error.HTTPError as exc:
            fallback_complete = False
            raw_query_error = f"raw-citation Crossref query failed: HTTP {exc.code}"
            search_failures.append(raw_query_error)
        except _MalformedCrossrefResponse:
            fallback_complete = False
            malformed_response_seen = True
            raw_query_error = "raw-citation Crossref query returned a malformed response"
            search_failures.append(raw_query_error)
        except Exception as exc:
            fallback_complete = False
            raw_query_error = f"raw-citation Crossref query failed: {type(exc).__name__}"
            search_failures.append(raw_query_error)

        if titleless_coordinate_route is not None:
            cited_author, cited_year, cited_volume, cited_locator, _locator_kind = titleless_coordinate_route
            raw_complete = bool(
                raw_url and search_result_complete.get(raw_url, False)
                and raw_query_error is None
            )
            raw_total = search_result_total.get(raw_url) if raw_url else None
            raw_query_truncated = bool(
                raw_url
                and raw_query_error is None
                and not malformed_response_seen
                and type(raw_total) is int
                and raw_total > len(raw_items)
            )
            raw_certified = [
                item for item in raw_items
                if _crossref_titleless_coordinate_certificate(
                    ref, item, _crossref_metadata_match_profile(ref, item),
                )
            ]
            raw_certified_dois = {
                _crossref_doi_key(item.get("DOI"))
                for item in raw_certified
                if _crossref_doi_key(item.get("DOI"))
            }
            candidate_items = list(raw_items)
            result_set_complete = raw_complete
            native_query_truncated = raw_query_truncated
            coordinate_search_needed = (
                not raw_certified_dois
                or (not raw_complete and len(raw_certified_dois) == 1)
            )
            if coordinate_search_needed:
                # A broad citation query may not return the complete candidate
                # set. Search the exact author/year/volume/locator tuple without
                # a title or venue term, and admit only a complete bounded page.
                coordinate_query = f"{cited_year} {cited_volume} {cited_locator}"
                coordinate_items: list[dict] = []
                coordinate_url: str | None = None
                coordinate_query_error: str | None = None
                try:
                    coordinate_items, coordinate_url, _, malformed_items = _search(
                        coordinate_query,
                        cited_year,
                        rows=1000,
                        author_filter=cited_author,
                        exact_year=True,
                    )
                    record_malformed_candidates(malformed_items)
                except urllib.error.HTTPError as exc:
                    fallback_complete = False
                    coordinate_items = []
                    coordinate_query_error = f"Crossref coordinate query failed: HTTP {exc.code}"
                except _MalformedCrossrefResponse:
                    fallback_complete = False
                    malformed_response_seen = True
                    coordinate_items = []
                    coordinate_query_error = "Crossref coordinate query returned a malformed response"
                except Exception as exc:
                    fallback_complete = False
                    coordinate_items = []
                    coordinate_query_error = f"Crossref coordinate query failed: {type(exc).__name__}"
                if coordinate_query_error:
                    search_failures.append(coordinate_query_error)
                coordinate_complete = bool(
                    coordinate_url and search_result_complete.get(coordinate_url, False)
                )
                coordinate_total = (
                    search_result_total.get(coordinate_url) if coordinate_url else None
                )
                native_query_truncated = bool(
                    coordinate_url
                    and coordinate_query_error is None
                    and not malformed_response_seen
                    and type(coordinate_total) is int
                    and coordinate_total > len(coordinate_items)
                )
                if not coordinate_complete and coordinate_query_error is None:
                    coordinate_query_error = "Crossref coordinate query result set was incomplete"
                    search_failures.append(coordinate_query_error)
                candidate_items.extend(coordinate_items)
                result_set_complete = coordinate_complete

            certified_by_doi: dict[str, dict] = {}
            for item in candidate_items:
                profile = _crossref_metadata_match_profile(ref, item)
                if not _crossref_titleless_coordinate_certificate(ref, item, profile):
                    continue
                doi_value = resolve_mod._normalize_doi_value(item.get("DOI"))
                if doi_value:
                    certified_by_doi.setdefault(doi_value, item)

            accepted_doi: str | None = None
            direct_failure: dict | None = None
            if (
                result_set_complete
                and not malformed_response_seen
                and len(certified_by_doi) == 1
            ):
                accepted_doi, selected_candidate = next(iter(certified_by_doi.items()))
                direct_url = (
                    "https://api.crossref.org/works/"
                    + urllib.parse.quote(accepted_doi)
                )
                search_urls.append(direct_url)
                direct_result = resolve_doi(accepted_doi, ref=ref)
                if (
                    direct_result.get("status") == "resolved"
                    and (direct_result.get("resolved_identifier") or {}).get("validated_via")
                    == "crossref:complete_coordinate_certificate"
                    and resolve_mod._normalize_doi_value(direct_result.get("doi")) == accepted_doi
                ):
                    fallback_complete = result_set_complete
                    direct_result.update({
                        "via": NAME,
                        "resolution_basis": "metadata_search",
                        "existence_confidence": "medium",
                        "identity_search": identity_search("compatible_identity"),
                    })
                    if malformed_response_seen:
                        direct_result["status"] = "unverified"
                        direct_result.pop("doi", None)
                        direct_result.pop("resolved_identifier", None)
                        direct_result.pop("abstract", None)
                        direct_result.pop("fulltext_links", None)
                        direct_result["reason"] = (
                            "Crossref response contained malformed candidate records; "
                            "titleless identity withheld"
                        )
                    else:
                        selected_alias = _titleless_coordinate_venue_alias(
                            ref,
                            selected_candidate,
                        )
                        direct_result["reason"] = (
                            "Crossref direct metadata repeated the complete titleless-coordinate certificate"
                            + (" using a registered short container title" if selected_alias else "")
                        )
                    for item in candidate_items:
                        item_doi = resolve_mod._normalize_doi_value(item.get("DOI"))
                        if item_doi == accepted_doi:
                            continue
                        record_candidate_rejection(
                            item,
                            _crossref_metadata_match_profile(ref, item),
                            reason=(
                                "Crossref candidate did not satisfy the complete titleless-coordinate "
                                "certificate; candidate identity withheld"
                            ),
                        )
                    return with_candidate_rejections(direct_result)
                direct_failure = direct_result
                direct_failure_reason = direct_result.get("reason")
                if isinstance(direct_failure_reason, str) and direct_failure_reason:
                    search_failures.append(
                        "direct Crossref metadata certificate failed: " + direct_failure_reason
                    )
                accepted_doi = None

            # Keep each retrieved Crossref row as a typed rejection when the
            # complete, unique, directly validated certificate was not met.
            seen_candidate_keys: set[tuple[str, str]] = set()
            for item in candidate_items:
                key = (
                    str(item.get("DOI") or "").casefold(),
                    str(_crossref_complete_title(item) or "").casefold(),
                )
                if key in seen_candidate_keys:
                    continue
                seen_candidate_keys.add(key)
                record_candidate_rejection(
                    item,
                    _crossref_metadata_match_profile(ref, item),
                    reason=(
                        "Crossref candidate did not satisfy the unique complete titleless-coordinate "
                        "certificate; candidate identity withheld"
                    ),
                )
            if direct_failure and direct_failure.get("status") == "unresolved":
                fallback_complete = False
                return with_candidate_rejections({
                    "status": "unresolved",
                    "via": NAME,
                    "reason": direct_failure.get("reason") or "direct Crossref validation was inconclusive",
                    "identity_search": identity_search("inconclusive"),
                })
            if raw_query_error and not candidate_items:
                return with_candidate_rejections({
                    "status": "unresolved",
                    "via": NAME,
                    "reason": raw_query_error,
                    "identity_search": identity_search("inconclusive"),
                })
            if native_query_truncated:
                scope_collection, scope_items, scope_complete = (
                    search_registered_titleless_scopes("coordinates")
                )
                scoped_certified: dict[str, tuple[dict, dict]] = {}
                conflicting_same_doi = False
                if scope_collection is not None and scope_complete:
                    for item in scope_items:
                        profile = _crossref_metadata_match_profile(ref, item)
                        if not _crossref_titleless_coordinate_certificate(
                            ref, item, profile,
                        ):
                            record_candidate_rejection(
                                item,
                                profile,
                                reason=(
                                    "Crossref registered NLM candidate did not satisfy the "
                                    "complete titleless-coordinate certificate"
                                ),
                            )
                            continue
                        doi_key = _crossref_doi_key(item.get("DOI"))
                        scope_snapshot = _registered_scope_native_record_snapshot(item)
                        if not doi_key or scope_snapshot is None:
                            conflicting_same_doi = True
                            continue
                        previous = scoped_certified.get(doi_key)
                        if previous is not None and previous[1] != scope_snapshot:
                            conflicting_same_doi = True
                            continue
                        scoped_certified.setdefault(doi_key, (item, scope_snapshot))
                if (
                    scope_collection is not None
                    and scope_complete
                    and not conflicting_same_doi
                    and len(scoped_certified) == 1
                ):
                    accepted_doi, (
                        selected_candidate, selected_scope_snapshot,
                    ) = next(iter(scoped_certified.items()))
                    scope_collection.update({
                        "selected_doi": accepted_doi,
                        "selected_native_record_snapshot": selected_scope_snapshot,
                        "selected_native_record_sha256": _generic_json_sha256(
                            selected_scope_snapshot,
                        ),
                    })
                    direct_url = (
                        "https://api.crossref.org/works/"
                        + urllib.parse.quote(accepted_doi)
                    )
                    search_urls.append(direct_url)
                    direct_result = _resolve_registered_titleless_coordinate_doi(
                        accepted_doi,
                        ref,
                        scope_collection,
                        selected_scope_snapshot,
                    )
                    if (
                        direct_result.get("status") == "resolved"
                        and (direct_result.get("resolved_identifier") or {}).get(
                            "validated_via"
                        ) == "crossref:complete_coordinate_certificate"
                        and _crossref_doi_key(direct_result.get("doi")) == accepted_doi
                    ):
                        fallback_complete = True
                        direct_result.update({
                            "via": NAME,
                            "resolution_basis": "metadata_search",
                            "existence_confidence": "medium",
                            "identity_search": identity_search("compatible_identity"),
                        })
                        return with_candidate_rejections(direct_result)
                    direct_failure_reason = direct_result.get("reason")
                    search_failures.append(
                        "direct Crossref registered titleless-coordinate validation failed: "
                        + str(direct_failure_reason or "certificate was not repeated")
                    )
                elif scope_complete and len(scoped_certified) > 1:
                    search_failures.append(
                        "registered NLM ISSN coordinate union returned multiple certified DOIs"
                    )
                elif conflicting_same_doi:
                    search_failures.append(
                        "registered NLM ISSN coordinate union contained conflicting records for one DOI"
                    )
            fallback_complete = result_set_complete and not malformed_response_seen
            final_reason = (
                "titleless citation candidates failed complete Crossref coordinate certification"
            )
            if search_failures:
                final_reason += "; " + "; ".join(search_failures)
            return with_candidate_rejections({
                "status": "unverified",
                "via": NAME,
                "reason": final_reason,
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "identity_search": identity_search("candidate_incompatible"),
            })

        if raw_query_error:
            return with_candidate_rejections({
                "status": "unresolved" if raw_query_error.startswith("raw-citation Crossref query failed") else "unverified",
                "via": NAME,
                "reason": raw_query_error,
                "identity_search": identity_search("inconclusive"),
            })
        record_raw_candidates_without_title(raw_items)
        return with_candidate_rejections({
            "status": "unverified", "via": NAME,
            "reason": (
                "raw-citation candidates retrieved but withheld because no parsed "
                "cited title is available"
                if raw_items else "no usable parsed title; raw-citation search found no candidate"
            ),
            "identity_search": identity_search("inconclusive"),
        })

    items: list[dict] = []
    year_filter_applied = False
    initial_search_failure = None
    try:
        items, _, year_filter_applied, malformed_items = _search(query, year)
        record_malformed_candidates(malformed_items)
        # A ranked search can return five irrelevant records while excluding the
        # real work because its online-first date falls outside the cited print
        # year window.  Retry without that window when the bounded result set has
        # no viable identity, rather than using the row count as a proxy.
        filtered_has_viable_identity = any(
            (
                profile.get("title_overlap") is not None
                and profile["title_overlap"] >= 0.85
                and (
                    profile.get("author_match") is True
                    or not author
                )
            )
            or profile.get("score", 0.0) >= resolve_mod.METADATA_VERIFY_MIN
            for msg in items
            for profile in [_crossref_metadata_match_profile(ref, msg)]
            if candidate_is_eligible(msg, profile)
        )
        if year is not None and not filtered_has_viable_identity:
            try:
                items_no_year, _, _, malformed_items = _search(query, None)
                record_malformed_candidates(malformed_items)
            except _MalformedCrossrefResponse:
                fallback_complete = False
                malformed_response_seen = True
                items_no_year = []
            except Exception:
                fallback_complete = False
                items_no_year = []
            seen_candidates = {
                (str(msg.get("DOI") or "").strip().casefold(),
                 _compact_citation_title(_crossref_complete_title(msg)))
                for msg in items
            }
            for msg in items_no_year:
                key = (
                    str(msg.get("DOI") or "").strip().casefold(),
                    _compact_citation_title(_crossref_complete_title(msg)),
                )
                if key not in seen_candidates:
                    seen_candidates.add(key)
                    items.append(msg)
            items = sorted(
                items,
                key=lambda msg: _crossref_metadata_match_profile(ref, msg).get("score", 0.0),
                reverse=True,
            )[:5]
            year_filter_applied = False
    except urllib.error.HTTPError as exc:
        fallback_complete = False
        initial_search_failure = f"Crossref title query failed: HTTP {exc.code}"
    except _MalformedCrossrefResponse:
        fallback_complete = False
        malformed_response_seen = True
        initial_search_failure = "Crossref title search response is malformed"
    except Exception as exc:
        fallback_complete = False
        initial_search_failure = f"network: {type(exc).__name__}"

    if not initial_search_failure and author and (
        not items
        or all(
            _crossref_metadata_match_profile(ref, msg)["score"]
            < 0.20
            for msg in items
        )
    ):
        try:
            items, _, _, malformed_items = _search(title[:300], year if year_filter_applied else None)
            record_malformed_candidates(malformed_items)
        except _MalformedCrossrefResponse:
            fallback_complete = False
            malformed_response_seen = True
        except Exception:
            fallback_complete = False

    def evaluate_items(pool_items: list[dict]) -> dict:
        unit_eligible_items = []
        for candidate_msg in pool_items:
            candidate_profile = _crossref_metadata_match_profile(ref, candidate_msg)
            if candidate_is_eligible(candidate_msg, candidate_profile):
                unit_eligible_items.append(candidate_msg)
            else:
                record_candidate_rejection(candidate_msg, candidate_profile)

        # Resolve applies the same first-author contradiction gate after all
        # providers have run. Apply it before choosing a Crossref winner too, so
        # a conflicting high-score candidate cannot suppress the raw-citation
        # retry. The only exception is the existing complete, unique correction
        # path, whose author membership was independently established.
        qualifying_corrections = []
        for candidate_msg in unit_eligible_items:
            candidate_title = _crossref_complete_title(candidate_msg)
            candidate_profile = _crossref_metadata_match_profile(ref, candidate_msg)
            if (candidate_msg.get("DOI") and resolve_mod.is_unique_same_work_correction_candidate(
                    ref, candidate_msg, candidate_title, candidate_profile)):
                qualifying_corrections.append(candidate_msg)
        unique_correction_ids = (
            {id(qualifying_corrections[0])}
            if fallback_complete and len(qualifying_corrections) == 1 else set()
        )

        supported_items = []
        for candidate_msg in unit_eligible_items:
            candidate_profile = _crossref_metadata_match_profile(ref, candidate_msg)
            if (
                id(candidate_msg) not in unique_correction_ids
                and _unsupported_title_fragment_with_year_conflict(
                    ref, candidate_msg, candidate_profile,
                )
            ):
                record_candidate_rejection(
                    candidate_msg,
                    candidate_profile,
                    reason=(
                        "metadata candidate title is only a fragment of the cited title "
                        "and its year conflicts without independent author or coordinate "
                        "support; candidate identity withheld"
                    ),
                )
            else:
                supported_items.append(candidate_msg)

        def would_resolve_without_author_conflict(candidate_msg: dict, profile: dict) -> bool:
            """Mirror the existing admission gates before prefiltering a winner."""
            candidate_title = _crossref_complete_title(candidate_msg)
            overlap = profile["title_overlap"]
            same_work_correction = id(candidate_msg) in unique_correction_ids
            short_title_fallback_match = resolve_mod._short_quoted_title_identifier_fallback_match(
                ref,
                candidate_title,
                profile,
                identifier_fallback=identifier_fallback,
            )
            strong_title_only = overlap is not None and overlap >= 0.85
            citation_title_equivalent = _citation_title_exactly_equivalent(ref, candidate_title)
            if _crossref_has_conflicting_cited_subtitle(ref, candidate_msg):
                return False
            if not (
                profile["score"] >= resolve_mod.METADATA_VERIFY_MIN
                or strong_title_only
                or citation_title_equivalent
                or short_title_fallback_match
                or same_work_correction
            ):
                return False
            correction_shaped = resolve_mod.is_same_work_correction_shape(
                ref, candidate_msg, candidate_title, profile
            )
            if (correction_shaped and profile.get("author_match") is False
                    and resolve_mod.requires_same_work_correction_author_gate(ref)
                    and not same_work_correction):
                return False
            year_disagrees = (
                ref.get("year") is not None
                and profile.get("matched_year") is not None
                and int(ref["year"]) != int(profile["matched_year"])
            )
            if (year_disagrees and not year_filter_applied
                    and not short_title_fallback_match
                    and (overlap is None or overlap < 0.85)):
                return False
            return True

        items = []
        for candidate_msg in supported_items:
            candidate_profile = _crossref_metadata_match_profile(ref, candidate_msg)
            if (
                resolve_mod._genuine_author_conflict(candidate_profile)
                and id(candidate_msg) not in unique_correction_ids
                and would_resolve_without_author_conflict(candidate_msg, candidate_profile)
            ):
                record_candidate_rejection(
                    candidate_msg,
                    candidate_profile,
                    reason=(
                        "title match contradicted by first author — likely a different work; "
                        "candidate identity withheld"
                    ),
                )
            else:
                items.append(candidate_msg)

        if not items:
            if candidate_rejections:
                rejected_with_doi = [item for item in candidate_rejections if item.get("doi")]
                result = {
                    "status": "unverified",
                    "via": NAME,
                    "reason": candidate_rejections[0]["reason"],
                    "resolution_basis": "metadata_search",
                    "existence_confidence": "low",
                    "identity_search": identity_search("candidate_incompatible"),
                }
                if rejected_with_doi:
                    # Match the selected-result schema: candidate_identifiers is a
                    # mapping, while the full rejected-candidate set lives in attempts.
                    result["candidate_identifiers"] = {"doi": rejected_with_doi[0]["doi"]}
                return with_candidate_rejections(result)
            if malformed_response_seen:
                return with_candidate_rejections({
                    "status": "unresolved",
                    "via": NAME,
                    "reason": "Crossref title search response contained malformed candidate record(s)",
                    "identity_search": identity_search("inconclusive"),
                })
            return with_candidate_rejections({
                "status": "unverified", "via": NAME, "reason": "no metadata candidate on Crossref",
                "identity_search": identity_search("no_compatible_identity"),
            })

        qualifying = []
        correction_shaped = False
        for candidate_msg in items:
            candidate_title = _crossref_complete_title(candidate_msg)
            candidate_profile = _crossref_metadata_match_profile(ref, candidate_msg)
            if (candidate_msg.get("DOI") and resolve_mod.is_unique_same_work_correction_candidate(
                    ref, candidate_msg, candidate_title, candidate_profile)):
                qualifying.append((candidate_msg, candidate_profile))
            if resolve_mod.is_same_work_correction_shape(
                    ref, candidate_msg, candidate_title, candidate_profile):
                correction_shaped = True

        best_msg = None
        best_profile = None
        best_score = -1.0
        for msg in items:
            matched_title = _crossref_complete_title(msg)
            profile = _crossref_metadata_match_profile(ref, msg)
            if profile["score"] > best_score:
                best_score = profile["score"]
                best_msg = msg
                best_profile = profile

        same_work_correction = fallback_complete and len(qualifying) == 1
        if same_work_correction:
            msg, profile = qualifying[0]
        else:
            msg, profile = best_msg, best_profile
        matched_title = _crossref_complete_title(msg)
        overlap = profile["title_overlap"]
        terminal_range_dois = {
            doi_key
            for candidate_msg in items
            if _matching._crossref_terminal_numeric_title_range_identity_supported(
                ref, candidate_msg,
            )
            and (doi_key := _crossref_doi_key(candidate_msg.get("DOI")))
        }
        terminal_range_title_supported = bool(
            terminal_range_dois
            and len(terminal_range_dois) == 1
            and _crossref_doi_key(msg.get("DOI")) in terminal_range_dois
            and _matching._crossref_terminal_numeric_title_range_identity_supported(
                ref, msg,
            )
        )
        primary_title_supported = _crossref_primary_title_match_profile(ref, msg) is not None
        if _crossref_has_conflicting_cited_subtitle(ref, msg):
            return with_candidate_rejections({
                "status": "unverified",
                "via": NAME,
                "matched_title": matched_title,
                "reason": (
                    "metadata candidate subtitle conflicts with the cited article title; "
                    "candidate identity withheld"
                ),
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "metadata_match": profile,
                "identity_search": identity_search("candidate_incompatible"),
            })
        short_title_fallback_match = resolve_mod._short_quoted_title_identifier_fallback_match(
            ref,
            matched_title,
            profile,
            identifier_fallback=identifier_fallback,
        )
        strong_title_only = overlap is not None and overlap >= 0.85
        citation_title_equivalent = _citation_title_exactly_equivalent(ref, matched_title)
        year_disagrees = (
            ref.get("year") is not None
            and profile.get("matched_year") is not None
            and int(ref["year"]) != int(profile["matched_year"])
        )
        if not (
            profile["score"] >= resolve_mod.METADATA_VERIFY_MIN
            or strong_title_only
            or citation_title_equivalent
            or short_title_fallback_match
            or same_work_correction
            or terminal_range_title_supported
        ):
            weak_result = {
                "status": "unverified",
                "via": NAME,
                "matched_title": matched_title,
                "reason": (
                    "metadata candidate below confidence threshold "
                    f"(overlap {overlap}, score {profile['score']})"
                ),
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "metadata_match": profile,
                "identity_search": identity_search("candidate_incompatible"),
            }
            candidate_doi = msg.get("DOI")
            if (
                isinstance(candidate_doi, str)
                and candidate_doi.strip()
                and profile.get("author_match") is True
                and profile.get("year_match") is True
                and profile.get("ordinal_conflict") is not True
                and profile.get("metadata_conflict") is not True
            ):
                candidate_doi = candidate_doi.strip()
                weak_result["candidate_identifiers"] = {"doi": candidate_doi}
                # Keep the selected DOI and its weak Crossref profile as a typed
                # provider attempt. It remains a lookup lead, not an identity.
                record_candidate_rejection(
                    msg,
                    profile,
                    reason=(
                        "Crossref candidate is below the metadata confidence threshold; "
                        "DOI retained only for independent metadata corroboration"
                    ),
                )
            return with_candidate_rejections(weak_result)
        if (correction_shaped and profile.get("author_match") is False
                and resolve_mod.requires_same_work_correction_author_gate(ref)
                and not same_work_correction):
            return with_candidate_rejections({
                "status": "unverified", "via": NAME, "matched_title": matched_title,
                "reason": "same-work correction candidate is not unique or lacks verified author membership",
                "resolution_basis": "metadata_search", "existence_confidence": "low",
                "metadata_match": profile,
                "identity_search": identity_search("candidate_incompatible"),
            })
        if (year_disagrees and not year_filter_applied
                and not short_title_fallback_match
                and (overlap is None or overlap < 0.85)):
            return with_candidate_rejections({
                "status": "unverified",
                "via": NAME,
                "matched_title": matched_title,
                "reason": (
                    "metadata candidate has wrong year "
                    f"(cited {ref['year']}, matched {profile['matched_year']}, overlap {overlap})"
                ),
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "metadata_match": profile,
                "identity_search": identity_search("candidate_incompatible"),
            })
        relation = msg.get("relation") or {}
        retracted = "is-retracted-by" in relation or bool(
            matched_title and matched_title.strip().lower().startswith("retracted")
        )
        if not retracted and msg.get("DOI"):
            retracted = resolve_mod._rw.is_retracted(str(msg["DOI"]))
        out = {
            "status": "resolved",
            "via": NAME,
            "matched_title": matched_title,
            "abstract": msg.get("abstract"),
            "retracted": retracted,
            "reason": (
                "Crossref primary title matched with corroborating author, year, container, "
                "volume, and article locator"
                if primary_title_supported else (
                    "metadata search match via strict short-title identifier fallback"
                    if short_title_fallback_match else (
                        "metadata search match via source-closed terminal numeric title range"
                        if terminal_range_title_supported else "metadata search match"
                    )
                )
            ),
            "resolution_basis": "metadata_search",
            "existence_confidence": "medium",
            "metadata_match": profile,
        }
        out["identity_search"] = identity_search("compatible_identity")
        if malformed_response_seen:
            out["reason"] += "; Crossref response contained malformed candidate record(s)"
        authors = _matched_authors(msg)
        if authors:
            out["matched_authors"] = authors
        if same_work_correction:
            out["reason"] = "unique Crossref same-work correction candidate"
            out["resolved_identifier"] = {
                "type": "doi", "value": msg["DOI"],
                "validated_via": "crossref:unique_same_work_correction",
            }
        out.update(_fulltext_meta(msg))
        return with_candidate_rejections(out)

    candidate_pool = list(items)
    first_result = evaluate_items(candidate_pool)
    if (
        first_result.get("status") == "resolved"
        and not resolved_result_needs_registered_coordinate_check(first_result)
    ):
        return first_result
    raw_query = re.sub(r"\s+", " ", unicodedata.normalize("NFC", raw)).strip()[:600]
    if not raw_query:
        return first_result
    raw_failure_reason = None
    try:
        raw_items, _, _, malformed_items = _search(raw_query, None)
        record_malformed_candidates(malformed_items)
    except urllib.error.HTTPError as exc:
        fallback_complete = False
        raw_failure_reason = f"raw-citation Crossref query failed: HTTP {exc.code}"
        raw_items = []
    except _MalformedCrossrefResponse:
        fallback_complete = False
        malformed_response_seen = True
        raw_failure_reason = "raw-citation Crossref query returned a malformed response"
        raw_items = []
    except Exception as exc:
        fallback_complete = False
        raw_failure_reason = f"raw-citation Crossref query failed: {type(exc).__name__}"
        raw_items = []
    for rejected in candidate_rejections + malformed_candidate_rejections:
        if isinstance(rejected.get("identity_search"), dict):
            previous_outcome = rejected["identity_search"].get("outcome")
            rejected["identity_search"] = identity_search(
                previous_outcome if isinstance(previous_outcome, str) else "inconclusive"
            )
    pool_keys = {
        (str(msg.get("DOI") or "").strip().casefold(),
         _compact_citation_title(_crossref_complete_title(msg)))
        for msg in candidate_pool
    }
    for msg in raw_items:
        key = (str(msg.get("DOI") or "").strip().casefold(),
               _compact_citation_title(_crossref_complete_title(msg)))
        if key not in pool_keys:
            pool_keys.add(key)
            candidate_pool.append(msg)
    final_result = evaluate_items(candidate_pool)
    if raw_failure_reason:
        if not candidate_pool:
            final_result["status"] = "unresolved"
        final_result["reason"] = "; ".join(filter(None, (
            final_result.get("reason"), raw_failure_reason,
        )))
        final_result["identity_search"] = identity_search("inconclusive")
    if initial_search_failure:
        final_result["reason"] = "; ".join(filter(None, (
            final_result.get("reason"), initial_search_failure,
        )))

    # A resolved ordinary row may still be provisional: only continue it when
    # the shared identity-lock predicate withholds it and the source has a
    # complete registered article tuple.  The NLM route searches complete
    # registered ISSN scopes and independently refetches one source-proved row.
    titled_details = _registered_titled_coordinate_details(ref)
    if final_result.get("status") == "resolved" and (
        titled_details is None
        or resolved_result_is_identity_locked(final_result)
    ):
        return final_result
    if titled_details is None:
        return final_result

    def refresh_candidate_search_provenance() -> None:
        for rejected in candidate_rejections + malformed_candidate_rejections:
            if not isinstance(rejected.get("identity_search"), dict):
                continue
            previous_outcome = rejected["identity_search"].get("outcome")
            rejected["identity_search"] = identity_search(
                "inconclusive" if search_failures
                else previous_outcome if isinstance(previous_outcome, str)
                else "candidate_incompatible"
            )

    from core.resolve.journal_authority import assess_local_journal, issns_for_record

    try:
        authority = assess_local_journal(ref)
        if (
            not isinstance(authority, dict)
            or authority.get("status") != "recognized"
            or authority.get("registry") != "nlm-pubmed"
        ):
            return final_result
        registered_issns = tuple(issns_for_record(authority))
    except Exception as exc:
        fallback_complete = False
        failure = f"registered NLM coordinate authority check failed: {type(exc).__name__}"
        search_failures.append(failure)
        final_result["reason"] = "; ".join(filter(None, (final_result.get("reason"), failure)))
        final_result["identity_search"] = identity_search("inconclusive")
        refresh_candidate_search_provenance()
        return with_candidate_rejections(final_result)
    if not registered_issns:
        fallback_complete = False
        failure = "registered NLM coordinate authority record has no ISSNs"
        search_failures.append(failure)
        final_result["reason"] = "; ".join(filter(None, (final_result.get("reason"), failure)))
        final_result["identity_search"] = identity_search("inconclusive")
        refresh_candidate_search_provenance()
        return with_candidate_rejections(final_result)

    cited_author, cited_year, cited_volume, cited_locator, _locator_kind = titled_details
    scope_items: list[dict] = []
    certified_by_doi: dict[str, dict] = {}
    all_scopes_complete = True
    malformed_scope_seen = False
    for issn in registered_issns:
        scoped_items: list[dict] = []
        try:
            scoped_items, scoped_url, _, malformed_items = _search(
                f"{cited_volume} {cited_locator}",
                cited_year,
                rows=1000,
                author_filter=cited_author,
                exact_year=True,
                journal_issn=issn,
            )
            scope_items.extend(scoped_items)
            if malformed_items:
                record_malformed_candidates(malformed_items)
                malformed_scope_seen = True
                all_scopes_complete = False
                search_failures.append(
                    f"registered NLM ISSN coordinate query contained malformed records: {issn}"
                )
            if not search_result_complete.get(scoped_url, False):
                all_scopes_complete = False
                search_failures.append(
                    f"registered NLM ISSN coordinate query result set was incomplete: {issn}"
                )
        except urllib.error.HTTPError as exc:
            all_scopes_complete = False
            search_failures.append(
                f"registered NLM ISSN coordinate query failed for {issn}: HTTP {exc.code}"
            )
        except _MalformedCrossrefResponse:
            all_scopes_complete = False
            malformed_scope_seen = True
            search_failures.append(
                f"registered NLM ISSN coordinate query returned a malformed response: {issn}"
            )
        except Exception as exc:
            all_scopes_complete = False
            search_failures.append(
                f"registered NLM ISSN coordinate query failed for {issn}: {type(exc).__name__}"
            )

        for scoped_item in scoped_items:
            doi_key = _crossref_doi_key(scoped_item.get("DOI"))
            candidate_issns = _normalized_crossref_issns(scoped_item.get("ISSN"))
            if (
                not _crossref_primary_title(scoped_item)
                or doi_key is None
                or not candidate_issns
                or not isinstance(scoped_item.get("type"), str)
            ):
                malformed_scope_seen = True
                all_scopes_complete = False
                fallback_complete = False
                search_failures.append(
                    f"registered NLM ISSN coordinate query contained an incomplete candidate: {issn}"
                )
                record_malformed_candidates([scoped_item])
                if not any(
                    item.get("doi") == scoped_item.get("DOI")
                    and "registered NLM scope candidate" in str(item.get("reason") or "")
                    for item in malformed_candidate_rejections
                ):
                    malformed_rejection = {
                        "status": "unverified",
                        "via": NAME,
                        "reason": (
                            "registered NLM scope candidate is malformed or lacks a "
                            "complete DOI, title, work type, or ISSN record"
                        ),
                        "resolution_basis": "metadata_search",
                        "existence_confidence": "low",
                        "identity_search": identity_search("inconclusive"),
                    }
                    title_value = _crossref_complete_title(scoped_item)
                    if title_value:
                        malformed_rejection["matched_title"] = title_value
                    if isinstance(scoped_item.get("DOI"), str) and scoped_item["DOI"].strip():
                        malformed_rejection["doi"] = scoped_item["DOI"]
                    malformed_candidate_rejections.append(malformed_rejection)
                continue
            profile = _crossref_metadata_match_profile(ref, scoped_item)
            try:
                certificate = _crossref_registered_titled_coordinate_certificate(
                    ref,
                    scoped_item,
                    profile,
                    authority=authority,
                    registered_issns=registered_issns,
                    queried_issn=issn,
                )
            except Exception as exc:
                all_scopes_complete = False
                search_failures.append(
                    f"registered NLM ISSN candidate validation failed: {type(exc).__name__}"
                )
                certificate = None
            if certificate is None:
                record_candidate_rejection(
                    scoped_item,
                    profile,
                    reason=(
                        "Crossref candidate did not satisfy the registered NLM full-coordinate "
                        "certificate; candidate identity withheld"
                    ),
                )
                continue
            certified_by_doi.setdefault(doi_key, scoped_item)

    if (
        all_scopes_complete
        and not malformed_scope_seen
        and len(certified_by_doi) == 1
    ):
        accepted_doi = next(iter(certified_by_doi))
        direct_url = (
            "https://api.crossref.org/works/" + urllib.parse.quote(accepted_doi)
        )
        search_urls.append(direct_url)
        direct_result = _resolve_registered_titled_coordinate_doi(
            accepted_doi, ref, authority, registered_issns,
        )
        if (
            direct_result.get("status") == "resolved"
            and (direct_result.get("resolved_identifier") or {}).get("validated_via")
            == _REGISTERED_TITLED_COORDINATE_VALIDATION
            and _crossref_doi_key(direct_result.get("doi")) == accepted_doi
        ):
            direct_result.update({
                "resolution_basis": "metadata_search",
                "existence_confidence": "medium",
                "identity_search": identity_search("compatible_identity"),
            })
            for scoped_item in scope_items:
                if _crossref_doi_key(scoped_item.get("DOI")) == accepted_doi:
                    continue
                record_candidate_rejection(
                    scoped_item,
                    _crossref_metadata_match_profile(ref, scoped_item),
                    reason=(
                        "Crossref candidate did not satisfy the unique registered NLM "
                        "coordinate certificate; candidate identity withheld"
                    ),
                )
            refresh_candidate_search_provenance()
            return with_candidate_rejections(direct_result)
        direct_reason = direct_result.get("reason")
        if isinstance(direct_reason, str) and direct_reason:
            search_failures.append(
                "direct Crossref registered-coordinate certificate failed: " + direct_reason
            )
    elif not all_scopes_complete or malformed_scope_seen:
        fallback_complete = False
    elif len(certified_by_doi) > 1:
        search_failures.append(
            "registered NLM ISSN coordinate search returned multiple certified DOIs"
        )

    final_result["reason"] = "; ".join(filter(None, (
        final_result.get("reason"),
        "registered NLM coordinate search did not establish one complete direct certificate",
        "; ".join(search_failures),
    )))
    final_result["identity_search"] = identity_search(
        "inconclusive" if search_failures or not all_scopes_complete
        else "candidate_incompatible"
    )
    refresh_candidate_search_provenance()
    return with_candidate_rejections(final_result)
