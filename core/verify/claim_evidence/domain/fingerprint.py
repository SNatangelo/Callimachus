# core/verify/claim_evidence/domain/fingerprint.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical, deterministic hashes for immutable verification records."""
from __future__ import annotations

import hashlib
from typing import Any

from core.shared.typed_canonical import TypedCanonicalError, encode as typed_canonical


class FingerprintError(ValueError):
    """A value cannot be represented by the frozen fingerprint format."""


FINGERPRINT_VERSION_V2 = "claim-evidence-fingerprint-v2"
# Current immutable codec marker, retained under the generic name for callers.
FINGERPRINT_VERSION = FINGERPRINT_VERSION_V2


def fingerprint(value: Any, *, version: str = FINGERPRINT_VERSION) -> str:
    """Return a generic SHA-256 digest using the selected canonical version."""
    return _named("generic-v1", value, version=version)


def pair_fingerprint(
    *, claim_id: str, ref_id: str, scope: str, version: str = FINGERPRINT_VERSION
) -> str:
    """Bind one run-local claim/reference/scope identity."""
    return _named(
        "pair-v1",
        {"claim_id": claim_id, "ref_id": ref_id, "scope": scope}, version=version,
    )


def payload_fingerprint(value: Any, *, version: str = FINGERPRINT_VERSION) -> str:
    return _named("payload-v1", value, version=version)


def provider_prompt_fingerprint(
    system_prompt: str, user_payload: bytes,
) -> str:
    """Bind the exact UTF-8 provider-visible prompt with unambiguous framing."""
    if not isinstance(system_prompt, str) or not isinstance(user_payload, bytes):
        raise FingerprintError("provider prompt material is invalid")
    system = system_prompt.encode("utf-8")
    return hashlib.sha256(
        b"claim-evidence-provider-prompt-v1\x00"
        + len(system).to_bytes(8, "big") + system
        + len(user_payload).to_bytes(8, "big") + user_payload
    ).hexdigest()


def policy_fingerprint(value: Any, *, version: str = FINGERPRINT_VERSION) -> str:
    return _named("policy-v1", value, version=version)


def candidate_fingerprint(
    *,
    outcome: str | None,
    evidence: tuple[str, ...],
    outcome_fields: dict[str, Any],
    grounded: tuple[dict[str, Any], ...],
    version: str = FINGERPRINT_VERSION,
) -> str:
    """Hash candidate semantics and grounded material, excluding explanation."""
    return _named("candidate-v1", {
        "outcome": outcome,
        "evidence": evidence,
        "outcome_fields": outcome_fields,
        "grounded": grounded,
    }, version=version)


def answer_fingerprint(value: Any, *, version: str = FINGERPRINT_VERSION) -> str:
    """Hash a durable jury answer with the current immutable codec version."""
    return _named("answer-v2", value, version=version)


def _named(namespace: str, value: Any, *, version: str) -> str:
    if version == FINGERPRINT_VERSION_V2:
        try:
            material = typed_canonical((FINGERPRINT_VERSION_V2, namespace, value))
        except TypedCanonicalError as exc:
            raise FingerprintError("value is not typed canonical") from exc
    else:
        raise FingerprintError(f"unknown fingerprint version: {version}")
    return hashlib.sha256(material).hexdigest()
