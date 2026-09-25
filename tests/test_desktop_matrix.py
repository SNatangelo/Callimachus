# tests/test_desktop_matrix.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Platform selection for budget-conscious desktop pull-request builds."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "select_desktop_matrix.py"
spec = importlib.util.spec_from_file_location("select_desktop_matrix", SCRIPT)
assert spec is not None and spec.loader is not None
MATRIX = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MATRIX)


def names(files: set[str] | None) -> list[str]:
    return [item["platform"] for item in MATRIX.select_platforms(files)]


def test_windows_installer_pr_does_not_build_linux_or_macos():
    assert names({
        "packaging/windows-installer.nsi",
        "docs/guide/12-desktop-packages.md",
    }) == ["windows-x64"]


def test_linux_or_macos_specific_pr_builds_only_affected_platforms():
    assert names({"packaging/collect_ubuntu_sources.py"}) == ["linux-x64"]
    assert names({"packaging/build_macos.py"}) == ["macos-arm64", "macos-x64"]


def test_mixed_platform_pr_builds_union_in_stable_order():
    assert names({
        "packaging/windows-installer.nsi",
        "packaging/collect_ubuntu_sources.py",
    }) == ["windows-x64", "linux-x64"]


def test_shared_changes_and_release_events_keep_full_matrix():
    full = ["windows-x64", "linux-x64", "macos-arm64", "macos-x64"]
    assert names({"packaging/build_desktop.py"}) == full
    assert names({
        "packaging/windows-installer.nsi", "packaging/build_desktop.py",
    }) == full
    assert names(None) == full


def test_pull_request_file_lookup_uses_github_api(monkeypatch):
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("Authorization"), timeout))
        return io.BytesIO(json.dumps([
            {"filename": "packaging/windows-installer.nsi"},
        ]).encode("utf-8"))

    monkeypatch.setattr(MATRIX.urllib.request, "urlopen", urlopen)
    assert MATRIX.pull_request_files("owner/repo", "123", "test-token") == {
        "packaging/windows-installer.nsi",
    }
    assert seen == [(
        "https://api.github.com/repos/owner/repo/pulls/123/files?per_page=100&page=1",
        "Bearer test-token", 30,
    )]


def test_synchronize_uses_latest_commit_files(monkeypatch):
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("Authorization"), timeout))
        return io.BytesIO(json.dumps({"files": [
            {"filename": "packaging/windows-installer.nsi"},
            {"filename": "packaging/build_desktop.py"},
        ]}).encode("utf-8"))

    monkeypatch.setattr(MATRIX.urllib.request, "urlopen", urlopen)
    assert MATRIX.commit_files("owner/repo", "abc123", "test-token") == {
        "packaging/windows-installer.nsi", "packaging/build_desktop.py",
    }
    assert seen == [(
        "https://api.github.com/repos/owner/repo/commits/abc123",
        "Bearer test-token", 30,
    )]


def test_synchronize_outputs_full_matrix_for_shared_change(monkeypatch, tmp_path):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("PR_ACTION", "synchronize")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("HEAD_SHA", "abc123")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(MATRIX, "commit_files", lambda *_args: {
        "packaging/windows-installer.nsi", "packaging/build_desktop.py",
    })

    MATRIX.main()

    line = output.read_text(encoding="utf-8").strip()
    assert json.loads(line.removeprefix("matrix=")) == {
        "include": list(MATRIX.PLATFORMS),
    }
