#!/usr/bin/env python3
# tests/test_gemini_cli_native.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Offline regression tests for native Gemini CLI transport and catalog."""
from tests._bootstrap import *  # noqa: F401,F403

import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.verify.backends import _gemini_cli_runtime as runtime
from core.verify.backends.errors import AuthError, ConfigError


class _FakeStdin:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(
        self, argv, environ, *, cwd=None, timeout=None, stdout="", frames=(), diagnostics=""
    ):
        self.argv = list(argv)
        self.environ = dict(environ)
        self.cwd = Path(cwd)
        self.timeout = timeout
        self.deadline = time.monotonic() + (timeout or 30)
        self.stdout = stdout
        self.diagnostics = diagnostics
        self.frames = list(frames)
        self.requests = []
        self.closed = False
        settings_path = Path(self.environ["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
        self.settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.settings_path = settings_path
        self.process = SimpleNamespace(stdin=_FakeStdin(), wait=lambda timeout: 0)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def send_text(self, value):
        self.prompt = value
        self.process.stdin.write(value.encode("utf-8"))

    def text_document(self):
        return self.stdout

    def send(self, value):
        self.requests.append(value)

    def frame(self):
        frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        return frame


class GeminiCliNativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.temp_root = Path(self.temp.name)

    def _session_factory(self, *, stdout="", frames=(), returncode=0, diagnostics=""):
        sessions = []

        def create(argv, environ, **kwargs):
            session = _FakeSession(
                argv, environ, stdout=stdout, frames=frames,
                diagnostics=diagnostics, **kwargs,
            )
            session.process.wait = lambda timeout: returncode
            sessions.append(session)
            return session

        return create, sessions

    def test_generation_uses_pinned_model_stdin_and_isolated_oauth_settings(self):
        prompt = "source text " * 6000
        user_env = {
            "HOME": str(self.temp_root / "native-home"),
            "USERPROFILE": str(self.temp_root / "native-home"),
            "GEMINI_CLI_SYSTEM_SETTINGS_PATH": "original-system-settings.json",
            "GEMINI_API_KEY": "gemini-secret",
            "GOOGLE_API_KEY": "google-secret",
            "GOOGLE_GENAI_USE_VERTEXAI": "true",
            "GOOGLE_CLOUD_PROJECT": "personal-project",
            "GEMINI_TELEMETRY_ENABLED": "true",
        }
        native_settings = Path(user_env["HOME"]) / ".gemini" / "settings.json"
        native_settings.parent.mkdir(parents=True)
        native_settings.write_text('{"userSetting":"preserve"}', encoding="utf-8")
        original_settings = native_settings.read_bytes()
        original_env = dict(user_env)
        create, sessions = self._session_factory(stdout='{"response":"grounded answer"}')

        with mock.patch.object(runtime, "_Session", side_effect=create):
            answer = runtime.run_gemini_prompt(
                "gemini", "system instructions", prompt, "gemini-2.5-pro",
                timeout=11, environ=user_env, temp_root=self.temp_root,
            )

        self.assertEqual(answer, "grounded answer")
        session = sessions[0]
        self.assertEqual(session.argv, ["gemini", "-m", "gemini-2.5-pro", "--output-format", "json"])
        self.assertEqual(session.prompt, "system instructions\n\n" + prompt)
        self.assertNotIn(prompt, session.argv)
        self.assertTrue(session.process.stdin.closed)
        self.assertTrue(session.closed)
        self.assertEqual(session.timeout, 11)
        self.assertNotEqual(str(session.cwd), str(self.temp_root))
        self.assertEqual(session.cwd.parent.parent, self.temp_root)
        self.assertFalse(session.cwd.exists())  # temporary workspace was removed
        self.assertEqual(session.environ["GEMINI_API_KEY"], "")
        self.assertEqual(session.environ["GOOGLE_API_KEY"], "")
        self.assertEqual(session.environ["GOOGLE_GENAI_USE_VERTEXAI"], "")
        self.assertEqual(session.environ["GEMINI_CLI_SYSTEM_SETTINGS_PATH"], str(session.settings_path))
        self.assertNotEqual(session.environ["GEMINI_CLI_SYSTEM_SETTINGS_PATH"], original_env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
        self.assertEqual(user_env, original_env)
        self.assertEqual(native_settings.read_bytes(), original_settings)

        settings = session.settings
        self.assertEqual(settings["security"]["auth"], {
            "selectedType": "oauth-personal", "enforcedType": "oauth-personal",
        })
        self.assertEqual(settings["tools"]["core"], [])
        self.assertEqual(settings["mcpServers"], {})
        self.assertFalse(settings["admin"]["mcp"]["enabled"])
        self.assertFalse(settings["admin"]["extensions"]["enabled"])
        self.assertFalse(settings["admin"]["skills"]["enabled"])
        self.assertFalse(settings["hooksConfig"]["enabled"])
        self.assertFalse(settings["skills"]["enabled"])
        self.assertFalse(settings["experimental"]["enableAgents"])
        self.assertFalse(settings["context"]["includeDirectoryTree"])
        self.assertFalse(settings["context"]["loadMemoryFromIncludeDirectories"])
        self.assertRegex(settings["context"]["fileName"], r"^CALLIMACHUS_NO_CONTEXT_[0-9a-f]{32}\.md$")

    def test_generation_rejects_plaintext_empty_tool_use_and_model_drift(self):
        cases = (
            ("not JSON", ConfigError),
            ('{"response":"  "}', RuntimeError),
            ('{"response":"answer","stats":{"tools":{"totalCalls":1}}}', ConfigError),
            ('{"response":"answer","stats":{"models":{"gemini-2.5-flash":1}}}', ConfigError),
        )
        for output, error_type in cases:
            with self.subTest(output=output):
                create, _sessions = self._session_factory(stdout=output)
                with mock.patch.object(runtime, "_Session", side_effect=create):
                    with self.assertRaises(error_type):
                        runtime.run_gemini_prompt(
                            "gemini", "system", "user", "gemini-2.5-pro",
                            timeout=11, environ={}, temp_root=self.temp_root,
                        )

    def test_generation_sanitizes_native_auth_errors(self):
        create, _sessions = self._session_factory(
            stdout='{"error":{"type":"AuthRequired","message":"private detail secret"}}',
            returncode=1,
        )
        with mock.patch.object(runtime, "_Session", side_effect=create):
            with self.assertRaises(AuthError) as raised:
                runtime.run_gemini_prompt(
                    "gemini", "system", "user", "gemini-2.5-pro",
                    timeout=11, environ={}, temp_root=self.temp_root,
                )
        self.assertNotIn("private detail", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_generation_detects_auth_failure_reported_only_on_stderr(self):
        create, _sessions = self._session_factory(
            stdout="", returncode=1,
            diagnostics="Authentication required: private detail secret",
        )
        with mock.patch.object(runtime, "_Session", side_effect=create):
            with self.assertRaises(AuthError) as raised:
                runtime.run_gemini_prompt(
                    "gemini", "system", "user", "gemini-2.5-pro",
                    timeout=11, environ={}, temp_root=self.temp_root,
                )
        self.assertNotIn("private detail", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_catalog_uses_acp_oauth_and_returns_models_without_prompt_or_tools(self):
        frames = (
            {"jsonrpc": "2.0", "id": 1, "result": {
                "protocolVersion": 1,
                "authMethods": [{"id": "oauth-personal", "name": "Google Login"}],
            }},
            {"jsonrpc": "2.0", "id": 2, "result": {"models": {
                "availableModels": [
                    {"modelId": "gemini-2.5-pro"},
                    {"modelId": "gemini-2.5-flash"},
                    {"modelId": "auto"},
                ],
            }}},
        )
        create, sessions = self._session_factory(frames=frames)
        with mock.patch.object(runtime, "_Session", side_effect=create):
            result = runtime.check_catalog("gemini", {}, temp_root=self.temp_root)

        self.assertTrue(result["ok"])
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["models"], ["gemini-2.5-pro", "gemini-2.5-flash"])
        session = sessions[0]
        self.assertEqual(session.argv, ["gemini", "--acp"])
        self.assertEqual([request["method"] for request in session.requests], ["initialize", "session/new"])
        self.assertEqual(session.requests[0]["params"]["clientCapabilities"], {})
        self.assertEqual(session.requests[1]["params"]["mcpServers"], [])
        self.assertNotIn("authenticate", [request["method"] for request in session.requests])
        self.assertNotIn("session/prompt", [request["method"] for request in session.requests])
        self.assertTrue(session.closed)

    def test_catalog_fails_closed_for_auth_error_empty_catalog_or_interaction_request(self):
        cases = (
            ((
                {"id": 1, "error": {"code": -32000, "message": "authentication required"}},
            ), False),
            ((
                {"id": 1, "result": {"protocolVersion": 1, "authMethods": [{"id": "oauth-personal"}]}},
                {"id": 2, "result": {"models": {"availableModels": []}}},
            ), None),
            ((
                {"id": 1, "result": {"protocolVersion": 1, "authMethods": [{"id": "oauth-personal"}]}},
                {"id": 99, "method": "session/request_permission", "params": {}},
            ), None),
        )
        for frames, expected_auth in cases:
            with self.subTest(expected_auth=expected_auth, frames=frames):
                create, _sessions = self._session_factory(frames=frames)
                with mock.patch.object(runtime, "_Session", side_effect=create):
                    result = runtime.check_catalog("gemini", {}, temp_root=self.temp_root)
                self.assertFalse(result["ok"])
                self.assertEqual(result["models"], [])
                self.assertEqual(result["authenticated"], expected_auth)

    def test_catalog_detects_auth_failure_reported_only_on_stderr(self):
        create, _sessions = self._session_factory(
            diagnostics="Authentication required: private detail secret",
            frames=(runtime._CatalogError("The CLI closed before returning its catalog."),),
        )
        with mock.patch.object(runtime, "_Session", side_effect=create):
            result = runtime.check_catalog("gemini", {}, temp_root=self.temp_root)
        self.assertFalse(result["ok"])
        self.assertFalse(result["authenticated"])
        self.assertNotIn("private detail", result["message"])
        self.assertNotIn("secret", result["message"])


if __name__ == "__main__":
    unittest.main()
