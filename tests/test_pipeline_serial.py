#!/usr/bin/env python3
# tests/test_pipeline_serial.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Serial per-source pipeline tests for Phase 4."""

from __future__ import annotations

import os
import tempfile
import unittest
import pytest

from tests._bootstrap import *  # noqa: F401,F403
from core.fetch.storage import content_store
from core.app import pipeline
from core.resolve import sources
from core.infra.db import RunRepository


class TestPipelineSerial(unittest.TestCase):
    REF = {
        "id": "r1",
        "ref_number": 1,
        "title": "Attention is all you need",
        "raw_entry": "Vaswani A. Attention is all you need. NeurIPS. 2017.",
        "source_type": "article",
        "source_kind": "article_like",
        "doi": "10.1000/example",
        "url": None,
        "year": 2017,
    }

    def _make_run(self) -> tuple[tempfile.TemporaryDirectory, str]:
        tmp = tempfile.TemporaryDirectory()
        run = os.path.abspath(tmp.name)
        repo = RunRepository.create(
            run,
            run_id="run-pipeline-serial",
            input_path="C:\\paper.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style="vancouver",
            model_id=None,
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-1",
        )
        repo.replace_parse_payload(claims=[], references=[dict(self.REF)], citations=[])
        repo.close()
        return tmp, run

    def _with_state_dir(self, path: str):
        class _StateDir:
            def __enter__(self_nonlocal):
                self_nonlocal.saved = os.environ.get("CITATION_VERIFIER_STATE_DIR")
                os.environ["CITATION_VERIFIER_STATE_DIR"] = path
                return self_nonlocal

            def __exit__(self_nonlocal, exc_type, exc, tb):
                if self_nonlocal.saved is None:
                    os.environ.pop("CITATION_VERIFIER_STATE_DIR", None)
                else:
                    os.environ["CITATION_VERIFIER_STATE_DIR"] = self_nonlocal.saved
                return False

        return _StateDir()

    def test_fabricated_reference_never_calls_fetch(self):
        calls = []
        deps = pipeline.ProcessSourceDeps(
            resolve_ref=lambda _ref: {
                "ref_id": "r1",
                "status": "not_found",
                "identity": {"scheme": "doi", "value": "10.1000/example", "is_strong": True, "class_": "global"},
                "identity_state": "fabricated",
                "evidence_payload": {},
            },
            fetch_fulltext=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
        with tempfile.TemporaryDirectory() as run:
            with self._with_state_dir(run):
                result = pipeline.process_source(dict(self.REF), run, deps=deps)
        self.assertEqual(calls, [])
        self.assertIsNone(result.fetched)
        self.assertIsNone(result.cached_source)

    def test_cache_gate_reuses_seeded_parsed_text_and_skips_fetch(self):
        calls = []
        from core.parse.source_text import PREPARATION_VERSION

        with tempfile.TemporaryDirectory() as root:
            seed_run = os.path.join(root, "seed-run")
            work_run = os.path.join(root, "work-run")
            for run in (seed_run, work_run):
                repo = RunRepository.create(
                    run,
                    run_id=f"run-{os.path.basename(run)}",
                    input_path="C:\\paper.pdf",
                    input_sha256="abc123",
                    accuracy="standard",
                    style="vancouver",
                    model_id=None,
                    http_profile="default",
                    challenge_mode="off",
                    fixture_fingerprint="fixture-1",
                )
                repo.replace_parse_payload(claims=[], references=[dict(self.REF)], citations=[])
                repo.close()

            with self._with_state_dir(root):
                seeded = sources.store_text(
                    seed_run,
                    dict(self.REF),
                    "fulltext",
                    "webfetch",
                    "Reusable full text with enough distinctive tokens." * 20,
                    source_ref="https://example.org/paper.pdf",
                    mapping="tokens",
                    signal="doi",
                    score=1.0,
                )
                saved_lookup = content_store.find_reusable_parsed_text
                deps = pipeline.ProcessSourceDeps(
                    resolve_ref=lambda _ref: {
                        "ref_id": "r1",
                        "status": "resolved",
                        "identity": {"scheme": "doi", "value": "10.1000/example", "is_strong": True, "class_": "global"},
                        "identity_state": "resolved_strong_declared",
                        "fulltext_exists": True,
                        "evidence_payload": {
                            "fulltext_links": [{"url": "https://doi.org/10.1000/example", "content_type": "doi"}],
                            "auxiliary_fulltext_links": [],
                            "oa_status": "open",
                            "fulltext_exists": True,
                            "abstract": None,
                            "matched_title": self.REF["title"],
                            "work_type": "journal article",
                            "via": "crossref",
                        },
                    },
                    fetch_fulltext=lambda *args, **kwargs: calls.append((args, kwargs)),
                )
                try:
                    content_store.find_reusable_parsed_text = lambda *_args, **_kwargs: {
                        "tier": "fulltext",
                        "origin": "webfetch",
                        "source_ref": "https://example.org/paper.pdf",
                        "stored_path": os.path.join(seed_run, "sources", seeded["stored_as"]),
                        "stored_relpath": seeded["stored_as"],
                        "parsed_text_id": "pt-1",
                        "mapping": "tokens",
                        "match_signal": "doi",
                        "match_score": 1.0,
                        "identity_status": None,
                        "identity_note": None,
                        "content_version": None,
                        "supplied_by": None,
                        "supplied_via": None,
                        "file_format": None,
                        "extraction_method": "pymupdf",
                        "extraction_flags": ["repeated_line"],
                        "preparation_version": PREPARATION_VERSION,
                        "preparation_flags": ["bibliography_removal_reverted"],
                        "preparation_before_chars": 21802,
                        "preparation_after_chars": 21802,
                        "document_relation": {
                            "decision": "inconclusive",
                            "reason_code": "document_relation_inconclusive",
                            "reason": "fixture relation",
                        },
                    }
                    result = pipeline.process_source(dict(self.REF), work_run, deps=deps)
                    manifest = sources.load_manifest(work_run)
                finally:
                    content_store.find_reusable_parsed_text = saved_lookup

        self.assertEqual(calls, [])
        self.assertIsNone(result.fetched)
        self.assertIsNotNone(result.cached_source)
        self.assertEqual(result.cached_source["tier"], "fulltext")
        self.assertEqual(result.cached_source["extraction_method"], "pymupdf")
        self.assertEqual(result.cached_source["extraction_flags"], ["repeated_line"])
        self.assertEqual(result.cached_source["preparation_version"], PREPARATION_VERSION)
        self.assertEqual(result.cached_source["preparation_before_chars"], 21802)
        self.assertEqual(
            result.cached_source["document_relation"]["decision"], "inconclusive"
        )
        self.assertTrue(any(e["ref_id"] == "r1" and e["tier"] == "fulltext" for e in manifest["entries"]))

    def test_fetch_receives_resolution_evidence_payload_directly(self):
        captured = {}
        deps = pipeline.ProcessSourceDeps(
            resolve_ref=lambda _ref: {
                "ref_id": "r1",
                "status": "resolved",
                "identity": {"scheme": "doi", "value": "10.1000/example", "is_strong": True, "class_": "global"},
                "identity_state": "resolved_strong_declared",
                "fulltext_exists": "unknown",
                "fulltext_links": [{"url": "https://wrong.example.org", "content_type": "html"}],
                "evidence_payload": {
                    "fulltext_links": [{"url": "https://payload.example.org/paper.pdf", "content_type": "pdf"}],
                    "auxiliary_fulltext_links": [],
                    "oa_status": "open",
                    "fulltext_exists": "unknown",
                    "abstract": "payload abstract",
                    "matched_title": "Payload title",
                    "work_type": "journal article",
                    "via": "payload_via",
                },
            },
            fetch_fulltext=lambda ref, run_dir, resolve_result, **kwargs: captured.update(
                {
                    "ref": ref,
                    "run_dir": run_dir,
                    "resolve_result": resolve_result,
                    "evidence_payload": kwargs.get("evidence_payload"),
                }
            ) or {"status": "skipped", "method": None, "reason": "test"},
        )
        with tempfile.TemporaryDirectory() as run:
            with self._with_state_dir(run):
                result = pipeline.process_source(dict(self.REF), run, deps=deps)
        self.assertIsNotNone(result.fetched)
        self.assertEqual(captured["evidence_payload"]["matched_title"], "Payload title")
        self.assertEqual(
            captured["evidence_payload"]["fulltext_links"][0]["url"],
            "https://payload.example.org/paper.pdf",
        )

    def test_inline_fulltext_policy_defaults_to_fetch_and_can_disable_it(self):
        calls = []

        def resolution(_ref):
            return {
                "ref_id": "r1",
                "status": "resolved",
                "identity": {"scheme": "doi", "value": "10.1000/example", "is_strong": True, "class_": "global"},
                "identity_state": "resolved_strong_declared",
                "fulltext_exists": "unknown",
                "evidence_payload": {"fulltext_links": [], "auxiliary_fulltext_links": []},
            }

        def fetch(*_args, **_kwargs):
            calls.append(True)
            return {"status": "not_found", "method": "test"}

        with tempfile.TemporaryDirectory() as run:
            with self._with_state_dir(run):
                fetched = pipeline.process_source(
                    dict(self.REF),
                    run,
                    deps=pipeline.ProcessSourceDeps(
                        resolve_ref=resolution,
                        fetch_fulltext=fetch,
                    ),
                )
                skipped = pipeline.process_source(
                    dict(self.REF),
                    run,
                    deps=pipeline.ProcessSourceDeps(
                        resolve_ref=resolution,
                        fetch_fulltext=fetch,
                        allow_inline_fulltext=False,
                    ),
                )

        self.assertEqual(calls, [True])
        self.assertEqual(fetched.fetched, {"status": "not_found", "method": "test"})
        self.assertIsNone(skipped.fetched)
        self.assertIsNone(skipped.cached_source)

    def test_pipeline_prefers_official_auxiliary_pdf_before_preprint_winner_links(self):
        from core.fetch import service as fetch_mod

        captured = {"queues": []}
        orig_process_queue = fetch_mod._fetch_pipeline.process_queue
        orig_direct_rows = fetch_mod._provider_direct_text_rows
        orig_candidate_rows = fetch_mod._provider_candidate_rows
        orig_fetch_url = fetch_mod._fetch_url
        try:
            fetch_mod._provider_direct_text_rows = lambda ref, email: []
            fetch_mod._provider_candidate_rows = lambda ref, email: []
            # Queue priority is the subject of this test; discovery must not
            # open a real transport or its persistent host-backoff database.
            fetch_mod._fetch_url = lambda *_args, **_kwargs: None

            def fake_process_queue(**kwargs):
                captured["queues"].append([item.get("url") for item in kwargs.get("queue") or []])
                return {
                    "status": "not_found",
                    "method": "metadata",
                    "reason": "stubbed fetch attempt",
                }

            fetch_mod._fetch_pipeline.process_queue = fake_process_queue
            deps = pipeline.ProcessSourceDeps(
                resolve_ref=lambda _ref: {
                    "ref_id": "r1",
                    "status": "resolved",
                    "via": "openalex_search",
                    "identity": {
                        "scheme": "doi",
                        "value": "10.48550/arXiv.1503.08895",
                        "is_strong": True,
                        "class_": "global",
                    },
                    "identity_state": "resolved_strong_discovered",
                    "fulltext_exists": True,
                    "fulltext_links": [
                        {"url": "https://doi.org/10.48550/arXiv.1503.08895", "content_type": "doi"},
                        {"url": "https://arxiv.org/pdf/1503.08895", "content_type": "pdf"},
                    ],
                    "evidence_payload": {
                        "fulltext_links": [
                            {"url": "https://doi.org/10.48550/arXiv.1503.08895", "content_type": "doi"},
                            {"url": "https://arxiv.org/pdf/1503.08895", "content_type": "pdf"},
                        ],
                        "auxiliary_fulltext_links": [
                            {
                                "url": "https://proceedings.neurips.cc/paper_files/paper/2015/file/8fb21ee7a2207526da55a679f0332de2-Paper.pdf",
                                "content_type": "pdf",
                                "discovered_via": "neurips_search",
                            }
                        ],
                        "oa_status": "open",
                        "fulltext_exists": True,
                        "abstract": None,
                        "matched_title": "End-to-end memory networks",
                        "work_type": "conference paper",
                        "via": "openalex_search",
                    },
                },
                fetch_fulltext=fetch_mod.fetch_fulltext,
            )
            with tempfile.TemporaryDirectory() as run:
                with self._with_state_dir(run):
                    result = pipeline.process_source(
                        dict(
                            self.REF,
                            doi=None,
                            title="End-to-end memory networks",
                            year=2015,
                            raw_entry=(
                                "Sainbayar Sukhbaatar, Arthur Szlam, Jason Weston, and Rob Fergus. "
                                "End-to-end memory networks. In Advances in Neural Information Processing Systems 28, 2015."
                            ),
                        ),
                        run,
                        deps=deps,
                    )
        finally:
            fetch_mod._fetch_pipeline.process_queue = orig_process_queue
            fetch_mod._provider_direct_text_rows = orig_direct_rows
            fetch_mod._provider_candidate_rows = orig_candidate_rows
            fetch_mod._fetch_url = orig_fetch_url

        self.assertIsNotNone(result.fetched)
        self.assertEqual(result.fetched["status"], "not_found")
        self.assertGreaterEqual(len(captured["queues"]), 2)
        self.assertEqual(
            captured["queues"][0][0],
            "https://proceedings.neurips.cc/paper_files/paper/2015/file/8fb21ee7a2207526da55a679f0332de2-Paper.pdf",
        )
        self.assertIn("https://arxiv.org/pdf/1503.08895", captured["queues"][1])


@pytest.mark.parametrize("changed", [False, True])
def test_abstract_cache_preserves_current_resolver_text(tmp_path, monkeypatch, changed):
    from core.app.runtime.sources import _store_resolve_abstract_if_needed
    from tests._resolve_fixtures import current_resolution

    monkeypatch.setenv("CITATION_VERIFIER_STATE_DIR", str(tmp_path / "state"))
    ref = dict(TestPipelineSerial.REF)
    seed, current = tmp_path / "seed", tmp_path / "current"
    for directory in (seed, current):
        repo = RunRepository.create(
            str(directory), run_id=directory.name, input_path="paper.pdf",
            input_sha256="a" * 64, accuracy="abstract", style=None,
            model_id=None, http_profile=None, challenge_mode=None,
            fixture_fingerprint="abstract-cache",
        )
        repo.replace_parse_payload(claims=[], references=[ref], citations=[])
        repo.close()
    old_text = "Previously retrieved abstract for Attention is all you need."
    text = "Updated abstract for Attention is all you need." if changed else old_text
    sources.store_text(str(seed), ref, "abstract", "openalex", old_text)
    resolution = current_resolution(ref, {
        "status": "resolved", "via": "crossref", "matched_title": ref["title"],
        "abstract": text, "abstract_via": "openalex", "fulltext_exists": False,
        "attempts": [{"via": "crossref", "status": "resolved"}],
    })
    fetch_calls = []
    result = pipeline.process_source(ref, str(current), deps=pipeline.ProcessSourceDeps(
        resolve_ref=lambda _ref: resolution,
        fetch_fulltext=lambda *args, **kwargs: fetch_calls.append((args, kwargs)) or {"status": "skipped"},
    ))
    assert bool(fetch_calls) is changed
    assert (result.cached_source is None) is changed
    _store_resolve_abstract_if_needed(str(current), ref, resolution)
    repo = RunRepository.open(str(current))
    try:
        records = repo.list_source_texts(ref["id"])
        assert len(records) == 1
        stored_path = os.path.join(str(current), records[0].stored_path)
        with open(stored_path, encoding="utf-8") as handle:
            assert handle.read() == text
    finally:
        repo.close()
