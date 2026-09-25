#!/usr/bin/env python3
# core/fetch/storage/content_store.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
content_store.py - persistent cross-run content store.

This module keeps a global SQLite index plus filesystem-backed caches for:
  - user-supplied original files (library)
  - parsed source texts (full text / abstract / web)

Run-local `sources/` remain the audit record; this store lets a later run reuse
already-identified content before going back to the network.
"""

from __future__ import annotations

import functools
import contextlib
import hashlib
import os
import re
import shutil
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


ENV_STATE_DIR = "CITATION_VERIFIER_STATE_DIR"
DB_FILENAME = "content.sqlite"
STORE_SUBDIR = "content_store"
_BOOTSTRAP_LOCK_FILENAME = ".content-store-bootstrap.lock"
_BOOTSTRAP_PROCESS_LOCK = threading.RLock()
_EXTERNAL_CACHE_SUBDIR = "external_cache"
_DOCUMENTED_CATALOG_SIBLING_FILES = frozenset(
    name
    for stem in ("resolver-coverage.sqlite",)
    for name in (stem, f"{stem}-wal", f"{stem}-shm")
)
_DOCUMENTED_CATALOG_SIBLING_DIRS = frozenset({"journal-authority"})
USER_LIBRARY_SUBDIR = "user_library"
PARSED_CACHE_SUBDIR = "parsed_cache"
QUARANTINE_SUBDIR = "quarantine"
SCHEMA_VERSION = 4

TIERS = ("fulltext", "abstract", "web")
TIER_RANK = {"web": 1, "abstract": 2, "fulltext": 3}
IDENTITY_SCHEMES = ("doi", "pmid", "isbn", "url", "title_author_year", "raw_entry")
_WORD = re.compile(r"[a-z0-9]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DOI_URL_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.I)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS works (
  work_id TEXT PRIMARY KEY,
  canonical_identity_key TEXT NOT NULL UNIQUE,
  canonical_identity_scheme TEXT NOT NULL,
  doi TEXT,
  pmid TEXT,
  isbn TEXT,
  url TEXT,
  normalized_title TEXT,
  normalized_author_year TEXT,
  normalized_raw_entry TEXT,
  title TEXT,
  year INTEGER,
  first_author_surname TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_aliases (
  alias_key TEXT PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
  alias_scheme TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_aliases_work_id
ON work_aliases(work_id);

CREATE TABLE IF NOT EXISTS library_items (
  library_item_id TEXT PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
  stored_relpath TEXT NOT NULL UNIQUE,
  original_filename TEXT NOT NULL,
  file_format TEXT,
  supplied_by TEXT NOT NULL,
  supplied_via TEXT NOT NULL,
  moved_from_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  CHECK (active IN (0,1))
);

CREATE INDEX IF NOT EXISTS idx_library_items_work_id
ON library_items(work_id);

CREATE TABLE IF NOT EXISTS parsed_texts (
  parsed_text_id TEXT PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
  tier TEXT NOT NULL,
  origin TEXT NOT NULL,
  acquisition_mode TEXT NOT NULL,
  supplied_by TEXT NOT NULL,
  supplied_via TEXT NOT NULL,
  file_format TEXT,
  source_ref TEXT,
  stored_relpath TEXT NOT NULL UNIQUE,
  sha256 TEXT NOT NULL,
  char_count INTEGER NOT NULL,
  content_version TEXT,
  provenance_relation TEXT,
  extraction_method TEXT,
  preparation_version TEXT,
  preparation_before_chars INTEGER,
  preparation_after_chars INTEGER,
  mapping TEXT,
  match_signal TEXT,
  match_score REAL,
  identity_status TEXT,
  identity_note TEXT,
  library_item_id TEXT REFERENCES library_items(library_item_id),
  active INTEGER NOT NULL DEFAULT 1,
  missing INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_verified_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (tier IN ('fulltext','abstract','web')),
  CHECK (active IN (0,1)),
  CHECK (missing IN (0,1))
);

CREATE INDEX IF NOT EXISTS idx_parsed_texts_work_id
ON parsed_texts(work_id);

CREATE INDEX IF NOT EXISTS idx_parsed_texts_lookup
ON parsed_texts(work_id, tier, active, missing);

CREATE TABLE IF NOT EXISTS parsed_text_extraction_flags (
  parsed_text_id TEXT NOT NULL REFERENCES parsed_texts(parsed_text_id) ON DELETE CASCADE,
  flag TEXT NOT NULL CHECK (flag = trim(flag) AND length(flag) > 0),
  PRIMARY KEY (parsed_text_id, flag)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS parsed_text_preparation_flags (
  parsed_text_id TEXT NOT NULL REFERENCES parsed_texts(parsed_text_id) ON DELETE CASCADE,
  flag TEXT NOT NULL CHECK (flag = trim(flag) AND length(flag) > 0),
  PRIMARY KEY (parsed_text_id, flag)
) WITHOUT ROWID;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text or "")
        if not unicodedata.combining(ch)
    )


def _norm_text(text: str | None) -> str:
    return _NON_ALNUM.sub(" ", _strip_accents((text or "").strip()).lower()).strip()


def _slug(text: str | None, *, max_len: int = 48) -> str:
    parts = _WORD.findall(_norm_text(text))
    if not parts:
        return "item"
    out = "_".join(parts[:8])
    return out[:max_len] or "item"


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name or "file")


def _canon_doi(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    raw = _DOI_URL_RE.sub("", raw).strip()
    return raw.lower() or None


def _canon_pmid(value: str | None) -> str | None:
    raw = re.sub(r"\D+", "", str(value or ""))
    return raw or None


def _canon_isbn(value: str | None) -> str | None:
    raw = re.sub(r"[^0-9Xx]", "", str(value or ""))
    compact = raw.upper()
    if len(compact) == 10:
        try:
            digits = [10 if char == "X" else int(char) for char in compact]
        except ValueError:
            return None
        if "X" in compact[:-1] or sum(
            (10 - index) * digit for index, digit in enumerate(digits)
        ) % 11:
            return None
        first_twelve = "978" + compact[:9]
        total = sum(
            (1 if index % 2 == 0 else 3) * int(char)
            for index, char in enumerate(first_twelve)
        )
        return first_twelve + str((10 - total % 10) % 10)
    if len(compact) == 13 and compact.isdigit():
        total = sum(
            (1 if index % 2 == 0 else 3) * int(char)
            for index, char in enumerate(compact[:12])
        )
        return compact if str((10 - total % 10) % 10) == compact[-1] else None
    return None


def _canon_url(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw.lower()
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or ""
    while path.endswith("/") and path != "/":
        path = path[:-1]
    query = parsed.query or ""
    norm = f"{parsed.scheme.lower()}://{host}{path}"
    if query:
        norm += f"?{query}"
    return norm or raw.lower()


def _year_int(value: Any) -> int | None:
    try:
        year = int(value)
    except Exception:
        return None
    return year if 0 < year < 10000 else None


def _surname(ref: dict) -> str | None:
    for key in ("ay_surname", "surname", "first_author_surname"):
        raw = _norm_text(ref.get(key))
        if raw:
            return raw.split(" ", 1)[0]
    raw_entry = str(ref.get("raw_entry") or "")
    if not raw_entry:
        return None
    clean = re.sub(r"[{}]", "", raw_entry)
    clean = re.sub(r"\\[a-zA-Z]+\s*\{\}", "", clean)
    clean = re.sub(r"\\[a-zA-Z]+", "", clean)
    title = str(ref.get("title") or "").strip()
    if title:
        title_pattern = re.escape(title)
        title_pattern = re.sub(r"\\\s+", r"\\s+", title_pattern)
        m_title = re.search(title_pattern, clean, flags=re.IGNORECASE)
        if m_title and m_title.start() > 0:
            clean = clean[:m_title.start()].strip(" .")
    first_author = re.split(r"\s+(?:and|&)\s+|,|;", clean, maxsplit=1)[0].strip()
    tokens = re.findall(r"[A-Za-z][A-Za-z'’.-]*", first_author)
    if len(tokens) >= 2:
        if re.fullmatch(r"[A-Z]{2,4}", tokens[1]):
            return _norm_text(tokens[0])
        return _norm_text(tokens[-1])
    return _norm_text(tokens[0]) if tokens else None


def _title_author_year_key(ref: dict) -> str | None:
    title = _norm_text(ref.get("title"))
    year = _year_int(ref.get("year") or ref.get("ay_year"))
    sur = _surname(ref)
    if not title or not year or not sur:
        return None
    return f"{title}|{sur}|{year}"


def _raw_entry_key(ref: dict) -> str | None:
    raw = _norm_text(ref.get("raw_entry"))
    return raw or None


def identity_aliases_for_ref(ref: dict | None) -> list[tuple[str, str]]:
    if not ref:
        return []
    out: list[tuple[str, str]] = []
    doi = _canon_doi(ref.get("doi"))
    pmid = _canon_pmid(ref.get("pmid"))
    isbn = _canon_isbn(ref.get("isbn"))
    url = _canon_url(ref.get("url"))
    tay = _title_author_year_key(ref)
    raw = _raw_entry_key(ref)
    if doi:
        out.append(("doi", f"doi:{doi}"))
    if pmid:
        out.append(("pmid", f"pmid:{pmid}"))
    if isbn:
        out.append(("isbn", f"isbn:{isbn}"))
    if url:
        out.append(("url", f"url:{url}"))
    if tay:
        out.append(("title_author_year", f"title_author_year:{tay}"))
    if raw:
        out.append(("raw_entry", f"raw_entry:{raw}"))
    return out


def _primary_identity(ref: dict) -> tuple[str, str] | None:
    aliases = identity_aliases_for_ref(ref)
    return aliases[0] if aliases else None


def _repo_root_from_run(run_dir: str | None) -> str:
    if not run_dir:
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    here = os.path.abspath(run_dir)
    parent = os.path.dirname(here)
    if os.path.basename(parent).lower() == "runs":
        return os.path.dirname(parent)
    return parent


def storage_root(run_dir: str | None = None, environ: dict[str, str] | None = None) -> str:
    env = environ or os.environ
    override = (env.get(ENV_STATE_DIR) or "").strip()
    if override:
        return os.path.join(os.path.abspath(override), STORE_SUBDIR)
    return os.path.join(_repo_root_from_run(run_dir), "storage")


def db_path(run_dir: str | None = None, environ: dict[str, str] | None = None) -> str:
    return os.path.join(storage_root(run_dir, environ), DB_FILENAME)


def user_library_dir(
    run_dir: str | None = None,
    category: str | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    base = os.path.join(storage_root(run_dir, environ), USER_LIBRARY_SUBDIR)
    return os.path.join(base, category) if category else base


def parsed_cache_dir(
    run_dir: str | None = None,
    tier: str | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    base = os.path.join(storage_root(run_dir, environ), PARSED_CACHE_SUBDIR)
    return os.path.join(base, tier) if tier else base


def quarantine_dir(run_dir: str | None = None, environ: dict[str, str] | None = None) -> str:
    return os.path.join(storage_root(run_dir, environ), QUARANTINE_SUBDIR)


_V4_PARSED_TEXT_COLUMNS = {
    "parsed_text_id", "work_id", "tier", "origin", "acquisition_mode", "supplied_by",
    "supplied_via", "file_format", "source_ref", "stored_relpath", "sha256", "char_count",
    "content_version", "provenance_relation", "extraction_method", "preparation_version",
    "preparation_before_chars", "preparation_after_chars", "mapping", "match_signal",
    "match_score", "identity_status", "identity_note", "library_item_id", "active", "missing",
    "created_at", "last_verified_at", "updated_at",
}


def _normalized_schema_sql(value: str) -> str:
    return " ".join(value.split())


def _schema_signature(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {
        (row[0], row[1]): _normalized_schema_sql(row[2])
        for row in conn.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
            """
        )
    }


@functools.lru_cache(maxsize=1)
def _expected_v4_schema_signature() -> dict[tuple[str, str], str]:
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(SCHEMA_SQL)
        return _schema_signature(expected)
    finally:
        expected.close()


def _v4_schema_is_valid(conn: sqlite3.Connection) -> bool:
    required_tables = {
        "meta", "works", "work_aliases", "library_items", "parsed_texts",
        "parsed_text_extraction_flags", "parsed_text_preparation_flags",
    }
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if tables != required_tables:
        return False
    if _schema_signature(conn) != _expected_v4_schema_signature():
        return False
    required_columns = {
        "meta": {"key", "value"},
        "works": {"work_id", "canonical_identity_key", "canonical_identity_scheme", "doi", "pmid", "isbn", "url", "normalized_title", "normalized_author_year", "normalized_raw_entry", "title", "year", "first_author_surname", "created_at", "updated_at"},
        "work_aliases": {"alias_key", "work_id", "alias_scheme", "created_at"},
        "library_items": {"library_item_id", "work_id", "stored_relpath", "original_filename", "file_format", "supplied_by", "supplied_via", "moved_from_path", "created_at", "updated_at", "active"},
        "parsed_texts": _V4_PARSED_TEXT_COLUMNS,
        "parsed_text_extraction_flags": {"parsed_text_id", "flag"},
        "parsed_text_preparation_flags": {"parsed_text_id", "flag"},
    }
    for table, columns in required_columns.items():
        if {row[1] for row in conn.execute(f"PRAGMA table_info({table})")} != columns:
            return False
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(parsed_texts)")}
    if not {"idx_parsed_texts_work_id", "idx_parsed_texts_lookup"} <= indexes:
        return False
    expected_foreign_keys = {
        "work_aliases": {("work_id", "works", "work_id")},
        "library_items": {("work_id", "works", "work_id")},
        "parsed_texts": {
            ("work_id", "works", "work_id"),
            ("library_item_id", "library_items", "library_item_id"),
        },
        "parsed_text_extraction_flags": {("parsed_text_id", "parsed_texts", "parsed_text_id")},
        "parsed_text_preparation_flags": {("parsed_text_id", "parsed_texts", "parsed_text_id")},
    }
    for table, expected in expected_foreign_keys.items():
        actual = {
            (row[3], row[2], row[4]) for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
        if actual != expected:
            return False
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return False
    return version is not None and version[0] == str(SCHEMA_VERSION)


def _inspect_existing_store(path: str) -> None:
    """Reject anything other than an intact v4 database without opening it writable."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("content store integrity check failed")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("content store foreign key check failed")
            if not _v4_schema_is_valid(conn):
                raise ValueError("content store is not an intact v4 store")
        finally:
            conn.close()
    except (sqlite3.DatabaseError, OSError) as exc:
        raise ValueError("content store is malformed") from exc


def _probe_existing_schema_version(path: str) -> str | None:
    """Strict read-only probe, including a committed WAL during bootstrap."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()
    except (sqlite3.DatabaseError, OSError) as exc:
        raise ValueError("content store is malformed") from exc


@contextlib.contextmanager
def _bootstrap_lock(root: str):
    """Serialize fresh-store bootstrap; the lock file is not store content."""
    lock_path = os.path.join(root, _BOOTSTRAP_LOCK_FILENAME)
    with _BOOTSTRAP_PROCESS_LOCK:
        with open(lock_path, "a+", encoding="utf-8") as handle:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except ImportError:
                # The process lock above serializes same-process callers on
                # non-POSIX platforms. SQLite remains responsible for any
                # concurrent process.
                pass
            try:
                yield
            finally:
                try:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except ImportError:
                    pass


def _connect(run_dir: str | None = None, environ: dict[str, str] | None = None) -> sqlite3.Connection:
    root = storage_root(run_dir, environ)
    path = db_path(run_dir, environ)
    if os.path.exists(root) and not os.path.isdir(root):
        raise ValueError("content store root is not a directory")
    os.makedirs(root, exist_ok=True)
    with _bootstrap_lock(root):
        entries = [
            name
            for name in os.listdir(root)
            if name != _BOOTSTRAP_LOCK_FILENAME
            and not (
                name == _EXTERNAL_CACHE_SUBDIR
                and os.path.isdir(os.path.join(root, name))
            )
            and not (
                name in _DOCUMENTED_CATALOG_SIBLING_FILES
                and os.path.isfile(os.path.join(root, name))
                and not os.path.islink(os.path.join(root, name))
            )
            and not (
                name in _DOCUMENTED_CATALOG_SIBLING_DIRS
                and os.path.isdir(os.path.join(root, name))
                and not os.path.islink(os.path.join(root, name))
            )
        ]
        fresh = not entries
        if not fresh:
            if not os.path.isfile(path):
                raise ValueError("content store database is missing")
            if _probe_existing_schema_version(path) != str(SCHEMA_VERSION):
                raise ValueError("content store is not an intact v4 store")
            _inspect_existing_store(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 8000")
        if fresh:
            try:
                _bootstrap_schema(conn)
            except Exception:
                conn.close()
                raise
    return conn


def preflight(
    run_dir: str | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    """Validate or bootstrap the selected store before pipeline state mutates."""
    conn = _connect(run_dir, environ)
    try:
        return db_path(run_dir, environ)
    finally:
        conn.close()


def _bootstrap_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO meta(key, value) VALUES('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(SCHEMA_VERSION),),
        )


def _infer_file_category(path: str | None, explicit_format: str | None = None) -> tuple[str, str | None]:
    if explicit_format:
        fmt = explicit_format.lower()
    else:
        ext = os.path.splitext(path or "")[1].lower()
        fmt = {
            ".pdf": "pdf",
            ".docx": "docx",
            ".tex": "latex",
            ".txt": "txt",
            ".md": "markdown",
            ".markdown": "markdown",
        }.get(ext)
    category = {
        "pdf": "pdf",
        "docx": "docx",
        "latex": "latex",
        "txt": "txt",
        "markdown": "txt",
    }.get(fmt or "", "other")
    return category, fmt


def _work_snapshot(ref: dict | None) -> dict[str, Any]:
    ref = ref or {}
    return {
        "doi": _canon_doi(ref.get("doi")),
        "pmid": _canon_pmid(ref.get("pmid")),
        "isbn": _canon_isbn(ref.get("isbn")),
        "url": _canon_url(ref.get("url")),
        "normalized_title": _norm_text(ref.get("title")),
        "normalized_author_year": _title_author_year_key(ref),
        "normalized_raw_entry": _raw_entry_key(ref),
        "title": (ref.get("title") or None),
        "year": _year_int(ref.get("year") or ref.get("ay_year")),
        "first_author_surname": _surname(ref),
    }


def _upsert_work(conn: sqlite3.Connection, ref: dict) -> tuple[str, str]:
    primary = _primary_identity(ref)
    if primary is None:
        raise ValueError("reference has no stable identity aliases")
    primary_scheme, primary_key = primary
    now = _now()
    aliases = identity_aliases_for_ref(ref)
    snap = _work_snapshot(ref)
    row = conn.execute(
        """
        SELECT w.*
        FROM work_aliases wa
        JOIN works w ON w.work_id = wa.work_id
        WHERE wa.alias_key = ?
        LIMIT 1
        """,
        (primary_key,),
    ).fetchone()
    if row is None:
        work_id = "work-" + hashlib.sha256(primary_key.encode("utf-8")).hexdigest()[:20]
        with conn:
            conn.execute(
                """
                INSERT INTO works(
                  work_id, canonical_identity_key, canonical_identity_scheme,
                  doi, pmid, isbn, url, normalized_title, normalized_author_year,
                  normalized_raw_entry, title, year, first_author_surname,
                  created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work_id,
                    primary_key,
                    primary_scheme,
                    snap["doi"],
                    snap["pmid"],
                    snap["isbn"],
                    snap["url"],
                    snap["normalized_title"],
                    snap["normalized_author_year"],
                    snap["normalized_raw_entry"],
                    snap["title"],
                    snap["year"],
                    snap["first_author_surname"],
                    now,
                    now,
                ),
            )
    else:
        work_id = row["work_id"]
        with conn:
            conn.execute(
                """
                UPDATE works
                SET canonical_identity_key = ?,
                    canonical_identity_scheme = ?,
                    doi = COALESCE(?, doi),
                    pmid = COALESCE(?, pmid),
                    isbn = COALESCE(?, isbn),
                    url = COALESCE(?, url),
                    normalized_title = COALESCE(?, normalized_title),
                    normalized_author_year = COALESCE(?, normalized_author_year),
                    normalized_raw_entry = COALESCE(?, normalized_raw_entry),
                    title = COALESCE(?, title),
                    year = COALESCE(?, year),
                    first_author_surname = COALESCE(?, first_author_surname),
                    updated_at = ?
                WHERE work_id = ?
                """,
                (
                    primary_key,
                    primary_scheme,
                    snap["doi"],
                    snap["pmid"],
                    snap["isbn"],
                    snap["url"],
                    snap["normalized_title"],
                    snap["normalized_author_year"],
                    snap["normalized_raw_entry"],
                    snap["title"],
                    snap["year"],
                    snap["first_author_surname"],
                    now,
                    work_id,
                ),
            )
    with conn:
        for scheme, alias_key in aliases:
            conn.execute(
                """
                INSERT OR IGNORE INTO work_aliases(alias_key, work_id, alias_scheme, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (alias_key, work_id, scheme, now),
            )
    return work_id, primary_key


def archive_user_original(
    run_dir: str,
    src_path: str,
    *,
    ref: dict,
    supplied_via: str,
    file_format: str | None = None,
    move: bool = True,
) -> dict | None:
    if not src_path or not os.path.exists(src_path):
        return None
    if _primary_identity(ref) is None:
        return None
    conn = _connect(run_dir)
    try:
        work_id, identity_key = _upsert_work(conn, ref)
        category, fmt = _infer_file_category(src_path, file_format)
        lib_root = user_library_dir(run_dir, category)
        os.makedirs(lib_root, exist_ok=True)
        abs_src = os.path.abspath(src_path)
        try:
            common = os.path.commonpath([abs_src, lib_root])
        except ValueError:
            common = ""
        if common == os.path.abspath(lib_root):
            dest_path = abs_src
        else:
            stem = _slug(identity_key, max_len=24)
            base = _safe_filename(os.path.basename(src_path))
            dest_name = f"{stem}_{base}"
            dest_path = os.path.join(lib_root, dest_name)
            if not os.path.exists(dest_path):
                if move:
                    shutil.move(abs_src, dest_path)
                else:
                    shutil.copyfile(abs_src, dest_path)
        relpath = os.path.relpath(dest_path, storage_root(run_dir)).replace("\\", "/")
        item_id = "lib-" + hashlib.sha256(
            f"{work_id}|{relpath}".encode("utf-8")
        ).hexdigest()[:20]
        now = _now()
        with conn:
            conn.execute(
                """
                INSERT INTO library_items(
                  library_item_id, work_id, stored_relpath, original_filename, file_format,
                  supplied_by, supplied_via, moved_from_path, created_at, updated_at, active
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(library_item_id) DO UPDATE SET
                  work_id = excluded.work_id,
                  file_format = excluded.file_format,
                  supplied_via = excluded.supplied_via,
                  updated_at = excluded.updated_at,
                  active = 1
                """,
                (
                    item_id,
                    work_id,
                    relpath,
                    os.path.basename(src_path),
                    fmt,
                    "user",
                    supplied_via,
                    abs_src,
                    now,
                    now,
                ),
            )
        return {
            "library_item_id": item_id,
            "work_id": work_id,
            "identity_key": identity_key,
            "stored_relpath": relpath,
            "stored_path": dest_path,
            "file_format": fmt,
        }
    finally:
        conn.close()


def _normalized_flags(value: list[str] | None, name: str) -> list[str] | None:
    """Return deterministic flags; an absent or empty list preserves stored flags."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of strings")
    if not value:
        return None
    normalized: list[str] = []
    for flag in value:
        if not isinstance(flag, str):
            raise ValueError(f"{name} must contain only strings")
        flag = unicodedata.normalize("NFC", flag).strip()
        if not flag:
            raise ValueError(f"{name} must contain nonempty strings")
        normalized.append(flag)
    return sorted(set(normalized))


def _replace_flags(
    conn: sqlite3.Connection,
    table: str,
    parsed_text_id: str,
    flags: list[str] | None,
) -> None:
    if flags is None:
        return
    conn.execute(f"DELETE FROM {table} WHERE parsed_text_id = ?", (parsed_text_id,))
    conn.executemany(
        f"INSERT INTO {table}(parsed_text_id, flag) VALUES(?, ?)",
        ((parsed_text_id, flag) for flag in flags),
    )


def _acquisition_mode(supplied_by: str | None, origin: str) -> str:
    producer = supplied_by or ("user" if origin in ("user", "ocr") else "script")
    if producer == "user":
        return "user_supplied"
    producer_class = producer.split(":", 1)[0]
    if producer_class in {"operator", "agent", "automation"}:
        return f"{producer_class}_supplied"
    return "script_recorded"


def record_parsed_text(
    run_dir: str,
    ref: dict,
    tier: str,
    origin: str,
    text: str,
    *,
    source_ref: str | None = None,
    mapping: str | None = None,
    signal: str | None = None,
    score: float | None = None,
    identity_status: str | None = None,
    identity_note: str | None = None,
    content_version: str | None = None,
    provenance_relation: str | None = None,
    supplied_by: str | None = None,
    supplied_via: str | None = None,
    file_format: str | None = None,
    library_item_id: str | None = None,
    extraction_method: str | None = None,
    extraction_flags: list[str] | None = None,
    preparation: dict | None = None,
) -> dict | None:
    if tier not in TIERS:
        raise ValueError(f"unsupported tier: {tier}")
    if not text:
        return None
    if _primary_identity(ref) is None:
        return None
    extraction_flags = _normalized_flags(extraction_flags, "extraction_flags")
    if preparation is not None and not isinstance(preparation, dict):
        raise ValueError("preparation must be a dict or None")
    preparation = preparation or {}
    preparation_flags = _normalized_flags(
        preparation.get("preparation_flags") if "preparation_flags" in preparation else None,
        "preparation_flags",
    )
    conn = _connect(run_dir)
    try:
        work_id, identity_key = _upsert_work(conn, ref)
        sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        category, fmt = _infer_file_category(source_ref, file_format)
        cache_root = parsed_cache_dir(run_dir, tier)
        os.makedirs(cache_root, exist_ok=True)
        content_id = "txt-" + hashlib.sha256(
            f"{work_id}|{tier}|{origin}|{source_ref or ''}|{content_version or ''}".encode("utf-8")
        ).hexdigest()[:20]
        stem = _slug(identity_key, max_len=28)
        cache_name = f"{stem}_{content_id}.txt"
        dest_path = os.path.join(cache_root, cache_name)
        if not os.path.exists(dest_path):
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(text)
        else:
            with open(dest_path, encoding="utf-8", errors="replace") as f:
                existing = f.read()
            if hashlib.sha256(existing.encode("utf-8")).hexdigest() != sha256:
                with open(dest_path, "w", encoding="utf-8") as f:
                    f.write(text)
        relpath = os.path.relpath(dest_path, storage_root(run_dir)).replace("\\", "/")
        now = _now()
        preparation_version = str(preparation.get("preparation_version") or "") or None
        before_chars = preparation.get("before_bibliography_char_count")
        after_chars = preparation.get("prepared_char_count")
        with conn:
            conn.execute(
                """
                INSERT INTO parsed_texts(
                  parsed_text_id, work_id, tier, origin, acquisition_mode, supplied_by,
                  supplied_via, file_format, source_ref, stored_relpath, sha256, char_count,
                  content_version, provenance_relation, extraction_method,
                  preparation_version, preparation_before_chars,
                  preparation_after_chars, mapping, match_signal, match_score, identity_status,
                  identity_note, library_item_id, active, missing, created_at,
                  last_verified_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?)
                ON CONFLICT(parsed_text_id) DO UPDATE SET
                  origin = excluded.origin,
                  acquisition_mode = excluded.acquisition_mode,
                  supplied_by = excluded.supplied_by,
                  supplied_via = excluded.supplied_via,
                  file_format = excluded.file_format,
                  source_ref = excluded.source_ref,
                  stored_relpath = excluded.stored_relpath,
                  sha256 = excluded.sha256,
                  char_count = excluded.char_count,
                  content_version = excluded.content_version,
                  provenance_relation = excluded.provenance_relation,
                  extraction_method = COALESCE(excluded.extraction_method, parsed_texts.extraction_method),
                  preparation_version = COALESCE(excluded.preparation_version, parsed_texts.preparation_version),
                  preparation_before_chars = COALESCE(excluded.preparation_before_chars, parsed_texts.preparation_before_chars),
                  preparation_after_chars = COALESCE(excluded.preparation_after_chars, parsed_texts.preparation_after_chars),
                  mapping = excluded.mapping,
                  match_signal = excluded.match_signal,
                  match_score = excluded.match_score,
                  identity_status = excluded.identity_status,
                  identity_note = excluded.identity_note,
                  library_item_id = COALESCE(excluded.library_item_id, parsed_texts.library_item_id),
                  active = 1,
                  missing = 0,
                  last_verified_at = excluded.last_verified_at,
                  updated_at = excluded.updated_at
                """,
                (
                    content_id,
                    work_id,
                    tier,
                    origin,
                    _acquisition_mode(supplied_by, origin),
                    supplied_by or ("user" if origin in ("user", "ocr") else "script"),
                    supplied_via or (f"{origin}_{tier}"),
                    fmt,
                    source_ref,
                    relpath,
                    sha256,
                    len(text),
                    content_version,
                    provenance_relation,
                    extraction_method,
                    preparation_version,
                    int(before_chars) if isinstance(before_chars, int) else None,
                    int(after_chars) if isinstance(after_chars, int) else None,
                    mapping,
                    signal,
                    score,
                    identity_status,
                    identity_note,
                    library_item_id,
                    now,
                    now,
                    now,
                ),
            )
            _replace_flags(conn, "parsed_text_extraction_flags", content_id, extraction_flags)
            _replace_flags(conn, "parsed_text_preparation_flags", content_id, preparation_flags)
        return {
            "parsed_text_id": content_id,
            "work_id": work_id,
            "identity_key": identity_key,
            "stored_relpath": relpath,
            "stored_path": dest_path,
        }
    finally:
        conn.close()


def deactivate_parsed_texts(
    run_dir: str,
    parsed_text_ids: list[str] | tuple[str, ...] | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> int:
    """Remove parsed texts from cross-run reuse without deleting audit material.

    Passing None deactivates every active parsed-text row. Supplying identifiers
    limits the operation to those rows. Cached files and user-library originals
    remain in the integrity-protected store; existing runs therefore keep their
    provenance and a later maintenance operation may compact files under a
    separate, crash-safe contract.
    """
    normalized: tuple[str, ...] | None
    if parsed_text_ids is None:
        normalized = None
    else:
        values = []
        for value in parsed_text_ids:
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise ValueError(
                    "parsed text identifiers must be non-empty NUL-free strings"
                )
            values.append(value.strip())
        normalized = tuple(dict.fromkeys(values))
        if not normalized:
            return 0

    conn = _connect(run_dir, environ)
    try:
        now = _now()
        with conn:
            if normalized is None:
                cursor = conn.execute(
                    """
                    UPDATE parsed_texts
                    SET active = 0, updated_at = ?
                    WHERE active = 1
                    """,
                    (now,),
                )
            else:
                placeholders = ",".join("?" for _ in normalized)
                cursor = conn.execute(
                    f"""
                    UPDATE parsed_texts
                    SET active = 0, updated_at = ?
                    WHERE active = 1
                      AND parsed_text_id IN ({placeholders})
                    """,
                    (now, *normalized),
                )
        return max(0, int(cursor.rowcount))
    finally:
        conn.close()


def _tier_case(column: str = "pt.tier") -> str:
    return (
        f"CASE {column} "
        "WHEN 'fulltext' THEN 3 "
        "WHEN 'abstract' THEN 2 "
        "WHEN 'web' THEN 1 "
        "ELSE 0 END"
    )


def _mark_missing(conn: sqlite3.Connection, parsed_text_id: str) -> None:
    with conn:
        conn.execute(
            """
            UPDATE parsed_texts
            SET active = 0, missing = 1, updated_at = ?, last_verified_at = ?
            WHERE parsed_text_id = ?
            """,
            (_now(), _now(), parsed_text_id),
        )


def _mark_stale_preparation(conn: sqlite3.Connection, parsed_text_id: str) -> None:
    """Stop reusing full text not produced by the current preparation contract."""
    with conn:
        conn.execute(
            """
            UPDATE parsed_texts
            SET active = 0, updated_at = ?, last_verified_at = ?
            WHERE parsed_text_id = ?
            """,
            (_now(), _now(), parsed_text_id),
        )


def _stored_flags(conn: sqlite3.Connection, table: str, parsed_text_id: str) -> list[str]:
    return [
        row[0] for row in conn.execute(
            f"SELECT flag FROM {table} WHERE parsed_text_id = ? ORDER BY flag",
            (parsed_text_id,),
        )
    ]


def _current_preparation_version() -> str | None:
    try:
        from core.parse.source_text import PREPARATION_VERSION
        version = str(PREPARATION_VERSION or "").strip()
        return version or None
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - direct execution
        return None


def _cached_fulltext_quality_status(path: str) -> str:
    """Recheck the exact cached verifier text before cross-run reuse."""
    try:
        from core.fetch.extraction.pdf import _quality
        with open(path, encoding="utf-8", errors="replace") as handle:
            return "ok" if _quality(handle.read()) else "bad_quality"
    except (ImportError, ModuleNotFoundError, OSError):
        return "unavailable"


def _source_ref_matches_strong_identifier(
    ref: dict,
    source_ref: str | None,
    mapping: str | None = None,
    identity_status: str | None = None,
) -> bool:
    source = str(source_ref or "").lower()
    doi = _canon_doi(ref.get("doi"))
    if doi and doi.startswith("10.48550/arxiv."):
        arxiv_id = doi.split("arxiv.", 1)[1]
        if arxiv_id and arxiv_id in source:
            return True
    cited_url = _canon_url(ref.get("url"))
    source_url = _canon_url(source_ref)
    if cited_url and source_url:
        cited_host = (urlparse(cited_url).hostname or "").lower()
        if cited_host not in {"doi.org", "dx.doi.org"}:
            cited_route = cited_url.split("://", 1)[-1]
            source_route = source_url.split("://", 1)[-1]
            cited_url_status = bool(
                mapping == "cited_url"
                and identity_status == "cited_url_reachable"
            )
            if cited_url_status and cited_route == source_route:
                return True
            if (
                cited_url_status
            ):
                cited_last = urlparse(cited_url).path.rstrip("/").rsplit("/", 1)[-1]
                source_last = urlparse(source_url).path.rstrip("/").rsplit("/", 1)[-1]
                if cited_last and cited_last == source_last:
                    return True
    return False


def _reusable_fulltext_identity_ok(
    ref: dict,
    path: str,
    source_ref: str | None = None,
    mapping: str | None = None,
    identity_status: str | None = None,
    match_signal: str | None = None,
    match_score: float | None = None,
) -> bool:
    if _source_ref_matches_strong_identifier(
        ref, source_ref, mapping, identity_status
    ):
        return True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    try:
        from core.resolve import sources as _sources
    except ImportError:
        import sources as _sources
    try:
        from core.resolve.providers import openai_reports
        if openai_reports.known_document_identity(ref, source_ref, text):
            return True
    except ImportError:
        pass
    probe = _sources.document_identity_probe(ref, text, None)
    if probe.get("ok") and probe.get("author_ok") is not False:
        return True
    try:
        score = float(match_score)
    except (TypeError, ValueError):
        score = 0.0
    return bool(
        identity_status == "externally_corroborated_text"
        and mapping == "web"
        and match_signal == "title"
        and score >= 0.95
        and probe.get("decision") != "rejected"
        and float(probe.get("title_overlap") or 0.0) >= 0.9
        and probe.get("author_ok") is True
    )


def _reusable_fulltext_material_ok(
    source_ref: str | None,
    path: str | None = None,
    ref: dict | None = None,
) -> bool:
    """Reject deterministic record-only and ancillary source routes."""
    try:
        from core.fetch.extraction import fetch_html
    except ImportError:  # pragma: no cover - direct execution
        from extraction import fetch_html
    source = str(source_ref or "")
    route_ok = bool(
        source
        and not fetch_html.is_ancillary_document_url(source)
        and fetch_html.metadata_record_kind(source) is None
    )
    if not route_ok or not path:
        return route_ok
    return True


def _reusable_fulltext_relation_probe(path: str, ref: dict) -> dict | None:
    """Return the shared relation decision, or ``None`` if bytes cannot be read."""
    try:
        from core.fetch.extraction.document_relation import document_relation_probe
    except ImportError:  # pragma: no cover - direct execution
        from extraction.document_relation import document_relation_probe
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return document_relation_probe(ref, handle.read())
    except OSError:
        return None


def _resolve_has_hard_identity_conflict(resolve_result: dict | None) -> bool:
    if not isinstance(resolve_result, dict):
        return False
    evidence = resolve_result.get("evidence_profile") or {}
    profiles = [
        resolve_result.get("metadata_match"),
        evidence.get("metadata_match"),
        (evidence.get("best_candidate") or {}).get("metadata_match"),
    ]
    if resolve_result.get("identity_conflict") or resolve_result.get("metadata_conflict"):
        return True
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        hard_conflicts = set(profile.get("hard_conflicts") or [])
        if (
            profile.get("metadata_conflict")
            or profile.get("author_conflict")
            or profile.get("venue_conflict")
            or hard_conflicts.intersection({"author", "venue"})
            or len(hard_conflicts.intersection({"author", "year", "venue"})) >= 2
        ):
            return True
    return False


def find_reusable_parsed_text(
    run_dir: str,
    ref: dict,
    *,
    tiers: tuple[str, ...] | list[str] | None = None,
    resolve_result: dict | None = None,
) -> dict | None:
    aliases = identity_aliases_for_ref(ref)
    if str(ref.get("source_kind") or "").casefold() == "chapter_like":
        # An ISBN identifies the containing book/edition, not a chapter within it.
        # Title/raw-entry aliases can also collapse onto a whole-book catalogue row
        # when the parser retained the container title.  Reuse therefore requires a
        # chapter-specific DOI/PMID/URL; weak and container aliases remain ineligible.
        aliases = [alias for alias in aliases if alias[0] in {"doi", "pmid", "url"}]
    if not aliases:
        return None
    conn = _connect(run_dir)
    try:
        placeholders = ",".join("?" for _ in aliases)
        params = [alias_key for _scheme, alias_key in aliases]
        where = ""
        if tiers:
            allowed = [t for t in tiers if t in TIERS]
            if not allowed:
                return None
            where = "AND pt.tier IN (" + ",".join("?" for _ in allowed) + ")"
            params.extend(allowed)
        rows = conn.execute(
            f"""
            SELECT pt.*, w.canonical_identity_key
            FROM work_aliases wa
            JOIN parsed_texts pt ON pt.work_id = wa.work_id
            JOIN works w ON w.work_id = pt.work_id
            WHERE wa.alias_key IN ({placeholders})
              AND pt.active = 1
              AND pt.missing = 0
              {where}
            ORDER BY {_tier_case('pt.tier')} DESC,
                     CASE COALESCE(pt.content_version, 'published')
                       WHEN 'published' THEN 1 ELSE 0 END DESC,
                     CASE pt.supplied_by WHEN 'user' THEN 1 ELSE 0 END DESC,
                     pt.updated_at DESC,
                     pt.parsed_text_id DESC
            """,
            params,
        ).fetchall()
        root = storage_root(run_dir)
        for row in rows:
            abs_path = os.path.join(root, row["stored_relpath"])
            if not os.path.exists(abs_path):
                _mark_missing(conn, row["parsed_text_id"])
                continue
            if row["tier"] == "fulltext":
                relation = _reusable_fulltext_relation_probe(abs_path, ref)
                if relation is None:
                    _mark_missing(conn, row["parsed_text_id"])
                    continue
                if relation["decision"] == "incompatible":
                    _mark_stale_preparation(conn, row["parsed_text_id"])
                    continue
            if (
                row["tier"] == "fulltext"
                and row["supplied_by"] != "user"
                and not _reusable_fulltext_material_ok(row["source_ref"], abs_path, ref)
            ):
                _mark_stale_preparation(conn, row["parsed_text_id"])
                continue
            if (
                row["tier"] == "fulltext"
                and row["supplied_by"] != "user"
                and _resolve_has_hard_identity_conflict(resolve_result)
                and not _reusable_fulltext_identity_ok(
                    ref,
                    abs_path,
                    row["source_ref"],
                    row["mapping"],
                    row["identity_status"],
                    row["match_signal"],
                    row["match_score"],
                )
            ):
                # A conflicting resolver candidate cannot vouch for cached bytes.
                # A citation-only document probe may still establish the identity;
                # otherwise skip without globally deactivating the source.
                continue
            if (
                row["tier"] == "fulltext"
                and row["supplied_by"] != "user"
                and not _reusable_fulltext_identity_ok(
                    ref,
                    abs_path,
                    row["source_ref"],
                    row["mapping"],
                    row["identity_status"],
                    row["match_signal"],
                    row["match_score"],
                )
            ):
                _mark_missing(conn, row["parsed_text_id"])
                continue
            if (
                row["tier"] == "fulltext"
                and row["supplied_by"] != "user"
            ):
                quality_status = _cached_fulltext_quality_status(abs_path)
                if quality_status == "bad_quality":
                    _mark_stale_preparation(conn, row["parsed_text_id"])
                    continue
                if quality_status == "unavailable":
                    continue
            if row["tier"] == "fulltext" and row["supplied_by"] != "user":
                current_preparation_version = _current_preparation_version()
                if current_preparation_version is None:
                    continue
                if row["preparation_version"] != current_preparation_version:
                    # Only text produced by the current deterministic preparation
                    # contract is reusable across runs. Invalidate incomplete or
                    # differently versioned rows and let fetch rebuild from source.
                    _mark_stale_preparation(conn, row["parsed_text_id"])
                    continue
            reusable = {
                "parsed_text_id": row["parsed_text_id"],
                "work_id": row["work_id"],
                "tier": row["tier"],
                "origin": row["origin"],
                "supplied_by": row["supplied_by"],
                "supplied_via": row["supplied_via"],
                "file_format": row["file_format"],
                "source_ref": row["source_ref"],
                "stored_relpath": row["stored_relpath"],
                "stored_path": abs_path,
                "sha256": row["sha256"],
                "char_count": row["char_count"],
                "content_version": row["content_version"],
                "provenance_relation": row["provenance_relation"],
                "extraction_method": row["extraction_method"],
                "extraction_flags": _stored_flags(
                    conn, "parsed_text_extraction_flags", row["parsed_text_id"]
                ),
                "preparation_version": row["preparation_version"],
                "preparation_flags": _stored_flags(
                    conn, "parsed_text_preparation_flags", row["parsed_text_id"]
                ),
                "preparation_before_chars": row["preparation_before_chars"],
                "preparation_after_chars": row["preparation_after_chars"],
                "mapping": row["mapping"],
                "match_signal": row["match_signal"],
                "match_score": row["match_score"],
                "identity_status": row["identity_status"],
                "identity_note": row["identity_note"],
                "identity_key": row["canonical_identity_key"],
            }
            if row["tier"] == "fulltext":
                reusable["document_relation"] = relation
            return reusable
        return None
    finally:
        conn.close()


def debug_snapshot(run_dir: str) -> dict[str, Any]:
    conn = _connect(run_dir)
    try:
        tables = {}
        for name in (
            "works", "work_aliases", "library_items", "parsed_texts",
            "parsed_text_extraction_flags", "parsed_text_preparation_flags",
        ):
            rows = conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
            tables[name] = [dict(row) for row in rows]
        return {
            "storage_root": storage_root(run_dir),
            "db_path": db_path(run_dir),
            "tables": tables,
        }
    finally:
        conn.close()
