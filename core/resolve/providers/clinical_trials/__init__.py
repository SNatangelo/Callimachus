#!/usr/bin/env python3
# core/resolve/providers/clinical_trials/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Clinical-trial registry resolver (plug-and-play sub-providers).

A citation to a registered trial names it by a strong registry id (NCT, ISRCTN,
EudraCT/CTIS). The cited page is usually a JavaScript single-page app that serves
an empty shell to non-browser clients, so the trial is resolved from the
registry's structured API instead.

Each registry is a self-contained sub-provider module in this package. Drop a new
`<registry>.py` exposing the contract below and it is auto-registered — no wiring:

    REGISTRY: str                      # human-readable registry name
    def find_ids(text: str) -> list[str]     # this registry's ids found in text
    def fetch(trial_id: str) -> dict | None  # {trial_id, registry, title,
                                             #  status, summary, url} or None

The trial id is a strong identifier lifted from the citation itself, so a fetched
record IS the cited trial; the summary becomes the reference's abstract.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

NAME = "clinical_trials_search"
MANIFEST = {}

_CONTRACT = ("find_ids", "fetch")


def _registries() -> list[ModuleType]:
    mods: list[ModuleType] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        if all(callable(getattr(module, attr, None)) for attr in _CONTRACT):
            mods.append(module)
    mods.sort(key=lambda m: getattr(m, "REGISTRY", m.__name__))
    return mods


def _search_text(ref: dict) -> str:
    return " ".join(str(ref.get(key) or "") for key in ("raw_entry", "url", "title", "doi"))


def supports(ref: dict) -> bool:
    text = _search_text(ref)
    return any(registry.find_ids(text) for registry in _registries())


def _resolved(rec: dict) -> dict:
    summary = (rec.get("summary") or "").strip()
    out = {
        "status": "resolved",
        "via": NAME,
        "matched_title": rec.get("title"),
        "reason": f"{rec['registry']} registry record {rec['trial_id']}",
        "resolution_basis": "identifier",
        "existence_confidence": "high",
        "retracted": False,
        "fulltext_exists": False,
        "oa_status": "open",
        "work_type": "clinical trial",
        "fulltext_links": [],
        "trial_registration": {
            "registry": rec["registry"],
            "id": rec["trial_id"],
            "status": rec.get("status"),
            "url": rec.get("url"),
        },
    }
    if summary:
        out["abstract"] = summary
        out["abstract_via"] = NAME
    return out


def discover(ref: dict) -> dict | None:
    text = _search_text(ref)
    network_error = False
    for registry in _registries():
        for trial_id in registry.find_ids(text):
            try:
                rec = registry.fetch(trial_id)
            except Exception:
                network_error = True
                continue
            if rec and rec.get("title"):
                return _resolved(rec)
    if network_error:
        return {"status": "unresolved", "via": NAME,
                "reason": "trial registry unreachable"}
    return {"status": "unverified", "via": NAME,
            "reason": "trial id not found in any registry"}
