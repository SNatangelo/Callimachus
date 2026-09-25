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
from typing import Any

try:
    from .matching import _author_names_equivalent, _fold_author, is_unique_same_work_correction_candidate
except ImportError:  # direct execution
    from resolve.matching import _author_names_equivalent, _fold_author, is_unique_same_work_correction_candidate


RULE_VERSION = "bibliographic-adjudication/v4"


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
    if basis != "metadata_search":
        return True

    # A registry-validated identifier discovered from an admitted metadata
    # candidate locks that candidate's identity, but does not erase cited-field
    # discrepancies (notably a wrong publication year).
    resolved_identifier = result.get("resolved_identifier")
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
    if (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        and (metadata_match.get("title_overlap") or 0) >= 0.85
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
    if not isinstance(overlap, (int, float)) or overlap < 0.85:
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
    refutations.extend(_coordinate_refutations(locked_match, source=locked_source))
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
