# core/verify/claim_evidence/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Public boundary for the current claim-evidence verifier."""
from .config import (
    ClaimEvidenceConfig,
    ProviderEnvSpec,
)
from .context_config import ConfigError, resolve_context_settings
from .contracts.schema import ContractError
from .domain.types import Jury1Decision, Jury2Decision
from .evidence.retrieval import (
    Bm25DependencyUnavailable,
    Bm25RetrievalLimitError,
)
from .runtime import ClaimEvidenceRuntime

__all__ = [
    "ClaimEvidenceConfig",
    "ProviderEnvSpec",
    "Jury1Decision",
    "Jury2Decision",
    "ClaimEvidenceRuntime",
    "Bm25DependencyUnavailable",
    "Bm25RetrievalLimitError",
    "ConfigError",
    "ContractError",
    "resolve_context_settings",
]
