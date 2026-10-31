#!/usr/bin/env python3
# tests/test_content_store.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from tests._bootstrap import *  # noqa: F401,F403

import sys
import sqlite3
import hashlib
import subprocess
import threading
from pathlib import Path
from unittest import mock


class TestContentStore(unittest.TestCase):
    REF = {
        "id": "ref-1",
        "ref_number": 1,
        "raw_entry": "Smith J. Example article. Journal. 2020.",
        "title": "Example article",
        "doi": "10.1000/example",
        "pmid": None,
        "isbn": None,
        "url": None,
        "year": 2020,
        "ay_surname": "smith",
        "ay_year": 2020,
    }

    def setUp(self):
        self._saved_state_dir = os.environ.get("CITATION_VERIFIER_STATE_DIR")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CITATION_VERIFIER_STATE_DIR"] = os.path.join(self.tmp.name, "state")

    def tearDown(self):
        if self._saved_state_dir is None:
            os.environ.pop("CITATION_VERIFIER_STATE_DIR", None)
        else:
            os.environ["CITATION_VERIFIER_STATE_DIR"] = self._saved_state_dir
        self.tmp.cleanup()

    def _make_run(self, name: str) -> str:
        from core.infra.db import RunRepository
        run_dir = os.path.join(self.tmp.name, "runs", name)
        repo = RunRepository.create(
            run_dir,
            run_id=name,
            input_path="C:\\paper.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style=None,
            model_id=None,
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-1",
        )
        repo.replace_parse_payload(claims=[], references=[dict(self.REF)], citations=[])
        repo.close()
        return run_dir

    def test_default_storage_root_survives_content_store_relocation(self):
        from core.fetch.storage import content_store

        root = content_store.storage_root(environ={"UNRELATED_SETTING": "1"})

        self.assertEqual(root, os.path.join(ROOT, "core", "storage"))

    def test_frozen_default_storage_uses_writable_user_data_root(self):
        from core.fetch.storage import content_store
        from core.app import runtime_paths

        bundle_root = os.path.join(self.tmp.name, "readonly-bundle")
        user_data_root = os.path.join(self.tmp.name, "user-data")
        default_environment = {"UNRELATED_SETTING": "1"}
        with mock.patch.object(runtime_paths, "is_frozen", return_value=True), \
             mock.patch.object(runtime_paths, "user_data_root", return_value=Path(user_data_root)):
            root = content_store.storage_root(environ=default_environment)
            self.assertEqual(content_store.storage_root("", environ=default_environment), root)
            self.assertEqual(
                content_store.storage_root(environ={content_store.ENV_STATE_DIR: user_data_root}),
                os.path.join(user_data_root, content_store.STORE_SUBDIR),
            )
            self.assertEqual(
                content_store.storage_root(os.path.join(self.tmp.name, "runs", "frozen-run"), environ=default_environment),
                os.path.join(self.tmp.name, "storage"),
            )

        self.assertEqual(root, os.path.join(user_data_root, "storage"))
        self.assertNotEqual(os.path.commonpath((bundle_root, root)), bundle_root)
        self.assertEqual(
            content_store.storage_root(environ={content_store.ENV_STATE_DIR: user_data_root}),
            os.path.join(user_data_root, content_store.STORE_SUBDIR),
        )
        run_dir = os.path.join(self.tmp.name, "runs", "frozen-run")
        self.assertEqual(
            content_store.storage_root(run_dir, environ=default_environment),
            os.path.join(self.tmp.name, "storage"),
        )

    @staticmethod
    def _store_snapshot(root: str) -> dict:
        out = {}
        for name in (
            "content.sqlite",
            "content.sqlite-wal",
            "content.sqlite-shm",
            "content.sqlite-journal",
        ):
            path = os.path.join(root, name)
            if os.path.exists(path):
                stat = os.stat(path)
                with open(path, "rb") as handle:
                    out[name] = (hashlib.sha256(handle.read()).hexdigest(), stat.st_size, stat.st_mtime_ns)
        return out

    def test_store_text_populates_global_parsed_cache(self):
        from core.fetch.storage import content_store
        from core.resolve import sources

        run1 = self._make_run("run-1")
        sources.store_text(
            run1,
            dict(self.REF),
            "fulltext",
            "user",
            "Example article full text.",
            supplied_by="user",
            supplied_via="user_txt",
            file_format="txt",
        )

        snap = content_store.debug_snapshot(run1)
        self.assertEqual(len(snap["tables"]["works"]), 1)
        self.assertEqual(len(snap["tables"]["parsed_texts"]), 1)
        rel = snap["tables"]["parsed_texts"][0]["stored_relpath"]
        self.assertTrue(os.path.exists(os.path.join(snap["storage_root"], rel)))

        run2 = self._make_run("run-2")
        cached = content_store.find_reusable_parsed_text(run2, dict(self.REF), tiers=("fulltext",))
        self.assertIsNotNone(cached)
        self.assertEqual(cached["tier"], "fulltext")
        self.assertEqual(cached["supplied_by"], "user")
        self.assertEqual(cached["document_relation"]["decision"], "inconclusive")

    def test_authenticated_agent_source_is_not_labelled_as_generic_script(self):
        from core.fetch.storage import content_store
        from core.resolve import sources

        run = self._make_run("run-agent-source")
        sources.store_text(
            run,
            dict(self.REF),
            "fulltext",
            "webfetch",
            "Agent-recovered full text.",
            supplied_by="agent:codex",
            supplied_via="controlled_task_answer:answer-1",
            file_format="txt",
        )
        row = content_store.debug_snapshot(run)["tables"]["parsed_texts"][0]
        self.assertEqual(row["supplied_by"], "agent:codex")
        self.assertEqual(row["acquisition_mode"], "agent_supplied")
        self.assertEqual(
            row["supplied_via"], "controlled_task_answer:answer-1"
        )

    def test_missing_cached_file_is_invalidated(self):
        from core.fetch.storage import content_store
        from core.resolve import sources

        run1 = self._make_run("run-missing-a")
        run2 = self._make_run("run-missing-b")
        sources.store_text(run1, dict(self.REF), "abstract", "crossref", "An abstract.")
        cached = content_store.find_reusable_parsed_text(run2, dict(self.REF), tiers=("abstract",))
        self.assertIsNotNone(cached)
        os.remove(cached["stored_path"])

        miss = content_store.find_reusable_parsed_text(run2, dict(self.REF), tiers=("abstract",))
        self.assertIsNone(miss)
        snap = content_store.debug_snapshot(run2)
        row = snap["tables"]["parsed_texts"][0]
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["missing"], 1)

    def test_v4_flag_relations_replace_and_retain_only_string_flags(self):
        from core.fetch.storage import content_store

        run = self._make_run("run-v4-flags")
        first = content_store.record_parsed_text(
            run, dict(self.REF), "abstract", "crossref", "An abstract.",
            extraction_flags=[" first ", "second"],
            preparation={"preparation_flags": ["prepared"]},
        )
        self.assertIsNotNone(first)
        content_store.record_parsed_text(
            run, dict(self.REF), "abstract", "crossref", "An abstract.",
            extraction_flags=["replacement"],
            preparation={"preparation_flags": []},
        )
        cached = content_store.find_reusable_parsed_text(
            run, dict(self.REF), tiers=("abstract",)
        )
        self.assertEqual(cached["extraction_flags"], ["replacement"])
        self.assertEqual(cached["preparation_flags"], ["prepared"])
        with self.assertRaises(ValueError):
            content_store.record_parsed_text(
                run, dict(self.REF), "abstract", "crossref", "Other abstract.",
                extraction_flags=[1],
            )
        with self.assertRaises(ValueError):
            content_store.record_parsed_text(
                run, dict(self.REF), "abstract", "crossref", "Other abstract.",
                preparation={"preparation_flags": [""]},
            )
        conn = sqlite3.connect(content_store.db_path(run))
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(parsed_texts)")}
        finally:
            conn.close()
        self.assertNotIn("extraction_flags_json", columns)
        self.assertNotIn("preparation_flags_json", columns)

    def test_sources_forwards_extraction_flags_to_shared_store(self):
        from core.fetch.storage import content_store
        from core.resolve import sources

        run = self._make_run("run-source-flags")
        sources.store_text(
            run, dict(self.REF), "abstract", "crossref", "An abstract.",
            extraction_flags=["repeated_line"], extraction_method="fixture",
        )
        cached = content_store.find_reusable_parsed_text(
            run, dict(self.REF), tiers=("abstract",)
        )
        self.assertEqual(cached["extraction_flags"], ["repeated_line"])
        self.assertEqual(cached["extraction_method"], "fixture")

    def test_main_rejects_invalid_store_before_creating_new_run(self):
        from core.fetch.storage import content_store

        future_run = os.path.join(self.tmp.name, "runs", "future-main")
        selected = content_store.preflight(future_run)
        self.assertEqual(selected, content_store.db_path(future_run))
        self.assertFalse(os.path.exists(future_run))
        conn = sqlite3.connect(selected)
        try:
            conn.execute(
                "UPDATE meta SET value = '3' WHERE key = 'schema_version'"
            )
            conn.commit()
            conn.execute("PRAGMA journal_mode = DELETE")
        finally:
            conn.close()

        manuscript = os.path.join(self.tmp.name, "manuscript.txt")
        with open(manuscript, "w", encoding="utf-8") as handle:
            handle.write("A manuscript that must not be parsed.\n")

        root = content_store.storage_root(future_run)
        before = self._store_snapshot(root)
        env = os.environ.copy()
        env["CITATION_VERIFIER_INTEGRITY_SOCKET"] = os.path.join(
            self.tmp.name, "authority-not-needed.sock"
        )
        for name in (
            "CITATION_VERIFIER_MAILTO",
            "GOOGLE_BOOKS_API_KEY",
            "CORE_API_KEY",
            "CITATION_VERIFIER_SIGNING_KEY",
            "CITATION_VERIFIER_SIGNING_KEY_FILE",
        ):
            env[name] = ""
        completed = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "run.py"),
                "--input",
                manuscript,
                "--run",
                future_run,
                "--accuracy",
                "maximum",
                "--proceed",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertIn("content store preflight failed", completed.stderr)
        self.assertEqual(self._store_snapshot(root), before)
        self.assertFalse(os.path.exists(future_run))

    def test_concurrent_fresh_bootstrap_is_serialized(self):
        from core.fetch.storage import content_store
        state = os.path.join(self.tmp.name, "concurrent-state")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def open_store():
            try:
                barrier.wait()
                conn = content_store._connect(environ={"CITATION_VERIFIER_STATE_DIR": state})
                results.append(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
                conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=open_store) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, ["4", "4"])
        conn = sqlite3.connect(content_store.db_path(environ={"CITATION_VERIFIER_STATE_DIR": state}))
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertTrue(content_store._v4_schema_is_valid(conn))
        finally:
            conn.close()

    def test_non_posix_concurrent_connect_waits_for_fresh_bootstrap(self):
        from core.fetch.storage import content_store

        state = os.path.join(self.tmp.name, "non-posix-concurrent-state")
        bootstrap_started = threading.Event()
        release_bootstrap = threading.Event()
        early_schema_probe = threading.Event()
        results = []
        errors = []
        original_bootstrap = content_store._bootstrap_schema
        original_probe = content_store._probe_existing_schema_version

        def delayed_bootstrap(conn):
            bootstrap_started.set()
            self.assertTrue(release_bootstrap.wait(timeout=2))
            original_bootstrap(conn)

        def tracked_probe(path):
            if not release_bootstrap.is_set():
                early_schema_probe.set()
            return original_probe(path)

        def open_store():
            try:
                conn = content_store._connect(
                    environ={"CITATION_VERIFIER_STATE_DIR": state}
                )
                results.append(
                    conn.execute(
                        "SELECT value FROM meta WHERE key='schema_version'"
                    ).fetchone()[0]
                )
                conn.close()
            except Exception as exc:
                errors.append(exc)

        with (
            mock.patch.dict(sys.modules, {"fcntl": None}),
            mock.patch.object(content_store, "_bootstrap_schema", delayed_bootstrap),
            mock.patch.object(content_store, "_probe_existing_schema_version", tracked_probe),
        ):
            first = threading.Thread(target=open_store)
            second = threading.Thread(target=open_store)
            first.start()
            self.assertTrue(bootstrap_started.wait(timeout=2))
            second.start()
            self.assertFalse(early_schema_probe.wait(timeout=0.2))
            release_bootstrap.set()
            first.join()
            second.join()

        self.assertEqual(errors, [])
        self.assertEqual(results, ["4", "4"])

    def test_external_cache_allows_fresh_bootstrap_but_other_siblings_do_not(self):
        from core.fetch.storage import content_store

        catalog_project = os.path.join(self.tmp.name, "catalog-project")
        catalog_run = os.path.join(catalog_project, "runs", "run-1")
        catalog_root = os.path.join(catalog_project, "storage")
        journal_root = os.path.join(catalog_root, "journal-authority")
        os.makedirs(journal_root)
        catalog_contents = {}
        for name in (
            "resolver-coverage.sqlite",
            "resolver-coverage.sqlite-wal",
            "resolver-coverage.sqlite-shm",
        ):
            path = os.path.join(catalog_root, name)
            catalog_contents[name] = name.encode("utf-8")
            with open(path, "wb") as handle:
                handle.write(catalog_contents[name])
        journal_path = os.path.join(journal_root, "nlm-journals.sqlite")
        journal_contents = b"journal authority catalog"
        with open(journal_path, "wb") as handle:
            handle.write(journal_contents)

        conn = content_store._connect(catalog_run, environ={"UNRELATED": "1"})
        conn.close()
        self.assertTrue(os.path.exists(os.path.join(catalog_root, "content.sqlite")))
        for name, contents in catalog_contents.items():
            with open(os.path.join(catalog_root, name), "rb") as handle:
                self.assertEqual(handle.read(), contents)
        with open(journal_path, "rb") as handle:
            self.assertEqual(handle.read(), journal_contents)

        state = os.path.join(self.tmp.name, "external-cache-state")
        external_cache = os.path.join(state, "content_store", "external_cache")
        os.makedirs(external_cache)
        watch_path = os.path.join(external_cache, "retraction_watch.csv")
        watch_contents = b"doi,status\n10.1000/example,clear\n"
        with open(watch_path, "wb") as handle:
            handle.write(watch_contents)

        conn = content_store._connect(environ={"CITATION_VERIFIER_STATE_DIR": state})
        try:
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
                "4",
            )
            self.assertTrue(content_store._v4_schema_is_valid(conn))
        finally:
            conn.close()
        with open(watch_path, "rb") as handle:
            self.assertEqual(handle.read(), watch_contents)

        named_file_state = os.path.join(self.tmp.name, "external-cache-file-state")
        named_file_path = os.path.join(
            named_file_state, "content_store", "external_cache"
        )
        os.makedirs(os.path.dirname(named_file_path))
        with open(named_file_path, "wb") as handle:
            handle.write(b"not a directory")

        with self.assertRaises(ValueError):
            content_store._connect(
                environ={"CITATION_VERIFIER_STATE_DIR": named_file_state}
            )
        with open(named_file_path, "rb") as handle:
            self.assertEqual(handle.read(), b"not a directory")

        unexpected_state = os.path.join(self.tmp.name, "unexpected-sibling-state")
        unexpected_path = os.path.join(unexpected_state, "content_store", "unexpected")
        os.makedirs(unexpected_path)
        marker_path = os.path.join(unexpected_path, "marker")
        with open(marker_path, "wb") as handle:
            handle.write(b"unchanged")

        with self.assertRaises(ValueError):
            content_store._connect(
                environ={"CITATION_VERIFIER_STATE_DIR": unexpected_state}
            )
        self.assertFalse(
            os.path.exists(content_store.db_path(environ={"CITATION_VERIFIER_STATE_DIR": unexpected_state}))
        )
        with open(marker_path, "rb") as handle:
            self.assertEqual(handle.read(), b"unchanged")

    def test_existing_invalid_store_normal_open_does_not_mutate(self):
        from core.fetch.storage import content_store

        for kind in ("future", "missing-meta", "mutilated", "weakened-check"):
            with self.subTest(kind=kind):
                state = os.path.join(self.tmp.name, kind)
                root = os.path.join(state, "content_store")
                conn = content_store._connect(environ={"CITATION_VERIFIER_STATE_DIR": state})
                if kind == "future":
                    conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
                elif kind == "missing-meta":
                    conn.execute("DELETE FROM meta WHERE key='schema_version'")
                elif kind == "mutilated":
                    conn.execute("DROP TABLE parsed_text_preparation_flags")
                else:
                    conn.execute("DROP TABLE parsed_text_extraction_flags")
                    conn.execute(
                        """
                        CREATE TABLE parsed_text_extraction_flags (
                          parsed_text_id TEXT NOT NULL
                            REFERENCES parsed_texts(parsed_text_id) ON DELETE CASCADE,
                          flag TEXT NOT NULL,
                          PRIMARY KEY (parsed_text_id, flag)
                        ) WITHOUT ROWID
                        """
                    )
                conn.commit()
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.close()
                before = self._store_snapshot(root)
                with self.assertRaises(ValueError):
                    content_store._connect(environ={"CITATION_VERIFIER_STATE_DIR": state})
                self.assertEqual(self._store_snapshot(root), before)

    def test_quality_metadata_round_trips_and_noncurrent_fulltext_is_not_reused(self):
        from core.fetch.storage import content_store
        from core.parse.source_text import PREPARATION_VERSION

        run1 = self._make_run("run-quality-a")
        run2 = self._make_run("run-quality-b")
        preparation = {
            "preparation_version": PREPARATION_VERSION,
            "preparation_flags": ["bibliography_removal_reverted"],
            "before_bibliography_char_count": 21_802,
            "prepared_char_count": 21_802,
        }
        prepared_text = (
            "Example article\nSmith J\n2020\n\n"
            + ("Prepared source text " * 200)
        )
        content_store.record_parsed_text(
            run1, dict(self.REF), "fulltext", "webfetch", prepared_text,
            source_ref="https://example.org/paper.pdf", mapping="tokens", signal="doi",
            score=1.0, extraction_method="pymupdf",
            extraction_flags=["repeated_line"], preparation=preparation,
        )
        cached = content_store.find_reusable_parsed_text(run2, dict(self.REF), tiers=("fulltext",))
        self.assertIsNotNone(cached)
        self.assertEqual(cached["extraction_method"], "pymupdf")
        self.assertEqual(cached["extraction_flags"], ["repeated_line"])
        self.assertEqual(cached["preparation_version"], PREPARATION_VERSION)
        self.assertEqual(cached["preparation_flags"], ["bibliography_removal_reverted"])
        self.assertEqual(cached["preparation_before_chars"], 21_802)
        self.assertEqual(cached["preparation_after_chars"], 21_802)

        # An explicitly versioned older preparation must force a fresh fetch.
        outdated_ref = {
            **self.REF,
            "doi": "10.1000/outdated",
            "title": "Outdated example article",
            "raw_entry": "Jones J. Outdated example article. Journal. 2021.",
            "year": 2021,
            "ay_surname": "jones",
            "ay_year": 2021,
        }
        outdated = content_store.record_parsed_text(
            run1, outdated_ref, "fulltext", "webfetch",
            "Outdated example article\nJones J\n2021\n\n" + "Outdated prepared text " * 200,
            source_ref="https://example.org/outdated.pdf",
            mapping="tokens", signal="doi", score=1.0,
            preparation={"preparation_version": "source_prepare_v1"},
        )
        self.assertIsNotNone(outdated)
        miss = content_store.find_reusable_parsed_text(
            run2, outdated_ref, tiers=("fulltext",)
        )
        self.assertIsNone(miss)

        # Non-user full text without the current preparation marker is never
        # reusable, even when its content would otherwise pass quality checks.
        unprepared_ref = {
            **self.REF,
            "doi": "10.1000/unprepared",
            "title": "Unprepared example article",
            "raw_entry": "Jones J. Unprepared example article. Journal. 2022.",
            "year": 2022,
            "ay_surname": "jones",
            "ay_year": 2022,
        }
        content_store.record_parsed_text(
            run1,
            unprepared_ref,
            "fulltext",
            "webfetch",
            "Unprepared example article\nJones J\n2022\n\n" + "Unprepared source text " * 200,
            source_ref="https://example.org/unprepared.pdf",
            mapping="tokens",
            signal="doi",
            score=1.0,
        )
        self.assertIsNone(content_store.find_reusable_parsed_text(
            run2, unprepared_ref, tiers=("fulltext",)
        ))

    def test_quality_unavailable_keeps_row_active_while_bad_quality_deactivates(self):
        from core.fetch.storage import content_store

        bad_run_a = self._make_run("run-quality-bad-a")
        bad_run_b = self._make_run("run-quality-bad-b")
        bad_entry = content_store.record_parsed_text(
            bad_run_a,
            dict(self.REF),
            "fulltext",
            "webfetch",
            "Prepared source text " * 200,
            source_ref="https://example.org/bad.pdf",
            mapping="tokens",
            signal="doi",
            score=1.0,
        )
        with (
            mock.patch.object(
                content_store,
                "_cached_fulltext_quality_status",
                return_value="bad_quality",
            ),
            mock.patch.object(
                content_store,
                "_reusable_fulltext_identity_ok",
                return_value=True,
            ),
        ):
            self.assertIsNone(
                content_store.find_reusable_parsed_text(
                    bad_run_b, dict(self.REF), tiers=("fulltext",)
                )
            )
        bad_rows = content_store.debug_snapshot(bad_run_b)["tables"]["parsed_texts"]
        bad_row = next(
            row for row in bad_rows
            if row["parsed_text_id"] == bad_entry["parsed_text_id"]
        )
        self.assertEqual(bad_row["active"], 0)
        self.assertEqual(bad_row["missing"], 0)

        unavailable_ref = {
            **self.REF,
            "doi": "10.1000/unavailable",
            "title": "Unavailable quality article",
            "raw_entry": "Jones J. Unavailable quality article. Journal. 2023.",
            "year": 2023,
            "ay_surname": "jones",
            "ay_year": 2023,
        }
        unavailable_run_a = self._make_run("run-quality-unavailable-a")
        unavailable_run_b = self._make_run("run-quality-unavailable-b")
        unavailable_entry = content_store.record_parsed_text(
            unavailable_run_a,
            unavailable_ref,
            "fulltext",
            "webfetch",
            "Prepared source text " * 200,
            source_ref="https://example.org/unavailable.pdf",
            mapping="tokens",
            signal="doi",
            score=1.0,
        )
        with (
            mock.patch.object(
                content_store,
                "_cached_fulltext_quality_status",
                return_value="unavailable",
            ),
            mock.patch.object(
                content_store,
                "_reusable_fulltext_identity_ok",
                return_value=True,
            ),
        ):
            self.assertIsNone(
                content_store.find_reusable_parsed_text(
                    unavailable_run_b, unavailable_ref, tiers=("fulltext",)
                )
            )
        unavailable_rows = content_store.debug_snapshot(
            unavailable_run_b
        )["tables"]["parsed_texts"]
        unavailable_row = next(
            row for row in unavailable_rows
            if row["parsed_text_id"] == unavailable_entry["parsed_text_id"]
        )
        self.assertEqual(unavailable_row["active"], 1)
        self.assertEqual(unavailable_row["missing"], 0)

    def test_nonuser_metadata_and_supplement_fulltexts_are_not_reused(self):
        from core.fetch.storage import content_store
        from core.parse.source_text import PREPARATION_VERSION

        blocked_sources = {
            "wayback-arxiv": (
                "https://web.archive.org/web/20230101000000id_/"
                "https://arxiv.org/abs/2205.14061"
            ),
            "wayback-pubmed": (
                "https://web.archive.org/web/20230101000000id_/"
                "https://pubmed.ncbi.nlm.nih.gov/12345678/"
            ),
            "wayback-ssrn": (
                "https://web.archive.org/web/20230101000000id_/"
                "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1234567"
            ),
            "acs-supplement": (
                "https://pubs.acs.org/nalefd/article-supplement/1405827/pdf/"
                "nl302665m_si_001/"
            ),
        }
        for index, (label, source_ref) in enumerate(blocked_sources.items(), start=1):
            with self.subTest(label=label):
                ref = {
                    **self.REF,
                    "doi": f"10.1000/blocked-{index}",
                    "title": f"Blocked material example {index}",
                    "raw_entry": f"Smith. Blocked material example {index}. 2020.",
                }
                run_a = self._make_run(f"run-material-{index}-a")
                run_b = self._make_run(f"run-material-{index}-b")
                entry = content_store.record_parsed_text(
                    run_a,
                    ref,
                    "fulltext",
                    "webfetch",
                    f"Blocked material example {index}. Smith. " + "Prepared body text. " * 200,
                    source_ref=source_ref,
                    mapping="tokens",
                    signal="title",
                    score=1.0,
                    preparation={"preparation_version": PREPARATION_VERSION},
                )
                with (
                    mock.patch.object(
                        content_store, "_reusable_fulltext_identity_ok", return_value=True
                    ),
                    mock.patch.object(
                        content_store, "_cached_fulltext_quality_status", return_value="ok"
                    ),
                ):
                    self.assertIsNone(
                        content_store.find_reusable_parsed_text(
                            run_b, ref, tiers=("fulltext",)
                        )
                    )
                row = next(
                    item
                    for item in content_store.debug_snapshot(run_b)["tables"]["parsed_texts"]
                    if item["parsed_text_id"] == entry["parsed_text_id"]
                )
                self.assertEqual((row["active"], row["missing"]), (0, 0))

    def test_nonuser_archived_article_body_remains_reusable(self):
        from core.fetch.storage import content_store
        from core.parse.source_text import PREPARATION_VERSION

        ref = {
            **self.REF,
            "doi": "10.1000/archived-article",
            "title": "Archived article example",
            "raw_entry": "Smith. Archived article example. 2020.",
        }
        run_a = self._make_run("run-archived-article-a")
        run_b = self._make_run("run-archived-article-b")
        entry = content_store.record_parsed_text(
            run_a,
            ref,
            "fulltext",
            "webfetch",
            "Archived article example. Smith. " + "Prepared article body. " * 200,
            source_ref=(
                "https://web.archive.org/web/20230101000000id_/"
                "https://publisher.example/articles/confirmed-study"
            ),
            mapping="tokens",
            signal="title",
            score=1.0,
            preparation={"preparation_version": PREPARATION_VERSION},
        )
        with (
            mock.patch.object(
                content_store, "_reusable_fulltext_identity_ok", return_value=True
            ),
            mock.patch.object(
                content_store, "_cached_fulltext_quality_status", return_value="ok"
            ),
        ):
            cached = content_store.find_reusable_parsed_text(
                run_b, ref, tiers=("fulltext",)
            )
        self.assertEqual(cached["parsed_text_id"], entry["parsed_text_id"])

    def test_nonuser_repository_cover_sheet_is_deactivated_on_reuse(self):
        from core.fetch.storage import content_store
        from core.parse.source_text import PREPARATION_VERSION

        run_a = self._make_run("run-cover-sheet-a")
        run_b = self._make_run("run-cover-sheet-b")
        text = (
            "Example article\nCitation for published version (APA).\n"
            "Document status and date: Published.\n"
            "Please check the document version of this publication.\n"
            + "Repository policy and copyright information. " * 70
            + "\nGeneral rights\nTake down policy\nDownload date: 27 Aug. 2026\n"
        )
        entry = content_store.record_parsed_text(
            run_a,
            dict(self.REF),
            "fulltext",
            "webfetch",
            text,
            source_ref="https://repository.example/cover.pdf",
            mapping="tokens",
            signal="title",
            score=1.0,
            preparation={"preparation_version": PREPARATION_VERSION},
        )
        with (
            mock.patch.object(
                content_store, "_reusable_fulltext_identity_ok", return_value=True
            ),
            mock.patch.object(
                content_store, "_cached_fulltext_quality_status", return_value="ok"
            ),
        ):
            self.assertIsNone(
                content_store.find_reusable_parsed_text(
                    run_b, dict(self.REF), tiers=("fulltext",)
                )
            )
        row = next(
            item
            for item in content_store.debug_snapshot(run_b)["tables"]["parsed_texts"]
            if item["parsed_text_id"] == entry["parsed_text_id"]
        )
        self.assertEqual((row["active"], row["missing"]), (0, 0))

    def test_user_supplied_book_review_is_deactivated_by_document_relation(self):
        from core.fetch.storage import content_store

        run_a = self._make_run("run-user-review-a")
        run_b = self._make_run("run-user-review-b")
        entry = content_store.record_parsed_text(
            run_a,
            dict(self.REF),
            "fulltext",
            "user",
            "Book reviews\n" + "A review of the cited book. " * 200,
            supplied_by="user",
            supplied_via="user_txt",
            file_format="txt",
        )
        self.assertIsNone(content_store.find_reusable_parsed_text(
            run_b, dict(self.REF), tiers=("fulltext",)
        ))
        row = next(
            item
            for item in content_store.debug_snapshot(run_b)["tables"]["parsed_texts"]
            if item["parsed_text_id"] == entry["parsed_text_id"]
        )
        self.assertEqual((row["active"], row["missing"]), (0, 0))

    def test_supplementary_information_is_deactivated_by_document_relation(self):
        from core.fetch.storage import content_store

        run_a = self._make_run("run-supplement-a")
        run_b = self._make_run("run-supplement-b")
        entry = content_store.record_parsed_text(
            run_a,
            dict(self.REF),
            "fulltext",
            "user",
            (
                "SUPPLEMENTARY INFORMATION\n"
                "Supplementary Information for\n"
                "Example article\n"
                + "Additional methods and tables are reported here. " * 80
            ),
            supplied_by="user",
            supplied_via="user_txt",
            file_format="txt",
        )
        self.assertIsNone(content_store.find_reusable_parsed_text(
            run_b, dict(self.REF), tiers=("fulltext",)
        ))
        row = next(
            item
            for item in content_store.debug_snapshot(run_b)["tables"]["parsed_texts"]
            if item["parsed_text_id"] == entry["parsed_text_id"]
        )
        self.assertEqual((row["active"], row["missing"]), (0, 0))

    def test_run_specific_resolve_conflict_blocks_reuse_without_deactivating_source(self):
        from core.fetch.storage import content_store
        from core.parse.source_text import PREPARATION_VERSION

        ref = {
            **self.REF,
            "doi": "10.1000/resolve-conflict",
            "title": "Resolve conflict example",
            "raw_entry": "Smith. Resolve conflict example. 2020.",
        }
        run_a = self._make_run("run-resolve-conflict-a")
        run_b = self._make_run("run-resolve-conflict-b")
        entry = content_store.record_parsed_text(
            run_a,
            ref,
            "fulltext",
            "webfetch",
            "Resolve conflict example. Smith. " + "Prepared article body. " * 200,
            source_ref="https://publisher.example/correct-paper.pdf",
            mapping="tokens",
            signal="title",
            score=1.0,
            preparation={"preparation_version": PREPARATION_VERSION},
        )
        resolve_result = {
            "status": "unverified",
            "via": "crossref_metadata",
            "evidence_profile": {
                "metadata_match": {
                    "metadata_conflict": True,
                    "hard_conflicts": ["author", "year"],
                }
            },
        }
        with (
            mock.patch.object(
                content_store, "_reusable_fulltext_identity_ok", return_value=False
            ),
            mock.patch.object(
                content_store, "_cached_fulltext_quality_status", return_value="ok"
            ),
        ):
            self.assertIsNone(
                content_store.find_reusable_parsed_text(
                    run_b,
                    ref,
                    tiers=("fulltext",),
                    resolve_result=resolve_result,
                )
            )
        row = next(
            item
            for item in content_store.debug_snapshot(run_b)["tables"]["parsed_texts"]
            if item["parsed_text_id"] == entry["parsed_text_id"]
        )
        self.assertEqual((row["active"], row["missing"]), (1, 0))

    def test_resolve_conflict_allows_cache_when_citation_itself_confirms_document(self):
        from core.fetch.storage import content_store
        from core.parse.source_text import PREPARATION_VERSION

        ref = {
            **self.REF,
            "doi": None,
            "title": "Adam: A Method for Stochastic Optimization",
            "raw_entry": (
                "Diederik P. Kingma and Jimmy Ba. Adam: A Method for Stochastic "
                "Optimization. 2014."
            ),
            "year": 2014,
            "ay_surname": "kingma",
            "ay_year": 2014,
        }
        run_a = self._make_run("run-resolve-citation-a")
        run_b = self._make_run("run-resolve-citation-b")
        text = (
            "Adam: A Method for Stochastic Optimization\n"
            "Diederik P. Kingma and Jimmy Ba\n2014\nAbstract\n"
            + "We introduce an optimization method for stochastic objectives. " * 100
        )
        entry = content_store.record_parsed_text(
            run_a,
            ref,
            "fulltext",
            "webfetch",
            text,
            source_ref="https://repository.example/1412.6980v5.pdf",
            mapping="tokens",
            signal="title",
            score=1.0,
            preparation={"preparation_version": PREPARATION_VERSION},
        )
        resolve_result = {
            "status": "unverified",
            "evidence_profile": {
                "metadata_match": {"hard_conflicts": ["author", "year"]}
            },
        }

        cached = content_store.find_reusable_parsed_text(
            run_b,
            ref,
            tiers=("fulltext",),
            resolve_result=resolve_result,
        )

        self.assertEqual(cached["parsed_text_id"], entry["parsed_text_id"])

    def test_resolve_conflict_does_not_override_exact_cited_url_provenance(self):
        from core.fetch.storage import content_store
        from core.parse.source_text import PREPARATION_VERSION

        cited_url = "https://example.org/articles/exact-cited-page"
        ref = {
            **self.REF,
            "doi": None,
            "url": cited_url,
            "title": "Exact cited web article",
            "raw_entry": f"Smith. Exact cited web article. 2024. {cited_url}",
        }
        self.assertFalse(
            content_store._source_ref_matches_strong_identifier(
                ref, cited_url, "tokens", None
            )
        )
        self.assertTrue(
            content_store._source_ref_matches_strong_identifier(
                ref, cited_url + "/", "cited_url", "cited_url_reachable"
            )
        )
        self.assertFalse(
            content_store._source_ref_matches_strong_identifier(
                ref, cited_url, "cited_url", "cited_url_unconfirmed"
            )
        )
        run_a = self._make_run("run-cited-url-a")
        run_b = self._make_run("run-cited-url-b")
        entry = content_store.record_parsed_text(
            run_a,
            ref,
            "fulltext",
            "webfetch",
            "Substantive text from the exact cited page. " * 120,
            source_ref=cited_url + "/",
            mapping="cited_url",
            signal="url",
            score=1.0,
            identity_status="cited_url_reachable",
            preparation={"preparation_version": PREPARATION_VERSION},
        )
        resolve_result = {
            "status": "unverified",
            "evidence_profile": {
                "metadata_match": {"hard_conflicts": ["author", "year"]}
            },
        }

        cached = content_store.find_reusable_parsed_text(
            run_b,
            ref,
            tiers=("fulltext",),
            resolve_result=resolve_result,
        )

        self.assertEqual(cached["parsed_text_id"], entry["parsed_text_id"])

    def test_official_report_catalogue_identity_is_reusable_without_live_context(self):
        from core.fetch.storage import content_store

        ref = {
            "title": "Improving language understanding with unsupervised learning",
            "raw_entry": (
                "Alec Radford. Improving language understanding with unsupervised "
                "learning. Technical Report, 2018."
            ),
            "year": 2018,
            "source_kind": "report_like",
        }
        source_ref = (
            "https://cdn.openai.com/research-covers/language-unsupervised/"
            "language_understanding_paper.pdf"
        )
        path = os.path.join(self.tmp.name, "openai-report.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "Improving Language Understanding by Generative Pre-Training\n"
                "Alec Radford\nAbstract\n" + "Substantive report body. " * 100
            )

        self.assertTrue(
            content_store._reusable_fulltext_identity_ok(ref, path, source_ref)
        )

    def test_externally_corroborated_cache_retains_capped_identity_contract(self):
        from core.fetch.storage import content_store
        from core.resolve import sources

        path = os.path.join(self.tmp.name, "institutional-award.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Official institutional award text. " * 100)
        ref = {
            "title": "ARB/01/8, Award, 12 May",
            "raw_entry": "CMS Gas Transmission v Argentina, ARB/01/8, Award, 12 May.",
            "ay_surname": "cms",
        }
        inconclusive = {
            "ok": False,
            "decision": "inconclusive",
            "title_overlap": 1.0,
            "author_ok": True,
        }
        kwargs = {
            "mapping": "web",
            "identity_status": "externally_corroborated_text",
            "match_signal": "title",
            "match_score": 1.0,
        }

        with mock.patch.object(
            sources, "document_identity_probe", return_value=inconclusive
        ):
            self.assertTrue(
                content_store._reusable_fulltext_identity_ok(
                    ref,
                    path,
                    "https://official.example/award.pdf",
                    **kwargs,
                )
            )
            self.assertFalse(
                content_store._reusable_fulltext_identity_ok(
                    ref,
                    path,
                    "https://official.example/award.pdf",
                    **{**kwargs, "match_score": 0.8},
                )
            )
        with mock.patch.object(
            sources,
            "document_identity_probe",
            return_value={**inconclusive, "decision": "rejected"},
        ):
            self.assertFalse(
                content_store._reusable_fulltext_identity_ok(
                    ref,
                    path,
                    "https://official.example/award.pdf",
                    **kwargs,
                )
            )

    def test_current_preparation_version_is_unknown_when_source_text_unavailable(self):
        from core.fetch.storage import content_store

        with mock.patch.dict(sys.modules, {"core.parse.source_text": None}):
            self.assertIsNone(content_store._current_preparation_version())

    def test_cached_quality_oserror_is_unavailable(self):
        from core.fetch.storage import content_store

        with mock.patch("builtins.open", side_effect=OSError):
            self.assertEqual(
                content_store._cached_fulltext_quality_status("unreadable.txt"),
                "unavailable",
            )

    def test_unknown_preparation_version_blocks_reuse_without_deactivating_row(self):
        from core.fetch.storage import content_store

        run_a = self._make_run("run-prep-unknown-a")
        run_b = self._make_run("run-prep-unknown-b")
        ref = {
            **self.REF,
            "doi": "10.1000/prep-unknown",
            "title": "Preparation unknown article",
            "raw_entry": "Jones J. Preparation unknown article. Journal. 2024.",
            "year": 2024,
            "ay_surname": "jones",
            "ay_year": 2024,
        }
        entry = content_store.record_parsed_text(
            run_a,
            ref,
            "fulltext",
            "webfetch",
            "Prepared source text " * 200,
            source_ref="https://example.org/prep-unknown.pdf",
            mapping="tokens",
            signal="doi",
            score=1.0,
            preparation={"preparation_version": "source_prepare_v1"},
        )
        with (
            mock.patch.object(
                content_store,
                "_current_preparation_version",
                return_value=None,
            ),
            mock.patch.object(
                content_store,
                "_cached_fulltext_quality_status",
                return_value="ok",
            ),
            mock.patch.object(
                content_store,
                "_reusable_fulltext_identity_ok",
                return_value=True,
            ),
        ):
            self.assertIsNone(
                content_store.find_reusable_parsed_text(
                    run_b, ref, tiers=("fulltext",)
                )
            )
        rows = content_store.debug_snapshot(run_b)["tables"]["parsed_texts"]
        row = next(
            row for row in rows if row["parsed_text_id"] == entry["parsed_text_id"]
        )
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["missing"], 0)

    def test_seed_reusable_sources_rehydrates_run_before_fetch(self):
        from core.app.phases import fetch as fetch_phase
        from core.resolve import sources
        from core.infra.db import RunRepository

        run1 = self._make_run("run-seed-a")
        run2 = self._make_run("run-seed-b")
        sources.store_text(run1, dict(self.REF), "fulltext", "user", "Reusable full text.",
                           supplied_by="user", supplied_via="user_txt", file_format="txt")

        checkpointed = []
        seeded = fetch_phase._seed_reusable_sources(
            run2,
            {"ref-1": dict(self.REF)},
            state={
                "run_dir": run2,
                "_integrity_unit_checkpoint": lambda group, unit: checkpointed.append(
                    (group, unit)
                ),
            },
        )
        self.assertEqual(seeded["fulltext"], 1)
        self.assertEqual(checkpointed, [("fetch_cache_seed", "ref-1")])

        repo = RunRepository.open(run2)
        try:
            exists = repo.source_text_exists("ref-1", tier="fulltext")
            completion = repo.list_integrity_unit_completions("fetch_cache_seed")[0]
        finally:
            repo.close()
        self.assertTrue(exists)
        self.assertEqual(completion["payload"]["document_relation"]["decision"], "inconclusive")

    def test_fresh_state_isolates_cold_paper_without_disabling_ordinary_reuse(self):
        from core.app.phases import fetch as fetch_phase
        from core.fetch.storage import content_store
        from core.resolve import sources

        state_a = os.path.join(self.tmp.name, "state-a")
        state_b = os.path.join(self.tmp.name, "state-b")
        run_a = self._make_run("run-cold-source")
        run_b = self._make_run("run-cold-target")

        os.environ["CITATION_VERIFIER_STATE_DIR"] = state_a
        sources.store_text(
            run_a,
            dict(self.REF),
            "fulltext",
            "user",
            "Reusable outside a cold benchmark.",
            supplied_by="user",
            supplied_via="user_txt",
            file_format="txt",
        )
        self.assertIsNotNone(
            content_store.find_reusable_parsed_text(
                run_a, dict(self.REF), tiers=("fulltext",)
            )
        )

        os.environ["CITATION_VERIFIER_STATE_DIR"] = state_b
        seeded = fetch_phase._seed_reusable_sources(
            run_b, {"ref-1": dict(self.REF)}
        )
        self.assertEqual(seeded, {"fulltext": 0, "abstract": 0})

        os.environ["CITATION_VERIFIER_STATE_DIR"] = state_a
        run_c = self._make_run("run-ordinary-target")
        ordinary_seeded = fetch_phase._seed_reusable_sources(
            run_c, {"ref-1": dict(self.REF)}
        )
        self.assertEqual(ordinary_seeded, {"fulltext": 1, "abstract": 0})
        self.assertIsNotNone(
            content_store.find_reusable_parsed_text(
                run_c, dict(self.REF), tiers=("fulltext",)
            )
        )

    def test_provide_map_moves_user_original_into_library(self):
        import argparse
        from core.fetch.storage import content_store
        from core.resolve import provide

        run_dir = self._make_run("run-map")
        src = os.path.join(self.tmp.name, "user.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("Smith wrote Example article in Journal 2020.")

        args = argparse.Namespace(
            run=run_dir,
            refs=None,
            ref="ref-1",
            file=src,
            tier="fulltext",
            origin="user",
            force=True,
        )
        with redirect_stdout(io.StringIO()):
            provide.cmd_map(args)

        self.assertFalse(os.path.exists(src))
        snap = content_store.debug_snapshot(run_dir)
        self.assertEqual(len(snap["tables"]["library_items"]), 1)
        rel = snap["tables"]["library_items"][0]["stored_relpath"]
        self.assertTrue(os.path.exists(os.path.join(snap["storage_root"], rel)))

    def test_archive_user_original_disambiguates_identity_slug_and_basename(self):
        from core.fetch.storage import content_store

        run_dir = self._make_run("run-archive-collision")
        ref_a = {**self.REF, "doi": "10.1000/abcdefghijklmn1111"}
        ref_b = {**self.REF, "id": "ref-2", "doi": "10.1000/abcdefghijklmn2222"}
        self.assertEqual(
            content_store._slug(content_store._primary_identity(ref_a)[1], max_len=24),
            content_store._slug(content_store._primary_identity(ref_b)[1], max_len=24),
        )

        input_a = Path(self.tmp.name, "source-a", "download.pdf")
        input_b = Path(self.tmp.name, "source-b", "download.pdf")
        input_a.parent.mkdir(parents=True)
        input_b.parent.mkdir(parents=True)
        input_a.write_bytes(b"PDF bytes for work A")
        input_b.write_bytes(b"different PDF bytes for work B")

        archived_a = content_store.archive_user_original(
            run_dir, str(input_a), ref=ref_a, supplied_via="test", file_format="pdf", move=False
        )
        archived_b = content_store.archive_user_original(
            run_dir, str(input_b), ref=ref_b, supplied_via="test", file_format="pdf", move=False
        )

        self.assertNotEqual(archived_a["work_id"], archived_b["work_id"])
        self.assertNotEqual(archived_a["stored_relpath"], archived_b["stored_relpath"])
        snapshot = content_store.debug_snapshot(run_dir)
        self.assertEqual(len(snapshot["tables"]["library_items"]), 2)
        for archived, expected in (
            (archived_a, b"PDF bytes for work A"),
            (archived_b, b"different PDF bytes for work B"),
        ):
            stored = Path(snapshot["storage_root"], archived["stored_relpath"])
            self.assertEqual(stored.read_bytes(), expected)
        self.assertEqual(input_a.read_bytes(), b"PDF bytes for work A")
        self.assertEqual(input_b.read_bytes(), b"different PDF bytes for work B")

    def test_archive_user_original_uses_bounded_pair_digest_for_staged_pdf(self):
        from core.fetch.storage import content_store

        run_dir = self._make_run("run-archive-staged-hash-name")
        ref = {
            **self.REF,
            "doi": "10.1000/" + ("long-identifier-" * 32),
        }
        staged_dir = Path(self.tmp.name, "staged")
        staged_dir.mkdir(parents=True)
        pdf_bytes = b"staged PDF bytes for a long DOI"
        source_hash = hashlib.sha256(pdf_bytes).hexdigest()
        source = staged_dir / f"{source_hash}.pdf"
        source.write_bytes(pdf_bytes)

        archived = content_store.archive_user_original(
            run_dir,
            str(source),
            ref=ref,
            supplied_via="test",
            file_format="pdf",
            move=False,
        )

        identity_key = content_store._primary_identity(ref)[1]
        identity_hash = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()
        expected_name = hashlib.sha256(
            f"{identity_hash}{source_hash}".encode("ascii")
        ).hexdigest() + ".pdf"
        stored = Path(archived["stored_path"])
        self.assertEqual(stored.name, expected_name)
        self.assertLessEqual(len(stored.name), 76)
        self.assertEqual(stored.read_bytes(), pdf_bytes)

        other_ref = {
            **ref,
            "id": "ref-2",
            "doi": ref["doi"] + "different-work",
        }
        other_work = content_store.archive_user_original(
            run_dir,
            str(source),
            ref=other_ref,
            supplied_via="test",
            file_format="pdf",
            move=False,
        )
        self.assertNotEqual(archived["work_id"], other_work["work_id"])
        self.assertNotEqual(archived["stored_relpath"], other_work["stored_relpath"])
        self.assertEqual(Path(other_work["stored_path"]).read_bytes(), pdf_bytes)

    def test_archive_user_original_bounds_fallback_extension(self):
        from core.fetch.storage import content_store

        run_dir = self._make_run("run-archive-fallback-extension")
        source_dir = Path(self.tmp.name, "unknown-format")
        source_dir.mkdir(parents=True)
        cases = (
            ("without-suffix", b"content without suffix", ".bin"),
            ("source." + ("x" * 40), b"content with long suffix", "." + ("x" * 11)),
        )

        for filename, data, expected_suffix in cases:
            source = source_dir / filename
            source.write_bytes(data)
            archived = content_store.archive_user_original(
                run_dir,
                str(source),
                ref=dict(self.REF),
                supplied_via="test",
                file_format="unsupported-format",
                move=False,
            )

            stored = Path(archived["stored_path"])
            self.assertTrue(stored.name.endswith(expected_suffix))
            self.assertLessEqual(len(stored.name), 76)

    def test_archive_user_original_keeps_same_identity_and_bytes_idempotent(self):
        from core.fetch.storage import content_store

        run_dir = self._make_run("run-archive-idempotent")
        source = Path(self.tmp.name, "source", "download.pdf")
        source.parent.mkdir(parents=True)
        source.write_bytes(b"same bytes on every archive")
        renamed_source = source.with_name("renamed.pdf")
        renamed_source.write_bytes(source.read_bytes())

        first = content_store.archive_user_original(
            run_dir, str(source), ref=dict(self.REF), supplied_via="test", file_format="pdf", move=False
        )
        second = content_store.archive_user_original(
            run_dir, str(renamed_source), ref=dict(self.REF), supplied_via="test", file_format="pdf", move=False
        )

        self.assertEqual(first["library_item_id"], second["library_item_id"])
        self.assertEqual(first["stored_relpath"], second["stored_relpath"])
        snapshot = content_store.debug_snapshot(run_dir)
        self.assertEqual(len(snapshot["tables"]["library_items"]), 1)
        self.assertEqual(
            Path(snapshot["storage_root"], first["stored_relpath"]).read_bytes(),
            b"same bytes on every archive",
        )

    def test_archive_user_original_keeps_different_bytes_for_same_identity_and_basename(self):
        from core.fetch.storage import content_store

        run_dir = self._make_run("run-archive-same-work-variants")
        input_a = Path(self.tmp.name, "variant-a", "download.pdf")
        input_b = Path(self.tmp.name, "variant-b", "download.pdf")
        input_a.parent.mkdir(parents=True)
        input_b.parent.mkdir(parents=True)
        input_a.write_bytes(b"first version of the PDF")
        input_b.write_bytes(b"second version of the PDF")

        archived_a = content_store.archive_user_original(
            run_dir,
            str(input_a),
            ref=dict(self.REF),
            supplied_via="test",
            file_format="pdf",
            move=False,
        )
        archived_b = content_store.archive_user_original(
            run_dir,
            str(input_b),
            ref=dict(self.REF),
            supplied_via="test",
            file_format="pdf",
            move=False,
        )

        self.assertEqual(archived_a["work_id"], archived_b["work_id"])
        self.assertNotEqual(archived_a["stored_relpath"], archived_b["stored_relpath"])
        snapshot = content_store.debug_snapshot(run_dir)
        self.assertEqual(len(snapshot["tables"]["library_items"]), 2)
        rows_by_path = {
            row["stored_relpath"]: row
            for row in snapshot["tables"]["library_items"]
        }
        for archived, expected in (
            (archived_a, b"first version of the PDF"),
            (archived_b, b"second version of the PDF"),
        ):
            stored = Path(snapshot["storage_root"], archived["stored_relpath"])
            self.assertTrue(stored.name.endswith(".pdf"))
            self.assertLessEqual(len(stored.name), 76)
            self.assertEqual(
                rows_by_path[archived["stored_relpath"]]["original_filename"],
                "download.pdf",
            )
            self.assertEqual(stored.read_bytes(), expected)

    def test_archive_user_original_rejects_existing_destination_with_different_bytes(self):
        from core.fetch.storage import content_store

        run_dir = self._make_run("run-archive-tampered-destination")
        input_a = Path(self.tmp.name, "first", "download.pdf")
        input_b = Path(self.tmp.name, "second", "download.pdf")
        input_a.parent.mkdir(parents=True)
        input_b.parent.mkdir(parents=True)
        input_a.write_bytes(b"verified original bytes")
        input_b.write_bytes(b"verified original bytes")

        archived = content_store.archive_user_original(
            run_dir,
            str(input_a),
            ref=dict(self.REF),
            supplied_via="test",
            file_format="pdf",
            move=False,
        )
        snapshot = content_store.debug_snapshot(run_dir)
        stored = Path(snapshot["storage_root"], archived["stored_relpath"])
        stored.write_bytes(b"tampered destination bytes")

        with self.assertRaisesRegex(ValueError, "different file bytes"):
            content_store.archive_user_original(
                run_dir,
                str(input_b),
                ref=dict(self.REF),
                supplied_via="test",
                file_format="pdf",
                move=False,
            )

        self.assertEqual(stored.read_bytes(), b"tampered destination bytes")
        self.assertEqual(input_b.read_bytes(), b"verified original bytes")
        self.assertEqual(len(content_store.debug_snapshot(run_dir)["tables"]["library_items"]), 1)

    def test_archive_user_original_does_not_overwrite_destination_created_during_copy(self):
        from core.fetch.storage import content_store

        run_dir = self._make_run("run-archive-destination-race")
        source = Path(self.tmp.name, "race.pdf")
        source.write_bytes(b"source bytes")
        ref = dict(self.REF)
        identity_key = content_store._primary_identity(ref)[1]
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        identity_hash = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()
        pair_hash = hashlib.sha256(
            f"{identity_hash}{source_hash}".encode("ascii")
        ).hexdigest()
        destination = Path(
            content_store.user_library_dir(run_dir, "pdf"), f"{pair_hash}.pdf"
        )
        content_store.preflight(run_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        original_exists = content_store.os.path.exists
        destination_key = str(
            content_store.logical_path(destination, resolve=False)
        ).casefold()

        def create_destination_after_precheck(path):
            path_key = str(content_store.logical_path(path, resolve=False)).casefold()
            if path_key == destination_key and not original_exists(str(destination)):
                destination.write_bytes(b"independent destination bytes")
                return False
            return original_exists(path)

        with mock.patch.object(
            content_store.os.path, "exists", side_effect=create_destination_after_precheck
        ):
            with self.assertRaisesRegex(ValueError, "appeared during archival"):
                content_store.archive_user_original(
                    run_dir,
                    str(source),
                    ref=ref,
                    supplied_via="test",
                    file_format="pdf",
                    move=True,
                )

        self.assertEqual(destination.read_bytes(), b"independent destination bytes")
        self.assertEqual(source.read_bytes(), b"source bytes")
        self.assertEqual(content_store.debug_snapshot(run_dir)["tables"]["library_items"], [])

    def test_archive_user_original_checks_copy_before_moving_source(self):
        from core.fetch.storage import content_store

        run_dir = self._make_run("run-archive-copy-race")
        source = Path(self.tmp.name, "changing-source.pdf")
        source.write_bytes(b"bytes observed before copy")
        original_hash_file = content_store._sha256_file
        source_hash_calls = 0

        def mutate_after_source_hash(path):
            nonlocal source_hash_calls
            digest = original_hash_file(path)
            if Path(path) == source:
                source_hash_calls += 1
                if source_hash_calls == 1:
                    source.write_bytes(b"bytes changed during copy")
            return digest

        with mock.patch.object(
            content_store, "_sha256_file", side_effect=mutate_after_source_hash
        ):
            with self.assertRaisesRegex(ValueError, "changed while being archived"):
                content_store.archive_user_original(
                    run_dir,
                    str(source),
                    ref=dict(self.REF),
                    supplied_via="test",
                    file_format="pdf",
                    move=True,
                )

        self.assertEqual(source.read_bytes(), b"bytes changed during copy")
        self.assertEqual(content_store.debug_snapshot(run_dir)["tables"]["library_items"], [])

    def test_archive_user_original_does_not_reassign_another_work_library_path(self):
        from core.fetch.storage import content_store

        run_dir = self._make_run("run-archive-owner")
        ref_a = dict(self.REF)
        ref_b = {**self.REF, "id": "ref-2", "doi": "10.1000/another-work"}
        source = Path(self.tmp.name, "owned-source.pdf")
        source.write_bytes(b"owned source bytes")

        archived_a = content_store.archive_user_original(
            run_dir, str(source), ref=ref_a, supplied_via="test", file_format="pdf", move=False
        )
        storage_root = Path(content_store.storage_root(run_dir))
        stored_a = storage_root / archived_a["stored_relpath"]
        archived_b = content_store.archive_user_original(
            run_dir, str(stored_a), ref=ref_b, supplied_via="test", file_format="pdf", move=False
        )

        self.assertNotEqual(archived_a["work_id"], archived_b["work_id"])
        self.assertNotEqual(archived_a["stored_relpath"], archived_b["stored_relpath"])
        self.assertEqual(stored_a.read_bytes(), b"owned source bytes")
        self.assertEqual(
            (storage_root / archived_b["stored_relpath"]).read_bytes(),
            b"owned source bytes",
        )
        self.assertEqual(len(content_store.debug_snapshot(run_dir)["tables"]["library_items"]), 2)
