#!/usr/bin/env python3
# tools/build_journal_authority.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Build a deterministic SQLite journal-authority snapshot.

Accepted inputs are the open NLM ``J_Medline.txt`` format, a canonical CSV, or
an authorised ISSN Register MARCXML export. Licensed registry data is imported
locally and is never bundled with Callimachus.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.resolve.journal_authority import build_catalog  # noqa: E402
from core.resolve.journal_authority_sources import (  # noqa: E402
    csv_records as _csv_records,
    marcxml_records as _marcxml_records,
    nlm_records as _nlm_records,
    require_first as _require_first,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--format", choices=("nlm", "csv", "issn-marcxml"), required=True,
    )
    parser.add_argument("--registry", required=True)
    parser.add_argument("--registry-version", required=True)
    args = parser.parse_args(argv)

    if args.format == "nlm":
        records = _nlm_records(args.input.read_text(encoding="utf-8"))
    elif args.format == "csv":
        records = _csv_records(args.input)
    else:
        records = _marcxml_records(args.input)
    try:
        build_catalog(
            args.output, _require_first(records),
            registry=args.registry, registry_version=args.registry_version,
            merge_duplicate_ids=args.format == "issn-marcxml",
        )
    except ValueError as exc:
        parser.error(str(exc))
    with sqlite3.connect(args.output) as conn:
        record_count = int(conn.execute(
            "SELECT record_count FROM snapshot WHERE singleton=1"
        ).fetchone()[0])
    print(f"journal authority: {record_count} identities -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
