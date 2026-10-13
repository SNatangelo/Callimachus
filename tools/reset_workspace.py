#!/usr/bin/env python3
# tools/reset_workspace.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Reset the Citation Verifier workspace: remove runs, caches and shared state.

Cross-platform port of the former tools/reset_workspace.ps1. Removes the
*contents* of selected workspace locations (runs, pytest caches and the shared
content store) while keeping the directories themselves and the excluded test
inputs. Protected locations require an explicit opt-in flag; --dry-run reports
what would happen without deleting.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent          # repo root (contains core/, run.py)
WORKSPACE_ROOT = PROJECT_ROOT.parent      # the directory the repo lives in


def _human_bytes(n: int) -> str:
    step = 1024.0
    if n >= step ** 3:
        return f"{n / step ** 3:.2f} GB"
    if n >= step ** 2:
        return f"{n / step ** 2:.2f} MB"
    if n >= step:
        return f"{n / step:.2f} KB"
    return f"{n} B"


def _inventory(root: Path) -> dict:
    if not root.exists():
        return {"exists": False, "files": 0, "dirs": 0, "bytes": 0}
    files = dirs = total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirs += len(dirnames)
        for name in filenames:
            files += 1
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                pass
    return {"exists": True, "files": files, "dirs": dirs, "bytes": total}


def _child_targets(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.iterdir())


def _print_section(name: str, root: Path, enabled: bool, reason: str) -> dict:
    inv = _inventory(root)
    targets = _child_targets(root)
    print()
    print(f"[{name}]")
    print(f"Root   : {root}")
    print(f"State  : {'present' if inv['exists'] else 'missing'}")
    print(f"Files  : {inv['files']}")
    print(f"Dirs   : {inv['dirs']}")
    print(f"Size   : {_human_bytes(inv['bytes'])}")
    if enabled:
        print(f"Action : remove {len(targets)} child item(s)")
    else:
        print("Action : keep")
        print(f"Reason : {reason}")
    return {"name": name, "root": root, "enabled": enabled, "targets": targets}


def _remove_targets(section: dict) -> None:
    for target in section["targets"]:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        print(f"removed: {target}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reset the Citation Verifier workspace.")
    parser.add_argument("--include-runs", action="store_true",
                        help="also remove paused/completed run directories under runs/")
    parser.add_argument("--include-shared-state", action="store_true",
                        help="also reset the shared content store (content.sqlite, parsed caches)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be removed without deleting anything")
    args = parser.parse_args(argv)

    state_dir = os.environ.get("CITATION_VERIFIER_STATE_DIR", "").strip()
    if state_dir:
        shared_state_root = (Path(state_dir) / "content_store").resolve()
    else:
        shared_state_root = (PROJECT_ROOT / "storage").resolve()

    sections = [
        _print_section("runs", (PROJECT_ROOT / "runs").resolve(), args.include_runs,
                       "protected by default; pass --include-runs to remove paused or completed run directories"),
        _print_section("pytest-cache-workspace", (WORKSPACE_ROOT / ".pytest_cache").resolve(), True, ""),
        _print_section("pytest-cache-project", (PROJECT_ROOT / ".pytest_cache").resolve(), True, ""),
        _print_section("shared-state", shared_state_root, args.include_shared_state,
                       "protected by default; pass --include-shared-state to reset content.sqlite and parsed caches"),
    ]

    print()
    print("[paper-test]")
    print(f"Root   : {(WORKSPACE_ROOT / 'Paper test').resolve()}")
    print("Action : always keep")
    print("Reason : test inputs are intentionally excluded from cleanup and are already gitignored")

    if args.dry_run:
        print()
        print("Dry run only. No files were removed.")
        return 0

    print()
    print("Executing cleanup...")
    for section in sections:
        if section["enabled"]:
            _remove_targets(section)

    print()
    print("Cleanup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
