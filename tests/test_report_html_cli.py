#!/usr/bin/env python3
# tests/test_report_html_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused companion-seal and command-surface checks."""

from tests._bootstrap import *  # noqa: F401,F403
from contextlib import redirect_stdout
import hashlib
import json
import sqlite3
from io import StringIO
from pathlib import Path
from unittest import mock
from core.report.human import cli
from core.report.human.cli import _parser
from core.report.human.projection import HumanReportProjection
from core.report.human import public_export
from core.report.human.public_export import export_public_html
from core.report.human.render import render_human_report
from core.report.human.sealing import (
    parse_html_seal,
    seal_html_body,
    strip_html_seal,
    verify_html_report,
    write_html_report,
)
def _projection():
    return HumanReportProjection({
        "schema_version": 1,
        "manuscript": {"title": "Test"},
        "overview": {}, "attention": [], "configuration": {}, "models": {},
        "claims": [], "sources": [], "pairs": [], "diagnostics": {},
    })
class TestHumanHtmlCli(unittest.TestCase):
    @staticmethod
    def _digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_public_export_requires_completed_semantic_verify_and_valid_html_seal(self):
        from core.infra.db.repository import RunRepository
        from tests.test_run_report import TestVerifyRunGate

        def make_html(run):
            fixture = TestVerifyRunGate()
            fixture._build_claim_evidence_run(run, [{
                "claim_id": "c1", "ref_id": "r1",
                "source_text": "The sky appears blue because of Rayleigh scattering.",
                "outcome": "supports", "pair_status": "accepted",
                "terminal_outcome": "supports", "terminal_cause": "jury2_accepted",
                "terminal_resolution": "jury2_accepted", "terminal_assurance": "passed",
                "evidence": ["blue because of Rayleigh scattering"],
                "grounded": [{"span_id": "span-1", "text": "blue because of Rayleigh scattering"}],
            }])
            repo = RunRepository.open(run)
            try:
                repo.update_run_settings({"references_only": False})
                repo.update_run_phase("done")
                repo.update_run_status("completed")
            finally:
                repo.close()
            fixture._write_report(run, sign=True)
            with mock.patch.object(sys, "argv", ["report-html", "--run", run]), redirect_stdout(StringIO()):
                self.assertEqual(cli.main(), 0)

        with tempfile.TemporaryDirectory() as temporary:
            run = os.path.join(temporary, "complete")
            os.mkdir(run)
            make_html(run)
            original = Path(run, "report.html").read_bytes()
            with self.assertRaisesRegex(ValueError, "outside the run directory"):
                export_public_html(run, os.path.join(run, "report.public.html"))
            self.assertFalse(Path(run, "report.public.html").exists())
            output = os.path.join(temporary, "public.html")
            self.assertEqual(export_public_html(run, output), output)
            self.assertTrue(Path(output).is_file())
            self.assertTrue(Path(output + ".redaction.json").is_file())
            self.assertEqual(Path(run, "report.html").read_bytes(), original)
            with self.assertRaisesRegex(ValueError, "already exists"):
                export_public_html(run, output)
            command_output = os.path.join(temporary, "public-from-cli.html")
            with redirect_stdout(StringIO()):
                self.assertEqual(public_export.main([
                    "--run", run, "--output", command_output,
                ]), 0)
            self.assertTrue(Path(command_output + ".redaction.json").is_file())

            references_only = os.path.join(temporary, "references-only")
            os.mkdir(references_only)
            make_html(references_only)
            repo = RunRepository.open(references_only)
            try:
                repo.update_run_settings({"references_only": True})
            finally:
                repo.close()
            with self.assertRaisesRegex(ValueError, "semantic Verify"):
                export_public_html(references_only, os.path.join(temporary, "refs.html"))

            incomplete = os.path.join(temporary, "incomplete")
            os.mkdir(incomplete)
            make_html(incomplete)
            repo = RunRepository.open(incomplete)
            try:
                repo.update_run_phase("verify")
            finally:
                repo.close()
            with self.assertRaisesRegex(ValueError, "completed run"):
                export_public_html(incomplete, os.path.join(temporary, "incomplete.html"))

            tampered = os.path.join(temporary, "tampered")
            os.mkdir(tampered)
            make_html(tampered)
            report = Path(tampered, "report.html")
            report.write_bytes(report.read_bytes().replace(b"<!doctype html>", b"<!DOCTYPE html>", 1))
            with self.assertRaisesRegex(ValueError, "body|seal|verification"):
                export_public_html(tampered, os.path.join(temporary, "tampered.html"))
