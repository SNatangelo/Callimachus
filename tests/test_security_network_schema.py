# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the real current SQLite bootstrap without application startup."""
import ast
from contextlib import ExitStack
import importlib
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class NetworkPermissionSchemaTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        package = ModuleType("_security_network_schema")
        package.__path__ = [str(ROOT / "core/infra/db")]
        stack.enter_context(mock.patch.dict(sys.modules, {package.__name__: package}))
        self.schema = importlib.import_module(package.__name__ + ".schema")
        self.bootstrap = importlib.import_module(package.__name__ + ".schema_bootstrap")
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.bootstrap.ensure_schema(self.conn)

    def test_full_bootstrap_preserves_existing_current_schema_contract(self):
        self.assertEqual(self.bootstrap.require_current_schema_structure(self.conn), 118)
        # Reuse the existing broad schema assertions with the actual bootstrap;
        # only the unrelated application repository startup is omitted.
        path = ROOT / "tests/infra/test_current_run_schema.py"
        names = {"_table_exists", "test_fresh_current_schema_omits_removed_verification_objects"}
        tree = ast.parse(path.read_text(encoding="utf-8"))
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        repo = SimpleNamespace(_conn=self.conn, close=lambda: None)
        scope = {"sqlite3": sqlite3, "SCHEMA_VERSION": self.schema.SCHEMA_VERSION,
                 "_repo": lambda _: repo,
                 "_REMOVED": tuple(ast.literal_eval(node.value) for node in tree.body
                                   if isinstance(node, ast.Assign)
                                   and any(isinstance(target, ast.Name) and target.id == "_REMOVED"
                                           for target in node.targets))[0]}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), scope)
        scope["test_fresh_current_schema_omits_removed_verification_objects"](Path("unused"))

    def test_current_schema_requires_network_audit_guard(self):
        self.conn.execute("DROP TRIGGER network_permission_decisions_no_update")
        with self.assertRaisesRegex(RuntimeError, "missing required current objects.*network_permission_decisions_no_update"):
            self.bootstrap.require_current_schema_structure(self.conn)
