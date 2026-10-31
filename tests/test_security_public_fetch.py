#!/usr/bin/env python3
# tests/test_security_public_fetch.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Isolated regressions for automatic public-only HTTP transport."""

from __future__ import annotations

import ast
import email.message
import http.cookiejar
import io
import importlib.util
import pathlib
import re
import socket
import ssl
import sys
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch


_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_public_http():
    path = _ROOT / "core" / "fetch" / "transport" / "public_http.py"
    package_name = "_public_transport_under_test"
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(path.parent)]
        package.__package__ = package_name
        sys.modules[package_name] = package
    module_name = package_name + ".public_http"
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the public HTTP transport module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_header_components(public_http):
    """Compile only the actual opener and redirect code with small stdlib stubs."""
    path = _ROOT / "core" / "fetch" / "transport" / "http_headers.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wanted = {
        "_http_origin",
        "_strip_provider_credentials",
        "_consent_cookie_for_url",
        "_ConsentCookieRedirectHandler",
        "_trusted_ssl_context",
        "_build_consent_opener",
        "build_session_opener",
    }
    selected = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            selected.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id == "_PROVIDER_CREDENTIAL_HEADERS"
            for target in node.targets
        ):
            selected.append(node)

    namespace = {
        "email": email,
        "http": http,
        "re": re,
        "ssl": ssl,
        "urllib": urllib,
        "_profiles": types.SimpleNamespace(active_module=lambda: None),
        "_certifi": None,
        "PublicOnlyURLHandler": public_http.PublicOnlyURLHandler,
        "PublicOnlyHTTPHandler": public_http.PublicOnlyHTTPHandler,
        "PublicOnlyHTTPSHandler": public_http.PublicOnlyHTTPSHandler,
    }
    code = compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec")
    exec(code, namespace)
    return namespace


def _addrinfo(address, port=443):
    if ":" in address:
        family = socket.AF_INET6
        sockaddr = (address, port, 0, 0)
    else:
        family = socket.AF_INET
        sockaddr = (address, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


class _FakeSocket:
    def __init__(self, family, socktype, proto, created):
        self.family = family
        self.socktype = socktype
        self.proto = proto
        self.created = created
        self.connected = None
        self.bound = None
        self.timeout = None
        self.sent = []
        self.response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        created.append(self)

    def settimeout(self, timeout):
        self.timeout = timeout

    def bind(self, address):
        self.bound = address

    def connect(self, address):
        self.connected = address

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, mode):
        return io.BytesIO(self.response)

    def close(self):
        pass


class PublicFetchTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.public_http = _load_public_http()
        cls.headers = _load_header_components(cls.public_http)

    def assert_policy_denial(self, error):
        for _ in range(4):
            if isinstance(error,self.public_http.PublicFetchPolicyError):
                return
            error=getattr(error,"reason",None)
        self.fail("request did not fail with the destination policy")

    def test_public_dns_answer_is_dialed_numerically_without_second_lookup(self):
        created = []
        lookups = []

        def resolve(host, port, **kwargs):
            lookups.append((host, port, kwargs))
            return [_addrinfo("8.8.8.8")]

        with patch.object(self.public_http.socket, "getaddrinfo", side_effect=resolve), patch.object(
            self.public_http.socket,
            "socket",
            side_effect=lambda *args: _FakeSocket(*args, created),
        ):
            connection = self.public_http._http_connection(
                "publisher.example", timeout=7
            )
            connected = connection._create_connection(
                (connection.host, 443), timeout=7, source_address=None
            )
            self.assertIs(connected, created[0])

        self.assertEqual(len(lookups), 1)
        self.assertEqual(lookups[0][0:2], ("publisher.example", 443))
        self.assertEqual(created[0].connected, ("8.8.8.8", 443))
        self.assertEqual(created[0].timeout, 7)
        self.assertEqual(connection.host, "publisher.example")

    def test_any_non_public_answer_denies_the_whole_dns_set_before_socket_creation(self):
        created = []
        answers = [_addrinfo("8.8.8.8"), _addrinfo("192.168.1.20")]
        with patch.object(self.public_http.socket, "getaddrinfo", return_value=answers), patch.object(
            self.public_http.socket,
            "socket",
            side_effect=lambda *args: _FakeSocket(*args, created),
        ):
            connection = self.public_http._http_connection("mixed.example", timeout=5)
            with self.assertRaises(self.public_http.PublicFetchPolicyError):
                connection._create_connection((connection.host, 443), timeout=5)
        self.assertEqual(created, [])

    def test_reserved_local_multicast_and_mapped_private_addresses_are_denied(self):
        candidates = (
            "0.0.0.0",
            "127.0.0.1",
            "10.0.0.1",
            "169.254.1.2",
            "192.168.1.2",
            "224.0.0.1",
            "240.0.0.1",
            "::",
            "::1",
            "::ffff:127.0.0.1",
            "fc00::1",
            "fe80::1",
            "ff02::1",
            "64:ff9b::7f00:1",
            "2002:7f00:1::1",
            "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
        )
        for address in candidates:
            created = []
            with self.subTest(address=address):
                with patch.object(
                    self.public_http.socket,
                    "getaddrinfo",
                    return_value=[_addrinfo(address)],
                ), patch.object(
                    self.public_http.socket,
                    "socket",
                    side_effect=lambda *args: _FakeSocket(*args, created),
                ):
                    connection = self.public_http._https_connection(
                        "publisher.example", timeout=5
                    )
                    with self.assertRaises(self.public_http.PublicFetchPolicyError):
                        connection._create_connection(
                            (connection.host, 443), timeout=5
                        )
                self.assertEqual(created, [])

    def test_each_connection_attempt_resolves_again_and_rejects_rebinding(self):
        created = []
        answers = [[_addrinfo("8.8.4.4")], [_addrinfo("127.0.0.1")]]
        lookups = []

        def resolve(host, port, **kwargs):
            lookups.append(host)
            return answers.pop(0)

        with patch.object(self.public_http.socket, "getaddrinfo", side_effect=resolve), patch.object(
            self.public_http.socket,
            "socket",
            side_effect=lambda *args: _FakeSocket(*args, created),
        ):
            connection = self.public_http._http_connection("rebind.example", timeout=5)
            connection._create_connection((connection.host, 443), timeout=5)
            with self.assertRaises(self.public_http.PublicFetchPolicyError):
                connection._create_connection((connection.host, 443), timeout=5)

        self.assertEqual(lookups, ["rebind.example", "rebind.example"])
        self.assertEqual([sock.connected for sock in created], [("8.8.4.4", 443)])

    def test_well_known_nat64_routes_allow_public_ipv4_and_reject_private_ipv4(self):
        created = []
        with patch.object(
            self.public_http.socket,
            "getaddrinfo",
            return_value=[_addrinfo("64:ff9b::808:808")],
        ), patch.object(
            self.public_http.socket,
            "socket",
            side_effect=lambda *args: _FakeSocket(*args, created),
        ):
            connection = self.public_http._http_connection("nat64.example", timeout=5)
            connection._create_connection((connection.host, 443), timeout=5)
        self.assertEqual(created[0].connected, ("64:ff9b::808:808", 443, 0, 0))

    def test_https_keeps_origin_hostname_for_host_sni_and_certificate_checks(self):
        created = []

        class VerifyingContext:
            check_hostname = True
            verify_mode = ssl.CERT_REQUIRED

            def __init__(self):
                self.server_hostname = None

            def wrap_socket(self, sock, *, server_hostname):
                self.server_hostname = server_hostname
                return sock

        context = VerifyingContext()
        class CaptureHTTPSHandler(self.public_http.PublicOnlyHTTPSHandler):
            def do_open(self, connection_class, req, **kwargs):
                self.connection_kwargs = kwargs
                return connection_class(req.host, timeout=5, **kwargs)

        handler = CaptureHTTPSHandler(context=context)
        request = urllib.request.Request("https://doi.org/10.1145/example")
        with patch.object(
            self.public_http.socket,
            "getaddrinfo",
            return_value=[_addrinfo("1.1.1.1")],
        ), patch.object(
            self.public_http.socket,
            "socket",
            side_effect=lambda *args: _FakeSocket(*args, created),
        ):
            connection = handler.https_open(request)
            connection.connect()
            connection.putrequest("GET", "/10.1145/example")

        self.assertEqual(handler.connection_kwargs, {"context": context})
        self.assertIs(connection._context, context)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(context.server_hostname, "doi.org")
        self.assertEqual(connection.host, "doi.org")
        self.assertIn(b"Host: doi.org", b"".join(connection._buffer))

    def test_public_publisher_urls_remain_valid_and_non_http_urls_are_denied(self):
        handler = self.public_http.PublicOnlyURLHandler()
        urls = (
            "https://api.crossref.org/works/10.1000/example",
            "https://doi.org/10.1145/3025453.3025717",
            "https://dl.acm.org/doi/abs/10.1145/3025453.3025717",
            "https://api.wiley.com/v2/articles/example",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(handler.default_open(urllib.request.Request(url)))

        for url in (
            "file:///not-a-network-fetch",
            "ftp://example.invalid/resource",
            "https://user:secret@example.org/article",
        ):
            with self.subTest(url=url):
                with self.assertRaises(self.public_http.PublicFetchPolicyError):
                    handler.default_open(urllib.request.Request(url))

    def test_redirect_to_non_http_url_is_denied_by_the_next_open(self):
        redirect = self.headers["_ConsentCookieRedirectHandler"]()
        request = urllib.request.Request("https://doi.org/10.1145/example")
        redirected = redirect.redirect_request(
            request, None, 302, "Found", {}, "ftp://publisher.example/document.pdf"
        )
        self.assertIsNotNone(redirected)
        with self.assertRaises(self.public_http.PublicFetchPolicyError):
            self.public_http.PublicOnlyURLHandler().default_open(redirected)

    def test_acm_rewrite_and_same_origin_credential_retention_are_preserved(self):
        redirect = self.headers["_ConsentCookieRedirectHandler"]()
        request = urllib.request.Request("https://doi.org/10.1145/3025453.3025717")
        redirected = redirect.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://dl.acm.org/citation.cfm?doid=10.1145/3025453.3025717",
        )
        self.assertEqual(
            redirected.full_url,
            "https://dl.acm.org/doi/abs/10.1145/3025453.3025717",
        )

        cookie_profile = types.SimpleNamespace(
            consent_cookie=lambda url: "acm-consent=accepted" if "dl.acm.org" in url else None
        )
        with patch.dict(
            self.headers,
            {"_profiles": types.SimpleNamespace(active_module=lambda: cookie_profile)},
        ):
            request = urllib.request.Request("https://doi.org/10.1145/example")
            request.add_header("Cookie", "stale=1")
            redirected = redirect.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://dl.acm.org/doi/abs/10.1145/example",
            )
            headers = {name.lower(): value for name, value in redirected.header_items()}
            self.assertEqual(headers["cookie"], "acm-consent=accepted")

        request = urllib.request.Request("https://api.wiley.com/start")
        request.add_header("Wiley-TDM-Client-Token", "same-origin-secret")
        request.add_header("Cookie", "stale-consent=1")
        redirected = redirect.redirect_request(
            request, None, 302, "Found", {}, "https://api.wiley.com:443/article"
        )
        headers = {name.lower(): value for name, value in redirected.header_items()}
        self.assertEqual(headers["wiley-tdm-client-token"], "same-origin-secret")
        self.assertNotIn("cookie", headers)

    def test_cross_origin_redirect_strips_credentials_and_stale_cookies(self):
        redirect = self.headers["_ConsentCookieRedirectHandler"]()
        request = urllib.request.Request("https://api.wiley.com/start")
        request.add_header("Wiley-TDM-Client-Token", "must-not-leak")
        request.add_header("X-ELS-APIKey", "also-must-not-leak")
        request.add_header("Cookie", "session=private")
        redirected = redirect.redirect_request(
            request, None, 302, "Found", {}, "https://publisher.example/article"
        )
        headers = {name.lower() for name, _value in redirected.header_items()}
        self.assertNotIn("wiley-tdm-client-token", headers)
        self.assertNotIn("x-els-apikey", headers)
        self.assertNotIn("cookie", headers)

    def test_shared_and_session_openers_keep_policy_proxy_and_cookie_handlers(self):
        with patch.object(urllib.request, "getproxies", return_value={"https": "http://proxy.example:8080"}):
            shared = self.headers["_build_consent_opener"]()
            session = self.headers["build_session_opener"]()
        for opener in (shared, session):
            handlers = opener.handlers
            self.assertTrue(
                any(isinstance(h, self.public_http.PublicOnlyURLHandler) for h in handlers)
            )
            self.assertTrue(
                any(isinstance(h, self.public_http.PublicOnlyHTTPHandler) for h in handlers)
            )
            self.assertTrue(
                any(isinstance(h, self.public_http.PublicOnlyHTTPSHandler) for h in handlers)
            )
            self.assertTrue(
                any(isinstance(h, urllib.request.ProxyHandler) for h in handlers),
                "the opener must retain urllib's selected-proxy behavior",
            )
            with self.assertRaises(self.public_http.PublicFetchPolicyError):
                opener.open("file:///not-a-network-fetch")

        cookie_handler = next(
            h for h in session.handlers if isinstance(h, urllib.request.HTTPCookieProcessor)
        )
        request = urllib.request.Request("https://publisher.example/warmup")
        response_headers = email.message.Message()
        response_headers.add_header(
            "Set-Cookie", "publisher-session=warm; Path=/; Secure"
        )
        response = types.SimpleNamespace(info=lambda: response_headers)
        cookie_handler.http_response(request, response)
        replay = urllib.request.Request("https://publisher.example/article")
        cookie_handler.http_request(replay)
        self.assertEqual(replay.get_header("Cookie"), "publisher-session=warm")

    def test_selected_http_and_https_proxy_routes_are_explicitly_refused(self):
        for scheme, handler in (
            ("http", self.public_http.PublicOnlyHTTPHandler()),
            ("https", self.public_http.PublicOnlyHTTPSHandler()),
        ):
            with self.subTest(scheme=scheme):
                request = urllib.request.Request(f"{scheme}://publisher.example/article")
                request.set_proxy("proxy.example:8080", "http")
                request.timeout = 5
                with patch.object(
                    self.public_http.socket,
                    "getaddrinfo",
                    return_value=[_addrinfo("8.8.8.8", 8080)],
                ), self.assertRaises(urllib.error.URLError) as denied:
                    if scheme == "http":
                        handler.http_open(request)
                    else:
                        handler.https_open(request)
                self.assert_policy_denial(denied.exception)


    def test_unavailable_ipv6_stack_falls_back_to_validated_ipv4(self):
        created=[]
        def constructor(family, kind, proto):
            if family==socket.AF_INET6:
                raise OSError("IPv6 is unavailable on this host")
            return _FakeSocket(family,kind,proto,created)
        with patch.object(self.public_http.socket,"getaddrinfo",return_value=[_addrinfo("2606:4700:4700::1111"),_addrinfo("1.1.1.1")]), patch.object(self.public_http.socket,"socket",side_effect=constructor):
            result=self.public_http._connect_public("publisher.example",443,timeout=5)
        self.assertIs(result,created[0]);self.assertEqual(result.connected,("1.1.1.1",443))
        self.assertIsNone(result.bound)

    def test_shared_and_session_public_get_preserve_host_and_os_routing(self):
        for build in ("_build_consent_opener","build_session_opener"):
            with self.subTest(build=build):
                created=[];lookups=[]
                def resolve(host,port,**kw):
                    lookups.append((host,port));return [_addrinfo("8.8.8.8",port)]
                with patch.object(urllib.request,"getproxies",return_value={}), patch.object(self.public_http.socket,"getaddrinfo",side_effect=resolve), patch.object(self.public_http.socket,"socket",side_effect=lambda *args:_FakeSocket(*args,created)):
                    opener=self.headers[build]()
                    with opener.open("http://publisher.example/article",timeout=5) as response:
                        self.assertEqual(response.read(),b"ok")
                self.assertEqual(lookups,[("publisher.example",80)])
                self.assertEqual(created[0].connected,("8.8.8.8",80));self.assertIsNone(created[0].bound)
                self.assertIn(b"Host: publisher.example",b"".join(created[0].sent))

    def test_actual_redirect_to_private_address_stops_before_second_socket(self):
        created=[]
        def constructor(*args):
            sock=_FakeSocket(*args,created)
            sock.response=b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n"
            return sock
        def resolve(host,port,**kw):
            return [_addrinfo("127.0.0.1" if host=="127.0.0.1" else "8.8.8.8",port)]
        with patch.object(urllib.request,"getproxies",return_value={}), patch.object(self.public_http.socket,"getaddrinfo",side_effect=resolve), patch.object(self.public_http.socket,"socket",side_effect=constructor):
            opener=self.headers["_build_consent_opener"]()
            with self.assertRaises(urllib.error.URLError) as denied:
                opener.open("http://publisher.example/article",timeout=5)
            self.assert_policy_denial(denied.exception)
        self.assertEqual(len(created),1)

    def test_actual_proxy_selection_is_denied_without_any_socket(self):
        for build in ("_build_consent_opener","build_session_opener"):
            for scheme in ("http","https"):
                with self.subTest(build=build,scheme=scheme), patch.object(urllib.request,"getproxies",return_value={scheme:"http://proxy.example:8080"}), patch.object(urllib.request,"proxy_bypass",return_value=False), patch.object(self.public_http.socket,"getaddrinfo",return_value=[_addrinfo("8.8.8.8",8080)]), patch.object(self.public_http.socket,"socket") as constructor:
                    opener=self.headers[build]()
                    with self.assertRaises(urllib.error.URLError) as denied:
                        opener.open(scheme+"://publisher.example/article",timeout=5)
                    self.assert_policy_denial(denied.exception)
                    constructor.assert_not_called()


    def test_proxy_bypass_still_allows_validated_direct_public_route(self):
        created=[]
        with patch.object(urllib.request,"getproxies",return_value={"http":"http://proxy.example:8080"}), patch.object(urllib.request,"proxy_bypass",return_value=True), patch.object(self.public_http.socket,"getaddrinfo",return_value=[_addrinfo("8.8.8.8",80)]), patch.object(self.public_http.socket,"socket",side_effect=lambda *args:_FakeSocket(*args,created)):
            opener=self.headers["_build_consent_opener"]()
            with opener.open("http://publisher.example/article",timeout=5) as response:
                self.assertEqual(response.read(),b"ok")
        self.assertEqual(len(created),1);self.assertIsNone(created[0].bound)

    def test_approved_private_route_dials_only_the_resolved_numeric_scope(self):
        created = []
        scopes = []
        answers = [_addrinfo("192.168.1.20"), _addrinfo("192.168.1.21")]
        with patch.object(
            self.public_http.socket, "getaddrinfo", return_value=answers
        ) as resolve, patch.object(
            self.public_http.socket,
            "socket",
            side_effect=lambda *args: _FakeSocket(*args, created),
        ), patch.object(
            self.public_http.network_consent,
            "request_permission",
            side_effect=lambda scope: scopes.append(scope) or True,
        ):
            connection = self.public_http._http_connection(
                "private.example",
                origin_url="https://private.example/article",
                timeout=5,
            )
            connected = connection._create_connection(
                (connection.host, 443), timeout=5
            )

        resolve.assert_called_once_with(
            "private.example",
            443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        self.assertIs(connected, created[0])
        self.assertEqual(created[0].connected, ("192.168.1.20", 443))
        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0]["origin"], "https://private.example:443")
        self.assertEqual(scopes[0]["connection_host"], "private.example")
        self.assertEqual(scopes[0]["connection_port"], 443)
        self.assertEqual(
            scopes[0]["addresses"], ["192.168.1.20", "192.168.1.21"]
        )
        self.assertFalse(scopes[0]["via_proxy"])

    def test_each_private_connection_reasks_when_the_resolved_ip_set_changes(self):
        created = []
        scopes = []
        answers = [
            [_addrinfo("192.168.1.20", 80)],
            [_addrinfo("192.168.1.21", 80)],
        ]
        with patch.object(
            self.public_http.socket,
            "getaddrinfo",
            side_effect=lambda *args, **kwargs: answers.pop(0),
        ), patch.object(
            self.public_http.socket,
            "socket",
            side_effect=lambda *args: _FakeSocket(*args, created),
        ), patch.object(
            self.public_http.network_consent,
            "request_permission",
            side_effect=lambda scope: scopes.append(scope) or True,
        ):
            connection = self.public_http._http_connection(
                "changing.example",
                origin_url="http://changing.example/resource",
                timeout=5,
            )
            connection._create_connection((connection.host, 80), timeout=5)
            connection._create_connection((connection.host, 80), timeout=5)

        self.assertEqual([scope["addresses"] for scope in scopes], [
            ["192.168.1.20"],
            ["192.168.1.21"],
        ])
        self.assertEqual(len(created), 2)
        self.assertEqual(
            [sock.connected for sock in created],
            [("192.168.1.20", 80), ("192.168.1.21", 80)],
        )

    def test_ipv6_ula_route_is_consent_gated_and_dialed_numerically(self):
        created = []
        scopes = []
        with patch.object(
            self.public_http.socket,
            "getaddrinfo",
            return_value=[_addrinfo("fd12:3456:789a::20", 443)],
        ), patch.object(
            self.public_http.socket,
            "socket",
            side_effect=lambda *args: _FakeSocket(*args, created),
        ), patch.object(
            self.public_http.network_consent,
            "request_permission",
            side_effect=lambda scope: scopes.append(scope) or True,
        ):
            connection = self.public_http._https_connection(
                "private-v6.example",
                origin_url="https://private-v6.example/article",
                timeout=5,
            )
            connection._create_connection((connection.host, 443), timeout=5)

        self.assertEqual(scopes[0]["addresses"], ["fd12:3456:789a::20"])
        self.assertFalse(scopes[0]["via_proxy"])
        self.assertEqual(
            created[0].connected, ("fd12:3456:789a::20", 443, 0, 0)
        )

    def test_public_proxy_requires_scope_bound_consent_for_proxy_route(self):
        created = []
        scopes = []

        class CaptureHTTPHandler(self.public_http.PublicOnlyHTTPHandler):
            def do_open(self, connection_class, req, **kwargs):
                self.connection = connection_class(req.host, timeout=5, **kwargs)
                return self.connection

        handler = CaptureHTTPHandler()
        request = urllib.request.Request("http://publisher.example/article")
        request.set_proxy("proxy.example:8080", "http")
        with patch.object(
            self.public_http.socket,
            "getaddrinfo",
            return_value=[_addrinfo("8.8.8.8", 8080)],
        ), patch.object(
            self.public_http.socket,
            "socket",
            side_effect=lambda *args: _FakeSocket(*args, created),
        ), patch.object(
            self.public_http.network_consent,
            "request_permission",
            side_effect=lambda scope: scopes.append(scope) or True,
        ):
            connection = handler.http_open(request)
            connection._create_connection((connection.host, 8080), timeout=5)

        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0]["origin"], "http://publisher.example:80")
        self.assertEqual(scopes[0]["connection_host"], "proxy.example")
        self.assertEqual(scopes[0]["connection_port"], 8080)
        self.assertEqual(scopes[0]["addresses"], ["8.8.8.8"])
        self.assertTrue(scopes[0]["via_proxy"])
        self.assertEqual(created[0].connected, ("8.8.8.8", 8080))

    def test_explicit_loopback_proxy_can_be_consented_but_local_resource_is_hard_denied(self):
        created = []
        scopes = []

        class CaptureHTTPHandler(self.public_http.PublicOnlyHTTPHandler):
            def do_open(self, connection_class, req, **kwargs):
                self.connection = connection_class(req.host, timeout=5, **kwargs)
                return self.connection

        handler = CaptureHTTPHandler()
        request = urllib.request.Request("http://publisher.example/article")
        request.set_proxy("127.0.0.1:3128", "http")
        with patch.object(
            self.public_http.socket,
            "getaddrinfo",
            return_value=[_addrinfo("127.0.0.1", 3128)],
        ), patch.object(
            self.public_http.socket,
            "socket",
            side_effect=lambda *args: _FakeSocket(*args, created),
        ), patch.object(
            self.public_http.network_consent,
            "request_permission",
            side_effect=lambda scope: scopes.append(scope) or True,
        ):
            connection = handler.http_open(request)
            connection._create_connection((connection.host, 3128), timeout=5)

        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0]["addresses"], ["127.0.0.1"])
        self.assertTrue(scopes[0]["via_proxy"])
        self.assertEqual(created[0].connected, ("127.0.0.1", 3128))

        local_resource = urllib.request.Request("http://localhost/private")
        local_resource.set_proxy("127.0.0.1:3128", "http")
        with patch.object(
            self.public_http.network_consent,
            "request_permission",
        ) as request_permission:
            with self.assertRaises(self.public_http.PublicFetchPolicyError):
                handler.http_open(local_resource)
            request_permission.assert_not_called()

    def test_metadata_and_link_local_routes_are_hard_denied_before_consent(self):
        for address in ("169.254.169.254", "100.100.100.200", "fe80::1", "fd00:ec2::254"):
            for via_proxy in (False, True):
                with self.subTest(address=address, via_proxy=via_proxy), patch.object(
                    self.public_http.socket,
                    "getaddrinfo",
                    return_value=[_addrinfo(address, 8080)],
                ), patch.object(
                    self.public_http.network_consent, "request_permission"
                ) as request_permission, patch.object(
                    self.public_http.socket, "socket"
                ) as constructor:
                    with self.assertRaises(self.public_http.PublicFetchPolicyError):
                        self.public_http._connect_route(
                            "proxy.example" if via_proxy else "publisher.example",
                            8080,
                            origin_url="https://publisher.example/article",
                            via_proxy=via_proxy,
                            timeout=5,
                        )
                    request_permission.assert_not_called()
                    constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
