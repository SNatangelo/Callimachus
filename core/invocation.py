# core/invocation.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Shell-safe commands for invoking the repository entry point."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Iterable


def _shell_join(argv: Iterable[str], platform_name: str) -> str:
    values = list(argv)
    if platform_name.startswith("win"):
        return " ".join(_windows_argument(value) for value in values)
    return shlex.join(values)


def _windows_argument(value: str) -> str:
    """Quote one argument for an operator-facing PowerShell command."""
    rendered = subprocess.list2cmdline([value])
    # ``list2cmdline`` targets CommandLineToArgvW and leaves angle brackets
    # bare.  PowerShell reads those as redirection syntax, so placeholders
    # must remain quoted in copied commands.
    if ("<" in value or ">" in value) and rendered == value:
        return f'"{value}"'
    return rendered


def _shell_command(argv: Iterable[str], platform_name: str) -> str:
    """Render an executable command for the operator's shell."""
    values = list(argv)
    rendered = _shell_join(values, platform_name)
    if platform_name.startswith("win") and _windows_argument(values[0]).startswith('"'):
        return f"& {rendered}"
    return rendered


def _normalised_path(path: str, platform_name: str) -> str:
    value = os.path.normpath(path.replace("\\", "/"))
    return value.lower() if platform_name.startswith("win") else value


def _project_venv_python(project_root: Path, platform_name: str) -> str:
    if platform_name.startswith("win"):
        return str(project_root / ".venv" / "Scripts" / "python.exe")
    return str(project_root / ".venv" / "bin" / "python")


def project_venv_executable(platform_name: str | None = None) -> str:
    """Return the repo-relative project interpreter for the operator OS."""
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name.startswith("win"):
        return r".\.venv\Scripts\python.exe"
    return ".venv/bin/python"


def run_prefix(
    *,
    executable: str | None = None,
    platform_name: str | None = None,
    project_root: Path | None = None,
) -> str:
    """Return the command prefix for this Callimachus checkout.

    A project virtual environment is shown relatively, so copied guidance is
    portable from the repository root.  Every other interpreter is retained
    verbatim and shell-quoted to avoid silently changing the active runtime.
    """
    platform_name = sys.platform if platform_name is None else platform_name
    executable = sys.executable if executable is None else executable
    if getattr(sys, "frozen", False):
        return _shell_command((executable,), platform_name)
    project_root = (
        Path(__file__).resolve().parent.parent
        if project_root is None else Path(project_root)
    )
    venv_python = _project_venv_python(project_root, platform_name)
    if _normalised_path(executable, platform_name) == _normalised_path(
        venv_python, platform_name,
    ):
        executable = project_venv_executable(platform_name)
    return _shell_command((executable, "run.py"), platform_name)


def format_run_examples(text: str) -> str:
    """Render generic ``python run.py`` examples for the active interpreter."""
    prefix = run_prefix()
    return re.sub(r"\bpython3? run\.py\b", lambda _match: prefix, text)


def run_command(
    *args: str,
    executable: str | None = None,
    platform_name: str | None = None,
    project_root: Path | None = None,
) -> str:
    """Return a shell-safe Callimachus command with ``args`` appended."""
    platform_name = sys.platform if platform_name is None else platform_name
    prefix = run_prefix(
        executable=executable, platform_name=platform_name, project_root=project_root,
    )
    return prefix if not args else f"{prefix} {_shell_join(args, platform_name)}"
