# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import io
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from core.app import runtime_paths
from core.app.commands import desktop
from core.gui import launcher
import run as entrypoint


def _set_frozen(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_paths.sys, "_MEIPASS", str(root), raising=False)


def test_runtime_paths_use_platform_data_root_for_frozen_app(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    local = tmp_path / "Local App Data"
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setattr(runtime_paths.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("CALLIMACHUS_DATA_DIR", raising=False)

    assert runtime_paths.resource_root() == bundle
    assert runtime_paths.user_data_root() == local / "Callimachus"
    assert runtime_paths.environment_file() == local / "Callimachus" / ".env"
    assert runtime_paths.runs_directory() == local / "Callimachus" / "runs"
    assert runtime_paths.application_argv("--run", "run-a") == [
        sys.executable, "--run", "run-a",
    ]


def test_source_runtime_paths_stay_in_the_selected_worktree(monkeypatch, tmp_path):
    monkeypatch.delattr(runtime_paths.sys, "frozen", raising=False)
    monkeypatch.delenv("CALLIMACHUS_DATA_DIR", raising=False)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert runtime_paths.user_data_root(source_root=worktree) == worktree
    assert runtime_paths.environment_file(source_root=worktree) == worktree / ".env"
    assert runtime_paths.runs_directory(source_root=worktree) == worktree / "runs"
    assert runtime_paths.application_argv("app", source_root=worktree) == [
        sys.executable, str(worktree / "run.py"), "app",
    ]


def test_windowed_frozen_startup_repairs_missing_standard_streams(monkeypatch):
    _set_frozen(monkeypatch, Path.cwd())
    monkeypatch.setattr(entrypoint.sys, "stdin", None)
    monkeypatch.setattr(entrypoint.sys, "stdout", None)
    monkeypatch.setattr(entrypoint.sys, "stderr", None)

    entrypoint._repair_frozen_stdio()

    repaired = (entrypoint.sys.stdin, entrypoint.sys.stdout, entrypoint.sys.stderr)
    assert all(stream is not None for stream in repaired)
    for stream in repaired:
        stream.close()


def test_frozen_output_uses_utf8_even_with_legacy_windows_console(monkeypatch):
    _set_frozen(monkeypatch, Path.cwd())
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="cp1252")
    monkeypatch.setattr(entrypoint.sys, "stdout", stream)

    entrypoint._repair_frozen_stdio()
    stream.write("⟦citation⟧")
    stream.flush()

    assert output.getvalue() == "⟦citation⟧".encode("utf-8")


def test_driver_handles_missing_windowed_stdin_as_noninteractive(monkeypatch):
    from core.app import run as driver

    monkeypatch.setattr(driver.sys, "stdin", None)
    assert driver._stdin_is_interactive() is False
    assert driver._guided_fetch_offer_available(
        {"phase": "fetch", "fetch_paused": True},
        driver.ACTION_REQUIRED,
        autonomous=False,
    ) is False
    monkeypatch.setenv("CALLIMACHUS_DESKTOP_CONTROL", "1")
    driver._start_desktop_control_reader()
    assert "CALLIMACHUS_DESKTOP_CONTROL" not in os.environ


def test_frozen_environment_initialization_does_not_touch_bundle_env(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    template = b"CITATION_VERIFIER_VERIFY_BACKENDS=\n"
    (bundle / ".env.example").write_bytes(template)
    (bundle / ".env").write_text("BUNDLE_SECRET=do-not-load\n", encoding="utf-8")
    state = tmp_path / "user-data"
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setattr(runtime_paths.platform, "system", lambda: "Linux")
    monkeypatch.setenv("CALLIMACHUS_DATA_DIR", str(state))

    assert entrypoint._ensure_local_env(root=bundle) is False
    assert (state / ".env").read_bytes() == template
    assert (bundle / ".env").read_text(encoding="utf-8") == "BUNDLE_SECRET=do-not-load\n"


def test_frozen_desktop_first_launch_initializes_user_env_from_template(
    monkeypatch, tmp_path
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    template = "CALLIMACHUS_FIRST_LAUNCH_TEST=ready\n"
    (bundle / ".env.example").write_text(template, encoding="utf-8")
    state = tmp_path / "user-data"
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setenv("CALLIMACHUS_DATA_DIR", str(state))
    monkeypatch.delenv("CALLIMACHUS_FIRST_LAUNCH_TEST", raising=False)

    assert entrypoint._load_frozen_environment() is True

    assert (state / ".env").read_text(encoding="utf-8") == template
    assert os.environ["CALLIMACHUS_FIRST_LAUNCH_TEST"] == "ready"


def test_third_party_notices_are_discoverable_from_the_bundled_index(tmp_path):
    bundle = tmp_path / "bundle"
    notice_dir = bundle / "THIRD-PARTY-NOTICES"
    notice_dir.mkdir(parents=True)
    (notice_dir / "index.json").write_text(
        json.dumps([{
            "name": "Example dependency",
            "version": "1.2.3",
            "license": "MIT",
            "files": ["LICENSE.txt"],
        }]),
        encoding="utf-8",
    )

    document = desktop._third_party_notice_document(bundle)

    assert document is not None
    assert "THIRD-PARTY-NOTICES/" in document
    assert "Example dependency" in document
    assert "1.2.3" in document
    assert "MIT" in document
    assert "license and notice texts are kept outside the application window" in document


def test_desktop_child_commands_use_executable_argv_and_user_data(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / ".env.example").write_text(
        "CITATION_VERIFIER_VERIFY_BACKENDS=\n", encoding="utf-8"
    )
    (bundle / ".env").write_text("BUNDLE_SECRET=do-not-load\n", encoding="utf-8")
    state = tmp_path / "user-data"
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setattr(desktop, "_root", lambda: bundle)
    monkeypatch.setattr(desktop, "load_run_brief", lambda _run: {"run_status": "paused"})
    monkeypatch.setattr(
        desktop,
        "preview_verify_configuration",
        lambda *_args, **_kwargs: {
            "available": True, "jury1": ["openai:model-a"], "jury2": [],
        },
    )
    monkeypatch.setattr(
        desktop,
        "available_verify_backend_options",
        lambda *_args, **_kwargs: [{
            "selector": "openai:model-a",
            "backend": "openai",
            "model_env": "OPENAI_MODEL",
            "model": "model-a",
        }],
    )
    monkeypatch.setattr(
        desktop,
        "selected_verify_environment",
        lambda *_args, **_kwargs: {"available": True, "overlay": {}},
    )
    captured = {}
    monkeypatch.setitem(
        sys.modules,
        "core.gui.desktop",
        SimpleNamespace(run_desktop=lambda **kwargs: captured.update(kwargs) or 0),
    )
    monkeypatch.setenv("CALLIMACHUS_DATA_DIR", str(state))
    monkeypatch.delenv("CITATION_VERIFIER_VERIFY_BACKENDS", raising=False)

    assert desktop.main([]) == 0
    start = captured["command_builder"]("paper.pdf", "standard", "medium")
    resume = captured["resume_command_builder"]("run-a")
    report = captured["report_callback"]({"run_dir": "run-a"})
    fork = captured["verify_fork_command_builder"](
        {"run_dir": "parent", "paper": "paper", "references_only": False},
        ["openai:model-a"],
    )

    assert start["command"][:2] == [sys.executable, "--input"]
    assert "run.py" not in start["command"]
    assert resume[:2] == [sys.executable, "--run"]
    assert "run.py" not in resume
    assert report[:2] == [sys.executable, "report"]
    assert "run.py" not in report
    assert fork["command"][:2] == [sys.executable, "--run"]
    assert "run.py" not in fork["command"]
    assert Path(start["run_dir"]).parent == state / "runs"
    assert captured["settings_saver"]({"CITATION_VERIFIER_VERIFY_BACKENDS": ""}) == (
        state / ".env"
    )
    assert (state / ".env").is_file()
    assert (bundle / ".env").read_text(encoding="utf-8") == "BUNDLE_SECRET=do-not-load\n"


def test_guided_fetch_probe_uses_internal_frozen_subcommand(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _set_frozen(monkeypatch, bundle)
    seen = []

    def runner(command, **kwargs):
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    assert launcher._probe_qapplication_startup(runner=runner) is None
    assert seen[0][0] == [sys.executable, "--qt-probe"]
    assert seen[0][1]["timeout"] == launcher._QT_STARTUP_PROBE_TIMEOUT_SECONDS

    called = []
    installed, reason = launcher.install_guided_dependencies(
        runner=lambda *args, **kwargs: called.append((args, kwargs))
    )
    assert installed is False
    assert "Repair or reinstall" in reason
    assert called == []


def test_qt_probe_emits_internal_failure_for_package_diagnostics(monkeypatch, capsys):
    widgets = ModuleType("PySide6.QtWidgets")

    class QApplication:
        @staticmethod
        def instance():
            raise RuntimeError("unavailable Qt platform library")

    widgets.QApplication = QApplication
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", widgets)
    monkeypatch.setattr(entrypoint, "record_startup_diagnostic", lambda _message: None)

    assert entrypoint._qt_probe() == 2
    assert "RuntimeError: unavailable Qt platform library" in capsys.readouterr().err


def test_frozen_package_self_test_checks_assets_qt_ocr_and_writable_data(
    monkeypatch, tmp_path
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for relative in entrypoint._PACKAGE_SELF_TEST_RESOURCES:
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "build-metadata.json":
            path.write_text(
                json.dumps({"revision": "a" * 40, "version": "test", "dirty": False}),
                encoding="utf-8",
            )
        elif relative == "THIRD-PARTY-NOTICES/index.json":
            path.write_text(
                json.dumps([{
                    "name": "Example dependency", "files": ["LICENSE.txt"],
                }]),
                encoding="utf-8",
            )
        else:
            path.write_text("resource", encoding="utf-8")
    state = tmp_path / "user-data"
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setattr(runtime_paths, "user_data_root", lambda **_kwargs: state)
    monkeypatch.setattr(entrypoint, "user_data_root", lambda **_kwargs: state)
    monkeypatch.setattr(entrypoint, "_probe_ocr_import", lambda: None)
    monkeypatch.setattr(launcher, "_probe_qapplication_startup", lambda: None)
    monkeypatch.setattr(entrypoint.sys, "stdout", None)

    assert entrypoint._package_self_test() == 0
    assert state.is_dir()
    assert not list(state.glob(".callimachus-self-test-*"))


def test_frozen_package_self_test_reports_missing_runtime_parts(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    state = tmp_path / "user-data"
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setattr(runtime_paths, "user_data_root", lambda **_kwargs: state)
    monkeypatch.setattr(entrypoint, "user_data_root", lambda **_kwargs: state)
    monkeypatch.setattr(entrypoint, "_probe_ocr_import", lambda: (_ for _ in ()).throw(ImportError()))
    monkeypatch.setattr(launcher, "_probe_qapplication_startup", lambda: "probe failed")
    monkeypatch.setattr(entrypoint.sys, "stderr", None)
    original_import = entrypoint.importlib.import_module

    def missing_desktop(name, *args, **kwargs):
        if name == entrypoint._COMMANDS["app"]:
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(entrypoint.importlib, "import_module", missing_desktop)

    assert entrypoint._package_self_test() == 2
    diagnostic = (state / "logs" / "startup.log").read_text(encoding="utf-8")
    assert "Qt startup probe" in diagnostic
    assert "OCR runtime import" in diagnostic
    assert "desktop entry point" in diagnostic


def test_package_revision_metadata_never_claims_a_git_snapshot(monkeypatch, tmp_path):
    from core.app import run as driver

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "build-metadata.json").write_text(
        json.dumps({"revision": "release-revision", "version": "v1", "dirty": True}),
        encoding="utf-8",
    )
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setattr(
        driver.subprocess,
        "run",
        lambda *_args, **_kwargs: type("GitResult", (), {
            "returncode": 0, "stdout": "unrelated-repository-revision\n",
        })(),
    )

    assert driver._code_revision() == "release-revision"
    snapshot = driver._debug_code_snapshot_identity("release-revision")
    assert snapshot["code_dirty"] is None
    assert snapshot["code_diff_sha256"] is None
    assert snapshot["code_snapshot_id"] is None
    assert "source checkout was modified" in snapshot["code_snapshot_error"]


def test_frozen_run_without_arguments_dispatches_desktop(monkeypatch):
    monkeypatch.setattr(entrypoint, "is_frozen", lambda: True)
    monkeypatch.setattr(entrypoint, "_prepare_frozen_desktop", lambda: True)
    monkeypatch.setattr(entrypoint, "_check_runtime_dependencies", lambda: None)
    monkeypatch.setattr(entrypoint, "_dispatch", lambda module, args, _prog: (module, args))
    monkeypatch.setattr(sys, "argv", ["Callimachus"])

    assert entrypoint._main() == ("core.app.commands.desktop", [])


def test_freeze_support_precedes_application_main():
    source = Path(entrypoint.__file__).read_text(encoding="utf-8")
    guard = source.split('if __name__ == "__main__":', 1)[1]
    assert guard.index("multiprocessing.freeze_support()") < guard.index("main()")
