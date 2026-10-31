#!/usr/bin/env python3
# tests/test_cli_backends.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the codex_cli and gemini_cli subprocess LLM backends."""
from tests._bootstrap import *  # noqa: F401,F403

from unittest import mock

from core.verify.backends import codex_cli, gemini_cli


class TestCodexCliBackend(unittest.TestCase):
    def test_available_true_when_codex_on_path(self):
        with mock.patch.object(codex_cli, "find_cli", return_value="/usr/bin/codex"):
            self.assertTrue(codex_cli._available())

    def test_available_false_when_codex_missing(self):
        with mock.patch.object(codex_cli, "find_cli", return_value=None):
            self.assertFalse(codex_cli._available())

    def test_call_writes_prompt_via_stdin_and_reads_output_file(self):
        captured = {}

        def fake_run(cmd, input=None, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = input
            # Simulate codex writing its final message to --output-last-message.
            out_path = cmd[cmd.index("--output-last-message") + 1]
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write("the answer")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(codex_cli.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(codex_cli, "find_cli", return_value="/usr/bin/codex"):
            out = codex_cli._call_codex_cli("sys-prompt", "user-prompt", "gpt-5-codex")

        self.assertEqual(out, "the answer")
        cmd = captured["cmd"]
        self.assertIn("exec", cmd)
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-5-codex")
        self.assertEqual(captured["input"], "sys-prompt\n\nuser-prompt")

    def test_call_without_model_omits_model_flag(self):
        def fake_run(cmd, input=None, **kwargs):
            out_path = cmd[cmd.index("--output-last-message") + 1]
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write("ok")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(codex_cli.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(codex_cli, "find_cli", return_value="/usr/bin/codex"):
            out = codex_cli._call_codex_cli("sys", "usr", None)
        self.assertEqual(out, "ok")

    def test_call_raises_on_nonzero_returncode(self):
        with mock.patch.object(codex_cli.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="boom")), \
             mock.patch.object(codex_cli, "find_cli", return_value="/usr/bin/codex"):
            with self.assertRaises(RuntimeError):
                codex_cli._call_codex_cli("sys", "usr", None)


class TestGeminiCliBackend(unittest.TestCase):
    def test_available_true_when_gemini_on_path(self):
        with mock.patch.object(gemini_cli, "find_cli", return_value="/usr/bin/gemini"):
            self.assertTrue(gemini_cli._available())

    def test_available_false_when_gemini_missing(self):
        with mock.patch.object(gemini_cli, "find_cli", return_value=None):
            self.assertFalse(gemini_cli._available())

    def test_backend_requires_and_forwards_exact_pinned_model(self):
        captured = {}

        def fake_prompt(path, system, user, model, **kwargs):
            captured.update(path=path, system=system, user=user, model=model, kwargs=kwargs)
            return "verified answer"

        with mock.patch.object(gemini_cli, "run_gemini_prompt", side_effect=fake_prompt), \
             mock.patch.object(gemini_cli, "_resolve_cli", return_value="/usr/bin/gemini"), \
             mock.patch.object(gemini_cli, "llm_timeout", return_value=17), \
             mock.patch.object(gemini_cli, "os") as mocked_os:
            mocked_os.environ = {"GEMINI_CLI_MODEL": "gemini-2.5-pro"}
            out = gemini_cli._call_gemini_cli("system", "user", "gemini-2.5-pro")

        self.assertEqual(out, "verified answer")
        self.assertEqual(captured["path"], "/usr/bin/gemini")
        self.assertEqual((captured["system"], captured["user"]), ("system", "user"))
        self.assertEqual(captured["model"], "gemini-2.5-pro")
        self.assertEqual(captured["kwargs"]["timeout"], 17)
        self.assertEqual(gemini_cli.ENV_GEMINI_CLI_MODEL, "GEMINI_CLI_MODEL")

    def test_backend_rejects_missing_and_automatic_model_aliases(self):
        for model in (None, "", "auto", "default", "gemini-2.5-pro-latest"):
            with self.subTest(model=model), self.assertRaises(gemini_cli.ConfigError):
                gemini_cli._validate_model(model)

if __name__ == "__main__":
    unittest.main()
