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
    from .resolve_types import EvidencePayload, Identity, IdentityState, Retraction, StageRecord
    from .matching import _anchored_metadata_author_conflict
except ImportError:  # direct execution fallback
    from resolve.resolve_types import EvidencePayload, Identity, IdentityState, Retraction, StageRecord
    from resolve.matching import _anchored_metadata_author_conflict


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
    return None


def _withhold_discovered_doi_promotion(
    result: dict,
    doi: str,
    *,
    relation: str,
    reason: str,
) -> dict:
    """Keep a validated DOI auditable without admitting it as the citation."""
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
    guarded["candidate_identifiers"] = {"doi": doi}
    guarded["reason"] = (
        f"DOI exists but citation-unit relation is {relation}; "
        f"identifier promotion withheld: {reason}"
    )
    return guarded


def _promote_validated_identifier(ref: dict, result: dict | None, attempts: list[dict]) -> dict | None:
    """Upgrade a metadata-only resolve to a terminal strong-identifier resolve.

    A DOI discovered during metadata search becomes terminal only after a direct,
    deterministic DOI lookup confirms it. This freezes the work identity while
    leaving fetch/copy selection to the fetch layer.
    """
    if not isinstance(result, dict) or result.get("status") != "resolved":
        return result
    if result.get("resolution_basis") != "metadata_search":
        return result
    resolve_mod = _resolve_module()
    hard_conflicts = _actionable_doi_metadata_conflicts(ref, result)
    if len(hard_conflicts) >= 2:
        guarded = dict(result)
        doi = resolve_mod._result_doi(result)
        metadata_match = dict(result.get("metadata_match") or {})
        metadata_match.update({
            "author_conflict": "author" in hard_conflicts,
            "year_conflict": "year" in hard_conflicts,
            "venue_conflict": "venue" in hard_conflicts,
            "hard_conflicts": list(hard_conflicts),
            "metadata_conflict": True,
        })
        guarded["metadata_match"] = metadata_match
        guarded["status"] = "unverified"
        guarded["existence_confidence"] = "low"
        guarded["metadata_conflict"] = True
        if doi:
            guarded["candidate_identifiers"] = {"doi": doi}
        guarded["reason"] = (
            "metadata candidate has multiple hard identity conflicts; "
            "identifier promotion withheld"
        )
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
    if resolve_mod._result_title_overlap(result) < 0.85:
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
        validation = resolve_mod._crossref_resolver_module().resolve_doi(doi)
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
            if relation is not None:
                relation_state, relation_reason = relation
                return _withhold_discovered_doi_promotion(
                    result,
                    doi,
                    relation=relation_state,
                    reason=relation_reason,
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
                "validated_via": "crossref",
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
    return result


_GLOBAL_IDENTITY_SCHEMES = {"doi", "pmid", "isbn", "arxiv_id", "pmcid"}
_AUTHORITY_LOCAL_IDENTITY_SCHEMES = {"acl_id", "ssrn_abstract_id", "url"}
_STRONG_IDENTITY_SCHEMES = _GLOBAL_IDENTITY_SCHEMES | {"acl_id", "ssrn_abstract_id"}


def _normalize_identity_value(scheme: str, value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    scheme = str(scheme or "").strip().lower()
    if scheme == "doi":
        return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw, flags=re.IGNORECASE).strip()
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
) -> _ResolutionDecision:
    identity = _resolution_identity(ref, result, status)
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
    identity_state = _resolution_identity_state(
        ref,
        status=status,
        reference_status_tag=reference_status_tag,
        identity=identity,
        discovered_identity_validated=discovered_identity_validated,
        bibliographic_identity_status=bibliographic_identity_status,
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


def _prefer_metadata_candidate(ref: dict, current: dict | None, candidate: dict) -> dict | None:
    if candidate.get("status") != "resolved":
        return current
    if (candidate.get("metadata_match") or {}).get("ordinal_conflict"):
        return current
    if (candidate.get("metadata_match") or {}).get("metadata_conflict"):
        return current
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
