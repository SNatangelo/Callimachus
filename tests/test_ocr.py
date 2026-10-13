#!/usr/bin/env python3
# tests/test_ocr.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""OCR wiring tests for the three scenarios:

  1. the MANUSCRIPT under verification is a scanned PDF  -> auto-OCR (no opt-in);
  2. a USER-PROVIDED source PDF is a scan                -> parked, opt-in OCR via fetch;
  3. a FETCHED source PDF is a scan                      -> parked, opt-in OCR via fetch.

The OCR engine itself (core.fetch.extraction.ocr) is monkeypatched so the tests need no OCR backend.
"""
from tests._bootstrap import *  # noqa: F401,F403

import os
import tempfile
import unittest
import hashlib
import json
from pathlib import Path
import shutil
import urllib.parse
from types import SimpleNamespace
from unittest import mock

from core.parse import extract as extract_mod
from core.fetch.extraction import ocr as ocr_mod
from core.fetch.extraction import pdf as pdf_mod
from core.fetch.admission import provided_fulltext
from core.app import run as driver
from core.app.commands import tasks as task_app
from core.app.phases import fetch as fetch_phase
from core.app.guided_fetch import (
    GuidedFetchController,
    _load_guided_ocr_artifacts,
    _preview_path,
    build_source_inventory,
    pending_ocr_count,
    source_payload,
)
from core.gui.guided_fetch import _read_preview
from core.resolve import sources
from core.infra.db import RunRepository
from core.infra.db import task_storage
from core.parse.extract import extract_text
from core.shared.filesystem_paths import io_path

_GOOD = ("A detailed study of residual learning for image recognition. "
         "This is a recovered full text with plenty of real words so it clears the "
         "quality gate comfortably. " * 8)


def _write_pdf(path):
    # Content is irrelevant: core.pdf.extract is monkeypatched to return junk for it.
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n% scanned, no text layer\n")


class TestManuscriptAutoOcr(unittest.TestCase):
    """Scenario 1: the manuscript is a scan -> OCR'd automatically."""

    def setUp(self):
        self._extract_orig = pdf_mod.extract
        self._ocr_orig = ocr_mod.ocr_pdf
        pdf_mod.extract = lambda p: ("   \n  ", "pdfminer")  # junk: fails quality gate

    def tearDown(self):
        pdf_mod.extract = self._extract_orig
        ocr_mod.ocr_pdf = self._ocr_orig

    def test_scanned_manuscript_is_auto_ocred(self):
        ocr_mod.ocr_pdf = lambda path, lang="eng", dpi=300: (_GOOD, "fake")
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "ms.pdf")
            _write_pdf(pdf)
            text, fmt, meta = extract_text(pdf, manuscript_ocr=True, ocr_lang="eng+ita")
            self.assertEqual(fmt, "pdf")
            self.assertIn("recovered full text", text)
            self.assertTrue(meta.get("ocr"))
            self.assertEqual(meta.get("pdf_method"), "ocr:fake")
            self.assertEqual(meta.get("ocr_lang"), "eng+ita")

    def test_ocr_language_is_threaded(self):
        seen = {}

        def fake(path, lang="eng", dpi=300):
            seen["lang"] = lang
            return _GOOD, "fake"

        ocr_mod.ocr_pdf = fake
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "ms.pdf")
            _write_pdf(pdf)
            extract_text(pdf, manuscript_ocr=True, ocr_lang="deu")
        self.assertEqual(seen.get("lang"), "deu")

    def test_manuscript_ocr_quality_gate_still_fails_loudly(self):
        ocr_mod.ocr_pdf = lambda path, lang="eng", dpi=300: ("..", "fake")  # too short
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "ms.pdf")
            _write_pdf(pdf)
            with self.assertRaises(ValueError):
                extract_text(pdf, manuscript_ocr=True)


class TestSourceNotAutoOcred(unittest.TestCase):
    """Scenarios 2/3: a cited source scan must NOT be auto-OCR'd; it parks instead."""

    def setUp(self):
        self._extract_orig = pdf_mod.extract
        pdf_mod.extract = lambda p: ("   \n  ", "pdfminer")

    def tearDown(self):
        pdf_mod.extract = self._extract_orig

    def test_source_scan_raises_for_parking(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "src.pdf")
            _write_pdf(pdf)
            # Default (manuscript_ocr=False): provide.py catches this and parks the scan.
            with self.assertRaises(ValueError):
                extract_text(pdf)


class TestOcrWorkerConfiguration(unittest.TestCase):
    def test_default_is_serial_and_environment_override_is_retained(self):
        self.assertEqual(ocr_mod.ocr_worker_count({}), 1)
        self.assertEqual(ocr_mod.ocr_worker_count({"CITATION_VERIFIER_OCR_WORKERS": "3"}), 3)


class TestSourceOcrFetchFlow(unittest.TestCase):
    """Scenario 2/3 end-to-end at the driver level: a parked source scan is OCR'd on
    opt-in, its text is registered as full text, and the scan is retired from the queue."""

    def _make_run(self, tmp):
        repo = RunRepository.create(
            tmp, run_id="run-ocr-1", input_path="ms.pdf", input_sha256="x",
            accuracy="standard", style=None, model_id=None,
            http_profile="default", challenge_mode="off", fixture_fingerprint="fx",
        )
        repo.replace_parse_payload(
            claims=[{"id": "c1", "sentence": "A claim [1].", "context_window": "A claim [1]."}],
            references=[{"id": "r1", "ref_number": 1,
                         "raw_entry": "Doe J. A detailed study of residual learning for image recognition. Journal. 2020. doi:10.1234/abcd",
                         "doi": "10.1234/abcd", "title": "A detailed study of residual learning for image recognition", "source_type": "article"}],
            citations=[{"claim_id": "c1", "ref_id": "r1"}],
        )
        repo.close()

    def _make_pending_fetch_task(self, tmp):
        repo = RunRepository.open(tmp)
        try:
            repo.upsert_resolve_result("r1", {
                "status": "resolved", "fulltext_exists": "unknown",
                "reference_status_tag": "confirmed", "fabrication_risk": "low",
                "tag_reason": "fixture",
            })
            row = repo._conn.execute("SELECT * FROM reference_entries WHERE ref_id='r1'").fetchone()
            reference = {field: row[field] for field in (
                "ref_number", "raw_entry", "title", "doi", "pmid", "url", "isbn",
                "year", "source_type", "source_kind", "indexability", "source_type_confidence",
            )}
            repo.create_task(
                task_id="fetch:r1", slot="fetch", ref_id="r1", claim_id=None, scope=None,
                task_payload={"kind": "fetch", "ref_id": "r1", "answer": None,
                              "status": "pending", "ref_number": 1, "reference": reference,
                              "source_identity": {
                                  "reference_status_tag": "confirmed", "fabrication_risk": "low",
                                  "tag_reason": "fixture", "matched_title": None,
                                  "metadata_match": None,
                              }, "instructions": "Retrieve an auditable source."},
            )
            return repo.get_execution_assurance()
        finally:
            repo.close()

    def _guided_controller(self, tmp, admitted):
        return GuidedFetchController(
            tmp,
            repository_opener=RunRepository.open_readonly,
            admit=lambda _run, task_id, payload, **_kwargs: admitted.append(
                (task_id, payload)
            ) or {"answer_id": task_id},
        )

    def test_guided_ocr_runs_only_on_action_and_recovers_before_proceed(self):
        ocr_orig = ocr_mod.ocr_pdf
        probe_orig = provided_fulltext.parse_extract.probe_file
        ocr_calls = []
        ocr_mod.ocr_pdf = lambda path, lang="eng", **kwargs: (
            ocr_calls.append((path, lang)) or (_GOOD + " doi:10.1234/abcd", "fake")
        )
        provided_fulltext.parse_extract.probe_file = lambda _path: {"format": "pdf"}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = os.path.abspath(tmp)
                self._make_run(tmp)
                assurance = self._make_pending_fetch_task(tmp)
                scan = os.path.join(tmp, "staged.pdf")
                _write_pdf(scan)
                admitted = []
                controller = self._guided_controller(tmp, admitted)
                controller.stage_source("r1", source_payload(file_path=scan))
                self.assertEqual(ocr_calls, [], "staging must not start OCR")

                events = []
                with mock.patch.object(task_app, "_answer_gate", return_value=SimpleNamespace(
                    gate=None, assurance=assurance,
                )):
                    result = controller.run_ocr(
                        [{"ref_id": "r1", "scan_id": None}], events.append,
                    )
                self.assertEqual(result[0]["status"], "done")
                self.assertIsNone(result[0]["reason"])
                self.assertEqual(len(ocr_calls), 1)
                self.assertEqual(
                    [event["status"] for event in events],
                    ["queued", "running", "done"],
                )
                self.assertEqual(admitted, [], "OCR must not answer Fetch before Proceed")

                inventory = build_source_inventory(tmp)
                queue_item = inventory["ocr_queue"][0]
                self.assertEqual(queue_item["status"], "done")
                self.assertNotIn("path", queue_item)
                self.assertNotIn("source_ref", queue_item)

                reopened = self._guided_controller(tmp, admitted)
                reopened.proceed()
                payload = admitted[0][1]
                with open(payload["ocr_scan_file_path"], "rb") as handle:
                    scan_digest = hashlib.sha256(handle.read()).hexdigest()
                self.assertEqual(
                    payload["url"], provided_fulltext.ocr_scan_source_ref(scan_digest)
                )
                self.assertTrue(payload["file_path"].endswith(".txt"))
                self.assertTrue(os.path.isfile(payload["ocr_scan_file_path"]))
                self.assertEqual(len(ocr_calls), 1, "reopen must reuse completed OCR")
        finally:
            ocr_mod.ocr_pdf = ocr_orig
            provided_fulltext.parse_extract.probe_file = probe_orig

    def test_guided_ocr_round_trips_long_artifacts_through_preview_and_admission(self):
        ocr_orig = ocr_mod.ocr_pdf
        probe_orig = provided_fulltext.parse_extract.probe_file
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

        def read_pdf(path):
            with open(io_path(path), "rb") as handle:
                assert handle.read().startswith(b"%PDF")
            return {"format": "pdf"}

        def run_ocr(path, lang="eng", **_kwargs):
            with open(io_path(path), "rb") as handle:
                assert handle.read().startswith(b"%PDF")
            return _GOOD + " doi:10.1234/abcd", "fake"

        ocr_mod.ocr_pdf = run_ocr
        provided_fulltext.parse_extract.probe_file = read_pdf
        try:
            root = Path(temp.name) / "long-run"
            Path(io_path(root)).mkdir(parents=True, exist_ok=True)
            while len(str(root)) < 175:
                remaining = 175 - len(str(root)) - 1
                component_length = max(1, min(80, remaining))
                root = root / ("r" + "x" * (component_length - 1))
                Path(io_path(root)).mkdir(parents=True, exist_ok=True)
            run_dir = str(root)

            self._make_run(run_dir)
            assurance = self._make_pending_fetch_task(run_dir)
            scan = root / "staged.pdf"
            _write_pdf(str(scan))
            controller = GuidedFetchController(
                run_dir, repository_opener=RunRepository.open_readonly,
            )
            controller.stage_source("r1", source_payload(file_path=str(scan)))
            with mock.patch.object(
                task_app, "_answer_gate",
                return_value=SimpleNamespace(gate=None, assurance=assurance),
            ):
                result = controller.run_ocr([{"ref_id": "r1", "scan_id": None}])
            self.assertEqual(result[0]["status"], "done")

            artifact = next(iter(_load_guided_ocr_artifacts(run_dir).values()))
            scan_path = Path(artifact["scan_path"])
            text_path = Path(artifact["text_file_path"])
            metadata_path = root / "sources" / "guided_ocr" / f"{artifact['scan_id']}.json"
            self.assertGreater(len(str(scan_path)), 260)
            self.assertGreater(len(str(metadata_path)), 260)
            self.assertGreater(len(str(text_path)), 260)
            for value in (artifact["scan_path"], artifact["text_file_path"]):
                self.assertFalse(value.startswith("\\\\?\\"))

            metadata = json.loads(Path(io_path(metadata_path)).read_text(encoding="utf-8"))
            preview_path = _preview_path(run_dir, metadata["text_path"])
            self.assertEqual(preview_path, str(text_path))
            self.assertEqual(_read_preview(preview_path), _GOOD + " doi:10.1234/abcd")

            reopened = GuidedFetchController(
                run_dir, repository_opener=RunRepository.open_readonly,
            )
            with mock.patch.object(
                task_app, "_answer_gate",
                return_value=SimpleNamespace(gate=None, assurance=assurance),
            ):
                reopened.proceed()
            repo = RunRepository.open_readonly(run_dir)
            try:
                view = repo.task_view("fetch:r1")
                provenance = repo.get_task_answer_provenance(
                    repo.get_latest_task_answer("fetch:r1").answer_id
                )
            finally:
                repo.close()
            answer = view["answer"]
            admitted_text = answer["file_path"]
            self.assertFalse(admitted_text.startswith("\\\\?\\"))
            self.assertLessEqual(len(admitted_text), 260)
            self.assertTrue(Path(admitted_text).is_file())
            marker = "#callimachus-scan-attachment="
            source_ref, separator, encoded_scan = answer["url"].partition(marker)
            self.assertEqual(separator, marker)
            self.assertEqual(
                source_ref,
                provided_fulltext.ocr_scan_source_ref(artifact["scan_sha256"]),
            )
            admitted_scan = urllib.parse.unquote(encoded_scan)
            self.assertFalse(admitted_scan.startswith("\\\\?\\"))
            self.assertLessEqual(len(admitted_scan), 260)
            self.assertTrue(Path(admitted_scan).is_file())
            with open(io_path(admitted_scan), "rb") as handle:
                self.assertEqual(
                    hashlib.sha256(handle.read()).hexdigest(), artifact["scan_sha256"]
                )
            self.assertEqual(len(provenance["files"]), 2)
            self.assertEqual(
                {item["sha256"] for item in provenance["files"]},
                {
                    hashlib.sha256((_GOOD + " doi:10.1234/abcd").encode("utf-8")).hexdigest(),
                    artifact["scan_sha256"],
                },
            )
            provided_fulltext.parse_extract.probe_file = probe_orig
            fetch_phase._ingest_fetch_answers({
                "run_dir": run_dir, "ocr_lang": "eng", "accuracy": "standard",
            })
            entries = sources.load_manifest(run_dir)["entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["ref_id"], "r1")
            self.assertEqual(entries[0]["tier"], "fulltext")
        finally:
            ocr_mod.ocr_pdf = ocr_orig
            provided_fulltext.parse_extract.probe_file = probe_orig
            shutil.rmtree(io_path(temp.name))
            temp.cleanup()

    def test_failed_ocr_can_be_retried_after_reopening(self):
        original = ocr_mod.ocr_pdf
        attempts = []

        def run_ocr(path, lang="eng", **_kwargs):
            attempts.append(path)
            if len(attempts) == 1:
                raise RuntimeError("OCR backend temporarily unavailable")
            return _GOOD + " doi:10.1234/abcd", "fake"

        ocr_mod.ocr_pdf = run_ocr
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = os.path.abspath(tmp)
                self._make_run(tmp)
                assurance = self._make_pending_fetch_task(tmp)
                scan = os.path.join(tmp, "scan.pdf")
                _write_pdf(scan)
                controller = self._guided_controller(tmp, [])
                controller.stage_source("r1", source_payload(file_path=scan))
                with mock.patch.object(task_app, "_answer_gate", return_value=SimpleNamespace(
                    gate=None, assurance=assurance,
                )):
                    self.assertEqual(
                        controller.run_ocr([{"ref_id": "r1", "scan_id": None}])[0]["status"],
                        "failed",
                    )
                    queue = build_source_inventory(tmp)["ocr_queue"]
                    self.assertEqual(queue[0]["status"], "failed")
                    reopened = self._guided_controller(tmp, [])
                    self.assertEqual(
                        reopened.run_ocr([{
                            "ref_id": "r1", "scan_id": queue[0]["scan_id"],
                        }])[0]["status"],
                        "done",
                    )
                self.assertEqual(len(attempts), 2)
        finally:
            ocr_mod.ocr_pdf = original

    def test_integrity_rejection_finishes_ocr_job_with_visible_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.abspath(tmp)
            self._make_run(tmp)
            self._make_pending_fetch_task(tmp)
            scan = os.path.join(tmp, "scan.pdf")
            _write_pdf(scan)
            controller = self._guided_controller(tmp, [])
            controller.stage_source("r1", source_payload(file_path=scan))
            events = []
            with mock.patch.object(
                task_app, "_answer_gate", side_effect=SystemExit("integrity gate rejected")
            ):
                result = controller.run_ocr([{"ref_id": "r1", "scan_id": None}], events.append)
            self.assertEqual(result[0]["status"], "failed")
            self.assertEqual([item["status"] for item in events], ["queued", "running", "failed"])

    def test_discarded_completed_ocr_is_not_restaged_after_reopen(self):
        ocr_orig = ocr_mod.ocr_pdf
        probe_orig = provided_fulltext.parse_extract.probe_file
        ocr_mod.ocr_pdf = lambda path, lang="eng", **kwargs: (
            _GOOD + " doi:10.1234/abcd", "fake")
        provided_fulltext.parse_extract.probe_file = lambda _path: {"format": "pdf"}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = os.path.abspath(tmp)
                self._make_run(tmp)
                assurance = self._make_pending_fetch_task(tmp)
                scan = os.path.join(tmp, "staged.pdf")
                _write_pdf(scan)
                admitted = []
                controller = self._guided_controller(tmp, admitted)
                controller.stage_source("r1", source_payload(file_path=scan))
                with mock.patch.object(task_app, "_answer_gate", return_value=SimpleNamespace(
                    gate=None, assurance=assurance,
                )):
                    self.assertEqual(
                        controller.run_ocr([{"ref_id": "r1", "scan_id": None}])[0]["status"],
                        "done",
                    )
                    self.assertTrue(controller.discard_source("r1"))

                reopened = self._guided_controller(tmp, admitted)
                reopened.proceed()
                self.assertEqual(admitted[0][1], {
                    "found": False,
                    "disposition": "user_waived",
                    "guided_fetch": True,
                })
                artifacts = list(Path(tmp, "sources", "guided_ocr").glob("*.json"))
                self.assertEqual(len(artifacts), 1)
                self.assertTrue(json.loads(artifacts[0].read_text(encoding="utf-8"))["discarded"])
        finally:
            ocr_mod.ocr_pdf = ocr_orig
            provided_fulltext.parse_extract.probe_file = probe_orig

    def test_pending_ocr_inventory_is_safe_and_skip_count_is_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.abspath(tmp)
            self._make_run(tmp)
            self._make_pending_fetch_task(tmp)
            scan = os.path.join(tmp, "scan.pdf")
            _write_pdf(scan)
            sources.park_unreadable(
                tmp, {"id": "r1", "ref_number": 1}, scan,
                origin="webfetch", reason="contains private path " + scan, move=False,
            )
            inventory = build_source_inventory(tmp)
            self.assertEqual(len(inventory["ocr_queue"]), 1)
            item = inventory["ocr_queue"][0]
            self.assertEqual(set(item), {
                "scan_id", "ref_id", "ref_number", "display_name", "status", "reason",
            })
            self.assertNotIn(scan, repr(item))
            self.assertEqual(pending_ocr_count(tmp), 1)

    def test_ocr_only_pause_keeps_queue_visible_and_can_proceed_without_fetch_tasks(self):
        ocr_orig = ocr_mod.ocr_pdf
        ocr_calls = []
        ocr_mod.ocr_pdf = lambda path, lang="eng", **kwargs: (
            ocr_calls.append(path) or (_GOOD, "fake")
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = os.path.abspath(tmp)
                self._make_run(tmp)
                repo = RunRepository.open(tmp)
                try:
                    repo.upsert_resolve_result("r1", {
                        "status": "resolved", "fulltext_exists": "unknown",
                        "reference_status_tag": "confirmed", "fabrication_risk": "low",
                        "tag_reason": "fixture",
                    })
                    assurance = repo.get_execution_assurance()
                finally:
                    repo.close()
                scan = os.path.join(tmp, "scan.pdf")
                _write_pdf(scan)
                sources.park_unreadable(
                    tmp, {"id": "r1", "ref_number": 1}, scan,
                    origin="webfetch", reason="scan", move=False,
                )

                inventory = build_source_inventory(tmp)
                self.assertEqual(len(inventory["ocr_queue"]), 1)
                self.assertEqual(pending_ocr_count(tmp), 1)
                controller = GuidedFetchController(
                    tmp, repository_opener=RunRepository.open_readonly,
                    admit=task_app.admit_fetch_payload,
                )
                with mock.patch.object(task_app, "_answer_gate", return_value=SimpleNamespace(
                    gate=None, assurance=assurance,
                )):
                    result = controller.run_ocr([{
                        "ref_id": "r1",
                        "scan_id": inventory["ocr_queue"][0]["scan_id"],
                    }])
                    self.assertEqual(result[0]["status"], "done")
                    self.assertTrue(controller.proceed()["proceeded"])
                fetch_phase._ingest_fetch_answers({
                    "run_dir": tmp, "ocr_lang": "eng", "accuracy": "standard",
                })
                entries = sources.load_manifest(tmp)["entries"]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["ref_id"], "r1")
                self.assertEqual(entries[0]["tier"], "fulltext")
                self.assertEqual(len(ocr_calls), 1)
                repo = RunRepository.open_readonly(tmp)
                try:
                    task = repo.list_tasks(slot="fetch")[0]
                    answers = repo.list_task_answers(task.task_id)
                    provenance = repo.get_task_answer_provenance(answers[0].answer_id)
                finally:
                    repo.close()
                self.assertEqual(len(provenance["files"]), 2)
                self.assertTrue(any(file["stored_path"].endswith(".pdf") for file in provenance["files"]))
        finally:
            ocr_mod.ocr_pdf = ocr_orig

    def test_browser_challenge_accepts_unattested_queued_ocr_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.abspath(tmp)
            self._make_run(tmp)
            repo = RunRepository.open(tmp)
            try:
                repo.upsert_resolve_result("r1", {
                    "status": "resolved", "fulltext_exists": "unknown",
                    "reference_status_tag": "confirmed", "fabrication_risk": "low",
                    "tag_reason": "fixture",
                })
                row = repo._conn.execute(
                    "SELECT * FROM reference_entries WHERE ref_id='r1'"
                ).fetchone()
                repo.create_task(
                    task_id="browser:one", slot="fetch", ref_id=None,
                    claim_id=None, scope=None,
                    task_payload={
                        "kind": "browser_challenge", "status": "pending", "answer": None,
                        "domain": "example.test", "instructions": "Retrieve the source.",
                        "candidate_urls": [],
                        "references": [{
                            "ref_id": "r1", "ref_number": row["ref_number"],
                            "raw_entry": row["raw_entry"], "doi": row["doi"],
                            "url": row["url"], "candidate_urls": [],
                        }],
                    },
                )
                assurance = repo.get_execution_assurance()
            finally:
                repo.close()
            scan = os.path.join(tmp, "scan.pdf")
            _write_pdf(scan)
            sources.park_unreadable(
                tmp, {"id": "r1", "ref_number": 1}, scan,
                origin="webfetch", reason="scan", move=False,
            )
            inventory = build_source_inventory(tmp)
            controller = GuidedFetchController(
                tmp, repository_opener=RunRepository.open_readonly,
                admit=task_app.admit_fetch_payload,
            )
            with mock.patch.object(ocr_mod, "ocr_pdf", return_value=(_GOOD, "fake")), \
                    mock.patch.object(task_app, "_answer_gate", return_value=SimpleNamespace(
                        gate=None, assurance=assurance,
                    )):
                result = controller.run_ocr([{
                    "ref_id": "r1", "scan_id": inventory["ocr_queue"][0]["scan_id"],
                }])
                self.assertEqual(result[0]["status"], "done")
                self.assertTrue(controller.proceed()["proceeded"])
            task = fetch_phase._answered_tasks(tmp, "fetch")[0][1]
            self.assertFalse(task["answer"]["items"][0].get("identity_attested", False))
            self.assertTrue(fetch_phase._browser_challenge_answer_is_valid(task))
            fetch_phase._ingest_fetch_answers({
                "run_dir": tmp, "ocr_lang": "eng", "accuracy": "standard",
            })
            entries = sources.load_manifest(tmp)["entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["ref_id"], "r1")

    def test_ocr_only_skip_records_waiver_and_keeps_pdf_without_running_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.abspath(tmp)
            self._make_run(tmp)
            repo = RunRepository.open(tmp)
            try:
                repo.upsert_resolve_result("r1", {
                    "status": "resolved", "fulltext_exists": "unknown",
                    "reference_status_tag": "confirmed", "fabrication_risk": "low",
                    "tag_reason": "fixture",
                })
                assurance = repo.get_execution_assurance()
            finally:
                repo.close()
            scan = os.path.join(tmp, "scan.pdf")
            _write_pdf(scan)
            sources.park_unreadable(
                tmp, {"id": "r1", "ref_number": 1}, scan,
                origin="webfetch", reason="scan", move=False,
            )
            controller = GuidedFetchController(
                tmp, repository_opener=RunRepository.open_readonly,
                admit=task_app.admit_fetch_payload,
            )
            with mock.patch.object(task_app, "_answer_gate", return_value=SimpleNamespace(
                gate=None, assurance=assurance,
            )):
                summary = controller.proceed()
            self.assertEqual(summary["waived_ref_ids"], ["r1"])
            repo = RunRepository.open_readonly(tmp)
            try:
                task = repo.list_tasks(slot="fetch")[0]
                answer = repo.list_task_answers(task.task_id)[0]
                provenance = repo.get_task_answer_provenance(answer.answer_id)
            finally:
                repo.close()
            self.assertFalse(answer.raw_payload["found"])
            self.assertEqual(answer.raw_payload["disposition"], "user_waived")
            self.assertEqual(len(provenance["files"]), 1)
            self.assertTrue(provenance["files"][0]["stored_path"].endswith(".pdf"))
            self.assertEqual(sources.load_manifest(tmp)["entries"], [])

    def test_ocr_attachment_marker_is_removed_before_source_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.abspath(tmp)
            self._make_run(tmp)
            self._make_pending_fetch_task(tmp)
            scan = os.path.join(tmp, "captured.pdf")
            text = os.path.join(tmp, "ocr.txt")
            _write_pdf(scan)
            with open(text, "w", encoding="utf-8") as handle:
                handle.write(_GOOD + " doi:10.1234/abcd")
            with open(scan, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            canonical = provided_fulltext.ocr_scan_source_ref(digest)
            url = (
                canonical + "#callimachus-scan-attachment="
                + urllib.parse.quote(scan, safe="")
            )

            result = fetch_phase._admit_controlled_fetch_answer(
                tmp,
                ref_id="r1",
                answer={
                    "found": True,
                    "guided_fetch": True,
                    "identity_attested": True,
                    "source_tier": "fulltext",
                    "file_path": text,
                    "url": url,
                },
                origin="webfetch",
                supplied_by="user",
                supplied_via="controlled_task_answer:test",
            )

            self.assertEqual(result["status"], "stored")
            manifest_entry = next(
                entry for entry in sources.load_manifest(tmp)["entries"]
                if entry.get("ref_id") == "r1"
            )
            self.assertEqual(manifest_entry["source_ref"], canonical)
            self.assertNotIn("callimachus-scan-attachment", manifest_entry["source_ref"])

    def test_queued_ocr_does_not_attest_a_conflicting_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.abspath(tmp)
            self._make_run(tmp)
            self._make_pending_fetch_task(tmp)
            scan = os.path.join(tmp, "scan.pdf")
            text = os.path.join(tmp, "ocr.txt")
            _write_pdf(scan)
            with open(text, "w", encoding="utf-8") as handle:
                handle.write("doi:10.9999/wrong\n" + _GOOD)
            with open(scan, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            url = (
                provided_fulltext.ocr_scan_source_ref(digest)
                + "#callimachus-scan-attachment="
                + urllib.parse.quote(scan, safe="")
            )
            result = fetch_phase._admit_controlled_fetch_answer(
                tmp, ref_id="r1",
                answer={
                    "found": True, "guided_fetch": True,
                    "identity_attested": False, "source_tier": "fulltext",
                    "file_path": text, "url": url,
                },
                origin="webfetch", supplied_by="user",
                supplied_via="controlled_task_answer:test",
            )
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(sources.load_manifest(tmp)["entries"], [])

    def test_ocr_attachment_payload_keeps_waiver_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = os.path.join(tmp, "scan.pdf")
            _write_pdf(scan)
            with open(scan, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            normalized = task_storage._source_answer({
                "found": False,
                "disposition": "user_waived",
                "guided_fetch": True,
                "ocr_scan_file_path": scan,
            }, "Fetch answer", ref_id_required=False)

            self.assertFalse(normalized["found"])
            self.assertEqual(normalized["disposition"], "user_waived")
            self.assertTrue(normalized["url_present"])
            self.assertTrue(normalized["url"].startswith(
                f"urn:callimachus:guided-ocr-skip:sha256:{digest}"
                "#callimachus-scan-attachment="
            ))
            self.assertNotIn("ocr_scan_file_path", normalized)

    def test_skip_answer_does_not_trigger_automatic_ocr(self):
        skip_ref = (
            "urn:callimachus:guided-ocr-skip:sha256:" + "a" * 64
            + "#callimachus-scan-attachment=C%3A%2Frun%2Fscan.pdf"
        )
        task = {"kind": "fetch", "ref_id": "r1", "answer": {
            "found": False,
            "disposition": "user_waived",
            "guided_fetch": True,
            "url": skip_ref,
        }}
        with mock.patch.object(fetch_phase, "_answered_tasks", return_value=[("fetch:r1", task)]), mock.patch.object(
            fetch_phase, "_controlled_answer_source_provenance", return_value=("operator:test", "controlled")
        ), mock.patch.object(fetch_phase, "_update_task") as update, mock.patch.object(
            fetch_phase, "_checkpoint_applied_fetch_answer"
        ), mock.patch.object(fetch_phase, "_admit_controlled_fetch_answer") as admit, mock.patch.object(
            fetch_phase, "_auto_run_source_ocr"
        ) as auto:
            driver._ingest_fetch_answers({"run_dir": "unused"})

        update.assert_called_once()
        admit.assert_not_called()
        auto.assert_not_called()

    def test_authenticated_precomputed_ocr_answer_is_bound_to_queued_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.abspath(tmp)
            self._make_run(tmp)
            assurance = self._make_pending_fetch_task(tmp)
            scan = os.path.join(tmp, "scan.pdf")
            _write_pdf(scan)
            kept = sources.park_unreadable(tmp, {"id": "r1", "ref_number": 1}, scan,
                                              origin="webfetch", reason="scan", move=False)
            ocr_text = os.path.join(tmp, "answer.ocr.txt")
            with open(ocr_text, "w", encoding="utf-8") as handle:
                handle.write(_GOOD + " doi:10.1234/abcd")
            expected_source_ref = provided_fulltext.precomputed_ocr_source_ref(tmp, "r1")
            args = SimpleNamespace(
                run=tmp, task="fetch:r1", ocr_text_file=ocr_text, file_path=None,
                text_file=None, url=None, not_found=False, item_file=None,
                item_text_file=None, item_not_found=None, agent_identity=None,
                debug_override_artifact_integrity=False, debug_override_reason=None,
            )
            with mock.patch.object(task_app, "_answer_gate", return_value=SimpleNamespace(
                gate=None, assurance=assurance,
            )):
                self.assertEqual(task_app.cmd_answer_fetch(args), 0)
            driver._ingest_fetch_answers({"run_dir": tmp})

            repo = RunRepository.open_readonly(tmp)
            try:
                self.assertEqual(repo.get_task("fetch:r1").status, "applied")
                answer = repo.get_latest_task_answer("fetch:r1")
                provenance = repo.get_task_answer_provenance(answer.answer_id)
                entry = next(item for item in repo.source_manifest_payload()["entries"]
                             if item["ref_id"] == "r1" and item["tier"] == "fulltext")
            finally:
                repo.close()
            self.assertEqual(entry["origin"], "ocr")
            self.assertEqual(entry["mapping"], "controlled_task_answer")
            self.assertTrue(entry["supplied_via"].startswith("controlled_task_answer:"))
            # The source reference is an immutable URN over the exact parked scan bytes.
            self.assertEqual(entry["source_ref"], expected_source_ref)
            with open(ocr_text, "rb") as handle:
                self.assertEqual(provenance["files"][0]["sha256"], hashlib.sha256(handle.read()).hexdigest())
            queue_entry = sources.load_unreadable(tmp)["entries"][0]
            self.assertEqual(queue_entry["ocr_status"], "done")
            self.assertEqual(queue_entry["ocr_method"], "ocr:precomputed")
            self.assertFalse(os.path.exists(os.path.join(tmp, kept)))

    def test_precomputed_ocr_hash_mismatch_reopens_task_and_leaves_scan_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.abspath(tmp)
            self._make_run(tmp)
            assurance = self._make_pending_fetch_task(tmp)
            scan = os.path.join(tmp, "scan.pdf")
            _write_pdf(scan)
            kept = sources.park_unreadable(tmp, {"id": "r1", "ref_number": 1}, scan,
                                              origin="webfetch", reason="scan", move=False)
            text = os.path.join(tmp, "answer.ocr.txt")
            with open(text, "w", encoding="utf-8") as handle:
                handle.write(_GOOD + " doi:10.1234/abcd")
            with open(text, "rb") as handle:
                text_sha256 = hashlib.sha256(handle.read()).hexdigest()
            repo = RunRepository.open(tmp)
            try:
                repo.submit_task_answer_with_provenance(
                    task_id="fetch:r1", actor_type="user",
                    raw_payload={"found": True, "file_path": text,
                                 "url": "urn:callimachus:ocr-scan:sha256:" + "0" * 64},
                    answer_id="answer-bad-ocr",
                    provenance={"answer_id": "answer-bad-ocr", "producer_class": "operator",
                                "producer_identity": "test", "authenticated_uid": 1,
                                "ingress_kind": "controlled_file", "admitted_at": "2026-09-05T00:00:00+00:00",
                                "file_count": 1, "authority_id": "test"},
                    files=[{"original_name": "answer.ocr.txt", "stored_path": "answer.ocr.txt",
                            "sha256": text_sha256,
                            "byte_count": os.path.getsize(text)}], expected_assurance=assurance,
                )
            finally:
                repo.close()
            with self.assertRaises(ValueError, msg="hash mismatch must fail closed"):
                driver._ingest_fetch_answers({"run_dir": tmp})
            repo = RunRepository.open_readonly(tmp)
            try:
                self.assertEqual(repo.get_task("fetch:r1").status, "pending")
            finally:
                repo.close()
            self.assertEqual(sources.load_unreadable(tmp)["entries"][0]["ocr_status"], "pending")
            self.assertTrue(os.path.exists(os.path.join(tmp, kept)))

    def test_opt_in_ocr_registers_text_and_retires_scan(self):
        self._ocr_orig = ocr_mod.ocr_pdf
        # OCR text carries the DOI so provide.map corroborates deterministically.
        ocr_mod.ocr_pdf = lambda path, lang="eng", dpi=300, **kwargs: (
            _GOOD + " doi:10.1234/abcd", "fake")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = os.path.abspath(tmp)
                self._make_run(tmp)
                ref = {"id": "r1", "ref_number": 1,
                       "raw_entry": "Doe J. A study. Journal. 2020. doi:10.1234/abcd",
                       "doi": "10.1234/abcd"}
                scan = os.path.join(tmp, "scan.pdf")
                _write_pdf(scan)
                kept = sources.park_unreadable(
                    tmp, ref, scan, origin="user", reason="unreadable", move=False)
                self.assertTrue(kept)
                self.assertTrue(os.path.exists(os.path.join(tmp, kept)))

                st = {"run_dir": tmp, "ocr_lang": "eng"}
                task = {"kind": "ocr", "ref_id": "r1", "ref_number": 1, "kept_as": kept}

                # Skip path leaves the scan parked.
                driver._run_source_ocr(st, task, {"proceed": False})
                man = sources.load_manifest(tmp)
                self.assertEqual(man.get("entries"), [])
                self.assertTrue(os.path.exists(os.path.join(tmp, kept)))

                # Opt-in path: OCR, register full text, retire the scan.
                driver._run_source_ocr(st, task, {"proceed": True})
                man = sources.load_manifest(tmp)
                ft = [e for e in man.get("entries", [])
                      if e.get("ref_id") == "r1" and e.get("tier") == "fulltext"]
                self.assertEqual(len(ft), 1, man)
                self.assertEqual(ft[0].get("origin"), "ocr")
                # The parked scan is retired (file deleted, queue entry done).
                self.assertFalse(os.path.exists(os.path.join(tmp, kept)))
                pending = [e for e in sources.load_unreadable(tmp).get("entries", [])
                           if e.get("ocr_status") != "done"]
                self.assertEqual(pending, [])
        finally:
            ocr_mod.ocr_pdf = self._ocr_orig

    def test_controlled_scan_parks_then_auto_ocr_uses_fulltext_gate(self):
        extract_orig = pdf_mod.extract
        ocr_orig = ocr_mod.ocr_pdf
        pdf_mod.extract = lambda path: ("   \n", "pdfminer")
        ocr_mod.ocr_pdf = lambda path, lang="eng", dpi=300, **kwargs: (
            _GOOD + " doi:10.1234/abcd", "fake")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = os.path.abspath(tmp)
                self._make_run(tmp)
                scan = os.path.join(tmp, ".integrity-inbox", "answer.pdf")
                os.makedirs(os.path.dirname(scan), exist_ok=True)
                _write_pdf(scan)
                ref = {
                    "id": "r1", "ref_number": 1,
                    "raw_entry": "Doe J. A detailed study of residual learning for image recognition. Journal. 2020. doi:10.1234/abcd",
                    "doi": "10.1234/abcd", "title": "A detailed study of residual learning for image recognition", "source_type": "article",
                }

                admitted = provided_fulltext.ingest_task_answer(
                    tmp, ref, {}, file_path=scan, origin="webfetch",
                    supplied_by="agent:test", supplied_via="controlled_task_answer:test",
                )
                self.assertEqual(admitted["status"], "ocr_pending")
                self.assertTrue(os.path.exists(scan), "controlled inbox original must remain")
                self.assertEqual(sources.load_manifest(tmp).get("entries"), [])
                pending = sources.load_unreadable(tmp)["entries"]
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["ocr_status"], "pending")
                self.assertTrue(os.path.exists(os.path.join(tmp, admitted["kept_as"])))

                result = driver._auto_run_source_ocr(
                    {"run_dir": tmp, "ocr_lang": "eng"}, {"r1": ref}
                )
                self.assertEqual(result["stored"], 1, result)
                entry = next(e for e in sources.load_manifest(tmp)["entries"] if e["ref_id"] == "r1")
                self.assertEqual(entry["origin"], "ocr")
                self.assertEqual(entry["mapping"], "auto")
                self.assertEqual(entry["supplied_by"], "ocr")
                self.assertEqual(entry["supplied_via"], f"ocr_queue:{admitted['kept_as']}")
                self.assertEqual(entry["extraction_method"], "ocr")
                self.assertEqual(sources.load_unreadable(tmp)["entries"][0]["ocr_status"], "done")
        finally:
            pdf_mod.extract = extract_orig
            ocr_mod.ocr_pdf = ocr_orig

    def test_controlled_answer_reuses_same_ref_pending_scan_without_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.abspath(tmp)
            self._make_run(tmp)
            ref = {"id": "r1", "ref_number": 1, "raw_entry": "Doe J. A study. 2020."}
            queued = os.path.join(tmp, "queued.pdf")
            answer = os.path.join(tmp, "answer.pdf")
            _write_pdf(queued)
            _write_pdf(answer)
            kept = sources.park_unreadable(
                tmp, ref, queued, origin="webfetch", reason="scan", move=False
            )

            with mock.patch.object(provided_fulltext.parse_extract, "probe_file") as probe, mock.patch.object(
                provided_fulltext.parse_extract, "extract_text"
            ) as extract:
                result = provided_fulltext.ingest_task_answer(
                    tmp, ref, {}, file_path=answer, origin="webfetch",
                    supplied_by="agent:test", supplied_via="controlled_task_answer:test",
                )

            self.assertEqual(result["status"], "ocr_pending")
            self.assertEqual(result["kept_as"], kept)
            probe.assert_not_called()
            extract.assert_not_called()
            self.assertEqual(len(sources.load_unreadable(tmp)["entries"]), 1)

    def test_controlled_answer_does_not_reuse_pending_scan_for_another_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.abspath(tmp)
            self._make_run(tmp)
            queued = os.path.join(tmp, "queued.pdf")
            answer = os.path.join(tmp, "answer.pdf")
            _write_pdf(queued)
            _write_pdf(answer)
            sources.park_unreadable(
                tmp, {"id": "r1", "ref_number": 1}, queued,
                origin="webfetch", reason="scan", move=False,
            )
            other_ref = {"id": "r2", "ref_number": 2, "raw_entry": "Other work. 2020."}

            with mock.patch.object(
                provided_fulltext.parse_extract, "probe_file", side_effect=ValueError("unreadable")
            ) as probe:
                result = provided_fulltext.ingest_task_answer(
                    tmp, other_ref, {}, file_path=answer, origin="webfetch",
                    supplied_by="agent:test", supplied_via="controlled_task_answer:test",
                )

            self.assertEqual(result["status"], "ocr_pending")
            probe.assert_called_once_with(str(answer))
            self.assertNotEqual(result["kept_as"], "sources/ocr_queue/1_fulltext_webfetch.unreadable.pdf")

    def test_fetch_answers_defer_automatic_ocr_until_all_pending_scans_are_applied(self):
        tasks = [
            ("fetch:r1", {"kind": "fetch", "ref_id": "r1", "answer": {"file_path": "r1.pdf"}}),
            ("fetch:r2", {"kind": "fetch", "ref_id": "r2", "answer": {"file_path": "r2.pdf"}}),
        ]
        events = []

        def admit(_run, *, ref_id, **_kwargs):
            events.append(f"admit:{ref_id}")
            return {"status": "ocr_pending"}

        def update(_run, _slot, handle, _task, *, status):
            events.append(f"update:{handle}:{status}")

        with mock.patch.object(fetch_phase, "_answered_tasks", return_value=tasks), mock.patch.object(
            fetch_phase, "_controlled_answer_source_provenance", return_value=("agent:test", "test")
        ), mock.patch.object(fetch_phase, "_admit_controlled_fetch_answer", side_effect=admit), mock.patch.object(
            fetch_phase, "_update_task", side_effect=update
        ), mock.patch.object(fetch_phase, "_checkpoint_applied_fetch_answer"), mock.patch.object(
            fetch_phase, "_load_parse_payload", return_value={"references": []}
        ), mock.patch.object(fetch_phase, "_auto_run_source_ocr", side_effect=lambda *_args, **_kwargs: events.append("ocr")) as auto:
            driver._ingest_fetch_answers({"run_dir": "test-run"})

        self.assertEqual(events, [
            "admit:r1", "update:fetch:r1:applied",
            "admit:r2", "update:fetch:r2:applied", "ocr",
        ])
        auto.assert_called_once()

    def test_controlled_sparse_pdf_is_parked_before_identity_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.abspath(tmp)
            self._make_run(tmp)
            scan = os.path.join(tmp, ".integrity-inbox", "sparse.pdf")
            os.makedirs(os.path.dirname(scan), exist_ok=True)
            _write_pdf(scan)
            sparse_text = "Sparse but superficially readable extracted words. " * 11
            ref = {
                "id": "r1", "ref_number": 1,
                "raw_entry": "Doe J. A detailed study of residual learning for image recognition. Journal. 2020. doi:10.1234/abcd",
                "doi": "10.1234/abcd",
                "title": "A detailed study of residual learning for image recognition",
                "source_type": "article",
            }

            with mock.patch.object(
                provided_fulltext.parse_extract, "extract_text",
                return_value=(sparse_text, "pdf", {}),
            ), mock.patch.object(
                provided_fulltext.fetch_ocr, "pdf_page_count", return_value=4,
            ):
                admitted = provided_fulltext.ingest_task_answer(
                    tmp, ref, {}, file_path=scan, origin="webfetch",
                    supplied_by="agent:test",
                    supplied_via="controlled_task_answer:test-sparse",
                )

            self.assertEqual(admitted["status"], "ocr_pending")
            self.assertEqual(sources.load_manifest(tmp).get("entries"), [])
            pending = sources.load_unreadable(tmp)["entries"]
            self.assertEqual(pending[0]["ocr_status"], "pending")
            self.assertIn("too sparse", pending[0]["reason"])

    def test_ocr_identity_rejection_leaves_scan_pending_without_source(self):
        ocr_orig = ocr_mod.ocr_pdf
        ocr_mod.ocr_pdf = lambda path, lang="eng", dpi=300, **kwargs: (
            "An unrelated marine biology report with extensive methods and results. " * 20,
            "fake",
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = os.path.abspath(tmp)
                self._make_run(tmp)
                scan = os.path.join(tmp, "scan.pdf")
                _write_pdf(scan)
                ref = {
                    "id": "r1", "ref_number": 1,
                    "raw_entry": "Doe J. A detailed study of residual learning for image recognition. Journal. 2020. doi:10.1234/abcd",
                    "doi": "10.1234/abcd",
                }
                kept = sources.park_unreadable(
                    tmp, ref, scan, origin="webfetch", reason="low text", move=False
                )
                driver._run_source_ocr(
                    {"run_dir": tmp, "ocr_lang": "eng"},
                    {"kind": "ocr", "ref_id": "r1", "ref_number": 1, "kept_as": kept},
                    {"proceed": True},
                )
                self.assertEqual(sources.load_manifest(tmp).get("entries"), [])
                pending = sources.load_unreadable(tmp)["entries"]
                self.assertEqual(pending[0]["ocr_status"], "pending")
                self.assertTrue(os.path.exists(os.path.join(tmp, kept)))
        finally:
            ocr_mod.ocr_pdf = ocr_orig

    def test_controlled_fetch_task_stays_applied_when_automatic_ocr_is_rejected(self):
        extract_orig = pdf_mod.extract
        ocr_orig = ocr_mod.ocr_pdf
        pdf_mod.extract = lambda path: ("   \n", "pdfminer")
        ocr_mod.ocr_pdf = lambda path, lang="eng", dpi=300, **kwargs: (
            "An unrelated marine biology report with extensive methods and results. " * 20,
            "fake",
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = os.path.abspath(tmp)
                self._make_run(tmp)
                scan = os.path.join(tmp, ".integrity-inbox", "answer.pdf")
                os.makedirs(os.path.dirname(scan), exist_ok=True)
                _write_pdf(scan)
                repo = RunRepository.open(tmp)
                try:
                    repo.upsert_resolve_result("r1", {
                        "status": "resolved", "fulltext_exists": "unknown",
                        "reference_status_tag": "confirmed", "fabrication_risk": "low",
                        "tag_reason": "fixture",
                    })
                    row = repo._conn.execute(
                        "SELECT * FROM reference_entries WHERE ref_id='r1'"
                    ).fetchone()
                    reference = {field: row[field] for field in (
                        "ref_number", "raw_entry", "title", "doi", "pmid", "url", "isbn",
                        "year", "source_type", "source_kind", "indexability",
                        "source_type_confidence",
                    )}
                    repo.create_task(
                        task_id="fetch:r1", slot="fetch", ref_id="r1", claim_id=None,
                        scope=None,
                        task_payload={
                            "kind": "fetch", "ref_id": "r1", "answer": None,
                            "status": "pending", "ref_number": 1, "reference": reference,
                            "source_identity": {
                                "reference_status_tag": "confirmed", "fabrication_risk": "low",
                                "tag_reason": "fixture", "matched_title": None,
                                "metadata_match": None,
                            },
                            "instructions": "Retrieve an auditable source.",
                        },
                    )
                    answer_id = repo.submit_task_answer(
                        task_id="fetch:r1", actor_type="agent",
                        raw_payload={"file_path": scan, "found": True},
                    )
                    with open(scan, "rb") as handle:
                        raw = handle.read()
                    repo.append_task_answer_provenance(
                        {
                            "answer_id": answer_id, "producer_class": "agent",
                            "producer_identity": "test", "authenticated_uid": 1,
                            "ingress_kind": "controlled_file",
                            "admitted_at": "2026-08-30T00:00:00+00:00", "file_count": 1,
                            "authority_id": "test-authority",
                        }, [{
                            "original_name": "answer.pdf",
                            "stored_path": ".integrity-inbox/answer.pdf",
                            "sha256": hashlib.sha256(raw).hexdigest(), "byte_count": len(raw),
                        }],
                    )
                finally:
                    repo.close()

                driver._ingest_fetch_answers({"run_dir": tmp, "ocr_lang": "eng"})

                repo = RunRepository.open(tmp)
                try:
                    self.assertEqual(repo.get_task("fetch:r1").status, "applied")
                    provenance = repo.get_task_answer_provenance(answer_id)
                finally:
                    repo.close()
                self.assertEqual(provenance["files"][0]["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertTrue(os.path.exists(scan))
                self.assertEqual(sources.load_manifest(tmp).get("entries"), [])
                pending = sources.load_unreadable(tmp)["entries"]
                self.assertEqual(pending[0]["ocr_status"], "pending")
                self.assertTrue(os.path.exists(os.path.join(tmp, pending[0]["kept_as"])))
        finally:
            pdf_mod.extract = extract_orig
            ocr_mod.ocr_pdf = ocr_orig


if __name__ == "__main__":
    unittest.main()
