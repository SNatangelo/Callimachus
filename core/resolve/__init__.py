# core/resolve/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Public interface for the deterministic resolution phase."""

from .service import (
    bind_openalex_batch_session,
    bind_pmc_idconv_batch_session,
    bind_pubmed_batch_session,
    bind_scholar_archive_circuit_session,
    bind_semantic_scholar_batch_session,
    main,
    new_openalex_batch_session,
    new_pmc_idconv_batch_session,
    new_pubmed_batch_session,
    new_scholar_archive_circuit_session,
    new_semantic_scholar_batch_session,
    now,
    prime_openalex_batch_session,
    prime_pmc_idconv_batch_session,
    prime_pubmed_batch_session,
    prime_semantic_scholar_batch_session,
    resolve,
    resume_semantic_scholar_cooldown,
    set_contact,
)

__all__ = [
    "bind_openalex_batch_session",
    "bind_pmc_idconv_batch_session",
    "bind_pubmed_batch_session",
    "bind_scholar_archive_circuit_session",
    "bind_semantic_scholar_batch_session",
    "main",
    "new_openalex_batch_session",
    "new_pmc_idconv_batch_session",
    "new_pubmed_batch_session",
    "new_scholar_archive_circuit_session",
    "new_semantic_scholar_batch_session",
    "now",
    "prime_openalex_batch_session",
    "prime_pmc_idconv_batch_session",
    "prime_pubmed_batch_session",
    "prime_semantic_scholar_batch_session",
    "resolve",
    "resume_semantic_scholar_cooldown",
    "set_contact",
]
