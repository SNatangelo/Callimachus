#!/usr/bin/env python3
# core/resolve/providers/princeton_bialek.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical author copy for Ruderman and Bialek's 1994 PRL article."""

from __future__ import annotations

import re

NAME = "princeton_bialek"
RESOLVE_NAME = "princeton_bialek_search"
OPTIONAL_STAGE = True
CANONICAL_COMPANION = True
MANIFEST = {"canonical_hosts": ["swh.princeton.edu"]}

DOI = "10.1103/physrevlett.73.814"
TITLE = "Statistics of natural images: Scaling in the woods"
AUTHORS = ["Daniel L. Ruderman", "William Bialek"]
YEAR = 1994
PDF_URL = "https://swh.princeton.edu/~wbialek/rome/refs/ruderman%2Bbialek_94.pdf"


def _norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def supports(ref: dict) -> bool:
    doi = str(ref.get("doi") or "").lower().removeprefix("https://doi.org/")
    try:
        year = int(ref.get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    return doi == DOI or (_norm(ref.get("title")) == _norm(TITLE) and year in (0, YEAR))


def _context() -> dict:
    return {
        "provider": RESOLVE_NAME,
        "canonical_host": True,
        "identifiers": {"doi": DOI},
        "title": TITLE,
        "authors": AUTHORS,
        "year": YEAR,
    }


def discover(ref: dict) -> dict:
    if not supports(ref):
        return {"status": "unverified", "via": RESOLVE_NAME, "reason": "not the supported Ruderman-Bialek article"}
    return {
        "status": "resolved",
        "via": RESOLVE_NAME,
        "matched_title": TITLE,
        "matched_authors": AUTHORS,
        "abstract": None,
        "retracted": False,
        "fulltext_exists": True,
        "oa_status": "open",
        "work_type": "journal article",
        "resolution_basis": "canonical_source",
        "existence_confidence": "high",
        "reason": "author-hosted copy matched to the exact cited PRL DOI",
        "resolved_identifier": {"type": "doi", "value": DOI, "validated_via": RESOLVE_NAME},
        "fulltext_links": [
            {"url": PDF_URL, "content_type": "application/pdf", "identity_context": _context()},
            {"url": f"https://doi.org/{DOI}", "content_type": "doi", "identity_context": _context()},
        ],
    }


def candidate_items(ref: dict, **kwargs) -> list[dict]:
    if not supports(ref):
        return []
    return [{
        "method": NAME,
        "url": PDF_URL,
        "kind": "pdf",
        "content_type": "application/pdf",
        "identity_context": _context(),
    }]
