# tests/test_provider_error_taxonomy.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import os
import socket
import tempfile
import urllib.error
import unittest
from unittest import mock

from core.resolve import service as resolve
from core.resolve import http
from core.resolve import providers


class _Response:
    status = 200

    def __init__(self, body=b"{}"):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _http_error(url, status, headers=None):
    return urllib.error.HTTPError(
        url, status, "failure", headers or {}, None
    )


class TestProviderErrorTaxonomy(unittest.TestCase):
    def test_categories_are_conservative(self):
        url = "https://api.example.test/resource"
        cases = [
            (_http_error(url, 401), "auth"),
            (_http_error(url, 403), "auth"),
            (_http_error(url, 429), "rate_limit"),
            (_http_error(url, 503), "transient_server"),
            (socket.timeout("slow"), "timeout"),
            (OSError("offline"), "network"),
            (MemoryError(), "resource_exhausted"),
        ]
        for exc, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(http.error_category(exc), expected)

    def test_auth_is_not_retried_and_retry_after_is_preserved(self):
        calls = []

        def fake_open(req, timeout=None):
            calls.append(req.full_url)
            raise _http_error(req.full_url, 401)

        with mock.patch.object(http, "open_request", fake_open), \
             mock.patch.object(http, "time") as clock:
            clock.monotonic.return_value = 0.0
            with self.assertRaises(urllib.error.HTTPError) as caught:
                http._get("https://api.example.test/auth")
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(len(calls), 1)
        clock.sleep.assert_not_called()

        # A Retry-After within the cap is honoured verbatim (the host limiter
        # escalates from there); only an extreme value is bounded to the ceiling.
        moderate = _http_error(
            "https://api.example.test/limited", 429, {"Retry-After": "99"}
        )
        self.assertEqual(http._retry_after_seconds(moderate), 99.0)
        extreme = _http_error(
            "https://api.example.test/limited", 429, {"Retry-After": "99999"}
        )
        self.assertEqual(http._retry_after_seconds(extreme), http.MAX_RETRY_AFTER)

    def test_server_errors_are_bounded_and_re_raise_last_error(self):
        calls = []

        def fake_open(req, timeout=None):
            calls.append(req.full_url)
            raise _http_error(req.full_url, 503)

        with mock.patch.object(http, "open_request", fake_open), \
                mock.patch.object(http.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                http._get("https://api.example.test/server")
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(len(calls), http.MAX_ATTEMPTS)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0, 3.0])

    def test_generic_http_500_is_diagnostic_but_not_retried(self):
        calls = []

        def fake_open(req, timeout=None):
            calls.append(req.full_url)
            raise _http_error(req.full_url, 500)

        exc = _http_error("https://api.example.test/server", 500)
        self.assertEqual(http.error_category(exc), "transient_server")
        self.assertFalse(http.is_retryable(exc))
        with mock.patch.object(http, "open_request", fake_open), \
                mock.patch.object(http.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                http._get("https://api.example.test/server")
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_provider_row_exposes_category_without_changing_reported_reason(self):
        class BrokenProvider:
            NAME = "broken"

            @staticmethod
            def candidate_items(**kwargs):
                raise MemoryError("provider allocation limit")

        row = providers._provider_row(BrokenProvider, "candidate_items", {})
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["reason"], "candidate_items raised an exception")
        self.assertEqual(row["error_type"], "resource_exhausted")
        self.assertEqual(row["error_reason"], "resource_exhausted: provider allocation limit")
        self.assertFalse(row["retryable"])

    def test_resolver_dispatch_preserves_caught_http_failure_diagnostics(self):
        class CaughtFailureProvider:
            RESOLVE_NAME = "caught_failure"

            @staticmethod
            def supports(_ref):
                return True

            @staticmethod
            def discover(_ref):
                # This mirrors existing providers that intentionally catch HTTP
                # failures and return an unresolved resolver result.
                return {"status": "unresolved", "reason": "HTTP 503"}

        row = resolve._call_registry_resolver(CaughtFailureProvider, {"title": "Example"})
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["error_type"], "transient_server")
        self.assertEqual(row["error_reason"], "transient_server (HTTP 503)")
        self.assertEqual(row["http_status"], 503)
        self.assertTrue(row["retryable"])

    def test_resolver_dispatch_omits_http_status_for_non_http_exception(self):
        class OfflineProvider:
            RESOLVE_NAME = "offline"

            @staticmethod
            def supports(_ref):
                return True

            @staticmethod
            def discover(_ref):
                raise TimeoutError("offline timeout")

        row = resolve._call_registry_resolver(OfflineProvider, {"title": "Example"})
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["error_type"], "timeout")
        self.assertEqual(row["error_reason"], "timeout: offline timeout")
        self.assertTrue(row["retryable"])
        self.assertNotIn("http_status", row)

    def test_resolver_dispatch_omits_http_status_for_caught_non_http_exception(self):
        class OfflineProvider:
            RESOLVE_NAME = "offline"

            @staticmethod
            def supports(_ref):
                return True

            @staticmethod
            def discover(_ref):
                try:
                    http._get("https://api.example.test/offline")
                except socket.timeout:
                    return {"status": "unresolved", "reason": "provider caught timeout"}
                raise AssertionError("expected timeout")

        with mock.patch.object(http, "open_request", side_effect=socket.timeout("offline timeout")):
            row = resolve._call_registry_resolver(OfflineProvider, {"title": "Example"})
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["error_type"], "timeout")
        self.assertEqual(row["error_reason"], "timeout: offline timeout")
        self.assertTrue(row["retryable"])
        self.assertNotIn("http_status", row)

    def test_resolver_dispatch_rejects_non_mapping_provider_results(self):
        class InvalidProvider:
            RESOLVE_NAME = "invalid"

            @staticmethod
            def supports(_ref):
                return True

            @staticmethod
            def discover(_ref):
                return [{"status": "resolved"}]

        with self.assertRaisesRegex(TypeError, "mapping or None"):
            resolve._call_registry_resolver(InvalidProvider, {"title": "Example"})

    def test_empty_list_after_caught_http_failure_is_a_diagnostic_row(self):
        class SwallowingProvider:
            NAME = "swallowing"

            @staticmethod
            def candidate_items(**kwargs):
                try:
                    http._get("https://api.example.test/limited")
                except urllib.error.HTTPError:
                    return []
                return []

        def fake_open(req, timeout=None):
            raise _http_error(req.full_url, 429)

        with mock.patch.object(http, "open_request", fake_open), \
                mock.patch.object(http.time, "sleep"):
            row = providers._provider_row(SwallowingProvider, "candidate_items", {})
        self.assertEqual(row["items"], [])
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error_type"], "rate_limit")
        self.assertEqual(row["http_status"], 429)
        self.assertTrue(row["retryable"])

    def test_fetch_trace_and_terminal_reason_preserve_provider_diagnostics(self):
        import core.fetch.service as fetch

        direct_rows, candidate_rows = fetch._provider_direct_text_rows, fetch._provider_candidate_rows
        failure = {
            "provider": "core",
            "items": [],
            "status": "error",
            "error": "HTTPError: 401",
            "reason": "candidate_items raised an exception",
            "error_type": "auth",
            "error_reason": "auth (HTTP 401)",
            "retryable": False,
            "http_status": 401,
        }
        try:
            fetch._provider_direct_text_rows = lambda *_args, **_kwargs: []
            fetch._provider_candidate_rows = lambda *_args, **_kwargs: [failure]
            with tempfile.TemporaryDirectory() as run:
                os.makedirs(os.path.join(run, "sources"), exist_ok=True)
                result = fetch.fetch_fulltext(
                    {"id": "r1", "title": "Unlocated work", "source_type": "article"},
                    run,
                    {"status": "unverified", "fulltext_exists": "unknown"},
                )
        finally:
            fetch._provider_direct_text_rows = direct_rows
            fetch._provider_candidate_rows = candidate_rows

        row = result["fetch_trace"]["candidate_generation"]["provider_candidates"][0]
        self.assertEqual(row["error_type"], "auth")
        self.assertEqual(row["error_reason"], "auth (HTTP 401)")
        self.assertFalse(row["retryable"])
        self.assertEqual(row["http_status"], 401)
        self.assertIn("provider failures: core: auth (HTTP 401)", result["reason"])


if __name__ == "__main__":
    unittest.main()
