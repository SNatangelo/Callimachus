# tests/test_recovery_regressions.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Real-DB regressions for missing sources and native-platform task admission.

No live network requests, LLM credentials, or hand-authored semantic verdicts.
"""
from __future__ import annotations

import builtins
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from core.app.phases import report_gate, verify as verify_phase, web_research
from core.infra.db import ExecutionAssuranceRecord, RunRepository
from core.infra.integrity import AuthorityError, admission, local_identity
from core.report import write_report
from core.resolve import sources
from core.verify import verify_run
from tests.test_infra import _current_fetch_task


def make_run(tmp_path, *, count=1, assurance=None):
    run = str(tmp_path / "run")
    repo = RunRepository.create(
        run, run_id="recovery-test", input_path="manuscript.txt",
        input_sha256="a" * 64, accuracy="standard", style="vancouver",
        model_id=None, http_profile="plain", challenge_mode="off",
        fixture_fingerprint="recovery-test", execution_assurance=assurance,
    )
    refs = [{
        "id": f"r{i}", "ref_number": i,
        "raw_entry": f"Author. Reference {i}. 2020.",
        "title": f"Reference {i}", "source_type": "article",
    } for i in range(1, count + 1)]
    claims = [{
        "id": f"c{i}", "sentence": f"Claim number {i} [{i}].",
        "context_window": f"Claim number {i} [{i}].", "marker_raw": f"[{i}]",
    } for i in range(1, count + 1)]
    repo.replace_parse_payload(
        manuscript_text="Example manuscript.", claims=claims, references=refs,
        citations=[{
            "claim_id": f"c{i}", "ref_id": f"r{i}", "ref_number": i,
        } for i in range(1, count + 1)],
    )
    return run, repo, refs




















