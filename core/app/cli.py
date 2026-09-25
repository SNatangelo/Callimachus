# core/app/cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Standard-library CLI construction shared by the root and pipeline drivers."""
from __future__ import annotations

import argparse
from typing import Sequence

from core.invocation import run_prefix


def _description() -> str:
    command = run_prefix()
    return f"""Callimachus — audit-ready citation verification

Run the complete pipeline
-------------------------
  Start:   {command} --input manuscript.pdf --accuracy standard
  Resume:  {command} --run runs/<id> --resume
  Guided Fetch: {command} --run runs/<id> --resume --guided-fetch
  Status:  {command} --run runs/<id> --status
  Tasks:   {command} tasks list --run runs/<id>

The persisted state machine is:
  parse -> resolve -> fetch -> gaps -> style -> verify -> web_research -> report -> done

The SQLite run database is the system of record. Callimachus fails closed when it
cannot establish required configuration, source identity, evidence provenance,
artifact integrity, or report completion. A paused run is resumed from recorded
state; it is not silently restarted.

Specialist commands
-------------------
  parse          extract claims, references, and citation edges
  preprocess     prepare source text or a bounded verification context
  authoryear     resolve an author-year citation against candidate references
  resolve        resolve one DB-native reference and optionally export text
  provide        ingest, map, record, or locate operator-provided source material
  fetch          run targeted full-text retrieval for one reference
  ocr            OCR a PDF into normalized text
  verify         run completion and authenticity gates for a prepared run
  report         regenerate the deterministic report projection and seal
  report-bibliography  export persisted existence/fabrication findings without Fetch/Verify
  report-html    generate or verify the optional human HTML companion
  present        emit a report only after its presentation gate accepts it
  preview        inspect key-gated Google Books preview evidence
  gaps           recompute unresolved-source gaps for an accuracy regime
  style-check    check citation style for an entry or run
  style-detect   detect a run's citation style
  tasks          list, inspect, and answer SQLite-backed tasks
  configure      inspect or write local configuration
  app            launch the optional Callimachus desktop application
  benchmark      compare terminal behavior across frozen Verify runs
  journal-catalog  create, inspect, or refresh local journal authority

Specialist commands are diagnostic or recovery interfaces; they do not replace the
normal pipeline driver. Run `{command} <command> --help` for command-specific
arguments.

Documentation
-------------
  Complete guide: docs/guide/README.md
  CLI reference:  docs/guide/02-cli-reference.md
  Keys and credentials: docs/guide/10-keys-and-credentials.md
  Environment labels: docs/guide/11-environment-reference.md

Exit codes
----------
  0   completed
  2   invalid invocation or runtime error
  3   another process holds the run lock
  10  operator action is required; inspect DB-native tasks
  20  report or completion gate failed
  21  `present` refused a report that did not pass its gate
"""

DEFAULT_ACCURACY_CHOICES = (
    "maximum",
    "maximum_fallback",
    "standard",
    "abstract",
    "standard_web",
)
DEFAULT_HTTP_PROFILE_CHOICES = ("browser_like", "plain")
DEFAULT_CHALLENGE_MODE_CHOICES = (
    "off",
    "queue",
    "browser_challenge",
    "interactive_challenge",
    "interactive",
    "playwright",
    "interactive_browser",
    "interactive_closed",
    "interactive_access",
    "interactive_browser_closed",
    "interactive_fallback",
    "interactive_recovery",
    "browser_fallback",
    "interactive_fallback_closed",
    "interactive_recovery_closed",
    "browser_fallback_closed",
)


def build_parser(
    *,
    prog: str | None = None,
    accuracy_choices: Sequence[str] = DEFAULT_ACCURACY_CHOICES,
    env_accuracy: str = "CITATION_VERIFIER_ACCURACY",
    default_accuracy: str = "standard",
    http_profile_choices: Sequence[str] = DEFAULT_HTTP_PROFILE_CHOICES,
    env_http_profile: str = "CITATION_VERIFIER_HTTP_PROFILE",
    default_http_profile: str = "browser_like",
    challenge_mode_choices: Sequence[str] = DEFAULT_CHALLENGE_MODE_CHOICES,
    env_challenge_mode: str = "CITATION_VERIFIER_FETCH_CHALLENGE_MODE",
    default_challenge_mode: str = "off",
    env_ocr_lang: str = "CITATION_VERIFIER_OCR_LANG",
    default_ocr_lang: str = "eng",
) -> argparse.ArgumentParser:
    """Build the pipeline parser without importing runtime dependencies."""
    ap = argparse.ArgumentParser(
        prog=prog,
        description=_description(),
        usage=(
            "%(prog)s --input MANUSCRIPT [pipeline options]\n"
            "       %(prog)s --run RUN --resume [pipeline options]\n"
            "       %(prog)s --run RUN --status [--json-only]\n"
            "       %(prog)s <command> [args]"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--input",
        help="manuscript path (.docx/.tex/.pdf/.md/.txt/.html)",
    )
    ap.add_argument(
        "--run",
        help="run directory: existing for resume/status, or explicit output path for a new run",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="continue an existing run from its persisted phase",
    )
    ap.add_argument(
        "--guided-fetch",
        action="store_true",
        help=("with --run --resume, open the human-operated Guided Fetch window "
              "for an eligible paused Fetch run; never answers tasks automatically"),
    )
    ap.add_argument(
        "--manual-review",
        action="store_true",
        help="pause after Parse and emit DB-native review tasks before Resolve",
    )
    ap.add_argument(
        "--manual-review-ref-number",
        metavar="N",
        type=int,
        action="append",
        help=("request hash-bound identity review for reference N and, when it "
              "is a footnote parent, source-boundary review; repeatable"),
    )
    ap.add_argument(
        "--agent-identity",
        metavar="AGENT_IDENTITY",
        help="stable opaque harness identity (1-64 ASCII characters); "
             "requires integrity authority",
    )
    ap.add_argument(
        "--fresh-start",
        action="store_true",
        help="with --run, start a new child run from the same manuscript "
             "and stable baseline config, without inheriting no-fetch, "
             "references-only, or autonomous mode",
    )
    ap.add_argument(
        "--fork-frozen-fetch-verify",
        metavar="BASELINE_RUN",
        help="create --run as a fresh debug-labelled verify child of a "
             "frozen fetch baseline; copies only parse/resolve/fetch assets "
             "and regenerates verify tasks with current code",
    )
    ap.add_argument(
        "--fork-completed-verify",
        metavar="PARENT_RUN",
        help=("create --run as a fresh Verify child of a completed run; "
              "reuses only the parent's frozen parse/resolve/fetch evidence "
              "and applies the currently configured LLM policy"),
    )
    ap.add_argument(
        "--fork-reference-only-fetch",
        metavar="PARENT_RUN",
        help=("create --run as a fresh Fetch child of a completed "
              "references-only run; reuses authenticated parse/resolve/source "
              "evidence and resumes normal Fetch task handling"),
    )
    ap.add_argument(
        "--remediate-completed",
        metavar="PARENT_RUN",
        help=("create --run as a fresh Parse child of a completed, sealed run; "
              "revalidates the parent input, report, journal, and source inventory"),
    )
    ap.add_argument(
        "--freeze-after-fetch",
        action="store_true",
        help="stop after Fetch enters its next phase; repeat on every resume until Fetch completes",
    )
    ap.add_argument(
        "--status",
        action="store_true",
        help="show the current state of an existing run and exit",
    )
    ap.add_argument(
        "--json-only",
        action="store_true",
        help="with --status, print only the machine-readable JSON snapshot",
    )
    ap.add_argument(
        "--autonomous",
        action="store_true",
        help=("mark the run as unattended and shorten pause messages; "
              "Verify runs with or without this flag"),
    )
    ap.add_argument(
        "--accuracy",
        default=None,
        choices=accuracy_choices,
        help=f"verification regime; falls back to ${env_accuracy} then "
             f"'{default_accuracy}'",
    )
    ap.add_argument(
        "--style",
        choices=["vancouver", "apa7", "chicago", "mla9"],
        help="citation style; if omitted it is auto-detected",
    )
    ap.add_argument("--mailto", help="contact email for the polite pool (sent to APIs)")
    ap.add_argument(
        "--http-profile",
        choices=http_profile_choices,
        help=f"HTTP request profile; falls back to ${env_http_profile} then "
             f"'{default_http_profile}'",
    )
    ap.add_argument(
        "--challenge-mode",
        choices=challenge_mode_choices,
        help=f"challenge handling mode; falls back to ${env_challenge_mode} then "
             f"'{default_challenge_mode}'",
    )
    ap.add_argument("--model", help="verification model ID, recorded in the audit ledger")
    ap.add_argument(
        "--verify-table-citations",
        action="store_true",
        help="verify citations that appear only inside a table row "
             "(benchmark tables, comparison grids). Off by default: a "
             "table row is a label, not an assertion, so a verdict on it "
             "is unreliable — dropped rows are still counted for reference "
             "coverage and listed in the report",
    )
    ap.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help=("legacy driver retry budget (default: 2); guarded Jury stages use "
              "their own configured caps"),
    )
    ap.add_argument(
        "--no-fetch",
        action="store_true",
        help="skip the full-text fetch pause (use only what resolve found)",
    )
    ap.add_argument(
        "--references-only",
        action="store_true",
        help=(
            "run Parse and Resolve only, then write an explicitly partial "
            "report.preview.html without Fetch or LLM Verify"
        ),
    )
    ap.add_argument(
        "--ocr-lang",
        default=None,
        help=f"OCR language(s) for scanned PDFs (e.g. 'eng' or 'eng+ita'); "
             f"falls back to ${env_ocr_lang} then '{default_ocr_lang}'",
    )
    ap.add_argument(
        "--proceed",
        "--ignore-missing",
        dest="proceed",
        action="store_true",
        help="start even if optional config (email / Google Books key) is "
             "missing, instead of stopping to ask",
    )
    ap.add_argument(
        "--debug-override-artifact-integrity",
        action="store_true",
        help="continue diagnostically only after a trusted mismatch override",
    )
    ap.add_argument(
        "--debug-override-reason",
        help="mandatory operator reason for the artifact-integrity debug override",
    )
    return ap
