from __future__ import annotations

from core.app.desktop import (
    _accepted_jury1_outcomes,
    discover_run_summaries,
    load_cache_inventory,
    load_run_snapshot,
)
from core.fetch.storage import content_store
from core.infra.db.repository import RunRepository
from core.parse.parsing_common import _make_reference


def _repo(path, name="run"):
    return RunRepository.create(
        str(path), run_id=name, input_path="/papers/example.pdf", input_sha256="x",
        accuracy="standard", style=None, model_id=None, http_profile=None,
        challenge_mode=None, fixture_fingerprint="fixture",
    )


def test_run_snapshot_and_history_are_read_only_and_isolate_corrupt_runs(tmp_path):
    root = tmp_path / "runs"
    good = root / "good"
    repo = _repo(good, "good")
    try:
        ref = _make_reference(1, "Doe. Example paper. Journal (2020).")
        ref["id"] = "r1"
        repo.replace_parse_payload(claims=[], references=[ref], citations=[])
    finally:
        repo.close()
    bad = root / "bad"
    bad.mkdir()
    (bad / "run.sqlite").write_text("not sqlite", encoding="utf-8")

    snapshot = load_run_snapshot(str(good))
    summaries = discover_run_summaries(str(root))

    assert snapshot["run_id"] == "good"
    assert snapshot["input"] == {"label": "example.pdf", "path": "/papers/example.pdf"}
    assert snapshot["action_required"] is False
    assert snapshot["source_inventory"][0]["ref_id"] == "r1"
    good_summary = next(row for row in summaries if row.get("available"))
    assert good_summary["source_counts"] == {
        "resolved": 0, "fulltext": 0, "abstract": 0, "none": 1,
    }
    assert good_summary["verification_counts"] == {
        "total": 0, "open": 0, "outcomes": {},
    }
    assert any(not row["available"] and row["run_dir"] == str(bad.resolve()) for row in summaries)
    assert (good / "run.sqlite").is_file()


def test_cache_inventory_lists_only_reusable_parsed_texts(tmp_path, monkeypatch):
    state = tmp_path / "state"
    run = tmp_path / "runs" / "one"
    environ = {content_store.ENV_STATE_DIR: str(state)}
    monkeypatch.setenv(content_store.ENV_STATE_DIR, str(state))
    ref = {"doi": "10.1000/example", "title": "Example", "year": 2020, "ay_surname": "Doe"}
    content_store.record_parsed_text(
        str(run), ref, "abstract", "fixture", "abstract body",
    )
    inventory = load_cache_inventory(str(run), environ=environ)

    assert inventory["available"] is True
    assert inventory["totals"]["works"] == 1
    assert inventory["totals"]["reusable"] == 1
    assert inventory["items"][0]["availability"] == "abstract"
    assert inventory["items"][0]["contents"][0]["file_present"] is True
    assert "source_ref" not in inventory["items"][0]["contents"][0]


def test_jury1_outcome_is_projected_only_for_a_valid_jury2_acceptance():
    state = {
        "claim_id": "c1", "ref_id": "r1", "scope": "fulltext_complete",
        "status": "accepted", "terminal_outcome": "supports",
        "terminal_cause": "jury2_accepted", "winner_call_id": "winner",
    }
    candidate = {
        "candidate_id": "winner", "claim_id": "c1", "ref_id": "r1",
        "scope": "fulltext_complete", "outcome": "supports",
        "grounded": [{"text": "evidence"}], "evidence": ["evidence"],
    }
    event = {
        "candidate_id": "winner", "event_type": "terminal",
        "payload": {"resolution": "jury2_accepted", "assurance": "passed"},
    }
    raw = {"pair_states": [state], "candidates": [candidate], "candidate_events": [event]}

    assert _accepted_jury1_outcomes(raw) == {
        ("c1", "r1", "fulltext_complete"): "supports",
    }
    raw["pair_states"][0]["status"] = "open"
    assert _accepted_jury1_outcomes(raw) == {}
