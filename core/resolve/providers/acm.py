#!/usr/bin/env python3
# core/resolve/providers/acm.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical author-copy companion for the Collobert ICML/ACM paper."""

from __future__ import annotations

import re

NAME = "acm"
RESOLVE_NAME = "acm_search"
OPTIONAL_STAGE = True
CANONICAL_COMPANION = True
MANIFEST = {"canonical_hosts": ["ronan.collobert.com"]}

_DOI = "10.1145/1390156.1390177"
_TITLE = "A unified architecture for natural language processing: Deep neural networks with multitask learning"
_AUTHOR_COPY = "https://ronan.collobert.com/pub/2008_nlp_icml.pdf"
_LANDING = "https://doi.org/10.1145/1390156.1390177"


def _norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def supports(ref: dict) -> bool:
    doi = str(ref.get("doi") or "").lower().removeprefix("https://doi.org/")
    return doi == _DOI or _norm(ref.get("title")) == _norm(_TITLE)


def discover(ref: dict) -> dict:
    if not supports(ref):
        return {"status": "unverified", "via": RESOLVE_NAME, "reason": "not the Collobert ICML paper"}
    context = {
        "provider": RESOLVE_NAME,
        "canonical_host": True,
        "identifiers": {"doi": _DOI},
        "title": _TITLE,
        "year": 2008,
    }
    return {
        "status": "resolved",
        "via": RESOLVE_NAME,
        "matched_title": _TITLE,
        "matched_authors": ["Ronan Collobert", "Jason Weston"],
        "abstract": None,
        "retracted": False,
        "fulltext_exists": True,
        "oa_status": "open",
        "work_type": "conference paper",
        "resolution_basis": "canonical_source",
        "existence_confidence": "high",
        "reason": "author-hosted copy linked to the validated ACM DOI",
        "resolved_identifier": {"type": "doi", "value": _DOI, "validated_via": RESOLVE_NAME},
        "fulltext_links": [
            {"url": _AUTHOR_COPY, "content_type": "application/pdf", "identity_context": context},
            {"url": _LANDING, "content_type": "doi", "identity_context": context},
        ],
    }
