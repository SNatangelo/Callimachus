#!/usr/bin/env python3
# core/report/rollup.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Typed verification rollup for claim badges and terminal health."""

from __future__ import annotations

from collections import Counter, defaultdict

try:
    from core.report.verification_projection import select_verification_pair_rows
except ImportError:  # direct execution
    from verification_projection import select_verification_pair_rows


BADGE = {"ok": "✅", "warn": "⚠️", "fail": "❌", "unstable": "⚪", "na": "·"}

_TERMINAL_PAIR_STATUSES = frozenset({
    "accepted", "uncertain", "exhausted", "deadline_exceeded",
    "cancelled", "infrastructure_error",
})
_EXHAUSTED_PAIR_STATUSES = frozenset({
    "exhausted", "deadline_exceeded", "cancelled", "infrastructure_error",
})
_MECHANICAL_TERMINAL_CAUSES = frozenset({
    "ambiguous_quote_match",
    "evidence_audit_protocol_invalid",
    "evidence_cardinality_invalid",
    "grounding_invalid",
    "invalid_outcome",
    "malformed_json",
    "missing_note",
    "no_passages",
    "numeric_near_miss",
    "passage_not_found",
    "passages_not_found",
    "passages_on_off_topic",
    "protocol_invalid",
    "provenance_invalid",
    "quote_missing",
    "schema_invalid",
    "source_span_invalid",
})
_SEMANTIC_TERMINAL_CAUSES = frozenset({
    "attribution_contested",
    "citation_attribution_contested",
    "citation_no_proposition",
    "contested_negative",
    "cross_judge_outcome_conflict",
    "evidence_audit_contested",
    "identity_corroborated_off_topic",
    "isolated_breaker",
    "isolated_uncertain",
    "jury1_provider_uncertain",
    "jury2_provider_uncertain",
    "majority_fallback",
    "negative_on_malformed_focus_not_evaluable",
    "soft_outcome_disagreement",
    "verdict_on_unevaluable_focus",
})
_INFRASTRUCTURE_TERMINAL_CAUSES = frozenset({
    "active_claim_budget_exceeded",
    "backend_error",
    "cancelled",
    "credential_invalid",
    "deadline_exceeded",
    "infrastructure_error",
    "jury_attempt_timeout",
    "lane_unavailable",
    "not_started",
    "provider_failure",
    "rate_limited",
    "timeout",
    "transport",
})
_DETERMINISTIC_GUARD_TERMINAL_CAUSES = frozenset({
    "bibliographic_identity_not_corroborated",
    "structural_claim_contamination",
    "source_integrity_unavailable",
    "source_identity_target_unavailable",
    "source_identity_check_unavailable",
})

_ATTEMPT_INHERITING_TERMINAL_CAUSES = frozenset({
    "jury1_technical",
    "jury2_technical",
    "jury_exhausted",
})


def effective_multisource_claim_ids(citations):
    """Return claims with effective multi-source evidence in the persisted data.

    ``is_multisource`` is a parser convenience field and is not a reporting
    contract. The durable evidence is two distinct resolved citation references.
    """
    refs_by_claim = defaultdict(set)
    for citation in citations or ():
        claim_id = citation.get("claim_id")
        ref_id = citation.get("ref_id")
        if claim_id and ref_id:
            refs_by_claim[claim_id].add(ref_id)
    return {claim_id for claim_id, refs in refs_by_claim.items() if len(refs) > 1}


def abstract_availability(ref_id, prov_by_ref, resolve_map):
    prov = prov_by_ref.get(ref_id, [])
    abstract_entry = next((e for e in prov if e.get("tier") == "abstract"), None)
    if abstract_entry:
        origin = abstract_entry.get("origin")
        return "available", (f"{origin} stored abstract" if origin else "stored abstract")
    ex = resolve_map.get(ref_id, {}) or {}
    if ex.get("abstract"):
        via = ex.get("via") or "metadata"
        suffix = "catalog" if via in ("openlibrary", "googlebooks") else "metadata"
        return "available", f"{via} {suffix}"
    return "absent", None


def _terminal_state_order(state, ordinal=0):
    terminal_cause = str(state.get("terminal_cause") or "")
    return (
        str(state.get("terminal_at") or ""),
        str(state.get("scope") or ""),
        1 if terminal_cause else 0,
        terminal_cause,
        str(state.get("status") or ""),
        str(state.get("terminal_outcome") or ""),
        str(state.get("winner_call_id") or ""),
        ordinal,
    )


def latest_terminal_causes(pair_states):
    """Return the latest persisted terminal cause for each non-accepted pair.

    The mapping is deliberately open-ended: future causes remain visible to the
    report instead of being collapsed into isolated-judge or retry labels.
    """
    result = {}
    for pair, state in latest_terminal_states(pair_states).items():
        if state.get("status") in {"open", "accepted"}:
            continue
        cause = state.get("terminal_cause")
        if cause:
            result[pair] = str(cause)

    return result


def _latest_terminal_states_by_scope(pair_states):
    """Resolve append-only history before selecting across evidence scopes."""
    latest = {}
    for ordinal, state in enumerate(pair_states or ()):
        if state.get("status") not in _TERMINAL_PAIR_STATUSES:
            continue
        key = (state.get("claim_id"), state.get("ref_id"), state.get("scope"))
        order = _terminal_state_order(state, ordinal)
        previous = latest.get(key)
        if previous is None or order >= previous[0]:
            latest[key] = (order, state)
    return [value[1] for value in latest.values()]


def latest_terminal_states(pair_states):
    """Return the authoritative terminal lifecycle row for each (claim, ref).

    Historical rows in a scope are reduced deterministically first. The shared
    evidence-scope selector then chooses the pair-level terminal.
    """
    return {
        (state.get("claim_id"), state.get("ref_id")): state
        for state in select_verification_pair_rows(
            _latest_terminal_states_by_scope(pair_states)
        )
    }


def terminal_pair_counts(
    all_pairs,
    pair_states=(),
    terminal_causes_by_pair=None,
    usable_text_refs=frozenset(),
    unreadable_ref_ids=frozenset(),
):
    """Classify each citation pair from its terminal lifecycle state.

    The returned sets are disjoint and cover the known pair universe. A pair
    with usable text but without a typed terminal state remains ``unaccounted``
    and therefore fails completion checks.
    """
    universe = set(all_pairs)
    states = latest_terminal_states(pair_states)
    accepted = set()
    causes = terminal_causes_by_pair or {}
    uncertain = set()
    exhausted = set()

    for pair, state in states.items():
        if pair not in universe:
            continue
        status = state.get("status")
        if status == "uncertain":
            uncertain.add(pair)
        elif status in _EXHAUSTED_PAIR_STATUSES:
            exhausted.add(pair)
        elif status == "accepted":
            accepted.add(pair)

    no_usable_text = {
        pair for pair in universe - accepted - uncertain - exhausted
        if pair[1] not in usable_text_refs or pair[1] in unreadable_ref_ids
    }
    unaccounted = universe - accepted - uncertain - exhausted - no_usable_text
    uncertain_by_cause = Counter(
        str(causes.get(pair) or "unknown") for pair in uncertain
    )
    contested_negative = {
        pair for pair in uncertain
        if causes.get(pair) == "contested_negative"
    }
    return {
        "accepted": accepted,
        "uncertain": uncertain,
        "uncertain_by_cause": dict(sorted(uncertain_by_cause.items())),
        "contested_negative": contested_negative,
        "exhausted": exhausted,
        "no_usable_text": no_usable_text,
        "unaccounted": unaccounted,
        "terminal": accepted | uncertain | exhausted | no_usable_text,
    }


def isolated_uncertain_pairs(pair_states):
    """Return only pairs whose latest terminal cause is isolated uncertain.

    A generic ``outcome=uncertain`` is not evidence that the isolated judge
    produced the terminal result.
    """
    return {
        pair for pair, cause in latest_terminal_causes(pair_states).items()
        if cause == "isolated_uncertain"
    }


def terminal_failure_dimension(cause, attempt_causes=()):
    """Classify one non-accepted terminal without conflating semantics.

    ``jury_exhausted`` is only a lifecycle label.  It inherits a dimension
    when every useful persisted attempt cause agrees; otherwise it remains
    explicitly unclassified.
    """
    normalized = str(cause or "").strip()
    if normalized in _MECHANICAL_TERMINAL_CAUSES:
        return "mechanical"
    if normalized in _SEMANTIC_TERMINAL_CAUSES:
        return "semantic"
    if normalized in _INFRASTRUCTURE_TERMINAL_CAUSES:
        return "infrastructure"
    if normalized in _DETERMINISTIC_GUARD_TERMINAL_CAUSES:
        return "deterministic_guard"
    if normalized.startswith("source_span_") or normalized.endswith(
        "_protocol_invalid"
    ):
        return "mechanical"
    if normalized.endswith("_contested") or normalized.endswith("_uncertain"):
        return "semantic"

    if not normalized or normalized in _ATTEMPT_INHERITING_TERMINAL_CAUSES:
        inherited = {
            terminal_failure_dimension(item)
            for item in attempt_causes
            if str(item or "").strip() not in {
                "", "jury_exhausted",
            }
        }
        if len(inherited) == 1 and "unclassified" not in inherited:
            return inherited.pop()
    return "unclassified"


def terminal_attempt_causes_by_pair(raw_verification):
    """Project physical dispatch failure causes onto their claim/source pair.

    Only immutable failed/abandoned events from a logical request that never
    completed contribute. A recovered retry is not a terminal failure cause,
    and candidate diagnostics do not alter the pair-level denominator.
    """
    raw_verification = raw_verification or {}
    request_pairs = {}
    for request in raw_verification.get("logical_requests") or ():
        request_id = request.get("logical_request_id")
        claim_id = request.get("claim_id")
        ref_id = request.get("ref_id")
        if not request_id or not claim_id or not ref_id:
            raise ValueError("logical request is missing its claim/source identity")
        pair = (claim_id, ref_id)
        previous = request_pairs.get(request_id)
        if previous is not None and previous != pair:
            raise ValueError("logical request has divergent claim/source identity")
        request_pairs[request_id] = pair

    completed_requests = set()
    failed_events = []
    for event in raw_verification.get("dispatch_events") or ():
        event_type = event.get("event_type")
        if event_type == "completed":
            completed_requests.add(event.get("logical_request_id"))
            continue
        if event_type not in {"failed", "abandoned"}:
            continue
        failed_events.append(event)

    causes_by_attempt = {}
    for event in failed_events:
        if event.get("logical_request_id") in completed_requests:
            continue
        pair = request_pairs.get(event.get("logical_request_id"))
        if pair is None:
            raise ValueError("terminal dispatch event has no logical request identity")
        attempt_id = event.get("dispatch_attempt_id")
        cause = str((event.get("payload") or {}).get("technical_result") or "").strip()
        if not attempt_id or not cause:
            raise ValueError("terminal dispatch failure is missing its technical cause")
        key = (pair, attempt_id)
        previous = causes_by_attempt.get(key)
        if previous is not None and previous != cause:
            raise ValueError("dispatch attempt has divergent technical causes")
        causes_by_attempt[key] = cause

    projected = defaultdict(list)
    for (pair, attempt_id), cause in sorted(
        causes_by_attempt.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        projected[pair].append(cause)
    return {pair: tuple(causes) for pair, causes in projected.items()}


def terminal_health_dimensions(
    pair_terminal,
    terminal_causes_by_pair,
    terminal_pairs,
    attempt_causes_by_pair=None,
):
    """Return pair-level protocol, semantic, and infrastructure cohorts.

    Candidate/retry diagnostics cannot change the pair-level denominator.
    """
    lifecycle_pairs = (
        set(pair_terminal.get("accepted", ()))
        | set(pair_terminal.get("uncertain", ()))
        | set(pair_terminal.get("exhausted", ()))
        | set(pair_terminal.get("unaccounted", ()))
    )
    eligible_pairs = set(terminal_pairs) & lifecycle_pairs

    dimensions = {
        "mechanical": set(),
        "semantic": set(),
        "infrastructure": set(),
        "deterministic_guard": set(),
        "unclassified": set(),
    }
    accepted_pairs = set(pair_terminal.get("accepted", ()))
    attempt_causes_by_pair = attempt_causes_by_pair or {}
    for pair in eligible_pairs - accepted_pairs:
        dimension = terminal_failure_dimension(
            terminal_causes_by_pair.get(pair),
            attempt_causes_by_pair.get(pair, ()),
        )
        dimensions[dimension].add(pair)

    return {
        "eligible_pairs": eligible_pairs,
        "accepted_pairs": accepted_pairs & eligible_pairs,
        "mechanical_pairs": dimensions["mechanical"],
        "semantic_pairs": dimensions["semantic"],
        "infrastructure_pairs": dimensions["infrastructure"],
        "deterministic_guard_pairs": dimensions["deterministic_guard"],
        "unclassified_pairs": dimensions["unclassified"],
    }



def crediting_result(results_by_pair, claim_id, ref_id):
    """Return the typed semantic projection for one claim/reference pair."""
    return results_by_pair.get((claim_id, ref_id))
def claim_badge(claim, cites, crediting_by_pair, resolve_map, style_map, attempted_pairs,
                unreadable_ref_ids=frozenset(), isolated_uncertain=frozenset(),
                extraction_suspect_refs=frozenset(), terminal_causes_by_pair=None):
    """DETERMINISTIC raw rollup. Does NOT know about fine coverage (Interpreter
    orphans): that is shown separately, never merged here."""
    ref_ids = [c["ref_id"] for c in cites if c.get("ref_id")]
    ambiguous = [c for c in cites if not c.get("ref_id") and c.get("candidate_ref_ids")]
    orphan = [c for c in cites if not c.get("ref_id") and not c.get("candidate_ref_ids")]
    if orphan:
        # Marker that resolves to NO bibliography entry: hard manuscript defect.
        which = ", ".join(c.get("marker_raw") or f"[{c.get('ref_number')}]" for c in orphan)
        return "fail", f"citation not in bibliography: {which}"
    if ambiguous and not ref_ids:
        which = ", ".join(c.get("marker_raw", "?") for c in ambiguous)
        return "warn", f"ambiguous citation, confirm source: {which}"
    if not ref_ids:
        return "fail", "orphan citation (marker does not resolve to any reference)"
    notes = []
    if ambiguous:
        notes.append("one citation of this claim is ambiguous (needs confirmation)")
    outcomes = []
    # Fabrication (red) ONLY with a unique identifier that is absent or points elsewhere.
    any_fab = any(resolve_map.get(rid, {}).get("status") in ("not_found", "identifier_mismatch")
                  for rid in ref_ids)
    any_mismatch = any(resolve_map.get(rid, {}).get("status") == "identifier_mismatch"
                       for rid in ref_ids)
    any_retracted = any(resolve_map.get(rid, {}).get("retracted") for rid in ref_ids)
    # 'unverified' = existence unconfirmed, NEVER fabrication: note, not fail.
    any_unresolved = any(resolve_map.get(rid, {}).get("status")
                         in (None, "unresolved", "unverified", "skipped")
                         for rid in ref_ids)
    any_title_warn = any(resolve_map.get(rid, {}).get("title_flag") == "warn"
                         for rid in ref_ids)
    # Book existence searched in BOTH catalogs (OpenLibrary + Google Books) and not
    # found: NOT fabrication (catalogs are not exhaustive), but a LOUD orange warning,
    # distinct from a plain "couldn't check".
    any_searched_not_found = any(
        resolve_map.get(rid, {}).get("existence_corroboration") == "searched_not_found"
        for rid in ref_ids)
    style_err = any(any(d["severity"] == "error" for d in style_map.get(rid, {}).get("deviations", []))
                    for rid in ref_ids)
    missing_text = False    # never attempted (no text)
    missing_ref_ids = set()
    unstable_pair = False   # terminal without a semantic outcome
    bad_passage = False
    inconclusive_abstract = False  # negative/partial seen only on the abstract
    support_relis = []             # a source whose full text exists (paywall not read)
    isolated_uncertain_seen = False  # unstable pair caused by second-stage judge (giudice 2)
    unstable_terminal_causes = set()
    contested_outcomes = set()
    terminal_causes_by_pair = terminal_causes_by_pair or {}
    for rid in ref_ids:
        v = crediting_result(crediting_by_pair, claim["id"], rid)
        if v is None:
            pair = (claim["id"], rid)
            cause = terminal_causes_by_pair.get(pair)
            if pair in attempted_pairs or cause:
                unstable_pair = True
                if cause:
                    unstable_terminal_causes.add(str(cause))
                if pair in isolated_uncertain or cause == "isolated_uncertain":
                    isolated_uncertain_seen = True
            else:
                missing_text = True
                missing_ref_ids.add(rid)
            continue
        out = v.get("outcome")
        if v.get("assurance") == "contested":
            contested_outcomes.add(out)
        # Deterministic reinterpretation (no model prompt): a negative/partial outcome
        # seen ONLY on the abstract is NOT conclusive if the full text exists (e.g.
        # paywalled, not read) — the data may be in the unread text. If instead the
        # full text does NOT exist (conference abstract), the abstract IS the source
        # and the outcome remains conclusive.
        ft_exists = resolve_map.get(rid, {}).get("fulltext_exists")
        if (out in ("off_topic", "related", "partial")
                and v.get("scope") == "abstract_only" and ft_exists is not False):
            inconclusive_abstract = True
            continue  # does not count as a hard negative or as support
        outcomes.append(out)
        if out == "non_decidable":
            notes.append("semantic outcome unresolved (non_decidable)")
        if out in ("supports", "partial"):
            support_relis.append(v.get("reliability", "medium"))
        if out in ("supports", "partial", "related", "contradicts") and not v.get("passage_verified"):
            bad_passage = True

    if contested_outcomes:
        notes.append("semantic outcome contested by Jury2")
    if any_fab:
        return "fail", ("DOI/PMID resolves to a different work (possibly wrong DOI)"
                        if any_mismatch else "source unresolved (possible fabrication)")
    if any_retracted:
        return "fail", "RETRACTED source"
    if "contradicts" in outcomes:
        if "contradicts" in contested_outcomes:
            return "warn", "a source may contradict the claim; semantic outcome contested by Jury2"
        return "fail", "a source contradicts the claim"
    # Hard fail only when EVERY source is genuinely extraneous (off-topic). A claim whose
    # sources are on-topic but individually unsupportive ("related") is a softer warning,
    # not a fabrication-grade failure.
    if outcomes and all(o == "off_topic" for o in outcomes):
        return "fail", "no source addresses the claim (sources are off-topic)"
    if unstable_pair:
        if unstable_terminal_causes:
            for cause in sorted(unstable_terminal_causes):
                if cause == "isolated_uncertain":
                    notes.append("uncertain verification (isolated_uncertain: isolated "
                                 "evidence insufficient)")
                else:
                    notes.append(f"uncertain verification (terminal cause: {cause})")
        elif isolated_uncertain_seen:
            notes.append("uncertain verification (isolated_uncertain: isolated evidence "
                         "insufficient)")
        else:
            notes.append("terminal verification without a semantic outcome")
    pending_ocr_refs = missing_ref_ids.intersection(unreadable_ref_ids)
    any_extraction_suspect = any(rid in extraction_suspect_refs for rid in ref_ids)
    if pending_ocr_refs:
        notes.append("full text FOUND but UNREADABLE (scanned/no OCR): source NOT checked — OCR pending")
    if missing_ref_ids - pending_ocr_refs:
        notes.append("source without text/verification")
    if any_unresolved:
        notes.append("bibliographic identity not automatically confirmed")
    if inconclusive_abstract:
        notes.append("inconclusive: only the abstract was read (existing full text not retrieved)")
    if any_title_warn:
        notes.append("source title does not fully match the DOI (check manually)")
    if any_searched_not_found:
        notes.append("⚠️ book not found in OpenLibrary or Google Books "
                     "(existence not corroborated — possible fabrication, verify manually)")
    # On-topic but none substantiates the claim: surface it as guidance (not a hard fail).
    if any(o == "related" for o in outcomes) and not any(
            o in ("supports", "partial") for o in outcomes):
        notes.append("sources are on-topic but none substantiate the claim")
    if bad_passage:
        notes.append("quoted passage not verified")
    if any_extraction_suspect and (unstable_pair or bad_passage):
        notes.append("quote failures may be extraction artifacts, not manuscript problems")
    if style_err:
        notes.append("style error")
    # Support only from an attributed preview snippet cannot be green.
    only_indirect = support_relis and all(r == "low" for r in support_relis)
    if only_indirect:
        notes.insert(0, "indirect/preview support only (low reliability)")
    if unstable_pair and not outcomes and not inconclusive_abstract:
        return "unstable", "; ".join(notes)
    if outcomes and all(o == "non_decidable" for o in outcomes):
        return "unstable", "; ".join(notes) or "semantic outcome unresolved (non_decidable)"
    if "partial" in outcomes or notes:
        return "warn", "; ".join(notes) or "partial support"
    if outcomes and any(o == "supports" for o in outcomes):
        return "ok", "supported"
    return "unstable", "no semantic verification outcome"
