#!/usr/bin/env python3
# core/report/render.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Report renderer — the `render` function and `main` entry point."""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from core.invocation import run_command

try:
    from core.infra.integrity import signing as _signing
    from core.infra.integrity import IntegrityGateError, RunIntegrityGate
    from core.infra.integrity.execution_assurance import (
        downgrade_after_authority_failure,
        resolve_existing,
    )
    from core.resolve import sources as _sources
    from core.report.io import (
        load_run_projection, _load_style_projection, _repo_open,
    )
    from core.report.source_availability import (
        is_verifiable_source_entry,
        usable_source_refs,
        usable_verification_source_refs,
    )
    from core.report.rollup import (
        BADGE,
        abstract_availability,
        claim_badge,
        effective_multisource_claim_ids,
        isolated_uncertain_pairs,
        latest_terminal_causes,
        terminal_attempt_causes_by_pair,
        terminal_pair_counts,
        terminal_health_dimensions,
    )
    from core.report.verification_projection import (
        select_verification_pair_rows,
        verification_reliability,
    )
    from core.report.sealing import (
        journal_entry,
        journal_path,
        journal_separator,
        provenance_fields,
        seal_payload,
        strip_seal,
    )
except ImportError:
    import signing as _signing
    from infra.integrity import IntegrityGateError, RunIntegrityGate
    from infra.integrity.execution_assurance import (
        downgrade_after_authority_failure,
        resolve_existing,
    )
    import sources as _sources
    from io import load_run_projection, _load_style_projection, _repo_open
    from source_availability import (
        is_verifiable_source_entry,
        usable_source_refs,
        usable_verification_source_refs,
    )
    from rollup import (  # noqa: F401
        BADGE,
        abstract_availability,
        claim_badge,
        effective_multisource_claim_ids,
        isolated_uncertain_pairs,
        latest_terminal_causes,
        terminal_attempt_causes_by_pair,
        terminal_pair_counts,
        terminal_health_dimensions,
    )
    from verification_projection import (
        select_verification_pair_rows,
        verification_reliability,
    )
    from sealing import (  # noqa: F401
        journal_entry,
        journal_path,
        journal_separator,
        provenance_fields,
        seal_payload,
        strip_seal,
    )


DEBUG_REPORT_BANNER = (
    "> **DEBUG RUN — NOT RELIABLE FOR CITATION VERIFICATION**\n"
    "> This run was executed in debug mode. Its outputs are diagnostic only and\n"
    "> must not be treated as reliable citation-verification results."
)
UNPROTECTED_AGENT_REPORT_BANNER = (
    "> **UNPROTECTED AGENT RUN — NOT RELIABLE FOR CITATION VERIFICATION**\n"
    "> The human user chose to continue after agent-resistant integrity controls failed.\n"
    "> This report is diagnostic only and is never audit-ready."
)
_REPORT_INTEGRITY_STATES = {"clean", "debug_overridden", "unverifiable"}
_OVERRIDE_REPORT_FIELDS = (
    "override_id",
    "violation_id",
    "reason",
    "authenticated_caller",
    "created_at",
    "authority_id",
)
_RECOVERY_REPORT_FIELDS = (
    "recovery_id",
    "subject_scope",
    "transition_id",
    "expected_checkpoint_id",
    "last_heartbeat_at",
    "observed_manifest_sha256",
    "restored_manifest_sha256",
    "database_changed",
    "difference_count",
    "action_count",
    "created_at",
    "authority_id",
)


def _report_override_rows(audit_records) -> list[dict]:
    if audit_records is None:
        return []
    if not isinstance(audit_records, dict):
        raise IntegrityGateError("integrity authority audit records are malformed")
    rows = audit_records.get("overrides", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise IntegrityGateError("integrity authority overrides are malformed")
    return [
        {field: row.get(field) for field in _OVERRIDE_REPORT_FIELDS}
        for row in rows
    ]


def _report_recovery_rows(audit_records) -> list[dict]:
    if audit_records is None:
        return []
    if not isinstance(audit_records, dict):
        raise IntegrityGateError("integrity authority audit records are malformed")
    rows = audit_records.get("recoveries", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise IntegrityGateError("integrity authority crash recoveries are malformed")
    return [
        {field: row.get(field) for field in _RECOVERY_REPORT_FIELDS}
        for row in rows
    ]


def _validate_debug_settings(debug_mode, debug_labels) -> tuple[bool, list[str]]:
    if type(debug_mode) is not bool:
        raise IntegrityGateError("report debug mode is malformed")
    if (
        not isinstance(debug_labels, list)
        or any(not isinstance(label, str) or not label for label in debug_labels)
    ):
        raise IntegrityGateError("report debug labels are malformed")
    return debug_mode, list(debug_labels)


def _authority_subject_projection(label: str, checked: dict) -> dict:
    if not isinstance(checked, dict):
        raise IntegrityGateError(f"{label} integrity result is malformed")
    status = checked.get("status")
    if status not in {"clean", "debug_overridden"}:
        raise IntegrityGateError(f"{label} integrity state is not reportable")
    if type(checked.get("audit_ready")) is not bool:
        raise IntegrityGateError(f"{label} audit readiness is malformed")
    trust_domain_isolated = checked.get("trust_domain_isolated") is True
    return {
        "state": status if trust_domain_isolated else "unverifiable",
        "audit_ready": checked["audit_ready"] if trust_domain_isolated else False,
        "trust_domain_isolated": trust_domain_isolated,
        "overrides": _report_override_rows(checked.get("audit_records")),
        "crash_recoveries": _report_recovery_rows(
            checked.get("audit_records")
        ),
    }


def _run_debug_settings(run_dir: str) -> tuple[bool, list[str]]:
    repository = _repo_open(run_dir)
    if repository is None:
        raise IntegrityGateError("report integrity repository is unavailable")
    try:
        debug_mode = repository.get_run_setting("debug_mode", False)
        debug_labels = repository.get_run_setting("debug_labels", [])
    finally:
        repository.close()
    return _validate_debug_settings(debug_mode, debug_labels)


def _validate_report_integrity(integrity) -> dict:
    if not isinstance(integrity, dict):
        raise IntegrityGateError("report integrity projection is malformed")
    debug_mode, debug_labels = _validate_debug_settings(
        integrity.get("debug_mode"), integrity.get("debug_labels")
    )
    subjects = {}
    for key, label in (("run", "run"), ("content_store", "content store")):
        subject = integrity.get(key)
        if not isinstance(subject, dict):
            raise IntegrityGateError(f"{label} integrity projection is malformed")
        state = subject.get("state")
        if state not in _REPORT_INTEGRITY_STATES:
            raise IntegrityGateError(f"{label} integrity state is malformed")
        audit_ready = subject.get("audit_ready")
        if type(audit_ready) is not bool:
            raise IntegrityGateError(f"{label} audit readiness is malformed")
        if state != "clean" and audit_ready:
            raise IntegrityGateError(f"{label} audit readiness contradicts its state")
        overrides = subject.get("overrides")
        if not isinstance(overrides, list) or any(
            not isinstance(row, dict) for row in overrides
        ):
            raise IntegrityGateError(f"{label} integrity overrides are malformed")
        recoveries = subject.get("crash_recoveries", [])
        if not isinstance(recoveries, list) or any(
            not isinstance(row, dict) for row in recoveries
        ):
            raise IntegrityGateError(
                f"{label} integrity crash recoveries are malformed"
            )
        subjects[key] = {
            "state": state,
            "audit_ready": audit_ready,
            "overrides": [
                {field: row.get(field) for field in _OVERRIDE_REPORT_FIELDS}
                for row in overrides
            ],
            "crash_recoveries": [
                {field: row.get(field) for field in _RECOVERY_REPORT_FIELDS}
                for row in recoveries
            ],
        }
    audit_ready = integrity.get("audit_ready")
    expected_audit_ready = (
        subjects["run"]["audit_ready"]
        and subjects["content_store"]["audit_ready"]
        and not debug_mode
    )
    if type(audit_ready) is not bool or audit_ready != expected_audit_ready:
        raise IntegrityGateError("report audit readiness is malformed")
    return {
        **subjects,
        "debug_mode": debug_mode,
        "debug_labels": debug_labels,
        "audit_ready": audit_ready,
    }


def trusted_report_integrity(
    gate: RunIntegrityGate,
    run_dir: str,
    *,
    debug_override: bool = False,
    override_reason: str | None = None,
) -> dict:
    """Project current authority state before report bytes are rendered."""
    run_checked = gate.preflight(
        run_dir,
        debug_override=debug_override,
        override_reason=override_reason,
    )
    content_checked = gate.preflight_content_store(
        run_dir,
        debug_override=debug_override,
        override_reason=override_reason,
    )
    run_projection = _authority_subject_projection("run", run_checked)
    content_projection = _authority_subject_projection(
        "content store", content_checked
    )
    run_authority_debug = run_checked.get("debug_mode", False)
    content_authority_debug = content_checked.get("debug_mode", False)
    if (
        type(run_authority_debug) is not bool
        or type(content_authority_debug) is not bool
    ):
        raise IntegrityGateError("integrity authority debug state is malformed")
    debug_mode, debug_labels = _run_debug_settings(run_dir)
    debug_mode = (
        debug_mode
        or run_authority_debug
        or content_authority_debug
        or run_checked.get("status") == "debug_overridden"
        or content_checked.get("status") == "debug_overridden"
        or run_projection["state"] == "debug_overridden"
        or content_projection["state"] == "debug_overridden"
    )
    return _validate_report_integrity({
        "run": run_projection,
        "content_store": content_projection,
        "debug_mode": debug_mode,
        "debug_labels": debug_labels,
        "audit_ready": (
            run_projection["audit_ready"]
            and content_projection["audit_ready"]
            and not debug_mode
        ),
    })


def _local_report_integrity(run_dir: str) -> dict:
    """Fail-closed projection for pure rendering without a live authority."""
    repository = _repo_open(run_dir)
    if repository is None:
        raise IntegrityGateError("report integrity repository is unavailable")
    try:
        run_summary = repository.artifact_integrity_summary()
        assurance = repository.get_execution_assurance()
        debug_mode = repository.get_run_setting("debug_mode", False)
        debug_labels = repository.get_run_setting("debug_labels", [])
    finally:
        repository.close()
    if not isinstance(run_summary, dict):
        raise IntegrityGateError("run-local integrity summary is malformed")
    state = run_summary.get("state")
    if state not in _REPORT_INTEGRITY_STATES:
        raise IntegrityGateError("run-local integrity state is malformed")
    debug_mode, debug_labels = _validate_debug_settings(debug_mode, debug_labels)
    audit_ready = run_summary.get("audit_ready")
    if type(audit_ready) is not bool or (state != "clean" and audit_ready):
        raise IntegrityGateError("run-local audit readiness is malformed")
    overrides = run_summary.get("overrides", [])
    if not isinstance(overrides, list) or any(
        not isinstance(row, dict) for row in overrides
    ):
        raise IntegrityGateError("run-local integrity overrides are malformed")
    recoveries = run_summary.get("crash_recoveries", [])
    if not isinstance(recoveries, list) or any(
        not isinstance(row, dict) for row in recoveries
    ):
        raise IntegrityGateError(
            "run-local integrity crash recoveries are malformed"
        )
    debug_mode = debug_mode or state == "debug_overridden"
    if assurance.protection != "agent_attested":
        if state == "clean":
            state = "unverifiable"
        audit_ready = False
    return _validate_report_integrity({
        "run": {
            "state": state,
            "audit_ready": audit_ready,
            "overrides": [
                {field: row.get(field) for field in _OVERRIDE_REPORT_FIELDS}
                for row in overrides
            ],
            "crash_recoveries": [
                {field: row.get(field) for field in _RECOVERY_REPORT_FIELDS}
                for row in recoveries
            ],
        },
        "content_store": {
            "state": "unverifiable",
            "audit_ready": False,
            "overrides": [],
            "crash_recoveries": [],
        },
        "debug_mode": debug_mode,
        "debug_labels": list(debug_labels),
        "audit_ready": False,
    })


def _execution_assurance_projection(run_dir: str) -> dict:
    repository = _repo_open(run_dir)
    if repository is None:
        raise IntegrityGateError("execution assurance repository is unavailable")
    try:
        assurance = repository.get_execution_assurance()
    finally:
        repository.close()
    return {
        "initial_origin": assurance.initial_origin,
        "protection": assurance.protection,
        "agent_identity": assurance.agent_identity,
        "failure_reason": assurance.failure_reason,
        "acknowledged_at": assurance.acknowledged_at,
    }


def _safe_report_text(value) -> str:
    return (
        html.escape(str(value), quote=True)
        .replace("`", "&#96;")
        .replace("|", "&#124;")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


_SUPERSCRIPT_SENTINEL_RE = re.compile(r"⟦SUP:([\d,\-–—]+)⟧")
_EVIDENCE_MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_\[\]{}()#+\-.!|])")
_EVIDENCE_EXCERPT_LIMIT = 160


def _display_evidence_excerpt(value) -> str:
    """Render persisted evidence safely without changing the stored text."""
    text = _SUPERSCRIPT_SENTINEL_RE.sub(r"[\1]", str(value))
    text = " ".join(text.split())
    if len(text) > _EVIDENCE_EXCERPT_LIMIT:
        truncated = text[: _EVIDENCE_EXCERPT_LIMIT + 1].rsplit(None, 1)[0]
        text = (truncated or text[:_EVIDENCE_EXCERPT_LIMIT]).rstrip() + "…"
    return _safe_report_text(_EVIDENCE_MARKDOWN_ESCAPE_RE.sub(r"\\\1", text))


def _report_signature_line() -> str:
    """Render the signature plan active for this report generation."""
    if _signing.key_present():
        return "- Report signature: **HMAC-signed**"
    return ("- Report signature: **content seal only — NOT signed by the trusted signer** "
            "(integrity: sha256 match only)")


def _unstable_terminal_line(terminal: dict | None) -> str:
    """Render an exhausted pair without erasing its actionable terminal cause."""
    terminal = terminal or {}
    cause = terminal.get("terminal_cause")
    if terminal.get("terminal_status") == "exhausted" or cause in {
        "jury_exhausted", "deadline_exceeded", "cancelled", "infrastructure_error",
    }:
        return (f"    - support: ⚪ _exhausted verification (terminal cause: `{cause}`) — "
                "no terminal verdict passed the required checks_")
    if cause == "soft_outcome_disagreement":
        counts = terminal.get("soft_outcome_counts") or {}
        detail = ", ".join(
            f"{outcome}×{count}" for outcome, count in sorted(counts.items()))
        return ("    - support: ⚠️ _soft jury disagreement"
                + (f": {detail}" if detail else "")
                + "; manual adjudication required_")
    if cause == "cross_judge_outcome_conflict":
        soft_counts = terminal.get("soft_outcome_counts") or {}
        confirmed = terminal.get("judge2_confirmed_outcome_counts") or {}
        soft_detail = ", ".join(
            f"{outcome}×{count}" for outcome, count in sorted(soft_counts.items()))
        confirmed_detail = ", ".join(
            f"{outcome}×{count}" for outcome, count in sorted(confirmed.items()))
        return ("    - support: ⚠️ _cross-judge outcome conflict"
                + (f": soft={soft_detail}" if soft_detail else "")
                + (f"; Judge2-confirmed={confirmed_detail}" if confirmed_detail else "")
                + "; manual adjudication required_")
    if cause == "verdict_on_unevaluable_focus":
        proposed = terminal.get("proposed_outcomes") or []
        detail = ", ".join(str(outcome) for outcome in proposed if outcome)
        return ("    - support: ⚠️ _evidence found but focus malformed/non-propositional"
                + (f"; proposed outcome `{detail}` not certified" if detail
                   else "; proposed outcome not certified")
                + "_")
    if cause == "identity_corroborated_off_topic":
        return ("    - support: ⚠️ _off-topic verdict withheld: bibliographic "
                "identity is corroborated, but semantic relevance remains uncertain_")
    if cause == "citation_attribution_contested":
        return ("    - support: ⚠️ _citation attribution contested: manuscript span "
                "selection was ambiguous or structurally invalid — manual adjudication required_")
    if cause == "citation_no_proposition":
        return ("    - support: ⚠️ _no manuscript proposition available; verification withheld_")
    if cause == "negative_on_malformed_focus_not_evaluable":
        return ("    - support: ⚠️ _negative verdict withheld: claim text malformed; "
                "repair extraction or adjudicate_")
    if cause == "negative_on_unevaluable_focus":
        return ("    - support: ⚠️ _negative verdict withheld: citation focus "
                "does not express a complete proposition; refine the focus or adjudicate_")
    if cause == "contested_negative":
        return ("    - support: ⚠️ _negative verdict did not survive the "
                "independent adversarial compatibility check — manual "
                "adjudication required_")
    if cause == "evidence_audit_contested":
        return ("    - support: ⚠️ _source evidence judgment and independent "
                "audit disagree; the pipeline did not repeat the same semantic "
                "question — manual adjudication required_")
    if cause == "source_degraded_pending_ocr":
        return ("    - support: ⚠️ _verification inconclusive: primary PDF is "
                "unreadable and OCR is pending/unavailable; the fallback text "
                "could not establish a guard-verified verdict_")
    if cause:
        return (f"    - support: ⚠️ _uncertain verification (terminal cause: `{cause}`) — "
                "manual adjudication required_")
    return ("    - support: ⚪ _unstable: retries exhausted, "
            "no verdict passed the guard_")


def _http_trace_summary(run_dir: str) -> dict:
    """Summarize current relational raw-network attempt telemetry."""
    counts = Counter()
    statuses = Counter()
    latencies = []
    raw_by_stage = Counter()
    repository = _repo_open(run_dir)
    if repository is None:
        return {"available": False}
    try:
        enabled = bool(repository.get_run_setting("debug_mode", False))
        attempts = repository.list_http_attempts()
    except Exception:
        return {"available": False}
    finally:
        repository.close()

    for attempt in attempts:
        counts["raw_attempts"] += 1
        raw_by_stage[attempt.get("jury_stage") or "unknown"] += 1
        latencies.append(float(attempt["duration_ms"]))
        outcome = attempt["outcome"]
        if outcome == "response":
            counts["responses"] += 1
            statuses[str(attempt["status"])] += 1
            hit = attempt.get("prompt_cache_hit_tokens")
            miss = attempt.get("prompt_cache_miss_tokens")
            if hit is not None and miss is not None:
                counts["prompt_cache_observed_responses"] += 1
                counts["prompt_cache_hit_tokens"] += hit
                counts["prompt_cache_miss_tokens"] += miss
        elif outcome == "network_error":
            counts["transport_errors"] += 1
        elif outcome == "http_error":
            status = attempt["status"]
            statuses[str(status)] += 1
            if status in (408, 504):
                counts["timeouts"] += 1
            elif attempt["retryable"]:
                counts["retryable_errors"] += 1
            else:
                counts["transport_errors"] += 1
        else:
            counts["transport_errors"] += 1

    raw_available = bool(attempts)
    requests = counts["raw_attempts"]
    responses = counts["responses"]
    terminal_failures = (counts["timeouts"] + counts["cancelled"]
                         + counts["transport_errors"] + counts["retryable_errors"])
    no_response = max(0, requests - responses - terminal_failures)
    accounting_delta = requests - responses - terminal_failures - no_response
    cache_total = (
        counts["prompt_cache_hit_tokens"] + counts["prompt_cache_miss_tokens"]
    )

    def _percentile(values, percentile):
        if not values:
            return None
        ordered = sorted(values)
        idx = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
        return round(ordered[idx], 1)

    return {
        "available": enabled or raw_available,
        "raw_available": raw_available,
        **counts,
        "raw_by_stage": dict(raw_by_stage),
        "no_response": no_response,
        "accounting_delta": accounting_delta,
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "statuses": dict(statuses),
        "prompt_cache_hit_rate": (
            counts["prompt_cache_hit_tokens"] / cache_total
            if cache_total else None
        ),
    }


def _verify_runtime_display_labels(verify_runtime: dict, llm_metrics: dict) -> tuple[str, str]:
    """Prefer immutable dispatch observations over the mutable runtime label."""
    observed_rows = [
        row
        for row in (llm_metrics.get("by_provider_model_role") or [])
        if isinstance(row, dict) and row.get("attempts", 0) > 0
    ]
    backends = sorted(
        {row["provider"] for row in observed_rows if isinstance(row.get("provider"), str)}
    )
    models = sorted(
        {row["model"] for row in observed_rows if isinstance(row.get("model"), str)}
    )
    return (
        ", ".join(backends) or verify_runtime.get("backend") or "auto",
        ", ".join(models) or verify_runtime.get("model") or "provider default",
    )


_EXTERNAL_IDENTITY_LABELS = {
    "exact_acl_id_confirmed": "externally corroborated via exact ACL source",
}


def _external_identity_label(entries: list[dict]) -> str | None:
    """Return a deterministic report label for an authenticated external source."""
    labels = sorted(
        {
            _EXTERNAL_IDENTITY_LABELS[status]
            for entry in entries
            if isinstance(entry, dict)
            and isinstance((status := entry.get("identity_status")), str)
            and status in _EXTERNAL_IDENTITY_LABELS
        }
    )
    return "; ".join(labels) or None


def _existence_check_reason(
    *, status: str | None, reason: str | None, external_label: str | None
) -> str | None:
    if status == "unverified" and external_label:
        return external_label
    return reason


def _claim_issue_with_external_identity(
    issue: str,
    cites: list[dict],
    resolve_map: dict,
    external_labels_by_ref: dict[str, str],
) -> str:
    """Replace only a stale unresolved-identity note backed by exact source identity."""
    unresolved = {
        cite.get("ref_id")
        for cite in cites
        if cite.get("ref_id")
        and resolve_map.get(cite["ref_id"], {}).get("status")
        in (None, "unresolved", "unverified", "skipped")
    }
    if not unresolved or not unresolved.issubset(external_labels_by_ref):
        return issue
    label = "; ".join(sorted({external_labels_by_ref[ref_id] for ref_id in unresolved}))
    return issue.replace("bibliographic identity not automatically confirmed", label)


def render(
    run_dir,
    protocol_threshold=0.30,
    min_attempts=5,
    *,
    integrity=None,
):
    integrity = _validate_report_integrity(
        integrity if integrity is not None else _local_report_integrity(run_dir)
    )
    execution_assurance = _execution_assurance_projection(run_dir)
    if execution_assurance["protection"] != "agent_attested":
        integrity["audit_ready"] = False
        for subject in ("run", "content_store"):
            if integrity[subject]["state"] == "clean":
                integrity[subject]["state"] = "unverifiable"
            integrity[subject]["audit_ready"] = False
    projection = load_run_projection(run_dir)
    parse = projection["parse_effective"]
    if parse is None:
        raise SystemExit(f"no parse payload found in {run_dir}")
    http_trace = _http_trace_summary(run_dir)
    verify_runtime = projection.get("verify_runtime") or {}
    style_data = _load_style_projection(run_dir, parse)
    manual_parse_adjudication = projection.get("manual_parse_adjudication") or []

    resolve_map = projection["resolve_map"]
    fetch_attempts_by_ref = projection.get("fetch_attempts_by_ref", {})
    style_map = {c["ref_id"]: c for c in style_data.get("checks", [])}
    manifest = projection["manifest"]
    refs_by_id = {ref["id"]: ref for ref in parse["references"]}
    invalidated_abstract_refs = {
        ref_id
        for ref_id, ref in refs_by_id.items()
        if _sources.weak_metadata_abstract_invalidated(
            ref,
            resolve_map.get(ref_id, {}),
            fetch_attempts_by_ref.get(ref_id, []),
        )
    }
    if invalidated_abstract_refs:
        resolve_map = dict(resolve_map)
        for ref_id in invalidated_abstract_refs:
            resolve_map[ref_id] = {
                key: value
                for key, value in resolve_map.get(ref_id, {}).items()
                if key != "abstract"
            }
        manifest = dict(manifest)
        manifest["entries"] = [
            entry for entry in manifest.get("entries", [])
            if not (
                entry.get("ref_id") in invalidated_abstract_refs
                and entry.get("tier") == "abstract"
                and entry.get("source_ref") == (
                    resolve_map.get(entry.get("ref_id"), {}).get("url")
                    or "resolve:" + str(
                        resolve_map.get(entry.get("ref_id"), {}).get("abstract_via")
                        or resolve_map.get(entry.get("ref_id"), {}).get("via")
                        or ""
                    )
                )
            )
        ]
    usable_entries = []
    for entry in manifest.get("entries", []):
        if not isinstance(entry, dict):
            continue
        entry_refs, _ = usable_source_refs(run_dir, {"entries": [entry]})
        if entry.get("ref_id") in entry_refs:
            usable_entries.append(entry)
    usable_manifest = {**manifest, "entries": usable_entries}
    usable_text_refs, _unavailable_sources = usable_verification_source_refs(run_dir, manifest)
    selected_by_ref = {}
    for entry in usable_manifest["entries"]:
        if not is_verifiable_source_entry(entry):
            continue
        rid = entry.get("ref_id")
        if rid and rid not in selected_by_ref:
            selected_by_ref[rid] = _sources.best_for(usable_manifest, rid)
    selected_fulltext_refs = frozenset(
        rid for rid, entry in selected_by_ref.items()
        if entry and entry.get("tier") == "fulltext"
    )
    selected_suspect_refs = frozenset(
        rid for rid, entry in selected_by_ref.items()
        if entry and entry.get("tier") == "fulltext"
        and entry.get("extraction_flags")
    )
    # Unreadable-PDF OCR queue: a parked primary scan may be covered by an
    # alternative full text and therefore must not be labelled "not checked".
    unreadable_q = [e for e in projection["unreadable"].get("entries", [])
                    if e.get("ocr_status") != "done"]
    raw_unreadable_ref_ids = frozenset(e["ref_id"] for e in unreadable_q if e.get("ref_id"))
    unreadable_ref_ids = frozenset(
        rid for rid in raw_unreadable_ref_ids if rid not in selected_fulltext_refs
    )
    prov_by_ref = defaultdict(list)
    extraction_flags_by_ref = defaultdict(set)
    for e in usable_manifest["entries"]:
        prov_by_ref[e["ref_id"]].append(e)
    external_labels_by_ref = {}
    for ref_id, entries in prov_by_ref.items():
        label = _external_identity_label(entries)
        if label:
            external_labels_by_ref[ref_id] = label
    for rid in selected_suspect_refs:
        extraction_flags_by_ref[rid].update(
            selected_by_ref[rid].get("extraction_flags") or []
        )
    extraction_suspect_refs = frozenset(
        ref_id for ref_id, flags in extraction_flags_by_ref.items() if flags
    )
    extraction_flag_counts = Counter(
        flag for flags in extraction_flags_by_ref.values() for flag in flags
    )

    claims = parse["claims"]
    refs = {r["id"]: r for r in parse["references"]}
    refnum = {r["ref_number"]: r for r in parse["references"]}
    external_text_refs = sorted({
        refs[rid]["ref_number"] for rid in refs
        if resolve_map.get(rid, {}).get("status") != "resolved"
        and any(e.get("identity_status") for e in prov_by_ref.get(rid, []))
    })
    external_text_ref_labels = {
        refs[ref_id]["ref_number"]: label
        for ref_id, label in external_labels_by_ref.items()
        if ref_id in refs
        and resolve_map.get(ref_id, {}).get("status") != "resolved"
    }
    cites_by_claim = defaultdict(list)
    for c in parse["citations"]:
        cites_by_claim[c["claim_id"]].append(c)
    all_pairs = {(c["id"], ci["ref_id"]) for c in claims
                 for ci in cites_by_claim.get(c["id"], []) if ci.get("ref_id")}
    pair_states = projection.get("verification_pair_states") or []
    verification_rows = select_verification_pair_rows(
        projection.get("verification_projection") or []
    )
    verification_by_pair = {
        (row["claim_id"], row["ref_id"]): row for row in verification_rows
    }
    llm_metrics = projection.get("llm_metrics") or {"by_provider_model_role": [], "totals": {}}
    operational_terminal_pairs = {
        (row["claim_id"], row["ref_id"])
        for row in verification_rows if row["operational_terminal"]
    } & all_pairs
    attempted_pairs = set(operational_terminal_pairs)
    terminal_causes_by_pair = latest_terminal_causes(pair_states)
    pair_terminal = terminal_pair_counts(
        all_pairs,
        pair_states,
        terminal_causes_by_pair,
        usable_text_refs=usable_text_refs,
        unreadable_ref_ids=unreadable_ref_ids,
    )
    terminal_causes_by_pair = dict(terminal_causes_by_pair)
    for pair in pair_terminal["uncertain"]:
        terminal_causes_by_pair.setdefault(pair, "unknown")
    for pair in pair_terminal["exhausted"]:
        terminal_causes_by_pair.setdefault(pair, "jury_exhausted")
    accepted_pairs = pair_terminal["accepted"]
    semantic_by_pair = {
        (row["claim_id"], row["ref_id"]): {
            "outcome": row["semantic_outcome"],
            "scope": row.get("scope") or "claim_evidence",
            "reliability": verification_reliability(row),
            "assurance": row.get("assurance"),
            "crediting": bool(row.get("crediting")),
            "passage_verified": (
                not row.get("evidence_bearing") or bool(row.get("evidence"))
            ),
            "evidence": row.get("evidence") or [],
        }
        for row in verification_rows if row.get("semantic_outcome") is not None
    }
    positive_by_pair = {
        pair: row for pair, row in semantic_by_pair.items()
        if row["outcome"] == "supports" and row["crediting"]
    }
    isolated_uncertain = isolated_uncertain_pairs(pair_states)
    accepted_pair_count = len(accepted_pairs & all_pairs)
    verification_complete = all_pairs <= operational_terminal_pairs
    pairs_incomplete = len(all_pairs - operational_terminal_pairs)
    result_class_by_pair = {
        pair: row["result_class"]
        for pair, row in verification_by_pair.items()
        if pair in all_pairs
    }
    result_class_counts = Counter(
        result_class_by_pair[pair] if pair in result_class_by_pair else "unresolved"
        for pair in all_pairs
    )
    crediting_positive_count = sum(
        1 for pair, row in verification_by_pair.items()
        if pair in all_pairs and row.get("crediting")
    )
    result_class_total = len(all_pairs)
    result_class_rates = {
        name: round(result_class_counts[name] / result_class_total, 3)
        if result_class_total else 0.0
        for name in ("positive", "incomplete", "negative", "unresolved")
    }

    # ---- Run health: diagnostics on the MODEL, not on the manuscript ----
    raw_verification = projection.get("verification_raw") or {}
    attempt_causes_by_pair = terminal_attempt_causes_by_pair(raw_verification)
    metric_totals = llm_metrics.get("totals") or {}
    dispatch_attempts = int(metric_totals.get("attempts", 0))
    admissible_candidates = len(raw_verification.get("candidates") or ())
    terminal_causes = Counter(
        terminal_causes_by_pair[pair]
        for pair in pair_terminal["uncertain"] | pair_terminal["exhausted"]
        if terminal_causes_by_pair.get(pair)
    )
    n_uncertain_pairs = len(pair_terminal["uncertain"])
    n_exhausted_pairs = len(pair_terminal["exhausted"])
    n_no_usable_text_pairs = len(pair_terminal["no_usable_text"])
    n_unaccounted_pairs = len(pair_terminal["unaccounted"])
    terminal_health = terminal_health_dimensions(
        pair_terminal,
        terminal_causes_by_pair,
        operational_terminal_pairs,
        attempt_causes_by_pair,
    )
    eligible_pair_count = len(terminal_health["eligible_pairs"])
    mechanical_pair_count = len(terminal_health["mechanical_pairs"])
    semantic_pair_count = len(terminal_health["semantic_pairs"])
    infrastructure_pair_count = len(terminal_health["infrastructure_pairs"])
    deterministic_guard_pair_count = len(terminal_health["deterministic_guard_pairs"])
    unclassified_pair_count = len(terminal_health["unclassified_pairs"])
    protocol_rate = (
        mechanical_pair_count / eligible_pair_count
        if eligible_pair_count else 0.0
    )
    semantic_rate = (
        semantic_pair_count / eligible_pair_count
        if eligible_pair_count else 0.0
    )
    weak_model_warning = (
        eligible_pair_count >= min_attempts
        and protocol_rate > protocol_threshold
    )
    health = {
        "logical_requests": int(metric_totals.get("logical_requests", 0)),
        "dispatch_attempts": dispatch_attempts,
        "admissible_candidates": admissible_candidates,
        "terminal_pairs": len(operational_terminal_pairs),
        "positive_pairs": result_class_counts["positive"],
        "semantic_positive_pairs": result_class_counts["positive"],
        "crediting_positive_pairs": crediting_positive_count,
        "semantic_incomplete_pairs": result_class_counts["incomplete"],
        "semantic_negative_pairs": result_class_counts["negative"],
        "semantic_unresolved_pairs": result_class_counts["unresolved"],
        "semantic_positive_rate": result_class_rates["positive"],
        "semantic_incomplete_rate": result_class_rates["incomplete"],
        "semantic_negative_rate": result_class_rates["negative"],
        "semantic_unresolved_rate": result_class_rates["unresolved"],
        "protocol_failure_rate": round(protocol_rate, 3),
        "mechanical_protocol_failure_rate": round(protocol_rate, 3),
        "mechanical_protocol_failure_pairs": mechanical_pair_count,
        "mechanical_protocol_eligible_pairs": eligible_pair_count,
        "mechanical_protocol_gate_passed": protocol_rate <= protocol_threshold,
        "semantic_uncertainty_rate": round(semantic_rate, 3),
        "semantic_uncertainty_pairs": semantic_pair_count,
        "infrastructure_failure_pairs": infrastructure_pair_count,
        "deterministic_guard_pairs": deterministic_guard_pair_count,
        "unclassified_terminal_pairs": unclassified_pair_count,
        "terminal_causes": dict(terminal_causes),
        "uncertain_pairs": n_uncertain_pairs,
        "uncertain_by_cause": pair_terminal["uncertain_by_cause"],
        "contested_negative_pairs": len(pair_terminal["contested_negative"]),
        "exhausted_pairs": n_exhausted_pairs,
        "no_usable_text_pairs": n_no_usable_text_pairs,
        "unaccounted_pairs": n_unaccounted_pairs,
        "weak_model_warning": weak_model_warning,
        "threshold": protocol_threshold,
    }

    ms = parse["manuscript"]
    lines = []
    if integrity["debug_mode"]:
        lines.extend(DEBUG_REPORT_BANNER.splitlines())
        lines.append("")
    if execution_assurance["protection"] == "agent_unprotected_acknowledged":
        lines.extend(UNPROTECTED_AGENT_REPORT_BANNER.splitlines())
        lines.append("")
    lines.append(f"# Citation Verification Report")
    lines.append(f"run · `{ms['filename']}` · sha `{ms['sha256'][:12]}` · "
                 f"parser `{ms['parser_version']}`\n")
    lines.append(
        "- Audit readiness: "
        f"**{'yes' if integrity['audit_ready'] else 'no'}** · "
        f"run integrity **{integrity['run']['state']}** · "
        "content-store integrity "
        f"**{integrity['content_store']['state']}**"
    )
    assurance_line = (
        "- Execution assurance: "
        f"**{execution_assurance['protection']}** · "
        f"initial origin **{execution_assurance['initial_origin']}**"
    )
    if execution_assurance["agent_identity"] is not None:
        assurance_line += (
            " · agent "
            f"**{_safe_report_text(execution_assurance['agent_identity'])}**"
        )
    lines.append(assurance_line)
    if manual_parse_adjudication:
        lines.append("\n## Manual Parse adjudication\n")
        lines.append("These are Parse decisions, distinct from Resolve/Fetch and HTTP failures.")
        lines.append("| Subject | Status / action | Audit evidence |")
        lines.append("|---|---|---|")
        for row in manual_parse_adjudication:
            subject = (f"note {row['note_number']}" if row["subject_type"] == "footnote_note"
                       else f"reference [{row['ref_number']}]")
            action = row.get("action") or ("manual review required" if row.get("manual_review_required") else "not applied")
            details = []
            if row.get("source_count") is not None:
                details.append(f"sources={row['source_count']}")
            if row["subject_type"] == "reference_identity":
                original_title = row.get("original_title") or "—"
                effective_title = row.get("effective_title") or "—"
                original_doi = row.get("original_doi") or "—"
                effective_doi = row.get("effective_doi") or "—"
                details.append("identity: " + _safe_report_text(original_title)
                               + " → " + _safe_report_text(effective_title))
                details.append("DOI: " + _safe_report_text(original_doi)
                               + " → " + _safe_report_text(effective_doi))
            if row.get("reason"):
                details.append("reason: " + _safe_report_text(row["reason"]))
            if row.get("actor_type"):
                provenance = row.get("producer_identity") or row["actor_type"]
                details.append(f"{_safe_report_text(provenance)} @ {row.get('applied_at')}")
            lines.append(f"| {_safe_report_text(subject)} | {_safe_report_text(row['status'])} / {_safe_report_text(action)} | {'; '.join(details) or '—'} |")
        lines.append("")
    if execution_assurance["protection"] == "agent_unprotected_acknowledged":
        lines.append(
            "- Human acknowledgement: "
            f"`{_safe_report_text(execution_assurance['acknowledged_at'])}` · "
            "integrity failure: "
            f"{_safe_report_text(execution_assurance['failure_reason'])}"
        )
    if integrity["debug_labels"]:
        labels = ", ".join(
            _safe_report_text(label) for label in integrity["debug_labels"]
        )
        lines.append(f"- Debug labels: {labels}")
    for subject in ("run", "content_store"):
        for recovery in integrity[subject]["crash_recoveries"]:
            lines.append(
                f"- Crash recovery ({subject.replace('_', ' ')}): "
                f"`{_safe_report_text(recovery.get('recovery_id'))}` restored "
                f"checkpoint `{_safe_report_text(recovery.get('expected_checkpoint_id'))}` "
                f"after {int(recovery.get('difference_count') or 0)} observed "
                f"difference(s); {int(recovery.get('action_count') or 0)} restore "
                f"action(s) · audit readiness preserved"
            )
        for override in integrity[subject]["overrides"]:
            lines.append(
                f"- {subject.replace('_', ' ')} integrity override "
                f"`{_safe_report_text(override.get('override_id'))}`: "
                f"{_safe_report_text(override.get('reason'))} · caller "
                f"`{_safe_report_text(override.get('authenticated_caller'))}` · "
                f"{_safe_report_text(override.get('created_at'))}"
            )
    lines.append("")

    # ---- Run health (diagnostics on the model, NOT on the manuscript) ----
    lines.append("## 0. Run Health\n")
    lines.append("_Diagnostics on the **model/run**, not a judgment on the manuscript. "
                 "A guard-fail always concerns the model output. An operationally "
                 "accepted negative outcome (`contradicts`/`off_topic`) is a "
                 "**model finding requiring review**, not a proven manuscript problem._\n")
    lines.append(
        f"- Typed verification terminals: **{len(operational_terminal_pairs)}** · "
        f"nominal positive results: **{result_class_counts['positive']}** · "
        f"strict crediting positives: **{crediting_positive_count}**"
    )
    lines.append(
        "- Semantic pair classes (all cited claim/source pairs): "
        f"positive **{result_class_counts['positive']} ({result_class_rates['positive']:.0%})** · "
        f"incomplete **{result_class_counts['incomplete']} ({result_class_rates['incomplete']:.0%})** · "
        f"negative **{result_class_counts['negative']} ({result_class_rates['negative']:.0%})** · "
        f"unresolved **{result_class_counts['unresolved']} ({result_class_rates['unresolved']:.0%})**"
    )
    lines.append(
        f"- LLM logical requests: **{health['logical_requests']}** · "
        f"dispatch attempts: **{dispatch_attempts}** · "
        f"admissible candidates: **{admissible_candidates}**"
    )
    completeness = (
        f"**complete** — **{len(operational_terminal_pairs)}/{len(all_pairs)}** pairs terminal"
        if verification_complete else
        f"**incomplete** — **{len(operational_terminal_pairs)}/{len(all_pairs)}** pairs terminal; "
        f"**{pairs_incomplete}** incomplete"
    )
    lines.append(f"- Verification completeness: {completeness}")
    lines.append(f"- Pairs ended `exhausted`: **{n_exhausted_pairs}**")
    lines.append(f"- Pairs ended `uncertain`: **{n_uncertain_pairs}**")
    if verification_rows:
        lines.append("- Verification projection: semantic outcome, assurance, resolution, and operational completion are reported separately.")
        for row in verification_rows:
            lines.append(f"  - `{row['claim_id']}·{row['ref_id']}`: outcome=`{row['semantic_outcome']}` · result_class=`{row['result_class']}` · assurance=`{row['assurance']}` · resolution=`{row['resolution']}` · lifecycle=`operationally_complete={str(row['operational_complete']).lower()}` · crediting=`{row['crediting']}`")
    if llm_metrics["by_provider_model_role"]:
        lines.append("- LLM diagnostics (provider/model/credential alias/role):")
        for row in llm_metrics["by_provider_model_role"]:
            lines.append(
                f"  - `{row['provider']}/{row['model']}/{row['credential_alias']}/{row['role']}`: "
                f"attempts={row['attempts']}, answers={row['answers_received']}, "
                f"transport-decodable={row['protocol_valid']}, "
                f"application-rejected={row.get('application_rejections', 0)} "
                f"(schema={row.get('application_schema_rejections', 0)})"
            )
    if pair_terminal["uncertain_by_cause"]:
        lines.append("- Uncertain by terminal cause: **" + ", ".join(
            f"{cause}={count}" for cause, count in pair_terminal["uncertain_by_cause"].items()
        ) + "**")
    lines.append(f"- Pairs with contested negative: **{len(pair_terminal['contested_negative'])}**")
    lines.append(f"- Pairs with no usable source text: **{n_no_usable_text_pairs}**")
    if n_unaccounted_pairs:
        lines.append(f"- Pairs without a terminal lifecycle classification: **{n_unaccounted_pairs}**")
    if terminal_causes:
        lines.append("- Terminal causes: **" + ", ".join(
            f"{cause}={count}" for cause, count in sorted(terminal_causes.items())
        ) + "**")
    lines.append(
        "- Mechanical protocol terminals: "
        f"**{mechanical_pair_count}/{eligible_pair_count}**"
    )
    lines.append(
        "- Semantic uncertainty terminals: "
        f"**{semantic_pair_count}/{eligible_pair_count}**"
    )
    if deterministic_guard_pair_count:
        lines.append(
            "- Deterministic guard terminals: "
            f"**{deterministic_guard_pair_count}/{eligible_pair_count}**"
        )
    if infrastructure_pair_count:
        lines.append(
            "- Infrastructure terminal failures: "
            f"**{infrastructure_pair_count}/{eligible_pair_count}**"
        )
    if unclassified_pair_count:
        lines.append(
            "- Unclassified non-accepted terminals: "
            f"**{unclassified_pair_count}/{eligible_pair_count}**"
        )
    signature_line = _report_signature_line()
    if signature_line:
        lines.append(signature_line)
    if extraction_suspect_refs:
        flag_summary = ", ".join(
            f"{flag}={count}" for flag, count in sorted(extraction_flag_counts.items())
        )
        lines.append("- ⚠️ Sources with suspect extraction (selected verification variant only): "
                     f"**{len(extraction_suspect_refs)}** (flags: {flag_summary}) "
                     "— per-source details may list alternative variants")
    manuscript_extract = (parse.get("_debug", {}).get("extract") or {})
    manuscript_flags = manuscript_extract.get("pdf_structure_flags") or []
    if manuscript_flags:
        lines.append("- ⚠️ Manuscript extraction: suspect (" + ", ".join(manuscript_flags)
                     + ") — claim texts may contain layout artifacts")
    if verify_runtime:
        revision = verify_runtime.get("code_revision") or "unknown"
        backend, model = _verify_runtime_display_labels(verify_runtime, llm_metrics)
        lines.append("- Verify runtime: backend **{backend}** · model **{model}** · reasoning "
                     "**{reasoning}** · full text required **{fulltext}** · revision `{revision}`".format(
                         backend=backend,
                         model=model,
                         reasoning=verify_runtime.get("reasoning") or "auto",
                         fulltext="yes" if verify_runtime.get("require_fulltext") else "no",
                         revision=revision,
                     ))
    metric_totals = llm_metrics.get("totals") or {}
    immutable_logical_requests = metric_totals.get("logical_requests", 0)
    if immutable_logical_requests:
        lines.append(f"- LLM logical requests: **{immutable_logical_requests}** (immutable ledger)")
        lines.append(f"- LLM dispatch attempts: **{metric_totals.get('attempts', 0)}** (immutable ledger)")
        lines.append(
            "- Jury1 application rejections: "
            f"**{metric_totals.get('application_rejections', 0)}** · "
            f"schema-invalid **{metric_totals.get('application_schema_rejections', 0)}**"
        )
        if metric_totals.get("jury2_evaluated", 0):
            lines.append("- Jury2 decisions: evaluated **{evaluated}** · yes **{yes}** · no **{no}**".format(
                evaluated=metric_totals["jury2_evaluated"],
                yes=metric_totals.get("jury2_yes", 0),
                no=metric_totals.get("jury2_no", 0),
            ))
    if http_trace.get("available"):
        if http_trace.get("raw_available"):
            lines.append("- Raw HTTP attempts: **{attempts}** · responses **{responses}** · "
                         "timeouts **{timeouts}** · retryable errors **{retryable}** · "
                         "transport errors **{errors}** · accounting delta **{delta}**".format(
                             attempts=http_trace.get("raw_attempts", 0),
                             responses=http_trace.get("responses", 0),
                             timeouts=http_trace.get("timeouts", 0),
                             retryable=http_trace.get("retryable_errors", 0),
                             errors=http_trace.get("transport_errors", 0),
                             delta=http_trace.get("accounting_delta", 0),
                         ))
            raw_parts = ", ".join(
                f"{stage}={count}" for stage, count in sorted(http_trace.get("raw_by_stage", {}).items())
            )
            if raw_parts:
                lines.append(f"  - Raw HTTP by stage: {raw_parts}")
            if http_trace.get("prompt_cache_observed_responses"):
                rate = http_trace.get("prompt_cache_hit_rate")
                rate_text = "n/a" if rate is None else f"{rate:.1%}"
                lines.append(
                    "  - Provider prompt cache: hit **{hit}** tokens · miss "
                    "**{miss}** tokens · hit rate **{rate}** · observed "
                    "responses **{responses}**".format(
                        hit=http_trace.get("prompt_cache_hit_tokens", 0),
                        miss=http_trace.get("prompt_cache_miss_tokens", 0),
                        rate=rate_text,
                        responses=http_trace["prompt_cache_observed_responses"],
                    )
                )
        else:
            lines.append("- Raw HTTP tracing: **unavailable** (logical calls are recorded; "
                         "raw attempt count is not known)")
        if http_trace.get("latency_p50_ms") is not None:
            lines.append("  - raw-attempt latency: p50 **{p50:.0f} ms**, p95 **{p95:.0f} ms**".format(
                p50=http_trace["latency_p50_ms"], p95=http_trace["latency_p95_ms"]))
    else:
        lines.append("- Raw HTTP tracing: **unavailable** (debug tracing was not recorded)")
    lines.append(
        "- Protocol failure rate (mechanical pair-level): "
        f"**{protocol_rate:.0%}** (threshold {protocol_threshold:.0%})"
    )
    if unreadable_q:
        n_assoc = len(raw_unreadable_ref_ids)
        n_alt = len(raw_unreadable_ref_ids & selected_fulltext_refs)
        n_alt_suspect = len(raw_unreadable_ref_ids & selected_suspect_refs)
        n_unassoc = sum(1 for e in unreadable_q if not e.get("ref_id"))
        lines.append(
            f"- 📄 **Full text FOUND but UNREADABLE** (scanned / no OCR layer): "
            f"**{len(unreadable_q)}** "
            f"— alternative full text used: **{n_alt}**"
            + (f" (suspect: **{n_alt_suspect}**)" if n_alt_suspect else "")
            + f"; pending OCR: **{len(unreadable_ref_ids) + n_unassoc}**"
            + ". A parked primary scan does not mean verification was skipped when "
            "an alternative full text was selected.")
    if weak_model_warning:
        lines.append(
            "\n> ⚠️ **High mechanical protocol failure rate.** Inspect the "
            "recorded contract/materialisation causes before attributing this "
            "to the manuscript or reducing source context. Legitimate semantic "
            "uncertainty is reported separately."
        )
    lines.append("")

    # ---- Triage, problems first ----
    lines.append("## 1. Triage — problems first\n")
    triage = []
    for i, claim in enumerate(claims, 1):
        cites = cites_by_claim.get(claim["id"], [])
        status, why = claim_badge(
            claim, cites, semantic_by_pair, resolve_map, style_map, attempted_pairs,
            unreadable_ref_ids, isolated_uncertain, extraction_suspect_refs,
            terminal_causes_by_pair,
        )
        why = _claim_issue_with_external_identity(
            why, cites, resolve_map, external_labels_by_ref,
        )
        if status in ("fail", "warn", "unstable"):
            triage.append((status, i, claim, why))
    order = {"fail": 0, "unstable": 1, "warn": 2}
    triage.sort(key=lambda t: order.get(t[0], 9))
    if triage:
        lines.append("| | Claim | Issue |")
        lines.append("|---|---|---|")
        for status, i, claim, why in triage:
            txt = claim["sentence"][:90].replace("|", "\\|")
            lines.append(f"| {BADGE[status]} | C{i} | {why} — _{txt}…_ |")
    else:
        lines.append("_No issues detected by the deterministic axes._")
    lines.append("")

    # ---- Coverage ----
    n_res = sum(1 for r in resolve_map.values() if r.get("status") == "resolved")
    n_nf = sum(1 for r in resolve_map.values() if r.get("status") == "not_found")
    n_mismatch = sum(1 for r in resolve_map.values() if r.get("status") == "identifier_mismatch")
    n_unverified = sum(1 for r in resolve_map.values() if r.get("status") == "unverified")
    n_retr = sum(1 for r in resolve_map.values() if r.get("retracted"))
    # Entries "to verify online" (existence unconfirmed, NOT fabrication).
    unverified_refs = sorted(refs[rid]["ref_number"]
                             for rid, r in resolve_map.items()
                             if r.get("status") == "unverified" and rid in refs)
    unverified_actionable_refs = sorted(
        refs[rid]["ref_number"]
        for rid, r in resolve_map.items()
        if r.get("status") == "unverified"
        and rid in refs
        and rid not in external_labels_by_ref
    )
    # Books searched in both catalogs and not found (orange, not red).
    searched_not_found_refs = sorted(
        refs[rid]["ref_number"] for rid, r in resolve_map.items()
        if r.get("existence_corroboration") == "searched_not_found" and rid in refs)
    suspected_refs = sorted(
        refs[rid]["ref_number"] for rid, r in resolve_map.items()
        if r.get("reference_status_tag") == "suspected_fabricated" and rid in refs)
    high_risk_weak_metadata_refs = sorted(
        refs[rid]["ref_number"] for rid, r in resolve_map.items()
        if r.get("reference_status_tag") == "high_risk_weak_metadata" and rid in refs)
    identifier_error_refs = sorted(
        refs[rid]["ref_number"] for rid, r in resolve_map.items()
        if (
            (
                str(r.get("reference_status_tag", "")).startswith("identifier_error")
                or r.get("reference_status_tag") == "verified_with_identifier_error"
            )
            and rid in refs
        ))
    low_index_refs = sorted(
        refs[rid]["ref_number"] for rid, r in resolve_map.items()
        if r.get("reference_status_tag") == "unverified_low_indexability" and rid in refs)
    suspected_ratio = (len(suspected_refs) / len(refs)) if refs else 0.0
    if suspected_ratio >= 0.50:
        risk_band = "severe"
    elif suspected_ratio >= 0.25:
        risk_band = "high"
    elif suspected_ratio >= 0.10:
        risk_band = "moderate"
    else:
        risk_band = "low"
    n_unr = len(refs) - n_res - n_nf - n_mismatch
    n_uncertain = n_uncertain_pairs
    n_no_usable_text = n_no_usable_text_pairs
    multisource_claim_ids = effective_multisource_claim_ids(parse.get("citations", []))
    lifecycle_decided = (
        pair_terminal["accepted"]
        | pair_terminal["uncertain"]
        | pair_terminal["exhausted"]
    )
    n_missing = len(all_pairs - attempted_pairs - lifecycle_decided)
    refs_with_text = usable_text_refs
    orphan_claim_ids = {o.get("claim_id") for o in parse.get("_debug", {}).get("orphans", [])
                        if o.get("claim_id")}
    orphan_claim_ids.update(
        c.get("claim_id") for c in parse.get("citations", [])
        if c.get("claim_id") and not c.get("ref_id")
    )
    claim_coverage = {
        "claims_total": len(claims),
        "claims_with_any_source_text": 0,
        "claims_without_source_text": 0,
        "claims_with_suspected_fabricated_source": 0,
        "claims_with_high_risk_weak_metadata_source": 0,
        "claims_with_identifier_error_source": 0,
        "claims_with_weak_metadata_source": 0,
        "claims_with_low_indexability_source": 0,
        "claims_with_orphan_citation": 0,
        "claims_with_positive_verification": 0,
    }
    claim_coverage_numbers = {k + "_numbers": [] for k in (
        "claims_with_any_source_text",
        "claims_without_source_text",
        "claims_with_suspected_fabricated_source",
        "claims_with_high_risk_weak_metadata_source",
        "claims_with_identifier_error_source",
        "claims_with_weak_metadata_source",
        "claims_with_low_indexability_source",
        "claims_with_orphan_citation",
        "claims_with_positive_verification",
    )}
    for i, claim in enumerate(claims, 1):
        claim_id = claim["id"]
        cited_ref_ids = [c.get("ref_id") for c in cites_by_claim.get(claim_id, [])
                         if c.get("ref_id")]
        tags = [resolve_map.get(rid, {}).get("reference_status_tag") for rid in cited_ref_ids]
        has_identifier_error = any(
            str(tag or "").startswith("identifier_error")
            or tag == "verified_with_identifier_error"
            for tag in tags
        )
        has_text = any(rid in refs_with_text for rid in cited_ref_ids)
        has_positive = any((claim_id, rid) in positive_by_pair for rid in cited_ref_ids)
        checks = [
            ("claims_with_any_source_text", has_text),
            ("claims_without_source_text", bool(cited_ref_ids) and not has_text),
            ("claims_with_suspected_fabricated_source", "suspected_fabricated" in tags),
            ("claims_with_high_risk_weak_metadata_source", "high_risk_weak_metadata" in tags),
            ("claims_with_identifier_error_source", has_identifier_error),
            ("claims_with_weak_metadata_source", "weak_metadata_match" in tags),
            ("claims_with_low_indexability_source", "unverified_low_indexability" in tags),
            ("claims_with_orphan_citation", claim_id in orphan_claim_ids),
            ("claims_with_positive_verification", has_positive),
        ]
        for key, ok in checks:
            if ok:
                claim_coverage[key] += 1
                claim_coverage_numbers[key + "_numbers"].append(i)
    lines.append("## 2. Coverage\n")
    lines.append(f"- Claims: **{len(claims)}** (effective multi-source: "
                 f"**{len(multisource_claim_ids)}**)")
    lines.append(f"- References: **{len(refs)}** — resolved **{n_res}**, "
                 f"not found **{n_nf}**"
                 + (f", **DOI mismatch {n_mismatch}**" if n_mismatch else "")
                 + (f", unverified **{n_unverified}**" if n_unverified else "")
                 + f", unresolved/no text **{n_unr}**"
                 + (f", **RETRACTED {n_retr}**" if n_retr else ""))
    if external_text_refs:
        lines.append("- ℹ️ **Textually corroborated outside `resolve`** "
                     "(traceable supplied/fetched text used for verification, without "
                     "changing bibliographic resolve status): "
                     + ", ".join(
                         f"[{n}] ({external_text_ref_labels[n]})"
                         if n in external_text_ref_labels else f"[{n}]"
                         for n in external_text_refs
                     ))
    if unverified_actionable_refs:
        lines.append("- ❔ **Existence to confirm** (no unique identifier or not indexed; "
                     "**not** fabrication): "
                     + ", ".join(f"[{n}]" for n in unverified_actionable_refs)
                     + " — verify bibliographic identity manually")
    if searched_not_found_refs:
        lines.append("- ⚠️ **Books not corroborated** (searched OpenLibrary + Google Books, "
                     "found nothing; **not** auto-fabrication but high suspicion): "
                     + ", ".join(f"[{n}]" for n in searched_not_found_refs)
                     + " — verify bibliographic identity manually")
    if suspected_refs:
        lines.append("- ⚠️ **Reference fabrication risk pattern**: "
                     f"**{len(suspected_refs)}/{len(refs)}** references tagged "
                     f"`suspected_fabricated` by deterministic evidence checks "
                     f"(risk: **{risk_band}**): "
                     + ", ".join(f"[{n}]" for n in suspected_refs))
    if high_risk_weak_metadata_refs:
        lines.append("- High-risk weak-metadata sources "
                     "(borderline metadata candidate found, but still risky enough to verify): "
                     + ", ".join(f"[{n}]" for n in high_risk_weak_metadata_refs))
    if identifier_error_refs:
        lines.append("- Hard identifier errors "
                     "(DOI/PMID/ISBN missing, unresolved, or resolving to another work; "
                     "not automatically counted as fabricated): "
                     + ", ".join(f"[{n}]" for n in identifier_error_refs))
    if low_index_refs:
        lines.append("- · **Low-indexability sources left unverified** "
                     "(books/reports/web items without strong identifiers; not counted as suspected): "
                     + ", ".join(f"[{n}]" for n in low_index_refs))
    lines.append("")
    lines.append("### 2a. Claim Verification Coverage\n")
    lines.append("_Claim support is checked only when a cited source has text in "
                 "the recorded source manifest; otherwise no LLM verification task is created._")
    lines.append(f"- Claims with any retrievable source text: "
                 f"**{claim_coverage['claims_with_any_source_text']}**")
    lines.append(f"- Claims without source text: "
                 f"**{claim_coverage['claims_without_source_text']}**")
    lines.append(f"- Claims depending on `suspected_fabricated` references: "
                 f"**{claim_coverage['claims_with_suspected_fabricated_source']}**")
    lines.append(f"- Claims depending on `high_risk_weak_metadata` references: "
                 f"**{claim_coverage['claims_with_high_risk_weak_metadata_source']}**")
    lines.append(f"- Claims depending on hard identifier errors: "
                 f"**{claim_coverage['claims_with_identifier_error_source']}**")
    lines.append(f"- Claims depending on `weak_metadata_match` references: "
                 f"**{claim_coverage['claims_with_weak_metadata_source']}**")
    lines.append(f"- Claims depending on `unverified_low_indexability` references: "
                 f"**{claim_coverage['claims_with_low_indexability_source']}**")
    lines.append(f"- Claims with orphan citations: "
                 f"**{claim_coverage['claims_with_orphan_citation']}**")
    lines.append(f"- Claims with positive verification results: "
                 f"**{claim_coverage['claims_with_positive_verification']}**")
    # Unreadable PDFs parked for OCR (scanned, no text layer): an opt-in queue.
    # (unreadable_q / unreadable_ref_ids were computed once at the top of render.)
    if unreadable_q:
        labelled = []
        for e in unreadable_q:
            rn = e.get("ref_number")
            rid = e.get("ref_id")
            label = f"[{rn}]" if rn is not None else "(unassociated)"
            # Explain WHY OCR was not offered
            skip_reason = ""
            if rid:
                selected = selected_by_ref.get(rid)
                has_fulltext_alt = bool(selected and selected.get("tier") == "fulltext")
                has_abstract = any(
                    pe.get("ref_id") == rid and pe.get("tier") == "abstract"
                    for pe in usable_manifest["entries"]
                )
                if has_fulltext_alt:
                    ft_origin = selected.get("origin") or "?"
                    quality = (" suspect" if selected.get("extraction_flags") else " clean")
                    skip_reason = (f" (selected{quality} fulltext via {ft_origin} from another "
                                   "source — scan not needed)")
                elif has_abstract:
                    skip_reason = " (abstract available — OCR skipped in standard/standard_web; use maximum accuracy to force)"
                else:
                    skip_reason = f" (no alternative text — OCR pending, run `{run_command('ocr')}`)"
            labelled.append(label + skip_reason)
        lines.append("- 📄 **Unreadable PDFs parked** (scanned, no text layer): "
                     + "; ".join(labelled)
                     + ". Unassociated files are "
                     "associated to a reference only AFTER OCR yields text.")
    lines.append(f"- Pairs (claim, source): **{len(all_pairs)}** — accepted "
                 f"**{len(accepted_pairs & all_pairs)}**, uncertain **{n_uncertain}**, "
                 f"exhausted **{n_exhausted_pairs}**, no usable text **{n_no_usable_text}**")
    # Bibliography <-> body consistency, both directions (deterministic).
    # A reference named only in a table IS cited — the marker is there, in a benchmark
    # row — it simply produces no claim to verify.  Counting it as "never cited" would
    # accuse the parser of losing a marker it read correctly, and would hide the one
    # list worth reading: the references the document names nowhere at all.
    cited_numbers = {ci["ref_number"] for c in claims for ci in cites_by_claim.get(c["id"], []) if ci.get("ref_number") is not None}
    table_only = parse.get("table_only_citations") or []
    table_nums = sorted({t.get("ref_number") for t in table_only
                         if t.get("ref_number") in refnum})
    covered = cited_numbers | set(table_nums)
    uncited = [rn for rn in sorted(refnum) if rn not in covered]
    if refnum:
        pct = 100.0 * len(covered) / len(refnum)
        lines.append(
            f"- Reference coverage: **{len(covered)}/{len(refnum)} ({pct:.0f}%)** of the "
            f"bibliography is cited in the manuscript — {len(cited_numbers)} in prose"
            + (f", {len(set(table_nums) - cited_numbers)} only in a table" if table_nums else ""))
    coverage = parse.get("coverage") or {}
    if coverage.get("warning"):
        lines.append(f"- 🚨 **Citation markers probably lost** — {coverage['warning']}")
    if table_nums:
        lines.append(
            f"- 📋 Cited **only inside a table** — counted as cited, **not verified** "
            f"(a benchmark row is a label, not an assertion; re-run with "
            f"`--verify-table-citations` to check them anyway): "
            + ", ".join(f"[{n}]" for n in table_nums))
    if uncited:
        lines.append(f"- ⚠️ References **cited nowhere** — not in prose, not in a table: "
                     f"{', '.join(f'[{n}]' for n in uncited)}")
    # Duplicate entries: same DOI or PMID under different numbers.
    by_ident = defaultdict(list)
    for r in refs.values():
        for kind in ("doi", "pmid"):
            if r.get(kind):
                by_ident[(kind, r[kind].lower())].append(r["ref_number"])
    dups = [(k, sorted(v)) for k, v in by_ident.items() if len(v) > 1]
    for (kind, ident), nums in dups:
        lines.append(f"- ⚠️ **Duplicate** entries (same {kind.upper()} `{ident}`): "
                     + ", ".join(f"[{n}]" for n in nums))
    ambiguities = parse.get("ambiguities", [])
    n_orphan_cites = sum(1 for c in parse["citations"]
                         if not c.get("ref_id") and not c.get("candidate_ref_ids"))
    if ms.get("citation_mode") == "author-year":
        lines.append(f"- Author-year citations: **ambiguous {len(ambiguities)}**, "
                     f"orphan **{n_orphan_cites}**")
    lines.append("")

    # ---- Ambiguous citations (author-year): user decides ----
    if ambiguities:
        lines.append("## 2b. Ambiguous citations — which source? (your call)\n")
        lines.append("_Author-year conversion stopped: same surname+year with multiple "
                     "entries. Verification suspended until you choose._\n")
        for a in ambiguities:
            cands = "; ".join(f"[{c['ref_number']}] {c['raw_entry']}" for c in a["candidates"])
            lines.append(f"- `{a['marker_raw']}` → candidates: {cands}")
        lines.append("")

    # ---- Per-claim detail ----
    lines.append("## 3. Per-claim detail\n")
    for i, claim in enumerate(claims, 1):
        cites = cites_by_claim.get(claim["id"], [])
        status, why = claim_badge(
            claim, cites, semantic_by_pair, resolve_map, style_map, attempted_pairs,
            unreadable_ref_ids, isolated_uncertain, extraction_suspect_refs,
            terminal_causes_by_pair,
        )
        why = _claim_issue_with_external_identity(
            why, cites, resolve_map, external_labels_by_ref,
        )
        tag = "multi-source" if claim["id"] in multisource_claim_ids else "single-source"
        lines.append(f"### {BADGE[status]} C{i} · {tag} · marker `{claim['marker_raw']}`\n")
        lines.append(f"> {claim['sentence']}\n")
        for ci in cites:
            rid = ci.get("ref_id")
            rn = ci.get("ref_number")
            if rid is None:
                cand = ci.get("candidate_ref_ids") or []
                mk = ci.get("marker_raw", f"[{rn}]")
                if cand:
                    nums = ", ".join(f"[{refs[c]['ref_number']}]" for c in cand if c in refs)
                    lines.append(f"- `{mk}` — ⚠️ **ambiguous**: candidates {nums} — confirm which "
                                 "(explicit user choice required).")
                else:
                    lines.append(f"- `{mk}` — ❌ **not in bibliography** (orphan).")
                continue
            ref = refs[rid]
            ex = resolve_map.get(rid, {})
            st = style_map.get(rid, {})
            ax_exist = {
                "resolved": "✅ exists",
                "not_found": "❌ NOT found (possible fabrication)",
                "identifier_mismatch": "❌ DOI/PMID resolves to a DIFFERENT work (possibly wrong DOI)",
                "unverified": "❔ not automatically verified (search online)",
                "unresolved": "· unresolved (transient)",
                "skipped": "· skipped",
            }.get(ex.get("status"), "· n/a")
            external_label = external_labels_by_ref.get(rid)
            if ex.get("status") == "unverified" and external_label:
                ax_exist = f"✅ {external_label}"
            if ex.get("status") == "resolved" and ex.get("title_flag") == "warn":
                ax_exist = "✅ exists · ⚠️ title does not fully match the DOI"
            if ex.get("existence_corroboration") == "searched_not_found":
                ax_exist = ("⚠️ book NOT found in OpenLibrary or Google Books "
                            "(existence not corroborated — verify manually)")
            if ex.get("retracted"):
                ax_exist = "❌ exists but **RETRACTED**"
            ax_style = ("✅ style ok" if st.get("conforms")
                        else f"⚠️ style: {len(st.get('deviations',[]))} deviations" if st
                        else "· style n/a")
            lines.append(f"- **[{rn}]** {ref.get('source_type','?')} · {ax_exist} · {ax_style}")
            verification_row = verification_by_pair.get((claim["id"], rid))
            if verification_row:
                if verification_row["semantic_outcome"] is not None:
                    lines.append(
                        "    - verification outcome: **{outcome}** · result class `{result_class}` · "
                        "assurance `{assurance}` · resolution `{resolution}` · "
                        "lifecycle `operationally_complete={operational_complete}`".format(
                            outcome=verification_row["semantic_outcome"],
                            result_class=verification_row["result_class"],
                            assurance=verification_row["assurance"],
                            resolution=verification_row["resolution"],
                            operational_complete=verification_row["operational_complete"],
                        )
                    )
                    for evidence in verification_row["evidence"]:
                        text = evidence.get("text") if isinstance(evidence, dict) else str(evidence)
                        if text:
                            lines.append(f"        - ✓ «{_display_evidence_excerpt(text)}»")
                else:
                    lines.append(
                        "    - verification assessment: ⚪ _operational terminal without a semantic outcome · resolution "
                        f"`{verification_row['resolution']}` · no public semantic outcome_"
                    )
            else:
                pair = (claim["id"], rid)
                if pair in attempted_pairs or pair in terminal_causes_by_pair:
                    terminal_cause = terminal_causes_by_pair.get(pair)
                    terminal = {
                        "terminal_cause": terminal_cause,
                        "terminal_status": (
                            "exhausted"
                            if pair in pair_terminal["exhausted"]
                            else "uncertain"
                        ),
                    }
                    lines.append(_unstable_terminal_line(terminal))
                elif rid not in refs_with_text:
                    lines.append(
                        "    - support: **NOT ASSESSABLE** — source text unavailable; "
                        "no semantic verdict. Bibliographic existence is assessed separately."
                    )
                else:
                    lines.append(
                        "    - support: **INCOMPLETE** — registered source text has "
                        "no verification terminal; completion gate must not pass."
                    )
        lines.append("")

    # ---- Per-source detail ----
    lines.append("## 4. Per-source detail\n")
    for rn in sorted(refnum):
        ref = refnum[rn]
        ex = resolve_map.get(ref["id"], {})
        st = style_map.get(ref["id"], {})
        lines.append(f"### [{rn}] {ref.get('source_type','?')}")
        lines.append(f"- entry: _{ref['raw_entry'][:240]}_")
        tf = ex.get("title_flag")
        title_note = ""
        if tf == "warn":
            title_note = (f" — ⚠️ returned title «{(ex.get('matched_title') or '')[:80]}» "
                          f"only partially matches (overlap {ex.get('title_overlap')})")
        elif tf == "mismatch":
            title_note = (f" — ❌ returned title «{(ex.get('matched_title') or '')[:80]}» "
                          f"differs from the cited entry (overlap {ex.get('title_overlap')})")
        external_label = external_labels_by_ref.get(ref["id"])
        if ex.get("status") == "unverified" and external_label:
            lines.append(
                f"- existence: **{external_label}** "
                f"(bibliographic resolve status retained: `{ex.get('status')}`)"
                + title_note
            )
        else:
            lines.append(f"- existence: **{ex.get('status','n/a')}**"
                         + (" — ❌ **RETRACTED**" if ex.get("retracted") else "")
                         + (f" (via {ex.get('via')})" if ex.get('via') else "")
                         + (f" — {ex.get('reason')}" if ex.get('reason') else "")
                         + title_note)
        if ex.get("resolution_basis") or ex.get("existence_confidence"):
            basis = ex.get("resolution_basis", "unknown")
            conf = ex.get("existence_confidence", "unknown")
            via = ex.get("via", "?")
            reason = ex.get("reason", "")
            # Build resolve chain from attempts if available
            attempts = ex.get("attempts") or []
            if attempts:
                chain_parts = []
                for a in attempts:
                    name = a.get("via") or a.get("resolver") or "?"
                    status = a.get("status", "?")
                    if status == "resolved":
                        chain_parts.append(f"**{name}** ✓")
                    elif status == "rate_limited":
                        chain_parts.append(f"{name} (429)")
                    else:
                        chain_parts.append(f"{name} (no match)")
                if chain_parts:
                    lines.append(f"- resolve path: `{' → '.join(chain_parts)}`")
            display_reason = _existence_check_reason(
                status=ex.get("status"),
                reason=reason,
                external_label=external_label,
            )
            lines.append(
                f"- existence check: _basis `{basis}`, confidence `{conf}`_"
                + (f" — {display_reason}" if display_reason else "")
            )
        if ex.get("reference_status_tag"):
            lines.append(f"- evidence tag: `{ex.get('reference_status_tag')}`"
                         f", fabrication risk `{ex.get('fabrication_risk', 'unknown')}`"
                         + (f" — {ex.get('tag_reason')}" if ex.get("tag_reason") else ""))
            ev = ex.get("evidence_profile") or {}
            adjudication = ev.get("bibliographic_adjudication") or {}
            if adjudication:
                lines.append(
                    f"- bibliographic adjudication: `{adjudication.get('outcome')}` "
                    f"(checks `{adjudication.get('check_status')}`, correction "
                    f"`{adjudication.get('correction_status')}`)"
                )
                for refutation in adjudication.get("refutations") or []:
                    lines.append(
                        f"  - refutation `{refutation.get('kind')}` on "
                        f"`{refutation.get('field')}`: cited "
                        f"`{refutation.get('cited_value')}`, observed "
                        f"`{refutation.get('observed_value')}` via "
                        f"`{refutation.get('source')}`"
                    )
            checks = ", ".join(ev.get("checks_completed") or []) or "none"
            lines.append(f"- evidence profile: source `{ev.get('source_kind', 'unknown')}`, "
                         f"indexability `{ev.get('indexability', 'unknown')}`, "
                         f"checks `{checks}`")
            synth = ev.get("synthetic_reference_risk") or {}
            if synth.get("band") and synth.get("band") not in ("none", "not_scored"):
                signals = ", ".join(synth.get("signals") or [])
                lines.append(f"- synthetic-reference risk: `{synth.get('band')}` "
                             f"(score {synth.get('score')}; signals: {signals or 'none'})")
        ft = ex.get("fulltext_exists")
        if ft is False:
            lines.append("- full text: _non-existent (e.g. conference abstract): "
                         "abstract is the maximum verifiable_")
        elif ex.get("oa_status") == "paywalled":
            lines.append("- full text: _exists but behind a paywall: provide it for high reliability_")
        abs_status, abs_source = abstract_availability(ref["id"], prov_by_ref, resolve_map)
        if abs_status == "available":
            lines.append("- abstract: _available"
                         + (f" via {abs_source}" if abs_source else "")
                         + "_")
        else:
            lines.append("- abstract: _not available_")
        if ex.get("oa_status") == "paywalled" and abs_status == "available":
            lines.append("- standard fallback: _the full text is paywalled; "
                         "verification may proceed on the abstract tier unless a full text is provided_")
        # Advisory book-availability hint (full | partial | none): NOT a verdict input,
        # just guidance on what is worth providing. Region- and access-dependent.
        avail = ex.get("book_availability")
        if avail and avail != "unknown":
            _avail_lbl = {"full": "full preview available",
                          "partial": "partial preview only",
                          "none": "no preview (metadata only)"}.get(avail, avail)
            lines.append(f"- book availability: _{_avail_lbl} (advisory; "
                         "varies by region/access, not a guarantee)_")
        prov = prov_by_ref.get(ref["id"])
        if prov:
            for e in sorted(prov, key=lambda x: -_sources.TIER_RANK.get(x.get("tier"), -1)):
                selected = selected_by_ref.get(ref["id"])
                is_selected = bool(selected and selected.get("stored_as") == e.get("stored_as"))
                is_verifiable = is_verifiable_source_entry(e)
                how = e.get("mapping", "?")
                sig = e.get("match_signal")
                src = e.get("source_ref") or ""
                lines.append(f"- text: **{e.get('tier')}** via **{e.get('origin')}** "
                             f"(`{e.get('stored_as')}`; mapping {how}"
                             + ("; selected for verification" if is_selected else "")
                             + (
                                 "; not admissible for citation Verify"
                                 if not is_verifiable else ""
                             )
                             + (f", signal {sig}" if sig else "")
                             + (f"; from {src}" if src else "") + ")")
                if e.get("extraction_method"):
                    lines.append(f"  - extraction method: **{e.get('extraction_method')}**")
                flags = e.get("extraction_flags") or []
                if flags:
                    lines.append("  - ⚠️ extraction quality: suspect ("
                                 + ", ".join(flags)
                                 + ") — quote verification against this text may fail on artifacts")
                cv = e.get("content_version")
                if cv in _sources.NON_RECORD_VERSIONS:
                    _cv_label = "preprint" if cv == "preprint" else "accepted manuscript"
                    if e.get("provenance_relation") == _sources.OFFICIALLY_SURFACED_COPY:
                        lines.append(
                            f"  - source version: **{_cv_label}** (official citation-path copy; "
                            "not the formal version of record)"
                        )
                    else:
                        lines.append(
                            f"  - source version: **{_cv_label}** (NOT the version "
                            "of record) - verdict provisional; peer review may have "
                            "changed the cited passage"
                        )
                if e.get("identity_status"):
                    if e.get("identity_status") == "externally_corroborated_text":
                        lines.append("  - text identity: corroborated from a traceable "
                                     "external source"
                                     + (f" - {e.get('identity_note')}"
                                        if e.get("identity_note") else ""))
                        lines.append("  - source identity reliability: low "
                                     "(assisted retrieval outside `resolve`)")
                    elif e.get("identity_status") == "browser_session_cleared":
                        lines.append("  - text identity: retrieved after a user-cleared "
                                     "publisher challenge in a visible browser session"
                                     + (f" - {e.get('identity_note')}"
                                        if e.get("identity_note") else ""))
                    else:
                        lines.append(f"  - identity corroboration: `{e.get('identity_status')}`"
                                     + (f" - {e.get('identity_note')}"
                                        if e.get("identity_note") else ""))
            verifiable_prov = [e for e in prov if is_verifiable_source_entry(e)]
            if not verifiable_prov:
                lines.append("- verification text: _none admitted_")
            # Tier progression: summarize admissible tiers.
            tiers_present = {e.get("tier"): e for e in verifiable_prov}
            ft_entry = tiers_present.get("fulltext")
            abs_entry = tiers_present.get("abstract")
            preview_entry = next(
                (e for e in verifiable_prov
                 if e.get("tier") == "web" and e.get("origin") == "googlebooks"),
                None,
            )
            ft_exists = ex.get("fulltext_exists")
            oa_paywalled = ex.get("oa_status") == "paywalled"
            # Build progression steps
            steps = []
            if ft_entry:
                steps.append(f"fulltext ({ft_entry.get('origin','?')}) ✓ — used")
            elif ft_exists is True and oa_paywalled:
                steps.append("fulltext (paywalled — not retrieved)")
            elif ft_exists is True:
                steps.append("fulltext (exists but not retrieved)")
            elif ft_exists is False:
                steps.append("fulltext (does not exist — abstract is ceiling)")
            else:
                steps.append("fulltext (not found)")
            if abs_entry:
                marker = "✓ — used" if not ft_entry else "(fallback, also available)"
                steps.append(f"abstract ({abs_entry.get('origin','?')}) {marker}")
            if preview_entry:
                steps.append("Google Books preview snippet — available")
            if steps:
                lines.append(f"- 📊 Tier progression: {' → '.join(steps)}")
        else:
            lines.append("- text: _none recorded (verification not possible)_")
        if st:
            if st.get("conforms"):
                lines.append(f"- style {st.get('style')}: ✅ conforms")
                for d in st.get("deviations", []):
                    if d["severity"] == "note":
                        lines.append(f"    - [note] {d['code']}: {d['message']}")
            else:
                lines.append(f"- style {st.get('style')}: ⚠️ deviations:")
                for d in st.get("deviations", []):
                    lines.append(f"    - [{d['severity']}] {d['code']}: {d['message']}")
        lines.append("")

    # ---- Provenance ----
    lines.append("## 5. Provenance\n")
    lines.append(f"- parser: `{ms['parser_version']}`")
    lines.append(f"- run dir: `{os.path.abspath(run_dir)}`")
    lines.append("- report journal policy: `append-only` "
                 "(new runs append a sealed snapshot; earlier report text is never rewritten)")
    lines.append("- llm interpretation included: `no` "
                 "(post-hoc commentary, if requested, must live outside the official report)")
    lines.append(f"- typed verification terminals: {len(verification_rows)}")
    lines.append(f"- immutable LLM dispatch attempts: {dispatch_attempts}")
    lines.append("")

    # ---- machine-readable summary for CI ----
    n_fail = sum(1 for t in triage if t[0] == "fail")
    n_warn = sum(1 for t in triage if t[0] == "warn")
    summary = {
        "audit_ready": integrity["audit_ready"],
        "debug_mode": integrity["debug_mode"],
        "debug_labels": integrity["debug_labels"],
        "execution_initial_origin": execution_assurance["initial_origin"],
        "execution_protection": execution_assurance["protection"],
        "execution_agent_identity": execution_assurance["agent_identity"],
        "execution_failure_reason": execution_assurance["failure_reason"],
        "execution_acknowledged_at": execution_assurance["acknowledged_at"],
        "integrity_run_state": integrity["run"]["state"],
        "integrity_content_store_state": integrity["content_store"]["state"],
        "integrity_run_overrides": integrity["run"]["overrides"],
        "integrity_content_store_overrides": integrity["content_store"]["overrides"],
        "integrity_run_crash_recoveries": integrity["run"]["crash_recoveries"],
        "integrity_content_store_crash_recoveries": integrity[
            "content_store"
        ]["crash_recoveries"],
        "manuscript": ms["filename"],
        "citation_mode": ms.get("citation_mode"),
        "ambiguous_citations": len(ambiguities),
        "claims": len(claims),
        "multisource_claims": len(multisource_claim_ids),
        "references": len(refs),
        "references_resolved": n_res,
        "references_not_found": n_nf,
        "references_identifier_mismatch": n_mismatch,
        "references_unverified": n_unverified,
        "references_unverified_numbers": unverified_refs,
        "references_books_not_corroborated": searched_not_found_refs,
        "references_suspected_fabricated": len(suspected_refs),
        "references_suspected_fabricated_numbers": suspected_refs,
        "references_high_risk_weak_metadata": len(high_risk_weak_metadata_refs),
        "references_high_risk_weak_metadata_numbers": high_risk_weak_metadata_refs,
        "references_externally_corroborated_outside_resolve": len(external_text_refs),
        "references_externally_corroborated_outside_resolve_numbers": external_text_refs,
        "references_identifier_errors": len(identifier_error_refs),
        "references_identifier_error_numbers": identifier_error_refs,
        "references_unverified_low_indexability_numbers": low_index_refs,
        "fabrication_risk_band": risk_band,
        "fabrication_risk_ratio": round(suspected_ratio, 4),
        "unreadable_pdfs_parked": len(unreadable_q),
        "references_unresolved": n_unr,
        "references_retracted": n_retr,
        "references_uncited": uncited,
        "duplicate_references": [nums for _k, nums in dups],
        "pairs_total": len(all_pairs),
        "pairs_accepted": accepted_pair_count,
        "pairs_incomplete": pairs_incomplete,
        "pairs_positive": result_class_counts["positive"],
        "pairs_crediting_positive": crediting_positive_count,
        "pairs_incomplete_semantic": result_class_counts["incomplete"],
        "pairs_negative": result_class_counts["negative"],
        "pairs_unresolved_semantic": result_class_counts["unresolved"],
        "pairs_positive_rate": result_class_rates["positive"],
        "pairs_incomplete_semantic_rate": result_class_rates["incomplete"],
        "pairs_negative_rate": result_class_rates["negative"],
        "pairs_unresolved_semantic_rate": result_class_rates["unresolved"],
        "verification_complete": verification_complete,
        "verification_projection": verification_rows,
        "llm_metrics": llm_metrics,
        "pairs_exhausted": n_exhausted_pairs,
        "pairs_uncertain": n_uncertain,
        "pairs_uncertain_by_cause": pair_terminal["uncertain_by_cause"],
        "pairs_contested_negative": len(pair_terminal["contested_negative"]),
        "pairs_no_usable_text": n_no_usable_text,
        "pairs_unaccounted": n_unaccounted_pairs,
        "pairs_missing_text": n_missing,
        "claim_verification_coverage": {**claim_coverage, **claim_coverage_numbers},
        "claims_fail": n_fail,
        "claims_warn": n_warn,
        "run_health": health,
        # 'unverified' does NOT fail CI: it is "to check", not fabrication.
        "ci_pass": n_fail == 0 and n_nf == 0 and n_mismatch == 0 and n_retr == 0,
    }
    return "\n".join(lines) + "\n", summary


def write_report(
    run_dir: str,
    *,
    protocol_threshold: float = 0.30,
    integrity: dict,
) -> dict:
    """Write and seal one report snapshot inside an authority-owned lease."""
    md, summary = render(
        run_dir,
        protocol_threshold=protocol_threshold,
        integrity=integrity,
    )
    fields = provenance_fields(run_dir, summary)
    # Bind the seal to the report BODY too (not only parse+ledger), so editing the prose
    # after signing invalidates the seal. strip_seal() makes the writer's body hash match
    # what the gate recomputes from the sealed file.
    body_sha256 = hashlib.sha256(strip_seal(md).encode("utf-8")).hexdigest()
    payload = seal_payload(fields, body_sha256)
    content = hashlib.sha256(payload).hexdigest()
    seal = _signing.sign(payload)   # HMAC if a signing key is present, else sha256
    # Seal the report to its inputs AND body: an authentic report.md ends with this comment.
    # content= binds the report to parse+ledger+body (anyone can check); sig= is the
    # deterministic system's signature (HMAC un-forgeable without the secret key).
    snapshot_md = md + (f"\n<!-- citation-verifier-provenance alg={seal['alg']} "
                        f"sig={seal['sig']} content={content} -->\n")
    report_path = os.path.join(run_dir, "report.md")
    journal_md_path = journal_path(run_dir)
    previous = ""
    if os.path.exists(journal_md_path):
        with open(journal_md_path, encoding="utf-8") as f:
            previous = f.read()
    separator = journal_separator(previous)
    history_text = previous + separator if previous else ""
    history_sha256 = (hashlib.sha256(history_text.encode("utf-8")).hexdigest()
                      if history_text else None)
    created_at = (datetime.now(timezone.utc)
                  .replace(microsecond=0)
                  .isoformat()
                  .replace("+00:00", "Z"))
    entry = journal_entry(snapshot_md, created_at=created_at,
                          history_sha256=history_sha256)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(snapshot_md)
    mode = "a" if previous else "w"
    with open(journal_md_path, mode, encoding="utf-8") as f:
        if separator:
            f.write(separator)
        f.write(entry)
    return {"ok": True, "alg": seal["alg"], "content": content, **summary}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="run directory")
    ap.add_argument(
        "--agent-identity",
        metavar="AGENT_IDENTITY",
        help="stable opaque harness identity (1-64 ASCII characters)",
    )
    ap.add_argument("--protocol-threshold", type=float, default=0.30,
                    help="threshold above which to warn that the model may be too weak")
    ap.add_argument("--debug-override-artifact-integrity", action="store_true")
    ap.add_argument("--debug-override-reason")
    args = ap.parse_args()
    gate = None
    lease = None
    try:
        resolved = resolve_existing(
            args.run,
            args.agent_identity,
            debug_override=args.debug_override_artifact_integrity,
            override_reason=args.debug_override_reason,
        )
        gate = resolved.gate
        integrity = (
            trusted_report_integrity(
                gate,
                args.run,
                debug_override=args.debug_override_artifact_integrity,
                override_reason=args.debug_override_reason,
            )
            if gate is not None else None
        )
        lease = (
            gate.begin_pipeline_transition(
                args.run,
                checkpoint_kind="report",
                mutates_content_store=False,
            )
            if gate is not None else None
        )
    except IntegrityGateError as exc:
        if gate is None:
            raise SystemExit(f"integrity gate stopped report: {exc}") from exc
        try:
            resolved = downgrade_after_authority_failure(
                args.run, resolved.assurance, exc
            )
        except IntegrityGateError as confirmation_exc:
            raise SystemExit(
                f"integrity gate stopped report: {confirmation_exc}"
            ) from confirmation_exc
        gate = resolved.gate
        integrity = None
        lease = None
    try:
        result = write_report(
            args.run,
            protocol_threshold=args.protocol_threshold,
            integrity=integrity,
        )
        if gate is not None:
            gate.commit_pipeline_transition(args.run, lease)
    except BaseException as exc:
        try:
            if gate is not None:
                gate.abort_pipeline_transition(
                    args.run,
                    lease,
                    reason=f"report generation raised {type(exc).__name__}",
                )
        except IntegrityGateError as abort_exc:
            raise SystemExit(
                f"report failed and integrity transition could not abort: {abort_exc}"
            ) from exc
        raise
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
