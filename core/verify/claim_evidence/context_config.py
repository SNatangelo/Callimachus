# core/verify/claim_evidence/context_config.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed context-horizon configuration for claim verification."""
from dataclasses import dataclass
from typing import Mapping


class ConfigError(ValueError):
    """Configuration is invalid before any provider dispatch."""


@dataclass(frozen=True, slots=True)
class ContextSettings:
    mode: str
    profile: str
    max_source_chars: int | None


def resolve_context_settings(env: Mapping[str, str]) -> ContextSettings:
    """Resolve the frozen context horizon before any Verify task is emitted."""
    mode = env.get("CITATION_VERIFIER_VERIFY_CONTEXT_MODE", "auto")
    profile = env.get("CITATION_VERIFIER_CONTEXT_PROFILE", "large")
    if mode not in {"auto", "full_text", "extractive_rag"}:
        raise ConfigError("invalid Verify context mode")
    if profile not in {"large", "medium", "small"}:
        raise ConfigError("invalid Verify context profile")

    raw_limit = env.get("CITATION_VERIFIER_MAX_SOURCE_CHARS")
    if raw_limit is None or not raw_limit.strip():
        max_source_chars = None
    else:
        if raw_limit != raw_limit.strip():
            raise ConfigError(
                "CITATION_VERIFIER_MAX_SOURCE_CHARS must not contain surrounding whitespace"
            )
        try:
            max_source_chars = int(raw_limit)
        except ValueError as exc:
            raise ConfigError(
                "CITATION_VERIFIER_MAX_SOURCE_CHARS must be an integer"
            ) from exc
        if max_source_chars <= 0:
            raise ConfigError("CITATION_VERIFIER_MAX_SOURCE_CHARS must be positive")

    requires_limit = mode == "extractive_rag" or (
        mode == "auto" and profile in {"medium", "small"}
    )
    if requires_limit and max_source_chars is None:
        raise ConfigError(
            "RAG context selection requires CITATION_VERIFIER_MAX_SOURCE_CHARS"
        )
    return ContextSettings(mode, profile, max_source_chars)
