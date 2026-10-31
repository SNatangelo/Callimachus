#!/usr/bin/env python3
# core/fetch/transport/network_consent.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Authenticated, fail-closed IPC for per-destination network permission."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable


CONTROL_ENV = "CALLIMACHUS_NETWORK_CONSENT_KEY"
REQUEST_PREFIX = "@callimachus-network-consent:"

_MAX_MESSAGE_BYTES = 16 * 1024
_WAIT_SECONDS = 120
_SCOPE_KEYS = frozenset(
    {
        "origin",
        "connection_host",
        "connection_port",
        "addresses",
        "via_proxy",
    }
)
_CHOICES = frozenset({"deny", "once", "session", "always"})
_HEX_KEY = re.compile(r"[0-9a-fA-F]{64}\Z")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")


def _canonical_json(value: Any) -> str:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        rendered.encode("utf-8")
        return rendered
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("value is not canonical JSON") from exc


def _key_bytes(keyhex64: object) -> bytes:
    if not isinstance(keyhex64, str) or _HEX_KEY.fullmatch(keyhex64) is None:
        raise ValueError("network consent key must be 64 hexadecimal characters")
    return bytes.fromhex(keyhex64)


def _canonical_host(host: object) -> str:
    if not isinstance(host, str) or not host or host != host.strip():
        raise ValueError("host must be a non-empty canonicalizable string")
    if any(char in host for char in "[]%/?#@\\"):
        raise ValueError("host contains forbidden URL syntax")
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        pass
    if ":" in host:
        raise ValueError("IPv6 hosts must be unbracketed numeric addresses")
    try:
        ascii_host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as exc:
        raise ValueError("host is not valid IDNA") from exc
    if not ascii_host or len(ascii_host) > 253:
        raise ValueError("host has an invalid length")
    labels = ascii_host.split(".")
    label_re = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
    if any(len(label) > 63 or label_re.fullmatch(label) is None for label in labels):
        raise ValueError("host contains an invalid DNS label")
    return ascii_host


def _canonical_origin(origin: object) -> str:
    if not isinstance(origin, str) or not origin:
        raise ValueError("origin must be a non-empty string")
    try:
        parsed = urllib.parse.urlsplit(origin)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("origin is malformed") from exc
    if scheme not in {"http", "https"}:
        raise ValueError("origin must use HTTP or HTTPS")
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("origin must contain a host and no user information")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("origin must not contain a path, query, or fragment")
    if port is None or not 1 <= port <= 65535:
        raise ValueError("origin must contain an explicit valid port")
    canonical_host = _canonical_host(hostname)
    try:
        is_ipv6 = isinstance(ipaddress.ip_address(canonical_host), ipaddress.IPv6Address)
    except ValueError:
        is_ipv6 = False
    display_host = f"[{canonical_host}]" if is_ipv6 else canonical_host
    return f"{scheme}://{display_host}:{port}"


def canonical_scope(scope: dict) -> dict:
    """Validate and return the canonical, exact network-consent scope."""

    if not isinstance(scope, dict) or frozenset(scope) != _SCOPE_KEYS:
        raise ValueError("network scope must contain exactly the required keys")
    origin = _canonical_origin(scope["origin"])
    connection_host = _canonical_host(scope["connection_host"])
    connection_port = scope["connection_port"]
    if type(connection_port) is not int or not 1 <= connection_port <= 65535:
        raise ValueError("connection_port must be an integer from 1 to 65535")
    if type(scope["via_proxy"]) is not bool:
        raise ValueError("via_proxy must be a boolean")
    raw_addresses = scope["addresses"]
    if not isinstance(raw_addresses, list) or not raw_addresses:
        raise ValueError("addresses must be a non-empty list")
    addresses = set()
    for value in raw_addresses:
        if not isinstance(value, str) or "%" in value:
            raise ValueError("addresses must be unscoped numeric IP strings")
        try:
            addresses.add(ipaddress.ip_address(value).compressed.lower())
        except ValueError as exc:
            raise ValueError("addresses must be numeric IP strings") from exc
    return {
        "origin": origin,
        "connection_host": connection_host,
        "connection_port": connection_port,
        "addresses": sorted(addresses),
        "via_proxy": scope["via_proxy"],
    }


def scope_key(scope: dict) -> str:
    """Return a stable SHA-256 identifier for a canonical permission scope."""

    canonical = canonical_scope(scope)
    return hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()


def encode_message(keyhex64: str, payload: dict) -> str:
    """Encode a signed control frame. The key is never included in the frame."""

    key = _key_bytes(keyhex64)
    if not isinstance(payload, dict):
        raise ValueError("network consent payload must be an object")
    canonical_payload = _canonical_json(payload)
    signature = hmac.new(
        key, canonical_payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    frame = REQUEST_PREFIX + _canonical_json(
        {"payload": payload, "signature": signature}
    )
    if len(frame.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        raise ValueError("network consent message exceeds the size limit")
    return frame


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError("non-finite JSON constants are not allowed")


def decode_message(keyhex64: str, line: str) -> dict | None:
    """Verify a bounded signed control frame, returning only its payload."""

    try:
        key = _key_bytes(keyhex64)
        if not isinstance(line, str):
            return None
        encoded = line.encode("utf-8")
        if len(encoded) > _MAX_MESSAGE_BYTES:
            return None
        if line.endswith("\n"):
            line = line[:-1]
            if line.endswith("\r"):
                line = line[:-1]
        if "\n" in line or "\r" in line or not line.startswith(REQUEST_PREFIX):
            return None
        envelope = json.loads(
            line[len(REQUEST_PREFIX) :],
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(envelope, dict) or frozenset(envelope) != {
            "payload",
            "signature",
        }:
            return None
        payload = envelope["payload"]
        signature = envelope["signature"]
        if not isinstance(payload, dict) or not isinstance(signature, str):
            return None
        if re.fullmatch(r"[0-9a-f]{64}", signature) is None:
            return None
        expected = hmac.new(
            key, _canonical_json(payload).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        return payload
    except (TypeError, ValueError, UnicodeError):
        return None


@dataclass
class _PendingRequest:
    request_id: str
    scope: dict
    permission_id: str
    event: threading.Event = field(default_factory=threading.Event)
    result: bool | None = None
    handling: bool = False


class _Broker:
    def __init__(self, keyhex64: str, stream, audit_callback: Callable[[dict], None]):
        self.keyhex64 = keyhex64.lower()
        self.stream = stream
        self.audit_callback = audit_callback
        self.prompt_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.pending: _PendingRequest | None = None
        self.active = True

    def request(self, scope: dict) -> bool:
        if not self.prompt_lock.acquire(timeout=_WAIT_SECONDS):
            return False
        try:
            with self.state_lock:
                if not self.active:
                    return False
                pending = _PendingRequest(
                    request_id=secrets.token_hex(16),
                    scope=scope,
                    permission_id=scope_key(scope),
                )
                self.pending = pending
            payload = {
                "type": "network_request",
                "request_id": pending.request_id,
                "scope": scope,
            }
            try:
                # Prefixing a newline keeps a preceding partial stderr diagnostic
                # from being concatenated with the control frame.
                self.stream.write("\n" + encode_message(self.keyhex64, payload) + "\n")
                self.stream.flush()
            except Exception:
                self._clear(pending)
                return False
            if not pending.event.wait(timeout=_WAIT_SECONDS):
                self._clear(pending)
                return False
            with self.state_lock:
                result = self.active and pending.result is True
                if self.pending is pending:
                    self.pending = None
                return result
        finally:
            self.prompt_lock.release()

    def _clear(self, pending: _PendingRequest) -> None:
        with self.state_lock:
            if self.pending is pending:
                self.pending = None
            if pending.result is None:
                pending.result = False
            pending.event.set()

    def consume(self, line: str) -> bool:
        payload = decode_message(self.keyhex64, line)
        if payload is None:
            return False
        if frozenset(payload) != {
            "type",
            "request_id",
            "allowed",
            "choice",
            "permission_id",
        }:
            return False
        if payload.get("type") != "network_decision":
            return False
        request_id = payload.get("request_id")
        choice = payload.get("choice")
        allowed = payload.get("allowed")
        permission_id = payload.get("permission_id")
        if (
            not isinstance(request_id, str)
            or _REQUEST_ID.fullmatch(request_id) is None
            or not isinstance(choice, str)
            or choice not in _CHOICES
            or type(allowed) is not bool
            or allowed != (choice != "deny")
            or not isinstance(permission_id, str)
        ):
            return False
        with self.state_lock:
            pending = self.pending
            if (
                not self.active
                or pending is None
                or pending.result is not None
                or pending.handling
                or request_id != pending.request_id
                or permission_id != pending.permission_id
            ):
                return False
            pending.handling = True

        event = {
            "type": "network_permission",
            "scope": {
                **pending.scope,
                "addresses": list(pending.scope["addresses"]),
            },
            "request_id": pending.request_id,
            "choice": choice,
            "allowed": allowed,
        }
        audit_ok = True
        try:
            self.audit_callback(event)
        except Exception:
            audit_ok = False
        with self.state_lock:
            if self.pending is not pending or pending.result is not None:
                return False
            pending.result = bool(allowed and audit_ok and self.active)
            pending.event.set()
            return True

    def cancel(self) -> None:
        with self.state_lock:
            self.active = False
            pending = self.pending
            if pending is not None and pending.result is None:
                pending.result = False
                pending.event.set()


_broker_guard = threading.Lock()
_broker: _Broker | None = None


def configure(keyhex64: str, stream, audit_callback: Callable[[dict], None]) -> None:
    """Install the signed IPC broker only when every required input is valid."""

    global _broker
    new_broker = None
    try:
        _key_bytes(keyhex64)
        if not callable(getattr(stream, "write", None)):
            raise ValueError("stream must provide write")
        if not callable(getattr(stream, "flush", None)):
            raise ValueError("stream must provide flush")
        if not callable(audit_callback):
            raise ValueError("audit callback must be callable")
        new_broker = _Broker(keyhex64, stream, audit_callback)
    except Exception:
        new_broker = None
    with _broker_guard:
        previous = _broker
        _broker = new_broker
    if previous is not None:
        previous.cancel()


def request_permission(scope: dict) -> bool:
    """Ask the configured desktop broker; absence or any failure denies."""

    try:
        canonical = canonical_scope(scope)
    except (TypeError, ValueError):
        return False
    with _broker_guard:
        broker = _broker
    if broker is None:
        return False
    try:
        return broker.request(canonical)
    except Exception:
        return False


def handle_control_line(line: str) -> bool:
    """Consume a valid signed decision for the currently pending request."""

    with _broker_guard:
        broker = _broker
    if broker is None:
        return False
    return broker.consume(line)


def cancel_pending() -> None:
    """Fail and disable the broker, waking any waiting permission request."""

    with _broker_guard:
        broker = _broker
    if broker is not None:
        broker.cancel()
