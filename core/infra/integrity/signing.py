#!/usr/bin/env python3
# core/infra/integrity/signing.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
signing.py — the seal the LLM cannot forge.

The content provenance in report.py (a plain sha256 over parse+ledger) catches a
hand-written or stale report, but an agent that has the repo can recompute it. To make a
report's approval UN-forgeable by the orchestrator, the deterministic system signs it with
an HMAC keyed by a secret the agent must not possess. The secret is read from the
environment of the TRUSTED context (the Stop hook / CI), never exported to the agent's
shell — that isolation is the load-bearing assumption; without it the HMAC degrades to the
public sha256 and verify_run says so.

Key resolution (first hit wins):
  1. file at $CITATION_VERIFIER_SIGNING_KEY_FILE  (preferred: restrict its permissions)
  2. $CITATION_VERIFIER_SIGNING_KEY               (env var; readable by anything in the env)

Algorithms:
  - hmac-sha256  → a key is available; the seal is un-forgeable without it.
  - sha256       → no key; content-binding only (degraded/local mode), clearly labelled.
"""
from __future__ import annotations

import hashlib
import hmac
import os

from core.shared.typed_canonical import encode as typed_canonical

ENV_KEY = "CITATION_VERIFIER_SIGNING_KEY"
ENV_KEY_FILE = "CITATION_VERIFIER_SIGNING_KEY_FILE"


def load_key() -> str | None:
    path = os.environ.get(ENV_KEY_FILE)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            k = f.read().strip()
        return k or None
    k = os.environ.get(ENV_KEY)
    return k.strip() if k and k.strip() else None


def key_present() -> bool:
    return load_key() is not None


def canonical(fields: dict) -> bytes:
    """Return the sole stable, order-independent typed payload."""
    return typed_canonical(fields)


def sign(payload: bytes, key: str | None = None) -> dict:
    """Return {'alg', 'sig'}. HMAC when a key is available, else a plain sha256."""
    key = key if key is not None else load_key()
    if key:
        sig = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return {"alg": "hmac-sha256", "sig": sig}
    return {"alg": "sha256", "sig": hashlib.sha256(payload).hexdigest()}


def verify(payload: bytes, alg: str, sig: str, key: str | None = None) -> bool:
    """Constant-time check that (alg, sig) matches payload. An hmac-sha256 seal cannot
    be verified — nor produced — without the key, which is the whole point."""
    key = key if key is not None else load_key()
    if alg == "hmac-sha256":
        if not key:
            return False
        expected = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig or "")
    if alg == "sha256":
        expected = hashlib.sha256(payload).hexdigest()
        return hmac.compare_digest(expected, sig or "")
    return False
