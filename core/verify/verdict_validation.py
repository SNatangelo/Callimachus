# core/verify/verdict_validation.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Validation helpers for model-provided verdict answers."""

from __future__ import annotations

from typing import Any


RETRY_SCHEMA_INVALID = "schema_invalid"
RETRY_MISSING_NOTE = "missing_note"
RETRY_QUOTE_UNMATCHED = "quote_unmatched"
RETRY_SOURCE_SPAN_INVALID = "source_span_invalid"
RETRY_SEMANTIC_DISAGREEMENT = "semantic_disagreement"
RETRY_CITATION_ROLE_UNCERTAIN = "citation_role_uncertain"
RETRY_TRANSPORT_ERROR = "transport_error"
RETRY_PARTIAL_MISSING_SUPPORTED_PART = "partial_missing_supported_part"
RETRY_EMPTY_OR_INVALID_REPLY = "empty_or_invalid_reply"


_REQUIRED_NOTE_REASON = (
    "missing note: every verdict must include a brief explanation of why "
    "that outcome was chosen"
)


def note_validation_reason(answer: dict[str, Any] | None) -> str | None:
    """Return a rejection reason when a verdict answer has no usable note."""
    if not isinstance(answer, dict):
        return _REQUIRED_NOTE_REASON
    note = answer.get("note")
    if note is None:
        return _REQUIRED_NOTE_REASON
    if not str(note).strip():
        return _REQUIRED_NOTE_REASON
    return None


def retry_cause(reason: str | None = None, guard_code: str | None = None) -> str:
    """Classify a rejected attempt without asking an LLM.

    Retry policy must be keyed by *why* an attempt failed.  In particular a
    quote alignment issue must not be rewritten into a semantic ``off_topic``
    instruction, and transport failures never become a semantic verdict.
    """
    text = f"{guard_code or ''} {reason or ''}".lower()
    if "citation_role" in text or "role_uncertain" in text:
        return RETRY_CITATION_ROLE_UNCERTAIN
    if "missing_note" in text or "missing note" in text:
        return RETRY_MISSING_NOTE
    if "json" in text or "schema" in text or "invalid outcome" in text:
        return RETRY_SCHEMA_INVALID
    if "source_span" in text or "span id" in text or "span catalogue" in text:
        return RETRY_SOURCE_SPAN_INVALID
    if "passage" in text or "quote" in text or "verbatim" in text:
        return RETRY_QUOTE_UNMATCHED
    if "timeout" in text or "transport" in text or "rate limit" in text:
        return RETRY_TRANSPORT_ERROR
    return RETRY_SEMANTIC_DISAGREEMENT
