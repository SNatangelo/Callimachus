# tests/test_tasks_skip_fetch.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from types import SimpleNamespace
from unittest import mock

import pytest

from core.app import run as app_run
from core.app.commands import tasks


def _args():
    return SimpleNamespace(
        run="run-dir",
        agent_identity=None,
        debug_override_artifact_integrity=False,
        debug_override_reason=None,
    )


def _task(task_id, *, status="pending", kind="fetch"):
    return SimpleNamespace(task_id=task_id, status=status, task_kind=kind)


def test_skip_fetch_answers_all_and_only_pending_ordinary_fetch_tasks():
    repo = mock.Mock()
    repo.list_tasks.return_value = [
        _task("fetch:b"),
        _task("fetch:done", status="answered"),
        _task("fetch:a"),
    ]
    with mock.patch.object(tasks, "_repo", return_value=repo), \
         mock.patch.object(tasks, "cmd_answer_fetch", return_value=0) as answer:
        assert tasks.cmd_skip_fetch(_args()) == 0

    repo.list_tasks.assert_called_once_with(slot="fetch")
    repo.close.assert_called_once_with()
    assert [call.args[0].task for call in answer.call_args_list] == ["fetch:a", "fetch:b"]
    assert all(call.args[0].not_found is True for call in answer.call_args_list)
    assert all(call.args[0].file_path is None for call in answer.call_args_list)


def test_skip_fetch_rejects_nonordinary_pending_tasks_before_writing_any_answer():
    repo = mock.Mock()
    repo.list_tasks.return_value = [
        _task("fetch:a"),
        _task("browser:challenge", kind="browser_challenge"),
    ]
    with mock.patch.object(tasks, "_repo", return_value=repo), \
         mock.patch.object(tasks, "cmd_answer_fetch") as answer:
        with pytest.raises(SystemExit, match="non-ordinary"):
            tasks.cmd_skip_fetch(_args())
    answer.assert_not_called()


def test_skip_fetch_leaves_source_identity_attestation_pending(capsys):
    repo = mock.Mock()
    repo.list_tasks.return_value = [_task("verify-identity:abc", kind="source_identity_attestation")]
    with mock.patch.object(tasks, "_repo", return_value=repo), \
         mock.patch.object(tasks, "cmd_answer_fetch") as answer:
        assert tasks.cmd_skip_fetch(_args()) == 0
    answer.assert_not_called()
    assert "1 source-identity review(s) remain" in capsys.readouterr().out


def test_skip_fetch_answers_ordinary_tasks_and_leaves_identity_pending(capsys):
    repo = mock.Mock()
    repo.list_tasks.return_value = [
        _task("fetch:a"),
        _task("verify-identity:abc", kind="source_identity_attestation"),
    ]
    with mock.patch.object(tasks, "_repo", return_value=repo), \
         mock.patch.object(tasks, "cmd_answer_fetch", return_value=0) as answer:
        assert tasks.cmd_skip_fetch(_args()) == 0
    assert [call.args[0].task for call in answer.call_args_list] == ["fetch:a"]
    assert "1 source-identity review(s) remain" in capsys.readouterr().out


def test_skip_fetch_is_a_noop_when_nothing_is_pending():
    repo = mock.Mock()
    repo.list_tasks.return_value = [_task("fetch:done", status="answered")]
    with mock.patch.object(tasks, "_repo", return_value=repo), \
         mock.patch.object(tasks, "cmd_answer_fetch") as answer:
        assert tasks.cmd_skip_fetch(_args()) == 0
    answer.assert_not_called()


def test_guided_proceed_closes_fetch_and_complete_browser_group_as_user_waived():
    repo = mock.Mock()
    repo.list_tasks.return_value = [_task("fetch:a"), _task("browser:b", kind="browser_challenge")]
    repo.task_view.side_effect = [
        {"task_id": "fetch:a", "kind": "fetch"},
        {"task_id": "browser:b", "kind": "browser_challenge", "references": [{"ref_id": "r1"}, {"ref_id": "r2"}]},
    ]
    with mock.patch.object(tasks, "_repo", return_value=repo), \
         mock.patch.object(tasks, "cmd_answer_fetch", return_value=0) as answer:
        assert tasks.cmd_guided_proceed(_args()) == 0
    browser_args, fetch_args = [call.args[0] for call in answer.call_args_list]
    assert fetch_args.not_found is True and fetch_args.guided_fetch is True
    assert browser_args.item_not_found == ["r1", "r2"] and browser_args.guided_fetch is True


def test_guided_proceed_leaves_source_identity_attestation_pending(capsys):
    repo = mock.Mock()
    repo.list_tasks.return_value = [_task("verify-identity:abc", kind="source_identity_attestation")]
    repo.task_view.return_value = {
        "task_id": "verify-identity:abc", "kind": "source_identity_attestation",
    }
    with mock.patch.object(tasks, "_repo", return_value=repo), \
         mock.patch.object(tasks, "cmd_answer_fetch") as answer:
        assert tasks.cmd_guided_proceed(_args()) == 0
    answer.assert_not_called()
    assert "1 source-identity review(s) remain" in capsys.readouterr().out


def test_guided_proceed_closes_retrieval_and_leaves_identity_pending(capsys):
    repo = mock.Mock()
    repo.list_tasks.return_value = [
        _task("fetch:a"),
        _task("verify-identity:abc", kind="source_identity_attestation"),
    ]
    repo.task_view.side_effect = [
        {"task_id": "fetch:a", "kind": "fetch"},
        {"task_id": "verify-identity:abc", "kind": "source_identity_attestation"},
    ]
    with mock.patch.object(tasks, "_repo", return_value=repo), \
         mock.patch.object(tasks, "cmd_answer_fetch", return_value=0) as answer:
        assert tasks.cmd_guided_proceed(_args()) == 0
    assert [call.args[0].task for call in answer.call_args_list] == ["fetch:a"]
    assert "1 source-identity review(s) remain" in capsys.readouterr().out


def test_guided_proceed_rejects_unknown_task_before_writing_any_answer():
    repo = mock.Mock()
    repo.list_tasks.return_value = [_task("fetch:a"), _task("ocr:a", kind="ocr")]
    repo.task_view.side_effect = [
        {"task_id": "fetch:a", "kind": "fetch"},
        {"task_id": "ocr:a", "kind": "ocr"},
    ]
    with mock.patch.object(tasks, "_repo", return_value=repo), \
         mock.patch.object(tasks, "cmd_answer_fetch") as answer:
        with pytest.raises(SystemExit, match="non-retrieval"):
            tasks.cmd_guided_proceed(_args())
    answer.assert_not_called()


def test_resume_no_fetch_overrides_the_persisted_fetch_policy(monkeypatch, tmp_path):
    captured = {}

    monkeypatch.setattr(app_run.atexit, "register", lambda *_args: None)
    monkeypatch.setattr(app_run, "_acquire_run_lock", lambda _run_dir: object())
    monkeypatch.setattr(
        app_run,
        "resolve_existing",
        lambda *_args, **_kwargs: SimpleNamespace(gate=None, assurance=None),
    )
    monkeypatch.setattr(app_run, "_preflight_content_store", lambda _run_dir: None)
    monkeypatch.setattr(app_run, "_debug_directives_from_parse", lambda: (False, []))
    monkeypatch.setattr(
        app_run,
        "_runtime_state_from_repo",
        lambda run_dir: {
            "run_dir": run_dir,
            "phase": "fetch",
            "no_fetch": False,
            "verify_table_citations": False,
        },
    )

    monkeypatch.setattr(app_run, "_claim_driver_session", lambda *_args, **_kwargs: "s1")
    monkeypatch.setattr(app_run, "_save_state", lambda _state: None)
    monkeypatch.setattr(app_run, "_ensure_resume_verification_integrity", lambda _run: None)
    heartbeat = mock.Mock()
    monkeypatch.setattr(app_run, "_start_driver_heartbeat", lambda *_args, **_kwargs: heartbeat)
    monkeypatch.setattr(app_run, "_repo_open", lambda _run: None)
    monkeypatch.setattr(app_run, "_end_driver_session", lambda *_args, **_kwargs: None)

    def capture_drive(state, **_kwargs):
        captured.update(state)
        return 0

    monkeypatch.setattr(app_run, "drive", capture_drive)
    monkeypatch.setattr(
        app_run.sys,
        "argv",
        ["run.py", "--run", str(tmp_path), "--resume", "--no-fetch"],
    )

    with pytest.raises(SystemExit) as stopped:
        app_run.main()

    assert stopped.value.code == 0
    assert captured["phase"] == "fetch"
    assert captured["no_fetch"] is True
