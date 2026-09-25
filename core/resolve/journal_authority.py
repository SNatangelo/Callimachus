# core/resolve/journal_authority.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic journal identity evidence from a versioned local registry."""

from __future__ import annotations

import html
from functools import lru_cache
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


CATALOG_SCHEMA_VERSION = 3
CATALOG_ENV = "CALLIMACHUS_JOURNAL_AUTHORITY_DB"


def _text_key(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = unicodedata.normalize("NFKD", text).casefold().replace("&", " and ")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in text).split()
    )


def normalize_issn(value: object) -> str | None:
    compact = re.sub(r"[^0-9Xx]", "", str(value or ""))
    if re.fullmatch(r"[0-9]{7}[0-9Xx]", compact) is None:
        return None
    total = sum(int(char) * weight for char, weight in zip(compact[:7], range(8, 1, -1)))
    check = (11 - total % 11) % 11
    expected = "X" if check == 10 else str(check)
    if compact[7].upper() != expected:
        return None
    return compact[:4] + "-" + compact[4:].upper()


def _cited_container(ref: dict) -> str | None:
    for item in ref.get("cited_coordinates") or ():
        if isinstance(item, dict) and item.get("kind") == "container":
            value = item.get("normalized_value") or item.get("raw_value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _hash_row(hasher, values: tuple[object, ...]) -> None:
    hasher.update(json.dumps(
        values, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))
    hasher.update(b"\n")


def _valid_created_at(value: object) -> bool:
    text = str(value or "")
    if not text.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    )


def _snapshot_hash(
    conn: sqlite3.Connection, registry: str, registry_version: str,
    created_at: str, source_kind: str, source_url: str | None,
    source_sha256: str | None,
) -> str:
    """Hash catalog metadata and ordered relational rows without materializing them."""
    hasher = hashlib.sha256()
    _hash_row(hasher, (
        "snapshot-v3", registry, registry_version, created_at, source_kind,
        source_url, source_sha256,
    ))
    for row in conn.execute(
        "SELECT record_id,canonical_title FROM journals ORDER BY record_id"
    ):
        _hash_row(hasher, ("journal", row[0], row[1]))
    for row in conn.execute(
        "SELECT record_id,alias,normalized_alias "
        "FROM journal_aliases ORDER BY record_id,alias"
    ):
        _hash_row(hasher, ("alias", row[0], row[1], row[2]))
    for row in conn.execute(
        "SELECT record_id,issn FROM journal_issns ORDER BY record_id,issn"
    ):
        _hash_row(hasher, ("issn", row[0], row[1]))
    return hasher.hexdigest()


def build_catalog(
    path: str | os.PathLike[str],
    records: Iterable[dict],
    *,
    registry: str,
    registry_version: str,
    merge_duplicate_ids: bool = False,
    created_at: str | None = None,
    source_kind: str = "manual",
    source_url: str | None = None,
    source_sha256: str | None = None,
) -> None:
    """Atomically build the local relational journal-authority snapshot."""
    registry = str(registry).strip()
    registry_version = str(registry_version).strip()
    if not registry or not registry_version:
        raise ValueError("journal authority registry and version must be non-empty")
    created_at = created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    source_kind = str(source_kind or "").strip()
    source_url = str(source_url).strip() if source_url is not None else None
    if (
        not source_kind
        or not _valid_created_at(created_at)
        or (source_url is not None and not source_url)
        or (source_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None)
    ):
        raise ValueError("journal authority source metadata is invalid")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".building")
    if staging.exists():
        staging.unlink()
    conn = sqlite3.connect(staging)
    try:
        conn.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE snapshot(
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              registry TEXT NOT NULL, registry_version TEXT NOT NULL,
              snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
              record_count INTEGER NOT NULL CHECK(record_count>=0),
              created_at TEXT NOT NULL,
              source_kind TEXT NOT NULL CHECK(length(trim(source_kind))>0),
              source_url TEXT,
              source_sha256 TEXT CHECK(source_sha256 IS NULL OR length(source_sha256)=64)
            );
            CREATE TABLE journals(
              record_id TEXT PRIMARY KEY,
              canonical_title TEXT NOT NULL CHECK(length(trim(canonical_title))>0)
            );
            CREATE TABLE journal_aliases(
              record_id TEXT NOT NULL REFERENCES journals(record_id) ON DELETE CASCADE,
              alias TEXT NOT NULL CHECK(length(trim(alias))>0),
              normalized_alias TEXT NOT NULL CHECK(length(normalized_alias)>0),
              PRIMARY KEY(record_id,alias)
            );
            CREATE TABLE journal_issns(
              record_id TEXT NOT NULL REFERENCES journals(record_id) ON DELETE CASCADE,
              issn TEXT NOT NULL,
              PRIMARY KEY(record_id,issn)
            );
        """)
        conn.execute(f"PRAGMA user_version={CATALOG_SCHEMA_VERSION}")
        for raw in records:
            record_id = str(raw.get("record_id") or "").strip()
            canonical = str(raw.get("canonical_title") or "").strip()
            aliases = sorted({
                str(value).strip() for value in raw.get("aliases") or ()
                if str(value).strip()
            } | ({canonical} if canonical else set()))
            issns = sorted({
                value for item in raw.get("issns") or ()
                if (value := normalize_issn(item)) is not None
            })
            if (
                not record_id or not canonical or not aliases or not issns
                or any(not _text_key(alias) for alias in aliases)
            ):
                raise ValueError(
                    "journal authority record is incomplete or has no valid ISSN"
                )
            existing = conn.execute(
                "SELECT canonical_title FROM journals WHERE record_id=?",
                (record_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO journals VALUES(?,?)", (record_id, canonical),
                )
            else:
                if not merge_duplicate_ids:
                    raise ValueError("journal authority record ids must be unique")
                selected = min(
                    (existing[0], canonical), key=lambda value: (_text_key(value), value),
                )
                if selected != existing[0]:
                    conn.execute(
                        "UPDATE journals SET canonical_title=? WHERE record_id=?",
                        (selected, record_id),
                    )
            conn.executemany(
                "INSERT OR IGNORE INTO journal_aliases VALUES(?,?,?)",
                (
                    (record_id, alias, _text_key(alias)) for alias in aliases
                ),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO journal_issns VALUES(?,?)",
                ((record_id, issn) for issn in issns),
            )
        conn.execute(
            "CREATE INDEX journal_aliases_lookup ON journal_aliases(normalized_alias)"
        )
        record_count = int(conn.execute("SELECT COUNT(*) FROM journals").fetchone()[0])
        snapshot_sha256 = _snapshot_hash(
            conn, registry, registry_version, created_at, source_kind,
            source_url, source_sha256,
        )
        conn.execute(
            "INSERT INTO snapshot VALUES(1,?,?,?,?,?,?,?,?)",
            (
                registry, registry_version, snapshot_sha256, record_count,
                created_at, source_kind, source_url, source_sha256,
            ),
        )
        if list(conn.execute("PRAGMA foreign_key_check")):
            raise RuntimeError("journal authority catalog foreign-key check failed")
        conn.commit()
    finally:
        conn.close()
    os.replace(staging, target)


def _catalog_signature(catalog: Path) -> tuple[int, int, int]:
    try:
        stat = catalog.stat()
    except OSError as exc:
        raise RuntimeError("configured journal authority catalog is unavailable") from exc
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _open_catalog(catalog_path: str) -> sqlite3.Connection:
    catalog = Path(catalog_path)
    uri = catalog.as_uri() + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        raise RuntimeError("configured journal authority catalog is unavailable") from exc


@lru_cache(maxsize=4)
def _load_catalog(
    catalog_path: str, size: int, mtime_ns: int, inode: int,
) -> dict:
    """Stream-validate one immutable snapshot once for each file identity."""
    del size, mtime_ns, inode
    conn = _open_catalog(catalog_path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != CATALOG_SCHEMA_VERSION:
            raise RuntimeError("journal authority catalog schema is unsupported")
        snapshot = conn.execute("SELECT * FROM snapshot WHERE singleton=1").fetchone()
        if (
            snapshot is None
            or not re.fullmatch(r"[0-9a-f]{64}", snapshot["snapshot_sha256"])
            or not _valid_created_at(snapshot["created_at"])
            or not str(snapshot["source_kind"] or "").strip()
            or (
                snapshot["source_sha256"] is not None
                and re.fullmatch(r"[0-9a-f]{64}", snapshot["source_sha256"]) is None
            )
        ):
            raise RuntimeError("journal authority catalog snapshot metadata is invalid")
        if list(conn.execute("PRAGMA foreign_key_check")):
            raise RuntimeError("journal authority catalog foreign keys are invalid")
        record_count = int(conn.execute("SELECT COUNT(*) FROM journals").fetchone()[0])
        if record_count != snapshot["record_count"]:
            raise RuntimeError("journal authority catalog record count is inconsistent")
        actual_hash = _snapshot_hash(
            conn, snapshot["registry"], snapshot["registry_version"],
            snapshot["created_at"], snapshot["source_kind"],
            snapshot["source_url"], snapshot["source_sha256"],
        )
        if actual_hash != snapshot["snapshot_sha256"]:
            raise RuntimeError("journal authority catalog snapshot hash is inconsistent")
        return {
            "registry": snapshot["registry"],
            "registry_version": snapshot["registry_version"],
            "snapshot_sha256": snapshot["snapshot_sha256"],
            "record_count": record_count,
            "created_at": snapshot["created_at"],
            "source_kind": snapshot["source_kind"],
            "source_url": snapshot["source_url"],
            "source_sha256": snapshot["source_sha256"],
        }
    except sqlite3.Error as exc:
        raise RuntimeError("configured journal authority catalog is invalid") from exc
    finally:
        conn.close()


@lru_cache(maxsize=8192)
def _lookup_alias(
    catalog_path: str, size: int, mtime_ns: int, inode: int, normalized_alias: str,
) -> tuple[tuple[str, str, str], ...]:
    del size, mtime_ns, inode
    conn = _open_catalog(catalog_path)
    try:
        return tuple(
            (row["record_id"], row["canonical_title"], row["alias"])
            for row in conn.execute(
                """
                SELECT j.record_id,j.canonical_title,a.alias
                FROM journal_aliases AS a
                JOIN journals AS j ON j.record_id=a.record_id
                WHERE a.normalized_alias=?
                ORDER BY j.record_id,a.alias
                """,
                (normalized_alias,),
            )
        )
    except sqlite3.Error as exc:
        raise RuntimeError("configured journal authority catalog is invalid") from exc
    finally:
        conn.close()


def _one_edit_keys(value: str) -> tuple[str, ...]:
    """Return the finite, indexed exact-lookup neighbourhood of ``value``."""
    alphabet = " abcdefghijklmnopqrstuvwxyz0123456789"
    # A one-character change is not discriminating for short journal
    # abbreviations (for example, two real three-letter suffixes).  Keep those
    # unrecognized rather than presenting a misleading near-identity.
    if len(value) < 12 or len(value) > 128:
        return ()
    keys = set()
    for index in range(len(value)):
        keys.add(value[:index] + value[index + 1:])
        for char in alphabet:
            if char != value[index]:
                keys.add(value[:index] + char + value[index + 1:])
    for index in range(len(value) + 1):
        for char in alphabet:
            keys.add(value[:index] + char + value[index:])
    keys.discard(value)
    return tuple(sorted(keys))


def _edit_distance_at_most_one(left: str, right: str) -> int | None:
    if left == right:
        return 0
    if abs(len(left) - len(right)) > 1:
        return None
    if len(left) > len(right):
        left, right = right, left
    index = offset = 0
    while index < len(left) and left[index] == right[index]:
        index += 1
    if index == len(left):
        return 1
    if len(left) == len(right):
        index += 1
    else:
        offset = 1
    while index < len(left) and left[index] == right[index + offset]:
        index += 1
    return 1 if index == len(left) else None


@lru_cache(maxsize=8192)
def _lookup_near_aliases(
    catalog_path: str, size: int, mtime_ns: int, inode: int, normalized_alias: str,
) -> tuple[tuple[str, str, str], ...]:
    """Fetch the finite edit-one neighbourhood using bounded indexed batches."""
    del size, mtime_ns, inode
    keys = _one_edit_keys(normalized_alias)
    if not keys:
        return ()
    conn = _open_catalog(catalog_path)
    try:
        rows: list[tuple[str, str, str]] = []
        for offset in range(0, len(keys), 900):
            batch = keys[offset:offset + 900]
            rows.extend(
                (row["record_id"], row["canonical_title"], row["alias"])
                for row in conn.execute(
                    "SELECT j.record_id,j.canonical_title,a.alias FROM journal_aliases AS a "
                    "JOIN journals AS j ON j.record_id=a.record_id "
                    f"WHERE a.normalized_alias IN ({','.join('?' for _ in batch)}) "
                    "ORDER BY j.record_id,a.alias",
                    batch,
                )
            )
        return tuple(sorted(set(rows)))
    except sqlite3.Error as exc:
        raise RuntimeError("configured journal authority catalog is invalid") from exc
    finally:
        conn.close()


def assess_local_journal(ref: dict, path: str | os.PathLike[str] | None = None) -> dict | None:
    """Recognize a cited venue against one immutable local SQLite snapshot.

    Missing configuration means that no authority evidence is available. An
    explicitly configured malformed catalog raises, so it cannot silently
    weaken or strengthen a verdict. Ambiguous aliases return no recognition.
    """
    cited = _cited_container(ref)
    configured = path if path is not None else os.environ.get(CATALOG_ENV)
    if not cited or not configured:
        return None
    catalog = Path(configured).expanduser().resolve()
    signature = _catalog_signature(catalog)
    metadata = _load_catalog(str(catalog), *signature)
    rows = _lookup_alias(str(catalog), *signature, _text_key(cited))
    if _catalog_signature(catalog) != signature:
        raise RuntimeError("journal authority catalog changed during lookup")
    record_ids = sorted({row[0] for row in rows})
    if len(record_ids) != 1:
        return None
    row = rows[0]
    return {
        "status": "recognized",
        "registry": metadata["registry"],
        "registry_version": metadata["registry_version"],
        "record_id": row[0],
        "cited_venue": cited,
        "canonical_title": row[1],
        "matched_alias": row[2],
        "match_basis": (
            "canonical_title"
            if _text_key(row[2]) == _text_key(row[1])
            else "registered_alias"
        ),
        "snapshot_sha256": metadata["snapshot_sha256"],
    }


def assess_local_journal_alias(ref: dict, path: str | os.PathLike[str] | None = None) -> dict | None:
    """Describe an exact or one-edit registered alias without granting authority.

    Every candidate is obtained through an indexed exact lookup of a finite
    edit-one neighbourhood.  ``near_unconfirmed`` deliberately has no ISSN or
    authority fields and must never be used to query a resolver.
    """
    cited = _cited_container(ref)
    configured = path if path is not None else os.environ.get(CATALOG_ENV)
    if not cited or not configured:
        return None
    key = _text_key(cited)
    if not key:
        return None
    catalog = Path(configured).expanduser().resolve()
    signature = _catalog_signature(catalog)
    metadata = _load_catalog(str(catalog), *signature)
    exact_rows = _lookup_alias(str(catalog), *signature, key)
    rows = exact_rows
    distance = 0
    if not rows:
        found: dict[tuple[str, str, str], int] = {}
        for row in _lookup_near_aliases(str(catalog), *signature, key):
            measured = _edit_distance_at_most_one(key, _text_key(row[2]))
            if measured == 1:
                found[row] = measured
        rows = tuple(sorted(found))
        distance = 1
    if _catalog_signature(catalog) != signature:
        raise RuntimeError("journal authority catalog changed during lookup")
    record_ids = sorted({row[0] for row in rows})
    base = {
        "registry": metadata["registry"], "registry_version": metadata["registry_version"],
        "cited_venue": cited, "snapshot_sha256": metadata["snapshot_sha256"],
    }
    if len(record_ids) != 1:
        return {
            **base, "status": "ambiguous" if rows else "unrecognized",
            "candidates": [
                {"record_id": row[0], "canonical_title": row[1], "matched_alias": row[2], "distance": distance}
                for row in rows
            ],
        }
    row = rows[0]
    return {
        **base,
        "status": "exact" if distance == 0 else "near_unconfirmed",
        "candidates": [{
            "record_id": row[0], "canonical_title": row[1],
            "matched_alias": row[2], "distance": distance,
        }],
    }


def catalog_metadata(path: str | os.PathLike[str]) -> dict:
    """Return validated snapshot metadata for catalog management commands."""
    catalog = Path(path).expanduser().resolve()
    signature = _catalog_signature(catalog)
    metadata = dict(_load_catalog(str(catalog), *signature))
    if _catalog_signature(catalog) != signature:
        raise RuntimeError("journal authority catalog changed during inspection")
    return metadata


def issns_for_record(authority: dict, path: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    """Read registered ISSNs for an already-recognized immutable authority record."""
    if not isinstance(authority, dict):
        return ()
    record_id = str(authority.get("record_id") or "").strip()
    configured = path if path is not None else os.environ.get(CATALOG_ENV)
    if not record_id or not configured:
        return ()
    catalog = Path(configured).expanduser().resolve()
    signature = _catalog_signature(catalog)
    conn = _open_catalog(str(catalog))
    try:
        values = tuple(row[0] for row in conn.execute(
            "SELECT issn FROM journal_issns WHERE record_id=? ORDER BY issn", (record_id,),
        ))
    except sqlite3.Error as exc:
        raise RuntimeError("configured journal authority catalog is invalid") from exc
    finally:
        conn.close()
    if _catalog_signature(catalog) != signature:
        raise RuntimeError("journal authority catalog changed during lookup")
    return values
