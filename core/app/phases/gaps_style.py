#!/usr/bin/env python3
# core/app/phases/gaps_style.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Phase: gaps + style — missing source detection and citation style conformance check."""

from __future__ import annotations

from core.app.runtime.repository import _load_parse_payload
from core.app.runtime.settings import _progress


def phase_gaps(st):
    """Check for missing sources, fill diagnostic gaps before verify."""
    _progress("checking for missing sources and filling gaps")
    run = st["run_dir"]
    try:
        from core.fetch.diagnostics import gaps as _gaps
    except ImportError:
        import gaps as _gaps
    _gaps.build_gap_report(run, accuracy=st["accuracy"])
    return "style"


def phase_style(st):
    """Check citation style conformance for every reference."""
    _progress("checking citation style conformance")
    run = st["run_dir"]
    try:
        from core.style import check as _style_check
    except ImportError:
        from style import check as _style_check
    refs = _load_parse_payload(run).get("references", [])
    for ref in refs:
        _style_check.run_one(st["style"], ref.get("source_type", "unknown"), ref["raw_entry"])
    return "verify"
