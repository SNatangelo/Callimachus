#!/usr/bin/env python3
# tools/fresh_start_run.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Start a fresh child run from the latest (or a given) run directory.

Resolves a parent run directory (either the one passed via --run, or the
most recently created directory under <repo_root>/runs), then invokes
``python run.py --run <parent> --fresh-start`` with the pass-through
options given here.

Cross-platform port of the former tools/fresh_start_run.ps1.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
RUN_PY = REPO_ROOT / "run.py"


def get_latest_run_dir(runs_root: Path) -> Path:
    if not runs_root.exists():
        raise SystemExit(f"Runs root not found: {runs_root}")

    candidates = [p for p in runs_root.iterdir() if p.is_dir()]
    if not candidates:
        raise SystemExit(f"No run directories found under {runs_root}")

    latest = sorted(candidates, key=lambda p: p.name, reverse=True)[0]
    return latest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start a fresh child run (python run.py --run <parent> --fresh-start)."
    )
    parser.add_argument("--run", default="", help="parent run dir (default: latest under runs/)")
    parser.add_argument(
        "--python-exe",
        default=sys.executable,
        help="python executable to invoke run.py with (default: current interpreter)",
    )
    parser.add_argument("--accuracy", default="")
    parser.add_argument("--style", default="")
    parser.add_argument("--mailto", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-retries", type=int, default=None)
    parser.add_argument("--http-profile", default="")
    parser.add_argument("--challenge-mode", default="")
    parser.add_argument("--ocr-lang", default="")
    parser.add_argument("--autonomous", action="store_true")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--proceed", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved command without executing it",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    runs_root = (REPO_ROOT / "runs").resolve()

    if args.run:
        parent_run = Path(args.run).resolve()
    else:
        parent_run = get_latest_run_dir(runs_root).resolve()

    if not parent_run.exists():
        raise SystemExit(f"Run directory not found: {parent_run}")

    cmd = [args.python_exe, str(RUN_PY), "--run", str(parent_run), "--fresh-start"]

    if args.accuracy:
        cmd += ["--accuracy", args.accuracy]
    if args.style:
        cmd += ["--style", args.style]
    if args.mailto:
        cmd += ["--mailto", args.mailto]
    if args.model:
        cmd += ["--model", args.model]
    if args.max_retries is not None:
        cmd += ["--max-retries", str(args.max_retries)]
    if args.http_profile:
        cmd += ["--http-profile", args.http_profile]
    if args.challenge_mode:
        cmd += ["--challenge-mode", args.challenge_mode]
    if args.ocr_lang:
        cmd += ["--ocr-lang", args.ocr_lang]
    if args.autonomous:
        cmd.append("--autonomous")
    if args.no_fetch:
        cmd.append("--no-fetch")
    if args.proceed:
        cmd.append("--proceed")

    print(f"Parent run : {parent_run}")
    print(f"Workdir    : {REPO_ROOT}")
    print(f"Command    : {' '.join(cmd)}")

    if args.dry_run:
        print("Dry run only. Command not executed.")
        return 0

    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
