#!/usr/bin/env python3
# tests/test_security_network_consent.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Isolated regressions for the signed network-consent control channel."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import threading
import unittest
from unittest.mock import patch


_ROOT = pathlib.Path(__file__).resolve().parents[1]
_KEY = "0123456789abcdef" * 4


def _load_network_consent():
    name = "_network_consent_under_test"
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    path = _ROOT / "core" / "fetch" / "transport" / "network_consent.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the network consent module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _CaptureStream:
    def __init__(self):
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._parts = []

    def write(self, value):
        with self._lock:
            self._parts.append(value)
            self._ready.set()
        return len(value)

    def flush(self):
        pass

    def wait_for_request(self):
        if not self._ready.wait(timeout=2):
            raise AssertionError("broker did not emit a request frame")
        with self._lock:
            return "".join(self._parts)


def _scope(**overrides):
    scope = {
        "origin": "https://private.example:443",
        "connection_host": "private.example",
        "connection_port": 443,
        "addresses": ["192.168.1.20"],
        "via_proxy": False,
    }
    scope.update(overrides)
    return scope


class NetworkConsentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.consent = _load_network_consent()

    def setUp(self):
        self.consent.configure("invalid", None, None)

    def tearDown(self):
        self.consent.configure("invalid", None, None)

    def _start_request(self, stream, scope=None):
        result = {}

        def run():
            result["allowed"] = self.consent.request_permission(scope or _scope())

        thread = threading.Thread(target=run)
        thread.start()
        line = stream.wait_for_request()
        return thread, result, line

    def _request_payload(self, line):
        framed = next(
            item
            for item in line.splitlines()
            if item.startswith(self.consent.REQUEST_PREFIX)
        )
        payload = self.consent.decode_message(_KEY, framed)
        self.assertIsNotNone(payload)
        return payload

    def _decision(self, request_payload, *, choice="once", allowed=True, **overrides):
        payload = {
            "type": "network_decision",
            "request_id": request_payload["request_id"],
            "allowed": allowed,
            "choice": choice,
            "permission_id": self.consent.scope_key(request_payload["scope"]),
        }
        payload.update(overrides)
        return self.consent.encode_message(_KEY, payload)

    def test_canonical_scope_normalizes_origin_ips_and_requires_exact_fields(self):
        scope = _scope(
            origin="HTTPS://Private.Example:0443",
            connection_host="PRIVATE.example.",
            addresses=["2001:0db8::1", "192.168.1.1"],
        )
        canonical = self.consent.canonical_scope(scope)
        self.assertEqual(canonical["origin"], "https://private.example:443")
        self.assertEqual(canonical["connection_host"], "private.example")
        self.assertEqual(canonical["addresses"], ["192.168.1.1", "2001:db8::1"])
        self.assertEqual(
            self.consent.scope_key(scope), self.consent.scope_key(canonical)
        )

        for invalid in (
            {key: value for key, value in _scope().items() if key != "via_proxy"},
            {**_scope(), "extra": True},
            _scope(connection_port=True),
            _scope(connection_port=0),
            _scope(addresses=[]),
            _scope(addresses=["publisher.example"]),
            _scope(addresses=["192.168.001.1"]),
            _scope(addresses=["fe80::1%3"]),
            _scope(via_proxy=1),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.consent.canonical_scope(invalid)

    def test_signed_messages_round_trip_and_invalid_mac_or_oversize_fails_closed(self):
        payload = {"type": "probe", "text": "caf\u00e9", "count": 2}
        encoded = self.consent.encode_message(_KEY, payload)
        self.assertTrue(encoded.startswith(self.consent.REQUEST_PREFIX))
        self.assertNotIn(_KEY, encoded)
        self.assertEqual(self.consent.decode_message(_KEY, encoded), payload)
        self.assertIsNone(self.consent.decode_message("f" * 64, encoded))
        tampered = encoded.replace('"count":2', '"count":3')
        self.assertNotEqual(tampered, encoded)
        self.assertIsNone(self.consent.decode_message(_KEY, tampered))
        self.assertIsNone(self.consent.decode_message(_KEY, "x" * (16 * 1024 + 1)))
        self.assertIsNone(self.consent.decode_message(_KEY, "not a frame"))

        with self.assertRaises(ValueError):
            self.consent.encode_message("short", payload)

    def test_permission_defaults_to_deny_without_broker_even_with_environment_key(self):
        with patch.dict("os.environ", {self.consent.CONTROL_ENV: _KEY}):
            self.assertFalse(self.consent.request_permission(_scope()))

    def test_once_decision_is_signed_scoped_audited_and_grants_only_matching_request(self):
        stream = _CaptureStream()
        audited = []
        self.consent.configure(_KEY, stream, audited.append)
        thread, result, line = self._start_request(stream)
        request = self._request_payload(line)
        self.assertEqual(request["type"], "network_request")
        self.assertEqual(request["scope"], self.consent.canonical_scope(_scope()))
        self.assertNotIn(_KEY, line)

        decision = self._decision(request, choice="once")
        self.assertTrue(self.consent.handle_control_line(decision + "\n"))
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result["allowed"])
        self.assertEqual(len(audited), 1)
        self.assertEqual(audited[0]["type"], "network_permission")
        self.assertEqual(audited[0]["scope"], request["scope"])
        self.assertEqual(audited[0]["choice"], "once")
        self.assertTrue(audited[0]["allowed"])
        self.assertFalse(self.consent.handle_control_line(decision))

    def test_session_and_always_choices_are_valid_grants(self):
        for choice in ("session", "always"):
            with self.subTest(choice=choice):
                stream = _CaptureStream()
                self.consent.configure(_KEY, stream, lambda _event: None)
                thread, result, line = self._start_request(stream)
                request = self._request_payload(line)
                self.assertTrue(
                    self.consent.handle_control_line(
                        self._decision(request, choice=choice)
                    )
                )
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertTrue(result["allowed"])

    def test_wrong_id_choice_mac_and_replay_do_not_grant_pending_request(self):
        stream = _CaptureStream()
        audited = []
        self.consent.configure(_KEY, stream, audited.append)
        thread, result, line = self._start_request(stream)
        request = self._request_payload(line)

        wrong_id = dict(request)
        wrong_id["request_id"] = "0" * 32
        self.assertFalse(
            self.consent.handle_control_line(self._decision(wrong_id))
        )
        self.assertFalse(
            self.consent.handle_control_line(
                self._decision(request, choice="forever")
            )
        )
        self.assertFalse(
            self.consent.handle_control_line(
                self._decision(request, choice="deny", allowed=True)
            )
        )
        self.assertFalse(
            self.consent.handle_control_line(
                self._decision(request, permission_id="0" * 64)
            )
        )
        valid = self._decision(request)
        self.assertFalse(
            self.consent.handle_control_line(valid[:-1] + ("0" if valid[-1] != "0" else "1"))
        )
        self.assertTrue(
            self.consent.handle_control_line(
                self._decision(request, choice="deny", allowed=False)
            )
        )
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(result["allowed"])
        self.assertFalse(self.consent.handle_control_line(valid))

    def test_audit_failure_and_timeout_deny_without_connect_permission(self):
        stream = _CaptureStream()

        def failed_audit(_event):
            raise OSError("audit unavailable")

        self.consent.configure(_KEY, stream, failed_audit)
        thread, result, line = self._start_request(stream)
        request = self._request_payload(line)
        self.assertTrue(self.consent.handle_control_line(self._decision(request)))
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(result["allowed"])

        self.consent.configure(_KEY, _CaptureStream(), lambda _event: None)
        with patch.object(self.consent, "_WAIT_SECONDS", 0.01):
            self.assertFalse(self.consent.request_permission(_scope()))

    def test_prompt_lock_serializes_requests_and_lock_timeout_denies(self):
        class BlockingStream:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def write(self, _value):
                self.entered.set()
                self.release.wait(timeout=2)

            def flush(self):
                pass

        stream = BlockingStream()
        self.consent.configure(_KEY, stream, lambda _event: None)
        first_result = {}
        first = threading.Thread(
            target=lambda: first_result.setdefault(
                "allowed", self.consent.request_permission(_scope())
            )
        )
        with patch.object(self.consent, "_WAIT_SECONDS", 0.01):
            first.start()
            self.assertTrue(stream.entered.wait(timeout=2))
            self.assertFalse(self.consent.request_permission(_scope()))
            self.consent.cancel_pending()
            stream.release.set()
            first.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(first_result["allowed"])

    def test_cancel_wakes_pending_request_and_disables_future_requests(self):
        stream = _CaptureStream()
        self.consent.configure(_KEY, stream, lambda _event: None)
        thread, result, _line = self._start_request(stream)
        self.consent.cancel_pending()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(result["allowed"])
        self.assertFalse(self.consent.request_permission(_scope()))


if __name__ == "__main__":
    unittest.main()
