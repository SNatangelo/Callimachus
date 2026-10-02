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
