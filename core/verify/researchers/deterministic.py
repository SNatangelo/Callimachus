#!/usr/bin/env python3
# core/verify/researchers/deterministic.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Legacy deterministic third-party web researcher (not Verify evidence).

The model-agnostic safety net: when the agent's web research yields nothing usable (or no
web-tool backend exists), search the open web through ``core.search`` for
third-party pages about the reference. It ignores the agent's hints entirely, so it works
under any underlying LLM. The current pipeline does not invoke this helper.
"""
from __future__ import annotations

try:
    from core.verify import websearch as _websearch
except ImportError:  # direct execution
    import websearch as _websearch

NAME = "deterministic"
PRIORITY = 90  # last resort, after the agent's validated findings


def available(st) -> bool:
    return True


def produce(st, ref, claims, answer) -> dict | None:
    if not (ref.get("title") or ref.get("raw_entry")):
        return None  # nothing identifying to search on
    try:
        kwargs = {"mailto": st.get("mailto")}
        if st.get("run_dir"):
            kwargs["run_dir"] = st["run_dir"]
        res = _websearch.gather(ref, **kwargs)
    except Exception:
        return None  # web search is best-effort; never break the run
    if res.get("status") == "unavailable" and res.get("retryable"):
        return {"status": "unavailable", "retryable": True}
    if res.get("status") != "stored" or not res.get("text"):
        return None
    return {"text": res["text"], "urls": res.get("urls") or [], "origin": "websearch"}
