# core/gui/guided_fetch_viewmodel.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure presentation model for the optional guided Fetch window."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from string import Formatter
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse


_STATUS = {
    "fulltext": {
        "label": "Full text", "color": "green", "marker": "✓",
        "tooltip": "Full text was acquired for this reference.",
    },
    "abstract": {
        "label": "Abstract", "color": "yellow", "marker": "●",
        "tooltip": "An abstract was acquired; full text was not recorded.",
    },
}
_DEFAULT_SEARCH_URL = "https://www.google.com/search?q={query}"
_DEFAULT_SEARCH_QUERY = "{raw_entry}"
_SEARCH_FIELDS = frozenset({
    "raw_entry", "title", "doi", "pmid", "isbn", "year", "ay_surname", "source_type",
})


@dataclass(frozen=True)
class SourceRow:
    ref_id: str
    ref_number: int | None
    title: str | None
    status: str
    status_label: str
    status_color: str
    status_marker: str
    status_tooltip: str
    suspected_fabricated: bool
    fabrication_reason: str | None
    retracted: bool
    bibliographic_concern: dict[str, Any] | None
    bibliographic_review_labels: tuple[dict[str, Any], ...]
    risk_label: str
    risk_tooltip: str
    review_label: str
    review_tooltip: str


class GuidedFetchViewModel:
    """Render audit-backed inventory data without mutating run state."""

    def __init__(self, inventory: dict[str, Any], *, env: dict[str, str] | None = None):
        references = inventory.get("references") if isinstance(inventory, dict) else None
        if not isinstance(references, list):
            raise ValueError("guided Fetch inventory requires references")
        self.inventory = inventory
        self._entries = {
            entry["ref_id"]: entry
            for entry in references
            if isinstance(entry, dict) and isinstance(entry.get("ref_id"), str)
        }
        environment = os.environ if env is None else env
        self._search_url_template = _validate_template(
            environment.get("CITATION_VERIFIER_GUIDED_SEARCH_URL", _DEFAULT_SEARCH_URL),
            allowed_fields={"query"}, required_fields={"query"}, label="search URL",
        )
        if not _is_safe_http_url(_render_template(self._search_url_template, {"query": "query"})):
            raise ValueError("guided Fetch search URL must be a credential-free HTTP(S) URL with a hostname")
        self._search_query_template = _validate_template(
            environment.get("CITATION_VERIFIER_GUIDED_SEARCH_QUERY", _DEFAULT_SEARCH_QUERY),
            allowed_fields=_SEARCH_FIELDS, required_fields=None, label="search query",
        )

    def rows(self) -> list[SourceRow]:
        return [self.row(ref_id) for ref_id in self._entries]

    def row(self, ref_id: str) -> SourceRow:
        entry = self.entry(ref_id)
        parsed = _mapping(entry.get("parsed"))
        fetch = _mapping(entry.get("fetch"))
        tier = fetch.get("tier")
        status = tier if tier in {"fulltext", "abstract"} else "none"
        resolve = _mapping(entry.get("resolve"))
        retracted = resolve.get("retracted") is True
        suspicion = _mapping(entry.get("fabrication_suspicion"))
        concern = _mapping_or_none(entry.get("bibliographic_concern"))
        review_labels = tuple(
            item for item in entry.get("bibliographic_review_labels") or []
            if isinstance(item, dict)
        )
        risk_label, risk_tooltip = _risk_presentation(
            concern, bool(suspicion.get("suspected")),
            _string_or_none(suspicion.get("reason")),
            retracted=retracted,
        )
        review_label, review_tooltip = _review_presentation(review_labels)
        style = (
            _STATUS[status]
            if status != "none"
            else _no_text_presentation(entry, fetch, concern, review_labels)
        )
        return SourceRow(
            ref_id=ref_id,
            ref_number=_int_or_none(entry.get("ref_number")),
            title=_string_or_none(parsed.get("title")),
            status=status,
            status_label=style["label"],
            status_color=style["color"],
            status_marker=style["marker"],
            status_tooltip=style["tooltip"],
            suspected_fabricated=bool(suspicion.get("suspected")),
            fabrication_reason=_string_or_none(suspicion.get("reason")),
            retracted=retracted,
            bibliographic_concern=concern,
            bibliographic_review_labels=review_labels,
            risk_label=risk_label,
            risk_tooltip=risk_tooltip,
            review_label=review_label,
            review_tooltip=review_tooltip,
        )

    def entry(self, ref_id: str) -> dict[str, Any]:
        try:
            return self._entries[ref_id]
        except KeyError as exc:
            raise ValueError("unknown guided Fetch reference") from exc

    def detail_tabs(self, ref_id: str) -> dict[str, Any]:
        """Return the fixed detail-tab payloads; never infer missing facts."""
        entry = self.entry(ref_id)
        fetch = _mapping(entry.get("fetch"))
        source = _mapping(fetch.get("best_source"))
        preview = fetch.get("preview_path")
        return {
            "Parsed": _mapping(entry.get("parsed")),
            "Resolved": _mapping(entry.get("resolve")),
            "Acquired": {
                "tier": fetch.get("tier"),
                "sources": list(fetch.get("sources") or []),
                "attempts": list(fetch.get("attempts") or []),
                "pending_tasks": list(fetch.get("pending_tasks") or []),
                "best_source": source,
            },
            "Preview": {"path": self.preview_path(ref_id)},
        }

    def detail_panels(self, ref_id: str) -> dict[str, dict[str, Any]]:
        """Return concise, presentation-neutral details and their audit payloads.

        The raw values intentionally remain available unchanged in ``raw``.  The
        curated rows are only a reading aid; they do not add or infer evidence.
        """
        raw = self.detail_tabs(ref_id)
        parsed = raw["Parsed"]
        resolved = raw["Resolved"]
        acquired = raw["Acquired"]
        fetch = _mapping(self.entry(ref_id).get("fetch"))
        identifier = _mapping(resolved.get("resolved_identifier"))
        pending = acquired["pending_tasks"]
        sources = acquired["sources"]
        attempts = acquired["attempts"]
        return {
            "Parsed": _detail_panel(
                "Citation received",
                "These are the bibliographic details Callimachus received for this reference.",
                _rows((
                    ("Title", parsed.get("title")), ("DOI", parsed.get("doi")),
                    ("Publication year", parsed.get("year")), ("PMID", parsed.get("pmid")),
                    ("ISBN", parsed.get("isbn")), ("Source link", parsed.get("url")),
                )), raw["Parsed"],
            ),
            "Resolved": _detail_panel(
                "Bibliographic lookup",
                _resolved_message(resolved),
                _rows((
                    ("Lookup result", _resolution_status_label(resolved.get("status"))),
                    ("Lookup route", resolved.get("via")),
                    ("Resolved identifier", _identifier_text(identifier)),
                    ("Matched title", resolved.get("matched_title")),
                    ("Resolved link", resolved.get("url")),
                    ("Full-text links recorded", len(resolved.get("fulltext_links") or [])),
                    ("Search attempts recorded", len(resolved.get("attempts") or [])),
                    ("Recorded explanation", resolved.get("reason")),
                )), raw["Resolved"],
            ),
            "Acquired": _detail_panel(
                "Source acquisition",
                _acquisition_message(fetch, pending),
                _rows((
                    ("Available text", _tier_label(acquired.get("tier"))),
                    ("Sources recorded", len(sources)),
                    ("Fetch attempts recorded", len(attempts)),
                    ("Pending actions", len(pending)),
                    ("Best source", _source_label(acquired.get("best_source"))),
                )), raw["Acquired"],
            ),
            "Preview": _detail_panel(
                "Source preview",
                "A text preview is shown below when an acquired source is available.",
                _rows((("Preview file", raw["Preview"].get("path")),)), raw["Preview"],
            ),
        }

    def can_submit_source(self, ref_id: str) -> bool:
        """Return whether Fetch currently owns a pending task for this source."""
        fetch = _mapping(self.entry(ref_id).get("fetch"))
        return any(
            isinstance(task, dict) and task.get("task_kind") in {"fetch", "browser_challenge"}
            for task in fetch.get("pending_tasks") or []
        )

    def identity_review(self, ref_id: str) -> dict[str, Any] | None:
        """Return the one pending, safe-to-display identity review for a reference."""
        fetch = _mapping(self.entry(ref_id).get("fetch"))
        reviews = [
            task for task in fetch.get("pending_tasks") or []
            if isinstance(task, dict)
            and task.get("task_kind") == "source_identity_attestation"
        ]
        if not reviews:
            return None
        if len(reviews) != 1:
            raise ValueError("guided Fetch reference has multiple pending identity reviews")
        task = reviews[0]
        target = task.get("target_sha256")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id or not isinstance(target, str) or not target:
            raise ValueError("guided Fetch identity review has no closed task target")
        source = _mapping(task.get("source_identity"))
        resolve = _mapping(task.get("resolve_identity"))
        return {
            "task_id": task_id,
            "target_sha256": target,
            "source_text_id": task.get("source_text_id"),
            "source_tier": task.get("source_tier"),
            "cited_title": _mapping(task.get("reference")).get("title"),
            "resolved_title": resolve.get("matched_title"),
            "resolved_identifier": _identifier_text(_mapping(resolve.get("resolved_identifier"))),
            "source_identity": {
                key: source.get(key)
                for key in ("identity_key", "origin", "mapping", "match_signal", "identity_status")
                if key in source
            },
        }

    def has_pending_identity_reviews(self) -> bool:
        """Return whether any reference still requires an identity decision."""
        return any(
            self.identity_review(ref_id) is not None
            for ref_id in self._entries
        )

    def rejected_source_guidance(self, ref_id: str) -> str | None:
        """Explain a reopened Fetch answer without exposing its raw traceback."""
        fetch = _mapping(self.entry(ref_id).get("fetch"))
        for task in fetch.get("pending_tasks") or []:
            if not isinstance(task, dict):
                continue
            error = _mapping(task.get("last_error"))
            if error.get("stage") not in {"fetch_answer_ingest", "browser_challenge_ingest"}:
                continue
            message = str(error.get("message") or "").casefold()
            if "challenge_or_login_page" in message:
                reason = "The previous capture was a login or browser-challenge page."
            elif "identity_mismatch" in message:
                reason = "The previous file conflicted with the cited work's identity."
            elif "insufficient_identity" in message:
                reason = "The previous file did not establish full text for the cited work."
            elif "unreadable" in message:
                reason = "The previous file could not be admitted as readable full text."
            else:
                reason = "The previous source answer was rejected."
            return (
                f"{reason} Choose another file, use Abstract only for an authentic abstract, "
                "or leave this source unresolved and select Proceed / skip remaining. "
                "The rejected answer remains in the audit history."
            )
        return None

    def action_guidance(self, ref_id: str) -> str:
        """Explain the permitted operator action without changing task state."""
        row = self.row(ref_id)
        if row.retracted:
            return (
                "This cited work is recorded as retracted. Capture only the exact cited work "
                "if needed for the audit; do not substitute another source. The retraction "
                "will remain a material finding in the final report."
            )
        review_notice = (
            f" Review labels: {row.review_label}. They are informational only and not proof "
            "of fabrication."
            if row.review_label else ""
        )
        if row.suspected_fabricated or (
            row.bibliographic_concern is not None
            and row.bibliographic_concern.get("level") in {
                "reference_refuted", "high_fabrication_suspicion",
            }
        ):
            reason = row.fabrication_reason or _concern_reason(row.bibliographic_concern)
            return (
                f"Why this is flagged: {reason}. What to do: submit a document only if it is "
                "the exact cited work. What not to do: do not substitute a similar paper, "
                "reconstruct missing text, or invent a source. If you cannot verify the exact "
                f"work, leave it unresolved and use Proceed / skip remaining.{review_notice}"
            )
        if (
            row.bibliographic_concern is not None
            and row.bibliographic_concern.get("level")
            == "elevated_bibliographic_suspicion"
        ):
            return (
                "Elevated bibliographic suspicion is non-diagnostic and not a fabrication "
                "finding. Supply only a captured file for the exact cited work; do not "
                "substitute a similar paper, reconstruct missing text, or invent a source. "
                "If you cannot verify it, leave it pending and use Proceed / skip remaining."
                f"{review_notice}"
            )
        if row.bibliographic_review_labels:
            return (
                f"Review labels: {row.review_label}. They are informational only and not proof "
                "of fabrication. Inspect the recorded evidence, then supply only a captured "
                "file for the exact cited work; otherwise leave it pending and use Proceed / "
                "skip remaining."
            )
        if row.status == "fulltext":
            return (
                "Full text is already available. No action is required unless the acquired "
                "document is visibly the wrong work."
            )
        if self.can_submit_source(ref_id):
            available = "an abstract" if row.status == "abstract" else "no usable text"
            return (
                f"Callimachus currently has {available}. Supply only a captured file for the "
                "exact cited work. If you cannot verify it, leave it pending and use "
                "Proceed / skip remaining; it will remain unavailable for verification."
            )
        return (
            "No Fetch action is available for this reference. Inspect the recorded evidence, "
            "but do not attach a different work or reconstruct missing source text."
        )

    def preview_path(self, ref_id: str) -> str | None:
        """Return only the pre-validated inventory preview path, if readable."""
        path = _mapping(_mapping(self.entry(ref_id).get("fetch"))).get("preview_path")
        if not isinstance(path, str) or not path:
            return None
        candidate = Path(path)
        return str(candidate) if candidate.is_file() else None

    def preferred_link(self, ref_id: str) -> str | None:
        entry = self.entry(ref_id)
        parsed = _mapping(entry.get("parsed"))
        resolved = _mapping(entry.get("resolve"))
        for value in (
            _first_url(resolved.get("fulltext_links")),
            _concrete_url(resolved.get("url")),
            _concrete_url(parsed.get("url")),
        ):
            if value:
                return value
        return None

    def browser_url(self, ref_id: str) -> str:
        """Return a navigable URL even when resolution recorded no link."""
        return self.preferred_link(ref_id) or self._search_url(self.entry(ref_id))

    def _search_url(self, entry: dict[str, Any]) -> str:
        query = _doi_search_query(entry) or _render_query(self._search_query_template, entry)
        url = _render_template(self._search_url_template, {"query": quote_plus(query)})
        if not _is_safe_http_url(url):
            raise ValueError("guided Fetch search URL must be a credential-free HTTP(S) URL with a hostname")
        return url


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _detail_panel(
    headline: str,
    message: str,
    rows: list[tuple[str, str]],
    raw: Any,
) -> dict[str, Any]:
    return {"headline": headline, "message": message, "rows": rows, "raw": raw}


def _rows(values: tuple[tuple[str, Any], ...]) -> list[tuple[str, str]]:
    return [
        (label, _fact_value(value))
        for label, value in values
        if value not in (None, "", [], {})
    ]


def _identifier_text(identifier: dict[str, Any]) -> str | None:
    value = _string_or_none(identifier.get("value"))
    kind = _string_or_none(identifier.get("type"))
    if value is None:
        return None
    return f"{kind.upper()}: {value}" if kind else value


def _resolved_message(resolved: dict[str, Any]) -> str:
    status = _string_or_none(resolved.get("status"))
    route = _string_or_none(resolved.get("via"))
    route_suffix = f" using {route}" if route else ""
    messages = {
        "resolved": f"Callimachus matched the citation to a bibliographic record{route_suffix}.",
        "not_found": (
            "Callimachus completed the recorded bibliographic searches but did not find "
            "a compatible work."
        ),
        "identifier_mismatch": (
            "Callimachus found a record, but its identifier or bibliographic details did "
            "not match the citation."
        ),
        "unresolved": "The recorded searches did not establish a reliable bibliographic match.",
        "unverified": "The recorded lookup could not verify this bibliographic identity.",
    }
    if status in messages:
        return messages[status]
    if status:
        return "Callimachus recorded a bibliographic result; see the facts below."
    return "No bibliographic lookup result was recorded for this reference."


def _resolution_status_label(value: Any) -> str | None:
    return {
        "resolved": "Matched",
        "not_found": "No compatible work found",
        "identifier_mismatch": "Bibliographic mismatch",
        "unresolved": "Not resolved",
        "unverified": "Not verified",
    }.get(value, _string_or_none(value))


def _tier_label(value: Any) -> str | None:
    return {"fulltext": "Full text", "abstract": "Abstract", None: "No usable text"}.get(value)


def _source_label(value: Any) -> str | None:
    source = _mapping(value)
    for key in ("source_ref", "url", "source_text_id", "path"):
        text = _string_or_none(source.get(key))
        if text:
            return text
    return None


def _acquisition_message(fetch: dict[str, Any], pending: list[Any]) -> str:
    tier = _tier_label(fetch.get("tier"))
    if fetch.get("tier") in {"fulltext", "abstract"}:
        return f"Callimachus acquired {tier.lower()} for this reference."
    if pending:
        return (
            "Callimachus has not acquired usable text yet and is awaiting the recorded "
            "Fetch action."
        )
    return "Callimachus did not acquire usable text for this reference."


def _concern_reason(concern: dict[str, Any] | None) -> str:
    if concern is None:
        return "bibliographic checks found a strong conflict"
    conclusion = _string_or_none(concern.get("conclusion"))
    return conclusion or "bibliographic checks found a strong conflict"


def _risk_presentation(
    concern: dict[str, Any] | None,
    suspected: bool,
    reason: str | None,
    *,
    retracted: bool = False,
) -> tuple[str, str]:
    if retracted:
        return (
            "Retracted source",
            "The resolved cited work is recorded as retracted.",
        )
    level = None if concern is None else concern.get("level")
    if level == "reference_refuted":
        return "Reference refuted", _concern_reason(concern)
    if level == "high_fabrication_suspicion" or suspected:
        return "High fabrication suspicion", reason or _concern_reason(concern)
    if level == "elevated_bibliographic_suspicion":
        detail = _concern_reason(concern)
        return (
            "Elevated bibliographic suspicion",
            f"Non-diagnostic; not a fabrication finding. {detail}",
        )
    return "", ""


_REVIEW_LABELS = {
    "incomplete_bibliographic_source": "Incomplete bibliographic source",
    "not_found_after_completed_searches": "Not found after completed bibliographic searches",
    "no_compatible_article_at_cited_coordinates": "No compatible article at cited coordinates",
    "author_list_discrepancy": "Cited author absent from returned metadata",
}


def _review_presentation(labels: tuple[dict[str, Any], ...]) -> tuple[str, str]:
    display = [
        _REVIEW_LABELS[item["code"]]
        for item in labels
        if isinstance(item.get("code"), str) and item["code"] in _REVIEW_LABELS
    ]
    if not display:
        return "", ""
    detail = "; ".join(
        ", ".join(str(value) for value in item.get("providers", []) if isinstance(value, str))
        for item in labels if item.get("providers")
    )
    suffix = f" Recorded evidence: {detail}." if detail else ""
    return "; ".join(display), f"Informational only; not proof of fabrication.{suffix}"


def _no_text_presentation(
    entry: dict[str, Any],
    fetch: dict[str, Any],
    concern: dict[str, Any] | None,
    review_labels: tuple[dict[str, Any], ...],
) -> dict[str, str]:
    """Present recorded no-text facts without changing their audit meaning."""
    resolved = _mapping(entry.get("resolve"))
    profile = _mapping(resolved.get("evidence_profile")) or _mapping(
        entry.get("evidence_profile")
    )
    adjudication = _mapping(profile.get("bibliographic_adjudication"))
    outcome = _string_or_none(adjudication.get("outcome"))
    concern_level = None if concern is None else _string_or_none(concern.get("level"))
    if outcome == "refuted" or (not adjudication and concern_level == "reference_refuted"):
        return {
            "label": "Cited reference contradicted · no text",
            "color": "red",
            "marker": "✕",
            "tooltip": (
                "Recorded bibliographic evidence contradicts the cited reference. "
                "This is not itself a finding of fabrication."
            ),
        }

    identity_status = _string_or_none(adjudication.get("identity_status"))
    identity_known = identity_status in {"identified", "identified_with_errors"} or (
        _string_or_none(resolved.get("status")) == "resolved"
    )
    attempts = [item for item in fetch.get("attempts") or [] if isinstance(item, dict)]
    access_blocked = any(
        item.get("paywalled") is True or item.get("challenge_blocked") is True
        for item in attempts
    ) or _string_or_none(resolved.get("oa_status")) in {
        "closed", "paywalled", "closed_access", "paywall",
    }
    if access_blocked:
        label = (
            "Access blocked · no text"
            if identity_known
            else "Retrieval blocked · identity uncertain"
        )
        return {
            "label": label,
            "color": "purple",
            "marker": "◆",
            "tooltip": (
                "A recorded paywall or access challenge blocked retrieval. "
                + (
                    "The bibliographic identity was identified."
                    if identity_known
                    else "The bibliographic identity was not established by this access signal."
                )
            ),
        }

    if identity_known:
        return {
            "label": "Known work · no text",
            "color": "blue",
            "marker": "●",
            "tooltip": "The bibliographic identity was identified, but no usable text was acquired.",
        }

    review_codes = {
        item.get("code") for item in review_labels if isinstance(item.get("code"), str)
    }
    if (
        (identity_status == "not_identified"
         and _string_or_none(adjudication.get("check_status")) == "complete"
         and outcome == "not_corroborated")
        or "not_found_after_completed_searches" in review_codes
    ):
        return {
            "label": "Not found in completed searches",
            "color": "amber",
            "marker": "△",
            "tooltip": (
                "Completed bibliographic searches did not find a compatible work. "
                "This is informational only and not proof of fabrication."
            ),
        }

    return {
        "label": "Identity uncertain · no text",
        "color": "neutral",
        "marker": "○",
        "tooltip": "No usable text was acquired and the bibliographic identity remains uncertain.",
    }


def present_facts(value: Any, *, _indent: int = 0) -> str:
    """Deterministic, compact audit-fact presentation without JSON syntax."""
    prefix = "  " * _indent
    if isinstance(value, dict):
        if not value:
            return f"{prefix}No recorded facts."
        lines: list[str] = []
        for key in sorted(value, key=str):
            label = str(key).replace("_", " ").strip().capitalize()
            item = value[key]
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{label}:")
                lines.append(present_facts(item, _indent=_indent + 1))
            else:
                lines.append(f"{prefix}{label}: {_fact_value(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return f"{prefix}None recorded."
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}•")
                lines.append(present_facts(item, _indent=_indent + 1))
            else:
                lines.append(f"{prefix}• {_fact_value(item)}")
        return "\n".join(lines)
    return f"{prefix}{_fact_value(value)}"


def _fact_value(value: Any) -> str:
    if value is None:
        return "Not recorded"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    # Values are audit evidence (and may be URLs, identifiers, or paths), so
    # presentation must not rewrite them.  Only field labels are humanized.
    return str(value)


def _validate_template(
    template: Any,
    *,
    allowed_fields: set[str] | frozenset[str],
    required_fields: set[str] | None,
    label: str,
) -> str:
    if not isinstance(template, str) or not template:
        raise ValueError(f"guided Fetch {label} template must not be empty")
    try:
        fields = list(Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"guided Fetch {label} template is invalid") from exc
    names = set()
    for _literal, name, spec, conversion in fields:
        if name is None:
            continue
        if name not in allowed_fields or spec or conversion:
            raise ValueError(f"guided Fetch {label} template has an unsupported placeholder")
        names.add(name)
    if required_fields is not None and not required_fields.issubset(names):
        raise ValueError(f"guided Fetch {label} template requires {{query}}")
    if required_fields is None and not names:
        raise ValueError(f"guided Fetch {label} template requires a parsed field placeholder")
    return template


def _render_template(template: str, values: dict[str, str]) -> str:
    try:
        value = template.format(**values)
    except (KeyError, ValueError) as exc:  # validation above makes this defensive.
        raise ValueError("guided Fetch template cannot be rendered") from exc
    value = " ".join(value.split())
    if not value:
        raise ValueError("guided Fetch template rendered an empty value")
    return value


def _doi_search_query(entry: dict[str, Any]) -> str | None:
    resolved = _mapping(entry.get("resolve"))
    parsed = _mapping(entry.get("parsed"))
    identifier = _mapping(resolved.get("resolved_identifier"))
    for value in (
        resolved.get("doi"),
        identifier.get("value") if str(identifier.get("type") or "").lower() == "doi" else None,
        parsed.get("doi"),
    ):
        doi = _string_or_none(value)
        if doi:
            return _normalise_doi(doi)
    return None


def _render_query(template: str, entry: dict[str, Any]) -> str:
    parsed = _mapping(entry.get("parsed"))
    values = {field: _normalised(parsed.get(field)) for field in _SEARCH_FIELDS}
    return _render_template(template, values)


def _normalised(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _normalise_doi(value: str) -> str:
    doi = value.strip()
    if doi.casefold().startswith("doi:"):
        doi = doi[4:].strip()
    parsed = urlparse(doi)
    if parsed.scheme.lower() in {"http", "https"} and parsed.hostname and parsed.hostname.casefold() == "doi.org":
        doi = unquote(parsed.path.lstrip("/"))
    return doi


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _concrete_url(value: Any) -> str | None:
    url = _string_or_none(value)
    return url if url and _is_safe_http_url(url) else None


def _first_url(value: Any) -> str | None:
    if isinstance(value, str):
        return _concrete_url(value)
    if isinstance(value, dict):
        return _concrete_url(value.get("url"))
    if isinstance(value, list):
        for item in value:
            url = _concrete_url(item) if isinstance(item, str) else _concrete_url(_mapping(item).get("url"))
            if url:
                return url
    return None


def _is_safe_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )
