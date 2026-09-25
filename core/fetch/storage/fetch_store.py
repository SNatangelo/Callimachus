#!/usr/bin/env python3
# core/fetch/storage/fetch_store.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Storage and corroboration helpers for deterministic source fetches."""

from __future__ import annotations

import re
import urllib.parse
import unicodedata

try:
    from core.resolve import sources as _sources
except ImportError:
    import sources as _sources

try:
    from core.parse import source_text as _source_text
except ImportError:
    import source_text as _source_text

try:
    from core.fetch.storage import content_store as _content_store
except ImportError:
    from storage import content_store as _content_store

try:
    from core.fetch.extraction.document_relation import document_relation_probe
    from core.fetch.extraction.pdf import _quality as _prepared_text_quality
except ImportError:
    from extraction.document_relation import document_relation_probe  # type: ignore
    from extraction.pdf import _quality as _prepared_text_quality  # type: ignore


_STORAGE_PROVENANCE_FIELDS = frozenset({
    "mapping",
    "supplied_by",
    "supplied_via",
    "file_format",
    "library_item_id",
})
_DOI_IN_TEXT_RE = re.compile(r"\b10\.\d{4,9}/\S+", re.IGNORECASE)


def _validated_storage_provenance(value: dict | None) -> dict:
    """Validate storage-only annotations without admitting identity overrides."""
    if value is None:
        return {}
    if type(value) is not dict:
        raise ValueError("storage_provenance must be a dict or None")
    unknown = set(value) - _STORAGE_PROVENANCE_FIELDS
    if unknown:
        raise ValueError(
            "unsupported storage_provenance fields: " + ", ".join(sorted(unknown))
        )
    normalized = {}
    for key, raw in value.items():
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            raise ValueError(f"storage_provenance.{key} must be nonempty NUL-free text")
        normalized[key] = raw.strip()
    return normalized


def _store_prepared_fulltext(
    run_dir: str,
    ref: dict,
    origin: str,
    text: str,
    *,
    preparation: dict,
    extraction_flags: list[str] | None,
    extraction_method: str | None,
    storage_provenance: dict | None = None,
    **kwargs,
) -> dict:
    """Store verifier text and upgrade its cache row with preparation provenance.

    ``sources.store_text`` retains the established run-local manifest contract.
    The content store has the additional cross-run preparation contract, so it is
    deliberately updated immediately afterwards with the exact same text.
    """
    stored_kwargs = dict(kwargs)
    stored_kwargs.update(storage_provenance or {})
    entry = _sources.store_text(
        run_dir, ref, "fulltext", origin, text,
        extraction_flags=extraction_flags,
        extraction_method=extraction_method,
        **stored_kwargs,
    )
    _content_store.record_parsed_text(
        run_dir,
        ref,
        "fulltext",
        origin,
        text,
        source_ref=stored_kwargs.get("source_ref"),
        mapping=stored_kwargs.get("mapping"),
        signal=stored_kwargs.get("signal"),
        score=stored_kwargs.get("score"),
        identity_status=stored_kwargs.get("identity_status"),
        identity_note=stored_kwargs.get("identity_note"),
        content_version=stored_kwargs.get("content_version"),
        provenance_relation=stored_kwargs.get("provenance_relation"),
        supplied_by=stored_kwargs.get("supplied_by"),
        supplied_via=stored_kwargs.get("supplied_via"),
        file_format=stored_kwargs.get("file_format"),
        library_item_id=stored_kwargs.get("library_item_id"),
        extraction_flags=entry.get("extraction_flags") or extraction_flags,
        extraction_method=extraction_method,
        preparation=preparation,
    )
    return entry


def corroborated(sig: str | None, score: float) -> bool:
    return sig in ("doi", "pmid") or score >= _sources.CORROBORATE_THRESHOLD


def is_doi_url(url: str | None) -> bool:
    if not url:
        return False
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    return host in {"doi.org", "dx.doi.org"}


def is_explicit_cited_url(ref: dict, method: str) -> bool:
    url = ref.get("url")
    return method == "reference_url" and bool(url) and not is_doi_url(url)


def _canonical_cited_route(url: str | None) -> tuple[str, str, str, str] | None:
    """Normalize a URL route without turning a redirect into a host match."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = urllib.parse.unquote(parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urllib.parse.urlencode(
        sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)), doseq=True
    )
    return parsed.scheme.lower(), netloc, path, query


def _explicit_cited_route_matches(ref: dict, source_ref: str) -> bool:
    """The web-citation exception is route-scoped, never merely host-scoped."""
    cited = _canonical_cited_route(ref.get("url"))
    final = _canonical_cited_route(source_ref)
    if not cited or not final or cited[1:] != final[1:]:
        return False
    return cited[0] == final[0] or (cited[0] == "http" and final[0] == "https")


def _same_origin(left_url: str, right_url: str) -> bool:
    try:
        left = urllib.parse.urlsplit(left_url)
        right = urllib.parse.urlsplit(right_url)
        left_port = left.port or (443 if left.scheme.lower() == "https" else 80)
        right_port = right.port or (443 if right.scheme.lower() == "https" else 80)
    except (TypeError, ValueError):
        return False
    return bool(
        left.scheme
        and left.hostname
        and right.scheme
        and right.hostname
        and left.scheme.lower() in {"http", "https"}
        and left.scheme.lower() == right.scheme.lower()
        and left.hostname.lower().rstrip(".") == right.hostname.lower().rstrip(".")
        and left_port == right_port
    )


def _cited_landing_pdf_identity(
    ref: dict,
    resolve_result: dict,
    candidate_context: dict | None,
    source_ref: str,
    method: str,
    citation_probe: dict,
) -> bool:
    """Bounded proof for a PDF anchor from the exact cited landing route."""
    context = candidate_context or {}
    citation_probe = citation_probe or {}
    parent = context.get("cited_landing_url")
    title_source = citation_probe.get("expected_title_source")
    return bool(
        method == "landing_link"
        and context.get("cited_landing_pdf") is True
        and isinstance(parent, str)
        and _explicit_cited_route_matches(ref, parent)
        and _same_origin(parent, source_ref)
        and not context.get("context_conflict")
        and not context.get("identity_conflict")
        and not resolve_result.get("context_conflict")
        and not resolve_result.get("identity_conflict")
        and not _resolve_has_hard_identity_conflict(resolve_result)
        and citation_probe.get("ok")
        and citation_probe.get("decision") == "confirmed"
        # A named cited author that is absent from the document is contrary
        # evidence, even when the cited landing page names the PDF directly.
        # ``None`` remains acceptable for institutional citations with no
        # author signal to check.
        and citation_probe.get("author_ok") is not False
        and title_source in {"citation", "citation_quoted", "citation_raw_entry"}
    )


def _last_path_segment(url: str | None) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(str(url or "")).path or "")
    return path.rstrip("/").rsplit("/", 1)[-1]


def _identity_preserving_redirect(
    ref: dict, source_ref: str, redirect_chain: list | None
) -> bool:
    """A literal route mismatch can still be trusted when the redirect chain proves
    a permanent move of the same resource, not merely a host match.

    All three must hold: every hop in the chain is a permanent redirect (301/308,
    never 302/303/307); the final URL does not land on the site root (a root landing
    is "we no longer have this", not a move); and the cited URL's last path segment
    survives unchanged onto the final URL. That last condition is what distinguishes
    a repository transfer (.../a/proj -> .../b/proj) from an unrelated redirect to a
    different resource on the same host (.../paper-x -> .../paper-y).

    The host is deliberately NOT required to match, so a domain migration is covered
    too. This is a wider trust surface than ``_explicit_cited_route_matches`` above,
    and the difference is intentional rather than an oversight: that function judges
    a URL on its own, where a host match proves nothing, while here the permanent
    redirect was issued BY the cited host, which is the only party entitled to say
    where its own resource moved. Abusing it already requires control of the cited
    host, and thus of what the manuscript pointed at in the first place.
    """
    if not redirect_chain:
        return False
    for hop in redirect_chain:
        status = hop[0] if isinstance(hop, (tuple, list)) else (
            hop.get("status") if isinstance(hop, dict) else None
        )
        if status not in (301, 308):
            return False
    final_path = urllib.parse.unquote(urllib.parse.urlsplit(str(source_ref or "")).path or "")
    if final_path in ("", "/"):
        return False
    cited_last = _last_path_segment(ref.get("url"))
    final_last = _last_path_segment(source_ref)
    if not cited_last or not final_last:
        return False
    return cited_last == final_last


def _explicit_cited_route_ok(
    ref: dict, source_ref: str, redirect_chain: list | None = None
) -> bool:
    """Share the exact cited-route predicate with bounded Fetch recoveries."""
    return bool(
        _explicit_cited_route_matches(ref, source_ref)
        or _identity_preserving_redirect(ref, source_ref, redirect_chain)
    )


_WEB_LIKE_SOURCE_TYPES = {
    "webpage",
    "website",
    "web",
    "blog",
    "news",
    "documentation",
}


def _source_type(ref: dict) -> str:
    return str(ref.get("source_type") or ref.get("source_kind") or "").strip().lower()


def explicit_cited_url_requires_identity(ref: dict, resolve_result: dict) -> bool:
    """Keep direct URL trust for true web citations, but not for scholarly works.

    Article-like references can cite a repository or landing URL that resolves to a
    different paper. In those cases the fetched text still needs the same identity
    corroboration and front-matter probe as provider-discovered candidates.
    """
    source_type = _source_type(ref)
    if source_type in _WEB_LIKE_SOURCE_TYPES:
        return False
    if ref.get("title") or resolve_result.get("matched_title"):
        return True
    return source_type in {
        "article",
        "conference",
        "paper",
        "preprint",
        "thesis",
        "chapter",
        "book",
    }


def _normalise_doi(value: object) -> str | None:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi:\s*", "", text).rstrip(".,;)")
    return text if text.startswith("10.") else None


def _doi_values_in_text(text: str) -> set[str]:
    values = {
        _normalise_doi(match.group(0))
        for match in _DOI_IN_TEXT_RE.finditer(text or "")
    }
    values.discard(None)
    return values


def _arxiv_id(value: object) -> str | None:
    text = urllib.parse.unquote(str(value or ""))
    identifier = r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})"
    bare = re.fullmatch(rf"\s*({identifier})(?:v\d+)?\s*", text, flags=re.IGNORECASE)
    if bare:
        return bare.group(1)
    match = re.search(
        rf"(?:arxiv[.:/]|(?:abs|pdf|html)/)({identifier})(?:v\d+)?(?:\.pdf)?",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def _acl_id(value: object) -> str | None:
    text = urllib.parse.unquote(str(value or ""))
    doi = re.search(r"10\.(?:18653|3115)/v\d+/([A-Za-z]\d{2}-\d{4})", text, re.IGNORECASE)
    if doi:
        return doi.group(1).upper()
    match = re.search(r"(?:aclanthology\.org/(?:[^/]+/)?|aclweb\.org/anthology/)([A-Za-z]\d{2}-\d{4})", text, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    match = re.fullmatch(r"\s*([A-Za-z]\d{2}-\d{4})\s*", text)
    return match.group(1).upper() if match else None


def _candidate_identifiers(candidate_context: dict | None) -> dict:
    identifiers = (candidate_context or {}).get("identifiers") or {}
    return identifiers if isinstance(identifiers, dict) else {}


def _resolved_doi_values(resolve_result: dict) -> set[str]:
    """Return DOI values from the canonical Resolve result fields only."""
    identity = resolve_result.get("identity")
    identity_doi = (
        _normalise_doi(identity.get("value"))
        if isinstance(identity, dict)
        else None
    )
    if not (
        isinstance(identity, dict)
        and str(identity.get("scheme") or "").lower() == "doi"
        and identity_doi
        and identity.get("is_strong") is True
        and identity.get("class_") == "global"
        and resolve_result.get("identity_state")
        in {"resolved_strong_declared", "resolved_strong_discovered"}
        and resolve_result.get("resolution_basis") == "doi"
    ):
        return set()

    values = {identity_doi, _normalise_doi(resolve_result.get("doi"))}
    resolved_identifier = resolve_result.get("resolved_identifier") or {}
    if isinstance(resolved_identifier, dict) and str(
        resolved_identifier.get("type") or resolved_identifier.get("scheme") or ""
    ).lower() == "doi":
        values.add(_normalise_doi(resolved_identifier.get("value")))
    values.discard(None)
    return values


def _doi_anchored_resolved_candidate_probe(
    ref: dict,
    resolve_result: dict,
    candidate_context: dict | None,
    document_identity_text: str,
) -> dict | None:
    """Confirm a resolved candidate only at the closed DOI/title boundary.

    This exception repairs a contaminated parsed title only after the ordinary
    citation-owned probe was inconclusive.  It is intentionally not a general
    resolver-title fallback: the citation, resolved record, candidate context,
    and document front matter must all independently carry the same DOI.
    """
    context = candidate_context or {}
    candidate_title = str(context.get("title") or "").strip()
    matched_title = str(resolve_result.get("matched_title") or "").strip()
    citation_doi = _normalise_doi(ref.get("doi"))
    candidate_doi = _normalise_doi(_candidate_identifiers(context).get("doi"))
    resolved_dois = _resolved_doi_values(resolve_result)
    if (
        resolve_result.get("status") != "resolved"
        or resolve_result.get("resolution_basis") != "doi"
        or not citation_doi
        or not candidate_doi
        or resolved_dois != {citation_doi}
        or candidate_doi != citation_doi
        or not candidate_title
        or _sources._norm(candidate_title) != _sources._norm(matched_title)
        or context.get("context_conflict")
        or context.get("identity_conflict")
        or resolve_result.get("context_conflict")
        or resolve_result.get("identity_conflict")
        or _resolve_has_hard_identity_conflict(resolve_result)
    ):
        return None

    # Keep identifier evidence out of references: the identity probe itself
    # already excludes a title found only in the bibliography, and the DOI must
    # be present in the same native front-matter boundary.
    document_head = (document_identity_text or "")[:_sources.DOCUMENT_IDENTITY_HEAD_CHARS]
    front_matter = _sources._front_matter_before_abstract(document_head)
    if citation_doi not in _doi_values_in_text(front_matter):
        return None

    candidate_ref = dict(ref)
    candidate_ref["title"] = candidate_title
    probe = _sources.document_identity_probe(
        candidate_ref, document_identity_text, None, None
    )
    if not (probe.get("ok") and probe.get("decision") == "confirmed"):
        return None
    probe = dict(probe)
    probe.update({
        "expected_title_source": "doi_anchored_resolved_candidate",
        "reason_code": "identity_confirmed_doi_anchored_resolved_candidate",
        "reason": (
            "citation DOI, resolved DOI, candidate DOI, and native document "
            "front matter confirmed the resolved candidate title"
        ),
        "fallback_confirmation": "doi_anchored_resolved_candidate",
    })
    return probe


def _trusted_springer_jats_front_confirmed(
    ref: dict,
    resolve_result: dict,
    candidate_context: dict | None,
    source_ref: str,
    identity_extract_text: str | None,
    identity_extract_method: str | None,
    extract_method: str | None,
    method: str,
    decision: str,
) -> bool:
    """Recognize the closed Springer JATS front-matter identity route."""
    context = candidate_context or {}
    identifiers = context.get("identifiers")
    context_doi = _normalise_doi(
        identifiers.get("doi") if isinstance(identifiers, dict) else None
    )
    cited_doi = _normalise_doi(ref.get("doi"))
    resolved_doi = _normalise_doi(resolve_result.get("doi"))
    resolved_identifier = resolve_result.get("resolved_identifier") or {}
    identifier_doi = _normalise_doi(
        resolved_identifier.get("value")
        if isinstance(resolved_identifier, dict)
        and str(resolved_identifier.get("type") or "").lower() == "doi"
        else None
    )
    expected_dois = {doi for doi in (cited_doi, resolved_doi, identifier_doi) if doi}
    if (
        method != "springer_openaccess"
        or extract_method != "api_jats"
        or identity_extract_method != "api_jats_front"
        or context.get("provider") != "springer_openaccess"
        or not isinstance(identity_extract_text, str)
        or not identity_extract_text
        or not context_doi
        or expected_dois != {context_doi}
        or decision != "confirmed"
        or _resolve_has_hard_identity_conflict(resolve_result)
    ):
        return False
    surname = _sources._first_author_surname(ref)
    if surname and not re.search(
        rf"(?<!\w){re.escape(surname)}(?!\w)", identity_extract_text, re.I,
    ):
        return False
    if not re.search(
        rf"(?<![\w/]){re.escape(context_doi)}(?![\w./-])",
        identity_extract_text,
        re.I,
    ):
        return False
    try:
        parsed = urllib.parse.urlsplit(source_ref)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "api.springernature.com"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/openaccess/jats"
        and not parsed.fragment
        and urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        == {"q": [f"doi:{context_doi}"], "p": ["1"], "s": ["1"]}
    )


def _resolve_has_hard_identity_conflict(resolve_result: dict) -> bool:
    evidence = resolve_result.get("evidence_profile") or {}
    profiles = [
        resolve_result.get("metadata_match"),
        evidence.get("metadata_match"),
        (evidence.get("best_candidate") or {}).get("metadata_match"),
    ]
    if resolve_result.get("identity_conflict") or resolve_result.get("metadata_conflict"):
        return True
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        hard_conflicts = set(profile.get("hard_conflicts") or [])
        if (
            profile.get("metadata_conflict")
            or profile.get("author_conflict")
            or profile.get("venue_conflict")
            or hard_conflicts.intersection({"author", "venue"})
            or len(hard_conflicts.intersection({"author", "year", "venue"})) >= 2
        ):
            return True
    return False


def _exact_arxiv_source_identity(
    ref: dict,
    resolve_result: dict,
    candidate_context: dict | None,
    source_ref: str,
) -> str | None:
    """Return the exact arXiv ID only when source URL and expected identity agree."""
    source_id = _arxiv_id(source_ref)
    if not source_id:
        return None
    identifiers = _candidate_identifiers(candidate_context)
    expected_values = [
        ref.get("doi"), ref.get("url"), ref.get("raw_entry"),
        resolve_result.get("doi"),
    ]
    resolved_identifier = resolve_result.get("resolved_identifier") or {}
    if isinstance(resolved_identifier, dict):
        expected_values.append(resolved_identifier.get("value"))
    resolve_identifiers = resolve_result.get("identifiers") or {}
    if isinstance(resolve_identifiers, dict):
        expected_values.append(resolve_identifiers.get("arxiv_id"))
    for link in resolve_result.get("fulltext_links") or []:
        if not isinstance(link, dict):
            continue
        contexts = [link.get("identity_context")]
        if isinstance(link.get("identity_contexts"), list):
            contexts.extend(link["identity_contexts"])
        for context in contexts:
            if isinstance(context, dict):
                expected_values.append(_candidate_identifiers(context).get("arxiv_id"))
    if source_id in {_arxiv_id(value) for value in expected_values}:
        return source_id

    # An OpenAlex location can identify its arXiv route, but its own arXiv ID
    # is not independent evidence.  It becomes sufficient only when the same
    # candidate DOI agrees exactly with a DOI already present in the citation
    # or Resolve payload/context.
    if (candidate_context or {}).get("provider") != "openalex":
        candidate_values = (identifiers.get("arxiv_id"), identifiers.get("doi"))
        return source_id if source_id in {_arxiv_id(value) for value in candidate_values} else None
    if source_id != _arxiv_id(identifiers.get("arxiv_id")):
        return None
    context_doi = _normalise_doi(identifiers.get("doi"))
    independent_dois = {
        doi for doi in (
            _normalise_doi(ref.get("doi")),
            _normalise_doi(resolve_result.get("doi")),
            _normalise_doi(resolve_identifiers.get("doi"))
            if isinstance(resolve_identifiers, dict) else None,
            _normalise_doi(resolved_identifier.get("value"))
            if isinstance(resolved_identifier, dict) else None,
        ) if doi
    }
    for link in resolve_result.get("fulltext_links") or []:
        if not isinstance(link, dict):
            continue
        contexts = [link.get("identity_context")]
        if isinstance(link.get("identity_contexts"), list):
            contexts.extend(link["identity_contexts"])
        for context in contexts:
            if isinstance(context, dict):
                independent_dois.add(_normalise_doi(_candidate_identifiers(context).get("doi")))
    independent_dois.discard(None)
    return source_id if context_doi and context_doi in independent_dois else None


def _exact_acl_source_identity(
    ref: dict,
    resolve_result: dict,
    candidate_context: dict | None,
    source_ref: str,
) -> str | None:
    parsed = urllib.parse.urlparse(source_ref)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host not in {"aclanthology.org", "aclweb.org"}:
        return None
    source_id = _acl_id(source_ref)
    if not source_id:
        return None
    identifiers = _candidate_identifiers(candidate_context)
    values = [
        ref.get("doi"), ref.get("url"), resolve_result.get("doi"),
        (
            candidate_context.get("provider_record_id")
            if isinstance(candidate_context, dict)
            else None
        ),
        identifiers.get("doi"), identifiers.get("provider_record_id"),
        identifiers.get("acl_id"), resolve_result.get("paper_id"),
        (resolve_result.get("resolved_identifier") or {}).get("value")
        if isinstance(resolve_result.get("resolved_identifier"), dict) else None,
    ]
    expected = {_acl_id(value) for value in values if _acl_id(value)}
    return source_id if source_id in expected else None


def _exact_author_copy_identity(
    ref: dict,
    resolve_result: dict,
    candidate_context: dict | None,
    source_ref: str,
    text: str,
) -> bool:
    """Conservative author-copy fallback for titles split by PDF layout.

    It requires an exact per-candidate DOI, an author-owned hostname/path, the
    cited surname in the front matter, and near-complete unordered title tokens.
    """
    identifiers = _candidate_identifiers(candidate_context)
    context_doi = _normalise_doi(identifiers.get("doi"))
    expected_dois = {
        doi for doi in (
            _normalise_doi(ref.get("doi")),
            _normalise_doi(resolve_result.get("doi")),
        ) if doi
    }
    resolved_identifier = resolve_result.get("resolved_identifier") or {}
    if isinstance(resolved_identifier, dict):
        expected_doi = _normalise_doi(resolved_identifier.get("value"))
        if expected_doi:
            expected_dois.add(expected_doi)
    surname = _sources._first_author_surname(ref)
    parsed = urllib.parse.urlparse(source_ref)
    owner_hint = re.sub(r"[^a-z0-9]", "", f"{parsed.netloc}{parsed.path}".lower())
    normalized_surname = re.sub(r"[^a-z0-9]", "", (surname or "").lower())
    head = unicodedata.normalize(
        "NFKC", (text or "")[:_sources.DOCUMENT_IDENTITY_HEAD_CHARS]
    )
    expected_title = (
        str((candidate_context or {}).get("title") or "").strip()
        or str(ref.get("title") or "").strip()
        or str(resolve_result.get("matched_title") or "").strip()
    )
    cited_title = str(ref.get("title") or resolve_result.get("matched_title") or "").strip()
    expected_title = unicodedata.normalize("NFKC", expected_title)
    cited_title = unicodedata.normalize("NFKC", cited_title)
    if not context_doi or context_doi not in expected_dois:
        return False
    if not normalized_surname or normalized_surname not in owner_hint:
        return False
    if not surname or surname.lower() not in _sources._norm(head):
        return False
    if cited_title and expected_title:
        cited_tokens = _sources._tokens(cited_title)
        candidate_tokens = _sources._tokens(expected_title)
        fidelity = len(cited_tokens & candidate_tokens) / max(len(cited_tokens), 1)
        if fidelity < 0.8:
            return False
    expected_tokens = _sources._tokens(expected_title)
    if len(expected_tokens) < 3:
        return False
    overlap = len(expected_tokens & _sources._tokens(head)) / len(expected_tokens)
    return overlap >= 0.9


def _cited_first_author(ref: dict) -> str | None:
    """Extract a normalized first-author/institution token from the citation."""
    surname = _sources._first_author_surname(ref)
    if surname:
        tokens = re.findall(r"[a-z0-9]+", str(surname).casefold())
        return tokens[-1] if tokens else None
    raw = str(ref.get("raw_entry") or "")
    title = str(ref.get("title") or "")
    if title:
        match = re.search(re.escape(title), raw, flags=re.IGNORECASE)
        if match:
            raw = raw[:match.start()]
    first = re.split(r",|\s+(?:and|&)\s+", raw, maxsplit=1)[0]
    tokens = re.findall(r"[a-z0-9]+", first.casefold())
    return tokens[-1] if tokens else None


def _exact_official_curated_document_identity(
    ref: dict,
    resolve_result: dict,
    candidate_context: dict | None,
    source_ref: str,
    text: str,
) -> bool:
    """Confirm the one explicitly opted-in official curated document route.

    This is intentionally not a general official-host exception.  A catalogue
    entry must opt in and its exact route, bibliographic metadata, official host,
    and front matter must all agree before a layout-split identity result can be
    repaired.
    """
    context = candidate_context or {}
    if (
        context.get("provider") != "curated_copies"
        or context.get("official") is not True
        or context.get("canonical_host") is not True
        or context.get("official_document_relation")
        != "official_curated_exact_layout_split_document"
        or context.get("context_conflict")
        or context.get("identity_conflict")
        or resolve_result.get("context_conflict")
        or _resolve_has_hard_identity_conflict(resolve_result)
    ):
        return False

    canonical_url = str(context.get("canonical_url") or "")
    canonical_route = _canonical_cited_route(canonical_url)
    source_route = _canonical_cited_route(source_ref)
    if not canonical_route or canonical_route != source_route:
        return False
    try:
        from core.resolve.providers import curated_copies
        catalogue_record = curated_copies.official_layout_split_catalogue_record(
            canonical_url
        )
    except ImportError:
        return False
    if catalogue_record is None:
        return False

    cited_title = curated_copies._norm(ref.get("title"))
    catalogue_title = curated_copies._norm(catalogue_record["title"])
    if (
        not cited_title
        or cited_title != catalogue_title
        or curated_copies._norm(context.get("title")) != catalogue_title
    ):
        return False
    try:
        cited_year = int(ref.get("year"))
        catalogue_year = int(context.get("year"))
    except (TypeError, ValueError):
        return False
    if cited_year != catalogue_record["year"] or catalogue_year != catalogue_record["year"]:
        return False
    catalogue_author_tokens = re.findall(
        r"[a-z0-9]+", str(context.get("first_author") or "").casefold()
    )
    catalogue_author = catalogue_record["first_author"]
    context_author = catalogue_author_tokens[-1] if catalogue_author_tokens else None
    if _cited_first_author(ref) != catalogue_author or context_author != catalogue_author:
        return False

    # Short edition labels (for example ``v3``) are often present in the
    # citation and versioned filename but omitted from the cover title.  Verify
    # them against the exact canonical route, then corroborate the substantive
    # title words in the document front matter.
    edition_tokens = set(re.findall(r"\bv\d+\b", catalogue_title))
    route_tokens = set(re.findall(
        r"[a-z0-9]+",
        urllib.parse.unquote(canonical_route[2]).casefold(),
    ))
    if edition_tokens and not edition_tokens.issubset(route_tokens):
        return False
    expected_tokens = _sources._tokens(catalogue_title)
    if len(expected_tokens) < 3:
        return False
    head = (text or "")[:_sources.DOCUMENT_IDENTITY_HEAD_CHARS]
    front_matter = re.split(r"\babstract\b", head, maxsplit=1, flags=re.IGNORECASE)[0]
    front_tokens = set(re.findall(r"[a-z0-9]+", _sources._norm(front_matter)))
    if len(expected_tokens & front_tokens) / len(expected_tokens) < 0.9:
        return False
    front = _sources._norm(front_matter)
    signals = list(catalogue_record.get("institutional_signals") or ()) + [
        catalogue_record["first_author"]
    ]
    return any(
        (signal_text := _sources._norm(signal)) and signal_text in front
        for signal in signals
    )


def _official_proceedings_document_identity(
    ref: dict,
    resolve_result: dict,
    candidate_context: dict | None,
    source_ref: str,
    text: str,
) -> bool:
    """Confirm a catalogued TAC or NeurIPS landing-page-to-PDF relation."""
    context = candidate_context or {}
    provider = context.get("provider")
    official_hosts = {
        "tac_search": "tac.nist.gov",
        "neurips_search": "proceedings.neurips.cc",
    }
    if (
        provider not in official_hosts
        or context.get("official") is not True
        or context.get("official_document_relation")
        != "official_landing_page_links_exact_document"
        or context.get("canonical_host") is not True
        or context.get("context_conflict")
        or context.get("identity_conflict")
        or _resolve_has_hard_identity_conflict(resolve_result)
    ):
        return False
    landing_route = _canonical_cited_route(context.get("landing_page_url"))
    document_route = _canonical_cited_route(context.get("canonical_url"))
    source_route = _canonical_cited_route(source_ref)
    if (
        not landing_route
        or not document_route
        or document_route != source_route
        or landing_route == document_route
        or document_route[1].split(":", 1)[0] != official_hosts[provider]
        or landing_route[1].split(":", 1)[0] != official_hosts[provider]
    ):
        return False
    citation_title = _sources._norm(ref.get("title"))
    catalogue_title = _sources._norm(context.get("title"))
    if not citation_title or not catalogue_title:
        return False
    if citation_title != catalogue_title:
        if not (
            provider == "neurips_search"
            and _neurips_exact_title_prefix_with_proceedings_tail(
                citation_title, catalogue_title,
            )
        ):
            return False
    try:
        if int(ref.get("year")) != int(context.get("year")):
            return False
    except (TypeError, ValueError):
        return False
    if not _official_route_first_author_matches(ref, context.get("first_author")):
        return False
    expected_tokens = _sources._tokens(
        _sources._norm(context.get("expected_document_title"))
    )
    if len(expected_tokens) < 3:
        return False
    # Multi-column proceedings PDFs can be extracted with the abstract before
    # the title/author block even though all three belong to the first page.
    # The route, catalogue record, year, and author checks above remain
    # mandatory, so inspect the bounded document head instead of truncating at
    # the first extracted ``Abstract`` token.
    document_head = _sources._norm(
        (text or "")[:_sources.DOCUMENT_IDENTITY_HEAD_CHARS]
    )
    if (
        len(expected_tokens & _sources._tokens(document_head))
        / len(expected_tokens)
        < 0.9
    ):
        return False
    author = _sources._norm(context.get("first_author"))
    return bool(author and author in document_head)


def _neurips_exact_title_prefix_with_proceedings_tail(
    citation_title: str, catalogue_title: str,
) -> bool:
    """Allow only an exact title followed by the known proceedings tail."""
    official = re.findall(r"[a-z0-9]+", catalogue_title)
    cited = re.findall(r"[a-z0-9]+", citation_title)
    if cited[:len(official)] != official:
        return False
    tail = cited[len(official):]
    proceedings = ["in", "advances", "in", "neural", "information", "processing", "systems"]
    if tail[:len(proceedings)] != proceedings:
        return False
    return all(token == "pages" or token.isdigit() for token in tail[len(proceedings):])


def _official_route_first_author_matches(ref: dict, expected_author: object) -> bool:
    """Match a catalogue surname without mistaking a citation initial for it."""
    expected = _sources._norm(expected_author)
    if not expected:
        return False
    if _cited_first_author(ref) == expected:
        return True
    raw = str(ref.get("raw_entry") or "")
    title = str(ref.get("title") or "")
    if title:
        match = re.search(re.escape(title), raw, flags=re.IGNORECASE)
        if match:
            raw = raw[:match.start()]
    return expected in _sources._norm(raw)


def _exact_openai_report_document_identity(
    ref: dict,
    resolve_result: dict,
    candidate_context: dict | None,
    source_ref: str,
    text: str,
) -> bool:
    """Confirm an explicitly catalogued OpenAI landing-page-to-PDF relation.

    This is intentionally scoped to catalogue entries that carry relation
    proof. An OpenAI hostname or provider label alone is never sufficient.
    """
    context = candidate_context or {}
    if (
        context.get("provider") != "openai_reports"
        or context.get("official") is not True
        or context.get("official_document_relation")
        != "official_landing_page_links_exact_document"
        or context.get("canonical_host") is not True
        or context.get("context_conflict")
        or context.get("identity_conflict")
        or _resolve_has_hard_identity_conflict(resolve_result)
    ):
        return False

    landing_route = _canonical_cited_route(context.get("landing_page_url"))
    document_route = _canonical_cited_route(context.get("canonical_url"))
    source_route = _canonical_cited_route(source_ref)
    if (
        not landing_route
        or not document_route
        or document_route != source_route
        or landing_route == document_route
    ):
        return False
    try:
        from core.resolve.providers import openai_reports
        official_hosts = openai_reports._OFFICIAL_HOSTS
    except ImportError:
        return False
    for route in (landing_route, document_route):
        host = route[1].split(":", 1)[0]
        if not any(host == item or host.endswith(f".{item}") for item in official_hosts):
            return False

    # The provider has already required this content-title key to identify one
    # and only one catalogue record. Recheck it here so persisted or supplied
    # context cannot weaken the authoritative relation.
    report = openai_reports._known_report(ref)
    citation_title_key = openai_reports.ordered_content_title_key(ref.get("title"))
    catalogue_title_key = openai_reports.ordered_content_title_key(context.get("title"))
    if (
        not report
        or not citation_title_key
        or citation_title_key != catalogue_title_key
        or not openai_reports.is_function_word_elision(ref.get("title"), report["title"])
        or _sources._norm(context.get("title")) != _sources._norm(report["title"])
        or context.get("year") != report["year"]
        or _sources._norm(context.get("first_author")) != report["first_author"]
        or context.get("canonical_url") != report["url"]
        or context.get("landing_page_url") != report.get("landing_page_url")
        or context.get("expected_document_title") != report.get("expected_document_title")
        or context.get("official_document_relation")
        != report.get("official_document_relation")
    ):
        return False
    try:
        if int(ref.get("year")) != int(context.get("year")):
            return False
    except (TypeError, ValueError):
        return False
    if (
        openai_reports._first_author(ref, report["title"])
        != _sources._norm(context.get("first_author"))
    ):
        return False

    expected_document_title = _sources._norm(context.get("expected_document_title"))
    expected_tokens = _sources._tokens(expected_document_title)
    if len(expected_tokens) < 3:
        return False
    head = (text or "")[:_sources.DOCUMENT_IDENTITY_HEAD_CHARS]
    front_matter = re.split(r"\babstract\b", head, maxsplit=1, flags=re.IGNORECASE)[0]
    front = _sources._norm(front_matter)
    front_tokens = _sources._tokens(front)
    if len(expected_tokens & front_tokens) / len(expected_tokens) < 0.9:
        return False
    author = _sources._norm(context.get("first_author"))
    return bool(author and author in front)


def metadata_origin(resolve_result: dict) -> str:
    via = str(resolve_result.get("via") or "").lower()
    if "europepmc" in via:
        return "europepmc"
    if "crossref" in via:
        return "crossref"
    return "webfetch"


def _web_search_identity(resolve_result: dict | None) -> bool:
    """True when the resolution was recovered by web search of a non-scholarly,
    link-less citation (e.g. the institutional-report resolver). Such content is
    stored as externally-corroborated so its claims can never be fully green.

    Keyed on ``resolution_basis`` (a persisted resolve column that survives the
    round-trip to the database and back into fetch) as well as the in-memory
    ``identity_basis`` hint, so the cap holds regardless of which one is present."""
    rr = resolve_result or {}
    return (str(rr.get("identity_basis") or "") == "web_search"
            or str(rr.get("resolution_basis") or "") == "institutional_web")


def origin_for_method(
    method: str,
    resolve_result: dict,
    candidate_context: dict | None = None,
) -> str:
    if method in _sources.ORIGINS:
        return method
    provider = str((candidate_context or {}).get("provider") or "").strip().lower()
    if provider:
        try:
            from core.resolve import providers as provider_registry
            provider = provider_registry.provider_name_for_via(provider) or provider
        except ImportError:
            pass
        provider = str(provider).removesuffix("_search")
        if provider in _sources.ORIGINS:
            return provider
    if method == "metadata":
        return metadata_origin(resolve_result)
    return "webfetch"


def abstract_origin(resolve_result: dict) -> str:
    via = str(resolve_result.get("abstract_via") or resolve_result.get("via") or "").strip().lower()
    if via in _sources.ORIGINS:
        return via
    try:
        from core.resolve import providers as provider_registry
    except ImportError:
        return "webfetch"
    provider = provider_registry.provider_name_for_via(via)
    if provider in _sources.ORIGINS:
        return provider
    return "webfetch"


def abstract_source_ref(resolve_result: dict) -> str:
    via = resolve_result.get("abstract_via") or resolve_result.get("via") or abstract_origin(resolve_result)
    return resolve_result.get("url") or f"resolve:{via}"


def store_if_new_abstract(
    run_dir: str, ref: dict, origin: str, text: str, source_ref: str
) -> dict | None:
    man = _sources.load_manifest(run_dir)
    existing = [
        e
        for e in man.get("entries", [])
        if e.get("ref_id") == ref["id"] and e.get("tier") == "abstract"
    ]
    if existing:
        return existing[0]
    sig, score = _sources.corroborate(ref, text)
    return _sources.store_text(
        run_dir,
        ref,
        "abstract",
        origin,
        text,
        source_ref=source_ref,
        mapping="auto" if sig in ("doi", "pmid") else "tokens",
        signal=sig,
        score=score,
    )


def store_resolver_abstract_if_present(run_dir: str, ref: dict, resolve_result: dict) -> dict | None:
    abstract = resolve_result.get("abstract")
    if not abstract:
        return None
    return store_if_new_abstract(
        run_dir,
        ref,
        abstract_origin(resolve_result),
        abstract,
        abstract_source_ref(resolve_result),
    )


def try_store_fulltext(
    run_dir: str,
    ref: dict,
    resolve_result: dict,
    text: str,
    *,
    method: str,
    source_ref: str,
    extract_method: str | None = None,
    identity_extract_text: str | None = None,
    corroborate_text: str | None = None,
    content_version: str | None = None,
    candidate_context: dict | None = None,
    identity_extract_method: str | None = None,
    extraction_flags: list[str] | None = None,
    redirect_chain: list | None = None,
    storage_provenance: dict | None = None,
    identity_attested: bool = False,
) -> dict:
    storage_provenance = _validated_storage_provenance(storage_provenance)
    if type(identity_attested) is not bool:
        raise ValueError("identity_attested must be boolean")
    if identity_attested and not (
        storage_provenance.get("mapping") == "controlled_task_answer"
        and storage_provenance.get("supplied_by") == "user"
        and str(storage_provenance.get("supplied_via") or "").startswith("controlled_task_answer:")
    ):
        raise ValueError("identity attestation requires authenticated controlled task provenance")
    # Identity checks below deliberately use the native extraction, including
    # front matter and references.  The text persisted for verify is prepared
    # separately: it is source prose, not a bibliography for the LLM to read.
    verify_text, preparation = _source_text.prepare_for_verify(
        text, format_hint=extract_method)
    if not verify_text:
        return {
            "status": "quality_error",
            "method": method,
            "reason": "source body was empty after canonical preparation",
            "reason_code": "empty_prepared_source",
        }
    native_quality_ok = _prepared_text_quality(text)
    prepared_quality_ok = _prepared_text_quality(verify_text)
    preparation["native_quality_ok"] = bool(native_quality_ok)
    preparation["prepared_quality_ok"] = bool(prepared_quality_ok)

    def _quality_rejection(**identity: object) -> dict:
        transformed_loss = native_quality_ok and not prepared_quality_ok
        result = {
            "status": "quality_error",
            "method": method,
            "reason": (
                "source body fell below the full-text quality gate after "
                "canonical preparation"
                if transformed_loss
                else "source text did not pass the full-text quality gate"
            ),
            "reason_code": (
                "prepared_source_below_quality_threshold"
                if transformed_loss
                else "source_below_quality_threshold"
            ),
            "preparation": preparation,
        }
        result.update({key: value for key, value in identity.items() if value is not None})
        return result

    # If a healthy native extraction became unhealthy during preparation, the
    # transformation itself is the primary diagnosis; no identity work is
    # needed.  Native low-text inputs continue through the read-only identity
    # probe below, but every storage branch still rejects them.
    if native_quality_ok and not prepared_quality_ok:
        return _quality_rejection()

    document_relation = document_relation_probe(ref, text)
    if document_relation["decision"] == "incompatible":
        return {
            "status": "identity_mismatch",
            "method": method,
            "reason": document_relation["reason"],
            "reason_code": document_relation["reason_code"],
            "document_relation": document_relation,
        }
    if identity_attested:
        if not prepared_quality_ok:
            return _quality_rejection()
        entry = _store_prepared_fulltext(
            run_dir, ref, origin_for_method(method, resolve_result, candidate_context), verify_text,
            preparation=preparation, extraction_flags=extraction_flags,
            extraction_method=extract_method, storage_provenance={
                **storage_provenance, "mapping": "operator_attested",
            }, source_ref=source_ref, signal="operator_confirmation", score=1.0,
            identity_status="operator_attested",
            identity_note=("identity confirmed by the authenticated operator through "
                           "the controlled task answer"),
            content_version=content_version,
        )
        store_resolver_abstract_if_present(run_dir, ref, resolve_result)
        return {
            "status": "stored", "stored_as": entry["stored_as"], "method": method,
            "pdf_url": source_ref, "corroborate_signal": "operator_confirmation",
            "corroborate_score": 1.0, "identity_status": "operator_attested",
            "document_relation": document_relation, "source_preparation": preparation,
        }

    explicit_cited_url = is_explicit_cited_url(ref, method)
    explicit_cited_url_eligible = (
        explicit_cited_url
        and not explicit_cited_url_requires_identity(ref, resolve_result)
    )
    # Literal route match is the primary trust path; a redirect that is not a
    # literal match can still qualify when it is provably identity-preserving
    # (permanent redirect, not onto the site root, same last path segment) —
    # e.g. a repository transfer. See `_identity_preserving_redirect`.
    explicit_cited_url_route_ok = explicit_cited_url_eligible and _explicit_cited_route_ok(
        ref, source_ref, redirect_chain
    )
    if explicit_cited_url_eligible and not explicit_cited_url_route_ok:
        return {
            "status": "identity_inconclusive",
            "method": method,
            "reason": (
                "explicit cited webpage redirected to a different route; "
                "a host-only redirect cannot establish source identity"
            ),
            "reason_code": "cited_url_route_mismatch",
        }
    if explicit_cited_url_route_ok:
        if not prepared_quality_ok:
            return _quality_rejection(
                corroborate_signal="url",
                corroborate_score=1.0,
            )
        origin = origin_for_method(method, resolve_result, candidate_context)
        entry = _store_prepared_fulltext(
            run_dir,
            ref,
            origin,
            verify_text,
            preparation=preparation,
            extraction_flags=extraction_flags,
            extraction_method=extract_method,
            storage_provenance=storage_provenance,
            source_ref=source_ref,
            mapping="cited_url",
            signal="url",
            score=1.0,
            identity_status="cited_url_reachable",
            identity_note=("URL explicitly cited in the manuscript; the retrieved text "
                           "is anchored to that cited URL rather than inferred from "
                           "bibliographic metadata"),
        )
        store_resolver_abstract_if_present(run_dir, ref, resolve_result)
        result = {
            "status": "stored",
            "stored_as": entry["stored_as"],
            "method": method,
            "pdf_url": source_ref,
            "corroborate_signal": "url",
            "corroborate_score": 1.0,
            "identity_probe": {
                "ok": True,
                "decision": "confirmed",
                "reason": (
                    "identity is anchored to the exact URL explicitly cited "
                    "in the manuscript"
                ),
                "reason_code": "identity_confirmed_explicit_cited_url",
            },
            "document_relation": document_relation,
            "source_preparation": preparation,
        }
        if extract_method:
            result["extract_method"] = extract_method
        return result
    # Landing metadata can corroborate a declared DOI/PMID, but it is not part of
    # the retrieved document.  A landing page must not supply a title that then
    # confirms an unrelated native body, so all document/provenance checks use
    # the native extraction only.
    document_identity_text = identity_extract_text or text
    hard_resolve_conflict = _resolve_has_hard_identity_conflict(resolve_result)
    citation_conflict_probe = None
    if hard_resolve_conflict:
        # A conflicting resolver candidate may not vouch for the document.  The
        # document can still prove itself directly against the citation's own
        # title/author (for example an exact Adam manuscript recovered from an
        # author repository).  Do not pass resolver or candidate context into
        # this escape hatch: that would make the conflicting candidate circular.
        citation_conflict_probe = _sources.document_identity_probe(
            ref, document_identity_text, None, None
        )
        citation_title_source = citation_conflict_probe.get("expected_title_source")
        citation_identity_confirmed = bool(
            citation_conflict_probe.get("ok")
            and citation_conflict_probe.get("decision") == "confirmed"
            and citation_conflict_probe.get("author_ok") is not False
            and citation_title_source
            in {"citation", "citation_quoted", "citation_raw_entry"}
        )
        if not citation_identity_confirmed:
            return {
                "status": "identity_mismatch",
                "method": method,
                "reason": (
                    "resolver metadata has hard conflicts with the cited work; "
                    "the document did not independently confirm the citation identity"
                ),
                "reason_code": "resolve_metadata_identity_conflict",
                "identity_probe": citation_conflict_probe,
            }
    # Which check answered, and what the resolution's status was at that moment.
    # The signal alone cannot tell the citation's own DOI from one the resolver
    # discovered, so a stored manifest row cannot be audited after the fact.
    corroboration = {}
    identity_resolution = None if hard_resolve_conflict else resolve_result
    identity_candidate_context = None if hard_resolve_conflict else candidate_context
    sig, score = _sources.corroborate(
        ref, document_identity_text, identity_resolution, detail=corroboration
    )
    # Metadata appended by an HTML landing page can supply only an explicit
    # identifier signal.  Its title and generic tokens describe the landing,
    # not the extracted document, so they must never improve corroboration.
    if corroborate_text:
        metadata_corroboration = {}
        metadata_sig, metadata_score = _sources.corroborate(
            ref, corroborate_text, identity_resolution, detail=metadata_corroboration
        )
        # Resolver-title corroboration is deliberately ignored for landing
        # metadata, but it can precede a cited PMID in ``corroborate``.  Retry
        # without resolver context solely to recover a cited identifier.
        if metadata_sig not in ("doi", "pmid"):
            metadata_corroboration = {}
            metadata_sig, metadata_score = _sources.corroborate(
                ref, corroborate_text, None, detail=metadata_corroboration
            )
        if metadata_sig in ("doi", "pmid"):
            sig, score = metadata_sig, metadata_score
            corroboration = metadata_corroboration
    citation_probe = citation_conflict_probe or _sources.document_identity_probe(
        ref, document_identity_text, identity_resolution, None
    )
    # This probe intentionally excludes resolver/candidate metadata.  The
    # cited-landing exception rests on the citation's own title plus the PDF's
    # front matter, never on a tautological author or resolver signal.
    cited_landing_pdf_probe = None
    if (candidate_context or {}).get("cited_landing_pdf") is True:
        cited_landing_pdf_probe = _sources.document_identity_probe(
            ref, document_identity_text, None, None
        )
    citation_probe_confirmed = bool(
        citation_probe.get("ok")
        and citation_probe.get("decision") == "confirmed"
        and citation_probe.get("author_ok") is not False
        and citation_probe.get("expected_title_source")
        in {"citation", "citation_quoted", "citation_raw_entry"}
    )
    identity_probe = (
        citation_probe
        if citation_probe_confirmed
        else _sources.document_identity_probe(
            ref, document_identity_text, identity_resolution, identity_candidate_context
        )
    )
    decision = identity_probe.get("decision") or (
        "confirmed" if identity_probe.get("ok") else "inconclusive"
    )
    doi_anchored_candidate_probe = (
        _doi_anchored_resolved_candidate_probe(
            ref, resolve_result, candidate_context, document_identity_text
        )
        if decision == "inconclusive"
        else None
    )
    if doi_anchored_candidate_probe is not None:
        identity_probe = doi_anchored_candidate_probe
        decision = "confirmed"
    exact_arxiv_id = _exact_arxiv_source_identity(
        ref, resolve_result, candidate_context, source_ref
    )
    exact_acl_id = _exact_acl_source_identity(
        ref, resolve_result, candidate_context, source_ref
    )
    author_copy_identity = _exact_author_copy_identity(
        ref, resolve_result, candidate_context, source_ref, document_identity_text
    )
    official_curated_document_identity = _exact_official_curated_document_identity(
        ref, resolve_result, candidate_context, source_ref, document_identity_text
    )
    openai_report_document_identity = _exact_openai_report_document_identity(
        ref, resolve_result, candidate_context, source_ref, document_identity_text
    )
    official_proceedings_document_identity = _official_proceedings_document_identity(
        ref, resolve_result, candidate_context, source_ref, document_identity_text
    )
    cited_landing_pdf_identity = _cited_landing_pdf_identity(
        ref,
        resolve_result,
        candidate_context,
        source_ref,
        method,
        cited_landing_pdf_probe,
    )
    if cited_landing_pdf_identity:
        identity_probe = dict(cited_landing_pdf_probe)
        decision = "confirmed"
    fallback_kind = (
        "doi_anchored_resolved_candidate" if doi_anchored_candidate_probe else
        "exact_arxiv_id" if exact_arxiv_id else
        "exact_acl_id" if exact_acl_id else
        "exact_author_copy" if author_copy_identity else
        "exact_official_curated_document"
        if (
            official_curated_document_identity
            or openai_report_document_identity
            or official_proceedings_document_identity
        ) else "cited_landing_pdf" if cited_landing_pdf_identity else None
    )
    strong_fallback = bool(
        fallback_kind
        and decision != "rejected"
        and not (candidate_context or {}).get("context_conflict")
        and not (candidate_context or {}).get("identity_conflict")
        and not _resolve_has_hard_identity_conflict(resolve_result)
    )
    if not corroborated(sig, score) and not strong_fallback:
        return {
            "status": "identity_mismatch",
            "method": method,
            "reason": "downloaded text did not corroborate the cited source identity",
            "reason_code": "identity_corroboration_insufficient",
            "corroborate_signal": sig,
            "corroborate_score": score,
            "corroborate_branch": corroboration.get("branch"),
            "corroborate_resolve_status": corroboration.get("resolve_status"),
            "identity_probe": identity_probe,
        }
    # A non-record version (preprint / accepted manuscript) has a different DOI than the
    # cited work, so identifier match cannot vouch for it: re-check identity at a higher
    # bar before accepting it, to avoid latching onto a different paper with a similar title.
    if (
        content_version in _sources.NON_RECORD_VERSIONS
        and sig not in ("doi", "pmid")
        and not strong_fallback
        and not _sources.preprint_identity_ok(ref, document_identity_text)
    ):
        return {
            "status": "identity_mismatch",
            "method": method,
            "reason": (
                "preprint candidate did not meet the stricter title/author identity bar "
                "required for a non-record version"
            ),
            "corroborate_signal": sig,
            "corroborate_score": score,
            "corroborate_branch": corroboration.get("branch"),
            "corroborate_resolve_status": corroboration.get("resolve_status"),
            "reason_code": "preprint_identity_insufficient",
        }
    provenance_relation = _sources.infer_provenance_relation(
        ref,
        document_identity_text,
        content_version=content_version,
    )
    if (
        decision == "confirmed"
        and (candidate_context or {}).get("context_conflict")
        and sig not in ("doi", "pmid")
        and identity_probe.get("author_ok") is not True
    ):
        decision = "inconclusive"
        identity_probe = dict(identity_probe)
        identity_probe.update({
            "ok": False,
            "decision": "inconclusive",
            "reason_code": "candidate_context_conflict",
            "reason": (
                "conflicting identities were attached to this URL and the cited author "
                "was not confirmed in the document front matter"
            ),
        })
    if strong_fallback and decision != "rejected":
        decision = "confirmed"
        identity_probe = dict(identity_probe)
        if fallback_kind == "doi_anchored_resolved_candidate":
            sig, score = "doi", 1.0
            reason_code = "identity_confirmed_doi_anchored_resolved_candidate"
            reason = (
                "citation DOI, resolved DOI, candidate DOI, and native document "
                "front matter confirmed the resolved candidate title"
            )
        elif fallback_kind == "exact_arxiv_id":
            sig, score = "arxiv_id", 1.0
            reason_code = "identity_confirmed_exact_arxiv_id"
            reason = f"source URL arXiv ID {exact_arxiv_id} exactly matched expected identity"
        elif fallback_kind == "exact_acl_id":
            sig, score = "acl_id", 1.0
            reason_code = "identity_confirmed_exact_acl_id"
            reason = f"ACL Anthology ID {exact_acl_id} exactly matched expected identity"
        elif fallback_kind == "exact_official_curated_document":
            sig, score = "official_curated_document", 1.0
            reason_code = "identity_confirmed_exact_official_curated_document"
            reason = (
                "exact official curated-document route, catalogue metadata, and "
                "front-matter title/institution signal confirmed a layout-split document"
            )
        elif fallback_kind == "cited_landing_pdf":
            sig, score = "cited_landing_pdf", 1.0
            reason_code = "identity_confirmed_cited_landing_pdf"
            reason = (
                "a normal same-origin PDF anchor from the exact cited landing route "
                "and the citation-owned front-matter title confirmed the document"
            )
        else:
            sig, score = "author_copy", 1.0
            reason_code = "identity_confirmed_exact_author_copy"
            reason = (
                "exact candidate DOI, author-owned URL, cited author and title tokens "
                "confirmed a layout-split author copy"
            )
        identity_probe.update({
            "ok": True,
            "decision": "confirmed",
            "reason_code": reason_code,
            "reason": reason,
            "fallback_confirmation": fallback_kind,
        })
    if decision != "confirmed":
        if (
            decision == "inconclusive"
            and explicit_cited_url
            and explicit_cited_url_requires_identity(ref, resolve_result)
        ):
            if not prepared_quality_ok:
                return _quality_rejection(
                    corroborate_signal=sig,
                    corroborate_score=score,
                    identity_probe=identity_probe,
                )
            if corroboration.get("branch") == "resolver_title":
                citation_only_corroboration = {}
                citation_only_sig, citation_only_score = _sources.corroborate(
                    ref, document_identity_text, None, detail=citation_only_corroboration
                )
                if not corroborated(citation_only_sig, citation_only_score):
                    return {
                        "status": "identity_inconclusive",
                        "method": method,
                        "reason": identity_probe.get("reason")
                        or "document front matter did not match the cited source identity",
                        "corroborate_signal": citation_only_sig,
                        "corroborate_score": citation_only_score,
                        "corroborate_branch": citation_only_corroboration.get("branch"),
                        "corroborate_resolve_status": citation_only_corroboration.get("resolve_status"),
                        "identity_probe": identity_probe,
                        "reason_code": identity_probe.get("reason_code"),
                    }
                sig, score = citation_only_sig, citation_only_score
                corroboration = citation_only_corroboration
            origin = origin_for_method(method, resolve_result, candidate_context)
            entry = _store_prepared_fulltext(
                run_dir,
                ref,
                origin,
                verify_text,
                preparation=preparation,
                extraction_flags=extraction_flags,
                extraction_method=extract_method,
                storage_provenance=storage_provenance,
                source_ref=source_ref,
                mapping="cited_url",
                signal=sig,
                score=score,
                identity_status="cited_url_unconfirmed",
                identity_note=(
                    "URL explicitly cited in the manuscript; the retrieved text "
                    "corroborated the cited source broadly, but document front matter "
                    "did not confidently confirm the expected source identity"
                ),
                content_version=content_version,
                provenance_relation=provenance_relation,
            )
            result = {
                "status": "stored",
                "stored_as": entry["stored_as"],
                "method": method,
                "pdf_url": source_ref,
                "corroborate_signal": sig,
                "corroborate_score": score,
                # A stored source records that it was corroborated, but not which
                # check answered nor which title the identity probe compared against.
                # Without those, a wrong attribution cannot be audited afterwards -
                # and inferring the branch from the signal alone gave the wrong answer
                # on the only rows where it mattered.
                "corroborate_branch": corroboration.get("branch"),
                "corroborate_resolve_status": corroboration.get("resolve_status"),
                "identity_reason_code": identity_probe.get("reason_code"),
                "identity_expected_title_source": identity_probe.get("expected_title_source"),
                "identity_probe": identity_probe,
                "identity_status": "cited_url_unconfirmed",
                "document_relation": document_relation,
                "source_preparation": preparation,
            }
            if entry.get("content_version"):
                result["content_version"] = entry["content_version"]
            if extract_method:
                result["extract_method"] = extract_method
            store_resolver_abstract_if_present(run_dir, ref, resolve_result)
            return result
        if _web_search_identity(resolve_result) and decision != "rejected":
            # Institutional / grey-literature report recovered by web search: the
            # resolver already discriminated an authoritative, institution-coherent,
            # title-coherent source. Front matter could not independently confirm the
            # identity (no DOI/PMID), but it did not contradict it either, so the text
            # is stored as externally-corroborated — usable for verification, yet
            # capped in reliability so the claim can never be fully green.
            if not prepared_quality_ok:
                return _quality_rejection(
                    corroborate_signal=sig,
                    corroborate_score=score,
                    identity_probe=identity_probe,
                )
            if corroboration.get("branch") == "resolver_title":
                citation_only_corroboration = {}
                citation_only_sig, citation_only_score = _sources.corroborate(
                    ref, document_identity_text, None, detail=citation_only_corroboration
                )
                if not corroborated(citation_only_sig, citation_only_score):
                    return {
                        "status": "identity_inconclusive",
                        "method": method,
                        "reason": identity_probe.get("reason")
                        or "document front matter did not match the cited source identity",
                        "corroborate_signal": citation_only_sig,
                        "corroborate_score": citation_only_score,
                        "corroborate_branch": citation_only_corroboration.get("branch"),
                        "corroborate_resolve_status": citation_only_corroboration.get("resolve_status"),
                        "identity_probe": identity_probe,
                        "reason_code": identity_probe.get("reason_code"),
                    }
                sig, score = citation_only_sig, citation_only_score
                corroboration = citation_only_corroboration
            origin = origin_for_method(method, resolve_result, candidate_context)
            entry = _store_prepared_fulltext(
                run_dir,
                ref,
                origin,
                verify_text,
                preparation=preparation,
                extraction_flags=extraction_flags,
                extraction_method=extract_method,
                storage_provenance=storage_provenance,
                source_ref=source_ref,
                mapping="web",
                signal=sig,
                score=score,
                identity_status="externally_corroborated_text",
                identity_note=(
                    "recovered from an authoritative web source matching the cited "
                    "institution and title; usable for claim verification, but its "
                    "identity was established by web search of a non-scholarly, "
                    "link-less citation, not by a strong identifier or scholarly index"
                ),
                content_version=content_version,
                provenance_relation=provenance_relation,
            )
            result = {
                "status": "stored",
                "stored_as": entry["stored_as"],
                "method": method,
                "pdf_url": source_ref,
                "corroborate_signal": sig,
                "corroborate_score": score,
                "corroborate_branch": corroboration.get("branch"),
                "corroborate_resolve_status": corroboration.get("resolve_status"),
                "identity_reason_code": identity_probe.get("reason_code"),
                "identity_expected_title_source": identity_probe.get("expected_title_source"),
                "identity_probe": identity_probe,
                "identity_status": "externally_corroborated_text",
                "document_relation": document_relation,
                "source_preparation": preparation,
            }
            if entry.get("content_version"):
                result["content_version"] = entry["content_version"]
            if extract_method:
                result["extract_method"] = extract_method
            store_resolver_abstract_if_present(run_dir, ref, resolve_result)
            return result
        return {
            "status": (
                "identity_mismatch" if decision == "rejected" else "identity_inconclusive"
            ),
            "method": method,
            "reason": identity_probe.get("reason")
            or "document front matter did not match the cited source identity",
            "corroborate_signal": sig,
            "corroborate_score": score,
            "corroborate_branch": corroboration.get("branch"),
            "corroborate_resolve_status": corroboration.get("resolve_status"),
            "identity_probe": identity_probe,
            "reason_code": identity_probe.get("reason_code"),
        }
    origin = origin_for_method(method, resolve_result, candidate_context)
    mapping = (
        "cited_url" if explicit_cited_url else
        "cited_landing_pdf" if fallback_kind == "cited_landing_pdf" else
        ("auto" if sig in ("doi", "pmid") else "tokens")
    )
    identity_status = None
    identity_note = None
    if explicit_cited_url:
        identity_status = "cited_url_reachable"
        identity_note = (
            "URL explicitly cited in the manuscript; the retrieved text also "
            "corroborated the cited source identity in document front matter"
        )
    elif fallback_kind == "exact_arxiv_id":
        identity_status = "exact_arxiv_id_confirmed"
        identity_note = identity_probe.get("reason")
    elif fallback_kind == "doi_anchored_resolved_candidate":
        identity_status = "doi_anchored_resolved_candidate_confirmed"
        identity_note = identity_probe.get("reason")
    elif _trusted_springer_jats_front_confirmed(
        ref,
        resolve_result,
        candidate_context,
        source_ref,
        identity_extract_text,
        identity_extract_method,
        extract_method,
        method,
        decision,
    ):
        identity_status = "trusted_springer_jats_front_confirmed"
        identity_note = identity_probe.get("reason")
    elif fallback_kind == "exact_acl_id":
        identity_status = "exact_acl_id_confirmed"
        identity_note = identity_probe.get("reason")
    elif fallback_kind == "exact_author_copy":
        identity_status = "exact_author_copy_confirmed"
        identity_note = identity_probe.get("reason")
    elif fallback_kind == "exact_official_curated_document":
        identity_status = "exact_official_curated_document_confirmed"
        identity_note = identity_probe.get("reason")
    elif fallback_kind == "cited_landing_pdf":
        identity_status = "cited_landing_pdf_confirmed"
        identity_note = identity_probe.get("reason")
    elif _web_search_identity(resolve_result):
        # Web-recovered institutional report: even when front matter corroborated
        # the identity, the resolution route was a web search of a non-scholarly,
        # link-less citation — cap it as externally-corroborated so it never greens.
        identity_status = "externally_corroborated_text"
        identity_note = (
            "recovered from an authoritative web source matching the cited "
            "institution and title; identity established by web search of a "
            "non-scholarly, link-less citation, not by a strong identifier"
        )
    if not prepared_quality_ok:
        return _quality_rejection(
            corroborate_signal=sig,
            corroborate_score=score,
            identity_probe=identity_probe,
        )
    entry = _store_prepared_fulltext(
        run_dir,
        ref,
        origin,
        verify_text,
        preparation=preparation,
        extraction_flags=extraction_flags,
        extraction_method=extract_method,
        storage_provenance=storage_provenance,
        source_ref=source_ref,
        mapping=mapping,
        signal=sig,
        score=score,
        identity_status=identity_status,
        identity_note=identity_note,
        content_version=content_version,
        provenance_relation=provenance_relation,
    )
    store_resolver_abstract_if_present(run_dir, ref, resolve_result)
    result = {
        "status": "stored",
        "stored_as": entry["stored_as"],
        "method": method,
        "pdf_url": source_ref,
        "corroborate_signal": sig,
        "corroborate_score": score,
        "corroborate_branch": corroboration.get("branch"),
        "corroborate_resolve_status": corroboration.get("resolve_status"),
        "identity_reason_code": identity_probe.get("reason_code"),
        "identity_expected_title_source": identity_probe.get("expected_title_source"),
        "identity_probe": identity_probe,
        "document_relation": document_relation,
        "source_preparation": preparation,
    }
    if identity_status:
        result["identity_status"] = identity_status
    if entry.get("content_version"):
        result["content_version"] = entry["content_version"]
    if extract_method:
        result["extract_method"] = extract_method
    if identity_extract_method:
        result["identity_extract_method"] = identity_extract_method
        result["stored_extract_method"] = extract_method or method
    return result
