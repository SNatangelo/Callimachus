# tests/test_telemetry_report.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Relational raw-HTTP telemetry: correlation, accounting, and privacy."""

from tests._bootstrap import *  # noqa: F401,F403


class _Response:
    status = 200
    code = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return (
            b'{"ok":true,"usage":{"prompt_cache_hit_tokens":96,'
            b'"prompt_cache_miss_tokens":32}}'
        )


def _create_run(run_dir, *, debug=True):
    from core.infra.db import RunRepository

    repository = RunRepository.create(
        run_dir,
        run_id="telemetry-run",
        input_path="paper.txt",
        input_sha256="a" * 64,
        accuracy="standard",
        style=None,
        model_id="model-1",
        http_profile="default",
        challenge_mode="off",
        fixture_fingerprint="fixture-1",
    )
    repository.set_run_setting("debug_mode", debug)
    return repository


def _attempt(identifier, **overrides):
    payload = {
        "network_attempt_id": identifier,
        "jury_stage": "support_gate",
        "url": "https://api.example.test/chat/completions",
        "attempt": 1,
        "outcome": "response",
        "status": 200,
        "duration_ms": 10.0,
    }
    payload.update(overrides)
    return payload


class TestLlmTelemetry(unittest.TestCase):
    def test_https_transport_adds_certifi_roots_to_strict_default_context(self):
        import ssl
        from unittest import mock
        from core.verify.backends import _chat_transport as compat

        empty_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with mock.patch.object(
            compat.ssl, "create_default_context", return_value=empty_context
        ) as default_context, mock.patch.object(
            compat.urllib.request, "urlopen", return_value=_Response()
        ) as urlopen:
            compat._json_post(
                "https://api.example.test/chat/completions", {"model": "m"}, {}, timeout=1
            )

        default_context.assert_called_once_with()
        self.assertGreater(len(empty_context.get_ca_certs()), 0)
        self.assertEqual(empty_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(empty_context.check_hostname)
        self.assertIs(urlopen.call_args.kwargs["context"], empty_context)

    def test_http_transport_does_not_pass_an_ssl_context(self):
        from unittest import mock
        from core.verify.backends import _chat_transport as compat

        with mock.patch.object(
            compat.urllib.request, "urlopen", return_value=_Response()
        ) as urlopen:
            compat._json_post("http://api.example.test/chat/completions", {}, {}, timeout=1)

        self.assertNotIn("context", urlopen.call_args.kwargs)

    def test_https_transport_fails_closed_when_certifi_bundle_is_invalid(self):
        import ssl
        from unittest import mock
        from core.verify.backends import _chat_transport as compat

        with mock.patch.object(compat.certifi, "where", return_value="missing-ca.pem"), mock.patch.object(
            compat.urllib.request, "urlopen"
        ) as urlopen:
            with self.assertRaises((OSError, ssl.SSLError)):
                compat._json_post(
                    "https://api.example.test/chat/completions", {}, {}, timeout=1
                )

        urlopen.assert_not_called()

    def test_raw_attempt_is_relational_correlated_and_secret_free(self):
        from core.infra.db import RunRepository
        from core.verify.backends import _chat_transport as compat
        from unittest import mock

        with tempfile.TemporaryDirectory() as run_dir:
            repository = _create_run(run_dir)
            repository.close()
            compat.configure_debug_trace(run_dir, True)
            token = compat.set_trace_context(
                logical_request_id="logical-1",
                dispatch_attempt_id="dispatch-1",
                jury_stage="support_gate",
                provider="openai_compatible",
                model="model-1",
                credential_alias="alias-1",
                credential_fingerprint="fingerprint-1",
                lane_id="lane-1",
                started_at_ms=1,
            )
            try:
                with mock.patch.object(
                    compat.urllib.request, "urlopen", return_value=_Response()
                ):
                    compat._json_post(
                        "https://api.example.test/chat/completions?api_key=URL_SECRET",
                        {"model": "model-1"},
                        {
                            "Authorization": "Bearer HEADER_SECRET",
                            "Content-Type": "application/json",
                        },
                        timeout=1,
                    )
            finally:
                compat.reset_trace_context(token)
                compat.configure_debug_trace(None, False)

            repository = RunRepository.open_readonly(run_dir)
            try:
                attempts = repository.list_http_attempts()
            finally:
                repository.close()

            self.assertEqual(len(attempts), 1)
            payload = attempts[0]
            self.assertEqual(payload["logical_request_id"], "logical-1")
            self.assertEqual(payload["dispatch_attempt_id"], "dispatch-1")
            self.assertEqual(payload["jury_stage"], "support_gate")
            self.assertEqual(payload["prompt_cache_hit_tokens"], 96)
            self.assertEqual(payload["prompt_cache_miss_tokens"], 32)
            self.assertTrue(payload["network_attempt_id"])
            serialized = json.dumps(payload)
            self.assertNotIn("HEADER_SECRET", serialized)
            self.assertNotIn("URL_SECRET", serialized)
            self.assertNotIn("Authorization", serialized)
            self.assertFalse(any(name.endswith(".jsonl") for name in os.listdir(run_dir)))

    def test_report_does_not_turn_missing_raw_trace_into_zero_http(self):
        from core.report.render import _http_trace_summary

        with tempfile.TemporaryDirectory() as run_dir:
            repository = _create_run(run_dir)
            repository.close()
            summary = _http_trace_summary(run_dir)

        self.assertTrue(summary["available"])
        self.assertFalse(summary["raw_available"])
        self.assertNotIn("raw_attempts", summary)

    def test_raw_attempt_arithmetic_is_conservative(self):
        from core.report.render import _http_trace_summary

        with tempfile.TemporaryDirectory() as run_dir:
            repository = _create_run(run_dir)
            repository.append_http_attempt(_attempt("network-1"))
            repository.append_http_attempt(_attempt(
                "network-2",
                outcome="http_error",
                status=503,
                retryable=False,
            ))
            repository.close()
            summary = _http_trace_summary(run_dir)

        self.assertEqual(summary["raw_attempts"], 2)
        self.assertEqual(summary["responses"], 1)
        self.assertEqual(summary["transport_errors"], 1)
        self.assertEqual(
            summary["raw_attempts"],
            summary["responses"] + summary["transport_errors"],
        )
        self.assertEqual(summary["accounting_delta"], 0)

    def test_report_aggregates_provider_prompt_cache_accounting(self):
        from core.report.render import _http_trace_summary

        with tempfile.TemporaryDirectory() as run_dir:
            repository = _create_run(run_dir)
            repository.append_http_attempt(_attempt(
                "network-1",
                prompt_cache_hit_tokens=80,
                prompt_cache_miss_tokens=20,
            ))
            repository.append_http_attempt(_attempt(
                "network-2",
                prompt_cache_hit_tokens=40,
                prompt_cache_miss_tokens=60,
            ))
            repository.close()
            summary = _http_trace_summary(run_dir)

        self.assertEqual(summary["prompt_cache_observed_responses"], 2)
        self.assertEqual(summary["prompt_cache_hit_tokens"], 120)
        self.assertEqual(summary["prompt_cache_miss_tokens"], 80)
        self.assertEqual(summary["prompt_cache_hit_rate"], 0.6)

    def test_report_groups_current_jury_stage(self):
        from core.report.render import _http_trace_summary

        with tempfile.TemporaryDirectory() as run_dir:
            repository = _create_run(run_dir)
            repository.append_http_attempt(_attempt("network-1", jury_stage="jury2"))
            repository.close()
            summary = _http_trace_summary(run_dir)

        self.assertEqual(summary["raw_by_stage"], {"jury2": 1})
        self.assertNotIn("unknown", summary["raw_by_stage"])

    def test_http_attempt_storage_rejects_open_payloads_atomically(self):
        with tempfile.TemporaryDirectory() as run_dir:
            repository = _create_run(run_dir)
            with self.assertRaisesRegex(ValueError, "unknown fields"):
                repository.append_http_attempt(_attempt(
                    "network-1", unexpected_payload={"anything": True}
                ))
            self.assertEqual(repository.list_http_attempts(), [])
            repository.close()

    def test_http_attempt_storage_rejects_partial_cache_accounting(self):
        with tempfile.TemporaryDirectory() as run_dir:
            repository = _create_run(run_dir)
            with self.assertRaisesRegex(ValueError, "accounting is incomplete"):
                repository.append_http_attempt(_attempt(
                    "network-1", prompt_cache_hit_tokens=12
                ))
            self.assertEqual(repository.list_http_attempts(), [])
            repository.close()
