# core/verify/backends/_registry.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Backend registry — the single source of truth for LLM backend metadata.

Each backend module registers a ``BackendSpec`` via ``register()`` at import time.
The claim-evidence transport reads the populated ``_REGISTRY`` (via ``all_specs``)
to route a jury call to the selected provider.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class BackendSpec:
    """Declarative descriptor for one LLM backend.

    Attributes:
        name: Canonical backend name (e.g. ``"mistral"``).
        env_key: Environment variable holding the API key / credential.
        env_model: Optional env var for a backend-specific model override
            (e.g. ``"MISTRAL_MODEL"``).  If ``None`` the generic
            ``CITATION_VERIFIER_MODEL`` is used.
        base_url: Base URL for HTTP backends (``None`` for subprocess ones).
        available: Zero-argument callable that returns ``True`` when the
            backend is usable (key present, CLI on PATH, …).
        call: The ``(system, user, model, max_tokens) -> str`` function.
        requires_model: Whether this backend needs a model id to be set.
        supports_tools: Whether the backend can run tool-equipped calls.
        supports_omitted_max_tokens: Whether the backend accepts no client-side
            output-token field.
        auto_priority: Order in the default auto-selection list — lower
            values are tried first.
        codex_priority: Override for the Codex host environment.
        claude_code_priority: Override for the Claude Code host environment.
        antigravity_priority: Override for the Antigravity host environment.
    """

    name: str
    env_key: str = ""
    env_model: str | None = None
    base_url: str | None = None
    available: Callable[[], bool] = lambda: False
    call: Callable[[str, str, str | None, int | None], str] | None = None
    requires_model: bool = True
    supports_tools: bool = False
    supports_reasoning: bool = False
    supports_omitted_max_tokens: bool = False
    auto_priority: int = 100
    codex_priority: int | None = None
    claude_code_priority: int | None = None
    antigravity_priority: int | None = None
    # Optional claim-evidence response codec.  This keeps provider wire
    # formats out of the common Verify transport.
    claim_evidence_decode: Callable[[str, str, bytes, float | None, str], object] | None = None
    # Declarative Verify constraints for probability-only providers.
    claim_evidence_capabilities: dict[str, object] | None = None


_REGISTRY: dict[str, BackendSpec] = {}
"""All registered backends, keyed by canonical name."""


def register(spec: BackendSpec) -> BackendSpec:
    """Add *spec* to the global registry and return it (decorator-friendly)."""
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> BackendSpec | None:
    """Look up a backend by canonical name."""
    return _REGISTRY.get(name)


def all_specs() -> list[BackendSpec]:
    """Return every registered spec in insertion order."""
    return list(_REGISTRY.values())


def all_names() -> list[str]:
    """Return canonical names in insertion order."""
    return list(_REGISTRY.keys())
