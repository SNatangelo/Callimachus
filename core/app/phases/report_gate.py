#!/usr/bin/env python3
# core/app/phases/report_gate.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Phase: report + gate — guarded multi-source composition, report generation, gate check."""

from __future__ import annotations

import json
import os
import sys

from core.app.runtime.repository import _save_state
from core.app.runtime.settings import ENV_REPORT_HTML
from core.infra.integrity import IntegrityGateError, RunIntegrityGate
from core.report import trusted_report_integrity, write_report
from core.report.human.projection import build_human_report_projection
from core.report.human.render import render_human_report
from core.report.human.sealing import write_html_report
from core.report.io import load_run_projection

from core.verify import verify_run


REFERENCE_ONLY_PREVIEW_FAILURES = (
    "Reference-check-only mode: semantic Verify was not completed.",
    "This preview is not audit-ready; previously recorded evidence is retained and may be incomplete.",
)


def _report_html_enabled(environ=None):
    """Read the opt-out HTML companion switch without a silent invalid fallback."""
    value = (os.environ if environ is None else environ).get(ENV_REPORT_HTML)
    normalized = str(value or "").strip().lower()
    if not normalized or normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{ENV_REPORT_HTML} must be one of 1/true/yes/on or 0/false/no/off"
    )


def phase_reference_report(st):
    """Render the standard HTML UI as an explicitly unverified preview."""
    run = st["run_dir"]
    try:
        projection = build_human_report_projection(load_run_projection(run))
        rendered = render_human_report(
            projection,
            preview=True,
            preview_failures=REFERENCE_ONLY_PREVIEW_FAILURES,
        )
        path = write_html_report(
            run,
            rendered=rendered,
            preview=True,
            # The surrounding Report phase already owns the integrity lease.
            gate=None,
        )
    except (IntegrityGateError, OSError, ValueError) as exc:
        print(
            f"Reference-check-only report failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return "error"

    print(f"\nReference-check-only report: {path}")
    print("Full-pipeline verification was not completed; the HTML is an unsealed preview.")
    st["phase"] = "done"
    _save_state(st)
    return "done"


def phase_report(st):
    run = st["run_dir"]
    try:
        gate = st.get("_execution_integrity_gate")
        integrity = trusted_report_integrity(gate, run) if gate is not None else None
        report_result = write_report(run, integrity=integrity)
        result = verify_run.verify(run, strict_crediting=False)
        verify_run.write_signature_status(run, strict_crediting=False)
        print("\n" + json.dumps({
            "report": os.path.join(run, "report.md"),
            "signature_status": os.path.join(run, "report.signature_status.md"),
            "audit_ready": report_result["audit_ready"],
            "debug_mode": report_result["debug_mode"],
            "integrity_run_state": report_result["integrity_run_state"],
            "integrity_content_store_state": report_result[
                "integrity_content_store_state"
            ],
            "gate_ok": result["ok"],
            "failures": result["failures"],
            "warnings": result["warnings"],
            "info": result["info"],
        }, ensure_ascii=False, indent=2))
        if not result["ok"]:
            print("\nGATE FAILED — report is incomplete or inauthentic. Not deliverable.",
                  file=sys.stderr)
            st["phase"] = "report"   # stay here; the agent must fix coverage and re-resume
            _save_state(st)
            return "_gate_failed"
        if _report_html_enabled():
            projection = build_human_report_projection(load_run_projection(run))
            rendered = render_human_report(projection)
            # The surrounding Report phase already owns the integrity lease. Opening a
            # second transition here would violate its single-writer contract.
            html_path = write_html_report(run, rendered=rendered, gate=None)
            print(f"[report] human HTML companion: {html_path}")
        return "done"
    except Exception as exc:
        print(f"report step failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return "error"
