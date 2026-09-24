#!/usr/bin/env python3
# tests/test_pipeline_end_to_end.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Phase 6 regression tests for the per-reference serial pipeline driver."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from core.infra.db import RunRepository
from ._resolve_fixtures import current_resolution


class PipelineEndToEndTests(unittest.TestCase):
    def _make_run(self, tmp: str) -> str:
        run = os.path.abspath(tmp)
        repo = RunRepository.create(
            run,
            run_id="run-pipeline-e2e",
            input_path="C:\\paper.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style="vancouver",
            model_id=None,
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-1",
        )
        repo.replace_parse_payload(
            claims=[
                {
                    "id": "c1",
                    "sentence": "Claim one [1].",
                    "context_window": "Claim one [1].",
                    "marker_raw": "[1]",
                },
                {
                    "id": "c2",
                    "sentence": "Claim two [2].",
                    "context_window": "Claim two [2].",
                    "marker_raw": "[2]",
                },
            ],
            references=[
                {
                    "id": "r1",
                    "ref_number": 1,
                    "raw_entry": "Smith. Example paper one. 2020.",
                    "title": "Example paper one",
                    "source_type": "article",
                    "source_kind": "article_like",
                },
                {
                    "id": "r2",
                    "ref_number": 2,
                    "raw_entry": "Jones. Example paper two. 2021.",
                    "title": "Example paper two",
                    "source_type": "article",
                    "source_kind": "article_like",
                },
            ],
            citations=[
                {"claim_id": "c1", "ref_id": "r1", "ref_number": 1},
                {"claim_id": "c2", "ref_id": "r2", "ref_number": 2},
            ],
        )
        repo.close()
        return run

    def test_phase_resolve_defers_verify_tasks_until_phase_verify(self):
        from core.app import run as driver
        from core.app.phases import verify as verify_phase

        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            original_resolve = driver._resolve.resolve
            try:
                def fake_resolve(ref):
                    return current_resolution(ref, {
                        "ref_id": ref["id"],
                        "status": "resolved",
                        "via": "crossref",
                        "matched_title": ref.get("title"),
                        "abstract": f"Abstract for {ref['id']} with enough distinctive tokens.",
                        "abstract_via": "openalex",
                        "retracted": False,
                        "fulltext_exists": False,
                        "oa_status": "paywalled",
                        "attempts": [{"via": "crossref", "status": "resolved"}],
                    })

                driver._resolve.resolve = fake_resolve
                nxt = driver.phase_resolve({"run_dir": run, "phase": "resolve", "accuracy": "standard"})
                self.assertEqual(nxt, "fetch")
            finally:
                driver._resolve.resolve = original_resolve

            self.assertEqual(driver._pending_tasks(run, "verify"), [])

            with mock.patch.dict(
                os.environ,
                {"CITATION_VERIFIER_VERIFY_BACKENDS": "codex_cli"},
            ), mock.patch.object(
                verify_phase, "_execute_claim_evidence_tasks", return_value=0,
            ):
                result = driver.phase_verify({
                    "run_dir": run,
                    "phase": "verify",
                    "accuracy": "standard",
                })
            self.assertEqual(result, "web_research")
            pending = driver._pending_tasks(run, "verify")
            self.assertEqual(
                {handle for handle, _task in pending},
                {"verify:c1:r1:abstract_only", "verify:c2:r2:abstract_only"},
            )
            self.assertTrue(all(
                task["kind"] == "claim_evidence"
                for _handle, task in pending
            ))

            before = {handle for handle, _task in pending}
            after = {handle for handle, _task in driver._pending_tasks(run, "verify")}
            self.assertEqual(after, before)

    def test_phase_verify_backfills_missing_tasks_even_after_verify_emitted(self):
        from core.app import run as driver
        from core.app.phases import verify as verify_phase

        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                repo.upsert_resolve_result(
                    "r1",
                    {
                        "ref_id": "r1",
                        "status": "resolved",
                        "via": "crossref",
                        "matched_title": "Example paper one",
                        "abstract": "Stored abstract text with enough distinctive tokens.",
                        "abstract_via": "openalex",
                        "retracted": False,
                        "fulltext_exists": False,
                        "oa_status": "paywalled",
                    },
                )
            finally:
                repo.close()

            driver._sources.store_text(
                run,
                {
                    "id": "r1",
                    "ref_number": 1,
                    "raw_entry": "Smith. Example paper one. 2020.",
                    "title": "Example paper one",
                    "source_type": "article",
                },
                "abstract",
                "openalex",
                "Stored abstract text with enough distinctive tokens.",
                source_ref="resolve:openalex",
                mapping="tokens",
                signal="tokens",
                score=0.9,
            )

            with mock.patch.dict(
                os.environ,
                {"CITATION_VERIFIER_VERIFY_BACKENDS": "codex_cli"},
            ), mock.patch.object(
                verify_phase, "_execute_claim_evidence_tasks", return_value=0,
            ):
                result = driver.phase_verify({
                    "run_dir": run,
                    "phase": "verify",
                    "accuracy": "standard",
                    "verify_emitted": True,
                })
            self.assertEqual(result, "web_research")

            pending = driver._pending_tasks(run, "verify")
            self.assertEqual([handle for handle, _task in pending], ["verify:c1:r1:abstract_only"])
            self.assertEqual(pending[0][1]["kind"], "claim_evidence")

    def test_phase_resolve_inline_repair_promotes_and_suppresses_seeded_abstract(self):
        from core.app import run as driver

        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            driver._sources.store_text(
                run,
                {
                    "id": "r1",
                    "ref_number": 1,
                    "raw_entry": "Smith. Example paper one. 2020.",
                    "title": "Example paper one",
                    "source_type": "article",
                },
                "abstract",
                "openalex",
                "Weak metadata abstract that should be suppressed after fulltext repair.",
                source_ref="resolve:openalex",
                mapping="tokens",
                signal="tokens",
                score=0.0,
            )

            original_resolve = driver._resolve.resolve
            original_fetch = driver._fetch.fetch_fulltext
            try:
                def fake_resolve(ref):
                    if ref["id"] == "r1":
                        return current_resolution(ref, {
                            "ref_id": "r1",
                            "status": "resolved",
                            "via": "crossref_metadata",
                            "matched_title": "Example paper one",
                            "abstract": "Weak metadata abstract that should be suppressed after fulltext repair.",
                            "abstract_via": "openalex",
                            "retracted": False,
                            "fulltext_exists": "unknown",
                            "oa_status": "unknown",
                            "reason": "metadata search match",
                            "reference_status_tag": "weak_metadata_match",
                            "fabrication_risk": "low",
                            "attempts": [{"via": "crossref_metadata", "status": "resolved"}],
                        })
                    return current_resolution(ref, {
                        "ref_id": "r2",
                        "status": "unverified",
                        "via": "none",
                        "matched_title": "Example paper two",
                        "retracted": False,
                        "fulltext_exists": "unknown",
                        "oa_status": "unknown",
                        "attempts": [],
                    })

                def fake_fetch(*_args, **_kwargs):
                    return {
                        "status": "stored",
                        "method": "acl",
                        "pdf_url": "https://aclanthology.org/J93-2004.pdf",
                        "corroborate_signal": "title",
                        "corroborate_score": 1.0,
                        "fetch_trace": {"direct_text": {"attempts": []}, "execution": {"attempts": []}},
                    }

                driver._resolve.resolve = fake_resolve
                driver._fetch.fetch_fulltext = fake_fetch
                nxt = driver.phase_resolve({"run_dir": run, "phase": "resolve", "accuracy": "standard"})
                self.assertEqual(nxt, "fetch")
            finally:
                driver._resolve.resolve = original_resolve
                driver._fetch.fetch_fulltext = original_fetch

            repo = RunRepository.open(run)
            try:
                result = repo.get_resolve_result("r1")
                self.assertIsNotNone(result)
                self.assertEqual(result.status, "resolved")
                self.assertEqual(result.via, "fetch_repair:acl")
                self.assertIsNone(result.abstract)
                self.assertIsNone(result.abstract_via)
                disposition = (result.evidence_profile or {}).get("fetch_repair", {}).get("abstract_disposition") or {}
                self.assertEqual(disposition.get("action"), "suppressed")
            finally:
                repo.close()

            manifest = driver._sources.load_manifest(run)
            self.assertFalse(any(entry["ref_id"] == "r1" and entry["tier"] == "abstract" for entry in manifest.get("entries", [])))

    def test_phase_resolve_abstract_and_no_fetch_skip_inline_fulltext(self):
        from core.app import run as driver

        for state in (
            {"accuracy": "abstract"},
            {"accuracy": "standard", "no_fetch": True},
        ):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                run = self._make_run(tmp)
                fetch_calls = []
                original_resolve = driver._resolve.resolve
                original_fetch = driver._fetch.fetch_fulltext
                try:
                    def fake_resolve(ref):
                        return current_resolution(ref, {
                            "ref_id": ref["id"],
                            "status": "resolved",
                            "via": "crossref",
                            "matched_title": ref.get("title"),
                            "abstract": f"Stored abstract for {ref['id']} with enough distinctive tokens.",
                            "abstract_via": "openalex",
                            "retracted": False,
                            "fulltext_exists": "unknown",
                            "oa_status": "open",
                            "attempts": [{"via": "crossref", "status": "resolved"}],
                        })

                    driver._resolve.resolve = fake_resolve
                    driver._fetch.fetch_fulltext = lambda *_args, **_kwargs: fetch_calls.append(True)
                    self.assertEqual(
                        driver.phase_resolve({"run_dir": run, "phase": "resolve", **state}),
                        "fetch",
                    )
                finally:
                    driver._resolve.resolve = original_resolve
                    driver._fetch.fetch_fulltext = original_fetch

                self.assertEqual(fetch_calls, [])
                repo = RunRepository.open(run)
                try:
                    self.assertTrue(all(repo.get_resolve_result(ref_id) is not None for ref_id in ("r1", "r2")))
                finally:
                    repo.close()
                manifest = driver._sources.load_manifest(run)
                self.assertEqual(
                    {entry["ref_id"] for entry in manifest.get("entries", []) if entry["tier"] == "abstract"},
                    {"r1", "r2"},
                )


class Gate1ResumeTests(unittest.TestCase):
    """Gate 1 resume behaviour per Finding C (commit 09cc7db).

    Gate 1 in _resolve_reference_pipeline opens the run DB, checks whether an
    existing resolution has fulltext stored, and either skips (early return) or
    falls through to re-resolve.  Four scenarios matter:

      (a) resume with fulltext stored  →  still skipped
      (b) resume with only abstract + fulltext_exists="unknown"  →  re-fetch attempted
      (c) fulltext_exists=False  →  only abstract tier cached
      (d) re-resolution does not duplicate verify tasks
    """

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _make_run(tmp: str) -> str:
        run = os.path.abspath(tmp)
        repo = RunRepository.create(
            run,
            run_id="run-gate1-resume",
            input_path="C:\\paper.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style="vancouver",
            model_id=None,
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-gate1",
        )
        repo.replace_parse_payload(
            claims=[{
                "id": "c1",
                "sentence": "Claim one [1].",
                "context_window": "Claim one [1].",
                "marker_raw": "[1]",
            }],
            references=[
                {
                    "id": "r1",
                    "ref_number": 1,
                    "raw_entry": "Smith. Example paper one. 2020.",
                    "title": "Example paper one",
                    "source_type": "article",
                }
            ],
            citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
        )
        repo.close()
        return run

    _REF = {
        "id": "r1",
        "ref_number": 1,
        "raw_entry": "Smith. Example paper one. 2020.",
        "title": "Example paper one",
        "source_type": "article",
    }

    @staticmethod
    def _store_resolve(run: str, fulltext_exists_val) -> None:
        repo = RunRepository.open(run)
        try:
            repo.upsert_resolve_result(
                "r1",
                {
                    "ref_id": "r1",
                    "status": "resolved",
                    "via": "crossref",
                    "matched_title": "Example paper one",
                    "abstract": "Distinctive abstract text repeated enough for token reuse.",
                    "abstract_via": "openalex",
                    "retracted": False,
                    "fulltext_exists": fulltext_exists_val,
                    "oa_status": "paywalled",
                },
            )
        finally:
            repo.close()

    @staticmethod
    def _store_source(run: str, tier: str, origin: str, text: str) -> None:
        from core.app import run as driver

        driver._sources.store_text(
            run,
            {
                "id": "r1",
                "ref_number": 1,
                "raw_entry": "Smith. Example paper one. 2020.",
                "title": "Example paper one",
                "source_type": "article",
            },
            tier,
            origin,
            text,
            source_ref=f"resolve:{origin}",
            mapping="tokens",
            signal="tokens",
            score=0.9,
        )

    # -- scenario (a) ----------------------------------------------------------

    def test_a_fulltext_resume_skipped(self):
        """Resume with fulltext stored → Gate 1 returns early without task emission."""
        from core.app import run as driver

        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            self._store_resolve(run, "unknown")
            self._store_source(
                run, "fulltext", "unpaywall",
                "Full text content with enough distinctive tokens for reuse.",
            )
            result = driver._resolve_reference_pipeline(
                {"run_dir": run}, run, self._REF, fetch_context={}
            )
            self.assertEqual(result, {})
            self.assertEqual(driver._pending_tasks(run, "verify"), [])

    # -- scenario (b) ----------------------------------------------------------

    def test_b_abstract_only_unknown_falls_through(self):
        """Abstract stored but fulltext_exists="unknown" → Gate 1 falls through.

        The function must NOT return the early-skip sentinel.  It must instead
        resolve, pass through process_source, and leave task emission to VERIFY.
        """
        from core.app import pipeline as pipeline_mod
        from core.app import run as driver

        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            self._store_resolve(run, "unknown")
            self._store_source(
                run, "abstract", "openalex",
                "Distinctive abstract text repeated enough for token reuse.",
            )
            orig_resolve = driver._resolve.resolve
            orig_process = driver._pipeline.process_source
            try:
                driver._resolve.resolve = lambda ref: current_resolution(ref, {
                    "ref_id": "r1",
                    "status": "resolved",
                    "via": "crossref",
                    "matched_title": "Example paper one",
                    "abstract": "Distinctive abstract text repeated enough for token reuse.",
                    "abstract_via": "openalex",
                    "retracted": False,
                    "fulltext_exists": False,
                    "oa_status": "paywalled",
                    "attempts": [{"via": "crossref", "status": "resolved"}],
                })
                driver._pipeline.process_source = (
                    lambda ref, run_dir, deps: pipeline_mod.ProcessSourceResult(
                        resolution=deps.resolve_ref(ref),
                        fetched=None,
                        cached_source=None,
                    )
                )
                result = driver._resolve_reference_pipeline(
                    {"run_dir": run}, run, self._REF, fetch_context={}
                )
                self.assertIsInstance(result, dict)
                self.assertEqual(result, {})
                self.assertEqual(driver._pending_tasks(run, "verify"), [])
            finally:
                driver._resolve.resolve = orig_resolve
                driver._pipeline.process_source = orig_process

    # -- scenario (c) ----------------------------------------------------------

    def test_c_fulltext_false_caches_abstract_only(self):
        """fulltext_exists=False → _cache_tiers_for_resolution returns ("abstract",).

        When fulltext is declared impossible, only the abstract tier is eligible
        for reuse — no wasted fulltext fetch is attempted.
        """
        from core.app import pipeline as pipeline_mod

        self.assertEqual(
            pipeline_mod._cache_tiers_for_resolution({"fulltext_exists": False}),
            ("abstract",),
        )
        self.assertEqual(
            pipeline_mod._cache_tiers_for_resolution({"fulltext_exists": "unknown"}),
            ("fulltext", "abstract"),
        )

    # -- scenario (d) ----------------------------------------------------------

    def test_d_reresolution_no_duplicate_verify_tasks(self):
        """Calling _emit_verify_tasks twice creates tasks only the first time."""
        from core.app import run as driver

        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            self._store_resolve(run, "unknown")
            self._store_source(
                run, "abstract", "openalex",
                "Distinctive abstract text repeated enough for token reuse.",
            )
            st = {"run_dir": run}
            info1 = driver._emit_verify_tasks(st, ref_id="r1")
            created1 = int(info1.get("created") or 0)
            self.assertGreater(
                created1, 0, "First emit_verify_tasks call should create tasks"
            )
            info2 = driver._emit_verify_tasks(st, ref_id="r1")
            created2 = int(info2.get("created") or 0)
            self.assertEqual(
                created2, 0,
                "Second emit_verify_tasks call must not duplicate tasks"
            )


if __name__ == "__main__":
    unittest.main()
