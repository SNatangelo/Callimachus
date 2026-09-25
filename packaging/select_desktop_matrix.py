# packaging/select_desktop_matrix.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Select native PR build targets; always build every platform for releases."""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path


PLATFORMS = (
    {"platform": "windows-x64", "os": "windows-2022"},
    {"platform": "linux-x64", "os": "ubuntu-22.04"},
    {"platform": "macos-arm64", "os": "macos-15"},
    {"platform": "macos-x64", "os": "macos-15-intel"},
)
WINDOWS_FILES = frozenset({
    "packaging/build_windows_installer.py",
    "packaging/install_vc_runtime.ps1",
    "packaging/smoke_windows_installer.py",
    "packaging/windows-installer.nsi",
    "packaging/NSIS-3.10-LICENSE.txt",
    "tests/test_windows_installer_build.py",
})
LINUX_FILES = frozenset({
    "packaging/collect_ubuntu_sources.py",
    "tests/test_ubuntu_sources.py",
})
MACOS_FILES = frozenset({
    "packaging/build_macos.py",
    "packaging/smoke_macos.py",
})
DOCUMENTATION_FILES = frozenset({
    "README.md", "DEPLOYMENT.md", "LICENSING.md", "CLA.md", "CONTRIBUTING.md",
})
DOCUMENTATION_PREFIXES = ("docs/", "knowledge/", "assets/readme/")


def select_platforms(changed_files: set[str] | None) -> list[dict[str, str]]:
    """Specific platform work selects that platform; shared work tests all four."""
    if changed_files is None:
        return list(PLATFORMS)
    relevant = {
        name for name in changed_files
        if name not in DOCUMENTATION_FILES
        and not name.startswith(DOCUMENTATION_PREFIXES)
    }
    platform_files = WINDOWS_FILES | LINUX_FILES | MACOS_FILES
    if relevant - platform_files:
        return list(PLATFORMS)
    selected: set[str] = set()
    if relevant & WINDOWS_FILES:
        selected.add("windows-x64")
    if relevant & LINUX_FILES:
        selected.add("linux-x64")
    if relevant & MACOS_FILES:
        selected.update({"macos-arm64", "macos-x64"})
    if not selected:
        return list(PLATFORMS)
    return [platform for platform in PLATFORMS if platform["platform"] in selected]


def pull_request_files(repository: str, number: str, token: str) -> set[str]:
    files: set[str] = set()
    for page in range(1, 31):
        url = f"https://api.github.com/repos/{repository}/pulls/{number}/files?per_page=100&page={page}"
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Callimachus-desktop-matrix",
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            batch = json.load(response)
        if not isinstance(batch, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("filename"), str)
            for item in batch
        ):
            raise RuntimeError("GitHub returned an invalid pull-request file list")
        files.update(item["filename"] for item in batch)
        if len(batch) < 100:
            return files
    raise RuntimeError("pull request exceeds GitHub's 3000-file listing limit")


def commit_files(repository: str, sha: str, token: str) -> set[str]:
    """Use the current push, not older platform changes already in the PR."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/commits/{sha}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Callimachus-desktop-matrix",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    entries = payload.get("files") if isinstance(payload, dict) else None
    if (not isinstance(entries, list) or len(entries) >= 300
            or any(not isinstance(item, dict) or not isinstance(item.get("filename"), str)
                   for item in entries)):
        raise RuntimeError("GitHub returned an incomplete commit file list")
    return {item["filename"] for item in entries}


def main() -> None:
    event = os.environ["GITHUB_EVENT_NAME"]
    changed = None
    if event == "pull_request":
        repository = os.environ["GITHUB_REPOSITORY"]
        token = os.environ["GH_TOKEN"]
        if os.environ.get("PR_ACTION") == "synchronize":
            changed = commit_files(repository, os.environ["HEAD_SHA"], token)
        else:
            changed = pull_request_files(repository, os.environ["PR_NUMBER"], token)
    matrix = json.dumps({"include": select_platforms(changed)}, separators=(",", ":"))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"matrix={matrix}\n")
    print(f"Desktop platforms: {matrix}")


if __name__ == "__main__":
    main()
