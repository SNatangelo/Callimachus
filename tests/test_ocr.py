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
    build_source_inventory,
    pending_ocr_count,
    source_payload,
)

from core.resolve import sources

from core.infra.db import RunRepository

from core.infra.db import task_storage

from core.parse.extract import extract_text

_GOOD = ("A detailed study of residual learning for image recognition. "
         "This is a recovered full text with plenty of real words so it clears the "
         "quality gate comfortably. " * 8)

def _write_pdf(path):
    # Content is irrelevant: core.pdf.extract is monkeypatched to return junk for it.
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n% scanned, no text layer\n")

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
