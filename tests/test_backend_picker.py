#!/usr/bin/env python3
# tests/test_backend_picker.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Registered model suggestions retained as non-production operator guidance."""
from tests._bootstrap import *  # noqa: F401,F403


class TestModelSuggestions(unittest.TestCase):
    def test_codex_cli_suggestions(self):
        self.assertEqual(
            run._model_suggestions_for("codex_cli"), ("gpt-5-mini", "gpt-5"))

    def test_claude_cli_suggestions(self):
        self.assertEqual(
            run._model_suggestions_for("claude_cli"),
            ("claude-haiku-4-5", "claude-sonnet-5"))

    def test_anthropic_suggestions(self):
        self.assertEqual(
            run._model_suggestions_for("anthropic"),
            ("claude-haiku-4-5", "claude-sonnet-5"))

    def test_gemini_cli_suggestions(self):
        self.assertEqual(
            run._model_suggestions_for("gemini_cli"),
            ("gemini-2.5-flash", "gemini-2.5-pro"))

    def test_unknown_backend_returns_none(self):
        self.assertIsNone(run._model_suggestions_for("something_unknown"))

    def test_all_suggestion_keys_are_registered_backends(self):
        from core.verify.backends._registry import _REGISTRY
        for backend in run._MODEL_SUGGESTIONS:
            self.assertIn(backend, _REGISTRY,
                          f"{backend!r} in _MODEL_SUGGESTIONS is not a registered backend")


if __name__ == "__main__":
    unittest.main()
