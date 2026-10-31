# tests/test_security_ipc_receive_deadline.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import ast
import json
import socket
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class _AuthorityError(Exception):
    pass


class _AuthorityUnavailable(_AuthorityError):
    pass


class _AuthorityPermissionError(_AuthorityError):
    pass


class _WorkerCapabilityRegistry:
    pass


def _load_source_nodes(
    relative_path: str,
    names: set[str],
    namespace: dict[str, object],
) -> None:
    source_path = Path(__file__).resolve().parents[1] / relative_path
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    future_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
    ]
    selected_nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in names:
                selected_nodes.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assigned_names = {
                target.id
                for target in targets
                if isinstance(target, ast.Name)
            }
            if assigned_names & names:
                selected_nodes.append(node)
    module = ast.Module(body=future_imports + selected_nodes, type_ignores=[])
    exec(compile(module, str(source_path), "exec"), namespace)


def _load_client_namespace() -> dict[str, object]:
    namespace: dict[str, object] = {
        "__name__": "_ipc_client_under_test",
        "json": json,
        "socket": socket,
        "struct": struct,
        "time": SimpleNamespace(monotonic=lambda: 0.0),
        "AuthorityError": _AuthorityError,
        "AuthorityUnavailable": _AuthorityUnavailable,
    }
    _load_source_nodes(
        "core/infra/integrity/client.py",
        {"MAX_FRAME_BYTES", "_recv_exact", "send_frame", "receive_frame"},
        namespace,
    )
    return namespace


def _load_service_namespace(client_namespace: dict[str, object]) -> dict[str, object]:
    namespace: dict[str, object] = {
        "__name__": "_ipc_service_under_test",
        "os": SimpleNamespace(getuid=lambda: 2000, unlink=lambda _path: None),
        "time": SimpleNamespace(monotonic=lambda: 0.0),
        "receive_frame": client_namespace["receive_frame"],
        "send_frame": client_namespace["send_frame"],
        "dispatch_request": lambda *_args, **_kwargs: {"status": "clean"},
        "_peer_credentials": lambda _connection: (1, 1000, 1000),
        "_prepare_socket": lambda _socket_path: (None, "unused.sock"),
        "AuthorityError": _AuthorityError,
        "AuthorityPermissionError": _AuthorityPermissionError,
        "AUTHORITY_PROTOCOL_VERSION": 1,
        "WorkerCapabilityRegistry": _WorkerCapabilityRegistry,
    }
    _load_source_nodes(
        "core/infra/integrity/service.py",
        {"_REQUEST_RECEIVE_TIMEOUT_SECONDS", "_attest_service_check_result", "serve"},
        namespace,
    )
    return namespace


_CLIENT = _load_client_namespace()
_SERVICE = _load_service_namespace(_CLIENT)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class _TimedSocket:
    def __init__(self, clock: _Clock, chunks: list[tuple[float, bytes]]) -> None:
        self.clock = clock
        self.chunks = list(chunks)
        self.timeout: float | None = None
        self.timeout_history: list[float | None] = []

    def gettimeout(self) -> float | None:
        return self.timeout

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout
        self.timeout_history.append(timeout)

    def recv(self, _byte_count: int) -> bytes:
        delay, data = self.chunks.pop(0)
        if self.timeout is not None and delay > self.timeout:
            self.clock.now += self.timeout
            raise TimeoutError("simulated socket timeout")
        self.clock.now += delay
        return data


class _Connection:
    def __init__(self) -> None:
        self.closed = False
        self.sent_data: list[bytes] = []

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def sendall(self, data: bytes) -> None:
        self.sent_data.append(data)


class _Listener:
    def __init__(self, connections: list[_Connection]) -> None:
        self.connections = list(connections)
        self.accepted = 0
        self.closed = False

    def accept(self) -> tuple[_Connection, None]:
        self.accepted += 1
        return self.connections.pop(0), None

    def close(self) -> None:
        self.closed = True


class ReceiveDeadlineTests(unittest.TestCase):
    def test_complete_frame_keeps_existing_no_deadline_behavior(self) -> None:
        clock = _Clock()
        encoded = json.dumps({"ok": True}, separators=(",", ":")).encode("utf-8")
        connection = _TimedSocket(
            clock,
            [(0.0, struct.pack(">Q", len(encoded))), (0.0, encoded)],
        )

        self.assertEqual(_CLIENT["receive_frame"](connection), {"ok": True})
        self.assertEqual(connection.timeout_history, [])

    def test_partial_header_expires_at_the_absolute_deadline(self) -> None:
        clock = _Clock()
        connection = _TimedSocket(clock, [(0.0, b"\x00\x00\x00"), (4.0, b"\x00" * 5)])

        with mock.patch.dict(_CLIENT, {"time": SimpleNamespace(monotonic=clock.monotonic)}):
            with self.assertRaises(TimeoutError):
                _CLIENT["receive_frame"](connection, deadline=3.0)

        self.assertEqual(clock.now, 3.0)
        self.assertIsNone(connection.timeout)

    def test_body_trickling_cannot_restart_the_receive_timeout(self) -> None:
        clock = _Clock()
        encoded = b'{"ok":true,"x":"slow"}'
        chunks = [(0.0, struct.pack(">Q", len(encoded)))]
        chunks.extend((8.0, bytes((byte,))) for byte in encoded)
        connection = _TimedSocket(clock, chunks)

        with mock.patch.dict(_CLIENT, {"time": SimpleNamespace(monotonic=clock.monotonic)}):
            with self.assertRaises(TimeoutError):
                _CLIENT["receive_frame"](connection, deadline=30.0)

        self.assertEqual(clock.now, 30.0)
        self.assertEqual(connection.timeout_history[-2], 6.0)
        self.assertIsNone(connection.timeout)

    def test_expired_connection_is_closed_and_next_connection_is_served(self) -> None:
        timed_out, valid = _Connection(), _Connection()
        listener = _Listener([timed_out, valid])
        request = {"operation": "check"}
        receive = mock.Mock(side_effect=[TimeoutError("expired"), request])
        dispatch = mock.Mock(return_value={"status": "clean"})
        service_time = _SERVICE["time"]

        with (
            mock.patch.dict(
                _SERVICE,
                {
                    "_prepare_socket": lambda _path: (listener, "unused.sock"),
                    "_peer_credentials": lambda _connection: (1, 1000, 1000),
                    "receive_frame": receive,
                    "dispatch_request": dispatch,
                },
            ),
            mock.patch.object(service_time, "monotonic", side_effect=[100.0, 200.0]),
        ):
            _SERVICE["serve"](
                socket_path="unused.sock",
                authority=object(),
                trusted_writer_uids=frozenset(),
                max_requests=1,
            )

        self.assertEqual(listener.accepted, 2)
        self.assertTrue(timed_out.closed)
        self.assertTrue(valid.closed)
        self.assertTrue(listener.closed)
        self.assertEqual(
            [call.kwargs["deadline"] for call in receive.call_args_list],
            [100.0 + _SERVICE["_REQUEST_RECEIVE_TIMEOUT_SECONDS"],
             200.0 + _SERVICE["_REQUEST_RECEIVE_TIMEOUT_SECONDS"]],
        )
        dispatch.assert_called_once()
        self.assertEqual(timed_out.sent_data, [])
        self.assertEqual(len(valid.sent_data), 1)
        response_size = struct.unpack(">Q", valid.sent_data[0][:8])[0]
        response = json.loads(valid.sent_data[0][8:])
        self.assertEqual(response_size, len(valid.sent_data[0]) - 8)
        self.assertEqual(response["ok"], True)
        self.assertEqual(response["result"]["status"], "clean")


class RealSocketDeadlineTests(unittest.TestCase):
    def _check_partial(self, prefix):
        import socket
        import time
        peer, connection = socket.socketpair()
        try:
            peer.sendall(prefix)
            with mock.patch.object(_CLIENT["time"], "monotonic", time.monotonic):
                with self.assertRaises(TimeoutError):
                    _CLIENT["receive_frame"](connection, deadline=time.monotonic() + 0.02)
            self.assertIsNone(connection.gettimeout())
        finally:
            peer.close(); connection.close()

    def test_real_partial_header_expires(self):
        self._check_partial(b"xx")

    def test_real_partial_body_expires(self):
        self._check_partial(struct.pack(">Q", 5) + b"{")

    def test_real_complete_frame_preserves_socket_timeout(self):
        import socket
        import time
        peer, connection = socket.socketpair()
        try:
            connection.settimeout(1.0)
            _CLIENT["send_frame"](peer, {"ok": True})
            with mock.patch.object(_CLIENT["time"], "monotonic", time.monotonic):
                self.assertEqual(_CLIENT["receive_frame"](connection, deadline=time.monotonic()+1), {"ok": True})
            self.assertEqual(connection.gettimeout(), 1.0)
        finally:
            peer.close(); connection.close()


if __name__ == "__main__":
    unittest.main()
