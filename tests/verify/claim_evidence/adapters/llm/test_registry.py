# tests/verify/claim_evidence/adapters/llm/test_registry.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from core.verify.backends.transport import call_registered_provider
from core.verify.claim_evidence.config import ConfigError
from core.verify.claim_evidence.adapters.llm.registry import (
    ProviderAdapter,
    ProviderRegistry,
    RegistryError,
    provider_registry,
    registered_provider_metadata,
)


def test_registry_is_explicit_and_provider_neutral():
    registry = ProviderRegistry()
    adapter = ProviderAdapter("fake", lambda system, user, model, secret: "{}")
    registry.register(adapter)
    assert registry.get("fake") is adapter
    with pytest.raises(RegistryError):
        registry.register(adapter)
    with pytest.raises(RegistryError):
        registry.get("missing")


def test_fake_provider_addition_requires_no_scheduler_or_controller_branch():
    first = ProviderRegistry()
    second = ProviderRegistry()
    fake = ProviderAdapter("new-provider", lambda *_: "{}")
    first.register(fake)
    second.register(ProviderAdapter("other", lambda *_: "{}"))
    second.register(fake)
    assert first.get("new-provider").call is second.get("new-provider").call


def test_provider_registry_uses_existing_backend_metadata_fail_closed():
    registry = provider_registry(("mistral",))
    assert registry.get("mistral").name == "mistral"
    with pytest.raises(RegistryError, match="not registered"):
        provider_registry(("definitely-missing",))


def test_registered_provider_call_rejects_noncanonical_backend_names_fail_closed():
    with pytest.raises(ConfigError, match="not registered"):
        call_registered_provider("definitely-missing", "system", "user", "model", None)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"reasoning": "sometimes"}, "reasoning mode"),
        ({"reasoning_effort": "extreme"}, "reasoning effort"),
    ],
)
def test_registered_provider_call_rejects_invalid_reasoning_controls(kwargs, message):
    with pytest.raises(ConfigError, match=message):
        call_registered_provider(
            "mistral", "system", "user", "model", "secret", **kwargs
        )


def test_omitted_output_budget_is_capability_gated():
    registry = provider_registry(("openai_compatible",), max_tokens=None)
    assert registry.get("openai_compatible").name == "openai_compatible"

    with pytest.raises(RegistryError, match="requires a max_tokens budget"):
        provider_registry(("mistral",), max_tokens=None)
    with pytest.raises(ConfigError, match="requires a max_tokens budget"):
        call_registered_provider(
            "mistral", "system", "user", "model", "secret", max_tokens=None
        )


def test_freetoken_is_registered_as_a_credentialless_provider():
    metadata = dict(
        (name, (env_key, env_model, credentialless))
        for name, env_key, env_model, credentialless, *_ in registered_provider_metadata()
    )

    assert metadata["freetoken"] == ("", "FREETOKEN_MODEL", True)
    assert provider_registry(("freetoken",), max_tokens=None).get(
        "freetoken"
    ).name == "freetoken"


def test_typesafe_declares_its_decoder_and_verify_policy_at_registration():
    metadata = {
        name: capability
        for name, _key, _model, _credentialless, capability
        in registered_provider_metadata()
    }

    adapter = provider_registry(("typesafe",), max_tokens=None).get("typesafe")
    assert callable(adapter.decode)
    assert metadata["typesafe"] == {
        "confidence_env": "TYPESAFE_MIN_CONFIDENCE",
        "policy_id": "typesafe-citation-confidence-v1",
        "pinned_model_pattern": r"jev-\d+\.\d+\.\d+",
        "jury1_context_mode": "extractive_rag",
        "jury1_context_help": (
            "Set CITATION_VERIFIER_VERIFY_CONTEXT_MODE=extractive_rag "
            "and CITATION_VERIFIER_MAX_SOURCE_CHARS=<positive integer>, "
            "or make every TypeSafe lane Jury2-only with "
            "CITATION_VERIFIER_VERIFY_JURY2_ONLY=typesafe:<jev-model>."
        ),
        "provider_label": "TypeSafe",
    }
