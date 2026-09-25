#!/usr/bin/env python3
# core/report/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
report.py — Reporter, pure projection of deterministic run artifacts. No generation.

Reads run artifacts and produces a current report.md plus an append-only
report.journal.md audit trail. Every cell is a field written when the action occurred.

The official report contains only deterministic evidence:
  - the parsed run payload
  - resolve results
  - style checks
  - the typed verification ledger stored in the run database
  - source provenance and OCR queue metadata

Any LLM-authored interpretation or briefing is intentionally excluded from the official
report and may exist only as a separate, post-hoc artifact when explicitly requested.

Three axes, never merged: exists? (resolve) / correct style? (style) / supports? (verify).

Usage:
  python run.py report --run runs/<timestamp>
"""

# Re-export the current report surface.
from core.report.io import (  # noqa: F401
    _db_projection_sha256,
    _load_run_projection,
    _load_style_projection,
    _repo_open,
    _repo_setting,
    _sha256_file,
    _verification_projection_sha256,
)
from core.report.render import (  # noqa: F401
    DEBUG_REPORT_BANNER,
    UNPROTECTED_AGENT_REPORT_BANNER,
    main,
    render,
    trusted_report_integrity,
    write_report,
)
from core.report.rollup import (  # noqa: F401
    BADGE,
    abstract_availability,
    claim_badge,
    crediting_result,
    effective_multisource_claim_ids,
    isolated_uncertain_pairs,
    latest_terminal_causes,
    terminal_attempt_causes_by_pair,
    terminal_failure_dimension,
    terminal_health_dimensions,
    terminal_pair_counts,
)
from core.report.sealing import (  # noqa: F401
    _parse_entry_meta,
    iter_report_entries,
    journal_entry,
    journal_path,
    journal_separator,
    latest_report_entry,
    latest_snapshot_text,
    provenance,
    provenance_fields,
    seal_payload,
    strip_seal,
)
