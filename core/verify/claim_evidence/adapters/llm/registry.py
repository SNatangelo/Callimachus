# core/verify/claim_evidence/adapters/llm/registry.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-neutral adapters over the existing exact-backend call boundary."""
from dataclasses import dataclass
from typing import Callable, Iterable


class RegistryError(ValueError):
    """A provider adapter registration or lookup is invalid."""


ProviderCall = Callable[[str, str, str, str | None], str]
ProviderDecode = Callable[[str, str, bytes, float | None, str], object]


@dataclass(frozen=True, slots=True)
class ProviderAdapter:
    name: str
    call: ProviderCall
    decode: ProviderDecode | None = None


class ProviderRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        names = (adapter.name,) if isinstance(adapter, ProviderAdapter) else ()
        if (
            not names
            or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)
            or any(name in self._adapters for name in names)
            or not callable(adapter.call)
            or (adapter.decode is not None and not callable(adapter.decode))
        ):
            raise RegistryError("provider registration is invalid")
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> ProviderAdapter:
        try:
            return self._adapters[name]
        except (KeyError, TypeError) as exc:
            raise RegistryError("provider is not registered") from exc


def provider_registry(
    names: Iterable[str] | None = None,
    *,
    max_tokens: int | None = 4000,
    reasoning: str | None = None,
    reasoning_effort: str | None = None,
) -> ProviderRegistry:
    """Build adapters from registered backend metadata."""
    import core.verify.backends  # noqa: F401
    from core.verify.backends._registry import all_specs

    selected = None if names is None else frozenset(names)
    registry = ProviderRegistry()
    for spec in all_specs():
        if selected is not None and spec.name not in selected:
            continue
        if spec.call is None:
            continue
        if max_tokens is None and not spec.supports_omitted_max_tokens:
            raise RegistryError(
                f"provider {spec.name!r} requires a max_tokens budget"
            )
        registry.register(
            ProviderAdapter(
                spec.name,
                _registered_call(
                    spec.name, max_tokens, reasoning, reasoning_effort
                ),
                spec.claim_evidence_decode,
            )
        )
    if selected is not None:
        missing = tuple(name for name in selected if _missing(registry, name))
        if missing:
            raise RegistryError("requested provider is not registered")
    return registry


def registered_provider_metadata(
) -> tuple[tuple[str, str, str | None, bool, dict[str, object] | None], ...]:
    """Expose registered transport metadata."""
    import core.verify.backends  # noqa: F401
    from core.verify.backends._registry import all_specs

    return tuple(
        (spec.name, spec.env_key, spec.env_model, not spec.env_key, spec.claim_evidence_capabilities)
        for spec in all_specs()
        if spec.call is not None
    )


def _missing(registry: ProviderRegistry, name: str) -> bool:
    try:
        registry.get(name)
    except RegistryError:
        return True
    return False


def _registered_call(
    provider: str,
    max_tokens: int | None,
    reasoning: str | None,
    reasoning_effort: str | None,
) -> ProviderCall:
    def call(
        system: str,
        user: str,
        model: str,
        secret: str | None,
    ) -> str:
        from core.verify.backends.transport import call_registered_provider

        return call_registered_provider(
            provider,
            system,
            user,
            model,
            secret,
            max_tokens=max_tokens,
            reasoning=reasoning,
            reasoning_effort=reasoning_effort,
        )

    return call
