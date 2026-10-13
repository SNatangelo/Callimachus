#!/usr/bin/env python3
# core/resolve/decision.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Resolution decision helpers extracted from core.resolve."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import re

try:
    from ..parse.citation_coordinates import (
        _has_closed_parenthesized_journal_tuple,
        _normal as _normal_cited_coordinate,
    )
    from .resolve_types import EvidencePayload, Identity, IdentityState, Retraction, StageRecord
    from .matching import (
        _anchored_metadata_author_conflict,
        _metadata_candidate_has_container_and_locator_conflict,
        _metadata_candidate_has_container_and_volume_conflict,
        _metadata_candidate_has_source_spanned_definite_locator_conflict,
        _metadata_candidate_has_volume_and_locator_conflict,
        _metadata_candidate_rejection_reason,
        _crossref_terminal_numeric_title_range_result_supported,
        _has_structurally_titled_article_coordinates,
    )
except ImportError:  # direct execution fallback
    from parse.citation_coordinates import (
        _has_closed_parenthesized_journal_tuple,
        _normal as _normal_cited_coordinate,
    )
    from resolve.resolve_types import EvidencePayload, Identity, IdentityState, Retraction, StageRecord
    from resolve.matching import (
        _anchored_metadata_author_conflict,
        _metadata_candidate_has_container_and_locator_conflict,
        _metadata_candidate_has_container_and_volume_conflict,
        _metadata_candidate_has_source_spanned_definite_locator_conflict,
        _metadata_candidate_has_volume_and_locator_conflict,
        _metadata_candidate_rejection_reason,
        _crossref_terminal_numeric_title_range_result_supported,
        _has_structurally_titled_article_coordinates,
    )


_PUBLISHER_NATIVE_HTML_QUERY_CONTRACT = (
    "Crossref selected DOI publisher native HTML candidate lookup"
)
_PUBLISHER_NATIVE_URI_QUERY_CONTRACT = (
    "Crossref selected DOI publisher native PII URI candidate lookup"
)
_PUBLISHER_NATIVE_URI_VALIDATED_VIA = (
    "crossref:publisher_native_pii_uri_attestation"
)
_DATACITE_NATIVE_DOI_VALIDATED_VIA = "datacite:native_doi_metadata"


def _resolve_module():
    try:
        return importlib.import_module("core.resolve.service")
    except ImportError:  # direct execution fallback
        return importlib.import_module("service")


def _actionable_doi_metadata_conflicts(ref: dict, result: dict) -> tuple[str, ...]:
    """Hard conflicts only when title search actually discovered a DOI."""
    resolve_mod = _resolve_module()
    if not resolve_mod._result_doi(result):
        return ()
    profile = result.get("metadata_match") or {}
    conflicts = []
    if (
        profile.get("cited_first_author")
        and profile.get("matched_first_author")
        and profile.get("author_match") is False
    ):
        conflicts.append("author")
    if (
        ref.get("year") is not None
        and profile.get("matched_year") is not None
        and profile.get("year_match") is False
        and profile.get("year_mismatch_plausible") is not True
    ):
        conflicts.append("year")
    venue_overlap = profile.get("venue_overlap")
    if isinstance(venue_overlap, (int, float)) and venue_overlap < 0.50:
        conflicts.append("venue")
    return tuple(conflicts)


_LEGAL_ANCHOR_PATTERNS = (
    # Case dockets have a closed issuer plus a structural case number.  A bare
    # number is deliberately not an anchor: it occurs in ordinary prose and
    # cannot establish that a metadata record is the cited legal instrument.
    re.compile(
        r"\b(?:ICSID|UNCITRAL)\s+(?:Case\s+)?No\.?\s*"
        r"[A-Z]{2,8}\s*/\s*\d{1,4}\s*/\s*\d{1,4}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bCase\s+No\.?\s*[A-Z0-9][A-Z0-9./-]{2,}\b", re.IGNORECASE),
    # Reporters and treaty series are positional identifiers, not title words.
    re.compile(
        r"\b\d{1,4}\s+(?:U\.?\s*S\.?|S\.?\s*Ct\.?|"
        r"F\.?\s*(?:Supp\.?\s*)?(?:\d+d|App'?x)|"
        r"L\.?\s*Ed\.?\s*(?:2d|3d)|I\.?\s*C\.?\s*J\.?|E\.?\s*C\.?\s*R\.?)\s+\d{1,5}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d{1,4}\s+(?:U\.?\s*N\.?\s*T\.?\s*S\.?|"
        r"T\.?\s*S\.?|Consol\.?\s*T\.?\s*S\.?)\s+\d{1,5}\b",
        re.IGNORECASE,
    ),
)
_MONOGRAPH_WORK_TYPES = {"book", "edited-book", "monograph"}
_STRICT_IN_CONTAINER_RE = re.compile(
    r"\bIn:\s*([^.;]{3,200})\.\s*"
    r"(?:(?:[A-Z][^.:;]{1,80}:\s*[^.;]{1,120}\.)|(?:\(?\d{4}\)?\.))?\s*$",
    re.IGNORECASE,
)
_TERMINAL_VOLUME_SUFFIX_RE = re.compile(
    r"(?:\s*(?:[-‐‑‒–—]\s*|\b(?:vol(?:ume)?|tome|band|part)\.?\s+))"
    r"(?P<number>[A-Za-z0-9]+)\s*$",
    re.IGNORECASE,
)
_ROMAN_NUMERAL_RE = re.compile(
    r"(?=.+$)M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$",
    re.IGNORECASE,
)


def _relation_key(value: object) -> str:
    """Closed comparison key for structural anchors and titles."""
    return " ".join(re.findall(r"[\w]+", str(value or "").casefold()))


def _citation_legal_anchor(raw_entry: object) -> str | None:
    raw = str(raw_entry or "")
    for pattern in _LEGAL_ANCHOR_PATTERNS:
        match = pattern.search(raw)
        if match:
            return match.group(0)
    return None


def _candidate_metadata_text(result: dict, validation: dict) -> str:
    """Only textual metadata returned by the candidate or DOI authority."""
    values = (
        result.get("matched_title"),
        result.get("abstract"),
        validation.get("matched_title"),
        validation.get("abstract"),
    )
    return " ".join(str(value) for value in values if isinstance(value, str))


def _strict_in_container(raw_entry: object) -> str | None:
    match = _STRICT_IN_CONTAINER_RE.search(str(raw_entry or ""))
    return match.group(1).strip() if match else None


def _is_structural_volume_number(value: str) -> bool:
    return value.isdecimal() or bool(_ROMAN_NUMERAL_RE.fullmatch(value))


def _container_title_compatible(candidate_title: object, container: object) -> bool:
    """Compare a container exactly, allowing only a terminal volume suffix."""
    container_key = _relation_key(container)
    candidate_key = _relation_key(candidate_title)
    if not container_key or not candidate_key:
        return False
    if candidate_key == container_key:
        return True
    candidate = str(candidate_title or "")
    suffix = _TERMINAL_VOLUME_SUFFIX_RE.search(candidate)
    if not suffix or not _is_structural_volume_number(suffix.group("number")):
        return False
    return _relation_key(candidate[:suffix.start()]) == container_key


def _discovered_doi_relation(
    ref: dict,
    result: dict,
    validation: dict,
) -> tuple[str, str] | None:
    """Return a proven DOI/citation-unit incompatibility or inconclusiveness.

    This runs only after Crossref has established that a DOI exists.  It does
    not reinterpret work types globally: legal instruments require their own
    citation-derived anchor, while a chapter-shaped citation is withheld only
    for the closed ``In:`` container substitution shape.
    """
    if ref.get("source_kind") == "legal_instrument":
        anchor = _citation_legal_anchor(ref.get("raw_entry"))
        if not anchor:
            return (
                "inconclusive",
                "legal citation has no structural anchor for discovered DOI admission",
            )
        anchor_key = _relation_key(anchor)
        candidate_key = _relation_key(_candidate_metadata_text(result, validation))
        if f" {anchor_key} " not in f" {candidate_key} ":
            return (
                "inconclusive",
                "candidate metadata does not contain the citation legal anchor",
            )

    container = _strict_in_container(ref.get("raw_entry"))
    cited_title = ref.get("title")
    candidate_title = validation.get("matched_title") or result.get("matched_title")
    candidate_type = str(
        validation.get("work_type") or result.get("work_type") or ""
    ).casefold()
    if (
        container
        and isinstance(cited_title, str)
        and cited_title.strip()
        and candidate_type in _MONOGRAPH_WORK_TYPES
        and _container_title_compatible(candidate_title, container)
        and _relation_key(candidate_title) != _relation_key(cited_title)
    ):
        return (
            "incompatible",
            "candidate is the strict In: container, not the citation-owned chapter",
        )

    direct_work_type = re.sub(
        r"[\s_]+", "-", str(validation.get("work_type") or "").strip().casefold(),
    )
    source_type = str(ref.get("source_type") or "").strip().casefold()
    source_kind = str(ref.get("source_kind") or "").strip().casefold()
    journal_unit = (
        direct_work_type in {"book-chapter", "posted-content"}
        and source_type in {"article", "unknown"}
        and source_kind in {"article_like", "unknown"}
        and _has_source_spanned_titled_journal_unit(ref)
    )
    if direct_work_type == "posted-content" and journal_unit:
        return (
            "incompatible",
            "candidate is posted content rather than the source-spanned journal article",
        )
    if (
        direct_work_type == "book-chapter"
        and journal_unit
    ):
        coordinates = [
            item for item in ref.get("cited_coordinates") or ()
            if isinstance(item, dict) and item.get("kind") == "container"
        ]
        profile = validation.get("metadata_match")
        comparisons = [
            item for item in (profile or {}).get("coordinate_comparisons") or ()
            if isinstance(item, dict) and item.get("kind") == "container"
        ] if isinstance(profile, dict) else []
        if len(coordinates) == 1 and len(comparisons) == 1:
            cited_container = coordinates[0].get("normalized_value")
            comparison = comparisons[0]
            matched_container = comparison.get("matched_value")
            if (
                isinstance(cited_container, str)
                and cited_container.strip()
                and comparison.get("status") == "mismatch"
                and comparison.get("cited_value") == cited_container
                and isinstance(matched_container, str)
                and matched_container.strip()
            ):
                cited_key = _relation_key(cited_container)
                matched_key = _relation_key(matched_container)
                if cited_key and matched_key and matched_key != cited_key:
                    return (
                        "incompatible",
                        "candidate is a book chapter in a different container than the "
                        "source-spanned journal article",
                    )
    return None


def _has_source_spanned_titled_journal_unit(ref: dict) -> bool:
    """Prove a journal unit without treating extraction tail as its metadata.

    This certificate is used only to withhold incompatible discovered units.
    The complete raw citation and all its coordinate spans remain unchanged.
    """
    if _has_structurally_titled_article_coordinates(ref):
        return True
    raw = ref.get("raw_entry")
    year = ref.get("year")
    if not isinstance(raw, str) or type(year) is not int:
        return False
    # A generic scalar proves only the unit being withheld, never a positive
    # identity. Require both the literal author/title grammar and closed tuple.
    if (
        _has_closed_parenthesized_journal_tuple(
            raw, ref.get("cited_coordinates"), year, allow_generic_scalar=True,
        )
        and _has_structurally_titled_article_coordinates(ref, allow_generic_scalar=True)
    ):
        return True
    locators = [
        row for row in ref.get("cited_coordinates") or ()
        if isinstance(row, dict)
        and row.get("kind") in {"article_page_range", "article_locator", "article_number", "elocator"}
    ]
    if len(locators) != 1:
        return False
    spans = locators[0].get("spans")
    if not isinstance(spans, list) or len(spans) != 1 or not isinstance(spans[0], dict):
        return False
    end = spans[0].get("raw_end")
    if type(end) is not int or not 0 < end <= len(raw):
        return False
    boundary = re.match(
        rf"\s*\(\s*{re.escape(str(year))}\s*\)\s*[.;](?=\s|$)", raw[end:],
    )
    if boundary is None:
        return False
    # Revalidate the full author/title/container/volume/locator/year grammar
    # and the original coordinate offsets on this closed literal span.
    return _has_structurally_titled_article_coordinates(
        {**ref, "raw_entry": raw[:end + boundary.end()]},
    )


def _withhold_discovered_doi_promotion(
    result: dict,
    doi: str,
    *,
    relation: str,
    reason: str,
) -> dict:
    """Keep a validated DOI auditable without admitting it as the citation."""
    return _withhold_metadata_candidate_identity(
        result,
        doi=doi,
        reason=(
            f"DOI exists but citation-unit relation is {relation}; "
            f"identifier promotion withheld: {reason}"
        ),
    )


def _withhold_metadata_candidate_identity(
    result: dict,
    *,
    doi: str | None = None,
    reason: str,
) -> dict:
    """Keep a metadata candidate auditable while clearing operative identity."""
    guarded = dict(result)
    for field in (
        "abstract",
        "fulltext_links",
        "auxiliary_fulltext_links",
        "doi",
        "identifiers",
        "resolved_identifier",
    ):
        guarded.pop(field, None)
    guarded["status"] = "unverified"
    guarded["existence_confidence"] = "low"
    candidate_identifiers = dict(guarded.pop("candidate_identifiers", {}) or {})
    if doi:
        candidate_identifiers["doi"] = doi
    if candidate_identifiers:
        guarded["candidate_identifiers"] = candidate_identifiers
    guarded["reason"] = reason
    return guarded


def _datacite_native_doi_evidence(
    ref: dict,
    doi: str,
    proof: object,
) -> tuple[dict | None, str | None, str | None, str | None]:
    """Recompute citation compatibility from a retained DataCite DOI record."""
    try:
        from .providers import datacite as datacite_provider
    except ImportError:  # pragma: no cover - direct execution fallback
        from providers import datacite as datacite_provider
    resolve_mod = _resolve_module()
    try:
        record = datacite_provider._native_doi_record_from_proof(
            proof, expected_doi=doi,
        )
    except (TypeError, ValueError):
        return None, None, None, "DataCite native DOI proof failed validation"
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        return None, None, None, "DataCite DOI record lacks attributes"
    titles = attributes.get("titles")
    native_title = (
        titles[0].get("title")
        if isinstance(titles, list) and titles and isinstance(titles[0], dict)
        else None
    )
    if not isinstance(native_title, str) or not native_title.strip():
        return None, None, None, "DataCite DOI record lacks a native title"
    native_title = re.sub(r"\s+", " ", native_title).strip()

    cited_title = resolve_mod._article_title_candidate(ref) or ""
    software_reference = datacite_provider._explicit_software_version(
        ref, cited_title,
    )
    scoring_ref = ref
    native_types = attributes.get("types")
    resource_type = (
        native_types.get("resourceTypeGeneral")
        if isinstance(native_types, dict)
        else None
    )
    if software_reference is not None:
        work_name, cited_release = software_reference
        scoring_ref = dict(ref)
        scoring_ref["title"] = work_name
        if resource_type != "Software":
            return None, native_title, None, "DataCite DOI record is not software"
        if not datacite_provider._matches_cited_version(
            cited_release, attributes.get("version"),
        ):
            return None, native_title, None, "DataCite DOI record has a different or missing software version"

    creators = attributes.get("creators")
    authors = []
    if isinstance(creators, list):
        for creator in creators:
            if not isinstance(creator, dict):
                continue
            name = creator.get("familyName") or creator.get("name")
            if isinstance(name, str) and name.strip():
                authors.append({"family": name.strip()})
    native_year = attributes.get("publicationYear")
    try:
        native_year = int(native_year) if native_year is not None else None
    except (TypeError, ValueError):
        native_year = None
    native_metadata = {
        "DOI": attributes.get("doi"),
        "type": resource_type,
        "title": [native_title],
        "author": authors,
        "container-title": [],
    }
    if native_year is not None:
        native_metadata["published"] = {"date-parts": [[native_year]]}
    profile = resolve_mod._metadata_match_profile(
        scoring_ref, native_metadata, native_title,
    )
    if not isinstance(profile, dict):
        return (
            None,
            native_title,
            resource_type if isinstance(resource_type, str) else None,
            "DataCite DOI metadata could not be compared",
        )
    if not isinstance(profile.get("title_overlap"), (int, float)) or profile["title_overlap"] < 0.85:
        return profile, native_title, resource_type if isinstance(resource_type, str) else None, "DataCite DOI title is incompatible with the cited work"
    if software_reference is not None:
        if (
            profile.get("cited_first_author")
            and profile.get("matched_first_author")
            and profile.get("author_match") is False
        ):
            return profile, native_title, resource_type, "DataCite DOI software creator conflicts with the citation"
        if (
            ref.get("year") is not None
            and profile.get("matched_year") is not None
            and profile.get("year_match") is False
        ):
            return profile, native_title, resource_type, "DataCite DOI software year conflicts with the citation"
    return profile, native_title, resource_type if isinstance(resource_type, str) else None, None


def _datacite_native_doi_decision_issue(
    ref: dict,
    result: dict,
    doi: str,
    profile: dict,
    native_title: str,
    resource_type: str | None,
) -> tuple[str | None, tuple[str, str] | None]:
    """Reapply existing metadata, author, relation, and locator gates."""
    relation_work_type = (
        re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", resource_type).casefold()
        if isinstance(resource_type, str)
        else None
    )
    direct_result = dict(result)
    direct_result.update({
        "doi": doi,
        "matched_title": native_title,
        "metadata_match": profile,
        "work_type": relation_work_type,
    })
    candidate_result = dict(result)
    candidate_result["doi"] = doi
    if len(_actionable_doi_metadata_conflicts(ref, direct_result)) >= 2:
        return (
            "direct DataCite metadata has multiple hard identity conflicts; "
            "identifier promotion withheld",
            None,
        )
    if _anchored_metadata_author_conflict(profile, ref.get("ay_surname")):
        return (
            "direct DataCite DOI creator conflicts with the cited author; "
            "identifier promotion withheld",
            None,
        )
    relation = _discovered_doi_relation(ref, candidate_result, direct_result)
    if relation is not None:
        return None, relation
    if (
        _metadata_candidate_has_source_spanned_definite_locator_conflict(
            ref,
            profile,
            work_type=relation_work_type,
            matched_title=native_title,
        )
        or _metadata_candidate_has_container_and_locator_conflict(ref, profile)
        or _metadata_candidate_has_container_and_volume_conflict(ref, profile)
    ):
        return (
            "direct DataCite metadata contradicts cited source coordinates; "
            "identifier promotion withheld",
            None,
        )
    return None, None


def _datacite_native_doi_admission_is_valid(
    ref: dict,
    result: dict,
    attempts: list[dict] | None,
) -> bool:
    """Relock a DataCite DOI only from its exact retained native record proof."""
    resolved_identifier = result.get("resolved_identifier")
    if (
        not isinstance(resolved_identifier, dict)
        or resolved_identifier.get("type") != "doi"
        or resolved_identifier.get("validated_via") != _DATACITE_NATIVE_DOI_VALIDATED_VIA
        or result.get("status") != "resolved"
        or result.get("resolution_basis") != "metadata_search"
    ):
        return False
    doi = _normalize_identity_value("doi", resolved_identifier.get("value"))
    result_doi = _normalize_identity_value(
        "doi",
        _resolve_module()._result_doi(result)
        or resolved_identifier.get("value"),
    )
    if not doi or result_doi != doi:
        return False
    try:
        from .providers import datacite as datacite_provider
    except ImportError:  # pragma: no cover - direct execution fallback
        from providers import datacite as datacite_provider
    proof_field = datacite_provider.DATACITE_NATIVE_DOI_PROOF_FIELD
    matched_attempts = [
        attempt for attempt in attempts or ()
        if isinstance(attempt, dict)
        and attempt.get("via") == "identifier_validation:datacite"
        and attempt.get("status") == "resolved"
        and attempt.get("identifier_type") == "doi"
        and _normalize_identity_value("doi", attempt.get("identifier_value")) == doi
        and proof_field in attempt
    ]
    if len(matched_attempts) != 1:
        return False
    attempt = matched_attempts[0]
    profile, native_title, _resource_type, issue = _datacite_native_doi_evidence(
        ref, doi, attempt.get(proof_field),
    )
    if issue or profile is None or native_title is None:
        return False
    if re.sub(r"\s+", " ", str(result.get("matched_title") or "")).strip() != native_title:
        return False
    evidence_profile = result.get("evidence_profile")
    persisted_profile = (
        evidence_profile.get("metadata_match")
        if isinstance(evidence_profile, dict)
        else None
    )
    result_profile = result.get("metadata_match") or persisted_profile
    if not (
        attempt.get("matched_title") == native_title
        and attempt.get("metadata_match") == profile
        and result_profile == profile
    ):
        return False
    if _is_published_preprint_candidate(ref, result, attempts):
        return False
    issue, relation = _datacite_native_doi_decision_issue(
        ref, result, doi, profile, native_title, _resource_type,
    )
    return issue is None and relation is None


def _promote_validated_identifier(
    ref: dict,
    result: dict | None,
    attempts: list[dict],
    *,
    trusted_publisher_uri_attestation: bool = False,
) -> dict | None:
    """Upgrade a metadata-only resolve to a terminal strong-identifier resolve.

    A DOI discovered during metadata search becomes terminal only after a direct,
    deterministic DOI lookup confirms it. This freezes the work identity while
    leaving fetch/copy selection to the fetch layer.
    """
    if not isinstance(result, dict):
        return result
    resolve_mod = _resolve_module()
    resolved_identifier = result.get("resolved_identifier")
    if (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        == _DATACITE_NATIVE_DOI_VALIDATED_VIA
    ):
        if _datacite_native_doi_admission_is_valid(ref, result, attempts):
            return result
        return _withhold_metadata_candidate_identity(
            result,
            doi=resolve_mod._result_doi(result),
            reason=(
                "DataCite native DOI proof failed citation revalidation; "
                "identity withheld"
            ),
        )
    if (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        == "crossref:arxiv_published_journal_unit_certificate"
    ):
        try:
            from .providers import arxiv as arxiv_provider
        except ImportError:  # pragma: no cover - direct execution fallback
            from providers import arxiv as arxiv_provider
        if arxiv_provider._published_relation_proof_is_valid(ref, result, attempts):
            return result
        return _withhold_metadata_candidate_identity(
            result,
            doi=resolve_mod._result_doi(result),
            reason="arXiv published relation certificate failed revalidation; identity withheld",
        )
    if (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        == "crossref:generic_article_locator_role_certificate"
    ):
        from .providers import crossref_metadata

        evidence = {
            "resolution_basis": result.get("resolution_basis"),
            "metadata_match": result.get("metadata_match"),
        }
        if crossref_metadata._crossref_generic_article_locator_relock(
            ref, evidence, result, attempts,
        ):
            return result
        return _withhold_metadata_candidate_identity(
            result,
            doi=resolve_mod._result_doi(result),
            reason=(
                "Crossref generic-locator certificate failed revalidation; "
                "candidate identity withheld"
            ),
        )
    if (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        == "crossref:ambiguous_title_journal_boundary_certificate"
    ):
        from .providers import crossref_metadata

        if crossref_metadata._crossref_ambiguous_boundary_relock(
            ref, result, attempts,
        ):
            return result
        return _withhold_metadata_candidate_identity(
            result,
            doi=resolve_mod._result_doi(result),
            reason=(
                "Crossref ambiguous-title boundary proof failed citation revalidation; "
                "candidate identity withheld"
            ),
        )
    uri_doi = (
        _normalize_identity_value("doi", resolved_identifier.get("value"))
        if isinstance(resolved_identifier, dict)
        and resolved_identifier.get("type") == "doi"
        else ""
    )
    uri_contract = (
        isinstance(result.get("identity_search"), dict)
        and result["identity_search"].get("query_contract")
        == _PUBLISHER_NATIVE_URI_QUERY_CONTRACT
    ) or any(
        isinstance(attempt, dict)
        and attempt.get("via") == "publisher_native_uri_metadata"
        and attempt.get("status") == "resolved"
        and _normalize_identity_value("doi", attempt.get("doi")) == uri_doi
        and isinstance(attempt.get("identity_search"), dict)
        and attempt["identity_search"].get("query_contract")
        == _PUBLISHER_NATIVE_URI_QUERY_CONTRACT
        and attempt["identity_search"].get("outcome") == "compatible_identity"
        for attempt in attempts or ()
    )
    persisted_attestation = (
        isinstance(result.get("resolved_identifier"), dict)
        and result["resolved_identifier"].get("validated_via")
        == _PUBLISHER_NATIVE_URI_VALIDATED_VIA
        and result.get("via") == "publisher_native_uri_metadata"
        and uri_contract
        and resolve_mod._resolved_identity_is_locked(ref, {}, result, attempts)
    )
    trusted_publisher_uri_attestation = bool(
        (trusted_publisher_uri_attestation or persisted_attestation)
        and result.get("via") == "publisher_native_uri_metadata"
        and uri_contract
        and result.get("status") == "resolved"
        and result.get("resolution_basis") == "metadata_search"
        and not ref.get("doi")
    )
    resolved_identifier = result.get("resolved_identifier")
    if (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("validated_via")
        == _PUBLISHER_NATIVE_URI_VALIDATED_VIA
        and not trusted_publisher_uri_attestation
    ):
        # This reserved proof can only be minted after the private publisher
        # attester and direct Crossref validation have both succeeded.
        result.pop("resolved_identifier", None)
    if result.get("status") != "resolved":
        return result
    if _is_published_preprint_candidate(ref, result, attempts):
        discovered_doi = resolve_mod._result_doi(result)
        return _withhold_metadata_candidate_identity(
            result,
            doi=discovered_doi or None,
            reason="preprint identity withheld for a citation with published journal coordinates",
        )
    if result.get("resolution_basis") != "metadata_search":
        return result
    if not ref.get("doi"):
        unit_reason = _metadata_candidate_rejection_reason(
            ref,
            result.get("matched_title"),
            result.get("metadata_match"),
            candidate_type=result.get("work_type"),
        )
        if unit_reason:
            return _withhold_metadata_candidate_identity(
                result,
                doi=resolve_mod._result_doi(result),
                reason=unit_reason,
            )
    if _metadata_candidate_has_volume_and_locator_conflict(
        result.get("metadata_match")
    ):
        return _withhold_metadata_candidate_identity(
            result,
            doi=resolve_mod._result_doi(result),
            reason=(
                "metadata candidate contradicts both cited volume and article locator; "
                "candidate identity withheld"
            ),
        )
    hard_conflicts = _actionable_doi_metadata_conflicts(ref, result)
    if len(hard_conflicts) >= 2:
        doi = resolve_mod._result_doi(result)
        guarded = _withhold_metadata_candidate_identity(
            result,
            doi=doi,
            reason=(
                "metadata candidate has multiple hard identity conflicts; "
                "identifier promotion withheld"
            ),
        )
        metadata_match = dict(result.get("metadata_match") or {})
        metadata_match.update({
            "author_conflict": "author" in hard_conflicts,
            "year_conflict": "year" in hard_conflicts,
            "venue_conflict": "venue" in hard_conflicts,
            "hard_conflicts": list(hard_conflicts),
            "metadata_conflict": True,
        })
        guarded["metadata_match"] = metadata_match
        guarded["metadata_conflict"] = True
        return guarded
    doi = resolve_mod._result_doi(result)
    resolved_identifier = result.get("resolved_identifier")
    is_unique_same_work_correction = (
        isinstance(resolved_identifier, dict)
        and resolved_identifier.get("type") == "doi"
        and resolved_identifier.get("value")
        and resolved_identifier.get("validated_via")
        == "crossref:unique_same_work_correction"
    )
    if (
        doi
        and not ref.get("doi")
        and _anchored_metadata_author_conflict(
            result.get("metadata_match"), ref.get("ay_surname")
        )
        and not is_unique_same_work_correction
    ):
        guarded = dict(result)
        metadata_match = dict(result.get("metadata_match") or {})
        metadata_match.update({
            "author_conflict": True,
            "year_conflict": False,
            "venue_conflict": False,
            "hard_conflicts": ["author"],
            "metadata_conflict": True,
        })
        guarded["metadata_match"] = metadata_match
        for field in (
            "abstract",
            "fulltext_links",
            "auxiliary_fulltext_links",
            "doi",
            "identifiers",
            "resolved_identifier",
        ):
            guarded.pop(field, None)
        guarded["status"] = "unverified"
        guarded["existence_confidence"] = "low"
        guarded["metadata_conflict"] = True
        if doi:
            guarded["candidate_identifiers"] = {"doi": doi}
        guarded["reason"] = (
            "metadata candidate first author conflicts with parsed citation; "
            "identifier promotion withheld"
        )
        return guarded
    if resolve_mod._resolved_identifier_summary(result):
        return result
    terminal_range_title_supported = (
        _crossref_terminal_numeric_title_range_result_supported(ref, result)
    )
    if (
        resolve_mod._result_title_overlap(result) < 0.85
        and not trusted_publisher_uri_attestation
        and not terminal_range_title_supported
    ):
        return result

    doi = resolve_mod._result_doi(result)
    if doi:
        normalized_doi = _normalize_identity_value("doi", doi).lower()
        if any(
            str(attempt.get("via") or "") == "identifier_validation:doi.org"
            and str(attempt.get("identifier_type") or "") == "doi"
            and str(attempt.get("status") or "") == "resolved"
            and _normalize_identity_value("doi", attempt.get("identifier_value")).lower()
            == normalized_doi
            for attempt in attempts
        ):
            return result
        crossref = resolve_mod._crossref_resolver_module()
        if ref.get("doi"):
            validation = crossref.resolve_doi(doi)
        elif (
            result.get("via") == "europepmc"
            and isinstance(result.get("identity_search"), dict)
            and result["identity_search"].get("query_contract")
            == "Europe PMC exact DOI core-record candidate lookup"
        ) or (
            result.get("via") == "publisher_native_metadata"
            and isinstance(result.get("identity_search"), dict)
            and result["identity_search"].get("query_contract")
            == _PUBLISHER_NATIVE_HTML_QUERY_CONTRACT
        ) or (
            result.get("via") == "publisher_native_uri_metadata"
            and isinstance(result.get("identity_search"), dict)
            and result["identity_search"].get("query_contract")
            == _PUBLISHER_NATIVE_URI_QUERY_CONTRACT
        ):
            validation = crossref._resolve_discovered_doi_exact(doi, ref=ref)
        else:
            validation = crossref.resolve_doi(doi, ref=ref)
        if isinstance(validation, dict):
            validation_identifier = validation.get("resolved_identifier")
            if (
                isinstance(validation_identifier, dict)
                and validation_identifier.get("validated_via")
                == _PUBLISHER_NATIVE_URI_VALIDATED_VIA
            ):
                # Direct resolver output is evidence, not the private publisher
                # attestation that authorizes this reserved lock marker.
                validation.pop("resolved_identifier", None)
        if validation.get("status") == "resolved":
            # A DOI printed by the citation is already a declared identifier;
            # this guard governs only identifiers discovered by metadata search.
            relation = None if ref.get("doi") else _discovered_doi_relation(
                ref, result, validation
            )
            traced_validation = dict(validation)
            if relation is not None:
                relation_state, relation_reason = relation
                traced_validation["reason"] = (
                    "DOI exists on Crossref; identifier promotion withheld: "
                    f"{relation_reason}"
                )
            attempts.append(
                {
                    **traced_validation,
                    "via": "identifier_validation:crossref",
                    "identifier_type": "doi",
                    "identifier_value": doi,
                }
            )
            if _is_published_preprint_candidate(ref, result, attempts):
                return _withhold_metadata_candidate_identity(
                    result,
                    doi=doi,
                    reason=(
                        "preprint identity withheld for a citation with published journal coordinates"
                    ),
                )
            if relation is not None:
                relation_state, relation_reason = relation
                return _withhold_discovered_doi_promotion(
                    result,
                    doi,
                    relation=relation_state,
                    reason=relation_reason,
                )
            direct_profile = validation.get("metadata_match")
            if (
                not any(ref.get(field) for field in ("doi", "pmid", "isbn"))
                and _metadata_candidate_has_source_spanned_definite_locator_conflict(
                    ref,
                    direct_profile,
                    work_type=validation.get("work_type"),
                    matched_title=validation.get("matched_title"),
                )
            ):
                return _withhold_metadata_candidate_identity(
                    result,
                    doi=doi,
                    reason=(
                        "direct Crossref journal-article metadata contradicts a "
                        "definite source locator; identifier promotion withheld"
                    ),
                )
            if not ref.get("doi") and (
                _metadata_candidate_has_container_and_locator_conflict(
                    ref, direct_profile,
                )
                or _metadata_candidate_has_container_and_volume_conflict(
                    ref, direct_profile,
                )
            ):
                return _withhold_metadata_candidate_identity(
                    result,
                    doi=doi,
                    reason=(
                        "direct Crossref metadata contradicts multiple source-spanned "
                        "citation coordinates; identifier promotion withheld"
                    ),
                )
            promoted = dict(result)
            # The local metadata candidate cannot erase an authority's
            # retraction indication.  A true value wins from either source.
            promoted["retracted"] = bool(result.get("retracted")) or bool(
                validation.get("retracted")
            )
            # A DOI discovered by a metadata search validates the candidate,
            # not an identifier declared by the citation.  Keep the discovery
            # provenance so later adjudication still compares every cited field.
            if ref.get("doi"):
                promoted["resolution_basis"] = "doi"
                promoted["existence_confidence"] = "high"
            promoted["resolved_identifier"] = {
                "type": "doi",
                "value": doi,
                "validated_via": (
                    _PUBLISHER_NATIVE_URI_VALIDATED_VIA
                    if trusted_publisher_uri_attestation
                    else "crossref"
                ),
            }
            return promoted
        attempts.append(
            {
                **validation,
                "via": "identifier_validation:crossref",
                "identifier_type": "doi",
                "identifier_value": doi,
            }
        )
        if validation.get("status") == "not_found":
            handle = resolve_mod._doi_handle(doi)
            attempts.append(
                {
                    **handle,
                    "via": "identifier_validation:doi.org",
                    "identifier_type": "doi",
                    "identifier_value": doi,
                }
            )
            try:
                from .providers import datacite as datacite_provider
            except ImportError:  # pragma: no cover - direct execution fallback
                from providers import datacite as datacite_provider
            native_validation = datacite_provider.validate_doi(doi)
            native_attempt = {
                **native_validation,
                "via": "identifier_validation:datacite",
                "identifier_type": "doi",
                "identifier_value": doi,
            }
            if native_validation.get("status") == "resolved":
                profile, native_title, resource_type, compatibility_issue = (
                    _datacite_native_doi_evidence(
                        ref,
                        doi,
                        native_validation.get(
                            datacite_provider.DATACITE_NATIVE_DOI_PROOF_FIELD,
                        ),
                    )
                )
                if native_title:
                    native_attempt["matched_title"] = native_title
                if isinstance(resource_type, str):
                    native_attempt["work_type"] = resource_type
                if isinstance(profile, dict):
                    native_attempt["metadata_match"] = profile
                attempts.append(native_attempt)
                if _is_published_preprint_candidate(ref, result, attempts):
                    native_attempt["reason"] = (
                        "preprint identity withheld for a citation with published journal coordinates"
                    )
                    return _withhold_metadata_candidate_identity(
                        result,
                        doi=doi,
                        reason=(
                            "preprint identity withheld for a citation with published journal coordinates"
                        ),
                    )
                if compatibility_issue or profile is None or native_title is None:
                    return _withhold_metadata_candidate_identity(
                        result,
                        doi=doi,
                        reason=(
                            "DataCite DOI record does not deterministically match the "
                            f"citation; identity withheld: {compatibility_issue or 'missing native metadata'}"
                        ),
                    )
                issue, relation = _datacite_native_doi_decision_issue(
                    ref, result, doi, profile, native_title, resource_type,
                )
                if issue is not None:
                    native_attempt["reason"] = issue
                    return _withhold_metadata_candidate_identity(
                        result,
                        doi=doi,
                        reason=issue,
                    )
                if relation is not None:
                    relation_state, relation_reason = relation
                    native_attempt["reason"] = (
                        "DataCite DOI exists; identifier promotion withheld: "
                        f"{relation_reason}"
                    )
                    return _withhold_discovered_doi_promotion(
                        result,
                        doi,
                        relation=relation_state,
                        reason=relation_reason,
                    )
                promoted = dict(result)
                promoted["matched_title"] = native_title
                promoted["metadata_match"] = profile
                promoted["resolved_identifier"] = {
                    "type": "doi",
                    "value": doi,
                    "validated_via": _DATACITE_NATIVE_DOI_VALIDATED_VIA,
                }
                return promoted
            attempts.append(native_attempt)
    return result


_GLOBAL_IDENTITY_SCHEMES = {"doi", "pmid", "isbn", "arxiv_id", "pmcid"}
_AUTHORITY_LOCAL_IDENTITY_SCHEMES = {"acl_id", "ssrn_abstract_id", "url"}
_STRONG_IDENTITY_SCHEMES = _GLOBAL_IDENTITY_SCHEMES | {"acl_id", "ssrn_abstract_id"}
_DOI_ASCII_CASE_TABLE = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz",
)


def _normalize_identity_value(scheme: str, value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    scheme = str(scheme or "").strip().lower()
    if scheme == "doi":
        doi = re.sub(
            r"^https?://(?:dx\.)?doi\.org/", "", raw,
            flags=re.IGNORECASE | re.ASCII,
        ).strip()
        return doi.translate(_DOI_ASCII_CASE_TABLE)
    if scheme == "pmid":
        return re.sub(r"\D+", "", raw)
    if scheme == "isbn":
        return re.sub(r"[^0-9Xx]", "", raw).upper()
    if scheme == "url":
        return raw
    return raw


def _identity_class_for_scheme(scheme: str) -> str:
    if scheme in _GLOBAL_IDENTITY_SCHEMES:
        return "global"
    if scheme in _AUTHORITY_LOCAL_IDENTITY_SCHEMES:
        return "authority_local"
    return "none"


def _identity_from_parts(scheme: str | None, value: str | None) -> Identity:
    scheme_text = str(scheme or "").strip().lower() or "none"
    value_text = _normalize_identity_value(scheme_text, value)
    if not value_text:
        return Identity(scheme="none", value="", is_strong=False, class_="none")
    return Identity(
        scheme=scheme_text,
        value=value_text,
        is_strong=scheme_text in _STRONG_IDENTITY_SCHEMES,
        class_=_identity_class_for_scheme(scheme_text),
    )


def _declared_identity(ref: dict) -> Identity:
    if ref.get("doi"):
        return _identity_from_parts("doi", ref.get("doi"))
    if ref.get("pmid"):
        return _identity_from_parts("pmid", ref.get("pmid"))
    if ref.get("isbn"):
        return _identity_from_parts("isbn", ref.get("isbn"))
    if ref.get("url"):
        return _identity_from_parts("url", ref.get("url"))
    return _identity_from_parts("none", None)


def _arxiv_identity_from_result(result: dict | None) -> Identity:
    for link in (result or {}).get("fulltext_links") or []:
        url = str((link or {}).get("url") or "").strip()
        match = re.search(r"arxiv\.org/pdf/([^/?#]+?)(?:\.pdf)?$", url, flags=re.IGNORECASE)
        if match:
            return _identity_from_parts("arxiv_id", match.group(1))
    return _identity_from_parts("none", None)


def _has_published_journal_unit(ref: dict | None) -> bool:
    """Whether the raw citation proves a published article unit with coordinates."""
    if not isinstance(ref, dict):
        return False
    raw = str(ref.get("raw_entry") or "")
    if not raw:
        return False
    resolve_mod = _resolve_module()
    normalize_doi = getattr(resolve_mod, "_normalize_doi_value", None)
    literal_doi = normalize_doi(ref.get("doi")) if callable(normalize_doi) else str(ref.get("doi") or "").strip()
    if literal_doi and literal_doi.casefold() in raw.casefold():
        return False
    coordinates = ref.get("cited_coordinates")
    if not isinstance(coordinates, list):
        return False
    year = ref.get("year")
    if _has_closed_parenthesized_journal_tuple(raw, coordinates, year):
        return True
    if (
        _has_closed_parenthesized_journal_tuple(
            raw, coordinates, year, allow_generic_scalar=True,
        )
        and _has_source_spanned_titled_journal_unit(ref)
    ):
        return True
    if (
        ref.get("source_kind") != "article_like"
        or ref.get("source_type_confidence") != "high"
        or type(year) is not int
    ):
        return False

    by_kind: dict[str, list[tuple[int, int]]] = {}
    locator_kinds = {"article_page_range", "article_locator", "article_number", "elocator"}
    for item in coordinates:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in {"container", "volume", *locator_kinds}:
            continue
        spans = item.get("spans")
        if not isinstance(spans, list) or len(spans) != 1 or not isinstance(spans[0], dict):
            return False
        start, end = spans[0].get("raw_start"), spans[0].get("raw_end")
        if (
            not isinstance(start, int) or isinstance(start, bool)
            or not isinstance(end, int) or isinstance(end, bool)
            or not 0 <= start < end <= len(raw)
            or raw[start:end] != item.get("raw_value")
            or not isinstance(item.get("normalized_value"), str)
            or not item.get("normalized_value")
            or _normal_cited_coordinate(raw[start:end]) != item.get("normalized_value")
        ):
            return False
        by_kind.setdefault(kind, []).append((start, end))
    locator_spans = [span for kind in locator_kinds for span in by_kind.get(kind, [])]
    if (
        len(by_kind.get("container", [])) != 1
        or len(by_kind.get("volume", [])) != 1
        or len(locator_spans) != 1
    ):
        return False
    container = by_kind["container"][0]
    volume = by_kind["volume"][0]
    locator = locator_spans[0]
    if not container[1] <= volume[0] < volume[1] <= locator[0] < locator[1]:
        return False
    year_text = str(year)
    between_volume_and_locator = raw[volume[1]:locator[0]]
    if len(re.findall(rf"(?<!\d){re.escape(year_text)}(?!\d)", between_volume_and_locator)) == 1:
        return True
    if (
        len(by_kind.get("article_page_range", [])) != 1
        or by_kind["article_page_range"] != [locator]
    ):
        return False

    # Some standard journal citations put the closed publication year after
    # the complete printed page range, e.g. ``814, 96–107 (2015)``. Accept that
    # shape only when the year is a terminal parenthesized tuple and any
    # following repository annotation is independently source-spanned.
    locator_tail = raw[locator[1]:]
    year_match = re.match(
        rf"^[\s,;.:]*\(\s*{re.escape(year_text)}\s*\)", locator_tail,
    )
    if year_match is None:
        return False
    suffix_start = locator[1] + year_match.end()
    suffix = list(raw[suffix_start:])
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
            if end <= suffix_start:
                continue
            if start < suffix_start:
                return False
            suffix[start - suffix_start:end - suffix_start] = " " * (end - start)
    suffix_text = "".join(suffix)
    # Use the same bounded catalogue-identifier syntax as Parse's closed
    # parenthesized journal tuple, without depending on a catalogue name.
    suffix_text = re.sub(r"\[[A-Za-z0-9][A-Za-z0-9:./_-]{0,79}\]", "", suffix_text)
    return bool(re.fullmatch(r"[\s\[\](){}.,;:]*", suffix_text))


def _is_published_preprint_candidate(
    ref: dict | None,
    candidate: dict | None,
    attempts: list[dict] | None = None,
) -> bool:
    """Recognize a preprint candidate only against a cited published journal unit.

    A posted-content work type alone is not a version/identity assertion. The
    candidate must carry an existing arXiv identity signal, an explicitly typed
    preprint file, a DOI-bound native Europe PMC preprint record, or a retained
    exact-DOI native record that explicitly declares preprint subtype or relation.
    """
    if not _has_published_journal_unit(ref) or not isinstance(candidate, dict):
        return False
    if candidate.get("via") == "arxiv_search":
        return True
    selected_dois = []
    candidate_doi = _normalize_identity_value("doi", candidate.get("doi"))
    if candidate_doi:
        selected_dois.append(candidate_doi.casefold())
    resolved_identifier = candidate.get("resolved_identifier")
    if isinstance(resolved_identifier, dict) and str(
        resolved_identifier.get("type") or resolved_identifier.get("scheme") or ""
    ).strip().lower() == "doi":
        resolved_doi = _normalize_identity_value("doi", resolved_identifier.get("value"))
        if resolved_doi:
            selected_dois.append(resolved_doi.casefold())
    identifiers = candidate.get("identifiers")
    if isinstance(identifiers, dict):
        identifier_doi = _normalize_identity_value("doi", identifiers.get("doi"))
        if identifier_doi:
            selected_dois.append(identifier_doi.casefold())
    selected_doi = selected_dois[0] if selected_dois and len(set(selected_dois)) == 1 else ""
    if not selected_dois and candidate.get("via") == "crossref":
        declared_doi = _normalize_identity_value("doi", (ref or {}).get("doi"))
        if declared_doi:
            selected_doi = declared_doi.casefold()
    if selected_doi:
        native_attempts = list(attempts or ())
        if candidate.get("via") == "europepmc":
            native_attempts.append(candidate)
        from .providers import crossref_metadata

        proof_sources = []
        if candidate.get("via") == "crossref" and candidate.get("status") == "resolved":
            proof_sources.append(candidate)
        for attempt in native_attempts:
            if not isinstance(attempt, dict) or attempt.get("status") != "resolved":
                continue
            attempt_doi = _normalize_identity_value("doi", attempt.get("doi"))
            if attempt_doi and attempt_doi.casefold() != selected_doi:
                continue
            if attempt.get("via") == "identifier_validation:crossref":
                identifier_doi = _normalize_identity_value(
                    "doi", attempt.get("identifier_value"),
                )
                if (
                    attempt.get("identifier_type") == "doi"
                    and identifier_doi
                    and identifier_doi.casefold() == selected_doi
                ):
                    proof_sources.append(attempt)
            elif attempt.get("via") == "crossref":
                proof_sources.append(attempt)
        if any(
            crossref_metadata._crossref_native_work_proof_is_valid(
                source.get("_crossref_native_work_proof"),
                expected_doi=selected_doi,
            )
            for source in proof_sources
        ):
            return True
        for attempt in native_attempts:
            if (
                not isinstance(attempt, dict)
                or attempt.get("via") != "europepmc"
                or attempt.get("status") != "resolved"
                or str(attempt.get("work_type") or "").strip().casefold() != "preprint"
            ):
                continue
            native_doi = _normalize_identity_value("doi", attempt.get("doi"))
            native_identifiers = attempt.get("identifiers")
            bound_doi = (
                _normalize_identity_value("doi", native_identifiers.get("doi"))
                if isinstance(native_identifiers, dict)
                else ""
            )
            if (
                native_doi
                and bound_doi
                and native_doi.casefold() == selected_doi
                and bound_doi.casefold() == selected_doi
            ):
                return True
        try:
            from .providers import datacite as datacite_provider
        except ImportError:  # pragma: no cover - direct execution fallback
            from providers import datacite as datacite_provider
        for attempt in native_attempts:
            if (
                not isinstance(attempt, dict)
                or attempt.get("via") != "identifier_validation:datacite"
                or attempt.get("status") != "resolved"
                or attempt.get("identifier_type") != "doi"
            ):
                continue
            attempt_doi = _normalize_identity_value(
                "doi", attempt.get("identifier_value"),
            )
            if not attempt_doi or attempt_doi.casefold() != selected_doi:
                continue
            try:
                record = datacite_provider._native_doi_record_from_proof(
                    attempt.get(datacite_provider.DATACITE_NATIVE_DOI_PROOF_FIELD),
                    expected_doi=selected_doi,
                )
            except (TypeError, ValueError):
                continue
            attributes = record.get("attributes")
            if not isinstance(attributes, dict):
                continue
            types = attributes.get("types")
            if isinstance(types, dict) and any(
                str(types.get(key) or "").strip().casefold() == "preprint"
                for key in ("resourceTypeGeneral", "resourceType")
            ):
                return True
            related_identifiers = attributes.get("relatedIdentifiers")
            if not isinstance(related_identifiers, list):
                continue
            for relation in related_identifiers:
                if not isinstance(relation, dict):
                    continue
                related_doi = relation.get("relatedIdentifier")
                if (
                    str(relation.get("relationType") or "").strip().casefold()
                    == "ispreprintof"
                    and str(
                        relation.get("relatedIdentifierType") or ""
                    ).strip().casefold() == "doi"
                    and isinstance(related_doi, str)
                    and related_doi == related_doi.strip()
                    and datacite_provider._DATACITE_DOI_RE.fullmatch(related_doi)
                    is not None
                    and related_doi.casefold() != selected_doi
                ):
                    return True
    resolved = candidate.get("resolved_identifier")
    direct_dois = [candidate.get("doi")]
    arxiv_identity = False
    for mapping in (resolved, identifiers, candidate.get("identity")):
        if not isinstance(mapping, dict):
            continue
        scheme = str(mapping.get("type") or mapping.get("scheme") or "").strip().lower()
        value = mapping.get("value")
        if scheme == "arxiv_id" and value:
            arxiv_identity = True
        elif scheme == "doi" and value:
            direct_dois.append(value)
        direct_dois.append(mapping.get("doi"))
    resolved_scheme = str((resolved or {}).get("type") or (resolved or {}).get("scheme") or "").strip().lower() if isinstance(resolved, dict) else ""
    if resolved_scheme == "arxiv_id":
        return True
    resolve_mod = _resolve_module()
    normalize_doi = getattr(resolve_mod, "_normalize_doi_value", None)
    doi_values = [
        normalized
        for value in direct_dois
        if (normalized := (normalize_doi(value) if callable(normalize_doi) else str(value or "").strip()))
    ]
    if any(re.match(r"^10\.48550/arxiv\.", value, re.IGNORECASE) for value in doi_values):
        return True
    # A candidate carrying a publisher DOI is a published-work identity even if
    # one of its available files is a preprint. File-version evidence is handled
    # separately by the service's content projection guard.
    if doi_values:
        return False
    if arxiv_identity:
        return True
    candidate_ids = candidate.get("candidate_identifiers")
    if isinstance(candidate_ids, dict):
        candidate_doi_raw = candidate_ids.get("doi")
        candidate_doi = (
            normalize_doi(candidate_doi_raw)
            if callable(normalize_doi)
            else str(candidate_doi_raw or "").strip()
        )
        if re.match(r"^10\.48550/arxiv\.", candidate_doi, re.IGNORECASE):
            return True
    content_version_for = getattr(resolve_mod._sources, "content_version_for", None)
    if callable(content_version_for):
        version = content_version_for(
            None, candidate.get("content_version") or candidate.get("version")
        )
        if version == "preprint":
            return True
        if str(candidate.get("work_type") or "").strip().lower() in {"posted-content", "preprint"}:
            links = candidate.get("fulltext_links") or ()
            typed_preprint = any(
                isinstance(link, dict)
                and content_version_for(
                    link.get("url"), link.get("content_version")
                ) == "preprint"
                for link in links
            )
            if typed_preprint:
                return True
    return False


def _resolution_identity(ref: dict, result: dict | None, status: str) -> Identity:
    resolve_mod = _resolve_module()
    resolved_identifier = resolve_mod._resolved_identifier_summary(result)
    if resolved_identifier:
        return _identity_from_parts(
            resolved_identifier.get("type"),
            resolved_identifier.get("value"),
        )
    discovered_doi = resolve_mod._result_doi(result)
    if discovered_doi and status == "resolved":
        return _identity_from_parts("doi", discovered_doi)
    if status == "resolved" and str((result or {}).get("via") or "") == "arxiv_search":
        arxiv_identity = _arxiv_identity_from_result(result)
        if arxiv_identity.scheme != "none":
            return arxiv_identity
    if _is_published_preprint_candidate(ref, {"doi": ref.get("doi")}):
        # A DOI inferred from a repository annotation is not a DOI declared
        # for the independently source-supported published journal unit.
        # Keep it in the rejected candidate evidence, not as a fetch/cache key.
        return _identity_from_parts("none", None)
    declared = _declared_identity(ref)
    if status in ("resolved", "not_found", "identifier_mismatch") and declared.scheme != "none":
        return declared
    return declared


def _resolution_identity_state(
    ref: dict,
    *,
    status: str,
    reference_status_tag: str | None,
    identity: Identity,
    discovered_identity_validated: bool = False,
    bibliographic_identity_status: str | None = None,
) -> str:
    declared = _declared_identity(ref)
    if status == "resolved":
        if bibliographic_identity_status not in {None, "identified", "identified_with_errors"}:
            return IdentityState.WEAKLY_CORROBORATED.value
        if identity.is_strong:
            if (
                declared.is_strong
                and declared.scheme == identity.scheme
                and declared.value == identity.value
            ):
                return IdentityState.RESOLVED_STRONG_DECLARED.value
            if discovered_identity_validated:
                return IdentityState.RESOLVED_STRONG_DISCOVERED.value
        return IdentityState.WEAKLY_CORROBORATED.value
    if status in ("not_found", "identifier_mismatch"):
        if reference_status_tag == "suspected_fabricated":
            return IdentityState.FABRICATED.value
        return IdentityState.DECLARED_IDENTIFIER_FAILED.value
    if status == "unverified":
        return IdentityState.UNVERIFIED.value
    return IdentityState.UNVERIFIED.value


def _metadata_identity_not_admitted(
    resolution_basis: str | None,
    bibliographic_identity_status: str | None,
) -> bool:
    """Metadata-search candidates that fail identity admission are not fetch keys."""
    return (
        resolution_basis == "metadata_search"
        and bibliographic_identity_status == "not_identified"
    )


def _evidence_payload_from_resolution(meta: dict, *, via: str | None) -> dict:
    payload = EvidencePayload(
        fulltext_links=list(meta.get("fulltext_links") or []),
        auxiliary_fulltext_links=list(meta.get("auxiliary_fulltext_links") or []),
        oa_status=str(meta.get("oa_status") or "unknown"),
        fulltext_exists=meta.get("fulltext_exists", "unknown"),
        abstract=meta.get("abstract"),
        matched_title=meta.get("matched_title"),
        work_type=meta.get("work_type"),
        via=via,
        fulltext_availability=meta.get("fulltext_availability"),
    )
    return payload.to_dict()


def _retraction_payload(result: dict | None, attempts: list[dict]) -> dict:
    current = result or {}
    recovered = current.get("identifier_error_recovered") is True
    evidence_attempt = None if recovered else next(
        (
            attempt for attempt in attempts
            if isinstance(attempt, dict)
            and "retracted" in attempt
            and str(attempt.get("via") or "") in {"crossref", "crossref_metadata", "europepmc", "identifier_validation:crossref"}
        ),
        None,
    )
    if evidence_attempt is not None:
        checked_via = "authority_metadata"
        status = "retracted" if bool(current.get("retracted")) else "not_retracted"
        evidence_retracted = bool(current.get("retracted"))
    elif bool(current.get("retracted")) and not recovered:
        checked_via = "retraction_watch"
        status = "retracted"
        evidence_retracted = True
    else:
        checked_via = "none"
        status = "unknown"
        evidence_retracted = False
    payload = Retraction(
        status=status,
        checked_via=checked_via,
        evidence={
            "retracted": evidence_retracted,
            "resolver_via": current.get("content_via") or current.get("via"),
            "attempt_via": None if evidence_attempt is None else evidence_attempt.get("via"),
        },
    )
    return payload.to_dict()


def _append_stage(trace_rows: list[dict], stage: str, outcome: str, detail: dict, position: int) -> None:
    row = StageRecord(
        stage=stage,
        outcome=outcome,
        detail=detail,
        at=f"stage_{position:02d}",
    )
    trace_rows.append(row.to_dict())


def _resolution_trace(
    ref: dict,
    result: dict | None,
    attempts: list[dict],
    *,
    status: str,
    via: str | None,
    reference_status_tag: str | None,
    fabrication_risk: str | None,
    resolution_basis: str | None,
    identity: Identity,
    identity_state: str,
    retraction: dict,
) -> list[dict]:
    trace_rows: list[dict] = []
    declared = _declared_identity(ref)
    position = 1

    _append_stage(
        trace_rows,
        "declared_present",
        "present" if declared.scheme != "none" else "absent",
        declared.to_dict(),
        position,
    )
    position += 1

    if declared.is_strong:
        _append_stage(
            trace_rows,
            "declared_cache_lookup",
            "not_checked",
            {"reason": "resolver has no declared-identity cache gate"},
            position,
        )
        position += 1

        if status == "resolved" and identity_state == IdentityState.RESOLVED_STRONG_DECLARED.value:
            declared_outcome = "matched"
        elif status in ("not_found", "identifier_mismatch"):
            declared_outcome = "failed"
        else:
            declared_outcome = "inconclusive"
        _append_stage(
            trace_rows,
            "declared_validation",
            declared_outcome,
            {"status": status, "via": via},
            position,
        )
        position += 1

    if identity_state == IdentityState.RESOLVED_STRONG_DISCOVERED.value:
        _append_stage(
            trace_rows,
            "strong_discovery",
            "found",
            identity.to_dict(),
            position,
        )
        position += 1
        _append_stage(
            trace_rows,
            "discovered_validation",
            "matched",
            {"status": status, "via": via},
            position,
        )
        position += 1

    if identity_state in {
        IdentityState.RESOLVED_STRONG_DECLARED.value,
        IdentityState.RESOLVED_STRONG_DISCOVERED.value,
    }:
        _append_stage(
            trace_rows,
            "strong_confirmed",
            "confirmed",
            {"identity_state": identity_state, "resolution_basis": resolution_basis},
            position,
        )
        position += 1
    else:
        _append_stage(
            trace_rows,
            "weak_aggregation",
            identity_state,
            {
                "status": status,
                "reference_status_tag": reference_status_tag,
                "fabrication_risk": fabrication_risk,
            },
            position,
        )
        position += 1

    _append_stage(
        trace_rows,
        "retraction_check",
        str(retraction.get("status") or "unknown"),
        {
            "checked_via": retraction.get("checked_via"),
            "retracted": bool((retraction.get("evidence") or {}).get("retracted")),
        },
        position,
    )
    position += 1

    _append_stage(
        trace_rows,
        "final_resolution",
        identity_state,
        {
            "status": status,
            "via": via,
            "attempt_count": len(attempts),
        },
        position,
    )
    return trace_rows


@dataclass(frozen=True)
class _ResolutionDecision:
    identity: Identity
    identity_state: str
    evidence_payload: dict
    retraction: dict
    trace: list[dict]


def _build_resolution_decision(
    ref: dict,
    result: dict | None,
    attempts: list[dict],
    *,
    status: str,
    via: str | None,
    reference_status_tag: str | None,
    fabrication_risk: str | None,
    resolution_basis: str | None,
    meta: dict,
    bibliographic_identity_status: str | None = None,
    evidence_via: str | None = None,
    withhold_identity: bool = False,
) -> _ResolutionDecision:
    identity = _resolution_identity(ref, result, status)
    metadata_identity_not_admitted = _metadata_identity_not_admitted(
        resolution_basis, bibliographic_identity_status,
    )
    if metadata_identity_not_admitted or withhold_identity:
        identity = _identity_from_parts("none", None)
    resolved_identifier = _resolve_module()._resolved_identifier_summary(result)
    discovered_identity_validated = (
        isinstance(resolved_identifier, dict)
        and all(
            isinstance(resolved_identifier.get(field), str)
            and bool(resolved_identifier[field].strip())
            for field in ("type", "value", "validated_via")
        )
    ) or (
        status == "resolved"
        and str((result or {}).get("via") or "") == "arxiv_search"
        and identity.scheme == "arxiv_id"
    )
    identity_state = (
        IdentityState.UNVERIFIED.value
        if metadata_identity_not_admitted
        else _resolution_identity_state(
            ref,
            status=status,
            reference_status_tag=reference_status_tag,
            identity=identity,
            discovered_identity_validated=discovered_identity_validated,
            bibliographic_identity_status=bibliographic_identity_status,
        )
    )
    retraction = _retraction_payload(result, attempts)
    return _ResolutionDecision(
        identity=identity,
        identity_state=identity_state,
        evidence_payload=_evidence_payload_from_resolution(
            meta, via=evidence_via or via,
        ),
        retraction=retraction,
        trace=_resolution_trace(
            ref,
            result,
            attempts,
            status=status,
            via=via,
            reference_status_tag=reference_status_tag,
            fabrication_risk=fabrication_risk,
            resolution_basis=resolution_basis,
            identity=identity,
            identity_state=identity_state,
            retraction=retraction,
        ),
    )


_PDF_CONTENT_TYPES = {"pdf", "application/pdf"}


def _result_has_pdf(result: dict | None) -> bool:
    """True when a resolver result carries at least one PDF fulltext link.

    Accepts both the Crossref MIME form ("application/pdf") and the short
    OpenAlex form ("pdf").
    """
    if not result:
        return False
    return any(
        link.get("content_type") in _PDF_CONTENT_TYPES
        for link in (result.get("fulltext_links") or [])
    )


def _result_metadata_score(result: dict | None) -> float:
    """Overall metadata score of a resolver result."""
    if not result:
        return 0.0
    score = (result.get("metadata_match") or {}).get("score")
    return score if isinstance(score, (int, float)) else 0.0


def _result_fulltext_is_non_record_only(result: dict | None) -> bool:
    if not result:
        return False
    resolve_mod = _resolve_module()
    non_record_versions = tuple(getattr(resolve_mod._sources, "NON_RECORD_VERSIONS", ()))
    content_version_for = getattr(resolve_mod._sources, "content_version_for", None)
    if not non_record_versions or not callable(content_version_for):
        return False
    pdf_links = [
        link for link in (result.get("fulltext_links") or [])
        if isinstance(link, dict) and link.get("content_type") in _PDF_CONTENT_TYPES
    ]
    if not pdf_links:
        return False
    return all(
        content_version_for(link.get("url"), link.get("content_version")) in non_record_versions
        for link in pdf_links
    )


def _is_exact_official_openai_report_candidate(ref: dict, candidate: dict) -> bool:
    """Return whether a closed OpenAI catalogue route identifies this report.

    The OpenAI catalogue intentionally carries its identity in immutable link
    context rather than generic metadata-match fields.  It can therefore
    displace a prior metadata-search winner only when every catalogue relation
    is present and agrees with the citation.
    """
    if (
        candidate.get("status") != "resolved"
        or candidate.get("via") != "openai_reports_search"
        or candidate.get("resolution_basis") != "canonical_source"
        or candidate.get("existence_confidence") != "high"
    ):
        return False
    metadata_match = candidate.get("metadata_match") or {}
    if (
        candidate.get("metadata_conflict") is True
        or candidate.get("ordinal_conflict") is True
        or metadata_match.get("metadata_conflict") is True
        or metadata_match.get("ordinal_conflict") is True
    ):
        return False
    resolve_mod = _resolve_module()
    cited_title = resolve_mod._article_title_candidate(ref)
    candidate_title = candidate.get("matched_title")
    cited_author = resolve_mod._first_author_key(ref.get("raw_entry"))
    cited_year = ref.get("year")
    if not all((cited_title, candidate_title, cited_author, cited_year is not None)):
        return False
    title_key = resolve_mod._title_key
    cited_title_key = title_key(cited_title)
    if not cited_title_key or cited_title_key != title_key(candidate_title):
        return False
    links = candidate.get("fulltext_links") or []
    if len(links) != 1:
        return False
    for link in links:
        if not isinstance(link, dict) or link.get("content_type") not in _PDF_CONTENT_TYPES:
            continue
        context = link.get("identity_context") or {}
        if not isinstance(context, dict):
            continue
        catalogue_title = context.get("title")
        canonical_url = context.get("canonical_url")
        landing_page_url = context.get("landing_page_url")
        if (
            context.get("provider") != "openai_reports"
            or context.get("official") is not True
            or context.get("canonical_host") is not True
            or context.get("official_document_relation")
            != "official_landing_page_links_exact_document"
            or not canonical_url
            or canonical_url != link.get("url")
            or not landing_page_url
            or landing_page_url == canonical_url
            or cited_title_key != title_key(catalogue_title)
            or str(context.get("first_author") or "").casefold()
            != str(cited_author).casefold()
            or str(context.get("year")) != str(cited_year)
        ):
            continue
        return True
    return False


def _is_authoritative_acl_short_title_candidate(ref: dict, candidate: dict) -> bool:
    """Whether ACL's closed catalogue proves a cited title-prefix identity."""
    if (
        candidate.get("status") != "resolved"
        or candidate.get("via") != "acl_search"
        or candidate.get("resolution_basis") != "metadata_search"
        or candidate.get("existence_confidence") != "high"
        or candidate.get("identity_basis") != "authoritative_acl_short_title_prefix"
    ):
        return False
    metadata_match = candidate.get("metadata_match") or {}
    if (
        metadata_match.get("author_match") is not True
        or metadata_match.get("year_match") is not True
        or metadata_match.get("metadata_conflict") is True
        or metadata_match.get("ordinal_conflict") is True
    ):
        return False
    resolve_mod = _resolve_module()
    cited_key = resolve_mod._title_key(resolve_mod._article_title_candidate(ref))
    candidate_key = resolve_mod._title_key(candidate.get("matched_title"))
    if not cited_key or not candidate_key.startswith(f"{cited_key} "):
        return False
    paper_id = str(candidate.get("paper_id") or "")
    if not paper_id:
        return False
    canonical_pdf = f"https://aclanthology.org/{paper_id}.pdf"
    return any(
        isinstance(link, dict)
        and link.get("url") == canonical_pdf
        and link.get("content_type") in _PDF_CONTENT_TYPES
        for link in candidate.get("fulltext_links") or ()
    )


def _prefer_metadata_candidate(
    ref: dict,
    current: dict | None,
    candidate: dict,
    attempts: list[dict] | None = None,
) -> dict | None:
    if candidate.get("status") != "resolved":
        return current
    if (candidate.get("metadata_match") or {}).get("ordinal_conflict"):
        return None if _is_published_preprint_candidate(ref, current, attempts) else current
    if (candidate.get("metadata_match") or {}).get("metadata_conflict"):
        return None if _is_published_preprint_candidate(ref, current, attempts) else current
    if _is_published_preprint_candidate(ref, candidate, attempts):
        return None if _is_published_preprint_candidate(ref, current, attempts) else current
    if _is_published_preprint_candidate(ref, current, attempts):
        return candidate
    if (
        current is not None
        and current.get("status") != "resolved"
        and current.get("metadata_conflict") is True
    ):
        resolve_mod = _resolve_module()
        candidate_mm = candidate.get("metadata_match") or {}
        cited_title = resolve_mod._article_title_candidate(ref)
        candidate_exact_title = (
            resolve_mod._title_key(cited_title)
            == resolve_mod._title_key(candidate.get("matched_title"))
        )
        candidate_contains_cited_title = resolve_mod._title_key_contains(
            candidate.get("matched_title"), cited_title
        )
        candidate_title_faithful = (
            candidate_exact_title
            or (
                candidate_contains_cited_title
                and candidate_mm.get("author_match") is True
            )
        )
        candidate_identity = (
            candidate_mm.get("author_match") is True
            and (
                candidate_mm.get("year_match") is True
                or (candidate_mm.get("venue_overlap") or 0.0) >= 0.50
                or resolve_mod._metadata_has_canonical_host(candidate)
            )
        )
        return candidate if candidate_title_faithful and candidate_identity else current
    if current is None or current.get("status") != "resolved":
        return candidate
    resolve_mod = _resolve_module()
    if (
        current.get("resolution_basis") == "metadata_search"
        and _is_exact_official_openai_report_candidate(ref, candidate)
    ):
        return candidate
    if (
        current.get("resolution_basis") == "metadata_search"
        and _is_authoritative_acl_short_title_candidate(ref, candidate)
    ):
        return candidate
    current_overlap = resolve_mod._result_title_overlap(current)
    candidate_overlap = resolve_mod._result_title_overlap(candidate)
    current_score = _result_metadata_score(current)
    candidate_score = _result_metadata_score(candidate)
    current_has_pdf = _result_has_pdf(current)
    candidate_has_pdf = _result_has_pdf(candidate)
    current_non_record_only = _result_fulltext_is_non_record_only(current)
    candidate_non_record_only = _result_fulltext_is_non_record_only(candidate)
    current_mm = current.get("metadata_match") or {}
    candidate_mm = candidate.get("metadata_match") or {}
    if current_mm.get("metadata_conflict") and not candidate_mm.get("metadata_conflict"):
        return candidate
    if current_mm.get("ordinal_conflict") and not candidate_mm.get("ordinal_conflict"):
        return candidate
    cited_title = resolve_mod._article_title_candidate(ref)
    current_exact_title = resolve_mod._title_key(cited_title) == resolve_mod._title_key(current.get("matched_title"))
    candidate_exact_title = resolve_mod._title_key(cited_title) == resolve_mod._title_key(candidate.get("matched_title"))
    current_contains_cited_title = resolve_mod._title_key_contains(current.get("matched_title"), cited_title)
    candidate_contains_cited_title = resolve_mod._title_key_contains(candidate.get("matched_title"), cited_title)
    current_title_faithful = (
        current_exact_title
        or (
            current_contains_cited_title
            and current_mm.get("author_match") is True
        )
    )
    candidate_title_faithful = (
        candidate_exact_title
        or (
            candidate_contains_cited_title
            and candidate_mm.get("author_match") is True
        )
    )
    current_identity = (
        current_mm.get("author_match") is True
        and (
            current_mm.get("year_match") is True
            or (current_mm.get("venue_overlap") or 0.0) >= 0.50
            or resolve_mod._metadata_has_canonical_host(current)
        )
    )
    candidate_identity = (
        candidate_mm.get("author_match") is True
        and (
            candidate_mm.get("year_match") is True
            or (candidate_mm.get("venue_overlap") or 0.0) >= 0.50
            or resolve_mod._metadata_has_canonical_host(candidate)
        )
    )
    if current_exact_title and candidate_exact_title:
        if current_identity and not candidate_identity:
            return current
        if candidate_identity and not current_identity:
            return candidate
    if current_exact_title and current_identity and not candidate_exact_title:
        return current
    if candidate_exact_title and candidate_identity and not current_exact_title:
        return candidate
    if (
        candidate_has_pdf
        and candidate_title_faithful
        and candidate_identity
        and not candidate_non_record_only
        and current_non_record_only
    ):
        return candidate
    if (
        current_has_pdf
        and current_title_faithful
        and current_identity
        and not current_non_record_only
        and candidate_non_record_only
    ):
        return current
    if candidate_has_pdf and not current_has_pdf:
        if (
            candidate_overlap >= 0.80
            and candidate_score >= 0.80
            and candidate_overlap >= current_overlap - 0.05
            and candidate_score >= current_score - 0.05
        ):
            return candidate
    if candidate_overlap > current_overlap + 0.10:
        return candidate
    if (
        current_overlap < 0.40
        and candidate_overlap > 0.60
        and candidate_overlap - current_overlap >= 0.30
    ):
        return candidate
    if candidate_identity and candidate_score >= 0.70:
        if (not current_identity) or current_score < 0.55:
            return candidate
    if current_title_faithful and current_identity and not candidate_title_faithful:
        return current
    if candidate_title_faithful and candidate_identity and not current_title_faithful:
        return candidate
    return current
