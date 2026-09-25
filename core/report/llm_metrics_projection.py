# core/report/llm_metrics_projection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure diagnostic projection of immutable, run-local LLM observations."""

from __future__ import annotations

from collections import defaultdict


_JURY1_FLOW_STAGES = {
    "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
    "explanation_evidence",
}


def _role(stage):
    return "jury1" if stage in _JURY1_FLOW_STAGES else stage or "unknown"


def _unique_records(records, identity_field, label):
    unique = {}
    for record in records or ():
        identity = record.get(identity_field)
        if not isinstance(identity, str) or not identity:
            raise ValueError(f"{label} is missing {identity_field}")
        previous = unique.get(identity)
        if previous is not None:
            if previous != record:
                raise ValueError(f"{label} has divergent observations for {identity_field}")
            continue
        unique[identity] = record
    return tuple(unique.values())


def _unique_pair_states(pair_states):
    unique = {}
    for state in pair_states or ():
        claim_id = state.get("claim_id")
        ref_id = state.get("ref_id")
        scope = state.get("scope") or ""
        if not isinstance(claim_id, str) or not claim_id:
            raise ValueError("pair state is missing claim_id")
        if not isinstance(ref_id, str) or not ref_id:
            raise ValueError("pair state is missing ref_id")
        key = (claim_id, ref_id, scope)
        previous = unique.get(key)
        if previous is not None:
            if previous != state:
                raise ValueError("verification pair has divergent state observations")
            continue
        unique[key] = state
    return tuple(unique.values())


def project_llm_metrics(
    requests,
    dispatch_attempts,
    dispatch_events,
    candidates=(),
    candidate_events=(),
    pair_states=(),
    jury1_rejections=(),
):
    """Summarize unique technical observations without exposing credentials."""
    requests = _unique_records(requests, "logical_request_id", "logical request")
    dispatch_attempts = _unique_records(
        dispatch_attempts, "dispatch_attempt_id", "dispatch attempt"
    )
    dispatch_events = _unique_records(dispatch_events, "event_id", "dispatch event")
    candidates = _unique_records(candidates, "candidate_id", "candidate")
    candidate_events = _unique_records(candidate_events, "event_id", "candidate event")
    pair_states = _unique_pair_states(pair_states)
    jury1_rejections = _unique_records(
        jury1_rejections, "event_id", "Jury1 rejection"
    )
    request_by_id = {r["logical_request_id"]: r for r in requests}
    terminal_by_attempt = {}
    pacing_waits = 0
    pacing_wait_ms = 0
    for event in dispatch_events:
        if event.get("event_type") == "pacing_wait":
            pacing_waits += 1
            payload = event.get("payload") or {}
            duration = payload.get("duration_ms", payload.get("wait_ms", 0))
            if isinstance(duration, (int, float)) and duration >= 0:
                pacing_wait_ms += duration
        if event.get("event_type") in {"completed", "failed", "abandoned"}:
            key = event.get("dispatch_attempt_id")
            previous = terminal_by_attempt.get(key)
            if previous is not None and previous != event:
                raise ValueError("dispatch has contradictory terminal observations")
            terminal_by_attempt[key] = event
    candidate_by_request = {}
    candidate_by_id = {}
    for candidate in candidates:
        request_id = candidate.get("origin_logical_request_id")
        if request_id in candidate_by_request:
            raise ValueError("logical request has multiple admissible candidates")
        candidate_by_request[request_id] = candidate
        candidate_by_id[candidate["candidate_id"]] = candidate
    valid_attempt_by_request = {}
    for attempt in dispatch_attempts:
        terminal = terminal_by_attempt.get(attempt.get("dispatch_attempt_id"))
        if (terminal or {}).get("event_type") != "completed":
            continue
        if ((terminal.get("payload") or {}).get("technical_result") == "protocol_invalid"):
            continue
        request_id = attempt.get("logical_request_id")
        if request_id in valid_attempt_by_request:
            raise ValueError("logical request has multiple protocol-valid completions")
        valid_attempt_by_request[request_id] = attempt
    jury2 = defaultdict(set)
    terminal_resolutions = defaultdict(int)
    for event in candidate_events:
        if event.get("event_type") in {"jury2_yes", "jury2_no"}:
            candidate_id = event.get("candidate_id")
            if candidate_id not in candidate_by_id:
                raise ValueError("Jury2 event references unknown candidate")
            jury2[candidate_id].add(event.get("event_type"))
        elif not pair_states and event.get("event_type") == "terminal":
            resolution = (event.get("payload") or {}).get("resolution")
            if isinstance(resolution, str) and resolution:
                terminal_resolutions[resolution] += 1
    for state in pair_states:
        if state.get("status") == "open":
            continue
        resolution = state.get("terminal_cause")
        if not isinstance(resolution, str) or not resolution:
            raise ValueError("terminal pair state is missing its resolution")
        terminal_resolutions[resolution] += 1
    groups = defaultdict(lambda: defaultdict(int))
    for attempt in dispatch_attempts:
        aid = attempt.get("dispatch_attempt_id")
        request = request_by_id.get(attempt.get("logical_request_id")) or {}
        role = _role(request.get("stage"))
        group = (attempt.get("provider_id") or "unknown", attempt.get("model_id") or "unknown", attempt.get("credential_id") or "unknown", role)
        item = groups[group]; item["attempts"] += 1
        terminal = terminal_by_attempt.get(aid)
        payload = (terminal or {}).get("payload") or {}
        if role in {"jury1", "jury2"}:
            if (terminal or {}).get("event_type") == "completed":
                item["answers_received"] += 1
                if payload.get("technical_result") != "protocol_invalid": item["protocol_valid"] += 1
            elif payload.get("technical_result") == "protocol_invalid":
                item["answers_received"] += 1
        status = payload.get("http_status")
        if status == 429: item["http_429"] += 1
        if status in {401, 403}: item["auth_failures"] += 1
        if payload.get("technical_result") == "timeout": item["timeouts"] += 1
        if (terminal or {}).get("event_type") == "failed" and payload.get("technical_result") not in {"timeout"}: item["transport_failures"] += 1
        if isinstance(payload.get("latency_ms"), (int, float)): item["latency_ms_total"] += payload["latency_ms"]
    application_rejection_causes = defaultdict(int)
    for rejection in jury1_rejections:
        request_id = rejection.get("logical_request_id")
        request = request_by_id.get(request_id)
        attempt = valid_attempt_by_request.get(request_id)
        if request is None or _role(request.get("stage")) != "jury1":
            raise ValueError("Jury1 rejection references an invalid logical request")
        if attempt is None:
            raise ValueError("Jury1 rejection lacks a transport-decodable answer")
        cause = rejection.get("cause")
        if not isinstance(cause, str) or not cause:
            raise ValueError("Jury1 rejection cause is invalid")
        group = (
            attempt.get("provider_id") or "unknown",
            attempt.get("model_id") or "unknown",
            attempt.get("credential_id") or "unknown",
            "jury1",
        )
        groups[group]["application_rejections"] += 1
        if rejection.get("state_cause") == "schema_invalid":
            groups[group]["application_schema_rejections"] += 1
        application_rejection_causes[cause] += 1
    for request_id, candidate in candidate_by_request.items():
        # Attribute Jury1 admission to its immutable origin request's provider/model
        attempt = valid_attempt_by_request.get(request_id)
        if attempt is None:
            raise ValueError("admissible candidate lacks a protocol-valid origin response")
        group = (attempt.get("provider_id") or "unknown", attempt.get("model_id") or "unknown", attempt.get("credential_id") or "unknown", "jury1")
        groups[group]["jury1_admissible"] += 1
    for candidate_id, outcomes in jury2.items():
        candidate = candidate_by_id[candidate_id]
        # Judge distributions stay attributed to the Jury2 dispatch.
        event = next(
            event
            for event in candidate_events
            if event.get("candidate_id") == candidate_id
            and event.get("event_type") in {"jury2_yes", "jury2_no"}
        )
        request_id = (event.get("payload") or {}).get("logical_request_id")
        attempt = valid_attempt_by_request.get(request_id)
        if attempt is None:
            raise ValueError("Jury2 decision lacks a protocol-valid judge response")
        group = (attempt.get("provider_id") or "unknown", attempt.get("model_id") or "unknown", attempt.get("credential_id") or "unknown", "jury2")
        groups[group]["jury2_evaluated"] += 1
        groups[group]["jury2_yes"] += int("jury2_yes" in outcomes)
        groups[group]["jury2_no"] += int("jury2_no" in outcomes)
        origin = valid_attempt_by_request.get(candidate.get("origin_logical_request_id"))
        if origin is None:
            raise ValueError("Jury2 candidate lacks a protocol-valid origin response")
        origin_group = (origin.get("provider_id") or "unknown", origin.get("model_id") or "unknown", origin.get("credential_id") or "unknown", "jury1")
        groups[origin_group]["jury2_evaluated_origin"] += 1
        groups[origin_group]["jury2_yes_origin"] += int("jury2_yes" in outcomes)
        groups[origin_group]["jury2_no_origin"] += int("jury2_no" in outcomes)
    rows = []
    for (provider, model, alias, role), item in sorted(groups.items()):
        row = {"provider": provider, "model": model, "credential_alias": alias, "role": role,
            **{key: item.get(key, 0) for key in ("attempts", "answers_received", "protocol_valid", "application_rejections", "application_schema_rejections", "jury1_admissible", "jury2_evaluated", "jury2_yes", "jury2_no", "jury2_evaluated_origin", "jury2_yes_origin", "jury2_no_origin", "http_429", "auth_failures", "timeouts", "transport_failures", "latency_ms_total")}}
        rows.append(row)
    # Exact run-level denominators intentionally span role groups.
    total = defaultdict(int)
    for row in rows:
        for key, value in row.items():
            if isinstance(value, int) and key not in {"latency_ms_total"}: total[key] += value
    totals = dict(total)
    totals["logical_requests"] = len(requests)
    return {"by_provider_model_role": rows, "totals": totals,
            "pacing_waits": pacing_waits, "pacing_wait_ms": pacing_wait_ms,
            "terminal_resolutions": dict(sorted(terminal_resolutions.items())),
            "application_rejection_causes": dict(sorted(application_rejection_causes.items()))}


llm_metrics_projection = project_llm_metrics
