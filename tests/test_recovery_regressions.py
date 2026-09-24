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
from tests._recovery_fixtures import _current_fetch_task


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


def test_native_not_found_answer_is_persisted_with_real_os_identity(tmp_path):
    run, repo, _refs = make_run(tmp_path)
    repo.create_task(
        task_id="fetch:r1", slot="fetch", ref_id="r1",
        task_payload=_current_fetch_task(repo),
    )
    assurance = repo.get_execution_assurance()
    repo.close()
    result = admission.admit_task_answer_locally(
        run_dir=run, task_id="fetch:r1", raw_payload={"found": False},
        assurance=assurance,
    )
    repo = RunRepository.open(run)
    try:
        assert repo.get_task("fetch:r1").status == "answered"
        assert repo.get_execution_assurance() == assurance
        assert repo.get_latest_task_answer("fetch:r1") is not None
        numeric_id = result["provenance"]["authenticated_uid"]
        assert type(numeric_id) is int and numeric_id >= 0
        assert result["provenance"]["authority_id"] == "local-unattested"
        identity = result["provenance"]["producer_identity"]
        if os.name == "nt":
            assert "@windows-sid:S-1-" in identity
            assert numeric_id == int(identity.rsplit("-", 1)[1])
        else:
            assert numeric_id == os.getuid()
            assert identity == "local-operator-unattested"
    finally:
        repo.close()


def test_attested_run_still_rejects_local_admission(tmp_path):
    assurance = ExecutionAssuranceRecord(
        initial_origin="agent", protection="agent_attested", agent_identity="test-agent",
    )
    run, repo, _refs = make_run(tmp_path, assurance=assurance)
    repo.close()
    with mock.patch.object(admission, "local_process_identity") as identity:
        with pytest.raises(AuthorityError, match="require authority admission"):
            admission.admit_task_answer_locally(
                run_dir=run, task_id="fetch:r1", raw_payload={"found": False},
                assurance=assurance, agent_identity="test-agent",
            )
        identity.assert_not_called()


def test_windows_identity_uses_complete_sid_and_rid(monkeypatch):
    monkeypatch.setattr(local_identity, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(local_identity, "_windows_process_sid", lambda: "S-1-5-21-11-22-33-1001")
    assert local_identity.local_process_identity() == (
        1001, "windows-sid:S-1-5-21-11-22-33-1001",
    )


@pytest.mark.parametrize("sid", [None, "test-operator", "S-1-5-21-4294967296", "S-1-5-21-1\n"])
def test_windows_identity_rejects_invalid_sid(monkeypatch, sid):
    monkeypatch.setattr(local_identity, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(local_identity, "_windows_process_sid", lambda: sid)
    with pytest.raises(AuthorityError, match="SID is invalid"):
        local_identity.local_process_identity()


def test_windows_identity_failure_does_not_invent_a_uid(monkeypatch):
    monkeypatch.setattr(local_identity, "os", SimpleNamespace(name="nt"))
    with mock.patch.object(local_identity, "_windows_process_sid", side_effect=OSError("denied")):
        with pytest.raises(AuthorityError, match="cannot determine Windows"):
            local_identity.local_process_identity()


@pytest.mark.parametrize("text", ["Heading\nAbstract\nEvidence.", "à Ω\nline two\n", "first\r\nsecond\n"])
def test_source_bytes_match_ledger_on_windows_and_posix(tmp_path, monkeypatch, text):
    run, repo, refs = make_run(tmp_path)
    repo.close()
    # Emulate Windows text-mode translation even on Linux; binary mode must
    # bypass it. This catches the bug without relaxing Verify's byte hashing.
    def windows_open(file, mode="r", *args, **kwargs):
        if "w" in mode and "b" not in mode and "newline" not in kwargs:
            kwargs["newline"] = "\r\n"
        return builtins.open(file, mode, *args, **kwargs)

    monkeypatch.setattr(sources, "open", windows_open, raising=False)
    entry = sources.store_text(run, refs[0], "abstract", "user", text)
    data = (Path(run) / "sources" / entry["stored_as"]).read_bytes()
    assert data == text.encode("utf-8")
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    repo = RunRepository.open(run)
    try:
        record = repo.list_source_texts()[0]
        assert verify_phase._claim_evidence_source_text(repo, run, {
            "source_text_id": record.source_text_id,
            "source_text_sha256": record.sha256,
        }) == text
    finally:
        repo.close()


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_registered_source_failure_is_not_silently_terminalized(tmp_path, damage):
    run, repo, refs = make_run(tmp_path)
    repo.close()
    entry = sources.store_text(run, refs[0], "abstract", "user", "An actual source abstract.")
    state = {"run_dir": run, "accuracy": "standard"}
    verify_phase._emit_verify_tasks(state)
    path = Path(run) / "sources" / entry["stored_as"]
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"different evidence")
    with pytest.raises((SystemExit, RuntimeError), match="source.*(unavailable|mismatch)"):
        verify_phase._emit_verify_tasks(state)
    repo = RunRepository.open(run)
    try:
        # Task readers validate source bytes, so inspect only state after damage.
        rows = repo._conn.execute("SELECT status FROM tasks WHERE slot='verify'").fetchall()
        assert [row["status"] for row in rows] == ["pending"]
        assert repo.verification_pair_state_payloads() == []
    finally:
        repo.close()


@pytest.mark.parametrize("html", ["0", "1"])
def test_no_source_report_reaches_done_without_a_jury(tmp_path, monkeypatch, html):
    run, repo, _refs = make_run(tmp_path, count=4)
    statuses = ["resolved", "not_found", "unresolved", "resolved"]
    for i, status in enumerate(statuses, 1):
        repo.upsert_resolve_result(f"r{i}", {
            "ref_id": f"r{i}", "status": status, "via": "crossref",
            "retracted": i == 4,
        })
    repo.close()
    monkeypatch.setenv("CITATION_VERIFIER_REPORT_HTML", html)
    state = {"run_dir": run, "phase": "verify", "accuracy": "standard"}
    with mock.patch.object(verify_phase.ClaimEvidenceRuntime, "for_run") as jury:
        assert verify_phase.phase_verify(state) == "web_research"
        assert web_research.phase_web_research(state) == "report"
        assert report_gate.phase_report(state) == "done"
        jury.assert_not_called()
    result = verify_run.verify(run)
    assert result["ok"], result["failures"]
    assert result["info"]["pairs_no_text"] == 4
    assert result["info"]["pairs_with_text_verified"] == 0
    assert result["info"]["quality"]["semantic_decision_complete"] is False
    assert result["info"]["quality"]["run_reliable"] is False
    md = (Path(run) / "report.md").read_text(encoding="utf-8")
    assert md.count("**NOT ASSESSABLE**") == 4
    assert "NOT found (possible fabrication)" in md
    assert "unresolved (transient)" in md
    assert "RETRACTED" in md
    assert not verify_run.verify(run, strict_crediting=True)["ok"]
    repo = RunRepository.open(run)
    try:
        assert repo.list_tasks(slot="verify") == []
        assert repo.verification_raw_payloads()["candidates"] == []
        assert repo.resolve_payload_map()["r4"]["retracted"] is True
    finally:
        repo.close()


def test_present_but_unverified_source_cannot_be_reported_as_missing(tmp_path):
    run, repo, refs = make_run(tmp_path)
    repo.close()
    sources.store_text(run, refs[0], "abstract", "user", "Available abstract.")
    write_report(run, integrity=None)
    md = (Path(run) / "report.md").read_text(encoding="utf-8")
    assert "**INCOMPLETE**" in md
    assert "**NOT ASSESSABLE**" not in md
    result = verify_run.verify(run)
    assert not result["ok"]
    assert result["info"]["pairs_skipped"] == 1


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_unavailable_registered_source_is_not_treated_as_usable_text(tmp_path, damage):
    run, repo, refs = make_run(tmp_path)
    repo.close()
    entry = sources.store_text(run, refs[0], "abstract", "user", "Available abstract.")
    source_path = Path(run) / "sources" / entry["stored_as"]
    if damage == "missing":
        source_path.unlink()
    else:
        source_path.write_bytes(b"different evidence")

    write_report(run, integrity=None)
    markdown = (Path(run) / "report.md").read_text(encoding="utf-8")
    assert "**NOT ASSESSABLE**" in markdown
    assert "**INCOMPLETE**" not in markdown

    result = verify_run.verify(run)
    assert not result["ok"]
    assert result["info"]["pairs_no_text"] == 1
    assert result["info"]["pairs_skipped"] == 0
    assert any("persisted source integrity failure" in failure for failure in result["failures"])
