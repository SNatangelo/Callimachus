# core/verify/claim_evidence/adapters/llm/credentials.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""In-memory alias-to-secret resolution with fingerprint validation."""
import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping


class CredentialError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Credential:
    alias: str
    fingerprint: str | None
    secret: str | None = field(repr=False)


class CredentialResolver:
    def __init__(self, credentials: Mapping[str, Credential]) -> None:
        self._credentials = dict(credentials)
        for alias, credential in self._credentials.items():
            if alias != credential.alias or not alias or (credential.secret is None) != (credential.fingerprint is None):
                raise CredentialError("credential mapping is invalid")
            if credential.secret is not None and hashlib.sha256(credential.secret.encode()).hexdigest() != credential.fingerprint:
                raise CredentialError("credential fingerprint does not match")

    def resolve(self, alias: str, fingerprint: str | None) -> str | None:
        credential = self._credentials.get(alias)
        if credential is None or credential.fingerprint != fingerprint:
            raise CredentialError("credential alias or fingerprint is unknown")
        return credential.secret

    def identities(self) -> tuple[tuple[str, str | None], ...]:
        """Return the only safe persistence/diagnostic projection."""
        return tuple(
            sorted(
                (credential.alias, credential.fingerprint)
                for credential in self._credentials.values()
            )
        )

def credentials_from_config(
    config: Any,
    specs: tuple[Any, ...],
    environ: Mapping[str, str],
) -> CredentialResolver:
    """Resolve raw values in memory while returning only validated identities."""
    by_name = {spec.name: spec for spec in specs}
    values: dict[str, Credential] = {}
    for provider in config.providers:
        spec = by_name[provider.name]
        secrets = (
            (None,)
            if spec.credentialless
            else tuple(
                item.strip() for item in environ[spec.key_env].split(",")
            )
        )
        for alias, digest, secret in zip(
            provider.credential_aliases,
            provider.credential_fingerprints,
            secrets,
        ):
            values[alias] = Credential(alias, digest, secret)
    return CredentialResolver(values)
