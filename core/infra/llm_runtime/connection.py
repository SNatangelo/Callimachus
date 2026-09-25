# core/infra/llm_runtime/connection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed connection helpers for the shared LLM runtime database."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ENV_STATE_DIR = "CITATION_VERIFIER_STATE_DIR"
DB_FILENAME = "llm_runtime.sqlite"
DEFAULT_BUSY_TIMEOUT_MS = 8000


class RuntimeStateError(RuntimeError):
    """The shared operational state cannot safely be used."""


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def db_path(environ: dict[str, str] | None = None) -> str:
    """Return the configured runtime path without creating it."""
    env = os.environ if environ is None else environ
    if ENV_STATE_DIR in env:
        value = env[ENV_STATE_DIR]
        if not isinstance(value, str) or not value.strip():
            raise RuntimeStateError("CITATION_VERIFIER_STATE_DIR is empty")
        if value != value.strip():
            raise RuntimeStateError("CITATION_VERIFIER_STATE_DIR is not canonical")
        root = Path(value).expanduser()
        if not root.is_absolute():
            raise RuntimeStateError("CITATION_VERIFIER_STATE_DIR must be absolute")
    else:
        root = _repository_root() / "storage"
    return str(root / "llm_runtime" / DB_FILENAME)


def connect(
    environ: dict[str, str] | None = None, *, path: str | None = None
) -> sqlite3.Connection:
    """Open a writable database, or raise an explicit operational error."""
    target = db_path(environ) if path is None else path
    if not isinstance(target, str) or not target:
        raise RuntimeStateError("LLM runtime database path is empty")
    if target != ":memory:" and not Path(target).is_absolute():
        raise RuntimeStateError("LLM runtime database path must be absolute")
    try:
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if target != ":memory:":
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeStateError("LLM runtime WAL mode is unavailable")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeStateError("LLM runtime foreign keys are disabled")
        return conn
    except RuntimeStateError:
        if "conn" in locals():
            conn.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        if "conn" in locals():
            conn.close()
        raise RuntimeStateError("LLM runtime state is unavailable") from exc


def open_runtime(
    environ: dict[str, str] | None = None, *, path: str | None = None
) -> sqlite3.Connection:
    """Open and preflight state before a caller can dispatch work."""
    from .schema_bootstrap import ensure_schema, preflight_existing_readonly

    target = db_path(environ) if path is None else path
    if target != ":memory:" and Path(target).exists() and Path(target).stat().st_size:
        # Do not enable WAL or create sidecars before rejecting a non-current DB.
        try:
            uri = f"file:{Path(target).resolve().as_posix()}?mode=ro"
            probe = sqlite3.connect(uri, uri=True)
            try:
                has_objects = probe.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                if has_objects:
                    preflight_existing_readonly(probe)
            finally:
                probe.close()
        except RuntimeStateError:
            raise
        except sqlite3.Error as exc:
            raise RuntimeStateError("LLM runtime state preflight failed") from exc

    conn = connect(environ, path=path)
    try:
        ensure_schema(conn)
        return conn
    except Exception:
        conn.close()
        raise
