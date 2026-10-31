#!/usr/bin/env python3
# tests/test_cli_error_classification.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Test suite for CLI error classification in core.verify.backends.errors.

Covers the `cli_failure_error()` function which maps CLI exit codes and stderr/stdout
to typed BackendError subclasses (RateLimitError, AuthError, TransientError, ConfigError).
This ensures subprocess-based backends apply the same retry policy as HTTP-based ones.
"""

from __future__ import annotations

import json
from tests._bootstrap import *  # noqa: F401,F403

from core.verify.backends import errors


class TestCliErrorClassificationJsonStdout(unittest.TestCase):
    """Test classification when stdout contains structured JSON error info."""

    def test_api_error_status_429_returns_rate_limit_error(self):
        """HTTP 429 in JSON stdout → RateLimitError, retryable=True."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 429,
            "result": "Rate limit exceeded"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.RateLimitError)
        self.assertTrue(exc.retryable)
        self.assertEqual(exc.backend, "claude")
        self.assertIn("Rate limit exceeded", str(exc))

    def test_api_error_status_529_returns_transient_error(self):
        """HTTP 529 (service overloaded) in JSON stdout → TransientError, retryable=True."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 529,
            "result": "Service overloaded"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.TransientError)
        self.assertTrue(exc.retryable)
        self.assertEqual(exc.backend, "claude")
        self.assertIn("Service overloaded", str(exc))

    def test_message_with_overloaded_keyword_returns_transient_error(self):
        """Message containing 'overloaded' (no 529 status) → TransientError."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 503,
            "result": "Server is overloaded, please retry"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.TransientError)
        self.assertTrue(exc.retryable)

    def test_message_with_overload_keyword_returns_transient_error(self):
        """Message containing 'overload' (singular) → TransientError."""
        stdout = json.dumps({
            "is_error": True,
            "result": "API overload detected"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.TransientError)
        self.assertTrue(exc.retryable)

    def test_api_error_status_401_returns_auth_error(self):
        """HTTP 401 (unauthorized) in JSON stdout → AuthError, retryable=False."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 401,
            "result": "Invalid credentials"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.AuthError)
        self.assertFalse(exc.retryable)
        self.assertEqual(exc.backend, "claude")
        self.assertIn("Invalid credentials", str(exc))

    def test_api_error_status_403_returns_auth_error(self):
        """HTTP 403 (forbidden) in JSON stdout → AuthError, retryable=False."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 403,
            "result": "Permission denied"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.AuthError)
        self.assertFalse(exc.retryable)

    def test_api_error_status_404_returns_config_error(self):
        """HTTP 404 (not found) in JSON stdout → ConfigError, retryable=False."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 404,
            "result": "Model not found"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.ConfigError)
        self.assertFalse(exc.retryable)
        self.assertEqual(exc.backend, "claude")
        self.assertIn("Model not found", str(exc))

    def test_api_error_status_400_returns_config_error(self):
        """HTTP 400 (bad request) in JSON stdout → ConfigError, retryable=False."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 400,
            "result": "Bad request"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.ConfigError)
        self.assertFalse(exc.retryable)

    def test_api_error_status_422_returns_config_error(self):
        """HTTP 422 (unprocessable entity) in JSON stdout → ConfigError, retryable=False."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 422,
            "result": "Invalid parameters"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.ConfigError)
        self.assertFalse(exc.retryable)

    def test_generic_5xx_returns_transient_error(self):
        """Generic 5xx status (e.g. 503) in JSON stdout → TransientError, retryable=True."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 503,
            "result": "Service unavailable"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.TransientError)
        self.assertTrue(exc.retryable)

    def test_generic_5xx_500_returns_transient_error(self):
        """HTTP 500 (internal error) in JSON stdout → TransientError, retryable=True."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 500,
            "result": "Internal server error"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.TransientError)
        self.assertTrue(exc.retryable)

    def test_generic_4xx_418_returns_config_error(self):
        """Generic 4xx status (e.g. 418 teapot) in JSON stdout → ConfigError, retryable=False."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 418,
            "result": "I'm a teapot"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.ConfigError)
        self.assertFalse(exc.retryable)

    def test_message_field_extracted_from_stdout_json(self):
        """Message from 'result' field appears in exception text."""
        custom_msg = "Custom error details from API"
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 400,
            "result": custom_msg
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIn(custom_msg, str(exc))

    def test_alternate_status_key_api_error_status(self):
        """Status can be in 'api_error_status' key."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 429,
            "result": "Rate limited"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.RateLimitError)

    def test_alternate_status_key_status(self):
        """Status can be in 'status' key (fallback key)."""
        stdout = json.dumps({
            "is_error": True,
            "status": 429,
            "result": "Rate limited"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.RateLimitError)

    def test_alternate_status_key_code(self):
        """Status can be in 'code' key (fallback key)."""
        stdout = json.dumps({
            "is_error": True,
            "code": 429,
            "result": "Rate limited"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.RateLimitError)

    def test_alternate_message_key_error(self):
        """Message can be in 'error' key."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 400,
            "error": "Missing required field"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIn("Missing required field", str(exc))

    def test_alternate_message_key_message(self):
        """Message can be in 'message' key."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 400,
            "message": "Invalid input"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIn("Invalid input", str(exc))

    def test_alternate_message_key_detail(self):
        """Message can be in 'detail' key."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 400,
            "detail": "Parameter out of range"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIn("Parameter out of range", str(exc))

    def test_backend_name_propagated_from_json(self):
        """Backend name is correctly propagated to exception."""
        stdout = json.dumps({"api_error_status": 429, "result": "Rate limited"})
        exc = errors.cli_failure_error("anthropic-beta", 1, "", stdout=stdout)
        self.assertEqual(exc.backend, "anthropic-beta")
        self.assertIn("anthropic-beta", str(exc))


class TestCliErrorClassificationNonJsonStdout(unittest.TestCase):
    """Test classification when stdout is not JSON or doesn't have recognized status."""

    def test_non_json_stdout_with_auth_hint_in_stderr(self):
        """Non-JSON stdout + stderr with 'unauthorized' → AuthError via stderr heuristic."""
        stdout = "boom"
        stderr = "Error: Unauthorized"
        exc = errors.cli_failure_error("claude", 1, stderr, stdout=stdout)
        self.assertIsInstance(exc, errors.AuthError)
        self.assertFalse(exc.retryable)

    def test_non_json_stdout_with_generic_stderr(self):
        """Non-JSON stdout + generic stderr → TransientError (fallback)."""
        stdout = "something went wrong"
        stderr = "Unknown error occurred"
        exc = errors.cli_failure_error("claude", 1, stderr, stdout=stdout)
        self.assertIsInstance(exc, errors.TransientError)
        self.assertTrue(exc.retryable)

    def test_invalid_json_stdout_falls_back_to_stderr(self):
        """Invalid JSON stdout falls back to stderr heuristics."""
        stdout = "{not valid json"
        stderr = "Authentication failed"
        exc = errors.cli_failure_error("claude", 1, stderr, stdout=stdout)
        self.assertIsInstance(exc, errors.AuthError)
        self.assertFalse(exc.retryable)

    def test_json_array_stdout_falls_back_to_stderr(self):
        """JSON array (not object) falls back to stderr heuristics."""
        stdout = '["error", "occurred"]'
        stderr = "Not logged in"
        exc = errors.cli_failure_error("claude", 1, stderr, stdout=stdout)
        self.assertIsInstance(exc, errors.AuthError)
        self.assertFalse(exc.retryable)


class TestCliErrorClassificationStderrOnly(unittest.TestCase):
    """Test current CLI failures that provide stderr without stdout."""

    def test_stdout_none_with_please_log_in_returns_auth_error(self):
        """stdout=None + stderr 'please log in' → AuthError."""
        stderr = "Please log in to use this service"
        exc = errors.cli_failure_error("claude", 1, stderr, stdout=None)
        self.assertIsInstance(exc, errors.AuthError)
        self.assertFalse(exc.retryable)

    def test_stdout_none_with_not_logged_in_returns_auth_error(self):
        """stdout=None + stderr 'not logged in' → AuthError."""
        stderr = "You are not logged in"
        exc = errors.cli_failure_error("claude", 1, stderr, stdout=None)
        self.assertIsInstance(exc, errors.AuthError)

    def test_stdout_none_with_login_required_returns_auth_error(self):
        """stdout=None + stderr 'login required' → AuthError."""
        stderr = "login required for API access"
        exc = errors.cli_failure_error("claude", 1, stderr, stdout=None)
        self.assertIsInstance(exc, errors.AuthError)

    def test_stdout_none_with_permission_denied_returns_auth_error(self):
        """stdout=None + stderr 'permission denied' → AuthError."""
        stderr = "permission denied"
        exc = errors.cli_failure_error("claude", 1, stderr, stdout=None)
        self.assertIsInstance(exc, errors.AuthError)

    def test_stdout_none_with_generic_stderr_returns_transient_error(self):
        """stdout=None + generic stderr → TransientError."""
        stderr = "Something went wrong"
        exc = errors.cli_failure_error("claude", 1, stderr, stdout=None)
        self.assertIsInstance(exc, errors.TransientError)
        self.assertTrue(exc.retryable)

    def test_stdout_none_default_parameter(self):
        """Calling without stdout parameter defaults to None, uses stderr heuristic."""
        stderr = "Generic error"
        # Call without stdout parameter to test default
        exc = errors.cli_failure_error("claude", 1, stderr)
        self.assertIsInstance(exc, errors.TransientError)
        self.assertTrue(exc.retryable)

    def test_stdout_empty_string_falls_back_to_stderr(self):
        """stdout='' (empty string, falsy) falls back to stderr heuristics."""
        exc = errors.cli_failure_error("claude", 1, "not logged in", stdout="")
        self.assertIsInstance(exc, errors.AuthError)
        self.assertFalse(exc.retryable)


class TestCliErrorClassificationEdgeCases(unittest.TestCase):
    """Test edge cases and corner conditions."""

    def test_returncode_parameter_used_in_fallback_message(self):
        """Fallback message includes returncode when falling back to stderr."""
        stderr = "Generic error"
        exc = errors.cli_failure_error("claude", 42, stderr)
        self.assertIn("rc=42", str(exc))

    def test_stderr_truncated_at_300_chars(self):
        """Error message from stderr is truncated at 300 chars."""
        long_stderr = "x" * 500
        exc = errors.cli_failure_error("claude", 1, long_stderr)
        exc_str = str(exc)
        # The message should contain part of stderr but not the full 500 chars
        self.assertIn("x", exc_str)
        # Should be limited to 300 chars of stderr plus prefix
        self.assertLess(len(exc_str), 500)

    def test_stdout_status_none_falls_back_to_stderr(self):
        """JSON stdout with no recognizable status falls back to stderr."""
        stdout = json.dumps({"is_error": True, "result": "Something failed"})
        stderr = "Please log in"
        exc = errors.cli_failure_error("claude", 1, stderr, stdout=stdout)
        self.assertIsInstance(exc, errors.AuthError)

    def test_message_with_overloaded_case_insensitive(self):
        """'Overloaded' keyword is case-insensitive."""
        stdout = json.dumps({
            "api_error_status": 503,
            "result": "Service is OVERLOADED"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.TransientError)

    def test_auth_hints_case_insensitive(self):
        """Auth hints in stderr are case-insensitive."""
        stderr = "ERROR: UNAUTHORIZED"
        exc = errors.cli_failure_error("claude", 1, stderr)
        self.assertIsInstance(exc, errors.AuthError)

    def test_multiple_status_keys_uses_first_match(self):
        """Multiple status keys: uses first found (api_error_status > status > code)."""
        stdout = json.dumps({
            "api_error_status": 429,
            "status": 500,
            "code": 503
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        # Should use api_error_status (429) since it's checked first
        self.assertIsInstance(exc, errors.RateLimitError)

    def test_multiple_message_keys_uses_first_nonempty(self):
        """Multiple message keys: uses first nonempty found."""
        stdout = json.dumps({
            "api_error_status": 400,
            "result": "This is the result",
            "error": "This is the error",
            "message": "This is the message"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        # Should use 'result' since it's checked first and is nonempty
        self.assertIn("This is the result", str(exc))

    def test_empty_message_skips_to_next_key(self):
        """Empty message string skips to next key in message keys."""
        stdout = json.dumps({
            "result": "",
            "error": "Actual error message",
            "api_error_status": 400
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        # Should skip empty 'result' and use 'error'
        self.assertIn("Actual error message", str(exc))

    def test_none_message_value_skipped(self):
        """None/null message value skips to next key."""
        stdout = json.dumps({
            "result": None,
            "error": "Next message",
            "api_error_status": 400
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIn("Next message", str(exc))

    def test_non_string_message_value_skipped(self):
        """Non-string message value (e.g., int) is skipped."""
        stdout = json.dumps({
            "result": 123,
            "error": "String message",
            "api_error_status": 400
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIn("String message", str(exc))

    def test_non_int_status_value_skipped(self):
        """Non-int status value (e.g., string) is skipped."""
        stdout = json.dumps({
            "api_error_status": "429",
            "status": 529,
            "result": "Service overloaded"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        # Should skip string status and use next int status (529)
        self.assertIsInstance(exc, errors.TransientError)


class TestBackendErrorAttributes(unittest.TestCase):
    """Test BackendError attributes across different exception types."""

    def test_rate_limit_error_has_backend_attribute(self):
        """RateLimitError carries backend attribute."""
        exc = errors.RateLimitError("msg", backend="claude")
        self.assertEqual(exc.backend, "claude")

    def test_rate_limit_error_retryable_always_true(self):
        """RateLimitError.retryable is always True."""
        exc = errors.RateLimitError("msg", backend="claude")
        self.assertTrue(exc.retryable)

    def test_auth_error_retryable_always_false(self):
        """AuthError.retryable is always False."""
        exc = errors.AuthError("msg", backend="claude")
        self.assertFalse(exc.retryable)

    def test_transient_error_retryable_always_true(self):
        """TransientError.retryable is always True."""
        exc = errors.TransientError("msg", backend="claude")
        self.assertTrue(exc.retryable)

    def test_config_error_retryable_always_false(self):
        """ConfigError.retryable is always False."""
        exc = errors.ConfigError("msg", backend="claude")
        self.assertFalse(exc.retryable)

    def test_retry_after_preserved_in_rate_limit(self):
        """RateLimitError can carry retry_after suggestion."""
        exc = errors.RateLimitError("msg", backend="claude", retry_after=60.0)
        self.assertEqual(exc.retry_after, 60.0)

    def test_retry_after_in_transient_error(self):
        """TransientError can carry retry_after suggestion."""
        exc = errors.TransientError("msg", backend="claude", retry_after=30.0)
        self.assertEqual(exc.retry_after, 30.0)

    def test_backend_error_is_runtime_error(self):
        """BackendError is a subclass of RuntimeError."""
        exc = errors.ConfigError("msg", backend="test")
        self.assertIsInstance(exc, RuntimeError)


class TestCliErrorClassificationIntegration(unittest.TestCase):
    """Integration tests matching realistic API error scenarios."""

    def test_claude_api_rate_limit_scenario(self):
        """Realistic scenario: claude CLI returns 429 in JSON stdout."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 429,
            "result": "Rate limit exceeded. Please retry after 60 seconds."
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.RateLimitError)
        self.assertTrue(exc.retryable)
        self.assertIn("Rate limit exceeded", str(exc))

    def test_claude_api_auth_failure_scenario(self):
        """Realistic scenario: claude CLI returns 401 in JSON stdout."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 401,
            "result": "Invalid API key"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.AuthError)
        self.assertFalse(exc.retryable)

    def test_claude_api_server_error_scenario(self):
        """Realistic scenario: claude CLI returns 500 in JSON stdout."""
        stdout = json.dumps({
            "is_error": True,
            "api_error_status": 500,
            "result": "Internal server error"
        })
        exc = errors.cli_failure_error("claude", 1, "", stdout=stdout)
        self.assertIsInstance(exc, errors.TransientError)
        self.assertTrue(exc.retryable)

    def test_cli_without_json_support_stderr_based_fallback(self):
        """A stderr-only CLI failure is still classified deterministically."""
        stderr = "Error: not logged in"
        exc = errors.cli_failure_error("stderr_only_tool", 1, stderr, stdout=None)
        self.assertIsInstance(exc, errors.AuthError)
        self.assertFalse(exc.retryable)

    def test_cli_network_timeout_as_transient(self):
        """Network error without specific status → TransientError."""
        stderr = "Connection timeout"
        exc = errors.cli_failure_error("claude", 1, stderr)
        self.assertIsInstance(exc, errors.TransientError)
        self.assertTrue(exc.retryable)


if __name__ == "__main__":
    unittest.main()
