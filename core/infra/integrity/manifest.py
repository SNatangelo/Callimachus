# core/infra/integrity/manifest.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical logical manifests for run and content-store artifacts.

SQLite file bytes are deliberately not hashed: page layout, WAL state, and
vacuum history are storage details rather than the logical audit contract.  A
stable projection of every non-volatile table is combined with a byte inventory
of the files surrounding the database.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from core.shared.typed_canonical import encode as typed_canonical


MANIFEST_VERSION = "callimachus.artifact-manifest.v1"

_RUN_EXCLUDED_TABLES = frozenset({
    "run_sessions",
    "artifact_checkpoints",
    "artifact_checkpoint_entries",
    "artifact_integrity_violations",
    "artifact_integrity_violation_entries",
    "artifact_integrity_overrides",
    "artifact_crash_recoveries",
    "artifact_crash_recovery_entries",
})
_CONTENT_STORE_EXCLUDED_TABLES = frozenset({
    "artifact_checkpoints",
    "artifact_checkpoint_entries",
    "artifact_integrity_violations",
    "artifact_integrity_violation_entries",
    "artifact_integrity_overrides",
    "artifact_crash_recoveries",
    "artifact_crash_recovery_entries",
})
_RUN_EXCLUDED_COLUMNS = MappingProxyType({"run": frozenset({"updated_at"})})


class ManifestError(ValueError):
    """The logical database or filesystem inventory cannot be trusted."""


@dataclass(frozen=True, order=True)
class ArtifactEntry:
    logical_path: str
    sha256: str
    byte_count: int

    def payload(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True)
class ArtifactManifest:
    scope: str
    database_filename: str
    database_projection_sha256: str
    database_tables: tuple[Mapping[str, Any], ...]
    files: tuple[ArtifactEntry, ...]
    manifest_sha256: str

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "version": MANIFEST_VERSION,
            "scope": self.scope,
            "database": {
                "filename": self.database_filename,
                "projection_sha256": self.database_projection_sha256,
                "tables": [dict(table) for table in self.database_tables],
            },
            "files": [entry.payload() for entry in self.files],
        }

    def payload(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "manifest_sha256": self.manifest_sha256}


@dataclass(frozen=True)
class ManifestDiff:
    database_changed: bool
    missing_files: tuple[str, ...]
    unexpected_files: tuple[str, ...]
    changed_files: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not (
            self.database_changed
            or self.missing_files
            or self.unexpected_files
            or self.changed_files
        )

    def payload(self) -> dict[str, Any]:
        return {
            "database_changed": self.database_changed,
            "missing_files": list(self.missing_files),
            "unexpected_files": list(self.unexpected_files),
            "changed_files": list(self.changed_files),
        }


def _validate_label(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ManifestError(f"{name} must be non-empty text without NUL")
    return value.strip()


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _normalise_sql_value(value: Any) -> Any:
    if value is None or type(value) in (int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ManifestError("SQLite projection contains a non-finite float")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"sqlite_blob_hex": bytes(value).hex()}
    raise ManifestError(
        f"SQLite projection contains unsupported value type: {type(value).__name__}"
    )


def _database_projection(
    connection,
    *,
    excluded_tables: frozenset[str],
    excluded_columns: Mapping[str, frozenset[str]],
) -> tuple[tuple[Mapping[str, Any], ...], str]:
    schema_rows = connection.execute(
        """
        SELECT name, sql
        FROM main.sqlite_schema
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    tables: list[Mapping[str, Any]] = []
    for schema_row in schema_rows:
        table_name = str(schema_row[0])
        if table_name in excluded_tables:
            continue
        schema_sql = schema_row[1]
        if not isinstance(schema_sql, str) or not schema_sql:
            raise ManifestError(f"table {table_name!r} has no canonical schema SQL")
        pragma = connection.execute(
            f"PRAGMA main.table_xinfo({_quote_identifier(table_name)})"
        ).fetchall()
        dropped = excluded_columns.get(table_name, frozenset())
        columns = [str(row[1]) for row in pragma if str(row[1]) not in dropped]
        if not columns:
            raise ManifestError(f"table {table_name!r} has no projected columns")
        select_columns = ", ".join(_quote_identifier(column) for column in columns)
        raw_rows = connection.execute(
            f"SELECT {select_columns} FROM main.{_quote_identifier(table_name)}"
        ).fetchall()
        rows = [
            [_normalise_sql_value(value) for value in tuple(raw_row)]
            for raw_row in raw_rows
        ]
        rows.sort(key=typed_canonical)
        table_payload = {
            "name": table_name,
            "schema_sql_sha256": hashlib.sha256(
                typed_canonical(schema_sql)
            ).hexdigest(),
            "columns": columns,
            "row_count": len(rows),
            "rows_sha256": hashlib.sha256(typed_canonical(rows)).hexdigest(),
        }
        tables.append(MappingProxyType(table_payload))
    projection_payload = {"tables": [dict(table) for table in tables]}
    projection_sha256 = hashlib.sha256(
        typed_canonical(projection_payload)
    ).hexdigest()
    return tuple(tables), projection_sha256


def _hash_regular_file(path: str) -> tuple[str, int]:
    if os.path.islink(path):
        raise ManifestError(f"artifact path is a symlink: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ManifestError(f"artifact is unreadable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ManifestError(f"artifact is not a regular file: {path}")
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            byte_count += len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    if stable_identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or byte_count != after.st_size:
        raise ManifestError(f"artifact changed while it was being hashed: {path}")
    return digest.hexdigest(), byte_count


def _file_inventory(
    artifact_root: str,
    *,
    ignored_relative_paths: frozenset[str],
) -> tuple[ArtifactEntry, ...]:
    root = os.path.abspath(artifact_root)
    if os.path.islink(root) or not os.path.isdir(root):
        raise ManifestError("artifact root must be a real directory")
    entries: list[ArtifactEntry] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        for directory_name in directory_names:
            directory_path = os.path.join(current, directory_name)
            if os.path.islink(directory_path):
                raise ManifestError(f"artifact directory is a symlink: {directory_path}")
        for file_name in file_names:
            path = os.path.join(current, file_name)
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if relative in ignored_relative_paths:
                continue
            sha256, byte_count = _hash_regular_file(path)
            entries.append(ArtifactEntry(relative, sha256, byte_count))
    entries.sort()
    return tuple(entries)


def _build_manifest(
    connection,
    *,
    artifact_root: str,
    database_filename: str,
    scope: str,
    excluded_tables: frozenset[str],
    excluded_columns: Mapping[str, frozenset[str]],
    ignored_paths: Iterable[str],
) -> ArtifactManifest:
    clean_scope = _validate_label(scope, "scope")
    clean_database_filename = _validate_label(database_filename, "database_filename")
    if os.path.basename(clean_database_filename) != clean_database_filename:
        raise ManifestError("database_filename must be a basename")
    database_path = os.path.join(os.path.abspath(artifact_root), clean_database_filename)
    if os.path.islink(database_path) or not os.path.isfile(database_path):
        raise ManifestError("database must be a regular, non-symlink file in artifact root")
    database_rows = connection.execute("PRAGMA database_list").fetchall()
    main_rows = [row for row in database_rows if str(row[1]) == "main"]
    attached = [
        str(row[1])
        for row in database_rows
        if str(row[1]) not in ("main", "temp") and str(row[2] or "")
    ]
    if len(main_rows) != 1 or not str(main_rows[0][2] or ""):
        raise ManifestError("manifest connection has no file-backed main database")
    if attached:
        raise ManifestError("manifest connection must not contain attached databases")
    if os.path.realpath(str(main_rows[0][2])) != os.path.realpath(database_path):
        raise ManifestError("manifest connection does not match the database in artifact root")
    tables, database_projection_sha256 = _database_projection(
        connection,
        excluded_tables=excluded_tables,
        excluded_columns=excluded_columns,
    )
    ignored = frozenset({
        clean_database_filename,
        f"{clean_database_filename}-wal",
        f"{clean_database_filename}-shm",
        f"{clean_database_filename}-journal",
        *ignored_paths,
    })
    files = _file_inventory(
        artifact_root,
        ignored_relative_paths=ignored,
    )
    unsigned_payload = {
        "version": MANIFEST_VERSION,
        "scope": clean_scope,
        "database": {
            "filename": clean_database_filename,
            "projection_sha256": database_projection_sha256,
            "tables": [dict(table) for table in tables],
        },
        "files": [entry.payload() for entry in files],
    }
    manifest_sha256 = hashlib.sha256(typed_canonical(unsigned_payload)).hexdigest()
    return ArtifactManifest(
        scope=clean_scope,
        database_filename=clean_database_filename,
        database_projection_sha256=database_projection_sha256,
        database_tables=tables,
        files=files,
        manifest_sha256=manifest_sha256,
    )


def build_run_manifest(connection, run_dir: str) -> ArtifactManifest:
    """Build the current logical manifest for a schema-current run."""
    return _build_manifest(
        connection,
        artifact_root=run_dir,
        database_filename="run.sqlite",
        scope="run",
        excluded_tables=_RUN_EXCLUDED_TABLES,
        excluded_columns=_RUN_EXCLUDED_COLUMNS,
        ignored_paths=(".run.lock",),
    )


def build_content_store_manifest(connection, storage_root: str) -> ArtifactManifest:
    """Build the current logical manifest for the shared content store."""
    return _build_manifest(
        connection,
        artifact_root=storage_root,
        database_filename="content.sqlite",
        scope="content_store",
        excluded_tables=_CONTENT_STORE_EXCLUDED_TABLES,
        excluded_columns=MappingProxyType({}),
        ignored_paths=(".content-store-bootstrap.lock",),
    )


def compare_manifests(
    expected: ArtifactManifest,
    observed: ArtifactManifest,
) -> ManifestDiff:
    if expected.scope != observed.scope:
        raise ManifestError("cannot compare manifests from different scopes")
    expected_files = {entry.logical_path: entry for entry in expected.files}
    observed_files = {entry.logical_path: entry for entry in observed.files}
    shared_paths = expected_files.keys() & observed_files.keys()
    return ManifestDiff(
        database_changed=(
            expected.database_projection_sha256
            != observed.database_projection_sha256
        ),
        missing_files=tuple(sorted(expected_files.keys() - observed_files.keys())),
        unexpected_files=tuple(sorted(observed_files.keys() - expected_files.keys())),
        changed_files=tuple(sorted(
            path
            for path in shared_paths
            if expected_files[path] != observed_files[path]
        )),
    )
