# core/infra/db/network_permission_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed, append-only persistence for exceptional network decisions."""

from __future__ import annotations

import ipaddress
import re
import sqlite3
import urllib.parse
from typing import Any


_EVENT_KEYS = frozenset({"type", "scope", "request_id", "choice", "allowed"})
_SCOPE_KEYS = frozenset({
    "origin", "connection_host", "connection_port", "addresses", "via_proxy",
})
_CHOICES = frozenset({"deny", "once", "session", "always"})
_REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")


NETWORK_PERMISSION_DDL = """
CREATE TABLE IF NOT EXISTS network_permission_decisions (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL UNIQUE CHECK(
    length(request_id)=32 AND request_id NOT GLOB '*[^0123456789abcdef]*'
  ),
  choice TEXT NOT NULL CHECK(choice IN ('deny','once','session','always')),
  allowed INTEGER NOT NULL CHECK(typeof(allowed)='integer' AND allowed IN (0,1)),
  origin TEXT NOT NULL CHECK(length(trim(origin))>0 AND instr(origin,char(0))=0),
  connection_host TEXT NOT NULL CHECK(length(trim(connection_host))>0 AND instr(connection_host,char(0))=0),
  connection_port INTEGER NOT NULL CHECK(typeof(connection_port)='integer' AND connection_port BETWEEN 1 AND 65535),
  via_proxy INTEGER NOT NULL CHECK(typeof(via_proxy)='integer' AND via_proxy IN (0,1)),
  address_count INTEGER NOT NULL CHECK(typeof(address_count)='integer' AND address_count>=1),
  session_id TEXT REFERENCES run_sessions(session_id),
  created_at TEXT NOT NULL,
  CHECK((choice='deny' AND allowed=0) OR (choice IN ('once','session','always') AND allowed=1))
);

CREATE TABLE IF NOT EXISTS network_permission_decision_addresses (
  decision_id INTEGER NOT NULL REFERENCES network_permission_decisions(decision_id),
  address_order INTEGER NOT NULL CHECK(typeof(address_order)='integer' AND address_order>=0),
  address TEXT NOT NULL CHECK(length(trim(address))>0 AND instr(address,char(0))=0),
  PRIMARY KEY(decision_id,address_order),
  UNIQUE(decision_id,address)
) WITHOUT ROWID;
"""

NETWORK_PERMISSION_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS network_permission_decisions_insert_guard
BEFORE INSERT ON network_permission_decisions
WHEN EXISTS (
  SELECT 1 FROM network_permission_decisions
  WHERE decision_id=NEW.decision_id OR request_id=NEW.request_id
)
BEGIN
  SELECT RAISE(ABORT, 'network permission decisions cannot be replaced');
END;
CREATE TRIGGER IF NOT EXISTS network_permission_decisions_no_update
BEFORE UPDATE ON network_permission_decisions BEGIN
  SELECT RAISE(ABORT, 'network permission decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS network_permission_decisions_no_delete
BEFORE DELETE ON network_permission_decisions BEGIN
  SELECT RAISE(ABORT, 'network permission decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS network_permission_decision_addresses_no_update
BEFORE UPDATE ON network_permission_decision_addresses BEGIN
  SELECT RAISE(ABORT, 'network permission decision addresses are append-only');
END;
CREATE TRIGGER IF NOT EXISTS network_permission_decision_addresses_no_delete
BEFORE DELETE ON network_permission_decision_addresses BEGIN
  SELECT RAISE(ABORT, 'network permission decision addresses are append-only');
END;
CREATE TRIGGER IF NOT EXISTS network_permission_decision_addresses_insert_guard
BEFORE INSERT ON network_permission_decision_addresses
WHEN NOT EXISTS (
  SELECT 1 FROM network_permission_decisions
  WHERE decision_id=NEW.decision_id AND NEW.address_order<address_count
)
OR EXISTS (
  SELECT 1 FROM network_permission_decision_addresses
  WHERE decision_id=NEW.decision_id
    AND (address_order=NEW.address_order OR address=NEW.address)
)
BEGIN
  SELECT RAISE(ABORT, 'network permission address order exceeds decision scope');
END;
"""


def _canonical_host(host: Any) -> str:
    if type(host) is not str or not host or host != host.strip():
        raise ValueError("network permission host must be non-empty text")
    if any(char in host for char in "[]%/?#@\\"):
        raise ValueError("network permission host contains URL syntax")
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        pass
    if ":" in host:
        raise ValueError("network permission IPv6 host must be numeric and unbracketed")
    try:
        ascii_host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as exc:
        raise ValueError("network permission host is not valid IDNA") from exc
    if not ascii_host or len(ascii_host) > 253:
        raise ValueError("network permission DNS host has an invalid length")
    labels = ascii_host.split(".")
    if any(len(label) > 63 or _DNS_LABEL.fullmatch(label) is None for label in labels):
        raise ValueError("network permission host contains an invalid DNS label")
    return ascii_host


def _canonical_origin(origin: Any) -> str:
    if type(origin) is not str or not origin or origin != origin.strip() or "\x00" in origin:
        raise ValueError("network permission origin must be non-empty text")
    try:
        parsed = urllib.parse.urlsplit(origin)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("network permission origin is malformed") from exc
    if scheme not in {"http", "https"}:
        raise ValueError("network permission origin must use HTTP or HTTPS")
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("network permission origin must have a host without user information")
    if parsed.path or parsed.query or parsed.fragment or "?" in origin or "#" in origin:
        raise ValueError("network permission origin must not include path, query, or fragment")
    if port is None or not 1 <= port <= 65535:
        raise ValueError("network permission origin must have an explicit valid port")
    canonical_host = _canonical_host(hostname)
    try:
        is_ipv6 = isinstance(ipaddress.ip_address(canonical_host), ipaddress.IPv6Address)
    except ValueError:
        is_ipv6 = False
    display_host = f"[{canonical_host}]" if is_ipv6 else canonical_host
    canonical = f"{scheme}://{display_host}:{port}"
    if origin != canonical:
        raise ValueError("network permission origin is not canonical")
    return canonical


def _normalise_event(event: Any, session_id: Any) -> tuple[dict[str, Any], str | None]:
    if type(event) is not dict or frozenset(event) != _EVENT_KEYS:
        raise ValueError("network permission event has an unsupported shape")
    if event["type"] != "network_permission":
        raise ValueError("network permission event type is invalid")
    request_id = event["request_id"]
    if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
        raise ValueError("network permission request_id must be 32 lowercase hex characters")
    choice = event["choice"]
    if type(choice) is not str or choice not in _CHOICES:
        raise ValueError("network permission choice is invalid")
    allowed = event["allowed"]
    if type(allowed) is not bool or allowed != (choice != "deny"):
        raise ValueError("network permission allowed value contradicts choice")
    scope = event["scope"]
    if type(scope) is not dict or frozenset(scope) != _SCOPE_KEYS:
        raise ValueError("network permission scope has an unsupported shape")

    origin = _canonical_origin(scope["origin"])
    connection_host = _canonical_host(scope["connection_host"])
    if scope["connection_host"] != connection_host:
        raise ValueError("network permission connection_host is not canonical")
    connection_port = scope["connection_port"]
    if type(connection_port) is not int or not 1 <= connection_port <= 65535:
        raise ValueError("network permission connection_port is invalid")
    via_proxy = scope["via_proxy"]
    if type(via_proxy) is not bool:
        raise ValueError("network permission via_proxy must be boolean")

    raw_addresses = scope["addresses"]
    if type(raw_addresses) is not list or not raw_addresses:
        raise ValueError("network permission addresses must be a non-empty list")
    addresses: list[str] = []
    for address in raw_addresses:
        if type(address) is not str or "%" in address:
            raise ValueError("network permission addresses must be unscoped numeric IPs")
        try:
            canonical_address = ipaddress.ip_address(address).compressed.lower()
        except ValueError as exc:
            raise ValueError("network permission address is not numeric") from exc
        if address != canonical_address:
            raise ValueError("network permission address is not canonical")
        addresses.append(canonical_address)
    if addresses != sorted(set(addresses)):
        raise ValueError("network permission addresses must be sorted and unique")

    if session_id is not None and (
        type(session_id) is not str
        or not session_id
        or session_id != session_id.strip()
        or "\x00" in session_id
    ):
        raise ValueError("network permission session_id must be non-empty text or null")

    canonical_event = {
        "type": "network_permission",
        "scope": {
            "origin": origin,
            "connection_host": connection_host,
            "connection_port": connection_port,
            "addresses": addresses,
            "via_proxy": via_proxy,
        },
        "request_id": request_id,
        "choice": choice,
        "allowed": allowed,
    }
    return canonical_event, session_id


def append_decision(
    conn: sqlite3.Connection,
    event: Any,
    *,
    session_id: str | None,
    created_at: str,
) -> int:
    """Append one decision and its ordered IP set inside the caller's transaction."""
    event, session_id = _normalise_event(event, session_id)
    if type(created_at) is not str or not created_at or "\x00" in created_at:
        raise ValueError("network permission created_at must be non-empty text")
    scope = event["scope"]
    cursor = conn.execute(
        """
        INSERT INTO network_permission_decisions(
          request_id,choice,allowed,origin,connection_host,connection_port,
          via_proxy,address_count,session_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event["request_id"], event["choice"], int(event["allowed"]),
            scope["origin"], scope["connection_host"], scope["connection_port"],
            int(scope["via_proxy"]), len(scope["addresses"]), session_id, created_at,
        ),
    )
    decision_id = int(cursor.lastrowid)
    conn.executemany(
        """
        INSERT INTO network_permission_decision_addresses(
          decision_id,address_order,address
        ) VALUES(?,?,?)
        """,
        ((decision_id, order, address) for order, address in enumerate(scope["addresses"])),
    )
    return decision_id


def list_decisions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read and validate stored decisions in append order."""
    result: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM network_permission_decisions ORDER BY decision_id"
    ):
        address_rows = list(conn.execute(
            """
            SELECT address_order,address FROM network_permission_decision_addresses
            WHERE decision_id=? ORDER BY address_order
            """,
            (row["decision_id"],),
        ))
        if (
            type(row["address_count"]) is not int
            or row["address_count"] < 1
            or len(address_rows) != row["address_count"]
            or [item["address_order"] for item in address_rows] != list(range(row["address_count"]))
        ):
            raise RuntimeError("network permission address rows contradict stored count")
        if type(row["allowed"]) is not int or row["allowed"] not in (0, 1):
            raise RuntimeError("network permission allowed value is malformed")
        if type(row["via_proxy"]) is not int or row["via_proxy"] not in (0, 1):
            raise RuntimeError("network permission via_proxy value is malformed")
        event = {
            "type": "network_permission",
            "scope": {
                "origin": row["origin"],
                "connection_host": row["connection_host"],
                "connection_port": row["connection_port"],
                "addresses": [item["address"] for item in address_rows],
                "via_proxy": bool(row["via_proxy"]),
            },
            "request_id": row["request_id"],
            "choice": row["choice"],
            "allowed": bool(row["allowed"]),
        }
        try:
            event, session_id = _normalise_event(event, row["session_id"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("stored network permission decision is malformed") from exc
        created_at = row["created_at"]
        if type(created_at) is not str or not created_at or "\x00" in created_at:
            raise RuntimeError("stored network permission created_at is malformed")
        result.append({
            "decision_id": row["decision_id"],
            "created_at": created_at,
            "session_id": session_id,
            "event": event,
        })
    return result
