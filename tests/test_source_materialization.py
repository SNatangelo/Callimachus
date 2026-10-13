# tests/test_source_materialization.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Regression tests for crash-safe source text materialization."""
from tests._bootstrap import *  # noqa: F401,F403

import hashlib
import ntpath
from types import SimpleNamespace
from unittest import mock

import pytest

from core.infra.db import RunRepository
from core.infra.db import repository as db_repository
from core.resolve import sources


REF = {
    "id": "ref-materialization", "ref_number": 1,
    "raw_entry": "Materialization source.", "title": "Materialization source",
}


def _repo(run):
    repo = RunRepository.create(
        run, run_id="materialization-run", input_path="input", input_sha256="sha",
        accuracy="standard", style=None, model_id=None, http_profile="default",
        challenge_mode="off", fixture_fingerprint="fixture",
    )
    return repo


def _pending_store(run, text="body", *, flags=None):
    with mock.patch.object(sources, "_after_materialization_replace", side_effect=RuntimeError("stop")):
        with pytest.raises(RuntimeError, match="stop"):
            sources.store_text(run, REF, "abstract", "crossref", text, source_ref="doi:10/x", extraction_flags=flags)


@pytest.mark.parametrize(
    ("run_dir", "real_target", "escapes"),
    [
        (r"C:\runs\materialization-run", r"\\?\C:\runs\materialization-run\sources\parsed\article.txt", False),
        (r"\\server\share\materialization-run", r"\\?\UNC\server\share\materialization-run\sources\parsed\article.txt", False),
        (r"C:\runs\materialization-run", r"C:\outside\article.txt", True),
        (r"C:\runs\materialization-run", r"\\?\C:\outside\article.txt", True),
    ],
)
def test_materialization_path_handles_equivalent_windows_spelling_and_contains_targets(
    tmp_path, monkeypatch, run_dir, real_target, escapes,
):
    repo = _repo(str(tmp_path))

    class WindowsPaths:
        join = staticmethod(ntpath.join)
        commonpath = staticmethod(ntpath.commonpath)
        normcase = staticmethod(ntpath.normcase)
        normpath = staticmethod(ntpath.normpath)

        @staticmethod
        def realpath(path):
            return run_dir if path == run_dir else real_target

    monkeypatch.setattr(db_repository, "os", SimpleNamespace(name="nt", path=WindowsPaths))
    monkeypatch.setattr(repo, "_run_dir", run_dir)
    try:
        stored_path = "sources/parsed/article.txt"
        if escapes:
            with pytest.raises(RuntimeError, match="escapes run directory"):
                repo._materialization_path(stored_path)
        else:
            assert repo._materialization_path(stored_path) == (stored_path, real_target)
    finally:
        repo.close()


def test_reconcile_registers_exact_prepared_intent_after_publish_failure(tmp_path):
    run = str(tmp_path)
    _repo(run).close()
    _pending_store(run, "body", flags=["fragmented_lines"])

    repo = RunRepository.open(run)
    try:
        assert repo.list_source_texts() == []
        assert repo._conn.execute("SELECT COUNT(*) FROM source_text_materialization_intents").fetchone()[0] == 1
        assert repo._conn.execute("SELECT COUNT(*) FROM source_text_materialization_outcomes").fetchone()[0] == 0
    finally:
        repo.close()

    assert sources.reconcile_materialized_texts(run) == 1
    repo = RunRepository.open(run)
    try:
        row = repo.list_source_texts()[0]
        assert (row.origin, row.source_ref, row.sha256, row.char_count, row.extraction_flags) == (
            "crossref", "doi:10/x", hashlib.sha256(b"body").hexdigest(), 4, ["fragmented_lines"]
        )
        assert repo._conn.execute("SELECT outcome FROM source_text_materialization_outcomes").fetchone()[0] == "registered"
    finally:
        repo.close()


def test_tampered_or_filename_only_asset_never_becomes_source_text(tmp_path):
    run = str(tmp_path)
    _repo(run).close()
    _pending_store(run, "body")
    path = tmp_path / "sources" / "parsed" / "1_abstract_crossref.txt"
    path.write_text("tampered", encoding="utf-8")
    assert sources.reconcile_materialized_texts(run) == 1

    orphan = tmp_path / "sources" / "parsed" / "99_fulltext_user.txt"
    orphan.write_text("unprovenanced", encoding="utf-8")
    assert sources.reconcile_materialized_texts(run) == 0
    repo = RunRepository.open(run)
    try:
        assert repo.list_source_texts() == []
        assert not repo.source_text_exists(REF["id"], tier="fulltext")
        assert repo._conn.execute("SELECT outcome FROM source_text_materialization_outcomes").fetchone()[0] == "file_hash_mismatch"
    finally:
        repo.close()


def test_missing_asset_is_terminal_and_never_registers_source_text(tmp_path):
    run = str(tmp_path)
    _repo(run).close()
    _pending_store(run, "body")
    (tmp_path / "sources" / "parsed" / "1_abstract_crossref.txt").unlink()
    assert sources.reconcile_materialized_texts(run) == 1
    repo = RunRepository.open(run)
    try:
        assert repo.list_source_texts() == []
        assert repo._conn.execute(
            "SELECT outcome FROM source_text_materialization_outcomes"
        ).fetchone()[0] == "file_missing"
    finally:
        repo.close()


def test_intent_uses_existing_empty_flag_normalization(tmp_path):
    run = str(tmp_path)
    _repo(run).close()
    sources.store_text(run, REF, "abstract", "crossref", "seed")
    for source_id, path, flags in (
        ("src-null", "sources/parsed/null.txt", None),
        ("src-empty", "sources/parsed/empty.txt", []),
    ):
        asset = tmp_path / path
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text("body", encoding="utf-8")
        repo = RunRepository.open(run)
        try:
            intent_id = repo.prepare_source_text_materialization(
                source_text_id=source_id, ref_id=REF["id"], identity_key=f"path:{path}",
                tier="abstract", origin="crossref", stored_path=path,
                sha256=hashlib.sha256(b"body").hexdigest(), char_count=4,
                extraction_flags=flags,
            )
            assert repo.finalize_source_text_materialization(intent_id) == "registered"
            state = repo._conn.execute(
                "SELECT is_null,flag_count FROM source_text_materialization_intent_flag_states WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            assert (state["is_null"], state["flag_count"]) == (1, 0)
        finally:
            repo.close()


def test_store_text_raises_when_its_own_intent_is_not_registered(tmp_path):
    run = str(tmp_path)
    _repo(run).close()
    path = tmp_path / "sources" / "parsed" / "1_abstract_crossref.txt"

    def tamper_after_publish():
        path.write_text("tampered", encoding="utf-8")

    with mock.patch.object(sources, "_after_materialization_replace", tamper_after_publish):
        with pytest.raises(RuntimeError, match="file_hash_mismatch"):
            sources.store_text(run, REF, "abstract", "crossref", "body")
    repo = RunRepository.open(run)
    try:
        assert repo.list_source_texts() == []
        assert repo._conn.execute(
            "SELECT outcome FROM source_text_materialization_outcomes"
        ).fetchone()[0] == "file_hash_mismatch"
    finally:
        repo.close()


def test_normal_same_tier_origin_replacement_remains_last_write_wins(tmp_path):
    run = str(tmp_path)
    _repo(run).close()
    sources.store_text(run, REF, "abstract", "crossref", "first")
    sources.store_text(run, REF, "abstract", "crossref", "second")
    repo = RunRepository.open(run)
    try:
        rows = repo.list_source_texts()
        assert len(rows) == 1
        assert rows[0].sha256 == hashlib.sha256(b"second").hexdigest()
    finally:
        repo.close()


def test_replace_parse_payload_clears_materialization_history_before_references(tmp_path):
    run = str(tmp_path)
    _repo(run).close()
    sources.store_text(run, REF, "abstract", "crossref", "body")
    repo = RunRepository.open(run)
    try:
        repo.replace_parse_payload(claims=[], references=[REF], citations=[])
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM source_text_materialization_intents"
        ).fetchone()[0] == 0
        assert repo.list_source_texts() == []
    finally:
        repo.close()


def test_restart_clears_pending_intent_so_reconcile_cannot_restore_it(tmp_path):
    run = str(tmp_path)
    _repo(run).close()
    _pending_store(run, "body")
    repo = RunRepository.open(run)
    try:
        repo.restart_in_place(reason="test")
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM source_text_materialization_intents"
        ).fetchone()[0] == 0
    finally:
        repo.close()
    assert sources.reconcile_materialized_texts(run) == 0
    repo = RunRepository.open(run)
    try:
        assert repo.list_source_texts() == []
    finally:
        repo.close()


@pytest.mark.parametrize("replacement", [None, "tampered"])
def test_reconcile_invalidates_missing_or_tampered_registered_source_once(tmp_path, replacement):
    run = str(tmp_path)
    _repo(run).close()
    sources.store_text(run, REF, "abstract", "crossref", "body")
    path = tmp_path / "sources" / "parsed" / "1_abstract_crossref.txt"
    if replacement is None:
        path.unlink()
        expected = "file_missing"
    else:
        path.write_text(replacement, encoding="utf-8")
        expected = "file_hash_mismatch"
    assert sources.reconcile_materialized_texts(run) == 0
    assert sources.reconcile_materialized_texts(run) == 0
    repo = RunRepository.open(run)
    try:
        assert not repo.source_text_exists(REF["id"], tier="abstract")
        rows = repo._conn.execute(
            "SELECT reason FROM source_text_materialization_invalidations"
        ).fetchall()
        assert [row["reason"] for row in rows] == [expected]
    finally:
        repo.close()


def test_reconcile_keeps_valid_current_source_and_ignores_replaced_history(tmp_path):
    run = str(tmp_path)
    _repo(run).close()
    sources.store_text(run, REF, "abstract", "crossref", "first")
    sources.store_text(run, REF, "abstract", "crossref", "second")
    assert sources.reconcile_materialized_texts(run) == 0
    repo = RunRepository.open(run)
    try:
        rows = repo.list_source_texts()
        assert len(rows) == 1
        assert rows[0].sha256 == hashlib.sha256(b"second").hexdigest()
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM source_text_materialization_invalidations"
        ).fetchone()[0] == 0
    finally:
        repo.close()


def test_failed_new_intent_preserves_matching_existing_source_and_is_idempotent(tmp_path):
    run = str(tmp_path)
    _repo(run).close()
    sources.store_text(run, REF, "abstract", "crossref", "first")
    repo = RunRepository.open(run)
    try:
        original = repo.list_source_texts()[0]
        repo.prepare_source_text_materialization(
            source_text_id="src-replacement", ref_id=REF["id"], identity_key="path:parsed/1_abstract_crossref.txt",
            tier="abstract", origin="crossref", stored_path="sources/parsed/1_abstract_crossref.txt",
            sha256=hashlib.sha256(b"different").hexdigest(), char_count=len("different"),
        )
    finally:
        repo.close()
    assert sources.reconcile_materialized_texts(run) == 1
    assert sources.reconcile_materialized_texts(run) == 0
    repo = RunRepository.open(run)
    try:
        assert repo.list_source_texts()[0].source_text_id == original.source_text_id
        assert repo._conn.execute("SELECT COUNT(*) FROM source_text_materialization_outcomes").fetchone()[0] == 2
    finally:
        repo.close()


def test_two_separate_registered_source_losses_append_two_invalidations(tmp_path):
    run = str(tmp_path)
    _repo(run).close()
    path = tmp_path / "sources" / "parsed" / "1_abstract_crossref.txt"
    for text in ("body", "body"):
        sources.store_text(run, REF, "abstract", "crossref", text)
        path.unlink()
        assert sources.reconcile_materialized_texts(run) == 0
        assert sources.reconcile_materialized_texts(run) == 0
    repo = RunRepository.open(run)
    try:
        rows = repo._conn.execute(
            "SELECT reason FROM source_text_materialization_invalidations ORDER BY invalidation_id"
        ).fetchall()
        assert [row["reason"] for row in rows] == ["file_missing", "file_missing"]
    finally:
        repo.close()
