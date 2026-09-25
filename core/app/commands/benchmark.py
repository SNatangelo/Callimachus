#!/usr/bin/env python3
# core/app/commands/benchmark.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
benchmark.py — cross-LLM and cross-run benchmarking from typed verification terminals.

Why this works WITHOUT a gold set
----------------------------------
The typed terminal projection records whether a pair has a crediting semantic outcome,
its assurance, resolution, and outcome. These give terminal completion and outcome-mix
metrics. They do NOT measure correctness against ground truth — that would need a
labelled corpus — so read the numbers as "terminal concordance", not "accuracy".

The unit of comparison is the RUN
---------------------------------
Each run contributes its typed terminal projection. A benchmark feeds in several
runs (each produced on a FROZEN deterministic fixture — same parse/resolve/sources —
so only the LLM slot varies) and this module measures concordance at two levels:
  - WITHIN a model: do separate runs of the SAME model agree on the same pairs?
    (the model's stability / determinism);
  - BETWEEN models: do different models agree on the same pairs?
    (where they diverge = the hard cases worth human review).

Crucial fairness rule: the deterministic phases must be run ONCE and frozen; otherwise
network variability in resolve/fetch contaminates the comparison. ``--debug-compare``
enforces the persisted frozen-run controls before comparing experimental runs.

Usage:
  python run.py benchmark --run runs/ts_gpt --run runs/ts_claude \\
         --out benchmark        # writes benchmark.md
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

from core.invocation import format_run_examples

try:
    from core.infra.db import RunRepository
    from core.report.verification_projection import project_verification_pairs
except ImportError:  # direct execution
    from db import RunRepository
    from verification_projection import project_verification_pairs

OUTCOMES = ["supports", "partial", "related", "off_topic", "contradicts"]


# --------------------------------------------------------------------------- #
#  Loading                                                                     #
# --------------------------------------------------------------------------- #

def _load_run_rows(run_dir: str) -> list[dict]:
    repo = RunRepository.open_readonly(run_dir)
    try:
        raw = repo.verification_raw_payloads()
        run = repo.get_run()
        runtime = repo.get_run_setting("verify_runtime", {})
    finally:
        repo.close()
    terminals = project_verification_pairs(
        raw["pair_states"], raw["candidates"], raw["candidate_events"]
    )
    fallback = os.path.basename(os.path.normpath(run_dir)) or run_dir
    rows = [{
        "run_id": run.run_id or fallback,
        "model_id": runtime.get("model") or run.model_id,
        "claim_id": row["claim_id"],
        "ref_id": row["ref_id"],
        "scope": row.get("scope") or "claim_evidence",
        "outcome": row["semantic_outcome"],
        "crediting": bool(row["crediting"]),
        "assurance": row["assurance"],
        "resolution": row["resolution"],
    } for row in terminals]
    return rows


def load_rows(run_dirs: list[str]) -> list[dict]:
    """Collect verdict rows from DB-backed run directories."""
    rows = []
    for d in run_dirs or []:
        rows.extend(_load_run_rows(d))
    return rows


def _debug_comparison_profile(run_dir: str) -> dict:
    """Read the persisted, strict-comparison provenance for one debug run."""
    repo = RunRepository.open_readonly(run_dir)
    try:
        run = repo.get_run()
        settings = repo.list_run_settings()
        pairs = [
            (row["claim_id"], row["ref_id"], row["scope"])
            for row in repo.verification_pair_state_payloads()
        ]
        tasks = [
            (task.claim_id, task.ref_id, task.scope)
            for task in repo.list_tasks(slot="verify")
            if task.task_kind == "claim_evidence"
        ]
    finally:
        repo.close()

    label = os.path.basename(os.path.normpath(run_dir)) or run_dir
    if settings.get("debug_mode") is not True:
        raise ValueError(f"debug comparison requires persisted debug_mode=true: {label}")
    if not isinstance(settings.get("debug_labels"), list) or not settings["debug_labels"]:
        raise ValueError(f"debug comparison requires non-empty debug_labels: {label}")

    runtime = settings.get("verify_runtime")
    config = settings.get("verify_claim_evidence_config")
    frozen = settings.get("frozen_fetch_provenance")
    if not isinstance(runtime, dict) or not isinstance(config, dict) or not isinstance(frozen, dict):
        raise ValueError(f"debug comparison missing persisted verify/frozen provenance: {label}")
    if not pairs:
        raise ValueError(f"debug comparison missing verification pair universe: {label}")

    def required(mapping: dict, key: str):
        if key not in mapping or mapping[key] in (None, ""):
            raise ValueError(f"debug comparison missing {key} provenance: {label}")
        return mapping[key]

    if not isinstance(settings.get("verify_table_citations"), bool):
        raise ValueError(f"debug comparison missing verify_table_citations provenance: {label}")
    if not isinstance(runtime.get("require_fulltext"), bool):
        raise ValueError(f"debug comparison missing require_fulltext provenance: {label}")
    config_controls = (
        "context_mode", "profile", "candidate_cap", "jury1_technical_cap",
        "jury2_technical_cap", "jury2_level", "aggregate_in_flight",
        "global_pacing_ms", "pacing_by_model_ms", "jury1_only", "jury2_only",
        "selection_seed", "selector_algorithm", "seed_derivation",
        "cursor_semantics", "credential_cursor", "model_draw_index", "cooldown",
        "cooldown_overrides",
    )
    for key in config_controls:
        if key not in config:
            raise ValueError(
                f"debug comparison missing effective {key} provenance: {label}"
            )
    for key in ("context_profile", "max_source_chars"):
        if key not in runtime:
            raise ValueError(
                f"debug comparison missing effective {key} provenance: {label}"
            )
    if runtime["context_profile"] != config["profile"]:
        raise ValueError(f"debug comparison has inconsistent context profile: {label}")

    code_snapshot_id = required(runtime, "code_snapshot_id")
    return {
        "run_id": run.run_id or label,
        "debug_labels": list(settings["debug_labels"]),
        "hard_controls": {
            "input_sha256": required({"input_sha256": run.input_sha256}, "input_sha256"),
            "source_inventory_sha256": required(frozen, "source_inventory_sha256"),
            "pair_task_universe": {
                "pairs": sorted(pairs),
                "verify_tasks": sorted(tasks, key=lambda task: tuple(
                    "" if value is None else value for value in task)),
            },
            "accuracy": required({"accuracy": run.accuracy}, "accuracy"),
            "verify_table_citations": settings["verify_table_citations"],
            "require_fulltext": runtime["require_fulltext"],
            "context_profile": runtime["context_profile"],
            "max_source_chars": runtime["max_source_chars"],
            "claim_evidence": {
                key: config[key] for key in config_controls
            },
        },
        "model_backend_reasoning": {
            "model": runtime.get("model"),
            "backend": runtime.get("backend"),
            "reasoning": runtime.get("reasoning"),
            "reasoning_effort": runtime.get("reasoning_effort"),
            "providers": config.get("providers"),
            "execution_policy_hash": config.get("execution_policy_hash"),
        },
        "code_snapshot_id": code_snapshot_id,
        "code_revision": runtime.get("code_revision"),
    }


def _debug_comparison_preflight(run_dirs: list[str]) -> dict:
    """Fail closed unless debug runs are comparable on every hard control."""
    profiles = [_debug_comparison_profile(run_dir) for run_dir in run_dirs or []]
    if not profiles:
        raise ValueError("debug comparison requires at least one run")
    baseline = profiles[0]
    for profile in profiles[1:]:
        if profile["hard_controls"] != baseline["hard_controls"]:
            raise ValueError("debug comparison hard-control mismatch")
    differing_axes = [
        name for name in ("model_backend_reasoning", "code_snapshot_id")
        if any(profile[name] != baseline[name] for profile in profiles[1:])
    ]
    if len(differing_axes) > 1:
        raise ValueError("debug comparison changes both model/backend/reasoning and code snapshot")
    return {
        "accepted": True,
        "differing_treatment_axes": differing_axes,
        "canonical_profile": {
            "hard_controls": baseline["hard_controls"],
            "model_backend_reasoning": baseline["model_backend_reasoning"],
            "code_snapshot_id": baseline["code_snapshot_id"],
        },
        "runs": [{
            "run_id": profile["run_id"],
            "debug_labels": profile["debug_labels"],
            "model_backend_reasoning": profile["model_backend_reasoning"],
            "code_snapshot_id": profile["code_snapshot_id"],
            "code_revision": profile["code_revision"],
        } for profile in profiles],
    }


# --------------------------------------------------------------------------- #
#  Run → model map, accepted projection                                        #
# --------------------------------------------------------------------------- #

def run_model_map(rows: list[dict]) -> tuple[dict, list]:
    """run_id → model_id (the modal model among that run's non-stub verdicts).
    Returns (map, warnings) — a warning per run that mixed multiple models."""
    by_run = defaultdict(Counter)
    for r in rows:
        if r.get("stub"):
            continue
        mid = r.get("model_id")
        if mid:
            by_run[r.get("run_id")][mid] += 1
    mapping, warnings = {}, []
    for run_id, c in by_run.items():
        mapping[run_id] = c.most_common(1)[0][0]
        if len(c) > 1:
            warnings.append(f"run {run_id} mixes models {dict(c)}; using {mapping[run_id]}")
    return mapping, warnings


def _pair(r):
    return (r.get("claim_id"), r.get("ref_id"), r.get("scope"))


def crediting_by_run(rows: list[dict]) -> dict:
    """(run_id, claim, ref, scope) → crediting typed terminal outcome."""
    outcomes = {}
    for r in rows:
        if not r.get("crediting"):
            continue
        k = (r.get("run_id"),) + _pair(r)
        if k in outcomes:
            raise ValueError("typed terminal projection contains duplicate pair")
        outcomes[k] = r.get("outcome")
    return outcomes


# --------------------------------------------------------------------------- #
#  Intrinsic per-model metrics (terminal-based, no gold)                       #
# --------------------------------------------------------------------------- #

def intrinsic_metrics(rows: list[dict], run_model: dict) -> dict:
    """Per model_id, aggregated over all its runs. Cells are (run, claim, ref, scope)."""
    cells = defaultdict(list)              # (model, run, claim, ref, scope) -> rows
    for r in rows:
        model = r.get("model_id") or run_model.get(r.get("run_id"))
        if not model:
            continue
        cells[(model, r.get("run_id")) + _pair(r)].append(r)

    per_model = defaultdict(lambda: {
        "runs": set(), "cells": 0, "crediting": 0, "noncrediting": 0,
        "outcomes": Counter()})
    for key, group in cells.items():
        model, run_id = key[0], key[1]
        m = per_model[model]
        m["runs"].add(run_id)
        m["cells"] += 1
        crediting = [r for r in group if r.get("crediting")]
        if crediting:
            m["crediting"] += 1
            m["outcomes"][crediting[0].get("outcome")] += 1
        else:
            m["noncrediting"] += 1

    out = {}
    for model, m in per_model.items():
        cells_n = m["cells"] or 1
        out[model] = {
            "runs": len(m["runs"]),
            "cells": m["cells"],
            "crediting_terminal_rate": round(m["crediting"] / cells_n, 3),
            "noncrediting_terminal_rate": round(m["noncrediting"] / cells_n, 3),
            "outcome_distribution": dict(m["outcomes"]),
        }
    return out


# --------------------------------------------------------------------------- #
#  Agreement helpers                                                           #
# --------------------------------------------------------------------------- #

def cohen_kappa(pairs: list[tuple]) -> float | None:
    """Cohen's kappa over a list of (label_a, label_b). None if undefined."""
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    labels = {a for a, _ in pairs} | {b for _, b in pairs}
    pe = sum((sum(1 for a, _ in pairs if a == L) / n)
             * (sum(1 for _, b in pairs if b == L) / n) for L in labels)
    if pe >= 1.0:
        return 1.0
    return round((po - pe) / (1 - pe), 3)


def _agreement_over_raters(rater_labels: dict) -> dict:
    """rater_labels: {rater_id: {pair: outcome}}. Returns unanimity rate, mean pairwise
    percent agreement and mean pairwise Cohen kappa over pairs COMMON to >=2 raters."""
    raters = list(rater_labels)
    # Per-pair labels present across raters.
    pair_labels = defaultdict(dict)
    for rid in raters:
        for pair, outcome in rater_labels[rid].items():
            pair_labels[pair][rid] = outcome
    common = {p: d for p, d in pair_labels.items() if len(d) >= 2}
    unanimous = sum(1 for d in common.values() if len(set(d.values())) == 1)
    # Pairwise stats.
    ag0, agn, kappas = 0, 0, []
    for i in range(len(raters)):
        for j in range(i + 1, len(raters)):
            a, b = raters[i], raters[j]
            pts = [(rater_labels[a][p], rater_labels[b][p])
                   for p in pair_labels if p in rater_labels[a] and p in rater_labels[b]]
            if not pts:
                continue
            agn += len(pts)
            ag0 += sum(1 for x, y in pts if x == y)
            k = cohen_kappa(pts)
            if k is not None:
                kappas.append(k)
    return {
        "raters": len(raters),
        "common_pairs": len(common),
        "unanimity_rate": round(unanimous / len(common), 3) if common else None,
        "mean_pairwise_agreement": round(ag0 / agn, 3) if agn else None,
        "mean_pairwise_kappa": round(sum(kappas) / len(kappas), 3) if kappas else None,
    }


def within_model_concordance(accepted: dict, run_model: dict) -> dict:
    """Per model with >=2 runs: agreement across its OWN runs (stability)."""
    by_model = defaultdict(dict)   # model -> {run_id: {pair: outcome}}
    for (run_id, *pair_parts), outcome in accepted.items():
        model = run_model.get(run_id, run_id)
        by_model[model].setdefault(run_id, {})[tuple(pair_parts)] = outcome
    out = {}
    for model, runs in by_model.items():
        if len(runs) >= 2:
            out[model] = _agreement_over_raters(runs)
    return out


def between_model_concordance(accepted: dict, run_model: dict) -> tuple[dict, list]:
    """Agreement BETWEEN models. Each model's per-pair outcome is the majority across
    its runs. Returns (summary, discordant_pairs)."""
    votes = defaultdict(lambda: defaultdict(list))   # model -> pair -> [outcomes]
    for (run_id, *pair_parts), outcome in accepted.items():
        model = run_model.get(run_id, run_id)
        votes[model][tuple(pair_parts)].append(outcome)
    model_labels = {}                                # model -> {pair: representative outcome}
    for model, pairs in votes.items():
        model_labels[model] = {p: Counter(o).most_common(1)[0][0] for p, o in pairs.items()}
    summary = _agreement_over_raters(model_labels)

    # Pairs where >=2 models gave an outcome and they are not unanimous.
    pair_models = defaultdict(dict)
    for model, labels in model_labels.items():
        for pair, outcome in labels.items():
            pair_models[pair][model] = outcome
    discordant = []
    for pair, dm in pair_models.items():
        if len(dm) >= 2 and len(set(dm.values())) > 1:
            discordant.append({"claim_id": pair[0], "ref_id": pair[1],
                               "scope": pair[2], "by_model": dm})
    discordant.sort(key=lambda d: (d["claim_id"] or "", d["ref_id"] or ""))
    return summary, discordant


# --------------------------------------------------------------------------- #
#  Outcome matrix                                                              #
# --------------------------------------------------------------------------- #

def outcome_matrix(accepted: dict, run_model: dict) -> dict:
    """Full (claim, ref) × run outcome table — every pair that appeared in any run.
    Scope is dropped here: pairs are keyed by (claim_id, ref_id) and the outcome is
    the one from the highest-scope accepted verdict for that run (fulltext > rag >
    abstract > preview_snippet > web_secondhand), falling back to any accepted row."""
    SCOPE_RANK = {
        "fulltext_complete": 0, "fulltext_trimmed": 1, "rag": 2,
        "abstract_only": 3, "preview_snippet": 4, "web_secondhand": 5,
    }
    # (run_id, claim_id, ref_id) -> (scope_rank, outcome)
    best: dict[tuple, tuple] = {}
    for (run_id, claim_id, ref_id, scope), outcome in accepted.items():
        key = (run_id, claim_id, ref_id)
        rank = SCOPE_RANK.get(scope or "", 9)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, outcome)

    # Sorted unique (claim_id, ref_id) pairs and run_ids
    all_pairs = sorted({(c, r) for (_, c, r) in best})
    run_ids = sorted({k[0] for k in best})

    rows_out = []
    for claim_id, ref_id in all_pairs:
        row = {"claim_id": claim_id, "ref_id": ref_id, "by_run": {}}
        for run_id in run_ids:
            k = (run_id, claim_id, ref_id)
            row["by_run"][run_id] = best[k][1] if k in best else None
        rows_out.append(row)

    columns = [{"run_id": r, "model_id": run_model.get(r, r)} for r in run_ids]
    return {"columns": columns, "rows": rows_out}


# --------------------------------------------------------------------------- #
#  Assemble + render                                                           #
# --------------------------------------------------------------------------- #

def build(rows: list[dict], *, debug_comparison: dict | None = None) -> dict:
    run_model, warnings = run_model_map(rows)
    crediting = crediting_by_run(rows)
    within = within_model_concordance(crediting, run_model)
    between, discordant = between_model_concordance(crediting, run_model)
    data = {
        "inputs": {
            "verdict_rows": len(rows),
            "runs": sorted({r.get("run_id") for r in rows if r.get("run_id")}),
            "models": sorted(set(run_model.values())),
            "run_to_model": run_model,
            "warnings": warnings,
        },
        "intrinsic_per_model": intrinsic_metrics(rows, run_model),
        "within_model_concordance": within,
        "between_model_concordance": between,
        "discordant_pairs": discordant,
        "outcome_matrix": outcome_matrix(crediting, run_model),
    }
    if debug_comparison is not None:
        data["inputs"]["debug_comparison"] = debug_comparison
    return data


def _fmt(v):
    return "—" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))


def render_md(data: dict) -> str:
    L = ["# Benchmark — cross-LLM / cross-run concordance\n"]
    inp = data["inputs"]
    L.append(f"- verdict rows: **{inp['verdict_rows']}** · runs: **{len(inp['runs'])}** "
             f"· models: **{len(inp['models'])}**")
    for run_id, model in inp["run_to_model"].items():
        L.append(f"    - run `{run_id}` → model `{model}`")
    for w in inp["warnings"]:
        L.append(f"    - ⚠️ {w}")
    comparison = inp.get("debug_comparison")
    if comparison:
        axes = comparison["differing_treatment_axes"] or ["none"]
        L.append("    - debug comparison: **accepted** · differing treatment axis: "
                 + ", ".join(f"`{axis}`" for axis in axes))
    L.append("\n> Typed-terminal metrics measure **completion + concordance**, not "
             "correctness vs ground truth (no gold set).\n")

    L.append("## 1. Intrinsic metrics per model")
    L.append("| model | runs | cells | crediting terminal | non-crediting terminal |")
    L.append("|---|---|---|---|---|")
    for model, m in sorted(data["intrinsic_per_model"].items()):
        L.append(f"| `{model}` | {m['runs']} | {m['cells']} | "
                 f"{_fmt(m['crediting_terminal_rate'])} | "
                 f"{_fmt(m['noncrediting_terminal_rate'])} |")
    L.append("")
    for model, m in sorted(data["intrinsic_per_model"].items()):
        if m["outcome_distribution"]:
            dist = ", ".join(f"{k}={v}" for k, v in sorted(m["outcome_distribution"].items()))
            L.append(f"- `{model}` crediting-outcome mix: {dist} "
                     "_(mix ≠ quality: a finding, not a score)_")

    L.append("\n## 2. Within-model concordance (run-to-run stability)")
    if data["within_model_concordance"]:
        L.append("| model | runs | common pairs | unanimity | mean agreement | mean κ |")
        L.append("|---|---|---|---|---|---|")
        for model, a in sorted(data["within_model_concordance"].items()):
            L.append(f"| `{model}` | {a['raters']} | {a['common_pairs']} | "
                     f"{_fmt(a['unanimity_rate'])} | {_fmt(a['mean_pairwise_agreement'])} | "
                     f"{_fmt(a['mean_pairwise_kappa'])} |")
    else:
        L.append("_No model has ≥2 runs — provide repeated runs per model to measure stability._")

    L.append("\n## 3. Between-model concordance")
    b = data["between_model_concordance"]
    L.append(f"- models compared: **{b['raters']}** · common pairs: **{b['common_pairs']}** "
             f"· unanimity: **{_fmt(b['unanimity_rate'])}** · mean agreement: "
             f"**{_fmt(b['mean_pairwise_agreement'])}** · mean κ: **{_fmt(b['mean_pairwise_kappa'])}**")

    L.append("\n## 4. Discordant pairs (where models/runs disagree — review these first)")
    if data["discordant_pairs"]:
        for d in data["discordant_pairs"]:
            by = ", ".join(f"`{mdl}`={o}" for mdl, o in sorted(d["by_model"].items()))
            L.append(f"- claim `{d['claim_id']}` · ref `{d['ref_id']}` · scope "
                     f"`{d['scope']}` → {by}")
    else:
        L.append("_No between-model disagreement on common pairs._")

    _EMOJI = {"supports": "✅", "partial": "⚠️", "related": "🔗",
              "off_topic": "➖", "contradicts": "🚫"}
    matrix = data.get("outcome_matrix", {})
    cols = matrix.get("columns", [])
    mrows = matrix.get("rows", [])
    if cols and mrows:
        L.append("\n## 5. Outcome matrix (all pairs × all runs)")
        L.append("> ✅ supports · ⚠️ partial · 🔗 related · ➖ off_topic · 🚫 contradicts · — missing\n")
        headers = ["claim", "ref"] + [f"`{c['run_id']}`<br>_{c['model_id']}_" for c in cols]
        L.append("| " + " | ".join(headers) + " |")
        L.append("|" + "|".join(["---"] * len(headers)) + "|")
        for row in mrows:
            cells = [row["claim_id"] or "—", row["ref_id"] or "—"]
            for c in cols:
                o = row["by_run"].get(c["run_id"])
                cells.append(_EMOJI.get(o, "—") if o else "—")
            L.append("| " + " | ".join(cells) + " |")

    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #

def _write_benchmark(data: dict, prefix: str) -> str:
    """Write <prefix>.md; return its path."""
    os.makedirs(os.path.dirname(prefix) or ".", exist_ok=True)
    md_path = prefix + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_md(data))
    return md_path


def main():
    ap = argparse.ArgumentParser(description=format_run_examples(__doc__),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="append", default=[],
                    help="DB-backed run directory; repeatable")
    ap.add_argument("--out-dir", default="benchmarks",
                    help="folder where dated benchmark files are written (default: benchmarks/)")
    ap.add_argument("--out", default=None,
                    help="explicit output prefix (overrides --out-dir); writes <out>.md")
    ap.add_argument("--debug-compare", action="store_true",
                    help="strictly compare persisted debug runs before loading verdict rows")
    args = ap.parse_args()

    try:
        debug_comparison = (
            _debug_comparison_preflight(args.run) if args.debug_compare else None
        )
    except ValueError as exc:
        ap.error(str(exc))
    rows = load_rows(args.run)
    if not rows:
        raise SystemExit("no verdict rows found in the given runs")
    data = build(rows, debug_comparison=debug_comparison)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if args.out:
        prefix = args.out
    else:
        prefix = os.path.join(args.out_dir, ts)

    md_path = _write_benchmark(data, prefix)

    print(json.dumps({"out_md": md_path,
                      "runs": len(data["inputs"]["runs"]),
                      "models": len(data["inputs"]["models"]),
                      "discordant_pairs": len(data["discordant_pairs"])},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
