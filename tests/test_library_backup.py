# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from core.app.library import backup as library_backup
from core.fetch.storage import content_store
from core.parse.extract import extract_text


def _env(tmp_path: Path, name: str) -> dict[str, str]:
    return {content_store.ENV_STATE_DIR: str(tmp_path / name)}


def _seed(tmp_path: Path, name: str, *, original: bool = True, text: bool = True, doi: str = "10.5555/backup.2026", title: str = "Résumé on archival safety"):
    env = _env(tmp_path, name)
    run = str(tmp_path / "runs" / name)
    content_store.preflight(run, env)
    ref = {
        "title": title,
        "doi": doi,
        "year": 2026,
        "first_author_surname": "Élodie Martin",
    }
    saved = {}
    if original:
        source = tmp_path / f"{name}-paper.pdf"
        saved["original_bytes"] = b"%PDF-1.7\nportable original\n"
        source.write_bytes(saved["original_bytes"])
        saved["original"] = content_store.archive_user_original(
            run, str(source), ref=ref, supplied_via="test", file_format="pdf", move=False, environ=env,
        )
    if text:
        saved["text_value"] = "A résumé with naïve Unicode text.\n"
        saved["text"] = content_store.record_parsed_text(
            run, ref, "fulltext", "user", saved["text_value"],
            supplied_by="user", supplied_via="test", file_format="pdf",
            mapping="script_verified", signal="gold-standard", score=1.0,
            identity_status="script_verified", identity_note="secret proof should not travel",
            extraction_method="trusted-extractor", extraction_flags=["fake-flag"],
            preparation={"preparation_version": "trusted-v9", "preparation_flags": ["trusted-flag"]},
            environ=env,
        )
    return run, env, ref, saved


def _snapshot(run: str, env: dict[str, str]):
    return content_store.debug_snapshot(run) if env == {} else _debug_snapshot(run, env)


def _debug_snapshot(run: str, env: dict[str, str]):
    conn = content_store._connect(run, env)
    try:
        tables = {}
        for table in ("works", "work_aliases", "library_items", "parsed_texts"):
            tables[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
        return {"storage_root": content_store.storage_root(run, env), "tables": tables}
    finally:
        conn.close()


def _two_original_backup(tmp_path: Path, name: str, *, same_bytes: bool = False):
    run, env, ref, _ = _seed(tmp_path, name, original=False, text=False)
    for index, label in enumerate(("bad", "good")):
        source = tmp_path / f"{name}-{label}.pdf"
        source.write_bytes(
            b"%PDF-1.7\nshared fixture bytes\n" if same_bytes else f"%PDF-1.7\n{label} fixture bytes\n".encode()
        )
        content_store.archive_user_original(
            run, str(source), ref={**ref, "doi": f"10.5555/{name}.{index}"},
            supplied_via="local-test", file_format="pdf", move=False, environ=env,
        )
    archive = tmp_path / f"{name}.zip"
    library_backup.export_library(archive, run_dir=run, environ=env)
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    return archive, {entry["filename"]: entry["blob"] for entry in manifest["originals"]}


def _rewrite_archive(source: Path, destination: Path, *, mutate_manifest=None, remove=(), replace=None):
    with zipfile.ZipFile(source) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        members = {name: zf.read(name) for name in zf.namelist() if name != "manifest.json"}
    if mutate_manifest is not None:
        mutate_manifest(manifest, members)
    for name in remove:
        members.pop(name, None)
    for name, value in (replace or {}).items():
        members[name] = value
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        for name, value in members.items():
            zf.writestr(name, value)


def _truncate_member_compressed_size(path: Path, member_name: str, *, amount: int = 4):
    raw = bytearray(path.read_bytes())
    wanted = member_name.encode("utf-8")
    cursor = 0
    while True:
        cursor = raw.find(b"PK\x01\x02", cursor)
        if cursor < 0:
            raise AssertionError(f"central directory entry {member_name!r} was not found")
        name_len = int.from_bytes(raw[cursor + 28:cursor + 30], "little")
        extra_len = int.from_bytes(raw[cursor + 30:cursor + 32], "little")
        comment_len = int.from_bytes(raw[cursor + 32:cursor + 34], "little")
        name_start = cursor + 46
        if raw[name_start:name_start + name_len] == wanted:
            compressed_size = int.from_bytes(raw[cursor + 20:cursor + 24], "little")
            if compressed_size <= amount:
                raise AssertionError("test blob is too small to truncate")
            raw[cursor + 20:cursor + 24] = (compressed_size - amount).to_bytes(4, "little")
            path.write_bytes(raw)
            return
        cursor = name_start + name_len + extra_len + comment_len


def test_export_import_round_trip_and_does_not_restore_verification_authority(tmp_path):
    run_a, env_a, _ref, saved = _seed(tmp_path, "source")
    archive = tmp_path / "backup.zip"
    assert library_backup.export_library(archive, run_dir=run_a, environ=env_a) == {
        "originals": 1, "texts": 1, "fulltexts": 1, "abstracts": 0, "webtexts": 0,
        "works": 1, "blobs": 2, "issues": [],
    }
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        all_bytes = b"".join(zf.read(name) for name in zf.namelist())
    assert str(tmp_path).encode() not in all_bytes
    assert b"secret proof" not in all_bytes
    assert b"script_verified" not in all_bytes

    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target", original=False, text=False)
    result = library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    assert result == {"originals": 1, "texts": 1, "skipped": 0, "skipped_originals": 0, "skipped_texts": 0, "issues": []}
    snap = _snapshot(run_b, env_b)
    assert len(snap["tables"]["library_items"]) == 1
    assert len(snap["tables"]["parsed_texts"]) == 1
    original_row = snap["tables"]["library_items"][0]
    assert (Path(snap["storage_root"]) / original_row["stored_relpath"]).read_bytes() == saved["original_bytes"]
    text_row = snap["tables"]["parsed_texts"][0]
    assert (Path(snap["storage_root"]) / text_row["stored_relpath"]).read_text(encoding="utf-8") == saved["text_value"]
    assert text_row["supplied_by"] == "user"
    assert text_row["supplied_via"] == "library_backup_import"
    assert text_row["identity_status"] == "user_library_reviewed"
    assert "selected library backup" in text_row["identity_note"]
    assert text_row["mapping"] is None and text_row["match_signal"] is None
    assert text_row["extraction_method"] is None and text_row["preparation_version"] is None


def test_import_is_additive_idempotent_and_preserves_existing_entries(tmp_path):
    run_a, env_a, _ref, _ = _seed(tmp_path, "source")
    archive = tmp_path / "backup.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target")
    before = _snapshot(run_b, env_b)
    result = library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    after = _snapshot(run_b, env_b)
    assert result == {"originals": 0, "texts": 0, "skipped": 2, "skipped_originals": 1, "skipped_texts": 1, "issues": []}, (result, before["tables"]["library_items"], _snapshot(run_a, env_a)["tables"]["library_items"], before["tables"]["works"], _snapshot(run_a, env_a)["tables"]["works"])
    assert after["tables"]["library_items"] == before["tables"]["library_items"]
    assert after["tables"]["parsed_texts"] == before["tables"]["parsed_texts"]


def test_import_merges_unseen_work_without_replacing_existing_library_rows(tmp_path):
    run_a, env_a, _ref, _ = _seed(tmp_path, "source")
    archive = tmp_path / "backup.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target", doi="10.5555/backup.existing", title="Separate existing work")
    before = _snapshot(run_b, env_b)
    result = library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    after = _snapshot(run_b, env_b)
    assert result["originals"] == result["texts"] == 1 and result["skipped"] == 0, result
    assert len(after["tables"]["library_items"]) == len(before["tables"]["library_items"]) + 1
    assert len(after["tables"]["parsed_texts"]) == len(before["tables"]["parsed_texts"]) + 1
    assert before["tables"]["library_items"][0] in after["tables"]["library_items"]
    assert before["tables"]["parsed_texts"][0] in after["tables"]["parsed_texts"]


def test_parsed_text_without_original_is_restored(tmp_path):
    run_a, env_a, _ref, _ = _seed(tmp_path, "source", original=False)
    archive = tmp_path / "text-only.zip"
    assert library_backup.export_library(archive, run_dir=run_a, environ=env_a)["originals"] == 0
    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target", original=False, text=False)
    result = library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    snap = _snapshot(run_b, env_b)
    assert result["texts"] == 1 and result["originals"] == 0
    assert len(snap["tables"]["parsed_texts"]) == 1
    assert snap["tables"]["parsed_texts"][0]["library_item_id"] is None


def test_exports_and_restores_raw_entry_and_title_author_year_identities(tmp_path):
    run_a, env_a = str(tmp_path / "runs" / "source"), _env(tmp_path, "source")
    run_b, env_b = str(tmp_path / "runs" / "restored"), _env(tmp_path, "restored")
    content_store.preflight(run_a, env_a)
    raw_ref = {"raw_entry": "Ångström, E. A paper with only a raw citation. 2021."}
    tay_ref = {"title": "A citation identified by title", "year": 2022, "first_author_surname": "Müller"}
    content_store.record_parsed_text(run_a, raw_ref, "abstract", "user", "Raw identity text.", supplied_by="user", environ=env_a)
    content_store.record_parsed_text(run_a, tay_ref, "web", "user", "Title author year text.", supplied_by="user", environ=env_a)
    archive = tmp_path / "identity-forms.zip"
    result = library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    assert result["texts"] == 2 and result["works"] == 2
    assert (result["fulltexts"], result["abstracts"], result["webtexts"]) == (0, 1, 1)
    assert library_backup.import_library(archive, run_dir=run_b, environ=env_b)["texts"] == 2
    restored = _snapshot(run_b, env_b)
    assert len(restored["tables"]["works"]) == 2
    assert {row["tier"] for row in restored["tables"]["parsed_texts"]} == {"abstract", "web"}


def test_distinct_dois_with_same_title_author_year_stay_distinct(tmp_path):
    run_a, env_a, _ref, _ = _seed(tmp_path, "source", original=False)
    second_ref = {
        "doi": "10.5555/backup.second",
        "title": "Résumé on archival safety",
        "year": 2026,
        "first_author_surname": "Élodie Martin",
    }
    content_store.record_parsed_text(
        run_a, second_ref, "fulltext", "user", "A résumé with naïve Unicode text.\n",
        supplied_by="user", environ=env_a,
    )
    archive = tmp_path / "two-dois.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target", original=False, text=False)
    result = library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    restored = _snapshot(run_b, env_b)
    assert result["texts"] == 2 and len(restored["tables"]["works"]) == 2
    assert len({row["work_id"] for row in restored["tables"]["parsed_texts"]}) == 2
    assert {row["doi"] for row in restored["tables"]["works"]} == {"10.5555/backup.2026", "10.5555/backup.second"}


@pytest.mark.parametrize("mutate", ["traversal", "extra-member", "symlink"])
def test_rejects_invalid_archive_before_store_changes(tmp_path, mutate):
    run_a, env_a, _ref, _ = _seed(tmp_path, "source")
    source = tmp_path / "source.zip"
    library_backup.export_library(source, run_dir=run_a, environ=env_a)
    with zipfile.ZipFile(source) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        members = {name: zf.read(name) for name in zf.namelist()}
    if mutate == "bad-digest":
        blob = next(name for name in members if name.startswith("blobs/"))
        members[blob] += b"tamper"
    elif mutate == "traversal":
        members["../escape"] = b"x"
    elif mutate == "extra-member":
        members["unexpected"] = b"x"
    elif mutate == "symlink":
        members["blobs/" + manifest["blobs"][0]] = members.pop("blobs/" + manifest["blobs"][0])
        # Recreate one member with Unix symlink metadata below.
    bad = tmp_path / f"{mutate}.zip"
    with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, value in members.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            if mutate == "symlink" and name.startswith("blobs/"):
                info.create_system = 3
                info.external_attr = (0o120777 << 16)
            zf.writestr(info, value)
    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target", original=False, text=False)
    before = _snapshot(run_b, env_b)
    before_mutation_calls = []
    with pytest.raises(library_backup.LibraryBackupError):
        library_backup.import_library(
            bad, run_dir=run_b, environ=env_b,
            before_mutation=lambda: before_mutation_calls.append(True),
        )
    assert before_mutation_calls == []
    assert _snapshot(run_b, env_b)["tables"] == before["tables"]


def test_failed_sql_transaction_removes_only_new_promoted_files(tmp_path, monkeypatch):
    run_a, env_a, _ref, _ = _seed(tmp_path, "source")
    archive = tmp_path / "backup.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target", title="Existing unrelated work", doi="10.5555/existing")
    before = _snapshot(run_b, env_b)
    root = Path(content_store.storage_root(run_b, env_b))
    original_connect = content_store._connect

    class FailingConnection:
        def __init__(self, conn):
            self._conn = conn
        def execute(self, sql, *args):
            if "INSERT INTO parsed_texts" in sql:
                raise RuntimeError("forced transaction failure")
            return self._conn.execute(sql, *args)
        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(content_store, "_connect", lambda *args, **kwargs: FailingConnection(original_connect(*args, **kwargs)))
    with pytest.raises(RuntimeError, match="forced transaction failure"):
        library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    monkeypatch.undo()
    snap = _snapshot(run_b, env_b)
    assert snap["tables"] == before["tables"]
    assert {p.relative_to(root).as_posix() for p in (root / "user_library").rglob("*.pdf")} == {
        row["stored_relpath"] for row in before["tables"]["library_items"]
    }
    assert {p.relative_to(root).as_posix() for p in (root / "parsed_cache").rglob("*.txt")} == {
        row["stored_relpath"] for row in before["tables"]["parsed_texts"]
    }


def test_export_skips_a_missing_original_and_keeps_healthy_text(tmp_path):
    run, env, _ref, saved = _seed(tmp_path, "source")
    snap = _snapshot(run, env)
    root = Path(snap["storage_root"])
    original = snap["tables"]["library_items"][0]
    (root / original["stored_relpath"]).unlink()
    archive = tmp_path / "out.zip"
    result = library_backup.export_library(archive, run_dir=run, environ=env)
    assert result["originals"] == 0 and result["texts"] == 1
    assert result["issues"] == [{"kind": "original", "name": "source-paper.pdf", "reason_code": "missing"}]
    run_target = str(tmp_path / "runs" / "target")
    env_target = _env(tmp_path, "target")
    restored = library_backup.import_library(archive, run_dir=run_target, environ=env_target)
    assert restored["texts"] == 1 and restored["issues"] == []
    text_row = _snapshot(run_target, env_target)["tables"]["parsed_texts"][0]
    assert (Path(content_store.storage_root(run_target, env_target)) / text_row["stored_relpath"]).read_text(encoding="utf-8") == saved["text_value"]


def test_export_empty_library_does_not_create_content_store_database(tmp_path):
    env = _env(tmp_path, "empty")
    run = str(tmp_path / "runs" / "empty")
    db = Path(content_store.db_path(run, env))
    assert not db.exists()
    counts = library_backup.export_library(tmp_path / "empty.zip", run_dir=run, environ=env)
    assert counts == {
        "originals": 0, "texts": 0, "fulltexts": 0, "abstracts": 0, "webtexts": 0,
        "works": 0, "blobs": 0, "issues": [],
    }
    assert not db.exists()


def test_restore_bounds_zip_directory_before_allocating_index(tmp_path, monkeypatch):
    archive = tmp_path / "many-members.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        for index in range(3):
            stream.writestr(str(index), b"test")
    monkeypatch.setattr(library_backup, "MAX_MEMBERS", 1)
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: pytest.fail("index must not be allocated"))
    env = _env(tmp_path, "empty")
    run = str(tmp_path / "runs" / "empty")
    with pytest.raises(library_backup.LibraryBackupError, match="directory"):
        library_backup.import_library(archive, run_dir=run, environ=env)
    assert not Path(content_store.db_path(run, env)).exists()


def test_export_rejects_destination_inside_store_and_uses_a_verified_snapshot(tmp_path, monkeypatch):
    run, env, _ref, saved = _seed(tmp_path, "source")
    archive = tmp_path / "existing.zip"
    archive.write_bytes(b"prior backup")
    root = Path(content_store.storage_root(run, env))
    with pytest.raises(library_backup.LibraryBackupError, match="inside the content store"):
        library_backup.export_library(root / "content.sqlite", run_dir=run, environ=env)

    original_snapshot = library_backup._snapshot_file
    changed = False
    def mutate_after_snapshot(path, snapshot):
        nonlocal changed
        result = original_snapshot(path, snapshot)
        if not changed and path.suffix == ".pdf":
            changed = True
            path.write_bytes(path.read_bytes() + b"changed")
        return result
    monkeypatch.setattr(library_backup, "_snapshot_file", mutate_after_snapshot)
    result = library_backup.export_library(archive, run_dir=run, environ=env)
    assert result["originals"] == result["texts"] == 1 and result["issues"] == []
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        original_blob = manifest["originals"][0]["blob"]
        assert zf.read(f"blobs/{original_blob}") == saved["original_bytes"]


def test_restore_promotion_never_overwrites_a_file_that_appears_during_commit(tmp_path, monkeypatch):
    run_a, env_a, _ref, _ = _seed(tmp_path, "source")
    archive = tmp_path / "backup.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target", original=False, text=False)
    original_link = library_backup.os.link
    raced_target = None
    def race_link(src, dst, *args, **kwargs):
        nonlocal raced_target
        raced_target = Path(dst)
        raced_target.write_bytes(b"user-created concurrent file")
        return original_link(src, dst, *args, **kwargs)
    monkeypatch.setattr(library_backup.os, "link", race_link)
    with pytest.raises(FileExistsError):
        library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    assert raced_target is not None and raced_target.read_bytes() == b"user-created concurrent file"
    snapshot = _snapshot(run_b, env_b)
    assert not snapshot["tables"]["library_items"] and not snapshot["tables"]["parsed_texts"]


def test_reimport_reactivates_deactivated_text_and_keeps_existing_metadata(tmp_path):
    run_a, env_a, ref, _ = _seed(tmp_path, "source", original=False, text=False)
    text_value = "A portable abstract that should become reusable again."
    content_store.record_parsed_text(
        run_a, ref, "abstract", "user", text_value,
        source_ref="local-user-source", content_version="reviewed-v1",
        supplied_by="user", supplied_via="local-user", file_format="txt",
        mapping="manual", signal="operator-note", score=1.0,
        identity_status="user_reviewed", identity_note="keep this metadata",
        environ=env_a,
    )
    archive = tmp_path / "deactivated.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)

    run_b, env_b, ref_b, _ = _seed(tmp_path, "target", original=False, text=False)
    assert library_backup.import_library(archive, run_dir=run_b, environ=env_b)["texts"] == 1
    before = _snapshot(run_b, env_b)["tables"]["parsed_texts"][0]
    assert content_store.deactivate_parsed_texts(run_b, [before["parsed_text_id"]], environ=env_b) == 1
    assert content_store.find_reusable_parsed_text(run_b, ref_b, tiers=("abstract",), environ=env_b) is None

    restored = library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    assert restored == {
        "originals": 0, "texts": 1, "skipped": 0,
        "skipped_originals": 0, "skipped_texts": 0, "issues": [],
    }
    after = _snapshot(run_b, env_b)["tables"]["parsed_texts"][0]
    assert len(_snapshot(run_b, env_b)["tables"]["parsed_texts"]) == 1
    assert after["active"] == 1 and after["missing"] == 0
    for field in ("parsed_text_id", "source_ref", "supplied_by", "supplied_via", "file_format", "mapping", "match_signal", "identity_status", "identity_note"):
        assert after[field] == before[field]
    reusable = content_store.find_reusable_parsed_text(run_b, ref_b, tiers=("abstract",), environ=env_b)
    assert reusable is not None
    assert (Path(content_store.storage_root(run_b, env_b)) / reusable["stored_relpath"]).read_text(encoding="utf-8") == text_value


def test_reimport_restores_missing_text_file_marked_missing_by_reuse_api(tmp_path):
    run_a, env_a, ref, _ = _seed(tmp_path, "source", original=False, text=False)
    text_value = "The archived text can repopulate its missing cache file."
    content_store.record_parsed_text(
        run_a, ref, "abstract", "user", text_value,
        source_ref="local-user-source", supplied_by="user", supplied_via="local-user",
        file_format="txt", environ=env_a,
    )
    archive = tmp_path / "missing-file.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)

    run_b, env_b, ref_b, _ = _seed(tmp_path, "target", original=False, text=False)
    library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    row = _snapshot(run_b, env_b)["tables"]["parsed_texts"][0]
    store_file = Path(content_store.storage_root(run_b, env_b)) / row["stored_relpath"]
    store_file.unlink()
    assert content_store.find_reusable_parsed_text(run_b, ref_b, tiers=("abstract",), environ=env_b) is None
    missing = _snapshot(run_b, env_b)["tables"]["parsed_texts"][0]
    assert missing["active"] == 0 and missing["missing"] == 1

    restored = library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    assert restored["texts"] == 1 and restored["skipped_texts"] == 0 and restored["issues"] == []
    active = _snapshot(run_b, env_b)["tables"]["parsed_texts"][0]
    assert active["parsed_text_id"] == row["parsed_text_id"]
    assert active["active"] == 1 and active["missing"] == 0
    assert store_file.read_text(encoding="utf-8") == text_value
    assert content_store.find_reusable_parsed_text(run_b, ref_b, tiers=("abstract",), environ=env_b) is not None


def test_reimport_does_not_overwrite_conflicting_existing_text_bytes(tmp_path):
    run_a, env_a, ref, _ = _seed(tmp_path, "source", original=False, text=False)
    content_store.record_parsed_text(
        run_a, ref, "abstract", "user", "Original archived text.",
        source_ref="local-user-source", supplied_by="user", file_format="txt", environ=env_a,
    )
    archive = tmp_path / "conflicting-file.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)

    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target", original=False, text=False)
    library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    before = _snapshot(run_b, env_b)["tables"]["parsed_texts"][0]
    store_file = Path(content_store.storage_root(run_b, env_b)) / before["stored_relpath"]
    assert content_store.deactivate_parsed_texts(run_b, [before["parsed_text_id"]], environ=env_b) == 1
    store_file.write_bytes(b"user-created conflicting bytes")

    result = library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    after = _snapshot(run_b, env_b)["tables"]["parsed_texts"][0]
    assert result["texts"] == 0 and result["skipped_texts"] == 1
    assert len(result["issues"]) == 1 and result["issues"][0]["reason_code"] == "damaged"
    assert store_file.read_bytes() == b"user-created conflicting bytes"
    assert after["active"] == 0 and after["missing"] == 0
    assert after["identity_note"] == before["identity_note"]


def test_before_mutation_hook_runs_once_immediately_before_store_connect(tmp_path, monkeypatch):
    run_a, env_a, _ref, _ = _seed(tmp_path, "source")
    archive = tmp_path / "valid.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target", original=False, text=False)
    original_connect = content_store._connect
    events = []

    def tracked_connect(*args, **kwargs):
        assert events == ["before_mutation"]
        events.append("connect")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(content_store, "_connect", tracked_connect)
    result = library_backup.import_library(
        archive, run_dir=run_b, environ=env_b,
        before_mutation=lambda: events.append("before_mutation"),
    )
    assert result["originals"] == result["texts"] == 1
    assert events == ["before_mutation", "connect"]


def test_backup_preserves_each_text_original_link_and_unlinked_text(tmp_path):
    run_a, env_a, ref, _ = _seed(tmp_path, "source", original=False, text=False)
    originals = []
    for filename, contents in (("article.pdf", b"primary original bytes"), ("supplement.pdf", b"supplement original bytes")):
        source = tmp_path / filename
        source.write_bytes(contents)
        originals.append(content_store.archive_user_original(
            run_a, str(source), ref=ref, supplied_via="local-test", file_format="pdf", move=False, environ=env_a,
        ))
    values = {
        "primary full text": ("fulltext", originals[0]["library_item_id"]),
        "supplement web text": ("web", originals[1]["library_item_id"]),
        "unlinked fetched abstract": ("abstract", None),
    }
    for text_value, (tier, linked_item_id) in values.items():
        content_store.record_parsed_text(
            run_a, ref, tier, "user", text_value,
            source_ref=f"fixture:{text_value}", supplied_by="user", supplied_via="fixture",
            file_format="pdf", library_item_id=linked_item_id, environ=env_a,
        )
    archive = tmp_path / "linked-library.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)

    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    text_refs = {entry["sha256"]: entry["original"] for entry in manifest["texts"]}
    assert text_refs[hashlib.sha256(b"primary full text").hexdigest()]["filename"] == "article.pdf"
    assert text_refs[hashlib.sha256(b"supplement web text").hexdigest()]["filename"] == "supplement.pdf"
    assert text_refs[hashlib.sha256(b"unlinked fetched abstract").hexdigest()] is None

    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target", original=False, text=False)
    library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    restored = _snapshot(run_b, env_b)
    item_by_name = {row["original_filename"]: row["library_item_id"] for row in restored["tables"]["library_items"]}
    text_by_sha = {row["sha256"]: row for row in restored["tables"]["parsed_texts"]}
    assert text_by_sha[hashlib.sha256(b"primary full text").hexdigest()]["library_item_id"] == item_by_name["article.pdf"]
    assert text_by_sha[hashlib.sha256(b"supplement web text").hexdigest()]["library_item_id"] == item_by_name["supplement.pdf"]
    assert text_by_sha[hashlib.sha256(b"unlinked fetched abstract").hexdigest()]["library_item_id"] is None


@pytest.mark.parametrize("suffix", [".html", ".htm", ".xhtml"])
def test_html_library_original_and_extracted_text_round_trip(tmp_path, suffix):
    doi = f"10.5555/html{suffix[1:]}"
    run_a, env_a, _ref, _ = _seed(
        tmp_path, "source" + suffix[1:], original=False, text=False, doi=doi,
        title="Local HTML source round trip",
    )
    ref = {"title": "Local HTML source round trip", "doi": doi, "year": 2026, "first_author_surname": "Martin"}
    original_bytes = (
        f"<!doctype html><html><head><title>Local HTML source</title></head>"
        f"<body><article><h1>Local HTML source round trip</h1>"
        + "".join(
            f"<p>Paragraph {index} discusses the article methods, findings, limitations, and implications "
            "with enough complete prose to preserve the local HTML extractor's full article body.</p>"
            for index in range(20)
        )
        + "</article></body></html>"
    ).encode("utf-8")
    source = tmp_path / f"article{suffix}"
    source.write_bytes(original_bytes)
    extracted, detected_format, _metadata = extract_text(str(source))
    assert detected_format == "html"
    original = content_store.archive_user_original(
        run_a, str(source), ref=ref, supplied_via="local-html-test", file_format=detected_format,
        move=False, environ=env_a,
    )
    content_store.record_parsed_text(
        run_a, ref, "abstract", "user", extracted,
        source_ref=str(source), supplied_by="user", supplied_via="local-html-test",
        file_format=detected_format, library_item_id=original["library_item_id"], environ=env_a,
    )
    archive = tmp_path / f"html-{suffix[1:]}.zip"
    exported = library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    assert exported["originals"] == exported["texts"] == 1 and exported["issues"] == []

    run_b, env_b, _ref_b, _ = _seed(tmp_path, "target" + suffix[1:], original=False, text=False, doi=doi,
                                    title="Local HTML source round trip")
    restored_result = library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    assert restored_result["originals"] == restored_result["texts"] == 1
    snapshot = _snapshot(run_b, env_b)
    original_row = snapshot["tables"]["library_items"][0]
    text_row = snapshot["tables"]["parsed_texts"][0]
    restored_path = Path(snapshot["storage_root"]) / original_row["stored_relpath"]
    restored_text_path = Path(snapshot["storage_root"]) / text_row["stored_relpath"]
    assert original_row["file_format"] == "html" and original_row["original_filename"] == source.name
    assert restored_path.suffix == suffix
    assert restored_path.read_bytes() == original_bytes
    assert text_row["file_format"] == "html" and text_row["library_item_id"] == original_row["library_item_id"]
    assert restored_text_path.read_text(encoding="utf-8") == extracted


@pytest.mark.parametrize("damage,expected_reason", [
    ("missing", "missing"),
    ("hash", "damaged"),
    ("crc", "damaged"),
    ("truncated", "damaged"),
    ("encrypted", "unreadable"),
    ("unsupported-method", "unreadable"),
    ("declared-size", "damaged"),
])
def test_import_skips_one_bad_original_blob_and_restores_healthy_sibling(tmp_path, damage, expected_reason):
    archive, digests = _two_original_backup(tmp_path, f"partial-{damage}")
    damaged_name = f"partial-{damage}-bad.pdf"
    damaged_member = f"blobs/{digests[damaged_name]}"
    bad_archive = tmp_path / f"partial-{damage}-damaged.zip"

    if damage == "missing":
        _rewrite_archive(archive, bad_archive, remove=[damaged_member])
    elif damage == "hash":
        with zipfile.ZipFile(archive) as zf:
            changed = zf.read(damaged_member) + b"changed"
        _rewrite_archive(archive, bad_archive, replace={damaged_member: changed})
    elif damage == "crc":
        _rewrite_archive(archive, bad_archive)
        raw = bytearray(bad_archive.read_bytes())
        cursor = 0
        wanted = damaged_member.encode("utf-8")
        while True:
            cursor = raw.find(b"PK\x01\x02", cursor)
            if cursor < 0:
                raise AssertionError("central directory member was not found")
            name_len = int.from_bytes(raw[cursor + 28:cursor + 30], "little")
            extra_len = int.from_bytes(raw[cursor + 30:cursor + 32], "little")
            comment_len = int.from_bytes(raw[cursor + 32:cursor + 34], "little")
            name_start = cursor + 46
            if raw[name_start:name_start + name_len] == wanted:
                crc = int.from_bytes(raw[cursor + 16:cursor + 20], "little")
                raw[cursor + 16:cursor + 20] = (crc ^ 0xFFFFFFFF).to_bytes(4, "little")
                bad_archive.write_bytes(raw)
                break
            cursor = name_start + name_len + extra_len + comment_len
    elif damage == "truncated":
        _rewrite_archive(archive, bad_archive)
        _truncate_member_compressed_size(bad_archive, damaged_member)
    elif damage in {"encrypted", "unsupported-method"}:
        _rewrite_archive(archive, bad_archive)
        raw = bytearray(bad_archive.read_bytes())
        cursor = 0
        wanted = damaged_member.encode("utf-8")
        while True:
            cursor = raw.find(b"PK\x01\x02", cursor)
            if cursor < 0:
                raise AssertionError("central directory member was not found")
            name_len = int.from_bytes(raw[cursor + 28:cursor + 30], "little")
            extra_len = int.from_bytes(raw[cursor + 30:cursor + 32], "little")
            comment_len = int.from_bytes(raw[cursor + 32:cursor + 34], "little")
            name_start = cursor + 46
            if raw[name_start:name_start + name_len] == wanted:
                if damage == "encrypted":
                    flags = int.from_bytes(raw[cursor + 8:cursor + 10], "little")
                    raw[cursor + 8:cursor + 10] = (flags | 1).to_bytes(2, "little")
                else:
                    raw[cursor + 10:cursor + 12] = (99).to_bytes(2, "little")
                bad_archive.write_bytes(raw)
                break
            cursor = name_start + name_len + extra_len + comment_len
    else:
        def wrong_declared_size(manifest, _members):
            entry = next(item for item in manifest["originals"] if item["filename"] == damaged_name)
            entry["size"] += 1
        _rewrite_archive(archive, bad_archive, mutate_manifest=wrong_declared_size)

    run_b = str(tmp_path / "runs" / f"target-{damage}")
    env_b = _env(tmp_path, f"target-{damage}")
    result = library_backup.import_library(bad_archive, run_dir=run_b, environ=env_b)
    assert result["originals"] == 1 and result["texts"] == 0
    assert result["issues"] == [{"kind": "original", "name": damaged_name, "reason_code": expected_reason}]
    rows = _snapshot(run_b, env_b)["tables"]["library_items"]
    assert [row["original_filename"] for row in rows] == [f"partial-{damage}-good.pdf"]


def test_import_skips_invalid_utf8_text_but_restores_healthy_original(tmp_path):
    run_a, env_a, _ref, _ = _seed(tmp_path, "utf8-source")
    archive = tmp_path / "utf8-source.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    bad_archive = tmp_path / "utf8-invalid.zip"
    invalid = b"\xff\xfe"

    def replace_text_blob(manifest, members):
        text_entry = manifest["texts"][0]
        old_member = f"blobs/{text_entry['blob']}"
        members.pop(old_member)
        text_entry["blob"] = hashlib.sha256(invalid).hexdigest()
        text_entry["size"] = len(invalid)
        manifest["blobs"] = sorted({entry["blob"] for entry in manifest["originals"] + manifest["texts"]})
        members[f"blobs/{text_entry['blob']}"] = invalid

    _rewrite_archive(archive, bad_archive, mutate_manifest=replace_text_blob)
    run_b = str(tmp_path / "runs" / "utf8-target")
    env_b = _env(tmp_path, "utf8-target")
    result = library_backup.import_library(bad_archive, run_dir=run_b, environ=env_b)
    assert result["originals"] == 1 and result["texts"] == 0
    original_text_digest = hashlib.sha256("A résumé with naïve Unicode text.\n".encode("utf-8")).hexdigest()[:16]
    assert result["issues"] == [{
        "kind": "text", "name": f"fulltext-text-{original_text_digest}",
        "reason_code": "unreadable",
    }]
    snapshot = _snapshot(run_b, env_b)
    assert len(snapshot["tables"]["library_items"]) == 1
    assert snapshot["tables"]["parsed_texts"] == []


def test_all_bad_import_returns_issues_without_connecting_or_mutating_store(tmp_path, monkeypatch):
    run_a, env_a, _ref, _ = _seed(tmp_path, "all-bad-source", original=True, text=False)
    archive = tmp_path / "all-bad-source.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        missing_member = f"blobs/{manifest['originals'][0]['blob']}"
    bad_archive = tmp_path / "all-bad-missing.zip"
    _rewrite_archive(archive, bad_archive, remove=[missing_member])

    run_b = str(tmp_path / "runs" / "all-bad-target")
    env_b = _env(tmp_path, "all-bad-target")
    calls = []
    monkeypatch.setattr(content_store, "_connect", lambda *a, **k: pytest.fail("all-bad input must not open the store"))
    result = library_backup.import_library(
        bad_archive, run_dir=run_b, environ=env_b,
        before_mutation=lambda: calls.append(True),
    )
    assert result["originals"] == result["texts"] == result["skipped"] == 0
    assert result["issues"] == [{"kind": "original", "name": "all-bad-source-paper.pdf", "reason_code": "missing"}]
    assert calls == []
    assert not Path(content_store.db_path(run_b, env_b)).exists()


def test_export_skips_corrupt_text_and_hash_named_truncated_original(tmp_path):
    run, env, ref, _saved = _seed(tmp_path, "corrupt-source")
    content_store.record_parsed_text(
        run, ref, "abstract", "user", "Healthy abstract remains exportable.",
        source_ref="healthy-abstract", supplied_by="user", file_format="txt", environ=env,
    )
    snapshot = _snapshot(run, env)
    root = Path(snapshot["storage_root"])
    original = snapshot["tables"]["library_items"][0]
    text = next(row for row in snapshot["tables"]["parsed_texts"] if row["tier"] == "fulltext")
    (root / original["stored_relpath"]).write_bytes(b"%PDF-1.7\ntruncated")
    (root / text["stored_relpath"]).write_bytes(b"corrupt parsed text")

    archive = tmp_path / "corrupt-export.zip"
    result = library_backup.export_library(archive, run_dir=run, environ=env)
    assert result["originals"] == 0 and result["texts"] == 1 and result["blobs"] == 1
    assert (result["fulltexts"], result["abstracts"], result["webtexts"]) == (0, 1, 0)
    assert {tuple(sorted(issue.items())) for issue in result["issues"]} == {
        tuple(sorted({"kind": "original", "name": "corrupt-source-paper.pdf", "reason_code": "damaged"}.items())),
        tuple(sorted({"kind": "text", "name": text["parsed_text_id"], "reason_code": "damaged"}.items())),
    }
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["originals"] == [] and len(manifest["texts"]) == 1 and manifest["works"]
    run_target = str(tmp_path / "runs" / "corrupt-target")
    env_target = _env(tmp_path, "corrupt-target")
    restored = library_backup.import_library(archive, run_dir=run_target, environ=env_target)
    assert restored["originals"] == 0 and restored["texts"] == 1


def test_expanded_entry_size_bound_counts_repeated_blobs_before_any_write(tmp_path, monkeypatch):
    raw = b"%PDF-1.7\nshared fixture bytes\n"
    assert len(raw) < 40 < 2 * len(raw)
    archive, digest_by_name = _two_original_backup(tmp_path, "shared-size", same_bytes=True)
    assert len(set(digest_by_name.values())) == 1
    run_b = str(tmp_path / "runs" / "expanded-size-target")
    env_b = _env(tmp_path, "expanded-size-target")
    monkeypatch.setattr(library_backup, "MAX_TOTAL", 40)
    monkeypatch.setattr(content_store, "_connect", lambda *a, **k: pytest.fail("size validation must happen before the store opens"))
    monkeypatch.setattr(library_backup.os, "link", lambda *a, **k: pytest.fail("size validation must happen before file promotion"))
    with pytest.raises(library_backup.LibraryBackupError, match="total restore size"):
        library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    assert not Path(content_store.db_path(run_b, env_b)).exists()


def test_import_before_mutation_hook_not_called_for_empty_archive(tmp_path, monkeypatch):
    archive = tmp_path / "empty-backup.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": library_backup.FORMAT, "version": library_backup.VERSION,
            "works": {}, "aliases": [], "originals": [], "texts": [], "blobs": [],
        }))
    run = str(tmp_path / "runs" / "empty-target")
    env = _env(tmp_path, "empty-target")
    calls = []
    monkeypatch.setattr(content_store, "_connect", lambda *a, **k: pytest.fail("empty import must not open the store"))
    result = library_backup.import_library(
        archive, run_dir=run, environ=env,
        before_mutation=lambda: calls.append(True),
    )
    assert result == {"originals": 0, "texts": 0, "skipped": 0, "skipped_originals": 0, "skipped_texts": 0, "issues": []}
    assert calls == []


def test_restore_does_not_need_to_write_in_the_backup_source_folder(tmp_path, monkeypatch):
    run_a, env_a, _, _ = _seed(tmp_path, "source", original=False)
    source_folder = tmp_path / "read-only-media"
    source_folder.mkdir()
    archive = source_folder / "backup.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    run_b = str(tmp_path / "runs" / "restored")
    env_b = _env(tmp_path, "restored")
    real_mkdtemp = library_backup.tempfile.mkdtemp

    def writable_staging(*args, **kwargs):
        assert Path(kwargs["dir"]).resolve() != source_folder.resolve()
        assert Path(kwargs["dir"]).resolve() == Path(content_store.storage_root(run_b, env_b)).resolve().parent
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(library_backup.tempfile, "mkdtemp", writable_staging)
    assert library_backup.import_library(archive, run_dir=run_b, environ=env_b)["texts"] == 1


def test_same_text_from_two_sources_is_deduplicated_before_file_promotion(tmp_path):
    run_a, env_a, ref, _ = _seed(tmp_path, "source", original=False, text=False)
    for source in ("source-one", "source-two"):
        content_store.record_parsed_text(
            run_a, ref, "abstract", "user", "Identical reusable abstract.",
            source_ref=source, supplied_by="user", file_format="txt", environ=env_a,
        )
    archive = tmp_path / "duplicate-texts.zip"
    exported = library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    assert exported["texts"] == 2
    assert (exported["fulltexts"], exported["abstracts"], exported["webtexts"]) == (0, 2, 0)
    run_b = str(tmp_path / "runs" / "restored")
    env_b = _env(tmp_path, "restored")
    result = library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    assert result["texts"] == 1 and result["skipped_texts"] == 1
    assert not result["issues"]
    assert len(_snapshot(run_b, env_b)["tables"]["parsed_texts"]) == 1


@pytest.mark.parametrize("failure", ["manifest-limit", "temp-creation"])
def test_export_cleans_snapshot_files_when_archive_preparation_fails(tmp_path, monkeypatch, failure):
    run, env, _, _ = _seed(tmp_path, "source")
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"previous backup")
    if failure == "manifest-limit":
        monkeypatch.setattr(library_backup, "MAX_MANIFEST", 1)
        expected = library_backup.LibraryBackupError
    else:
        def cannot_create(*args, **kwargs):
            raise OSError("disk full")
        monkeypatch.setattr(library_backup.tempfile, "mkstemp", cannot_create)
        expected = OSError
    with pytest.raises(expected):
        library_backup.export_library(archive, run_dir=run, environ=env)
    assert archive.read_bytes() == b"previous backup"
    assert not list(tmp_path.glob(".library-backup-stage-*"))


def test_restore_reactivates_logically_identical_text_across_crlf_and_lf(tmp_path):
    run_a, env_a, ref, _ = _seed(tmp_path, "source", original=False, text=False)
    text = "A portable abstract.\nSecond line.\n"
    saved = content_store.record_parsed_text(
        run_a, ref, "abstract", "user", text, supplied_by="user", environ=env_a,
    )
    Path(saved["stored_path"]).write_bytes(text.replace("\n", "\r\n").encode())
    archive = tmp_path / "windows-backup.zip"
    library_backup.export_library(archive, run_dir=run_a, environ=env_a)
    run_b, env_b, _, _ = _seed(tmp_path, "target", original=False, text=False)
    library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    row = _snapshot(run_b, env_b)["tables"]["parsed_texts"][0]
    target = Path(content_store.storage_root(run_b, env_b)) / row["stored_relpath"]
    target.write_bytes(text.encode())
    content_store.deactivate_parsed_texts(run_b, [row["parsed_text_id"]], environ=env_b)
    result = library_backup.import_library(archive, run_dir=run_b, environ=env_b)
    assert result["texts"] == 1 and not result["issues"]
    assert target.read_bytes() == text.encode()
    assert content_store.find_reusable_parsed_text(run_b, ref, tiers=("abstract",), environ=env_b) is not None
