# core/resolve/manuscript_identity.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve the uploaded manuscript's title from manuscript-owned identifiers.

The caller supplies the ordinary reference resolver and document identity probe.
This module only adapts persisted Parse evidence to those existing mechanisms;
it never searches by a title unless the manuscript first declared a strong
identifier of its own.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any


_IDENTIFIER_PRIORITY = ("doi", "pmid", "arxiv_id", "isbn", "url")
_SUPPORTED_SCHEMES = frozenset(_IDENTIFIER_PRIORITY)
_EDITORIAL_STATUS_PREFIX_RE = re.compile(
    r"^\s*retracted\s+article\s*[:\-\u2013\u2014]\s*",
    re.IGNORECASE,
)


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _title_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _identity_comparison_title(value: object) -> str:
    """Remove a recognised leading editorial status label for identity checks."""
    title = unicodedata.normalize("NFKC", str(value or ""))
    return _EDITORIAL_STATUS_PREFIX_RE.sub("", title, count=1)


def _select_identifier(identity: Mapping[str, Any]) -> dict[str, str] | None:
    identifiers = identity.get("identifiers")
    if not isinstance(identifiers, list):
        return None
    for scheme in _IDENTIFIER_PRIORITY:
        for identifier in identifiers:
            if not isinstance(identifier, Mapping):
                continue
            value = _clean_text(identifier.get("value"))
            if identifier.get("scheme") == scheme and value:
                return {"scheme": scheme, "value": value}
    return None


def _resolver_reference(selected: Mapping[str, str]) -> dict[str, Any]:
    scheme, value = selected["scheme"], selected["value"]
    source_kind = "book_like" if scheme == "isbn" else "article_like"
    ref: dict[str, Any] = {
        "id": "__manuscript_identity__",
        "ref_number": None,
        # Keep the ordinary resolver on its identifier-only path.  Supplying
        # the local candidate here would allow its hard-identifier fallback to
        # search by title and then help confirm the same candidate.
        "raw_entry": "",
        "title": None,
        "source_type": "book" if scheme == "isbn" else "article",
        "source_kind": source_kind,
        "source_type_confidence": "high",
        "source_type_evidence": ["manuscript_declared_identifier"],
        "indexability": "high",
    }
    if scheme == "arxiv_id":
        # The ordinary resolver already treats an arXiv DOI as a strong direct
        # identifier and canonicalises away any version suffix.
        ref["doi"] = f"10.48550/arXiv.{value}"
    else:
        ref[scheme] = value
    return ref


def _resolved_identifier(
    result: Mapping[str, Any], selected: Mapping[str, str], *,
    fallback_to_selected: bool,
) -> dict[str, str] | None:
    aliases = {"arxiv": "arxiv_id", "canonical_url": "url"}

    def candidate(scheme: object, value: object) -> dict[str, str] | None:
        normalised_scheme = aliases.get(str(scheme or "").lower(), str(scheme or "").lower())
        normalised_value = _clean_text(value)
        if normalised_scheme in _SUPPORTED_SCHEMES and normalised_value:
            return {"scheme": normalised_scheme, "value": normalised_value}
        return None

    summary = result.get("resolved_identifier")
    if isinstance(summary, Mapping):
        found = candidate(summary.get("type") or summary.get("scheme"), summary.get("value"))
        if found:
            return found
    identifiers = result.get("identifiers")
    if isinstance(identifiers, Mapping):
        for scheme in _IDENTIFIER_PRIORITY:
            found = candidate(scheme, identifiers.get(scheme))
            if found:
                return found
    found = candidate("doi", result.get("doi"))
    return found or (dict(selected) if fallback_to_selected else None)


def _local_fallback(
    identity: Mapping[str, Any], selected: Mapping[str, str], *,
    resolution_status: str, resolved_identifier: Mapping[str, str] | None = None,
    resolved_title: str | None = None, resolved_via: str | None = None,
    reason: str | None = None, attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    local_title = _clean_text(identity.get("local_title"))
    local_status = str(identity.get("local_status") or "unknown")
    local_method = str(identity.get("local_method") or "none")
    if local_status not in {"validated_local", "inferred_local"} or not local_title:
        local_title, local_status, local_method = None, "unknown", "none"
    return {
        "selected_identifier": dict(selected),
        "resolution_status": resolution_status,
        "resolved_identifier": dict(resolved_identifier) if resolved_identifier else None,
        "resolved_title": resolved_title,
        "resolved_via": resolved_via,
        "final_title": local_title,
        "final_status": local_status,
        "final_method": local_method,
        "reason": reason,
        "attempts": attempts,
    }


def resolve_manuscript_identity(
    identity: Mapping[str, Any],
    manuscript_text: str,
    *,
    resolve_ref: Callable[[dict[str, Any]], dict[str, Any]],
    probe_document: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    """Return a repository resolution update, or ``None`` when no own ID exists."""
    selected = _select_identifier(identity)
    if selected is None:
        return None
    local_title = _clean_text(identity.get("local_title"))
    ref = _resolver_reference(selected)
    try:
        result = resolve_ref(ref)
    except Exception as exc:
        return _local_fallback(
            identity, selected, resolution_status="error",
            reason=f"resolver exception: {type(exc).__name__}",
        )
    if not isinstance(result, Mapping):
        return _local_fallback(
            identity, selected, resolution_status="error",
            reason="resolver returned an invalid manuscript identity result",
        )
    attempts = result.get("attempts")
    if not isinstance(attempts, list):
        return _local_fallback(
            identity, selected, resolution_status="error",
            reason="resolver returned manuscript identity attempts in an invalid shape",
        )

    status = str(result.get("status") or "unresolved")
    via = _clean_text(result.get("via"))
    reason = _clean_text(result.get("reason"))
    candidate_title = _clean_text(result.get("matched_title"))
    resolved_title = candidate_title if status in {"resolved", "identifier_mismatch"} else None
    resolved_identifier = _resolved_identifier(
        result,
        selected,
        fallback_to_selected=status in {"resolved", "identifier_mismatch"},
    )
    if status == "not_found":
        return _local_fallback(
            identity, selected, resolution_status="not_found",
            resolved_identifier=resolved_identifier, resolved_title=resolved_title,
            resolved_via=via, reason=reason, attempts=attempts,
        )
    if status == "identifier_mismatch":
        return {
            **_local_fallback(
                identity, selected, resolution_status="conflict",
                resolved_identifier=resolved_identifier, resolved_title=resolved_title,
                resolved_via=via, reason=reason or "declared identifier conflicts with resolved metadata",
                attempts=attempts,
            ),
            "final_title": None,
            "final_status": "unknown",
            "final_method": "identity_conflict",
        }
    if status != "resolved" or not resolved_title:
        return _local_fallback(
            identity, selected, resolution_status="unresolved",
            resolved_identifier=resolved_identifier, resolved_title=resolved_title,
            resolved_via=via,
            reason=reason or "declared identifier did not yield a resolved title",
            attempts=attempts,
        )

    try:
        probe = probe_document(_identity_comparison_title(resolved_title), manuscript_text)
    except Exception as exc:
        return _local_fallback(
            identity, selected, resolution_status="error",
            resolved_identifier=resolved_identifier, resolved_title=resolved_title,
            resolved_via=via,
            reason=f"front-matter identity probe exception: {type(exc).__name__}",
            attempts=attempts,
        )
    local_comparison_title = _identity_comparison_title(local_title)
    resolved_comparison_title = _identity_comparison_title(resolved_title)
    local_agrees = bool(
        local_title
        and _title_key(local_comparison_title)
        and _title_key(local_comparison_title) == _title_key(resolved_comparison_title)
    )
    probe_ok = isinstance(probe, Mapping) and probe.get("ok") is True
    if probe_ok or local_agrees:
        if probe_ok:
            final_method = "resolved_identifier_front_matter"
            final_reason = _clean_text(probe.get("reason"))
        else:
            final_method = "resolved_identifier_local_title"
            final_reason = "identifier-resolved title agrees with independent local title evidence"
        return {
            "selected_identifier": dict(selected),
            "resolution_status": "resolved",
            "resolved_identifier": resolved_identifier,
            "resolved_title": resolved_title,
            "resolved_via": via,
            "final_title": resolved_title,
            "final_status": "validated_identifier",
            "final_method": final_method,
            "reason": final_reason or "identifier-resolved title found in manuscript front matter",
            "attempts": attempts,
        }

    probe_reason = (
        _clean_text(probe.get("reason"))
        if isinstance(probe, Mapping) else None
    )
    return {
        **_local_fallback(
            identity, selected, resolution_status="conflict",
            resolved_identifier=resolved_identifier, resolved_title=resolved_title,
            resolved_via=via,
            reason=probe_reason or "resolved title was not found in manuscript front matter",
            attempts=attempts,
        ),
        "final_title": None,
        "final_status": "unknown",
        "final_method": "identity_conflict",
    }
