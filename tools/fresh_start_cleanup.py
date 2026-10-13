#!/usr/bin/env python3
# tools/fresh_start_cleanup.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fresh-start cleanup: reset runs and shared state.

Cross-platform port of the former tools/fresh_start_cleanup.ps1. Delegates to
reset_workspace with the "keep only durable repo files" switches enabled, so a
single command returns the workspace to a genuinely first-run state while
preserving the excluded (gitignored) test inputs.
"""
from __future__ import annotations

import argparse
import sys

import reset_workspace


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fresh-start cleanup: keep only durable repo files and excluded test inputs."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be removed without deleting anything")
    args = parser.parse_args(argv)

    forwarded = ["--include-runs", "--include-shared-state"]
    if args.dry_run:
        forwarded.append("--dry-run")

    print("Fresh-start cleanup keeps only durable repo files and excluded test inputs.")
    print("Delegating to reset_workspace with: " + " ".join(forwarded))
    return reset_workspace.main(forwarded)


if __name__ == "__main__":
    sys.exit(main())
