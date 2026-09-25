#!/usr/bin/env python3
# core/resolve/providers/clinical_trials/ctis.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""EU clinical trials sub-provider (CTIS) — CTIS ct-numbers and EudraCT numbers.

CTIS is the EU's current public system; its REST API exposes a per-trial detail
document. A citation may give a CTIS ct-number (YYYY-NNNNNN-NN-NN) or a legacy
EudraCT number (YYYY-NNNNNN-NN); the latter is resolved by a CTIS search with a
strict identity guard (the number must appear in the retrieved record).
"""

from __future__ import annotations

import json
import re

REGISTRY = "EU CTIS"
_CTNUMBER_RE = re.compile(r"\b\d{4}-\d{6}-\d{2}-\d{2}\b")
_EUDRACT_RE = re.compile(r"\b\d{4}-\d{6}-\d{2}\b")
_RETRIEVE = "https://euclinicaltrials.eu/ctis-public-api/retrieve/{}"
_SEARCH = "https://euclinicaltrials.eu/ctis-public-api/search"


def find_ids(text: str) -> list[str]:
    text = text or ""
    ids: list[str] = []
    for match in _CTNUMBER_RE.findall(text):
        if match not in ids:
            ids.append(match)
    for match in _EUDRACT_RE.findall(text):
        # Skip an EudraCT pattern that is only the prefix of a CTIS number above.
        if any(ct.startswith(match) for ct in ids) or match in ids:
            continue
        ids.append(match)
    return ids


def _dig(node, *path):
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _record(detail: dict) -> dict | None:
    ident = _dig(detail, "authorizedApplication", "authorizedPartI",
                 "trialDetails", "clinicalTrialIdentifiers") or {}
    title = ident.get("publicTitle") or ident.get("fullTitle")
    if not title:
        return None
    summary = _dig(detail, "authorizedApplication", "authorizedPartI",
                   "trialDetails", "trialInformation", "trialObjective", "mainObjective") or ""
    ct = detail.get("ctNumber")
    return {
        "trial_id": ct,
        "registry": REGISTRY,
        "title": title,
        "status": detail.get("ctStatus"),
        "summary": summary,
        "url": f"https://euclinicaltrials.eu/ctis-public/#/view/{ct}",
    }


def fetch(trial_id: str) -> dict | None:
    from core.resolve import service as resolve_mod

    if _CTNUMBER_RE.fullmatch(trial_id):
        status, body = resolve_mod._get(_RETRIEVE.format(trial_id), accept="application/json")
        if status != 200:
            return None
        detail = json.loads(body) or {}
        if detail.get("ctNumber") != trial_id:      # identity guard
            return None
        return _record(detail)

    # Legacy EudraCT number: search CTIS, accept only a record that actually
    # carries the cited number (strict identity).
    status, body = resolve_mod._http._post_json(
        _SEARCH,
        {"pagination": {"page": 1, "size": 5}, "searchCriteria": {"containAll": trial_id}},
        accept="application/json",
    )
    if status != 200:
        return None
    for row in (json.loads(body) or {}).get("data") or []:
        ct = row.get("ctNumber")
        if not ct:
            continue
        detail_status, detail_body = resolve_mod._get(_RETRIEVE.format(ct), accept="application/json")
        if detail_status == 200 and trial_id in detail_body:
            return _record(json.loads(detail_body) or {})
    return None
