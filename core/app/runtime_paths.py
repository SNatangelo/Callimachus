# core/app/runtime_paths.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve bundled resources, writable application data, and child commands."""
from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
import platform
import sys
from typing import Any


_LOADED_ENV_VALUES: dict[str, str] = {}


def is_frozen() -> bool:
    """Return whether this process runs from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Return the read-only source or PyInstaller resource directory."""
    if is_frozen():
        bundled = getattr(sys, "_MEIPASS", None)
        if bundled:
            return Path(bundled).resolve()
    return Path(__file__).resolve().parents[2]


def user_data_root(*, source_root: str | os.PathLike[str] | None = None) -> Path:
    """Return the per-user writable root for frozen builds.

    Source checkouts intentionally continue to keep ``.env`` and ``runs`` in
    their own worktree. A frozen application stores these under the operating
    system's normal per-user data directory.
    """
    if not is_frozen():
        return Path(source_root).resolve() if source_root is not None else resource_root()

    override = os.environ.get("CALLIMACHUS_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    home = Path.home()
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base_path = Path(base).expanduser() if base else home / "AppData" / "Local"
    elif system == "Darwin":
        base_path = home / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME", "").strip()
        base_path = Path(base).expanduser() if base else home / ".local" / "share"
    return (base_path / "Callimachus").resolve()


def ensure_user_data_root(*, source_root: str | os.PathLike[str] | None = None) -> Path:
    root = user_data_root(source_root=source_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def environment_file(*, source_root: str | os.PathLike[str] | None = None) -> Path:
    name = "settings.conf" if is_frozen() else ".env"
    return user_data_root(source_root=source_root) / name


def settings_template_file(
    *, source_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the read-only settings inventory template for this runtime."""
    root = Path(source_root).resolve() if source_root is not None else resource_root()
    name = "settings.defaults" if is_frozen() else ".env.example"
    return root / name


def saved_secret_values(
    *, names: set[str] | None = None,
    overrides: Mapping[str, str] | None = None,
    source_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Load frozen-build secrets from the OS vault, never from the settings file."""
    if not is_frozen():
        return {}
    from core.app.settings.credentials import (
        load_secret_values,
        secret_names_from_template,
    )

    selected = secret_names_from_template(
        settings_template_file(source_root=source_root)
    )
    if names is not None:
        selected.intersection_update(names)
    process_overrides = os.environ if overrides is None else overrides
    missing_from_process = selected - set(process_overrides)
    return load_secret_values(missing_from_process)


def runs_directory(*, source_root: str | os.PathLike[str] | None = None) -> Path:
    return user_data_root(source_root=source_root) / "runs"


def load_environment_file(path: str | os.PathLike[str]) -> None:
    """Load source dotenv or frozen non-secret settings beneath process overrides."""
    frozen = is_frozen()
    env_path = environment_file() if frozen else Path(path)
    from core.app.settings.credentials import is_secret_setting

    if env_path.is_file():
        with env_path.open(encoding="utf-8") as source:
            for line in source:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith(("export ", "export\t")):
                    line = line[len("export"):].lstrip()
                key, value = (piece.strip() for piece in line.split("=", 1))
                if not key or (frozen and is_secret_setting(key)):
                    continue
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                else:
                    comment = value.find(" #")
                    if comment != -1:
                        value = value[:comment].rstrip()
                if key not in os.environ:
                    os.environ[key] = value
                    _LOADED_ENV_VALUES[key] = value

    if frozen:
        for key, value in saved_secret_values().items():
            if key not in os.environ:
                os.environ[key] = value
                _LOADED_ENV_VALUES[key] = value


def process_environment_without_dotenv() -> dict[str, str]:
    """Exclude values injected from .env, retaining genuine process overrides."""
    values = dict(os.environ)
    for name, loaded_value in _LOADED_ENV_VALUES.items():
        if values.get(name) == loaded_value:
            values.pop(name, None)
    return values


def refresh_loaded_environment(updates: Mapping[str, str]) -> None:
    """Keep inherited child-process settings in sync after an in-app .env edit."""
    for name, value in updates.items():
        loaded_value = _LOADED_ENV_VALUES.get(name)
        if loaded_value is not None and os.environ.get(name) == loaded_value:
            os.environ[name] = str(value)
            _LOADED_ENV_VALUES[name] = str(value)


def clear_loaded_environment(names: Iterable[str]) -> None:
    """Remove previously loaded settings that were deleted by import or reset."""
    for name in names:
        loaded_value = _LOADED_ENV_VALUES.pop(name, None)
        if loaded_value is not None and os.environ.get(name) == loaded_value:
            os.environ.pop(name, None)


def application_argv(
    *args: str,
    source_root: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Build a child command for the current source or packaged application."""
    command = [sys.executable]
    if not is_frozen():
        root = Path(source_root).resolve() if source_root is not None else resource_root()
        command.append(str(root / "run.py"))
    command.extend(str(arg) for arg in args)
    return command


def read_build_metadata(
    *, root: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    """Read immutable metadata written by the release build, if present."""
    metadata_path = (Path(root) if root is not None else resource_root()) / "build-metadata.json"
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    revision = value.get("revision")
    if not isinstance(revision, str) or not revision.strip():
        return None
    return value


def record_startup_diagnostic(message: str) -> Path | None:
    """Append a user-safe startup diagnostic without recording environment data."""
    try:
        log_path = user_data_root() / "logs" / "startup.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as target:
            from datetime import datetime, timezone

            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            target.write(f"{timestamp} {message.rstrip()}\n")
        return log_path
    except OSError:
        return None


def show_startup_error(message: str, *, title: str = "Callimachus") -> None:
    """Show a short actionable error without requiring Qt to have initialized."""
    record_startup_diagnostic(message)
    shown = False
    if platform.system() == "Windows":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10 | 0x40000)
            shown = True
        except Exception:
            pass
    elif platform.system() == "Darwin":
        try:
            import subprocess

            safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
            safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
            result = subprocess.run(
                ["osascript", "-e", f'display alert "{safe_title}" message "{safe_message}" as critical'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            shown = result.returncode == 0
        except Exception:
            pass
    else:
        import subprocess

        for command in (
            ["zenity", "--error", f"--title={title}", f"--text={message}"],
            ["kdialog", "--error", message, "--title", title],
        ):
            try:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                if result.returncode == 0:
                    shown = True
                    break
            except (OSError, subprocess.SubprocessError):
                continue

    if not shown:
        stream = getattr(sys, "stderr", None)
        if stream is not None:
            try:
                stream.write(f"{title}: {message}\n")
                stream.flush()
            except (OSError, ValueError):
                pass
