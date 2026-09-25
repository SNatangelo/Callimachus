# core/gui/launcher.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Lazy optional-dependency launcher for the guided Fetch desktop window."""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

from core.app.runtime_paths import application_argv, is_frozen


GUIDED_DEPENDENCIES = {"PySide6": "PySide6", "playwright": "playwright"}
GUIDED_REQUIREMENTS = Path(__file__).resolve().parents[2] / "requirements-gui.txt"
_QT_STARTUP_PROBE_TIMEOUT_SECONDS = 5
_QT_STARTUP_PROBE = (
    "from PySide6.QtWidgets import QApplication\n"
    "app = QApplication.instance() or QApplication([])\n"
    "app.quit()\n"
)


@dataclass(frozen=True)
class GuiLaunchResult:
    started: bool
    reason: str | None = None


def missing_guided_dependencies() -> list[str]:
    """Return missing optional distributions without importing GUI/browser code."""
    missing = []
    for distribution, module in GUIDED_DEPENDENCIES.items():
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            available = False
        if not available:
            missing.append(distribution)
    return missing


def install_guided_dependencies(*, runner=subprocess.run) -> tuple[bool, str | None]:
    """Install the opt-in Python extra; never install or download a browser."""
    if is_frozen():
        return False, (
            "Guided Fetch components cannot be installed from the packaged app. "
            "Repair or reinstall the current Callimachus release."
        )
    if not GUIDED_REQUIREMENTS.is_file():
        return False, f"optional requirements file not found: {GUIDED_REQUIREMENTS}"
    try:
        completed = runner(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                str(GUIDED_REQUIREMENTS),
            ],
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"optional dependency installation failed: {exc}"
    if completed.returncode != 0:
        return False, f"optional dependency installation exited with {completed.returncode}"
    importlib.invalidate_caches()
    remaining = missing_guided_dependencies()
    if remaining:
        return False, "optional dependencies are still missing: " + ", ".join(remaining)
    return True, None


def _linux_gui_environment_available() -> bool:
    """Return whether Linux has a display or an explicitly selected Qt platform."""
    return any(
        os.environ.get(name)
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM")
    )


def _probe_qapplication_startup(*, runner=subprocess.run) -> str | None:
    """Contain native Qt platform-plugin startup failures in a child process."""
    command = (
        application_argv("--qt-probe")
        if is_frozen()
        else [sys.executable, "-c", _QT_STARTUP_PROBE]
    )
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=_QT_STARTUP_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "Qt startup probe timed out; guided Fetch was not launched."
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Qt startup probe failed; guided Fetch was not launched: {exc}"
    if completed.returncode != 0:
        return (
            "Qt could not initialize a graphical platform "
            f"(startup probe exited with {completed.returncode}); guided Fetch was not launched."
        )
    return None


def launch_guided_fetch(
    run_dir: str,
    *,
    submit_source: Callable[[str, dict[str, Any]], Any],
    proceed: Callable[[], Any],
    discard_source: Callable[[str], Any] | None = None,
    submit_identity_review: Callable[[str, str, str, str], Any] | None = None,
    identity_review_allowed: bool = True,
) -> GuiLaunchResult:
    """Launch the optional Qt window, or report the missing optional extra."""
    try:
        # ``guided_fetch`` itself is deliberately import-safe, so probe the
        # optional dependency here at the explicit launch boundary.
        import PySide6  # noqa: F401
        from .guided_fetch import run_guided_fetch_window
    except ModuleNotFoundError as exc:
        if exc.name not in GUIDED_DEPENDENCIES:
            raise
        if is_frozen():
            return GuiLaunchResult(
                False,
                "A Guided Fetch component is missing from this Callimachus release. "
                "Repair or reinstall the current release.",
            )
        return GuiLaunchResult(
            False,
            "Guided Fetch requires PySide6 and Playwright; install requirements-gui.txt.",
        )
    if sys.platform.startswith("linux") and not _linux_gui_environment_available():
        return GuiLaunchResult(
            False,
            "No Linux graphical display is available; guided Fetch was not launched.",
        )
    probe_error = _probe_qapplication_startup()
    if probe_error is not None:
        return GuiLaunchResult(False, probe_error)
    try:
        run_guided_fetch_window(
            run_dir,
            submit_source=submit_source,
            proceed=proceed,
            discard_source=discard_source,
            submit_identity_review=submit_identity_review,
            identity_review_allowed=identity_review_allowed,
        )
    except Exception as exc:
        return GuiLaunchResult(False, f"Guided Fetch could not be launched: {exc}")
    return GuiLaunchResult(True)
