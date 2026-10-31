# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded CLI lookup for desktop processes without a login-shell PATH."""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
import posixpath
import shutil
import stat
import sys
from collections.abc import Mapping


def _environment(environ: Mapping[str, str] | None) -> dict[str, str]:
    if sys.platform == "win32":
        values = {key.upper(): value for key, value in os.environ.items()}
        if environ is not None:
            values.update({key.upper(): value for key, value in environ.items()})
    else:
        values = dict(os.environ)
        if environ is not None:
            values.update(environ)
    return values


def _directories(command: str, values: Mapping[str, str]) -> tuple[list[str], list[str]]:
    windows = sys.platform == "win32"
    path = ntpath if windows else posixpath
    separator = ";" if windows else ":"
    explicit = [item for item in values.get("PATH", "").split(separator) if path.isabs(item)]
    raw_home = values.get("USERPROFILE" if windows else "HOME", "")
    home = raw_home if path.isabs(raw_home) else str(Path.home())
    standard = [path.join(home, ".local", "bin"), path.join(home, "bin"),
                path.join(home, ".bun", "bin"), path.join(home, ".npm-global", "bin")]
    prefix = values.get("NPM_CONFIG_PREFIX") or values.get("npm_config_prefix")
    if prefix and path.isabs(prefix):
        standard.append(prefix if windows else path.join(prefix, "bin"))
    if windows:
        roaming = values.get("APPDATA") or path.join(home, "AppData", "Roaming")
        local = values.get("LOCALAPPDATA") or path.join(home, "AppData", "Local")
        programs = values.get("PROGRAMFILES") or r"C:\Program Files"
        standard += [path.join(roaming, "npm"), path.join(programs, "nodejs")]
        if command == "codex":
            standard.append(path.join(local, "Programs", "OpenAI", "Codex", "bin"))
        if command == "agy":
            standard.append(path.join(local, "agy", "bin"))
        if command == "ollama":
            standard.append(path.join(local, "Programs", "Ollama"))
        if command == "ft":
            # FreeToken Desktop keeps its native engine outside PATH. Resolve only
            # the documented managed venv executable; never search its tree.
            standard.append(path.join(local, "FreeToken", "venv", "Scripts"))
    elif sys.platform == "darwin":
        standard += ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/bin"]
        if command == "ollama":
            standard += ["/Applications/Ollama.app/Contents/Resources",
                         path.join(home, "Applications", "Ollama.app", "Contents", "Resources")]
    else:
        standard += ["/usr/local/bin", "/usr/bin", "/bin", "/snap/bin",
                     "/home/linuxbrew/.linuxbrew/bin", path.join(home, ".linuxbrew", "bin")]
    if command == "opencode":
        standard.append(path.join(home, ".opencode", "bin"))
    # Fixed paths only: never scan project directories, version trees or profiles.
    return list(dict.fromkeys(explicit)), [item for item in dict.fromkeys(standard)
                                          if path.isabs(item) and item not in explicit]


def _trusted_directory(directory: str) -> bool:
    if sys.platform == "win32":
        return True  # Windows ACLs and executable checks are enforced by the OS.
    try:
        info = os.stat(directory)
        return (stat.S_ISDIR(info.st_mode) and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                and info.st_uid in {0, os.getuid()})
    except OSError:
        return False


def find_cli(command: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Honor absolute PATH entries before fixed, executable installer locations."""
    if not command or "/" in command or "\\" in command:
        return None
    values = _environment(environ)
    explicit, standard = _directories(command.removesuffix(".exe").removesuffix(".cmd"), values)
    for fallback, directories in ((False, explicit), (True, standard)):
        for directory in directories:
            if fallback and not _trusted_directory(directory):
                continue
            if sys.platform == "win32":
                # Absolute commands avoid Windows' implicit current-directory search.
                found = shutil.which(ntpath.join(directory, command), path=directory)
                found = _compatible_windows_cli(found, values)
                if found is None and not ntpath.splitext(command)[1]:
                    found = _compatible_windows_cli(
                        shutil.which(ntpath.join(directory, command + ".exe"), path=directory), values,
                    )
            else:
                found = shutil.which(command, path=directory)
            if found:
                if fallback and sys.platform != "win32":
                    try:
                        info = os.stat(found)
                        if (not stat.S_ISREG(info.st_mode) or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                                or info.st_uid not in {0, os.getuid()}):
                            continue
                    except OSError:
                        continue
                return found
    return None


def _compatible_windows_cli(found: str | None, values: Mapping[str, str]) -> str | None:
    if found is None:
        return None
    command = ntpath.splitext(ntpath.basename(found))[0].casefold()
    if command == "ft":
        return found if found.lower().endswith(".exe") else None
    if not found.lower().endswith((".cmd", ".bat")):
        return found
    if command not in {"agy", "claude", "codex", "gemini", "opencode"}:
        return found  # Other providers expose installation state only.
    try:
        argv = cli_argv([found], values)
    except OSError:
        return None
    return argv[0] if command == "claude" else found


def cli_environment(command: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Use the same paths for CLI launchers and their Node/runtime children."""
    values = _environment(environ)
    explicit, standard = _directories(command, values)
    separator = ";" if sys.platform == "win32" else ":"
    values["PATH"] = separator.join(explicit + [item for item in standard if _trusted_directory(item)])
    if sys.platform == "win32":
        values["NoDefaultCurrentDirectoryInExePath"] = "1"
    return values


def cli_argv(argv: list[str], environ: Mapping[str, str] | None = None) -> list[str]:
    """Launch known Windows npm packages directly, preserving argument boundaries."""
    if sys.platform != "win32" or not argv or not argv[0].lower().endswith((".cmd", ".bat")):
        return list(argv)
    executable = Path(argv[0])
    if not executable.is_absolute():
        raise OSError("The CLI batch launcher requires an absolute installation path.")
    command = executable.stem.casefold()
    entries = {
        "codex": "@openai/codex/bin/codex.js",
        "gemini": "@google/gemini-cli/dist/index.js",
        "opencode": "opencode-ai/bin/opencode",
        "claude": "@anthropic-ai/claude-code/bin/claude.exe",
    }
    entry = entries.get(command)
    if entry is None:
        raise OSError("The CLI batch launcher is not supported.")
    target = executable.parent / "node_modules" / entry
    if not target.is_file():
        raise OSError("The CLI batch launcher's installed package is missing.")
    if target.suffix.casefold() == ".exe":
        return [str(target), *argv[1:]]
    adjacent_node = executable.parent / "node.exe"
    node = str(adjacent_node) if adjacent_node.is_file() else find_cli("node.exe", environ)
    if node is None:
        raise OSError("The CLI requires a Node.js executable.")
    return [node, str(target), *argv[1:]]
