#!/usr/bin/env python3
# core/verify/researchers/agent_findings.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Legacy validator for agent web-research hints; not citation evidence.

The agent returns UNTRUSTED hints ``{url, stance, quote}``. We re-fetch every
URL ourselves and keep only findings whose quote is mechanically present on
the page, so no fabricated page or quote can survive. The stored document is
built from real page text. The current pipeline does not invoke this helper.
"""
from __future__ import annotations

try:
    from core.verify.claim_evidence.evidence.grounding import normalize_for_matching
    from core.verify import websearch as _websearch
except ImportError:  # direct execution
    from claim_evidence.evidence.grounding import normalize_for_matching
    import websearch as _websearch

NAME = "agent_findings"
PRIORITY = 10  # try the agent's (validated) findings before any deterministic search


def available(st) -> bool:
    return True


def produce(st, ref, claims, answer) -> dict | None:
    answer = answer or {}
    if not answer.get("found"):
        return None
    blocks, urls = [], []
    for f in answer.get("findings") or []:
        url = (f.get("url") or "").strip()
        quote = (f.get("quote") or "").strip()
        if not url or not quote:
            continue
        try:
            page = _websearch._page_text(url, mailto=st.get("mailto"))
        except Exception:
            page = ""
        if not page:
            continue  # page unreachable / does not exist -> drop (existence check)
        if normalize_for_matching(quote) not in normalize_for_matching(page):
            continue  # quote not substantiated by the page -> drop (fabrication guard)
        stance = f.get("stance") if f.get("stance") in ("supports", "contradicts") else "unclear"
        excerpt = page[:_websearch._MAX_PAGE_CHARS]
        blocks.append(f"SOURCE (third-party page, stance={stance})\nURL: {url}\n{excerpt}")
        urls.append(url)
    if not blocks:
        return None
    text = "\n\n---\n\n".join(blocks)[:_websearch._MAX_DOC_CHARS]
    return {"text": text, "urls": urls, "origin": "web_research"}
