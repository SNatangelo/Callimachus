# core/verify/identity_gate.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic bibliographic-identity guard for verifier outcomes."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from core.resolve import sources as _sources
from core.resolve.matching import _title_match_score
from core.resolve.providers.crossref_metadata import _titles_exactly_equivalent


_IDENTITY_ENTRY_SIGNALS = frozenset({"doi", "pmid", "title", "identifier"})
_IDENTITY_CONFIRMED_STATUSES = frozenset({
    "exact_identifier",
    "corroborated_bibliography",
    "exact_arxiv_id_confirmed",
    "exact_acl_id_confirmed",
    "exact_author_copy_confirmed",
    "exact_official_curated_document_confirmed",
    "externally_corroborated_text",
})
_EXACT_FULLTEXT_ADMISSION_STATUSES = frozenset({
    "doi_anchored_resolved_candidate_confirmed",
    "exact_arxiv_id_confirmed",
    "exact_acl_id_confirmed",
    "exact_author_copy_confirmed",
    "exact_official_curated_document_confirmed",
    "trusted_springer_jats_front_confirmed",
})
_DOI_IN_TEXT_RE = re.compile(r"\b10\.\d{4,9}/\S+", re.IGNORECASE)
_ABSTRACT_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:(?:graphical|structured|visual)[ \t]+)?"
    r"abstract[ \t]*[:.]?[ \t]*$"
)
_BODY_SECTION_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:(?:\d+(?:\.\d+)*)|[ivxlcdm]+)?[.)]?[ \t]*"
    r"(?:introduction|materials[ \t]+and[ \t]+methods|methods?|results?|"
    r"discussion|conclusions?|references|bibliography)[ \t]*[:.]?[ \t]*$"
)


def _normalised_doi(value: object) -> str | None:
    """Return a comparable DOI value without accepting arbitrary identifiers."""
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi:\s*", "", text)
    return text if text.startswith("10.") else None


def _observed_doi_matches(expected: str, observed: object) -> bool:
    """Compare text DOI punctuation without deleting identifier characters."""
    candidate = _normalised_doi(observed)
    if not candidate:
        return False
    if candidate == expected:
        return True
    candidate = candidate.rstrip(".,;")
    while candidate and candidate[-1] in ")]}":
        closing = candidate[-1]
        opening = {")": "(", "]": "[", "}": "{"}[closing]
        if candidate.count(closing) <= candidate.count(opening):
            break
        candidate = candidate[:-1]
    return candidate == expected


def _validated_resolved_doi_in_front_matter(resolve: dict, source_text: str) -> bool:
    """Require Resolve's validated DOI in the document's native front matter."""
    identifier = resolve.get("resolved_identifier")
    if (
        str(resolve.get("status") or "").strip().lower() != "resolved"
        or not isinstance(identifier, dict)
        or str(identifier.get("type") or "").strip().lower() != "doi"
        or not str(identifier.get("validated_via") or "").strip()
    ):
        return False
    expected = _normalised_doi(identifier.get("value"))
    if not expected:
        return False

    head = (source_text or "")[:_sources.DOCUMENT_IDENTITY_HEAD_CHARS]
    boundary = _ABSTRACT_HEADING_RE.search(head)
    # Without an actual Abstract heading, this text region can include body
    # prose.  An ordinary use of the word "abstract" is not a boundary.
    if boundary is None:
        return False
    front_matter = head[:boundary.start()]
    for match in _DOI_IN_TEXT_RE.finditer(front_matter):
        if not _observed_doi_matches(expected, match.group(0)):
            continue
        # A DOI after a major body heading is a citation, not native
        # front-matter identity evidence, even if a later Abstract heading is
        # present in the extraction.
        if _BODY_SECTION_HEADING_RE.search(front_matter[:match.start()]):
            return False
        return True
    return False


def _validated_resolved_doi_identity_admitted(
    reference: dict, source_text: str, resolve: dict,
) -> bool:
    """Admit a resolved DOI only when independent citation evidence confirms it."""
    if not _validated_resolved_doi_in_front_matter(resolve, source_text):
        return False
    try:
        probe = _sources.document_identity_probe(reference, source_text, resolve)
    except (AttributeError, TypeError, ValueError):
        return False
    if not isinstance(probe, dict):
        return False
    return (
        str(probe.get("decision") or "").strip().lower() == "confirmed"
        and probe.get("author_ok") is True
        and str(probe.get("expected_title_source") or "").startswith("citation")
    )


def _citation_owned_expected_title_matches_resolve(
    reference: dict, resolve: dict, resolved_title: str, *,
    require_usable: bool = False,
) -> bool:
    """Match Resolve only when the existing selector finds that title in the citation."""
    selector = getattr(_sources, "_expected_probe_title_info", None)
    if not callable(selector):
        return False
    try:
        citation_title, title_source = selector(reference, resolve)
    except (AttributeError, TypeError, ValueError):
        return False
    if title_source not in {"citation", "citation_raw_entry", "citation_quoted"}:
        return False
    citation_title = str(citation_title or "").strip()
    resolved_title = str(resolved_title or "").strip()
    if not citation_title or not resolved_title:
        return False
    if require_usable:
        usable = getattr(_sources, "probe_title_is_usable", None)
        if not callable(usable) or not usable(citation_title):
            return False
    title_key = getattr(_sources, "_title_key", None)
    if callable(title_key):
        return (
            title_key(citation_title) == title_key(resolved_title)
            or _titles_exactly_equivalent(citation_title, resolved_title)
        )
    return _titles_exactly_equivalent(citation_title, resolved_title)


def _bibliographic_identity_corroborated(
    task: dict,
    source_text: str,
    *,
    allow_citation_owned_title_alignment: bool = False,
) -> bool:
    """Return true only when three independent identity observations agree.

    The source entry, resolver metadata, and document front matter must all
    identify the cited work. Missing or conflicting fields fail closed.
    Identity never implies semantic support for the manuscript proposition.
    """
    ref = task.get("reference") or {}
    evidence = task.get("source_identity_evidence") or {}
    resolve = evidence.get("resolve") or {}
    if not isinstance(resolve, dict):
        return False

    entry_status = str(evidence.get("identity_status") or "").strip().lower()
    entry_signal = str(evidence.get("match_signal") or "").strip().lower()
    try:
        entry_score = float(evidence.get("match_score"))
    except (TypeError, ValueError):
        entry_score = 0.0
    entry_ok = (
        entry_status in _IDENTITY_CONFIRMED_STATUSES
        or entry_signal in _IDENTITY_ENTRY_SIGNALS and entry_score >= 0.95
    )
    if not entry_ok:
        return False

    reference_title = str(ref.get("title") or "").strip()
    resolved_title = str(resolve.get("matched_title") or "").strip()
    if (
        not reference_title
        or not resolved_title
        or str(resolve.get("status") or "").strip().lower() != "resolved"
    ):
        return False
    title_key = getattr(_sources, "_title_key", None)
    if callable(title_key):
        metadata_ok = (
            title_key(reference_title) == title_key(resolved_title)
            or _titles_exactly_equivalent(reference_title, resolved_title)
        )
    else:
        normalise = lambda value: " ".join(  # noqa: E731
            re.findall(r"[a-z0-9]+", value.lower()))
        metadata_ok = normalise(reference_title) == normalise(resolved_title)
    if (
        not metadata_ok
        and allow_citation_owned_title_alignment
        and _citation_owned_expected_title_matches_resolve(
            ref, resolve, resolved_title,
        )
    ):
        metadata_ok = True
    if not metadata_ok:
        return False

    try:
        probe = _sources.document_identity_probe(ref, source_text, resolve)
    except (AttributeError, TypeError, ValueError):
        return False
    if not isinstance(probe, dict) or probe.get("decision") != "confirmed":
        return False
    # A one-token title is not an independent document-identity observation:
    # ``document_identity_probe`` deliberately returns a compatibility result
    # for it because there is no meaningful title phrase to locate.  It must
    # never be enough to corroborate an identity-only off_topic downgrade.
    return probe.get("reason_code") != "identity_confirmed_no_expected_title"


def bibliographic_identity_corroborated(task: dict, source_text: str) -> bool:
    """Return the strict three-way identity proof used for off-topic gating."""
    return _bibliographic_identity_corroborated(task, source_text)


def _explicit_cited_http_url(reference: dict) -> bool:
    """Return whether the citation contains an explicit non-DOI web URL."""
    value = reference.get("url")
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        # Accessing .port validates malformed bracketed hosts and ports.
        parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and host
        and host not in {"doi.org", "dx.doi.org"}
    )


def _cited_url_identity_admitted(task: dict, source_text: str) -> bool:
    """Admit only Fetch-persisted evidence for the exact explicitly cited URL."""
    reference = task.get("reference")
    evidence = task.get("source_identity_evidence")
    if not isinstance(reference, dict) or not isinstance(evidence, dict):
        return False
    if (
        str(evidence.get("mapping") or "").strip() != "cited_url"
        or str(evidence.get("match_signal") or "").strip() != "url"
        or not _explicit_cited_http_url(reference)
    ):
        return False
    try:
        score = float(evidence.get("match_score"))
    except (TypeError, ValueError):
        return False
    if score != 1.0:
        return False
    return source_identity_attestation_block_reason(task, source_text) is None


def _resolve_has_hard_identity_conflict(resolve: dict) -> bool:
    """Return whether Resolve recorded an identity conflict Fetch treats as fatal."""
    profiles = [resolve.get("metadata_match") or {}]
    evidence = resolve.get("evidence_profile") or {}
    profiles += [
        evidence.get("metadata_match") or {},
        (evidence.get("best_candidate") or {}).get("metadata_match") or {},
    ]
    for profile in profiles:
        flags = {str(x).lower() for x in (profile.get("hard_conflicts") or [])}
        explicit = any(
            profile.get(key)
            for key in (
                "metadata_conflict", "author_conflict", "venue_conflict",
            )
        )
        dimensions = {
            name for name in ("author", "year", "venue")
            if name in flags or profile.get(f"{name}_conflict")
        }
        if (
            explicit
            or flags.intersection({"author", "venue"})
            or len(dimensions) >= 2
        ):
            return True
    return bool(resolve.get("identity_conflict") or resolve.get("metadata_conflict"))


def source_identity_attestation_block_reason(task: dict, source_text: str) -> str | None:
    """Return a stable hard-deny reason for an operator identity attestation."""
    if not isinstance(task, dict) or not isinstance(task.get("reference"), dict):
        return "invalid_attestation_target"
    evidence = task.get("source_identity_evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("resolve"), dict):
        return "missing_resolve_identity"
    resolve = evidence["resolve"]
    status = str(resolve.get("status") or "").strip().lower()
    if status in {"identifier_mismatch", "fabricated"}:
        return "identifier_or_fabrication_conflict"
    if resolve.get("retracted"):
        return "retracted_source"
    status_tag = str(resolve.get("reference_status_tag") or "").strip().lower()
    fabrication_risk = str(resolve.get("fabrication_risk") or "").strip().lower()
    if status_tag == "suspected_fabricated" or fabrication_risk == "high":
        return "fabrication_risk"
    if _resolve_has_hard_identity_conflict(resolve):
        return "identity_conflict"
    if resolve.get("identity_context_conflict"):
        return "identity_context_conflict"
    relation = evidence.get("provenance_relation")
    allowed_relation = getattr(
        _sources, "OFFICIALLY_SURFACED_COPY", "officially_surfaced_copy",
    )
    if relation not in (None, allowed_relation):
        return "unsupported_source_relation"
    # Resolve abstracts have no document front matter.  Their independent
    # bibliographic proof is checked by the tier-specific admission predicate;
    # retaining this common hard-denial check keeps operator attestations from
    # overriding conflicts, retractions, or suspect provenance.
    if task.get("source_tier") == "abstract":
        _, identifier_conflict = _resolved_abstract_identifier_matches_reference(
            task["reference"], resolve,
        )
        if identifier_conflict:
            return "identifier_conflict"
        return None
    try:
        probe = _sources.document_identity_probe(task["reference"], source_text, resolve)
    except (AttributeError, TypeError, ValueError):
        return "document_identity_probe_invalid"
    if (
        not isinstance(probe, dict)
        or str(probe.get("decision") or "").strip().lower()
        not in {"confirmed", "inconclusive"}
    ):
        return "document_identity_conflict"
    return None


def _normalised_pmid(value: object) -> str | None:
    text = re.sub(r"(?i)^pmid\s*:\s*", "", str(value or "").strip())
    if not re.fullmatch(r"\d+", text):
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    return str(number) if number > 0 else None


def _normalised_abstract_doi(value: object) -> str | None:
    normalized = _normalised_doi(value)
    if normalized and re.fullmatch(r"10\.\d{4,9}/\S+", normalized):
        return normalized
    return None


def _resolved_abstract_identifier_matches_reference(
    reference: dict, resolve: dict,
) -> tuple[bool, bool]:
    """Return (corroborated, conflicting) for current resolver identifiers."""
    identifier = resolve.get("resolved_identifier")
    corroborated = False
    conflicting = False
    if isinstance(identifier, dict):
        kind = str(identifier.get("type") or "").strip().lower()
        validated = bool(str(identifier.get("validated_via") or "").strip())
        if kind == "doi":
            resolved = _normalised_abstract_doi(identifier.get("value"))
            declared = _normalised_abstract_doi(reference.get("doi"))
            if resolved and declared:
                conflicting = resolved != declared
                corroborated = resolved == declared and validated
        elif kind == "pmid":
            resolved = _normalised_pmid(identifier.get("value"))
            declared = _normalised_pmid(reference.get("pmid"))
            if resolved and declared:
                conflicting = resolved != declared
                corroborated = resolved == declared and validated

    for kind, resolve_key, reference_key, normalise in (
        ("doi", "doi", "doi", _normalised_abstract_doi),
        ("pmid", "pmid", "pmid", _normalised_pmid),
    ):
        resolved = normalise(resolve.get(resolve_key))
        declared = normalise(reference.get(reference_key))
        if resolved and declared and resolved != declared:
            conflicting = True
    return corroborated, conflicting


def _citation_title_exactly_matches_resolve(
    reference: dict, resolved_title: str,
) -> bool:
    """Require the parsed citation title itself to match Resolve exactly."""
    citation_title = str(reference.get("title") or "").strip()
    resolved_title = str(resolved_title or "").strip()
    if not citation_title or not resolved_title:
        return False
    usable = getattr(_sources, "probe_title_is_usable", None)
    if not callable(usable) or not usable(citation_title):
        return False
    # Word-only keys discard scientific operators (CD4+ versus CD4-).
    # Keep Resolve's symbol-aware rejection before allowing typography aliases.
    if _title_match_score(
        _sources._identity_normalized_text(citation_title),
        _sources._identity_normalized_text(resolved_title),
    ) == 0.0:
        return False
    title_key = getattr(_sources, "_title_key", None)
    if callable(title_key):
        return (
            title_key(citation_title) == title_key(resolved_title)
            or _titles_exactly_equivalent(citation_title, resolved_title)
        )
    return _titles_exactly_equivalent(citation_title, resolved_title)


def bibliographic_abstract_identity_admitted(
    task: dict, source_text: str, *, identity_attested: bool = False,
) -> bool:
    """Admit abstract text only on independent identity proof or review."""
    if not isinstance(task, dict) or task.get("source_tier") != "abstract":
        return False
    evidence = task.get("source_identity_evidence")
    reference = task.get("reference")
    if not isinstance(evidence, dict) or not isinstance(reference, dict):
        return False
    resolve = evidence.get("resolve")
    if not isinstance(resolve, dict):
        return False
    if source_identity_attestation_block_reason(task, source_text) is not None:
        return False
    identifier_match, identifier_conflict = (
        _resolved_abstract_identifier_matches_reference(reference, resolve)
    )
    if identifier_conflict:
        return False
    if identity_attested:
        # Callers can set this only after re-reading and validating the
        # hash-bound operator decision against the current repository target.
        return True

    try:
        operator_score = float(evidence.get("match_score"))
    except (TypeError, ValueError):
        operator_score = 0.0
    if (
        str(evidence.get("identity_status") or "").strip().lower()
        == "operator_attested"
        and str(evidence.get("mapping") or "").strip() == "operator_attested"
        and str(evidence.get("match_signal") or "").strip()
        == "operator_confirmation"
        and operator_score == 1.0
        and evidence.get("supplied_by") == "user"
        and str(evidence.get("supplied_via") or "").startswith(
            "controlled_task_answer:"
        )
    ):
        return True

    if (
        str(evidence.get("identity_status") or "").strip().lower()
        == "cited_url_reachable"
    ):
        return _cited_url_identity_admitted(task, source_text)

    if str(resolve.get("status") or "").strip().lower() != "resolved":
        return False
    abstract = resolve.get("abstract")
    if not isinstance(abstract, str) or not abstract or source_text != abstract:
        return False

    if identifier_match:
        return True

    resolved_title = str(resolve.get("matched_title") or "").strip()
    return _citation_title_exactly_matches_resolve(reference, resolved_title)


def _document_identity_shortcut_accepted(
    reference: dict, source_text: str, resolve: dict
) -> bool:
    """Return whether a document probe permits the exact-route shortcut."""
    try:
        probe = _sources.document_identity_probe(reference, source_text, resolve)
    except (AttributeError, TypeError, ValueError):
        return False
    if not isinstance(probe, dict):
        return False
    return str(probe.get("decision") or "").lower() in {"confirmed", "inconclusive"}


def bibliographic_identity_admitted(
    task: dict, source_text: str, *, identity_attested: bool = False,
) -> bool:
    """Admit full text when corroborated or Fetch authenticated its exact route.

    This deliberately differs from :func:`bibliographic_identity_corroborated`.
    The latter remains the stricter three-observation predicate used to guard a
    semantic ``off_topic`` correction.  Full-text verification may also use an
    exact Fetch identity route, unless Resolve or the document probe rejects it.
    """
    if not isinstance(task, dict):
        return False
    evidence = task.get("source_identity_evidence")
    reference = task.get("reference")
    if not isinstance(evidence, dict) or not isinstance(reference, dict):
        return False
    resolve = evidence.get("resolve")
    if not isinstance(resolve, dict):
        return False
    try:
        operator_attested_score = float(evidence.get("match_score"))
    except (TypeError, ValueError):
        operator_attested_score = 0.0
    if (
        str(evidence.get("identity_status") or "").strip().lower() == "operator_attested"
        and str(evidence.get("mapping") or "").strip() == "operator_attested"
        and str(evidence.get("match_signal") or "").strip() == "operator_confirmation"
        and operator_attested_score == 1.0
        and evidence.get("supplied_by") == "user"
        and str(evidence.get("supplied_via") or "").startswith("controlled_task_answer:")
    ):
        return True
    if _resolve_has_hard_identity_conflict(resolve):
        return False
    if identity_attested:
        return source_identity_attestation_block_reason(task, source_text) is None
    entry_status = str(evidence.get("identity_status") or "").strip().lower()
    if entry_status == "cited_url_reachable":
        return _cited_url_identity_admitted(task, source_text)
    if _bibliographic_identity_corroborated(
        task,
        source_text,
        allow_citation_owned_title_alignment=True,
    ):
        return True
    if _validated_resolved_doi_identity_admitted(reference, source_text, resolve):
        return True

    entry_signal = str(evidence.get("match_signal") or "").strip().lower()
    try:
        entry_score = float(evidence.get("match_score"))
    except (TypeError, ValueError):
        entry_score = 0.0
    exact_route = entry_status in _EXACT_FULLTEXT_ADMISSION_STATUSES or (
        entry_status == "exact_identifier"
        and entry_signal in {"doi", "pmid"}
        and entry_score >= 0.95
    )
    if not exact_route:
        return False
    return _document_identity_shortcut_accepted(reference, source_text, resolve)


def apply_off_topic_gate(
    enriched: dict,
    task: dict,
    outcome: str | None,
    source_text: str,
) -> bool:
    """Withhold identity-only ``off_topic``; return whether the gate fired."""
    if not enriched.get("accepted") or outcome != "off_topic":
        return False
    if not bibliographic_identity_corroborated(task, source_text):
        return False
    enriched["accepted"] = False
    enriched["outcome"] = "uncertain"
    enriched["guard_code"] = "identity_corroborated_off_topic"
    enriched["guard_reason"] = (
        "off_topic withheld: entry, resolver metadata, and document front matter "
        "corroborate the cited bibliographic identity; semantic relevance remains uncertain"
    )
    enriched["terminal_cause"] = "identity_corroborated_off_topic"
    task["terminal_cause"] = "identity_corroborated_off_topic"
    return True
