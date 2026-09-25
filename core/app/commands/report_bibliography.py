# core/app/commands/report_bibliography.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only bibliographic screening export, independent of Fetch and Verify.

This is deliberately not the sealed full-pipeline report and never marks a run
complete. Every risk/tag is projected from persisted Resolve evidence, not
inferred from missing full text, an absent verdict, or a provider timeout.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote

from core.infra.db import RunRepository
from core.infra.integrity import IntegrityGateError, RunIntegrityGate
from core.shared.typed_canonical import encode


_REF_FIELDS = ("id", "ref_number", "raw_entry", "title", "doi", "pmid", "isbn", "year")
_RESOLVE_FIELDS = (
    "status", "via", "matched_title", "reason", "reference_status_tag",
    "fabrication_risk", "tag_reason", "resolution_basis", "existence_confidence",
    "resolved_identifier", "identity_state", "identity", "retracted", "checked_at",
    "trace", "evidence_profile", "identifier_validations", "retraction_checks",
)
_BANNER = (
    "BIBLIOGRAPHIC SCREENING ONLY — persisted Parse/Resolve evidence. "
    "Fetch and semantic Verify are not run by this command. "
    "This is not the sealed full-pipeline report; run/task state is unchanged. "
    "A missing source, unresolved lookup, or missing verdict does not prove fabrication."
)


def read_snapshot(run_dir: str) -> dict:
    """Capture one SQLite read snapshot; never migrate, downgrade, or answer tasks."""
    repo = RunRepository.open_readonly(run_dir)
    gate = None
    try:
        assurance = repo.get_execution_assurance()
        if assurance.protection == "agent_attested":
            # No local downgrade or override is offered by a read-only exporter.
            gate = RunIntegrityGate.from_environment()
            gate.preflight(run_dir, mirror_audit_records=False)
        repo._conn.execute("BEGIN")
        run = repo.get_run()
        parse = repo.effective_parse_payload()
        results = repo.resolve_payload_map()
        refs = parse.get("references") or []
        if not refs:
            raise ValueError("no parsed references: complete Parse before exporting")
        rows = []
        for ref in sorted(refs, key=lambda r: (r.get("ref_number") is None,
                                               r.get("ref_number") or 0, r["id"])):
            result = results.get(ref["id"])
            adjudication = (
                ((result or {}).get("evidence_profile") or {}).get(
                    "bibliographic_adjudication"
                ) or {}
            )
            rows.append({
                "reference": {key: ref.get(key) for key in _REF_FIELDS},
                "resolve": None if result is None else {
                    key: result.get(key) for key in _RESOLVE_FIELDS
                },
                "suspected_fabricated": bool(
                    result and result.get("reference_status_tag") == "suspected_fabricated"
                ),
                "bibliographic_outcome": adjudication.get("outcome"),
            })
        data = {
            "format": "callimachus.bibliographic-screening.v1",
            "scope": "bibliographic_existence_only",
            "notice": _BANNER,
            "semantic_verification": "not_performed_by_this_export",
            "full_pipeline_completion_asserted": False,
            "export_signed": False,
            "source_run": {
                "run_id": run.run_id, "input_sha256": run.input_sha256,
                "created_at": run.created_at, "phase": run.phase, "status": run.status,
                "schema_version": repo.schema_version,
                "execution_assurance": assurance.protection,
                "authority_preflight": "checked" if gate is not None else "not_attested",
            },
            "counts": {
                "references": len(rows),
                "resolve_recorded": sum(row["resolve"] is not None for row in rows),
                "not_checked": sum(row["resolve"] is None for row in rows),
                "suspected_fabricated": sum(row["suspected_fabricated"] for row in rows),
                "transient_unresolved": sum(
                    (row["resolve"] or {}).get("status") == "unresolved" for row in rows
                ),
                "retraction_flagged": sum(
                    (row["resolve"] or {}).get("retracted") is True for row in rows
                ),
                "identified": sum(row["bibliographic_outcome"] == "identified" for row in rows),
                "identified_with_errors": sum(
                    row["bibliographic_outcome"] == "identified_with_errors" for row in rows
                ),
                "refuted": sum(row["bibliographic_outcome"] == "refuted" for row in rows),
                "not_corroborated": sum(
                    row["bibliographic_outcome"] == "not_corroborated" for row in rows
                ),
                "checks_incomplete": sum(
                    row["bibliographic_outcome"] == "checks_incomplete" for row in rows
                ),
            },
            "references": rows,
        }
        repo._conn.rollback()
        if gate is not None:
            gate.preflight(run_dir, mirror_audit_records=False)
        data["snapshot_sha256"] = hashlib.sha256(encode(data)).hexdigest()
        return data
    finally:
        repo.close()


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else "—"), quote=True)


def _md(value: object) -> str:
    value = _escape(value).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", value)


def render_markdown(data: dict, *, suspects_only: bool = False) -> str:
    counts = data["counts"]
    lines = ["# Callimachus — Bibliographic screening", "", _BANNER, "",
             f"Run: {_md(data['source_run']['run_id'])}",
             f"Snapshot SHA-256: `{data['snapshot_sha256']}`", "",
             f"References: {counts['references']}; Resolve recorded: {counts['resolve_recorded']}; "
             f"not checked: {counts['not_checked']}; suspected fabrication: {counts['suspected_fabricated']}; "
             f"transient unresolved: {counts['transient_unresolved']}; retraction flags: {counts['retraction_flagged']}.",
             "", "## Suspected fabrication", ""]
    suspects = [row for row in data["references"] if row["suspected_fabricated"]]
    if not suspects:
        lines += ["No `suspected_fabricated` tag is recorded. This is not proof that every reference exists.", ""]
    groups = [(None, suspects)]
    if not suspects_only:
        groups.append(("Other references / incomplete checks", [
            row for row in data["references"] if not row["suspected_fabricated"]
        ]))
    for title, rows in groups:
        if title:
            lines += [f"## {title}", ""]
        for row in rows:
            ref, result = row["reference"], row["resolve"] or {}
            adjudication = (result.get("evidence_profile") or {}).get(
                "bibliographic_adjudication"
            ) or {}
            label = ref.get("ref_number") or ref["id"]
            lines += [f"### Reference {_md(label)}", "",
                      _md(ref.get("raw_entry") or ref.get("title")), "",
                      f"Status: {_md(result.get('status') or 'not_checked')}; "
                      f"tag: {_md(result.get('reference_status_tag'))}; "
                      f"fabrication risk: {_md(result.get('fabrication_risk'))}.",
                      f"Provider: {_md(result.get('via'))}; matched title: {_md(result.get('matched_title'))}.",
                      f"Reason: {_md(result.get('tag_reason') or result.get('reason'))}.",
                      f"Retraction: {'FLAGGED' if result.get('retracted') is True else 'no flag recorded (not a negative attestation)'}." ]
            if adjudication:
                lines.append(
                    f"Bibliographic adjudication: {_md(adjudication.get('outcome'))}; "
                    f"checks: {_md(adjudication.get('check_status'))}; "
                    f"correction: {_md(adjudication.get('correction_status'))}."
                )
                for refutation in adjudication.get("refutations") or []:
                    lines.append(
                        f"Refutation: {_md(refutation.get('kind'))} on "
                        f"{_md(refutation.get('field'))}: cited "
                        f"{_md(refutation.get('cited_value'))}, observed "
                        f"{_md(refutation.get('observed_value'))}; source "
                        f"{_md(refutation.get('source'))}."
                    )
            if ref.get("doi"):
                lines.append(f"Cited DOI: {_md(ref['doi'])}")
            lines.append("")
    return "\n".join(lines) + "\n"


def render_html(data: dict, *, suspects_only: bool = False) -> str:
    # No script, external stylesheet, embedded source text, or arbitrary href.
    rows = []
    selected = [row for row in data["references"] if row["suspected_fabricated"] or not suspects_only]
    selected.sort(key=lambda row: not row["suspected_fabricated"])
    for row in selected:
        ref, result = row["reference"], row["resolve"] or {}
        adjudication = (result.get("evidence_profile") or {}).get(
            "bibliographic_adjudication"
        ) or {}
        doi = str(ref.get("doi") or "")
        doi_cell = _escape(doi or None)
        if re.match(r"^10\.\d{4,9}/\S+$", doi):
            doi_cell = f'<a href="https://doi.org/{quote(doi, safe="/")}" rel="noreferrer">{_escape(doi)}</a>'
        values = [
            _escape(ref.get("ref_number") or ref["id"]),
            _escape(ref.get("raw_entry") or ref.get("title")),
            _escape(result.get("status") or "not_checked"),
            _escape(result.get("reference_status_tag")),
            _escape(result.get("fabrication_risk")),
            _escape(result.get("tag_reason") or result.get("reason")),
            _escape(adjudication.get("outcome")),
            _escape(result.get("matched_title")), doi_cell,
            "FLAGGED" if result.get("retracted") is True else "No flag recorded",
        ]
        rows.append("<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>")
    counts = "; ".join(f"{_escape(key)}: {value}" for key, value in data["counts"].items())
    empty = "<p>No suspected-fabrication tag recorded; incomplete checks are not proof of existence.</p>" if not selected else ""
    headers = "".join(f"<th>{name}</th>" for name in (
        "Reference", "Cited entry", "Resolve status", "Recorded tag", "Fabrication risk",
        "Recorded reason", "Bibliographic adjudication", "Matched title", "Cited DOI",
        "Retraction flag",
    ))
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Callimachus — Bibliographic screening</title>'
        '<style>body{font:16px system-ui;margin:2rem;line-height:1.5}table{border-collapse:collapse;width:100%}'
        'td,th{padding:.6rem;border:1px solid #bbb;text-align:left;vertical-align:top}'
        '.notice{padding:1rem;border:2px solid #805d00}code{overflow-wrap:anywhere}</style>'
        f'<h1>Callimachus — Bibliographic screening</h1><p class="notice">{_escape(_BANNER)}</p>'
        f'<p>Run: {_escape(data["source_run"]["run_id"])}. Assurance: {_escape(data["source_run"]["execution_assurance"])}.</p>'
        f'<p>{counts}</p><p>Snapshot SHA-256: <code>{data["snapshot_sha256"]}</code></p>'
        '<p>Suspected-fabrication rows are shown first. Retraction is a separate flag, not evidence of fabrication.</p>'
        f'{empty}<table><thead><tr>{headers}</tr></thead><tbody>{"".join(rows)}</tbody></table></html>\n'
    )


def export(run_dir: str, output_dir: str | None = None, *, suspects_only: bool = False) -> Path:
    run = Path(run_dir).resolve(strict=True)
    target = Path(output_dir).resolve() if output_dir else run.with_name(run.name + "-bibliography")
    if target == run or run in target.parents:
        raise ValueError("bibliographic exports must be outside the run to preserve its artifact inventory")
    data = read_snapshot(str(run))
    outputs = {
        "bibliography-report.md": render_markdown(data, suspects_only=suspects_only),
        "bibliography-report.html": render_html(data, suspects_only=suspects_only),
        "bibliography-report.json": json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
    }
    target.mkdir(parents=True, exist_ok=True)
    for name in outputs:
        path = target / name
        if path.is_symlink():
            raise ValueError(f"refusing a symlink export target: {path}")
    for name, text in outputs.items():
        fd, temporary = tempfile.mkstemp(prefix=".bibliography-", dir=target)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(text.encode("utf-8"))
            os.replace(temporary, target / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=_BANNER)
    parser.add_argument("--run", required=True, help="existing DB-native run (Parse/Resolve may be partial)")
    parser.add_argument("--output", help="export directory outside the run; default: <run>-bibliography")
    parser.add_argument("--suspects-only", action="store_true", help="filter Markdown/HTML detail to recorded suspected-fabrication tags; JSON retains all rows")
    args = parser.parse_args()
    try:
        output = export(args.run, args.output, suspects_only=args.suspects_only)
    except (OSError, RuntimeError, ValueError, IntegrityGateError) as exc:
        print(f"bibliographic export failed: {exc}", file=sys.stderr)
        return 2
    print(f"Bibliographic screening: {output / 'bibliography-report.html'}")
    print("Markdown and JSON written alongside. No Fetch/Verify executed; original run remains unchanged.")
    return 0
