#!/usr/bin/env python3
# core/infra/db/connection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""SQLite connection helpers for DB-native runs."""

from __future__ import annotations

import os
import sqlite3
from urllib.parse import quote


DB_FILENAME = "run.sqlite"
DEFAULT_BUSY_TIMEOUT_MS = 8000


def db_path(run_dir: str) -> str:
    return os.path.join(run_dir, DB_FILENAME)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    configure_writable_connection(conn)
    return conn


def connect_existing_writable(path: str) -> sqlite3.Connection:
    """Open an existing database without creating or configuring it yet."""
    absolute_path = os.path.abspath(path)
    uri = f"file:{quote(absolute_path, safe='/:')}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
    return conn


def connect_readonly(path: str) -> sqlite3.Connection:
    """Open an existing run database without permitting file mutations."""
    absolute_path = os.path.abspath(path)
    uri = f"file:{quote(absolute_path, safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _configure_readonly_connection(conn)
    return conn


def connect_run(run_dir: str) -> sqlite3.Connection:
    os.makedirs(run_dir, exist_ok=True)
    return connect(db_path(run_dir))


def configure_writable_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")


def _configure_readonly_connection(conn: sqlite3.Connection) -> None:
    """Configure only connection-local read safeguards; never enable WAL."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
