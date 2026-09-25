#!/usr/bin/env python3
# core/report/sealing.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Report sealing — provenance binding, journal append, HMAC seals."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone

try:
    from core.infra.integrity import signing as _signing
    from core.report.io import (
        _db_projection_sha256, _repo_open, _verification_projection_sha256,
    )
except ImportError:
    import signing as _signing
    from io import _db_projection_sha256, _repo_open, _verification_projection_sha256


# The seal comment that core.report appends to report.md. Matched (and stripped) by
# both the writer and the gate so the seal can bind the report BODY, not only its inputs.
_SEAL_COMMENT_RE = re.compile(
    r"\n?<!--\s*citation-verifier-provenance\b.*?-->\s*$", re.DOTALL)
_ENTRY_RE = re.compile(
    r"<!--\s*citation-verifier-report-entry\b(?P<meta>.*?)-->\n"
    r"(?P<body>.*?)"
    r"\n<!--\s*/citation-verifier-report-entry\s*-->",
    re.DOTALL,
)
_META_FIELD_RE = re.compile(r"(\w+)=([^\s]+)")


def provenance_fields(run_dir, summary):
    """The canonical fields that bind a report to the EXACT inputs it projects: the
    parse and the typed verification ledger. signed by signing.py over
    signing.canonical()."""
    repo = _repo_open(run_dir)
    repo.close()
    return {
        "run_db_projection_sha256": _db_projection_sha256(run_dir),
        "parse_sha256": None,
        "verification_sha256": _verification_projection_sha256(run_dir),
        "style_sha256": None,
        "claims": summary.get("claims"),
        "references": summary.get("references"),
        "pairs_total": summary.get("pairs_total"),
        "pairs_missing_text": summary.get("pairs_missing_text"),
        "pairs_accepted": summary.get("pairs_accepted"),
        "ci_pass": summary.get("ci_pass"),
    }


def provenance(run_dir, summary):
    """Content seal: a plain sha256 over the canonical fields. Catches a
    hand-written or stale report (the agent can recompute it — that is fine for content
    binding). For an UN-forgeable approval, signing.py adds an HMAC seal (see main)."""
    fields = provenance_fields(run_dir, summary)
    sig = hashlib.sha256(_signing.canonical(fields)).hexdigest()
    return {"signature": sig, "report_generator": "core.report", **fields}


def strip_seal(md_text):
    """Return the report body without its trailing provenance seal comment, so the body
    hash is computed identically by the writer (before sealing) and the gate (after)."""
    return _SEAL_COMMENT_RE.sub("", md_text)


def _parse_entry_meta(raw_meta):
    return {k: v for k, v in _META_FIELD_RE.findall(raw_meta or "")}


def iter_report_entries(report_text):
    """Yield append-only report journal entries in on-disk order."""
    for m in _ENTRY_RE.finditer(report_text or ""):
        meta = _parse_entry_meta(m.group("meta"))
        yield {
            "start": m.start(),
            "end": m.end(),
            "meta": meta,
            "body": m.group("body"),
            "raw": m.group(0),
        }


def latest_report_entry(report_text):
    entries = list(iter_report_entries(report_text))
    return entries[-1] if entries else None


def latest_snapshot_text(report_text):
    """Return the latest verifiable append-only journal snapshot body."""
    entry = latest_report_entry(report_text)
    if entry is None:
        raise ValueError("report journal contains no verifiable snapshot entry")
    return entry["body"]


def seal_payload(fields, body_sha256):
    """Bytes that the seal signs: the canonical input fields AND a hash of the report
    body. Binding the body too means editing the report's prose after signing breaks the
    seal — not only changing typed run facts. Used for both content= and the HMAC sig=."""
    return _signing.canonical(fields) + b"\n" + body_sha256.encode("ascii")


def journal_entry(snapshot_md, *, created_at, history_sha256, entry_version="1"):
    """Wrap a signed report snapshot into an append-only journal entry."""
    history = history_sha256 or "none"
    return (
        f"<!-- citation-verifier-report-entry v={entry_version} "
        f"created_at={created_at} history_sha256={history} -->\n"
        f"{snapshot_md.rstrip()}\n"
        f"<!-- /citation-verifier-report-entry -->\n"
    )


def journal_separator(existing_text):
    """Separator inserted before a new journal entry, if any."""
    if not existing_text:
        return ""
    sep = ""
    prefix = existing_text
    if not prefix.endswith("\n"):
        sep += "\n"
        prefix += "\n"
    if not prefix.endswith("\n\n"):
        sep += "\n"
    return sep


def journal_path(run_dir):
    return os.path.join(run_dir, "report.journal.md")
