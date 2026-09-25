#!/usr/bin/env python3
# core/fetch/refdata.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Reference-data extraction helpers — normalise identifiers, classify URLs, and
surface the strongest resolved identity fields for fetch decisions."""

from __future__ import annotations

import html
import re
import urllib.parse

try:
    from core.fetch.storage import fetch_store as _fetch_store
    from core.resolve import sources as _sources
except ImportError:
    from storage import fetch_store as _fetch_store
    from core.resolve import sources as _sources


_DOI_SHAPE_RE = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
_DOI_RESOLVER_HOSTS = frozenset(
    {"doi.org", "www.doi.org", "dx.doi.org", "www.dx.doi.org"}
)
_ESCAPED_INLINE_BIBLIOGRAPHIC_TAG_RE = re.compile(
    r"&lt;(?P<tag>i|em|b|strong|sub|sup|span)\s*&gt;"
    r"(?P<content>.*?)&lt;/(?P=tag)\s*&gt;",
    re.IGNORECASE | re.DOTALL,
)


def _valid_doi_shape(doi: str | None) -> bool:
    """Accept a DOI-shaped value only when structural delimiters are complete."""
    if not doi or not _DOI_SHAPE_RE.fullmatch(doi):
        return False
    if doi.endswith(("/", ":", "(", "<")) or "≤" in doi or "≥" in doi:
        return False
    for opening, closing in (("(", ")"), ("<", ">")):
        depth = 0
        for char in doi:
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth < 0:
                    return False
        if depth:
            return False
    return True


# ---------------------------------------------------------------------------
# Resolve-result data extraction
# ---------------------------------------------------------------------------

def _confidence_number(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _metadata_link_items(resolve_result: dict) -> list[dict]:
    out = []
    by_url = {}
    items = list(resolve_result.get("fulltext_links") or [])
    items.extend(resolve_result.get("auxiliary_fulltext_links") or [])
    for item in items:
        if isinstance(item, dict) and item.get("url"):
            url = item["url"]
            row = dict(item)
            if url not in by_url:
                by_url[url] = row
                out.append(row)
                continue
            current = by_url[url]
            contexts = []
            for context in (
                current.get("identity_context"),
                *(current.get("identity_contexts") or []),
                row.get("identity_context"),
                *(row.get("identity_contexts") or []),
            ):
                if isinstance(context, dict) and context not in contexts:
                    contexts.append(dict(context))
            if contexts:
                contexts.sort(
                    key=lambda ctx: (
                        not bool(ctx.get("canonical_host")),
                        -_confidence_number(ctx.get("source_confidence")),
                        _title_key(ctx.get("title")),
                    )
                )
                current["identity_context"] = contexts[0]
                if len(contexts) > 1:
                    current["identity_contexts"] = contexts
                    titles = {_title_key(ctx.get("title")) for ctx in contexts if ctx.get("title")}
                    current["identity_context_conflict"] = len(titles) > 1
            provenance = list(current.get("provenance") or [])
            for provider in (
                current.get("discovered_via"),
                row.get("discovered_via"),
                *(row.get("provenance") or []),
            ):
                if provider and provider not in provenance:
                    provenance.append(provider)
            if provenance:
                current["provenance"] = provenance
    return out


def _metadata_urls(resolve_result: dict) -> list[str]:
    return [item["url"] for item in _metadata_link_items(resolve_result)]


def _extract_doi_from_resolve(resolve_result: dict | None) -> str | None:
    """Recover a DOI from resolve metadata when the reference entry had none."""
    if not resolve_result:
        return None
    for item in _metadata_link_items(resolve_result):
        url = str(item.get("url") or "").strip()
        try:
            parsed = urllib.parse.urlsplit(url)
            host = (parsed.hostname or "").lower()
        except ValueError:
            parsed = None
            host = ""
        if (
            parsed is not None
            and parsed.scheme.lower() in {"http", "https"}
            and host in _DOI_RESOLVER_HOSTS
        ):
            doi = _normalize_doi(urllib.parse.unquote(parsed.path.lstrip("/")))
            if _valid_doi_shape(doi):
                return doi
        if url.startswith("10."):
            doi = _normalize_doi(url)
            if _valid_doi_shape(doi):
                return doi
    return None


def _resolved_identifiers(resolve_result: dict | None) -> dict[str, str]:
    """Return identifiers explicitly confirmed by the resolve result.

    Europe PMC may discover a PMID/PMCID while resolving a DOI. Preserve only
    identifiers serialized on the selected result (or its selected resolved
    identifier); attempts are audit history and may describe another record.
    """
    if not isinstance(resolve_result, dict):
        return {}
    values: dict[str, str] = {}

    def add(payload: dict):
        nested = payload.get("identifiers")
        sources = (payload, nested) if isinstance(nested, dict) else (payload,)
        for source in sources:
            for key in ("doi", "pmid", "pmcid"):
                value = str(source.get(key) or "").strip()
                if not value or key in values:
                    continue
                values[key] = _normalize_doi(value) if key == "doi" else value

    resolved = resolve_result.get("resolved_identifier")
    if isinstance(resolved, dict):
        key = str(resolved.get("type") or "").strip().lower()
        value = str(resolved.get("value") or "").strip()
        if key in {"doi", "pmid", "pmcid"} and value:
            values[key] = _normalize_doi(value) if key == "doi" else value
    add(resolve_result)
    return {key: value for key, value in values.items() if value}


def _validated_recovered_doi(resolve_result: dict | None) -> str | None:
    """Return a correction DOI only after explicit resolve-side validation.

    The raw citation remains the audit record of what was cited.  Fetch may
    substitute its DOI only for a separately admitted recovery, never for a
    merely plausible fallback candidate.
    """
    if not isinstance(resolve_result, dict):
        return None
    if resolve_result.get("identifier_error_recovered") is not True:
        return None
    identifier = resolve_result.get("resolved_identifier")
    if not isinstance(identifier, dict):
        return None
    if str(identifier.get("type") or "").strip().lower() != "doi":
        return None
    if not identifier.get("validated_via"):
        return None
    doi = _normalize_doi(identifier.get("value"))
    return doi if _valid_doi_shape(doi) else None


def _extract_url_from_resolve(resolve_result: dict | None) -> str | None:
    """Recover a candidate fetch URL from resolve metadata."""
    if not resolve_result:
        return None
    for item in _metadata_link_items(resolve_result):
        url = item.get("url") or ""
        if url.startswith("http"):
            return url
    return None


def _extract_year_from_resolve(resolve_result: dict | None) -> int | None:
    if not resolve_result:
        return None
    candidates = []
    evidence = resolve_result.get("evidence_profile") or {}
    metadata_match = evidence.get("metadata_match") or {}
    best_candidate = evidence.get("best_candidate") or {}
    best_metadata = best_candidate.get("metadata_match") or {}
    candidates.extend(
        [
            metadata_match.get("matched_year"),
            best_metadata.get("matched_year"),
        ]
    )
    for attempt in resolve_result.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        match = attempt.get("metadata_match") or {}
        candidates.append(match.get("matched_year"))
    for year in candidates:
        try:
            if year:
                return int(year)
        except Exception:
            continue
    return None


def _title_key(text: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _bibliographic_title_key(text: str | None) -> str:
    """Normalize markup only while comparing bibliographic title spellings."""
    rendered = str(text or "")
    while True:
        without_markup = _ESCAPED_INLINE_BIBLIOGRAPHIC_TAG_RE.sub(
            lambda match: match.group("content"), rendered,
        )
        if without_markup == rendered:
            break
        rendered = without_markup
    rendered = html.unescape(rendered)
    rendered = rendered.replace("<", " angleopen ").replace(">", " angleclose ")
    return _title_key(rendered)


def _extract_title_from_resolve(ref: dict | None, resolve_result: dict | None) -> str | None:
    if not resolve_result:
        return None
    matched = str(resolve_result.get("matched_title") or "").strip()
    parsed = str((ref or {}).get("title") or "").strip()
    if str(resolve_result.get("status") or "").strip().lower() == "unverified":
        return parsed or None
    if not matched:
        return None
    matched_tokens = len(_sources._tokens(matched))
    parsed_tokens = len(_sources._tokens(parsed))
    if parsed:
        parsed_key = _bibliographic_title_key(parsed)
        matched_key = _bibliographic_title_key(matched)
        if (
            parsed_tokens >= 3
            and parsed_key
            and matched_key
            and parsed_key == matched_key
            and _sources._has_spaced_hyphen_artifact(parsed)
        ):
            return matched
        if parsed_tokens >= 3 and parsed_key and matched_key and parsed_key in matched_key:
            return parsed
        if parsed_tokens < 3 and matched_tokens >= 3:
            return matched
    return matched


def _effective_fetch_ref(ref: dict, resolve_result: dict | None) -> dict:
    """Overlay strong identifiers discovered during resolve onto the parsed ref.

    The parsed citation may omit a DOI/URL that the resolver later validated.
    Fetch providers should operate on the resolved work identity, not only the
    raw citation fields.
    """
    effective = dict(ref or {})
    identifiers = _resolved_identifiers(resolve_result)
    recovered_doi = _validated_recovered_doi(resolve_result)
    if recovered_doi:
        effective["doi"] = recovered_doi
    elif not effective.get("doi"):
        doi = identifiers.get("doi") or _extract_doi_from_resolve(resolve_result)
        if doi:
            effective["doi"] = doi
    for key in ("pmid", "pmcid"):
        if not effective.get(key) and identifiers.get(key):
            effective[key] = identifiers[key]
    if not effective.get("url"):
        url = _extract_url_from_resolve(resolve_result)
        if url:
            effective["url"] = url
    if not effective.get("year"):
        year = _extract_year_from_resolve(resolve_result)
        if year:
            effective["year"] = year
    title = _extract_title_from_resolve(ref, resolve_result)
    if title:
        effective["title"] = title
    return effective


# ---------------------------------------------------------------------------
# URL / identifier utilities
# ---------------------------------------------------------------------------

def _normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    text = str(doi).strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
    return text.strip().rstrip(".,;")


def _looks_pdf_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    path = (parsed.path or "").lower()
    if path.endswith(".pdf"):
        return True
    segments = [s for s in path.split("/") if s]
    if "pdf" in segments:
        return True
    if parsed.query.lower().endswith("=pdf"):
        return True
    return False


def _is_doi_url(url: str | None) -> bool:
    if not url:
        return False
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    return host in {"doi.org", "dx.doi.org"}


def _kind_from_url(url: str, content_type: str | None = None) -> str:
    if content_type and "pdf" in content_type.lower():
        return "pdf"
    return "pdf" if _looks_pdf_url(url) else "landing"


def _origin_for_method(method: str, resolve_result: dict) -> str:
    return _fetch_store.origin_for_method(method, resolve_result)


def _host(url: str | None) -> str:
    if not url:
        return ""
    host = (urllib.parse.urlparse(url).netloc or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":", 1)[0]
