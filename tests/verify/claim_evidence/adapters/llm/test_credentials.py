# tests/verify/claim_evidence/adapters/llm/test_credentials.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import pytest

from core.verify.claim_evidence.adapters.llm.credentials import Credential, CredentialError, CredentialResolver


def test_credentials_resolve_only_matching_alias_and_fingerprint_without_leaking_secret():
    secret = "secret-canary"
    credential = Credential("fake:1", hashlib.sha256(secret.encode()).hexdigest(), secret)
    resolver = CredentialResolver({credential.alias: credential, "fake:credentialless": Credential("fake:credentialless", None, None)})
    assert resolver.resolve("fake:1", credential.fingerprint) == secret
    assert resolver.resolve("fake:credentialless", None) is None
    with pytest.raises(CredentialError): resolver.resolve("fake:1", "wrong")
    assert secret not in repr(credential)
    assert resolver.identities() == (
        ("fake:1", credential.fingerprint),
        ("fake:credentialless", None),
    )
    assert secret not in repr(resolver.identities())


def test_credential_fingerprint_mismatch_fails_before_dispatch():
    with pytest.raises(CredentialError, match="fingerprint"):
        CredentialResolver({
            "fake:1": Credential("fake:1", "0" * 64, "different-secret")
        })
