#!/usr/bin/env python3
# tools/debug_first_run_env_reset.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Run the pipeline with a clean environment, to debug "first run" behavior.

Saves the citation-verifier environment variables, unsets them for the
duration of the run (so the run behaves as if it were a genuinely first
invocation on this machine), optionally deletes the default signer key
files, invokes ``python run.py --input ...``, then restores the saved
environment variables no matter what happened.

Cross-platform port of the former debug_first_run_env_reset.ps1.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# The signer/public-key file locations are resolved entirely from env vars
# in core/infra/signing.py (ENV_KEY_FILE = CITATION_VERIFIER_SIGNING_KEY_FILE).
# There is no OS-specific "default signer file" concept in the current code
# (no %APPDATA%\CitationVerifier\signer.json equivalent) -- signing.py only
# ever reads a key from CITATION_VERIFIER_SIGNING_KEY_FILE or the raw
# CITATION_VERIFIER_SIGNING_KEY env var. To keep --delete-default-signer
# meaningful without inventing a Windows-only path, we delete whatever file
# CITATION_VERIFIER_SIGNER_FILE / CITATION_VERIFIER_PUBLIC_KEY_FILE point at,
# if those env vars are set (they are among the vars this script saves and
# temporarily unsets, so read them before unsetting).
ENV_NAMES = [
    "CITATION_VERIFIER_SIGNER_FILE",
    "CITATION_VERIFIER_PUBLIC_KEY_FILE",
    "CITATION_VERIFIER_ACCURACY",
    "CITATION_VERIFIER_MAILTO",
    "GOOGLE_BOOKS_API_KEY",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_PY = REPO_ROOT / "run.py"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run python run.py --input <pdf> with the citation-verifier "
            "environment variables cleared, to reproduce first-run behavior."
        )
    )
    parser.add_argument("--input-pdf", required=True, help="manuscript path to pass as --input")
    parser.add_argument("--accuracy", default="", help="accuracy regime to pass through (optional)")
    parser.add_argument(
        "--python-exe",
        default=sys.executable,
        help="python executable to invoke run.py with (default: current interpreter)",
    )
    parser.add_argument("--proceed", action="store_true", help="pass --proceed through to run.py")
    parser.add_argument(
        "--delete-default-signer",
        action="store_true",
        help=(
            "delete the signer key file(s) pointed to by "
            "CITATION_VERIFIER_SIGNER_FILE / CITATION_VERIFIER_PUBLIC_KEY_FILE "
            "(and their .pub.json siblings), if those env vars were set, before running"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    saved = {name: os.environ.get(name) for name in ENV_NAMES}

    exit_code = 0
    try:
        # Read the signer-related paths BEFORE unsetting, since deletion
        # depends on the (about to be cleared) env vars.
        signer_file = saved.get("CITATION_VERIFIER_SIGNER_FILE")
        public_file = saved.get("CITATION_VERIFIER_PUBLIC_KEY_FILE")

        for name in ENV_NAMES:
            os.environ.pop(name, None)

        if args.delete_default_signer:
            candidates: list[Path] = []
            for raw in (signer_file, public_file):
                if raw:
                    p = Path(raw)
                    candidates.append(p)
                    candidates.append(Path(str(p) + ".pub.json"))
            for path in candidates:
                if path.exists():
                    path.unlink()

        cmd = [args.python_exe, str(RUN_PY), "--input", args.input_pdf]
        if args.accuracy:
            cmd += ["--accuracy", args.accuracy]
        if args.proceed:
            cmd.append("--proceed")

        result = subprocess.run(cmd)
        exit_code = result.returncode
    finally:
        for name in ENV_NAMES:
            value = saved.get(name)
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
