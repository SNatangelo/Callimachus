# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.app.parse_review import ParseReviewController

from core.app.phases import parse as parse_phase

from core.infra.db.repository import RunRepository

from core.infra.db.run_setting_storage import normalize_setting

def _repo(tmp_path):
    repo = RunRepository.create(
        str(tmp_path), run_id="review", input_path="input.md", input_sha256="x",
        accuracy="standard", style=None, model_id=None, http_profile=None,
        challenge_mode=None, fixture_fingerprint="f",
    )
    repo.replace_parse_payload(
        claims=[{"claim_id": "claim-1", "sentence": "Claim", "marker_numbers": [1]}],
        references=[{"ref_id": "ref-1", "ref_number": 1, "raw_entry": "Alpha (2020)"}],
        citations=[{"claim_id": "claim-1", "ref_id": "ref-1", "ref_number": 1}],
        footnote_notes=[{
            "note_id": "note-1", "manuscript_id": "m", "note_number": 1,
            "raw_note": "Alpha (2020)", "extraction_status": "sources_extracted",
        }],
        footnote_note_sources=[{
            "note_id": "note-1", "ref_id": "ref-1", "source_order": 0,
            "raw_start": 0, "raw_end": len("Alpha (2020)"),
        }],
        claim_footnotes=[{"claim_id": "claim-1", "note_id": "note-1"}],
        footnote_note_parents=[{"note_id": "note-1", "ref_id": "ref-1"}],
    )
    return repo

def test_skipped_manual_parse_reviews_resume_without_effective_overlay(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    st = {
        "run_dir": str(tmp_path), "phase": "parse", "manual_review": True,
        "parse_review_paused": True, "manual_review_ref_numbers": [1],
    }
    try:
        assert parse_phase._emit_manual_parse_review_tasks(st, repo, {
            "footnote_notes": [{"note_id": "note-1", "extraction_status": "ambiguous"}],
        }) == 2
        effective_before = repo.effective_parse_payload()
    finally:
        repo.close()

    def local_resolution(run_dir, *_args, **_kwargs):
        repo = RunRepository.open_readonly(run_dir)
        try:
            assurance = repo.get_execution_assurance()
        finally:
            repo.close()
        return SimpleNamespace(gate=None, assurance=assurance)

    monkeypatch.setattr("core.app.parse_review.resolve_existing", local_resolution)
    answers = ParseReviewController(str(tmp_path)).skip_remaining_reviews()
    assert len(answers) == 2
    assert len({answer["answer_id"] for answer in answers}) == 2

    def fail_if_reparsed(*_args, **_kwargs):
        raise AssertionError("skipped Parse review must not rerun parser")

    monkeypatch.setattr(parse_phase.parse_manuscript, "parse", fail_if_reparsed)
    saved = []
    result = parse_phase.phase_parse(
        st, _progress=lambda _message: None, _configure_debug_mode=lambda _st: None,
        _repo_open=RunRepository.open, _save_state=lambda state: saved.append(dict(state)),
    )
    assert result == "resolve"
    assert st["parse_review_paused"] is False
    assert saved

    repo = RunRepository.open(str(tmp_path))
    try:
        tasks = repo.list_tasks(slot="parse_review")
        assert len(tasks) == 2
        assert {task.status for task in tasks} == {"applied"}
        assert {
            repo.get_latest_task_answer(task.task_id).raw_payload["action"]
            for task in tasks
        } == {"skip_review"}
        assert repo.effective_parse_payload() == effective_before
        assert repo.effective_footnote_sources("note-1") == [{
            "ref_id": "ref-1", "source_order": 0,
            "raw_start": 0, "raw_end": len("Alpha (2020)"),
        }]
        assert repo.effective_reference_identity("ref-1")["title"] is None
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM manual_footnote_source_overrides"
        ).fetchone()[0] == 0
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM manual_reference_identity_overrides"
        ).fetchone()[0] == 0
    finally:
        repo.close()

def test_manual_parse_review_without_tasks_continues_to_resolve(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    repo.close()
    parse_payload = {
        "manuscript": {"format": "txt", "full_text": "Claim [1]."},
        "claims": [{"claim_id": "claim-1", "sentence": "Claim [1].", "marker_numbers": [1]}],
        "references": [{"ref_id": "ref-1", "ref_number": 1, "raw_entry": "Alpha (2020)"}],
        "citations": [{"claim_id": "claim-1", "ref_id": "ref-1", "ref_number": 1}],
        "_debug": {"reference_coverage": {
            "cited": 1, "total": 1, "pct": 100.0, "in_prose": 1,
            "in_tables": 0, "uncited": [], "warning": None, "fatal": False,
        }},
    }
    monkeypatch.setattr(parse_phase.parse_manuscript, "parse", lambda *_args, **_kwargs: (parse_payload, ""))
    st = {
        "run_dir": str(tmp_path), "input": "input.md", "phase": "parse",
        "manual_review": True, "style": "vancouver",
    }
    saved = []
    assert parse_phase.phase_parse(
        st, _progress=lambda _message: None, _configure_debug_mode=lambda _st: None,
        _repo_open=RunRepository.open, _save_state=lambda state: saved.append(dict(state)),
    ) == "resolve"
    assert not st.get("parse_review_paused")
    repo = RunRepository.open(str(tmp_path))
    try:
        assert repo.list_tasks(slot="parse_review") == []
    finally:
        repo.close()
