# tests/test_opencode_cli_native.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused isolation and event-protocol tests for the native OpenCode backend."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

import pytest

from core.verify.backends import opencode_cli
from core.verify.backends.errors import AuthError, ConfigError, RateLimitError


_TEST_ROOT = Path(__file__).resolve().parents[1] / ".t" / "opencode-native-tests"


def _auth_file(root: Path, *, provider: str = "opencode-go", entry: dict | None = None) -> Path:
    data = root / "host-data" / "opencode"
    data.mkdir(parents=True, exist_ok=True)
    path = data / "auth.json"
    selected = entry or {"type": "api", "key": "own-opencode-go-key"}
    path.write_text(json.dumps({provider: selected}), encoding="utf-8")
    return path


def _environment(root: Path) -> dict[str, str]:
    _auth_file(root)
    program_data = root / "program-data"
    program_data.mkdir(exist_ok=True)
    return {
        "HOME": str(root / "host-home"),
        "USERPROFILE": str(root / "host-home"),
        "XDG_DATA_HOME": str(root / "host-data"),
        "ProgramData": str(program_data),
    }


def _root():
    _TEST_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="case-", dir=str(_TEST_ROOT))


class _FakeSession:
    instances = []
    event_output = None
    model_output = "opencode-go/model-one\n"
    version_output = "1.19.3\n"

    def __init__(self, argv, environ, *, cwd=None, timeout=None):
        self.argv = list(argv)
        self.environ = dict(environ)
        self.cwd = Path(cwd)
        self.deadline = time.monotonic() + (timeout or 10)
        self.diagnostics = ""
        self.sent = None
        self.process = SimpleNamespace(
            stdin=SimpleNamespace(close=lambda: None), returncode=0,
            wait=lambda **_kwargs: 0,
        )
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def send_text(self, value):
        self.sent = value

    def text_document(self):
        if "--version" in self.argv:
            return self.version_output
        if "models" in self.argv:
            return self.model_output
        return self.event_output or _events()


def _events(*, session="s-1", message="m-1", text="ok", reason="stop") -> str:
    common = {"timestamp": 1, "sessionID": session}
    rows = [
        {**common, "type": "step_start", "part": {
            "id": "p-1", "messageID": message, "sessionID": session, "type": "step-start",
        }},
        {**common, "type": "text", "part": {
            "id": "p-2", "messageID": message, "sessionID": session, "type": "text", "text": text,
        }},
        {**common, "type": "step_finish", "part": {
            "id": "p-3", "messageID": message, "sessionID": session,
            "type": "step-finish", "reason": reason,
        }},
    ]
    return "\n".join(json.dumps(row) for row in rows) + "\n"


@pytest.fixture(autouse=True)
def _fake_sessions(monkeypatch):
    _FakeSession.instances = []
    _FakeSession.event_output = None
    _FakeSession.model_output = "opencode-go/model-one\n"
    _FakeSession.version_output = "1.19.3\n"
    monkeypatch.setattr(opencode_cli, "_Session", _FakeSession)


def test_run_keeps_prompt_verbatim_and_clears_hostile_environment(monkeypatch):
    with _root() as name:
        root = Path(name)
        (root / "AGENTS.md").write_text("Hostile project instruction.", encoding="utf-8")
        (root / "CONTEXT.md").write_text("Hostile context instruction.", encoding="utf-8")
        hostile = _environment(root)
        hostile.update({
            "OPENCODE_CONFIG": str(root / "host-config.json"),
            "OPENCODE_CONFIG_DIR": str(root / "host-config"),
            "OPENCODE_CONFIG_CONTENT": '{"plugin":["hostile-plugin"]}',
            "OPENCODE_AUTH_CONTENT": '{"openai":{"type":"api","key":"third-party"}}',
            "OPENCODE_DISABLE_PROJECT_CONFIG": "0",
            "OPENCODE_PURE": "0",
            "OPENCODE_PLUGIN": "hostile-plugin",
            "OPENAI_API_KEY": "third-party-secret",
            "GEMINI_API_KEY": "third-party-secret",
            "XDG_CONFIG_HOME": str(root / "host-config-root"),
            "OPENCODE_DB": str(root / "host.db"),
        })
        system = "Review citations deterministically.\nKeep this exact text."
        user = "Raw user α & <citation>\nSecond line."

        response = opencode_cli.run_opencode_prompt(
            "opencode.exe", system, user, "opencode-go/model-one",
            timeout=10, environ=hostile, temp_root=root,
        )

        assert response == "ok"
        version, run = _FakeSession.instances
        assert run.sent == user
        assert "--title" in run.argv
        assert "--format" in run.argv and run.argv[run.argv.index("--format") + 1] == "json"
        config = json.loads(run.environ["OPENCODE_CONFIG_CONTENT"])
        assert config["enabled_providers"] == ["opencode-go"]
        assert config["model"] == config["small_model"] == "opencode-go/model-one"
        assert config["agent"][opencode_cli._AGENT]["prompt"] == system
        assert config["agent"][opencode_cli._AGENT]["permission"] == "deny"
        assert "steps" not in config["agent"][opencode_cli._AGENT]
        assert json.loads(run.environ["OPENCODE_AUTH_CONTENT"]) == {
            "opencode-go": {"type": "api", "key": "own-opencode-go-key"},
        }
        assert run.environ["OPENCODE_CONFIG"] == ""
        assert run.environ["OPENCODE_PLUGIN"] == ""
        assert run.environ["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
        assert not (Path(run.environ["OPENCODE_CONFIG_DIR"]) / "AGENTS.md").exists()
        assert run.environ["OPENAI_API_KEY"] == run.environ["GEMINI_API_KEY"] == ""
        assert run.environ["XDG_CONFIG_HOME"] != hostile["XDG_CONFIG_HOME"]
        assert Path(run.environ["XDG_CONFIG_HOME"]).is_relative_to(root)
        assert Path(run.environ["OPENCODE_DB"]).is_relative_to(root)
        assert version.cwd == run.cwd


def test_managed_machine_config_is_rejected_before_any_cli_launch(monkeypatch):
    monkeypatch.setattr(opencode_cli, "sys", SimpleNamespace(platform="win32"))
    with _root() as name:
        root = Path(name)
        environ = _environment(root)
        machine = Path(environ["ProgramData"]) / "opencode"
        machine.mkdir()
        (machine / "opencode.json").write_text('{"plugin":["external"]}', encoding="utf-8")

        with pytest.raises(ConfigError, match="machine-managed"):
            opencode_cli.run_opencode_prompt(
                "opencode.exe", "system", "user", "opencode-go/model-one",
                timeout=10, environ=environ, temp_root=root,
            )
        assert _FakeSession.instances == []


def test_auth_file_symlink_is_rejected():
    with _root() as name:
        root = Path(name)
        environ = _environment(root)
        path = opencode_cli._native_auth_path(opencode_cli._merged_environment(environ))
        target = root / "real-auth.json"
        target.write_text('{"opencode-go":{"type":"api","key":"secret"}}', encoding="utf-8")
        path.unlink()
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("This Windows account cannot create file symlinks.")
        with pytest.raises(opencode_cli._CatalogError, match="ambiguous"):
            opencode_cli._load_native_auth(opencode_cli._merged_environment(environ), "opencode-go")


@pytest.mark.parametrize(
    "raw,match",
    [
        (_events(text=""), "empty or incomplete"),
        (_events(reason="tool-calls"), "normally"),
        (_events().replace('"type": "text"', '"type": "tool_use"', 1), "tool, error"),
        (_events().replace('"sessionID": "s-1", "type": "text"', '"sessionID": "s-2", "type": "text"', 1), "another session"),
        (_events().replace('"messageID": "m-1", "sessionID": "s-1", "type": "text"', '"messageID": "m-2", "sessionID": "s-1", "type": "text"', 1), "another jury message"),
        (_events().rsplit("\n", 2)[0] + "\n", "incomplete jury response"),
    ],
)
def test_protocol_rejects_tool_errors_identity_drift_and_incomplete_output(raw, match):
    with pytest.raises(ConfigError, match=match):
        opencode_cli._parse_response(raw, "opencode-go/model-one")


def test_protocol_rejects_model_drift():
    raw = _events().replace('"type": "text"', '"type": "text", "modelID": "other-model"', 1)
    with pytest.raises(ConfigError, match="pinned model"):
        opencode_cli._parse_response(raw, "opencode-go/model-one")


def test_catalog_uses_native_saved_auth_and_actual_provider_catalog():
    with _root() as name:
        root = Path(name)
        environ = _environment(root)

        result = opencode_cli.check_catalog("C:/fake/opencode.exe", environ, temp_root=root)

        assert result["ok"] is True
        assert result["authenticated"] is None
        assert result["models"] == ["opencode-go/model-one"]
        assert len(_FakeSession.instances) == 2
        catalog = _FakeSession.instances[-1]
        assert catalog.argv[-1] == "opencode-go"
        assert json.loads(catalog.environ["OPENCODE_AUTH_CONTENT"]).keys() == {"opencode-go"}


def test_catalog_does_not_start_cli_without_native_auth():
    with _root() as name:
        root = Path(name)
        environ = {"HOME": str(root / "empty-home"), "XDG_DATA_HOME": str(root / "empty-data")}

        result = opencode_cli.check_catalog("C:/fake/opencode.exe", environ)

        assert result["ok"] is False
        assert result["authenticated"] is False
        assert _FakeSession.instances == []


def test_only_exact_qualified_version_is_accepted():
    with _root() as name:
        root = Path(name)
        environ = _environment(root)
        _FakeSession.version_output = "1.20.0\n"

        with pytest.raises(ConfigError, match="v1.19.3 is required"):
            opencode_cli.run_opencode_prompt(
                "opencode.exe", "system", "user", "opencode-go/model-one",
                timeout=10, environ=environ, temp_root=root,
            )
        assert len(_FakeSession.instances) == 1


def test_catalog_reports_version_failure_without_claiming_invalid_credentials():
    with _root() as name:
        root = Path(name)
        environ = _environment(root)
        _FakeSession.version_output = "1.20.0\n"

        result = opencode_cli.check_catalog("C:/fake/opencode.exe", environ, temp_root=root)

        assert result["ok"] is False
        assert result["authenticated"] is None
        assert result["models"] == []
        assert "v1.19.3 is required" in result["message"]
        assert "own-opencode-go-key" not in repr(result)
        assert len(_FakeSession.instances) == 1


@pytest.mark.parametrize(
    "error,expected",
    [
        ({"name": "ProviderAuthError", "data": {"message": "Unauthorized"}}, AuthError),
        ({"name": "RateLimitError", "data": {"statusCode": 429}}, RateLimitError),
        ({"name": "ModelNotFoundError", "data": {"message": "Model not found"}}, ConfigError),
    ],
)
def test_exit_zero_error_events_map_to_typed_failures(error, expected):
    raw = json.dumps({"type": "error", "sessionID": "s-1", "error": error}) + "\n"
    with pytest.raises(expected):
        opencode_cli._parse_response(raw, "opencode-go/model-one")


@pytest.mark.parametrize("alias", ["auto", "default", "latest", "stable", "auto-fast", "model-latest"])
def test_automatic_model_aliases_are_rejected(alias):
    with pytest.raises(ConfigError, match="pinned model"):
        opencode_cli._validate_model(f"opencode-go/{alias}")
