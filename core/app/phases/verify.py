# core/app/phases/verify.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Phase: verify through the current claim-evidence public boundary."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from concurrent.futures import ThreadPoolExecutor

from core.app.runtime.repository import _load_parse_payload, _repo_open
from core.app.runtime.fetch_audit import record_identity_anomaly
from core.app.runtime.settings import ACTION_REQUIRED, _debug_mode_enabled, _progress
from core.app.runtime.sources import (
    _load_manifest_payload,
    _load_unreadable_payload,
    _materialize_resolve_abstracts,
    _resolve_map,
    _scope_for_source,
)
from core.app.runtime.tasks import _create_task, _pending_tasks, _pause
from core.invocation import run_command
from core.parse.footnotes import cross_reference_map
from core.resolve import sources as _sources
from core.verify.identity_gate import (
    bibliographic_identity_admitted,
    source_identity_attestation_block_reason,
)
from core.verify.claim_evidence import (
    Bm25DependencyUnavailable,
    Bm25RetrievalLimitError,
    ClaimEvidenceRuntime,
    ConfigError,
    resolve_context_settings,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
def _verify_config_error_message(exc: ConfigError) -> str:
    return f"invalid Verify configuration: {exc}"


def _resolved_verify_runtime_labels(config: object) -> tuple[str, str]:
    """Return deterministic provider and model labels from frozen Verify policy."""
    if not isinstance(config, dict):
        raise RuntimeError("missing or invalid frozen claim-evidence configuration")
    providers = config.get("providers")
    if not isinstance(providers, list) or not providers:
        raise RuntimeError("frozen claim-evidence configuration has no providers")

    backends, models = set(), set()
    for provider in providers:
        if not isinstance(provider, dict):
            raise RuntimeError("frozen claim-evidence configuration has invalid provider")
        name = provider.get("name")
        lanes = provider.get("lanes")
        if not isinstance(name, str) or not name or not isinstance(lanes, list) or not lanes:
            raise RuntimeError("frozen claim-evidence configuration has invalid provider lanes")
        backends.add(name)
        for lane in lanes:
            model = lane.get("model") if isinstance(lane, dict) else None
            if not isinstance(model, str) or not model:
                raise RuntimeError("frozen claim-evidence configuration has invalid lane model")
            models.add(model)

    return ", ".join(sorted(backends)), ", ".join(sorted(models))


def _sync_verify_runtime_setting(repository) -> None:
    """Project the already frozen provider policy into the mutable runtime label."""
    backend, model = _resolved_verify_runtime_labels(
        repository.get_run_setting("verify_claim_evidence_config")
    )
    runtime = repository.get_run_setting("verify_runtime")
    if not isinstance(runtime, dict):
        raise RuntimeError("missing or invalid verify runtime setting")
    repository.set_run_setting(
        "verify_runtime",
        {**runtime, "backend": backend, "model": model},
    )


def _verify_pairs(run, *, ref_id: str | None = None):
    """Return each cited source with locally persisted text exactly once."""
    parse = _load_parse_payload(run)
    refs = {ref["id"]: ref for ref in parse.get("references", [])}
    claims = {claim["id"]: claim for claim in parse.get("claims", [])}
    manifest = _load_manifest_payload(run)
    resolve_map = _resolve_map(run)
    unreadable = {
        row.get("ref_id"): row
        for row in (_load_unreadable_payload(run).get("entries") or [])
        if row.get("ref_id") and row.get("ocr_status") != "done"
    }
    cross_references = cross_reference_map(parse.get("references", []))
    pairs, seen = [], set()
    for citation in parse.get("citations", []):
        claim_id, reference_id = citation.get("claim_id"), citation.get("ref_id")
        # Link-silent resolved markers deliberately have no claim.  They are
        # coverage facts retained in Parse, not verification pairs.
        if claim_id is None and citation.get("provenance") == "link_silent_resolved":
            continue
        if ref_id is not None and reference_id != ref_id:
            continue
        if not reference_id or (claim_id, reference_id) in seen:
            continue
        if reference_id in cross_references:
            identity_reference_id, pinpoint = cross_references[reference_id]
            entry = _sources.best_for(manifest, identity_reference_id)
        else:
            identity_reference_id = reference_id
            pinpoint = None
            entry = _sources.best_for(manifest, reference_id)
        if not entry or not entry.get("stored_as"):
            continue
        entry = dict(
            entry,
            _identity_reference=refs.get(identity_reference_id, {}),
            _resolve_result=resolve_map.get(identity_reference_id) or {},
        )
        if pinpoint is not None:
            entry["cross_reference_pinpoint"] = pinpoint
        if reference_id in unreadable:
            entry["source_degradation"] = {
                "kind": "primary_pdf_unreadable",
                "ocr_status": unreadable[reference_id].get("ocr_status") or "pending",
                "reason": unreadable[reference_id].get("reason") or "unreadable PDF",
            }
        scope = _scope_for_source(dict(entry, ref_id=reference_id), resolve_map)
        if scope is None:
            continue
        seen.add((claim_id, reference_id))
        pairs.append((
            claims.get(claim_id, {}), refs.get(reference_id, {}),
            scope,
            os.path.join(run, "sources", entry["stored_as"]), entry,
        ))
    return pairs


def _verify_task_id(claim_id, reference_id, scope):
    return f"verify:{claim_id}:{reference_id}:{scope}"


def _source_identity_attestation_task_id(source_text_id: str) -> str:
    digest = hashlib.sha256(source_text_id.encode("utf-8")).hexdigest()
    return f"verify-identity:{digest[:24]}"


def _identity_gate_task(target: dict) -> dict:
    return {
        "reference": target["reference"],
        "source_identity_evidence": {
            **target["source_identity"],
            "resolve": target["resolve_identity"],
        },
    }


def _operator_kept_source_unverified(repository, entry: dict) -> bool:
    """Keep an applied negative identity decision binding across code updates."""
    source_text_id = entry.get("source_text_id")
    owner_ref_id = (entry.get("_identity_reference") or {}).get("id")
    if not isinstance(source_text_id, str) or not source_text_id:
        return False
    if not isinstance(owner_ref_id, str) or not owner_ref_id:
        return False
    record = repository.get_task(_source_identity_attestation_task_id(source_text_id))
    if record is None or record.status != "applied":
        return False
    decision = repository.source_identity_attestation_for(source_text_id)
    if decision is None or decision.get("action") != "keep_unverified":
        return False
    target = repository.source_identity_attestation_target(
        ref_id=owner_ref_id,
        source_text_id=source_text_id,
    )
    if (
        decision.get("ref_id") != owner_ref_id
        or decision.get("target_sha256") != target.get("target_sha256")
        or target.get("source_text_id") != source_text_id
        or target.get("source_text_sha256") != entry.get("sha256")
    ):
        raise SystemExit("applied source-identity skip is inconsistent")
    return True


def _selected_contract(repository):
    persisted = repository.get_run_setting("verify_semantic_contract")
    try:
        contract = ClaimEvidenceRuntime.select_contract(persisted, os.environ)
    except ValueError as exc:
        raise SystemExit(f"invalid verify semantic contract: {exc}") from exc
    if persisted is None:
        repository.set_run_setting("verify_semantic_contract", contract)
    return contract


def _claim_evidence_task(
    claim, reference, scope, path, *, source_text_id, source_text_sha256,
):
    if not isinstance(source_text_id, str) or not source_text_id:
        raise SystemExit("claim-evidence source identity is unavailable")
    if not isinstance(source_text_sha256, str) or not _SHA256_RE.fullmatch(source_text_sha256):
        raise SystemExit("claim-evidence source hash is unavailable")
    try:
        source_bytes = Path(path).read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != source_text_sha256:
            raise ValueError("source hash mismatch")
        source_text = source_bytes.decode("utf-8")
        payload, context = ClaimEvidenceRuntime.prepare_task(
            claim,
            source_text,
            context_settings=resolve_context_settings(os.environ),
        )
    except Bm25DependencyUnavailable as exc:
        raise SystemExit(str(exc)) from exc
    except Bm25RetrievalLimitError as exc:
        raise SystemExit(f"claim-evidence RAG context unavailable: {exc}") from exc
    except ConfigError as exc:
        raise SystemExit(f"invalid Verify context configuration: {exc}") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit("claim-evidence source context is unavailable") from exc
    return {
        "kind": "claim_evidence",
        "status": "pending",
        "semantic_contract": ClaimEvidenceRuntime.contract_id,
        "claim_id": claim.get("id"),
        "ref_id": reference.get("id"),
        "scope": scope,
        "claim_evidence_payload": payload,
        "effective_context": context,
        "source_text_id": source_text_id,
        "source_text_sha256": source_text_sha256,
        "answer": None,
    }


def _claim_evidence_source_text(repository, run: str, task: dict) -> str:
    """Resolve and verify a task source exclusively inside the current run."""
    source_text_id = task.get("source_text_id")
    expected_sha256 = task.get("source_text_sha256")
    if not isinstance(source_text_id, str) or not source_text_id:
        raise RuntimeError("claim-evidence task has no persisted source identity")
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
        raise RuntimeError("claim-evidence task has no valid source hash")
    record = repository.get_source_text(source_text_id)
    if record is None:
        raise RuntimeError("claim-evidence persisted source identity is unavailable")
    if record.sha256 != expected_sha256:
        raise RuntimeError("claim-evidence task and source ledger hashes disagree")

    stored_path = record.stored_path
    if (
        not isinstance(stored_path, str)
        or not stored_path
        or "\\" in stored_path
        or PurePosixPath(stored_path).is_absolute()
        or PureWindowsPath(stored_path).is_absolute()
        or ".." in PurePosixPath(stored_path).parts
        or PurePosixPath(stored_path).parts[:1] != ("sources",)
    ):
        raise RuntimeError("claim-evidence persisted source path is not run-local")
    run_root = Path(run).resolve()
    try:
        source_path = (run_root / Path(*PurePosixPath(stored_path).parts)).resolve(
            strict=True
        )
        source_path.relative_to(run_root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("claim-evidence persisted source is unavailable") from exc
    if not source_path.is_file():
        raise RuntimeError("claim-evidence persisted source is not a regular file")
    try:
        source_bytes = source_path.read_bytes()
        source_text = source_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("claim-evidence persisted source is unavailable") from exc
    if hashlib.sha256(source_bytes).hexdigest() != expected_sha256:
        raise RuntimeError("claim-evidence persisted source hash mismatch")
    if len(source_text) != record.char_count:
        raise RuntimeError("claim-evidence persisted source length mismatch")
    return source_text


def _require_final_task(record, expected: dict | None = None) -> None:
    payload = record.task_payload or {}
    if (
        payload.get("kind") != "claim_evidence"
        or payload.get("semantic_contract") != ClaimEvidenceRuntime.contract_id
        or not isinstance(payload.get("source_text_id"), str)
        or not _SHA256_RE.fullmatch(str(payload.get("source_text_sha256") or ""))
        or "source_text_path" in payload
    ):
        raise SystemExit("persisted Verify task uses an unsupported contract")
    if (
        expected is not None
        and record.status == "pending"
        and payload != expected
    ):
        raise SystemExit(
            "persisted claim-evidence task differs from the frozen source"
        )


def _require_identity_attestation_task(
    record, expected: dict | None = None,
) -> None:
    payload = record.task_payload or {}
    if (
        record.slot not in {"fetch", "verify"}
        or record.ref_id is None
        or record.claim_id is not None
        or record.scope is not None
        or payload.get("kind") != "source_identity_attestation"
        or payload.get("ref_id") != record.ref_id
        or not isinstance(payload.get("source_text_id"), str)
        or not _SHA256_RE.fullmatch(str(payload.get("source_text_sha256") or ""))
        or not _SHA256_RE.fullmatch(str(payload.get("target_sha256") or ""))
        or "stored_path" in payload
        or "source_text" in payload
    ):
        raise SystemExit(
            "persisted source-identity attestation uses an unsupported contract"
        )
    if expected is not None and any(
        payload.get(key) != expected.get(key)
        for key in (
            "ref_id",
            "source_text_id",
            "source_text_sha256",
            "target_sha256",
        )
    ):
        raise SystemExit(
            "persisted source-identity attestation differs from the frozen source"
        )


def _require_verify_task(record) -> None:
    if record.task_kind == "claim_evidence":
        _require_final_task(record)
    elif record.task_kind == "source_identity_attestation":
        _require_identity_attestation_task(record)
    else:
        raise SystemExit("persisted Verify task uses an unsupported contract")


def _terminalize_identity_unverified(
    repository, claim, reference, scope, *,
    cause: str = "bibliographic_identity_not_corroborated",
) -> None:
    task_id = _verify_task_id(claim.get("id"), reference.get("id"), scope)
    existing = repository.get_task(task_id)
    if existing is not None and existing.status == "applied":
        pair = repository.get_verification_pair_state(
            claim_id=claim.get("id"),
            ref_id=reference.get("id"),
            scope=scope,
        )
        if (
            pair is not None
            and pair.get("status") == "uncertain"
            and pair.get("terminal_cause")
            == cause
        ):
            # This exact nonsemantic terminal is the durable record of a
            # keep_unverified decision.  A resumed Verify phase must preserve
            # it rather than treating its runner-owned task as semantic work.
            return
        raise SystemExit(
            "cannot keep source identity unverified after semantic verification"
        )
    repository.ensure_verification_pair(
        claim_id=claim.get("id"),
        ref_id=reference.get("id"),
        scope=scope,
    )
    repository.claim_verification_pair_terminal(
        claim_id=claim.get("id"),
        ref_id=reference.get("id"),
        scope=scope,
        status="uncertain",
        outcome=None,
        cause=cause,
    )
    if existing is not None:
        payload = dict(existing.task_payload or {})
        payload["status"] = "done"
        repository.apply_task(task_id, task_payload=payload)


def _emit_verify_tasks(st: dict, *, ref_id: str | None = None) -> dict:
    run = st["run_dir"]
    pairs = _verify_pairs(run, ref_id=ref_id)
    repository = _repo_open(run)
    if repository is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    created = 0
    identity_created = 0
    try:
        contract = _selected_contract(repository)
        for record in repository.list_tasks(slot="verify"):
            _require_verify_task(record)
        for claim, reference, scope, path, entry in pairs:
            if _claim_has_page_layout_interruption(claim):
                repository.ensure_verification_pair(
                    claim_id=claim.get("id"),
                    ref_id=reference.get("id"),
                    scope=scope,
                )
                repository.claim_verification_pair_terminal(
                    claim_id=claim.get("id"),
                    ref_id=reference.get("id"),
                    scope=scope,
                    status="uncertain",
                    outcome=None,
                    cause="structural_claim_contamination",
                )
                continue
            try:
                source_text = _claim_evidence_source_text(
                    repository,
                    run,
                    {
                        "source_text_id": entry.get("source_text_id"),
                        "source_text_sha256": entry.get("sha256"),
                    },
                )
            except (OSError, RuntimeError, UnicodeError) as exc:
                owner_ref_id = (entry.get("_identity_reference") or {}).get("id")
                record_identity_anomaly(
                    repository,
                    owner_ref_id or reference.get("id"),
                    phase="verify",
                    stage="source_context",
                    error=exc,
                )
                _terminalize_identity_unverified(
                    repository,
                    claim,
                    reference,
                    scope,
                    cause="source_integrity_unavailable",
                )
                continue
            identity_task = {
                "reference": entry.get("_identity_reference"),
                "source_identity_evidence": {
                    "identity_status": entry.get("identity_status"),
                    "match_signal": entry.get("match_signal"),
                    "match_score": entry.get("match_score"),
                    "resolve": entry.get("_resolve_result") or {},
                    "origin": entry.get("origin"),
                    "mapping": entry.get("mapping"),
                    "supplied_by": entry.get("supplied_by"),
                    "supplied_via": entry.get("supplied_via"),
                },
            }
            if scope.startswith("fulltext") and _operator_kept_source_unverified(
                repository, entry,
            ):
                _terminalize_identity_unverified(
                    repository, claim, reference, scope,
                )
                continue
            identity_admitted = True
            if scope.startswith("fulltext"):
                try:
                    identity_admitted = bibliographic_identity_admitted(
                        identity_task, source_text,
                    )
                except Exception as exc:
                    owner_ref_id = (entry.get("_identity_reference") or {}).get("id")
                    record_identity_anomaly(
                        repository,
                        owner_ref_id or reference.get("id"),
                        phase="verify",
                        stage="identity_admission",
                        error=exc,
                    )
                    identity_admitted = False
            if scope.startswith("fulltext") and not identity_admitted:
                source_text_id = entry.get("source_text_id")
                owner_ref_id = (entry.get("_identity_reference") or {}).get("id")
                if (
                    not isinstance(source_text_id, str)
                    or not source_text_id
                    or not isinstance(owner_ref_id, str)
                    or not owner_ref_id
                ):
                    exc = RuntimeError(
                        "source-identity attestation target is unavailable"
                    )
                    record_identity_anomaly(
                        repository,
                        reference.get("id"),
                        phase="verify",
                        stage="identity_target",
                        error=exc,
                    )
                    _terminalize_identity_unverified(
                        repository,
                        claim,
                        reference,
                        scope,
                        cause="source_identity_target_unavailable",
                    )
                    continue
                try:
                    target = repository.source_identity_attestation_target(
                        ref_id=owner_ref_id,
                        source_text_id=source_text_id,
                    )
                except (RuntimeError, ValueError) as exc:
                    record_identity_anomaly(
                        repository,
                        owner_ref_id,
                        phase="verify",
                        stage="identity_target",
                        error=exc,
                    )
                    _terminalize_identity_unverified(
                        repository,
                        claim,
                        reference,
                        scope,
                        cause="source_identity_target_unavailable",
                    )
                    continue
                if (
                    target["source_text_sha256"] != entry.get("sha256")
                    or target["source_text_id"] != source_text_id
                ):
                    exc = RuntimeError(
                        "source-identity attestation source binding changed"
                    )
                    record_identity_anomaly(
                        repository,
                        owner_ref_id,
                        phase="verify",
                        stage="identity_target_binding",
                        error=exc,
                    )
                    _terminalize_identity_unverified(
                        repository,
                        claim,
                        reference,
                        scope,
                        cause="source_identity_target_unavailable",
                    )
                    continue
                identity_task = _identity_gate_task(target)
                decision = repository.source_identity_attestation_for(
                    source_text_id,
                )
                if decision is not None:
                    if (
                        decision.get("ref_id") != owner_ref_id
                        or decision.get("target_sha256")
                        != target["target_sha256"]
                    ):
                        raise SystemExit(
                            "applied source-identity attestation is inconsistent"
                        )
                    if decision.get("action") == "keep_unverified":
                        _terminalize_identity_unverified(
                            repository, claim, reference, scope,
                        )
                        continue
                    if decision.get("action") != "attest_identity" or not (
                        bibliographic_identity_admitted(
                            identity_task,
                            source_text,
                            identity_attested=True,
                        )
                    ):
                        raise SystemExit(
                            "applied source-identity attestation is not admissible"
                        )
                else:
                    try:
                        block_reason = source_identity_attestation_block_reason(
                            identity_task, source_text,
                        )
                    except Exception as exc:
                        record_identity_anomaly(
                            repository,
                            owner_ref_id,
                            phase="verify",
                            stage="identity_attestation_eligibility",
                            error=exc,
                        )
                        _terminalize_identity_unverified(
                            repository,
                            claim,
                            reference,
                            scope,
                            cause="source_identity_check_unavailable",
                        )
                        continue
                    identity_task_id = _source_identity_attestation_task_id(
                        source_text_id,
                    )
                    existing = repository.get_task(identity_task_id)
                    if existing is not None:
                        _require_identity_attestation_task(existing, target)
                        if existing.status == "applied":
                            raise SystemExit(
                                "applied source-identity attestation has no decision"
                            )
                        if existing.status == "answered":
                            raise SystemExit(
                                "source-identity attestation answer was not applied"
                            )
                        if existing.status == "cancelled":
                            raise SystemExit(
                                "source-identity attestation was cancelled"
                            )
                    if block_reason is None:
                        if existing is None:
                            repository.create_source_identity_attestation(
                                task_id=identity_task_id,
                                ref_id=owner_ref_id,
                                source_text_id=source_text_id,
                                instructions=(
                                    "Confirm whether this exact source text is the cited "
                                    "work. Answer attest_identity or keep_unverified, "
                                    "binding the answer to target_sha256 and giving a reason."
                                ),
                            )
                            identity_created += 1
                        continue
                    if existing is not None:
                        raise SystemExit(
                            "pending source-identity attestation is no longer eligible"
                        )
                    _terminalize_identity_unverified(
                        repository, claim, reference, scope,
                    )
                    continue
            task = _claim_evidence_task(
                claim,
                reference,
                scope,
                path,
                source_text_id=entry.get("source_text_id"),
                source_text_sha256=entry.get("sha256"),
            )
            task_id = _verify_task_id(claim.get("id"), reference.get("id"), scope)
            existing = repository.get_task(task_id)
            if existing is None:
                _create_task(run, "verify", task_id, task)
                created += 1
            else:
                _require_final_task(existing, task)
    finally:
        repository.close()
    return {
        "pairs": pairs,
        "created": created,
        "identity_created": identity_created,
        "semantic_contract": contract,
    }


def _claim_has_page_layout_interruption(claim: dict) -> bool:
    provenance = claim.get("structural_provenance")
    return (
        isinstance(provenance, dict)
        and isinstance(provenance.get("boundary_kinds"), list)
        and "page_layout_interruption" in provenance["boundary_kinds"]
    )


def _execute_claim_evidence_tasks(st: dict) -> int:
    run = st["run_dir"]
    pending = [
        (handle, task) for handle, task in _pending_tasks(run, "verify")
        if task.get("kind") == "claim_evidence"
    ]
    if not pending:
        return 0
    repository = _repo_open(run)
    if repository is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    runtime = None
    try:
        runtime_env = dict(os.environ)
        model = st.get("model")
        if isinstance(model, str) and model and not runtime_env.get(
            "CITATION_VERIFIER_MODEL"
        ):
            runtime_env["CITATION_VERIFIER_MODEL"] = model
        try:
            runtime = ClaimEvidenceRuntime.for_run(
                repository, runtime_env, run_id=repository.get_run().run_id,
            )
        except ConfigError as exc:
            raise SystemExit(_verify_config_error_message(exc)) from None
        _sync_verify_runtime_setting(repository)

        prepared = [
            (handle, task, _claim_evidence_source_text(repository, run, task))
            for handle, task in pending
        ]

        def execute_task(item):
            _handle, task, source_text = item
            terminal = runtime.execute(task, source_text=source_text)
            if not isinstance(terminal, dict) or terminal.get("status") == "open":
                raise RuntimeError("claim-evidence controller did not publish a terminal")
            return terminal

        def apply_terminal(handle):
            record = repository.get_task(handle)
            if record is None:
                raise RuntimeError("claim-evidence task disappeared before completion")
            payload = dict(record.task_payload or {})
            payload["status"] = "done"
            repository.apply_task(handle, task_payload=payload)

        checkpoint = st.get("_integrity_unit_checkpoint")
        if callable(checkpoint):
            # A signed checkpoint follows every accepted pair.  Serial guarded
            # dispatch means a hard crash can replay at most the pair in flight.
            for item in prepared:
                handle = item[0]
                execute_task(item)
                apply_terminal(handle)
                checkpoint("verify_pair", handle)
            return len(pending)

        with ThreadPoolExecutor(
            max_workers=runtime.aggregate_in_flight,
        ) as executor:
            futures = [executor.submit(execute_task, item) for item in prepared]
        # Leaving the executor waits for every worker before task-state writes,
        # so the shared run connection is not mutated here while a controller
        # is still persisting its ledger.  Resolve each future separately so a
        # later worker failure cannot discard an earlier successful terminal.
        outcomes = []
        first_error = None
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as exc:
                outcomes.append(None)
                if first_error is None:
                    first_error = exc
        for (handle, _task), terminal in zip(pending, outcomes):
            if terminal is None:
                continue
            apply_terminal(handle)
        if first_error is not None:
            raise first_error
    finally:
        if runtime is not None:
            runtime.close()
        repository.close()
    return len(pending)


def _apply_answered_source_identity_attestations(run: str) -> None:
    repository = _repo_open(run)
    if repository is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        tasks = [
            task for task in repository.list_tasks()
            if task.task_kind == "source_identity_attestation"
        ]
        cancelled = [task.task_id for task in tasks if task.status == "cancelled"]
        if cancelled:
            raise SystemExit(
                "source-identity attestation cancelled: "
                + ", ".join(sorted(cancelled))
            )
        for task in tasks:
            if task.status == "answered":
                try:
                    repository.apply_source_identity_attestation(task.task_id)
                except (RuntimeError, ValueError) as exc:
                    raise SystemExit(
                        "source-identity attestation application failed for "
                        f"{task.task_id}: {exc}"
                    ) from exc
    finally:
        repository.close()


def _pending_source_identity_attestations(run: str) -> int:
    repository = _repo_open(run)
    if repository is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        return sum(
            task.task_kind == "source_identity_attestation"
            and task.status == "pending"
            for task in repository.list_tasks()
        )
    finally:
        repository.close()


def _retire_web_secondhand_tasks(run: str) -> None:
    """Cancel legacy third-party web Verify work before it can run on resume."""
    repository = _repo_open(run)
    if repository is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        for task in repository.list_tasks(slot="verify"):
            if task.scope == "web_secondhand" and task.status != "applied":
                repository.cancel_task(task.task_id)
    finally:
        repository.close()


def phase_verify(st):
    _progress("verifying claims against source texts")
    run = st["run_dir"]
    # Point raw HTTP tracing at this run so debug runs capture per-attempt
    # telemetry; a no-op outside debug mode (the debug_events writer self-gates).
    from core.verify.backends._chat_transport import configure_debug_trace
    configure_debug_trace(run, _debug_mode_enabled(st))
    _retire_web_secondhand_tasks(run)
    _materialize_resolve_abstracts(run)
    _apply_answered_source_identity_attestations(run)
    emitted = _emit_verify_tasks(st)
    pending_identity = _pending_source_identity_attestations(run)
    if pending_identity:
        return _pause(
            "verify",
            run,
            pending_identity,
            (
                "Inspect each source-identity task and answer with "
                f"`{run_command('tasks', 'answer-review', '--action', 'attest-identity')}` "
                "or skip all of them while keeping their sources unverified with "
                f"`{run_command('tasks', 'skip-identity', '--run', run)}`."
            ),
        )
    if not emitted["pairs"]:
        return "web_research"
    _execute_claim_evidence_tasks(st)
    return "web_research"
