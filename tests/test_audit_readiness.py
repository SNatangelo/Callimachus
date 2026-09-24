# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed report readiness checks selected for deployment."""

from core.infra.db import ExecutionAssuranceRecord, RunRepository
from core.report.render import _local_report_integrity


def _repo(tmp_path, name, assurance=None):
    return RunRepository.create(
        str(tmp_path / name), run_id=name, input_path="paper.pdf", input_sha256="abc",
        accuracy="standard", style=None, model_id=None, http_profile=None,
        challenge_mode=None, fixture_fingerprint="fixture", execution_assurance=assurance,
    )


def test_local_report_projection_never_marks_standalone_clean_as_audit_ready(tmp_path):
    repo = _repo(tmp_path, "run"); repo.close()
    integrity = _local_report_integrity(str(tmp_path / "run"))
    assert integrity["audit_ready"] is False
    assert integrity["run"]["state"] == "unverifiable"


def test_attested_assurance_without_checkpoint_is_not_audit_ready(tmp_path):
    repo = _repo(
        tmp_path,
        "run",
        ExecutionAssuranceRecord("agent", "agent_attested", "codex"),
    )
    repo.close()

    integrity = _local_report_integrity(str(tmp_path / "run"))

    assert integrity["audit_ready"] is False
    assert integrity["run"]["state"] == "unverifiable"
