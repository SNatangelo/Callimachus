#!/usr/bin/env python3
# core/infra/perf.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
perf.py — opt-in, thread-safe wall-clock timing collector.

Disabled by default (near-zero-cost no-op): every hot-path call site wraps its
work in ``span(phase, key)`` (or calls ``record`` directly), and when the
feature is off those calls do the minimum possible work — a single env lookup
and an immediate ``yield``/``return``, no lock, no dict mutation.

Enable with:  CITATION_VERIFIER_PERF=1   (also: true/yes/on, case-insensitive)

When enabled, a module-level registry accumulates per-(phase, key) counters
(count / total_s / max_s) behind a lock, so it is safe to call ``record``/
``span`` concurrently from multiple threads (e.g. parallel fetch/verify
workers). ``persist_summary`` writes a closed relational snapshot into the run
database at the end of a run, and ``format_table`` renders a compact table.

Stdlib only on hot paths: time, threading, os, contextlib.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time

ENV_PERF = "CITATION_VERIFIER_PERF"
_TRUTHY = {"1", "true", "yes", "on"}

_lock = threading.Lock()
_registry: dict[tuple[str, str | None], dict[str, float]] = {}


def is_enabled() -> bool:
    """Cheap per-call env check — no caching needed, no lock, no I/O."""
    return str(os.environ.get(ENV_PERF) or "").strip().lower() in _TRUTHY


def reset() -> None:
    """Clear the registry. Used by tests to isolate runs."""
    with _lock:
        _registry.clear()


def record(phase: str, key: str | None, seconds: float) -> None:
    """Accumulate one timing sample. No-op (no lock, no dict work) when disabled."""
    if not is_enabled():
        return
    slot = (phase, key)
    with _lock:
        entry = _registry.get(slot)
        if entry is None:
            _registry[slot] = {"count": 1, "total_s": seconds, "max_s": seconds}
        else:
            entry["count"] += 1
            entry["total_s"] += seconds
            if seconds > entry["max_s"]:
                entry["max_s"] = seconds


@contextlib.contextmanager
def span(phase: str, key: str | None = None):
    """Time a block of code and record it under (phase, key).

    When disabled this is a bare ``yield`` — no ``perf_counter`` call, no
    try/finally overhead beyond what the generator protocol itself costs.
    Exception-safe: recording happens in a ``finally`` so a raised exception
    still contributes its elapsed time before propagating.
    """
    if not is_enabled():
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        record(phase, key, time.perf_counter() - start)


def summary() -> dict:
    """JSON-serializable snapshot, spans sorted by total_s descending."""
    with _lock:
        items = [
            {
                "phase": phase,
                "key": key,
                "count": entry["count"],
                "total_s": round(entry["total_s"], 4),
                "max_s": round(entry["max_s"], 4),
            }
            for (phase, key), entry in _registry.items()
        ]
    items.sort(key=lambda row: row["total_s"], reverse=True)
    return {"spans": items}


def persist_summary(run_dir: str, session_id: str) -> bool:
    """Persist this driver session's immutable relational snapshot."""
    if not is_enabled():
        return False
    from core.infra.db import RunRepository

    repository = RunRepository.open(run_dir)
    try:
        repository.write_performance_spans(summary()["spans"], session_id=session_id)
    finally:
        repository.close()
    return True


def format_table() -> str:
    """Compact human-readable table of the current summary; "" when empty."""
    spans = summary()["spans"]
    if not spans:
        return ""
    header = f"{'phase':<12} {'key':<24} {'count':>7} {'total_s':>10} {'max_s':>10}"
    lines = [header, "-" * len(header)]
    for row in spans:
        key = row["key"] if row["key"] is not None else "-"
        lines.append(
            f"{row['phase']:<12} {str(key):<24} {row['count']:>7} "
            f"{row['total_s']:>10.4f} {row['max_s']:>10.4f}"
        )
    return "\n".join(lines)
