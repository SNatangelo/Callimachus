# tests/test_integrity_driver_gate.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from core.app import run as driver
from core.app.commands import tasks
from core.infra.integrity import IntegrityGateError


class _RecordingGate:
    def __init__(self, *, admission_error=False):
        self.calls = []
        self.admission_error = admission_error

    def begin_transition(self, run_dir, *, checkpoint_kind):
        self.calls.append(("begin", run_dir, checkpoint_kind))
        return f"lease-{len(self.calls)}"

    def commit_transition(self, run_dir, lease_id):
        self.calls.append(("commit", run_dir, lease_id))

    def abort_transition(self, run_dir, lease_id, *, reason):
        self.calls.append(("abort", run_dir, lease_id, reason))

    def begin_pipeline_transition(
        self, run_dir, *, checkpoint_kind, mutates_content_store=True
    ):
        self.calls.append(
            ("begin", run_dir, checkpoint_kind, mutates_content_store)
        )
        return f"pipeline-lease-{len(self.calls)}"

    def commit_pipeline_transition(self, run_dir, lease):
        self.calls.append(("commit", run_dir, lease))

    def abort_pipeline_transition(self, run_dir, lease, *, reason):
        self.calls.append(("abort", run_dir, lease, reason))

    def heartbeat_pipeline_transition(self, run_dir, lease):
        self.calls.append(("heartbeat", run_dir, lease))

    def admit_task_answer(self, run_dir, task_id, payload, **kwargs):
        self.calls.append(("admit", run_dir, task_id, payload, kwargs))
        if self.admission_error:
            raise IntegrityGateError("rejected admission")
        return {
            "provenance": {
                "producer_class": "agent",
                "producer_identity": "codex",
            }
        }


def _isolate_driver(monkeypatch):
    monkeypatch.setattr(driver, "_save_state", lambda st: None)
    monkeypatch.setattr(driver, "_repo_mark_phase", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        driver.RunRepository, "session", classmethod(lambda cls, run_dir: nullcontext())
    )
    monkeypatch.setattr(driver._perf, "persist_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(driver._perf, "is_enabled", lambda: False)


def test_task_answer_gate_uses_atomic_admission_without_worker_capability(
    monkeypatch,
):
    resolution = SimpleNamespace(gate=object(), assurance=object())
    calls = []

    def resolve(run_dir, agent_identity, **options):
        calls.append((run_dir, agent_identity, options))
        return resolution

    monkeypatch.setattr(tasks, "resolve_existing", resolve)
    args = SimpleNamespace(
        run="run-dir",
        agent_identity="codex",
        debug_override_artifact_integrity=False,
        debug_override_reason=None,
    )

    assert tasks._answer_gate(args) is resolution
    assert calls == [(
        "run-dir",
        "codex",
        {
            "debug_override": False,
            "override_reason": None,
            "activation_only": True,
            "activate_worker": False,
            "mirror_audit_records": False,
            "allow_local_downgrade": True,
        },
    )]


def test_driver_checkpoints_each_phase_and_completion(monkeypatch, tmp_path):
    _isolate_driver(monkeypatch)
    gate = _RecordingGate()
    monkeypatch.setattr(driver, "PHASE_FN", {"parse": lambda st: "done"})
    state = {"run_dir": str(tmp_path), "phase": "parse"}

    assert driver.drive(state, integrity_gate=gate) == 0

    assert [call[0] for call in gate.calls] == ["begin", "commit", "begin", "commit"]
    assert gate.calls[0][2] == "phase_boundary"
    assert gate.calls[0][3] is False


def test_driver_rotates_content_store_lease_after_each_completed_verify_unit(
    monkeypatch, tmp_path
):
    _isolate_driver(monkeypatch)
    gate = _RecordingGate()

    def phase(state):
        checkpoint = state["_integrity_unit_checkpoint"]
        checkpoint("verify_pair", "pair-1")
        checkpoint("verify_pair", "pair-2")
        return "done"

    monkeypatch.setattr(driver, "PHASE_FN", {"verify": phase})
    state = {"run_dir": str(tmp_path), "phase": "verify"}

    assert driver.drive(state, integrity_gate=gate) == 0

    begins = [call for call in gate.calls if call[0] == "begin"]
    assert [call[2] for call in begins] == [
        "phase_boundary", "unit", "unit", "phase_boundary"
    ]
    assert [call[3] for call in begins] == [True, True, True, False]
    assert "_integrity_unit_checkpoint" not in state
    assert "_active_integrity_lease" not in state


def test_fetch_unit_checkpoints_pair_the_content_store_lease(
    monkeypatch, tmp_path
):
    _isolate_driver(monkeypatch)
    gate = _RecordingGate()

    def phase(state):
        state["_integrity_unit_checkpoint"]("fetch_auto", "r1")
        return "done"

    monkeypatch.setattr(driver, "PHASE_FN", {"fetch": phase})
    state = {
        "run_dir": str(tmp_path),
        "phase": "fetch",
        "fetch_paused": False,
    }

    assert driver.drive(state, integrity_gate=gate) == 0

    begins = [call for call in gate.calls if call[0] == "begin"]
    assert [call[3] for call in begins] == [True, True, False]


def test_driver_aborts_open_lease_when_phase_raises(monkeypatch, tmp_path):
    _isolate_driver(monkeypatch)
    gate = _RecordingGate()

    def fail(_state):
        raise ValueError("broken phase")

    monkeypatch.setattr(driver, "PHASE_FN", {"parse": fail})
    with pytest.raises(ValueError, match="broken phase"):
        driver.drive(
            {"run_dir": str(tmp_path), "phase": "parse"},
            integrity_gate=gate,
        )

    assert [call[0] for call in gate.calls] == ["begin", "abort"]
    assert "phase parse raised ValueError" in gate.calls[-1][-1]


def test_driver_preserves_phase_error_when_abort_fails_and_removes_callback(
    monkeypatch, tmp_path, capsys
):
    _isolate_driver(monkeypatch)

    class AbortFailingGate(_RecordingGate):
        def abort_pipeline_transition(self, run_dir, lease, *, reason):
            super().abort_pipeline_transition(run_dir, lease, reason=reason)
            raise RuntimeError("abort boom")

    def fail(_state):
        raise ValueError("broken phase")

    gate = AbortFailingGate()
    state = {"run_dir": str(tmp_path), "phase": "parse"}
    monkeypatch.setattr(driver, "PHASE_FN", {"parse": fail})

    with pytest.raises(ValueError, match="broken phase") as raised:
        driver.drive(state, integrity_gate=gate)

    assert [call[0] for call in gate.calls] == ["begin", "abort"]
    assert "_integrity_unit_checkpoint" not in state
    notes = getattr(raised.value, "__notes__", ())
    if notes:
        assert any("abort failed" in note for note in notes)
    else:
        assert "abort failed" in capsys.readouterr().err


def test_driver_heartbeat_refreshes_the_active_integrity_lease(
    monkeypatch, tmp_path
):
    beat = threading.Event()

    class Repo:
        def heartbeat_session(self, session_id):
            assert session_id == "session-1"

        def close(self):
            pass

    class Gate:
        def heartbeat_pipeline_transition(self, run_dir, lease):
            assert run_dir == str(tmp_path)
            assert lease == "lease-1"
            beat.set()

    state = {"_active_integrity_lease": "lease-1"}
    monkeypatch.setattr(driver, "_DRIVER_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(driver, "_repo_open", lambda _run_dir: Repo())

    stop = driver._start_driver_heartbeat(
        str(tmp_path),
        "session-1",
        state=state,
        integrity_gate=Gate(),
    )
    try:
        assert beat.wait(1)
    finally:
        stop.set()

    assert "_integrity_heartbeat_error" not in state


def test_crash_recovery_reclaims_the_restored_db_session_immediately(
    monkeypatch, tmp_path
):
    observed = {}

    class Repo:
        def claim_driver_session(self, **kwargs):
            observed.update(kwargs)
            return "session-new"

        def append_phase_event(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(driver, "_repo_open", lambda _run_dir: Repo())
    state = {"run_dir": str(tmp_path), "phase": "verify"}

    assert driver._claim_driver_session(
        state, resumed=True, crash_recovered=True
    ) == "session-new"
    assert observed["stale_after_seconds"] == 0


def test_resume_preflight_runs_before_loading_mutable_runtime_state(
    monkeypatch, tmp_path
):
    events = []

    class StopGate:
        def activate_worker(self, run_dir):
            events.append(("activate", run_dir))
            return {"scope": "worker", "run_dir": run_dir}

        def recover_pipeline_transition(self, run_dir):
            events.append(("recover", run_dir))
            return {"status": "none", "recoveries": []}

        def preflight(self, *args, **kwargs):
            events.append(("preflight", args[0]))
            raise IntegrityGateError("mismatch")

    monkeypatch.setattr(
        driver.RunIntegrityGate,
        "from_environment",
        classmethod(lambda cls: StopGate()),
    )
    monkeypatch.setattr(
        driver,
        "resolve_existing",
        lambda run_dir, *_args, **_kwargs: (
            lambda gate: (
                gate.activate_worker(run_dir),
                SimpleNamespace(gate=gate, assurance=None),
            )[1]
        )(driver.RunIntegrityGate.from_environment()),
    )
    monkeypatch.setattr(
        driver,
        "downgrade_after_authority_failure",
        lambda _run_dir, _assurance, exc: (_ for _ in ()).throw(exc),
    )
    monkeypatch.setattr(
        driver,
        "_runtime_state_from_repo",
        lambda run_dir: pytest.fail("runtime state loaded before integrity preflight"),
    )
    monkeypatch.setattr(
        driver,
        "_acquire_run_lock",
        lambda run_dir: events.append(("lock", run_dir)) or None,
    )
    monkeypatch.setattr(
        driver.sys,
        "argv",
        ["run.py", "--run", str(tmp_path), "--resume"],
    )

    with pytest.raises(SystemExit) as stopped:
        driver.main()

    assert stopped.value.code == 2
    expected = str(tmp_path.resolve())
    assert events == [
        ("lock", expected),
        ("activate", expected),
        ("recover", expected),
        ("preflight", expected),
    ]


def test_resume_defers_debug_database_write_until_startup_lease(
    monkeypatch, tmp_path
):
    events = []

    class StopAfterDebug(RuntimeError):
        pass

    class Gate:
        def activate_worker(self, run_dir):
            events.append(("activate", run_dir))
            return {"scope": "worker", "run_dir": run_dir}

        def recover_pipeline_transition(self, run_dir):
            events.append(("recover", run_dir))
            return {"status": "none", "recoveries": []}

        def preflight(self, run_dir, **_kwargs):
            events.append(("preflight", run_dir))
            return {"status": "clean"}

        def preflight_content_store(self, run_dir, **_kwargs):
            events.append(("preflight_store", run_dir))
            return {"status": "clean"}

        def begin_transition(self, run_dir, *, checkpoint_kind):
            events.append(("begin", run_dir, checkpoint_kind))
            return "startup-lease"

    expected = str(tmp_path.resolve())
    lock_handle = object()
    monkeypatch.setattr(
        driver.RunIntegrityGate,
        "from_environment",
        staticmethod(lambda: Gate()),
    )
    monkeypatch.setattr(
        driver,
        "resolve_existing",
        lambda run_dir, *_args, **_kwargs: (
            lambda gate: (
                gate.activate_worker(run_dir),
                SimpleNamespace(gate=gate, assurance=None),
            )[1]
        )(driver.RunIntegrityGate.from_environment()),
    )
    monkeypatch.setattr(
        driver,
        "_acquire_run_lock",
        lambda run_dir: events.append(("lock", run_dir)) or lock_handle,
    )
    monkeypatch.setattr(driver.atexit, "register", lambda *_args: None)
    monkeypatch.setattr(
        driver,
        "_runtime_state_from_repo",
        lambda run_dir: events.append(("load", run_dir))
        or {
            "run_dir": run_dir,
            "phase": "verify",
            "debug_mode": False,
            "verify_table_citations": False,
        },
    )
    monkeypatch.setattr(
        driver, "_debug_directives_from_parse", lambda: (True, ["api_trace"])
    )

    def stop_after_debug(state):
        events.append(("configure_debug", state["run_dir"]))
        raise StopAfterDebug

    monkeypatch.setattr(driver, "_configure_debug_mode", stop_after_debug)
    monkeypatch.setattr(
        driver.sys,
        "argv",
        ["run.py", "--run", str(tmp_path), "--resume"],
    )

    with pytest.raises(StopAfterDebug):
        driver.main()

    assert events == [
        ("lock", expected),
        ("activate", expected),
        ("recover", expected),
        ("preflight", expected),
        ("preflight_store", expected),
        ("load", expected),
        ("begin", expected, "phase_boundary"),
        ("configure_debug", expected),
    ]


class _TaskRepo:
    def __init__(self):
        self.closed = False

    def get_task(self, task_id):
        return SimpleNamespace(status="pending")

    def task_view(self, task_id):
        return {"kind": "fetch_source"}

    def close(self):
        self.closed = True


def _fetch_args():
    return SimpleNamespace(
        run="run-dir",
        task="fetch-1",
        url=None,
        not_found=True,
        file_path=None,
        text_file=None,
        item_file=None,
        item_text_file=None,
        item_not_found=None,
    )


def test_task_answer_is_committed_inside_external_input_transition(monkeypatch):
    gate = _RecordingGate()
    repo = _TaskRepo()
    monkeypatch.setattr(tasks, "_answer_gate", lambda args: gate)
    monkeypatch.setattr(tasks, "_repo", lambda run_dir: repo)

    assert tasks.cmd_answer_fetch(_fetch_args()) == 0
    assert gate.calls[-1][-1] == {"mirror_checkpoint": False}

    assert [call[0] for call in gate.calls] == ["admit"]
    assert gate.calls[0][3] == {"found": False}


def test_guided_cli_found_source_carries_operator_identity_attestation(monkeypatch):
    gate = _RecordingGate()
    repo = _TaskRepo()
    args = _fetch_args()
    args.not_found = False
    args.file_path = "/controlled/source.pdf"
    args.guided_fetch = True
    args.source_tier = "fulltext"
    args.agent_identity = None
    args.debug_override_artifact_integrity = False
    args.debug_override_reason = None
    monkeypatch.setattr(tasks, "_answer_gate", lambda _args: gate)
    monkeypatch.setattr(tasks, "_repo", lambda _run_dir: repo)

    assert tasks.cmd_answer_fetch(args) == 0
    assert gate.calls[0][3] == {
        "found": True,
        "file_path": "/controlled/source.pdf",
        "guided_fetch": True,
        "identity_attested": True,
    }


def test_guided_agent_answer_rejects_before_assurance_or_task_mutation(monkeypatch):
    monkeypatch.setattr(
        tasks,
        "_answer_gate",
        lambda _args: pytest.fail("agent guided answer must not resolve assurance"),
    )
    monkeypatch.setattr(
        tasks, "_repo", lambda _run: pytest.fail("agent guided answer must not reach task mutation")
    )

    with pytest.raises(SystemExit, match="agent-identified caller"):
        tasks.admit_fetch_payload(
            "run-dir",
            "fetch-1",
            {"found": True, "guided_fetch": True, "identity_attested": True, "file_path": "/x"},
            agent_identity="codex-agent",
        )


def test_guided_standalone_operator_answer_uses_local_provenance(monkeypatch):
    repo = _TaskRepo()
    resolution = SimpleNamespace(assurance=object(), gate=None)
    local_calls = []
    monkeypatch.setattr(tasks, "_answer_gate", lambda _args: resolution)
    monkeypatch.setattr(tasks, "_repo", lambda _run: repo)
    monkeypatch.setattr(
        tasks,
        "admit_task_answer_locally",
        lambda **kwargs: local_calls.append(kwargs) or {"answer_id": "operator-answer"},
    )

    result = tasks.admit_fetch_payload(
        "run-dir",
        "fetch-1",
        {"found": True, "guided_fetch": True, "identity_attested": True, "file_path": "/x"},
    )

    assert result == {"answer_id": "operator-answer"}
    assert local_calls[0]["assurance"] is resolution.assurance
    assert local_calls[0]["agent_identity"] is None


def test_task_answer_reads_readonly_before_authority_admission(monkeypatch):
    gate = _RecordingGate()
    repo = _TaskRepo()

    monkeypatch.setattr(tasks, "_answer_gate", lambda args: gate)
    monkeypatch.setattr(
        tasks.RunRepository,
        "open",
        classmethod(
            lambda cls, run_dir: pytest.fail("task answer must not open the run DB read-write")
        ),
    )
    monkeypatch.setattr(
        tasks.RunRepository,
        "open_readonly",
        classmethod(lambda cls, run_dir: repo),
    )

    assert tasks.cmd_answer_fetch(_fetch_args()) == 0
    assert repo.closed is True
    assert [call[0] for call in gate.calls] == ["admit"]
    assert gate.calls[0][3] == {"found": False}


def test_rejected_task_answer_is_not_written_directly_by_cli(monkeypatch):
    gate = _RecordingGate(admission_error=True)
    repo = _TaskRepo()
    monkeypatch.setattr(tasks, "_answer_gate", lambda args: gate)
    monkeypatch.setattr(tasks, "_repo", lambda run_dir: repo)

    with pytest.raises(SystemExit, match="rejected task answer"):
        tasks.cmd_answer_fetch(_fetch_args())

    assert [call[0] for call in gate.calls] == ["admit"]
