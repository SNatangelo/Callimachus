#!/usr/bin/env python3
# tools/parsed_sources_snapshot.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Snapshot every corpus paper's references the way resolve/fetch receives them.

Writes one Markdown file per paper plus an INDEX table, in the same shape as the
``Paper test/parsed_sources`` baseline, so a parser change can be diffed against
it paper by paper.  The baseline itself is never written to: the output folder is
supplied by the caller and refused if it is the baseline.

Usage:
  python3 tools/parsed_sources_snapshot.py --out "Paper test/parsed_sources_<date>"
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.parse import footnotes
from core.parse.parse_manuscript import parse

CORPUS_DIRS = ("Paper test", os.path.join("Paper test", "Nuovo"))
BASELINE_DIR = os.path.join("Paper test", "parsed_sources")


def corpus_papers() -> list[str]:
    paths = []
    for d in CORPUS_DIRS:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.lower().endswith(".pdf"):
                paths.append(os.path.join(d, name))
    return paths


def _identifier_line(ref: dict) -> str:
    parts = []
    for key in ("doi", "pmid", "isbn", "url"):
        value = str(ref.get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if not parts:
        return "(no identifier — resolves by title+year)"
    return "  ".join(parts)


def _render_paper(paper: str, parsed: dict) -> tuple[str, dict]:
    refs = parsed.get("references") or []
    mode = (parsed.get("manuscript") or {}).get("citation_mode") or "?"
    xrefs = footnotes.cross_reference_map(refs)
    by_id = {r.get("id"): r for r in refs}

    # Identifier tallies span every reference, cross-references included: a back
    # reference often reprints the antecedent's link, and the baseline counts it.
    # "title-only" instead describes what still needs resolving by title+year, so
    # there cross-references are excluded - they inherit an antecedent's result.
    # The asymmetry is not an oversight: it reproduces how the baseline in
    # Paper test/parsed_sources/ was counted, and that baseline is the only
    # historical reference point for a parser regression.  Making the two
    # consistent would make every diff against it meaningless.
    counts = {"doi": 0, "pmid": 0, "isbn": 0, "url": 0, "cross": 0, "title_only": 0}
    for ref in refs:
        has_id = False
        for key in ("doi", "pmid", "isbn", "url"):
            if str(ref.get(key) or "").strip():
                counts[key] += 1
                has_id = True
        if ref.get("id") in xrefs:
            counts["cross"] += 1
        elif not has_id:
            counts["title_only"] += 1

    lines = [
        f"# {os.path.basename(paper)}",
        "",
        f"- citation mode: **{mode}**",
        f"- references parsed: **{len(refs)}**",
        f"- with DOI: {counts['doi']}  ·  PMID: {counts['pmid']}  ·  "
        f"ISBN: {counts['isbn']}  ·  URL: {counts['url']}",
        f"- cross-references (inherit another note): **{counts['cross']}**",
        f"- title-only (no identifier, resolve by title+year): {counts['title_only']}",
        "",
        "Each block below is one parsed source, in the exact shape the resolver receives.",
        "",
        "---",
        "",
    ]

    for ref in sorted(refs, key=lambda r: r.get("ref_number") or 0):
        number = ref.get("ref_number")
        raw = " ".join(str(ref.get("raw_entry") or "").split())
        target = xrefs.get(ref.get("id"))
        if target is not None:
            target_id, pinpoint = target
            target_number = (by_id.get(target_id) or {}).get("ref_number")
            head = f"### [{number}] — CROSS-REFERENCE → note {target_number}"
            if pinpoint:
                head += f", pinpoint {pinpoint}"
            lines += [
                head,
                f"- inherits the resolution/fulltext of note {target_number}",
                f"- raw: `{raw}`",
                "",
            ]
            continue
        title = str(ref.get("title") or "").strip()
        year = ref.get("year")
        head = f"### [{number}] — {ref.get('source_type') or 'unknown'}"
        if ref.get("further_reading"):
            head += "  ·  FURTHER-READING (not counted as cited)"
        lines += [
            head,
            f"- identifiers: {_identifier_line(ref)}",
            f"- title: {title or '(none extracted — resolver derives from raw)'}",
            f"- year: {year if year else '(none)'}",
            f"- raw: `{raw}`",
            "",
        ]

    counts["refs"] = len(refs)
    counts["mode"] = mode
    return "\n".join(lines), counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output folder (must not be the baseline)")
    args = ap.parse_args()

    out_dir = args.out
    if os.path.abspath(out_dir) == os.path.abspath(BASELINE_DIR):
        print(f"refusing to write into the baseline folder: {BASELINE_DIR}", file=sys.stderr)
        return 2
    os.makedirs(out_dir, exist_ok=True)

    papers = corpus_papers()
    print(f"{len(papers)} papers -> {out_dir}\n", flush=True)

    rows, failures = [], []
    for paper in papers:
        name = os.path.basename(paper)
        print(f"  {name} ... ", end="", flush=True)
        try:
            parsed, _debug = parse(paper, 1, "auto")
        except Exception as exc:  # noqa: BLE001 - a failing paper must not stop the sweep
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print("FAILED")
            traceback.print_exc(file=sys.stderr)
            continue
        body, counts = _render_paper(paper, parsed)
        stem = os.path.splitext(name)[0]
        with open(os.path.join(out_dir, f"{stem}.md"), "w", encoding="utf-8") as fh:
            fh.write(body)
        rows.append((name, counts))
        print(f"{counts['refs']} refs  (doi {counts['doi']}, url {counts['url']}, "
              f"x-ref {counts['cross']}, title-only {counts['title_only']})")

    index = [
        f"# Parsed-sources snapshot ({len(rows)} papers)",
        "",
        "What each paper's references look like the moment they reach resolve/fetch.",
        "One file per paper in this folder.",
        "",
        "| paper | mode | refs | with DOI | cross-refs | title-only |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for name, c in rows:
        index.append(
            f"| {name} | {c['mode']} | {c['refs']} | {c['doi']} | {c['cross']} | {c['title_only']} |")
    if failures:
        index += ["", "## Failed to parse", ""]
        index += [f"- `{name}` — {err}" for name, err in failures]
    with open(os.path.join(out_dir, "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(index) + "\n")

    print(f"\ntotal references: {sum(c['refs'] for _n, c in rows)}")
    if failures:
        print(f"failed papers: {len(failures)}")
        for name, err in failures:
            print(f"  {name}: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
