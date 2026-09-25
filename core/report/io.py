#!/usr/bin/env python3
# core/report/io.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Report I/O helpers — typed repository access and projection hashing."""

from __future__ import annotations

import hashlib
import importlib
import os

try:
    from core.infra.integrity import signing as _signing
    from core.infra.db import RunRepository
    from core.report.verification_projection import project_verification_pairs
    from core.report.llm_metrics_projection import project_llm_metrics
    from core.report.credential_metrics_projection import project_credential_metrics
except ImportError:
    import signing as _signing
    from db import RunRepository
    from verification_projection import project_verification_pairs
    from llm_metrics_projection import project_llm_metrics
    from credential_metrics_projection import project_credential_metrics


def _repo_open(run_dir):
    return RunRepository.open_readonly(run_dir)


def _repo_setting(run_dir, key, default=None):
    repo = _repo_open(run_dir)
    if repo is None:
        return default
    try:
        return repo.get_run_setting(key, default)
    finally:
        repo.close()


def _effective_style(repo):
    return repo.get_run_setting("style") or repo.get_run().style


def _sha256_file(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _db_projection_sha256(run_dir):
    repo = _repo_open(run_dir)
    if repo is None:
        return None
    try:
        verification_raw = repo.verification_raw_payloads()
        verification_projection = project_verification_pairs(
            verification_raw["pair_states"],
            verification_raw["candidates"],
            verification_raw["candidate_events"],
        )
        projection = {
            "parse_raw": repo.parse_payload(),
            "parse_effective": repo.effective_parse_payload(),
            "manual_parse_adjudication": repo.manual_parse_review_projection(),
            "resolve": repo.resolve_payload_map(),
            "manifest": repo.source_manifest_payload(),
            "unreadable": repo.unreadable_payload(),
            "verify_runtime": repo.get_run_setting("verify_runtime"),
            "verification_raw": verification_raw,
            "verification_projection": verification_projection,
            "style": _effective_style(repo),
        }
    finally:
        repo.close()
    return hashlib.sha256(_signing.canonical(projection)).hexdigest()


def _verification_projection_sha256(run_dir):
    repo = _repo_open(run_dir)
    if repo is None:
        return None
    try:
        raw = repo.verification_raw_payloads()
        projection = project_verification_pairs(
            raw["pair_states"], raw["candidates"], raw["candidate_events"]
        )
    finally:
        repo.close()
    return hashlib.sha256(_signing.canonical(projection)).hexdigest()


def _operator_classification(provenance):
    """Return a non-identifying admission label, never provenance contents."""
    if (
        not isinstance(provenance, dict)
        or provenance.get("producer_class") != "operator"
        or not isinstance(provenance.get("authority_id"), str)
        or not provenance["authority_id"]
    ):
        return None
    return (
        "local_operator_unattested"
        if provenance.get("authority_id") == "local-unattested"
        else "authority_authenticated_operator"
    )


def _remediation_state(repo):
    """Read the narrow, privacy-safe remediation audit projection.

    This is intentionally not a task inbox: only applied interventions are
    shown, and their answer bodies, file metadata and operator identities never
    leave the repository boundary.
    """
    interventions = []
    footnote_split_required = []
    for row in repo.manual_parse_review_projection():
        if (
            row.get("subject_type") == "footnote_note"
            and row.get("manual_review_required") is True
            and row.get("action") is None
        ):
            footnote_split_required.append({"note_number": row.get("note_number")})
            continue
        classification = _operator_classification({
            "producer_class": row.get("producer_class"),
            "authority_id": row.get("authority_id"),
        })
        if row.get("action") is None or not row.get("applied_at") or classification is None:
            continue
        interventions.append({
            "kind": "manual_parse_override", "effect": (
                "retained_uncertainty" if row.get("action") in {"keep_ambiguous", "keep_unresolved"} else "override"
            ),
            "task_id": row.get("task_id"), "answer_id": row.get("answer_id"),
            "claim_id": row.get("claim_id"), "ref_id": row.get("ref_id"),
            "source_id": None, "action": row.get("action"),
            "applied_at": row.get("applied_at"),
            "admission_classification": classification,
        })

    manifest = repo.source_manifest_payload().get("entries", [])
    for source in manifest:
        source_id = source.get("source_text_id")
        if not isinstance(source_id, str):
            continue
        decision = repo.source_identity_attestation_for(source_id)
        if decision is not None:
            classification = (decision.get("provenance") or {}).get("classification")
            interventions.append({
                "kind": "source_identity_attestation", "effect": (
                    "retained_uncertainty"
                    if decision.get("action") == "keep_unverified"
                    else "operator_identity_attestation"
                ),
                "task_id": decision.get("task_id"), "answer_id": decision.get("answer_id"),
                "claim_id": None, "ref_id": decision.get("ref_id"), "source_id": source_id,
                "action": decision.get("action"), "applied_at": decision.get("applied_at"),
                "admission_classification": classification if classification in {
                    "local_operator_unattested", "authority_authenticated_operator"
                } else None,
            })

    source_by_answer = {}
    for source in manifest:
        supplied_via = source.get("supplied_via")
        if isinstance(supplied_via, str) and supplied_via.startswith("controlled_task_answer:"):
            answer_id = supplied_via.removeprefix("controlled_task_answer:")
            if answer_id:
                source_by_answer.setdefault(answer_id, []).append(source)
    for view in repo.list_task_views(statuses=("applied",)):
        task_id = view.get("task_id")
        if not isinstance(task_id, str):
            continue
        task = repo.get_task(task_id)
        if task is None or task.task_kind not in {"fetch", "browser_challenge", "web_research"}:
            continue
        answer = repo.get_latest_task_answer(task.task_id)
        if answer is None or not answer.accepted_for_processing:
            continue
        classification = _operator_classification(repo.get_task_answer_provenance(answer.answer_id))
        if classification is None:
            continue
        if task.task_kind == "web_research":
            if answer.raw_payload.get("found") is False:
                continue
            kind, sources = "operator_research", [None]
        else:
            sources = source_by_answer.get(answer.answer_id, [])
            if not sources:
                continue
            kind = {"fetch": "operator_fetch", "browser_challenge": "operator_browser"}[task.task_kind]
        for source in sources:
            source_kind = kind
            if task.task_kind == "fetch" and (
                source.get("origin") == "ocr"
                or source.get("extraction_method") == "ocr"
            ):
                source_kind = "operator_ocr"
            interventions.append({
                "kind": source_kind,
                "effect": "operator_submitted_research" if source_kind == "operator_research" else "operator_supplied_evidence",
                "task_id": task.task_id, "answer_id": answer.answer_id,
                "claim_id": task.claim_id,
                "ref_id": (source or {}).get("ref_id") or task.ref_id,
                "source_id": (source or {}).get("source_text_id"),
                "action": "submitted", "applied_at": task.applied_at,
                "admission_classification": classification,
            })
    interventions = [row for row in interventions if isinstance(row.get("task_id"), str) and isinstance(row.get("answer_id"), str)]
    interventions.sort(key=lambda row: (str(row.get("applied_at") or ""), row["kind"], row["task_id"], row["answer_id"], str(row.get("source_id") or "")))
    deduped = []
    seen = set()
    for row in interventions:
        key = tuple(row.get(field) for field in ("kind", "task_id", "answer_id", "source_id"))
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    run = repo.get_run()
    provenance = repo.get_completed_remediation_provenance()
    child_run = None
    if run.run_origin == "remediated_from_completed":
        if (
            not isinstance(run.parent_run_id, str)
            or provenance is None
            or provenance.get("parent_run_id") != run.parent_run_id
        ):
            raise RuntimeError("completed remediation child lineage is inconsistent")
        child_run = {
            "run_origin": run.run_origin,
            "parent_run_id": run.parent_run_id,
            "provenance": provenance,
        }
    return {
        "interventions": deduped, "child_run": child_run,
        "footnote_split_required": sorted(
            footnote_split_required, key=lambda item: str(item.get("note_number") or "")
        ),
    }


def load_run_projection(run_dir):
    """Load the deterministic, report-facing projection for one run.

    This deliberately exposes the same typed inputs used by the Markdown
    renderer so companion renderers never need to parse ``report.md``.
    """
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        raw = repo.verification_raw_payloads()
        parse_raw = repo.parse_payload()
        parse_effective = repo.effective_parse_payload()
        resolve_map = repo.resolve_payload_map()
        projection = {
            "parse_raw": parse_raw,
            "parse_effective": parse_effective,
            "manual_parse_adjudication": repo.manual_parse_review_projection(),
            "resolve_map": resolve_map,
            "manifest": repo.source_manifest_payload(),
            "unreadable": repo.unreadable_payload(),
            "fetch_attempts_by_ref": {
                ref["id"]: repo.list_fetch_attempts(ref["id"])
                for ref in parse_effective.get("references", [])
            },
            "verification_pair_states": repo.verification_pair_state_payloads(),
            "verification_raw": raw,
            "verify_runtime": repo.get_run_setting("verify_runtime", {}),
            "verify_policy": repo.get_run_setting(
                "verify_claim_evidence_config", {}
            ),
            "execution": {
                "debug_mode": repo.get_run_setting("debug_mode", False),
                "debug_labels": repo.get_run_setting("debug_labels", []),
            },
            "manuscript_text": repo.get_manuscript_text(),
            "remediation": _remediation_state(repo),
        }
        projection["verification_projection"] = project_verification_pairs(
            raw["pair_states"], raw["candidates"], raw["candidate_events"])
        projection["llm_metrics"] = project_llm_metrics(
            raw["logical_requests"], raw["dispatch_attempts"], raw["dispatch_events"],
            raw["candidates"], raw["candidate_events"], raw["pair_states"],
            raw.get("jury1_rejections", ()))
        projection["credential_metrics"] = project_credential_metrics(
            repo.credential_inventory(),
            repo.list_credential_transport_observations(),
        )
        return projection
    finally:
        repo.close()


# Kept private as an alias for existing report callers while companion
# renderers use the public name above.
_load_run_projection = load_run_projection


def _load_style_projection(run_dir, parse):
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        style_name = _effective_style(repo)
    finally:
        repo.close()
    if not style_name:
        return {"checks": []}
    mod = importlib.import_module(f"core.style.{style_name}")
    checks = []
    for ref in parse.get("references", []):
        res = mod.check(ref["raw_entry"], ref.get("source_type") or "unknown")
        res["style"] = style_name
        res["source_type"] = ref.get("source_type")
        res["ref_id"] = ref["id"]
        res["ref_number"] = ref.get("ref_number")
        checks.append(res)
    return {"style": style_name, "checks": checks}
