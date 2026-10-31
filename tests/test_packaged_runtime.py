# tests/test_packaged_runtime.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import io
import os
import posixpath
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from core.app import platform_support, runtime_paths
from core.app.commands import desktop
from core.gui import launcher
import run as entrypoint


def _set_frozen(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_paths.sys, "_MEIPASS", str(root), raising=False)


def _preload_pipeline_without_settings(monkeypatch, request) -> None:
    if entrypoint._PIPELINE in sys.modules:
        return
    # The self-test simulates a frozen import; avoid reading dotenv or OS secrets
    # when that first import initializes the pipeline module in this process.
    original_loader = runtime_paths.load_environment_file
    monkeypatch.setattr(runtime_paths, "load_environment_file", lambda _path: None)
    pipeline_driver = entrypoint.importlib.import_module(entrypoint._PIPELINE)
    request.addfinalizer(
        lambda: setattr(pipeline_driver, "load_environment_file", original_loader)
    )


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ((10, 0, 17762), False),
        ((10, 0, 17763), True),
        ((10, 0, 19045), True),
        (None, False),
    ],
)
def test_windows_desktop_floor_uses_version_and_build(version, supported):
    assert platform_support.windows_version_supported(version) is supported


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("14.3.9", False),
        ("14.4", True),
        ("15.0", True),
        ("unknown", False),
    ],
)
def test_macos_desktop_floor(version, supported):
    assert platform_support.macos_version_supported(version) is supported


@pytest.mark.parametrize(
    ("reported", "supported"),
    [
        ("glibc 2.34", False),
        ("glibc 2.35", True),
        ("glibc 2.36.1", True),
        ("musl 1.2.5", False),
        (None, False),
    ],
)
def test_linux_startup_guard_checks_actual_glibc_floor(monkeypatch, reported, supported):
    monkeypatch.setattr(platform_support.platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform_support, "_linux_glibc_version", lambda: reported)

    error = platform_support.startup_compatibility_error()

    assert (error is None) is supported
    assert "Ubuntu" not in (error or "")
    if not supported:
        assert "GNU glibc 2.35 or later" in error


def test_msix_package_identity_uses_its_native_minimum_version_gate(monkeypatch):
    monkeypatch.setattr(platform_support.platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform_support, "_windows_has_package_identity", lambda: True)
    monkeypatch.setattr(
        platform_support, "_windows_version",
        lambda: pytest.fail("MSIX minimum version is enforced by its package manifest"),
    )

    assert platform_support.startup_compatibility_error() is None


@pytest.mark.parametrize(
    "argv",
    [["Callimachus"], ["Callimachus", "--qt-probe"]],
    ids=["before-settings", "before-qt-probe"],
)
def test_frozen_startup_rejects_unsupported_platform_before_runtime_loading(
    monkeypatch, argv,
):
    shown = []
    monkeypatch.setattr(entrypoint, "is_frozen", lambda: True)
    monkeypatch.setattr(entrypoint, "startup_compatibility_error", lambda: "unsupported OS")
    monkeypatch.setattr(entrypoint, "show_startup_error", shown.append)
    monkeypatch.setattr(
        entrypoint, "_load_frozen_environment",
        lambda: pytest.fail("settings and keyring must not load before the OS check"),
    )
    monkeypatch.setattr(
        entrypoint, "_qt_probe",
        lambda: pytest.fail("Qt must not initialize before the OS check"),
    )
    monkeypatch.setattr(sys, "argv", argv)

    assert entrypoint._main() == 2
    assert shown == ["unsupported OS"]


def test_source_cli_does_not_run_packaged_os_guard(monkeypatch):
    from core.app.providers import readiness as llm_readiness

    monkeypatch.setattr(entrypoint, "is_frozen", lambda: False)
    monkeypatch.setattr(
        entrypoint, "startup_compatibility_error",
        lambda: pytest.fail("source CLI is not subject to the desktop package guard"),
    )
    monkeypatch.setattr(llm_readiness, "main", lambda: 17)
    monkeypatch.setattr(sys, "argv", ["run.py", "--jury-readiness-probe"])

    assert entrypoint._main() == 17


def test_frozen_linux_cli_errors_name_the_public_launcher(monkeypatch, capsys):
    monkeypatch.setattr(entrypoint, "is_frozen", lambda: True)
    monkeypatch.setattr(entrypoint, "startup_compatibility_error", lambda: None)
    monkeypatch.setattr(entrypoint, "_load_frozen_environment", lambda: True)
    monkeypatch.setattr(entrypoint, "_check_runtime_dependencies", lambda: None)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "executable", "/opt/Callimachus/Callimachus.bin")
    monkeypatch.setattr(sys, "argv", ["Callimachus.bin", "unknown-command"])

    assert entrypoint._main() == 2
    assert "Callimachus: unknown command" in capsys.readouterr().err


def _posix_workspace_path(path: Path) -> str:
    return path.resolve().as_posix()


def _run_linux_launcher(
    tmp_path: Path, *, libc_report: str, arguments: list[str],
    internal_exit_code: int = 23, symlink_style: str | None = None,
):
    if sys.platform == "win32":
        pytest.skip("The POSIX launcher is qualified on native Linux/macOS builders")
    launcher = tmp_path / "Callimachus Bundle" / "Callimachus"
    launcher.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "packaging" / "linux-launcher.sh", launcher)
    internal = launcher.with_name("Callimachus.bin")
    internal.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CALLIMACHUS_TEST_ARGS\"\n"
        "exit \"$CALLIMACHUS_TEST_EXIT\"\n",
        encoding="utf-8", newline="\n",
    )
    fake_bin = tmp_path / "fake commands"
    fake_bin.mkdir()
    (fake_bin / "getconf").write_text(
        "#!/bin/sh\nprintf '%s\\n' " + shlex.quote(libc_report) + "\n",
        encoding="utf-8", newline="\n",
    )
    for command in ("zenity", "kdialog"):
        (fake_bin / command).write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    observed_args = tmp_path / "arguments.txt"
    runner = tmp_path / "run launcher.sh"
    launch_command = _posix_workspace_path(launcher)
    path_prefix = _posix_workspace_path(fake_bin)
    link_setup = ""
    if symlink_style is not None:
        first_alias = tmp_path / "launcher alias one" / "Callimachus"
        second_alias = tmp_path / "launcher alias two" / "Callimachus"
        first_alias.parent.mkdir()
        second_alias.parent.mkdir()
        if symlink_style == "relative":
            first_target = posixpath.relpath(
                _posix_workspace_path(launcher),
                _posix_workspace_path(first_alias.parent),
            )
            second_target = posixpath.relpath(
                _posix_workspace_path(first_alias),
                _posix_workspace_path(second_alias.parent),
            )
        else:
            first_target = _posix_workspace_path(launcher)
            second_target = _posix_workspace_path(first_alias)
        link_setup = (
            f"ln -s {shlex.quote(first_target)} {shlex.quote(_posix_workspace_path(first_alias))}\n"
            f"ln -s {shlex.quote(second_target)} {shlex.quote(_posix_workspace_path(second_alias))}\n"
        )
        launch_command = "Callimachus"
        path_prefix += ":" + _posix_workspace_path(second_alias.parent)
    runner.write_text(
        "#!/bin/bash\n"
        f"PATH={shlex.quote(path_prefix)}:$PATH\nexport PATH\n"
        f"CALLIMACHUS_TEST_ARGS={shlex.quote(_posix_workspace_path(observed_args))}\n"
        f"CALLIMACHUS_TEST_EXIT={internal_exit_code}\n"
        "export CALLIMACHUS_TEST_ARGS CALLIMACHUS_TEST_EXIT\n"
        + "chmod +x " + " ".join(shlex.quote(_posix_workspace_path(path)) for path in (
            launcher, internal, fake_bin / "getconf", fake_bin / "zenity", fake_bin / "kdialog",
        )) + "\n"
        + link_setup
        + f"exec {shlex.quote(launch_command)} \"$@\"\n",
        encoding="utf-8", newline="\n",
    )
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("bash is unavailable for the POSIX launcher contract")
    command = [shell, str(runner), *arguments]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return completed, observed_args


def test_linux_launcher_checks_glibc_before_executable_and_forwards_arguments(tmp_path):
    completed, observed_args = _run_linux_launcher(
        tmp_path,
        libc_report="glibc 2.35",
        arguments=["--input", "papers/with spaces.pdf", "--resume"],
    )

    assert completed.returncode == 23
    assert observed_args.read_text(encoding="utf-8").splitlines() == [
        "--input", "papers/with spaces.pdf", "--resume",
    ]


def test_linux_launcher_with_empty_path_uses_system_getconf_and_preserves_child_path(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Requires the native Ubuntu Linux release baseline")
    reported = os.confstr("CS_GNU_LIBC_VERSION")
    assert reported is not None and reported.startswith("glibc "), reported
    version = tuple(int(part) for part in reported.removeprefix("glibc ").split(".")[:2])
    assert version >= (2, 35), f"Requires GNU glibc 2.35 or later, found {reported}"

    launcher = tmp_path / "Callimachus Bundle" / "Callimachus"
    launcher.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "packaging" / "linux-launcher.sh", launcher)
    internal = launcher.with_name("Callimachus.bin")
    internal.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"${PATH-<unset>}\" > \"$CALLIMACHUS_TEST_OUTPUT\"\n"
        "printf '%s\\n' \"$@\" >> \"$CALLIMACHUS_TEST_OUTPUT\"\n"
        "exit 23\n",
        encoding="utf-8", newline="\n",
    )
    launcher.chmod(0o755)
    internal.chmod(0o755)
    observed = tmp_path / "launcher-output.txt"
    arguments = ["--input", "papers/with spaces.pdf", "--resume"]
    completed = subprocess.run(
        [str(launcher), *arguments],
        env={"PATH": "", "CALLIMACHUS_TEST_OUTPUT": str(observed)},
        capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 23, completed.stderr
    assert observed.read_text(encoding="utf-8").splitlines() == [
        "", *arguments,
    ]


@pytest.mark.parametrize("libc_report", ["glibc 2.34", "musl 1.2.5", "unrecognized"])
def test_linux_launcher_refuses_old_or_unknown_libc_before_running_binary(
    tmp_path, libc_report,
):
    completed, observed_args = _run_linux_launcher(
        tmp_path, libc_report=libc_report, arguments=["--input", "paper.pdf"],
    )

    assert completed.returncode == 2
    assert "GNU glibc 2.35 or later" in completed.stderr
    assert not observed_args.exists()


@pytest.mark.parametrize("symlink_style", ["relative", "absolute"])
@pytest.mark.parametrize("libc_report, expected_exit", [("glibc 2.34", 2), ("glibc 2.35", 23)])
def test_linux_launcher_resolves_symlink_chain_in_another_directory(
    tmp_path, symlink_style, libc_report, expected_exit,
):
    completed, observed_args = _run_linux_launcher(
        tmp_path,
        libc_report=libc_report,
        arguments=["--input", "papers/with spaces.pdf", "--resume"],
        symlink_style=symlink_style,
    )

    assert completed.returncode == expected_exit
    if expected_exit == 2:
        assert "GNU glibc 2.35 or later" in completed.stderr
        assert not observed_args.exists()
    else:
        assert observed_args.read_text(encoding="utf-8").splitlines() == [
            "--input", "papers/with spaces.pdf", "--resume",
        ]


@pytest.mark.parametrize("frozen", [False, True])
def test_readiness_worker_dispatch_preserves_supplied_environment(monkeypatch, frozen):
    from core.app.providers import readiness as llm_readiness

    monkeypatch.setattr(entrypoint, "is_frozen", lambda: frozen)
    monkeypatch.setattr(entrypoint.sys, "argv", ["callimachus", "--jury-readiness-probe"])
    monkeypatch.setattr(entrypoint, "_load_frozen_environment",
                        lambda: pytest.fail("must not reload a different settings snapshot"))
    monkeypatch.setattr(llm_readiness, "main", lambda: 17)
    assert entrypoint._main() == 17


def test_runtime_paths_use_platform_data_root_for_frozen_app(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    local = tmp_path / "Local App Data"
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setattr(runtime_paths.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("CALLIMACHUS_DATA_DIR", raising=False)

    assert runtime_paths.is_frozen() is True
    assert runtime_paths.resource_root() == bundle
    assert runtime_paths.user_data_root() == local / "Callimachus"
    assert runtime_paths.environment_file() == local / "Callimachus" / "settings.conf"
    assert runtime_paths.settings_template_file() == bundle / "settings.defaults"
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


def test_driver_handles_missing_windowed_stdin_as_noninteractive(monkeypatch, tmp_path):
    isolated_source = tmp_path / "source-without-env"
    original_resource_root = runtime_paths.resource_root
    monkeypatch.setattr(runtime_paths, "resource_root", lambda: isolated_source)
    from core.app import run as driver
    # The import-time dotenv bootstrap above must see only the isolated path;
    # keep later packaged-runtime checks pointed at the real fixture bundle.
    driver.resource_root = original_resource_root

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


def test_frozen_environment_initialization_does_not_touch_bundle_env(
    monkeypatch, tmp_path,
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    template = (
        b"OPENAI_API_KEY=synthetic-template-secret\n"
        b"CITATION_VERIFIER_VERIFY_BACKENDS=\n"
    )
    (bundle / "settings.defaults").write_bytes(template)
    (bundle / ".env.example").write_text(
        "BUNDLE_ALIAS_SECRET=must-not-be-used\n", encoding="utf-8",
    )
    (bundle / ".env").write_text("BUNDLE_SECRET=do-not-load\n", encoding="utf-8")
    state = tmp_path / "user-data"
    state.mkdir()
    (state / ".env").write_text(
        "OPENAI_API_KEY=legacy-user-secret\n", encoding="utf-8",
    )
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setattr(runtime_paths.platform, "system", lambda: "Linux")
    monkeypatch.setenv("CALLIMACHUS_DATA_DIR", str(state))
    monkeypatch.setattr(runtime_paths, "saved_secret_values", lambda **_kwargs: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BUNDLE_SECRET", raising=False)
    monkeypatch.delenv("BUNDLE_ALIAS_SECRET", raising=False)

    assert entrypoint._ensure_local_env(root=bundle) is False
    assert entrypoint._load_frozen_environment() is True
    settings = (state / "settings.conf").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=" in settings
    assert "synthetic-template-secret" not in settings
    assert "CITATION_VERIFIER_VERIFY_BACKENDS=" in settings
    assert (state / ".env").read_text(encoding="utf-8") == (
        "OPENAI_API_KEY=legacy-user-secret\n"
    )
    assert (bundle / ".env").read_text(encoding="utf-8") == "BUNDLE_SECRET=do-not-load\n"
    assert "OPENAI_API_KEY" not in os.environ
    assert "BUNDLE_SECRET" not in os.environ
    assert "BUNDLE_ALIAS_SECRET" not in os.environ


def test_frozen_desktop_first_launch_initializes_user_env_from_template(
    monkeypatch, tmp_path
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    template = (
        "OPENAI_API_KEY=synthetic-template-secret\n"
        "CALLIMACHUS_FIRST_LAUNCH_TEST=ready\n"
    )
    (bundle / "settings.defaults").write_text(template, encoding="utf-8")
    (bundle / ".env.example").write_text(
        "CALLIMACHUS_FIRST_LAUNCH_TEST=wrong-alias\n", encoding="utf-8",
    )
    (bundle / ".env").write_text("BUNDLE_SECRET=do-not-load\n", encoding="utf-8")
    state = tmp_path / "user-data"
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setattr(runtime_paths, "saved_secret_values", lambda **_kwargs: {})
    monkeypatch.setenv("CALLIMACHUS_DATA_DIR", str(state))
    monkeypatch.delenv("CALLIMACHUS_FIRST_LAUNCH_TEST", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert entrypoint._load_frozen_environment() is True

    settings = (state / "settings.conf").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=\n" in settings
    assert "synthetic-template-secret" not in settings
    assert "CALLIMACHUS_FIRST_LAUNCH_TEST=ready" in settings
    assert os.environ["CALLIMACHUS_FIRST_LAUNCH_TEST"] == "ready"
    assert "OPENAI_API_KEY" not in os.environ
    assert (bundle / ".env").read_text(encoding="utf-8") == "BUNDLE_SECRET=do-not-load\n"
    assert (bundle / ".env.example").read_text(encoding="utf-8") == (
        "CALLIMACHUS_FIRST_LAUNCH_TEST=wrong-alias\n"
    )


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
    (bundle / "settings.defaults").write_text(
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
    monkeypatch.setattr(
        desktop,
        "catalog_verify_preview",
        lambda *_args, **_kwargs: {"available": True, "overlay": {}},
    )
    captured = {}
    monkeypatch.setitem(
        sys.modules,
        "core.gui.desktop",
        SimpleNamespace(
            _load_language_preference=lambda: None,
            _load_settings_environment_preferences=lambda: {},
            run_desktop=lambda **kwargs: captured.update(kwargs) or 0,
        ),
    )
    monkeypatch.setenv("CALLIMACHUS_DATA_DIR", str(state))
    monkeypatch.delenv("CITATION_VERIFIER_VERIFY_BACKENDS", raising=False)
    monkeypatch.setattr(runtime_paths, "saved_secret_values", lambda **_kwargs: {})

    assert desktop.main([]) == 0
    start = captured["command_builder"]("paper.pdf", "standard", "medium")
    resume = captured["resume_command_builder"]("run-a")
    report = captured["report_callback"]({"run_dir": "run-a"})
    fork = captured["verify_fork_command_builder"](
        {"run_dir": "parent", "paper": "paper", "references_only": False},
        {"jury1": ["openai:model-a"], "jury2": []},
        {},
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
        state / "settings.conf"
    )
    assert (state / "settings.conf").is_file()
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


def test_historical_export_probe_uses_isolated_key_provider_and_cleans_state(
    monkeypatch, tmp_path,
):
    from core.infra.integrity import signing

    state = tmp_path / "user-data"

    def unexpected_key_read():
        raise AssertionError("historical export probe read an external signing key")

    monkeypatch.setattr(signing, "load_key", unexpected_key_read)
    entrypoint._probe_historical_export(state)

    assert signing.load_key is unexpected_key_read
    assert not list(state.glob(".callimachus-historical-smoke-*"))


def test_library_import_probe_round_trips_pdf_and_docx(tmp_path):
    state = tmp_path / "user-data"

    entrypoint._probe_library_import(state)

    assert state.is_dir()
    assert not list(state.glob(".callimachus-library-smoke-*"))


def test_frozen_package_self_test_checks_assets_qt_ocr_and_writable_data(
    monkeypatch, request, tmp_path
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
    _preload_pipeline_without_settings(monkeypatch, request)
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setattr(runtime_paths, "user_data_root", lambda **_kwargs: state)
    monkeypatch.setattr(entrypoint, "user_data_root", lambda **_kwargs: state)
    monkeypatch.setattr(entrypoint, "_probe_ocr_import", lambda: None)
    monkeypatch.setattr(launcher, "_probe_qapplication_startup", lambda: None)
    library_probe_calls = []
    history_probe_calls = []
    gui_probe_calls = []
    monkeypatch.setattr(
        entrypoint, "_probe_library_import", lambda parent: library_probe_calls.append(parent),
    )
    monkeypatch.setattr(
        entrypoint, "_probe_historical_export", lambda parent: history_probe_calls.append(parent),
    )
    monkeypatch.setattr(
        entrypoint, "_probe_desktop_gui", lambda parent: gui_probe_calls.append(parent),
    )
    monkeypatch.setattr(entrypoint.sys, "stdout", None)

    assert entrypoint._package_self_test() == 0
    assert library_probe_calls == [state]
    assert history_probe_calls == [state]
    assert gui_probe_calls == [state]
    assert state.is_dir()
    assert not list(state.glob(".callimachus-self-test-*"))
    assert not list(state.glob(".callimachus-library-smoke-*"))
    assert not list(state.glob(".callimachus-historical-smoke-*"))


def test_frozen_package_self_test_reports_missing_runtime_parts(monkeypatch, request, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    state = tmp_path / "user-data"
    _preload_pipeline_without_settings(monkeypatch, request)
    _set_frozen(monkeypatch, bundle)
    monkeypatch.setattr(runtime_paths, "user_data_root", lambda **_kwargs: state)
    monkeypatch.setattr(entrypoint, "user_data_root", lambda **_kwargs: state)
    monkeypatch.setattr(entrypoint, "_probe_ocr_import", lambda: (_ for _ in ()).throw(ImportError()))
    monkeypatch.setattr(
        entrypoint, "_probe_library_import",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("round-trip failed")),
    )
    monkeypatch.setattr(
        entrypoint, "_probe_historical_export",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("historical export failed")),
    )
    monkeypatch.setattr(
        entrypoint, "_probe_desktop_gui",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("GUI initialization failed")),
    )
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
    assert "Library PDF/DOCX round-trip" in diagnostic
    assert "Historical report export" in diagnostic
    assert "GUI shell/settings initialization" in diagnostic


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
    monkeypatch.setattr(entrypoint, "_load_frozen_environment", lambda: True)
    monkeypatch.setattr(entrypoint, "_check_runtime_dependencies", lambda: None)
    monkeypatch.setattr(entrypoint, "_launch_desktop", lambda module, args, _prog: (module, args))
    monkeypatch.setattr(sys, "argv", ["Callimachus"])

    assert entrypoint._main() == ("core.app.commands.desktop", [])


def test_source_app_uses_splash_without_changing_other_commands(monkeypatch):
    monkeypatch.setattr(entrypoint, "is_frozen", lambda: False)
    monkeypatch.setattr(entrypoint, "_check_runtime_dependencies", lambda: None)
    calls = []
    monkeypatch.setattr(entrypoint, "_launch_desktop", lambda *args: calls.append(("splash", args)) or 7)
    monkeypatch.setattr(entrypoint, "_dispatch", lambda *args: calls.append(("dispatch", args)) or 9)

    monkeypatch.setattr(sys, "argv", ["run.py", "app", "--language", "it"])
    assert entrypoint._main() == 7
    assert calls == [("splash", ("core.app.commands.desktop", ["--language", "it"], "run.py app"))]

    monkeypatch.setattr(sys, "argv", ["run.py", "parse", "--help"])
    assert entrypoint._main() == 9
    assert calls[-1] == ("dispatch", (entrypoint._COMMANDS["parse"], ["--help"], "run.py parse"))

    monkeypatch.setattr(sys, "argv", ["run.py", "app", "--help"])
    assert entrypoint._main() == 9
    assert calls[-1] == ("dispatch", ("core.app.commands.desktop", ["--help"], "run.py app"))


def test_missing_gui_dependency_keeps_desktop_diagnosis(monkeypatch):
    from core.gui import startup_splash

    def missing_gui(**_kwargs):
        raise ModuleNotFoundError("No module named 'PySide6'", name="PySide6")

    calls = []
    monkeypatch.setattr(startup_splash, "run_with_splash", missing_gui)
    monkeypatch.setattr(entrypoint, "_dispatch", lambda *args: calls.append(args) or 2)
    assert entrypoint._launch_desktop("core.app.commands.desktop", [], "run.py app") == 2
    assert calls == [("core.app.commands.desktop", [], "run.py app")]


def test_freeze_support_precedes_application_main():
    source = Path(entrypoint.__file__).read_text(encoding="utf-8")
    guard = source.split('if __name__ == "__main__":', 1)[1]
    assert guard.index("multiprocessing.freeze_support()") < guard.index("main()")


@pytest.mark.parametrize("failure", ["none", "missing-shell", "broken-settings", "qt-callback", "dialog-timeout", "broken-fetch-embedding"])
def test_packaged_gui_probe_initializes_offscreen_without_personal_state(tmp_path, failure):
    pytest.importorskip("PySide6")
    state = tmp_path / "probe-state"
    code = r"""
import builtins, pathlib, socket, sys
from contextlib import ExitStack
from unittest import mock
import run
from core.app import runtime_paths
from core.app.settings import credentials
from PySide6 import QtCore
state = pathlib.Path(sys.argv[1])
failure = sys.argv[2]
violations = []
settings_calls = []
def forbidden(*args, **kwargs):
    violations.append(True)
    raise AssertionError("GUI probe attempted a personal-state or network operation")
settings_class = QtCore.QSettings
def fenced_settings(*args, **kwargs):
    assert len(args) == 2 and pathlib.Path(args[0]).resolve().is_relative_to(state.resolve()), args
    assert args[1] == settings_class.Format.IniFormat
    settings_calls.append(True)
    return settings_class(*args, **kwargs)
fenced_settings.Status = settings_class.Status
fenced_settings.Format = settings_class.Format
original_import = builtins.__import__
def checked_import(name, globals=None, locals=None, fromlist=(), level=0):
    if failure == "missing-shell" and name == "core.gui.shell" and "scroll" in fromlist:
        raise ModuleNotFoundError("synthetic missing shell module")
    return original_import(name, globals, locals, fromlist, level)
with mock.patch.object(credentials, "_trusted_backend", forbidden), \
     mock.patch.object(runtime_paths, "load_environment_file", forbidden), \
     mock.patch.object(socket.socket, "connect", forbidden), \
     mock.patch.object(QtCore, "QSettings", fenced_settings), \
     mock.patch.object(builtins, "__import__", checked_import), \
     ExitStack() as stack:
    if failure in {"broken-settings", "qt-callback", "dialog-timeout"}:
        from core.gui.settings import transfer
        original_options = transfer.options
        def broken(*args, **kwargs):
            raise RuntimeError("synthetic settings initialization failure")
        def callback_error():
            raise RuntimeError("synthetic Qt callback failure")
        def with_callback(*args, **kwargs):
            QtCore.QTimer.singleShot(0, callback_error)
            return original_options(*args, **kwargs)
        def with_timeout(*args, **kwargs):
            result = original_options(*args, **kwargs)
            # A second modal after the expected cancellation must not qualify.
            from PySide6.QtWidgets import QDialog
            dialog = QDialog(args[0])
            dialog.setObjectName("unexpectedModal")
            dialog.exec()
            dialog.deleteLater()
            return result
        replacement = {"broken-settings": broken, "qt-callback": with_callback,
                       "dialog-timeout": with_timeout}[failure]
        stack.enter_context(mock.patch.object(transfer, "options", replacement))
    if failure == "broken-fetch-embedding":
        from core.gui import desktop
        original_create = desktop.create_desktop_window
        def with_broken_embedding(*args, **kwargs):
            window = original_create(*args, **kwargs)
            def broken_embedding(_guided):
                raise RuntimeError("synthetic Fetch embedding failure")
            window._embed_guided_fetch = broken_embedding
            return window
        stack.enter_context(mock.patch.object(desktop, "create_desktop_window", with_broken_embedding))
    try:
        run._probe_desktop_gui(state)
    except (ModuleNotFoundError, RuntimeError) as exc:
        expected = {"missing-shell": "synthetic missing shell module",
                    "broken-settings": "synthetic settings initialization failure",
                    "qt-callback": "uncaught Qt callback errors",
                    "dialog-timeout": "settings transfer timed out",
                    "broken-fetch-embedding": "synthetic Fetch embedding failure"}
        assert failure in expected and expected[failure] in str(exc), exc
    else:
        assert failure == "none", "GUI probe swallowed an initialization failure"
assert not violations
assert settings_calls or failure == "missing-shell"
assert QtCore.QSettings is settings_class
assert state.is_dir() and list(state.iterdir()) == []
print("isolated GUI probe completed")
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(state), failure],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "isolated GUI probe completed" in result.stdout
    assert "Traceback" not in result.stderr
    assert list(state.iterdir()) == []
