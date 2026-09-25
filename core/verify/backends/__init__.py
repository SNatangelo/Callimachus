# core/verify/backends/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""LLM backend registry — import triggers auto-registration of all backends.

Each backend module calls ``register(BackendSpec(...))`` at import time.
After importing this package, ``_registry._REGISTRY`` contains every backend.
"""

from core.verify.backends import (  # noqa: F401 — side-effect: register()
    _registry,
    anthropic,
    openai,
    gemini,
    openrouter,
    ollama,
    host,
    typesafe,
    claude_cli,
    codex_cli,
    gemini_cli,
    openai_compat,
    freetoken,
    glm,
    mistral,
    opencode,
)
