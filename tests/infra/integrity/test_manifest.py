# tests/infra/integrity/test_manifest.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from core.infra.integrity import (
    ManifestError,
    build_content_store_manifest,
    build_run_manifest,
    compare_manifests,
)
from core.shared.filesystem_paths import io_path


def _run_database(root, rows):
    connection = sqlite3.connect(root / "run.sqlite")
    connection.executescript(
        """
        CREATE TABLE run(
          run_id TEXT PRIMARY KEY,
          phase TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE run_sessions(
          session_id TEXT PRIMARY KEY,
          heartbeat_at TEXT NOT NULL
        );
        CREATE TABLE source_texts(
          source_text_id TEXT PRIMARY KEY,
          stored_path TEXT NOT NULL,
          sha256 TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO run VALUES('run-1','fetch','2026-08-22T00:00:00Z')"
    )
    connection.execute(
        "INSERT INTO run_sessions VALUES('session-1','2026-08-22T00:00:00Z')"
    )
    for row in rows:
        connection.execute("INSERT INTO source_texts VALUES(?,?,?)", row)
    connection.commit()
    return connection


def test_run_manifest_is_stable_across_row_order_root_and_volatile_updates(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "sources").mkdir()
    (second_root / "sources").mkdir()
    (first_root / "sources" / "a.txt").write_text("alpha", encoding="utf-8")
    (second_root / "sources" / "a.txt").write_text("alpha", encoding="utf-8")
    rows = [
        ("source-b", "sources/b.txt", "b" * 64),
        ("source-a", "sources/a.txt", hashlib.sha256(b"alpha").hexdigest()),
    ]
    first = _run_database(first_root, rows)
    second = _run_database(second_root, reversed(rows))
    try:
        first_manifest = build_run_manifest(first, str(first_root))
        second.execute(
            "UPDATE run SET updated_at='2026-08-22T01:00:00Z'"
        )
        second.execute(
            "UPDATE run_sessions SET heartbeat_at='2026-08-22T01:00:00Z'"
        )
        second.commit()
        second_manifest = build_run_manifest(second, str(second_root))
    finally:
        first.close()
        second.close()
    assert first_manifest.manifest_sha256 == second_manifest.manifest_sha256
    assert first_manifest.payload()["version"] == "callimachus.artifact-manifest.v1"


def test_manifest_detects_file_and_database_changed_together(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "sources").mkdir()
    source = root / "sources" / "source.txt"
    source.write_text("original", encoding="utf-8")
    connection = _run_database(root, [
        (
            "source-1",
            "sources/source.txt",
            hashlib.sha256(b"original").hexdigest(),
        )
    ])
    try:
        expected = build_run_manifest(connection, str(root))
        source.write_text("altered", encoding="utf-8")
        connection.execute(
            "UPDATE source_texts SET sha256=? WHERE source_text_id='source-1'",
            (hashlib.sha256(b"altered").hexdigest(),),
        )
        connection.commit()
        observed = build_run_manifest(connection, str(root))
    finally:
        connection.close()
    difference = compare_manifests(expected, observed)
    assert not difference.clean
    assert difference.database_changed
    assert difference.changed_files == ("sources/source.txt",)


def test_content_store_manifest_covers_cached_objects(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    cache = root / "parsed_cache"
    cache.mkdir()
    cached_file = cache / "work.txt"
    cached_file.write_text("cached", encoding="utf-8")
    connection = sqlite3.connect(root / "content.sqlite")
    connection.execute(
        "CREATE TABLE parsed_texts(parsed_text_id TEXT PRIMARY KEY, stored_relpath TEXT)"
    )
    connection.execute("INSERT INTO parsed_texts VALUES('p1','parsed_cache/work.txt')")
    connection.commit()
    try:
        expected = build_content_store_manifest(connection, str(root))
        cached_file.write_text("changed", encoding="utf-8")
        observed = build_content_store_manifest(connection, str(root))
    finally:
        connection.close()
    difference = compare_manifests(expected, observed)
    assert not difference.clean
    assert not difference.database_changed
    assert difference.changed_files == ("parsed_cache/work.txt",)


def test_manifest_rejects_symlinked_artifact(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    connection = _run_database(root, [])
    target = root / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = root / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        connection.close()
        pytest.skip("symlinks are unavailable")
    try:
        with pytest.raises(ManifestError, match="symlink"):
            build_run_manifest(connection, str(root))
    finally:
        connection.close()


def test_manifest_rejects_connection_to_different_database(tmp_path):
    root = tmp_path / "run"
    other_root = tmp_path / "other"
    root.mkdir()
    other_root.mkdir()
    expected_connection = _run_database(root, [])
    wrong_connection = _run_database(other_root, [])
    expected_connection.close()
    try:
        with pytest.raises(ManifestError, match="does not match"):
            build_run_manifest(wrong_connection, str(root))
    finally:
        wrong_connection.close()


def test_run_manifest_inventories_long_guided_ocr_paths_unchanged(tmp_path):
    root = tmp_path / "long-run"
    Path(io_path(root)).mkdir(parents=True, exist_ok=True)
    while len(str(root)) < 180:
        remaining = 180 - len(str(root)) - 1
        component_length = max(1, min(80, remaining))
        root = root / ("r" + "x" * (component_length - 1))
        Path(io_path(root)).mkdir(parents=True, exist_ok=True)

    scan_id = "a" * 64
    text_sha256 = hashlib.sha256(b"guided OCR text bytes").hexdigest()
    relative = f"sources/guided_ocr/{scan_id}-{text_sha256}.txt"
    artifact = root.joinpath(*relative.split("/"))
    artifact.parent.mkdir(parents=True, exist_ok=True)
    data = b"guided OCR text bytes"
    with open(io_path(artifact), "wb") as handle:
        handle.write(data)
    connection = _run_database(root, [])
    try:
        manifest = build_run_manifest(connection, str(root))
    finally:
        connection.close()
        shutil.rmtree(io_path(root))

    assert len(str(artifact)) > 260
    assert [
        (entry.logical_path, entry.sha256, entry.byte_count)
        for entry in manifest.files
    ] == [(relative, hashlib.sha256(data).hexdigest(), len(data))]


def test_run_manifest_hashes_binary_bytes_through_windows_crt(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    data = b"first line\r\n\x1a bytes after ctrl-z"
    with open(io_path(root / "binary.dat"), "wb") as handle:
        handle.write(data)
    connection = _run_database(root, [])
    try:
        manifest = build_run_manifest(connection, str(root))
    finally:
        connection.close()

    assert [(entry.logical_path, entry.sha256, entry.byte_count) for entry in manifest.files] == [
        ("binary.dat", hashlib.sha256(data).hexdigest(), len(data)),
    ]
