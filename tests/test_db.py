#!/usr/bin/env python3
# tests/test_db.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the SQLite-backed run repository."""
import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from tests._bootstrap import *  # noqa: F401,F403

from core.fetch.diagnostics import gaps as gaps_mod
from core.infra.db import RunRepository
from core.infra.db.schema import SCHEMA_VERSION
from core.verify.claim_evidence import ClaimEvidenceRuntime
from core.verify.claim_evidence.evidence.context import build_effective_context
from core.verify.source_spans import build_catalog


def _valid_resolve_evidence_profile(*, risk_score=1, risk_band="low"):
    return {
        "has_identifier": True,
        "has_searchable_title": True,
        "has_author": True,
        "has_year": True,
        "has_venue": False,
        "source_kind": "article_like",
        "source_type_confidence": "high",
        "source_type_evidence": [],
        "indexability": "high",
        "checks_completed": ["crossref", "doi.org"],
        "minimum_checks_completed": True,
        "best_candidate": None,
        "resolution_basis": "doi",
        "existence_confidence": "high",
        "title_overlap": 1.0,
        "metadata_match": None,
        "identifier_fallback": None,
        "synthetic_reference_risk": {
            "score": risk_score,
            "band": risk_band,
            "signals": ["complete_core_bibliography"],
        },
    }


def _seed_claim_evidence_source(
    repo,
    *,
    source_text_id="s1",
    ref_id="r1",
    source_text="Claim [1].",
    stored_path=None,
    origin="user",
):
    stored_path = stored_path or f"sources/parsed/{source_text_id}.txt"
    source_path = Path(repo._run_dir) / stored_path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source_text, encoding="utf-8")
    source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    repo.store_source_text(
        source_text_id=source_text_id,
        ref_id=ref_id,
        identity_key=f"utf8:{source_text_id}",
        tier="fulltext",
        origin=origin,
        stored_path=stored_path,
        sha256=source_sha256,
        char_count=len(source_text),
    )
    return source_text, source_sha256


def _claim_evidence_task_payload(
    repo,
    *,
    claim_sentence="Claim [1].",
    source_text="Claim [1].",
    claim_id="c1",
    ref_id="r1",
    scope="abstract_only",
    source_text_id="s1",
    stored_path=None,
    origin="user",
):
    stored_source_text, source_sha256 = _seed_claim_evidence_source(
        repo,
        source_text_id=source_text_id,
        ref_id=ref_id,
        source_text=source_text,
        stored_path=stored_path,
        origin=origin,
    )
    payload, context = ClaimEvidenceRuntime.prepare_task(
        {"sentence": claim_sentence, "context_window": "Manuscript context.", "marker_raw": "[1]"},
        stored_source_text,
    )
    return {
        "kind": "claim_evidence",
        "status": "pending",
        "semantic_contract": ClaimEvidenceRuntime.contract_id,
        "claim_id": claim_id,
        "ref_id": ref_id,
        "scope": scope,
        "claim_evidence_payload": payload,
        "effective_context": context,
        "source_text_id": source_text_id,
        "source_text_sha256": source_sha256,
        "answer": None,
    }


def _web_research_task_payload(
    *,
    ref_id="r1",
    ref_number=1,
    raw_entry="Smith. A paper. 2020.",
    claim_sentences=None,
):
    return {
        "kind": "web_research",
        "status": "pending",
        "attempts": 0,
        "ref_id": ref_id,
        "ref_number": ref_number,
        "reference": {
            "ref_number": ref_number,
            "raw_entry": raw_entry,
            "title": None,
            "doi": None,
            "pmid": None,
            "url": None,
            "isbn": None,
            "year": None,
            "source_type": None,
        },
        "claim_sentences": list(claim_sentences or ["Claim [1]."]),
        "instructions": (
            "Use web research tools to find third-party pages and return "
            "{found, findings:[{url, stance, quote}]}."
        ),
        "answer": None,
    }


class TestRunRepositoryParseAndResolve(unittest.TestCase):
    def _make_repo(self, run_dir):
        return RunRepository.create(
            run_dir,
            run_id="run-db-1",
            input_path="C:\\input.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style=None,
            model_id="model-x",
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-1",
        )

    def _freeze_fetch_candidate(self, repo):
        repo.replace_parse_payload(
            claims=[],
            references=[{
                "id": "r1",
                "ref_number": 1,
                "raw_entry": "Smith. A frozen paper. 2020.",
            }],
            citations=[],
        )
        repo.freeze_fetch_candidate_stage(
            "r1",
            "published",
            {"reference": "r1"},
            [{
                "method": "doi",
                "url": "https://example.test/frozen.pdf",
                "kind": "pdf",
                "queue_index": 0,
                "batch_index": 0,
            }],
        )

    def test_frozen_fetch_audit_rejects_in_place_restart_and_reparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                self._freeze_fetch_candidate(repo)
                before = (
                    repo.get_run().status,
                    repo.get_run().phase,
                    [row.ref_id for row in repo.list_references()],
                    repo._conn.execute(
                        "SELECT COUNT(*) FROM fetch_candidate_plans"
                    ).fetchone()[0],
                    repo._conn.execute(
                        "SELECT COUNT(*) FROM phase_events"
                    ).fetchone()[0],
                )

                with self.assertRaisesRegex(RuntimeError, "clone_as_fresh_run"):
                    repo.restart_in_place(reason="test")
                self.assertEqual(
                    (
                        repo.get_run().status,
                        repo.get_run().phase,
                        [row.ref_id for row in repo.list_references()],
                        repo._conn.execute(
                            "SELECT COUNT(*) FROM fetch_candidate_plans"
                        ).fetchone()[0],
                        repo._conn.execute(
                            "SELECT COUNT(*) FROM phase_events"
                        ).fetchone()[0],
                    ),
                    before,
                )

                with self.assertRaisesRegex(RuntimeError, "clone_as_fresh_run"):
                    repo.replace_parse_payload(
                        claims=[],
                        references=[{
                            "id": "r2",
                            "ref_number": 2,
                            "raw_entry": "Jones. A replacement paper. 2021.",
                        }],
                        citations=[],
                    )
                self.assertEqual(
                    [row.ref_id for row in repo.list_references()], ["r1"]
                )
                self.assertEqual(
                    repo._conn.execute(
                        "SELECT COUNT(*) FROM fetch_candidate_plans"
                    ).fetchone()[0],
                    1,
                )

                clone = repo.clone_as_fresh_run(
                    new_run_dir=os.path.join(tmp, "fresh-clone"),
                    new_run_id="fresh-clone",
                )
                try:
                    clone.replace_parse_payload(
                        claims=[],
                        references=[{
                            "id": "r2",
                            "ref_number": 2,
                            "raw_entry": "Jones. A replacement paper. 2021.",
                        }],
                        citations=[],
                    )
                    self.assertEqual(
                        [row.ref_id for row in clone.list_references()], ["r2"]
                    )
                finally:
                    clone.close()
            finally:
                repo.close()

    def test_resolve_fulltext_links_are_typed_and_roundtrip_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.replace_parse_payload(
                    claims=[],
                    references=[{
                        "ref_id": "r1", "ref_number": 1,
                        "raw_entry": "Reference",
                    }],
                    citations=[],
                )
                payload = {
                    "status": "resolved", "fulltext_exists": True,
                    "fulltext_links": [{"url": "https://example.test/a.pdf", "content_type": "pdf", "discovered_via": "crossref", "provenance": ["crossref", "openalex"], "identity_context": {"title": "A", "authors": ["One", "Two"], "year": 2024, "identifiers": {"doi": "10.1/a"}, "provider": "crossref", "canonical_host": True}, "identity_contexts": [{"title": "A alternate", "identifiers": {"pmid": "1"}}], "identity_context_conflict": True}],
                    "auxiliary_fulltext_links": [], "attempts": [],
                }
                repo.upsert_resolve_result("r1", payload)
                row = repo.get_resolve_result("r1")
                self.assertTrue(row.fulltext_exists)
                self.assertEqual(row.fulltext_links, payload["fulltext_links"])
                self.assertEqual(row.auxiliary_fulltext_links, [])
                cols = {r["name"] for r in repo._conn.execute("PRAGMA table_info(resolve_results)")}
                self.assertNotIn("fulltext_links_json", cols)
                self.assertNotIn("auxiliary_fulltext_links_json", cols)
            finally:
                repo.close()

    def test_resolved_identifier_roundtrips_and_rejects_malformed_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.replace_parse_payload(
                    claims=[],
                    references=[{"ref_id": "r1", "ref_number": 1, "raw_entry": "Reference"}],
                    citations=[],
                )
                payload = {
                    "status": "resolved",
                    "fulltext_exists": True,
                    "resolved_identifier": {
                        "type": "doi",
                        "value": "10.1145/1390156.1390177",
                        "validated_via": "crossref",
                    },
                }
                repo.upsert_resolve_result("r1", payload)

                row = repo.get_resolve_result("r1")
                self.assertIsNotNone(row)
                self.assertEqual(row.resolved_identifier, payload["resolved_identifier"])
                self.assertEqual(
                    repo.resolve_payload_map()["r1"]["resolved_identifier"],
                    payload["resolved_identifier"],
                )

                for malformed in (
                    {},
                    {"type": "doi"},
                    {"type": "doi", "value": "10.1/x", "unexpected": "value"},
                    {"type": "", "value": "10.1/x"},
                    {"type": "doi", "value": 10},
                    {"type": "doi", "value": "10.1/x", "validated_via": ""},
                ):
                    with self.assertRaisesRegex(ValueError, "resolved_identifier"):
                        repo.upsert_resolve_result(
                            "r1",
                            {"status": "resolved", "resolved_identifier": malformed},
                        )

                repo.upsert_resolve_result("r1", {"status": "resolved"})
                self.assertNotIn("resolved_identifier", repo.resolve_payload_map()["r1"])
                with self.assertRaises(sqlite3.IntegrityError):
                    repo._conn.execute(
                        """
                        UPDATE resolve_results
                        SET resolved_identifier_validated_via = 'crossref'
                        WHERE ref_id = 'r1'
                        """
                    )
            finally:
                repo.close()

    def test_open_and_open_readonly_reject_inconsistent_current_schema_states_immutably(self):
        def _assert_rejected_immutably(run_dir, *, message):
            path = os.path.join(run_dir, "run.sqlite")
            with open(path, "rb") as db_file:
                before_hash = hashlib.sha256(db_file.read()).hexdigest()
            before_mtime_ns = os.stat(path).st_mtime_ns
            sidecars = [f"{path}-wal", f"{path}-shm", f"{path}-journal"]
            before_sidecars = {sidecar: os.path.exists(sidecar) for sidecar in sidecars}

            for opener in (RunRepository.open, RunRepository.open_readonly):
                with self.assertRaisesRegex(RuntimeError, message):
                    opener(run_dir)

            with open(path, "rb") as db_file:
                self.assertEqual(hashlib.sha256(db_file.read()).hexdigest(), before_hash)
            self.assertEqual(os.stat(path).st_mtime_ns, before_mtime_ns)
            self.assertEqual(
                {sidecar: os.path.exists(sidecar) for sidecar in sidecars},
                before_sidecars,
            )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            repo._conn.execute("PRAGMA journal_mode = DELETE")
            repo.close()
            path = os.path.join(tmp, "run.sqlite")

            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION - 1),),
                )
            _assert_rejected_immutably(tmp, message="unsupported")

            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION + 1),),
                )
            _assert_rejected_immutably(tmp, message="unsupported")

            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                conn.execute("DROP TABLE meta")
            _assert_rejected_immutably(tmp, message="schema.*metadata")

            structural_dir = os.path.join(tmp, "missing-trigger")
            os.mkdir(structural_dir)
            structural = self._make_repo(structural_dir)
            structural._conn.execute("PRAGMA journal_mode = DELETE")
            structural.close()
            structural_path = os.path.join(structural_dir, "run.sqlite")
            with closing(sqlite3.connect(structural_path)) as conn, conn:
                conn.execute("DROP TRIGGER phase_events_no_update")
            _assert_rejected_immutably(structural_dir, message="missing required current objects")

    def test_replace_parse_payload_roundtrips_claims_references_and_citations(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.replace_parse_payload(
                    claims=[
                        {
                            "id": "c1",
                            "sentence": "Claim one [1].",
                            "context_window": "Claim one [1]. Context.",
                            "marker_raw": "[1]",
                            "structural_provenance": {"boundary_kinds": ["page_layout_interruption"]},
                        },
                        {
                            "claim_id": "c2",
                            "sentence": "Claim two [2].",
                            "claim_order": 7,
                        },
                    ],
                    references=[
                        {
                            "id": "r1",
                            "ref_number": 1,
                            "raw_entry": "Smith. A paper. 2020.",
                            "title": "A paper",
                            "doi": "10.1/a",
                            "source_type": "article",
                            "source_kind": "article_like",
                            "indexability": "high",
                            "source_type_confidence": "high",
                            "ay_surname": "smith",
                            "ay_year": 2020,
                            "ay_suffix": "a",
                        },
                        {
                            "ref_id": "r2",
                            "ref_number": 2,
                            "raw_entry": "Jones. A book. 2010.",
                            "isbn": "9780000000002",
                            "source_type": "book",
                            "source_kind": "book_like",
                            "indexability": "low",
                        },
                    ],
                    citations=[
                        {"claim_id": "c1", "ref_id": "r1", "ref_number": 1},
                        {"claim_id": "c2", "ref_id": "r2", "ref_number": 2},
                    ],
                )

                claims = repo.list_claims()
                refs = repo.list_references()
                cites = repo.list_citations()
                run_rec = repo.get_run()

                self.assertEqual([c.claim_id for c in claims], ["c1", "c2"])
                self.assertEqual(claims[1].claim_order, 7)
                self.assertEqual(
                    claims[0].structural_provenance,
                    {"boundary_kinds": ["page_layout_interruption"]},
                )
                self.assertIsNone(claims[1].structural_provenance)
                parse_claims = repo.parse_payload()["claims"]
                self.assertEqual(
                    parse_claims[0]["structural_provenance"],
                    {"boundary_kinds": ["page_layout_interruption"]},
                )
                self.assertNotIn("structural_provenance", parse_claims[1])
                self.assertEqual([r.ref_id for r in refs], ["r1", "r2"])
                self.assertEqual(refs[0].doi, "10.1/a")
                self.assertEqual(
                    (refs[0].ay_surname, refs[0].ay_year, refs[0].ay_suffix),
                    ("smith", 2020, "a"),
                )
                self.assertEqual(refs[1].isbn, "9780000000002")
                self.assertEqual([(c.claim_id, c.ref_id) for c in cites], [("c1", "r1"), ("c2", "r2")])
                self.assertEqual(run_rec.phase, "resolve")
            finally:
                repo.close()

    def test_claim_structural_provenance_rejects_invalid_or_tampered_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                with self.assertRaisesRegex(ValueError, "structural_provenance"):
                    repo.replace_parse_payload(
                        claims=[{"id": "c1", "sentence": "Claim.", "structural_provenance": []}],
                        references=[],
                        citations=[],
                    )
                with self.assertRaisesRegex(ValueError, "boundary_kinds"):
                    repo.replace_parse_payload(
                        claims=[{
                            "id": "c1",
                            "sentence": "Claim.",
                            "structural_provenance": {
                                "boundary_kinds": "page_layout_interruption",
                            },
                        }],
                        references=[],
                        citations=[],
                    )
                with self.assertRaisesRegex(ValueError, "boundary_kinds"):
                    repo.replace_parse_payload(
                        claims=[{
                            "id": "c1",
                            "sentence": "Claim.",
                            "structural_provenance": {"boundary_kinds": None},
                        }],
                        references=[],
                        citations=[],
                    )
                repo.replace_parse_payload(
                    claims=[{"id": "c1", "sentence": "Claim.", "structural_provenance": {"a": 1}}],
                    references=[],
                    citations=[],
                )
                repo._conn.execute(
                    "UPDATE claims SET structural_provenance_json = ? WHERE claim_id = ?",
                    ('{ "a": 1 }', "c1"),
                )
                with self.assertRaisesRegex(RuntimeError, "not canonical"):
                    repo.list_claims()
                repo._conn.execute(
                    "UPDATE claims SET structural_provenance_json = ? WHERE claim_id = ?",
                    ('{"boundary_kinds":"page_layout_interruption"}', "c1"),
                )
                with self.assertRaisesRegex(RuntimeError, "boundary_kinds"):
                    repo.list_claims()
            finally:
                repo.close()

    def test_replace_parse_payload_tolerates_duplicate_and_claimless_citations(self):
        """A real corpus run persists two citation shapes the constraints reject.

        7-135's 2006-23058-006 gives one claim two markers to the same reference —
        a single (claim, ref) edge, but citations(claim_id, ref_id) is UNIQUE.
        icae138 gives a marker that resolved to a reference without belonging to any
        claim sentence (claim_id is None) — no claim row to point at, so it is a
        provenance-bearing coverage marker rather than a citation edge.  Both used to
        crash phase_parse's replace_parse_payload; neither is a defect in the parse, so
        persistence retains the marker and deduplicates the edge instead of aborting.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.replace_parse_payload(
                    claims=[{"id": "c1", "sentence": "One claim, two markers [1] [1]."}],
                    references=[
                        {
                            "id": "r1", "ref_number": 1,
                            "raw_entry": "Smith. A paper. 2020.",
                            "source_type": "article", "source_kind": "article_like",
                            "indexability": "high", "source_type_confidence": "high",
                        },
                        {
                            "id": "r2", "ref_number": 2,
                            "raw_entry": "Skene. A linked source. 1880.",
                            "source_type": "article", "source_kind": "article_like",
                            "indexability": "high", "source_type_confidence": "high",
                        },
                    ],
                    citations=[
                        {"claim_id": "c1", "ref_id": "r1", "ref_number": 1},
                        {"claim_id": "c1", "ref_id": "r1", "ref_number": 1},   # duplicate edge
                        {
                            "claim_id": None, "ref_id": "r2", "ref_number": 2,
                            "provenance": "link_silent_resolved", "marker_raw": "1880 ,",
                        },
                    ],
                    coverage={
                        "cited": 2, "total": 2, "pct": 100.0,
                        "in_prose": 2, "in_tables": 0, "uncited": [],
                        "warning": None, "fatal": False,
                    },
                    parse_extract={},
                )
                cites = repo.list_citations()
                # The duplicate collapses to one edge; the claimless row never becomes
                # a verifiable edge, but remains in the Parse projection and coverage.
                self.assertEqual([(c.claim_id, c.ref_id) for c in cites], [("c1", "r1")])
                self.assertEqual(
                    repo.parse_payload()["citations"],
                    [
                        {
                            "citation_id": 1, "claim_id": "c1",
                            "ref_id": "r1", "ref_number": 1,
                        },
                        {
                            "ref_id": "r2", "ref_number": 2, "claim_id": None,
                            "provenance": "link_silent_resolved", "marker_raw": "1880 ,",
                        },
                    ],
                )
                self.assertEqual(repo.parse_coverage()["cited"], 2)
                self.assertEqual(repo.parse_coverage()["in_prose"], 2)
            finally:
                repo.close()


class TestRunRepositoryFetchAndSources(unittest.TestCase):
    def _make_repo(self, run_dir):
        repo = RunRepository.create(
            run_dir,
            run_id="run-db-fetch",
            input_path="C:\\input.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style=None,
            model_id="model-x",
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-1",
        )
        repo.replace_parse_payload(
            manuscript_text="Complete manuscript text.",
            claims=[{
                "id": "c1",
                "sentence": "Claim [1].",
                "context_window": "Manuscript context.",
                "marker_raw": "[1]",
            }],
            references=[{"id": "r1", "ref_number": 1, "raw_entry": "Smith. A paper. 2020."}],
            citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
        )
        return repo

    def test_append_fetch_attempt_roundtrips_trace_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                attempt_id = repo.append_fetch_attempt(
                    "r1",
                    method="reference_url",
                    url="https://example.org/a",
                    kind="landing",
                    final_url="https://example.org/final",
                    status_code=403,
                    content_type="text/html",
                    outcome="challenge_blocked",
                    reason="cloudflare",
                    challenge_blocked=True,
                    paywalled=False,
                    trace={
                        "queue_index": 0,
                        "batch_index": 0,
                        "method": "reference_url",
                        "url": "https://example.org/a",
                        "kind": "landing",
                        "outcome": "challenge_blocked",
                        "status": 403,
                        "final_url": "https://example.org/final",
                        "content_type": "text/html",
                        "reason": "cloudflare",
                    },
                    origin="resolved_primary",
                )
                rows = repo.list_fetch_attempts("r1")
                self.assertEqual(attempt_id, rows[0]["fetch_attempt_id"])
                self.assertEqual(rows[0]["outcome"], "challenge_blocked")
                self.assertTrue(rows[0]["challenge_blocked"])
                self.assertEqual(rows[0]["trace"]["method"], "reference_url")
                self.assertEqual(rows[0]["origin"], "resolved_primary")
                self.assertEqual(repo.get_run().phase, "resolve")
            finally:
                repo.close()


class TestRunRepositoryTasksAndVerificationPairs(unittest.TestCase):
    def _make_repo(self, run_dir):
        repo = RunRepository.create(
            run_dir,
            run_id="run-db-tasks",
            input_path="C:\\input.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style=None,
            model_id="model-x",
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-1",
        )
        repo.replace_parse_payload(
            manuscript_text="Complete manuscript text.",
            claims=[{
                "id": "c1",
                "sentence": "Claim [1].",
                "context_window": "Manuscript context.",
                "marker_raw": "[1]",
            }],
            references=[{"id": "r1", "ref_number": 1, "raw_entry": "Smith. A paper. 2020."}],
            citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
        )
        return repo

    def test_task_lifecycle_and_answers_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                payload = _web_research_task_payload()
                repo.create_task(
                    task_id="t1",
                    slot="research",
                    ref_id="r1",
                    claim_id=None,
                    scope=None,
                    task_payload=payload,
                )
                repo.store_task_answer(
                    answer_id="a1",
                    task_id="t1",
                    actor_type="llm",
                    raw_payload={"found": False, "findings": []},
                    accepted_for_processing=False,
                )
                repo.store_task_answer(
                    answer_id="a2",
                    task_id="t1",
                    actor_type="script",
                    raw_payload={
                        "found": True,
                        "findings": [{
                            "url": "https://example.org/evidence",
                            "stance": "supports",
                            "quote": "x",
                        }],
                    },
                    accepted_for_processing=True,
                )

                self.assertEqual(repo.list_pending_tasks(slot="research")[0].task_id, "t1")
                self.assertEqual(len(repo.list_task_answers("t1")), 2)
                self.assertEqual(repo.get_latest_task_answer("t1").answer_id, "a2")

                repo.mark_task_answered("t1")
                answered = repo.list_answered_tasks_waiting_apply(slot="research")
                self.assertEqual(answered[0].task_id, "t1")
                self.assertEqual(answered[0].status, "answered")

                repo.mark_task_applied("t1")
                applied = repo.get_task("t1")
                self.assertEqual(applied.status, "applied")
                self.assertIsNotNone(applied.applied_at)
            finally:
                repo.close()

    def test_claim_evidence_context_is_exact_across_task_writes(self):
        raw = "decomposed e\u0301\x00context"
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                payload = _claim_evidence_task_payload(
                    repo,
                    source_text=raw,
                    source_text_id="s-exact",
                    stored_path="sources/parsed/s-exact.txt",
                )
                repo.create_task(
                    task_id="t-exact", slot="verify", ref_id="r1",
                    claim_id="c1", scope="abstract_only", task_payload=payload,
                )
                normalized = repo.get_task("t-exact").task_payload
                self.assertEqual(normalized["kind"], "claim_evidence")
                self.assertEqual(normalized["source_text_id"], "s-exact")
                self.assertNotIn("source_text_path", normalized)
                repo.update_task_payload("t-exact", normalized)
                repo.reopen_task("t-exact", task_payload=normalized)
                repo.apply_task("t-exact", task_payload=normalized)
                applied = repo.get_task("t-exact").task_payload
                self.assertEqual(applied["status"], "done")
                self.assertEqual(applied["source_text_id"], normalized["source_text_id"])
                self.assertEqual(applied["source_text_sha256"], normalized["source_text_sha256"])
                self.assertEqual(applied["claim_evidence_payload"], normalized["claim_evidence_payload"])
                self.assertEqual(applied["effective_context"], normalized["effective_context"])

                repo.create_task(
                    task_id="t-exact-atomic", slot="verify", ref_id="r1",
                    claim_id="c1", scope="fulltext_complete",
                    task_payload=_claim_evidence_task_payload(
                        repo,
                        scope="fulltext_complete",
                        source_text=raw,
                        source_text_id="s-exact-atomic",
                        stored_path="sources/parsed/s-exact-atomic.txt",
                        origin="manual",
                    ),
                )
                atomic_payload = repo.get_task("t-exact-atomic").task_payload
                repo.ensure_verification_pair(
                    claim_id="c1", ref_id="r1", scope="fulltext_complete",
                )
                self.assertTrue(repo.claim_verification_pair_terminal(
                    claim_id="c1",
                    ref_id="r1",
                    scope="fulltext_complete",
                    status="accepted",
                    outcome="supports",
                    cause="jury_confirmed",
                    call_id="call-1",
                ))
                repo.apply_task("t-exact-atomic", task_payload=atomic_payload)
                applied_atomic = repo.get_task("t-exact-atomic").task_payload
                self.assertEqual(applied_atomic["status"], "done")
                self.assertEqual(applied_atomic["source_text_id"], atomic_payload["source_text_id"])
                self.assertEqual(applied_atomic["source_text_sha256"], atomic_payload["source_text_sha256"])
                self.assertEqual(
                    applied_atomic["claim_evidence_payload"],
                    atomic_payload["claim_evidence_payload"],
                )
                self.assertEqual(
                    applied_atomic["effective_context"],
                    atomic_payload["effective_context"],
                )

                path_payload = dict(normalized)
                path_payload["source_text_path"] = "sources/parsed/s-path-rejected.txt"
                with self.assertRaisesRegex(ValueError, "unsupported shape"):
                    repo.update_task_payload("t-exact", path_payload)

                repo._conn.execute(
                    "UPDATE manuscript_text SET sha256=?", ("0" * 64,)
                )
                persisted = repo.get_task("t-exact").task_payload
                self.assertEqual(
                    persisted["claim_evidence_payload"],
                    applied["claim_evidence_payload"],
                )
                self.assertNotIn(
                    "manuscript_text", persisted["claim_evidence_payload"]
                )

                with self.assertRaises(ValueError):
                    repo.set_run_setting("debug_event_log", raw)
            finally:
                repo.close()

    def test_repository_sanitizes_nul_bytes_in_json_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                with self.assertRaises(ValueError):
                    repo.set_run_setting("debug_event_log", "alpha\x00beta")
                with self.assertRaises(ValueError):
                    repo.set_run_setting("audit_note", {"text": "alpha"})
            finally:
                repo.close()

    def test_claim_evidence_rag_retrieval_config_round_trips_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                source_text = "Claim [1]."
                payload = _claim_evidence_task_payload(repo, source_text=source_text)
                repo.create_task(
                    task_id="t-full-config",
                    slot="verify",
                    ref_id="r1",
                    claim_id="c1",
                    scope="abstract_only",
                    task_payload=payload,
                )
                span = build_catalog(source_text).spans[0]
                context = build_effective_context(
                    source_text,
                    mode="extractive_rag",
                    budget=len(source_text),
                    retrieved_ranges=[(span.raw_start, span.raw_end)],
                    retrieval_algorithm="bm25",
                    retrieval_config={"k": 10, "stem": True},
                )
                payload["effective_context"] = context.snapshot()
                repo.create_task(
                    task_id="t-rag-config",
                    slot="verify",
                    ref_id="r1",
                    claim_id="c1",
                    scope="abstract_only",
                    task_payload=payload,
                )
                self.assertEqual(
                    repo.get_task("t-rag-config").task_payload["effective_context"],
                    context.snapshot(),
                )
                repo._conn.execute(
                    "UPDATE task_claim_evidence_details SET retrieval_config=? WHERE task_id=?",
                    ('{"k":11,"stem":true}', "t-rag-config"),
                )
                with self.assertRaises(RuntimeError):
                    repo.get_task("t-rag-config")

                repo._conn.execute(
                    "UPDATE task_claim_evidence_details SET retrieval_config=? WHERE task_id=?",
                    ('{"unexpected":true}', "t-full-config"),
                )
                with self.assertRaisesRegex(RuntimeError, "full-text"):
                    repo.get_task("t-full-config")
            finally:
                repo.close()

    def test_invalid_identifier_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                payload = _claim_evidence_task_payload(repo)
                with self.assertRaises(ValueError):
                    repo.create_task(
                        task_id="bad id with spaces",
                        slot="verify",
                        ref_id="r1",
                        claim_id="c1",
                        scope="abstract_only",
                        task_payload=payload,
                    )
            finally:
                repo.close()

    def _prepare_atomic_terminal(self, repo, *, task_id="t-terminal", scope="abstract_only"):
        payload = _claim_evidence_task_payload(repo, scope=scope)
        repo.create_task(
            task_id=task_id,
            slot="verify",
            ref_id="r1",
            claim_id="c1",
            scope=scope,
            task_payload=payload,
        )
        repo.ensure_verification_pair(claim_id="c1", ref_id="r1", scope=scope)
    def test_claim_verification_pair_terminal_raises_when_pair_state_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.create_task(
                    task_id="t-terminal",
                    slot="verify",
                    ref_id="r1",
                    claim_id="c1",
                    scope="abstract_only",
                    task_payload=_claim_evidence_task_payload(repo),
                )
                with self.assertRaisesRegex(RuntimeError, "pair state missing"):
                    repo.claim_verification_pair_terminal(
                        claim_id="c1",
                        ref_id="r1",
                        scope="abstract_only",
                        status="accepted",
                        outcome="supports",
                        cause="jury_confirmed",
                        call_id="call-1",
                    )
                self.assertEqual(repo.get_task("t-terminal").status, "pending")
                transitions = repo._conn.execute(
                    "SELECT COUNT(*) AS n FROM verification_pair_transitions"
                ).fetchone()["n"]
                self.assertEqual(transitions, 0)
            finally:
                repo.close()

    def test_claim_verification_pair_terminal_is_idempotent_and_writes_one_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                self._prepare_atomic_terminal(repo)
                self.assertTrue(repo.claim_verification_pair_terminal(
                    claim_id="c1",
                    ref_id="r1",
                    scope="abstract_only",
                    status="accepted",
                    outcome="supports",
                    cause="jury_confirmed",
                    call_id="call-1",
                ))
                self.assertFalse(repo.claim_verification_pair_terminal(
                    claim_id="c1",
                    ref_id="r1",
                    scope="abstract_only",
                    status="accepted",
                    outcome="supports",
                    cause="jury_confirmed",
                    call_id="call-2",
                ))
                state = repo.get_verification_pair_state(
                    claim_id="c1", ref_id="r1", scope="abstract_only",
                )
                self.assertEqual(state["status"], "accepted")
                transitions = repo._conn.execute(
                    "SELECT to_status FROM verification_pair_transitions"
                ).fetchall()
                self.assertEqual([row["to_status"] for row in transitions], ["accepted"])
                self.assertEqual(repo.get_task("t-terminal").status, "pending")
            finally:
                repo.close()

    def test_lifecycle_diagnostics_detect_terminal_orphan_deterministically(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                self._prepare_atomic_terminal(repo, task_id="t-orphan")
                self.assertTrue(repo.claim_verification_pair_terminal(
                    claim_id="c1",
                    ref_id="r1",
                    scope="abstract_only",
                    status="accepted",
                    outcome="supports",
                    cause="pair_orphaned",
                    call_id="call-orphan",
                ))

                diagnostics = repo.list_verification_lifecycle_violations()
                self.assertEqual(
                    [row["code"] for row in diagnostics],
                    ["terminal_pair_verify_task_not_applied"],
                )
                self.assertEqual(diagnostics[0]["task_id"], "t-orphan")
                self.assertEqual(diagnostics[0]["task_status"], "pending")
            finally:
                repo.close()

    def test_lifecycle_diagnostics_are_empty_for_atomic_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                self._prepare_atomic_terminal(repo)
                self.assertTrue(repo.claim_verification_pair_terminal(
                    claim_id="c1",
                    ref_id="r1",
                    scope="abstract_only",
                    status="accepted",
                    outcome="supports",
                    cause="jury_confirmed",
                    call_id="call-1",
                ))
                repo.apply_task(
                    "t-terminal",
                    task_payload=repo.get_task("t-terminal").task_payload,
                )
                self.assertEqual(repo.list_verification_lifecycle_violations(), [])
            finally:
                repo.close()

    def test_lifecycle_diagnostics_ignore_applied_web_research_without_pair_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.create_task(
                    task_id="t-web-research",
                    slot="research",
                    ref_id="r1",
                    claim_id=None,
                    scope=None,
                    task_payload=_web_research_task_payload(),
                )
                repo.apply_task("t-web-research")
                self.assertEqual(repo.list_verification_lifecycle_violations(), [])
            finally:
                repo.close()

    def test_lifecycle_diagnostics_ignore_applied_verify_task_without_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.create_task(
                    task_id="t-applied",
                    slot="verify",
                    ref_id="r1",
                    claim_id="c1",
                    scope="fulltext_complete",
                    task_payload=_claim_evidence_task_payload(
                        repo,
                        scope="fulltext_complete",
                        source_text_id="s-applied",
                        stored_path="sources/parsed/s-applied.txt",
                    ),
                )
                repo.apply_task("t-applied")
                self.assertEqual(repo.list_verification_lifecycle_violations(), [])
            finally:
                repo.close()


class TestRunRepositoryExportsAndGate(unittest.TestCase):
    def _make_repo(self, run_dir):
        repo = RunRepository.create(
            run_dir,
            run_id="run-db-export",
            input_path="C:\\papers\\paper.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style=None,
            model_id="model-x",
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-1",
        )
        repo.replace_parse_payload(
            manuscript_text="Complete manuscript text.",
            claims=[{
                "id": "c1",
                "sentence": "Claim [1].",
                "context_window": "Manuscript context.",
                "marker_raw": "[1]",
            }],
            references=[{"id": "r1", "ref_number": 1, "raw_entry": "Smith. A paper. 2020."}],
            citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
        )
        return repo

    def test_gate_readiness_flags_pairs_with_text_but_no_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.store_source_text(
                    source_text_id="s1",
                    ref_id="r1",
                    identity_key="doi:10.1/a",
                    tier="fulltext",
                    origin="user",
                    stored_path="sources/parsed/1_fulltext_user.txt",
                    sha256="sha-1",
                    char_count=100,
                )
                gate = repo.verify_run_gate_readiness()
                self.assertFalse(gate["ok"])
                self.assertEqual(gate["info"]["pairs_skipped"], 1)
            finally:
                repo.close()

    def test_gate_readiness_counts_claim_evidence_terminal_as_operational(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.store_source_text(
                    source_text_id="s1",
                    ref_id="r1",
                    identity_key="doi:10.1/a",
                    tier="fulltext",
                    origin="user",
                    stored_path="sources/parsed/1_fulltext_user.txt",
                    sha256="sha-1",
                    char_count=100,
                )
                repo.ensure_verification_pair(
                    claim_id="c1",
                    ref_id="r1",
                    scope="",
                )
                self.assertTrue(
                    repo.claim_verification_pair_terminal(
                        claim_id="c1",
                        ref_id="r1",
                        scope="",
                        status="accepted",
                        outcome="related",
                        cause="jury2_not_eligible",
                        call_id="candidate-1",
                    )
                )

                gate = repo.verify_run_gate_readiness()
                self.assertTrue(gate["ok"])
                self.assertEqual(gate["info"]["pairs_skipped"], 0)
                self.assertEqual(gate["info"]["terminal_pairs"], 1)
                self.assertEqual(gate["info"]["accepted_pairs"], 1)
            finally:
                repo.close()

    def test_exports_emit_readable_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.upsert_resolve_result(
                    "r1",
                    {
                        "status": "resolved",
                        "via": "crossref",
                        "fulltext_exists": True,
                        "abstract": "Abstract text.",
                        "fulltext_links": [{"url": "https://doi.org/10.1/a", "content_type": "doi"}],
                        "auxiliary_fulltext_links": [
                            {"url": "https://example.org/a.pdf", "content_type": "pdf", "discovered_via": "openalex"}
                        ],
                        "attempts": [{"status": "resolved", "via": "crossref"}],
                        "evidence_profile": _valid_resolve_evidence_profile(),
                    },
                )
                repo.append_fetch_attempt(
                    "r1",
                    method="reference_url",
                    url="https://example.org/a",
                    kind="landing",
                    final_url="https://example.org/a",
                    status_code=200,
                    content_type="text/html",
                    outcome="stored",
                )
                repo.store_source_text(
                    source_text_id="s1",
                    ref_id="r1",
                    identity_key="doi:10.1/a",
                    tier="fulltext",
                    origin="user",
                    stored_path="sources/parsed/1_fulltext_user.txt",
                    sha256="sha-1",
                    char_count=100,
                )
                repo.create_task(
                    task_id="t1",
                    slot="verify",
                    ref_id="r1",
                    claim_id="c1",
                    scope="fulltext_complete",
                    task_payload=_claim_evidence_task_payload(
                        repo,
                        scope="fulltext_complete",
                        source_text_id="s-export",
                        stored_path="sources/parsed/s-export.txt",
                    ),
                )
                repo.ensure_verification_pair(
                    claim_id="c1",
                    ref_id="r1",
                    scope="fulltext_complete",
                )
                self.assertTrue(repo.claim_verification_pair_terminal(
                    claim_id="c1",
                    ref_id="r1",
                    scope="fulltext_complete",
                    status="accepted",
                    outcome="supports",
                    cause="jury_confirmed",
                    call_id="call-export",
                ))
                repo.apply_task(
                    "t1",
                    task_payload=repo.get_task("t1").task_payload,
                )

                payload = repo.export_report_inputs()
                self.assertEqual(payload["parse"]["references"][0]["id"], "r1")
                self.assertEqual(payload["resolve"]["r1"]["status"], "resolved")
                self.assertEqual(
                    payload["resolve"]["r1"]["fulltext_links"][0]["url"],
                    "https://doi.org/10.1/a",
                )
                self.assertEqual(
                    payload["resolve"]["r1"]["auxiliary_fulltext_links"][0]["discovered_via"],
                    "openalex",
                )
                self.assertEqual(payload["verification_raw"]["pair_states"][0]["status"], "accepted")
                self.assertEqual(payload["verification_raw"]["pair_states"][0]["terminal_outcome"], "supports")
                self.assertTrue(payload["gate"]["ok"])
            finally:
                repo.close()

    def test_export_report_inputs_includes_verification_raw_and_decoded_candidate_fields(self):
        from core.verify.claim_evidence.domain.fingerprint import (
            candidate_fingerprint,
            payload_fingerprint,
            provider_prompt_fingerprint,
            FINGERPRINT_VERSION_V2,
        )
        from core.verify.claim_evidence.contracts.jury1_flow import (
            JURY1_PROMPT_SPEC, JURY1_SYSTEM_PROMPT,
        )

        def _hash(label):
            return __import__("hashlib").sha256(label.encode("utf-8")).hexdigest()

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                adapter = repo.claim_evidence_adapter()
                payload = {
                    "source_spans": [
                        {"span_id": "span-1", "text": "quoted evidence"}
                    ],
                    "source_hash": _hash("request-1:source_hash"),
                    "cited_source_mode": "full_text",
                    "claim": "claim text",
                    "claim_context": "manuscript context",
                    "citation_marker": "[1]",
                    "task": {
                        "task_id": "explanation_evidence",
                        "instructions": JURY1_PROMPT_SPEC.task_prompt(
                            "explanation_evidence"
                        ),
                    },
                    "determined_outcome": "supports",
                }
                request = {
                    "logical_request_id": "request-1",
                    "claim_id": "c1",
                    "ref_id": "r1",
                    "scope": "",
                    "candidate_id": None,
                    "candidate_cycle": 1,
                    "stage": "explanation_evidence",
                    "payload": payload,
                    "payload_hash": payload_fingerprint(payload, version=FINGERPRINT_VERSION_V2),
                    "source_hash": _hash("request-1:source_hash"),
                    "context_hash": _hash("request-1:context_hash"),
                    "retrieval_hash": _hash("request-1:retrieval_hash"),
                    "prompt_hash": provider_prompt_fingerprint(
                        JURY1_SYSTEM_PROMPT,
                        __import__("json").dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8"),
                    ),
                    "model_hash": _hash("request-1:model_hash"),
                    "policy_hash": _hash("request-1:policy_hash"),
                }
                adapter.append_logical_request(request)
                candidate = {
                    "outcome": "supports",
                    "explanation": "because",
                    "evidence": ["quoted evidence"],
        "outcome_fields": {"claim_hash": _hash("claim text"), "source_hash": request["source_hash"], "supported_part": "the source-backed role", "incompatible_proposition": None, "reason": None, "provider_confidence": None},
                    "grounded": [{"text": "quoted evidence", "span_id": "span-1", "raw_start": 0, "raw_end": 15, "source_hash": request["source_hash"], "match_mode": "exact_raw", "score": 1.0}],
                    "source_hash": request["source_hash"],
                    "context_hash": request["context_hash"],
                    "retrieval_hash": request["retrieval_hash"],
                    "prompt_hash": request["prompt_hash"],
                    "model_hash": request["model_hash"],
                    "policy_hash": request["policy_hash"],
                }
                candidate["fingerprint"] = candidate_fingerprint(
                    outcome=candidate["outcome"],
                    evidence=tuple(candidate["evidence"]),
                    outcome_fields=candidate["outcome_fields"],
                    grounded=tuple({name: row[name] for name in ("span_id", "raw_start", "raw_end", "text", "source_hash")} for row in candidate["grounded"]), version=FINGERPRINT_VERSION_V2,
                )
                adapter.append_candidate(
                    candidate_id="candidate-1",
                    claim_id="c1",
                    ref_id="r1",
                    scope="",
                    candidate_cycle=1,
                    origin_logical_request_id="request-1",
                    record=candidate,
                )
                adapter.append_candidate_event(
                    event_id="candidate-1:terminal",
                    candidate_id="candidate-1",
                    event_type="terminal",
                    payload={"resolution": "jury2_accepted", "assurance": "passed"},
                )

                payload = repo.export_report_inputs()
                verification_raw = payload["verification_raw"]
                exported_candidate = verification_raw["candidates"][0]
                self.assertIn("verification_raw", payload)
                self.assertEqual(exported_candidate["evidence"], ["quoted evidence"])
                self.assertEqual(exported_candidate["outcome_fields"]["claim_hash"], _hash("claim text"))
                self.assertEqual(exported_candidate["grounded"][0]["span_id"], "span-1")
                self.assertNotIn("evidence_json", exported_candidate)
                self.assertNotIn("grounded_json", exported_candidate)
                self.assertNotIn("outcome_fields_json", exported_candidate)
            finally:
                repo.close()

    def test_gap_report_is_derived_without_run_setting_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.upsert_resolve_result(
                    "r1",
                    {"status": "resolved", "via": "crossref", "fulltext_exists": False},
                )
                report = gaps_mod.build_gap_report(tmp, accuracy="standard")
                self.assertEqual(report["summary"]["references"], 1)
                self.assertIsNone(
                    repo._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='run_settings'"
                    ).fetchone()
                )
            finally:
                repo.close()

    def test_gap_report_covers_a_back_reference_from_its_antecedent(self):
        # A back-reference note ("Id. at 96.") stores no source of its own: it
        # inherits the antecedent's resolve result, and verify reads the
        # antecedent's stored text.  Listing it as a gap makes the fetch phase
        # re-download and re-extract that same document once per citing note.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.replace_parse_payload(
                    claims=[],
                    references=[
                        {"id": "r1", "ref_number": 1,
                         "raw_entry": "E. Lauterpacht, Freedom of Transit, 44 Grotius Soc'y 313 (1958)."},
                        {"id": "r2", "ref_number": 2, "raw_entry": "Id. at 96."},
                    ],
                    citations=[],
                )
                resolved = {"status": "resolved", "via": "crossref", "fulltext_exists": True}
                repo.upsert_resolve_result("r1", resolved)
                repo.upsert_resolve_result(
                    "r2", dict(resolved, resolution_basis="cross_reference",
                               via="cross_reference:note_1"))
                # Only the antecedent has stored text - exactly what resolve does.
                repo.store_source_text(
                    source_text_id="s1", ref_id="r1", identity_key="k1",
                    tier="fulltext", origin="auto", stored_path="sources/r1.txt",
                    sha256="d" * 64, char_count=5000,
                )

                report = gaps_mod.build_gap_report(tmp, accuracy="standard")
                ready = {row["ref_id"] for row in report["ready_fulltext"]}
                gaps = {row["ref_id"] for row in report["gaps"]}
                self.assertIn("r1", ready)
                self.assertIn("r2", ready)
                self.assertNotIn("r2", gaps)
            finally:
                repo.close()

    def test_gap_report_merges_back_reference_tiers_without_overwriting_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.replace_parse_payload(
                    claims=[],
                    references=[
                        {"id": "r1", "ref_number": 1, "raw_entry": "Source."},
                        {"id": "r2", "ref_number": 2, "raw_entry": "Id."},
                        {"id": "r3", "ref_number": 3, "raw_entry": "Other source."},
                        {"id": "r4", "ref_number": 4, "raw_entry": "Id."},
                    ],
                    citations=[],
                )
                resolved = {"status": "resolved", "via": "crossref", "fulltext_exists": True}
                repo.upsert_resolve_result("r1", resolved)
                repo.upsert_resolve_result(
                    "r2",
                    dict(
                        resolved,
                        resolution_basis="cross_reference",
                        via="cross_reference:note_1",
                    ),
                )
                no_fulltext = {
                    "status": "resolved",
                    "via": "crossref",
                    "fulltext_exists": False,
                }
                repo.upsert_resolve_result("r3", no_fulltext)
                repo.upsert_resolve_result(
                    "r4",
                    dict(
                        no_fulltext,
                        resolution_basis="cross_reference",
                        via="cross_reference:note_3",
                    ),
                )
                repo.store_source_text(
                    source_text_id="s1",
                    ref_id="r1",
                    identity_key="k1",
                    tier="fulltext",
                    origin="antecedent_fulltext",
                    stored_path="sources/r1.txt",
                    sha256="d" * 64,
                    char_count=5000,
                )
                repo.store_source_text(
                    source_text_id="s2",
                    ref_id="r2",
                    identity_key="k2",
                    tier="abstract",
                    origin="child_abstract",
                    stored_path="sources/r2.txt",
                    sha256="e" * 64,
                    char_count=100,
                )
                repo.store_source_text(
                    source_text_id="s3",
                    ref_id="r3",
                    identity_key="k3",
                    tier="abstract",
                    origin="antecedent_abstract",
                    stored_path="sources/r3.txt",
                    sha256="c" * 64,
                    char_count=100,
                )
                repo.store_source_text(
                    source_text_id="s4",
                    ref_id="r4",
                    identity_key="k4",
                    tier="abstract",
                    origin="child_abstract",
                    stored_path="sources/r4.txt",
                    sha256="b" * 64,
                    char_count=100,
                )
                report = gaps_mod.build_gap_report(tmp, accuracy="standard")
                ready = {row["ref_id"]: row for row in report["ready_fulltext"]}
                gaps = {row["ref_id"]: row for row in report["gaps"]}
                self.assertIn("r2", ready)
                self.assertEqual(ready["r2"]["origin"], "antecedent_fulltext")
                self.assertNotIn("r2", gaps)
                self.assertEqual(ready["r4"]["tier"], "abstract")
                self.assertEqual(ready["r4"]["origin"], "child_abstract")
            finally:
                repo.close()

    def test_gap_report_keeps_a_back_reference_open_when_antecedent_has_no_text(self):
        # The fallback never invents coverage: an antecedent with nothing stored
        # leaves the citing note a genuine gap.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.replace_parse_payload(
                    claims=[],
                    references=[
                        {"id": "r1", "ref_number": 1,
                         "raw_entry": "E. Lauterpacht, Freedom of Transit, 44 Grotius Soc'y 313 (1958)."},
                        {"id": "r2", "ref_number": 2, "raw_entry": "Id. at 96."},
                    ],
                    citations=[],
                )
                resolved = {"status": "resolved", "via": "crossref", "fulltext_exists": True}
                repo.upsert_resolve_result("r1", resolved)
                repo.upsert_resolve_result(
                    "r2", dict(resolved, resolution_basis="cross_reference"))

                report = gaps_mod.build_gap_report(tmp, accuracy="standard")
                self.assertIn("r2", {row["ref_id"] for row in report["gaps"]})
            finally:
                repo.close()

    def test_cancel_task_marks_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.create_task(
                    task_id="t-cancel",
                    slot="research",
                    ref_id="r1",
                    claim_id=None,
                    scope=None,
                    task_payload=_web_research_task_payload(),
                )
                repo.cancel_task("t-cancel")
                self.assertEqual(repo.get_task("t-cancel").status, "cancelled")
            finally:
                repo.close()

    def test_store_source_text_prefers_fulltext_and_replaces_same_tier_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.store_source_text(
                    source_text_id="s1",
                    ref_id="r1",
                    identity_key="doi:10.1/a",
                    tier="abstract",
                    origin="crossref",
                    stored_path="sources/parsed/1_abstract_crossref.txt",
                    sha256="sha-1",
                    char_count=100,
                    source_ref="resolve:crossref",
                    mapping="manual",
                    match_signal="doi",
                    match_score=1.0,
                )
                repo.store_source_text(
                    source_text_id="s2",
                    ref_id="r1",
                    identity_key="doi:10.1/a",
                    tier="fulltext",
                    origin="user",
                    stored_path="sources/parsed/1_fulltext_user.txt",
                    sha256="sha-2",
                    char_count=500,
                    source_ref="provided:file.pdf",
                    mapping="manual",
                    match_signal="tokens",
                    match_score=0.9,
                    content_version="published",
                )
                repo.store_source_text(
                    source_text_id="s3",
                    ref_id="r1",
                    identity_key="doi:10.1/a",
                    tier="fulltext",
                    origin="user",
                    stored_path="sources/parsed/1_fulltext_user_v2.txt",
                    sha256="sha-3",
                    char_count=550,
                    source_ref="provided:file-v2.pdf",
                    mapping="manual",
                    match_signal="tokens",
                    match_score=0.95,
                    identity_status="externally_corroborated_text",
                    supplied_by="user",
                    supplied_via="user_manifest",
                    file_format="pdf",
                )
                all_rows = repo.list_source_texts("r1")
                best = repo.get_best_source_text("r1")

                self.assertEqual(len(all_rows), 2)
                self.assertEqual(best.source_text_id, "s3")
                self.assertEqual(best.tier, "fulltext")
                self.assertEqual(best.supplied_by, "user")
                self.assertEqual(best.supplied_via, "user_manifest")
                self.assertEqual(best.file_format, "pdf")
                manifest = repo.source_manifest_payload()["entries"]
                user_entry = next(entry for entry in manifest if entry["source_text_id"] == "s3")
                self.assertEqual(user_entry["supplied_by"], "user")
                self.assertEqual(user_entry["supplied_via"], "user_manifest")
                self.assertEqual(user_entry["file_format"], "pdf")
                self.assertTrue(repo.source_text_exists("r1"))
                self.assertTrue(repo.source_text_exists("r1", tier="abstract"))
                self.assertFalse(repo.source_text_exists("r1", tier="web"))
            finally:
                repo.close()

    def test_unreadable_queue_can_be_retired_by_ref_or_kept_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.park_unreadable_source(
                    ref_id="r1",
                    ref_number=1,
                    kept_path="sources/ocr_queue/1_fulltext_user.unreadable.pdf",
                    source_ref=None,
                    origin="user",
                    reason="scan",
                )
                repo.park_unreadable_source(
                    ref_id="r1",
                    ref_number=1,
                    kept_path="sources/ocr_queue/1_fulltext_openalex.unreadable.pdf",
                    source_ref=None,
                    origin="openalex",
                    reason="scan-from-memory",
                )
                done_one = repo.resolve_unreadable_source(
                    kept_path="sources/ocr_queue/1_fulltext_user.unreadable.pdf",
                    method="ocr",
                )
                self.assertTrue(done_one)
                rows = repo._conn.execute(
                    """
                    SELECT kept_path, ocr_status, ocr_method
                    FROM unreadable_sources
                    ORDER BY unreadable_source_id
                    """
                ).fetchall()
                self.assertEqual(rows[0]["ocr_status"], "done")
                self.assertEqual(rows[0]["ocr_method"], "ocr")
                self.assertEqual(rows[1]["ocr_status"], "pending")

                done_rest = repo.resolve_unreadable_source(ref_id="r1", method="ocr-batch")
                self.assertTrue(done_rest)
                rows = repo._conn.execute(
                    """
                    SELECT kept_path, ocr_status, ocr_method
                    FROM unreadable_sources
                    ORDER BY unreadable_source_id
                    """
                ).fetchall()
                self.assertEqual(rows[1]["ocr_status"], "done")
                self.assertEqual(rows[1]["ocr_method"], "ocr-batch")
            finally:
                repo.close()

    def test_upsert_resolve_result_roundtrips_boolean_and_json_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.replace_parse_payload(
                    manuscript_text="Complete manuscript text.",
                    claims=[{
                        "id": "c1",
                        "sentence": "Claim [1].",
                        "context_window": "Manuscript context.",
                        "marker_raw": "[1]",
                    }],
                    references=[{"id": "r1", "ref_number": 1, "raw_entry": "Smith. A paper. 2020."}],
                    citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
                )
                repo.upsert_resolve_result(
                    "r1",
                    {
                        "status": "resolved",
                        "via": "crossref",
                        "matched_title": "A paper",
                        "abstract": "Abstract text.",
                        "abstract_via": "openalex",
                        "retracted": False,
                        "fulltext_exists": True,
                        "oa_status": "open",
                        "work_type": "journal-article",
                        "resolution_basis": "doi",
                        "existence_confidence": "high",
                        "reason": None,
                        "reference_status_tag": "weak_metadata_match",
                        "fabrication_risk": "low",
                        "evidence_profile": _valid_resolve_evidence_profile(),
                        "attempts": [{"via": "crossref", "status": "resolved"}],
                        "checked_at": "2026-06-21T12:00:00+00:00",
                    },
                )

                result = repo.get_resolve_result("r1")
                listed = repo.list_resolve_results()
                run_rec = repo.get_run()

                self.assertIsNotNone(result)
                self.assertIs(result.fulltext_exists, True)
                self.assertEqual(result.abstract_via, "openalex")
                self.assertEqual(
                    result.evidence_profile["synthetic_reference_risk"]["band"],
                    "low",
                )
                self.assertEqual(listed[0].ref_id, "r1")
                self.assertEqual(run_rec.phase, "resolve")
            finally:
                repo.close()

    def test_upsert_resolve_result_accepts_pre_index_era_risk_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            profile = _valid_resolve_evidence_profile(risk_score=0, risk_band="none")
            profile["synthetic_reference_risk"]["signals"] = ["pre_index_era"]
            try:
                repo.upsert_resolve_result(
                    "r1",
                    {
                        "status": "unverified",
                        "via": "crossref_metadata",
                        "fulltext_exists": "unknown",
                        "fulltext_links": [],
                        "auxiliary_fulltext_links": [],
                        "attempts": [],
                        "evidence_profile": profile,
                    },
                )
                result = repo.get_resolve_result("r1")
                self.assertEqual(
                    result.evidence_profile["synthetic_reference_risk"]["signals"],
                    ["pre_index_era"],
                )
            finally:
                repo.close()

    def test_persistence_writes_preserve_driver_phase_across_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.update_run_phase("verify")
            finally:
                repo.close()

            repo = RunRepository.open(tmp)
            try:
                repo.upsert_resolve_result(
                    "r1",
                    {"status": "resolved", "fulltext_exists": "unknown"},
                )
                repo.append_fetch_attempt(
                    "r1",
                    method="reference_url",
                    url="https://example.org/a",
                    kind="landing",
                    final_url="https://example.org/a",
                    status_code=200,
                    content_type="text/html",
                    outcome="stored",
                )

                self.assertEqual(repo.get_run().phase, "verify")
            finally:
                repo.close()

    def test_replace_parse_payload_clears_downstream_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            try:
                repo.replace_parse_payload(
                    manuscript_text="Complete manuscript text.",
                    claims=[{
                        "id": "c1",
                        "sentence": "Claim [1].",
                        "context_window": "Manuscript context.",
                        "marker_raw": "[1]",
                    }],
                    references=[{"id": "r1", "ref_number": 1, "raw_entry": "Smith. A paper. 2020."}],
                    citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
                )
                repo.upsert_reference_identity(
                    "r1",
                    identity_key="doi:10.1/a",
                    identity_scheme="doi",
                    canonical_doi="10.1/a",
                    identity_status="exact_identifier",
                )
                repo.upsert_resolve_result("r1", {"status": "resolved", "fulltext_exists": "unknown"})
                repo.create_task(
                    task_id="t1",
                    slot="verify",
                    ref_id="r1",
                    claim_id="c1",
                    scope="abstract_only",
                    task_payload=_claim_evidence_task_payload(repo),
                )

                repo.replace_parse_payload(
                    manuscript_text="New complete manuscript text.",
                    claims=[{
                        "id": "c2",
                        "sentence": "New claim [2].",
                        "context_window": "New manuscript context.",
                    }],
                    references=[{"id": "r2", "ref_number": 2, "raw_entry": "Jones. Another paper. 2021."}],
                    citations=[{"claim_id": "c2", "ref_id": "r2", "ref_number": 2}],
                )

                self.assertEqual([c.claim_id for c in repo.list_claims()], ["c2"])
                self.assertEqual(
                    repo.get_manuscript_text(), "New complete manuscript text."
                )
                self.assertEqual([r.ref_id for r in repo.list_references()], ["r2"])
                self.assertEqual(repo.get_resolve_result("r1"), None)
                task_count = repo._conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
                self.assertEqual(task_count, 0)
            finally:
                repo.close()
