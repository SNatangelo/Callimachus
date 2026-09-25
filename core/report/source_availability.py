# core/report/source_availability.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed physical availability checks for persisted source text."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath, PureWindowsPath


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def is_verifiable_source_entry(entry: dict) -> bool:
    """True only for source-attributed text admissible to citation Verify."""
    return entry.get("tier") in {"fulltext", "abstract"} or (
        entry.get("tier") == "web" and entry.get("origin") == "googlebooks"
    )


def usable_source_refs(run_dir, manifest):
    """Return usable ref IDs and deterministic diagnostics for bad source entries.

    The manifest is a ledger projection, not proof that the referenced bytes
    remain available.  Validate every entry against its stored path, digest,
    and character count before treating its reference as evidence-bearing.
    """
    entries = manifest.get("entries", ()) if isinstance(manifest, dict) else ()
    run_root = Path(run_dir).resolve()
    sources_root = run_root / "sources"
    usable = set()
    unavailable = []
    for entry in entries:
        if not isinstance(entry, dict):
            unavailable.append("source manifest entry is malformed")
            continue
        ref_id = entry.get("ref_id")
        source_id = entry.get("source_text_id")
        label = str(source_id or ref_id or "unknown")
        stored_as = entry.get("stored_as")
        expected_sha256 = entry.get("sha256")
        expected_char_count = entry.get("char_count")
        if (
            not isinstance(ref_id, str) or not ref_id
            or not isinstance(stored_as, str) or not stored_as
            or "\\" in stored_as
            or PurePosixPath(stored_as).is_absolute()
            or PureWindowsPath(stored_as).is_absolute()
            or ".." in PurePosixPath(stored_as).parts
            or not isinstance(expected_sha256, str)
            or not _SHA256_RE.fullmatch(expected_sha256)
            or isinstance(expected_char_count, bool)
            or not isinstance(expected_char_count, int)
            or expected_char_count < 0
        ):
            unavailable.append(f"source {label} has an invalid persisted ledger entry")
            continue
        try:
            source_path = (sources_root / Path(*PurePosixPath(stored_as).parts)).resolve(
                strict=True
            )
            source_path.relative_to(sources_root.resolve(strict=True))
            if not source_path.is_file():
                raise OSError("not a regular file")
            source_bytes = source_path.read_bytes()
            source_text = source_bytes.decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            unavailable.append(f"source {label} is unavailable")
            continue
        if hashlib.sha256(source_bytes).hexdigest() != expected_sha256:
            unavailable.append(f"source {label} hash mismatch")
            continue
        if len(source_text) != expected_char_count:
            unavailable.append(f"source {label} length mismatch")
            continue
        usable.add(ref_id)
    return frozenset(usable), tuple(unavailable)


def usable_verification_source_refs(run_dir, manifest):
    """Physical availability limited to evidence that can support a citation verdict."""
    entries = manifest.get("entries", ()) if isinstance(manifest, dict) else ()
    return usable_source_refs(
        run_dir,
        {"entries": [entry for entry in entries
                     if isinstance(entry, dict) and is_verifiable_source_entry(entry)]},
    )
