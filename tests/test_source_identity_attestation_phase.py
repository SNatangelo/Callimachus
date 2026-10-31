# tests/test_source_identity_attestation_phase.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fetch/Verify lifecycle for manual source-identity decisions."""

from __future__ import annotations

import hashlib
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from core.app.guided_fetch import GuidedFetchController, source_payload
from core.app.commands import tasks as task_commands
from core.app.phases import verify as verify_phase
from core.app.phases import fetch as fetch_phase
from core.app.runtime.settings import ACTION_REQUIRED
from core.infra.db.repository import RunRepository
from core.infra.db.task_storage import _FETCH_REFERENCE_FIELDS
from core.parse.parsing_common import _make_reference


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


































































