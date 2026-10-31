# tests/test_antigravity_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the native Antigravity adapter with isolated fixture processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

from core.verify.backends import _registry
from core.verify.backends import antigravity_cli as agy
from core.verify.backends.errors import AuthError, BackendTimeoutError, ConfigError

_REAL_HOOK_GUARD = agy._check_hook_guard


_FIXTURE = r'''
import json, os, pathlib, sys, time
mode, kind, transcript, model, agent = sys.argv[1:]

def record(value):
    with open(transcript, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\n")

if kind == "catalog":
    if mode == "catalog_hang":
        print("gemini-4-argon-high  Gemini 4 Argon (High)", flush=True)
        time.sleep(20)
    elif mode == "catalog_oversize":
        print("x" * (600 * 1024), flush=True)
        time.sleep(20)
    elif mode == "catalog_auth":
        print("Sign in to Antigravity. SECRET-CATALOG", flush=True)
        sys.exit(1)
    elif mode == "catalog_warning":
        print("sign-in-required  Please sign in to Antigravity. SECRET-CATALOG", flush=True)
        sys.exit(0)
    elif mode == "catalog_malformed_rows":
        print("--unsafe  Do not include", flush=True)
        sys.exit(0)
    else:
        print("gemini-4-argon-high    Gemini 4 Argon (High)")
        print("gemini-3.8-flash-medium  Gemini 3.8 Flash (Medium)")
        print("Available models:")
        record({"api_key": os.environ.get("GEMINI_API_KEY"),
                "google_api_key": os.environ.get("GOOGLE_API_KEY"),
                "base_url": os.environ.get("GOOGLE_GEMINI_BASE_URL"),
                "vertex": os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"),
                "project": os.environ.get("GOOGLE_CLOUD_PROJECT")})
        sys.exit(0)

init = {
    "cwd": os.getcwd(), "tools": [], "permission_mode": "request-review",
    "model": model, "agent": agent,
}
if mode == "wrong_agent":
    init["agent"] = "other-agent"
elif mode == "missing_agent":
    del init["agent"]
elif mode == "wrong_model":
    init["model"] = "other-model"
elif mode == "missing_model":
    del init["model"]
elif mode == "missing_tools":
    del init["tools"]
elif mode == "tools_nonempty":
    init["tools"] = ["run_command"]
elif mode == "wrong_cwd":
    init["cwd"] = os.path.dirname(os.getcwd())
if mode == "auth_before_init":
    print("Please log in to Antigravity. SECRET-STDERR", file=sys.stderr, flush=True)
    sys.exit(1)
if mode == "no_init":
    time.sleep(20)
elif mode == "malformed_init":
    print("not-json: SECRET-STREAM", flush=True)
else:
    print(json.dumps({"event": "init", "conversation_id": "fixture", "init": init}), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    agent_path = pathlib.Path(".agents/agents/callimachus-jury.md")
    record({"message": message,
            "agent": agent_path.read_bytes().decode("utf-8"),
            "api_key": os.environ.get("GEMINI_API_KEY"),
            "google_api_key": os.environ.get("GOOGLE_API_KEY"),
            "base_url": os.environ.get("GOOGLE_GEMINI_BASE_URL"),
            "vertex": os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"),
            "project": os.environ.get("GOOGLE_CLOUD_PROJECT")})
    if mode == "malformed":
        print("not-json: SECRET-STREAM", flush=True)
        continue
    if mode == "oversize":
        print("x" * (600 * 1024), flush=True)
        time.sleep(20)
        continue
    if mode == "tool_event":
        print(json.dumps({"event": "step_update", "step_update": {
            "state": "DONE", "step_type": "tool", "tool_name": "run_command",
            "tool_info": {"name": "run_command"}}}), flush=True)
        continue
    if mode == "partial":
        print(json.dumps({"event": "step_update", "step_update": {
            "state": "ACTIVE", "step_type": "agent_response", "text_delta": "partial"}}), flush=True)
        time.sleep(20)
        continue
    if mode == "error_result":
        print(json.dumps({"event": "step_update", "step_update": {
            "state": "DONE", "step_type": "user_input"}}), flush=True)
        print(json.dumps({"event": "result", "result": {
            "status": "ERROR", "response": "", "error": "Sign in required SECRET-STREAM"}}), flush=True)
        continue
    response = "" if mode == "empty_result" else "jury answer"
    if mode == "event_model_drift":
        print(json.dumps({"event": "step_update", "model": "different-model", "step_update": {
            "state": "DONE", "step_type": "user_input"}}), flush=True)
        continue
    print(json.dumps({"event": "step_update", "step_update": {
        "state": "DONE", "step_type": "user_input"}}), flush=True)
    print(json.dumps({"event": "step_update", "step_update": {
        "state": "DONE", "step_type": "agent_response", "text_delta": response}}), flush=True)
    result = {"status": "SUCCESS", "response": response}
    if mode == "result_model_drift":
        result["model"] = "different-model"
    print(json.dumps({"event": "result", "result": result}), flush=True)
    if mode == "duplicate_result":
        print(json.dumps({"event": "result", "result": {
            "status": "SUCCESS", "response": "second"}}), flush=True)
    elif mode == "trailing_malformed":
        print("not-json", flush=True)
    elif mode == "nonzero":
        sys.exit(7)
'''


@pytest.fixture
def sandbox():
    # Keep every fixture artifact inside this checkout and remove it on exit.
    with tempfile.TemporaryDirectory(prefix=".agy-test-", dir=Path(__file__).resolve().parent) as directory:
        yield Path(directory)


def _fixture(monkeypatch, sandbox, mode="ok"):
    monkeypatch.setattr(tempfile, "tempdir", str(sandbox))
    script = sandbox / "fixture_agy.py"
    script.write_text(_FIXTURE, encoding="utf-8")
    transcript = sandbox / "events.jsonl"
    sessions, commands, workspaces = [], [], []
    original = agy._Session

    def session(argv, environ, *, cwd=None, timeout=None):
        commands.append(list(argv))
        kind = "catalog" if argv[1:2] == ["models"] else "stream"
        if kind == "stream":
            agent = argv[argv.index("--agent") + 1]
            model = argv[argv.index("--model") + 1]
            workspaces.append(Path(cwd))
        else:
            agent, model = "", ""
        proc_argv = [
            sys.executable, "-u", str(script), mode, kind,
            str(transcript), model, agent,
        ]
        instance = original(proc_argv, environ, cwd=cwd, timeout=timeout)
        sessions.append(instance)
        return instance

    monkeypatch.setattr(agy, "_Session", session)
    monkeypatch.setattr(agy, "_resolve_cli", lambda: "agy")
    monkeypatch.setattr(agy, "_check_hook_guard", lambda *_args: None)
    monkeypatch.setattr(agy, "_require_hook_guard", lambda *_args: None)
    return transcript, sessions, commands, workspaces


def _records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _home_env(sandbox):
    home = sandbox / "home"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "USERPROFILE": str(home)}


def test_registry_contract_and_actual_catalog_models(sandbox, monkeypatch):
    transcript, sessions, commands, _workspaces = _fixture(monkeypatch, sandbox)
    monkeypatch.setenv("GEMINI_API_KEY", "personal-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "personal-google-key")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://invalid.example")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "personal-project")
    spec = _registry.get("antigravity_cli")
    assert spec is not None
    assert spec.env_key == "" and spec.env_model == "ANTIGRAVITY_CLI_MODEL"
    assert spec.requires_model is True and spec.supports_tools is False

    result = agy.check_catalog("agy", _home_env(sandbox))
    assert result["ok"] is True
    assert result["models"] == ["gemini-4-argon-high", "gemini-3.8-flash-medium"]
    assert result["authenticated"] is None
    assert commands == [["agy", "models"]]
    record = _records(transcript)[0]
    assert record == {
        "api_key": "", "google_api_key": "", "base_url": "",
        "vertex": "", "project": "",
    }
    assert sessions[0].process.poll() == 0 and not sessions[0].reader.is_alive()


def test_run_waits_for_valid_init_then_sends_exactly_one_user_event(sandbox, monkeypatch):
    transcript, sessions, commands, workspaces = _fixture(monkeypatch, sandbox)
    monkeypatch.setenv("GEMINI_API_KEY", "personal-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "personal-google-key")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://invalid.example")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "personal-project")
    system = "Keep the supplied system prompt byte-for-byte.\nLine two."
    user = "Check this citation evidence exactly as supplied."
    response = agy._call_antigravity_cli(system, user, "gemini-4-argon-high")
    assert response == "jury answer"
    assert len(commands) == 1
    command = commands[0]
    assert command == [
        "agy", "--agent", "callimachus-jury", "--model", "gemini-4-argon-high",
        "--input-format", "stream-json", "--output-format", "stream-json",
        "--print-timeout", "5m",
    ]
    records = _records(transcript)
    assert len(records) == 1
    assert records[0]["message"] == {
        "event": "user", "message": {"content": user},
    }
    assert records[0]["agent"] == (
        "---\n"
        "name: callimachus-jury\n"
        "description: Callimachus citation verification jury.\n"
        "mainAgent: true\n"
        "tools: []\n"
        "inheritCustomizations: false\n"
        "excludeDefaultComponents: true\n"
        "---\n"
        + system
    )
    assert records[0]["api_key"] == records[0]["google_api_key"] == ""
    assert records[0]["base_url"] == records[0]["vertex"] == records[0]["project"] == ""
    assert len(workspaces) == 1 and not workspaces[0].exists()
    assert sessions[0].process.poll() == 0 and not sessions[0].reader.is_alive()


@pytest.mark.parametrize("mode", [
    "wrong_agent", "missing_agent", "wrong_model", "missing_model",
    "missing_tools", "tools_nonempty", "wrong_cwd", "no_init", "malformed_init",
])
def test_invalid_init_never_sends_evidence_and_cleans_up(sandbox, monkeypatch, mode):
    transcript, sessions, _commands, workspaces = _fixture(monkeypatch, sandbox, mode)
    if mode == "no_init":
        monkeypatch.setattr(agy, "llm_timeout", lambda: 0.2)
    with pytest.raises((ConfigError, BackendTimeoutError)):
        agy._call_antigravity_cli("system", "private evidence", "gemini-4-argon-high")
    assert _records(transcript) == []
    assert len(workspaces) == 1 and not workspaces[0].exists()
    assert sessions[0].process.poll() is not None and not sessions[0].reader.is_alive()


@pytest.mark.parametrize("mode", ["event_model_drift", "result_model_drift"])
def test_later_model_drift_is_rejected(sandbox, monkeypatch, mode):
    transcript, sessions, _commands, workspaces = _fixture(monkeypatch, sandbox, mode)
    with pytest.raises(ConfigError, match="pinned model"):
        agy._call_antigravity_cli("system", "evidence", "gemini-4-argon-high")
    assert len(_records(transcript)) == 1
    assert len(workspaces) == 1 and not workspaces[0].exists()
    assert sessions[0].process.poll() is not None


@pytest.mark.parametrize("mode,error", [
    ("malformed", ConfigError),
    ("tool_event", ConfigError),
    ("partial", BackendTimeoutError),
    ("empty_result", ConfigError),
    ("duplicate_result", ConfigError),
    ("trailing_malformed", ConfigError),
    ("nonzero", ConfigError),
    ("oversize", ConfigError),
    ("error_result", AuthError),
    ("auth_before_init", AuthError),
])
def test_stream_failures_are_typed_redacted_and_cleaned(sandbox, monkeypatch, mode, error):
    transcript, sessions, _commands, workspaces = _fixture(monkeypatch, sandbox, mode)
    if mode == "partial":
        monkeypatch.setattr(agy, "llm_timeout", lambda: 0.2)
    with pytest.raises(error) as caught:
        agy._call_antigravity_cli("system", "private evidence", "gemini-4-argon-high")
    assert "SECRET-STREAM" not in str(caught.value)
    if mode == "auth_before_init":
        assert _records(transcript) == []
    assert len(workspaces) == 1 and not workspaces[0].exists()
    assert sessions[0].process.poll() is not None and not sessions[0].reader.is_alive()


@pytest.mark.parametrize("mode", [
    "catalog_auth", "catalog_warning", "catalog_malformed_rows", "catalog_hang", "catalog_oversize",
])
def test_catalog_auth_failure_and_bounds_are_redacted(sandbox, monkeypatch, mode):
    _transcript, sessions, _commands, _workspaces = _fixture(monkeypatch, sandbox, mode)
    if mode == "catalog_hang":
        monkeypatch.setattr(agy, "_CATALOG_TIMEOUT", 0.2)
    result = agy.check_catalog("agy", _home_env(sandbox))
    assert result["ok"] is False and result["models"] == []
    assert result["authenticated"] is None
    assert "SECRET-CATALOG" not in str(result)
    assert sessions[0].process.poll() is not None and not sessions[0].reader.is_alive()


def test_catalog_rejects_non_agy_and_batch_paths_before_spawn(sandbox, monkeypatch):
    _transcript, sessions, commands, _workspaces = _fixture(monkeypatch, sandbox)
    for path in ("antigravity.exe", "agy.cmd", "agy.bat"):
        result = agy.check_catalog(path, _home_env(sandbox))
        assert result["ok"] is False and result["models"] == []
    assert commands == [] and sessions == []


@pytest.mark.parametrize("case", ["workspace", "global", "settings", "settings_nested", "settings_deep", "duplicate", "malformed", "plugin"])
def test_hook_guard_fails_closed_before_catalog_spawn(sandbox, monkeypatch, case):
    home_env = _home_env(sandbox)
    home = Path(home_env["HOME"])
    workspace = sandbox / "workspace"
    workspace.mkdir()
    if case == "workspace":
        path = workspace / ".agents" / "hooks.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"reminder": {"PreInvocation": [{
            "type": "command", "command": "echo not allowed",
        }]}}), encoding="utf-8")
    elif case == "global":
        path = home / ".gemini" / "config" / "hooks.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"startup": {"SessionStart": [{"command": "echo no"}]}}), encoding="utf-8")
    elif case == "settings":
        path = home / ".gemini" / "antigravity-cli" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"hooks": {"startup": {"PreInvocation": [{"command": "echo no"}]}}}), encoding="utf-8")
    elif case == "settings_nested":
        path = home / ".gemini" / "antigravity-cli" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"cli": {"extensions": {"runtime": {
            "hooks": {"startup": {"PreInvocation": [{"command": "echo no"}]}}
        }}}}), encoding="utf-8")
    elif case == "settings_deep":
        path = home / ".gemini" / "antigravity-cli" / "settings.json"
        path.parent.mkdir(parents=True)
        config = {"hooks": {"startup": {"PreInvocation": [{"command": "echo no"}]}}}
        for _ in range(40):
            config = {"nested": config}
        path.write_text(json.dumps(config), encoding="utf-8")
    elif case == "duplicate":
        path = home / ".gemini" / "config" / "hooks.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"a": {}, "a": {}}', encoding="utf-8")
    elif case == "malformed":
        path = home / ".gemini" / "config" / "hooks.json"
        path.parent.mkdir(parents=True)
        path.write_text("not-json", encoding="utf-8")
    else:
        path = home / ".gemini" / "antigravity-cli" / "plugins" / "plugin-one"
        path.mkdir(parents=True)
        (path / "hooks.json").write_text("{}", encoding="utf-8")

    _transcript, sessions, commands, _workspaces = _fixture(monkeypatch, sandbox)
    monkeypatch.setattr(agy, "_check_hook_guard", _REAL_HOOK_GUARD)
    monkeypatch.chdir(workspace)
    # check_catalog must reject before it reaches the fixture process.
    result = agy.check_catalog("agy", home_env)
    assert result["ok"] is False and result["models"] == []
    assert commands == [] and sessions == []
