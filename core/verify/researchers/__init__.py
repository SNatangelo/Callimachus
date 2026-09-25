#!/usr/bin/env python3
# core/verify/researchers/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Legacy third-party web researchers, disconnected from citation Verify.

A *researcher* turns a cited reference (plus the claims that cite it and any untrusted
hints the agent's web tools returned) into a legacy `web_secondhand` document
``{text, urls, origin}``, a retryable ``unavailable`` outcome, or ``None`` when it
has nothing to offer.

This mirrors ``core/fetch/fallbacks/fetch_modes``: drop a module in this package exposing

    NAME: str                       # stable identifier
    PRIORITY: int                   # lower runs first (default 100)
    def available(st) -> bool       # can it run in this environment? (default: yes)
    def produce(st, ref, claims, answer) -> dict | None

and it is discovered automatically and slotted into the fallback chain. ``produce`` runs
the providers in priority order and returns the first real document, so a high-priority
researcher (e.g. validating the agent's findings) is tried before the deterministic web
search that backs everything up. The retrieval is always done by the providers themselves
and validated against fetched text. The current pipeline does not call this registry
or admit its output as citation evidence.
"""
from __future__ import annotations

import importlib
import pkgutil


def discover_module_names() -> list[str]:
    return sorted(
        name
        for _finder, name, _ispkg in pkgutil.iter_modules(__path__)
        if not name.startswith("_")
    )


def load_module(module_name: str):
    try:
        return importlib.import_module(f"{__name__}.{module_name}")
    except Exception:
        return None


def providers() -> list:
    """All loadable researcher modules, ordered by PRIORITY (lower first)."""
    mods = []
    for name in discover_module_names():
        mod = load_module(name)
        if mod is not None and hasattr(mod, "produce"):
            mods.append(mod)
    return sorted(mods, key=lambda m: getattr(m, "PRIORITY", 100))


def provider_names() -> list[str]:
    return [getattr(m, "NAME", m.__name__.rsplit(".", 1)[-1]) for m in providers()]


def produce(st, ref, claims, answer) -> dict | None:
    """Try each available researcher in priority order; the first non-empty document wins.

    `answer` is the untrusted web-research task answer supplied to the phase.
    Providers re-fetch and validate independently, so a fabricated hint cannot survive.
    If no provider returns text, preserve a retryable ``unavailable`` outcome so the
    phase can pause without treating an outage as evidential absence."""
    unavailable = None
    for mod in providers():
        try:
            if hasattr(mod, "available") and not mod.available(st):
                continue
            doc = mod.produce(st, ref, claims, answer)
        except Exception:
            doc = None  # a researcher is best-effort; never break the run
        if doc and doc.get("text"):
            return doc
        if doc and doc.get("status") == "unavailable" and doc.get("retryable"):
            unavailable = {"status": "unavailable", "retryable": True}
    return unavailable
