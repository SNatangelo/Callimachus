#!/usr/bin/env python3
# core/resolve/providers/clinical_trials/isrctn.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""ISRCTN registry sub-provider via the public query API (returns XML)."""

from __future__ import annotations

import html
import re

REGISTRY = "ISRCTN"
_ID_RE = re.compile(r"\bISRCTN\d{8}\b", re.IGNORECASE)
_API = "https://www.isrctn.com/api/query/format/default?q={}"


def find_ids(text: str) -> list[str]:
    seen: list[str] = []
    for match in _ID_RE.findall(text or ""):
        norm = match.upper()
        if norm not in seen:
            seen.append(norm)
    return seen


def _tag(body: str, name: str) -> str | None:
    match = re.search(rf"<{name}>(.*?)</{name}>", body, re.S)
    if not match:
        return None
    text = re.sub(r"<[^>]+>", " ", html.unescape(match.group(1)))
    return re.sub(r"\s+", " ", text).strip() or None


def fetch(trial_id: str) -> dict | None:
    from core.resolve import service as resolve_mod

    status, body = resolve_mod._get(_API.format(trial_id), accept="application/xml")
    if status != 200:
        return None
    # Identity guard: the canonical id must be the one we asked for.
    if f'publicIdentifierCanonical="{trial_id}"' not in body:
        return None
    title = _tag(body, "title") or _tag(body, "scientificTitle")
    if not title:
        return None
    return {
        "trial_id": trial_id,
        "registry": REGISTRY,
        "title": title,
        "status": _tag(body, "trialStatus"),
        "summary": _tag(body, "plainEnglishSummary") or "",
        "url": f"https://www.isrctn.com/{trial_id}",
    }
