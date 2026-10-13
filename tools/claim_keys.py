#!/usr/bin/env python3
# tools/claim_keys.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Print V3 content-addressed manuscript/reference pair keys."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.parse.parse_manuscript import parse
from core.verify.pair_keys import assign_pair_keys
from tools.stable_pair_keys import canonical_pair_rows


def manifest_pair_universe(manifest: dict) -> set[str]:
    expected = manifest.get("expected") or {}
    universe = {pair for values in expected.values() for pair in (values or [])}
    universe.update(manifest.get("either") or {})
    universe.update(manifest.get("forbidden") or {})
    return universe


def stable_claim_keys_from_result(result: dict, manuscript_sha: str) -> list[str]:
    claims = {claim["id"]: claim for claim in result.get("claims", [])}
    references = {reference.get("id"): reference for reference in result.get("references", [])}
    order = {claim_id: position for position, claim_id in enumerate(claims, 1)}
    citations = sorted(
        (citation for citation in result.get("citations", [])
         if citation.get("claim_id") in claims and citation.get("ref_number") is not None),
        key=lambda citation: (order[citation["claim_id"]], citation["claim_id"],
                              citation["ref_number"], citation.get("ref_id") or ""),
    )
    rows = []
    for citation in citations:
        claim = claims[citation["claim_id"]]
        reference = references.get(citation.get("ref_id")) or {}
        rows.append({
            "claim_id": citation["claim_id"], "claim_order": order[citation["claim_id"]],
            "ref_number": citation["ref_number"], "sentence": claim.get("sentence", ""),
            "context_window": claim.get("context_window", ""),
            "marker_raw": claim.get("marker_raw") or citation.get("marker_raw") or "",
            "ref_id": reference.get("id"),
            **{name: reference.get(name) for name in (
                "raw_entry", "title", "doi", "pmid", "isbn", "year",
                "ay_surname", "ay_year", "ay_suffix",
            )},
        })
    return [row["key"] for row in assign_pair_keys(canonical_pair_rows(rows), manuscript_sha)]


def stable_claim_keys(path: str) -> list[str]:
    result, _ = parse(path, window=1, citations_mode="auto")
    return stable_claim_keys_from_result(
        result, hashlib.sha256(Path(path).read_bytes()).hexdigest()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manuscript")
    parser.add_argument("--manifest", help="also compare against a V3 benchmark manifest")
    args = parser.parse_args()
    manifest = None
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if manifest.get("pair_key_version") != 3:
            parser.error("manifest must declare pair_key_version 3")
    actual = stable_claim_keys(args.manuscript)
    for key in sorted(actual):
        print(key)
    if manifest is None:
        return 0
    actual_set = set(actual)
    expected = manifest_pair_universe(manifest)
    missing, extra = sorted(expected - actual_set), sorted(actual_set - expected)
    print(f"summary: {len(actual)} keys, {len(missing)} missing, {len(extra)} extra")
    for key in missing:
        print(f"MISSING: {key}")
    for key in extra:
        print(f"EXTRA: {key}")
    return int(bool(missing or extra))


if __name__ == "__main__":
    raise SystemExit(main())
