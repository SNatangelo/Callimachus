# tests/test_guided_fetch_controller.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.app.guided_fetch import GuidedFetchController, source_payload
from core.app.commands import tasks


def _record(task_id: str):
    return SimpleNamespace(task_id=task_id)


class _Repo:
    def __init__(self, views: list[dict]):
        self._views = {item["task_id"]: item for item in views}
        self.closed = False

    def list_pending_tasks(self, *, slot: str):
        assert slot == "fetch"
        return [_record(task_id) for task_id in self._views]

    def task_view(self, task_id: str):
        return self._views[task_id]

    def close(self):
        self.closed = True


def _controller(views: list[dict], admitted: list[tuple[str, dict]]):
    repo = _Repo(views)

    def admit(_run, task_id, payload, **_kwargs):
        admitted.append((task_id, payload))
        return {"answer_id": task_id}

    return GuidedFetchController(
        "run",
        repository_opener=lambda _run: repo,
        admit=admit,
    ), repo


def test_controller_stages_ordinary_file_then_proceeds_without_writing_early():
    admitted: list[tuple[str, dict]] = []
    controller, repo = _controller(
        [{"task_id": "fetch:r1", "kind": "fetch", "ref_id": "r1"}], admitted
    )

    controller.stage_source("r1", source_payload(file_path="/source/r1.pdf"))

    assert admitted == []
    assert repo.closed is True
    result = controller.proceed()
    assert result["proceeded"] is True
    assert admitted == [("fetch:r1", source_payload(file_path="/source/r1.pdf"))]


def test_controller_carries_agent_identity_to_admission_without_operator_bypass():
    repo = _Repo([{"task_id": "fetch:r1", "kind": "fetch", "ref_id": "r1"}])
    calls = []

    def admit(_run, task_id, payload, **kwargs):
        calls.append((task_id, payload, kwargs))
        return {"answer_id": task_id}

    controller = GuidedFetchController(
        "run", agent_identity="codex-agent", repository_opener=lambda _run: repo, admit=admit
    )
    controller.stage_source("r1", source_payload(file_path="/source/r1.pdf"))
    controller.proceed()

    assert calls[0][2]["agent_identity"] == "codex-agent"
    assert "operator_interaction" not in calls[0][2]


def test_controller_preserves_declared_browser_reference_order_with_mixed_answers():
    admitted: list[tuple[str, dict]] = []
    controller, _repo = _controller(
        [{
            "task_id": "browser:one", "kind": "browser_challenge",
            "references": [{"ref_id": "r2"}, {"ref_id": "r1"}],
        }], admitted,
    )
    source = source_payload(
        file_path="/source/r1.html", source_ref="https://example.test/r1"
    )
    controller.stage_source("r1", source)

    controller.proceed()

    assert admitted == [("browser:one", {"items": [
        {"ref_id": "r2", "found": False, "disposition": "user_waived", "guided_fetch": True},
        {"ref_id": "r1", **source},
    ]})]


def test_controller_accepts_confirmed_local_file_for_browser_reference():
    admitted: list[tuple[str, dict]] = []
    controller, _repo = _controller(
        [{
            "task_id": "browser:one", "kind": "browser_challenge",
            "references": [{"ref_id": "r1"}],
        }],
        admitted,
    )
    source = source_payload(file_path="/source/r1.pdf")

    controller.stage_source("r1", source)
    controller.proceed()

    assert admitted == [(
        "browser:one",
        {"items": [{"ref_id": "r1", **source}]},
    )]


def test_controller_proceed_uses_explicit_waivers_for_all_pending_tasks():
    admitted: list[tuple[str, dict]] = []
    controller, _repo = _controller(
        [
            {"task_id": "fetch:r1", "kind": "fetch", "ref_id": "r1"},
            {"task_id": "browser:one", "kind": "browser_challenge", "references": [{"ref_id": "r2"}]},
        ], admitted,
    )

    controller.proceed()

    assert admitted == [
        ("browser:one", {"items": [{"ref_id": "r2", "found": False, "disposition": "user_waived", "guided_fetch": True}]}),
        ("fetch:r1", {"found": False, "disposition": "user_waived", "guided_fetch": True}),
    ]


@pytest.mark.parametrize("tier", ["fulltext", "abstract"])
def test_controller_discarded_source_is_waived_when_proceeding(tier):
    admitted: list[tuple[str, dict]] = []
    controller, _repo = _controller(
        [{"task_id": "fetch:r1", "kind": "fetch", "ref_id": "r1"}], admitted
    )

    controller.stage_source("r1", source_payload(file_path="/source/r1.pdf", tier=tier))

    assert controller.discard_source("r1") is True
    assert controller.discard_source("r1") is False
    result = controller.proceed()

    assert result["waived_ref_ids"] == ["r1"]
    assert admitted == [("fetch:r1", {
        "found": False, "disposition": "user_waived", "guided_fetch": True,
    })]


def test_controller_rejects_staged_source_without_pending_task():
    admitted: list[tuple[str, dict]] = []
    controller, _repo = _controller([], admitted)

    with pytest.raises(ValueError, match="exactly one pending"):
        controller.stage_source("r1", source_payload(file_path="/source/r1.pdf"))
    assert admitted == []


def test_controller_rejects_ambiguous_reference_task_association():
    admitted: list[tuple[str, dict]] = []
    controller, _repo = _controller(
        [
            {"task_id": "fetch:r1", "kind": "fetch", "ref_id": "r1"},
            {"task_id": "browser:one", "kind": "browser_challenge", "references": [{"ref_id": "r1"}]},
        ], admitted,
    )

    with pytest.raises(ValueError, match="exactly one pending"):
        controller.stage_source("r1", source_payload(file_path="/source/r1.pdf"))
    assert admitted == []


def test_admit_guided_payload_rejects_explicit_agent_before_opening_run(monkeypatch):
    opened = []
    monkeypatch.setattr(tasks, "_repo", lambda _run: opened.append(_run))
    monkeypatch.setattr(
        tasks,
        "_answer_gate",
        lambda _args: pytest.fail("agent guided answer must not resolve assurance"),
    )

    with pytest.raises(SystemExit, match="agent-identified caller"):
        tasks.admit_fetch_payload(
            "run", "fetch:r1", source_payload(file_path="/source/r1.pdf"),
            agent_identity="agent-1",
        )
    assert opened == []


def test_controller_translates_agent_guided_admission_rejection_before_mutation(
    monkeypatch,
):
    controller = GuidedFetchController(
        "run",
        agent_identity="codex-agent",
        repository_opener=lambda _run: _Repo(
            [{"task_id": "fetch:r1", "kind": "fetch", "ref_id": "r1"}]
        ),
    )
    controller.stage_source("r1", source_payload(file_path="/source/r1.pdf"))
    monkeypatch.setattr(
        tasks,
        "_answer_gate",
        lambda _args: pytest.fail("agent guided answer must not resolve assurance"),
    )
    monkeypatch.setattr(
        tasks,
        "_repo",
        lambda _run: pytest.fail("agent guided answer must not mutate a task"),
    )

    with pytest.raises(ValueError, match="agent-identified caller"):
        controller.proceed()
    assert controller.proceeded is False


def test_controller_translates_cli_admission_rejection_to_recoverable_error(
    monkeypatch,
):
    controller = GuidedFetchController(
        "run",
        repository_opener=lambda _run: _Repo(
            [{"task_id": "fetch:r1", "kind": "fetch", "ref_id": "r1"}]
        ),
    )
    controller.stage_source("r1", source_payload(file_path="/source/r1.pdf"))
    monkeypatch.setattr(
        tasks,
        "admit_fetch_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("rejected")),
    )

    with pytest.raises(ValueError, match="rejected"):
        controller.proceed()
    assert controller.proceeded is False


def test_controller_retry_keeps_only_sources_not_already_admitted():
    views = [
        {"task_id": "fetch:r1", "kind": "fetch", "ref_id": "r1"},
        {"task_id": "fetch:r2", "kind": "fetch", "ref_id": "r2"},
    ]
    repo = _Repo(views)
    admitted: list[str] = []
    fail_second_once = True

    def admit(_run, task_id, _payload, **_kwargs):
        nonlocal fail_second_once
        admitted.append(task_id)
        if task_id == "fetch:r2" and fail_second_once:
            fail_second_once = False
            raise ValueError("transient rejection")
        repo._views.pop(task_id)
        return {"answer_id": task_id}

    controller = GuidedFetchController(
        "run",
        repository_opener=lambda _run: repo,
        admit=admit,
    )
    controller.stage_source("r1", source_payload(file_path="/source/r1.pdf"))
    controller.stage_source("r2", source_payload(file_path="/source/r2.pdf"))

    with pytest.raises(ValueError, match="transient rejection"):
        controller.proceed()
    result = controller.proceed()

    assert result["proceeded"] is True
    assert admitted == ["fetch:r1", "fetch:r2", "fetch:r2"]


def _identity_view():
    return {
        "task_id": "identity:r1",
        "kind": "source_identity_attestation",
        "ref_id": "r1",
        "target_sha256": "a" * 64,
    }


def test_controller_submits_hash_bound_identity_decision_separately():
    repo = _Repo([_identity_view()])
    admitted = []

    def admit_identity(_run, task_id, **kwargs):
        admitted.append((task_id, kwargs))
        return {"answer_id": "answer-1"}

    controller = GuidedFetchController(
        "run",
        repository_opener=lambda _run: repo,
        admit_identity_review=admit_identity,
    )

    result = controller.submit_identity_decision(
        "r1", "attest_identity", "a" * 64, "The title and author match."
    )

    assert result == {"answer_id": "answer-1"}
    assert admitted == [("identity:r1", {
        "action": "attest_identity",
        "target_sha256": "a" * 64,
        "reason": "The title and author match.",
        "agent_identity": None,
        "debug_override_artifact_integrity": False,
        "debug_override_reason": None,
    })]


def test_controller_identity_decision_rejects_agent_and_changed_target():
    repo = _Repo([_identity_view()])
    admitted = []
    controller = GuidedFetchController(
        "run",
        agent_identity="codex-agent",
        repository_opener=lambda _run: repo,
        admit_identity_review=lambda *_args, **_kwargs: admitted.append(True),
    )

    assert controller.identity_review_allowed is False
    with pytest.raises(ValueError, match="human operator"):
        controller.submit_identity_decision(
            "r1", "attest_identity", "a" * 64, "Looks correct."
        )

    human = GuidedFetchController(
        "run",
        repository_opener=lambda _run: repo,
        admit_identity_review=lambda *_args, **_kwargs: admitted.append(True),
    )
    with pytest.raises(ValueError, match="target hash"):
        human.submit_identity_decision(
            "r1", "keep_unverified", "b" * 64, "Cannot confirm."
        )
    assert admitted == []


def test_controller_proceed_never_waives_pending_identity_review():
    admitted = []
    controller, _repo = _controller([_identity_view()], admitted)

    with pytest.raises(ValueError, match="identity review is pending"):
        controller.proceed()

    assert admitted == []
    assert controller.proceeded is False
