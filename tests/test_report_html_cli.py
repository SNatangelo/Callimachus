#!/usr/bin/env python3
# tests/test_report_html_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused companion-seal and command-surface checks."""
from tests._bootstrap import *  # noqa: F401,F403

from contextlib import closing, redirect_stdout
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

    def test_retroactive_command_accepts_existing_run_and_preview_switch(self):
        args = _parser().parse_args([
            "--run", "runs/existing", "--locale", "it", "--theme", "callimachus",
            "--preview-unverified",
        ])
        self.assertEqual(args.run, "runs/existing")
        self.assertTrue(args.preview_unverified)
        self.assertEqual(args.locale, "it")

    def test_seal_is_final_reversible_and_binds_the_unsealed_body(self):
        rendered = render_human_report(_projection())
        sealed = seal_html_body(rendered, b"canonical markdown bytes")

        self.assertEqual(strip_html_seal(sealed), rendered.body)
        meta = parse_html_seal(sealed)
        self.assertEqual(meta["format"], "html")
        self.assertEqual(meta["locale"], rendered.locale)
        self.assertEqual(meta["projection_sha256"], rendered.projection_sha256)
        self.assertNotEqual(strip_html_seal(sealed.replace("<!doctype", "<!DOCTYPE", 1)), rendered.body)
        with self.assertRaises(ValueError):
            parse_html_seal(sealed + "x")

    def test_existing_completed_run_generates_and_verifies_without_mutating_canonical_files(self):
        from tests.test_run_report import TestVerifyRunGate

        with tempfile.TemporaryDirectory() as temporary:
            run = TestVerifyRunGate()._build_run(temporary, with_verdict=True, sign=True)
            protected = {
                name: self._digest(os.path.join(run, name))
                for name in ("run.sqlite", "report.md", "report.journal.md")
            }
            argv = ["report-html", "--run", run, "--locale", "it", "--theme", "callimachus"]
            with mock.patch.object(sys, "argv", argv), redirect_stdout(StringIO()):
                self.assertEqual(cli.main(), 0)

            html_path = os.path.join(run, "report.html")
            first = Path(html_path).read_bytes()
            result = verify_html_report(run)

            self.assertTrue(result["ok"], result["failures"])
            self.assertEqual(
                protected,
                {name: self._digest(os.path.join(run, name)) for name in protected},
            )

            with mock.patch.object(sys, "argv", argv), redirect_stdout(StringIO()):
                self.assertEqual(cli.main(), 0)
            self.assertEqual(first, Path(html_path).read_bytes())

            verify_output = StringIO()
            with mock.patch.object(
                sys, "argv", ["report-html", "--run", run, "--verify"]
            ), redirect_stdout(verify_output):
                self.assertEqual(cli.main(), 0)
            self.assertTrue(json.loads(verify_output.getvalue())["ok"])

            Path(html_path).write_text(
                Path(html_path).read_text(encoding="utf-8").replace(
                    "<!doctype html>", "<!DOCTYPE html>", 1
                ),
                encoding="utf-8",
            )
            self.assertFalse(verify_html_report(run)["ok"])

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

    def test_report_phase_generates_a_verified_companion_by_default(self):
        from core.app.phases import report_gate
        from tests.test_run_report import TestVerifyRunGate

        with tempfile.TemporaryDirectory() as run, mock.patch.dict(
            os.environ, {}, clear=False
        ):
            os.environ.pop(report_gate.ENV_REPORT_HTML, None)
            TestVerifyRunGate()._build_run(run, with_verdict=True, sign=True)

            with redirect_stdout(StringIO()):
                self.assertEqual(report_gate.phase_report({"run_dir": run}), "done")

            self.assertTrue(Path(run, "report.html").is_file())
            result = verify_html_report(run)
            self.assertTrue(result["ok"], result["failures"])
            self.assertEqual(
                parse_html_seal(Path(run, "report.html").read_text(encoding="utf-8"))["locale"],
                "en",
            )

    def test_run_projection_keeps_fetch_attempts_without_resolver_abstract(self):
        from core.infra.db import RunRepository
        from core.report.io import load_run_projection
        from tests.test_run_report import TestVerifyRunGate

        with tempfile.TemporaryDirectory() as run:
            TestVerifyRunGate()._build_run(run, with_verdict=False, sign=True)
            repo = RunRepository.open(run)
            try:
                repo.update_run_settings({
                    "debug_mode": True,
                    "debug_labels": ["fork:test"],
                })
                repo.append_fetch_attempt(
                    "r1",
                    method="GET",
                    url="https://example.test/source.pdf",
                    kind="pdf",
                    final_url="https://example.test/source.pdf",
                    status_code=404,
                    content_type="text/html",
                    outcome="http_error",
                    reason="http_404",
                )
            finally:
                repo.close()

            projection = load_run_projection(run)

        self.assertEqual(
            projection["fetch_attempts_by_ref"]["r1"][0]["outcome"],
            "http_error",
        )
        self.assertEqual(projection["execution"], {
            "debug_mode": True,
            "debug_labels": ["fork:test"],
        })

    def test_unverified_run_requires_explicit_unsealed_preview(self):
        from tests.test_run_report import TestVerifyRunGate

        with tempfile.TemporaryDirectory() as temporary:
            run = TestVerifyRunGate()._build_run(temporary, with_verdict=True, sign=True)
            report_path = os.path.join(run, "report.md")
            with open(report_path, "a", encoding="utf-8") as stream:
                stream.write("tampered")

            with mock.patch.object(sys, "argv", ["report-html", "--run", run]):
                with self.assertRaises(SystemExit):
                    cli.main()
            self.assertFalse(os.path.exists(os.path.join(run, "report.html")))

            with mock.patch.object(
                sys, "argv", ["report-html", "--run", run, "--preview-unverified"]
            ), redirect_stdout(StringIO()):
                self.assertEqual(cli.main(), 0)

            preview = Path(run, "report.preview.html").read_text(encoding="utf-8")
            self.assertIn('"preview":true', preview)
            self.assertIn('"preview_failures":[', preview)
            with self.assertRaises(ValueError):
                parse_html_seal(preview)

    def test_obsolete_schema_database_is_rejected_explicitly(self):
        from core.infra.db.schema import SCHEMA_VERSION
        from tests.test_run_report import TestVerifyRunGate

        with tempfile.TemporaryDirectory() as run:
            TestVerifyRunGate()._build_run(run, with_verdict=True, sign=True)
            with closing(sqlite3.connect(Path(run, "run.sqlite"))) as conn:
                conn.execute(
                    "UPDATE meta SET value='59' WHERE key='schema_version'"
                )
                conn.commit()

            with mock.patch.object(sys, "argv", ["report-html", "--run", run]):
                with self.assertRaisesRegex(
                    SystemExit,
                    rf"database schema version 59 is unsupported \(current: {SCHEMA_VERSION}\)",
                ):
                    cli.main()
            self.assertFalse(Path(run, "report.html").exists())

            with mock.patch.object(
                sys, "argv", ["report-html", "--run", run, "--verify"]
            ), redirect_stdout(StringIO()) as stdout:
                self.assertEqual(cli.main(), 1)
            self.assertIn(
                f"database schema version 59 is unsupported (current: {SCHEMA_VERSION})",
                stdout.getvalue(),
            )

    def test_writer_uses_report_checkpoint_without_content_store_lease(self):
        class Gate:
            def __init__(self):
                self.calls = []

            def begin_pipeline_transition(self, run, **kwargs):
                self.calls.append(("begin", run, kwargs))
                return "lease"

            def commit_pipeline_transition(self, run, lease):
                self.calls.append(("commit", run, lease))

            def abort_pipeline_transition(self, run, lease, *, reason):
                self.calls.append(("abort", run, lease, reason))

        with tempfile.TemporaryDirectory() as run:
            Path(run, "report.md").write_bytes(b"canonical markdown bytes")
            gate = Gate()
            path = write_html_report(run, rendered=render_human_report(_projection()), gate=gate)

        self.assertEqual(os.path.basename(path), "report.html")
        self.assertEqual(
            gate.calls,
            [
                ("begin", run, {"checkpoint_kind": "report", "mutates_content_store": False}),
                ("commit", run, "lease"),
            ],
        )

    def test_writer_rejects_preview_mode_mismatch(self):
        with tempfile.TemporaryDirectory() as run:
            Path(run, "report.md").write_bytes(b"canonical markdown bytes")
            rendered = render_human_report(_projection(), preview=True)

            with self.assertRaisesRegex(ValueError, "output mode"):
                write_html_report(run, rendered=rendered, preview=False)
            self.assertFalse(Path(run, "report.html").exists())
