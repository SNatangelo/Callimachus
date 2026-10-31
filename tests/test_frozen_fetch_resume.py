# tests/test_frozen_fetch_resume.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Real-SQLite regression tests for one frozen deadline-candidate replay."""

from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from core.fetch import service as fetch_core
from core.fetch.transport import host_limiter
from core.app.phases import fetch as fetch_phase
from core.infra.db import RunRepository
from core.infra.db.fetch_candidates import fingerprints
from core.infra.db.task_storage import (
    _FETCH_REFERENCE_FIELDS,
    _operational_reference,
    _source_identity,
)


REF_ID = "r1"


def _create_run(run_dir: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    repo = RunRepository.create(
        str(run_dir),
        run_id=f"frozen-{run_dir.name}",
        input_path="paper.pdf",
        input_sha256="input-sha",
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile="default",
        challenge_mode="off",
        fixture_fingerprint="fixture",
    )
    try:
        repo.replace_parse_payload(
            claims=[],
            references=[
                {
                    "id": REF_ID,
                    "ref_number": 1,
                    "raw_entry": "Example. Frozen paper. 2024.",
                    "title": "Frozen paper",
                    "doi": "10.1234/frozen",
                    "year": 2024,
                }
            ],
            citations=[],
        )
        repo.upsert_resolve_result(
            REF_ID,
            {
                "ref_id": REF_ID,
                "status": "resolved",
                "via": "crossref",
                "matched_title": "Frozen paper",
                "fulltext_exists": True,
                "fulltext_links": [
                    {
                        "url": "https://example.test/frozen.pdf",
                        "content_type": "application/pdf",
                        "discovered_via": "crossref",
                    }
                ],
                "attempts": [],
            },
        )
    finally:
        repo.close()
    return _reload_inputs(run_dir)


def _reload_inputs(run_dir: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    parse = fetch_phase._load_parse_payload(str(run_dir))
    refs = {row["id"]: row for row in parse["references"]}
    return refs, fetch_phase._resolve_map(str(run_dir))


def _context(refs: dict[str, dict], resolved: dict[str, dict]) -> dict:
    resolution = resolved[REF_ID]
    evidence = fetch_phase._fetch_evidence_payload(resolution)
    return fetch_core._frozen_fetch_context(
        refs[REF_ID], resolution, evidence_payload=evidence
    )


def _candidate(url: str, *, queue_index: int = 0, batch_index: int = 0) -> dict:
    return {
        "method": "crossref",
        "url": url,
        "kind": "pdf",
        "queue_index": queue_index,
        "batch_index": batch_index,
    }


def _execution_attempt(
    candidate_id: int,
    candidate: dict,
    outcome: str,
    *,
    deadline: bool = False,
) -> dict:
    attempt = {
        "queue_index": candidate["queue_index"],
        "batch_index": candidate["batch_index"],
        "method": candidate["method"],
        "url": candidate["url"],
        "kind": candidate["kind"],
        "outcome": outcome,
        "reason": "deadline before admission" if deadline else outcome,
        "frozen_candidate_id": candidate_id,
    }
    if deadline:
        attempt["request"] = "none"
    else:
        attempt.update(
            {
                "status": 200,
                "final_url": candidate["url"],
                "content_type": "application/pdf",
            }
        )
    return attempt


def _fetched(candidate_id: int, candidate: dict, outcome: str) -> dict:
    return {
        "status": "stored" if outcome == "stored" else "not_found",
        "method": candidate["method"],
        "fetch_trace": {
            "execution": {
                "attempts": [
                    _execution_attempt(candidate_id, candidate, outcome)
                ]
            }
        },
    }


def _deferred(candidate_id: int, candidate: dict) -> dict:
    attempt = _execution_attempt(candidate_id, candidate, "rate_limit_deferred", deadline=True)
    attempt["reason_code"] = "host_cooldown"
    return {
        "status": "rate_limit_deferred",
        "method": candidate["method"],
        "fetch_trace": {"execution": {"attempts": [attempt]}},
    }


def _unissued(candidate_id: int, candidate: dict) -> dict:
    attempt = _execution_attempt(candidate_id, candidate, "signed_url_expired", deadline=True)
    attempt["reason_code"] = "signed_url_expired"
    return {
        "status": "not_found",
        "method": candidate["method"],
        "fetch_trace": {"execution": {"attempts": [attempt]}},
    }


def _record_deadline(
    run_dir: Path,
    candidate_id: int,
    candidate: dict,
    resolved: dict[str, dict],
) -> int:
    fetched = {
        "status": "not_found",
        "fetch_trace": {
            "execution": {
                "attempts": [
                    _execution_attempt(
                        candidate_id,
                        candidate,
                        "deadline_exceeded",
                        deadline=True,
                    )
                ]
            }
        },
    }
    return fetch_phase._record_fetch_attempts(
        str(run_dir), REF_ID, fetched, resolved[REF_ID]
    )[candidate_id]


def _freeze_replay_fixture(
    run_dir: Path,
) -> tuple[dict[str, dict], dict[str, dict], dict[str, int], dict[str, dict]]:
    refs, resolved = _create_run(run_dir)
    frozen_context = _context(refs, resolved)
    candidates = {
        "admitted": _candidate("https://example.test/admitted.pdf"),
        "eligible": _candidate("https://example.test/eligible.pdf"),
    }
    repo = RunRepository.open(str(run_dir))
    try:
        admitted = repo.freeze_fetch_candidate_stage(
            REF_ID, "published", frozen_context, [candidates["admitted"]]
        )[0]
        eligible = repo.freeze_fetch_candidate_stage(
            REF_ID, "preprint", frozen_context, [candidates["eligible"]]
        )[0]
        assert repo.admit_frozen_fetch_candidate_batch([admitted]) is True
    finally:
        repo.close()
    attempt_ids = {
        "admitted": _record_deadline(
            run_dir, admitted, candidates["admitted"], resolved
        ),
        "eligible": _record_deadline(
            run_dir, eligible, candidates["eligible"], resolved
        ),
    }
    repo = RunRepository.open(str(run_dir))
    try:
        assert repo.list_eligible_frozen_fetch_candidates() == [eligible]
    finally:
        repo.close()
    return refs, resolved, {
        "admitted": admitted,
        "eligible": eligible,
        **{f"{name}_attempt": value for name, value in attempt_ids.items()},
    }, candidates


def _events(repo: RunRepository, candidate_id: int) -> list[dict]:
    return [
        dict(row)
        for row in repo._conn.execute(
            """
            SELECT event_id, frozen_candidate_id, event_type, fetch_attempt_id,
                   reason_code, reason_detail, created_at
            FROM fetch_frozen_candidate_events
            WHERE frozen_candidate_id = ?
            ORDER BY event_id
            """,
            (candidate_id,),
        )
    ]


def _attempt(repo: RunRepository, attempt_id: int) -> dict:
    return next(
        row
        for row in repo.list_fetch_attempts(REF_ID)
        if row["fetch_attempt_id"] == attempt_id
    )


def _bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _source(
    repo: RunRepository,
    *,
    source_text_id: str,
    tier: str,
    origin: str,
    text: str,
) -> None:
    repo.store_source_text(
        source_text_id=source_text_id,
        ref_id=REF_ID,
        identity_key=f"doi:10.1234/frozen:{tier}",
        tier=tier,
        origin=origin,
        stored_path=f"sources/{source_text_id}.txt",
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        char_count=len(text),
        source_ref="https://example.test/frozen.pdf",
        identity_status="matched",
        extraction_method="fixture",
    )


def _resume(
    run_dir: Path,
    refs: dict[str, dict],
    resolved: dict[str, dict],
) -> dict | None:
    return fetch_phase._resume_one_frozen_fetch_candidate(
        {"run_dir": str(run_dir), "mailto": "audit@example.test"},
        refs,
        resolved,
    )


def _create_pending_task(repo: RunRepository, *, task_id: str, kind: str = "fetch") -> None:
    if kind == "fetch":
        reference = _operational_reference(
            repo._conn, REF_ID, _FETCH_REFERENCE_FIELDS
        )
        payload = {
            "kind": "fetch",
            "status": "pending",
            "ref_id": REF_ID,
            "ref_number": reference["ref_number"],
            "reference": reference,
            "source_identity": _source_identity(repo._conn, REF_ID),
            "instructions": "Supply the full text or mark it not found.",
            "answer": None,
        }
    else:
        reference = _operational_reference(
            repo._conn, REF_ID, ("ref_number", "raw_entry", "doi", "url")
        )
        payload = {
            "kind": "browser_challenge",
            "status": "pending",
            "domain": "example.test",
            "references": [{
                "ref_id": REF_ID,
                **reference,
                "candidate_urls": ["https://example.test/frozen.pdf"],
            }],
            "candidate_urls": ["https://example.test/frozen.pdf"],
            "instructions": "Use a visible browser session.",
            "answer": None,
        }
    repo.create_task(
        task_id=task_id,
        slot="fetch",
        ref_id=REF_ID if kind == "fetch" else None,
        scope=None,
        task_payload=payload,
    )


def _pending_handles(run_dir: Path) -> list[str]:
    return [handle for handle, _task in fetch_phase._fetch_resume_pending_tasks(str(run_dir))]


def _store_retry_fulltext(repo: RunRepository) -> None:
    _source(
        repo,
        source_text_id="retry-fulltext",
        tier="fulltext",
        origin="fixture",
        text="Recovered full text",
    )


def test_real_resume_replays_only_eligible_candidate_and_is_idempotent(tmp_path):
    run_dir = tmp_path / "lifecycle"
    refs, resolved, ids, candidates = _freeze_replay_fixture(run_dir)
    repo = RunRepository.open(str(run_dir))
    try:
        admitted_before = _bytes(
            {
                "record": repo.get_frozen_fetch_candidate_record(ids["admitted"]),
                "events": _events(repo, ids["admitted"]),
                "attempt": _attempt(repo, ids["admitted_attempt"]),
            }
        )
        eligible_deadline_before = _bytes(
            _attempt(repo, ids["eligible_attempt"])
        )
    finally:
        repo.close()


    replayed: list[tuple[dict, int]] = []

    def singleton(_ref, _run, _resolved, candidate, candidate_id, **_kwargs):
        replayed.append((deepcopy(candidate), candidate_id))
        return _fetched(candidate_id, candidate, "http_error")

    with (
        mock.patch.object(
            fetch_phase._fetch,
            "_fetch_frozen_candidate",
            side_effect=singleton,
        ),
        mock.patch.object(fetch_phase, "_progress"),
    ):
        first = _resume(run_dir, refs, resolved)
        second = _resume(run_dir, refs, resolved)

    assert first is not None and first["status"] == "completed"
    assert second is None
    assert replayed == [(candidates["eligible"], ids["eligible"])]

    repo = RunRepository.open(str(run_dir))
    try:
        assert _bytes(
            {
                "record": repo.get_frozen_fetch_candidate_record(ids["admitted"]),
                "events": _events(repo, ids["admitted"]),
                "attempt": _attempt(repo, ids["admitted_attempt"]),
            }
        ) == admitted_before
        assert _bytes(_attempt(repo, ids["eligible_attempt"])) == eligible_deadline_before
        attempts = repo.list_fetch_attempts(REF_ID)
        assert len(attempts) == 3
        retry = attempts[-1]
        assert retry["trace"]["frozen_candidate_id"] == ids["eligible"]
        events = _events(repo, ids["eligible"])
        assert [row["event_type"] for row in events] == [
            "deadline_skipped",
            "retry_claimed",
            "retry_completed",
        ]
        assert events[0]["fetch_attempt_id"] == ids["eligible_attempt"]
        assert events[1]["fetch_attempt_id"] is None
        assert events[2]["fetch_attempt_id"] == retry["fetch_attempt_id"]
        assert repo.list_eligible_frozen_fetch_candidates() == []
    finally:
        repo.close()


def test_no_request_replay_is_deferred_and_reclaimed_until_one_real_attempt(tmp_path):
    run_dir = tmp_path / "deferred-replay"
    refs, resolved, ids, candidates = _freeze_replay_fixture(run_dir)
    deferred = _deferred(ids["eligible"], candidates["eligible"])
    completed = _fetched(ids["eligible"], candidates["eligible"], "http_error")
    calls: list[int] = []

    def singleton(_ref, _run, _resolved, _candidate, candidate_id, **_kwargs):
        calls.append(candidate_id)
        return deferred if len(calls) < 3 else completed

    with (
        mock.patch.object(
            fetch_phase._fetch, "_fetch_frozen_candidate", side_effect=singleton
        ),
        mock.patch.object(fetch_phase, "_progress"),
    ):
        first = _resume(run_dir, refs, resolved)
        second = _resume(run_dir, refs, resolved)
        third = _resume(run_dir, refs, resolved)

    assert first is not None and first["status"] == "rate_limit_deferred"
    assert second is not None and second["status"] == "rate_limit_deferred"
    assert third is not None and third["status"] == "completed"
    assert calls == [ids["eligible"], ids["eligible"], ids["eligible"]]

    repo = RunRepository.open(str(run_dir))
    try:
        attempts = repo.list_fetch_attempts(REF_ID)
        assert len(attempts) == 4
        assert all(row.tier != "fulltext" for row in repo.list_source_texts(REF_ID))
        retry_attempts = attempts[-2:]
        assert [row["trace"].get("request") for row in retry_attempts] == [
            "none", None,
        ]
        assert [row["event_type"] for row in _events(repo, ids["eligible"])] == [
            "deadline_skipped",
            "retry_claimed",
            "retry_deferred",
            "retry_claimed",
            "retry_deferred",
            "retry_claimed",
            "retry_completed",
        ]
        assert repo.list_eligible_frozen_fetch_candidates() == []
    finally:
        repo.close()


def test_real_frozen_replay_host_cooldown_records_retry_deferred(tmp_path):
    run_dir = tmp_path / "real-host-cooldown"
    refs, resolved, ids, candidates = _freeze_replay_fixture(run_dir)
    limiter = host_limiter.HostLimiter(
        default_rate=host_limiter.HostRate.per_second(0.0)
    )
    limiter.cooldown_for_url(candidates["eligible"]["url"], seconds=60)

    with (
        mock.patch(
            "core.fetch.transport.http._hosts_mod._shared_host_limiter",
            return_value=limiter,
        ),
        mock.patch.object(fetch_phase, "_progress"),
    ):
        result = _resume(run_dir, refs, resolved)

    assert result is not None and result["status"] == "rate_limit_deferred"
    repo = RunRepository.open(str(run_dir))
    try:
        attempts = repo.list_fetch_attempts(REF_ID)
        retry = attempts[-1]
        assert retry["trace"]["outcome"] == "rate_limit_deferred"
        assert retry["trace"]["request"] == "none"
        assert [row["event_type"] for row in _events(repo, ids["eligible"])] == [
            "deadline_skipped", "retry_claimed", "retry_deferred",
        ]
        assert repo.list_eligible_frozen_fetch_candidates() == [ids["eligible"]]
    finally:
        repo.close()
    fetch_phase._write_manual_fetch_report(str(run_dir), refs, resolved)
    report = (run_dir / "fetch_manual_sources.md").read_text(encoding="utf-8")
    assert "frozen_retry_deferred:host_cooldown" in report


def test_unissued_non_cooldown_replay_is_invalidated_not_completed(tmp_path):
    run_dir = tmp_path / "unissued-replay"
    refs, resolved, ids, candidates = _freeze_replay_fixture(run_dir)
    unissued = _unissued(ids["eligible"], candidates["eligible"])

    with (
        mock.patch.object(
            fetch_phase._fetch, "_fetch_frozen_candidate", return_value=unissued
        ) as singleton,
        mock.patch.object(fetch_phase, "_progress"),
    ):
        first = _resume(run_dir, refs, resolved)
        second = _resume(run_dir, refs, resolved)

    assert first is not None and first["status"] == "invalidated"
    assert second is None
    singleton.assert_called_once()
    repo = RunRepository.open(str(run_dir))
    try:
        events = _events(repo, ids["eligible"])
        assert [row["event_type"] for row in events] == [
            "deadline_skipped", "retry_claimed", "retry_invalidated",
        ]
        assert events[-1]["reason_code"] == "replay_request_not_issued"
        assert events[-1]["fetch_attempt_id"] is not None
        assert repo.list_eligible_frozen_fetch_candidates() == []
    finally:
        repo.close()


def test_two_real_concurrent_resumes_claim_once_and_persist_once(tmp_path):
    run_dir = tmp_path / "concurrent"
    refs, resolved, ids, _candidates = _freeze_replay_fixture(run_dir)
    start = threading.Barrier(3)
    singleton_entered = threading.Event()
    release_singleton = threading.Event()
    calls: list[int] = []
    results: list[dict | None] = []

    def singleton(_ref, _run, _resolved, candidate, candidate_id, **_kwargs):
        calls.append(candidate_id)
        singleton_entered.set()
        assert release_singleton.wait(5)
        return _fetched(candidate_id, candidate, "http_error")

    def worker() -> None:
        start.wait()
        results.append(_resume(run_dir, refs, resolved))

    with (
        mock.patch.object(
            fetch_phase._fetch,
            "_fetch_frozen_candidate",
            side_effect=singleton,
        ),
        mock.patch.object(fetch_phase, "_progress"),
    ):
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait()
        assert singleton_entered.wait(5)
        release_singleton.set()
        for thread in threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in threads)

    assert calls == [ids["eligible"]]
    assert len(results) == 2
    assert sum(row is None for row in results) == 1
    assert [row["status"] for row in results if row is not None] == ["completed"]
    repo = RunRepository.open(str(run_dir))
    try:
        attempts = repo.list_fetch_attempts(REF_ID)
        assert len(attempts) == 3
        events = _events(repo, ids["eligible"])
        assert [row["event_type"] for row in events] == [
            "deadline_skipped",
            "retry_claimed",
            "retry_completed",
        ]
        assert events[-1]["fetch_attempt_id"] == attempts[-1]["fetch_attempt_id"]
    finally:
        repo.close()


def test_singleton_crash_after_real_claim_is_ambiguous_and_never_retried(tmp_path):
    run_dir = tmp_path / "singleton-crash"
    refs, resolved, ids, _candidates = _freeze_replay_fixture(run_dir)
    with (
        mock.patch.object(
            fetch_phase._fetch,
            "_fetch_frozen_candidate",
            side_effect=RuntimeError("crash before request"),
        ) as singleton,
        mock.patch.object(fetch_phase, "_progress"),
    ):
        first = _resume(run_dir, refs, resolved)
        second = _resume(run_dir, refs, resolved)
    assert first == {
        "status": "ambiguous_claim",
        "candidate_id": ids["eligible"],
        "ref_id": REF_ID,
        "error": "RuntimeError",
    }
    assert second is None
    singleton.assert_called_once()
    repo = RunRepository.open(str(run_dir))
    try:
        assert len(repo.list_fetch_attempts(REF_ID)) == 2
        assert [row["event_type"] for row in _events(repo, ids["eligible"])] == [
            "deadline_skipped",
            "retry_claimed",
        ]
    finally:
        repo.close()


def test_recorder_crash_after_singleton_keeps_real_claim_ambiguous(tmp_path):
    run_dir = tmp_path / "recorder-crash"
    refs, resolved, ids, candidates = _freeze_replay_fixture(run_dir)
    fetched = _fetched(ids["eligible"], candidates["eligible"], "http_error")
    with (
        mock.patch.object(
            fetch_phase._fetch, "_fetch_frozen_candidate", return_value=fetched
        ) as singleton,
        mock.patch.object(
            fetch_phase,
            "_record_fetch_attempts",
            side_effect=RuntimeError("recorder crash"),
        ) as recorder,
        mock.patch.object(fetch_phase, "_progress"),
    ):
        first = _resume(run_dir, refs, resolved)
        second = _resume(run_dir, refs, resolved)
    assert first is not None and first["status"] == "ambiguous_claim"
    assert first["error"] == "RuntimeError"
    assert second is None
    singleton.assert_called_once()
    recorder.assert_called_once()
    repo = RunRepository.open(str(run_dir))
    try:
        assert len(repo.list_fetch_attempts(REF_ID)) == 2
        assert [row["event_type"] for row in _events(repo, ids["eligible"])] == [
            "deadline_skipped",
            "retry_claimed",
        ]
    finally:
        repo.close()


def test_preclaimed_candidate_is_never_selected_even_when_stale(tmp_path):
    run_dir = tmp_path / "preclaimed"
    refs, resolved, ids, _candidates = _freeze_replay_fixture(run_dir)
    repo = RunRepository.open(str(run_dir))
    try:
        assert repo.claim_frozen_fetch_candidate(ids["eligible"]) is True
    finally:
        repo.close()
    with mock.patch.object(
        fetch_phase._fetch, "_fetch_frozen_candidate"
    ) as singleton:
        assert _resume(run_dir, refs, resolved) is None
    singleton.assert_not_called()
    repo = RunRepository.open(str(run_dir))
    try:
        assert repo.list_eligible_frozen_fetch_candidates() == []
        assert [row["event_type"] for row in _events(repo, ids["eligible"])] == [
            "deadline_skipped",
            "retry_claimed",
        ]
    finally:
        repo.close()


def test_context_mismatch_invalidates_before_real_claim_or_singleton(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "context-mismatch"
    refs, resolved, ids, _candidates = _freeze_replay_fixture(run_dir)
    repo = RunRepository.open(str(run_dir))
    try:
        frozen = repo.get_frozen_fetch_candidate_record(ids["eligible"])
    finally:
        repo.close()
    old_language = fetch_core.os.environ.get("CITATION_VERIFIER_OCR_LANG")
    monkeypatch.setenv(
        "CITATION_VERIFIER_OCR_LANG",
        "zz-frozen-mismatch" if old_language != "zz-frozen-mismatch" else "eng",
    )
    current_context = _context(refs, resolved)
    current_context_fp, current_plan_fp = fingerprints(REF_ID, current_context)
    assert (current_context_fp, current_plan_fp) != (
        frozen["context_fingerprint"],
        frozen["plan_fingerprint"],
    )
    with mock.patch.object(
        fetch_phase._fetch, "_fetch_frozen_candidate"
    ) as singleton:
        assert _resume(run_dir, refs, resolved) is None
    singleton.assert_not_called()
    repo = RunRepository.open(str(run_dir))
    try:
        events = _events(repo, ids["eligible"])
        assert [row["event_type"] for row in events] == [
            "deadline_skipped",
            "retry_invalidated",
        ]
        assert events[-1]["reason_code"] == "context_mismatch"
    finally:
        repo.close()


def test_tampered_candidate_fingerprint_is_queryably_invalidated_without_http(
    tmp_path,
):
    run_dir = tmp_path / "tamper"
    refs, resolved, ids, _candidates = _freeze_replay_fixture(run_dir)
    repo = RunRepository.open(str(run_dir))
    try:
        with repo._conn:
            repo._conn.execute("DROP TRIGGER fetch_frozen_candidates_no_update")
            repo._conn.execute(
                """
                UPDATE fetch_frozen_candidates
                SET candidate_fingerprint = ?
                WHERE frozen_candidate_id = ?
                """,
                ("0" * 64, ids["eligible"]),
            )
            repo._conn.execute(
                """
                CREATE TRIGGER fetch_frozen_candidates_no_update
                BEFORE UPDATE ON fetch_frozen_candidates
                BEGIN
                  SELECT RAISE(ABORT, 'fetch_frozen_candidates immutable');
                END
                """
            )
    finally:
        repo.close()
    with mock.patch.object(
        fetch_phase._fetch, "_fetch_frozen_candidate"
    ) as singleton:
        assert _resume(run_dir, refs, resolved) is None
    singleton.assert_not_called()
    repo = RunRepository.open(str(run_dir))
    try:
        events = _events(repo, ids["eligible"])
        assert [row["event_type"] for row in events] == [
            "deadline_skipped",
            "retry_invalidated",
        ]
        assert events[-1]["reason_code"] == "record_invalid"
        assert repo.list_eligible_frozen_fetch_candidates() == []
        summaries = repo.list_frozen_fetch_candidate_lifecycle_summaries(REF_ID)
        invalidated = next(
            row for row in summaries if row["candidate_id"] == ids["eligible"]
        )
        assert invalidated["lifecycle"] == "retry_invalidated"
        assert invalidated["reason_code"] == "record_invalid"
        assert invalidated["fetch_attempt_id"] is None
    finally:
        repo.close()


def test_preexisting_fulltext_invalidates_before_claim_and_preserves_source(tmp_path):
    run_dir = tmp_path / "fulltext-present"
    refs, resolved, ids, _candidates = _freeze_replay_fixture(run_dir)
    repo = RunRepository.open(str(run_dir))
    try:
        _source(
            repo,
            source_text_id="existing-fulltext",
            tier="fulltext",
            origin="fixture",
            text="Existing full text",
        )
        _create_pending_task(repo, task_id="fetch-r1")
        before = asdict(repo.list_source_texts(REF_ID)[0])
    finally:
        repo.close()
    with mock.patch.object(
        fetch_phase._fetch, "_fetch_frozen_candidate"
    ) as singleton:
        assert _resume(run_dir, refs, resolved) is None
    singleton.assert_not_called()
    repo = RunRepository.open(str(run_dir))
    try:
        assert asdict(repo.list_source_texts(REF_ID)[0]) == before
        events = _events(repo, ids["eligible"])
        assert [row["event_type"] for row in events] == [
            "deadline_skipped",
            "retry_invalidated",
        ]
        assert events[-1]["reason_code"] == "fulltext_present"
    finally:
        repo.close()
    assert _pending_handles(run_dir) == []


def test_wrong_identity_retry_persists_one_linked_attempt_and_no_source(tmp_path):
    run_dir = tmp_path / "wrong-identity"
    refs, resolved, ids, candidates = _freeze_replay_fixture(run_dir)
    fetched = _fetched(
        ids["eligible"], candidates["eligible"], "identity_mismatch"
    )
    with (
        mock.patch.object(
            fetch_phase._fetch, "_fetch_frozen_candidate", return_value=fetched
        ) as singleton,
        mock.patch.object(fetch_phase, "_progress"),
    ):
        result = _resume(run_dir, refs, resolved)
    assert result is not None and result["status"] == "completed"
    singleton.assert_called_once()
    repo = RunRepository.open(str(run_dir))
    try:
        attempts = repo.list_fetch_attempts(REF_ID)
        assert len(attempts) == 3
        retry = attempts[-1]
        assert retry["outcome"] == "identity_mismatch"
        assert retry["trace"]["frozen_candidate_id"] == ids["eligible"]
        assert repo.list_source_texts(REF_ID) == []
        events = _events(repo, ids["eligible"])
        assert events[-1]["event_type"] == "retry_completed"
        assert events[-1]["fetch_attempt_id"] == retry["fetch_attempt_id"]
    finally:
        repo.close()


def test_successful_retry_preserves_abstract_and_adds_one_linked_fulltext(tmp_path):
    run_dir = tmp_path / "success"
    refs, resolved, ids, _candidates = _freeze_replay_fixture(run_dir)
    repo = RunRepository.open(str(run_dir))
    try:
        _source(
            repo,
            source_text_id="existing-abstract",
            tier="abstract",
            origin="crossref",
            text="Existing abstract",
        )
        abstract_before = asdict(repo.list_source_texts(REF_ID)[0])
    finally:
        repo.close()

    def singleton(_ref, run, _resolved, candidate, candidate_id, **_kwargs):
        worker_repo = RunRepository.open(run)
        try:
            _source(
                worker_repo,
                source_text_id="replayed-fulltext",
                tier="fulltext",
                origin="crossref",
                text="Recovered full text",
            )
        finally:
            worker_repo.close()
        return _fetched(candidate_id, candidate, "stored")

    with (
        mock.patch.object(
            fetch_phase._fetch,
            "_fetch_frozen_candidate",
            side_effect=singleton,
        ),
        mock.patch.object(fetch_phase, "_progress"),
    ):
        result = _resume(run_dir, refs, resolved)
    assert result is not None and result["status"] == "completed"
    repo = RunRepository.open(str(run_dir))
    try:
        sources = repo.list_source_texts(REF_ID)
        assert len(sources) == 2
        by_tier = {row.tier: row for row in sources}
        assert asdict(by_tier["abstract"]) == abstract_before
        assert by_tier["fulltext"].source_text_id == "replayed-fulltext"
        attempts = repo.list_fetch_attempts(REF_ID)
        assert len(attempts) == 3
        retry = attempts[-1]
        assert retry["outcome"] == "stored"
        assert retry["trace"]["frozen_candidate_id"] == ids["eligible"]
        events = _events(repo, ids["eligible"])
        assert events[-1]["event_type"] == "retry_completed"
        assert events[-1]["fetch_attempt_id"] == retry["fetch_attempt_id"]
    finally:
        repo.close()


def test_admitted_deadline_candidate_is_not_eligible_or_selected(tmp_path):
    run_dir = tmp_path / "admitted-deadline"
    refs, resolved, ids, _candidates = _freeze_replay_fixture(run_dir)
    repo = RunRepository.open(str(run_dir))
    try:
        assert [row["event_type"] for row in _events(repo, ids["admitted"])] == [
            "admitted",
            "deadline_skipped",
        ]
        assert repo.list_eligible_frozen_fetch_candidates() == [ids["eligible"]]
        repo.record_frozen_fetch_candidate_event(
            ids["eligible"],
            "retry_invalidated",
            reason_code="test_terminal",
            reason_detail="isolate admitted candidate",
        )
    finally:
        repo.close()
    with mock.patch.object(
        fetch_phase._fetch, "_fetch_frozen_candidate"
    ) as singleton:
        assert _resume(run_dir, refs, resolved) is None
    singleton.assert_not_called()


def test_admitted_without_execution_trace_replays_once_and_is_idempotent(tmp_path):
    run_dir = tmp_path / "admitted-only"
    refs, resolved = _create_run(run_dir)
    candidate = _candidate("https://example.test/admitted-only.pdf")
    repo = RunRepository.open(str(run_dir))
    try:
        candidate_id = repo.freeze_fetch_candidate_stage(
            REF_ID, "published", _context(refs, resolved), [candidate]
        )[0]
        assert repo.admit_frozen_fetch_candidate_batch([candidate_id])
        assert repo.list_eligible_frozen_fetch_candidates() == [candidate_id]
    finally:
        repo.close()

    with mock.patch.object(
        fetch_phase._fetch,
        "_fetch_frozen_candidate",
        side_effect=lambda _ref, _run, _resolved, frozen, ident, **_kwargs: _fetched(
            ident, frozen, "http_error"
        ),
    ) as singleton:
        first = _resume(run_dir, refs, resolved)
        second = _resume(run_dir, refs, resolved)

    assert first is not None and first["status"] == "completed"
    assert second is None
    singleton.assert_called_once()
    repo = RunRepository.open(str(run_dir))
    try:
        attempts = repo.list_fetch_attempts(REF_ID)
        assert len(attempts) == 1
        assert attempts[0]["trace"]["frozen_candidate_id"] == candidate_id
        assert [row["event_type"] for row in _events(repo, candidate_id)] == [
            "admitted", "retry_claimed", "retry_completed",
        ]
    finally:
        repo.close()


def test_two_concurrent_admitted_resumes_issue_one_request(tmp_path):
    run_dir = tmp_path / "concurrent-admitted"
    refs, resolved = _create_run(run_dir)
    candidate = _candidate("https://example.test/concurrent-admitted.pdf")
    repo = RunRepository.open(str(run_dir))
    try:
        candidate_id = repo.freeze_fetch_candidate_stage(
            REF_ID, "published", _context(refs, resolved), [candidate]
        )[0]
        assert repo.admit_frozen_fetch_candidate_batch([candidate_id])
    finally:
        repo.close()

    start = threading.Barrier(3)
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    results: list[dict | None] = []

    def singleton(_ref, _run, _resolved, frozen, ident, **_kwargs):
        calls.append(ident)
        entered.set()
        assert release.wait(5)
        return _fetched(ident, frozen, "http_error")

    def worker() -> None:
        start.wait()
        results.append(_resume(run_dir, refs, resolved))

    with mock.patch.object(
        fetch_phase._fetch, "_fetch_frozen_candidate", side_effect=singleton
    ):
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait()
        assert entered.wait(5)
        release.set()
        for thread in threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in threads)

    assert calls == [candidate_id]
    assert sum(row is None for row in results) == 1
    assert [row["status"] for row in results if row is not None] == ["completed"]


def test_admitted_bound_attempt_stays_excluded_but_unrelated_attempt_does_not(tmp_path):
    run_dir = tmp_path / "admitted-bound"
    refs, resolved = _create_run(run_dir)
    candidates = [_candidate("https://example.test/unbound.pdf"), _candidate("https://example.test/bound.pdf", queue_index=1)]
    repo = RunRepository.open(str(run_dir))
    try:
        candidate_ids = repo.freeze_fetch_candidate_stage(
            REF_ID, "published", _context(refs, resolved), candidates
        )
        assert repo.admit_frozen_fetch_candidate_batch(candidate_ids)
    finally:
        repo.close()
    fetch_phase._record_fetch_attempts(
        str(run_dir), REF_ID, _fetched(candidate_ids[1], candidates[1], "http_error"), resolved[REF_ID]
    )
    repo = RunRepository.open(str(run_dir))
    try:
        assert repo.list_eligible_frozen_fetch_candidates() == [candidate_ids[0]]
    finally:
        repo.close()


def test_admitted_missing_context_invalidates_without_http(tmp_path):
    run_dir = tmp_path / "admitted-missing-context"
    refs, resolved = _create_run(run_dir)
    candidate = _candidate("https://example.test/missing-context.pdf")
    repo = RunRepository.open(str(run_dir))
    try:
        candidate_id = repo.freeze_fetch_candidate_stage(
            REF_ID, "published", _context(refs, resolved), [candidate]
        )[0]
        assert repo.admit_frozen_fetch_candidate_batch([candidate_id])
    finally:
        repo.close()

    with mock.patch.object(fetch_phase._fetch, "_fetch_frozen_candidate") as singleton:
        assert _resume(run_dir, {}, resolved) is None
    singleton.assert_not_called()
    repo = RunRepository.open(str(run_dir))
    try:
        events = _events(repo, candidate_id)
        assert [row["event_type"] for row in events] == [
            "admitted", "retry_invalidated",
        ]
        assert events[-1]["reason_code"] == "missing_context"
    finally:
        repo.close()


def test_admitted_fulltext_present_invalidates_without_http(tmp_path):
    run_dir = tmp_path / "admitted-fulltext"
    refs, resolved = _create_run(run_dir)
    candidate = _candidate("https://example.test/fulltext-present.pdf")
    repo = RunRepository.open(str(run_dir))
    try:
        candidate_id = repo.freeze_fetch_candidate_stage(
            REF_ID, "published", _context(refs, resolved), [candidate]
        )[0]
        assert repo.admit_frozen_fetch_candidate_batch([candidate_id])
        _store_retry_fulltext(repo)
    finally:
        repo.close()

    with mock.patch.object(fetch_phase._fetch, "_fetch_frozen_candidate") as singleton:
        assert _resume(run_dir, refs, resolved) is None
    singleton.assert_not_called()
    repo = RunRepository.open(str(run_dir))
    try:
        events = _events(repo, candidate_id)
        assert [row["event_type"] for row in events] == [
            "admitted", "retry_invalidated",
        ]
        assert events[-1]["reason_code"] == "fulltext_present"
    finally:
        repo.close()


def test_admitted_cooldown_defers_then_reclaims(tmp_path):
    run_dir = tmp_path / "admitted-cooldown"
    refs, resolved = _create_run(run_dir)
    candidate = _candidate("https://example.test/admitted-cooldown.pdf")
    repo = RunRepository.open(str(run_dir))
    try:
        candidate_id = repo.freeze_fetch_candidate_stage(
            REF_ID, "published", _context(refs, resolved), [candidate]
        )[0]
        assert repo.admit_frozen_fetch_candidate_batch([candidate_id])
    finally:
        repo.close()
    deferred = _deferred(candidate_id, candidate)
    completed = _fetched(candidate_id, candidate, "http_error")
    with mock.patch.object(
        fetch_phase._fetch,
        "_fetch_frozen_candidate",
        side_effect=[deferred, completed],
    ):
        first = _resume(run_dir, refs, resolved)
        repo = RunRepository.open(str(run_dir))
        try:
            assert repo.list_eligible_frozen_fetch_candidates() == [candidate_id]
        finally:
            repo.close()
        second = _resume(run_dir, refs, resolved)

    assert first is not None and first["status"] == "rate_limit_deferred"
    assert second is not None and second["status"] == "completed"
    repo = RunRepository.open(str(run_dir))
    try:
        assert [row["event_type"] for row in _events(repo, candidate_id)] == [
            "admitted", "retry_claimed", "retry_deferred", "retry_claimed",
            "retry_completed",
        ]
    finally:
        repo.close()


def test_completed_retry_with_materialized_fulltext_waives_only_untouched_fetch_task(
    tmp_path,
):
    run_dir = tmp_path / "completed-task"
    refs, resolved, ids, candidates = _freeze_replay_fixture(run_dir)
    repo = RunRepository.open(str(run_dir))
    try:
        _create_pending_task(repo, task_id="fetch-r1")
        before = dict(repo.list_task_views(slot="fetch", statuses=("pending",))[0])
    finally:
        repo.close()

    def singleton(_ref, run, _resolved, candidate, candidate_id, **_kwargs):
        worker_repo = RunRepository.open(run)
        try:
            _store_retry_fulltext(worker_repo)
        finally:
            worker_repo.close()
        return _fetched(candidate_id, candidate, "stored")

    with mock.patch.object(fetch_phase._fetch, "_fetch_frozen_candidate", side_effect=singleton):
        result = _resume(run_dir, refs, resolved)
    assert result is not None and result["status"] == "completed"
    assert _pending_handles(run_dir) == []
    repo = RunRepository.open(str(run_dir))
    try:
        after = dict(repo.list_task_views(slot="fetch", statuses=("pending",))[0])
    finally:
        repo.close()
    assert after == before
    assert ids["eligible"] > 0


def test_claimed_retry_with_fulltext_stays_blocking_and_is_reported(tmp_path):
    run_dir = tmp_path / "claimed-task"
    refs, resolved, ids, _candidates = _freeze_replay_fixture(run_dir)
    repo = RunRepository.open(str(run_dir))
    try:
        _store_retry_fulltext(repo)
        _create_pending_task(repo, task_id="fetch-r1")
        assert repo.claim_frozen_fetch_candidate(ids["eligible"])
    finally:
        repo.close()
    assert _pending_handles(run_dir) == ["fetch-r1"]
    fetch_phase._write_manual_fetch_report(str(run_dir), refs, resolved)
    report = (run_dir / "fetch_manual_sources.md").read_text(encoding="utf-8")
    assert "References without full text: **0**" in report
    assert "frozen_retry_claimed_without_completion" in report


def test_ambiguous_claim_vetoes_an_earlier_completed_retry_with_fulltext(tmp_path):
    run_dir = tmp_path / "completed-then-ambiguous"
    refs, resolved, ids, candidates = _freeze_replay_fixture(run_dir)
    ambiguous_candidate = _candidate("https://example.test/ambiguous.pdf")

    repo = RunRepository.open(str(run_dir))
    try:
        ambiguous_id = repo.freeze_fetch_candidate_stage(
            REF_ID,
            "wayback",
            _context(refs, resolved),
            [ambiguous_candidate],
        )[0]
    finally:
        repo.close()
    _record_deadline(run_dir, ambiguous_id, ambiguous_candidate, resolved)

    repo = RunRepository.open(str(run_dir))
    try:
        assert repo.claim_frozen_fetch_candidate(ids["eligible"]) is True
    finally:
        repo.close()
    completed_attempt_id = fetch_phase._record_fetch_attempts(
        str(run_dir),
        REF_ID,
        _fetched(ids["eligible"], candidates["eligible"], "wrong_identity"),
        resolved[REF_ID],
    )[ids["eligible"]]

    repo = RunRepository.open(str(run_dir))
    try:
        repo.record_frozen_fetch_candidate_event(
            ids["eligible"],
            "retry_completed",
            fetch_attempt_id=completed_attempt_id,
        )
        assert repo.claim_frozen_fetch_candidate(ambiguous_id) is True
        _store_retry_fulltext(repo)
        _create_pending_task(repo, task_id="fetch-r1")
    finally:
        repo.close()

    assert _pending_handles(run_dir) == ["fetch-r1"]
    fetch_phase._write_manual_fetch_report(str(run_dir), refs, resolved)
    report = (run_dir / "fetch_manual_sources.md").read_text(encoding="utf-8")
    assert "frozen_retry_claimed_without_completion" in report


def test_invalidated_retry_is_blocking_and_reported(tmp_path):
    run_dir = tmp_path / "invalidated-task"
    refs, resolved, ids, _candidates = _freeze_replay_fixture(run_dir)
    repo = RunRepository.open(str(run_dir))
    try:
        _create_pending_task(repo, task_id="fetch-r1")
        repo.record_frozen_fetch_candidate_event(
            ids["eligible"],
            "retry_invalidated",
            reason_code="context_mismatch",
            reason_detail="test context changed",
        )
    finally:
        repo.close()
    assert _pending_handles(run_dir) == ["fetch-r1"]
    fetch_phase._write_manual_fetch_report(str(run_dir), refs, resolved)
    report = (run_dir / "fetch_manual_sources.md").read_text(encoding="utf-8")
    assert "frozen_retry_invalidated:context_mismatch" in report


def test_completed_retry_without_fulltext_stays_blocking(tmp_path):
    run_dir = tmp_path / "completed-without-fulltext"
    refs, resolved, _ids, candidates = _freeze_replay_fixture(run_dir)
    with mock.patch.object(
        fetch_phase._fetch,
        "_fetch_frozen_candidate",
        side_effect=lambda _ref, _run, _resolved, candidate, candidate_id, **_kwargs: _fetched(
            candidate_id, candidate, "http_error"
        ),
    ):
        result = _resume(run_dir, refs, resolved)
    assert result is not None and result["status"] == "completed"
    repo = RunRepository.open(str(run_dir))
    try:
        _create_pending_task(repo, task_id="fetch-r1")
    finally:
        repo.close()
    assert candidates["eligible"]["url"]
    assert _pending_handles(run_dir) == ["fetch-r1"]

    repo = RunRepository.open(str(run_dir))
    try:
        _store_retry_fulltext(repo)
    finally:
        repo.close()
    assert _pending_handles(run_dir) == ["fetch-r1"]


def test_legacy_and_browser_challenge_tasks_keep_prior_blocking_behavior(tmp_path):
    legacy_dir = tmp_path / "legacy-task"
    _refs, _resolved = _create_run(legacy_dir)
    repo = RunRepository.open(str(legacy_dir))
    try:
        _create_pending_task(repo, task_id="fetch-r1")
    finally:
        repo.close()
    assert _pending_handles(legacy_dir) == ["fetch-r1"]

    challenge_dir = tmp_path / "challenge-task"
    _refs, _resolved, ids, _candidates = _freeze_replay_fixture(challenge_dir)
    repo = RunRepository.open(str(challenge_dir))
    try:
        _store_retry_fulltext(repo)
        _create_pending_task(repo, task_id="challenge-example", kind="browser_challenge")
        assert repo.claim_frozen_fetch_candidate(ids["eligible"])
    finally:
        repo.close()
    assert _pending_handles(challenge_dir) == ["challenge-example"]


def test_lower_frozen_replay_fetches_exactly_once_without_discovery_or_fallbacks(
    tmp_path,
):
    candidate = _candidate("https://example.test/one.pdf")
    original = deepcopy(candidate)
    calls: list[tuple[str, dict]] = []

    class FetchContext:
        def cached_fetch(self, _fetcher, url, **kwargs):
            calls.append((url, dict(kwargs)))
            return {
                "status": 404,
                "body": b"not found",
                "content_type": "text/plain",
                "url": url,
            }

        def remember_challenge_host(self, _host):
            raise AssertionError("challenge state must not be written")

    forbidden = AssertionError("replay regenerated candidates or fallbacks")
    with (
        mock.patch.object(fetch_core, "_candidate_urls", side_effect=forbidden),
        mock.patch.object(
            fetch_core, "_provider_candidate_rows", side_effect=forbidden
        ),
        mock.patch.object(
            fetch_core, "_provider_direct_text_rows", side_effect=forbidden
        ),
        mock.patch.object(fetch_core._perma, "enabled", side_effect=forbidden),
        mock.patch.object(fetch_core._wayback, "enabled", side_effect=forbidden),
        mock.patch.object(
            fetch_core, "_enqueue_landing_candidates", side_effect=forbidden
        ),
    ):
        result = fetch_core._fetch_frozen_candidate(
            {"id": REF_ID, "title": "Frozen paper"},
            str(tmp_path),
            {"status": "resolved"},
            candidate,
            17,
            fetch_context=FetchContext(),
        )
    assert len(calls) == 1
    assert calls[0][0] == candidate["url"]
    assert candidate == original
    attempts = result["fetch_trace"]["execution"]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["frozen_candidate_id"] == 17
    assert attempts[0]["queue_index"] == 0
    assert result["fetch_trace"]["execution"]["candidate_workers"] == 1
