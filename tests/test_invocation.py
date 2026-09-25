# tests/test_invocation.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.invocation import format_run_examples, run_command, run_prefix


def test_project_venv_prefix_is_relative_on_posix():
    root = Path("/repo")
    assert run_prefix(
        executable="/repo/.venv/bin/python", platform_name="linux", project_root=root,
    ) == ".venv/bin/python run.py"


def test_project_venv_prefix_is_relative_on_windows():
    root = Path("C:/repo")
    assert run_prefix(
        executable=r"C:\repo\.venv\Scripts\python.exe",
        platform_name="win32",
        project_root=root,
    ) == r".\.venv\Scripts\python.exe run.py"


def test_windows_project_venv_command_quotes_spaced_arguments():
    command = run_command(
        "--run", r"C:\operator path\run", "--resume",
        executable=r"C:\repo\.venv\Scripts\python.exe",
        platform_name="win32",
        project_root=Path("C:/repo"),
    )
    assert command == (
        r'.\.venv\Scripts\python.exe run.py --run "C:\operator path\run" --resume'
    )


def test_external_interpreter_and_arguments_are_shell_safe():
    command = run_command(
        "--run", "/operator path/run", "--resume",
        executable="/operator path/python", platform_name="linux",
    )
    assert command == "'/operator path/python' run.py --run '/operator path/run' --resume"


def test_windows_external_interpreter_uses_powershell_call_operator():
    command = run_command(
        "--input", "<manuscript>",
        executable=r"C:\operator path\python.exe",
        platform_name="win32",
    )
    assert command == (
        r'& "C:\operator path\python.exe" run.py --input "<manuscript>"'
    )


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="requires PowerShell")
def test_windows_operator_command_executes_in_powershell(tmp_path):
    launcher = tmp_path / "operator path" / "python.ps1"
    observed = launcher.with_name("observed.txt")
    launcher.parent.mkdir()
    launcher.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Rest)\n"
        "Set-Content -LiteralPath (Join-Path $PSScriptRoot 'observed.txt') -Value $Rest\n",
        encoding="utf-8",
    )
    command = run_command(
        "--input", "<manuscript>", executable=str(launcher),
        platform_name="win32", project_root=tmp_path / "unrelated-project",
    )

    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert '<manuscript>' in observed.read_text(encoding="utf-8")


def test_generic_run_examples_use_the_current_prefix():
    rendered = format_run_examples(
        "python run.py --input paper.pdf; python3 run.py verify --run runs/example"
    )
    assert rendered == (
        f"{run_prefix()} --input paper.pdf; "
        f"{run_prefix()} verify --run runs/example"
    )


def test_frozen_prefix_does_not_reference_source_entrypoint(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert run_prefix(executable="/opt/Callimachus/Callimachus", platform_name="linux") == (
        "/opt/Callimachus/Callimachus"
    )
    assert run_command(
        "--run", "/operator path/run", "--resume",
        executable="/opt/Callimachus/Callimachus", platform_name="linux",
    ) == "/opt/Callimachus/Callimachus --run '/operator path/run' --resume"
