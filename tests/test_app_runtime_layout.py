# tests/test_app_runtime_layout.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Structural checks for the application runtime support package."""

from importlib import import_module
from pathlib import Path


RUNTIME_MODULES = (
    "credentials",
    "fetch_audit",
    "repository",
    "resolve_failures",
    "settings",
    "sources",
    "tasks",
)


def test_runtime_helpers_are_split_by_responsibility():
    project_root = Path(__file__).resolve().parents[1]
    runtime_dir = project_root / "core" / "app" / "runtime"

    assert not (project_root / "core" / "app" / "_common.py").exists()
    for name in RUNTIME_MODULES:
        module = import_module(f"core.app.runtime.{name}")
        assert Path(module.__file__).resolve().parent == runtime_dir.resolve()


def test_application_phases_do_not_restore_the_common_wildcard():
    project_root = Path(__file__).resolve().parents[1]
    app_dir = project_root / "core" / "app"

    for path in (app_dir / "run.py", *(app_dir / "phases").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "core.app._common" not in source
        assert "runtime" not in source or "import *" not in source


def test_source_materialization_uses_the_repository_reference_lookup():
    from core.app.runtime import repository, sources

    assert sources._run_ref_by_id is repository._run_ref_by_id
