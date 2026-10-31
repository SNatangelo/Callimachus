# tests/test_llm_cli_discovery.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise actual bounded pipes with fixture processes, never a real provider."""
import errno
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from core.app.providers import cli_discovery as discovery


_SERVER = r'''
import json, sys, time
mode, kind, transcript = sys.argv[1:]
if mode == "hang":
    time.sleep(20)
if mode == "oversize":
    print("x" * (512 * 1024 + 1), flush=True)
    time.sleep(20)
if kind == "auth":
    print(json.dumps({"authMethod": "none" if mode == "logged_out" else "claude.ai"}, indent=2), flush=True)
    sys.exit(0)
for line in sys.stdin:
    request = json.loads(line)
    with open(transcript, "a", encoding="utf-8") as log:
        log.write(json.dumps(request) + "\n")
    if kind == "claude":
        if mode == "malformed":
            print("not json: SECRET-ERROR", flush=True)
        elif mode == "provider_error":
            print(json.dumps({"type":"control_response","response":{
                "subtype":"error","request_id":"catalog_init","error":"SECRET-ERROR"}}), flush=True)
        else:
            print(json.dumps({"type":"control_response","response":{
                "subtype":"success","request_id":"catalog_init","response":{
                    "models":[{"value":"claude-test"},{"value":"--unsafe"}]}}}), flush=True)
        continue
    method = request.get("method")
    if method == "initialized":
        continue
    if method == "initialize":
        response = {}
    elif method == "account/read":
        response = {"account": None if mode in {"logged_out", "keyless"} else {
            "type":"chatgpt","email":"private-email","token":"SECRET-ERROR"}, "requiresOpenaiAuth":mode != "keyless"}
    elif method == "model/list":
        cursor = request["params"].get("cursor")
        response = {"data": [{"id":"display-only","model":"test-b" if cursor else "test-a"},
                             {"model":"hidden-model","hidden":True}, {"model":"--unsafe"}],
                    "nextCursor":"page2" if not cursor else None}
        if mode == "loop":
            response["nextCursor"] = "page2"
        if mode == "empty":
            response = {"data":[],"nextCursor":None}
    else:
        raise RuntimeError("Unexpected method")
    if mode == "provider_error":
        print(json.dumps({"id":request["id"],"error":{"message":"SECRET-ERROR"}}), flush=True)
    elif mode == "malformed":
        print("not json: SECRET-ERROR", flush=True)
    else:
        print(json.dumps({"id":request["id"],"result":response}), flush=True)
'''


def _fixture_cli(tmp_path, monkeypatch, provider, mode="ok"):
    script = tmp_path / "catalog_server.py"
    script.write_text(_SERVER, encoding="utf-8")
    transcript = tmp_path / "requests.jsonl"
    sessions, commands = [], []
    original = discovery._Session

    def session(argv, environ):
        commands.append(argv)
        kind = "auth" if argv[1:3] == ["auth", "status"] else (
            "codex" if provider == "codex_cli" else "claude"
        )
        instance = original(
            [sys.executable, "-u", str(script), mode, kind, str(transcript)], environ,
        )
        sessions.append(instance)
        return instance

    monkeypatch.setattr(discovery, "_Session", session)
    monkeypatch.setattr(discovery, "find_cli", lambda command, *args: None if command.endswith(".exe") else command)
    return transcript, commands, sessions


def _requests(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_codex_catalog_checks_auth_and_pages_without_a_generation(tmp_path, monkeypatch):
    path, commands, sessions = _fixture_cli(tmp_path, monkeypatch, "codex_cli")
    result = discovery.cli_catalog("codex_cli", {})
    assert result["ok"] is True and result["authenticated"] is True
    assert result["models"] == ["test-a", "test-b"]
    requests = _requests(path)
    assert [row["method"] for row in requests] == [
        "initialize", "initialized", "account/read", "model/list", "model/list",
    ]
    assert requests[2]["params"] == {"refreshToken": False}
    assert requests[-1]["params"]["cursor"] == "page2"
    assert commands == [["codex", "app-server"]]
    assert all(item.process.poll() is not None and not item.reader.is_alive() for item in sessions)
    assert "SECRET-ERROR" not in str(result) and "private-email" not in str(result)


def test_claude_catalog_initializes_without_prompt_tools_or_hooks(tmp_path, monkeypatch):
    path, commands, sessions = _fixture_cli(tmp_path, monkeypatch, "claude_cli")
    result = discovery.cli_catalog("claude_cli", {})
    assert result["ok"] is True and result["models"] == ["claude-test"]
    assert commands[0] == ["claude", "auth", "status"]
    assert commands[1][commands[1].index("--tools") + 1] == ""
    assert "--strict-mcp-config" in commands[1]
    assert '{"disableAllHooks":true}' in commands[1]
    requests = _requests(path)
    assert requests == [{"type":"control_request","request_id":"catalog_init",
                         "request":{"subtype":"initialize","hooks":None}}]
    assert all(item.process.poll() is not None for item in sessions)


def test_codex_keyless_provider_can_return_models_without_claiming_login(tmp_path, monkeypatch):
    _fixture_cli(tmp_path, monkeypatch, "codex_cli", "keyless")
    result = discovery.cli_catalog("codex_cli", {})
    assert result["ok"] is True and result["authenticated"] is False
    assert result["models"] == ["test-a", "test-b"]


@pytest.mark.parametrize("provider", ["codex_cli", "claude_cli"])
def test_logged_out_cli_returns_no_jury_options(tmp_path, monkeypatch, provider):
    path, _commands, _sessions = _fixture_cli(tmp_path, monkeypatch, provider, "logged_out")
    result = discovery.cli_catalog(provider, {})
    assert result["ok"] is False and result["installed"] is True
    assert result["authenticated"] is False and result["models"] == []
    assert not any(row.get("method") == "model/list" for row in _requests(path))


@pytest.mark.parametrize("provider", ["codex_cli", "claude_cli"])
@pytest.mark.parametrize("mode", ["provider_error", "malformed", "hang", "oversize"])
def test_cli_failure_is_bounded_redacted_and_process_is_stopped(tmp_path, monkeypatch, provider, mode):
    _path, _commands, sessions = _fixture_cli(tmp_path, monkeypatch, provider, mode)
    if mode == "hang":
        monkeypatch.setattr(discovery, "_TIMEOUT", 0.2)
    result = discovery.cli_catalog(provider, {})
    assert result["ok"] is False and result["models"] == []
    assert "SECRET-ERROR" not in str(result)
    assert all(item.process.poll() is not None and not item.reader.is_alive() for item in sessions)


@pytest.mark.parametrize("mode", ["loop", "empty"])
def test_codex_rejects_unusable_or_repeating_catalog(tmp_path, monkeypatch, mode):
    _fixture_cli(tmp_path, monkeypatch, "codex_cli", mode)
    result = discovery.cli_catalog("codex_cli", {})
    assert result["ok"] is False and result["models"] == []


def test_absent_cli_does_not_spawn_or_claim_authentication(monkeypatch):
    monkeypatch.setattr(discovery, "find_cli", lambda *args, **kwargs: None)
    monkeypatch.setattr(discovery, "_Session", lambda *args: pytest.fail("must not spawn"))
    result = discovery.cli_catalog("codex_cli", {})
    assert result["installed"] is False and result["authenticated"] is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
@pytest.mark.parametrize("launcher_exits", [False, True])
@pytest.mark.parametrize("blocked_request", [False, True])
def test_catalog_timeout_stops_descendants_holding_inherited_pipes(
    launcher_exits, blocked_request,
):
    script = (
        "import json, subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); "
        "print(json.dumps({'ready': True}), flush=True); "
        + ("" if launcher_exits else "time.sleep(5)")
    )
    started = time.monotonic()
    with pytest.raises(discovery._CatalogError, match="timed out"):
        with discovery._Session([sys.executable, "-u", "-c", script], {}, timeout=0.5) as session:
            assert session.frame() == {"ready": True}
            if launcher_exits:
                session.process.wait(timeout=1)
            if blocked_request:
                session.send_text("x" * (1024 * 1024))
            else:
                session.document()
    assert time.monotonic() - started < 2
    assert session.process.poll() is not None
    assert not session.reader.is_alive()
    assert not session.error_reader.is_alive()
    assert all(not writer.is_alive() for writer in session.writers)
    assert all(stream.closed for stream in (
        session.process.stdin, session.process.stdout, session.process.stderr,
    ))


def test_windows_teardown_does_not_close_pipes_owned_by_busy_threads(monkeypatch):
    class BusyThread:
        def join(self, timeout):
            assert timeout <= 0.3

        def is_alive(self):
            return True

    def unexpected_close():
        pytest.fail("closing a busy buffered pipe can block teardown")

    session = discovery._Session.__new__(discovery._Session)
    session.closing = threading.Event()
    session.reader = BusyThread()
    session.error_reader = BusyThread()
    session.writers = [BusyThread()]
    pipe = SimpleNamespace(close=unexpected_close)
    session.process = SimpleNamespace(poll=lambda: 0, stdin=pipe, stdout=pipe, stderr=pipe)
    monkeypatch.setattr(discovery, "os", SimpleNamespace(name="nt"))
    session.__exit__()
    assert session.closing.is_set()


def test_closed_cli_request_error_is_preserved_during_pipe_cleanup():
    with pytest.raises(discovery._CatalogError, match="closed before accepting"):
        with discovery._Session([sys.executable, "-S", "-c", "pass"], {}, timeout=2) as session:
            session.process.wait(timeout=1)
            session.send_text("x" * 10)
    assert session.process.stdin.closed


@pytest.mark.skipif(os.name == "nt", reason="Darwin process-group zombie behavior")
def test_finished_child_is_reaped_before_group_signal_on_darwin():
    if not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT"):
        pytest.skip("waitid(WNOWAIT) is required to observe an unreaped child")
    session = discovery._Session(
        [sys.executable, "-S", "-c", "import json; print(json.dumps({'ok': True}), flush=True)"],
        {}, timeout=2,
    )
    assert session.document() == {"ok": True}
    session.reader.join(timeout=1)
    session.error_reader.join(timeout=1)
    assert not session.reader.is_alive() and not session.error_reader.is_alive()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        status = os.waitid(
            os.P_PID, session.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
        if status is not None and status.si_pid == session.process.pid:
            break
        time.sleep(0.01)
    else:
        pytest.fail("CLI child did not exit within the test bound")
    assert session.process.returncode is None
    session.__exit__(None, None, None)
    assert session.process.returncode == 0
    assert all(not thread.is_alive() for thread in (
        session.reader, session.error_reader, *session.writers,
    ))


def test_group_permission_race_preserves_body_error_after_reap_and_empty_probe(monkeypatch):
    code = "import json, sys; print(json.dumps({'ok': True}), flush=True); sys.stdin.read(1)"
    session = discovery._Session([sys.executable, "-S", "-c", code], {}, timeout=2)
    signals = []
    raced = False

    def race_exit_then_report_eprem(pgid, sig):
        nonlocal raced
        signals.append(sig)
        if sig == 15 and not raced:
            raced = True
            session.process.stdin.write(b"x")
            session.process.stdin.flush()
            assert session.process.wait(timeout=1) == 0
            raise PermissionError(errno.EPERM, "simulated exit during group signal")
        raise ProcessLookupError(errno.ESRCH, "simulated empty Darwin group")

    monkeypatch.setattr(
        discovery, "os", SimpleNamespace(name="posix", killpg=race_exit_then_report_eprem),
    )
    monkeypatch.setattr(discovery, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    with pytest.raises(discovery._CatalogError, match="body failure"):
        with session:
            assert session.frame() == {"ok": True}
            raise discovery._CatalogError("catalog body failure")
    assert raced and 0 in signals
    assert session.process.returncode == 0
    assert not session.reader.is_alive() and not session.error_reader.is_alive()


def test_live_group_permission_error_remains_a_failure_after_bounded_cleanup(monkeypatch):
    session = discovery._Session(
        [sys.executable, "-S", "-c", "import time; time.sleep(20)"], {}, timeout=2,
    )

    def denied_group_signal(_pgid, _sig):
        raise PermissionError(errno.EPERM, "simulated live-group permission denial")

    monkeypatch.setattr(
        discovery, "os", SimpleNamespace(name="posix", killpg=denied_group_signal),
    )
    monkeypatch.setattr(discovery, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    with pytest.raises(PermissionError, match="live-group permission denial"):
        session.__exit__(None, None, None)
    assert session.process.poll() is not None
    assert not session.reader.is_alive() and not session.error_reader.is_alive()
