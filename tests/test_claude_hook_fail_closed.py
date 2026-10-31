# tests/test_claude_hook_fail_closed.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused fail-closed contract tests for the Claude Code hooks."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import ntpath
import os
import pathlib
import posixpath
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

from tests._bootstrap import *  # noqa: F401,F403


ROOT = pathlib.Path(__file__).resolve().parents[1]
ARM = ROOT / "adapters/claude-code/hooks/citation_arm.py"
STOP = ROOT / "adapters/claude-code/hooks/citation_verify_stop.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _platform_os(name, path):
    return SimpleNamespace(**{**vars(os), "name": name, "path": path})


def _run_main(module, payload):
    out = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(payload)), contextlib.redirect_stdout(out):
        with __import__("unittest").TestCase().assertRaises(SystemExit) as raised:
            module.main()
    assert raised.exception.code == 0
    return json.loads(out.getvalue())


class TestClaudeHookFailClosed:
    def test_arm_guidance_uses_hook_runtime_and_quotes_placeholder(self):
        arm = _load("citation_arm_command", ARM)
        with mock.patch.object(arm, "os", _platform_os("posix", posixpath)), \
             mock.patch.object(arm.sys, "executable", "/usr/bin/python3"):
            command = arm._run_command("--input", "<manuscript>", "--autonomous")
        assert command.startswith("/usr/bin/python3 ")
        assert command.endswith("--input '<manuscript>' --autonomous")

    def test_arm_live_guidance_uses_the_runtime_command(self):
        arm = _load("citation_arm_live_command", ARM)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(arm, "os", _platform_os("posix", posixpath)), \
             mock.patch.object(arm.sys, "executable", "/usr/bin/python3"), \
             mock.patch.object(arm.sys, "stdin", io.StringIO(json.dumps({
                 "cwd": tmp, "prompt": "verify these citations",
             }))), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            with __import__("unittest").TestCase().assertRaises(SystemExit) as raised:
                arm.main()
        assert raised.exception.code == 0
        assert "`python run.py" not in out.getvalue()
        assert "--input '<manuscript>' --autonomous" in out.getvalue()

    def test_stop_guidance_uses_hook_runtime_and_quotes_windows_placeholder(self):
        stop = _load("citation_verify_stop_command", STOP)
        with mock.patch.object(stop, "os", _platform_os("nt", ntpath)), \
             mock.patch.object(stop.sys, "executable", r"C:\Program Files\Python\python.exe"):
            command = stop._run_command(r"C:\repo", "--input", "<manuscript>")
        assert command == (
            r'& "C:\Program Files\Python\python.exe" C:\repo\run.py --input "<manuscript>"'
        )

    def test_arm_rejects_malformed_and_non_object_json(self, monkeypatch):
        monkeypatch.delenv("CITATION_VERIFIER_STATE_DIR", raising=False)
        for payload in ("{", "[]"):
            with tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run([sys.executable, str(ARM)], input=payload, text=True,
                                        capture_output=True, cwd=tmp, check=False)
                assert result.returncode == 0
                assert "Hook error" in result.stdout
                with open(os.path.join(tmp, ".citation_verifier", "pending.json")) as f:
                    assert json.load(f)["manuscript"] is None

    def test_arm_uses_configured_state_dir_for_error_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "hook-state")
            env = os.environ | {"CITATION_VERIFIER_STATE_DIR": state}
            result = subprocess.run([sys.executable, str(ARM)], input="{", text=True,
                                    capture_output=True, cwd=tmp, env=env, check=False)
            assert result.returncode == 0
            assert os.path.isfile(os.path.join(state, "pending.json"))
            assert not os.path.exists(os.path.join(tmp, ".citation_verifier", "pending.json"))

    def test_arm_reports_controlled_failure_when_error_sentinel_cannot_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "not-a-directory")
            with open(blocker, "w", encoding="utf-8"):
                pass
            env = os.environ | {"CITATION_VERIFIER_STATE_DIR": os.path.join(blocker, "child")}
            result = subprocess.run([sys.executable, str(ARM)], input="{", text=True,
                                    capture_output=True, cwd=tmp, env=env, check=False)
        assert result.returncode == 1
        assert "could not persist" in result.stdout
        assert "Traceback" not in result.stderr

    def test_stop_rejects_malformed_and_non_object_json(self):
        stop = _load("citation_verify_stop_input", STOP)
        for payload in ("{", "[]"):
            decision = _run_main(stop, payload)
            assert decision["decision"] == "block"

    def test_stop_rejects_invalid_sentinel_shape(self, monkeypatch):
        monkeypatch.delenv("CITATION_VERIFIER_STATE_DIR", raising=False)
        stop = _load("citation_verify_stop_sentinel", STOP)
        for sentinel in ([], {"manuscript": []}):
            with tempfile.TemporaryDirectory() as tmp:
                state = os.path.join(tmp, ".citation_verifier")
                os.mkdir(state)
                with open(os.path.join(state, "pending.json"), "w") as f:
                    json.dump(sentinel, f)
                decision = _run_main(stop, json.dumps({"cwd": tmp}))
            assert decision["decision"] == "block"
            assert "invalid" in decision["reason"]

    def test_none_manuscript_never_accepts_unrelated_signed_run(self):
        stop = _load("citation_verify_stop_none", STOP)
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, ".citation_verifier")
            os.mkdir(state)
            with open(os.path.join(state, "pending.json"), "w") as f:
                json.dump({"manuscript": None}, f)
            with mock.patch.object(stop, "_pkg_dir", return_value=tmp), \
                 mock.patch.object(stop, "_runs", side_effect=AssertionError("must not scan runs")):
                decision = _run_main(stop, json.dumps({"cwd": tmp}))
        assert decision["decision"] == "block"
        assert "no manuscript" in decision["reason"]

    def test_verify_rejects_non_json_non_bool_and_exit_mismatch(self):
        stop = _load("citation_verify_stop_verify", STOP)
        for response in ((0, "not json", ""),
                         (0, json.dumps([]), ""),
                         (0, json.dumps({"ok": "true"}), ""),
                         (1, json.dumps({"ok": True}), "failed")):
            with mock.patch.object(stop, "_cli", return_value=response):
                assert stop._verify("pkg", "runs/x")["ok"] is False
        with mock.patch.object(stop, "_cli", side_effect=OSError("runner unavailable")):
            assert stop._verify("pkg", "runs/x")["ok"] is False
        with mock.patch.object(stop, "_cli", return_value=(
                0, json.dumps({"ok": True, "failures": [], "info": {}}), "")):
            assert stop._verify("pkg", "runs/x")["ok"] is True

    def test_stop_operational_error_is_a_block_decision(self):
        stop = _load("citation_verify_stop_error", STOP)
        with mock.patch.object(stop, "_pkg_dir", side_effect=OSError("disk error")):
            decision = _run_main(stop, json.dumps({"cwd": "/workspace"}))
        assert decision["decision"] == "block"
        assert "hook error" in decision["reason"]
