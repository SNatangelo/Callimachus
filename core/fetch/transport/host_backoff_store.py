# core/fetch/transport/host_backoff_store.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""SQLite persistence for learned backoff and explicit suite cooldown state."""
from __future__ import annotations

import math
import os
import sqlite3
import time


FILENAME = "host_backoff.sqlite"
ENV_HOST_COOLDOWN_DB = "CITATION_VERIFIER_HOST_COOLDOWN_DB"
_CONNECT_RETRIES = 5
_CONNECT_RETRY_SECONDS = 0.01


class HostCooldownStoreError(RuntimeError):
    """Explicit suite cooldown state is unavailable or invalid."""


def _path(environ: dict[str, str] | None = None) -> str:
    from core.fetch.storage import content_store

    env = environ or os.environ
    override = (env.get(content_store.ENV_STATE_DIR) or "").strip()
    if override:
        return os.path.join(os.path.abspath(override), "storage", FILENAME)
    return os.path.join(content_store.storage_root(environ=environ), FILENAME)


def _cooldown_path(environ: dict[str, str] | None = None) -> str | None:
    env = environ or os.environ
    configured = (env.get(ENV_HOST_COOLDOWN_DB) or "").strip()
    if not configured:
        return None
    if not os.path.isabs(configured):
        raise HostCooldownStoreError(f"{ENV_HOST_COOLDOWN_DB} must be an absolute path")
    return configured


def _canonical_host(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    text = value.strip().lower()
    if (
        not text
        or "://" in text
        or text.startswith((".", "-"))
        or text.endswith((".", "-"))
        or ".." in text
        or any(not (char.isascii() and (char.isalnum() or char in ".-")) for char in text)
    ):
        return ""
    if text.startswith("www."):
        text = text[4:]
    return text.split(":", 1)[0]


def _finite_non_negative(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HostCooldownStoreError(f"invalid host cooldown {name}") from exc
    if not math.isfinite(number) or number < 0.0:
        raise HostCooldownStoreError(f"invalid host cooldown {name}")
    return number


def _validate_cooldown_row(row: tuple) -> tuple[str, dict]:
    (
        host, cooldown_until, last_applied_seconds, in_cooldown,
        post_cooldown_successes, learned_seconds, learned_updated_at,
    ) = row
    canonical = _canonical_host(host)
    if not canonical or canonical != host:
        raise HostCooldownStoreError("invalid host cooldown host")
    cooldown_until = _finite_non_negative(cooldown_until, "cooldown_until")
    last_applied_seconds = _finite_non_negative(last_applied_seconds, "last_applied_seconds")
    learned_seconds = _finite_non_negative(learned_seconds, "learned_seconds")
    learned_updated_at = _finite_non_negative(learned_updated_at, "learned_updated_at")
    if isinstance(in_cooldown, bool) or not isinstance(in_cooldown, int) or in_cooldown not in (0, 1):
        raise HostCooldownStoreError("invalid host cooldown in_cooldown")
    if (
        isinstance(post_cooldown_successes, bool)
        or not isinstance(post_cooldown_successes, int)
        or post_cooldown_successes < 0
    ):
        raise HostCooldownStoreError("invalid host cooldown post_cooldown_successes")
    if in_cooldown and last_applied_seconds <= 0.0:
        raise HostCooldownStoreError("open host cooldown has no applied wait")
    if not in_cooldown and learned_seconds == 0.0 and post_cooldown_successes:
        raise HostCooldownStoreError("host cooldown successes have no open episode or learning")
    if (learned_seconds == 0.0) != (learned_updated_at == 0.0):
        raise HostCooldownStoreError("invalid host cooldown learning timestamp")
    return canonical, {
        "cooldown_until": cooldown_until,
        "last_applied_seconds": last_applied_seconds,
        "in_cooldown": bool(in_cooldown),
        "post_cooldown_successes": post_cooldown_successes,
        "learned_seconds": learned_seconds,
        "learned_updated_at": learned_updated_at,
    }


def _connect(environ: dict[str, str] | None = None) -> sqlite3.Connection:
    return _connect_path(_path(environ))


def _connect_path(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for attempt in range(_CONNECT_RETRIES):
        conn = None
        try:
            conn = sqlite3.connect(path, timeout=5.0)
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS host_backoff ("
                "host TEXT PRIMARY KEY, learned_seconds REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            return conn
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            if attempt + 1 == _CONNECT_RETRIES or not _is_transient_lock(exc):
                raise
            time.sleep(_CONNECT_RETRY_SECONDS)
        except Exception:
            if conn is not None:
                conn.close()
            raise
    raise AssertionError("unreachable")


def _is_transient_lock(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def load(environ: dict[str, str] | None = None) -> dict:
    """Return persisted learning, or no state when its dedicated DB is unavailable."""
    try:
        with _connect(environ) as conn:
            rows = conn.execute(
                "SELECT host, learned_seconds, updated_at FROM host_backoff"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}
    return {
        host: {"learned_seconds": learned_seconds, "updated_at": updated_at}
        for host, learned_seconds, updated_at in rows
    }


def save(mapping: dict, environ: dict[str, str] | None = None) -> None:
    """Merge learned state atomically in the dedicated SQLite database."""
    if not mapping:
        return
    rows = []
    deleted_hosts = []
    for host, value in mapping.items():
        if not isinstance(host, str) or not host or not isinstance(value, dict):
            continue
        try:
            learned_seconds = float(value["learned_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(learned_seconds):
            continue
        if learned_seconds <= 0.0:
            deleted_hosts.append((host,))
            continue
        try:
            updated_at = float(value["updated_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(updated_at):
            continue
        rows.append((host, learned_seconds, updated_at))
    if not rows and not deleted_hosts:
        return
    try:
        with _connect(environ) as conn:
            conn.executemany("DELETE FROM host_backoff WHERE host = ?", deleted_hosts)
            conn.executemany(
                "INSERT INTO host_backoff(host, learned_seconds, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(host) DO UPDATE SET "
                "learned_seconds = excluded.learned_seconds, updated_at = excluded.updated_at",
                rows,
            )
    except (OSError, sqlite3.Error):
        return


def load_cooldowns(environ: dict[str, str] | None = None) -> dict:
    """Load explicit suite-wide cooldown state, failing closed when configured."""
    path = _cooldown_path(environ)
    if path is None:
        return {}
    try:
        with _connect_path(path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS host_cooldown ("
                "host TEXT PRIMARY KEY, cooldown_until REAL NOT NULL, "
                "last_applied_seconds REAL NOT NULL, in_cooldown INTEGER NOT NULL, "
                "post_cooldown_successes INTEGER NOT NULL, learned_seconds REAL NOT NULL, "
                "learned_updated_at REAL NOT NULL)"
            )
            rows = conn.execute(
                "SELECT host, cooldown_until, last_applied_seconds, in_cooldown, "
                "post_cooldown_successes, learned_seconds, learned_updated_at "
                "FROM host_cooldown"
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise HostCooldownStoreError("explicit host cooldown store is unavailable") from exc
    return dict(_validate_cooldown_row(row) for row in rows)


def save_cooldown(host: str, record: dict, environ: dict[str, str] | None = None) -> None:
    """Synchronously persist one explicit suite-wide cooldown state."""
    path = _cooldown_path(environ)
    if path is None:
        return
    values = (
        host,
        float(record["cooldown_until"]),
        float(record["last_applied_seconds"]),
        int(bool(record["in_cooldown"])),
        int(record["post_cooldown_successes"]),
        float(record["learned_seconds"]),
        float(record["learned_updated_at"]),
    )
    _validate_cooldown_row(values)
    try:
        with _connect_path(path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS host_cooldown ("
                "host TEXT PRIMARY KEY, cooldown_until REAL NOT NULL, "
                "last_applied_seconds REAL NOT NULL, in_cooldown INTEGER NOT NULL, "
                "post_cooldown_successes INTEGER NOT NULL, learned_seconds REAL NOT NULL, "
                "learned_updated_at REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO host_cooldown("
                "host, cooldown_until, last_applied_seconds, in_cooldown, "
                "post_cooldown_successes, learned_seconds, learned_updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(host) DO UPDATE SET "
                "cooldown_until = excluded.cooldown_until, "
                "last_applied_seconds = excluded.last_applied_seconds, "
                "in_cooldown = excluded.in_cooldown, "
                "post_cooldown_successes = excluded.post_cooldown_successes, "
                "learned_seconds = excluded.learned_seconds, "
                "learned_updated_at = excluded.learned_updated_at",
                values,
            )
    except (OSError, sqlite3.Error) as exc:
        raise HostCooldownStoreError("explicit host cooldown store is unavailable") from exc
