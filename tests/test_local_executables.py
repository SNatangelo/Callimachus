# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Installer-location regression fixtures: no real providers or shell profiles."""
from __future__ import annotations

import ntpath
import posixpath
import stat
from types import SimpleNamespace

import pytest

from core.infra import _local_executables as local
from core.app.providers import cli_discovery, discovery


@pytest.fixture
def filesystem(monkeypatch):
    def setup(platform):
        windows = platform == "win32"
        path = ntpath if windows else posixpath
        home = r"C:\Users\Test User" if windows else "/home/test user"
        values = {"PATH": r"C:\Windows\System32" if windows else "/usr/bin:/bin",
                  "USERPROFILE" if windows else "HOME": home}
        files, unsafe, queries, modes = set(), set(), [], {}

        def which(command, *, path):
            queries.append(command)
            if windows:
                assert ntpath.isabs(command), "Windows lookup must exclude implicit cwd"
                candidates = ([command] if command.lower().endswith((".exe", ".cmd", ".bat", ".com"))
                              else [command + ".exe", command + ".cmd", command + ".bat", command + ".com"])
            else:
                candidates = [posixpath.join(directory, command) for directory in path.split(":")]
            return next((candidate for candidate in candidates if candidate in files), None)

        def metadata(directory):
            return SimpleNamespace(st_mode=(stat.S_IFREG if directory in files else stat.S_IFDIR) | modes.get(directory, 0o777 if directory in unsafe else 0o755), st_uid=1000)

        class FakePath:
            def __init__(self, value):
                self.value = path.normpath(str(value))

            @property
            def parent(self):
                return FakePath(path.dirname(self.value))

            @property
            def stem(self):
                return path.splitext(path.basename(self.value))[0]

            @property
            def suffix(self):
                return path.splitext(self.value)[1]

            def is_absolute(self):
                return path.isabs(self.value)

            def is_file(self):
                return self.value in files

            def __truediv__(self, other):
                return FakePath(path.join(self.value, str(other)))

            def __str__(self):
                return self.value

        monkeypatch.setattr(local, "Path", FakePath)
        monkeypatch.setattr(local, "sys", SimpleNamespace(platform=platform))
        monkeypatch.setattr(local, "os", SimpleNamespace(environ=values, stat=metadata, getuid=lambda: 1000))
        monkeypatch.setattr(local, "shutil", SimpleNamespace(which=which))
        return SimpleNamespace(platform=platform, path=path, home=home, env=values,
                               files=files, unsafe=unsafe, queries=queries, modes=modes,
                               binary=lambda name: name + ".exe" if windows else name)
    return setup


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("provider,command", [
    ("codex_cli", "codex"), ("claude_cli", "claude"), ("gemini_cli", "gemini"),
    ("antigravity_cli", "agy"), ("opencode_cli", "opencode"),
    ("cursor_cli", "agent"), ("copilot_cli", "copilot"),
    ("ollama", "ollama"), ("freetoken", "ft"),
])
def test_nine_local_provider_install_flags_with_minimal_gui_path(filesystem, monkeypatch, platform, provider, command):
    fs = filesystem(platform)
    expected = fs.path.join(fs.home, ".local", "bin", fs.binary(command))
    fs.files.add(expected)
    monkeypatch.setattr(discovery, "find_cli", local.find_cli)
    assert local.find_cli(command, fs.env) == expected
    assert discovery._installed(provider, fs.env)


@pytest.mark.parametrize("platform,directory", [
    ("darwin", "/opt/homebrew/bin"), ("darwin", "/usr/local/bin"),
    ("darwin", "/opt/local/bin"), ("linux", "/home/linuxbrew/.linuxbrew/bin"),
    ("linux", "/usr/local/bin"),
])
def test_system_package_managers_without_login_shell(filesystem, platform, directory):
    fs = filesystem(platform)
    expected = fs.path.join(directory, "gemini")
    fs.files.add(expected)
    assert local.find_cli("gemini", fs.env) == expected
    assert directory in local.cli_environment("gemini", fs.env)["PATH"].split(":")


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_explicit_path_has_priority_over_standard_install(filesystem, platform):
    fs = filesystem(platform)
    explicit = r"D:\Custom Tools" if platform == "win32" else "/custom tools/bin"
    values = {**fs.env, "PATH": explicit}
    expected = fs.path.join(explicit, fs.binary("codex"))
    fs.files.update({expected, fs.path.join(fs.home, ".local", "bin", fs.binary("codex"))})
    assert local.find_cli("codex", values) == expected


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_empty_relative_and_project_paths_are_not_implicit_fallbacks(filesystem, platform):
    fs = filesystem(platform)
    values = {**fs.env, "PATH": ";.;bin;.." if platform == "win32" else ":.:bin:.."}
    fs.files.update({"codex", "codex.exe", fs.path.join(fs.home, "project", fs.binary("codex"))})
    assert local.find_cli("codex", values) is None
    assert local.find_cli("../codex", values) is None
    child = local.cli_environment("codex", values)
    assert all(fs.path.isabs(entry) for entry in child["PATH"].split(";" if platform == "win32" else ":"))


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("mode", [0o775, 0o777])
def test_other_writable_fallback_directory_is_not_used(filesystem, platform, mode):
    fs = filesystem(platform)
    directory = fs.path.join(fs.home, ".local", "bin")
    fs.files.add(fs.path.join(directory, "codex"))
    fs.modes[directory] = mode
    assert local.find_cli("codex", fs.env) is None
    assert directory not in local.cli_environment("codex", fs.env)["PATH"].split(":")


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_opencode_native_installer_directory(filesystem, platform):
    fs = filesystem(platform)
    expected = fs.path.join(fs.home, ".opencode", "bin", fs.binary("opencode"))
    fs.files.add(expected)
    assert local.find_cli("opencode", fs.env) == expected
    assert local.find_cli("codex", fs.env) is None


def test_mac_ollama_app_resources_are_only_used_for_ollama(filesystem):
    fs = filesystem("darwin")
    expected = "/Applications/Ollama.app/Contents/Resources/ollama"
    fs.files.add(expected)
    assert local.find_cli("ollama", fs.env) == expected
    assert local.find_cli("codex", fs.env) is None


def test_windows_native_codex_antigravity_and_ollama_directories(filesystem):
    fs = filesystem("win32")
    for command, parts in [("codex", ("Programs", "OpenAI", "Codex", "bin")), ("agy", ("agy", "bin")), ("ollama", ("Programs", "Ollama"))]:
        expected = ntpath.join(fs.home, "AppData", "Local", *parts, command + ".exe")
        fs.files.add(expected)
        assert local.find_cli(command, fs.env) == expected


def test_windows_managed_freetoken_engine_uses_only_native_executable(filesystem, monkeypatch):
    fs = filesystem("win32")
    managed = ntpath.join(fs.home, "AppData", "Local", "FreeToken")
    expected = ntpath.join(managed, "venv", "Scripts", "ft.exe")
    decoy = ntpath.join(managed, "other", "nested", "ft.exe")
    fs.files.update({expected, decoy})
    monkeypatch.setattr(discovery, "find_cli", local.find_cli)

    assert local.find_cli("ft", fs.env) == expected
    assert discovery._installed("freetoken", fs.env)
    canonical_lookup = ntpath.join(managed, "venv", "Scripts", "ft")
    managed_lookups = {query for query in fs.queries if query.startswith(managed + "\\")}
    assert managed_lookups == {canonical_lookup}
    assert decoy not in fs.queries


@pytest.mark.parametrize(
    "name,accepted",
    [
        ("ft.exe", True), ("ft.ExE", True), ("ft.com", False), ("ft.COM", False),
        ("ft.cmd", False), ("ft.BAT", False), ("ft", False),
    ],
)
def test_windows_freetoken_cli_accepts_only_exe_suffix(name, accepted):
    found = ntpath.join(r"D:\Configured Tools", name)

    assert local._compatible_windows_cli(found, {}) == (found if accepted else None)


@pytest.mark.parametrize("extension", [".cmd", ".bat", ".com"])
def test_windows_freetoken_non_exe_only_is_not_installed(filesystem, monkeypatch, extension):
    fs = filesystem("win32")
    search = r"D:\Configured Tools"
    fs.env["PATH"] = search
    fs.files.add(ntpath.join(search, "ft" + extension))
    monkeypatch.setattr(discovery, "find_cli", local.find_cli)

    assert local.find_cli("ft", fs.env) is None
    assert not discovery._installed("freetoken", fs.env)


@pytest.mark.parametrize("extension", [".cmd", ".bat", ".com"])
def test_windows_freetoken_non_exe_path_falls_through_to_managed_engine(filesystem, monkeypatch, extension):
    fs = filesystem("win32")
    search = r"D:\Configured Tools"
    managed = ntpath.join(fs.home, "AppData", "Local", "FreeToken", "venv", "Scripts")
    expected = ntpath.join(managed, "ft.exe")
    fs.env["PATH"] = search
    fs.files.update({ntpath.join(search, "ft" + extension), expected})
    monkeypatch.setattr(discovery, "find_cli", local.find_cli)

    assert local.find_cli("ft", fs.env) == expected
    assert discovery._installed("freetoken", fs.env)


def test_windows_freetoken_native_exe_wins_over_same_path_wrapper_and_managed_engine(filesystem):
    fs = filesystem("win32")
    search = r"D:\Configured Tools"
    explicit = ntpath.join(search, "ft.exe")
    managed = ntpath.join(fs.home, "AppData", "Local", "FreeToken", "venv", "Scripts", "ft.exe")
    fs.env["PATH"] = search
    fs.files.update({explicit, ntpath.join(search, "ft.cmd"), managed})

    assert local.find_cli("ft", fs.env) == explicit


def test_windows_freetoken_batch_does_not_hide_later_native_path(filesystem):
    fs = filesystem("win32")
    first, second = r"D:\Earlier Wrapper", r"D:\Later Native"
    expected = ntpath.join(second, "ft.exe")
    managed = ntpath.join(fs.home, "AppData", "Local", "FreeToken", "venv", "Scripts", "ft.exe")
    fs.env["PATH"] = first + ";" + second
    fs.files.update({ntpath.join(first, "ft.cmd"), expected, managed})

    assert local.find_cli("ft", fs.env) == expected


@pytest.mark.skipif(local.sys.platform != "win32", reason="requires native Windows PATH and PATHEXT resolution")
@pytest.mark.parametrize("extension", [".cmd", ".CMD", ".bat", ".BAT", ".com", ".COM"])
@pytest.mark.parametrize("with_managed_engine", [False, True])
def test_native_windows_synthetic_freetoken_non_exe_is_ignored(
    tmp_path, monkeypatch, extension, with_managed_engine,
):
    search = tmp_path / "configured tools"
    search.mkdir()
    (search / ("ft" + extension)).touch()
    profile = tmp_path / "profile"
    app_data = tmp_path / "local"
    managed = app_data / "FreeToken" / "venv" / "Scripts"
    managed.mkdir(parents=True)
    if with_managed_engine:
        (managed / "ft.exe").touch()
    monkeypatch.setenv("PATHEXT", ".COM;.CMD;.EXE;.BAT")
    environment = {
        "PATH": str(search),
        "USERPROFILE": str(profile),
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(app_data),
        "PROGRAMFILES": str(tmp_path / "program files"),
        "NPM_CONFIG_PREFIX": "",
    }
    expected = str(managed / "ft.exe") if with_managed_engine else None

    actual = local.find_cli("ft", environment)
    if expected is None:
        assert actual is None
    else:
        assert ntpath.normcase(actual) == ntpath.normcase(expected)



@pytest.mark.skipif(local.sys.platform != "win32", reason="requires native Windows PATH and PATHEXT resolution")
@pytest.mark.parametrize("extension", [".cmd", ".CMD", ".bat", ".BAT", ".com", ".COM"])
def test_native_windows_managed_freetoken_requires_ft_exe(tmp_path, monkeypatch, extension):
    search = tmp_path / "empty PATH"
    search.mkdir()
    app_data = tmp_path / "local"
    managed = app_data / "FreeToken" / "venv" / "Scripts"
    managed.mkdir(parents=True)
    (managed / ("ft" + extension)).touch()
    monkeypatch.setenv("PATHEXT", ".COM;.CMD;.EXE;.BAT")
    environment = {
        "PATH": str(search),
        "USERPROFILE": str(tmp_path / "profile"),
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(app_data),
        "PROGRAMFILES": str(tmp_path / "program files"),
        "NPM_CONFIG_PREFIX": "",
    }

    assert local.find_cli("ft", environment) is None
    assert not discovery._installed("freetoken", environment)


@pytest.mark.skipif(local.sys.platform != "win32", reason="requires native Windows PATH and PATHEXT resolution")
@pytest.mark.parametrize(
    "with_batch,with_managed_engine",
    [(False, False), (True, True), (False, True)],
)
def test_native_windows_synthetic_freetoken_exe_precedes_managed_engine(
    tmp_path, monkeypatch, with_batch, with_managed_engine,
):
    search = tmp_path / "configured tools"
    search.mkdir()
    explicit = search / "ft.exe"
    explicit.touch()
    if with_batch:
        (search / "ft.CMD").touch()
    app_data = tmp_path / "local"
    managed = app_data / "FreeToken" / "venv" / "Scripts"
    managed.mkdir(parents=True)
    managed_executable = managed / "ft.exe"
    if with_managed_engine:
        managed_executable.touch()
    monkeypatch.setenv("PATHEXT", ".COM;.CMD;.EXE;.BAT")
    environment = {
        "PATH": str(search),
        "USERPROFILE": str(tmp_path / "profile"),
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(app_data),
        "PROGRAMFILES": str(tmp_path / "program files"),
        "NPM_CONFIG_PREFIX": "",
    }

    # Empty synthetic files exercise native path resolution; no launcher is invoked.
    actual = local.find_cli("ft", environment)
    assert actual is not None
    assert ntpath.normcase(actual) == ntpath.normcase(str(explicit))


def test_windows_explicit_path_key_is_case_insensitive(filesystem):
    fs = filesystem("win32")
    fs.files.add(r"D:\Custom Tools\codex.exe")
    assert local.find_cli("codex", {"Path": r"D:\Custom Tools"}) == r"D:\Custom Tools\codex.exe"


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("module,command", [
    ("codex_cli", "codex"), ("claude_cli", "claude"), ("gemini_cli", "gemini"),
    ("antigravity_cli", "agy"), ("opencode_cli", "opencode"),
])
def test_catalog_and_execution_use_same_resolved_executable(filesystem, monkeypatch, platform, module, command):
    from importlib import import_module
    backend = import_module("core.verify.backends." + module)
    fs = filesystem(platform)
    expected = fs.path.join(fs.home, ".local", "bin", fs.binary(command))
    fs.files.add(expected)
    monkeypatch.setattr(backend, "find_cli", local.find_cli)
    monkeypatch.setattr(cli_discovery, "find_cli", local.find_cli)
    captured = []
    def catalog(path, environ, result):
        captured.append(path)
        result.update(ok=True, models=["fixture-model"])
    if module in {"codex_cli", "claude_cli"}:
        monkeypatch.setattr(cli_discovery, "_codex" if command == "codex" else "_claude", catalog)
    else:
        target = import_module("core.verify.backends._gemini_cli_runtime") if command == "gemini" else backend
        monkeypatch.setattr(target, "check_catalog", lambda path, environ: captured.append(path) or {"ok": True, "models": ["fixture-model"]})
    result = cli_discovery.cli_catalog(module, fs.env)
    assert result["installed"] and result["ok"]
    assert captured == [expected]
    assert backend._resolve_cli() == expected
    assert backend._available()


@pytest.mark.parametrize("command,entry", [
    ("codex", "@openai/codex/bin/codex.js"),
    ("gemini", "@google/gemini-cli/dist/index.js"),
    ("opencode", "opencode-ai/bin/opencode"),
    ("claude", "@anthropic-ai/claude-code/bin/claude.exe"),
])
def test_windows_npm_launchers_preserve_spaces_and_metacharacter_arguments(tmp_path, monkeypatch, command, entry):
    monkeypatch.setattr(local, "sys", SimpleNamespace(platform="win32"))
    prefix = tmp_path / "User With Spaces" / "npm"
    target = prefix / "node_modules" / entry
    target.parent.mkdir(parents=True)
    target.write_text("fixture, never executed", encoding="utf-8")
    node = prefix / "node.exe"
    node.write_text("fixture, never executed", encoding="utf-8")
    arguments = ["a path with spaces", "literal & whoami", "$(literal)", 'quoted "value"', "100% literal"]
    result = local.cli_argv([str(prefix / (command + ".cmd")), *arguments], {})
    assert result == ([str(target)] if command == "claude" else [str(node), str(target)]) + arguments


def test_windows_unknown_batch_launcher_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(OSError, match="not supported"):
        local.cli_argv([str(tmp_path / "unknown.cmd"), "status"], {})


@pytest.mark.parametrize("installed,reachable", [(False, True), (True, False)])
def test_ollama_binary_presence_and_http_service_are_independent(monkeypatch, installed, reachable):
    monkeypatch.setattr(discovery, "find_cli", lambda *args: "/usr/local/bin/ollama" if installed else None)
    def probe(*args):
        if not reachable:
            raise discovery._DiscoveryError("Ollama service is unavailable.")
        return ["fixture-model"], "ollama_local", "OLLAMA_MODEL", {"host": "http://127.0.0.1:11434"}
    monkeypatch.setattr(discovery, "_probe", probe)
    result = discovery.check_provider("ollama", {})
    assert result["installed"] is installed
    assert result["ok"] is reachable
    assert result["models"] == (["fixture-model"] if reachable else [])


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("mode", [0o775, 0o777])
def test_other_writable_fallback_executable_is_not_used(filesystem, platform, mode):
    fs = filesystem(platform)
    candidate = fs.path.join(fs.home, ".local", "bin", "codex")
    fs.files.add(candidate)
    fs.modes[candidate] = mode
    assert local.find_cli("codex", fs.env) is None


@pytest.mark.parametrize("module", ["codex_cli", "claude_cli", "gemini_cli"])
def test_missing_cli_cannot_fall_back_to_current_directory(monkeypatch, module):
    from importlib import import_module
    from core.verify.backends.errors import ConfigError
    backend = import_module("core.verify.backends." + module)
    monkeypatch.setattr(backend, "find_cli", lambda *args: None)
    with pytest.raises(ConfigError, match="not found"):
        backend._resolve_cli()


@pytest.mark.parametrize("module,command", [("antigravity_cli", "agy"), ("claude_cli", "claude")])
@pytest.mark.parametrize("with_native", [False, True])
def test_windows_unqualified_or_broken_wrapper_matches_catalog_and_execution(
    filesystem, monkeypatch, module, command, with_native,
):
    from importlib import import_module
    from core.verify.backends.errors import ConfigError
    backend = import_module("core.verify.backends." + module)
    fs = filesystem("win32")
    first, second = r"D:\Earlier Wrapper", r"D:\Later Native"
    fs.env["PATH"] = first + ";" + second
    fs.files.add(ntpath.join(first, command + ".cmd"))
    expected = ntpath.join(second, command + ".exe") if with_native else None
    if expected:
        fs.files.add(expected)
    monkeypatch.setattr(backend, "find_cli", local.find_cli)
    monkeypatch.setattr(cli_discovery, "find_cli", local.find_cli)
    captured = []
    if command == "claude":
        def catalog(path, environ, result):
            captured.append(path)
            result.update(ok=True, models=["fixture-model"])
        monkeypatch.setattr(cli_discovery, "_claude", catalog)
    else:
        monkeypatch.setattr(backend, "check_catalog", lambda path, environ: captured.append(path) or {"ok": True, "models": ["fixture-model"]})
    result = cli_discovery.cli_catalog(module, fs.env)
    assert result["installed"] is with_native
    assert result["ok"] is with_native
    assert captured == ([expected] if with_native else [])
    assert backend._available() is with_native
    if with_native or command == "agy":
        assert backend._resolve_cli() == expected
    else:
        with pytest.raises(ConfigError, match="not found"):
            backend._resolve_cli()
    if not with_native:
        assert result["authenticated"] is False and result["models"] == []


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("mode", [0o775, 0o777])
@pytest.mark.parametrize("writable_target", ["directory", "executable"])
def test_unsafe_fallback_reports_not_installed_without_launching(
    filesystem, monkeypatch, platform, mode, writable_target,
):
    fs = filesystem(platform)
    directory = fs.path.join(fs.home, ".local", "bin")
    executable = fs.path.join(directory, "codex")
    fs.files.add(executable)
    fs.modes[directory if writable_target == "directory" else executable] = mode
    monkeypatch.setattr(cli_discovery, "find_cli", local.find_cli)
    monkeypatch.setattr(cli_discovery, "_Session", lambda *args, **kwargs: pytest.fail("untrusted fallback must not launch"))
    result = cli_discovery.cli_catalog("codex_cli", fs.env)
    assert result["installed"] is False and result["authenticated"] is False
    assert result["ok"] is False and result["models"] == []


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("mode", [0o775, 0o777])
def test_explicit_user_path_preserves_other_writable_installation(filesystem, platform, mode):
    fs = filesystem(platform)
    directory = "/explicitly/configured/bin"
    executable = posixpath.join(directory, "codex")
    fs.files.add(executable)
    fs.modes[directory] = fs.modes[executable] = mode
    assert local.find_cli("codex", {**fs.env, "PATH": directory}) == executable



def test_windows_complete_claude_wrapper_selects_same_native_target(filesystem, monkeypatch):
    from core.verify.backends import claude_cli
    fs = filesystem("win32")
    prefix = r"D:\First Npm With Spaces"
    later = r"D:\Other Native"
    fs.env["PATH"] = prefix + ";" + later
    expected = ntpath.join(prefix, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
    fs.files.update({ntpath.join(prefix, "claude.cmd"), expected, ntpath.join(later, "claude.exe")})
    monkeypatch.setattr(claude_cli, "find_cli", local.find_cli)
    monkeypatch.setattr(cli_discovery, "find_cli", local.find_cli)
    captured = []
    def catalog(path, environ, result):
        captured.append(path)
        result.update(ok=True, models=["fixture-model"])
    monkeypatch.setattr(cli_discovery, "_claude", catalog)
    assert cli_discovery.cli_catalog("claude_cli", fs.env)["ok"] is True
    assert captured == [expected]
    assert claude_cli._resolve_cli() == expected
