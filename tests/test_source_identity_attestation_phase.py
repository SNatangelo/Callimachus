# tests/test_source_identity_attestation_phase.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pause/resume integration for manual source-identity attestations."""

from __future__ import annotations

import hashlib
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from core.app.phases import verify as verify_phase
from core.app.phases import fetch as fetch_phase
from core.app.runtime.settings import ACTION_REQUIRED
from core.infra.db.repository import RunRepository


def _run_with_unverified_fulltext(
    tmp_path,
    *,
    resolve_status="unverified",
    identity_status="unverified",
    match_signal="title",
    match_score=0.5,
    include_admitted_sibling=False,
):
    run_dir = tmp_path / "run"
    source_text = "A cited study\nSmith\nEvidence inspected by the operator."
    source_path = run_dir / "sources" / "parsed" / "1_fulltext_manual.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source_text.encode("utf-8"))
    repo = RunRepository.create(
        str(run_dir),
        run_id="identity-phase",
        input_path="paper.pdf",
        input_sha256="a" * 64,
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile="default",
        challenge_mode="off",
        fixture_fingerprint="identity-phase",
    )
    claims = [{
            "id": "c1",
            "sentence": "Claim [1].",
            "context_window": "Claim [1].",
            "marker_raw": "[1]",
        }]
    references = [{
            "id": "r1",
            "ref_number": 1,
            "raw_entry": "Smith. A cited study. 2020.",
            "title": "A cited study",
            "year": 2020,
        }]
    citations = [{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}]
    if include_admitted_sibling:
        claims.append({
            "id": "c2",
            "sentence": "Sibling claim [2].",
            "context_window": "Sibling claim [2].",
            "marker_raw": "[2]",
        })
        references.append({
            "id": "r2",
            "ref_number": 2,
            "raw_entry": "Jones. A second cited study. 2021.",
            "title": "A second cited study",
            "year": 2021,
        })
        citations.append({"claim_id": "c2", "ref_id": "r2", "ref_number": 2})
    repo.replace_parse_payload(
        manuscript_text="Claim [1]. Sibling claim [2].",
        claims=claims,
        references=references,
        citations=citations,
    )
    repo.store_source_text(
        source_text_id="source-1",
        ref_id="r1",
        identity_key="path:parsed/1_fulltext_manual.txt",
        tier="fulltext",
        origin="user",
        stored_path="sources/parsed/1_fulltext_manual.txt",
        sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        char_count=len(source_text),
        mapping="user_manifest",
        match_signal=match_signal,
        match_score=match_score,
        identity_status=identity_status,
        supplied_by="user",
        supplied_via="user_manifest",
    )
    repo.upsert_resolve_result("r1", {
        "ref_id": "r1",
        "status": resolve_status,
        "reason": "manual source identity is not independently corroborated",
        "matched_title": "A cited study",
        "reference_status_tag": "unverified",
        "fabrication_risk": "low",
    })
    if include_admitted_sibling:
        sibling_text = "A second cited study\nJones\nEvidence for the sibling claim."
        sibling_path = run_dir / "sources" / "parsed" / "2_fulltext_manual.txt"
        sibling_path.write_bytes(sibling_text.encode("utf-8"))
        repo.store_source_text(
            source_text_id="source-2",
            ref_id="r2",
            identity_key="path:parsed/2_fulltext_manual.txt",
            tier="fulltext",
            origin="user",
            stored_path="sources/parsed/2_fulltext_manual.txt",
            sha256=hashlib.sha256(sibling_text.encode("utf-8")).hexdigest(),
            char_count=len(sibling_text),
            mapping="user_manifest",
            match_signal="title",
            match_score=1.0,
            identity_status="corroborated_bibliography",
            supplied_by="user",
            supplied_via="user_manifest",
        )
        repo.upsert_resolve_result("r2", {
            "ref_id": "r2",
            "status": "resolved",
            "reason": "title corroborated",
            "matched_title": "A second cited study",
            "reference_status_tag": "verified",
            "fabrication_risk": "low",
        })
    repo.close()
    return str(run_dir)


def _answer_identity(run_dir, action):
    repo = RunRepository.open(run_dir)
    try:
        task = next(
            task for task in repo.list_tasks()
            if task.task_kind == "source_identity_attestation"
        )
        target = task.task_payload["target_sha256"]
        answer_id = repo.submit_task_answer(
            task_id=task.task_id,
            actor_type="user",
            raw_payload={
                "action": action,
                "target_sha256": target,
                "reason": "I inspected the exact source and reference.",
            },
        )
        repo.append_task_answer_provenance({
            "answer_id": answer_id,
            "producer_class": "operator",
            "producer_identity": "local-operator-unattested",
            "authenticated_uid": 1000,
            "ingress_kind": "controlled_metadata",
            "admitted_at": "2026-09-06T12:00:00+00:00",
            "file_count": 0,
            "authority_id": "local-unattested",
        }, [])
        return task.task_id
    finally:
        repo.close()


def test_fetch_preflight_creates_hash_bound_identity_task_for_selected_fulltext(tmp_path):
    run_dir = _run_with_unverified_fulltext(tmp_path)
    with mock.patch.object(
        fetch_phase._sources, "document_identity_probe",
        return_value={"decision": "inconclusive"},
    ):
        assert fetch_phase._emit_fetch_identity_tasks(
            {"run_dir": run_dir}, verify_phase._load_parse_payload(run_dir),
        ) == 1

    repo = RunRepository.open(run_dir)
    try:
        task = next(
            item for item in repo.list_tasks(slot="fetch")
            if item.task_kind == "source_identity_attestation"
        )
        assert task.task_payload["source_text_id"] == "source-1"
        assert task.task_payload["source_text_sha256"] == hashlib.sha256(
            b"A cited study\nSmith\nEvidence inspected by the operator."
        ).hexdigest()
        assert task.task_payload["target_sha256"]
    finally:
        repo.close()


def test_fetch_preflight_does_not_task_an_automatically_admitted_source(tmp_path):
    run_dir = _run_with_unverified_fulltext(
        tmp_path,
        resolve_status="resolved",
        identity_status="exact_identifier",
        match_signal="doi",
        match_score=1.0,
    )
    with mock.patch.object(
        fetch_phase._sources, "document_identity_probe",
        return_value={"decision": "inconclusive"},
    ):
        assert fetch_phase._emit_fetch_identity_tasks(
            {"run_dir": run_dir}, verify_phase._load_parse_payload(run_dir),
        ) == 0

    repo = RunRepository.open(run_dir)
    try:
        assert not [
            item for item in repo.list_tasks(slot="fetch")
            if item.task_kind == "source_identity_attestation"
        ]
    finally:
        repo.close()


def test_no_fetch_runs_and_applies_source_identity_preflight(tmp_path):
    run_dir = _run_with_unverified_fulltext(tmp_path)
    state = {"run_dir": run_dir, "accuracy": "standard", "no_fetch": True}
    common = (
        mock.patch.object(fetch_phase._sources, "reconcile_materialized_texts"),
        mock.patch.object(fetch_phase, "_ingest_user_source_directories"),
        mock.patch.object(fetch_phase, "_materialize_resolve_abstracts"),
        mock.patch.object(fetch_phase, "_print_phase_credential_warnings"),
        mock.patch.object(fetch_phase, "_pause", return_value=ACTION_REQUIRED),
        mock.patch.object(
            fetch_phase._sources,
            "document_identity_probe",
            return_value={"decision": "inconclusive"},
        ),
    )
    with common[0], common[1], common[2], common[3], common[4], common[5]:
        assert fetch_phase.phase_fetch(state) == ACTION_REQUIRED

    _answer_identity(run_dir, "attest_identity")
    with common[0], common[1], common[2], common[3], common[4], common[5]:
        assert fetch_phase.phase_fetch(state) == "gaps"

    repo = RunRepository.open(run_dir)
    try:
        task = next(
            item for item in repo.list_tasks(slot="fetch")
            if item.task_kind == "source_identity_attestation"
        )
        assert task.status == "applied"
    finally:
        repo.close()


def test_initial_fetch_checkpoint_includes_retrieval_and_identity_tasks():
    state = {"run_dir": "run", "accuracy": "standard"}
    parse = {
        "references": [{"id": "r1", "ref_number": 1, "title": "Source"}],
        "citations": [{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
    }
    gaps = {"need_fulltext": [{"ref_id": "r1"}]}
    with ExitStack() as stack:
        patches = [
            mock.patch.object(fetch_phase, "_load_parse_payload", return_value=parse),
            mock.patch.object(fetch_phase, "_resolve_map", return_value={"r1": {}}),
            mock.patch.object(fetch_phase, "_ingest_user_source_directories"),
            mock.patch.object(fetch_phase, "_materialize_resolve_abstracts"),
            mock.patch.object(fetch_phase, "_seed_reusable_sources", return_value={"fulltext": 0, "abstract": 0}),
            mock.patch.object(fetch_phase, "_include_table_only_need", side_effect=lambda need, *_args: need),
            mock.patch.object(fetch_phase, "_fetchable_need_items", side_effect=lambda need, *_args: need),
            mock.patch.object(fetch_phase, "_blocking_fetch_need_items", side_effect=lambda need, *_args, **_kwargs: need),
            mock.patch.object(fetch_phase, "_auto_fetch_fulltexts", return_value=[]),
            mock.patch.object(fetch_phase, "_resume_one_frozen_fetch_candidate"),
            mock.patch.object(fetch_phase, "_auto_run_source_ocr", return_value={}),
            mock.patch.object(fetch_phase, "_print_automatic_fetch_summary"),
            mock.patch.object(fetch_phase._fetch_modes, "selected_module", return_value=None),
            mock.patch.object(fetch_phase, "_write_manual_fetch_report"),
            mock.patch.object(fetch_phase, "_pending_fetch_identity_attestations", return_value=1),
            mock.patch.object(fetch_phase, "_save_state"),
            mock.patch.object(fetch_phase, "_print_phase_credential_warnings"),
            mock.patch.object(fetch_phase._sources, "reconcile_materialized_texts"),
            mock.patch("core.fetch.diagnostics.gaps.build_gap_report", return_value=gaps),
        ]
        create_task = stack.enter_context(mock.patch.object(fetch_phase, "_create_task"))
        emit_identity = stack.enter_context(
            mock.patch.object(fetch_phase, "_emit_fetch_identity_tasks", return_value=1)
        )
        pause = stack.enter_context(
            mock.patch.object(fetch_phase, "_pause", return_value=ACTION_REQUIRED)
        )
        for patch in patches:
            stack.enter_context(patch)
        assert fetch_phase.phase_fetch(state) == ACTION_REQUIRED

    create_task.assert_called_once()
    emit_identity.assert_called_once_with(state, parse)
    assert pause.call_args.args[2] == 2
    assert "retrieval" in pause.call_args.args[3].lower()
    assert "identity" in pause.call_args.args[3].lower()


def test_fetch_preflight_does_not_offer_hard_identity_conflict(tmp_path):
    run_dir = _run_with_unverified_fulltext(tmp_path)
    repo = RunRepository.open(run_dir)
    try:
        repo.upsert_resolve_result("r1", {
            "ref_id": "r1", "status": "unverified", "reason": "refuted",
            "matched_title": "A cited study", "reference_status_tag": "suspected_fabricated",
            "fabrication_risk": "high",
        })
    finally:
        repo.close()
    assert fetch_phase._emit_fetch_identity_tasks(
        {"run_dir": run_dir}, verify_phase._load_parse_payload(run_dir),
    ) == 0
    repo = RunRepository.open(run_dir)
    try:
        assert not [
            item for item in repo.list_tasks(slot="fetch")
            if item.task_kind == "source_identity_attestation"
        ]
    finally:
        repo.close()


def test_fetch_preflight_marks_unexpected_gate_error_without_excluding_source(tmp_path):
    run_dir = _run_with_unverified_fulltext(tmp_path)
    with mock.patch.object(
        fetch_phase,
        "bibliographic_identity_admitted",
        side_effect=RuntimeError("new identity shape"),
    ):
        assert fetch_phase._emit_fetch_identity_tasks(
            {"run_dir": run_dir}, verify_phase._load_parse_payload(run_dir),
        ) == 0

    repo = RunRepository.open(run_dir)
    try:
        attempts = repo.list_fetch_attempts("r1")
        assert attempts[-1]["outcome"] == "identity_anomaly"
        assert "fetch:identity_admission:RuntimeError" in attempts[-1]["reason"]
        assert not [
            item for item in repo.list_tasks(slot="fetch")
            if item.task_kind == "source_identity_attestation"
        ]
    finally:
        repo.close()


def test_fetch_preflight_marks_unreadable_source_and_continues(tmp_path):
    run_dir = _run_with_unverified_fulltext(tmp_path)
    source_path = (
        Path(run_dir) / "sources" / "parsed" / "1_fulltext_manual.txt"
    )
    source_path.write_text("A different document.", encoding="utf-8")

    assert fetch_phase._emit_fetch_identity_tasks(
        {"run_dir": run_dir}, verify_phase._load_parse_payload(run_dir),
    ) == 0

    repo = RunRepository.open(run_dir)
    try:
        attempts = repo.list_fetch_attempts("r1")
        assert attempts[-1]["outcome"] == "identity_anomaly"
        assert "fetch:source_context:RuntimeError" in attempts[-1]["reason"]
        assert not repo.list_tasks(slot="fetch")
    finally:
        repo.close()


def test_fetch_identity_reader_normalizes_malformed_persisted_path(tmp_path):
    repository = mock.Mock()
    repository.get_source_text.return_value = SimpleNamespace(
        sha256="a" * 64,
        stored_path=None,
        char_count=1,
    )

    with pytest.raises(RuntimeError, match="not run-local"):
        fetch_phase._read_fetch_identity_source(
            repository, str(tmp_path), "source-1", "a" * 64,
        )


def test_fetch_preflight_rejects_existing_identity_task_for_another_target(tmp_path):
    run_dir = _run_with_unverified_fulltext(
        tmp_path, include_admitted_sibling=True,
    )
    repo = RunRepository.open(run_dir)
    try:
        repo.create_source_identity_attestation(
            task_id=fetch_phase._source_identity_attestation_task_id("source-1"),
            ref_id="r2",
            source_text_id="source-2",
            slot="fetch",
            instructions="Review the sibling source.",
        )
    finally:
        repo.close()

    with pytest.raises(SystemExit, match="differs from the frozen source"):
        fetch_phase._emit_fetch_identity_tasks(
            {"run_dir": run_dir}, verify_phase._load_parse_payload(run_dir),
        )


def test_verify_reuses_fetch_identity_task_and_applies_its_operator_answer(tmp_path):
    run_dir = _run_with_unverified_fulltext(tmp_path)
    with mock.patch.object(
        fetch_phase._sources, "document_identity_probe",
        return_value={"decision": "inconclusive"},
    ):
        assert fetch_phase._emit_fetch_identity_tasks(
            {"run_dir": run_dir}, verify_phase._load_parse_payload(run_dir),
        ) == 1
    task_id = _answer_identity(run_dir, "attest_identity")
    with _phase_context():
        verify_phase._apply_answered_source_identity_attestations(run_dir)
        info = verify_phase._emit_verify_tasks({"run_dir": run_dir})

    repo = RunRepository.open(run_dir)
    try:
        identity_tasks = [
            item for item in repo.list_tasks()
            if item.task_kind == "source_identity_attestation"
        ]
        assert len(identity_tasks) == 1
        assert identity_tasks[0].task_id == task_id
        assert identity_tasks[0].status == "applied"
        assert repo.source_identity_attestation_for("source-1")["action"] == "attest_identity"
    finally:
        repo.close()
    assert info["identity_created"] == 0


def test_verify_marks_gate_anomaly_then_continues_to_operator_review(tmp_path):
    run_dir = _run_with_unverified_fulltext(tmp_path)
    with _phase_context(), mock.patch.object(
        verify_phase,
        "bibliographic_identity_admitted",
        side_effect=RuntimeError("new identity shape"),
    ):
        info = verify_phase._emit_verify_tasks({"run_dir": run_dir})

    repo = RunRepository.open(run_dir)
    try:
        attempts = repo.list_fetch_attempts("r1")
        identity_tasks = [
            item for item in repo.list_tasks()
            if item.task_kind == "source_identity_attestation"
        ]
    finally:
        repo.close()
    assert attempts[-1]["outcome"] == "identity_anomaly"
    assert "verify:identity_admission:RuntimeError" in attempts[-1]["reason"]
    assert len(identity_tasks) == 1
    assert identity_tasks[0].status == "pending"
    assert info["identity_created"] == 1


def test_verify_excludes_only_pair_when_registered_source_is_unreadable(tmp_path):
    run_dir = _run_with_unverified_fulltext(
        tmp_path, include_admitted_sibling=True,
    )
    source_path = (
        Path(run_dir) / "sources" / "parsed" / "1_fulltext_manual.txt"
    )
    source_path.write_text("A different document.", encoding="utf-8")

    with _phase_context():
        info = verify_phase._emit_verify_tasks({"run_dir": run_dir})

    repo = RunRepository.open(run_dir)
    try:
        pair = repo.get_verification_pair_state(
            claim_id="c1", ref_id="r1", scope="fulltext_complete",
        )
        sibling_tasks = [
            task for task in repo.list_tasks()
            if task.ref_id == "r2"
        ]
        attempts = repo.list_fetch_attempts("r1")
    finally:
        repo.close()
    assert pair["status"] == "uncertain"
    assert pair["terminal_cause"] == "source_integrity_unavailable"
    assert len(sibling_tasks) == 1
    assert sibling_tasks[0].status == "pending"
    assert attempts[-1]["outcome"] == "identity_anomaly"
    assert info["identity_created"] == 1


def test_fetch_identity_answer_fails_closed_if_source_bytes_change(tmp_path):
    run_dir = _run_with_unverified_fulltext(tmp_path)
    with mock.patch.object(
        fetch_phase._sources, "document_identity_probe",
        return_value={"decision": "inconclusive"},
    ):
        assert fetch_phase._emit_fetch_identity_tasks(
            {"run_dir": run_dir}, verify_phase._load_parse_payload(run_dir),
        ) == 1
    _answer_identity(run_dir, "attest_identity")
    source_path = (
        Path(run_dir) / "sources" / "parsed" / "1_fulltext_manual.txt"
    )
    source_path.write_text("A different document.", encoding="utf-8")

    with pytest.raises(SystemExit, match="attestation application failed"):
        verify_phase._apply_answered_source_identity_attestations(run_dir)


@contextmanager
def _phase_context():
    with (
        mock.patch.object(verify_phase, "_materialize_resolve_abstracts"),
        mock.patch.object(
            verify_phase, "_selected_contract",
            return_value=verify_phase.ClaimEvidenceRuntime.contract_id,
        ),
        mock.patch.object(verify_phase, "_pause", return_value=ACTION_REQUIRED),
        mock.patch.object(
            verify_phase._sources,
            "document_identity_probe",
            return_value={"decision": "inconclusive"},
        ),
    ):
        yield


@pytest.mark.parametrize(
    ("action", "claim_task_expected", "terminal_expected"),
    [
        ("attest_identity", True, False),
        ("keep_unverified", False, True),
    ],
)
def test_phase_pauses_then_applies_operator_identity_decision(
    tmp_path, action, claim_task_expected, terminal_expected,
):
    run_dir = _run_with_unverified_fulltext(tmp_path)
    with _phase_context(), mock.patch.object(
        verify_phase, "_execute_claim_evidence_tasks", return_value=0,
    ) as execute:
        assert verify_phase.phase_verify({"run_dir": run_dir}) == ACTION_REQUIRED
        pause_instructions = verify_phase._pause.call_args.args[3]
        assert "tasks skip-identity --run" in pause_instructions
        assert "keeping their sources unverified" in pause_instructions

        repo = RunRepository.open(run_dir)
        try:
            identity_tasks = [
                task for task in repo.list_tasks(slot="verify")
                if task.task_kind == "source_identity_attestation"
            ]
            assert len(identity_tasks) == 1
            assert identity_tasks[0].status == "pending"
            assert not repo.verification_pair_state_payloads()
        finally:
            repo.close()

        identity_task_id = _answer_identity(run_dir, action)
        assert verify_phase.phase_verify({"run_dir": run_dir}) == "web_research"

    repo = RunRepository.open(run_dir)
    try:
        identity_task = repo.get_task(identity_task_id)
        decision = repo.source_identity_attestation_for("source-1")
        claim_task = repo.get_task("verify:c1:r1:fulltext_complete")
        pair = repo.get_verification_pair_state(
            claim_id="c1", ref_id="r1", scope="fulltext_complete",
        )
    finally:
        repo.close()
    assert identity_task.status == "applied"
    assert decision["action"] == action
    assert (claim_task is not None) is claim_task_expected
    assert (pair is not None and pair["status"] == "uncertain") is terminal_expected
    execute.assert_called_once()


def test_keep_unverified_remains_binding_when_updated_gate_would_admit(tmp_path):
    run_dir = _run_with_unverified_fulltext(tmp_path)
    with _phase_context(), mock.patch.object(
        verify_phase, "_execute_claim_evidence_tasks", return_value=0,
    ):
        assert verify_phase.phase_verify({"run_dir": run_dir}) == ACTION_REQUIRED

    with _phase_context(), mock.patch.object(
        verify_phase, "bibliographic_identity_admitted", return_value=True,
    ), mock.patch.object(
        verify_phase, "_execute_claim_evidence_tasks", return_value=0,
    ):
        assert verify_phase.phase_verify({"run_dir": run_dir}) == ACTION_REQUIRED

    repo = RunRepository.open(run_dir)
    try:
        claim_task = repo.get_task("verify:c1:r1:fulltext_complete")
        assert claim_task is not None and claim_task.status == "pending"
    finally:
        repo.close()

    identity_task_id = _answer_identity(run_dir, "keep_unverified")
    with _phase_context(), mock.patch.object(
        verify_phase, "bibliographic_identity_admitted", return_value=True,
    ), mock.patch.object(
        verify_phase, "_execute_claim_evidence_tasks", return_value=0,
    ):
        assert verify_phase.phase_verify({"run_dir": run_dir}) == "web_research"

    repo = RunRepository.open(run_dir)
    try:
        assert repo.get_task(identity_task_id).status == "applied"
        assert repo.get_task("verify:c1:r1:fulltext_complete").status == "applied"
        assert repo.list_verification_lifecycle_violations() == []
        pair = repo.get_verification_pair_state(
            claim_id="c1", ref_id="r1", scope="fulltext_complete",
        )
    finally:
        repo.close()
    assert pair["status"] == "uncertain"
    assert pair["terminal_cause"] == "bibliographic_identity_not_corroborated"


def test_keep_unverified_terminalization_is_idempotent_on_verify_resume(tmp_path):
    run_dir = _run_with_unverified_fulltext(tmp_path)
    with _phase_context(), mock.patch.object(
        verify_phase, "_execute_claim_evidence_tasks", return_value=0,
    ):
        assert verify_phase.phase_verify({"run_dir": run_dir}) == ACTION_REQUIRED

    # A later admission rule may already have created the runner-owned task
    # before the operator chooses to keep this source unverified.
    with _phase_context(), mock.patch.object(
        verify_phase, "bibliographic_identity_admitted", return_value=True,
    ), mock.patch.object(
        verify_phase, "_execute_claim_evidence_tasks", return_value=0,
    ):
        assert verify_phase.phase_verify({"run_dir": run_dir}) == ACTION_REQUIRED

    _answer_identity(run_dir, "keep_unverified")
    with _phase_context(), mock.patch.object(
        verify_phase, "_execute_claim_evidence_tasks", return_value=0,
    ):
        assert verify_phase.phase_verify({"run_dir": run_dir}) == "web_research"
    with _phase_context(), mock.patch.object(
        verify_phase, "bibliographic_identity_admitted", return_value=True,
    ), mock.patch.object(
        verify_phase, "_execute_claim_evidence_tasks", return_value=0,
    ):
        assert verify_phase.phase_verify({"run_dir": run_dir}) == "web_research"

    repo = RunRepository.open(run_dir)
    try:
        task = repo.get_task("verify:c1:r1:fulltext_complete")
        pair = repo.get_verification_pair_state(
            claim_id="c1", ref_id="r1", scope="fulltext_complete",
        )
    finally:
        repo.close()
    assert task.status == "applied"
    assert pair["status"] == "uncertain"
    assert pair["terminal_cause"] == "bibliographic_identity_not_corroborated"


def test_keep_unverified_rejects_an_applied_semantic_task():
    repository = mock.Mock()
    repository.get_task.return_value = SimpleNamespace(status="applied")
    repository.get_verification_pair_state.return_value = {
        "status": "accepted",
        "terminal_cause": None,
    }

    with pytest.raises(SystemExit, match="after semantic verification"):
        verify_phase._terminalize_identity_unverified(
            repository,
            {"id": "c1"},
            {"id": "r1"},
            "fulltext_complete",
        )


def test_hard_identity_failure_never_offers_attestation(tmp_path):
    run_dir = _run_with_unverified_fulltext(
        tmp_path, resolve_status="identifier_mismatch",
    )
    with _phase_context(), mock.patch.object(
        verify_phase, "_execute_claim_evidence_tasks", return_value=0,
    ):
        assert verify_phase.phase_verify({"run_dir": run_dir}) == "web_research"

    repo = RunRepository.open(run_dir)
    try:
        assert not [
            task for task in repo.list_tasks(slot="verify")
            if task.task_kind == "source_identity_attestation"
        ]
        pair = repo.get_verification_pair_state(
            claim_id="c1", ref_id="r1", scope="fulltext_complete",
        )
    finally:
        repo.close()
    assert pair["status"] == "uncertain"
    assert pair["terminal_cause"] == "bibliographic_identity_not_corroborated"


def test_attestation_task_collision_fails_closed():
    target = {
        "ref_id": "r1",
        "source_text_id": "source-1",
        "source_text_sha256": "a" * 64,
        "target_sha256": "b" * 64,
    }
    record = mock.Mock(
        slot="verify",
        ref_id="r1",
        claim_id=None,
        scope=None,
        task_payload={
            "kind": "source_identity_attestation",
            "ref_id": "r1",
            "source_text_id": "another-source",
            "source_text_sha256": "a" * 64,
            "target_sha256": "b" * 64,
        },
    )
    with pytest.raises(SystemExit, match="differs from the frozen source"):
        verify_phase._require_identity_attestation_task(record, target)


def test_cross_reference_attestation_uses_source_owner_not_citing_reference(
    tmp_path,
):
    source_text = "Unverified antecedent source."
    source_path = tmp_path / "sources" / "antecedent.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source_text.encode("utf-8"))
    antecedent = {"id": "r1", "title": "Antecedent"}
    citing = {"id": "r2", "title": "Id."}
    entry = {
        "source_text_id": "source-r1",
        "sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "identity_status": "unverified",
        "match_signal": "title",
        "match_score": 0.5,
        "_identity_reference": antecedent,
        "_resolve_result": {"status": "unverified"},
    }
    target = {
        "ref_id": "r1",
        "reference": antecedent,
        "source_text_id": "source-r1",
        "source_text_sha256": entry["sha256"],
        "source_identity": {
            "identity_status": "unverified",
            "match_signal": "title",
            "match_score": 0.5,
        },
        "resolve_identity": {"status": "unverified"},
        "target_sha256": "c" * 64,
    }
    repository = mock.Mock()
    repository.list_tasks.return_value = []
    repository.get_task.return_value = None
    repository.get_source_text.return_value = mock.Mock(
        sha256=entry["sha256"],
        stored_path="sources/antecedent.txt",
        char_count=len(source_text),
    )
    repository.source_identity_attestation_target.return_value = target
    repository.source_identity_attestation_for.return_value = None
    with (
        mock.patch.object(verify_phase, "_repo_open", return_value=repository),
        mock.patch.object(
            verify_phase, "_verify_pairs",
            return_value=[(
                {"id": "c1"}, citing, "fulltext_complete",
                str(source_path), entry,
            )],
        ),
        mock.patch.object(
            verify_phase, "_selected_contract", return_value="contract",
        ),
        mock.patch.object(
            verify_phase._sources,
            "document_identity_probe",
            return_value={"decision": "inconclusive"},
        ),
    ):
        info = verify_phase._emit_verify_tasks({"run_dir": str(tmp_path)})

    assert info["identity_created"] == 1
    repository.create_source_identity_attestation.assert_called_once_with(
        task_id=verify_phase._source_identity_attestation_task_id("source-r1"),
        ref_id="r1",
        source_text_id="source-r1",
        instructions=mock.ANY,
    )
    repository.claim_verification_pair_terminal.assert_not_called()
