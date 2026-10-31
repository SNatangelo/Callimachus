#!/usr/bin/env python3
# tests/test_http_redirect_chain.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the redirect-chain capture in core.fetch.transport.http / http_headers.

Covers two layers, both offline (no real network, no real threads):

1. ``_ConsentCookieRedirectHandler.redirect_request`` — verifies the chain is
   carried on the ``Request`` object (not on the shared handler instance),
   so concurrent/interleaved requests through the same handler never
   cross-contaminate each other's hops.
2. ``core.fetch.transport.http._open_and_read`` / ``_result_dict`` — verifies the
   completed chain surfaces under the ``redirect_chain`` key of the returned
   dict, for both the success path and the HTTPError path.
"""

from tests._bootstrap import *  # noqa: F401,F403

import io
import urllib.error
import urllib.request
from unittest.mock import patch

from core.fetch.transport import http as http_mod
from core.fetch.transport import http_headers as hh


class _FakeResponse:
    """Minimal stand-in for an http.client.HTTPResponse used as a context manager."""

    def __init__(self, status, url, body=b"", headers=None):
        self.status = status
        self._url = url
        self._body = body
        self._offset = 0
        self.headers = headers or {}

    def read(self, size=-1):
        if size < 0:
            size = len(self._body) - self._offset
        end = min(len(self._body), self._offset + size)
        chunk = self._body[self._offset:end]
        self._offset = end
        return chunk

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        self.closed = True


class TestRedirectRequestChain(unittest.TestCase):
    """Exercises the handler's chain bookkeeping directly, without urllib.request.OpenerDirector."""

    def setUp(self):
        self.handler = hh._ConsentCookieRedirectHandler()

    def _redirect(self, req, code, newurl, msg="Redirect"):
        return self.handler.redirect_request(req, None, code, msg, {}, newurl)

    def test_no_redirect_yields_empty_chain(self):
        # A request that never goes through redirect_request simply has no
        # chain attribute at all -- the http.py layer must default this to [].
        req = urllib.request.Request("https://example.org/no-redirect")
        self.assertIsNone(getattr(req, "_redirect_chain", None))

    def test_single_301_redirect_chain(self):
        req = urllib.request.Request("https://example.org/start")
        target = "https://example.org/moved-permanently"
        new_req = self._redirect(req, 301, target, msg="Moved Permanently")

        self.assertIsNotNone(new_req)
        self.assertEqual(req._redirect_chain, [(301, target)])
        self.assertEqual(new_req._redirect_chain, [(301, target)])
        # Same list object, not a copy -- later hops must mutate this in place.
        self.assertIs(req._redirect_chain, new_req._redirect_chain)

    def test_two_hop_chain_records_both_in_order(self):
        req = urllib.request.Request("https://example.org/start")
        hop1_target = "https://example.org/hop1"
        hop2_target = "https://example.org/hop2-final"

        req_after_hop1 = self._redirect(req, 302, hop1_target)
        req_after_hop2 = self._redirect(req_after_hop1, 301, hop2_target)

        # The ORIGINAL request the caller constructed sees both hops, in order,
        # because every hop appends to the same underlying list.
        self.assertEqual(req._redirect_chain, [(302, hop1_target), (301, hop2_target)])
        self.assertEqual(req_after_hop2._redirect_chain, [(302, hop1_target), (301, hop2_target)])
        self.assertIs(req._redirect_chain, req_after_hop2._redirect_chain)

    def test_acm_rewrite_recorded_as_rewritten_target(self):
        # Regression guard: the existing ACM citation.cfm -> dl.acm.org/doi/abs
        # rewrite must still work, and the chain must record the REWRITTEN
        # target (what is actually fetched next), not the raw Location header.
        req = urllib.request.Request("https://doi.org/10.1145/3025453.3025717")
        raw_target = "https://dl.acm.org/citation.cfm?doid=10.1145/3025453.3025717"
        new_req = self._redirect(req, 302, raw_target)

        rewritten = "https://dl.acm.org/doi/abs/10.1145/3025453.3025717"
        self.assertEqual(new_req.full_url, rewritten)
        self.assertEqual(req._redirect_chain, [(302, rewritten)])

    def test_cross_origin_redirect_does_not_forward_provider_credentials(self):
        credentials = {
            "Wiley-TDM-Client-Token": "wiley-secret",
            "X-ELS-APIKey": "elsevier-secret",
        }
        targets = (
            "https://onlinelibrary.wiley.com/article",
            "http://api.wiley.com/article",
            "https://api.wiley.com:444/article",
            "ftp://api.wiley.com/article",
        )
        for target in targets:
            req = urllib.request.Request("https://api.wiley.com/start")
            for name, value in credentials.items():
                req.add_header(name, value)
            redirected = self._redirect(req, 302, target)

            self.assertIsNotNone(redirected)
            forwarded = {name.lower() for name, _value in redirected.header_items()}
            for name in credentials:
                with self.subTest(target=target, header=name):
                    self.assertNotIn(name.lower(), forwarded)

    def test_same_origin_redirect_retains_provider_credentials(self):
        req = urllib.request.Request("https://api.wiley.com/start")
        credentials = {
            "Wiley-TDM-Client-Token": "wiley-secret",
            "X-ELS-APIKey": "elsevier-secret",
        }
        for name, value in credentials.items():
            req.add_header(name, value)

        redirected = self._redirect(
            req,
            302,
            "https://api.wiley.com:443/article",
        )

        self.assertIsNotNone(redirected)
        forwarded = {name.lower(): value for name, value in redirected.header_items()}
        for name, value in credentials.items():
            with self.subTest(header=name):
                self.assertEqual(forwarded[name.lower()], value)

    def test_concurrent_interleaved_requests_do_not_cross_contaminate(self):
        """The whole point of the design: one shared handler instance, two
        independent redirect sequences interleaved hop-by-hop, each request
        must end up with only its own hops. Driven deterministically (no
        real threads, no real network) by interleaving direct calls to
        redirect_request on a single handler instance.
        """
        handler = self.handler  # single shared instance, like _consent_opener

        req_a = urllib.request.Request("https://a.example.org/start")
        req_b = urllib.request.Request("https://b.example.org/start")

        # Hop 1 for A, then hop 1 for B (interleaved).
        req_a_1 = handler.redirect_request(req_a, None, 302, "x", {}, "https://a.example.org/hop1")
        req_b_1 = handler.redirect_request(req_b, None, 301, "x", {}, "https://b.example.org/hop1")

        # Hop 2 for B, then hop 2 for A (interleaved, reversed order).
        req_b_2 = handler.redirect_request(req_b_1, None, 302, "x", {}, "https://b.example.org/hop2")
        req_a_2 = handler.redirect_request(req_a_1, None, 301, "x", {}, "https://a.example.org/hop2")

        self.assertEqual(
            req_a._redirect_chain,
            [(302, "https://a.example.org/hop1"), (301, "https://a.example.org/hop2")],
        )
        self.assertEqual(
            req_b._redirect_chain,
            [(301, "https://b.example.org/hop1"), (302, "https://b.example.org/hop2")],
        )
        # Cross-check: neither chain leaked hops from the other request.
        self.assertNotIn("https://b.example.org/hop1", [u for _, u in req_a._redirect_chain])
        self.assertNotIn("https://a.example.org/hop1", [u for _, u in req_b._redirect_chain])
        self.assertIs(req_a_2._redirect_chain, req_a._redirect_chain)
        self.assertIs(req_b_2._redirect_chain, req_b._redirect_chain)


class TestOpenAndReadSurfacesChain(unittest.TestCase):
    """Verifies core.fetch.transport.http._open_and_read / _result_dict expose the chain."""

    def test_no_redirect_yields_empty_list_key(self):
        req = urllib.request.Request("https://example.org/direct")

        def fake_open(r, timeout=None):
            return _FakeResponse(200, "https://example.org/direct", b"ok", {"Content-Type": "text/plain"})

        result = http_mod._open_and_read(fake_open, req, timeout=5, url="https://example.org/direct")
        self.assertIn("redirect_chain", result)
        self.assertEqual(result["redirect_chain"], [])

    def test_redirected_request_surfaces_chain_in_dict(self):
        req = urllib.request.Request("https://example.org/start")
        # Simulate what the handler would have attached to req by the time
        # opener.open(req) returns.
        req._redirect_chain = [(301, "https://example.org/final")]

        def fake_open(r, timeout=None):
            return _FakeResponse(200, "https://example.org/final", b"ok", {"Content-Type": "text/plain"})

        result = http_mod._open_and_read(fake_open, req, timeout=5, url="https://example.org/start")
        self.assertEqual(result["redirect_chain"], [(301, "https://example.org/final")])
        # Existing keys are untouched.
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["url"], "https://example.org/final")
        self.assertEqual(result["body"], b"ok")

    def test_http_error_branch_also_surfaces_chain(self):
        req = urllib.request.Request("https://example.org/start")
        req._redirect_chain = [(302, "https://example.org/gate")]

        def fake_open(r, timeout=None):
            raise urllib.error.HTTPError(
                "https://example.org/gate", 403, "Forbidden",
                {"Content-Type": "text/plain"}, io.BytesIO(b"blocked"),
            )

        result = http_mod._open_and_read(fake_open, req, timeout=5, url="https://example.org/start")
        self.assertTrue(result["http_error"])
        self.assertEqual(result["redirect_chain"], [(302, "https://example.org/gate")])

    def test_two_hop_chain_end_to_end_through_open_and_read(self):
        req = urllib.request.Request("https://example.org/start")

        def fake_open(r, timeout=None):
            # Simulate the OpenerDirector driving two redirects through the
            # real handler before the final response is produced.
            handler = hh._ConsentCookieRedirectHandler()
            r1 = handler.redirect_request(r, None, 302, "x", {}, "https://example.org/hop1")
            r2 = handler.redirect_request(r1, None, 301, "x", {}, "https://example.org/hop2-final")
            # opener.open(req) leaves the chain attached to the ORIGINAL req
            # object the caller holds -- mirror that here.
            return _FakeResponse(200, "https://example.org/hop2-final", b"ok")

        result = http_mod._open_and_read(fake_open, req, timeout=5, url="https://example.org/start")
        self.assertEqual(
            result["redirect_chain"],
            [(302, "https://example.org/hop1"), (301, "https://example.org/hop2-final")],
        )


class TestResponseClosure(unittest.TestCase):
    def test_http_error_response_is_closed_after_evidence_is_extracted(self):
        error = urllib.error.HTTPError(
            "https://example.org/gate", 403, "Forbidden", {"Content-Type": "text/plain"},
            io.BytesIO(b"blocked"),
        )
        closed = []
        def close():
            closed.append(True)
            raise ValueError("I/O operation on closed file")
        error.close = close

        result = http_mod._open_and_read(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
            urllib.request.Request("https://example.org/gate"), timeout=5,
            url="https://example.org/gate",
        )

        self.assertTrue(result["http_error"])
        self.assertEqual(result["body"], b"blocked")
        self.assertEqual(closed, [True])

    def test_cookie_warmup_closes_success_response(self):
        warm_response = _FakeResponse(200, "https://example.org/")
        original_close = warm_response.close

        def close():
            original_close()
            raise ValueError("I/O operation on closed file")

        warm_response.close = close

        class Opener:
            def open(self, *_args, **_kwargs):
                return warm_response

        with (
            patch.object(http_mod._http_headers_mod, "build_session_opener", return_value=Opener()),
            patch.object(http_mod, "_acquire_for_fetch", return_value=0),
            patch.object(http_mod, "_open_and_read", return_value={"status": 200}),
        ):
            result = http_mod._fetch_url_with_session("https://example.org/article")

        self.assertEqual(result, {"status": 200})
        self.assertTrue(warm_response.closed)

    def test_cookie_warmup_closes_http_error_response(self):
        error = urllib.error.HTTPError(
            "https://example.org/", 403, "Forbidden", {}, io.BytesIO(b"blocked")
        )
        closed = []
        def close():
            closed.append(True)
            raise ValueError("I/O operation on closed file")
        error.close = close

        class Opener:
            def open(self, *_args, **_kwargs):
                raise error

        with (
            patch.object(http_mod._http_headers_mod, "build_session_opener", return_value=Opener()),
            patch.object(http_mod, "_acquire_for_fetch", return_value=0),
            patch.object(http_mod, "_open_and_read", return_value={"status": 200}),
        ):
            result = http_mod._fetch_url_with_session("https://example.org/article")

        self.assertEqual(result, {"status": 200})
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
