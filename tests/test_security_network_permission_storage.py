"""stdlib-only regression coverage for the consent audit callback and storage."""

from __future__ import annotations

import ast
import copy
import importlib.util
import sqlite3
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]
_STORAGE_PATH = _ROOT / "core" / "infra" / "db" / "network_permission_storage.py"
_REPOSITORY_PATH = _ROOT / "core" / "infra" / "db" / "repository.py"
_DRIVER_PATH = _ROOT / "core" / "app" / "run.py"


def _load_storage():
    spec = importlib.util.spec_from_file_location(
        "network_permission_storage_under_test", _STORAGE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load network permission storage module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _class_method(class_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return copy.deepcopy(node)
    raise RuntimeError(f"method {name!r} was not found")


def _load_repository_probe(storage):
    tree = ast.parse(_REPOSITORY_PATH.read_text(encoding="utf-8"))
    repository_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RunRepository"
    )
    probe_class = ast.ClassDef(
        name="RunRepositoryProbe",
        bases=[],
        keywords=[],
        body=[
            _class_method(repository_class, "_write_transaction"),
            _class_method(repository_class, "append_network_permission"),
        ],
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[probe_class], type_ignores=[]))
    namespace = {
        "Any": Any,
        "contextmanager": contextmanager,
        "_DB_WRITE_LOCK": threading.RLock(),
        "_append_network_permission_decision": storage.append_decision,
        "_now": lambda: "2026-10-25T00:00:00Z",
        "require_current_schema_structure": lambda _conn: 118,
    }
    exec(compile(module, str(_REPOSITORY_PATH), "exec"), namespace)

    class ProbeRepository(namespace["RunRepositoryProbe"]):
        active = None

        @classmethod
        def open(cls, _run_dir):
            return cls.active

        def close(self):
            # Keep the in-memory SQLite handle available to the test assertions.
            return None

    return ProbeRepository


def _load_driver_callback(repository_type):
    tree = ast.parse(_DRIVER_PATH.read_text(encoding="utf-8"))
    callback = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_record_network_permission"
    )
    callback = copy.deepcopy(callback)
    # Resolve the repository class from the harness namespace while keeping the
    # actual driver callback body and call site unchanged.
    callback.body = [
        node for node in callback.body
        if not (
            isinstance(node, ast.ImportFrom)
            and node.module == "core.infra.db"
        )
    ]
    module = ast.fix_missing_locations(ast.Module(body=[callback], type_ignores=[]))
    namespace = {"RunRepository": repository_type, "_ACTIVE_RUN_DIR": "memory-run", "_ACTIVE_DRIVER_SESSION": None}
    exec(compile(module, str(_DRIVER_PATH), "exec"), namespace)
    return namespace["_record_network_permission"], namespace


def _event(request_id: str = "a" * 32, *, choice: str = "once", allowed: bool = True):
    return {
        "type": "network_permission",
        "scope": {
            "origin": "https://example.org:443",
            "connection_host": "example.org",
            "connection_port": 443,
            "addresses": ["2001:db8::1", "203.0.113.8"],
            "via_proxy": True,
        },
        "request_id": request_id,
        "choice": choice,
        "allowed": allowed,
    }


class NetworkPermissionStorageTests(unittest.TestCase):
    def setUp(self):
        self.storage = _load_storage()
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute(
            "CREATE TABLE run_sessions(session_id TEXT PRIMARY KEY)"
        )
        self.connection.executescript(self.storage.NETWORK_PERMISSION_DDL)
        self.connection.executescript(self.storage.NETWORK_PERMISSION_TRIGGERS)
        self.repository_type = _load_repository_probe(self.storage)
        self.repository = self.repository_type()
        self.repository._conn = self.connection
        self.repository._schema_version = 118
        self.repository_type.active = self.repository

    def tearDown(self):
        self.connection.close()

    def test_actual_driver_callback_persists_typed_decision_and_ip_set(self):
        self.connection.execute(
            "INSERT INTO run_sessions(session_id) VALUES(?)", ("session-1",)
        )
        callback, callback_globals = _load_driver_callback(self.repository_type)
        callback_globals["_ACTIVE_DRIVER_SESSION"] = ("driver", "session-1")

        callback(_event())

        records = self.storage.list_decisions(self.connection)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["session_id"], "session-1")
        self.assertEqual(records[0]["created_at"], "2026-10-25T00:00:00Z")
        self.assertEqual(records[0]["event"], _event())

    def test_all_choices_are_appended_including_denial(self):
        for index, (choice, allowed) in enumerate((
            ("deny", False), ("once", True), ("session", True), ("always", True),
        )):
            self.repository.append_network_permission(
                _event(f"{index:032x}", choice=choice, allowed=allowed),
            )

        records = self.storage.list_decisions(self.connection)
        self.assertEqual(
            [(item["event"]["choice"], item["event"]["allowed"]) for item in records],
            [("deny", False), ("once", True), ("session", True), ("always", True)],
        )

    def test_closed_payload_and_canonical_scope_validation_prevent_partial_rows(self):
        invalid_events = []
        contradictory = _event(choice="deny", allowed=True)
        invalid_events.append(contradictory)
        extra_key = _event()
        extra_key["payload"] = {}
        invalid_events.append(extra_key)
        invalid_origin = _event()
        invalid_origin["scope"]["origin"] = "https://example.org:443/path"
        invalid_events.append(invalid_origin)
        unsorted_ips = _event()
        unsorted_ips["scope"]["addresses"] = ["203.0.113.8", "2001:db8::1"]
        invalid_events.append(unsorted_ips)
        uppercase_request = _event("A" * 32)
        invalid_events.append(uppercase_request)

        for event in invalid_events:
            with self.subTest(event=event):
                with self.assertRaises(ValueError):
                    self.repository.append_network_permission(event)
        self.assertEqual(self.storage.list_decisions(self.connection), [])

    def test_duplicate_request_id_and_unknown_session_fail_closed(self):
        event = _event()
        self.repository.append_network_permission(event)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.append_network_permission(event)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.append_network_permission(
                _event("b" * 32), session_id="missing-session",
            )
        self.assertEqual(len(self.storage.list_decisions(self.connection)), 1)

    def test_parent_and_ip_rows_are_append_only_and_child_order_is_bounded(self):
        decision_id = self.repository.append_network_permission(_event())
        operations = (
            (
                "UPDATE network_permission_decisions SET choice='once' WHERE decision_id=?",
                (decision_id,),
            ),
            (
                "DELETE FROM network_permission_decisions WHERE decision_id=?",
                (decision_id,),
            ),
            (
                "UPDATE network_permission_decision_addresses SET address='203.0.113.9' WHERE decision_id=? AND address_order=0",
                (decision_id,),
            ),
            (
                "DELETE FROM network_permission_decision_addresses WHERE decision_id=? AND address_order=0",
                (decision_id,),
            ),
            (
                "INSERT INTO network_permission_decision_addresses(decision_id,address_order,address) VALUES(?,?,?)",
                (decision_id, 2, "198.51.100.9"),
            ),
        )
        for sql, params in operations:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.connection.execute(sql, params)

    def test_replace_cannot_rewrite_existing_decision_or_ip_without_recursive_triggers(self):
        self.assertEqual(self.connection.execute("PRAGMA recursive_triggers").fetchone()[0], 0)
        decision_id = self.repository.append_network_permission(_event())
        operations = (
            (
                "INSERT OR REPLACE INTO network_permission_decision_addresses(decision_id,address_order,address) VALUES(?,?,?)",
                (decision_id, 0, "2001:db8::2"),
            ),
            (
                "INSERT OR REPLACE INTO network_permission_decisions "
                "SELECT decision_id,request_id,choice,allowed,'https://changed.example:443',"
                "connection_host,connection_port,via_proxy,address_count,session_id,created_at "
                "FROM network_permission_decisions WHERE decision_id=?",
                (decision_id,),
            ),
        )
        for sql, params in operations:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.connection.execute(sql, params)

    def test_implicit_rowid_cannot_replace_a_historical_ip_from_another_decision(self):
        decision_id = self.repository.append_network_permission(_event())
        cursor = self.connection.execute(
            "INSERT INTO network_permission_decisions("
            "request_id,choice,allowed,origin,connection_host,connection_port,"
            "via_proxy,address_count,session_id,created_at) "
            "SELECT ?,choice,allowed,origin,connection_host,connection_port,"
            "via_proxy,address_count,session_id,created_at "
            "FROM network_permission_decisions WHERE decision_id=?",
            ("b" * 32, decision_id),
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.connection.execute(
                "INSERT OR REPLACE INTO network_permission_decision_addresses"
                "(rowid,decision_id,address_order,address) VALUES(1,?,0,?)",
                (cursor.lastrowid, "2001:db8::2"),
            )


if __name__ == "__main__":
    unittest.main()
