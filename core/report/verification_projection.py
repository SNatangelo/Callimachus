# core/report/verification_projection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure, fail-closed public projection of claim-evidence terminals."""

from __future__ import annotations

from collections import defaultdict

_OUTCOMES = frozenset({"supports", "partial", "contradicts", "related", "off_topic", "non_decidable"})
_TERMINAL = frozenset({"accepted", "uncertain", "exhausted", "deadline_exceeded", "cancelled", "infrastructure_error"})
_RESOLUTIONS = frozenset({"jury2_accepted", "jury2_off", "jury2_not_eligible", "jury2_rejected_nonbinding", "majority_fallback", "jury1_guard", "jury1_technical", "jury2_technical", "jury2_rejected", "no_consensus", "jury1_provider_uncertain", "jury2_provider_uncertain", "cancelled", "bibliographic_identity_not_corroborated", "structural_claim_contamination", "source_integrity_unavailable", "source_identity_target_unavailable", "source_identity_check_unavailable"})
_RESULT_CLASSES = {
    "supports": "positive",
    "partial": "incomplete",
    "contradicts": "negative",
    "related": "negative",
    "off_topic": "negative",
    "non_decidable": "unresolved",
}
_KNOWN_SCOPES = frozenset({
    "fulltext_complete",
    "abstract_only",
    "abstract_fallback",
    "preview_snippet",
    "web_secondhand",
})
_ABSTRACT_SCOPES = frozenset({"abstract_only", "abstract_fallback"})
_INCONCLUSIVE_OUTCOMES = frozenset({"partial", "related", "off_topic", "non_decidable"})
_NONSEMANTIC_UNCERTAIN_RESOLUTIONS = frozenset({
    "bibliographic_identity_not_corroborated",
    "structural_claim_contamination",
    "source_integrity_unavailable",
    "source_identity_target_unavailable",
    "source_identity_check_unavailable",
})


def verification_reliability(row):
    """Return the deterministic report reliability for one selected terminal."""
    if row.get("scope") == "preview_snippet":
        return "low"
    return "high" if row.get("assurance") == "jury2_passed" else "medium"


def _is_inconclusive_for_preview(row):
    return (
        row.get("semantic_outcome", row.get("terminal_outcome")) in _INCONCLUSIVE_OUTCOMES
        or row.get("result_class") == "unresolved"
        or row.get("assurance") == "contested"
        or row.get("status") in {"uncertain", "exhausted", "deadline_exceeded", "cancelled", "infrastructure_error"}
    )


def select_verification_pair_rows(rows):
    """Select one admissible verification row per (claim, ref) pair.

    The raw verification projection remains one row per pair and evidence
    scope for sealing and audit. Pair-level consumers must use this selector
    so source quality, rather than insertion order, determines the terminal.
    """
    by_pair = defaultdict(dict)
    for row in rows or ():
        scope = row.get("scope")
        if scope not in _KNOWN_SCOPES:
            raise ValueError("verification scope is missing or invalid")
        pair = (row.get("claim_id"), row.get("ref_id"))
        if scope in by_pair[pair]:
            raise ValueError("verification pair has duplicate scope rows")
        by_pair[pair][scope] = row

    selected = []
    for pair in sorted(by_pair, key=lambda value: (str(value[0]), str(value[1]))):
        scoped = by_pair[pair]
        abstracts = _ABSTRACT_SCOPES & scoped.keys()
        if len(abstracts) > 1:
            raise ValueError("verification pair has incompatible abstract scopes")
        fulltext = scoped.get("fulltext_complete")
        if fulltext is not None:
            selected.append(fulltext)
            continue
        preview = scoped.get("preview_snippet")
        if preview is not None:
            abstract = next((scoped[scope] for scope in _ABSTRACT_SCOPES if scope in scoped), None)
            if abstract is not None and not _is_inconclusive_for_preview(abstract):
                selected.append(abstract)
            else:
                selected.append(preview)
            continue
        if abstracts:
            selected.append(scoped[next(iter(abstracts))])
    return selected


def project_verification_pairs(pair_states, candidates=(), candidate_events=()):
    """Return deterministic, public pair rows from immutable verify facts.

    Current lifecycle states are authoritative; non-terminal pairs produce no
    public projection rows.
    """
    by_pair = defaultdict(list)
    for candidate in candidates or ():
        by_pair[(candidate.get("claim_id"), candidate.get("ref_id"), candidate.get("scope") or "")].append(candidate)
    events = defaultdict(list)
    for event in candidate_events or ():
        events[event.get("candidate_id")].append(event)
    rows = []
    for state in sorted(pair_states or (), key=lambda r: (str(r.get("claim_id")), str(r.get("ref_id")), str(r.get("scope") or ""))):
        status = state.get("status")
        if status not in _TERMINAL:
            continue
        key = (state.get("claim_id"), state.get("ref_id"), state.get("scope") or "")
        resolution = str(state.get("terminal_cause") or "")
        if resolution not in _RESOLUTIONS:
            raise ValueError("verification terminal resolution is missing or invalid")
        allowed_statuses = {
            "jury2_accepted": {"accepted"},
            "jury2_off": {"accepted"},
            "jury2_not_eligible": {"accepted"},
            "jury2_rejected_nonbinding": {"uncertain"},
            "majority_fallback": {"uncertain"},
            "jury1_guard": {"exhausted"},
            "jury1_technical": {"exhausted"},
            "jury2_technical": {"exhausted"},
            "jury2_rejected": {"exhausted"},
            "no_consensus": {"exhausted"},
            "jury1_provider_uncertain": {"uncertain"},
            "jury2_provider_uncertain": {"uncertain"},
            "cancelled": {"cancelled"},
            "bibliographic_identity_not_corroborated": {"uncertain"},
            "structural_claim_contamination": {"uncertain"},
            "source_integrity_unavailable": {"uncertain"},
            "source_identity_target_unavailable": {"uncertain"},
            "source_identity_check_unavailable": {"uncertain"},
        }
        if status not in allowed_statuses[resolution]:
            raise ValueError("terminal status contradicts its resolution")
        terminal_candidates = []
        for candidate in by_pair[key]:
            for event in events.get(candidate.get("candidate_id"), ()):
                if event.get("event_type") == "terminal":
                    payload = event.get("payload") or {}
                    if payload.get("resolution") != resolution:
                        raise ValueError("terminal event resolution contradicts pair state")
                    terminal_candidates.append((candidate, payload))
        if len(terminal_candidates) > 1:
            raise ValueError("verification pair has conflicting terminal candidates")
        candidate, terminal = terminal_candidates[0] if terminal_candidates else (None, {})
        outcome = state.get("terminal_outcome")
        if resolution in _NONSEMANTIC_UNCERTAIN_RESOLUTIONS:
            if (
                outcome is not None
                or state.get("winner_call_id") is not None
                or by_pair[key]
                or terminal_candidates
            ):
                raise ValueError("nonsemantic uncertain terminal must not expose LLM candidates or outcomes")
            assurance = "non_crediting"
            jury2 = None
            result_class = "unresolved"
            crediting = False
        elif (
            status in {"exhausted", "deadline_exceeded", "cancelled", "infrastructure_error"}
            or resolution in {"jury1_provider_uncertain", "jury2_provider_uncertain"}
        ):
            if outcome is not None or state.get("winner_call_id") is not None:
                raise ValueError("terminal without a semantic outcome cannot expose an outcome")
            assurance = "non_crediting"
            jury2 = False if resolution in {"jury2_rejected", "no_consensus"} else None
            result_class = "unresolved"
            crediting = False
        else:
            if outcome not in _OUTCOMES:
                raise ValueError("terminal semantic outcome is invalid")
            if not state.get("winner_call_id") or candidate is None:
                raise ValueError("semantic terminal lacks its winner candidate")
            if candidate.get("candidate_id") != state.get("winner_call_id"):
                raise ValueError("terminal winner does not match pair state")
            if candidate.get("outcome") != outcome:
                raise ValueError("terminal winner outcome contradicts pair state")
            if outcome == "related" and resolution == "jury2_accepted":
                raise ValueError("related cannot be represented as Jury2 accepted")
            if resolution == "jury2_off":
                assurance, jury2 = "not_evaluated", None
            elif outcome in {"related", "off_topic", "non_decidable"} and resolution == "jury2_not_eligible":
                assurance, jury2 = "not_evaluated", None
            elif resolution == "jury2_accepted":
                if outcome not in {"supports", "partial", "contradicts"}:
                    raise ValueError("Jury2 accepted a non-evidence outcome")
                assurance, jury2 = "jury2_passed", True
            elif resolution in {"jury2_rejected_nonbinding", "majority_fallback"}:
                if outcome not in {"supports", "partial", "contradicts"}:
                    raise ValueError("contested terminal has a non-evidence outcome")
                assurance, jury2 = "contested", False
            else:
                raise ValueError("semantic terminal resolution is inconsistent")
            result_class = _RESULT_CLASSES[outcome]
        crediting = result_class == "positive" and assurance != "contested"
        raw_assurance = terminal.get("assurance")
        expected_raw = {"jury2_passed": "passed", "not_evaluated": "not_evaluated", "contested": "contested", "non_crediting": "non_crediting"}[assurance]
        if raw_assurance is not None and raw_assurance != expected_raw:
            raise ValueError("terminal assurance contradicts pair state")
        evidence = (
            list(candidate.get("grounded") or [])
            if outcome in {"supports", "partial", "contradicts"}
            and candidate is not None
            else []
        )
        if outcome in {"supports", "partial", "contradicts"} and not evidence:
            raise ValueError("evidence outcome lacks representative evidence")
        rows.append({"claim_id": key[0], "ref_id": key[1], "scope": key[2], "semantic_outcome": outcome,
                     "assurance": assurance, "resolution": resolution, "jury2_passed": jury2,
                     "operational_terminal": True, "operational_complete": True,
                     "result_class": result_class, "crediting": crediting,
                     "representative_candidate_id": (
                         candidate.get("candidate_id") if outcome is not None and candidate else None
                     ),
                     "evidence": evidence, "evidence_bearing": outcome in {"supports", "partial", "contradicts"}})
    return rows


verification_projection = project_verification_pairs
