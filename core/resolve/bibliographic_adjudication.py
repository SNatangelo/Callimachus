# core/resolve/bibliographic_adjudication.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic adjudication of bibliographic identity evidence.

This module classifies what the stored resolver evidence proves.  It never
infers author intent and never turns a failed or incomplete lookup into proof
that a cited work does not exist.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

try:
    from .matching import (
        _author_names_equivalent,
        _anchored_metadata_author_conflict,
        _first_author_key,
        _fold_author,
        _crossref_terminal_numeric_title_range_result_supported,
        _metadata_candidate_has_source_spanned_definite_locator_conflict,
        _pubmed_titleless_coordinate_profile_supported,
        _pubmed_titled_coordinate_profile_supported,
        is_unique_same_work_correction_candidate,
    )
except ImportError:  # direct execution
    from resolve.matching import (
        _author_names_equivalent,
        _anchored_metadata_author_conflict,
        _first_author_key,
        _fold_author,
        _crossref_terminal_numeric_title_range_result_supported,
        _metadata_candidate_has_source_spanned_definite_locator_conflict,
        _pubmed_titleless_coordinate_profile_supported,
        _pubmed_titled_coordinate_profile_supported,
        is_unique_same_work_correction_candidate,
    )


RULE_VERSION = "bibliographic-adjudication/v4"
_PUBLISHER_NATIVE_URI_QUERY_CONTRACT = (
    "Crossref selected DOI publisher native PII URI candidate lookup"
)
_PUBLISHER_NATIVE_URI_VALIDATED_VIA = (
    "crossref:publisher_native_pii_uri_attestation"
)


def _publisher_literal_article_uri(ref: dict[str, Any]) -> tuple[str, str, str] | None:
    """Extract the one literal article URL and exact PII path segment from source."""
    raw_entry = ref.get("raw_entry")
    if not isinstance(raw_entry, str):
        return None
    urls = re.findall(r"https?://[^\s<>\"']+", raw_entry, flags=re.IGNORECASE)
    if len(urls) != 1:
        return None
    source_uri = urls[0].rstrip(".,;:!?]")
    while source_uri.endswith(")") and source_uri.count("(") < source_uri.count(")"):
        source_uri = source_uri[:-1]
    try:
        parsed = urllib.parse.urlsplit(source_uri)
    except ValueError:
        return None
    segments = parsed.path.strip("/").split("/")
    if (
        not parsed.hostname
        or len(segments) != 3
        or segments[0].casefold() != "article"
        or segments[2].casefold() != "fulltext"
        or not segments[1]
        or "%" in segments[1]
    ):
        return None
    return source_uri, parsed.hostname.rstrip(".").casefold(), segments[1]


def _normalized_doi(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    return text.rstrip(".,;)")


def _selected_result_doi(result: dict[str, Any]) -> str:
    """Return only a DOI carried by the selected candidate itself.

    ``result['identifiers']`` is intentionally excluded: enrichment copies its
    identifiers there, so reading it back would mistake one provider's assertion
    for independent corroboration.
    """
    direct = _normalized_doi(result.get("doi"))
    if direct:
        return direct
    for link in result.get("fulltext_links") or ():
        if not isinstance(link, dict):
            continue
        url = str(link.get("url") or "").strip()
        if re.match(r"^https?://(?:dx\.)?doi\.org/", url, flags=re.IGNORECASE):
            doi = _normalized_doi(url)
            if doi:
                return doi
    return ""


def _independent_biomedical_identifier_corroboration(
    result: dict[str, Any],
    attempts: list[dict[str, Any]] | None,
) -> bool:
    """Lock a metadata candidate corroborated by DOI *and* PMID.

    This covers citations whose title contains a conservative abbreviation or
    typographical error: the selected catalogue record must already agree on
    author, year, and at least two editorial coordinates, then an independent
    PubMed/Europe PMC enrichment must return the selected DOI plus a PMID.
    """
    metadata_match = result.get("metadata_match")
    if not isinstance(metadata_match, dict):
        return False
    if (
        result.get("resolution_basis") != "metadata_search"
        or metadata_match.get("author_match") is not True
        or metadata_match.get("year_match") is not True
        or metadata_match.get("ordinal_conflict") is True
        or metadata_match.get("metadata_conflict") is True
        or (metadata_match.get("title_overlap") or 0) < 0.65
    ):
        return False
    coordinate_matches = sum(
        1
        for comparison in metadata_match.get("coordinate_comparisons") or ()
        if isinstance(comparison, dict) and comparison.get("status") == "match"
    )
    if coordinate_matches < 2:
        return False
    selected_doi = _selected_result_doi(result)
    if not selected_doi:
        return False
    return any(
        isinstance(attempt, dict)
        and attempt.get("enrichment_only") is True
        and attempt.get("status") == "resolved"
        and attempt.get("via") in {"europepmc", "pubmed"}
        and _normalized_doi(attempt.get("doi")) == selected_doi
        and bool(str(attempt.get("pmid") or "").strip())
        and bool(attempt.get("abstract"))
        for attempt in attempts or ()
    )


def _is_crossref_same_work_correction(result: dict[str, Any]) -> bool:
    resolved_identifier = result.get("resolved_identifier")
    return bool(
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        == "crossref:unique_same_work_correction"
        and resolved_identifier.get("type") == "doi"
        and resolved_identifier.get("value")
    )


def has_unresolved_identity_attempt(attempts: list[dict[str, Any]] | None) -> bool:
    """Return whether an identity check, rather than optional enrichment, failed."""
    return any(
        isinstance(attempt, dict)
        and attempt.get("status") == "unresolved"
        and attempt.get("enrichment_only") is not True
        for attempt in attempts or ()
    )


def _declared_identifier(ref: dict[str, Any]) -> tuple[str | None, str | None]:
    for field in ("doi", "pmid", "isbn"):
        value = ref.get(field)
        if value not in (None, ""):
            return field, str(value)
    return None, None


def _coordinate_refutations(
    metadata_match: dict[str, Any] | None,
    *,
    source: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for comparison in (metadata_match or {}).get("coordinate_comparisons") or ():
        if comparison.get("status") != "mismatch":
            continue
        out.append({
            "kind": "coordinate_mismatch",
            "field": comparison["kind"],
            "cited_value": comparison["cited_value"],
            "observed_value": comparison.get("matched_value"),
            "source": source,
            "basis": "locked candidate metadata contradicts the cited coordinate",
        })
    return out


def _year_refutation(
    ref: dict[str, Any],
    metadata_match: dict[str, Any] | None,
    *,
    source: str,
) -> dict[str, Any] | None:
    metadata_match = metadata_match or {}
    if (
        metadata_match.get("year_match") is not False
        or metadata_match.get("year_mismatch_plausible") is True
        or ref.get("year") is None
        or metadata_match.get("matched_year") is None
    ):
        return None
    return {
        "kind": "year_mismatch",
        "field": "year",
        "cited_value": str(ref["year"]),
        "observed_value": str(metadata_match["matched_year"]),
        "source": source,
        "basis": "locked candidate metadata contradicts the cited publication year",
    }


def _anchored_issue_author_conflict(
    metadata_match: dict[str, Any] | None,
    ay_surname: str | None,
) -> bool:
    """Require the parsed and raw-entry author anchors to agree before refuting."""
    if not isinstance(metadata_match, dict) or metadata_match.get("author_match") is not False:
        return False
    cited = metadata_match.get("cited_first_author")
    matched = metadata_match.get("matched_first_author")
    if not ay_surname or not cited or not matched:
        return False
    anchor_key = _fold_author(str(ay_surname))
    cited_key = _fold_author(str(cited))
    matched_key = _fold_author(str(matched))
    return (
        _author_names_equivalent(anchor_key, cited_key)
        and not _author_names_equivalent(anchor_key, matched_key)
        and not _author_names_equivalent(cited_key, matched_key)
    )


def _bound_validation_author_refutation(
    ref: dict[str, Any], result: dict[str, Any],
    attempts: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Keep a direct validation's author contradiction on the locked DOI."""
    raw = ref.get("raw_entry")
    if not isinstance(raw, str):
        return None
    try:
        from core.parse.parsing_common import _closed_source_author_head_end
    except ImportError:  # direct execution
        from parse.parsing_common import _closed_source_author_head_end
    end = _closed_source_author_head_end(raw)
    if end is None:
        return None
    anchor = _first_author_key(raw[:end])
    selected = _selected_result_doi(result)
    identifier = result.get("resolved_identifier")
    if (
        not selected or not isinstance(identifier, dict)
        or identifier.get("type") != "doi"
        or _normalized_doi(identifier.get("value")) != selected
    ):
        return None
    for attempt in attempts or ():
        if (
            not isinstance(attempt, dict)
            or attempt.get("via") != "identifier_validation:crossref"
            or attempt.get("status") != "resolved"
            or attempt.get("identifier_type") != "doi"
            or _normalized_doi(attempt.get("identifier_value")) != selected
        ):
            continue
        match = attempt.get("metadata_match")
        if _anchored_metadata_author_conflict(match, anchor):
            return {
                "kind": "author_mismatch", "field": "author",
                "cited_value": match["cited_first_author"],
                "observed_value": match["matched_first_author"],
                "source": "identifier_validation:crossref",
                "basis": "identifier-bound validation contradicts the closed cited first author",
            }
    return None


def _resolved_identity_is_locked(
    ref: dict[str, Any],
    evidence: dict[str, Any],
    result: dict[str, Any],
    attempts: list[dict[str, Any]] | None = None,
) -> bool:
    """Require identity evidence beyond a provider's transport-level status."""
    metadata_match = result.get("metadata_match") or evidence.get("metadata_match")
    metadata_match = metadata_match if isinstance(metadata_match, dict) else {}
    if (
        result.get("ordinal_conflict")
        or result.get("metadata_conflict")
        or metadata_match.get("ordinal_conflict")
        or metadata_match.get("metadata_conflict")
    ):
        return False

    basis = evidence.get("resolution_basis") or result.get("resolution_basis")
    resolved_identifier = result.get("resolved_identifier")
    if (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        == "crossref:arxiv_published_journal_unit_certificate"
    ):
        from .providers import arxiv as arxiv_provider

        return arxiv_provider._published_relation_proof_is_valid(
            ref, result, attempts,
        )
    if (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        == "crossref:generic_article_locator_role_certificate"
    ):
        from .providers import crossref_metadata

        return crossref_metadata._crossref_generic_article_locator_relock(
            ref, evidence, result, attempts,
        )
    if (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        == "crossref:ambiguous_title_journal_boundary_certificate"
    ):
        from .providers import crossref_metadata

        return crossref_metadata._crossref_ambiguous_boundary_relock(
            ref, result, attempts,
        )
    if (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        == "crossref:registered_coordinate_certificate"
    ):
        if (
            result.get("status") != "resolved"
            or result.get("via") != "crossref_metadata"
            or basis != "metadata_search"
            or resolved_identifier.get("type") != "doi"
            or not _normalized_doi(result.get("doi"))
            or _normalized_doi(result.get("doi"))
            != _normalized_doi(resolved_identifier.get("value"))
        ):
            return False
        proof = metadata_match.get("registered_coordinate_certificate")
        if not isinstance(proof, dict):
            return False
        from .providers.crossref_metadata import (
            _crossref_metadata_match_profile,
            _crossref_registered_titled_coordinate_certificate,
        )

        candidate_msg = {
            "DOI": result.get("doi"),
            "type": result.get("work_type"),
            "title": [result.get("matched_title")],
            "author": [{"family": name} for name in result.get("matched_authors") or ()],
            "published": {"date-parts": [[metadata_match.get("matched_year")]]},
            "container-title": [metadata_match.get("matched_venue")],
            "ISSN": proof.get("candidate_issns"),
        }
        fields = {
            "container": "container-title", "volume": "volume", "issue": "issue",
            "article_page_range": "page", "article_locator": "page",
            "elocator": "article-number", "article_number": "article-number",
        }
        for comparison in metadata_match.get("coordinate_comparisons") or ():
            if not isinstance(comparison, dict):
                continue
            field = fields.get(comparison.get("kind"))
            if field:
                value = comparison.get("matched_value")
                candidate_msg[field] = [value] if field == "container-title" else value
        recomputed_profile = _crossref_metadata_match_profile(ref, candidate_msg)
        profile_fields = (
            "title_overlap", "score", "author_match", "year_match",
            "year_mismatch_plausible", "matched_first_author", "cited_first_author",
            "matched_year", "matched_venue", "venue_overlap",
            "coordinate_comparisons", "ordinal_conflict", "metadata_conflict",
        )
        if any(
            recomputed_profile.get(field) != metadata_match.get(field)
            for field in profile_fields
        ):
            return False
        return _crossref_registered_titled_coordinate_certificate(
            ref,
            candidate_msg,
            recomputed_profile,
            stored_proof=proof,
        ) is not None
    if basis != "metadata_search":
        return True

    # A registry-validated identifier discovered from an admitted metadata
    # candidate locks that candidate's identity, but does not erase cited-field
    # discrepancies (notably a wrong publication year).
    if _is_crossref_same_work_correction(result):
        candidate_msg = {
            "author": [{"family": author} for author in result.get("matched_authors") or ()],
            "published": {"date-parts": [[metadata_match.get("matched_year")]]},
            "container-title": [metadata_match.get("matched_venue")],
        }
        for comparison in metadata_match.get("coordinate_comparisons") or ():
            if comparison.get("kind") == "volume":
                candidate_msg["volume"] = comparison.get("matched_value")
            elif comparison.get("kind") == "container":
                candidate_msg["container-title"] = [comparison.get("matched_value")]
        if is_unique_same_work_correction_candidate(
            ref, candidate_msg, result.get("matched_title"), metadata_match,
        ):
            return True
    identifiers = result.get("identifiers")
    if (
        result.get("status") == "resolved"
        and result.get("via") == "crossref_metadata"
        and isinstance(resolved_identifier, dict)
        and resolved_identifier.get("type") == "doi"
        and resolved_identifier.get("validated_via")
        == "crossref:complete_coordinate_certificate"
        and _normalized_doi(result.get("doi"))
        and _normalized_doi(result.get("doi"))
        == _normalized_doi(resolved_identifier.get("value"))
    ):
        from .providers.crossref_metadata import (
            _REGISTERED_SOURCE_SCOPE_CERTIFICATE_KEY,
            _TITLELESS_NATIVE_VENUE_COMPOSITION_KEY,
            _crossref_titleless_coordinate_certificate,
        )

        selected_scope_proof = metadata_match.get(
            _REGISTERED_SOURCE_SCOPE_CERTIFICATE_KEY,
        )
        retained_scope_proofs = []
        for attempt in attempts or ():
            attempt_identifier = (
                attempt.get("resolved_identifier")
                if isinstance(attempt, dict) else None
            )
            attempt_profile = (
                attempt.get("metadata_match")
                if isinstance(attempt, dict) else None
            )
            if (
                not isinstance(attempt, dict)
                or attempt.get("status") != "resolved"
                or attempt.get("via") != "crossref_metadata"
                or not isinstance(attempt_identifier, dict)
                or attempt_identifier.get("validated_via")
                != "crossref:complete_coordinate_certificate"
                or _normalized_doi(attempt.get("doi"))
                != _normalized_doi(result.get("doi"))
                or not isinstance(attempt_profile, dict)
                or _REGISTERED_SOURCE_SCOPE_CERTIFICATE_KEY not in attempt_profile
            ):
                continue
            attempt_scope_proof = attempt_profile[
                _REGISTERED_SOURCE_SCOPE_CERTIFICATE_KEY
            ]
            if not isinstance(attempt_scope_proof, dict):
                return False
            retained_scope_proofs.append(attempt_scope_proof)
        if retained_scope_proofs and selected_scope_proof not in retained_scope_proofs:
            return False

        # Recheck the direct record's full tuple against source spans. A
        # composed-venue witness reconstructs only its native short title;
        # matched_venue remains the primary container and the comparison row
        # retains the provider's original container result.
        candidate_msg = {
            "DOI": result.get("doi"), "type": result.get("work_type"),
            "title": [result.get("matched_title")],
            "author": [{"family": name} for name in result.get("matched_authors") or ()],
            "published": {"date-parts": [[metadata_match.get("matched_year")]]},
        }
        venue_composition = metadata_match.get(
            _TITLELESS_NATIVE_VENUE_COMPOSITION_KEY,
        )
        if isinstance(venue_composition, dict):
            candidate_msg["short-container-title"] = [
                venue_composition.get("native_short"),
            ]
        fields = {
            "container": "container-title", "volume": "volume", "issue": "issue",
            "article_page_range": "page", "article_locator": "page",
            "elocator": "article-number", "article_number": "article-number",
        }
        for comparison in metadata_match.get("coordinate_comparisons") or ():
            if not isinstance(comparison, dict):
                continue
            field = fields.get(comparison.get("kind"))
            if field:
                value = comparison.get("matched_value")
                candidate_msg[field] = [value] if field == "container-title" else value
        if (
            _crossref_titleless_coordinate_certificate(ref, candidate_msg, metadata_match)
            and _crossref_titleless_coordinate_certificate(ref, candidate_msg)
        ):
            return True
    if (
        result.get("status") == "resolved"
        and result.get("via") == "publisher_native_uri_metadata"
        and basis == "metadata_search"
        and not ref.get("doi")
        and isinstance(resolved_identifier, dict)
        and resolved_identifier.get("type") == "doi"
        and resolved_identifier.get("validated_via")
        == _PUBLISHER_NATIVE_URI_VALIDATED_VIA
        and _normalized_doi(resolved_identifier.get("value"))
        and (
            not result.get("doi")
            or _normalized_doi(result.get("doi"))
            == _normalized_doi(resolved_identifier.get("value"))
        )
    ):
        selected_doi = _normalized_doi(resolved_identifier.get("value"))
        try:
            from . import service as resolve_service
        except ImportError:  # direct execution
            import service as resolve_service

        native_uri_attested = any(
            isinstance(attempt, dict)
            and attempt.get("via") == "publisher_native_uri_metadata"
            and attempt.get("status") == "resolved"
            and _normalized_doi(attempt.get("doi")) == selected_doi
            and isinstance(attempt.get("identity_search"), dict)
            and attempt["identity_search"].get("query_contract")
            == _PUBLISHER_NATIVE_URI_QUERY_CONTRACT
            and attempt["identity_search"].get("completion") == "complete"
            and attempt["identity_search"].get("outcome") == "compatible_identity"
            and resolve_service._publisher_native_uri_proof_is_valid(
                ref, result, attempt, selected_doi,
                metadata_match if metadata_match else None,
            )
            for attempt in attempts or ()
        )
        direct_crossref_validated = any(
            isinstance(attempt, dict)
            and attempt.get("via") == "identifier_validation:crossref"
            and attempt.get("status") == "resolved"
            and attempt.get("identifier_type") == "doi"
            and _normalized_doi(attempt.get("identifier_value")) == selected_doi
            for attempt in attempts or ()
        )
        if native_uri_attested and direct_crossref_validated:
            return True
    if (
        result.get("status") == "resolved"
        and result.get("via") == "pubmed_citation_match"
        and (
            _pubmed_titleless_coordinate_profile_supported(ref, metadata_match)
            or _pubmed_titled_coordinate_profile_supported(ref, metadata_match)
        )
        and isinstance(resolved_identifier, dict)
        and resolved_identifier.get("type") == "doi"
        and resolved_identifier.get("validated_via") == "pubmed_citation_match"
        and isinstance(identifiers, dict)
        and _normalized_doi(resolved_identifier.get("value"))
        and _normalized_doi(resolved_identifier.get("value"))
        == _normalized_doi(identifiers.get("doi"))
        and str(result.get("pmid") or "").strip().isdigit()
        and str(identifiers.get("pmid") or "").strip()
        == str(result.get("pmid") or "").strip()
    ):
        return True
    selected_doi = (
        _normalized_doi(resolved_identifier.get("value"))
        if isinstance(resolved_identifier, dict)
        and resolved_identifier.get("type") == "doi"
        and resolved_identifier.get("validated_via") == "crossref"
        else ""
    )
    if (
        result.get("status") == "resolved"
        and result.get("resolution_basis") == "metadata_search"
        and not any(ref.get(field) for field in ("doi", "pmid", "isbn"))
        and selected_doi
    ):
        for attempt in attempts or ():
            if (
                isinstance(attempt, dict)
                and attempt.get("via") == "identifier_validation:crossref"
                and attempt.get("status") == "resolved"
                and attempt.get("identifier_type") == "doi"
                and _normalized_doi(attempt.get("identifier_value")) == selected_doi
                and _metadata_candidate_has_source_spanned_definite_locator_conflict(
                    ref,
                    attempt.get("metadata_match"),
                    work_type=attempt.get("work_type"),
                    matched_title=attempt.get("matched_title"),
                )
            ):
                return False
    if (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        and (metadata_match.get("title_overlap") or 0) >= 0.85
        and not (
            metadata_match.get("author_match") is not True
            and (
                (
                    metadata_match.get("year_match") is False
                    and metadata_match.get("matched_year") is not None
                )
                or any(
                    comparison.get("status") == "mismatch"
                    for comparison in metadata_match.get("coordinate_comparisons") or ()
                )
            )
        )
        and (
            metadata_match.get("author_match") is not False
            or not metadata_match.get("matched_first_author")
        )
    ):
        return True
    if _independent_biomedical_identifier_corroboration(result, attempts):
        return True

    if (
        result.get("via") == "arxiv_search"
        and (metadata_match.get("title_overlap") or 0) >= 0.90
        and any(
            isinstance(link, dict)
            and isinstance(link.get("identity_context"), dict)
            and link["identity_context"].get("canonical_host") is True
            and bool((link["identity_context"].get("identifiers") or {}).get("arxiv_id"))
            for link in result.get("fulltext_links") or ()
        )
    ):
        return True
    if (
        result.get("via") == "acl_search"
        and (
            (metadata_match.get("title_overlap") or 0) >= 0.90
            or result.get("identity_basis") == "authoritative_acl_short_title_prefix"
        )
        and any(
            isinstance(link, dict)
            and str(link.get("url") or "").startswith("https://aclanthology.org/")
            for link in result.get("fulltext_links") or ()
        )
    ):
        return True
    if (
        result.get("via") in {"jmlr_search", "neurips_search"}
        and (metadata_match.get("title_overlap") or 0) >= 0.90
        and metadata_match.get("author_match") is True
        and any(
            isinstance(link, dict)
            and isinstance(link.get("identity_context"), dict)
            and link["identity_context"].get("canonical_host") is True
            and link["identity_context"].get("provider") == result.get("via")
            for link in result.get("fulltext_links") or ()
        )
    ):
        # These adapters enumerate records from the publisher-maintained JMLR
        # and NeurIPS catalogues.  An exact title/author record plus its canonical
        # document link locks the work even when the citation's year is wrong;
        # the mismatch is retained below as a refutation, never silently fixed.
        return True

    # An authoritative catalogue can lock an exact local record without the
    # generic metadata scorer.  Ordinary title-search candidates must pass the
    # same deterministic identity dimensions that selected them.
    confidence = evidence.get("existence_confidence") or result.get("existence_confidence")
    if not metadata_match:
        return confidence == "high"
    overlap = metadata_match.get("title_overlap")
    terminal_range_title_supported = (
        basis == "metadata_search"
        and _crossref_terminal_numeric_title_range_result_supported(ref, result)
    )
    if (
        (not isinstance(overlap, (int, float)) or overlap < 0.85)
        and not terminal_range_title_supported
    ):
        return False
    if evidence.get("has_author") and metadata_match.get("author_match") is not True:
        return False
    if evidence.get("has_year") and metadata_match.get("year_match") is not True:
        return False
    return True


def adjudicate_bibliographic_evidence(
    ref: dict[str, Any],
    *,
    status: str,
    evidence: dict[str, Any],
    result: dict[str, Any],
    attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a versioned verdict derived only from explicit stored evidence."""
    fallback = result.get("identifier_fallback") or evidence.get("identifier_fallback")
    fallback = fallback if isinstance(fallback, dict) else {}
    recovered = result.get("identifier_error_recovered") is True
    fallback_match = fallback.get("metadata_match")
    fallback_match = fallback_match if isinstance(fallback_match, dict) else {}
    issue_attestations = result.get("issue_attestations") or evidence.get("issue_attestations") or []
    issue_attestations = [item for item in issue_attestations if isinstance(item, dict)]
    present_attestation = next((
        item for item in issue_attestations
        if item.get("status") in {"complete", "enumerated"}
        and item.get("target_status") == "present"
    ), {})
    absent_attestation = next((
        item for item in issue_attestations
        if item.get("status") == "complete"
        and item.get("target_status") == "absent"
    ), {})
    issue_target_present = bool(present_attestation)
    issue_target_absent = bool(absent_attestation)
    recovered_identity_locked = (issue_target_present and status != "resolved") or (
        recovered and not (
            fallback.get("ordinal_conflict")
            or fallback.get("metadata_conflict")
            or fallback_match.get("ordinal_conflict")
            or fallback_match.get("metadata_conflict")
        )
    )
    resolved_identity_locked = status == "resolved" and (
        issue_target_present
        or _resolved_identity_is_locked(ref, evidence, result, attempts)
    )
    refutations: list[dict[str, Any]] = []
    identifier_field, identifier_value = _declared_identifier(ref)

    if status == "not_found" and identifier_field is not None:
        refutations.append({
            "kind": "identifier_not_found",
            "field": identifier_field,
            "cited_value": identifier_value,
            "observed_value": None,
            "source": str(result.get("via") or "identifier resolver"),
            "basis": str(result.get("reason") or "authoritative identifier lookup returned not found"),
        })
    elif status == "identifier_mismatch" and identifier_field is not None:
        refutations.append({
            "kind": "identifier_targets_other_work",
            "field": identifier_field,
            "cited_value": identifier_value,
            "observed_value": (
                result.get("identifier_target_title") or result.get("matched_title")
            ),
            "source": str(
                result.get("identifier_target_via")
                or result.get("via")
                or "identifier resolver"
            ),
            "basis": str(result.get("reason") or "identifier metadata contradicts the cited work"),
        })

    if issue_target_absent:
        target_order = absent_attestation.get("target_member_order")
        members = absent_attestation.get("members") or []
        observed_title = None
        if (isinstance(target_order, int) and not isinstance(target_order, bool)
                and 0 <= target_order < len(members)
                and isinstance(members[target_order], dict)):
            observed_title = members[target_order].get("title")
        cited_scope = ", ".join(
            value for value in (
                str(absent_attestation.get("cited_container") or "").strip(),
                f"volume {absent_attestation.get('cited_volume')}"
                if absent_attestation.get("cited_volume") else "",
                f"issue {absent_attestation.get('cited_issue')}"
                if absent_attestation.get("cited_issue") else "",
            ) if value
        )
        refutations.append({
            "kind": "absent_from_complete_issue",
            "field": "issue_inventory",
            "cited_value": cited_scope,
            "observed_value": observed_title,
            "source": str(absent_attestation.get("provider") or "complete issue archive"),
            "basis": str(
                absent_attestation.get("completeness_basis")
                or "complete issue inventory does not contain the cited work"
            ),
        })

    # Coordinate contradictions are admissible only after identity is locked by
    # a successful resolution or by a separately corroborated correction.
    locked_match = None
    locked_source = str(result.get("via") or "resolver")
    if resolved_identity_locked:
        locked_match = result.get("metadata_match") or evidence.get("metadata_match")
    elif recovered_identity_locked:
        locked_match = result.get("metadata_match") or evidence.get("metadata_match")
        locked_source = str(
            result.get("content_via") or result.get("via") or "correction search"
        )
    refutation_match = locked_match
    resolved_identifier = result.get("resolved_identifier")
    registered_titled_coordinate_lock = bool(
        resolved_identity_locked
        and result.get("via") == "crossref_metadata"
        and result.get("resolution_basis") == "metadata_search"
        and isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        == "crossref:registered_coordinate_certificate"
    )
    if registered_titled_coordinate_lock and isinstance(locked_match, dict):
        # The NLM/ISSN certificate explains only the exact cited-container to
        # primary-Crossref-container mismatch. Preserve that source evidence,
        # while excluding it from bibliographic refutation.
        refutation_match = dict(locked_match)
        refutation_match["coordinate_comparisons"] = [
            row for row in locked_match.get("coordinate_comparisons") or ()
            if not (
                isinstance(row, dict)
                and row.get("kind") == "container"
                and row.get("status") == "mismatch"
            )
        ]
    refutations.extend(_coordinate_refutations(refutation_match, source=locked_source))
    year_refutation = _year_refutation(ref, locked_match, source=locked_source)
    if year_refutation is not None:
        refutations.append(year_refutation)
    for attempt in attempts or ():
        if not isinstance(attempt, dict):
            continue
        via = str(attempt.get("via") or "")
        try:
            from . import providers as resolver_modules
        except ImportError:  # pragma: no cover - direct execution fallback
            from resolve import providers as resolver_modules
        if via != "pubmed_coordinate_occupancy" and not resolver_modules.coordinate_occupancy_via(via):
            continue
        if attempt.get("status") != "resolved":
            continue
        match = attempt.get("metadata_match") or {}
        cited_coordinates = ", ".join(
            str(item.get("cited_value")) for item in match.get("coordinate_comparisons") or ()
            if item.get("kind") in {"container", "volume", "article_page_range", "elocator", "article_number", "article_locator"}
        )
        if not cited_coordinates or not attempt.get("matched_title"):
            continue
        refutations.append({
            "kind": "coordinate_occupied_by_other_work",
            "field": "coordinates",
            "cited_value": cited_coordinates,
            "observed_value": attempt["matched_title"],
            "source": via,
            "basis": str(attempt.get("reason") or "provider metadata confirms another work at the cited coordinates"),
        })
    if _is_crossref_same_work_correction(result) and _anchored_issue_author_conflict(
        result.get("metadata_match") or evidence.get("metadata_match"),
        ref.get("ay_surname"),
    ):
        match = result.get("metadata_match") or evidence.get("metadata_match")
        refutations.append({
            "kind": "author_mismatch",
            "field": "author",
            "cited_value": match.get("cited_first_author"),
            "observed_value": match.get("matched_first_author"),
            "source": str(result.get("via") or "crossref_metadata"),
            "basis": (
                "same-work correction contains the cited author set, but the "
                "declared first author differs from the indexed first author"
            ),
        })
    elif issue_target_present and _anchored_issue_author_conflict(
        result.get("metadata_match") or evidence.get("metadata_match"),
        ref.get("ay_surname"),
    ):
        match = result.get("metadata_match") or evidence.get("metadata_match")
        refutations.append({
            "kind": "author_mismatch",
            "field": "author",
            "cited_value": match.get("cited_first_author"),
            "observed_value": match.get("matched_first_author"),
            "source": str(present_attestation.get("provider") or "complete issue archive"),
            "basis": "identity-locked issue member contradicts the cited first author",
        })

    if resolved_identity_locked and not any(
        item["kind"] == "author_mismatch" for item in refutations
    ):
        author_refutation = _bound_validation_author_refutation(ref, result, attempts)
        if author_refutation is not None:
            refutations.append(author_refutation)

    fallback_status = fallback.get("status")
    if resolved_identity_locked:
        correction_status = "not_needed"
    elif recovered_identity_locked:
        correction_status = "identified"
    elif issue_target_absent:
        correction_status = "not_found"
    elif fallback_status == "ambiguous":
        correction_status = "ambiguous"
    elif fallback_status in {"resolved", "not_found", "unverified"}:
        correction_status = "not_found"
    else:
        correction_status = "not_attempted"

    checks_complete = bool(evidence.get("minimum_checks_completed"))
    if has_unresolved_identity_attempt(attempts):
        checks_complete = False
    if status in {"not_found", "identifier_mismatch"}:
        checks_complete = checks_complete and correction_status in {
            "identified", "ambiguous", "not_found",
        }
    if status == "unresolved":
        checks_complete = False
    if issue_target_absent and not has_unresolved_identity_attempt(attempts):
        checks_complete = True

    if resolved_identity_locked:
        identity_status = "identified_with_errors" if refutations else "identified"
        outcome = identity_status
    elif recovered_identity_locked or (
        status in {"not_found", "identifier_mismatch"}
        and correction_status == "identified"
    ):
        identity_status = "identified_with_errors"
        outcome = "identified_with_errors"
    elif any(item["kind"] == "coordinate_occupied_by_other_work" for item in refutations):
        identity_status = "not_identified"
        outcome = "refuted"
    elif status in {"not_found", "identifier_mismatch"}:
        identity_status = "ambiguous" if correction_status == "ambiguous" else "not_identified"
        outcome = "refuted" if checks_complete and refutations else "checks_incomplete"
    elif issue_target_absent:
        identity_status = "not_identified"
        outcome = "refuted" if checks_complete else "checks_incomplete"
    else:
        identity_status = "not_identified"
        outcome = "not_corroborated" if checks_complete else "checks_incomplete"

    return {
        "rule_version": RULE_VERSION,
        "outcome": outcome,
        "identity_status": identity_status,
        "check_status": "complete" if checks_complete else "incomplete",
        "correction_status": correction_status,
        "refutations": refutations,
    }
