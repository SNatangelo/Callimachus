# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.app.parse_review import ParseReviewController

from core.infra.db import RunRepository

def _paused_parse_run(tmp_path):
    repo = RunRepository.create(
        str(tmp_path), run_id="parse-review", input_path="paper.pdf",
        input_sha256="x", accuracy="standard", style=None, model_id=None,
        http_profile=None, challenge_mode=None, fixture_fingerprint="f",
    )
    repo.replace_parse_payload(
        claims=[{"claim_id": "c1", "sentence": "A concrete claim."}],
        references=[{
            "ref_id": "r1", "ref_number": 1,
            "raw_entry": "Smith (2020). Source title.",
        }],
        citations=[{
            "claim_id": "c1", "marker_raw": "(Smyth 2020)",
            "surname": "smyth", "year": 2020,
            "candidate_ref_ids": ["r1"],
        }],
    )
    occurrence = repo.parse_payload()["ambiguities"][0]["occurrence_id"]
    repo.create_manual_parse_review(
        task_id="review", review_kind="citation_reference_review",
        target_id=occurrence,
        candidates=[{"id": "r1", "origin": "parser", "score": 1.0}],
        instructions="Review the citation.",
    )
    target_hash = repo.get_task("review").task_payload["target_sha256"]
    repo.close()
    return target_hash

def test_parse_review_controller_projects_readable_closed_choices(tmp_path):
    target_hash = _paused_parse_run(tmp_path)
    controller = ParseReviewController(str(tmp_path))

    assert controller.pending_count() == 1
    tasks = controller.load_tasks()
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "review"
    assert tasks[0]["target_sha256"] == target_hash
    assert tasks[0]["claim_text"] == "A concrete claim."
    assert tasks[0]["candidates"][0]["id"] == "r1"
    assert tasks[0]["candidates"][0]["label"] == "[1] Smith (2020). Source title."

def test_parse_review_controller_admits_operator_choice_through_gate(tmp_path, monkeypatch):
    target_hash = _paused_parse_run(tmp_path)
    calls = []

    class Gate:
        def admit_task_answer(self, run_dir, task_id, payload, *, mirror_checkpoint):
            calls.append((run_dir, task_id, payload, mirror_checkpoint))
            return {"answer_id": "answer-1"}

    monkeypatch.setattr(
        "core.app.parse_review.resolve_existing",
        lambda *_args, **_kwargs: SimpleNamespace(gate=Gate()),
    )
    controller = ParseReviewController(str(tmp_path))
    answer = controller.submit_decision(
        "review", {
            "action": "select_reference", "target_sha256": target_hash,
            "ref_id": "r1",
        },
    )

    assert answer == {"answer_id": "answer-1"}
    assert calls == [(
        str(tmp_path), "review", {
            "action": "select_reference", "target_sha256": target_hash,
            "ref_id": "r1", "reason": "",
        }, False,
    )]
    with pytest.raises(ValueError, match="unavailable"):
        controller.submit_decision("missing", {"action": "keep_unresolved"})
    assert len(calls) == 1

def test_skip_remaining_reviews_uses_stable_order_and_preserves_answered_tasks(tmp_path, monkeypatch):
    citation_hash = _paused_parse_run(tmp_path)
    repo = RunRepository.open(str(tmp_path))
    try:
        repo.create_manual_parse_review(
            task_id="identity-pending", review_kind="reference_identity_review",
            target_id="r1", instructions="Review identity.",
        )
        repo.create_manual_parse_review(
            task_id="identity-answered", review_kind="reference_identity_review",
            target_id="r1", instructions="Review identity.",
        )
        answered_hash = repo.get_task("identity-answered").task_payload["target_sha256"]
        repo.submit_task_answer(
            task_id="identity-answered", actor_type="user",
            raw_payload={
                "action": "keep_ambiguous", "target_sha256": answered_hash,
                "reason": "Already reviewed.",
            },
        )
        pending = repo.list_pending_tasks(slot="parse_review")
        expected = [
            (task.task_id, task.task_payload["target_sha256"])
            for task in pending
        ]
        assert {task_id for task_id, _target_hash in expected} == {
            "review", "identity-pending",
        }
        saved_answer_id = repo.get_latest_task_answer("identity-answered").answer_id
    finally:
        repo.close()

    calls = []

    class Gate:
        def admit_task_answer(self, run_dir, task_id, payload, *, mirror_checkpoint):
            calls.append((run_dir, task_id, payload, mirror_checkpoint))
            return {"answer_id": f"skip-answer-{len(calls)}"}

    monkeypatch.setattr(
        "core.app.parse_review.resolve_existing",
        lambda *_args, **_kwargs: SimpleNamespace(gate=Gate()),
    )
    answers = ParseReviewController(str(tmp_path)).skip_remaining_reviews()

    assert [call[1] for call in calls] == [task_id for task_id, _hash in expected]
    assert [call[2] for call in calls] == [
        {
            "action": "skip_review", "target_sha256": target_hash,
            "reason": "Operator explicitly skipped the remaining manual Parse reviews.",
        }
        for _task_id, target_hash in expected
    ]
    assert [answer["answer_id"] for answer in answers] == [
        "skip-answer-1", "skip-answer-2",
    ]
    assert len({answer["answer_id"] for answer in answers}) == len(answers)

    repo = RunRepository.open(str(tmp_path))
    try:
        answered = repo.get_task("identity-answered")
        assert answered.status == "answered"
        assert repo.get_latest_task_answer("identity-answered").answer_id == saved_answer_id
        assert repo.get_task("review").task_payload["target_sha256"] == citation_hash
    finally:
        repo.close()

def test_skip_review_answer_requires_nonempty_reason_and_current_hash(tmp_path):
    target_hash = _paused_parse_run(tmp_path)
    repo = RunRepository.open(str(tmp_path))
    try:
        with pytest.raises(ValueError, match="reason"):
            repo.submit_task_answer(
                task_id="review", actor_type="user",
                raw_payload={
                    "action": "skip_review", "target_sha256": target_hash,
                    "reason": "  ",
                },
            )
        with pytest.raises(ValueError, match="target hash"):
            repo.submit_task_answer(
                task_id="review", actor_type="user",
                raw_payload={
                    "action": "skip_review", "target_sha256": "0" * 64,
                    "reason": "Operator skipped review.",
                },
            )
        assert repo.get_task("review").status == "pending"
    finally:
        repo.close()
