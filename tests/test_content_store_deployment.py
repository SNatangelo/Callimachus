# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from core.fetch.storage import content_store


ROOT = Path(__file__).resolve().parents[1]


def _store_snapshot(root: str) -> dict[str, bytes]:
    base = Path(root)
    if not base.exists():
        return {}
    return {
        str(path.relative_to(base)): path.read_bytes()
        for path in base.rglob("*")
        if path.is_file()
        and path.name != "content.sqlite-shm"
    }


def test_main_rejects_invalid_store_before_creating_new_run(tmp_path):
    future_run = tmp_path / "runs" / "future-main"
    selected = content_store.preflight(str(future_run))
    assert selected == content_store.db_path(str(future_run))
    assert not future_run.exists()

    with sqlite3.connect(selected) as conn:
        conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
        conn.execute("PRAGMA journal_mode = DELETE")

    manuscript = tmp_path / "manuscript.txt"
    manuscript.write_text("Synthetic manuscript; it must not be parsed.\n", encoding="utf-8")
    store_root = content_store.storage_root(str(future_run))
    before = _store_snapshot(store_root)

    env = os.environ.copy()
    env["CITATION_VERIFIER_INTEGRITY_SOCKET"] = str(tmp_path / "unused.sock")
    for name in (
        "CITATION_VERIFIER_MAILTO",
        "GOOGLE_BOOKS_API_KEY",
        "CORE_API_KEY",
        "CITATION_VERIFIER_SIGNING_KEY",
        "CITATION_VERIFIER_SIGNING_KEY_FILE",
    ):
        env[name] = ""

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "run.py"),
            "--input",
            str(manuscript),
            "--run",
            str(future_run),
            "--accuracy",
            "maximum",
            "--proceed",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "content store preflight failed" in completed.stderr
    assert _store_snapshot(store_root) == before
    assert not future_run.exists()
