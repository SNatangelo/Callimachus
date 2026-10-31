#!/usr/bin/env python3
# tests/test_report_html_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused companion-seal and command-surface checks."""
from tests._bootstrap import *  # noqa: F401,F403

from contextlib import redirect_stdout
import base64
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
from core.report.human import sealing as human_sealing
from core.report.human.sealing import (
    parse_html_seal,
    seal_html_body,
    strip_html_seal,
    verify_html_report,
    write_html_report,
)
from core.report.sealing import seal_payload


def _projection():
    return HumanReportProjection({
        "schema_version": 1,
        "manuscript": {"title": "Test"},
        "overview": {}, "attention": [], "configuration": {}, "models": {},
        "claims": [], "sources": [], "pairs": [], "diagnostics": {},
    })


_HISTORICAL_SIGNING_KEY = "historical-export-test-key"
_HISTORICAL_LONG_TEXT = (
    "CONFIDENTIAL_UNPUBLISHED_SOURCE_TEXT "
    "supporting material should be removed from every public historical export. "
) * 8


def _historical_projection(**overrides):
    value = {
        "schema_version": 1,
        "execution": {"debug_mode": False, "debug_labels": []},
        "manuscript": {"title": "Historical artifact"},
        "overview": {},
        "attention": [],
        "configuration": {},
        "models": {},
        "claims": [{
            "claim": {"context_window": _HISTORICAL_LONG_TEXT, "sentence": "Claim [1]."},
            "focus_text": "Claim [1].",
        }],
        "sources": [{
            "reference": {"id": "r1", "title": "Source title"},
            "resolve": {"abstract": _HISTORICAL_LONG_TEXT},
        }],
        "pairs": [{
            "ref_id": "r1",
            "status": "accepted",
            "decision": {
                "verdict": "supports",
                "status": "accepted",
                "explanation": "The cited source supports the claim.",
            },
        }],
        "diagnostics": {},
    }
    value.update(overrides)
    return HumanReportProjection(value)


def _historical_sealed_html(
    *,
    key=None,
    projection=None,
    preview=False,
    metadata_changes=None,
    valid_signature=True,
):
    rendered = render_human_report(
        projection or _historical_projection(),
        preview=preview,
        preview_failures=("fixture preview",) if preview else (),
    )
    with mock.patch.object(public_export.signing, "load_key", return_value=key):
        html = seal_html_body(rendered, b"historical report markdown")
        if not metadata_changes and valid_signature:
            return html
        body = strip_html_seal(html)
        meta = parse_html_seal(html)
        meta.update(metadata_changes or {})
        fields = {
            name: meta[name]
            for name in (
                "schema", "format", "renderer", "report_md_sha256",
                "projection_sha256", "locale", "theme", "asset_sha256",
            )
        }
        meta["body_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        payload = seal_payload(fields, meta["body_sha256"])
        meta["content"] = hashlib.sha256(payload).hexdigest()
        if valid_signature:
            signature = public_export.signing.sign(payload, key=key)
            meta["alg"] = signature["alg"]
            meta["sig"] = signature["sig"]
        else:
            meta["sig"] = "0" * 64
        encoded = base64.urlsafe_b64encode(
            json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return (
            f"{body}\n<!-- citation-verifier-human-report-seal data={encoded} -->\n"
        )


def _report_data(html):
    encoded = html.split('<script id="cv-data" type="application/json">', 1)[1]
    return json.loads(encoded.split("</script>", 1)[0])


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
            public_bytes = Path(output).read_bytes()
            manifest_bytes = Path(output + ".redaction.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "already exists"):
                export_public_html(run, output)
            self.assertEqual(Path(output).read_bytes(), public_bytes)
            self.assertEqual(Path(output + ".redaction.json").read_bytes(), manifest_bytes)

            Path(output).write_bytes(b"previous public HTML")
            Path(output + ".redaction.json").write_bytes(b"previous manifest")
            self.assertEqual(export_public_html(run, output, overwrite=True), output)
            public_bytes = Path(output).read_bytes()
            manifest_bytes = Path(output + ".redaction.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            self.assertNotEqual(public_bytes, b"previous public HTML")
            self.assertEqual(
                manifest["public_html_sha256"], hashlib.sha256(public_bytes).hexdigest()
            )
            self.assertEqual(Path(run, "report.html").read_bytes(), original)

            real_replace = os.replace
            cancellation_requested = [False]

            def cancel_after_html_replacement(source, destination):
                result = real_replace(source, destination)
                if Path(destination) == Path(output):
                    cancellation_requested[0] = True
                return result

            with mock.patch.object(
                public_export.os, "replace", side_effect=cancel_after_html_replacement,
            ):
                with self.assertRaises(public_export.PublicExportCancelled):
                    export_public_html(
                        run, output, overwrite=True,
                        cancel_check=lambda: cancellation_requested[0],
                    )
            self.assertEqual(Path(output).read_bytes(), public_bytes)
            self.assertEqual(Path(output + ".redaction.json").read_bytes(), manifest_bytes)

            failed_sidecar_replace = [False]

            def fail_manifest_replacement_once(source, destination):
                if (
                    Path(destination) == Path(output + ".redaction.json")
                    and not failed_sidecar_replace[0]
                ):
                    failed_sidecar_replace[0] = True
                    raise OSError("simulated manifest replacement failure")
                return real_replace(source, destination)

            with mock.patch.object(
                public_export.os, "replace", side_effect=fail_manifest_replacement_once,
            ):
                with self.assertRaisesRegex(OSError, "simulated manifest replacement failure"):
                    export_public_html(run, output, overwrite=True)
            self.assertTrue(failed_sidecar_replace[0])
            self.assertEqual(Path(output).read_bytes(), public_bytes)
            self.assertEqual(Path(output + ".redaction.json").read_bytes(), manifest_bytes)

            cancelled_output = Path(temporary) / "cancelled.html"

            def cancel_after_html_write():
                return cancelled_output.is_file() and cancelled_output.stat().st_size > 0

            with self.assertRaises(public_export.PublicExportCancelled):
                export_public_html(
                    run, str(cancelled_output), cancel_check=cancel_after_html_write,
                )
            self.assertFalse(cancelled_output.exists())
            self.assertFalse(Path(str(cancelled_output) + ".redaction.json").exists())
            self.assertEqual(Path(run, "report.html").read_bytes(), original)
            command_output = os.path.join(temporary, "public-from-cli.html")
            with redirect_stdout(StringIO()):
                self.assertEqual(public_export.main([
                    "--run", run, "--output", command_output,
                ]), 0)
            self.assertTrue(Path(command_output + ".redaction.json").is_file())

            large_run = os.path.join(temporary, "large-source")
            os.mkdir(large_run)
            make_html(large_run)
            source_text = "漢" * 400_000
            source_bytes = source_text.encode("utf-8")
            repo = RunRepository.open(large_run)
            try:
                record = repo.list_source_texts()[0]
                source_path = Path(large_run, record.stored_path)
                source_path.write_bytes(source_bytes)
                repo.store_source_text(
                    source_text_id=record.source_text_id,
                    ref_id=record.ref_id,
                    identity_key=record.identity_key,
                    tier=record.tier,
                    origin=record.origin,
                    stored_path=record.stored_path,
                    sha256=hashlib.sha256(source_bytes).hexdigest(),
                    char_count=len(source_text),
                    source_ref=record.source_ref,
                    mapping=record.mapping,
                    match_signal=record.match_signal,
                    match_score=record.match_score,
                    identity_status=record.identity_status,
                    identity_note=record.identity_note,
                    content_version=record.content_version,
                    provenance_relation=record.provenance_relation,
                    supplied_by=record.supplied_by,
                    supplied_via=record.supplied_via,
                    file_format=record.file_format,
                    extraction_flags=record.extraction_flags,
                    extraction_method=record.extraction_method,
                    arxiv_version=record.arxiv_version,
                )
            finally:
                repo.close()
            TestVerifyRunGate()._write_report(large_run, sign=True)
            with mock.patch.object(
                sys, "argv", ["report-html", "--run", large_run]
            ), redirect_stdout(StringIO()):
                self.assertEqual(cli.main(), 0)
            from core.report.source_availability import usable_verification_source_refs
            repo = RunRepository.open_readonly(large_run)
            try:
                manifest = repo.source_manifest_payload()
            finally:
                repo.close()
            usable_refs, unavailable = usable_verification_source_refs(large_run, manifest)
            self.assertEqual(usable_refs, frozenset({"r1"}))
            self.assertFalse(unavailable)

            source_read_bytes = [0]
            real_path_open = Path.open

            class CountingReader:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def read(self, size=-1):
                    value = self.stream.read(size)
                    source_read_bytes[0] += len(value)
                    return value

            def track_source_open(path, mode="r", *args, **kwargs):
                stream = real_path_open(path, mode, *args, **kwargs)
                if Path(path).resolve() == source_path.resolve() and mode == "rb":
                    return CountingReader(stream)
                return stream

            output_mid_verification = Path(temporary, "cancel-during-verification.html")

            def cancel_during_source_check():
                return 128 * 1024 <= source_read_bytes[0] < len(source_bytes)

            with mock.patch.object(Path, "open", track_source_open):
                with self.assertRaises(public_export.PublicExportCancelled):
                    export_public_html(
                        large_run,
                        str(output_mid_verification),
                        cancel_check=cancel_during_source_check,
                    )
            self.assertGreaterEqual(source_read_bytes[0], 128 * 1024)
            self.assertLess(source_read_bytes[0], len(source_bytes))
            self.assertFalse(output_mid_verification.exists())
            self.assertFalse(Path(str(output_mid_verification) + ".redaction.json").exists())
            self.assertEqual(source_path.read_bytes(), source_bytes)

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

    def test_historical_export_is_artifact_only_and_preserves_verdicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary, "historical-run")
            run.mkdir()
            report = run / "report.html"
            original = _historical_sealed_html()
            report.write_bytes(original.encode("utf-8"))
            report_bytes = report.read_bytes()
            # This deliberately cannot be opened as a run database. Historical
            # export must rely only on the sealed report artifact.
            Path(run, "run.sqlite").write_bytes(b"not a sqlite database")
            output = Path(temporary, "public", "historical.html")
            output.parent.mkdir()

            with mock.patch.object(public_export.signing, "load_key", return_value=None):
                with self.assertRaises(sqlite3.DatabaseError):
                    export_public_html(str(run), str(output))
                self.assertEqual(
                    export_public_html(str(run), str(output), historical=True),
                    str(output),
                )

            public = output.read_bytes()
            public_html = public.decode("utf-8")
            manifest = json.loads(Path(str(output) + ".redaction.json").read_text(encoding="utf-8"))
            historical = manifest["historical_export"]
            self.assertEqual(historical["kind"], "historical_artifact_only")
            self.assertEqual(historical["source_html_seal"], {
                "algorithm": "sha256",
                "authentication": "checksum_only_unauthenticated",
            })
            self.assertFalse(historical["current_run_database_verified"])
            self.assertFalse(historical["completion_status_verified"])
            self.assertFalse(historical["verification_mode_verified"])
            self.assertFalse(historical["report_markdown_binding_verified"])
            self.assertEqual(manifest["public_html_sha256"], hashlib.sha256(public).hexdigest())
            self.assertIn("Historical artifact-only export.", public_html)
            self.assertIn("does not authenticate the report's author", public_html)
            self.assertIn("current run database, completion status, semantic Verify mode", public_html)
            self.assertNotIn("citation-verifier-human-report-seal", public_html)
            self.assertNotIn("CONFIDENTIAL_UNPUBLISHED_SOURCE_TEXT", public_html)
            self.assertEqual(report.read_bytes(), report_bytes)

            original_projection = _report_data(original)["projection"]
            public_projection = _report_data(public_html)["projection"]
            self.assertEqual(
                public_projection["pairs"][0]["decision"],
                original_projection["pairs"][0]["decision"],
            )
            self.assertEqual(
                public_projection["pairs"][0]["status"],
                original_projection["pairs"][0]["status"],
            )

            hmac_run = Path(temporary, "historical-hmac-run")
            hmac_run.mkdir()
            hmac_report = hmac_run / "report.html"
            hmac_report.write_bytes(
                _historical_sealed_html(key=_HISTORICAL_SIGNING_KEY).encode("utf-8")
            )
            Path(hmac_run, "run.sqlite").write_bytes(b"still not a sqlite database")
            hmac_output = Path(temporary, "public", "historical-hmac.html")
            with mock.patch.object(
                public_export.signing,
                "load_key",
                return_value=_HISTORICAL_SIGNING_KEY,
            ):
                export_public_html(
                    str(hmac_run), str(hmac_output), historical=True
                )
            hmac_manifest = json.loads(
                Path(str(hmac_output) + ".redaction.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                hmac_manifest["historical_export"]["source_html_seal"],
                {"algorithm": "hmac-sha256", "authentication": "hmac_authenticated"},
            )
            self.assertIn(
                "source HMAC was authenticated",
                hmac_output.read_text(encoding="utf-8"),
            )

    def test_historical_export_rejects_unsealed_forged_and_unsupported_artifacts(self):
        valid_sha = _historical_sealed_html()
        body_only = render_human_report(_historical_projection()).body
        tampered = valid_sha.replace("<!doctype html>", "<!DOCTYPE html>", 1)
        fake_signature = _historical_sealed_html(valid_signature=False)
        hmac_sealed = _historical_sealed_html(key=_HISTORICAL_SIGNING_KEY)
        unknown_format = _historical_sealed_html(metadata_changes={"format": "pdf"})
        with mock.patch.object(human_sealing, "RENDERER_VERSION", "future.renderer.v99"):
            unknown_renderer = _historical_sealed_html()
        unsupported_projection = _historical_sealed_html(
            projection=_historical_projection(schema_version=2)
        )
        preview = _historical_sealed_html(preview=True)
        debug = _historical_sealed_html(projection=_historical_projection(
            execution={"debug_mode": True, "debug_labels": ["fixture"]}
        ))

        cases = (
            ("missing-seal", body_only, None, "seal"),
            ("tampered-body", tampered, None, "body|deterministic renderer"),
            ("fake-signature", fake_signature, None, "checksum is invalid|signature is invalid"),
            ("missing-hmac-key", hmac_sealed, None, "HMAC cannot be authenticated"),
            ("unknown-format", unknown_format, None, "unsupported format or renderer"),
            ("unknown-renderer", unknown_renderer, None, "unsupported format or renderer"),
            ("unsupported-projection", unsupported_projection, None, "unsupported report projection schema"),
            ("preview", preview, None, "deterministic renderer"),
            ("debug", debug, None, "non-preview, non-debug"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for name, html, key, error in cases:
                with self.subTest(name=name):
                    run = Path(temporary, name)
                    run.mkdir()
                    report = run / "report.html"
                    report.write_bytes(html.encode("utf-8"))
                    original = report.read_bytes()
                    output = Path(temporary, f"{name}.public.html")
                    with mock.patch.object(
                        public_export.signing, "load_key", return_value=key
                    ):
                        with self.assertRaisesRegex(ValueError, error):
                            export_public_html(
                                str(run), str(output), historical=True
                            )
                    self.assertFalse(output.exists())
                    self.assertFalse(Path(str(output) + ".redaction.json").exists())
                    self.assertEqual(report.read_bytes(), original)

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
            conn = sqlite3.connect(Path(run, "run.sqlite"))
            try:
                conn.execute(
                    "UPDATE meta SET value='59' WHERE key='schema_version'"
                )
                conn.commit()
            finally:
                conn.close()

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
