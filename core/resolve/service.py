#!/usr/bin/env python3
# core/resolve/service.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
resolve.py — Phase 1b, axis 1 (existence) + text retrieval.

For a reference, tries to:
  1. verify it actually exists (DOI via Crossref, PMID via Europe PMC/PubMed,
     ISBN/title via OpenLibrary for books);
  2. retrieve the abstract / useful metadata when possible.

A distinction that must NOT be lost (status/reason never collapsed). The golden rule:
**a source is NEVER suspected as fabricated from search absence alone.** A hard unique-
identifier error or absence from an independently attested complete issue inventory may
support that suspicion; transport failures and incomplete inventories never do.

  - 'not_found'           => ONLY a unique DOI/PMID/ISBN that does NOT resolve.
                            HARD IDENTIFIER ERROR. It becomes suspected fabrication
                            only if independent metadata fallback also fails.
  - 'identifier_mismatch' => the DOI/PMID resolves, but to a work whose title CLEARLY
                            differs from the cited one. HARD IDENTIFIER ERROR
                            (wrong/copied DOI); not automatically fabrication if
                            independent metadata corroborates the cited source.
  - 'resolved'            => exists; optional abstract in --text-out. A title that only
                            partially matches adds a YELLOW WARNING
                            (title_flag='warn'), not a fabrication finding.
  - 'unverified'          => no strong identifier, or title-only search empty,
                            or PMID not in EPMC and unverifiable. NOT fabrication:
                            the report suggests "search online?" (opt-in).
  - 'unresolved'          => rate-limited (429) or network/HTTP: transient, retryable.
                            NOT fabrication.

Beyond existence, deterministically records (from service metadata) whether the full
text *exists at all*: `fulltext_exists` (true/false/unknown), `oa_status`
(open/paywalled/unknown), `work_type`. Used downstream to avoid requesting a full text
that doesn't exist (e.g. a conference abstract) and to weight verdicts seen only on the
abstract. All from metadata — no model prompts.

Book resolution (OpenLibrary + Google Books):
  - ISBN present → exact lookup (strong: not found = fabrication signal).
  - No ISBN but source_type='book' → title+author search (weak: unverified, not fabrication).
  - Books rarely have retrievable full text; fetch.py handles OA PDFs for articles.
    Attributed Google Books preview snippets may provide limited claim evidence.
  - `book_availability` (full | partial | none | unknown) records the catalog-declared
    preview scope as an ADVISORY provisioning hint (region/access dependent, never a
    verdict input); see `availability_note`.

Conference/proceedings cross-check: Crossref cannot tell a full proceedings paper from a
one-page meeting abstract, so a proceedings hit whose `fulltext_exists` is still 'unknown'
is re-checked against Europe PMC (authoritative for the 'meeting abstract' pubType). If
Europe PMC decides, its determination is adopted (`fulltext_exists_refined_by='europepmc'`),
so an abstract-only conference item is not chased as if a paywalled full text existed.

Polite pool: Crossref/Europe PMC give higher rate limits to clients that include a
contact email in the User-Agent. Pass it with --mailto or CITATION_VERIFIER_MAILTO
(recommended but optional). The email goes ONLY in the request header to those
services: it never appears in the report or project files.

Pure stdlib (urllib). Short timeouts, everything in try/except: the script NEVER
crashes on network errors; it degrades gracefully with explicit status/reason.

Usage:
  python -m core.resolve --run runs/<id> --ref-id r1 [--text-out abstract.txt]
      [--mailto you@uni.edu]
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone

try:
    from . import provider_config
    from . import sources as _sources
    from . import retraction_watch as _rw
    from core.infra import perf as _perf
    from .providers.openalex import raw_source_pdf_url as _openalex_raw_source_pdf_url
    from . import providers as resolver_modules
    from .journal_authority import assess_local_journal
    from .bibliographic_adjudication import (
        adjudicate_bibliographic_evidence,
        has_unresolved_identity_attempt,
    )
    from . import policy as resolver_policy
    from .books import (
        _book_meta,
        _extract_book_title,
        _gb_availability,
        _googlebooks,
        _ol_availability,
        _openlibrary,
        _resolve_book,
    )
    from . import http as _http
    from .decision import (
        _build_resolution_decision,
        _prefer_metadata_candidate,
        _promote_validated_identifier,
        _result_has_pdf,
    )
    from .identifiers import (
        _doi_handle,
        _epmc_fulltext_meta,
        _europepmc,
        _is_conference_type,
        _pmc_links_from_ids,
        _pubmed_enrich,
        _pubmed_citation_match,
        _pubmed_fetch_metadata,
        _pubmed_search_pmid,
        _xml_text,
        pubmed_exists,
    )
    from .matching import (
        ABBREVIATED_TITLE_MAX_TOKENS,
        TITLE_MISMATCH_MAX,
        TITLE_MIN_TOKENS,
        TITLE_WARN_MAX,
        _abbreviated_title_guard,
        _abbreviated_title_tokens,
        _aggregate_metadata_identity_signals,
        _article_like_resolution_candidate,
        _article_title_candidate,
        _canonical_link_host,
        _first_author_key,
        _metadata_has_canonical_host,
        _metadata_match_profile,
        _genuine_author_conflict,
        is_unique_same_work_correction_candidate,
        is_same_work_correction_shape,
        requires_same_work_correction_author_gate,
        _same_work_title_match,
        _short_quoted_title_identifier_fallback_match,
        _title_key,
        _title_key_contains,
        _title_match_score,
        title_flag,
        title_overlap,
    )
except ImportError:  # direct execution
    import importlib
    from resolve import provider_config
    from resolve import sources as _sources
    from resolve import retraction_watch as _rw
    import perf as _perf
    from resolve.providers.openalex import raw_source_pdf_url as _openalex_raw_source_pdf_url
    from resolve import providers as resolver_modules
    from resolve.journal_authority import assess_local_journal
    from resolve.bibliographic_adjudication import (
        adjudicate_bibliographic_evidence,
        has_unresolved_identity_attempt,
    )
    from resolve import policy as resolver_policy
    from resolve.books import (
        _book_meta,
        _extract_book_title,
        _gb_availability,
        _googlebooks,
        _ol_availability,
        _openlibrary,
        _resolve_book,
    )
    from resolve.decision import (
        _build_resolution_decision,
        _prefer_metadata_candidate,
        _promote_validated_identifier,
        _result_has_pdf,
    )
    _http = importlib.import_module("resolve.http")
    from resolve.identifiers import (
        _doi_handle,
        _epmc_fulltext_meta,
        _europepmc,
        _is_conference_type,
        _pmc_links_from_ids,
        _pubmed_enrich,
        _pubmed_citation_match,
        _pubmed_fetch_metadata,
        _pubmed_search_pmid,
        _xml_text,
        pubmed_exists,
    )
    from resolve.matching import (
        ABBREVIATED_TITLE_MAX_TOKENS,
        TITLE_MISMATCH_MAX,
        TITLE_MIN_TOKENS,
        TITLE_WARN_MAX,
        _abbreviated_title_guard,
        _abbreviated_title_tokens,
        _aggregate_metadata_identity_signals,
        _article_like_resolution_candidate,
        _article_title_candidate,
        _canonical_link_host,
        _first_author_key,
        _metadata_has_canonical_host,
        _metadata_match_profile,
        _genuine_author_conflict,
        is_unique_same_work_correction_candidate,
        is_same_work_correction_shape,
        requires_same_work_correction_author_gate,
        _same_work_title_match,
        _short_quoted_title_identifier_fallback_match,
        _title_key,
        _title_key_contains,
        _title_match_score,
        title_flag,
        title_overlap,
    )

BASE_UA = _http.BASE_UA
TIMEOUT = _http.TIMEOUT
MAX_RETRY_AFTER = _http.MAX_RETRY_AFTER
open_request = _http.open_request
request_headers = _http.request_headers
user_agent_with_mailto = _http.user_agent_with_mailto
configured_user_agent = _http.configured_user_agent
_OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY", "").strip() or None
_CORE_API_KEY = os.environ.get("CORE_API_KEY", "").strip() or None
# API-key env names for the recovered Semantic Scholar / Lens resolvers.
# Read lazily via _configured_key() so the keys can be set after import.
ENV_SEMANTIC_SCHOLAR_API_KEY = "SEMANTIC_SCHOLAR_API_KEY"
ENV_LENS_API_KEY = "LENS_API_KEY"


def _official_oa_doi_prefixes() -> tuple[str, ...]:
    return provider_config.official_oa_doi_prefixes()

METADATA_BORDERLINE_MIN = 0.35  # enough signal to avoid a fabrication tag, not enough to verify
METADATA_VERIFY_MIN = 0.55       # enough combined metadata signal to corroborate a source
SYNTHETIC_RISK_HIGH_MIN = 5      # plausible scholarly package, no corroboration
SYNTHETIC_RISK_MEDIUM_MIN = 3    # concerning but less complete pattern

# Stop words used only for the arXiv all: fallback query — common title words
# that add noise without signal in keyword searches.
_STOP_TITLE = {"with", "from", "that", "this", "their", "they", "have", "been",
               "into", "over", "some", "such", "each", "more", "what", "when",
               "which", "than", "then", "also", "very", "just", "only", "other",
               "about", "these", "those", "using", "based", "through", "between"}


def _tex_strip_braces(raw: str) -> str:
    r"""Remove LaTeX braces from a string, preserving the content inside.

    ``{\L}ukasz`` → ``ukasz``, ``{NIPS}`` → ``NIPS``,
    ``{Advances in Neural...}`` → ``Advances in Neural...``.
    """
    return re.sub(r"[{}]", "", raw)

def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def __getattr__(name: str):
    if name in {"_UA", "_CONTACT_EMAIL"}:
        return getattr(_http, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def set_contact(mailto: str | None):
    _http.set_contact(mailto)


def _refresh_http_test_seam() -> None:
    _http.open_request = open_request


def _shared_host_limiter():
    return _http._shared_host_limiter()


def _get(
    url: str,
    accept: str = "application/json",
    headers_extra: dict[str, str] | None = None,
    *,
    preserve_cooldown_after_429: bool = False,
):
    _refresh_http_test_seam()
    return _http._get(
        url, accept=accept, headers_extra=headers_extra,
        preserve_cooldown_after_429=preserve_cooldown_after_429,
    )


def _get_with_final_url(
    url: str,
    accept: str = "application/json",
    headers_extra: dict[str, str] | None = None,
    *,
    preserve_cooldown_after_429: bool = False,
):
    _refresh_http_test_seam()
    return _http._get_with_final_url(
        url, accept=accept, headers_extra=headers_extra,
        preserve_cooldown_after_429=preserve_cooldown_after_429,
    )


def _get_bytes(
    url: str,
    accept: str = "application/octet-stream",
    headers_extra: dict[str, str] | None = None,
    *,
    preserve_cooldown_after_429: bool = False,
):
    _refresh_http_test_seam()
    return _http._get_bytes(
        url,
        accept=accept,
        headers_extra=headers_extra,
        preserve_cooldown_after_429=preserve_cooldown_after_429,
    )


def _get_bytes_with_final_url(
    url: str,
    accept: str = "application/octet-stream",
    headers_extra: dict[str, str] | None = None,
    *,
    preserve_cooldown_after_429: bool = False,
):
    _refresh_http_test_seam()
    return _http._get_bytes_with_final_url(
        url,
        accept=accept,
        headers_extra=headers_extra,
        preserve_cooldown_after_429=preserve_cooldown_after_429,
    )


def _ncbi_url(base: str, params: dict[str, str]) -> str:
    return _http._ncbi_url(base, params)


def _retry_after_seconds(err) -> float:
    return _http._retry_after_seconds(err)




def _identifier_fallback_search(ref: dict) -> dict | None:
    """Independent metadata lookup after a hard identifier failure.

    This does not erase the identifier error. It only asks whether the source identity
    itself is corroborated by title/author/year/venue metadata.
    """
    source_kind = ref.get("source_kind") or ref.get("source_type")
    source_type = ref.get("source_type")
    if (source_kind == "book_like" or source_type == "book") and not _article_like_resolution_candidate(ref):
        if ref.get("isbn") and _extract_book_title(ref):
            title_ref = dict(ref)
            title_ref["isbn"] = None
            return _resolve_book(title_ref)
        return None
    if (source_kind == "article_like" or source_type == "article"
            or ref.get("doi") or ref.get("pmid") or _article_title_candidate(ref)):
        return _crossref_resolver_module().discover(ref, identifier_fallback=True)
    return None


def _coordinate_value(ref: dict, kind: str) -> str | None:
    for item in ref.get("cited_coordinates") or ():
        if isinstance(item, dict) and item.get("kind") == kind:
            value = item.get("normalized_value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _article_coordinate_eligible(ref: dict) -> bool:
    """Whether the citation structurally supports an article-coordinate check."""
    if not isinstance(ref, dict):
        return False
    kind = ref.get("source_kind") or ref.get("source_type")
    if ref.get("isbn"):
        return False
    if (kind == "book_like" or ref.get("source_type") == "book") and not _article_like_resolution_candidate(ref):
        return False
    return bool(
        _coordinate_value(ref, "container")
        and str(ref.get("year") or "").strip()
        and _coordinate_value(ref, "volume")
        and any(_coordinate_value(ref, locator) for locator in (
            "article_page_range", "elocator", "article_number", "article_locator",
        ))
    )


def _journal_issue_eligible(ref: dict) -> bool:
    """Whether a citation structurally supports a journal issue check."""
    if not isinstance(ref, dict):
        return False
    kind = ref.get("source_kind") or ref.get("source_type")
    if ref.get("isbn"):
        return False
    if (kind == "book_like" or ref.get("source_type") == "book") and not _article_like_resolution_candidate(ref):
        return False
    return bool(_coordinate_value(ref, "container") and _coordinate_value(ref, "volume"))


def _pubmed_coordinate_correction_search(ref: dict) -> dict | None:
    """Search PubMed by journal coordinates, then verify the returned identity."""
    if not _article_coordinate_eligible(ref):
        return None
    journal = _coordinate_value(ref, "container")
    volume = _coordinate_value(ref, "volume")
    locator_kind = next((kind for kind in (
        "article_page_range", "elocator", "article_number", "article_locator",
    ) if _coordinate_value(ref, kind)), None)
    page = _coordinate_value(ref, locator_kind) if locator_kind else None
    first_page = re.split(r"[-–—]", page or "", maxsplit=1)[0].strip()
    author = ref.get("ay_surname") or _first_author_key(ref.get("raw_entry"))
    year = str(ref.get("year") or "").strip()
    if not all((journal, year, volume, first_page, author)):
        return None
    candidate = _pubmed_citation_match(
        journal=journal, year=year, volume=volume, first_page=first_page,
        author=str(author),
    )
    if candidate.get("status") != "resolved":
        return candidate
    msg = {
        "title": [candidate.get("matched_title")],
        "author": [
            {"family": name} for name in candidate.get("matched_authors") or ()
        ],
        "published": {"date-parts": [[candidate.get("matched_year")]]},
        "container-title": [candidate.get("matched_venue")],
        "volume": candidate.get("matched_volume"),
        "issue": candidate.get("matched_issue"),
        "page": candidate.get("matched_page"),
        "article-number": (
            candidate.get("matched_page") if locator_kind == "article_number" else None
        ),
    }
    metadata_match = _metadata_match_profile(ref, msg, candidate.get("matched_title"))
    accepted = (
        metadata_match.get("ordinal_conflict") is not True
        and (metadata_match.get("title_overlap") or 0) >= 0.90
        and metadata_match.get("author_match") is True
        and metadata_match.get("year_match") is not False
    )
    article_ids = candidate.get("article_ids") or {}
    identifiers = {
        key: value
        for key, value in {
            "doi": _normalize_doi_value(article_ids.get("doi")),
            "pmid": str(candidate.get("pmid") or "").strip() or None,
            "pmcid": str(
                article_ids.get("pmc") or article_ids.get("pmcid") or ""
            ).strip() or None,
        }.items()
        if value
    }
    result = {
        "status": "resolved" if accepted else "unverified",
        "via": "pubmed_citation_match",
        "reason": (
            "NCBI ECitMatch candidate passed deterministic identity comparison"
            if accepted else
            "NCBI ECitMatch candidate did not pass deterministic identity comparison"
        ),
        "matched_title": candidate.get("matched_title"),
        "matched_authors": candidate.get("matched_authors") or [],
        "matched_year": candidate.get("matched_year"),
        "pmid": candidate.get("pmid"),
        "abstract": candidate.get("abstract"),
        "retracted": False,
        "fulltext_exists": "unknown",
        "oa_status": "unknown",
        "work_type": "article",
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium" if accepted else "low",
        "metadata_match": metadata_match,
        "fulltext_links": [],
    }
    if accepted and identifiers:
        result["identifiers"] = identifiers
        if identifiers.get("doi"):
            result["resolved_identifier"] = {
                "type": "doi",
                "value": identifiers["doi"],
                "validated_via": "pubmed_citation_match",
            }
    return result


def _pubmed_coordinate_occupancy_search(
    ref: dict, prior_candidate: dict | None = None, *, allow_authorless: bool = True,
) -> dict | None:
    """Record a positively identified other work at the cited PubMed tuple."""
    if not _article_coordinate_eligible(ref):
        return None
    journal = _coordinate_value(ref, "container")
    volume = _coordinate_value(ref, "volume")
    locator_kind = next((kind for kind in (
        "article_page_range", "elocator", "article_number", "article_locator",
    ) if _coordinate_value(ref, kind)), None)
    page = _coordinate_value(ref, locator_kind) if locator_kind else None
    first_page = re.split(r"[-–—]", page or "", maxsplit=1)[0].strip()
    year = str(ref.get("year") or "").strip()
    if not all((journal, year, volume, first_page)):
        return None
    def project(candidate: dict) -> dict | None:
        if not candidate.get("matched_title"):
            return None
        profile = candidate.get("metadata_match")
        if not isinstance(profile, dict):
            msg = {
                "title": [candidate.get("matched_title")],
                "author": [
                    {"family": name} for name in candidate.get("matched_authors") or ()
                ],
                "published": {"date-parts": [[candidate.get("matched_year")]]},
                "container-title": [candidate.get("matched_venue")],
                "volume": candidate.get("matched_volume"),
                "issue": candidate.get("matched_issue"),
                "page": candidate.get("matched_page"),
                "article-number": (
                    candidate.get("matched_page")
                    if locator_kind == "article_number" else None
                ),
            }
            profile = _metadata_match_profile(ref, msg, candidate.get("matched_title"))
        comparisons = {
            item["kind"]: item for item in profile.get("coordinate_comparisons") or ()
        }
        page_comparison = (
            comparisons.get("article_page_range")
            or comparisons.get("elocator")
            or comparisons.get("article_number")
            or comparisons.get("article_locator")
            or {}
        )
        matched_page = str(
            candidate.get("matched_page") or page_comparison.get("matched_value") or ""
        )
        matched_first_page = re.split(r"[-–—]", matched_page, maxsplit=1)[0].strip()
        tuple_confirmed = (
            profile.get("year_match") is True
            and comparisons.get("volume", {}).get("status") == "match"
            and matched_first_page == first_page
            and comparisons.get("container", {}).get("status") in {"match", "inconclusive"}
        )
        author_contradiction = (
            profile.get("author_match") is False
            and bool(profile.get("cited_first_author"))
            and bool(profile.get("matched_first_author"))
        )
        raw_title_overlap = title_overlap(
            candidate.get("matched_title"), ref.get("raw_entry"),
        )
        title_contradiction = (
            profile.get("title_overlap") is not None
            and profile["title_overlap"] < 0.50
            and not (
                raw_title_overlap is not None
                and raw_title_overlap >= 0.85
                and profile.get("author_match") is True
            )
        )
        incompatible = title_contradiction or author_contradiction
        return {
            "status": "resolved" if tuple_confirmed and incompatible else "unverified",
            "via": "pubmed_coordinate_occupancy",
            "reason": (
                "PubMed metadata positively confirms another work occupies the cited coordinates"
                if tuple_confirmed and incompatible else
                "PubMed coordinate candidate did not establish an incompatible occupant"
            ),
            "matched_title": candidate.get("matched_title"),
            "matched_authors": candidate.get("matched_authors") or [],
            "matched_year": candidate.get("matched_year"),
            "pmid": candidate.get("pmid"),
            "resolution_basis": "metadata_search",
            "existence_confidence": "medium" if tuple_confirmed and incompatible else "low",
            "metadata_match": profile,
        }

    if isinstance(prior_candidate, dict):
        projected = project(prior_candidate)
        if projected is not None and projected["status"] == "resolved":
            return projected

    author = str(ref.get("ay_surname") or _first_author_key(ref.get("raw_entry")) or "")
    candidates: list[dict] = []
    if prior_candidate is None and author:
        candidates.append(_pubmed_citation_match(
            journal=journal, year=year, volume=volume, first_page=first_page,
            author=author,
        ))
    if allow_authorless:
        candidates.append(_pubmed_citation_match(
            journal=journal, year=year, volume=volume, first_page=first_page, author="",
        ))
    last: dict | None = None
    for candidate in candidates:
        last = candidate
        projected = project(candidate)
        if projected is not None and projected["status"] == "resolved":
            return projected
    if last is None:
        return None
    projected = project(last)
    return projected or {**last, "via": "pubmed_coordinate_occupancy"}


def _without_ecitmatch_audit(value: dict) -> dict:
    """Keep raw ECitMatch transport evidence out of ordinary resolve attempts."""
    return {key: item for key, item in value.items() if not key.startswith("ecitmatch_")}


def _issue_member_result(ref: dict, attestation: dict) -> dict | None:
    """Project a registry-attested issue member as an identity candidate."""
    order = attestation.get("target_member_order")
    members = attestation.get("members") or []
    if not isinstance(order, int) or isinstance(order, bool) or not 0 <= order < len(members):
        return None
    member = members[order]
    if not isinstance(member, dict):
        return None
    identifiers = {
        key: member.get(key)
        for key in ("doi", "pmid", "pmcid")
        if member.get(key)
    }
    msg = {
        "title": [member.get("title")],
        "author": ([{"family": member.get("first_author")}]
                   if member.get("first_author") else []),
        "published": ({"date-parts": [[member.get("year")]]}
                      if member.get("year") else {}),
        "container-title": [member.get("journal")] if member.get("journal") else [],
        "volume": member.get("volume"),
        "issue": member.get("issue"),
        "page": member.get("locator"),
    }
    metadata_match = _metadata_match_profile(ref, msg, member.get("title"))
    provider = attestation.get("provider")
    url = member.get("url")
    if not url and member.get("pmcid"):
        url = f"https://pmc.ncbi.nlm.nih.gov/articles/{member['pmcid']}/"
    fulltext_links = []
    if url:
        fulltext_links.append({
            "url": url, "content_type": "pdf" if str(url).lower().split("?", 1)[0].endswith(".pdf") else "html",
            "identity_context": {"provider": provider, "provider_record_id": member.get("record_id"),
                                 "canonical_host": True, "canonical_url": url,
                                 "title": member.get("title"), "first_author": member.get("first_author"),
                                 "year": member.get("year"), "identifiers": identifiers},
        })
    return {
        "status": "resolved",
        "via": provider,
        "record_id": member.get("record_id"),
        "matched_title": member.get("title"),
        "matched_authors": ([member.get("first_author")]
                            if member.get("first_author") else []),
        "matched_year": member.get("year"),
        "reason": "work identified in an enumerated issue attestation",
        "resolution_basis": "metadata_search",
        "existence_confidence": "high",
        "metadata_match": metadata_match,
        "identifiers": identifiers,
        "abstract": None,
        "retracted": False,
        "fulltext_exists": True if fulltext_links else "unknown",
        "oa_status": "open" if fulltext_links else "unknown",
        "work_type": "article",
        "fulltext_links": fulltext_links,
    }


def _issue_attestation_checks(ref: dict, status: str) -> list[dict]:
    """Invoke every applicable registry adapter through one deterministic boundary."""
    if status not in {"unverified", "not_found", "identifier_mismatch"}:
        return []
    if not _journal_issue_eligible(ref):
        return []
    if not (_coordinate_value(ref, "container") and _coordinate_value(ref, "volume")):
        return []
    return [item for item in resolver_modules.attest_issues(ref)
            if item.get("status") != "not_applicable"]


def _fallback_evidence_view(candidate: dict) -> dict:
    """Keep attempt-only candidate identifiers out of the fallback profile shape."""
    return {
        key: value for key, value in candidate.items()
        if key not in {
            "pmid", "matched_year", "resolved_identifier", "identifiers",
            "identity_search",
        }
        and not key.startswith("ecitmatch_")
    }


def _project_recovered_content(result: dict, correction: dict) -> dict:
    """Replace wrong-identifier content with the admitted correction's content."""
    out = dict(result)
    # The original result identifies the cited (wrong) identifier.  Its
    # selected-work identifiers must never leak into the recovered fetch path.
    out.pop("resolved_identifier", None)
    out.pop("identifiers", None)
    projected = _annotate_primary_fulltext_links(dict(correction)) or {}
    correction_identifier = correction.get("resolved_identifier")
    correction_doi = None
    if isinstance(correction_identifier, dict):
        identifier_type = str(correction_identifier.get("type") or "").lower()
        candidate_doi = _normalize_doi_value(correction_identifier.get("value"))
        if (identifier_type == "doi" and candidate_doi
                and correction_identifier.get("validated_via")):
            correction_doi = candidate_doi
    correction_identifiers = correction.get("identifiers")
    if isinstance(correction_identifiers, dict) and correction_identifiers.get("doi"):
        listed_doi = _normalize_doi_value(correction_identifiers.get("doi"))
        if (not correction_doi or not listed_doi
                or listed_doi.casefold() != correction_doi.casefold()):
            correction_doi = None
    out.setdefault("identifier_target_title", result.get("matched_title"))
    out.setdefault("identifier_target_via", result.get("via"))
    out["matched_title"] = projected.get("matched_title")
    out["matched_authors"] = projected.get("matched_authors")
    out["content_via"] = projected.get("via")
    out["resolution_basis"] = projected.get("resolution_basis", "metadata_search")
    out["existence_confidence"] = projected.get("existence_confidence", "medium")
    out["metadata_match"] = projected.get("metadata_match")
    out["abstract"] = projected.get("abstract")
    if out["abstract"]:
        out["abstract_via"] = projected.get("abstract_via") or projected.get("via")
    else:
        out.pop("abstract_via", None)
    out["retracted"] = bool(projected.get("retracted", False))
    out["fulltext_links"] = list(projected.get("fulltext_links") or [])
    out["auxiliary_fulltext_links"] = list(
        projected.get("auxiliary_fulltext_links") or []
    )
    out["fulltext_exists"] = projected.get("fulltext_exists", "unknown")
    out["oa_status"] = projected.get("oa_status", "unknown")
    out["work_type"] = projected.get("work_type")
    # A correction can replace fetch identity only when its DOI itself was
    # explicitly validated.  Keep the cited identifier on the parsed reference
    # and in the mismatch evidence; this is solely the selected-work identity.
    if correction_doi:
        out["resolved_identifier"] = {
            "type": "doi",
            "value": correction_doi,
            "validated_via": correction_identifier["validated_via"],
        }
        if isinstance(correction_identifiers, dict):
            identifiers = {
                key: value for key, value in correction_identifiers.items()
                if key in {"pmid", "pmcid"} and str(value or "").strip()
            }
            listed_doi = _normalize_doi_value(correction_identifiers.get("doi"))
            if listed_doi and listed_doi.casefold() == correction_doi.casefold():
                identifiers["doi"] = correction_doi
            if identifiers:
                out["identifiers"] = identifiers
    for key in (
        "fulltext_availability", "oa_declared_status", "oa_license_urls",
        "book_availability", "availability_note", "fulltext_exists_refined_by",
    ):
        if key in projected:
            out[key] = projected[key]
        else:
            out.pop(key, None)
    return out


def _merge_fulltext_links(primary: list[dict] | None, extra: list[dict] | None) -> list[dict]:
    out = []
    by_url = {}
    for item in list(primary or []) + list(extra or []):
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        key = url or json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key not in by_url:
            row = dict(item)
            by_url[key] = row
            out.append(row)
            continue
        current = by_url[key]
        contexts = []
        for context in (
            current.get("identity_context"),
            *(current.get("identity_contexts") or []),
            item.get("identity_context"),
            *(item.get("identity_contexts") or []),
        ):
            if isinstance(context, dict) and context not in contexts:
                contexts.append(context)
        if contexts:
            current["identity_context"] = contexts[0]
            if len(contexts) > 1:
                current["identity_contexts"] = contexts
                titles = {_title_key(ctx.get("title")) for ctx in contexts if ctx.get("title")}
                current["identity_context_conflict"] = len(titles) > 1
        provenance = list(current.get("provenance") or [])
        for value in (
            current.get("discovered_via"),
            item.get("discovered_via"),
            *((item.get("provenance") or [])),
        ):
            if value and value not in provenance:
                provenance.append(value)
        if provenance:
            current["provenance"] = provenance
    return out


def _candidate_identity_context(candidate: dict, via: str, link: dict | None = None) -> dict:
    """Build immutable per-link identity/provenance for downstream fetch probes."""
    link = link or {}
    metadata_match = candidate.get("metadata_match") or {}
    identifiers = {}
    doi = _result_doi(candidate)
    if doi:
        identifiers["doi"] = doi
    resolved = candidate.get("resolved_identifier") or {}
    if resolved.get("type") and resolved.get("value"):
        identifiers[str(resolved["type"])] = str(resolved["value"])
    url = str(link.get("url") or "")
    arxiv_match = re.search(r"arxiv\.org/(?:abs|pdf)/([^/?#]+?)(?:\.pdf)?$", url, re.I)
    if arxiv_match:
        identifiers.setdefault("arxiv_id", arxiv_match.group(1))
    confidence = candidate.get("existence_confidence")
    confidence_value = {"high": 0.98, "medium": 0.85, "low": 0.50}.get(confidence)
    score = metadata_match.get("score")
    if isinstance(score, (int, float)):
        confidence_value = float(score)
    host = urllib.parse.urlparse(url).netloc.lower().split(":", 1)[0]
    canonical_hosts = set(provider_config.provider_canonical_hosts())
    canonical_hosts.update(provider_config.load().get("jmlr_hosts") or [])
    context = {
        "title": candidate.get("matched_title"),
        "authors": list(candidate.get("matched_authors") or []),
        "year": metadata_match.get("matched_year") or candidate.get("matched_year"),
        "identifiers": identifiers,
        "provider": via,
        "provider_record_id": candidate.get("paper_id") or candidate.get("record_id"),
        "source_confidence": confidence_value,
        "canonical_host": any(host == suffix or host.endswith(f".{suffix}") for suffix in canonical_hosts),
    }
    # A resolver may attach an immutable, record-specific document relation to
    # one link.  Keep it with that link rather than inferring it from a host.
    # This is used only by narrowly scoped Fetch identity routes.
    if isinstance(link.get("identity_context"), dict):
        context.update(link["identity_context"])
    return {key: value for key, value in context.items() if value not in (None, "", [], {})}


def _annotate_primary_fulltext_links(result: dict | None) -> dict | None:
    if not isinstance(result, dict):
        return result
    via = str(result.get("via") or "resolver")
    enriched = dict(result)
    links = []
    for item in result.get("fulltext_links") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row.setdefault("discovered_via", via)
        row.setdefault("identity_context", _candidate_identity_context(result, via, row))
        links.append(row)
    enriched["fulltext_links"] = links
    return enriched


# Content-type tokens that denote a usable full-text resource. Crossref link
# objects carry the MIME type ("application/pdf"); OpenAlex-derived links use
# the short tag "pdf"; arXiv abstracts may be delivered as "text/plain".
_FULLTEXT_CONTENT_TYPES = ("application/pdf", "pdf", "text/plain")


def _links_have_fulltext(links: list[dict] | None) -> bool:
    return any(
        link.get("content_type") in _FULLTEXT_CONTENT_TYPES
        for link in (links or [])
        if isinstance(link, dict)
    )


def _capture_auxiliary_fulltext_links(
    target: dict,
    *,
    via: str,
    links: list[dict] | None,
    candidate: dict | None = None,
) -> bool:
    tagged = []
    for item in links or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row.setdefault("discovered_via", via)
        if candidate is not None:
            row.setdefault("identity_context", _candidate_identity_context(candidate, via, row))
        tagged.append(row)
    if not tagged:
        return False
    target["auxiliary_fulltext_links"] = _merge_fulltext_links(
        target.get("auxiliary_fulltext_links"),
        tagged,
    )
    return _links_have_fulltext(tagged)


def _adopt_preferred_metadata_candidate(
    ref: dict,
    current: dict | None,
    candidate: dict | None,
) -> dict | None:
    if candidate is None:
        return current
    preferred = _prefer_metadata_candidate(ref, current, candidate)
    if preferred is None:
        return None
    if preferred is candidate and current and current is not candidate:
        enriched = resolver_policy.capture_same_work_companion_links(
            ref,
            preferred,
            current,
            article_title_candidate=_article_title_candidate,
            title_key=_title_key,
            title_key_contains=_title_key_contains,
            metadata_has_canonical_host=_metadata_has_canonical_host,
            capture_auxiliary_fulltext_links=_capture_auxiliary_fulltext_links,
        )
        if enriched is not None:
            # These links were already admitted against ``current``.  Retain
            # their identity context when metadata preference changes winner;
            # the same-work gate above still governs newly encountered links.
            enriched = dict(enriched)
            _capture_auxiliary_fulltext_links(
                enriched,
                via=str(current.get("via") or "resolver"),
                links=current.get("auxiliary_fulltext_links"),
                candidate=current,
            )
        return enriched
    return preferred


def _result_title_overlap(result: dict | None) -> float:
    """Title overlap of a resolver result, read from its nested ``metadata_match``.

    Resolver results store the overlap inside ``metadata_match`` (the scoring
    profile), not as a top-level key — so callers must dig in to compare matches.
    Returns 0.0 when absent so comparisons degrade safely.
    """
    if not result:
        return 0.0
    overlap = (result.get("metadata_match") or {}).get("title_overlap")
    return overlap if isinstance(overlap, (int, float)) else 0.0


def _result_has_fulltext(result: dict | None) -> bool:
    """True when a resolver result carries at least one PDF fulltext link.

    Accepts both the Crossref MIME form ("application/pdf") and the short
    OpenAlex form ("pdf").
    """
    if not result:
        return False
    return _links_have_fulltext(result.get("fulltext_links"))


def _result_doi(result: dict | None) -> str:
    if not result:
        return ""
    doi = str(result.get("doi") or "").strip()
    if doi:
        return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    for link in result.get("fulltext_links") or []:
        url = str((link or {}).get("url") or "").strip()
        m = re.search(r"doi\.org/(10\.[^/\s]+/.+)$", url, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _resolved_identifier_summary(result: dict | None) -> dict | None:
    if not isinstance(result, dict):
        return None
    summary = result.get("resolved_identifier")
    return summary if isinstance(summary, dict) else None


def _resolved_identifier_type(result: dict | None) -> str | None:
    summary = _resolved_identifier_summary(result)
    if not summary:
        return None
    ident_type = str(summary.get("type") or "").strip().lower()
    return ident_type or None


def _effective_enrichment_ref(ref: dict, result: dict | None) -> dict:
    """Overlay resolver-discovered identity onto the parsed ref for enrichers only."""
    effective = dict(ref or {})
    ident_type = _resolved_identifier_type(result)
    ident = _resolved_identifier_summary(result) or {}
    if not effective.get("doi"):
        doi = _result_doi(result)
        if not doi and ident_type == "doi":
            doi = _normalize_doi_value(ident.get("value"))
        if doi:
            effective["doi"] = doi
    if not effective.get("pmid") and ident_type == "pmid":
        pmid = str(ident.get("value") or "").strip()
        if pmid:
            effective["pmid"] = pmid
    matched_title = str((result or {}).get("matched_title") or "").strip()
    if matched_title:
        effective["title"] = matched_title
    return effective


def _copy_verified_identifiers(target: dict, candidate: dict | None) -> None:
    """Carry same-work enrichment identifiers onto the selected result only."""
    if not isinstance(candidate, dict) or candidate.get("status") != "resolved":
        return
    identifiers = dict(target.get("identifiers") or {})
    discovered = candidate.get("identifiers")
    if isinstance(discovered, dict):
        for key in ("doi", "pmid", "pmcid"):
            value = str(discovered.get(key) or "").strip()
            if value:
                identifiers.setdefault(key, value)
    for key in ("doi", "pmid", "pmcid"):
        value = str(candidate.get(key) or "").strip()
        if value:
            identifiers.setdefault(key, value)
    article_ids = candidate.get("article_ids")
    if isinstance(article_ids, dict):
        for raw_key, value in article_ids.items():
            key = {"pubmed": "pmid", "pmc": "pmcid"}.get(str(raw_key).lower(), str(raw_key).lower())
            text = str(value or "").strip()
            if key in {"doi", "pmid", "pmcid"} and text:
                identifiers.setdefault(key, text)
    for link in candidate.get("fulltext_links") or []:
        if not isinstance(link, dict):
            continue
        match = re.search(r"/articles/(PMC\d+)(?:/|$)", str(link.get("url") or ""), re.I)
        if match:
            identifiers.setdefault("pmcid", match.group(1).upper())
    if identifiers:
        target["identifiers"] = identifiers


def _attempt_year(candidate: dict | None) -> int | None:
    if not candidate:
        return None
    raw = (
        candidate.get("year")
        or candidate.get("matched_year")
        or (candidate.get("metadata_match") or {}).get("matched_year")
    )
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _co_identified(winner: dict, sibling: dict) -> bool:
    """Whether ``sibling`` is provably the same work as ``winner`` — the gate for
    borrowing its abstract/links. Requires a normalized-title match and vetoes any
    DOI or year conflict, so a same-title-different-work sibling is never adopted.

    The year veto tolerates a one-year gap: conference proceedings routinely carry
    the publication year (e.g. NeurIPS 2008 indexed as 2009) while another resolver
    reports the conference year, and the two are the same work. A wider gap on an
    identical title still reads as a distinct reissue and is rejected.
    """
    winner_title = _title_key(winner.get("matched_title"))
    sibling_title = _title_key(sibling.get("matched_title"))
    if not winner_title or winner_title != sibling_title:
        return False
    winner_doi, sibling_doi = _result_doi(winner).lower(), _result_doi(sibling).lower()
    if winner_doi and sibling_doi and winner_doi != sibling_doi:
        return False
    winner_year, sibling_year = _attempt_year(winner), _attempt_year(sibling)
    if (
        winner_year is not None
        and sibling_year is not None
        and abs(winner_year - sibling_year) > 1
    ):
        return False
    return True


def _harvest_co_identified_siblings(result: dict, attempts: list[dict]) -> dict:
    """Fill a resolved result's missing abstract / full-text links from co-identified
    sibling attempts already collected, with no new network request.

    When several resolvers independently resolve the same work (e.g. OpenAlex,
    NeurIPS and Semantic Scholar), the winner is picked on match quality — which
    can be a record that lacks an abstract or an OA link even though a sibling has
    them. Rather than re-query by identifier (which fails for DOI-less papers),
    borrow from a sibling, but only when :func:`_co_identified` proves it is the
    same work. Full-text links are additionally re-verified by the fetch identity
    probe; abstracts have no such downstream guard, so the same-work check is the
    only thing standing between them and contamination — hence it gates both.
    """
    if not isinstance(result, dict) or result.get("status") != "resolved":
        return result
    if result.get("abstract") and _links_have_fulltext(result.get("fulltext_links")):
        return result
    enriched = dict(result)
    for attempt in attempts or []:
        if not isinstance(attempt, dict) or attempt is result:
            continue
        if attempt.get("status") != "resolved" or attempt.get("enrichment_only"):
            continue
        if attempt.get("via") and attempt.get("via") == result.get("via"):
            continue
        if not _co_identified(result, attempt):
            continue
        if not _links_have_fulltext(enriched.get("fulltext_links")):
            merged = _merge_fulltext_links(enriched.get("fulltext_links"), attempt.get("fulltext_links"))
            if _links_have_fulltext(merged):
                enriched["fulltext_links"] = merged
                if enriched.get("fulltext_exists") in (None, "unknown") and attempt.get("fulltext_exists") is True:
                    enriched["fulltext_exists"] = True
                    enriched["fulltext_exists_refined_by"] = attempt.get("via")
                if enriched.get("oa_status") in (None, "unknown") and attempt.get("oa_status") not in (None, "unknown"):
                    enriched["oa_status"] = attempt.get("oa_status")
        if not enriched.get("abstract") and attempt.get("abstract"):
            enriched["abstract"] = attempt.get("abstract")
            enriched["abstract_via"] = attempt.get("via")
    return enriched


def _attest_repository_record(
    ref: dict, result: dict, attempts: list[dict],
) -> dict:
    """Promote only an exact, official repository record.

    A sibling's repository landing is useful only after the sibling has already
    been shown to carry the citation's exact title.  The provider then performs
    the stricter author/coordinates/DOI attestation; failed pages leave the
    existing result untouched.
    """
    cited_title = _title_key(_article_title_candidate(ref) or ref.get("title"))
    if not cited_title:
        return result
    try:
        from .providers import repository
    except ImportError:  # pragma: no cover - direct execution fallback
        from resolve.providers import repository
    seen = set()
    for candidate in (result, *(attempts or ())):
        if not isinstance(candidate, dict) or candidate.get("status") != "resolved":
            continue
        if _title_key(candidate.get("matched_title")) != cited_title:
            continue
        for link in candidate.get("fulltext_links") or ():
            if not isinstance(link, dict):
                continue
            url = str(link.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            if repository.is_bepress_article_landing_url(url):
                attested = repository.attest_bepress_record(ref, url)
            elif repository.is_ojs_article_download_url(url):
                attested = repository.attest_ojs_download_record(ref, url)
            else:
                continue
            if attested is None:
                continue
            resolved_identifier = _resolved_identifier_summary(result) or {}
            resolved_doi = (
                _normalize_doi_value(resolved_identifier.get("value"))
                if str(resolved_identifier.get("type") or "").lower() == "doi"
                else None
            )
            observed_doi = _normalize_doi_value(
                (attested.get("identifiers") or {}).get("doi")
            )
            # Parse may have omitted a DOI even though an earlier resolver has
            # already established one.  A repository page naming another DOI
            # cannot replace that identity merely because its title matches.
            if (
                resolved_doi
                and observed_doi
                and resolved_doi.casefold() != observed_doi.casefold()
            ):
                continue
            # The attestation replaces weak identity metadata, not already
            # acquired same-work content.  Keep that provenance for Fetch and
            # the report while the canonical repository PDF becomes primary.
            for field in ("abstract", "abstract_via", "auxiliary_fulltext_links"):
                if result.get(field) is not None and attested.get(field) is None:
                    attested[field] = result[field]
            attempt = dict(attested)
            # Auxiliary links belong to the selected final projection.  The
            # typed provider-attempt schema records only the attested primary
            # links and deliberately has no auxiliary_fulltext_links field.
            attempt.pop("auxiliary_fulltext_links", None)
            attempts.append(attempt)
            return attested
    return result


def _enrich_resolved_metadata(
    ref: dict, result: dict, attempts: list[dict], *, continuation: dict | None = None,
) -> dict:
    """Fill missing abstract/auxiliary metadata from a secondary deterministic source.

    This never changes the primary existence verdict; it only enriches a already-resolved
    record when the first resolver lacked an abstract or useful auxiliary metadata.
    """
    if result.get("status") != "resolved":
        return result

    if continuation is not None and continuation.get("kind") == "enrichment_done":
        return dict(continuation["enriched"])

    if continuation is not None and continuation.get("kind") == "enrichment_provider":
        module = continuation["module"]
        try:
            with resolver_modules.credential_scope_for_resolver(module):
                enrichment = module.enrich(continuation["effective_ref"])
        except _http.ProviderCooldownDeferred as exc:
            exc.continuation = continuation
            raise
        enriched = dict(continuation["enriched"])
        if enrichment is not None:
            enrichment_via = resolver_modules.origin_name(module) or str(getattr(module, "NAME", "resolver"))
            continuation["attempts"].append({"via": enrichment_via, "enrichment_only": True, **enrichment})
            if enrichment.get("status") == "resolved":
                if not enriched.get("abstract") and enrichment.get("abstract"):
                    enriched["abstract"] = enrichment["abstract"]
                    enriched["abstract_via"] = enrichment_via
                _capture_auxiliary_fulltext_links(
                    enriched, via=enrichment_via,
                    links=enrichment.get("fulltext_links") or [], candidate=enrichment,
                )
        return enriched

    if continuation is not None:
        enriched = dict(continuation["enriched"])
        effective_ref = dict(continuation["effective_ref"])
        auxiliary_has_fulltext = bool(continuation["auxiliary_has_fulltext"])
        enrichers = list(continuation["enrichers"])
        start_index = int(continuation["next_index"])
    else:
        # Cheapest enrichment first: reuse abstract/full-text links already fetched by a
        # co-identified sibling resolver before re-querying providers by identifier
        # (which fails for DOI-less papers like older conference proceedings).
        result = _harvest_co_identified_siblings(result, attempts)

        via = result.get("via")
        enriched = dict(result)
        effective_ref = _effective_enrichment_ref(ref, result)
        source_kind = effective_ref.get("source_kind") or effective_ref.get("source_type")

        if ((source_kind == "book_like" or effective_ref.get("source_type") == "book")
            and not _article_like_resolution_candidate(effective_ref)) or effective_ref.get("isbn"):
            if via == "googlebooks":
                return result
            gb = _googlebooks(effective_ref)
            attempts.append({"via": "googlebooks", "enrichment_only": True, **gb})
            if gb.get("status") == "resolved":
                if gb.get("abstract"):
                    enriched["abstract"] = gb.get("abstract")
                    enriched["abstract_via"] = "googlebooks"
                if (not enriched.get("matched_authors")) and gb.get("matched_authors"):
                    enriched["matched_authors"] = gb.get("matched_authors")
                if enriched.get("book_availability") in (None, "unknown") and gb.get("book_availability"):
                    enriched["book_availability"] = gb.get("book_availability")
                    enriched["availability_note"] = gb.get("availability_note")
            return enriched

        if via == "europepmc":
            return result
        pmid = effective_ref.get("pmid")
        doi = effective_ref.get("doi")
        title = effective_ref.get("title")
        if not (pmid or doi or (title and effective_ref.get("source_type") != "book")):
            return result
        epmc = _europepmc(pmid, doi, title, effective_ref.get("year"))
        attempts.append({"via": "europepmc", "enrichment_only": True, **epmc})
        auxiliary_has_fulltext = _links_have_fulltext(enriched.get("auxiliary_fulltext_links"))
        if epmc.get("status") == "resolved":
            _copy_verified_identifiers(enriched, epmc)
            if not enriched.get("abstract") and epmc.get("abstract"):
                enriched["abstract"] = epmc.get("abstract")
                enriched["abstract_via"] = "europepmc"
            auxiliary_has_fulltext = _capture_auxiliary_fulltext_links(
                enriched,
                via="europepmc",
                links=epmc.get("fulltext_links"),
                candidate=epmc,
            )
            if enriched.get("oa_status") == "unknown" and epmc.get("oa_status") not in (None, "unknown"):
                enriched["oa_status"] = epmc.get("oa_status")
            if enriched.get("fulltext_exists") == "unknown" and epmc.get("fulltext_exists") in (True, False):
                enriched["fulltext_exists"] = epmc.get("fulltext_exists")
                enriched["fulltext_exists_refined_by"] = "europepmc"

        enrichers = list(resolver_modules.resolve_enrichers())
        start_index = 0

    deferred_enrichers: list[tuple[_http.ProviderCooldownDeferred, object]] = []
    has_fulltext = _links_have_fulltext(enriched.get("fulltext_links"))
    need_registry_enrichment = (not enriched.get("abstract")) or (not has_fulltext and not auxiliary_has_fulltext)
    if need_registry_enrichment:
        for index, module in enumerate(enrichers[start_index:], start=start_index):
            if enriched.get("abstract") and (has_fulltext or auxiliary_has_fulltext):
                break
            enricher = getattr(module, "enrich", None)
            if not callable(enricher):
                continue
            try:
                with resolver_modules.credential_scope_for_resolver(module):
                    enrichment = enricher(effective_ref)
            except _http.ProviderCooldownDeferred as exc:
                deferred_enrichers.append((exc, module))
                attempts.append({
                    "via": resolver_modules.origin_name(module) or str(getattr(module, "NAME", "resolver")),
                    "status": "unresolved", "enrichment_only": True,
                    "reason": "rate_limit_deferred (no request sent)",
                })
                continue
            if enrichment is None:
                continue
            enrichment_via = resolver_modules.origin_name(module) or str(getattr(module, "NAME", "resolver"))
            attempts.append({"via": enrichment_via, "enrichment_only": True, **enrichment})
            if enrichment.get("status") != "resolved":
                continue
            if not enriched.get("abstract") and enrichment.get("abstract"):
                enriched["abstract"] = enrichment.get("abstract")
                enriched["abstract_via"] = enrichment_via
            auxiliary_has_fulltext = (
                _capture_auxiliary_fulltext_links(
                    enriched,
                    via=enrichment_via,
                    links=enrichment.get("fulltext_links") or [],
                    candidate=enrichment,
                )
                or auxiliary_has_fulltext
            )
    if enriched.get("abstract") and (has_fulltext or auxiliary_has_fulltext):
        return enriched
    pubmed = _pubmed_enrich(effective_ref)
    if pubmed is not None:
        attempts.append({"via": "pubmed", "enrichment_only": True, **pubmed})
        _copy_verified_identifiers(enriched, pubmed)
        if not enriched.get("abstract") and pubmed.get("abstract"):
            enriched["abstract"] = pubmed.get("abstract")
            enriched["abstract_via"] = "pubmed"
        auxiliary_has_fulltext = (
            _capture_auxiliary_fulltext_links(
                enriched,
                via="pubmed",
                links=pubmed.get("fulltext_links"),
                candidate=pubmed,
            )
            or auxiliary_has_fulltext
        )
        if enriched.get("oa_status") == "unknown" and pubmed.get("oa_status") not in (None, "unknown"):
            enriched["oa_status"] = pubmed.get("oa_status")
        if enriched.get("fulltext_exists") == "unknown" and pubmed.get("fulltext_exists") in (True, False):
            enriched["fulltext_exists"] = pubmed.get("fulltext_exists")
            enriched["fulltext_exists_refined_by"] = pubmed.get("fulltext_exists_refined_by")
    has_fulltext = _links_have_fulltext(enriched.get("fulltext_links"))
    if (deferred_enrichers
            and not (enriched.get("abstract") and (has_fulltext or auxiliary_has_fulltext))):
        raise ProviderDeferredWork(
            kind="enrichment_provider", tasks=deferred_enrichers, state={
                "ref": dict(ref), "result": dict(result), "attempts": attempts,
                "enriched": dict(enriched), "effective_ref": dict(effective_ref),
                "auxiliary_has_fulltext": auxiliary_has_fulltext,
            },
        )
    return enriched


def _meta_subset(d: dict) -> dict:
    out = {
        "matched_title": d.get("matched_title"),
        "matched_authors": d.get("matched_authors"),   # books only (OpenLibrary)
        "abstract": d.get("abstract"),
        "retracted": bool(d.get("retracted", False)),
        "fulltext_exists": d.get("fulltext_exists", "unknown"),
        "oa_status": d.get("oa_status", "unknown"),
        "work_type": d.get("work_type"),
        "fulltext_links": d.get("fulltext_links") or [],
        "fulltext_availability": d.get("fulltext_availability"),
    }
    # Optional metadata: carried through only when present, so existing consumers and
    # fixtures that don't set them are unaffected.
    for k in (
        "book_availability",
        "availability_note",
        "fulltext_exists_refined_by",
        "fulltext_availability",
        "auxiliary_fulltext_links",
    ):
        if d.get(k) is not None:
            out[k] = d[k]
    if d.get("oa_declared_status") not in (None, "unknown"):
        out["oa_declared_status"] = d["oa_declared_status"]
    if d.get("oa_license_urls"):
        out["oa_license_urls"] = d["oa_license_urls"]
    if d.get("abstract_via") is not None:
        out["abstract_via"] = d.get("abstract_via")
    return out


def _resolution_provenance(ref: dict, result: dict) -> dict:
    if result.get("resolution_basis"):
        return {
            "resolution_basis": result.get("resolution_basis"),
            "existence_confidence": result.get("existence_confidence", "medium"),
        }
    via = result.get("via")
    if ref.get("doi") and via in ("crossref", "doi.org", "crossref+doi.org"):
        return {"resolution_basis": "doi", "existence_confidence": "high"}
    if ref.get("pmid") and via in ("europepmc", "pubmed"):
        return {"resolution_basis": "pmid", "existence_confidence": "high"}
    if ref.get("isbn") and via in ("openlibrary", "googlebooks", "openlibrary+googlebooks"):
        return {"resolution_basis": "isbn", "existence_confidence": "high"}
    if via in ("openlibrary", "googlebooks", "openlibrary+googlebooks"):
        return {"resolution_basis": "book_title_search", "existence_confidence": "low"}
    if via == "crossref_metadata":
        return {"resolution_basis": "metadata_search", "existence_confidence": "medium"}
    if ref.get("url"):
        return {"resolution_basis": "url", "existence_confidence": "low"}
    return {"resolution_basis": "none", "existence_confidence": "unknown"}


def _checks_completed(attempts: list[dict]) -> list[str]:
    checks = []
    for a in attempts:
        via = a.get("via")
        if via and via not in checks:
            checks.append(via)
    return checks


def _has_searchable_title(ref: dict) -> bool:
    if ref.get("title"):
        return True
    raw = ref.get("raw_entry") or ""
    return bool(_article_title_candidate(ref) or _extract_book_title(ref) or len(_sources._tokens(raw)) >= 6)


def _has_author(ref: dict) -> bool:
    raw = ref.get("raw_entry") or ""
    return bool(re.match(r"\s*[A-Z][^\W\d_][\w'’.-]+", raw))


def _has_venue(ref: dict) -> bool:
    raw = ref.get("raw_entry") or ""
    return bool(
        _coordinate_value(ref, "container")
        or re.search(r"\bJournal\b|\bJ\.\b|\bPress\b|\bSAGE\b|\bProceedings\b", raw, re.IGNORECASE)
        or re.search(r"\b\d+\s*\(\s*\d+\s*\)\s*[:,]\s*\d", raw)
    )


def _minimum_checks_completed(ref: dict, checks: list[str], status: str) -> bool:
    kind = ref.get("source_kind") or ref.get("source_type") or "unknown"
    authoritative_checks = {
        str(getattr(module, "RESOLVE_NAME", None) or getattr(module, "NAME", ""))
        for module in resolver_modules.authoritative_identifier_capable().values()
        if module.supports(ref)
    }
    if authoritative_checks.intersection(checks):
        return True
    if ref.get("doi"):
        return "crossref" in checks and ("doi.org" in checks or status != "not_found")
    if ref.get("pmid"):
        return "europepmc" in checks or "pubmed" in checks
    if ref.get("isbn") or kind == "book_like":
        return any(c in checks for c in ("openlibrary", "googlebooks", "openlibrary+googlebooks"))
    if kind == "article_like" or _article_coordinate_eligible(ref):
        return "crossref_metadata" in checks
    if kind in ("report_like", "webpage_like"):
        return bool(ref.get("url"))
    return False


INDEX_ERA_MIN = 2000
_REFERENCE_WORK_DOI_RE = re.compile(r"10\.1093/(?:acref|law:epil|ref:)", re.IGNORECASE)


def _pre_index_era(ref: dict) -> bool:
    try:
        year = int(str(ref.get("year"))[:4])
    except (TypeError, ValueError):
        return False
    return year < INDEX_ERA_MIN


def _reference_work_doi(ref: dict) -> bool:
    return bool(_REFERENCE_WORK_DOI_RE.search(str(ref.get("doi") or "")))


def _synthetic_reference_risk(ref: dict, status: str, evidence: dict, result: dict) -> dict:
    """Score the LLM-like pattern: plausible scholarly package, no corroboration."""
    if status == "unresolved" or not evidence.get("minimum_checks_completed"):
        return {
            "score": None,
            "band": "not_scored",
            "reason": "checks_incomplete",
            "signals": [],
        }
    if (
        status == "resolved"
        and evidence.get("existence_confidence") == "high"
        and (
            evidence.get("resolution_basis") in ("doi", "pmid", "isbn")
            or resolver_modules.authoritative_identifier_via(result.get("via"))
        )
        and (
            evidence.get("title_overlap") is None
            or evidence.get("title_overlap") >= TITLE_WARN_MAX
        )
    ):
        return {
            "score": 0,
            "band": "none",
            "reason": "strong_identifier_corroborated",
            "signals": ["strong_identifier_corroborated"],
        }
    score = 0
    signals = []

    kind = evidence.get("source_kind")
    indexability = evidence.get("indexability")
    pre_index_era = _pre_index_era(ref)
    mm = evidence.get("metadata_match") or result.get("metadata_match") or {}
    fallback = result.get("identifier_fallback") or evidence.get("identifier_fallback") or {}
    fallback_mm = fallback.get("metadata_match") or {}

    if status in ("not_found", "identifier_mismatch"):
        score += 2
        signals.append("hard_identifier_error")
    if kind == "article_like" and indexability == "high" and not pre_index_era:
        score += 2
        signals.append("high_index_article_like")
    if evidence.get("has_searchable_title") and evidence.get("has_author") and evidence.get("has_year"):
        score += 1
        signals.append("complete_core_bibliography")
    if evidence.get("has_venue"):
        score += 1
        signals.append("venue_or_journal_shape")
    if evidence.get("minimum_checks_completed"):
        score += 1
        signals.append("expected_checks_completed")

    candidate_mm = fallback_mm or mm
    if candidate_mm:
        mismatches = [
            candidate_mm.get("author_match") is False,
            candidate_mm.get("year_match") is False,
            (
                candidate_mm.get("venue_overlap") is not None
                and candidate_mm.get("venue_overlap") < 0.50
            ),
        ]
        if sum(1 for x in mismatches if x) >= 2:
            score += 2
            signals.append("plausible_candidate_bibliographically_incoherent")
        elif candidate_mm.get("score") is not None and candidate_mm.get("score") < METADATA_BORDERLINE_MIN:
            score += 1
            signals.append("best_candidate_below_borderline")
    if result.get("via") == "crossref_metadata" and result.get("status") == "resolved":
        score -= 3
        signals.append("metadata_corroborated")
    if fallback.get("status") == "resolved":
        score -= 3
        signals.append("identifier_fallback_corroborated")
    if indexability == "low" and not _article_coordinate_eligible(ref):
        score -= 3
        signals.append("low_indexability")
    if pre_index_era:
        score -= 3
        signals.append("pre_index_era")
    score = max(0, score)
    if score >= SYNTHETIC_RISK_HIGH_MIN:
        band = "high"
    elif score >= SYNTHETIC_RISK_MEDIUM_MIN:
        band = "medium"
    elif score:
        band = "low"
    else:
        band = "none"
    return {"score": score, "band": band, "signals": signals}


def _reference_evidence_profile(ref: dict, result: dict, status: str,
                                attempts: list[dict], overlap, prov: dict,
                                coverage_article_lookups: list[dict] | None = None,
                                coverage_payloads: list[dict] | None = None) -> dict:
    checks = _checks_completed(attempts)
    checks_incomplete = has_unresolved_identity_attempt(attempts)
    best = None
    for a in attempts:
        if a.get("matched_title") or a.get("matched_authors"):
            best = {
                "source": a.get("via"),
                "title": a.get("matched_title"),
                "title_overlap": title_overlap(a.get("matched_title"), ref.get("raw_entry")),
                "metadata_match": a.get("metadata_match"),
                "authors": a.get("matched_authors"),
                "status": a.get("status"),
                "reason": a.get("reason"),
            }
            break
    evidence = {
        "has_identifier": bool(ref.get("doi") or ref.get("pmid") or ref.get("isbn") or ref.get("url")),
        "has_searchable_title": _has_searchable_title(ref),
        "has_author": _has_author(ref),
        "has_year": bool(ref.get("year")),
        "has_venue": _has_venue(ref),
        "source_kind": ref.get("source_kind") or ref.get("source_type") or "unknown",
        "source_type_confidence": ref.get("source_type_confidence") or "unknown",
        "source_type_evidence": ref.get("source_type_evidence") or [],
        "indexability": ref.get("indexability") or "low",
        "checks_completed": checks,
        "minimum_checks_completed": (
            _minimum_checks_completed(ref, checks, status) and not checks_incomplete
        ),
        "best_candidate": best,
        "resolution_basis": prov.get("resolution_basis"),
        "existence_confidence": prov.get("existence_confidence"),
        "title_overlap": overlap,
        "metadata_match": result.get("metadata_match"),
        "identifier_fallback": result.get("identifier_fallback"),
    }
    if result.get("issue_attestations") is not None:
        evidence["issue_attestations"] = result["issue_attestations"]
    journal_authority = assess_local_journal(ref)
    if journal_authority is not None:
        evidence["journal_authority"] = journal_authority
    from .journal_authority import assess_local_journal_alias
    journal_alias_assessment = assess_local_journal_alias(ref)
    if journal_alias_assessment is not None:
        evidence["journal_alias_assessment"] = journal_alias_assessment
    evidence["bibliographic_adjudication"] = adjudicate_bibliographic_evidence(
        ref, status=status, evidence=evidence, result=result, attempts=attempts,
    )
    # Coverage is a dated operational fact, never a substitute for identity.
    if status == "unverified" and evidence["bibliographic_adjudication"].get("identity_status") not in {"identified", "identified_with_errors"}:
        from . import resolver_coverage
        authority = resolver_coverage.coverage_authority(ref)
        if authority is not None:
            try:
                observations = [{key: value for key, value in item.items() if key != "authority_hash"}
                                for item in resolver_coverage.evaluate(ref)]
                catalog = resolver_coverage.Catalog()
                try:
                    authority_hash = catalog.authority_hash(authority)
                    catalog_identity = catalog.identity()
                    payloads = [
                        {"sha256": item["response_sha256"], "media_type": "application/json", "body": catalog.payload(item["response_sha256"])}
                        for item in observations if item.get("response_sha256") is not None
                    ]
                finally:
                    catalog.close()
            except Exception as exc:
                # The ordinary typed check ledger remains the valid audit channel
                # when no real coverage catalog identity can be materialized.
                evidence["checks_completed"].append(f"coverage_refresh_incomplete:{type(exc).__name__}")
            else:
                article_lookups = list(coverage_article_lookups or [])
                payloads.extend(coverage_payloads or [])
                decision = resolver_coverage.suspicion(observations, compatible_identity=False, article_lookups=article_lookups)
                evidence["resolver_coverage"] = {
                    "authority": {**authority, "issns": list(authority["issns"]), "authority_hash": authority_hash},
                    "suspicion_level": decision["suspicion_level"], "conclusion": decision["reason"],
                    "article_lookups": article_lookups, "observations": observations,
                    "catalog": catalog_identity, "payloads": payloads,
                }
    from .resolver_coverage import bibliographic_suspicion
    suspicion = bibliographic_suspicion(
        ref, status=status, attempts=attempts,
        alias_assessment=evidence.get("journal_alias_assessment"),
        observations=((evidence.get("resolver_coverage") or {}).get("observations") or ()),
        compatible_identity=(
            evidence["bibliographic_adjudication"].get("identity_status")
            in {"identified", "identified_with_errors"}
        ),
        adjudication=evidence["bibliographic_adjudication"],
    )
    if suspicion["suspicion_level"] != "none":
        evidence["bibliographic_suspicion"] = suspicion
    evidence["synthetic_reference_risk"] = _synthetic_reference_risk(
        ref, status, evidence, result)
    return evidence


def _reference_status_tag(ref: dict, status: str, evidence: dict, result: dict) -> tuple[str, str, str]:
    synthetic = evidence.get("synthetic_reference_risk") or {}
    adjudication = evidence.get("bibliographic_adjudication") or {}
    issue_attestations = evidence.get("issue_attestations") or []
    issue_present = any(item.get("status") in {"complete", "enumerated"}
                        and item.get("target_status") == "present"
                        for item in issue_attestations if isinstance(item, dict))
    issue_absent = any(item.get("status") == "complete"
                       and item.get("target_status") == "absent"
                       for item in issue_attestations if isinstance(item, dict)) and adjudication.get("outcome") == "refuted"
    coordinate_occupied = any(
        item.get("kind") == "coordinate_occupied_by_other_work"
        for item in adjudication.get("refutations") or ()
        if isinstance(item, dict)
    )
    pre_index_era = _pre_index_era(ref)
    if status in ("not_found", "identifier_mismatch"):
        fallback = result.get("identifier_fallback") or {}
        if (
            (fallback.get("status") == "resolved" or issue_present)
            and adjudication.get("outcome") == "identified_with_errors"
        ):
            return "verified_with_identifier_error", "medium", (
                "strong identifier failed or points elsewhere, but independent metadata "
                "corroborates the source identity"
            )
        if issue_absent:
            return "suspected_fabricated", "high", (
                "the declared identifier is invalid or points elsewhere, no compatible "
                "correction was identified, and the attested complete issue excludes the work"
            )
        mm = fallback.get("metadata_match") or {}
        if mm.get("score") is not None and mm.get("score") >= METADATA_BORDERLINE_MIN:
            return "identifier_error_weak_metadata", "medium", (
                "strong identifier failed or points elsewhere; independent metadata is "
                "borderline, so this is not counted as suspected fabrication"
            )
        if _reference_work_doi(ref):
            return "unverified_low_indexability", "low", (
                "identifier is an OUP reference-work DOI with limited resolver coverage"
            )
        if (
            fallback.get("status") in ("unverified", "not_found")
            and adjudication.get("outcome") == "refuted"
        ):
            if synthetic.get("band") == "high":
                return "suspected_fabricated", "high", (
                    "hard identifier error plus failed independent corroboration; "
                    "pattern is consistent with a synthetic plausible reference"
                )
            return "suspected_fabricated", "high", (
                "strong identifier failed or points elsewhere, and independent metadata "
                "did not corroborate the source identity"
            )
        return "identifier_error_unresolved", "unknown", (
            "strong identifier failed or points elsewhere; independent corroboration was "
            "not completed"
        )
    if status == "resolved":
        mm = evidence.get("metadata_match") or {}
        if adjudication.get("identity_status") not in {"identified", "identified_with_errors"}:
            return "weak_metadata_match", "low", (
                "a metadata candidate was found, but deterministic identity admission failed"
            )
        if (result.get("via") == "crossref_metadata"
                and evidence.get("source_kind") == "article_like"
                and evidence.get("indexability") == "high"
                and mm.get("score") is not None
                and mm.get("score") < 0.55):
            if mm.get("score") >= METADATA_BORDERLINE_MIN:
                return "weak_metadata_match", "low", "metadata candidate is borderline; not counted as suspected fabrication"
            if pre_index_era:
                return "weak_metadata_match", "low", (
                    "title found but citation details uncorroborated before reliable index coverage"
                )
            return "weak_metadata_match", "low", (
                "title candidate found, but its bibliographic identity was not corroborated; "
                "candidate mismatch is not proof that the cited work was fabricated"
            )
        conf = evidence.get("existence_confidence")
        if conf == "high":
            return "verified", "low", "strong identifier or authoritative resolver confirmed the source"
        return "weakly_verified", "low", "metadata/catalog match found, but identifier-level evidence is absent"
    if status == "unresolved":
        return "unverified", "unknown", "transient resolver/network condition; checks incomplete"
    if status == "unverified":
        if adjudication.get("outcome") == "refuted" and coordinate_occupied:
            return "suspected_fabricated", "high", (
                "no compatible work was reconstructed and PubMed positively identifies "
                "another work at the cited journal, year, volume, and starting page"
            )
        if issue_absent:
            return "suspected_fabricated", "high", (
                "no compatible work was reconstructed and an attested complete issue "
                "does not contain the cited source"
            )
        bibliographic_suspicion = evidence.get("bibliographic_suspicion") or {}
        if bibliographic_suspicion.get("suspicion_level") == "elevated":
            if bibliographic_suspicion.get("conclusion") == "complete_issue_absence_with_incomplete_identity_checks":
                return "unverified", "unknown", (
                    "an official complete issue inventory excludes the cited work, "
                    "but independent identity checks remain incomplete; this is "
                    "an elevated non-diagnostic suspicion, not a fabrication finding"
                )
            return "unverified", "unknown", (
                "completed independent bibliographic checks did not identify a compatible "
                "source; this is an elevated non-diagnostic suspicion, not a fabrication finding"
            )
        if evidence.get("indexability") != "high" and not _article_coordinate_eligible(ref):
            return "unverified_low_indexability", "low", "source type has weak catalog/index coverage"
        if pre_index_era:
            return "unverified_low_indexability", "low", (
                "published before reliable DOI/index coverage; index absence is not evidence of fabrication"
            )
        if ((evidence.get("source_kind") == "article_like" or _article_coordinate_eligible(ref))
                and evidence.get("minimum_checks_completed")
                and evidence.get("has_searchable_title")
                and evidence.get("has_author")
                and evidence.get("has_year")):
            mm = evidence.get("metadata_match") or {}
            if mm.get("score") is not None and mm.get("score") >= METADATA_BORDERLINE_MIN:
                return "weak_metadata_match", "low", "metadata candidate is borderline; not counted as suspected fabrication"
            return "unverified", "unknown", (
                "article-like reference remained unverified; recognizing the declared "
                "journal does not prove that its article inventory was exhaustively searched"
            )
        return "unverified", "unknown", "not enough completed evidence to assign a stronger tag"
    return "unverified", "unknown", "status not classified by evidence tagger"


def resolve(ref: dict) -> dict:
    with _perf.span("resolve", None):
        return _finalize_discovery(ref, _discover_resolution(ref))


def _discover_resolution(ref: dict) -> dict:
    """Perform candidate discovery without identifier-driven enrichment.

    This private phase boundary is used by the run phase to prime identifiers
    discovered from title searches together.  ``resolve`` remains the public,
    sequential-compatible entry point.
    """
    return _resolve_impl(ref, discovery_only=True)


def _finalize_discovery(ref: dict, state: dict) -> dict:
    """Finish a previously discovered resolution state deterministically."""
    if (
        type(state) is not dict
        or set(state) != {"result", "attempts"}
        or type(state["result"]) is not dict
        or type(state["attempts"]) is not list
        or any(type(attempt) is not dict for attempt in state["attempts"])
    ):
        raise ValueError("invalid resolve discovery state")
    return _finalize_resolution(ref, dict(state["result"]), list(state["attempts"]))


def _run_optional_resolvers(
    ref: dict, result: dict | None, attempts: list[dict], *, reason: str,
    modules: list[object] | None = None, start_index: int = 0,
    discovery_only: bool,
) -> dict | None:
    """Run optional resolvers without letting one provider stall the suffix."""
    plan = modules if modules is not None else list(resolver_policy.optional_resolver_modules(
        attempts, load_order=_load_fallback_order, registry=_resolver_registry, reason=reason,
    ))
    deferred: list[tuple[_http.ProviderCooldownDeferred, object]] = []
    for index, module in enumerate(plan[start_index:], start=start_index):
        try:
            r = _call_registry_resolver(module, ref)
        except _http.ProviderCooldownDeferred as exc:
            # The provider did not send this request.  Continue the independent
            # suffix and retain only this provider's work for a later FIFO turn.
            # A later successful resolution makes the pending probe unnecessary.
            deferred.append((exc, module))
            attempts.append({
                "via": resolver_modules.origin_name(module) or str(getattr(module, "NAME", "resolver")),
                "status": "unresolved", "reason": "rate_limit_deferred (no request sent)",
            })
            continue
        if r is None:
            continue
        attempts.append(r)
        if (
            r.get("via") == "tac_search" and r.get("status") == "resolved"
            and r.get("resolution_basis") == "canonical_source"
            and r.get("existence_confidence") == "high"
        ):
            result = r
            continue
        preferred = _adopt_preferred_metadata_candidate(ref, result, r)
        if preferred is not None and preferred is not result:
            result = preferred
            continue
        result = resolver_policy.capture_same_work_companion_links(
            ref, result, r,
            article_title_candidate=_article_title_candidate,
            title_key=_title_key, title_key_contains=_title_key_contains,
            metadata_has_canonical_host=_metadata_has_canonical_host,
            capture_auxiliary_fulltext_links=_capture_auxiliary_fulltext_links,
        )
        if r["status"] == "unverified" and result is None:
            result = r
    if deferred and (result is None or result.get("status") != "resolved"):
        # A provider cooldown is not a reference-wide barrier.  Complete the
        # independent discovery tail now; if it resolves the work, the deferred
        # provider probe is obsolete.  Otherwise retain only the provider-local
        # probe and remember that the tail must not be replayed after wake-up.
        tail_state = _complete_discovery_after_optional(
            ref, result, attempts, discovery_only=True,
        )
        result = tail_state["result"]
        attempts = tail_state["attempts"]
        if result.get("status") == "resolved":
            return result
        raise ProviderDeferredWork(
            kind="discovery_provider", tasks=deferred, state={
                "ref": dict(ref), "result": dict(result) if result else None,
                "attempts": attempts, "discovery_only": discovery_only,
                "tail_complete": True,
            },
        )
    return result


def _apply_optional_result(ref: dict, result: dict | None, attempts: list[dict], r: dict | None) -> dict | None:
    """Apply one resumed optional result without rerunning its completed suffix."""
    if r is None:
        return result
    attempts.append(r)
    if (r.get("via") == "tac_search" and r.get("status") == "resolved"
            and r.get("resolution_basis") == "canonical_source"
            and r.get("existence_confidence") == "high"):
        return r
    preferred = _adopt_preferred_metadata_candidate(ref, result, r)
    if preferred is not None and preferred is not result:
        return preferred
    result = resolver_policy.capture_same_work_companion_links(
        ref, result, r, article_title_candidate=_article_title_candidate,
        title_key=_title_key, title_key_contains=_title_key_contains,
        metadata_has_canonical_host=_metadata_has_canonical_host,
        capture_auxiliary_fulltext_links=_capture_auxiliary_fulltext_links,
    )
    return r if r["status"] == "unverified" and result is None else result


class ProviderDeferredWork(_http.SemanticScholarCooldownDeferred):
    """In-memory provider-local continuations for one already-processed reference."""

    def __init__(self, *, kind: str, tasks: list[tuple[_http.ProviderCooldownDeferred, object]], state: dict):
        providers = [exc.provider for exc, _module in tasks]
        if len(set(providers)) != len(providers):
            raise ValueError("duplicate provider-local deferred task")
        _http.ProviderCooldownDeferred.__init__(
            self, not_before=min(exc.not_before for exc, _module in tasks), provider="_provider_deferred"
        )
        self.kind = kind
        self.tasks = {exc.provider: {"exc": exc, "module": module} for exc, module in tasks}
        self.state = state
        self.terminal = False

    def providers(self):
        return tuple(self.tasks)

    def task(self, provider):
        return self.tasks.get(provider)


def exhaust_provider_cooldown(
    deferred: _http.ProviderCooldownDeferred, provider: str, *, reason: str,
) -> dict:
    """Close one bounded provider continuation without cancelling siblings."""
    if not isinstance(deferred, ProviderDeferredWork):
        return {"_provider_deferred_cancelled": True}
    task = deferred.tasks.pop(provider, None)
    if task is None:
        return {"_provider_deferred_cancelled": True}
    via = resolver_modules.origin_name(task["module"]) or provider
    exhaustion = {
        "via": via,
        "status": "unverified",
        "reason": reason,
        "error_type": "rate_limit",
        "error_reason": reason,
        "retryable": False,
    }
    deferred.state["attempts"].append(exhaustion)
    if deferred.kind == "discovery_provider" and deferred.state["result"] is None:
        # The absence of a response is uncertainty, never negative existence
        # evidence. Preserve the explicit bound in the final resolution while
        # allowing the normal deterministic suffix to replace it on success.
        deferred.state["result"] = dict(exhaustion)
    if deferred.tasks:
        raise deferred
    if deferred.kind == "discovery_provider":
        if deferred.state.get("tail_complete"):
            discovery_state = {
                "result": deferred.state["result"],
                "attempts": deferred.state["attempts"],
            }
            if deferred.state["discovery_only"]:
                return discovery_state
            return _finalize_discovery(deferred.state["ref"], discovery_state)
        return _complete_discovery_after_optional(
            deferred.state["ref"], deferred.state["result"], deferred.state["attempts"],
            discovery_only=deferred.state["discovery_only"],
        )
    return _finalize_resolution(
        deferred.state["ref"], deferred.state["result"], deferred.state["attempts"],
        enrichment_continuation={"kind": "enrichment_done", "enriched": deferred.state["enriched"]},
    )


def _complete_discovery_after_optional(
    ref: dict, result: dict | None, attempts: list[dict], *, discovery_only: bool,
) -> dict:
    """The discovery tail which must run exactly once after optional resolvers."""
    isbn = ref.get("isbn")
    url = ref.get("url")
    source_type = ref.get("source_type", "unknown")
    source_kind = ref.get("source_kind")
    article_candidate = _article_like_resolution_candidate(ref)
    result = _promote_validated_identifier(ref, result, attempts)
    if result is not None and result.get("status") == "resolved":
        for module in resolver_policy.canonical_companion_modules(
            attempts, load_order=_load_fallback_order, registry=_resolver_registry,
        ):
            r = _call_registry_resolver(module, ref)
            if r is None:
                continue
            attempts.append(r)
            result = resolver_policy.capture_same_work_companion_links(
                ref, result, r,
                article_title_candidate=_article_title_candidate,
                title_key=_title_key, title_key_contains=_title_key_contains,
                metadata_has_canonical_host=_metadata_has_canonical_host,
                capture_auxiliary_fulltext_links=_capture_auxiliary_fulltext_links,
            )
    if ((result is None or (source_kind == "book_like" and result.get("status") != "resolved"
            and not result.get("_hard_identifier_error")))
            and (isbn or source_type == "book" or source_kind == "book_like")):
        r = _resolve_book(ref)
        attempts.append(r)
        if r["status"] in ("resolved", "not_found", "unverified"):
            result = r
    if result is None or (result.get("status") != "resolved" and not result.get("_hard_identifier_error")):
        for identifier_key in ("un_digital_library", "courtlistener"):
            if result is not None and result.get("status") == "resolved":
                break
            module = _resolver_registry().get(identifier_key)
            if module is None or not getattr(module, "supports", lambda _r: False)(ref):
                continue
            found = _call_registry_resolver(module, ref)
            if found is not None:
                attempts.append(found)
                if found.get("status") == "resolved":
                    result = found
    if result is None or (result.get("status") != "resolved" and not result.get("_hard_identifier_error")):
        inst_module = _resolver_registry().get("institutional_search")
        if inst_module is not None and getattr(inst_module, "supports", lambda _r: False)(ref):
            inst = _call_registry_resolver(inst_module, ref)
            if inst is not None:
                attempts.append(inst)
                if inst.get("status") == "resolved":
                    result = inst
    if ((result is None or (result.get("status") != "resolved" and not result.get("_hard_identifier_error")))
            and article_candidate):
        sa_module = _resolver_registry().get("scholar_archive_search")
        if sa_module is not None:
            sa = _call_registry_resolver(sa_module, ref)
            if sa is not None:
                attempts.append(sa)
                if sa.get("status") == "resolved":
                    result = sa
    if result is None:
        if attempts and any(a.get("status") == "unresolved" for a in attempts):
            reasons = "; ".join(f"{a.get('via')}: {a.get('reason')}" for a in attempts)
            result = {"status": "unresolved", "via": attempts[-1].get("via"),
                      "reason": f"all attempts failed (transient): {reasons}"}
        elif url:
            result = {"status": "unverified", "via": None,
                      "reason": "webpage: retrieval/verification delegated to agent WebFetch"}
        else:
            result = {"status": "unverified", "via": None,
                      "reason": "no identifier (DOI/PMID/ISBN/URL): search online to verify"}
    result = _abbreviated_title_guard(ref, result, attempts)
    resolved_identifier = _resolved_identifier_summary(result) or {}
    is_unique_same_work_correction = (
        resolved_identifier.get("type") == "doi"
        and bool(resolved_identifier.get("value"))
        and resolved_identifier.get("validated_via")
        == "crossref:unique_same_work_correction"
    )
    if (result.get("status") == "resolved" and result.get("resolution_basis") == "metadata_search"
            and _genuine_author_conflict(result.get("metadata_match"))
            and not is_unique_same_work_correction):
        result = {"status": "unverified", "via": result.get("via"),
                  "matched_title": result.get("matched_title"),
                  "reason": "title match contradicted by first author — likely a different work",
                  "resolution_basis": "metadata_search", "existence_confidence": "low",
                  "metadata_match": result.get("metadata_match")}
    if discovery_only:
        return {"result": result, "attempts": attempts}
    return _finalize_resolution(ref, result, attempts)


def _resolve_impl(ref: dict, *, discovery_only: bool = False) -> dict:
    # Ensure Retraction Watch data is loaded with the configured email.
    _rw.load(_http._CONTACT_EMAIL or "")
    # The declared DOI is checked before anything else, so it is normalised before
    # anything else: the raw field used to go to Crossref exactly as parsed, and a
    # trailing markup character was enough to have a real work reported as a
    # non-existent identifier - the harshest verdict the pipeline issues.
    doi = _normalize_doi_value(ref.get("doi"))
    pmid = ref.get("pmid")
    isbn = ref.get("isbn")
    title = ref.get("title")
    url = ref.get("url")
    year = ref.get("year")
    raw_entry = ref.get("raw_entry")
    source_type = ref.get("source_type", "unknown")
    source_kind = ref.get("source_kind")
    article_candidate = _article_like_resolution_candidate(ref)

    # Strip arXiv version suffix from DOI (v2 → removed): the canonical DOI
    # points to the paper, not a specific version.
    if doi:
        doi = re.sub(r"(10\.48550/arXiv\.\d{4}\.\d{4,5})v\d+", r"\1", doi)

    attempts = []
    result = None

    # 0) Explicit non-DOI identifiers whose own authority adapter recognizes
    # the citation.  A provider may supersede a parser-derived scheme: JSTOR's
    # historic ``/stable/10.2307/...`` spelling, for example, is not a DOI even
    # though a generic DOI regex can extract it as one.
    superseded_schemes: set[str] = set()
    authoritative_results: list[dict] = []
    for module in resolver_modules.authoritative_identifier_capable().values():
        try:
            supported = module.supports(ref)
        except Exception as exc:
            attempts.append({
                "status": "unresolved",
                "via": getattr(module, "RESOLVE_NAME", None) or getattr(module, "NAME", None),
                "reason": f"authoritative identifier support check failed: {type(exc).__name__}",
            })
            continue
        if not supported:
            continue
        superseded_schemes.update(
            resolver_modules.superseded_identifier_schemes(module, ref)
        )
        authoritative = _call_registry_resolver(module, ref)
        if authoritative is None:
            authoritative = {
                "status": "unresolved",
                "via": getattr(module, "RESOLVE_NAME", None) or getattr(module, "NAME", None),
                "reason": "authoritative identifier adapter returned no result",
            }
        attempts.append(authoritative)
        authoritative_results.append(authoritative)

    resolved_authorities = [
        item for item in authoritative_results if item.get("status") == "resolved"
    ]
    authority_identities = {
        (
            str((item.get("resolved_identifier") or {}).get("type") or ""),
            str((item.get("resolved_identifier") or {}).get("value") or ""),
        )
        for item in resolved_authorities
    }
    if len(authority_identities) == 1:
        result = resolved_authorities[0]
    elif len(authority_identities) > 1:
        result = {
            "status": "unverified",
            "via": "authoritative_identifier_registry",
            "reason": "explicit authoritative identifiers resolve to different records",
            "resolution_basis": "identifier_conflict",
            "existence_confidence": "low",
        }
    elif authoritative_results:
        # Preserve one completed attempt as the current state. Subsequent
        # metadata/correction searches may still identify the intended work.
        result = authoritative_results[0]

    # 1) DOI → Crossref. 404 = not_found (hard identifier error).
    if doi and "doi" not in superseded_schemes:
        r = _crossref_resolver_module().resolve_doi(doi, ref=ref)
        attempts.append(r)
        if r["status"] == "not_found":
            h = _doi_handle(doi)
            attempts.append(h)
            if h["status"] == "resolved":
                result = h
            elif h["status"] == "not_found":
                result = {"status": "not_found", "via": "crossref+doi.org",
                          "reason": "DOI not found on Crossref or DOI resolver "
                                    "(hard identifier error)",
                          "_hard_identifier_error": True}
        elif r["status"] == "resolved":
            result = r
            # Crossref rarely flags abstract-only records, so a conference/proceedings
            # hit whose full-text existence is still "unknown" would otherwise be chased
            # as if a (paywalled) full text existed. Defer to Europe PMC, which exposes
            # the 'meeting abstract' pubType: if it can decide, adopt its determination.
            if (r["status"] == "resolved" and r.get("fulltext_exists") == "unknown"
                    and _is_conference_type(r.get("work_type"))):
                epmc = _europepmc(pmid, doi, title, year)
                attempts.append(epmc)
                ft = epmc.get("fulltext_exists")
                if ft in (True, False):
                    result = dict(r)
                    result["fulltext_exists"] = ft
                    if epmc.get("oa_status") and epmc["oa_status"] != "unknown":
                        result["oa_status"] = epmc["oa_status"]
                    result["fulltext_exists_refined_by"] = "europepmc"

    # 1.5) Clinical-trial registry ids (NCT/ISRCTN/EudraCT) are strong identifiers.
    #      The cited registry page is usually a JS single-page app with no
    #      extractable text, so resolve the trial from the registry API instead.
    if result is None:
        ct_module = _resolver_registry().get("clinical_trials_search")
        if ct_module is not None:
            ct = _call_registry_resolver(ct_module, ref)
            if ct is not None and ct.get("status") == "resolved":
                attempts.append(ct)
                result = ct

    # 2) PMID/DOI/title → Europe PMC (skipped for pure books: no PMIDs there).
    acl_module = _acl_resolver_module()
    acl_host_match = getattr(acl_module, "_is_acl_anthology_url", None) if acl_module is not None else None
    if result is None and callable(acl_host_match) and acl_host_match(url):
        r = _call_registry_resolver(acl_module, ref)
        if r is not None:
            attempts.append(r)
            if r["status"] == "resolved":
                result = r

    if result is None and (pmid or doi or (title and article_candidate)):
        r = _europepmc(pmid, doi, title, year)
        attempts.append(r)
        if r["status"] == "resolved":
            # For title-only EPMC matches (no strong ID), verify the title actually
            # overlaps with the cited entry before accepting. EPMC full-text search can
            # return superficially similar titles that are different works.
            if not pmid and not doi:
                epmc_overlap = title_overlap(r.get("matched_title"), raw_entry)
                if epmc_overlap is not None and epmc_overlap < 0.30:
                    # Weak title match — do NOT stop here; let Crossref try.
                    pass
                elif (year is not None and r.get("matched_year") is not None
                      and str(year) != str(r["matched_year"])):
                    # Year mismatch on a title-only EPMC hit — wrong paper.
                    pass
                else:
                    result = r
            else:
                result = r
        elif r["status"] == "unverified":
            # Title-only EPMC search returned nothing — weak signal, do NOT stop here.
            # Crossref bibliographic search has different coverage and may succeed.
            pass
        elif r["status"] == "_empty_strong":
            # Strong ID (PMID) with no EPMC match: EPMC is not exhaustive. If we have
            # a PMID, confirm on PubMed: absent => not_found; exists => resolved (no
            # abstract); network down => unverified (never accuse due to network).
            if pmid:
                ex = pubmed_exists(pmid)
                pubmed_status = {
                    "exists": "resolved", "absent": "not_found", "unknown": "unresolved",
                }[ex]
                attempts.append({
                    "status": pubmed_status,
                    "via": "pubmed",
                    "reason": f"esummary: {ex}",
                })
                if ex == "absent":
                    result = {"status": "not_found", "via": "pubmed",
                              "reason": "PMID not found on PubMed (hard identifier error)"}
                elif ex == "exists":
                    result = {"status": "resolved", "via": "pubmed",
                              "reason": "PMID found on PubMed (not indexed in Europe PMC)"}
                else:
                    result = {"status": "unverified", "via": "pubmed",
                              "reason": "PMID unconfirmable (PubMed unreachable) — manual verification required"}
            else:
                # DOI only, already tried on Crossref without a conclusive result
                result = {"status": "unverified", "via": "europepmc",
                          "reason": "no match on Europe PMC; DOI non-conclusive"}

    # 3) ISBN / book title → OpenLibrary + Google Books (two-catalog corroboration).
    #    Runs when: no result yet AND (explicit ISBN present OR source_type is book).
    #    Well-formed ISBN absent from BOTH catalogs = hard identifier error (not_found).
    #    Title-only absent from both = unverified + existence_corroboration=
    #    'searched_not_found' (orange warning, never red).
    #
    #    NOTE: Crossref metadata search now runs for article_candidate even if url
    #    is present. The URL is kept as an additional fetch candidate, not a
    #    resolver substitute. This fixes unresolved article references that have
    #    publisher URLs but lack DOI identifiers. Risk: metadata search on WHO
    #    reports may find commentary instead of the report itself, mitigated by
    #    title overlap (>0.85) and author conflict checks below.
    # 2.5) An explicitly configured, local metadata accelerator may avoid a
    # remote Crossref search.  It has no authority over identity: its DOI is
    # independently validated before its result is retained.
    if result is None and article_candidate and not url:
        for module in resolver_policy.local_accelerator_modules(
            attempts,
            load_order=_load_fallback_order,
            registry=_resolver_registry,
        ):
            r = _call_registry_resolver(module, ref)
            if r is None:
                continue
            attempts.append(r)
            candidate = _promote_validated_identifier(ref, r, attempts)
            identifier = _resolved_identifier_summary(candidate)
            if (
                candidate is not None
                and candidate.get("status") == "resolved"
                and identifier is not None
                and identifier.get("type") == "doi"
            ):
                # Crossref confirms the DOI, not biblio-glutton's optional
                # DOI-to-PubMed mappings.  Keep PMID/PMCID audit-only in the
                # provider attempt until an exact authority validates them.
                confirmed_doi = _normalize_doi_value(identifier.get("value"))
                if confirmed_doi:
                    candidate = dict(candidate)
                    candidate["identifiers"] = {"doi": confirmed_doi}
                result = candidate
                break

    if (result is None and article_candidate
            and (source_type == "article" or _article_title_candidate(ref))):
        r = _crossref_resolver_module().discover(ref)
        attempts.append(r)
        if r["status"] in ("resolved", "unverified"):
            result = r
    result = _promote_validated_identifier(ref, result, attempts)

    # 4) OpenAlex title search — universal index covering journals, arXiv, conference
    #    papers, bioRxiv, and more.  Called when:
    #      - no result yet,
    #      - Crossref returned unverified with low confidence, OR
    #      - Crossref returned a weak resolved from metadata_search (title overlap
    #        below 0.85 — may be the wrong paper, e.g. "Grammar in the foreign
    #        language classroom" instead of "Grammar as a Foreign Language").
    #    OpenAlex `filter=title.search:` does phrase matching on titles and
    #    reliably finds the right paper even with common keywords.
    #
    #    NOTE: Now runs for article_candidate even if url present (same risk
    #    mitigation as Crossref above).
    if article_candidate:
        need_openalex = result is None
        if not need_openalex:
            st = result.get("status")
            conf = result.get("existence_confidence")
            cr_overlap = _result_title_overlap(result)
            if st == "unverified" and conf == "low":
                need_openalex = True
            elif st == "resolved":
                # Always run OpenAlex as a second opinion for resolved results
                # that came from a title-based search (not a direct DOI lookup).
                # Crossref metadata/bibliographic searches can return records with
                # matching titles but wrong DOIs (e.g. Penn Treebank assigned a
                # DTIC report DOI, Grammar as FL assigned a Taylor & Francis
                # chapter DOI).  OpenAlex serves as an independent verifier.
                if (
                    result.get("resolution_basis") == "metadata_search"
                    and _resolved_identifier_summary(result) is None
                ):
                    need_openalex = True
            elif (st == "unverified" and conf == "medium"
                  and result.get("via") != "openalex_search"):
                # Crossref metadata_search with medium confidence but low overlap
                # may return "unverified"; OpenAlex can still find the paper.
                need_openalex = True
        if need_openalex:
            # Try OpenAlex first; on 429, cycle through every other configured
            # resolver (arXiv, CORE, DataCite, Crossref metadata) in the order
            # defined by core/resolve/providers.json.  After all fallbacks are
            # exhausted, wait and retry OpenAlex — 429 is transient.
            r = _try_resolvers_with_fallback(
                "openalex_search", ref,
                result=result, attempts=attempts,
                can_use_title_search=bool(_article_title_candidate(ref)),
            )
            if r is not None and r.get("status") == "resolved":
                if result is None or result.get("status") != "resolved":
                    result = r
                else:
                    preferred = _adopt_preferred_metadata_candidate(ref, result, r)
                    if preferred is not None and preferred is not result:
                        result = preferred
            elif r is not None and r["status"] == "unverified" and result is None:
                result = r
    result = _promote_validated_identifier(ref, result, attempts)

    # 4.5) arXiv API title search — universal last-resort for papers that should be on
    #      arXiv but lack a DOI in the citation.  Many machine learning papers cite
    #      arXiv preprints without including the arXiv DOI.  This is the deepest
    #      fallback: when Crossref, OpenAlex, Europe PMC all come up empty, arXiv
    #      often has the right paper because it was posted there first.
    need_arxiv = result is None
    if not need_arxiv and result is not None:
        # Also try arXiv when the current result is too weak to be useful:
        # - unverified with low/medium confidence (no confirmed match)
        # - resolved but to a clearly wrong work (title overlap < 0.85)
        # - unresolved (network error)
        # - DOI is an ACL/ACM paywalled DOI (many are also on arXiv OA)
        st = result.get("status")
        cr_overlap = _result_title_overlap(result)
        if st == "unverified":
            need_arxiv = True
        elif st == "resolved" and cr_overlap < 0.85:
            # Crossref resolved but title match is less than excellent;
            # arXiv might have the canonical version with a better DOI.
            need_arxiv = True
        elif st == "unresolved":
            need_arxiv = True
        elif st == "resolved":
            # Even for well-matched resolved results, check if the DOI is a
            # paywalled publisher DOI.  Many CS papers (ACL, IEEE/CVPR, ACM)
            # are also posted on arXiv and the OA version is preferable.
            # The DOI may be in the result directly OR in the fulltext_links
            # (Crossref bibliographic search puts it only in fulltext_links).
            resolved_identifier = _resolved_identifier_summary(result) or {}
            resolved_doi = result.get("doi", "") or (
                resolved_identifier.get("value")
                if resolved_identifier.get("type") == "doi" else ""
            )
            official_oa_doi_prefixes = _official_oa_doi_prefixes()
            if not resolved_doi:
                for attempt in result.get("attempts", []):
                    for link in attempt.get("fulltext_links", []):
                        u = link.get("url", "")
                        for prefix in official_oa_doi_prefixes:
                            m = re.search(rf"{re.escape(prefix)}[^/\s]+", u, re.IGNORECASE)
                            if m:
                                resolved_doi = m.group(0)
                                break
                        if resolved_doi:
                            break
                    if resolved_doi:
                        break
            # The discovery loop above is best-effort and may find no DOI even
            # if attempts are present; guard against None before string operations.
            if resolved_doi and any(resolved_doi.lower().startswith(prefix) for prefix in official_oa_doi_prefixes):
                need_arxiv = True
    if (need_arxiv and article_candidate
            and _article_title_candidate(ref)):
        arxiv_module = _resolver_registry().get("arxiv_search")
        if arxiv_module is not None:
            arxiv_result = _call_registry_resolver(arxiv_module, ref)
            if arxiv_result is not None:
                attempts.append(arxiv_result)
                if arxiv_result["status"] == "resolved":
                    preferred = _adopt_preferred_metadata_candidate(ref, result, arxiv_result)
                    if preferred is not None:
                        result = preferred
    result = _promote_validated_identifier(ref, result, attempts)

    # 4.6) Semantic Scholar + Lens — optional metadata resolvers, consulted when
    #      the result is still missing or only a weak metadata match.  (They are
    #      also reached automatically as 429-fallbacks via the OpenAlex chain;
    #      this block additionally covers the weak-but-not-rate-limited case.)
    optional_stage_reason = resolver_policy.optional_stage_reason(
        ref,
        result,
        article_title_candidate=_article_title_candidate,
        result_title_overlap=_result_title_overlap,
        result_has_fulltext=_result_has_fulltext,
        result_doi=_result_doi,
        normalize_doi=_normalize_doi_value,
        normalize_content_version=getattr(_sources, "normalize_content_version", None),
        host_is_preprint=getattr(_sources, "host_is_preprint", None),
        ref_is_preprint=getattr(_sources, "ref_is_preprint", None),
        article_like_resolution_candidate=_article_like_resolution_candidate,
        non_record_versions=getattr(_sources, "NON_RECORD_VERSIONS", ()),
    )
    if optional_stage_reason:
        result = _run_optional_resolvers(
            ref, result, attempts, reason=optional_stage_reason,
            discovery_only=discovery_only,
        )
    return _complete_discovery_after_optional(
        ref, result, attempts, discovery_only=discovery_only,
    )


def _finalize_resolution(
    ref: dict, result: dict, attempts: list[dict], *, enrichment_continuation: dict | None = None,
) -> dict:
    """Apply enrichment and admission to a completed discovery state."""
    doi = _normalize_doi_value(ref.get("doi"))
    pmid = ref.get("pmid")
    isbn = ref.get("isbn")
    raw_entry = ref.get("raw_entry")
    result = _attest_repository_record(ref, result, attempts)
    result = _enrich_resolved_metadata(
        ref, result, attempts, continuation=enrichment_continuation,
    )
    result = _annotate_primary_fulltext_links(result)

    # Title vs cited entry comparison: applicable only when the service resolved and
    # returned a title. A clear mismatch downgrades 'resolved' to 'identifier_mismatch'
    # a partial mismatch stays 'resolved' + yellow warning.
    overlap = title_overlap(result.get("matched_title"), raw_entry)
    flag = title_flag(overlap)
    status = result["status"]
    # A trial resolved by its registration id is identified by that strong id, not
    # by title: citations often give only the id, so a title mismatch against the
    # citation text is meaningless and must not downgrade the resolution.
    if status == "resolved" and flag == "mismatch" and not result.get("trial_registration"):
        if doi or pmid or isbn:
            status = "identifier_mismatch"
        else:
            # There is no identifier here for a title mismatch to contradict.  The
            # citation declared none, so this resolution came from searching on the
            # title — and a title search that lands on the wrong work is an
            # unverified reference, not a hard identifier error.  Calling it one
            # spends a fabrication warning, and the fallback search behind it, on a
            # reference that never claimed an identifier in the first place: seven
            # of the nine such verdicts in the corpus are book title searches.
            #
            # Rebuilt rather than merely restatused, following the sibling guard
            # above: the matched work is not the cited one, so its abstract and
            # links must not travel on as if they described the citation.
            status = "unverified"
            result = {
                "status": "unverified",
                "via": result.get("via"),
                "matched_title": result.get("matched_title"),
                "reason": ("title search matched a different work, and the citation "
                           "declares no identifier for it to contradict"),
                "resolution_basis": result.get("resolution_basis"),
                "existence_confidence": "low",
                "metadata_match": result.get("metadata_match"),
            }
    identifier_check = None
    coverage_article_lookups: list[dict] = []
    coverage_payloads: list[dict] = []
    if status == "unverified":
        correction = _pubmed_coordinate_correction_search(ref)
        if correction is not None:
            attempts.append(_without_ecitmatch_audit(correction))
            if correction.get("status") == "resolved":
                result = dict(correction)
                status = "resolved"
                overlap = title_overlap(result.get("matched_title"), raw_entry)
        if status == "unverified":
            authority = None
            try:
                from .resolver_coverage import coverage_authority
                authority = coverage_authority(ref)
            except Exception:
                authority = None
            lookup_candidate = None
            if authority is not None:
                for lookup in resolver_modules.article_lookups(ref, authority):
                    body, media_type = lookup.pop("body"), lookup.pop("media_type")
                    lookup_candidate = lookup.pop("candidate", None) or lookup_candidate
                    lookup.pop("rule_version", None)
                    response_sha = hashlib.sha256(body.encode()).hexdigest() if isinstance(body, str) else None
                    coverage_article_lookups.append({**lookup, "response_sha256": response_sha})
                    if response_sha is not None:
                        coverage_payloads.append({"sha256": response_sha, "media_type": media_type, "body": body})
            occupancy = _pubmed_coordinate_occupancy_search(
                ref, lookup_candidate or correction, allow_authorless=not bool(coverage_article_lookups),
            )
            if occupancy is not None:
                attempts.append(_without_ecitmatch_audit(occupancy))
            # Registry-owned positive occupancy adapters are independent of
            # PubMed.  Their negative and operational outcomes remain attempts,
            # never absence evidence.
            attempts.extend(resolver_modules.occupy_coordinates(ref))
    if status in ("not_found", "identifier_mismatch"):
        fallback = _identifier_fallback_search(ref)
        identifier_check = {
            "status": status,
            "reason": result.get("reason"),
            "fallback_attempted": fallback is not None,
        }
        if fallback is not None:
            attempts.append({
                "attempt_kind": "identifier_fallback",
                "provider_via": fallback.get("via"),
                **fallback,
            })
            result["identifier_fallback"] = _fallback_evidence_view(fallback)
            identifier_check.update({
                "fallback_status": fallback.get("status"),
                "fallback_via": fallback.get("via"),
                "fallback_reason": fallback.get("reason"),
                "fallback_matched_title": fallback.get("matched_title"),
                "fallback_metadata_match": fallback.get("metadata_match"),
            })
            # A hard identifier error (e.g. a mangled/garbled DOI) still names a
            # real work: when the independent title search lands on the SAME work
            # — coherent author, year and title — recover its full-text links and
            # abstract so the source is retrievable, WITHOUT promoting the status
            # (it stays flagged; the tag is verified_with_identifier_error). If the
            # other identifiers do not agree, nothing is borrowed and the weaker
            # corroboration tags stand.
            mm = fallback.get("metadata_match") or {}
            short_title_fallback_match = _short_quoted_title_identifier_fallback_match(
                ref,
                fallback.get("matched_title"),
                mm,
                identifier_fallback=True,
            )
            if (fallback.get("status") == "resolved"
                    and fallback.get("ordinal_conflict") is not True
                    and fallback.get("metadata_conflict") is not True
                    and mm.get("ordinal_conflict") is not True
                    and mm.get("metadata_conflict") is not True
                    and (
                        (
                            mm.get("author_match") is True
                            and mm.get("year_match") is not False
                            and (mm.get("title_overlap") or 0) >= 0.90
                        )
                        or short_title_fallback_match
                    )):
                result["identifier_error_recovered"] = True
                result = _project_recovered_content(result, fallback)

        if fallback is None or fallback.get("status") != "resolved":
            correction = _pubmed_coordinate_correction_search(ref)
            if correction is not None:
                attempts.append({
                    "attempt_kind": "identifier_fallback",
                    "provider_via": correction.get("via"),
                    **_without_ecitmatch_audit(correction),
                })
                fallback = _fallback_evidence_view(correction)
                result["identifier_fallback"] = fallback
                identifier_check.update({
                    "fallback_attempted": True,
                    "fallback_status": fallback.get("status"),
                    "fallback_via": fallback.get("via"),
                    "fallback_reason": fallback.get("reason"),
                    "fallback_matched_title": fallback.get("matched_title"),
                    "fallback_metadata_match": fallback.get("metadata_match"),
                })
                mm = fallback.get("metadata_match") or {}
                if fallback.get("status") == "resolved":
                    result["identifier_error_recovered"] = True
                    # The compact fallback view deliberately omits attempt-only
                    # identifiers.  This accepted PubMed correction is the
                    # validated content source, so project from the correction
                    # itself while retaining the compact audit summary above.
                    result = _project_recovered_content(result, correction)

    issue_attestations = ([] if result.get("identifier_error_recovered")
                          else _issue_attestation_checks(ref, status))
    if issue_attestations:
        result["issue_attestations"] = issue_attestations
        for attestation in issue_attestations:
            if attestation.get("status") == "incomplete":
                attempts.append({"status": "unresolved", "via": attestation["provider"],
                                 "reason": attestation.get("reason")})
            if (attestation.get("status") in {"complete", "enumerated"}
                    and attestation.get("target_status") == "present"):
                issue_candidate = _issue_member_result(ref, attestation)
                if issue_candidate is None:
                    continue
                attempts.append(dict(issue_candidate))
                if status == "unverified":
                    result = dict(issue_candidate)
                    result["issue_attestations"] = issue_attestations
                    status = "resolved"
                    overlap = title_overlap(result.get("matched_title"), raw_entry)
                    flag = title_flag(overlap)
                else:
                    result["identifier_error_recovered"] = True
                    result = _project_recovered_content(result, issue_candidate)
                    result["issue_attestations"] = issue_attestations
                    result["identifiers"] = dict(issue_candidate.get("identifiers") or {})
                    result["metadata_match"] = issue_candidate.get("metadata_match")
                break

    prov = _resolution_provenance(ref, result)
    meta = _meta_subset(result)
    if (status in ("not_found", "identifier_mismatch", "unresolved")
            and not result.get("identifier_error_recovered")):
        # Keep raw provider output in attempts for audit, but never expose it
        # as corroborated application evidence after a failed/weak resolution.
        # The one exception is a coherent same-work title match recovered above
        # after a hard identifier error: its content is kept (the reference stays
        # flagged verified_with_identifier_error, not clean-resolved).
        meta["abstract"] = None
        meta.pop("abstract_via", None)
    evidence = _reference_evidence_profile(ref, result, status, attempts, overlap, prov,
                                            coverage_article_lookups, coverage_payloads)
    adjudication = evidence.get("bibliographic_adjudication") or {}
    if adjudication.get("identity_status") not in {"identified", "identified_with_errors"}:
        # Preserve the candidate in attempts/evidence, but never project content
        # from a record whose bibliographic identity did not pass admission.
        meta["abstract"] = None
        meta.pop("abstract_via", None)
        meta["fulltext_links"] = []
        meta.pop("auxiliary_fulltext_links", None)
        meta["fulltext_exists"] = "unknown"
        meta["oa_status"] = "unknown"
        meta.pop("fulltext_availability", None)
    if meta.get("fulltext_availability") is not None:
        evidence = dict(evidence)
        evidence["fulltext_availability"] = meta["fulltext_availability"]
    tag, risk, tag_reason = _reference_status_tag(ref, status, evidence, result)
    decision = _build_resolution_decision(
        ref,
        result,
        attempts,
        status=status,
        via=result.get("via"),
        reference_status_tag=tag,
        fabrication_risk=risk,
        resolution_basis=prov["resolution_basis"],
        meta=meta,
        bibliographic_identity_status=adjudication.get("identity_status"),
        evidence_via=result.get("content_via") or result.get("via"),
    )
    out = {
        "ref_id": ref["id"],
        "ref_number": ref.get("ref_number"),
        "status": status,
        "via": result.get("via"),
        "matched_title": meta["matched_title"],
        "abstract": meta["abstract"],
        "retracted": meta["retracted"],
        "fulltext_exists": meta["fulltext_exists"],
        "oa_status": meta["oa_status"],
        "work_type": meta["work_type"],
        "fulltext_links": meta["fulltext_links"],
        "title_overlap": overlap,
        "title_flag": flag,
        "resolution_basis": prov["resolution_basis"],
        "existence_confidence": prov["existence_confidence"],
        "reason": result.get("reason"),
        "reference_status_tag": tag,
        "fabrication_risk": risk,
        "tag_reason": tag_reason,
        "evidence_profile": evidence,
        "identifier_check": identifier_check,
        "attempts": attempts,
        "checked_at": now(),
    }
    if result.get("resolved_identifier") is not None:
        out["resolved_identifier"] = result.get("resolved_identifier")
    if result.get("identifiers"):
        out["identifiers"] = dict(result["identifiers"])
    if result.get("identifier_error_recovered") is True:
        out["identifier_error_recovered"] = True
    if result.get("title_guard") is not None:
        out["title_guard"] = result.get("title_guard")
    if meta.get("matched_authors") is not None:
        out["matched_authors"] = meta["matched_authors"]
    if meta.get("abstract_via") is not None:
        out["abstract_via"] = meta["abstract_via"]
    if result.get("existence_corroboration") is not None:
        out["existence_corroboration"] = result["existence_corroboration"]
    # Optional advisory metadata: emitted only when present.
    for k in (
        "book_availability",
        "availability_note",
        "fulltext_exists_refined_by",
        "fulltext_availability",
        "auxiliary_fulltext_links",
    ):
        if meta.get(k) is not None:
            out[k] = meta[k]
    if meta.get("oa_declared_status") not in (None, "unknown"):
        out["oa_declared_status"] = meta["oa_declared_status"]
    if meta.get("oa_license_urls"):
        out["oa_license_urls"] = meta["oa_license_urls"]
    out["identity"] = decision.identity.to_dict()
    out["identity_state"] = decision.identity_state
    out["evidence_payload"] = decision.evidence_payload
    out["retraction"] = decision.retraction
    out["trace"] = decision.trace
    return out


def resume_semantic_scholar_cooldown(deferred: _http.ProviderCooldownDeferred, provider: str | None = None) -> dict:
    """Resume one validated, memory-only Semantic Scholar continuation."""
    if isinstance(deferred, ProviderDeferredWork):
        if deferred.terminal:
            return {"_provider_deferred_cancelled": True}
        provider = provider or getattr(deferred, "_resume_provider", None) or next(iter(deferred.providers()), None)
        task = deferred.task(provider)
        if task is None:
            return {"_provider_deferred_cancelled": True}
        state, module = deferred.state, task["module"]
        try:
            if deferred.kind == "discovery_provider":
                r = _call_registry_resolver(module, state["ref"])
                state["result"] = _apply_optional_result(
                    state["ref"], state["result"], state["attempts"], r,
                )
                deferred.tasks.pop(provider, None)
                if state["result"] is not None and state["result"].get("status") == "resolved":
                    deferred.terminal = True
                    return _complete_discovery_after_optional(
                        state["ref"], state["result"], state["attempts"],
                        discovery_only=state["discovery_only"],
                    )
                if deferred.tasks:
                    raise deferred
                if state.get("tail_complete"):
                    discovery_state = {
                        "result": state["result"],
                        "attempts": state["attempts"],
                    }
                    if state["discovery_only"]:
                        return discovery_state
                    return _finalize_discovery(state["ref"], discovery_state)
                return _complete_discovery_after_optional(
                    state["ref"], state["result"], state["attempts"],
                    discovery_only=state["discovery_only"],
                )
            with resolver_modules.credential_scope_for_resolver(module):
                enrichment = module.enrich(state["effective_ref"])
        except _http.ProviderCooldownDeferred as exc:
            task["exc"] = exc
            raise deferred
        deferred.tasks.pop(provider, None)
        if enrichment is not None:
            via = resolver_modules.origin_name(module) or str(getattr(module, "NAME", "resolver"))
            state["attempts"].append({"via": via, "enrichment_only": True, **enrichment})
            if enrichment.get("status") == "resolved":
                if not state["enriched"].get("abstract") and enrichment.get("abstract"):
                    state["enriched"]["abstract"] = enrichment["abstract"]
                    state["enriched"]["abstract_via"] = via
                state["auxiliary_has_fulltext"] = (
                    _capture_auxiliary_fulltext_links(
                        state["enriched"], via=via,
                        links=enrichment.get("fulltext_links") or [], candidate=enrichment,
                    )
                    or state["auxiliary_has_fulltext"]
                )
        sufficient = state["enriched"].get("abstract") and (
            _links_have_fulltext(state["enriched"].get("fulltext_links"))
            or state["auxiliary_has_fulltext"]
        )
        if sufficient:
            deferred.terminal = True
            return _finalize_resolution(state["ref"], state["result"], state["attempts"],
                                        enrichment_continuation={"kind": "enrichment_done", "enriched": state["enriched"]})
        if deferred.tasks:
            raise deferred
        return _finalize_resolution(state["ref"], state["result"], state["attempts"],
                                    enrichment_continuation={"kind": "enrichment_done", "enriched": state["enriched"]})
    continuation = getattr(deferred, "continuation", None)
    if not isinstance(continuation, dict) or not isinstance(continuation.get("kind"), str):
        raise ValueError("invalid Semantic Scholar resolve continuation")
    if continuation["kind"] == "discovery_provider":
        expected = {"kind", "ref", "result", "attempts", "module", "discovery_only"}
        if (set(continuation) != expected or type(continuation["ref"]) is not dict
                or continuation["result"] is not None and type(continuation["result"]) is not dict
                or type(continuation["attempts"]) is not list
                or not callable(getattr(continuation["module"], "discover", None))
                or type(continuation["discovery_only"]) is not bool):
            raise ValueError("invalid provider-local discovery continuation")
        try:
            r = _call_registry_resolver(continuation["module"], continuation["ref"])
        except _http.ProviderCooldownDeferred as exc:
            exc.continuation = continuation
            raise
        result = _apply_optional_result(
            continuation["ref"], continuation["result"], continuation["attempts"], r,
        )
        return _complete_discovery_after_optional(
            continuation["ref"], result, continuation["attempts"],
            discovery_only=continuation["discovery_only"],
        )
    if continuation["kind"] == "discovery_optional":
        expected = {
            "kind", "ref", "result", "attempts", "optional_stage_reason", "modules",
            "next_index", "discovery_only",
        }
        if (set(continuation) != expected or type(continuation["ref"]) is not dict
                or continuation["result"] is not None and type(continuation["result"]) is not dict
                or type(continuation["attempts"]) is not list
                or type(continuation["optional_stage_reason"]) is not str
                or type(continuation["modules"]) is not list
                or type(continuation["next_index"]) is not int
                or type(continuation["discovery_only"]) is not bool):
            raise ValueError("invalid Semantic Scholar discovery continuation")
        result = _run_optional_resolvers(
            continuation["ref"], continuation["result"], continuation["attempts"],
            reason=continuation["optional_stage_reason"], modules=continuation["modules"],
            start_index=continuation["next_index"],
            discovery_only=continuation["discovery_only"],
        )
        return _complete_discovery_after_optional(
            continuation["ref"], result, continuation["attempts"],
            discovery_only=continuation["discovery_only"],
        )
    if continuation["kind"] == "enrichment_provider":
        expected = {
            "kind", "ref", "result", "attempts", "enriched", "effective_ref",
            "auxiliary_has_fulltext", "module",
        }
        if (set(continuation) != expected or type(continuation["ref"]) is not dict
                or type(continuation["result"]) is not dict
                or type(continuation["attempts"]) is not list
                or type(continuation["enriched"]) is not dict
                or type(continuation["effective_ref"]) is not dict
                or type(continuation["auxiliary_has_fulltext"]) is not bool
                or not callable(getattr(continuation["module"], "enrich", None))):
            raise ValueError("invalid provider-local enrichment continuation")
        return _finalize_resolution(
            continuation["ref"], continuation["result"], continuation["attempts"],
            enrichment_continuation=continuation,
        )
    if continuation["kind"] != "enrichment":
        raise ValueError("unknown Semantic Scholar resolve continuation")
    expected = {
        "kind", "ref", "result", "attempts", "enriched", "effective_ref",
        "auxiliary_has_fulltext", "enrichers", "next_index",
    }
    if set(continuation) != expected:
        raise ValueError("invalid Semantic Scholar enrichment continuation")
    return _finalize_resolution(
        continuation["ref"], continuation["result"], continuation["attempts"],
        enrichment_continuation=continuation,
    )


def main():
    from core.infra.db import RunRepository

    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="DB-backed run directory")
    ap.add_argument("--ref-id", required=True, help="reference id from the run database")
    ap.add_argument("--text-out", help="where to save the retrieved abstract/text")
    ap.add_argument("--mailto", help="contact email for the polite pool "
                    "(or CITATION_VERIFIER_MAILTO env var); optional")
    args = ap.parse_args()

    set_contact(args.mailto or os.environ.get("CITATION_VERIFIER_MAILTO"))
    repo = RunRepository.open(args.run)
    try:
        matches = [
            ref for ref in repo.effective_parse_payload().get("references", [])
            if ref.get("id") == args.ref_id
        ]
    finally:
        repo.close()
    if len(matches) != 1:
        raise SystemExit(
            f"reference {args.ref_id!r} is not present exactly once in the run"
        )
    ref = matches[0]
    res = resolve(ref)

    repo = RunRepository.open(args.run)
    try:
        repo.upsert_resolve_result(args.ref_id, res, trace_state="produced")
    finally:
        repo.close()

    if args.text_out and res.get("abstract"):
        os.makedirs(os.path.dirname(os.path.abspath(args.text_out)), exist_ok=True)
        with open(args.text_out, "w", encoding="utf-8") as f:
            f.write(res["abstract"])
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


# ===========================================================================
# Semantic Scholar + Lens.org resolvers (recovered from PR #42)
# ===========================================================================


def _configured_key(env_name: str) -> str | None:
    value = str(os.environ.get(env_name) or "").strip()
    return value or None


def _post_json(
    url: str,
    payload: dict,
    *,
    accept: str = "application/json",
    headers_extra: dict[str, str] | None = None,
    preserve_cooldown_after_429: bool = False,
):
    _refresh_http_test_seam()
    return _http._post_json(
        url,
        payload,
        accept=accept,
        headers_extra=headers_extra,
        preserve_cooldown_after_429=preserve_cooldown_after_429,
    )


# Characters a DOI cannot end on, so a trailing run of them is punctuation or
# markup the entry carried in - "10.3366/gels.2020.0029>" was reported as a
# non-existent DOI, a hard identifier error, on the strength of one stray angle
# bracket.  Only the tail is touched: a Wiley SICI DOI uses "<", ">", ":" and ";"
# internally (10.1002/(sici)1097-4571(199210)43:9<628::aid-asi5>3.0.co;2-0) and
# must survive untouched.  ")" is deliberately not in the set: it ends real DOIs.
_DOI_TRAILING_NOISE_RE = re.compile(r"[.,;:'\"<>\]}]+$")


def _normalize_doi_value(value: str | None) -> str | None:
    doi = str(value or "").strip()
    if not doi:
        return None
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = _DOI_TRAILING_NOISE_RE.sub("", doi.strip())
    return doi.strip() or None


def _content_type_hint(url: str | None, kind: str | None = None) -> str:
    kind_text = str(kind or "").lower()
    url_text = str(url or "").lower()
    if "pdf" in kind_text or url_text.endswith(".pdf"):
        return "pdf"
    if "doi" in kind_text or "doi.org/" in url_text:
        return "doi"
    if "html" in kind_text or kind_text == "landing_page":
        return "html"
    return "link"


def _weak_metadata_result(result: dict | None) -> bool:
    if not result:
        return True
    status = result.get("status")
    if status == "unverified":
        return True
    if status != "resolved":
        return False
    if result.get("resolution_basis") != "metadata_search":
        return False
    if result.get("existence_confidence") == "low":
        return True
    return _result_title_overlap(result) < 0.85


# Phase 2 registry overrides: resolver iteration is routed through
# file-backed modules and core/resolve/providers.json.
def _resolver_registry() -> dict[str, object]:
    return resolver_modules.resolve_capable()


def new_openalex_batch_session():
    """Create the opt-in, phase-scoped OpenAlex DOI batch accelerator."""
    from .providers import openalex

    return openalex.new_batch_session() if openalex.batch_enabled() else None


def bind_openalex_batch_session(session):
    from .providers import openalex

    return openalex.bind_batch_session(session)


def prime_openalex_batch_session(session, refs: list[dict]) -> None:
    """Prime declared DOI records without assigning resolver outcomes."""
    from .providers import openalex

    items = [
        (ref["id"], ref.get("doi"))
        for ref in refs
        if _article_like_resolution_candidate(ref)
        and _normalize_doi_value(ref.get("doi"))
    ]
    if items:
        with resolver_modules.credential_scope_for_resolver(openalex):
            session.prime_many(
                items,
                get_fn=_get,
                normalize_doi=_normalize_doi_value,
                email=_http._CONTACT_EMAIL,
            )


def new_semantic_scholar_batch_session():
    """Create the opt-in, phase-scoped Semantic Scholar DOI batch accelerator."""
    from .providers import semantic_scholar

    return semantic_scholar.new_batch_session() if semantic_scholar.batch_enabled() else None


def bind_semantic_scholar_batch_session(session):
    from .providers import semantic_scholar

    return semantic_scholar.bind_batch_session(session)


def prime_semantic_scholar_batch_session(session, refs: list[dict]) -> None:
    """Prime eligible declared DOI enrichments without resolver semantics."""
    from .providers import semantic_scholar

    headers = semantic_scholar._headers(sys.modules[__name__])
    if headers is None:
        return
    items = [
        (ref["id"], ref.get("doi"))
        for ref in refs
        if _article_like_resolution_candidate(ref)
        and _normalize_doi_value(ref.get("doi"))
    ]
    if items:
        with resolver_modules.credential_scope_for_resolver(semantic_scholar):
            session.prime_many(
                items,
                post_fn=lambda url, payload, **kwargs: _post_json(
                    url, payload, preserve_cooldown_after_429=True, **kwargs
                ),
                normalize_doi=_normalize_doi_value,
                headers=headers,
            )


def new_pubmed_batch_session():
    """Create the opt-in, phase-scoped PubMed EFetch batch accelerator."""
    from .providers import europepmc

    return europepmc.new_pubmed_batch_session() if europepmc.pubmed_batch_enabled() else None


def bind_pubmed_batch_session(session):
    from .providers import europepmc

    return europepmc.bind_pubmed_batch_session(session)


def new_pmc_idconv_batch_session():
    from .providers import europepmc

    return europepmc.new_pmc_idconv_batch_session() if europepmc.pubmed_batch_enabled() else None


def bind_pmc_idconv_batch_session(session):
    from .providers import europepmc
    return europepmc.bind_pmc_idconv_batch_session(session)


def prime_pmc_idconv_batch_session(session, refs: list[dict]) -> None:
    items = [
        (ref["id"], ref.get("pmid"))
        for ref in refs
        if _article_like_resolution_candidate(ref)
        and str(ref.get("pmid") or "").strip().isdigit()
    ]
    if items:
        session.prime_many(items, get_fn=_get, ncbi_url=_ncbi_url)


def prime_pubmed_batch_session(session, refs: list[dict]) -> None:
    """Prime declared PMID metadata without deriving or admitting identifiers."""
    items = [
        (ref["id"], ref.get("pmid"))
        for ref in refs
        if _article_like_resolution_candidate(ref)
        and str(ref.get("pmid") or "").strip()
    ]
    if items:
        session.prime_many(items, get_fn=_get, ncbi_url=_ncbi_url)


def _load_fallback_order() -> list[str]:
    """Return resolver order from core/resolve/providers.json plus discovery merge."""
    return resolver_modules.load_order()


def _crossref_resolver_module():
    module = _resolver_registry().get("crossref_metadata")
    if module is None:
        raise RuntimeError("crossref_metadata resolver missing")
    return module


def _acl_resolver_module():
    return _resolver_registry().get("acl_search")


def new_scholar_archive_circuit_session():
    from .providers import scholar_archive

    return scholar_archive.new_circuit_session()


def bind_scholar_archive_circuit_session(session):
    from .providers import scholar_archive

    return scholar_archive.bind_circuit_session(session)


def _call_registry_resolver(module, ref: dict) -> dict | None:
    # Route the current mapping-or-null resolver contract through the registry's
    # diagnostic normalizer.
    return resolver_modules.call_resolver(module, ref)


def _try_resolvers_with_fallback(
    primary_key: str,
    ref: dict,
    *,
    result: dict | None,
    attempts: list[dict],
    can_use_title_search: bool = True,
    max_rounds: int = 3,
) -> dict | None:
    """Try *primary_key* resolver; on 429, cycle through discovered fallback modules."""
    if not can_use_title_search:
        return None

    registry = _resolver_registry()
    primary_module = registry.get(primary_key)
    if primary_module is None:
        return None
    primary_supports = getattr(primary_module, "supports", None)
    if callable(primary_supports) and not primary_supports(ref):
        return None

    full_order = _load_fallback_order()
    fallback_order = [
        name for name in full_order
        if name != primary_key
        and name in registry
        and not getattr(registry[name], "LOCAL_ACCELERATOR", False)
    ]

    r: dict | None = None
    for round_idx in range(max_rounds):
        r = _call_registry_resolver(primary_module, ref)
        if r is not None:
            attempts.append(r)
        if r is not None and r.get("status") != "unresolved":
            return r
        if r is not None and "rate_limited" not in str(r.get("reason") or ""):
            return r

        for fallback_name in fallback_order:
            module = registry.get(fallback_name)
            if module is None:
                continue
            fr = _call_registry_resolver(module, ref)
            if fr is not None:
                attempts.append(fr)
            if fr is not None and fr.get("status") == "resolved":
                return fr
            if fr is not None and fr.get("status") == "unverified" and result is None:
                return fr

        if round_idx < max_rounds - 1:
            time.sleep(2.0 * (round_idx + 1))

    return r
