from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from core.app.desktop import (
    _accepted_jury1_outcomes,
    _progress_counts,
    _phase_progress,
    discover_run_briefs,
    discover_run_summaries,
    load_cache_inventory,
    load_latest_run_brief,
    load_run_brief,
    load_run_overview,
    load_run_snapshot,
    load_run_summary,
    load_source_page,
    load_source_detail,
)
import core.app.desktop as desktop_projection
from core.fetch.storage import content_store
from core.infra.db.repository import RunRepository
from core.parse.parsing_common import _make_reference
from core.verify.claim_evidence.contracts.jury1_flow import (
    JURY1_PROMPT_SPEC,
    JURY1_SYSTEM_PROMPT,
)
from core.verify.claim_evidence.contracts.jury2 import JURY2_SYSTEM_PROMPT
from core.verify.claim_evidence.domain.fingerprint import (
    FINGERPRINT_VERSION_V2,
    candidate_fingerprint,
    payload_fingerprint,
    provider_prompt_fingerprint,
)


def _repo(path, name="run"):
    return RunRepository.create(
        str(path), run_id=name, input_path="/papers/example.pdf", input_sha256="x",
        accuracy="standard", style=None, model_id=None, http_profile=None,
        challenge_mode=None, fixture_fingerprint="fixture",
    )


def _ledger_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _append_accepted_pair(repo, *, ref_id: str, claim_id: str) -> None:
    scope = "fulltext_complete"
    candidate_id = f"candidate-{ref_id}"
    source_hash = _ledger_hash(f"source-{ref_id}")
    context_hash = _ledger_hash(f"context-{ref_id}")
    retrieval_hash = _ledger_hash(f"retrieval-{ref_id}")
    model_hash = _ledger_hash(f"model-{ref_id}")
    policy_hash = _ledger_hash(f"policy-{ref_id}")
    evidence_text = "Quoted source evidence."

    origin_payload = {
        "source_spans": [{"span_id": f"span-{ref_id}", "text": evidence_text}],
        "source_hash": source_hash,
        "cited_source_mode": "full_text",
        "claim": f"Claim {ref_id}",
        "claim_context": "Manuscript context.",
        "citation_marker": "[1]",
        "task": {
            "task_id": "support_gate",
            "instructions": JURY1_PROMPT_SPEC.task_prompt("support_gate"),
        },
    }
    origin = {
        "logical_request_id": f"request-{ref_id}",
        "claim_id": claim_id,
        "ref_id": ref_id,
        "scope": scope,
        "candidate_id": None,
        "candidate_cycle": 1,
        "stage": "support_gate",
        "payload": origin_payload,
        "payload_hash": payload_fingerprint(
            origin_payload, version=FINGERPRINT_VERSION_V2,
        ),
        "source_hash": source_hash,
        "context_hash": context_hash,
        "retrieval_hash": retrieval_hash,
        "model_hash": model_hash,
        "policy_hash": policy_hash,
    }
    origin["prompt_hash"] = provider_prompt_fingerprint(
        JURY1_SYSTEM_PROMPT,
        json.dumps(origin_payload, ensure_ascii=False, separators=(",", ":")).encode(),
    )

    record = {
        "outcome": "supports",
        "explanation": "The source states the claim.",
        "evidence": [evidence_text],
        "outcome_fields": {
            "claim_hash": _ledger_hash(f"claim-{ref_id}"),
            "source_hash": source_hash,
            "supported_part": f"Claim {ref_id}",
            "incompatible_proposition": None,
            "reason": None,
            "provider_confidence": None,
        },
        "grounded": [{
            "text": evidence_text,
            "span_id": f"span-{ref_id}",
            "raw_start": 0,
            "raw_end": len(evidence_text),
            "source_hash": source_hash,
            "match_mode": "exact_raw",
            "score": 1.0,
        }],
        "source_hash": source_hash,
        "context_hash": context_hash,
        "retrieval_hash": retrieval_hash,
        "prompt_hash": origin["prompt_hash"],
        "model_hash": model_hash,
        "policy_hash": policy_hash,
    }
    record["fingerprint"] = candidate_fingerprint(
        outcome=record["outcome"],
        evidence=tuple(record["evidence"]),
        outcome_fields=record["outcome_fields"],
        grounded=tuple({
            name: item[name]
            for name in ("span_id", "raw_start", "raw_end", "text", "source_hash")
        } for item in record["grounded"]),
        version=FINGERPRINT_VERSION_V2,
    )

    repo.ensure_verification_pair(claim_id=claim_id, ref_id=ref_id, scope=scope)
    adapter = repo.claim_evidence_adapter()
    adapter.append_logical_request(origin)
    adapter.append_candidate(
        candidate_id=candidate_id,
        claim_id=claim_id,
        ref_id=ref_id,
        scope=scope,
        candidate_cycle=1,
        origin_logical_request_id=origin["logical_request_id"],
        record=record,
    )
    jury2_payload = {
        "asserted_relation": "basis",
        "passage_subject": "the source-backed claim",
        "selected_passages": [{"span_id": f"span-{ref_id}", "text": evidence_text}],
    }
    jury2_request = {
        **origin,
        "logical_request_id": f"jury2-{ref_id}",
        "candidate_id": candidate_id,
        "stage": "jury2",
        "payload": jury2_payload,
        "payload_hash": payload_fingerprint(
            jury2_payload, version=FINGERPRINT_VERSION_V2,
        ),
    }
    jury2_request["prompt_hash"] = provider_prompt_fingerprint(
        JURY2_SYSTEM_PROMPT,
        json.dumps(jury2_payload, ensure_ascii=False, separators=(",", ":")).encode(),
    )
    adapter.append_logical_request(jury2_request)
    adapter.append_candidate_event(
        event_id=f"{candidate_id}:guard",
        candidate_id=candidate_id,
        event_type="guard_accepted",
        payload={"guard_result": {"accepted": True}, "cause": None},
    )
    adapter.append_candidate_event(
        event_id=f"{candidate_id}:jury2",
        candidate_id=candidate_id,
        event_type="jury2_yes",
        payload={
            "answer": True,
            "reason": "verified",
            "provider_confidence": None,
            "logical_request_id": jury2_request["logical_request_id"],
            "jury2_payload_hash": jury2_request["payload_hash"],
        },
    )
    adapter.append_candidate_event(
        event_id=f"{candidate_id}:terminal",
        candidate_id=candidate_id,
        event_type="terminal",
        payload={"resolution": "jury2_accepted", "assurance": "passed"},
    )
    assert repo.claim_verification_pair_terminal(
        claim_id=claim_id,
        ref_id=ref_id,
        scope=scope,
        status="accepted",
        outcome="supports",
        cause="jury2_accepted",
        call_id=candidate_id,
    )


def test_run_snapshot_and_history_are_read_only_and_isolate_corrupt_runs(tmp_path):
    root = tmp_path / "runs"
    good = root / "good"
    repo = _repo(good, "good")
    try:
        ref = _make_reference(1, "Doe. Example paper. Journal (2020).")
        ref["id"] = "r1"
        repo.replace_parse_payload(claims=[], references=[ref], citations=[])
        repo.set_run_setting("references_only", True)
        session_id = repo.start_session(pid=123, host="test-host")
        with repo._conn:
            repo._conn.execute(
                "UPDATE run_sessions SET heartbeat_at=? WHERE session_id=?",
                ("2000-01-01T00:00:00+00:00", session_id),
            )
    finally:
        repo.close()
    bad = root / "bad"
    bad.mkdir()
    (bad / "run.sqlite").write_text("not sqlite", encoding="utf-8")
    (good / "report.preview.html").write_text("<html></html>", encoding="utf-8")

    snapshot = load_run_snapshot(str(good))
    summaries = discover_run_summaries(str(root))

    assert snapshot["run_id"] == "good"
    assert snapshot["input"] == {"label": "example.pdf", "path": "/papers/example.pdf"}
    assert snapshot["action_required"] is False
    assert snapshot["references_only"] is True
    assert snapshot["source_inventory"][0]["ref_id"] == "r1"
    good_summary = next(row for row in summaries if row.get("available"))
    bad_summary = next(row for row in summaries if not row.get("available"))
    assert good_summary["source_counts"] == {
        "resolved": 0, "fulltext": 0, "abstract": 0, "none": 1,
    }
    assert good_summary["verification_counts"] == {
        "total": 0, "open": 0, "outcomes": {},
    }
    assert good_summary["report_html_present"] is True
    assert good_summary["report_html_path"] == str(good / "report.preview.html")
    assert good_summary["crash_recoverable"] is True
    assert good_summary["references_only"] is True
    assert any(not row["available"] and row["run_dir"] == str(bad.resolve()) for row in summaries)
    assert load_run_summary(str(good)) == good_summary
    assert load_run_summary(str(bad)) == bad_summary
    assert (good / "run.sqlite").is_file()


def test_history_summary_projects_pending_parse_review_count(tmp_path):
    repo = _repo(tmp_path, "parse-review-summary")
    try:
        reference = _make_reference(1, "Smith. Example title. Journal (2020).")
        reference["id"] = "r1"
        repo.replace_parse_payload(claims=[], references=[reference], citations=[])
        repo.create_manual_parse_review(
            task_id="review-1", review_kind="reference_identity_review",
            target_id="r1", instructions="Review the reference identity.",
        )
        repo.update_run_phase("parse")
        repo.update_run_status("paused")
    finally:
        repo.close()

    summary = load_run_summary(str(tmp_path))

    assert summary["available"] is True
    assert summary["phase"] == "parse"
    assert summary["run_status"] == "paused"
    assert summary["action_required"] is True
    assert summary["parse_review_pending"] == 1


def test_history_classifies_only_explicit_schema_rejections(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    (root / "old").mkdir(parents=True)
    (root / "old" / "run.sqlite").touch()
    (root / "broken").mkdir()
    (root / "broken" / "run.sqlite").touch()

    def load(run_dir):
        if run_dir.endswith("old"):
            raise RuntimeError("database schema version 106 is unsupported (current: 109)")
        raise sqlite3.DatabaseError("not a database")

    monkeypatch.setattr(desktop_projection, "load_run_snapshot", load)
    rows = {Path(row["run_dir"]).name: row for row in discover_run_summaries(str(root))}
    assert rows["old"]["unavailable_reason"] == "incompatible_schema"
    assert rows["broken"]["unavailable_reason"] == "unavailable"


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


def test_phase_progress_uses_only_durable_phase_and_counts():
    assert _phase_progress({"phase": "parse"}) == 0
    assert _phase_progress({"phase": "resolve", "progress_counts": {
        "sources_total": 2, "resolve_completed": 1,
    }}) == 19
    assert _phase_progress({"phase": "fetch", "progress_counts": {
        "sources_total": 2, "fetch_completed": 1,
    }}) == 52
    assert _phase_progress({"phase": "verify", "progress_counts": {
        "verify_total": 2, "verify_completed": 1,
    }}) == 85
    assert _phase_progress({"phase": "done", "run_status": "completed"}) == 100


def test_progress_counts_use_distinct_durable_fetch_conclusions_and_actual_pairs():
    repo = SimpleNamespace(
        list_citations=lambda: [
            SimpleNamespace(claim_id="c1", ref_id="r1"),
            SimpleNamespace(claim_id="c1", ref_id="r1"),
            SimpleNamespace(claim_id="c2", ref_id="r2"),
        ],
        list_integrity_unit_completions=lambda group: [
            {"unit_id": "r1", "payload": {"ref_id": "r1", "status": "not_found"}},
            {"unit_id": "r1#2", "payload": {"ref_id": "r1", "status": "stored"}},
            {"unit_id": "r2", "payload": {"ref_id": "r2", "status": "failed"}},
            {"unit_id": "not-in-inventory", "payload": {"ref_id": "not-in-inventory"}},
            {"unit_id": "r3#2", "payload": {"ref_id": "r3"}},
        ] if group == "fetch_auto" else [],
    )
    references = [
        {"ref_id": "r1", "resolve": {}},
        {"ref_id": "r2", "resolve": None},
        {"ref_id": "r3", "resolve": {}},
    ]
    pairs = [{"status": "accepted"}, {"status": "open"}]

    assert _progress_counts(repo, references, pairs) == {
        "sources_total": 3,
        "resolve_completed": 2,
        "fetch_completed": 2,
        "citation_pairs_total": 2,
        "verify_total": 2,
        "verify_completed": 1,
    }


def test_snapshot_exposes_durable_progress_counts(tmp_path):
    repo = _repo(tmp_path, "progress")
    try:
        refs = []
        for number in (1, 2):
            ref = _make_reference(number, f"Doe. Source {number}. Journal (2020).")
            ref["id"] = f"r{number}"
            refs.append(ref)
        repo.replace_parse_payload(
            claims=[{"id": "c1", "sentence": "A claim.", "marker_raw": "[1]"}],
            references=refs,
            citations=[{"claim_id": "c1", "ref_id": "r1"}],
        )
        repo.append_integrity_unit_completion(
            "fetch_auto", "r1", {"ref_id": "r1", "status": "not_found"},
        )
        repo.ensure_verification_pair(claim_id="c1", ref_id="r1", scope="fulltext_complete")
    finally:
        repo.close()

    snapshot = load_run_snapshot(str(tmp_path))

    assert snapshot["progress_counts"] == {
        "sources_total": 2,
        "resolve_completed": 0,
        "fetch_completed": 1,
        "citation_pairs_total": 1,
        "verify_total": 1,
        "verify_completed": 0,
    }


def test_run_overview_matches_full_snapshot_without_projecting_inventory(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "overview")
    try:
        refs = []
        for number in (1, 2, 3):
            ref = _make_reference(number, f"Doe. Source {number}. Journal (2020).")
            ref["id"] = f"r{number}"
            refs.append(ref)
        repo.replace_parse_payload(
            claims=[{"id": "c1", "sentence": "Claim one.", "marker_raw": "[1]"}],
            references=refs,
            citations=[{"claim_id": "c1", "ref_id": "r1"}],
        )
        repo.upsert_resolve_result("r1", {"status": "resolved", "via": "fixture"})
        repo.append_integrity_unit_completion(
            "fetch_auto", "r1", {"ref_id": "r1", "status": "not_found"},
        )
        repo.ensure_verification_pair(
            claim_id="c1", ref_id="r1", scope="fulltext_complete",
        )
    finally:
        repo.close()

    snapshot = load_run_snapshot(str(tmp_path))

    def unexpected_projection(*_args, **_kwargs):
        raise AssertionError("run overview must use durable aggregates only")

    monkeypatch.setattr(desktop_projection, "build_source_inventory", unexpected_projection)
    monkeypatch.setattr(RunRepository, "list_references", unexpected_projection)
    monkeypatch.setattr(RunRepository, "verification_pair_state_payloads", unexpected_projection)

    overview = load_run_overview(str(tmp_path))

    for key in (
        "run_dir", "run_id", "phase", "active", "paused", "action_required",
        "fetch_paused", "references_only", "run_status", "input", "accuracy",
        "progress_counts", "phase_progress",
    ):
        assert overview[key] == snapshot[key]
    assert overview["updated_at"]
    assert "source_inventory" not in overview
    assert "verification_pairs" not in overview


def test_source_pages_are_bounded_ordered_and_pair_scoped(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "pages")
    try:
        references = []
        claims = []
        citations = []
        for number in (4, 1, 5, 2, 3):
            ref = _make_reference(number, f"Doe. Source {number}. Journal (2020).")
            ref["id"] = f"r{number}"
            references.append(ref)
            claim_id = f"c{number}"
            claims.append({"id": claim_id, "sentence": f"Claim {number}.", "marker_raw": f"[{number}]"})
            citations.append({"claim_id": claim_id, "ref_id": f"r{number}"})
        repo.replace_parse_payload(claims=claims, references=references, citations=citations)
        for number in range(1, 6):
            repo.ensure_verification_pair(
                claim_id=f"c{number}", ref_id=f"r{number}",
                scope="fulltext_complete",
            )
    finally:
        repo.close()

    full = load_run_snapshot(str(tmp_path))
    expected_order = [item["ref_id"] for item in full["source_inventory"]]
    expected_pairs = full["verification_pairs"]
    resolver_calls = []
    pair_queries = []
    original_get_resolve_result = RunRepository.get_resolve_result
    original_pair_states = RunRepository.verification_pair_state_payloads

    def track_resolve(repo, ref_id):
        resolver_calls.append(ref_id)
        return original_get_resolve_result(repo, ref_id)

    def track_pairs(repo, *, ref_ids=None):
        pair_queries.append(ref_ids)
        return original_pair_states(repo, ref_ids=ref_ids)

    monkeypatch.setattr(RunRepository, "get_resolve_result", track_resolve)
    monkeypatch.setattr(RunRepository, "verification_pair_state_payloads", track_pairs)

    first = load_source_page(str(tmp_path), offset=0, limit=2)
    second = load_source_page(str(tmp_path), offset=2, limit=2)

    first_ids = [item["ref_id"] for item in first["source_inventory"]]
    second_ids = [item["ref_id"] for item in second["source_inventory"]]
    assert first["total"] == second["total"] == len(expected_order) == 5
    assert first_ids == expected_order[:2]
    assert second_ids == expected_order[2:4]
    assert not set(first_ids) & set(second_ids)
    assert resolver_calls == [*first_ids, *second_ids]
    assert pair_queries == [first_ids, second_ids]
    assert first["verification_pairs"] == [
        pair for pair in expected_pairs if pair["ref_id"] in set(first_ids)
    ]
    assert second["verification_pairs"] == [
        pair for pair in expected_pairs if pair["ref_id"] in set(second_ids)
    ]
    assert first["phase"] == full["phase"]


def test_source_page_keeps_accepted_verdict_projection_scoped(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "accepted-pages")
    try:
        references = []
        claims = []
        citations = []
        for number in (1, 2):
            ref = _make_reference(number, f"Doe. Source {number}. Journal (2020).")
            ref["id"] = f"r{number}"
            references.append(ref)
            claim_id = f"c{number}"
            claims.append({"id": claim_id, "sentence": f"Claim {number}.", "marker_raw": f"[{number}]"})
            citations.append({"claim_id": claim_id, "ref_id": f"r{number}"})
        repo.replace_parse_payload(claims=claims, references=references, citations=citations)
        _append_accepted_pair(repo, ref_id="r1", claim_id="c1")
        _append_accepted_pair(repo, ref_id="r2", claim_id="c2")
    finally:
        repo.close()

    full = load_run_snapshot(str(tmp_path))
    expected = next(pair for pair in full["verification_pairs"] if pair["ref_id"] == "r1")
    assert expected["jury1_outcome"] == "supports"
    scoped_reads = []
    original_scoped_read = RunRepository.verification_pair_projection_payloads

    def tracked_scoped_read(repo, *, ref_ids):
        scoped_reads.append(list(ref_ids))
        return original_scoped_read(repo, ref_ids=ref_ids)

    def forbidden_full_read(*_args, **_kwargs):
        raise AssertionError("a source page must not validate the full verification ledger")

    monkeypatch.setattr(
        RunRepository,
        "verification_pair_projection_payloads",
        tracked_scoped_read,
    )
    monkeypatch.setattr(RunRepository, "verification_raw_payloads", forbidden_full_read)

    page = load_source_page(str(tmp_path), offset=0, limit=1)

    assert [item["ref_id"] for item in page["source_inventory"]] == ["r1"]
    assert page["verification_pairs"] == [expected]
    assert scoped_reads == [["r1"]]

    detail = load_source_detail(str(tmp_path), "r1")

    assert detail is not None
    assert detail["verification_pairs"] == [expected]
    assert scoped_reads == [["r1"], ["r1"]]


def test_source_page_rejects_unbounded_or_invalid_limits(tmp_path):
    repo = _repo(tmp_path, "page-bounds")
    repo.close()

    for offset, limit in (
        (-1, 10), (True, 10), (1 << 63, 10), (0, 0), (0, 101), (0, True),
    ):
        try:
            load_source_page(str(tmp_path), offset=offset, limit=limit)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid source page bounds: {offset}, {limit}")


def test_latest_brief_uses_durable_update_time_without_loading_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    root.mkdir()
    older = root / "older"
    newer = root / "newer"
    for path, name, updated_at in (
        (older, "older", "2024-01-01T00:00:00+00:00"),
        (newer, "newer", "2025-01-01T00:00:00+00:00"),
    ):
        repo = _repo(path, name)
        try:
            with repo._conn:
                repo._conn.execute(
                    "UPDATE run SET updated_at=? WHERE run_id=?",
                    (updated_at, name),
                )
        finally:
            repo.close()

    # Directory mtimes deliberately point at a different "latest" run.
    os.utime(older, (2_000_000_000, 2_000_000_000))
    os.utime(newer, (1_000_000_000, 1_000_000_000))
    broken = root / "broken"
    broken.mkdir()
    (broken / "run.sqlite").write_text("not sqlite", encoding="utf-8")
    closed = []
    original_close = RunRepository.close

    def tracked_close(repo):
        closed.append(repo._run_dir)
        original_close(repo)

    def unexpected_full_projection(*_args, **_kwargs):
        raise AssertionError("latest brief must not load source or pair snapshots")

    original_list_pending_tasks = RunRepository.list_pending_tasks

    def pending_tasks_for_latest(repo, *, slot=None):
        if repo._run_dir == str(newer.resolve()):
            return [SimpleNamespace(task_id="pending-fetch")]
        return original_list_pending_tasks(repo, slot=slot)

    monkeypatch.setattr(RunRepository, "close", tracked_close)
    monkeypatch.setattr(RunRepository, "list_pending_tasks", pending_tasks_for_latest)
    monkeypatch.setattr(desktop_projection, "build_source_inventory", unexpected_full_projection)
    monkeypatch.setattr(
        RunRepository, "verification_pair_state_payloads", unexpected_full_projection,
    )

    briefs = discover_run_briefs(str(root))
    latest = load_latest_run_brief(str(root))
    direct = load_run_brief(str(newer))

    assert [row.get("run_id") for row in briefs[:2]] == ["newer", "older"]
    assert briefs[2]["available"] is False
    assert all("source_counts" not in row for row in briefs)
    assert all("verification_counts" not in row for row in briefs)
    assert latest is not None
    assert latest["run_id"] == "newer"
    assert latest["updated_at"] == "2025-01-01T00:00:00+00:00"
    assert latest["run_dir"] == str(newer.resolve())
    assert latest["action_required"] is True
    assert direct["completed"] is False
    assert direct["action_required"] is True
    assert direct["report_html_present"] is False
    # Each discovery pass closes both valid handles and the failed corrupt
    # handle; the direct brief read closes one additional valid handle.
    assert len(closed) == 7


def test_source_detail_is_scoped_and_snapshot_can_omit_fetch_attempts(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "detail")
    try:
        references = []
        for number in (1, 2):
            reference = _make_reference(
                number, f"Doe. Source {number}. Journal (2020).",
            )
            reference["id"] = f"r{number}"
            references.append(reference)
        repo.replace_parse_payload(
            claims=[
                {"id": "c1", "sentence": "Claim one.", "marker_raw": "[1]"},
                {"id": "c2", "sentence": "Claim two.", "marker_raw": "[2]"},
            ],
            references=references,
            citations=[
                {"claim_id": "c1", "ref_id": "r1"},
                {"claim_id": "c2", "ref_id": "r2"},
            ],
        )
        for ref_id in ("r1", "r2"):
            repo.append_fetch_attempt(
                ref_id, method="GET", url=f"https://example.test/{ref_id}",
                kind="fulltext", final_url=f"https://example.test/{ref_id}",
                status_code=200, content_type="text/plain", outcome="stored",
                origin="fixture",
            )
            repo.ensure_verification_pair(
                claim_id=f"c{ref_id[-1]}", ref_id=ref_id,
                scope="fulltext_complete",
            )
            text = f"Source bytes for {ref_id}."
            source_path = tmp_path / "sources" / f"{ref_id}.txt"
            source_path.parent.mkdir(exist_ok=True)
            source_path.write_text(text, encoding="utf-8")
            repo.store_source_text(
                source_text_id=f"source-{ref_id}", ref_id=ref_id,
                identity_key=f"fixture:{ref_id}", tier="fulltext", origin="fixture",
                stored_path=f"sources/{ref_id}.txt",
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                char_count=len(text), source_ref=f"https://example.test/{ref_id}",
                extraction_method="fixture",
            )
            if ref_id == "r1":
                source_path.write_text("tampered after admission", encoding="utf-8")
    finally:
        repo.close()

    queried_fetch_refs = []
    original_list_fetch_attempts = RunRepository.list_fetch_attempts

    def tracked_list_fetch_attempts(repo, ref_id):
        queried_fetch_refs.append(ref_id)
        return original_list_fetch_attempts(repo, ref_id)

    monkeypatch.setattr(
        RunRepository, "list_fetch_attempts", tracked_list_fetch_attempts,
    )

    detail = load_source_detail(str(tmp_path), "r1")
    snapshot = load_run_snapshot(str(tmp_path), include_fetch_attempts=False)

    assert detail is not None
    assert detail["parsed"]["ref_id"] == "r1"
    assert detail["resolved"] is None
    assert [item["ref_id"] for item in detail["fetch"]["attempts"]] == ["r1"]
    assert [item["ref_id"] for item in detail["fetch"]["sources"]] == ["r1"]
    assert detail["fetch"]["sources"][0]["available"] is False
    assert "hash mismatch" in detail["fetch"]["sources"][0]["availability_diagnostics"][0]
    assert [pair["ref_id"] for pair in detail["verification_pairs"]] == ["r1"]
    assert queried_fetch_refs == ["r1"]
    assert all(
        not item["fetch"]["attempts"] for item in snapshot["source_inventory"]
    )
    assert load_source_detail(str(tmp_path), "missing") is None
