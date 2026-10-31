# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Portable, additive backup and restore for the user's content library.

The archive carries active original files and parsed text with only the
identity and library metadata needed to recreate those entries. It deliberately
does not serialize the content-store database or review/evidence state.
"""

from __future__ import annotations

import hashlib
import codecs
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from core.fetch.storage import content_store


FORMAT = "callimachus-library-backup"
VERSION = 1
MAX_MANIFEST = 16 * 1024 * 1024
MAX_MEMBER = 512 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
MAX_DIRECTORY = 32 * 1024 * 1024
MAX_ORIGINALS = 25_000
MAX_TEXTS = 100_000
_SHA = re.compile(r"^[0-9a-f]{64}$")
_WORK_FIELDS = ("doi", "pmid", "isbn", "url", "title", "year", "first_author_surname", "raw_entry")
_EXT_FORMAT = {
    ".pdf": "pdf", ".docx": "docx", ".tex": "latex", ".txt": "txt",
    ".md": "markdown", ".markdown": "markdown",
    ".html": "html", ".htm": "html", ".xhtml": "html",
}
_FORMAT_EXT = {
    "pdf": ".pdf", "docx": ".docx", "latex": ".tex", "txt": ".txt",
    "markdown": ".md", "html": ".html",
}


class LibraryBackupError(ValueError):
    """Invalid, unsafe, or inconsistent library backup."""


class _LibraryFileProblem(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code


def _snapshot_file(source: Path, destination: Path) -> tuple[str, int]:
    """Copy one store file to a private snapshot while hashing that exact copy."""
    try:
        before = source.stat()
        if before.st_size > MAX_MEMBER:
            raise _LibraryFileProblem("too_large")
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as src, destination.open("xb") as dst:
            while chunk := src.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_MEMBER:
                    raise _LibraryFileProblem("too_large")
                digest.update(chunk)
                dst.write(chunk)
            dst.flush()
        after = source.stat()
    except _LibraryFileProblem:
        destination.unlink(missing_ok=True)
        raise
    except FileNotFoundError as exc:
        destination.unlink(missing_ok=True)
        raise _LibraryFileProblem("missing") from exc
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise _LibraryFileProblem("unreadable") from exc
    if size != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size, after.st_mtime_ns, after.st_ino
    ):
        destination.unlink(missing_ok=True)
        raise _LibraryFileProblem("damaged")
    return digest.hexdigest(), size


def _issue(kind: str, name: str, reason_code: str) -> dict[str, str]:
    return {"kind": kind, "name": name, "reason_code": reason_code}


def _check_directory(path: Path) -> None:
    # Bound the central-directory index before ZipFile allocates it. The same
    # stdlib EOCD reader used by ZipFile supports normal ZIP and ZIP64 files.
    with path.open("rb") as stream:
        end = zipfile._EndRecData(stream)
    if end is None:
        raise zipfile.BadZipFile("library archive has no ZIP directory")
    if end[zipfile._ECD_ENTRIES_TOTAL] > MAX_MEMBERS + 1 or end[zipfile._ECD_SIZE] > MAX_DIRECTORY:
        raise LibraryBackupError("library archive directory exceeds its size limit")


def _digest(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_MEMBER:
                raise LibraryBackupError("a library file exceeds the archive size limit")
            h.update(chunk)
    return h.hexdigest(), size


def _text_digest(path: Path) -> str:
    """Match the store's UTF-8/universal-newline checksum across platforms."""
    digest = hashlib.sha256()
    total = 0
    try:
        if path.stat().st_size > MAX_MEMBER:
            raise _LibraryFileProblem("too_large")
        with path.open("r", encoding="utf-8", newline=None) as stream:
            while text := stream.read(64 * 1024):
                chunk = text.encode("utf-8")
                total += len(chunk)
                if total > MAX_MEMBER:
                    raise _LibraryFileProblem("too_large")
                digest.update(chunk)
    except UnicodeError as exc:
        raise _LibraryFileProblem("damaged") from exc
    except OSError as exc:
        raise _LibraryFileProblem("unreadable") from exc
    return digest.hexdigest()


def _safe_store_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise LibraryBackupError("content store contains an invalid relative path")
    rel = PurePosixPath(relative)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise LibraryBackupError("content store contains an invalid relative path")
    target = root
    for part in rel.parts:
        target = target / part
        if target.is_symlink():
            raise LibraryBackupError("content store file cannot be reached through a symbolic link")
    try:
        if target.is_symlink() or not target.resolve(strict=True).is_relative_to(root.resolve()):
            raise LibraryBackupError("content store file escapes its storage directory")
    except FileNotFoundError as exc:
        raise LibraryBackupError("an active library file is missing") from exc
    if not target.is_file():
        raise LibraryBackupError("an active library entry is not a regular file")
    return target


def _new_store_target(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if not relative or "\\" in relative or rel.is_absolute() or any(p in ("", ".", "..") for p in rel.parts):
        raise LibraryBackupError("restore generated an invalid storage path")
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise LibraryBackupError("content store path contains a symbolic link")
        if current.exists() and not current.is_dir():
            raise LibraryBackupError("content store path is not a directory")
        if not current.resolve(strict=False).is_relative_to(root):
            raise LibraryBackupError("content store path escapes its storage directory")
    target = current / rel.parts[-1]
    if target.is_symlink():
        raise LibraryBackupError("content store destination is a symbolic link")
    return target


def _ensure_target_parent(root: Path, target: Path, created: list[Path]) -> None:
    relative = target.parent.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LibraryBackupError("content store path contains a symbolic link")
        if not current.exists():
            current.mkdir()
            created.append(current)
        elif not current.is_dir():
            raise LibraryBackupError("content store path is not a directory")
        if not current.resolve().is_relative_to(root):
            raise LibraryBackupError("content store path escapes its storage directory")


def _work_ref(row: sqlite3.Row) -> dict[str, Any]:
    result = {key: row[key] for key in _WORK_FIELDS if key != "raw_entry" and row[key] is not None}
    if row["normalized_raw_entry"]:
        result["raw_entry"] = row["normalized_raw_entry"]
    return result


def _portable_ref(ref: dict[str, Any]) -> dict[str, Any]:
    raw_entry = str(ref.get("raw_entry") or "")
    if re.search(r"(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+|/(?:Users|home|var)/)", raw_entry):
        raise LibraryBackupError("library metadata contains a local file path")
    url = str(ref.get("url") or "")
    if url:
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise LibraryBackupError("library metadata contains an invalid URL") from exc
        sensitive = {"token", "access_token", "api_key", "apikey", "key", "password", "passwd", "auth", "authorization", "signature", "sig"}
        if parts.scheme.casefold() == "file" or parts.username or parts.password or parts.fragment or any(k.casefold() in sensitive for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
            raise LibraryBackupError("library metadata contains an authenticated or local URL")
    return ref


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def export_library(
    path: str | os.PathLike[str], *, run_dir: str, environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Write active user originals and active parsed texts into a portable ZIP."""
    destination = Path(path).expanduser().absolute()
    root = Path(content_store.storage_root(run_dir, environ)).resolve()
    if destination.resolve(strict=False).is_relative_to(root):
        raise LibraryBackupError("backup destination cannot be inside the content store")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".library-backup-stage-", dir=destination.parent))
    db_file = Path(content_store.db_path(run_dir, environ)).resolve()
    conn: sqlite3.Connection | None = None
    blobs: dict[str, Path] = {}
    works: dict[str, dict[str, Any]] = {}
    originals: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    exported_originals: dict[str, tuple[str, dict[str, str]]] = {}
    total = 0
    try:
        if db_file.exists():
            content_store._inspect_existing_store(str(db_file))
            conn = sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
        if conn is not None:
            conn.execute("BEGIN")
        for table, kind in (("library_items", "original"), ("parsed_texts", "text")):
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE active = 1 ORDER BY {('library_item_id' if kind == 'original' else 'parsed_text_id')}"
            ).fetchall() if conn is not None else []
            for index, row in enumerate(rows):
                name = row["original_filename"] if kind == "original" else row["parsed_text_id"]
                safe_name = PurePosixPath(str(name or "library-entry").replace("\\", "/")).name[:256] or "library-entry"
                snapshot = staging / f"snapshot-{kind}-{index}"
                try:
                    if kind == "text" and row["missing"]:
                        raise _LibraryFileProblem("missing")
                    file_path = _safe_store_file(root, row["stored_relpath"])
                    digest, size = _snapshot_file(file_path, snapshot)
                    if kind == "text":
                        try:
                            text_value = snapshot.read_text(encoding="utf-8")
                        except UnicodeDecodeError as exc:
                            raise _LibraryFileProblem("unreadable") from exc
                        if hashlib.sha256(text_value.encode("utf-8")).hexdigest() != row["sha256"]:
                            raise _LibraryFileProblem("damaged")
                    if total + size > MAX_TOTAL:
                        raise _LibraryFileProblem("too_large")

                    work = conn.execute("SELECT * FROM works WHERE work_id = ?", (row["work_id"],)).fetchone()
                    if work is None:
                        raise _LibraryFileProblem("damaged")
                    try:
                        ref = _portable_ref(_work_ref(work))
                        identity_aliases = content_store.identity_aliases_for_ref(ref)
                    except (LibraryBackupError, TypeError, ValueError) as exc:
                        raise _LibraryFileProblem("damaged") from exc
                    canonical = (work["canonical_identity_scheme"], work["canonical_identity_key"])
                    if not identity_aliases or canonical not in identity_aliases:
                        raise _LibraryFileProblem("damaged")
                    work_key = work["canonical_identity_key"]

                    if kind == "original":
                        fmt = row["file_format"] or _EXT_FORMAT.get(file_path.suffix.lower())
                        filename = row["original_filename"]
                        if fmt not in _FORMAT_EXT or not isinstance(filename, str) or not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
                            raise _LibraryFileProblem("damaged")
                        stem = PurePosixPath(row["stored_relpath"]).stem
                        if _SHA.fullmatch(stem):
                            identity_keys = {work["canonical_identity_key"]}
                            identity_keys.update(alias[0] for alias in conn.execute(
                                "SELECT alias_key FROM work_aliases WHERE work_id = ?", (work["work_id"],),
                            ))
                            expected_pairs = {
                                hashlib.sha256(
                                    f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}{digest}".encode("ascii")
                                ).hexdigest()
                                for key in identity_keys
                            }
                            if stem not in expected_pairs:
                                raise _LibraryFileProblem("damaged")
                        entry = {
                            "work": work_key, "sha256": digest, "blob": digest, "size": size,
                            "filename": filename, "format": fmt,
                        }
                    else:
                        entry = {
                            "work": work_key, "sha256": row["sha256"], "blob": digest, "size": size,
                            "tier": row["tier"], "format": row["file_format"],
                            "content_version": row["content_version"], "original": None,
                        }
                        linked = exported_originals.get(row["library_item_id"] or "")
                        if linked is not None and linked[0] == work_key:
                            entry["original"] = linked[1]

                    total += size
                    if digest in blobs:
                        snapshot.unlink(missing_ok=True)
                    else:
                        blobs[digest] = snapshot
                    works[work_key] = {"identity": ref, "scheme": canonical[0], "key": canonical[1]}
                    if kind == "original":
                        originals.append(entry)
                        exported_originals[row["library_item_id"]] = (
                            work_key,
                            {"sha256": digest, "format": fmt, "filename": filename},
                        )
                    else:
                        texts.append(entry)
                except _LibraryFileProblem as exc:
                    issues.append(_issue(kind, safe_name, exc.reason_code))
                    snapshot.unlink(missing_ok=True)
                except LibraryBackupError as exc:
                    reason = "missing" if "missing" in str(exc).casefold() else "damaged"
                    issues.append(_issue(kind, safe_name, reason))
                    snapshot.unlink(missing_ok=True)
                except (OSError, UnicodeError) as exc:
                    reason = "missing" if isinstance(exc, FileNotFoundError) else "unreadable"
                    issues.append(_issue(kind, safe_name, reason))
                    snapshot.unlink(missing_ok=True)
        aliases = [
            {"work": key, "items": [[alias, scheme] for scheme, alias in content_store.identity_aliases_for_ref(entry["identity"])]}
            for key, entry in sorted(works.items())
        ]
        if len(originals) > MAX_ORIGINALS or len(texts) > MAX_TEXTS or len(blobs) > MAX_MEMBERS:
            raise LibraryBackupError("library contains too many entries for a portable backup")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if conn is not None:
            conn.close()

    try:
        manifest = {
            "format": FORMAT, "version": VERSION,
            "works": works, "aliases": aliases, "originals": originals, "texts": texts,
            "blobs": sorted(blobs),
        }
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_MANIFEST:
            raise LibraryBackupError("library manifest exceeds the archive size limit")
        fd, temp_name = tempfile.mkstemp(prefix=".library-backup-", suffix=".zip", dir=destination.parent)
        os.close(fd)
        temp = Path(temp_name)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("manifest.json", encoded)
            for digest, source in sorted(blobs.items()):
                written_hash = hashlib.sha256()
                written_size = 0
                with source.open("rb") as src, archive.open(f"blobs/{digest}", "w") as dst:
                    while chunk := src.read(1024 * 1024):
                        written_hash.update(chunk)
                        written_size += len(chunk)
                        if written_size > MAX_MEMBER:
                            raise LibraryBackupError("library file grew beyond the archive size limit")
                        dst.write(chunk)
                if written_hash.hexdigest() != digest or written_size > MAX_MEMBER:
                    raise LibraryBackupError("library file changed during backup")
        os.replace(temp, destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "originals": len(originals),
        "texts": len(texts),
        "fulltexts": sum(entry["tier"] == "fulltext" for entry in texts),
        "abstracts": sum(entry["tier"] == "abstract" for entry in texts),
        "webtexts": sum(entry["tier"] == "web" for entry in texts),
        "works": len(works),
        "blobs": len(blobs),
        "issues": issues,
    }


def _json_object(data: bytes) -> dict[str, Any]:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LibraryBackupError("library manifest contains duplicate JSON keys")
            result[key] = value
        return result
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LibraryBackupError("library manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise LibraryBackupError("library manifest must be an object")
    return value


def _validate_manifest(manifest: dict[str, Any], names: set[str]) -> dict[str, Any]:
    if set(manifest) != {"format", "version", "works", "aliases", "originals", "texts", "blobs"}:
        raise LibraryBackupError("library manifest has unsupported fields")
    if manifest["format"] != FORMAT or type(manifest["version"]) is not int or manifest["version"] != VERSION:
        raise LibraryBackupError("unsupported library backup format or version")
    works, aliases = manifest["works"], manifest["aliases"]
    originals, texts, blobs = manifest["originals"], manifest["texts"], manifest["blobs"]
    if not isinstance(works, dict) or not isinstance(aliases, list) or not isinstance(originals, list) or not isinstance(texts, list) or not isinstance(blobs, list):
        raise LibraryBackupError("library manifest collections are malformed")
    if len(originals) > MAX_ORIGINALS or len(texts) > MAX_TEXTS or len(blobs) > MAX_MEMBERS:
        raise LibraryBackupError("library backup contains too many entries")
    expected_blobs = set()
    original_refs: set[tuple[str, str, str, str]] = set()
    for key, entry in works.items():
        if not isinstance(key, str) or not isinstance(entry, dict) or set(entry) != {"identity", "scheme", "key"} or not isinstance(entry["identity"], dict):
            raise LibraryBackupError("library work metadata is malformed")
        ref = entry["identity"]
        if set(ref) - set(_WORK_FIELDS) or not ref or any(
            (field == "year" and value is not None and (not isinstance(value, (str, int)) or isinstance(value, bool))
             or (field != "year" and not isinstance(value, str))
            ) for field, value in ref.items()
        ):
            raise LibraryBackupError("library work identity contains unsupported values")
        ref = _portable_ref(ref)
        aliases_for_ref = content_store.identity_aliases_for_ref(ref)
        if not isinstance(entry["scheme"], str) or not isinstance(entry["key"], str) or entry["key"] != key or (entry["scheme"], key) not in aliases_for_ref:
            raise LibraryBackupError("library work identity does not match its canonical key")
    for kind, entries in (("original", originals), ("text", texts)):
        allowed = ({"work", "sha256", "blob", "size", "filename", "format"} if kind == "original" else {"work", "sha256", "blob", "size", "tier", "format", "content_version", "original"})
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != allowed or not isinstance(entry.get("work"), str) or entry["work"] not in works or not isinstance(entry.get("sha256"), str) or not _SHA.fullmatch(entry["sha256"]) or not isinstance(entry.get("blob"), str) or not _SHA.fullmatch(entry["blob"]):
                raise LibraryBackupError(f"library {kind} metadata is malformed")
            if not isinstance(entry["size"], int) or isinstance(entry["size"], bool) or entry["size"] < 0:
                raise LibraryBackupError(f"library {kind} size is invalid")
            if kind == "original":
                if not isinstance(entry["filename"], str) or not entry["filename"] or Path(entry["filename"]).name != entry["filename"] or not isinstance(entry["format"], str) or entry["format"] not in _FORMAT_EXT:
                    raise LibraryBackupError("library original filename or format is invalid")
                original_refs.add((entry["work"], entry["sha256"], entry["format"], entry["filename"]))
            else:
                if not isinstance(entry["tier"], str) or entry["tier"] not in content_store.TIERS or entry["format"] is not None and (not isinstance(entry["format"], str) or len(entry["format"]) > 64) or entry["content_version"] is not None and (not isinstance(entry["content_version"], str) or len(entry["content_version"]) > 256):
                    raise LibraryBackupError("parsed text metadata is invalid")
                original = entry["original"]
                if original is not None:
                    if (
                        not isinstance(original, dict)
                        or set(original) != {"sha256", "format", "filename"}
                        or not isinstance(original["sha256"], str)
                        or not _SHA.fullmatch(original["sha256"])
                        or not isinstance(original["format"], str)
                        or original["format"] not in _FORMAT_EXT
                        or not isinstance(original["filename"], str)
                        or not original["filename"]
                        or Path(original["filename"]).name != original["filename"]
                        or "/" in original["filename"]
                        or "\\" in original["filename"]
                        or (entry["work"], original["sha256"], original["format"], original["filename"]) not in original_refs
                    ):
                        raise LibraryBackupError("parsed text original reference is invalid")
            if kind == "original" and entry["sha256"] != entry["blob"]:
                raise LibraryBackupError("original content digest is inconsistent")
            expected_blobs.add(entry["blob"])
    if sum(entry["size"] for entry in originals + texts) > MAX_TOTAL:
        raise LibraryBackupError("library entries exceed the total restore size limit")
    if not isinstance(blobs, list) or any(not isinstance(x, str) or not _SHA.fullmatch(x) for x in blobs) or len(blobs) != len(set(blobs)) or set(blobs) != expected_blobs:
        raise LibraryBackupError("library blob list is inconsistent")
    allowed_names = {"manifest.json", *(f"blobs/{digest}" for digest in blobs)}
    if not names <= allowed_names:
        raise LibraryBackupError("library archive contains missing or unexpected members")
    if len(aliases) != len(works) or {entry.get("work") for entry in aliases if isinstance(entry, dict)} != set(works):
        raise LibraryBackupError("library aliases are incomplete")
    for entry in aliases:
        if not isinstance(entry, dict) or set(entry) != {"work", "items"} or not isinstance(entry.get("work"), str) or entry["work"] not in works or not isinstance(entry["items"], list):
            raise LibraryBackupError("library aliases are malformed")
        expected = [[alias, scheme] for scheme, alias in content_store.identity_aliases_for_ref(works[entry["work"]]["identity"])]
        if entry["items"] != expected:
            raise LibraryBackupError("library aliases do not match the work identity")
    return manifest


def import_library(
    path: str | os.PathLike[str], *, run_dir: str, environ: dict[str, str] | None = None,
    before_mutation: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Validate and add an archive's originals/texts without overwriting data."""
    archive_path = Path(path).expanduser().absolute()
    root = Path(content_store.storage_root(run_dir, environ)).resolve()
    store_staging: Path | None = None
    promoted: list[Path] = []
    created_dirs: list[Path] = []
    conn: sqlite3.Connection | None = None
    issues: list[dict[str, str]] = []

    def record_issue(kind: str, entry: dict[str, Any], reason: str) -> None:
        if kind == "original":
            name = PurePosixPath(entry["filename"].replace("\\", "/")).name[:256] or "original"
        else:
            name = f"{entry['tier']}-text-{entry['sha256'][:16]}"
        issues.append(_issue(kind, name, reason))

    try:
        try:
            _check_directory(archive_path)
            archive = zipfile.ZipFile(archive_path, "r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise LibraryBackupError("not a valid library backup ZIP") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS + 1:
                raise LibraryBackupError("library archive contains too many members")
            names: set[str] = set()
            encrypted_members: set[str] = set()
            for info in infos:
                name = info.filename
                posix = PurePosixPath(name)
                mode = (info.external_attr >> 16) & 0xFFFF
                if name in names or "\\" in name or posix.is_absolute() or any(p in ("", ".", "..") for p in posix.parts) or mode and (mode & 0o170000) == 0o120000:
                    raise LibraryBackupError("archive has duplicate or unsafe member paths")
                if info.flag_bits & 1:
                    if name == "manifest.json":
                        raise LibraryBackupError("library manifest is encrypted")
                    encrypted_members.add(name)
                if name == "manifest.json" and info.file_size > MAX_MANIFEST:
                    raise LibraryBackupError("library manifest exceeds the size limit")
                names.add(name)
            if "manifest.json" not in names:
                raise LibraryBackupError("library archive has no manifest")
            manifest_info = archive.getinfo("manifest.json")
            try:
                manifest_data = archive.read(manifest_info)
            except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError, RuntimeError) as exc:
                raise LibraryBackupError("library manifest is unreadable") from exc
            manifest = _validate_manifest(_json_object(manifest_data), names)
            if manifest["blobs"]:
                root.parent.mkdir(parents=True, exist_ok=True)
                store_staging = Path(tempfile.mkdtemp(prefix=".library-restore-", dir=root.parent))
            text_digests: set[str] = set()
            for entry in manifest["texts"]:
                text_digests.add(entry["blob"])
            text_char_counts: dict[str, int] = {}
            text_logical_digests: dict[str, str] = {}
            text_decode_errors: set[str] = set()
            blob_sizes: dict[str, int] = {}
            blob_errors: dict[str, str] = {
                digest: "unreadable"
                for digest in manifest["blobs"]
                if f"blobs/{digest}" in encrypted_members
            }
            blob_paths: dict[str, Path] = {}
            staged_total = 0
            for digest in manifest["blobs"]:
                if digest in blob_errors:
                    continue
                try:
                    info = archive.getinfo(f"blobs/{digest}")
                except KeyError:
                    blob_errors[digest] = "missing"
                    continue
                if info.file_size > MAX_MEMBER or staged_total + info.file_size > MAX_TOTAL:
                    blob_errors[digest] = "too_large"
                    continue
                target = store_staging / digest
                h = hashlib.sha256()
                size = 0
                logical = hashlib.sha256()
                char_count = 0
                decoder = codecs.getincrementaldecoder("utf-8")() if digest in text_digests else None
                decode_error = False
                pending_cr = False
                try:
                    with archive.open(info, "r") as src, target.open("xb") as dst:
                        while chunk := src.read(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_MEMBER:
                                raise _LibraryFileProblem("too_large")
                            h.update(chunk)
                            dst.write(chunk)
                            if decoder is not None and not decode_error:
                                try:
                                    decoded = decoder.decode(chunk)
                                except UnicodeDecodeError:
                                    decode_error = True
                                    decoder = None
                                    continue
                                if pending_cr:
                                    decoded = "\r" + decoded
                                    pending_cr = False
                                if decoded.endswith("\r"):
                                    decoded, pending_cr = decoded[:-1], True
                                normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
                                char_count += len(normalized)
                                logical.update(normalized.encode("utf-8"))
                        if decoder is not None and not decode_error:
                            try:
                                decoded = decoder.decode(b"", final=True)
                            except UnicodeDecodeError:
                                decode_error = True
                            else:
                                if pending_cr:
                                    decoded = "\r" + decoded
                                normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
                                char_count += len(normalized)
                                logical.update(normalized.encode("utf-8"))
                    if size != info.file_size or h.hexdigest() != digest:
                        raise _LibraryFileProblem("damaged")
                except _LibraryFileProblem as exc:
                    target.unlink(missing_ok=True)
                    blob_errors[digest] = exc.reason_code
                    continue
                except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError, RuntimeError) as exc:
                    target.unlink(missing_ok=True)
                    blob_errors[digest] = (
                        "missing" if isinstance(exc, FileNotFoundError)
                        else "unreadable" if isinstance(exc, (NotImplementedError, RuntimeError))
                        else "damaged"
                    )
                    continue
                blob_sizes[digest] = size
                blob_paths[digest] = target
                staged_total += size
                if digest in text_digests:
                    if decode_error:
                        text_decode_errors.add(digest)
                    else:
                        text_logical_digests[digest] = logical.hexdigest()
                        text_char_counts[digest] = char_count

            valid_originals: list[dict[str, Any]] = []
            for entry in manifest["originals"]:
                reason = blob_errors.get(entry["blob"])
                if reason is None and entry["size"] > MAX_MEMBER:
                    reason = "too_large"
                if reason is None and entry["size"] != blob_sizes[entry["blob"]]:
                    reason = "damaged"
                if reason:
                    record_issue("original", entry, reason)
                else:
                    valid_originals.append(entry)

            valid_texts: list[dict[str, Any]] = []
            for entry in manifest["texts"]:
                reason = blob_errors.get(entry["blob"])
                if reason is None and entry["size"] > MAX_MEMBER:
                    reason = "too_large"
                if reason is None and entry["size"] != blob_sizes[entry["blob"]]:
                    reason = "damaged"
                if reason is None and entry["blob"] in text_decode_errors:
                    reason = "unreadable"
                if reason is None and (
                    text_char_counts.get(entry["blob"], 0) == 0
                    or text_logical_digests.get(entry["blob"]) != entry["sha256"]
                ):
                    reason = "damaged"
                if reason:
                    record_issue("text", entry, reason)
                else:
                    valid_texts.append(entry)

            if not valid_originals and not valid_texts:
                return {
                    "originals": 0, "texts": 0, "skipped": 0,
                    "skipped_originals": 0, "skipped_texts": 0, "issues": issues,
                }

            used_works = {entry["work"] for entry in valid_originals + valid_texts}
            manifest = {
                **manifest,
                "works": {key: value for key, value in manifest["works"].items() if key in used_works},
                "aliases": [entry for entry in manifest["aliases"] if entry["work"] in used_works],
                "originals": valid_originals,
                "texts": valid_texts,
                "blobs": sorted({entry["blob"] for entry in valid_originals + valid_texts}),
            }

        if before_mutation is not None:
            before_mutation()
        conn = content_store._connect(run_dir, environ)
        conn.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        work_ids: dict[str, str] = {}
        skipped_originals = skipped_texts = 0

        # Resolve or create works through stable aliases. Existing rows are left byte-for-byte intact.
        for key, entry in manifest["works"].items():
            ref = entry["identity"]
            aliases = content_store.identity_aliases_for_ref(ref)
            primary_scheme, primary_key = entry["scheme"], entry["key"]
            primary = conn.execute("SELECT work_id FROM work_aliases WHERE alias_key = ?", (primary_key,)).fetchone()
            if primary:
                work_id = primary[0]
            else:
                work_id = "work-" + hashlib.sha256(primary_key.encode("utf-8")).hexdigest()[:20]
                snap = content_store._work_snapshot(ref)
                collision = conn.execute("SELECT work_id, canonical_identity_key FROM works WHERE work_id = ? OR canonical_identity_key = ?", (work_id, primary_key)).fetchone()
                if collision:
                    if collision["canonical_identity_key"] != primary_key:
                        raise LibraryBackupError("library work identity collides with an existing key")
                    work_id = collision[0]
                else:
                    conn.execute(
                        """INSERT INTO works(work_id, canonical_identity_key, canonical_identity_scheme, doi, pmid, isbn, url,
                        normalized_title, normalized_author_year, normalized_raw_entry, title, year, first_author_surname,
                        created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (work_id, primary_key, primary_scheme, snap["doi"], snap["pmid"], snap["isbn"], snap["url"],
                         snap["normalized_title"], snap["normalized_author_year"], snap["normalized_raw_entry"], snap["title"],
                         snap["year"], snap["first_author_surname"], now, now),
                    )
            work_ids[key] = work_id
            for scheme, alias in aliases:
                # Match content_store._upsert_work: weaker secondary aliases
                # already owned by another DOI/work remain attached there.
                conn.execute("INSERT OR IGNORE INTO work_aliases(alias_key, work_id, alias_scheme, created_at) VALUES(?,?,?,?)", (alias, work_id, scheme, now))

        item_ids: dict[tuple[str, str, str, str], str] = {}
        file_records: list[tuple[Path, Path]] = []
        for entry in manifest["originals"]:
            work_id = work_ids[entry["work"]]
            identity_row = conn.execute("SELECT canonical_identity_key FROM works WHERE work_id = ?", (work_id,)).fetchone()
            if identity_row is None:
                raise LibraryBackupError("library work disappeared during restore")
            identity = identity_row["canonical_identity_key"]
            source_extension = PurePosixPath(entry["filename"]).suffix.lower()
            if _EXT_FORMAT.get(source_extension) != entry["format"]:
                source_extension = _FORMAT_EXT[entry["format"]]
            category, _fmt = content_store._infer_file_category("x" + source_extension, entry["format"])
            identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            pair = hashlib.sha256(f"{identity_hash}{entry['sha256']}".encode("ascii")).hexdigest()
            extension = content_store._user_library_extension("original" + source_extension, entry["format"])
            relpath = f"user_library/{category}/{pair}{extension}"
            item_id = "lib-" + hashlib.sha256(f"{work_id}|{relpath}".encode("utf-8")).hexdigest()[:20]
            existing = conn.execute("SELECT * FROM library_items WHERE work_id = ? AND stored_relpath = ?", (work_id, relpath)).fetchone()
            target = _new_store_target(root, relpath)
            if existing:
                if target.is_file() and _digest(target)[0] == entry["sha256"]:
                    item_ids[(entry["work"], entry["sha256"], entry["format"], entry["filename"])] = existing["library_item_id"]
                    skipped_originals += 1
                else:
                    skipped_originals += 1
                continue
            if conn.execute("SELECT 1 FROM library_items WHERE stored_relpath = ? OR library_item_id = ?", (relpath, item_id)).fetchone():
                skipped_originals += 1
                continue
            if target.exists():
                if not target.is_file() or _digest(target)[0] != entry["sha256"]:
                    skipped_originals += 1
                    continue
            else:
                file_records.append((blob_paths[entry["sha256"]], target))
            conn.execute(
                """INSERT INTO library_items(library_item_id, work_id, stored_relpath, original_filename, file_format,
                supplied_by, supplied_via, moved_from_path, created_at, updated_at, active)
                VALUES(?,?,?,?,?,'user','library_backup_import',NULL,?,?,1)""",
                (item_id, work_id, relpath, entry["filename"], entry["format"], now, now),
            )
            item_ids[(entry["work"], entry["sha256"], entry["format"], entry["filename"])] = item_id

        # Add parsed text only when the exact same content identity is not already present.
        restored_text_ids: set[str] = set()
        for entry in manifest["texts"]:
            work_id = work_ids[entry["work"]]
            source_ref = f"library-backup:{entry['sha256']}"
            content_version = entry["content_version"]
            parsed_id = "txt-" + hashlib.sha256(f"{work_id}|{entry['tier']}|user|{source_ref}|{content_version or ''}".encode("utf-8")).hexdigest()[:20]
            present = conn.execute("SELECT * FROM parsed_texts WHERE parsed_text_id = ?", (parsed_id,)).fetchone()
            if present is not None and (
                present["work_id"] != work_id
                or present["tier"] != entry["tier"]
                or present["sha256"] != entry["sha256"]
                or present["source_ref"] != source_ref
                or present["file_format"] != entry["format"]
                or present["content_version"] != content_version
            ):
                skipped_texts += 1
                continue
            if present is None:
                present = conn.execute(
                    "SELECT * FROM parsed_texts WHERE work_id = ? AND tier = ? AND sha256 = ? AND active = 1 ORDER BY updated_at DESC, parsed_text_id DESC LIMIT 1",
                    (work_id, entry["tier"], entry["sha256"]),
                ).fetchone()
            if present is None:
                present = conn.execute(
                    """SELECT * FROM parsed_texts
                       WHERE work_id = ? AND tier = ? AND sha256 = ?
                         AND file_format IS ? AND content_version IS ?
                       ORDER BY updated_at DESC, parsed_text_id DESC LIMIT 1""",
                    (work_id, entry["tier"], entry["sha256"], entry["format"], content_version),
                ).fetchone()
            if present:
                if present["parsed_text_id"] in restored_text_ids:
                    skipped_texts += 1
                    continue
                target = _new_store_target(root, present["stored_relpath"])
                if target.exists():
                    problem = "damaged"
                    try:
                        matching_text = _text_digest(target) == entry["sha256"]
                    except _LibraryFileProblem as exc:
                        matching_text = False
                        problem = exc.reason_code
                    if not matching_text:
                        # Existing bytes are user data. Do not replace a file that
                        # does not match the verified logical text.
                        record_issue("text", entry, problem)
                        skipped_texts += 1
                        continue
                    if present["active"] and not present["missing"]:
                        skipped_texts += 1
                        continue
                else:
                    file_records.append((blob_paths[entry["blob"]], target))
                conn.execute(
                    "UPDATE parsed_texts SET active = 1, missing = 0, last_verified_at = ?, updated_at = ? WHERE parsed_text_id = ?",
                    (now, now, present["parsed_text_id"]),
                )
                restored_text_ids.add(present["parsed_text_id"])
                continue
            category, fmt = content_store._infer_file_category(None, entry["format"])
            # Entry paths use our own content-addressed filename and never source paths.
            filename = f"{parsed_id}.txt"
            relpath = f"parsed_cache/{entry['tier']}/{filename}"
            target = _new_store_target(root, relpath)
            if conn.execute("SELECT 1 FROM parsed_texts WHERE stored_relpath = ?", (relpath,)).fetchone():
                skipped_texts += 1
                continue
            if target.exists():
                if not target.is_file() or hashlib.sha256(target.read_text(encoding="utf-8").encode("utf-8")).hexdigest() != entry["sha256"]:
                    skipped_texts += 1
                    continue
            else:
                file_records.append((blob_paths[entry["blob"]], target))
            original = entry["original"]
            linked_item = item_ids.get((entry["work"], original["sha256"], original["format"], original["filename"])) if original else None
            conn.execute(
                """INSERT INTO parsed_texts(parsed_text_id, work_id, tier, origin, acquisition_mode, supplied_by, supplied_via,
                file_format, source_ref, stored_relpath, sha256, char_count, content_version, provenance_relation,
                extraction_method, preparation_version, preparation_before_chars, preparation_after_chars, mapping,
                match_signal, match_score, identity_status, identity_note, library_item_id, active, missing,
                created_at, last_verified_at, updated_at)
                VALUES(?,?,?,'user','user_supplied','user','library_backup_import',?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,
                NULL,NULL,'user_library_reviewed','Restored from a user-selected library backup; source verification was not carried over.',?,1,0,?,?,?)""",
                (parsed_id, work_id, entry["tier"], fmt, source_ref, relpath, entry["sha256"], text_char_counts[entry["blob"]],
                 content_version, linked_item, now, now, now),
            )
            restored_text_ids.add(parsed_id)
        for staged_file, target in file_records:
            _ensure_target_parent(root, target, created_dirs)
            try:
                os.link(staged_file, target)
            except FileExistsError:
                raise
            except OSError:
                # Some supported Windows filesystems or sandbox policies deny
                # hard links. Exclusive creation still guarantees no overwrite.
                with target.open("xb") as output:
                    promoted.append(target)
                    with staged_file.open("rb") as source:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
            else:
                promoted.append(target)
        conn.commit()
        return {"originals": len(manifest["originals"]) - skipped_originals, "texts": len(manifest["texts"]) - skipped_texts,
                "skipped": skipped_originals + skipped_texts, "skipped_originals": skipped_originals,
                "skipped_texts": skipped_texts, "issues": issues}
    except Exception:
        if conn is not None:
            conn.rollback()
        for path_to_remove in reversed(promoted):
            try:
                path_to_remove.unlink(missing_ok=True)
            except OSError:
                pass
        for path_to_remove in reversed(created_dirs):
            try:
                path_to_remove.rmdir()
            except OSError:
                pass
        raise
    finally:
        if conn is not None:
            conn.close()
        if store_staging is not None:
            shutil.rmtree(store_staging, ignore_errors=True)
