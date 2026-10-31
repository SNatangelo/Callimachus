"""Standalone tests of the actual terminal emission functions, no app startup."""
import argparse
import ast
import contextlib
import io
from pathlib import Path
import sys
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "core/app/commands/present.py"

def functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {"_terminal_safe", "main", "present"}]
    scope = {"argparse": argparse, "sys": sys, "__doc__": "presentation", "format_run_examples": lambda x: x, "REFUSED": 21, "os": os, "_BAR": "="*72}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope

class PresentControlsTests(unittest.TestCase):
    def test_readable_unicode_and_layout_preserved(self):
        self.assertEqual(functions()["_terminal_safe"]("città λ\n\tα"), "città λ\n\tα")

    def test_escape_bell_carriage_and_c1_are_visible(self):
        self.assertEqual(functions()["_terminal_safe"]("\x1b]text\x07\r\x9b"), r"\x1b]text\x07\x0d\x9b")

    def test_bidi_controls_are_visible(self):
        self.assertEqual(functions()["_terminal_safe"]("a\u202eb\u2069"), r"a\u202eb\u2069")

    def emit(self, ok, text):
        scope=functions();scope["present"]=lambda run: (ok,text)
        output=io.StringIO()
        with mock.patch.object(sys,"argv",["present","--run","dummy"]), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            scope["main"]()
        return output.getvalue(), exit.exception.code

    def test_ordinary_output_unchanged(self):
        self.assertEqual(self.emit(True,"plain report"),("plain report\n",0))

    def test_controls_are_escaped_and_view_is_labelled(self):
        value="shown verbatim by the deterministic system\nEverything between these banners is the signed, code-produced report.\n\x1b]dummy\x07"
        output,code=self.emit(True,value)
        self.assertEqual(code,0)
        self.assertNotIn("\x1b",output);self.assertNotIn("\x07",output)
        self.assertIn("Terminal-safe view",output);self.assertIn("shown verbatim",output)
        self.assertIn(r"\x1b]dummy\x07",output)

    def test_refusal_exit_code_and_control_safety(self):
        output,code=self.emit(False,"WITHHELD\x1b")
        self.assertEqual(code,21);self.assertIn(r"WITHHELD\x1b",output)
        self.assertNotIn("\x1b",output)

    def test_quoted_banner_phrases_in_report_body_are_not_rewritten(self):
        scope=functions()
        scope["_verify_run"]=SimpleNamespace(verify=lambda *a, **kw: {"ok":True,"info":{}})
        scope["_signing"]=SimpleNamespace(key_present=lambda:False)
        body="shown verbatim by the deterministic system\nEverything between these banners is the signed, code-produced report.\n\x1b"
        with tempfile.TemporaryDirectory(dir=globals().get("SECURITY_TEST_SCRATCH", SOURCE.parents[3])) as folder:
            report=Path(folder)/"report.md";report.write_text(body,encoding="utf-8")
            ok,text=scope["present"](folder)
            self.assertTrue(ok);self.assertIn("terminal-safe view",text)
            self.assertIn(body,text);self.assertEqual(report.read_text(encoding="utf-8"),body)
