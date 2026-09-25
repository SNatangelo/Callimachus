#!/usr/bin/env python3
# core/resolve/providers/crossref_metadata.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Crossref DOI and metadata-search resolver module."""

from __future__ import annotations

import json
import html
import re
import urllib.error
import urllib.parse
import unicodedata


NAME = "crossref_metadata"
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


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return (
        not ref.get("url")
        and resolve_mod._article_like_resolution_candidate(ref)
        and (
            ref.get("source_type") == "article"
            or bool(resolve_mod._article_title_candidate(ref))
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
        title = (msg.get("title") or [None])[0]
        if not isinstance(title, str) or not title.strip():
            continue
        profile = resolve_mod._metadata_match_profile(ref, msg, title)
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
        (msg.get("title") or [None])[0],
        # This is used only to reject a positive contradiction.  It does not
        # promote the candidate to an identified work.
        ref.get("raw_entry"),
    )
    title_conflict = (
        isinstance(profile.get("title_overlap"), (int, float))
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
    title = (msg.get("title") or [None])[0]
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


def resolve_doi(doi: str, *, ref: dict | None = None) -> dict:
    from core.resolve import service as resolve_mod

    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
    try:
        _status, body = resolve_mod._get(url)
        msg = json.loads(body).get("message", {})
        title = (msg.get("title") or [None])[0]
        abstract = msg.get("abstract")
        relation = msg.get("relation") or {}
        retracted = "is-retracted-by" in relation or bool(
            title and title.strip().lower().startswith("retracted")
        )
        if not retracted:
            retracted = resolve_mod._rw.is_retracted(doi)
        out = {
            "status": "resolved",
            "via": "crossref",
            "matched_title": title,
            "abstract": abstract,
            "retracted": retracted,
            "reason": None,
        }
        matched_authors = _matched_authors(msg)
        if matched_authors:
            out["matched_authors"] = matched_authors
        if ref is not None:
            out["metadata_match"] = resolve_mod._metadata_match_profile(
                ref, msg, title,
            )
        out.update(_fulltext_meta(msg))
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


def discover(ref: dict, *, identifier_fallback: bool = False) -> dict | None:
    from core.resolve import service as resolve_mod

    title = resolve_mod._article_title_candidate(ref)
    raw = ref.get("raw_entry") or ""
    author = resolve_mod._first_author_key(raw)
    year = ref.get("year")
    if not title:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no usable title/bibliographic text for metadata search",
        }

    def _search(query_text: str, yr: int | None) -> tuple[list[dict], str, bool]:
        params: dict[str, str] = {"query.bibliographic": query_text, "rows": "5"}
        year_applied = False
        if yr is not None:
            params["filter"] = f"from-pub-date:{yr - 1},until-pub-date:{yr + 1}"
            year_applied = True
        url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
        status, body = resolve_mod._get(url)
        payload = json.loads(body)
        message = payload.get("message") if isinstance(payload, dict) else None
        items = message.get("items") if isinstance(message, dict) else None
        records_valid = isinstance(items, list) and all(
            isinstance(item, dict)
            and isinstance(item.get("title"), list)
            and bool(item["title"])
            and isinstance(item["title"][0], str)
            and bool(item["title"][0].strip())
            for item in items
        )
        if status != 200 or not isinstance(message, dict) or not records_valid:
            raise ValueError("Crossref title search response is malformed")
        return items, url, year_applied

    query = title[:300]
    if author:
        query += f" {author}"
    year_filter_applied = False
    fallback_complete = True

    def identity_search(outcome: str) -> dict:
        return {
            "resolver": "crossref",
            "query_contract": "Crossref bounded bibliographic title search",
            "completion": "complete" if fallback_complete else "incomplete",
            "outcome": outcome if fallback_complete else "inconclusive",
        }
    try:
        items, _, year_filter_applied = _search(query, year)
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
            for profile in [resolve_mod._metadata_match_profile(
                ref, msg, (msg.get("title") or [None])[0]
            )]
        )
        if year is not None and not filtered_has_viable_identity:
            try:
                items_no_year, _, _ = _search(query, None)
            except Exception:
                fallback_complete = False
                items_no_year = []
            seen_titles = {(msg.get("title") or [None])[0] for msg in items}
            for msg in items_no_year:
                matched_title = (msg.get("title") or [None])[0]
                if matched_title and matched_title not in seen_titles:
                    seen_titles.add(matched_title)
                    items.append(msg)
            items = sorted(
                items,
                key=lambda msg: resolve_mod._metadata_match_profile(
                    ref, msg, (msg.get("title") or [None])[0],
                ).get("score", 0.0),
                reverse=True,
            )[:5]
            year_filter_applied = False
    except urllib.error.HTTPError as exc:
        return {"status": "unresolved", "via": NAME, "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"status": "unresolved", "via": NAME, "reason": f"network: {type(exc).__name__}"}

    if author and (
        not items
        or all(
            resolve_mod._metadata_match_profile(
                ref, msg, (msg.get("title") or [None])[0]
            )["score"]
            < 0.20
            for msg in items
        )
    ):
        try:
            items, _, _ = _search(title[:300], year if year_filter_applied else None)
        except Exception:
            fallback_complete = False

    if not items:
        return {
            "status": "unverified", "via": NAME, "reason": "no metadata candidate on Crossref",
            "identity_search": identity_search("no_compatible_identity"),
        }

    qualifying = []
    correction_shaped = False
    for candidate_msg in items:
        candidate_title = (candidate_msg.get("title") or [None])[0]
        candidate_profile = resolve_mod._metadata_match_profile(ref, candidate_msg, candidate_title)
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
        matched_title = (msg.get("title") or [None])[0]
        profile = resolve_mod._metadata_match_profile(ref, msg, matched_title)
        if profile["score"] > best_score:
            best_score = profile["score"]
            best_msg = msg
            best_profile = profile

    same_work_correction = len(qualifying) == 1
    if same_work_correction:
        msg, profile = qualifying[0]
    else:
        msg, profile = best_msg, best_profile
    matched_title = (msg.get("title") or [None])[0]
    overlap = profile["title_overlap"]
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
    ):
        return {
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
    if (correction_shaped and profile.get("author_match") is False
            and resolve_mod.requires_same_work_correction_author_gate(ref)
            and not same_work_correction):
        return {
            "status": "unverified", "via": NAME, "matched_title": matched_title,
            "reason": "same-work correction candidate is not unique or lacks verified author membership",
            "resolution_basis": "metadata_search", "existence_confidence": "low",
            "metadata_match": profile,
            "identity_search": identity_search("candidate_incompatible"),
        }
    if (year_disagrees and not year_filter_applied
            and not short_title_fallback_match
            and (overlap is None or overlap < 0.85)):
        return {
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
        }
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
            "metadata search match via strict short-title identifier fallback"
            if short_title_fallback_match else "metadata search match"
        ),
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium",
        "metadata_match": profile,
    }
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
    return out
