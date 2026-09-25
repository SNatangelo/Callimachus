# core/report/human/projection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Presentation-neutral projection for the human HTML companion report.

The projection is deliberately built from the run projection, never from the
Markdown report.  It retains canonical codes and raw audit facts; translating
or choosing display labels is a renderer responsibility.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import re
from typing import Any, Mapping

from core.report.reason_codes import ASSESSMENT_REASON_CODES
from core.report.remediation import (
    completed_report_command,
    remediation_for_assessment_reason,
    remediation_for_manual_parse_signal,
    remediation_for_terminal_cause,
    remediation_for_terminal_resolution,
    technical_task_route,
)
from core.report.rollup import latest_terminal_states
from core.report.verification_projection import select_verification_pair_rows
from core.resolve.resolver_coverage import (
    bibliographic_concern,
    bibliographic_review_labels,
)
from .privacy import sanitize_local_paths


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

_CREDENTIAL_COUNT_FIELDS = (
    "calls",
    "successful_http_responses",
    "http_401",
    "http_403",
    "http_429",
    "other_http_errors",
    "network_errors",
)
_CREDENTIAL_TOTAL_FIELDS = (
    *_CREDENTIAL_COUNT_FIELDS,
    "keys_present_at_start",
    "keys_used",
)
_CREDENTIAL_ROW_FIELDS = {
    "provider",
    "env_name",
    "present_at_start",
    "used",
    *_CREDENTIAL_COUNT_FIELDS,
    "by_channel",
    "warning_code",
}
_CREDENTIAL_CHANNELS = ("resolve", "fetch", "search")
_CREDENTIAL_WARNING = "credential_rejected_or_not_entitled"
_SAFE_QUERY_CONTRACT_LABELS = {
    "GET /journals/{issn}": "Crossref journal lookup by ISSN",
    "GET /sources/issn:{issn}": "OpenAlex source lookup by ISSN",
}


def _label_safe_query_contracts(value: JsonValue) -> JsonValue:
    """Replace only known public API route templates before path sanitisation."""
    if isinstance(value, list):
        return [_label_safe_query_contracts(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                _SAFE_QUERY_CONTRACT_LABELS.get(item, item)
                if key == "query_contract" and isinstance(item, str)
                else _label_safe_query_contracts(item)
            )
            for key, item in value.items()
        }
    return value


def _empty_credential_projection() -> dict[str, Any]:
    return {
        "recorded": False,
        "rows": [],
        "totals": {field: 0 for field in _CREDENTIAL_TOTAL_FIELDS},
    }


def _credential_projection(value: Any) -> dict[str, Any]:
    if value is None:
        return _empty_credential_projection()
    if type(value) is not dict or type(value.get("recorded")) is not bool:
        raise ValueError("invalid credential metrics")
    recorded = value["recorded"]
    expected_fields = (
        {"recorded", "recorded_at", "rows", "totals"}
        if recorded
        else {"recorded", "rows", "totals"}
    )
    if set(value) != expected_fields:
        raise ValueError("invalid credential metrics")
    if recorded and (
        type(value["recorded_at"]) is not str
        or not value["recorded_at"].strip()
    ):
        raise ValueError("invalid credential metrics timestamp")
    rows = value["rows"]
    if type(rows) is not list:
        raise ValueError("invalid credential metrics")
    if recorded and not rows:
        raise ValueError("invalid credential metric rows")
    clean = []
    seen_env_names: set[str] = set()
    for row in rows:
        if type(row) is not dict or set(row) != _CREDENTIAL_ROW_FIELDS:
            raise ValueError("invalid credential metric row")
        if (
            type(row["provider"]) is not str
            or re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", row["provider"]) is None
            or type(row["env_name"]) is not str
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", row["env_name"]) is None
            or row["env_name"] in seen_env_names
            or type(row["present_at_start"]) is not bool
            or type(row["used"]) is not bool
        ):
            raise ValueError("invalid credential metric row")
        seen_env_names.add(row["env_name"])
        counts = {field: row[field] for field in _CREDENTIAL_COUNT_FIELDS}
        derived_warning = (
            _CREDENTIAL_WARNING
            if row["calls"]
            and row["http_401"] + row["http_403"] == row["calls"]
            else None
        )
        if (
            any(type(number) is not int or number < 0 for number in counts.values())
            or row["calls"]
            != sum(row[field] for field in _CREDENTIAL_COUNT_FIELDS[1:])
            or row["used"] != (row["calls"] > 0)
            or row["warning_code"] != derived_warning
        ):
            raise ValueError("invalid credential metric row")
        channels = row["by_channel"]
        if type(channels) is not dict or set(channels) != set(_CREDENTIAL_CHANNELS):
            raise ValueError("invalid credential metric channels")
        clean_channels = {}
        for channel in _CREDENTIAL_CHANNELS:
            channel_counts = channels[channel]
            if (
                type(channel_counts) is not dict
                or set(channel_counts) != set(_CREDENTIAL_COUNT_FIELDS)
                or any(
                    type(number) is not int or number < 0
                    for number in channel_counts.values()
                )
                or channel_counts["calls"]
                != sum(
                    channel_counts[field]
                    for field in _CREDENTIAL_COUNT_FIELDS[1:]
                )
            ):
                raise ValueError("invalid credential metric channels")
            clean_channels[channel] = dict(channel_counts)
        if any(
            counts[field]
            != sum(clean_channels[channel][field] for channel in _CREDENTIAL_CHANNELS)
            for field in _CREDENTIAL_COUNT_FIELDS
        ):
            raise ValueError("credential channel metrics do not match row")
        clean_row = dict(row)
        clean_row["by_channel"] = clean_channels
        clean.append(clean_row)
    totals = value["totals"]
    if type(totals) is not dict or set(totals) != set(_CREDENTIAL_TOTAL_FIELDS):
        raise ValueError("invalid credential metric totals")
    if any(type(number) is not int or number < 0 for number in totals.values()):
        raise ValueError("invalid credential metric totals")
    expected = {
        field: sum(row[field] for row in clean)
        for field in _CREDENTIAL_COUNT_FIELDS
    }
    expected["keys_present_at_start"] = sum(row["present_at_start"] for row in clean)
    expected["keys_used"] = sum(row["used"] for row in clean)
    if totals != expected:
        raise ValueError("credential metric totals do not match rows")
    if not recorded:
        if rows or any(totals.values()):
            raise ValueError("unrecorded credential metrics must be empty")
        return _empty_credential_projection()
    return {
        "recorded": True,
        "recorded_at": value["recorded_at"],
        "rows": clean,
        "totals": dict(totals),
    }


_STAGE_ORDER = {
    "support_gate": 0,
    "full_support_gate": 1,
    "contrary_gate": 2,
    "topic_gate": 3,
    "explanation_evidence": 4,
    "jury2": 5,
}

# A bounded partial-support tail does not downgrade an otherwise supportive paper.
_MINOR_PARTIAL_RATIO_LIMIT = 0.10

_NAME_LINE = re.compile(
    r"^[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+"
    r"(?:\s+(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+|[A-Z]\.?)){1,6}$"
)
_AFFILIATION_LINE = re.compile(
    r"\b(?:university|institute|laborator(?:y|ies)|department|college|school|"
    r"google|microsoft|openai)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HumanReportProjection:
    """Immutable wrapper around JSON-serializable human-report facts."""

    value: dict[str, JsonValue]

    def as_dict(self) -> dict[str, JsonValue]:
        """Return a detached JSON-compatible value for rendering or sealing."""
        return _json_value(self.value)


def _json_value(value: Any) -> JsonValue:
    """Copy only JSON primitives so renderer input is deterministic and safe."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("human report projection requires finite numbers")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("human report projection requires string object keys")
        return {key: _json_value(value[key]) for key in sorted(value)}
    raise ValueError("human report projection requires JSON-serializable values")


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"human report projection is missing {label}")
    return value


def _records(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"human report projection {label} must be a list")
    if any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"human report projection {label} has an invalid record")
    return list(value)


def _identifier(record: Mapping[str, Any], key: str, label: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"human report projection {label} is missing {key}")
    return value


def _sorted_records(records: list[Mapping[str, Any]], *keys: str) -> list[Mapping[str, Any]]:
    return sorted(records, key=lambda record: tuple(str(record.get(key) or "") for key in keys))


def _request_order(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(record.get("candidate_cycle") or 0),
        _STAGE_ORDER.get(str(record.get("stage") or ""), len(_STAGE_ORDER)),
        str(record.get("created_at") or ""),
        str(record.get("logical_request_id") or ""),
    )


def _candidate_order(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(record.get("candidate_cycle") or 0),
        str(record.get("created_at") or ""),
        str(record.get("candidate_id") or ""),
    )


def _event_order(record: Mapping[str, Any]) -> tuple[str, str]:
    return (str(record.get("created_at") or ""), str(record.get("event_id") or ""))


def _attempt_order(record: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(record.get("created_at") or record.get("queued_at") or ""),
        str(record.get("dispatch_attempt_id") or ""),
    )


def _display_title(
    manuscript: Mapping[str, Any],
    manuscript_text: Any,
    *,
    allow_legacy_inference: bool = False,
) -> str | None:
    """Return persisted title evidence; retain the old heuristic only for legacy input."""
    stored = manuscript.get("title")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    if not allow_legacy_inference:
        return None
    if not isinstance(manuscript_text, str) or not manuscript_text.strip():
        return None
    lines = [line.strip() for line in manuscript_text.splitlines() if line.strip()][:16]
    if not lines:
        return None
    if lines[0].startswith("#"):
        return lines[0].lstrip("# ").strip() or None
    title_lines: list[str] = []
    for index, line in enumerate(lines):
        if line.casefold() == "abstract":
            break
        following = lines[index + 1] if index + 1 < len(lines) else ""
        previous_requires_continuation = bool(
            title_lines
            and re.search(
                r"(?:\b(?:and|for|from|in|of|on|to|with)|[:–—-])$",
                title_lines[-1],
                re.IGNORECASE,
            )
        )
        looks_like_author_block = bool(
            title_lines
            and not previous_requires_continuation
            and _NAME_LINE.fullmatch(line)
            and (
                _NAME_LINE.fullmatch(following)
                or _AFFILIATION_LINE.search(following)
                or "@" in following
            )
        )
        if looks_like_author_block:
            break
        title_lines.append(line)
    title = " ".join(title_lines).strip()
    return title if title and len(title) <= 500 else lines[0]


def _claim_focus(claim: Mapping[str, Any]) -> str:
    """Return the claim text with its citation marker removed when identifiable."""
    sentence = str(claim.get("sentence") or "").strip()
    marker = str(claim.get("marker_raw") or "").strip()
    start = claim.get("marker_start")
    end = claim.get("marker_end")
    if (
        marker
        and isinstance(start, int)
        and isinstance(end, int)
        and 0 <= start < end <= len(sentence)
        and marker in sentence[start:end]
    ):
        sentence = sentence[:start] + sentence[end:]
    elif marker and marker in sentence:
        sentence = sentence.replace(marker, "", 1)
    sentence = re.sub(r"\s+", " ", sentence)
    sentence = re.sub(r"\s+([,.;:!?])", r"\1", sentence)
    return sentence.strip(" \t\r\n,;")


def _display_source(reference: Mapping[str, Any], fallback_number: int) -> dict[str, Any]:
    number = reference.get("ref_number")
    if not isinstance(number, int) or number <= 0:
        number = fallback_number
    surname = str(reference.get("ay_surname") or "").strip()
    author = surname[:1].upper() + surname[1:] if surname else None
    year = reference.get("year") or reference.get("ay_year")
    citation = None
    if author and year:
        citation = f"{author} et al., {year}"
    elif author:
        citation = f"{author} et al."
    else:
        citation = reference.get("title") or reference.get("raw_entry")
    return {
        "display_id": f"[{number}]",
        "display_author": author,
        "display_year": year,
        "display_citation": citation,
    }


def _candidate_decision(
    pair_state: Mapping[str, Any] | None,
    candidate_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    if pair_state is None:
        return None
    candidate_id = pair_state.get("winner_call_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        return None
    candidate = candidate_by_id.get(candidate_id)
    if candidate is None:
        return None
    outcome_fields = candidate.get("outcome_fields")
    if not isinstance(outcome_fields, Mapping):
        outcome_fields = {}
    return {
        "candidate_cycle": candidate.get("candidate_cycle"),
        "outcome": candidate.get("outcome"),
        "explanation": candidate.get("explanation"),
        "supported_content": outcome_fields.get("supported_part"),
        "incompatible_proposition": outcome_fields.get("incompatible_proposition"),
        "reason": outcome_fields.get("reason"),
        "provider_confidence": outcome_fields.get("provider_confidence"),
        "evidence": candidate.get("evidence") or [],
    }


def _protocol_valid_dispatch(
    request_id: str,
    *,
    attempts_by_request: Mapping[str, list[Mapping[str, Any]]],
    events_by_attempt: Mapping[str, list[Mapping[str, Any]]],
) -> Mapping[str, Any]:
    """Return the sole protocol-valid completion for an authoritative request."""
    valid: list[Mapping[str, Any]] = []
    for attempt in attempts_by_request[request_id]:
        attempt_id = _identifier(attempt, "dispatch_attempt_id", "dispatch attempt")
        terminals = [
            event for event in events_by_attempt.get(attempt_id, [])
            if event.get("event_type") in {"completed", "failed", "abandoned"}
        ]
        if len(terminals) > 1:
            raise ValueError("authoritative dispatch has contradictory terminal observations")
        if not terminals or terminals[0].get("event_type") != "completed":
            continue
        if (terminals[0].get("payload") or {}).get("technical_result") == "protocol_invalid":
            continue
        valid.append(attempt)
    if len(valid) != 1:
        raise ValueError("authoritative logical request lacks one protocol-valid completion")
    return valid[0]


def _model_attribution(
    attempt: Mapping[str, Any],
    *,
    logical_request_id: str,
) -> dict[str, str]:
    """Project the actual provider/model that completed one logical request."""
    return {
        "logical_request_id": logical_request_id,
        "provider_id": _identifier(attempt, "provider_id", "authoritative dispatch attempt"),
        "model_id": _identifier(attempt, "model_id", "authoritative dispatch attempt"),
    }


def _verdict_provenance(
    pair_state: Mapping[str, Any] | None,
    *,
    candidate_by_id: Mapping[str, Mapping[str, Any]],
    events_by_candidate: Mapping[str, list[Mapping[str, Any]]],
    requests_by_id: Mapping[str, Mapping[str, Any]],
    attempts_by_request: Mapping[str, list[Mapping[str, Any]]],
    events_by_attempt: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, JsonValue]:
    """Build authoritative model provenance for one terminal pair only.

    Raw dispatch history is intentionally not consulted for winner selection:
    the deterministic pair state owns that decision.
    """
    finalizer = {
        key: pair_state.get(key) if pair_state is not None else None
        for key in ("status", "terminal_outcome", "terminal_cause", "winner_call_id")
    }
    winner_id = finalizer["winner_call_id"]
    if winner_id is None:
        return {"proposal": None, "jury2_reviews": [], "finalizer": finalizer}
    if not isinstance(winner_id, str) or not winner_id:
        raise ValueError("authoritative pair state has an invalid winner_call_id")
    candidate = candidate_by_id.get(winner_id)
    if candidate is None:
        raise ValueError("authoritative pair state references an unknown winner candidate")
    if (candidate.get("claim_id"), candidate.get("ref_id")) != (
        pair_state.get("claim_id"), pair_state.get("ref_id"),
    ):
        raise ValueError("authoritative winner candidate belongs to another pair")
    origin_id = _identifier(candidate, "origin_logical_request_id", "winner candidate")
    origin = requests_by_id.get(origin_id)
    if origin is None:
        raise ValueError("winner candidate has an unknown origin logical request")
    if origin.get("stage") not in _STAGE_ORDER or origin.get("stage") == "jury2":
        raise ValueError("winner candidate origin is not a Jury1 request")
    proposal = _model_attribution(
        _protocol_valid_dispatch(
            origin_id,
            attempts_by_request=attempts_by_request,
            events_by_attempt=events_by_attempt,
        ),
        logical_request_id=origin_id,
    )
    jury2_reviews: list[dict[str, str]] = []
    for event in sorted(events_by_candidate.get(winner_id, []), key=_event_order):
        event_type = event.get("event_type")
        if event_type not in {"jury2_yes", "jury2_no"}:
            continue
        payload = _required_mapping(event.get("payload") or {}, "Jury2 event payload")
        jury2_request_id = _identifier(payload, "logical_request_id", "Jury2 event payload")
        jury2_request = requests_by_id.get(jury2_request_id)
        if jury2_request is None or jury2_request.get("stage") != "jury2":
            raise ValueError("winner Jury2 event has an invalid logical request")
        if jury2_request.get("candidate_id") != winner_id:
            raise ValueError("winner Jury2 event references another candidate")
        review = _model_attribution(
            _protocol_valid_dispatch(
                jury2_request_id,
                attempts_by_request=attempts_by_request,
                events_by_attempt=events_by_attempt,
            ),
            logical_request_id=jury2_request_id,
        )
        jury2_reviews.append({"event_type": event_type, **review})
    return {"proposal": proposal, "jury2_reviews": jury2_reviews, "finalizer": finalizer}


def _rejected_jury2_attempts(
    candidates: list[Mapping[str, Any]],
    *,
    events_by_candidate: Mapping[str, list[Mapping[str, Any]]],
    requests_by_id: Mapping[str, Mapping[str, Any]],
    attempts_by_request: Mapping[str, list[Mapping[str, Any]]],
    events_by_attempt: Mapping[str, list[Mapping[str, Any]]],
) -> list[dict[str, JsonValue]]:
    """Project each rejected Jury2 cycle with its exact Jury1 proposal."""
    rejected: list[dict[str, JsonValue]] = []
    for candidate in sorted(candidates, key=_candidate_order):
        candidate_id = _identifier(candidate, "candidate_id", "candidate")
        origin_id = _identifier(candidate, "origin_logical_request_id", "candidate")
        origin = requests_by_id.get(origin_id)
        if origin is None or origin.get("stage") not in _STAGE_ORDER:
            raise ValueError("candidate has an invalid Jury1 origin request")
        if origin.get("stage") == "jury2":
            raise ValueError("candidate origin is not a Jury1 request")
        for event in sorted(events_by_candidate.get(candidate_id, []), key=_event_order):
            if event.get("event_type") != "jury2_no":
                continue
            payload = _required_mapping(event.get("payload") or {}, "Jury2 event payload")
            review_id = _identifier(payload, "logical_request_id", "Jury2 event payload")
            review_request = requests_by_id.get(review_id)
            if review_request is None or review_request.get("stage") != "jury2":
                raise ValueError("rejected Jury2 event has an invalid logical request")
            if review_request.get("candidate_id") != candidate_id:
                raise ValueError("rejected Jury2 event references another candidate")
            proposal = _model_attribution(
                _protocol_valid_dispatch(
                    origin_id,
                    attempts_by_request=attempts_by_request,
                    events_by_attempt=events_by_attempt,
                ),
                logical_request_id=origin_id,
            )
            review = _model_attribution(
                _protocol_valid_dispatch(
                    review_id,
                    attempts_by_request=attempts_by_request,
                    events_by_attempt=events_by_attempt,
                ),
                logical_request_id=review_id,
            )
            rejected.append({
                "candidate_id": candidate_id,
                "candidate_cycle": candidate.get("candidate_cycle"),
                "jury1_outcome": candidate.get("outcome"),
                "proposal": proposal,
                "jury2_review": {
                    "event_type": "jury2_no",
                    "reason": payload.get("reason"),
                    **review,
                },
            })
    return rejected


def _rejected_jury1_attempts(
    rejections: list[Mapping[str, Any]],
    *,
    requests_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, JsonValue]]:
    """Project guard-rejected Jury1 proposals without promoting them to verdicts."""
    rejected: list[dict[str, JsonValue]] = []
    for rejection in sorted(rejections, key=_event_order):
        request_id = _identifier(rejection, "logical_request_id", "Jury1 rejection")
        request = requests_by_id.get(request_id)
        if request is None:
            raise ValueError("Jury1 rejection references an unknown logical request")
        payload = request.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise ValueError("rejected Jury1 request has an invalid payload")
        jury1_outcome = payload.get("determined_outcome")
        if jury1_outcome is not None and (
            not isinstance(jury1_outcome, str) or not jury1_outcome
        ):
            raise ValueError("rejected Jury1 request has an invalid determined_outcome")
        rejected.append({
            "logical_request_id": request_id,
            "candidate_cycle": request.get("candidate_cycle"),
            "stage": request.get("stage"),
            "jury1_outcome": jury1_outcome,
            "cause": rejection.get("cause"),
            "state_cause": rejection.get("state_cause"),
        })
    return rejected


def _overview_assessment(
    *,
    claims_by_id: Mapping[str, Mapping[str, Any]],
    refs_by_id: Mapping[str, Mapping[str, Any]],
    citations_by_claim: Mapping[str, list[Mapping[str, Any]]],
    rows_by_pair: Mapping[tuple[str, str], Mapping[str, Any]],
    resolve_map: Mapping[str, Any],
    parse_coverage: Mapping[str, Any],
    review_labels_by_ref: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, JsonValue]:
    """Classify paper-level findings without collapsing their audit axes.

    A claim or source is one unit even when it has several pairs or several
    reasons.  The reason codes retain the deterministic basis for the summary,
    while the UI can keep internal identifiers out of reader-facing copy.
    """
    hard_units: set[tuple[str, str]] = set()
    review_units: set[tuple[str, str]] = set()
    minor_units: set[tuple[str, str]] = set()
    technical_units: set[tuple[str, str]] = set()
    reason_codes: set[str] = set()
    severe_bibliographic_finding = False

    # Parse owns the calibrated coverage policy.  Its structured warning means
    # either the manuscript leaves many listed references uncited or the parser
    # failed to recognise their markers.  Both require review, but neither is
    # evidence of fabrication or a semantic failure by itself.
    if isinstance(parse_coverage.get("warning"), str) and parse_coverage["warning"].strip():
        review_units.add(("manuscript", "reference_coverage"))
        reason_codes.add("review.manuscript.low_reference_coverage")

    pair_outcomes = [row.get("semantic_outcome") for row in rows_by_pair.values()]
    support_pairs = sum(outcome == "supports" for outcome in pair_outcomes)
    partial_pairs = sum(outcome == "partial" for outcome in pair_outcomes)
    supportive_pairs = support_pairs + partial_pairs
    disqualifying_outcomes = {
        "contradicts", "off_topic", "related", "non_decidable"
    }
    partials_are_minor = (
        partial_pairs > 0
        and support_pairs > 0
        and partial_pairs / supportive_pairs <= _MINOR_PARTIAL_RATIO_LIMIT
        and not any(outcome in disqualifying_outcomes for outcome in pair_outcomes)
    )

    for claim_id in claims_by_id:
        citations = citations_by_claim.get(claim_id, [])
        pair_rows = [
            rows_by_pair.get((claim_id, ref_id))
            for ref_id in {citation.get("ref_id") for citation in citations}
            if isinstance(ref_id, str)
        ]
        recorded_rows = [row for row in pair_rows if row is not None]
        outcomes = [
            str(row.get("semantic_outcome"))
            for row in recorded_rows
            if row.get("semantic_outcome") is not None
        ]
        hard_reasons: set[str] = set()
        review_reasons: set[str] = set()
        technical_reasons: set[str] = set()
        has_partial = any(
            row.get("semantic_outcome") == "partial" for row in recorded_rows
        )

        if any(
            citation.get("ref_id") is None
            and not citation.get("candidate_ref_ids")
            for citation in citations
        ):
            hard_reasons.add("hard.claim.orphan_citation")
        if any(
            row.get("semantic_outcome") == "contradicts"
            and row.get("assurance") != "contested"
            for row in recorded_rows
        ):
            hard_reasons.add("hard.claim.uncontested_contradiction")
        if outcomes and all(outcome == "off_topic" for outcome in outcomes):
            hard_reasons.add("hard.claim.all_off_topic")

        if any(row.get("semantic_outcome") == "off_topic" for row in recorded_rows):
            review_reasons.add("review.claim.off_topic")
        if (
            any(row.get("semantic_outcome") == "related" for row in recorded_rows)
            and not any(outcome in {"supports", "partial"} for outcome in outcomes)
        ):
            review_reasons.add("review.claim.related_only")
        if any(row.get("semantic_outcome") == "non_decidable" for row in recorded_rows):
            review_reasons.add("review.claim.non_decidable")
        if any(
            row.get("semantic_outcome") is None
            or row.get("semantic_outcome") == "unresolved"
            or row.get("result_class") == "unresolved"
            for row in recorded_rows
        ) or not recorded_rows:
            technical_reasons.add("technical.claim.no_semantic_outcome")
        if (
            len(recorded_rows) != len(pair_rows)
            or any(not row.get("operational_complete") for row in recorded_rows)
        ):
            technical_reasons.add("technical.claim.incomplete_pair_coverage")
        if any(row.get("assurance") == "contested" for row in recorded_rows):
            technical_reasons.add("technical.claim.contested_assurance")
        if any(
            citation.get("ambiguous")
            or citation.get("candidate_ref_ids")
            for citation in citations
        ):
            technical_reasons.add("technical.claim.ambiguous_citation")

        unit = ("claim", claim_id)
        if technical_reasons:
            technical_units.add(unit)
            reason_codes.update(technical_reasons)
        if hard_reasons:
            hard_units.add(unit)
            reason_codes.update(hard_reasons)
            reason_codes.update(review_reasons)
            if has_partial:
                reason_codes.add("review.claim.partial_support")
        elif review_reasons:
            review_units.add(unit)
            reason_codes.update(review_reasons)
            if has_partial:
                reason_codes.add("review.claim.partial_support")
        elif has_partial:
            if partials_are_minor:
                minor_units.add(unit)
                reason_codes.add("minor.claim.partial_support")
            else:
                review_units.add(unit)
                reason_codes.add("review.claim.partial_support")

    hard_statuses = {"not_found", "identifier_mismatch", "suspected_fabricated"}
    for ref_id in refs_by_id:
        resolve = resolve_map.get(ref_id)
        if not isinstance(resolve, Mapping):
            resolve = {}
        concern = bibliographic_concern(resolve)
        hard_reasons: set[str] = set()
        review_reasons: set[str] = set()
        if resolve.get("status") in hard_statuses:
            hard_reasons.add(f"hard.source.{resolve['status']}")
        if resolve.get("reference_status_tag") == "suspected_fabricated":
            hard_reasons.add("hard.source.suspected_fabricated_tag")
        if concern is not None and concern["level"] == "reference_refuted":
            hard_reasons.add("hard.source.reference_refuted")
            severe_bibliographic_finding = True
        elif concern is not None and concern["level"] == "high_fabrication_suspicion":
            hard_reasons.add("hard.source.high_fabrication_suspicion")
            severe_bibliographic_finding = True
        elif concern is not None and concern["level"] == "elevated_bibliographic_suspicion":
            review_reasons.add("review.source.elevated_bibliographic_suspicion")
        if resolve.get("retracted") is True:
            hard_reasons.add("hard.source.retracted")
        if resolve.get("title_flag") == "warn":
            review_reasons.add("review.source.title_flag_warn")
        if resolve.get("existence_corroboration") == "searched_not_found":
            review_reasons.add("review.source.searched_not_found")
        if any(
            label.get("code") == "author_list_discrepancy"
            for label in review_labels_by_ref.get(ref_id, [])
        ):
            review_reasons.add("review.source.author_list_discrepancy")

        unit = ("source", ref_id)
        if hard_reasons:
            hard_units.add(unit)
            reason_codes.update(hard_reasons)
            reason_codes.update(review_reasons)
        elif review_reasons:
            review_units.add(unit)
            reason_codes.update(review_reasons)

    state = (
        "red" if severe_bibliographic_finding or len(hard_units) >= 2
        else "yellow" if (hard_units or review_units) else "green"
    )
    if not reason_codes <= ASSESSMENT_REASON_CODES:
        raise ValueError("human report assessment emitted an unknown reason code")
    return {
        "state": state,
        "hard_findings": len(hard_units),
        "review_findings": len(review_units),
        "minor_findings": len(minor_units),
        "technical_findings": len(technical_units),
        "reason_codes": sorted(reason_codes),
    }


def _remediation_projection(
    assessment: Mapping[str, Any], pairs: list[Mapping[str, Any]], state: Mapping[str, Any]
) -> dict[str, JsonValue]:
    """Project only catalog-backed guidance and privacy-safe applied facts."""
    guidance = []
    for code in assessment.get("reason_codes", []):
        policy = remediation_for_assessment_reason(code)
        if policy.mechanism == "none":
            continue
        guidance.append({
            "finding_kind": "assessment_reason", "code": code,
            "claim_id": None, "ref_id": None, "scope": None,
            "action": policy.action_id, "mechanism": policy.mechanism,
            "user_override_allowed": policy.user_override_allowed,
            "preserve_automatic_finding": policy.preserve_automatic_finding,
            "child_run_command": (
                completed_report_command(policy) if not policy.child_run_task_routes else None
            ),
            "child_run_routes": [
                {
                    "route_id": route_id, "task_kind": route.task_kind,
                    "child_start_command": route.child_start_command,
                    "answer_modes": list(route.answer_modes),
                    "commands": list(route.commands),
                }
                for route_id in policy.child_run_task_routes
                if (route := technical_task_route(route_id)) is not None
            ],
        })
    for pair in pairs:
        verification = pair.get("verification") or {}
        pair_state = pair.get("pair_state") or {}
        cause = pair_state.get("terminal_cause")
        if cause is not None:
            policy = remediation_for_terminal_cause(cause)
            finding_kind, code = "terminal_cause", cause
        else:
            resolution = verification.get("resolution")
            policy = remediation_for_terminal_resolution(
                resolution, verification.get("semantic_outcome")
            )
            finding_kind, code = "terminal_resolution", resolution
        if policy.mechanism == "none":
            continue
        guidance.append({
            "finding_kind": finding_kind, "code": code,
            "claim_id": pair.get("claim_id"), "ref_id": pair.get("ref_id"),
            "scope": (pair.get("pair_state") or {}).get("scope"),
            "action": policy.action_id, "mechanism": policy.mechanism,
            "user_override_allowed": policy.user_override_allowed,
            "preserve_automatic_finding": policy.preserve_automatic_finding,
            "child_run_command": (
                completed_report_command(policy) if not policy.child_run_task_routes else None
            ),
            "child_run_routes": [
                {
                    "route_id": route_id, "task_kind": route.task_kind,
                    "child_start_command": route.child_start_command,
                    "answer_modes": list(route.answer_modes),
                    "commands": list(route.commands),
                }
                for route_id in policy.child_run_task_routes
                if (route := technical_task_route(route_id)) is not None
            ],
        })
    raw = state.get("remediation", {})
    if not isinstance(raw, Mapping):
        raise ValueError("human report remediation state is invalid")
    interventions = raw.get("interventions", [])
    if not isinstance(interventions, list) or any(not isinstance(item, Mapping) for item in interventions):
        raise ValueError("human report remediation interventions are invalid")
    child_run = raw.get("child_run")
    if child_run is not None and not isinstance(child_run, Mapping):
        raise ValueError("human report remediation child lineage is invalid")
    footnotes = raw.get("footnote_split_required", [])
    if not isinstance(footnotes, list) or any(
        not isinstance(item, Mapping)
        or set(item) != {"note_number"}
        or not isinstance(item.get("note_number"), int)
        or isinstance(item.get("note_number"), bool)
        or item["note_number"] <= 0
        for item in footnotes
    ):
        raise ValueError("human report remediation footnote signals are invalid")
    policy = remediation_for_manual_parse_signal("footnote_source_review")
    route = technical_task_route("footnote_source_split")
    for item in footnotes:
        guidance.append({
            "finding_kind": "manual_parse_signal", "code": "footnote_source_review",
            "claim_id": None, "ref_id": None, "scope": item.get("note_number"),
            "action": policy.action_id, "mechanism": policy.mechanism,
            "user_override_allowed": policy.user_override_allowed,
            "preserve_automatic_finding": policy.preserve_automatic_finding,
            "child_run_command": None,
            "child_run_routes": [] if route is None else [{
                "route_id": route.route_id, "task_kind": route.task_kind,
                "child_start_command": route.child_start_command,
                "answer_modes": list(route.answer_modes), "commands": list(route.commands),
            }],
        })
    intervention_fields = {
        "kind", "effect", "task_id", "answer_id", "claim_id", "ref_id",
        "source_id", "action", "applied_at", "admission_classification",
    }
    safe_interventions = []
    actions_by_kind = {
        "manual_parse_override": {
            "correct_identity", "keep_ambiguous", "keep_unresolved",
            "no_sources", "select_claim", "select_reference", "split_sources",
        },
        "source_identity_attestation": {"attest_identity", "keep_unverified"},
        "operator_fetch": {"submitted"},
        "operator_ocr": {"submitted"},
        "operator_browser": {"submitted"},
        "operator_research": {"submitted"},
    }
    effects_by_kind = {
        "manual_parse_override": {"override", "retained_uncertainty"},
        "source_identity_attestation": {
            "operator_identity_attestation", "retained_uncertainty",
        },
        "operator_fetch": {"operator_supplied_evidence"},
        "operator_ocr": {"operator_supplied_evidence"},
        "operator_browser": {"operator_supplied_evidence"},
        "operator_research": {"operator_submitted_research"},
    }
    for item in interventions:
        if set(item) != intervention_fields:
            raise ValueError("human report remediation intervention has unsupported fields")
        kind = item.get("kind")
        if (
            kind not in actions_by_kind
            or item.get("action") not in actions_by_kind[kind]
            or item.get("effect") not in effects_by_kind[kind]
        ):
            raise ValueError("human report remediation intervention is invalid")
        if kind in {"manual_parse_override", "source_identity_attestation"}:
            if item["action"] in {"keep_ambiguous", "keep_unresolved", "keep_unverified"}:
                expected_effect = "retained_uncertainty"
            elif kind == "source_identity_attestation":
                expected_effect = "operator_identity_attestation"
            else:
                expected_effect = "override"
            if item["effect"] != expected_effect:
                raise ValueError("human report remediation intervention is invalid")
        for field in ("task_id", "answer_id", "action", "applied_at"):
            if not isinstance(item.get(field), str) or not item[field]:
                raise ValueError("human report remediation intervention is invalid")
        for field in ("claim_id", "ref_id", "source_id"):
            if item.get(field) is not None and not isinstance(item[field], str):
                raise ValueError("human report remediation intervention is invalid")
        if item.get("admission_classification") not in {
            "local_operator_unattested", "authority_authenticated_operator",
        }:
            raise ValueError("human report remediation intervention is invalid")
        safe_interventions.append(dict(item))
    safe_child = None
    if child_run is not None:
        if set(child_run) != {"run_origin", "parent_run_id", "provenance"}:
            raise ValueError("human report remediation child lineage has unsupported fields")
        provenance = child_run.get("provenance")
        if not isinstance(provenance, Mapping) or set(provenance) != {
            "parent_run_id", "parent_input_sha256", "parent_report_sha256",
            "parent_journal_sha256", "source_inventory_sha256", "created_at",
        }:
            raise ValueError("human report remediation child provenance has unsupported fields")
        if (
            child_run.get("run_origin") != "remediated_from_completed"
            or not isinstance(child_run.get("parent_run_id"), str)
            or not child_run["parent_run_id"]
            or provenance.get("parent_run_id") != child_run["parent_run_id"]
            or not isinstance(provenance.get("created_at"), str)
            or not provenance["created_at"]
            or any(
                not isinstance(provenance.get(field), str)
                or re.fullmatch(r"[0-9a-f]{64}", provenance[field]) is None
                for field in (
                    "parent_input_sha256", "parent_report_sha256",
                    "parent_journal_sha256", "source_inventory_sha256",
                )
            )
        ):
            raise ValueError("human report remediation child lineage is invalid")
        safe_child = dict(child_run)
    deduped = []
    seen = set()
    for entry in guidance:
        key = tuple(entry.get(field) for field in (
            "finding_kind", "code", "claim_id", "ref_id", "scope", "action"
        ))
        if key not in seen:
            seen.add(key)
            deduped.append(entry)
    return {
        "guidance": sorted(deduped, key=lambda row: tuple(str(row.get(key) or "") for key in ("finding_kind", "code", "claim_id", "ref_id", "action"))),
        "interventions": safe_interventions,
        "child_run": safe_child,
    }


def _manual_parse_diagnostics(value: Any) -> list[dict[str, JsonValue]]:
    """Expose adjudication outcomes without answer reasons or operator identity."""
    public_fields = (
        "subject_type", "subject_id", "note_number", "ref_number",
        "claim_id", "ref_id", "status", "action", "source_count",
        "manual_review_required", "task_id", "answer_id", "submitted_at",
        "applied_at", "direction", "candidate_origin", "candidate_score",
        "original_title", "original_doi", "effective_title", "effective_doi",
    )
    return [
        {
            field: _json_value(row[field])
            for field in public_fields
            if field in row
        }
        for row in _records(value, "manual Parse adjudication")
    ]


def build_human_report_projection(run_projection: Mapping[str, Any]) -> HumanReportProjection:
    """Build one stable, UI-neutral projection from an authoritative run view."""
    source = _required_mapping(run_projection, "run projection")
    execution = _required_mapping(source.get("execution", {}), "execution")
    debug_mode = execution.get("debug_mode", False)
    debug_labels = execution.get("debug_labels", [])
    if type(debug_mode) is not bool:
        raise ValueError("human report projection debug_mode must be boolean")
    if (
        not isinstance(debug_labels, (list, tuple))
        or any(not isinstance(label, str) or not label for label in debug_labels)
    ):
        raise ValueError("human report projection debug_labels must be nonempty text")
    parse = _required_mapping(source.get("parse_effective"), "parse_effective")
    manuscript = _required_mapping(parse.get("manuscript"), "manuscript")
    claims = _records(parse.get("claims"), "claims")
    references = _records(parse.get("references"), "references")
    citations = _records(parse.get("citations"), "citations")
    table_only_citations = _records(
        parse.get("table_only_citations", []), "table_only_citations"
    )
    resolve_map = _required_mapping(source.get("resolve_map", {}), "resolve_map")
    manifest = _required_mapping(source.get("manifest", {}), "manifest")
    manifest_entries = _records(manifest.get("entries", []), "manifest.entries")
    raw = _required_mapping(source.get("verification_raw", {}), "verification_raw")
    candidates = _records(raw.get("candidates", []), "verification_raw.candidates")
    candidate_events = _records(raw.get("candidate_events", []), "verification_raw.candidate_events")
    requests = _records(raw.get("logical_requests", []), "verification_raw.logical_requests")
    jury1_rejections = _records(raw.get("jury1_rejections", []), "verification_raw.jury1_rejections")
    attempts = _records(raw.get("dispatch_attempts", []), "verification_raw.dispatch_attempts")
    dispatch_events = _records(raw.get("dispatch_events", []), "verification_raw.dispatch_events")
    pair_states = _records(source.get("verification_pair_states", []), "verification_pair_states")
    verification_rows = select_verification_pair_rows(
        _records(source.get("verification_projection", []), "verification_projection")
    )

    claims_by_id = {_identifier(record, "id", "claim"): record for record in claims}
    refs_by_id = {_identifier(record, "id", "reference"): record for record in references}
    if len(claims_by_id) != len(claims) or len(refs_by_id) != len(references):
        raise ValueError("human report projection has duplicate claim or reference identifiers")
    claim_display_by_id = {
        claim_id: f"C{claim.get('claim_order') or index}"
        for index, (claim_id, claim) in enumerate(claims_by_id.items(), start=1)
    }
    source_display_by_id = {
        ref_id: _display_source(reference, index)
        for index, (ref_id, reference) in enumerate(refs_by_id.items(), start=1)
    }

    citations_by_claim: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    citations_by_ref: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for citation in citations:
        claim_id = citation.get("claim_id")
        # Link-silent resolved markers deliberately have no claim.  Preserve
        # them in the Parse payload as coverage/provenance, but do not turn
        # them into claim-to-source report edges.
        if claim_id is None and citation.get("provenance") == "link_silent_resolved":
            continue
        claim_id = _identifier(citation, "claim_id", "citation")
        if claim_id not in claims_by_id:
            raise ValueError("human report projection citation references unknown claim")
        ref_id = citation.get("ref_id")
        if ref_id is not None:
            if not isinstance(ref_id, str) or ref_id not in refs_by_id:
                raise ValueError("human report projection citation references unknown source")
            citations_by_ref[ref_id].append(citation)
        citations_by_claim[claim_id].append(citation)

    rows_by_pair: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in verification_rows:
        claim_id = _identifier(row, "claim_id", "verification row")
        ref_id = _identifier(row, "ref_id", "verification row")
        if claim_id not in claims_by_id or ref_id not in refs_by_id:
            raise ValueError("human report projection verification row has unknown entity")
        key = (claim_id, ref_id)
        if key in rows_by_pair:
            raise ValueError("human report projection has duplicate selected pair rows")
        rows_by_pair[key] = row

    claim_order = {claim_id: index for index, claim_id in enumerate(claims_by_id)}
    ref_order = {ref_id: index for index, ref_id in enumerate(refs_by_id)}
    pair_keys = sorted(
        {(claim_id, str(citation["ref_id"]))
         for claim_id, entries in citations_by_claim.items()
         for citation in entries if citation.get("ref_id") is not None},
        key=lambda item: (claim_order[item[0]], ref_order[item[1]]),
    )
    pair_key_set = set(pair_keys)
    candidate_by_id = {}
    for candidate in candidates:
        candidate_id = _identifier(candidate, "candidate_id", "candidate")
        claim_id = _identifier(candidate, "claim_id", "candidate")
        ref_id = _identifier(candidate, "ref_id", "candidate")
        if claim_id not in claims_by_id or ref_id not in refs_by_id:
            raise ValueError("human report projection candidate has unknown entity")
        if (claim_id, ref_id) not in pair_key_set:
            raise ValueError("human report projection candidate has no citation pair")
        candidate_by_id[candidate_id] = candidate
    if len(candidate_by_id) != len(candidates):
        raise ValueError("human report projection has duplicate candidate identifiers")
    candidates_by_pair: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_pair[(_identifier(candidate, "claim_id", "candidate"),
                            _identifier(candidate, "ref_id", "candidate"))].append(candidate)
    events_by_candidate: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in candidate_events:
        candidate_id = _identifier(event, "candidate_id", "candidate event")
        if candidate_id not in candidate_by_id:
            raise ValueError("human report projection candidate event has unknown candidate")
        events_by_candidate[candidate_id].append(event)
    requests_by_id = {}
    requests_by_pair: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for request in requests:
        request_id = _identifier(request, "logical_request_id", "logical request")
        claim_id = _identifier(request, "claim_id", "logical request")
        ref_id = _identifier(request, "ref_id", "logical request")
        if claim_id not in claims_by_id or ref_id not in refs_by_id:
            raise ValueError("human report projection logical request has unknown entity")
        if (claim_id, ref_id) not in pair_key_set:
            raise ValueError("human report projection logical request has no citation pair")
        requests_by_id[request_id] = request
        requests_by_pair[(claim_id, ref_id)].append(request)
    if len(requests_by_id) != len(requests):
        raise ValueError("human report projection has duplicate logical request identifiers")
    for candidate in candidates:
        request_id = _identifier(candidate, "origin_logical_request_id", "candidate")
        request = requests_by_id.get(request_id)
        if request is None:
            raise ValueError("human report projection candidate has unknown logical request")
        if (request["claim_id"], request["ref_id"]) != (
            candidate["claim_id"], candidate["ref_id"]
        ):
            raise ValueError("human report projection candidate request has a different pair")
    rejections_by_request: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for rejection in jury1_rejections:
        request_id = _identifier(rejection, "logical_request_id", "Jury1 rejection")
        if request_id not in requests_by_id:
            raise ValueError("human report projection Jury1 rejection has unknown logical request")
        rejections_by_request[request_id].append(rejection)
    attempts_by_request: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    attempt_by_id = {}
    for attempt in attempts:
        request_id = _identifier(attempt, "logical_request_id", "dispatch attempt")
        attempt_id = _identifier(attempt, "dispatch_attempt_id", "dispatch attempt")
        if request_id not in requests_by_id:
            raise ValueError("human report projection dispatch attempt has unknown logical request")
        if attempt_id in attempt_by_id:
            raise ValueError("human report projection has duplicate dispatch attempt identifiers")
        attempt_by_id[attempt_id] = attempt
        attempts_by_request[request_id].append(attempt)
    events_by_request: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    events_by_attempt: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in dispatch_events:
        request_id = _identifier(event, "logical_request_id", "dispatch event")
        if request_id not in requests_by_id:
            raise ValueError("human report projection dispatch event has unknown logical request")
        attempt_id = event.get("dispatch_attempt_id")
        if attempt_id is not None and (
            not isinstance(attempt_id, str) or attempt_id not in attempt_by_id
        ):
            raise ValueError("human report projection dispatch event has unknown dispatch attempt")
        if attempt_id is not None and attempt_by_id[attempt_id]["logical_request_id"] != request_id:
            raise ValueError("human report projection dispatch event has a mismatched request")
        events_by_request[request_id].append(event)
        if attempt_id is not None:
            events_by_attempt[attempt_id].append(event)

    pair_state_by_pair = latest_terminal_states(pair_states)

    pairs = []
    for claim_id, ref_id in pair_keys:
        pair_candidates = sorted(candidates_by_pair[(claim_id, ref_id)], key=_candidate_order)
        for candidate in pair_candidates:
            for event in events_by_candidate[_identifier(candidate, "candidate_id", "candidate")]:
                request_id = (event.get("payload") or {}).get("logical_request_id")
                if isinstance(request_id, str) and request_id not in requests_by_id:
                    raise ValueError("human report projection candidate event has unknown logical request")
        pair_requests = sorted(requests_by_pair[(claim_id, ref_id)], key=_request_order)
        request_ids = {request["logical_request_id"] for request in pair_requests}
        pair_rejections = sorted(
            [rejection for request_id in request_ids
             for rejection in rejections_by_request[request_id]],
            key=_event_order,
        )
        pair_attempts = [attempt for request in pair_requests
                         for attempt in sorted(attempts_by_request[request["logical_request_id"]], key=_attempt_order)]
        pair_state = pair_state_by_pair.get((claim_id, ref_id))
        pairs.append(_json_value({
            "claim_id": claim_id,
            "claim_display_id": claim_display_by_id[claim_id],
            "ref_id": ref_id,
            "source_display_id": source_display_by_id[ref_id]["display_id"],
            "source_display_citation": source_display_by_id[ref_id]["display_citation"],
            "verification": rows_by_pair.get((claim_id, ref_id)),
            "pair_state": pair_state,
            "decision": _candidate_decision(pair_state, candidate_by_id),
            "verdict_provenance": _verdict_provenance(
                pair_state,
                candidate_by_id=candidate_by_id,
                events_by_candidate=events_by_candidate,
                requests_by_id=requests_by_id,
                attempts_by_request=attempts_by_request,
                events_by_attempt=events_by_attempt,
            ),
            "rejected_jury2_attempts": _rejected_jury2_attempts(
                pair_candidates,
                events_by_candidate=events_by_candidate,
                requests_by_id=requests_by_id,
                attempts_by_request=attempts_by_request,
                events_by_attempt=events_by_attempt,
            ),
            "rejected_jury1_attempts": _rejected_jury1_attempts(
                pair_rejections,
                requests_by_id=requests_by_id,
            ),
            "candidates": pair_candidates,
            "candidate_events": [event for candidate in pair_candidates
                                 for event in sorted(events_by_candidate[candidate["candidate_id"]], key=_event_order)],
            "logical_requests": pair_requests,
            "jury1_rejections": pair_rejections,
            "dispatch_attempts": pair_attempts,
            "dispatch_events": sorted(
                [event for request_id in request_ids for event in events_by_request[request_id]],
                key=_event_order,
            ),
        }))

    pair_rows = [rows_by_pair[key] for key in pair_keys if key in rows_by_pair]
    outcome_counts = Counter(
        row.get("semantic_outcome") if row.get("semantic_outcome") is not None else "no_outcome"
        for row in pair_rows
    )
    result_counts = Counter(row.get("result_class") or "unresolved" for row in pair_rows)
    attention = []
    for pair in pairs:
        verification = pair["verification"]
        if verification is not None and (
            verification.get("result_class") != "positive" or not verification.get("crediting")
        ):
            attention.append({
                "entity": "pair",
                "claim_id": pair["claim_id"],
                "claim_display_id": pair["claim_display_id"],
                "ref_id": pair["ref_id"],
                "source_display_id": pair["source_display_id"],
                "source_display_citation": pair["source_display_citation"],
                "result_class": verification.get("result_class"),
                "semantic_outcome": verification.get("semantic_outcome"),
                "resolution": verification.get("resolution"),
            })
    for claim_id, entries in citations_by_claim.items():
        for citation in entries:
            if citation.get("ref_id") is None:
                attention.append({
                    "entity": "citation",
                    "claim_id": claim_id,
                    "claim_display_id": claim_display_by_id[claim_id],
                    "reason": "orphan",
                    "citation": citation,
                })
    concerns_by_ref = {
        ref_id: bibliographic_concern(resolve_map.get(ref_id))
        for ref_id, reference in refs_by_id.items()
    }
    review_labels_by_ref = {
        ref_id: bibliographic_review_labels(
            reference=reference,
            status=(resolve_map.get(ref_id) or {}).get("status"),
            attempts=(resolve_map.get(ref_id) or {}).get("attempts") or [],
            adjudication=(((resolve_map.get(ref_id) or {}).get("evidence_profile") or {}).get(
                "bibliographic_adjudication"
            ) or {}),
            coverage=(((resolve_map.get(ref_id) or {}).get("evidence_profile") or {}).get(
                "resolver_coverage"
            ) or {}),
        )
        for ref_id, reference in refs_by_id.items()
    }
    for ref_id, reference in refs_by_id.items():
        status = (resolve_map.get(ref_id) or {}).get("status")
        concern = concerns_by_ref[ref_id]
        review_labels = review_labels_by_ref[ref_id]
        if status not in {None, "resolved"} or concern is not None or review_labels or (
            resolve_map.get(ref_id) or {}
        ).get("retracted") is True:
            display = source_display_by_id[ref_id]
            item = {
                "entity": "source",
                "ref_id": ref_id,
                "source_display_id": display["display_id"],
                "source_display_citation": display["display_citation"],
                "identity_status": status,
            }
            if concern is not None:
                item["bibliographic_concern"] = concern
            if review_labels:
                item["bibliographic_review_labels"] = review_labels
            attention.append(item)

    # Keep the source records independently addressable and cross-linked.
    sources = []
    for ref_id, reference in sorted(
        refs_by_id.items(), key=lambda item: (int(item[1].get("ref_number") or 0), item[0])
    ):
        sources.append(_json_value({
            **source_display_by_id[ref_id],
            "reference": reference,
            "resolve": resolve_map.get(ref_id),
            "bibliographic_concern": concerns_by_ref[ref_id],
            "bibliographic_review_labels": review_labels_by_ref[ref_id],
            "manifest_entries": _sorted_records(
                [entry for entry in manifest_entries if entry.get("ref_id") == ref_id],
                "source_text_id", "tier",
            ),
            "fetch_attempts": _sorted_records(
                _records((source.get("fetch_attempts_by_ref") or {}).get(ref_id, []), "fetch attempts"),
                "attempt_order", "attempt_id",
            ),
            "claim_ids": sorted({citation["claim_id"] for citation in citations_by_ref.get(ref_id, [])}),
            "claim_display_ids": sorted(
                {claim_display_by_id[citation["claim_id"]] for citation in citations_by_ref.get(ref_id, [])},
                key=lambda display_id: int(display_id[1:]),
            ),
        }))
    claims_output = []
    for claim_id, claim in claims_by_id.items():
        claim_pairs = [pair for pair in pairs if pair["claim_id"] == claim_id]
        claims_output.append(_json_value({
            "display_id": claim_display_by_id[claim_id],
            "focus_text": _claim_focus(claim),
            "claim": claim,
            "citations": _sorted_records(citations_by_claim[claim_id], "ref_id", "ref_number"),
            "ref_ids": sorted({str(citation["ref_id"]) for citation in citations_by_claim[claim_id]
                               if citation.get("ref_id") is not None}),
            "source_display_ids": [pair["source_display_id"] for pair in claim_pairs],
            "pair_ids": [
                {
                    "claim_id": pair["claim_id"],
                    "claim_display_id": pair["claim_display_id"],
                    "ref_id": pair["ref_id"],
                    "source_display_id": pair["source_display_id"],
                }
                for pair in claim_pairs
            ],
        }))

    table_only_output = []
    for entry in table_only_citations:
        ref_id = entry.get("ref_id")
        display = source_display_by_id.get(str(ref_id), {})
        table_only_output.append(_json_value({
            **entry,
            "source_display_id": display.get("display_id"),
            "source_display_citation": display.get("display_citation"),
        }))

    diagnostics = {
        "manual_parse_adjudication": _manual_parse_diagnostics(
            source.get("manual_parse_adjudication", [])
        ),
        "unreadable": source.get("unreadable", {}),
        "parse_coverage": parse.get("coverage", {}),
    }
    if source.get("legacy") is not None:
        diagnostics["legacy"] = source["legacy"]

    manuscript_output = {
        key: item for key, item in manuscript.items() if key != "input_path"
    }
    manuscript_output["display_title"] = _display_title(
        manuscript,
        source.get("manuscript_text"),
        allow_legacy_inference=source.get("legacy") is not None,
    )
    identity_counts = Counter(
        str((resolve_map.get(ref_id) or {}).get("status") or "unknown")
        for ref_id in refs_by_id
    )
    suspected_fabricated = sum(
        (concerns_by_ref[ref_id] or {}).get("level")
        in {"reference_refuted", "high_fabrication_suspicion"}
        for ref_id in refs_by_id
    )
    elevated_bibliographic_suspicion = sum(
        (concerns_by_ref[ref_id] or {}).get("level")
        == "elevated_bibliographic_suspicion"
        for ref_id in refs_by_id
    )
    completed_search_review_labels = sum(
        any(label.get("code") == "not_found_after_completed_searches"
            for label in review_labels_by_ref[ref_id])
        for ref_id in refs_by_id
    )
    coordinate_review_labels = sum(
        any(label.get("code") == "no_compatible_article_at_cited_coordinates"
            for label in review_labels_by_ref[ref_id])
        for ref_id in refs_by_id
    )
    incomplete_bibliographic_source_labels = sum(
        any(label.get("code") == "incomplete_bibliographic_source"
            for label in review_labels_by_ref[ref_id])
        for ref_id in refs_by_id
    )
    sources_retracted = sum(
        (resolve_map.get(ref_id) or {}).get("retracted") is True
        for ref_id in refs_by_id
    )
    assessment = _overview_assessment(
        claims_by_id=claims_by_id,
        refs_by_id=refs_by_id,
        citations_by_claim=citations_by_claim,
        rows_by_pair=rows_by_pair,
        resolve_map=resolve_map,
        parse_coverage=parse.get("coverage", {}),
        review_labels_by_ref=review_labels_by_ref,
    )

    value = _json_value({
        "schema_version": 1,
        "manuscript": manuscript_output,
        "execution": {
            "debug_mode": debug_mode,
            "debug_labels": list(debug_labels),
        },
        "overview": {
            "claims_total": len(claims),
            "references_total": len(references),
            "references_cited": len(citations_by_ref),
            "citations_total": len(citations),
            "orphan_citations": sum(
                citation.get("ref_id") is None for citation in citations
            ),
            "pairs_total": len(pair_keys),
            "pairs_completed": sum(bool(row.get("operational_complete")) for row in pair_rows),
            "pairs_by_semantic_outcome": dict(sorted(outcome_counts.items())),
            "pairs_by_result_class": dict(sorted(result_counts.items())),
            "claims_with_crediting_support": sum(
                any((pair["verification"] or {}).get("crediting") for pair in pairs if pair["claim_id"] == claim_id)
                for claim_id in claims_by_id
            ),
            "source_identity_counts": dict(sorted(identity_counts.items())),
            "sources_identity_confirmed": identity_counts["resolved"],
            "sources_identity_not_automatically_confirmed": (
                len(references) - identity_counts["resolved"]
            ),
            "sources_suspected_fabricated": suspected_fabricated,
            "sources_elevated_bibliographic_suspicion": (
                elevated_bibliographic_suspicion
            ),
            "sources_completed_search_review": completed_search_review_labels,
            "sources_coordinate_lookup_review": coordinate_review_labels,
            "sources_incomplete_bibliographic_source": incomplete_bibliographic_source_labels,
            "sources_retracted": sources_retracted,
            "table_only_citations": len(table_only_output),
            "assessment": assessment,
        },
        "remediation": _remediation_projection(assessment, pairs, source),
        "attention": sorted(attention, key=lambda item: (item["entity"], str(item.get("claim_id") or ""), str(item.get("ref_id") or ""))),
        "configuration": {
            "verify_runtime": source.get("verify_runtime", {}),
            "verify_policy": source.get("verify_policy", {}),
            "credentials": _credential_projection(source.get("credential_metrics")),
        },
        "models": source.get("llm_metrics", {"by_provider_model_role": [], "totals": {}}),
        "table_only_citations": table_only_output,
        "sources": sources,
        "claims": claims_output,
        "pairs": pairs,
        "diagnostics": diagnostics,
    })
    return HumanReportProjection(
        _json_value(sanitize_local_paths(_label_safe_query_contracts(value)))
    )
