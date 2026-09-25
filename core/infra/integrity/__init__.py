# core/infra/integrity/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic artifact integrity primitives."""

from .manifest import (
    ArtifactEntry,
    ArtifactManifest,
    ManifestDiff,
    ManifestError,
    build_content_store_manifest,
    build_run_manifest,
    compare_manifests,
)
from .authority import (
    AuthorityError,
    AuthorityIntegrityError,
    AuthorityPermissionError,
    FileAuthority,
    initialise_authority,
)
from .client import (
    AuthorityRejected,
    AuthorityUnavailable,
    IntegrityAuthorityClient,
)
from .gate import (
    ArtifactIntegrityViolation,
    ContentStoreIntegrityViolation,
    DEBUG_INTEGRITY_LABEL,
    DEBUG_OVERRIDE_LABEL,
    ENV_INTEGRITY_SOCKET,
    IntegrityGateError,
    PipelineIntegrityLease,
    RunIntegrityGate,
)

__all__ = [
    "ArtifactEntry",
    "ArtifactManifest",
    "AuthorityError",
    "AuthorityIntegrityError",
    "AuthorityPermissionError",
    "AuthorityRejected",
    "AuthorityUnavailable",
    "ArtifactIntegrityViolation",
    "ContentStoreIntegrityViolation",
    "DEBUG_INTEGRITY_LABEL",
    "DEBUG_OVERRIDE_LABEL",
    "ENV_INTEGRITY_SOCKET",
    "FileAuthority",
    "IntegrityAuthorityClient",
    "IntegrityGateError",
    "ManifestDiff",
    "ManifestError",
    "PipelineIntegrityLease",
    "RunIntegrityGate",
    "build_content_store_manifest",
    "build_run_manifest",
    "compare_manifests",
    "initialise_authority",
]
