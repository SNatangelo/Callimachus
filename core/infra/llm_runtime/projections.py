# core/infra/llm_runtime/projections.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic typed runtime projection identifiers."""
from __future__ import annotations
import hashlib
from typing import Any
from core.shared.typed_canonical import encode

def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty trimmed text")

def runtime_projection_id(source_run_id: str, source_event_id: str | None = None, *, kind: str = "model_observation") -> str:
    """Hash a typed, queryable source identity; v2 never serializes it as JSON."""
    _text("source_run_id", source_run_id)
    if source_event_id is None:
        raise ValueError("source_event_id is required")
    _text("source_event_id", source_event_id); _text("kind", kind)
    return hashlib.sha256(encode({"kind":kind,"source_event_id":source_event_id,"source_run_id":source_run_id})).hexdigest()

def runtime_payload_id(payload: Any) -> str:
    return hashlib.sha256(encode(payload)).hexdigest()
