#!/usr/bin/env python3
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused regressions for fail-closed resume and integrity checkpoints."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from core.app import run as driver
from core.app.phases import resolve as resolve_phase
from core.app.phases import verify as verify_phase
from core.infra.db import RunRepository
from core.verify.claim_evidence import ClaimEvidenceRuntime


def _create_run(run_dir: Path, run_id: str) -> RunRepository:
    return RunRepository.create(
        str(run_dir),
        run_id=run_id,
        input_path="manuscript.txt",
        input_sha256="a" * 64,
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile="plain",
        challenge_mode="off",
        fixture_fingerprint="focused-recovery-fixture",
    )


def test_resume_diagnosis_rejects_orphan_terminal_without_mutating_run(tmp_path):
    run_dir = tmp_path / "orphan-terminal"
    repo = _create_run(run_dir, "orphan-terminal")
    try:
        repo.replace_parse_payload(
            manuscript_text="Example manuscript.",
            claims=[{
                "id": "c1",
                "sentence": "Claim [1].",
                "context_window": "Manuscript context.",
                "marker_raw": "[1]",
            }],
            references=[{
                "id": "r1",
                "ref_number": 1,
                "raw_entry": "Smith. A paper. 2020.",
            }],
            citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
        )

        source_text = "Claim evidence from this run-local UTF-8 source."
        source_relpath = "sources/current-claim-evidence.txt"
        source_path = run_dir / source_relpath
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(source_text, encoding="utf-8")
        source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        repo.store_source_text(
            source_text_id="current-claim-source",
            ref_id="r1",
            identity_key="current-fixture",
            tier="fulltext",
            origin="fixture",
            stored_path=source_relpath,
            sha256=source_sha256,
            char_count=len(source_text),
        )
        claim_payload, effective_context = ClaimEvidenceRuntime.prepare_task(
            {
                "sentence": "Claim [1].",
                "context_window": "Manuscript context.",
                "marker_raw": "[1]",
            },
            source_text,
        )
        repo.create_task(
            task_id="verify-c1-r1",
            slot="verify",
            ref_id="r1",
            claim_id="c1",
            scope="fulltext_complete",
            task_payload={
                "kind": "claim_evidence",
                "status": "pending",
                "answer": None,
                "semantic_contract": ClaimEvidenceRuntime.contract_id,
                "claim_id": "c1",
                "ref_id": "r1",
                "scope": "fulltext_complete",
                "claim_evidence_payload": claim_payload,
                "effective_context": effective_context,
                "source_text_id": "current-claim-source",
                "source_text_sha256": source_sha256,
            },
        )
        repo.ensure_verification_pair(
            claim_id="c1", ref_id="r1", scope="fulltext_complete",
        )
        repo.claim_verification_pair_terminal(
            claim_id="c1",
            ref_id="r1",
            scope="fulltext_complete",
            status="accepted",
            outcome="supports",
            cause="jury2_accepted",
            call_id="call-orphan",
        )
    finally:
        repo.close()

    with pytest.raises(RuntimeError, match="cannot be resumed safely"):
        driver._ensure_resume_verification_integrity(str(run_dir))

    repo = RunRepository.open_readonly(str(run_dir))
    try:
        assert repo.get_run().status == "active"
        assert repo.get_run_setting("verification_lifecycle_failure") is None
    finally:
        repo.close()


def test_discovery_wave_checkpoints_once_after_persisting_units(tmp_path):
    run_dir = tmp_path / "discovery-wave"
    repo = _create_run(run_dir, "discovery-wave")
    repo.close()
    states = [
        ({"id": "r1"}, {"result": {"status": "unverified"}, "attempts": []}),
        ({"id": "r2"}, {"result": {"status": "unverified"}, "attempts": []}),
    ]
    checkpoint = mock.Mock()
    checkpoint_order = []

    def record_checkpoint(group, handle):
        persisted = RunRepository.open_readonly(str(run_dir))
        try:
            rows = persisted.list_integrity_unit_completions(group)
        finally:
            persisted.close()
        assert [row["unit_id"] for row in rows] == ["r1", "r2"]
        checkpoint_order.append((group, handle))

    checkpoint.side_effect = record_checkpoint
    resolve_phase._persist_discovery_wave(
        {"_integrity_unit_checkpoint": checkpoint}, str(run_dir), states,
    )

    repo = RunRepository.open_readonly(str(run_dir))
    try:
        rows = repo.list_integrity_unit_completions("resolve_discovery")
    finally:
        repo.close()
    assert [row["unit_id"] for row in rows] == ["r1", "r2"]
    checkpoint.assert_called_once_with("resolve_discovery", "wave")
    assert checkpoint_order == [("resolve_discovery", "wave")]


def test_guarded_verify_is_serial_and_checkpoints_each_applied_pair(tmp_path):
    active = 0
    max_active = 0
    lock = threading.Lock()
    applied = []
    checkpoints = []

    class FakeRuntime:
        aggregate_in_flight = 2

        def execute(self, task, *, source_text):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03 if task["claim_id"] == "c1" else 0.08)
            with lock:
                active -= 1
            return {"status": "accepted"}

        def close(self):
            pass

    class FakeRepository:
        def __init__(self):
            self.settings = {
                "verify_runtime": {"backend": "dummy", "model": "dummy-model"},
                "verify_claim_evidence_config": {
                    "providers": [{
                        "name": "dummy",
                        "lanes": [{"model": "dummy-model"}],
                    }],
                },
            }

        def get_run(self):
            return SimpleNamespace(run_id="run-1")

        def get_task(self, handle):
            return SimpleNamespace(task_payload={"kind": "claim_evidence"})

        def get_run_setting(self, key):
            return self.settings.get(key)

        def set_run_setting(self, key, value):
            self.settings[key] = value

        def apply_task(self, handle, *, task_payload):
            with lock:
                assert active == 0
            applied.append((handle, task_payload["status"]))

        def close(self):
            pass

    repository = FakeRepository()
    runtime = FakeRuntime()
    pending = [
        (f"task-{index}", {
            "kind": "claim_evidence",
            "claim_id": f"c{index}",
            "source_text_id": f"source-{index}",
            "source_text_sha256": hashlib.sha256(b"source").hexdigest(),
        })
        for index in (1, 2)
    ]
    run_dir = str(tmp_path / "verify-run")

    def record_checkpoint(group, handle):
        assert group == "verify_pair"
        assert applied[-1] == (handle, "done")
        checkpoints.append(handle)

    with (
        mock.patch.object(verify_phase, "_pending_tasks", return_value=pending),
        mock.patch.object(verify_phase, "_repo_open", return_value=repository),
        mock.patch.object(
            verify_phase.ClaimEvidenceRuntime, "for_run", return_value=runtime,
        ),
        mock.patch.object(
            verify_phase, "_claim_evidence_source_text", return_value="source",
        ),
    ):
        state = {"run_dir": run_dir, "_integrity_unit_checkpoint": record_checkpoint}
        assert verify_phase._execute_claim_evidence_tasks(state) == 2

    assert max_active == 1
    assert applied == [("task-1", "done"), ("task-2", "done")]
    assert checkpoints == ["task-1", "task-2"]
