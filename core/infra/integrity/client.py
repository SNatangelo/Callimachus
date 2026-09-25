# core/infra/integrity/client.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Length-framed Unix-socket client for the external integrity authority."""

from __future__ import annotations

import json
import os
import socket
import struct
from typing import Any

from .authority import AUTHORITY_PROTOCOL_VERSION, AuthorityError


MAX_FRAME_BYTES = 64 * 1024 * 1024


class AuthorityUnavailable(AuthorityError):
    pass


class AuthorityRejected(AuthorityError):
    pass


def _recv_exact(connection: socket.socket, byte_count: int) -> bytes:
    chunks = []
    remaining = byte_count
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise AuthorityUnavailable("integrity authority closed an incomplete response")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_frame(connection: socket.socket, payload: dict[str, Any]) -> None:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        raise AuthorityError("integrity authority frame exceeds the closed size limit")
    connection.sendall(struct.pack(">Q", len(encoded)) + encoded)


def receive_frame(connection: socket.socket) -> dict[str, Any]:
    size = struct.unpack(">Q", _recv_exact(connection, 8))[0]
    if size > MAX_FRAME_BYTES:
        raise AuthorityError("integrity authority frame exceeds the closed size limit")
    try:
        payload = json.loads(_recv_exact(connection, size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityUnavailable("integrity authority returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise AuthorityUnavailable("integrity authority response is not an object")
    return payload


class IntegrityAuthorityClient:
    def __init__(self, socket_path: str, *, timeout_seconds: float = 30.0) -> None:
        if not isinstance(socket_path, str) or not socket_path.strip():
            raise AuthorityUnavailable("integrity authority socket path is required")
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self._worker_capability: str | None = None
        self._worker_run_dir: str | None = None
        self._admin_capability: str | None = None

    def _request(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        worker_capability: str | None,
    ) -> dict[str, Any]:
        request = {
            "version": AUTHORITY_PROTOCOL_VERSION,
            "operation": operation,
            "payload": payload,
            "worker_capability": worker_capability,
        }
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout_seconds)
        try:
            connection.connect(self.socket_path)
            send_frame(connection, request)
            response = receive_frame(connection)
        except (OSError, TimeoutError) as exc:
            raise AuthorityUnavailable("integrity authority is unavailable") from exc
        finally:
            connection.close()
        if response.get("version") != AUTHORITY_PROTOCOL_VERSION:
            raise AuthorityUnavailable("integrity authority protocol version is invalid")
        if response.get("ok") is not True:
            message = response.get("message")
            raise AuthorityRejected(
                message if isinstance(message, str) and message else "integrity authority rejected request"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise AuthorityUnavailable("integrity authority result is malformed")
        return result

    def request(self, operation: str, **payload: Any) -> dict[str, Any]:
        return self._request(operation, payload, worker_capability=None)

    @staticmethod
    def _capability(result: dict[str, Any], expected_scope: str) -> str:
        capability = result.get("capability")
        if (
            not isinstance(capability, str)
            or not capability
            or result.get("scope") != expected_scope
        ):
            raise AuthorityUnavailable(
                "integrity authority returned a malformed writer capability"
            )
        return capability

    def activate_worker(self, run_dir: str) -> dict[str, Any]:
        canonical = os.path.realpath(os.path.abspath(run_dir))
        if (
            self._worker_run_dir == canonical
            and self._worker_capability is not None
        ):
            return {
                "capability": self._worker_capability,
                "run_dir": canonical,
                "scope": "worker",
            }
        result = self.request("activate_worker", run_dir=run_dir)
        capability = self._capability(result, "worker")
        if result.get("run_dir") != canonical:
            raise AuthorityUnavailable(
                "integrity authority returned a capability for a different run"
            )
        self._worker_capability = capability
        self._worker_run_dir = canonical
        return result

    def activate_admin(self) -> dict[str, Any]:
        result = self.request("activate_admin")
        self._admin_capability = self._capability(result, "admin")
        return result

    def _worker_request(
        self, operation: str, run_dir: str, **payload: Any
    ) -> dict[str, Any]:
        canonical = os.path.realpath(os.path.abspath(run_dir))
        if self._worker_capability is None or self._worker_run_dir != canonical:
            raise AuthorityRejected(
                "Callimachus worker is not activated for this run"
            )
        return self._request(
            operation,
            {"run_dir": run_dir, **payload},
            worker_capability=self._worker_capability,
        )

    def enroll(self, run_dir: str) -> dict[str, Any]:
        return self._worker_request("enroll", run_dir)

    def check(self, run_dir: str) -> dict[str, Any]:
        return self.request("check", run_dir=run_dir)

    def begin_transition(
        self,
        run_dir: str,
        checkpoint_kind: str,
        *,
        transition_id: str,
        content_store_required: bool,
    ) -> dict[str, Any]:
        return self._worker_request(
            "begin_transition",
            run_dir,
            checkpoint_kind=checkpoint_kind,
            transition_id=transition_id,
            content_store_required=content_store_required,
        )

    def commit_transition(self, run_dir: str, lease_id: str) -> dict[str, Any]:
        return self._worker_request("commit_transition", run_dir, lease_id=lease_id)

    def abort_transition(
        self, run_dir: str, lease_id: str, reason: str
    ) -> dict[str, Any]:
        return self._worker_request(
            "abort_transition",
            run_dir,
            lease_id=lease_id,
            reason=reason,
        )

    def heartbeat_transition(
        self, run_dir: str, lease_id: str
    ) -> dict[str, Any]:
        return self._worker_request(
            "heartbeat_transition", run_dir, lease_id=lease_id
        )

    def recover_pipeline_transition(self, run_dir: str) -> dict[str, Any]:
        return self._worker_request("recover_pipeline_transition", run_dir)

    def override(self, run_dir: str, reason: str) -> dict[str, Any]:
        return self._worker_request("override", run_dir, reason=reason)

    def enroll_content_store(self) -> dict[str, Any]:
        if self._admin_capability is None:
            raise AuthorityRejected("Callimachus authority admin is not activated")
        return self._request(
            "content_store_enroll",
            {},
            worker_capability=self._admin_capability,
        )

    def check_content_store(self) -> dict[str, Any]:
        return self.request("content_store_check")

    def begin_content_store_transition(
        self, run_dir: str, transition_id: str
    ) -> dict[str, Any]:
        return self._worker_request(
            "content_store_begin_transition",
            run_dir,
            transition_id=transition_id,
        )

    def commit_content_store_transition(
        self, run_dir: str, lease_id: str
    ) -> dict[str, Any]:
        return self._worker_request(
            "content_store_commit_transition", run_dir, lease_id=lease_id
        )

    def abort_content_store_transition(
        self, run_dir: str, lease_id: str, reason: str
    ) -> dict[str, Any]:
        return self._worker_request(
            "content_store_abort_transition",
            run_dir,
            lease_id=lease_id,
            reason=reason,
        )

    def heartbeat_content_store_transition(
        self, run_dir: str, lease_id: str
    ) -> dict[str, Any]:
        return self._worker_request(
            "content_store_heartbeat_transition", run_dir, lease_id=lease_id
        )

    def override_content_store(self, run_dir: str, reason: str) -> dict[str, Any]:
        return self._worker_request("content_store_override", run_dir, reason=reason)

    def admit_task_answer(
        self,
        run_dir: str,
        task_id: str,
        raw_payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(
            "admit_task_answer",
            run_dir=run_dir,
            task_id=task_id,
            raw_payload=raw_payload,
        )
