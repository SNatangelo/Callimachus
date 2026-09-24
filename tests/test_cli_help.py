# tests/test_cli_help.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from run import (
    _COMMANDS,
    _pip_install_command,
    _project_run_command,
    _runtime_install_commands,
)
from core.invocation import run_command, run_prefix


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _shell_quote(value):
    if sys.platform.startswith("win"):
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def _child_run_prefix():
    return run_prefix(executable=os.path.abspath(sys.executable))


@pytest.mark.parametrize(
    ("command", "module_path"),
    [
        ("tasks", "core.app.commands.tasks"),
        ("present", "core.app.commands.present"),
        ("configure", "core.app.commands.configure"),
        ("desktop", "core.app.commands.desktop"),
        ("benchmark", "core.app.commands.benchmark"),
    ],
)
@pytest.mark.parametrize("cwd", [PROJECT_ROOT, PROJECT_ROOT / "core"])
def test_operator_command_help_uses_the_grouped_module_from_any_cwd(
    command, module_path, cwd,
):
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "run.py"), command, "--help"],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert _COMMANDS[command] == module_path
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout.lower()


@pytest.mark.parametrize(
    "command", ("configure", "present", "benchmark", "preview", "verify"),
)
def test_operator_command_help_renders_the_current_invocation(command):
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "run.py"), command, "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert _child_run_prefix() in completed.stdout
    assert not re.search(r"(?<![/\\\w])python run\.py", completed.stdout)


def test_root_help_is_operator_facing_and_complete():
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "run.py"), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    output = completed.stdout

    assert "Callimachus — audit-ready citation verification" in output
    assert (
        "parse -> resolve -> fetch -> gaps -> style -> verify -> "
        "web_research -> report -> done"
    ) in output
    assert "docs/guide/README.md" in output
    assert "docs/guide/10-keys-and-credentials.md" in output
    assert "docs/guide/11-environment-reference.md" in output
    assert f"{_child_run_prefix()} <command> --help" in output
    assert "10  operator action is required" in output
    assert "20  report or completion gate failed" in output
    assert "--input" in output
    assert "--resume" in output
    assert "--status" in output
    assert "--fork-completed-verify" in output
    start_line = next(
        line.strip() for line in output.splitlines() if line.strip().startswith("Start:")
    )
    assert start_line == f"Start:   {_child_run_prefix()} --input manuscript.pdf --accuracy standard"
    normalized = " ".join(output.split())
    assert (
        "mark the run as unattended and shorten pause messages; "
        "Verify runs with or without this flag"
    ) in normalized
    for command in _COMMANDS:
        assert re.search(rf"^  {re.escape(command)}(?:\s|$)", output, re.MULTILINE), command

    assert "Why this exists" not in output


def test_root_help_matches_the_runtime_parser_byte_for_byte():
    fast_path = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "run.py"), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    runtime = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.argv = ['run.py', '--help']; "
                "from core.app import run; run.main()"
            ),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert fast_path.returncode == runtime.returncode == 0
    assert fast_path.stdout == runtime.stdout
    assert fast_path.stderr == runtime.stderr


@pytest.mark.parametrize("flag", ("--help", "-h"))
def test_root_help_uses_only_the_standard_library(flag):
    completed = subprocess.run(
        [sys.executable, "-S", str(PROJECT_ROOT / "run.py"), flag],
        cwd=PROJECT_ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    output = completed.stdout
    for marker in (
        "Callimachus — audit-ready citation verification",
        "parse -> resolve -> fetch -> gaps -> style -> verify -> web_research -> report -> done",
        "docs/guide/README.md",
        "docs/guide/10-keys-and-credentials.md",
        "docs/guide/11-environment-reference.md",
        f"{_child_run_prefix()} <command> --help",
        "10  operator action is required",
        "20  report or completion gate failed",
        "--input",
        "--resume",
    ):
        assert marker in output
    for command in _COMMANDS:
        assert re.search(rf"^  {re.escape(command)}(?:\s|$)", output, re.MULTILINE), command


@pytest.mark.parametrize("argv", [("--input", "missing.txt"), ("fetch", "--help")])
def test_missing_requirements_are_reported_without_a_traceback(argv, tmp_path):
    entrypoint = tmp_path / "run.py"
    entrypoint.write_text(
        (PROJECT_ROOT / "run.py").read_text(encoding="utf-8"), encoding="utf-8",
    )
    (tmp_path / ".env").write_text("SETTING=value\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-S", str(entrypoint), *argv],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 2
    assert "missing mandatory runtime dependencies: certifi, Brotli" in completed.stderr
    install_commands = {
        f"{_shell_quote(sys.executable)} -m pip install -r requirements.txt",
        ".venv/bin/python -m pip install -r requirements.txt",
        r".\.venv\Scripts\python.exe -m pip install -r requirements.txt",
    }
    assert any(command in completed.stderr for command in install_commands)
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize(
    ("platform_name", "executable", "expected"),
    [
        ("linux", "/operator path/python", "'/operator path/python' -m pip install -r requirements.txt"),
        ("win32", "C:\\operator path\\python.exe", '& "C:\\operator path\\python.exe" -m pip install -r requirements.txt'),
    ],
)
def test_missing_requirement_install_command_matches_operator_platform(
    platform_name, executable, expected,
):
    assert _pip_install_command(platform_name, executable=executable) == expected


def test_missing_requirement_install_command_managed_linux_uses_existing_venv():
    assert _runtime_install_commands(
        "linux",
        executable="/usr/bin/python3",
        externally_managed=True,
        venv_exists=True,
    ) == [".venv/bin/python -m pip install -r requirements.txt"]


def test_missing_requirement_install_command_managed_linux_creates_venv():
    assert _runtime_install_commands(
        "linux",
        executable="/usr/bin/python3",
        externally_managed=True,
        venv_exists=False,
    ) == [
        "/usr/bin/python3 -m venv .venv",
        ".venv/bin/python -m pip install -r requirements.txt",
    ]


def test_missing_requirement_install_command_managed_windows_creates_venv():
    assert _runtime_install_commands(
        "win32",
        executable=r"C:\operator path\python.exe",
        externally_managed=True,
        venv_exists=False,
    ) == [
        r'& "C:\operator path\python.exe" -m venv .venv',
        r".\.venv\Scripts\python.exe -m pip install -r requirements.txt",
    ]


def test_missing_requirement_managed_install_commands_are_rooted_outside_checkout(tmp_path):
    root = tmp_path / "checkout"
    elsewhere = tmp_path / "elsewhere"
    assert _runtime_install_commands(
        "linux",
        executable="/usr/bin/python3",
        externally_managed=True,
        venv_exists=False,
        root=root,
        cwd=elsewhere,
    ) == [
        f"/usr/bin/python3 -m venv {root}/.venv",
        f"{root}/.venv/bin/python -m pip install -r {root}/requirements.txt",
    ]
    assert _runtime_install_commands(
        "linux",
        executable="/usr/bin/python3",
        externally_managed=False,
        root=root,
        cwd=elsewhere,
    ) == [f"/usr/bin/python3 -m pip install -r {root}/requirements.txt"]


def test_missing_env_is_created_from_template_once(tmp_path, capsys):
    import run as entrypoint

    template = b"SETTING=\n# exact template bytes\r\n"
    (tmp_path / ".env.example").write_bytes(template)

    assert not entrypoint._ensure_local_env(root=tmp_path)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Created .env from .env.example for this first run." in captured.err
    assert "[config]" not in captured.err
    assert "Traceback" not in captured.err
    assert (tmp_path / ".env").read_bytes() == template


def test_existing_env_is_preserved_and_bootstrap_is_silent(tmp_path, capsys):
    import run as entrypoint

    (tmp_path / ".env").write_text("SETTING=value\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SETTING=replacement\n", encoding="utf-8")

    assert not entrypoint._ensure_local_env(root=tmp_path)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "SETTING=value\n"


def test_missing_env_and_template_fail_closed(tmp_path, capsys):
    import run as entrypoint

    assert entrypoint._ensure_local_env(root=tmp_path)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "both .env and .env.example are missing" in captured.err
    assert "recover them before starting" in captured.err
    assert not (tmp_path / ".env").exists()


def test_missing_requirement_install_command_detects_pep668_marker(
    monkeypatch, tmp_path,
):
    import run as entrypoint

    (tmp_path / "EXTERNALLY-MANAGED").write_text("managed\n", encoding="utf-8")
    monkeypatch.setattr(entrypoint.sys, "prefix", "/usr")
    monkeypatch.setattr(entrypoint.sys, "base_prefix", "/usr")
    monkeypatch.setattr(entrypoint.sysconfig, "get_path", lambda _name: str(tmp_path))

    assert entrypoint._is_externally_managed_python()

    monkeypatch.setattr(entrypoint.sys, "prefix", "/project/.venv")
    assert not entrypoint._is_externally_managed_python()


def test_missing_requirement_ready_project_venv_only_prints_run(
    monkeypatch, capsys,
):
    import run as entrypoint

    monkeypatch.setattr(entrypoint, "_project_venv_is_ready", lambda: True)
    monkeypatch.setattr(entrypoint.sys, "argv", ["run.py", "--input", "paper.pdf"])

    entrypoint._print_missing_runtime_dependencies(
        ["Brotli"], missing_pdf_backend=False,
    )

    captured = capsys.readouterr()
    assert "project virtual environment already has" in captured.err
    assert "pip install" not in captured.err
    assert entrypoint._project_run_command(["--input", "paper.pdf"]) in captured.err
    assert "Traceback" not in captured.err


def test_project_venv_run_command_is_rooted_outside_checkout(tmp_path):
    root = tmp_path / "checkout"
    elsewhere = tmp_path / "elsewhere"
    expected = (
        f"{root}/.venv/bin/python {root}/run.py --input paper.pdf"
    )
    assert _project_run_command(
        ["--input", "paper.pdf"], "linux", root=root, cwd=elsewhere,
    ) == expected


def test_missing_requirement_project_venv_probe_uses_its_python(
    monkeypatch, tmp_path,
):
    import run as entrypoint

    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    calls = []

    class Completed:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Completed()

    monkeypatch.setattr(entrypoint.subprocess, "run", fake_run)

    assert entrypoint._project_venv_is_ready("linux", root=tmp_path)
    assert calls[0][0][0] == str(venv_python)
    assert calls[0][0][1] == "-c"
    assert calls[0][1]["timeout"] == 10


def test_keyboard_interrupt_before_run_exits_quietly(monkeypatch, capsys):
    from core.app import run as pipeline_run

    def interrupt_before_run():
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline_run, "_main", interrupt_before_run)

    assert pipeline_run.main() == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_root_keyboard_interrupt_before_pipeline_exits_quietly(monkeypatch, capsys):
    import run as entrypoint

    def interrupt_before_pipeline():
        raise KeyboardInterrupt

    monkeypatch.setattr(entrypoint, "_main", interrupt_before_pipeline)

    assert entrypoint.main() == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_keyboard_interrupt_after_run_start_prints_resume_command(monkeypatch, capsys):
    from core.app import run as pipeline_run

    run_dir = str(PROJECT_ROOT / "callimachus run")
    stopped = []
    closed = []

    class HeartbeatStop:
        def set(self):
            stopped.append(True)

    def interrupt_after_run_start():
        pipeline_run._ACTIVE_RUN_DIR = run_dir
        pipeline_run._ACTIVE_DRIVER_SESSION = (run_dir, "session-1")
        pipeline_run._ACTIVE_HEARTBEAT_STOP = HeartbeatStop()
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline_run, "_main", interrupt_after_run_start)
    monkeypatch.setattr(
        pipeline_run,
        "_end_driver_session",
        lambda path, session_id, *, status: closed.append(
            (path, session_id, status)
        ),
    )

    assert pipeline_run.main() == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Interrupted. Resume this run with:" in captured.err
    assert run_command("--run", run_dir, "--resume") in captured.err
    assert stopped == [True]
    assert closed == [(run_dir, "session-1", "interrupted")]
    assert pipeline_run._ACTIVE_DRIVER_SESSION is None
    assert pipeline_run._ACTIVE_HEARTBEAT_STOP is None


@pytest.mark.parametrize(
    ("platform_name", "executable", "run_dir", "expected"),
    [
        ("linux", "/operator path/python", "/operator path/run", "'/operator path/python' run.py --run '/operator path/run' --resume"),
        ("win32", "C:\\operator path\\python.exe", "C:\\operator path\\run", '& "C:\\operator path\\python.exe" run.py --run "C:\\operator path\\run" --resume'),
    ],
)
def test_resume_command_is_shell_safe_for_operator_platform(
    monkeypatch, platform_name, executable, run_dir, expected,
):
    from core.app.run import _resume_command

    monkeypatch.setattr(sys, "executable", executable)
    assert _resume_command(run_dir, platform_name) == expected


def test_pdf_backend_preflight_accepts_pdfminer_or_pdftotext(monkeypatch):
    import run as entrypoint

    def only_pdfminer(module):
        if module == "pdfminer":
            return object()
        return None

    monkeypatch.setattr(entrypoint.importlib.util, "find_spec", only_pdfminer)
    monkeypatch.setattr(entrypoint.shutil, "which", lambda _name: None)
    assert entrypoint._has_usable_pdf_backend()

    monkeypatch.setattr(
        entrypoint.importlib.util, "find_spec", lambda _module: None
    )
    monkeypatch.setattr(entrypoint.shutil, "which", lambda _name: "/usr/bin/pdftotext")
    assert entrypoint._has_usable_pdf_backend()


def test_standalone_entrypoints_do_not_require_autonomous_flag():
    paths = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docs" / "guide" / "01-quickstart.md",
        PROJECT_ROOT / "docs" / "guide" / "02-cli-reference.md",
        PROJECT_ROOT / "docs" / "guide" / "05-tasks-and-recovery.md",
    ]
    documents = {path: path.read_text(encoding="utf-8") for path in paths}

    for path, document in documents.items():
        input_examples = re.findall(r"^python run\.py --input .+$", document, re.MULTILINE)
        assert input_examples, path
        assert "--autonomous" not in input_examples[0], path

    for path in paths[1:3]:
        assert "runs Verify with or without this flag" in documents[path]


def test_readme_describes_standalone_and_harness_invocation():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for required_text in (
        "## What it checks",
        "## How it works",
        "### Running with an agent",
        "You can run Callimachus directly; an external agent is not required.",
        "while the same Python driver controls the run",
        "`standalone_unattested`, not audit-ready",
        "[skill contract](SKILL.md)",
        "[procedure](PLAYBOOK.md)",
        "[isolated integrity authority](docs/deployment/agent-guide.md)",
        "`--agent-identity`",
    ):
        assert required_text in readme

    assert "Standalone first, agent integration optional" not in readme


def test_guide_surfaces_setup_references_and_report_layout():
    guide_index = (PROJECT_ROOT / "docs" / "guide" / "README.md").read_text(
        encoding="utf-8"
    )
    quickstart = (PROJECT_ROOT / "docs" / "guide" / "01-quickstart.md").read_text(
        encoding="utf-8"
    )
    artifacts = (
        PROJECT_ROOT / "docs" / "guide" / "07-artifacts-and-provenance.md"
    ).read_text(encoding="utf-8")

    assert guide_index.count("**Setup reference:**") == 2
    assert "part of initial setup" in quickstart
    for section in (
        "0. Run Health",
        "1. Triage — problems first",
        "2. Coverage",
        "3. Per-claim detail",
        "4. Per-source detail",
        "5. Provenance",
    ):
        assert section in artifacts
    assert "No LLM writes its prose" in artifacts


def test_environment_reference_covers_template_and_active_controls():
    template = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    reference = (
        PROJECT_ROOT / "docs" / "guide" / "11-environment-reference.md"
    ).read_text(encoding="utf-8")
    configuration = (
        PROJECT_ROOT / "docs" / "guide" / "03-configuration.md"
    ).read_text(encoding="utf-8")
    labels = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", template, re.MULTILINE))
    active_controls = {
        "CITATION_VERIFIER_OCR_AUTO",
        "CITATION_VERIFIER_OCR_AUTO_MAX_PAGES",
        "CITATION_VERIFIER_OCR_WORKERS",
        "CITATION_VERIFIER_VERIFY_TABLE_CITATIONS",
        "CITATION_VERIFIER_REPORT_HTML",
        "CITATION_VERIFIER_OA_ALTERNATES",
        "CITATION_VERIFIER_WAYBACK",
        "CITATION_VERIFIER_PERMA",
    }
    removed_controls = {
        "CITATION_VERIFIER_LLM_HOST_TOOLS",
        "CITATION_VERIFIER_VERIFY_INVALID_KEY_ENV_SYNC",
        "CITATION_VERIFIER_SUPPLEMENT_FALLBACK",
    }

    assert len(labels) == 95
    assert active_controls <= labels
    assert removed_controls.isdisjoint(labels)
    for label in removed_controls:
        assert label not in reference
    assert "CITATION_VERIFIER_OCR_AUTO_WORKERS" not in labels
    for label in labels:
        assert f"`{label}`" in reference, label
    assert "This chapter explains every assignment label" in reference
    assert "`freetoken`" in configuration
    assert "Opt-in archival acquisition paths" not in configuration
