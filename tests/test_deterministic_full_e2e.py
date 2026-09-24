#!/usr/bin/env python3
# tests/test_deterministic_full_e2e.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""A deterministic local exercise of the complete persisted pipeline."""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.app import run as driver
from core.infra.db import RunRepository
from core.report.human.sealing import verify_html_report
from core.report.io import load_run_projection
from core.verify import verify_run
from core.verify.claim_evidence import ClaimEvidenceRuntime
from core.verify.claim_evidence.config import CONTRACT_ID
from core.verify.claim_evidence.domain.jury1_flow import (
    ContraryGateDecision,
    ExplanationEvidenceDecision,
    SupportGateDecision,
    TopicGateDecision,
)
from core.verify.claim_evidence.domain.replies import (
    ContraryGateAnswer,
    ExplanationEvidenceAnswer,
    SupportGateAnswer,
    TopicGateAnswer,
)
from tests._resolve_fixtures import current_resolution


class _LocalRelatedTransport:
    """Deterministic Verify transport double; persistence remains production code."""

    def dispatch(self, request, _assignment):
        if request.stage == "support_gate":
            answer = SupportGateAnswer(
                request.logical_request_id, request.payload_fingerprint,
                SupportGateDecision(False),
            )
        elif request.stage == "contrary_gate":
            answer = ContraryGateAnswer(
                request.logical_request_id, request.payload_fingerprint,
                ContraryGateDecision(False),
            )
        elif request.stage == "topic_gate":
            answer = TopicGateAnswer(
                request.logical_request_id, request.payload_fingerprint,
                TopicGateDecision(True),
            )
        elif request.stage == "explanation_evidence":
            answer = ExplanationEvidenceAnswer(
                request.logical_request_id, request.payload_fingerprint,
                ExplanationEvidenceDecision(
                    "The source is related but supplies no usable support.",
                    None, None, None, (), None,
                ),
            )
        else:
            raise AssertionError(f"unexpected Verify stage: {request.stage}")
        return answer


def _canonical_projection(run_dir: str) -> dict:
    """Durable inputs and terminal facts, deliberately excluding timestamps."""
    projection = load_run_projection(run_dir)
    return {
        "parse": projection["parse_effective"],
        "resolve": projection["resolve_map"],
        "sources": [{
            key: entry.get(key)
            for key in (
                "ref_id", "tier", "origin", "source_ref", "mapping",
                "signal", "score", "sha256", "char_count", "stored_as",
            )
        } for entry in projection["manifest"]["entries"]],
        "verification": projection["verification_pair_states"],
    }


def test_complete_local_pipeline_persists_terminal_evidence_and_regenerates_report(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("CITATION_VERIFIER_STATE_DIR", str(tmp_path / "state"))
    run_dir = str(tmp_path / "run")
    input_path = tmp_path / "manuscript.txt"
    input_path.write_text(
        "A deterministic local claim is cited here [1].\n\n"
        "References\n"
        "[1] Smith J. Deterministic source. 2020.\n",
        encoding="utf-8",
    )
    repo = RunRepository.create(
        run_dir,
        run_id="deterministic-local-e2e-" + hashlib.sha256(
            str(tmp_path).encode("utf-8")
        ).hexdigest()[:16],
        input_path=str(input_path),
        input_sha256="a" * 64,
        accuracy="abstract",
        style="vancouver",
        model_id=None,
        http_profile="default",
        challenge_mode="off",
        fixture_fingerprint="deterministic-local-e2e",
    )
    session_id = repo.start_session(pid=1, host="test-host")
    repo.set_run_setting("verify_runtime", {
        "code_revision": "a" * 40,
        "backend": "local",
        "model": "deterministic-model",
        "reasoning": "off",
        "reasoning_effort": "medium",
        "context_profile": "large",
        "max_source_chars": "",
        "require_fulltext": False,
        "semantic_contract": CONTRACT_ID,
    })
    repo.close()

    def resolve_locally(reference):
        return current_resolution(reference, {
            "ref_id": reference["id"],
            "status": "resolved",
            "via": "crossref",
            "matched_title": reference["title"],
            "abstract": "The deterministic local source discusses the fixture claim.",
            "abstract_via": "openalex",
            "retracted": False,
            "fulltext_exists": False,
            "oa_status": "paywalled",
            "attempts": [{"via": "crossref", "status": "resolved"}],
        })

    real_for_run = ClaimEvidenceRuntime.for_run

    def local_for_run(cls, repository, environ, *, run_id, **_kwargs):
        return real_for_run(
            repository,
            environ,
            run_id=run_id,
            transport=_LocalRelatedTransport(),
            provider_metadata=(("local", "LOCAL_KEYS", "LOCAL_MODELS", False),),
        )

    monkeypatch.setattr(driver._resolve, "resolve", resolve_locally)
    monkeypatch.setattr(ClaimEvidenceRuntime, "for_run", classmethod(local_for_run))
    monkeypatch.setenv("CITATION_VERIFIER_VERIFY_BACKENDS", "local")
    monkeypatch.setenv("CITATION_VERIFIER_VERIFY_JURY2_LEVEL", "off")
    monkeypatch.setenv("CITATION_VERIFIER_VERIFY_SELECTION_SEED", "local-e2e")
    monkeypatch.setenv("LOCAL_KEYS", "deterministic-key")
    monkeypatch.setenv("LOCAL_MODELS", "deterministic-model")
    state = {
        "run_dir": run_dir,
        "phase": "parse",
        "input": str(input_path),
        "accuracy": "abstract",
        "style": "vancouver",
        "db_session_id": session_id,
    }

    assert driver.drive(state) == 0
    assert state["phase"] == "done"

    repo = RunRepository.open(run_dir)
    try:
        assert repo.get_run().status == "completed"
        resolve_map = repo.resolve_payload_map()
        assert len(resolve_map) == 1
        ref_id, resolved = next(iter(resolve_map.items()))
        assert resolved["via"] == "crossref"
        source = repo.list_source_texts(ref_id)[0]
        assert (source.tier, source.origin, source.source_ref) == (
            "abstract", "openalex", "resolve:openalex",
        )
        terminal = repo.verification_pair_state_payloads()
        assert len(terminal) == 1
        assert terminal[0]["status"] == "accepted"
        assert terminal[0]["terminal_outcome"] == "related"
    finally:
        repo.close()

    first = _canonical_projection(run_dir)
    report = Path(run_dir) / "report.md"
    html = Path(run_dir) / "report.html"
    first_report = report.read_bytes()
    first_html = html.read_bytes()
    assert verify_run.verify(run_dir)["ok"]
    assert verify_html_report(run_dir)["ok"]

    # Re-enter the real transactional Report phase and require identical sealed
    # outputs and the same canonical persisted facts.
    state["phase"] = "report"
    assert driver.drive(state) == 0
    assert report.read_bytes() == first_report
    assert html.read_bytes() == first_html
    assert _canonical_projection(run_dir) == first
