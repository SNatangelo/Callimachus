#!/usr/bin/env python3
# tools/claims_snapshot.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Snapshot the claims of every paper in the corpus, and diff a later parse against it.

Why this exists.  The two signals we normally watch — the orphan count and the reference
coverage — are both blind to a citation that was INVENTED.  A fabricated marker pointing
at a reference number that happens to exist resolves cleanly, so it never becomes an
orphan, and it pushes coverage UP, so that alarm reads as good news.  Totals cannot see
this class of error; only reading the markers one by one can.

So before a change that can add markers, snapshot what every paper currently says, and
afterwards diff and READ the differences.  The interesting category is not "how many
claims changed" but "this sentence, which was already there, now carries a marker it did
not carry before" — that is where a fabrication shows itself, and where a recovered
citation does too.  They look identical to a counter and obvious to a reader.

    python3 tools/claims_snapshot.py snapshot            # before the change
    python3 tools/claims_snapshot.py diff                # after it

Snapshots live next to the corpus in one SQLite database. The parse
always runs with the PDF link layer OFF: the text pathway is the regression oracle, and
a link layer that substitutes for parsing would hide the very thing we are measuring.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.claims_snapshot_store import read_snapshot, write_snapshots

CORPUS_GLOBS = ("Paper test/*.pdf", "Paper test/Nuovo/*.pdf")
DEFAULT_DIR = "Paper test/claims_baseline"


def _skeleton(sentence: str) -> str:
    """The sentence stripped to its letters — a key that survives a marker change.

    "CheckM v1.2.231 on all the genomes" and "CheckM v1.2.2⟦SUP:31⟧ on all the genomes"
    are the same sentence with a different reading of its digits.  Keying on the letters
    alone lets the diff align them and report what it is actually about: the markers.
    """
    return " ".join(re.sub(r"[^a-z ]+", " ", sentence.lower()).split())


def _snapshot_one(path: str) -> dict:
    from core.parse import parse_manuscript as pm
    try:
        result, _ = pm.parse(path, window=1)
    except Exception as e:                                  # noqa: BLE001
        return {"file": path, "error": f"{type(e).__name__}: {e}"}

    cited: dict[str, list] = {}
    for c in result.get("citations") or []:
        cited.setdefault(c["claim_id"], []).append(c.get("ref_number"))

    claims = []
    for c in result.get("claims") or []:
        claims.append({
            "skeleton": _skeleton(c["sentence"]),
            "sentence": c["sentence"],
            "marker_raw": c.get("marker_raw"),
            "marker_numbers": c.get("marker_numbers"),
            "resolved": sorted(x for x in cited.get(c["id"], []) if x is not None),
            "orphaned": cited.get(c["id"], []).count(None),
        })
    refs = result.get("references") or []
    cov = result["_debug"]["reference_coverage"]
    return {
        "file": path,
        "format": (result.get("manuscript") or {}).get("format"),
        "citation_mode": (result.get("manuscript") or {}).get("citation_mode"),
        "totals": {
            "claims": len(claims),
            "references": len(refs),
            "orphans": sum(1 for c in result.get("citations") or []
                           if not c.get("ref_id")),
            "coverage_pct": cov["pct"],
        },
        "references": [{"number": r.get("ref_number"),
                        "raw": (r.get("raw_entry") or "")[:110]} for r in refs],
        "claims": claims,
    }


def _corpus() -> list[str]:
    files: list[str] = []
    for pattern in CORPUS_GLOBS:
        files.extend(sorted(glob.glob(pattern)))
    return files


def cmd_snapshot(args) -> int:
    os.environ.setdefault("CITATION_VERIFIER_PDF_LINKS", "0")
    os.makedirs(args.dir, exist_ok=True)
    snapshots = []
    for path in _corpus():
        snap = _snapshot_one(path)
        snapshots.append(snap)
        t = snap.get("totals")
        print(f"  {os.path.basename(path)[:44]:46} "
              + (f"claims={t['claims']:4} refs={t['references']:4} "
                 f"orphans={t['orphans']:4} coverage={t['coverage_pct']:5.1f}%"
                 if t else f"ERROR {snap['error'][:40]}"))
    database = os.path.join(args.dir, "claims_snapshot.sqlite")
    write_snapshots(database, snapshots)
    print(f"\nSnapshot written to {database}")
    return 0


def _index(snap: dict) -> dict:
    """skeleton -> the claim, so the same sentence can be found on both sides."""
    out: dict[str, dict] = {}
    for c in snap.get("claims") or []:
        out.setdefault(c["skeleton"], c)
    return out


def _markers(claim: dict) -> tuple:
    return tuple(claim.get("marker_numbers") or [])


def cmd_diff(args) -> int:
    os.environ.setdefault("CITATION_VERIFIER_PDF_LINKS", "0")
    database = os.path.join(args.dir, "claims_snapshot.sqlite")
    if not os.path.isfile(database):
        print(f"no snapshot in {args.dir} — run `snapshot` first", file=sys.stderr)
        return 1
    grand = {"changed": 0, "new": 0, "gone": 0}
    for path in _corpus():
        old = read_snapshot(database, path)
        if old is None:
            continue
        new = _snapshot_one(path)
        label = os.path.basename(path)[:44]

        if old.get("error") or new.get("error"):
            if old.get("error") != new.get("error"):
                print(f"\n### {label}\n  ERROR changed: {old.get('error')} -> "
                      f"{new.get('error')}")
            continue

        ot, nt = old["totals"], new["totals"]
        deltas = {k: nt[k] - ot[k] for k in ot}
        oi, ni = _index(old), _index(new)

        changed = [(k, oi[k], ni[k]) for k in oi.keys() & ni.keys()
                   if _markers(oi[k]) != _markers(ni[k])]
        gone = [oi[k] for k in oi.keys() - ni.keys()]
        added = [ni[k] for k in ni.keys() - oi.keys()]
        if not (changed or gone or added or any(deltas.values())):
            print(f"\n### {label}\n  unchanged")
            continue

        grand["changed"] += len(changed)
        grand["new"] += len(added)
        grand["gone"] += len(gone)
        print(f"\n### {label}")
        print("  totals  " + "  ".join(
            f"{k}: {ot[k]} -> {nt[k]} ({deltas[k]:+g})" if deltas[k] else f"{k}: {nt[k]}"
            for k in ("claims", "references", "orphans", "coverage_pct")))

        # The category that matters: a sentence that was always there, now read
        # differently.  Every line here has to be read, not counted.
        for _k, o, n in sorted(changed, key=lambda x: str(_markers(x[2]))):
            print(f"\n  MARKERS {list(_markers(o))} -> {list(_markers(n))}"
                  f"   (resolved {o['resolved']} -> {n['resolved']})")
            print(f"    was: …{o['sentence'].strip()[:150]}")
            print(f"    now: …{n['sentence'].strip()[:150]}")
        for c in added:
            print(f"\n  CLAIM APPEARED  markers={c['marker_numbers']} "
                  f"resolved={c['resolved']}\n    {c['sentence'].strip()[:150]}")
        for c in gone:
            print(f"\n  CLAIM VANISHED  markers={c['marker_numbers']} "
                  f"resolved={c['resolved']}\n    {c['sentence'].strip()[:150]}")

    print(f"\n=== {grand['changed']} sentences re-read, {grand['new']} claims appeared, "
          f"{grand['gone']} vanished — read them, do not count them.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("snapshot", "diff"))
    ap.add_argument("--dir", default=DEFAULT_DIR,
                    help=f"where the snapshots live (default: {DEFAULT_DIR})")
    args = ap.parse_args()
    return cmd_snapshot(args) if args.command == "snapshot" else cmd_diff(args)


if __name__ == "__main__":
    raise SystemExit(main())
