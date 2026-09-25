#!/usr/bin/env python3
# core/resolve/providers/clinical_trials/ctgov.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""ClinicalTrials.gov sub-provider (NCT ids) via the v2 REST API."""

from __future__ import annotations

import json
import re

REGISTRY = "ClinicalTrials.gov"
_ID_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)
_API = "https://clinicaltrials.gov/api/v2/studies/{}?fields=protocolSection"


def find_ids(text: str) -> list[str]:
    seen: list[str] = []
    for match in _ID_RE.findall(text or ""):
        norm = match.upper()
        if norm not in seen:
            seen.append(norm)
    return seen


def fetch(trial_id: str) -> dict | None:
    from core.resolve import service as resolve_mod

    status, body = resolve_mod._get(_API.format(trial_id), accept="application/json")
    if status != 200:
        return None
    section = (json.loads(body) or {}).get("protocolSection") or {}
    ident = section.get("identificationModule") or {}
    desc = section.get("descriptionModule") or {}
    title = ident.get("officialTitle") or ident.get("briefTitle")
    if not title:
        return None
    summary = " ".join(
        part for part in (desc.get("briefSummary"), desc.get("detailedDescription")) if part
    ).strip()
    nct = ident.get("nctId") or trial_id.upper()
    return {
        "trial_id": nct,
        "registry": REGISTRY,
        "title": title,
        "status": (section.get("statusModule") or {}).get("overallStatus"),
        "summary": summary,
        "url": f"https://clinicaltrials.gov/study/{nct}",
    }
