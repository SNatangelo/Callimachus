#!/usr/bin/env python3
# core/fetch/transport/public_http.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP transport handlers with public defaults and scoped private consent."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

if __package__:
    from . import network_consent
else:
    import network_consent


class PublicFetchPolicyError(urllib.error.URLError):
    """A request was refused by the automatic public-fetch network policy."""


_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")
_RFC1918 = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
_IPV6_ULA = ipaddress.IPv6Network("fc00::/7")
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("169.254.169.253"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)
_METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.azure.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _deny(message: str) -> None:
    raise PublicFetchPolicyError("public fetch policy denied: " + message)


def _parse_http_url(url: str):
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        # Accessing .port also checks malformed and out-of-range ports.
        port = parsed.port
    except (AttributeError, TypeError, ValueError) as exc:
        raise PublicFetchPolicyError(
            "public fetch policy denied: malformed HTTP(S) URL"
        ) from exc

    if scheme not in _ALLOWED_SCHEMES:
        _deny("only HTTP and HTTPS URLs are allowed")
    if not hostname:
        _deny("the URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        _deny("URL user information is not allowed")
    # IPv6 zone identifiers are local-interface scoped. Percent escapes in a
    # host also make its numeric interpretation needlessly ambiguous.
    if "%" in hostname:
        _deny("scoped or escaped host names are not allowed")
    if port == 0:
        _deny("port zero is not allowed")
    return parsed


def _authority_hostname(authority: str | None) -> str | None:
    if not authority:
        return None
    try:
        return urllib.parse.urlsplit("//" + authority).hostname
    except ValueError:
        return None


def _request_uses_proxy(req, parsed) -> bool:
    """Detect urllib's rewritten request when ProxyHandler selected a proxy."""
    if getattr(req, "_tunnel_host", None) is not None:
        return True
    request_host = _authority_hostname(getattr(req, "host", None))
    if request_host is None or request_host.casefold() != parsed.hostname.casefold():
        return True
    selector = getattr(req, "selector", "")
    return isinstance(selector, str) and selector.lower().startswith(
        ("http://", "https://")
    )


def _public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return _public_ip(mapped)
        # These encodings can carry an IPv4 destination with routing semantics
        # that are not visible from a simple IPv6 global-address check.
        if address in _NAT64_WELL_KNOWN:
            embedded_ipv4 = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
            return _public_ip(embedded_ipv4)
        if (
            address.sixtofour is not None
            or address.teredo is not None
            or address in _NAT64_LOCAL_USE
        ):
            return False
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _address_class(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    via_proxy: bool,
) -> str:
    """Classify one numeric route as public, consent-gated, or forbidden."""

    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return _address_class(mapped, via_proxy=via_proxy)
        # Only the well-known NAT64 prefix retains its existing public-v4
        # exception. Embedded private/reserved destinations remain forbidden.
        if address in _NAT64_WELL_KNOWN:
            embedded_ipv4 = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
            return "public" if _public_ip(embedded_ipv4) else "deny"
        if (
            address.sixtofour is not None
            or address.teredo is not None
            or address in _NAT64_LOCAL_USE
        ):
            return "deny"

    if address in _METADATA_IPS:
        return "deny"
    if address.is_loopback:
        # An explicitly selected loopback proxy is a local transport endpoint,
        # not the requested resource. It still requires the user's approval.
        return "consent" if via_proxy else "deny"
    if (
        address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        return "deny"

    if isinstance(address, ipaddress.IPv4Address) and any(
        address in network for network in _RFC1918
    ):
        return "consent"
    if isinstance(address, ipaddress.IPv6Address) and address in _IPV6_ULA:
        return "consent"
    if address.is_reserved:
        return "deny"
    if _public_ip(address):
        return "public"
    return "deny"


def _resolve_addresses(host: str, port: int, *, via_proxy: bool = False):
    try:
        answers = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError:
        raise
    if not answers:
        _deny("DNS returned no addresses")

    # Validate the full answer set before prompting or creating any socket.
    # One forbidden answer denies the whole set; eligible private answers make
    # the exact full set part of the permission scope.
    validated = []
    seen_records = set()
    numeric_addresses = set()
    needs_consent = via_proxy
    for family, socktype, proto, _canonname, sockaddr in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            _deny("DNS returned an unsupported address family")
        if socktype not in (0, socket.SOCK_STREAM):
            _deny("DNS returned an unsupported socket type")
        if proto not in (0, socket.IPPROTO_TCP):
            _deny("DNS returned an unsupported transport protocol")
        expected_sockaddr_size = 2 if family == socket.AF_INET else 4
        if not isinstance(sockaddr, tuple) or len(sockaddr) != expected_sockaddr_size:
            _deny("DNS returned an invalid socket address")
        numeric_host = sockaddr[0]
        if not isinstance(numeric_host, str) or "%" in numeric_host:
            _deny("DNS returned a scoped or non-numeric address")
        try:
            address = ipaddress.ip_address(numeric_host)
        except ValueError as exc:
            raise PublicFetchPolicyError(
                "public fetch policy denied: DNS returned a non-numeric address"
            ) from exc
        if (family == socket.AF_INET) != isinstance(address, ipaddress.IPv4Address):
            _deny("DNS returned an address inconsistent with its family")
        if type(sockaddr[1]) is not int or sockaddr[1] != port:
            _deny("DNS returned an unexpected port")
        if family == socket.AF_INET6:
            if (
                type(sockaddr[2]) is not int
                or sockaddr[2] != 0
                or type(sockaddr[3]) is not int
                or sockaddr[3] != 0
            ):
                _deny("DNS returned a scoped or non-canonical IPv6 address")
        address_class = _address_class(address, via_proxy=via_proxy)
        if address_class == "deny":
            _deny("DNS resolved to a forbidden or unsupported address")
        if address_class == "consent":
            needs_consent = True
        if family == socket.AF_INET:
            numeric_sockaddr = (address.compressed, port)
        else:
            numeric_sockaddr = (address.compressed, port, 0, 0)
        record_key = (family, numeric_sockaddr)
        if record_key not in seen_records:
            seen_records.add(record_key)
            validated.append(
                (family, socktype or socket.SOCK_STREAM, proto, numeric_sockaddr)
            )
        numeric_addresses.add(address.compressed.lower())
    return validated, sorted(numeric_addresses), needs_consent


def _connect_numeric(
    answers,
    *,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address=None,
):
    errors = []
    for family, socktype, proto, sockaddr in answers:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            # This numeric sockaddr came from the validated answer set, so
            # connect cannot perform a second name lookup.
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            errors.append(exc)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    if errors:
        raise errors[-1]
    _deny("no usable validated address was returned")


def _connect_public(
    host: str,
    port: int,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address=None,
):
    """Strict-public helper retained for focused direct transport tests."""

    answers, _numeric_addresses, needs_consent = _resolve_addresses(host, port)
    if needs_consent:
        _deny("DNS resolved to a non-public address")
    return _connect_numeric(
        answers, timeout=timeout, source_address=source_address
    )


def _origin_for_scope(origin_url: str | None, connection_host: str, port: int) -> str:
    if origin_url is None:
        host = connection_host
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if isinstance(address, ipaddress.IPv6Address):
            host = f"[{address.compressed}]"
        return f"https://{host}:{port}"
    parsed = _parse_http_url(origin_url)
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if isinstance(address, ipaddress.IPv6Address):
        host = f"[{address.compressed}]"
    else:
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
    origin_port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return f"{parsed.scheme.lower()}://{host}:{origin_port}"


def _connect_route(
    host: str,
    port: int,
    *,
    origin_url: str | None,
    via_proxy: bool,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address=None,
):
    answers, numeric_addresses, needs_consent = _resolve_addresses(
        host, port, via_proxy=via_proxy
    )
    if needs_consent:
        scope = {
            "origin": _origin_for_scope(origin_url, host, port),
            "connection_host": host,
            "connection_port": port,
            "addresses": numeric_addresses,
            "via_proxy": via_proxy,
        }
        if not network_consent.request_permission(scope):
            _deny("network destination was not approved")

    # Use only the numeric address records approved in the scope.
    return _connect_numeric(
        answers, timeout=timeout, source_address=source_address
    )


def _validate_origin_resource(parsed) -> None:
    host = parsed.hostname
    if not host:
        _deny("the URL must include a host")
    normalized = host.rstrip(".").casefold()
    if (
        normalized == "localhost"
        or normalized.endswith(".localhost")
        or normalized == "localhost.localdomain"
        or normalized.endswith(".localhost.localdomain")
        or normalized in _METADATA_HOSTS
        or normalized.endswith(".metadata.google.internal")
    ):
        _deny("the requested resource is a local or metadata destination")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if _address_class(address, via_proxy=False) == "deny":
        _deny("the requested resource is a forbidden destination")


def _connection_factory(
    connection_class,
    host,
    *,
    origin_url: str | None = None,
    via_proxy: bool = False,
    **kwargs,
):
    connection = connection_class(host, **kwargs)
    # Install after construction so the standard HTTPConnection/HTTPSConnection
    # keeps its original hostname for Host, SNI, and certificate verification.
    original_host = connection.host

    def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        return _connect_route(
            original_host,
            address[1],
            origin_url=origin_url,
            via_proxy=via_proxy,
            timeout=timeout,
            source_address=source_address,
        )

    connection._create_connection = create_connection
    return connection


def _http_connection(
    host, *, origin_url: str | None = None, via_proxy: bool = False, **kwargs
):
    return _connection_factory(
        http.client.HTTPConnection,
        host,
        origin_url=origin_url,
        via_proxy=via_proxy,
        **kwargs,
    )


def _https_connection(
    host, *, origin_url: str | None = None, via_proxy: bool = False, **kwargs
):
    return _connection_factory(
        http.client.HTTPSConnection,
        host,
        origin_url=origin_url,
        via_proxy=via_proxy,
        **kwargs,
    )


class PublicOnlyURLHandler(urllib.request.BaseHandler):
    """Reject unsupported schemes and URL credentials before protocol dispatch."""

    handler_order = 100

    def default_open(self, req):
        _parse_http_url(req.full_url)
        return None


class PublicOnlyHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        parsed = _parse_http_url(req.full_url)
        _validate_origin_resource(parsed)
        via_proxy = _request_uses_proxy(req, parsed)

        def connection_factory(host, **kwargs):
            return _http_connection(
                host,
                origin_url=req.full_url,
                via_proxy=via_proxy,
                **kwargs,
            )

        return self.do_open(connection_factory, req)


class PublicOnlyHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        parsed = _parse_http_url(req.full_url)
        _validate_origin_resource(parsed)
        via_proxy = _request_uses_proxy(req, parsed)

        def connection_factory(host, **kwargs):
            return _https_connection(
                host,
                origin_url=req.full_url,
                via_proxy=via_proxy,
                **kwargs,
            )

        return self.do_open(
            connection_factory,
            req,
            context=self._context,
        )
