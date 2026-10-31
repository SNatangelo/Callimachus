# tests/verify/claim_evidence/test_config.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import json

import pytest

from core.verify.claim_evidence.config import (
    CONTRACT_ID,
    CURSOR_SEMANTICS,
    ENV_NAMES,
    ConfigError,
    ProviderEnvSpec,
    resolve_config,
    resolve_context_settings,
)
from core.verify.claim_evidence.domain.types import (
    EXHAUSTION_CAUSES,
    MECHANICAL_REJECTION_CAUSES,
    TECHNICAL_FAILURE_CAUSES,
)
from core.verify.outcomes import CLAIM_EVIDENCE_OUTCOMES, VALID_OUTCOMES


SPECS = (ProviderEnvSpec("alpha", "ALPHA_KEYS", "ALPHA_MODELS"),)
TYPESAFE_CAPABILITY = {
    "confidence_env": "TYPESAFE_MIN_CONFIDENCE",
    "policy_id": "typesafe-citation-confidence-v1",
    "pinned_model_pattern": r"jev-\d+\.\d+\.\d+",
    "jury1_context_mode": "extractive_rag",
    "jury1_context_help": "Configure deterministic extractive context.",
    "provider_label": "TypeSafe",
}


def resolve(values, specs=SPECS, **extra):
    env = {"CITATION_VERIFIER_VERIFY_BACKENDS": "alpha", "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off", **values, **extra}
    return resolve_config(env, specs, run_id="run-1")


def test_role_selectors_accept_exact_known_tagged_model_ids():
    config = resolve(
        {"ALPHA_KEYS": "test-key", "ALPHA_MODELS": "llama3:8b,other"},
        CITATION_VERIFIER_VERIFY_JURY1_ONLY="alpha:llama3:8b",
    )
    assert config.jury1_only == frozenset({"alpha:llama3:8b"})
    assert config.providers[0].models == ("llama3:8b", "other")


@pytest.mark.parametrize("selector", ["alpha:llama3:70b", "alpha:llama3:8b:extra"])
def test_role_selectors_reject_unknown_tagged_model_ids(selector):
    with pytest.raises(ConfigError, match="unknown provider:model"):
        resolve(
            {"ALPHA_KEYS": "test-key", "ALPHA_MODELS": "llama3:8b,other"},
            CITATION_VERIFIER_VERIFY_JURY1_ONLY=selector,
        )


def test_typesafe_accepts_pinned_jev_for_extractive_rag_jury1():
    config = resolve_config(
        {
            "CITATION_VERIFIER_VERIFY_BACKENDS": "typesafe",
            "CITATION_VERIFIER_VERIFY_CONTEXT_MODE": "extractive_rag",
            "CITATION_VERIFIER_MAX_SOURCE_CHARS": "10000",
            "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
            "TYPESAFE_API_KEY": "test-secret",
            "TYPESAFE_MODEL": "jev-1.13.0",
            "TYPESAFE_MIN_CONFIDENCE": "0.8",
        },
        (ProviderEnvSpec("typesafe", "TYPESAFE_API_KEY", "TYPESAFE_MODEL", claim_evidence_capabilities=TYPESAFE_CAPABILITY),),
        run_id="run-1",
    )

    assert config.providers[0].models == ("jev-1.13.0",)
    assert config.providers[0].lanes[0].jury1_eligible is True
    assert config.provider_confidence_threshold == 0.8
    assert config.confidence_policy_id == "typesafe-citation-confidence-v1"
    assert config.snapshot()["confidence_policy_id"] == "typesafe-citation-confidence-v1"


def test_typesafe_jury1_rejects_non_rag_context_but_jury2_only_accepts_it():
    typesafe_env = {
        "TYPESAFE_API_KEY": "test-secret",
        "TYPESAFE_MODEL": "jev-1.13.0",
        "TYPESAFE_MIN_CONFIDENCE": "0.8",
    }
    typesafe_spec = ProviderEnvSpec(
        "typesafe", "TYPESAFE_API_KEY", "TYPESAFE_MODEL", claim_evidence_capabilities=TYPESAFE_CAPABILITY
    )
    with pytest.raises(
        ConfigError,
        match=(
            "TypeSafe Jury1 requires extractive_rag context. "
            "Configure deterministic extractive context"
        ),
    ):
        resolve_config(
            {
                **typesafe_env,
                "CITATION_VERIFIER_VERIFY_BACKENDS": "typesafe",
                "CITATION_VERIFIER_VERIFY_CONTEXT_MODE": "full_text",
                "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
            },
            (typesafe_spec,),
            run_id="run-1",
        )

    config = resolve_config(
        {
            **typesafe_env,
            "CITATION_VERIFIER_VERIFY_BACKENDS": "alpha,typesafe",
            "CITATION_VERIFIER_VERIFY_CONTEXT_MODE": "full_text",
            "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "low",
            "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "typesafe:jev-1.13.0",
            "ALPHA_KEYS": "alpha-secret",
            "ALPHA_MODELS": "alpha-model",
        },
        (
            ProviderEnvSpec("alpha", "ALPHA_KEYS", "ALPHA_MODELS"),
            typesafe_spec,
        ),
        run_id="run-1",
    )

    typesafe_lane = config.providers[1].lanes[0]
    assert typesafe_lane.jury1_eligible is False
    assert typesafe_lane.jury2_eligible is True


def test_common_config_has_no_typesafe_dependency_when_backend_is_absent():
    config = resolve({"ALPHA_KEYS": "key", "ALPHA_MODELS": "model"})

    assert config.provider_confidence_threshold is None
    assert config.confidence_policy_id is None
    assert "TYPESAFE_MIN_CONFIDENCE" not in ENV_NAMES


def test_positional_lanes_roles_and_snapshot_are_frozen_nonsecret():
    config = resolve({"ALPHA_KEYS": " k1 , k2 ", "ALPHA_MODELS": "m1, m2"}, CITATION_VERIFIER_VERIFY_JURY1_ONLY="alpha:m1")
    provider = config.providers[0]
    assert provider.pairing_mode == "positional_exclusive"
    assert [(lane.key_alias, lane.model, lane.jury1_eligible, lane.jury2_eligible) for lane in provider.lanes] == [("alpha:1", "m1", True, False), ("alpha:2", "m2", True, True)]
    snapshot = config.snapshot()
    assert snapshot["contract_id"] == CONTRACT_ID
    assert snapshot["jury1_prompt_id"] == "jury1_flow_prompt"
    assert snapshot["jury1_prompt_version"] > 0
    assert len(snapshot["jury1_prompt_sha256"]) == 64
    assert snapshot["jury2_prompt_id"] == "jury2_system_prompt"
    assert "k1" not in json.dumps(snapshot) and len(provider.credential_fingerprints) == 2
    assert config.credential_cursor == config.model_draw_index == 0


def test_unequal_and_credentialless_pairing_do_not_remap_lanes():
    config = resolve({"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1,m2"})
    assert config.providers[0].pairing_mode == "seeded_cross_product"
    assert [(lane.key_alias, lane.model) for lane in config.providers[0].lanes] == [("alpha:1", "m1"), ("alpha:1", "m2")]
    credentialless = resolve({"ALPHA_KEYS": "", "ALPHA_MODELS": "m1"}, (ProviderEnvSpec("alpha", "ALPHA_KEYS", "ALPHA_MODELS", credentialless=True),))
    assert credentialless.providers[0].credential_aliases == ("alpha:credentialless",)


def test_generic_cli_model_is_a_fallback_not_an_override():
    fallback = resolve({
        "ALPHA_KEYS": "k1",
        "ALPHA_MODELS": "",
        "CITATION_VERIFIER_MODEL": "cli-model",
    })
    assert fallback.providers[0].models == ("cli-model",)
    explicit = resolve({
        "ALPHA_KEYS": "k1",
        "ALPHA_MODELS": "provider-model",
        "CITATION_VERIFIER_MODEL": "cli-model",
    })
    assert explicit.providers[0].models == ("provider-model",)


def test_unequal_pairing_freezes_all_seeded_draw_candidates():
    two_keys_one_model = resolve({"ALPHA_KEYS": "k1,k2", "ALPHA_MODELS": "m1"})
    assert two_keys_one_model.providers[0].pairing_mode == "seeded_cross_product"
    assert [(lane.key_alias, lane.model) for lane in two_keys_one_model.providers[0].lanes] == [
        ("alpha:1", "m1"),
        ("alpha:2", "m1"),
    ]
    two_keys_three_models = resolve({"ALPHA_KEYS": "k1,k2", "ALPHA_MODELS": "m1,m2,m3"})
    assert [(lane.key_alias, lane.model) for lane in two_keys_three_models.providers[0].lanes] == [
        ("alpha:1", "m1"),
        ("alpha:1", "m2"),
        ("alpha:1", "m3"),
        ("alpha:2", "m1"),
        ("alpha:2", "m2"),
        ("alpha:2", "m3"),
    ]
    credentialless = resolve(
        {"ALPHA_MODELS": "m1,m2"},
        (ProviderEnvSpec("alpha", "", "ALPHA_MODELS", credentialless=True),),
    )
    assert [(lane.key_alias, lane.model) for lane in credentialless.providers[0].lanes] == [
        ("alpha:credentialless", "m1"),
        ("alpha:credentialless", "m2"),
    ]


def test_provider_specs_are_provider_scoped_and_fail_closed():
    shared = (
        ProviderEnvSpec("alpha", "SHARED_KEYS", "SHARED_MODELS"),
        ProviderEnvSpec("beta", "SHARED_KEYS", "SHARED_MODELS"),
    )
    config = resolve_config(
        {
            "CITATION_VERIFIER_VERIFY_BACKENDS": "alpha,beta",
            "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
            "SHARED_KEYS": "k1",
            "SHARED_MODELS": "m1",
        },
        shared,
        run_id="run-1",
    )
    assert [provider.name for provider in config.providers] == ["alpha", "beta"]
    with pytest.raises(ConfigError):
        resolve({"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1"}, SPECS + SPECS)
    with pytest.raises(ConfigError):
        resolve(
            {"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1"},
            (ProviderEnvSpec("alpha", "KEY", "KEY"),),
        )
    with pytest.raises(ConfigError):
        resolve(
            {"ALPHA_KEYS": "forbidden", "ALPHA_MODELS": "m1"},
            (ProviderEnvSpec("alpha", "ALPHA_KEYS", "ALPHA_MODELS", credentialless=True),),
        )


@pytest.mark.parametrize("env", [
    {"ALPHA_KEYS": "", "ALPHA_MODELS": "m1"},
    {"ALPHA_KEYS": "k1,,k2", "ALPHA_MODELS": "m1"},
    {"ALPHA_KEYS": "k1,k1", "ALPHA_MODELS": "m1"},
    {"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1,m1"},
    {"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1", "CITATION_VERIFIER_VERIFY_MAX_IN_FLIGHT": "0"},
    {"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1", "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "alpha:m1", "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "alpha:m1"},
    {"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1", "CITATION_VERIFIER_VERIFY_PACING_INTERVAL_MS": "-1"},
    {"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1", "CITATION_VERIFIER_VERIFY_COOLDOWN_OVERRIDES": "alpha:m1=0"},
    {"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1", "CITATION_VERIFIER_VERIFY_MAX_TOKENS": "0"},
    {"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1", "CITATION_VERIFIER_REASONING": "sometimes"},
    {"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1", "CITATION_VERIFIER_REASONING_EFFORT": "extreme"},
])
def test_invalid_configuration_fails_before_dispatch(env):
    with pytest.raises(ConfigError):
        resolve(env)


def test_explicit_jury2_level_specs_and_deterministic_seed_are_required():
    with pytest.raises(ConfigError):
        resolve_config({"CITATION_VERIFIER_VERIFY_BACKENDS": "alpha", "ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1"}, SPECS, run_id="run-1")
    first = resolve({"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1"})
    second = resolve({"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1"})
    assert first.execution_policy_hash == second.execution_policy_hash and first.selection_seed == second.selection_seed
    empty = resolve({
        "ALPHA_KEYS": "k1",
        "ALPHA_MODELS": "m1",
        "CITATION_VERIFIER_VERIFY_SELECTION_SEED": "",
    })
    assert empty.execution_policy_hash == first.execution_policy_hash
    assert empty.selection_seed == first.selection_seed
    explicit = resolve({"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1", "CITATION_VERIFIER_VERIFY_SELECTION_SEED": "fixed"})
    assert explicit.selection_seed == "fixed"
    assert explicit.execution_policy_hash != first.execution_policy_hash
    with pytest.raises(ConfigError):
        resolve({"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1", "CITATION_VERIFIER_VERIFY_SELECTION_SEED": " "})
    with pytest.raises(ConfigError):
        resolve_config(
            {
                "CITATION_VERIFIER_VERIFY_BACKENDS": "alpha",
                "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
                "ALPHA_KEYS": "k1",
                "ALPHA_MODELS": "m1",
            },
            SPECS,
            run_id=" ",
        )


def test_role_configuration_must_leave_required_jury_lanes():
    with pytest.raises(ConfigError):
        resolve(
            {"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1"},
            CITATION_VERIFIER_VERIFY_JURY2_ONLY="alpha:m1",
        )
    with pytest.raises(ConfigError):
        resolve(
            {
                "ALPHA_KEYS": "k1",
                "ALPHA_MODELS": "m1",
                "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "low",
            },
            CITATION_VERIFIER_VERIFY_JURY1_ONLY="alpha:m1",
        )


def test_default_numeric_versioned_policies_are_frozen():
    config = resolve({"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1"})
    assert (config.max_tokens, config.reasoning, config.reasoning_effort) == (
        4000,
        "auto",
        "medium",
    )
    assert (config.context_mode, config.profile, config.candidate_cap, config.jury1_technical_cap, config.jury2_technical_cap) == ("auto", "large", 5, 3, 3)
    assert (config.aggregate_in_flight, config.global_pacing_ms) == (4, 0)
    assert (config.cooldown.baseline_seconds, config.cooldown.multiplier, config.cooldown.local_max_seconds, config.cooldown.stable_successes) == (5, 2, 300, 2)
    assert config.cooldown.retry_after_is_lower_bound and config.cooldown.retry_after_not_truncated
    assert config.selector_algorithm == "sha256-counter-v1" and config.seed_derivation == "sha256-run-contract-policy-v1"
    assert config.cursor_semantics == CURSOR_SEMANTICS == "provider-credential-cycle-and-model-draw-v1"
    assert len(set(config.providers[0].credential_fingerprints)) == len(config.providers[0].credential_fingerprints)
    assert "invalid_key_env_sync" not in config.snapshot()


def test_output_budget_and_reasoning_change_frozen_policy_identity():
    default = resolve({"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1"})
    configured = resolve(
        {
            "ALPHA_KEYS": "k1",
            "ALPHA_MODELS": "m1",
            "CITATION_VERIFIER_VERIFY_MAX_TOKENS": "9000",
            "CITATION_VERIFIER_REASONING": "off",
            "CITATION_VERIFIER_REASONING_EFFORT": "high",
        }
    )

    assert configured.snapshot()["max_tokens"] == 9000
    assert configured.snapshot()["reasoning"] == "off"
    assert configured.snapshot()["reasoning_effort"] == "high"
    assert configured.execution_policy_hash != default.execution_policy_hash


def test_environment_and_failure_taxonomies_are_frozen():
    assert ENV_NAMES == frozenset(
        {
            "CITATION_VERIFIER_VERIFY_BACKENDS",
            "CITATION_VERIFIER_VERIFY_CONTEXT_MODE",
            "CITATION_VERIFIER_CONTEXT_PROFILE",
            "CITATION_VERIFIER_MAX_SOURCE_CHARS",
            "CITATION_VERIFIER_VERIFY_MAX_CANDIDATE_CYCLES",
            "CITATION_VERIFIER_VERIFY_JURY2_LEVEL",
            "CITATION_VERIFIER_VERIFY_JURY1_MAX_TECHNICAL_ATTEMPTS",
            "CITATION_VERIFIER_VERIFY_JURY2_MAX_TECHNICAL_ATTEMPTS",
            "CITATION_VERIFIER_VERIFY_MAX_IN_FLIGHT",
            "CITATION_VERIFIER_VERIFY_PACING_INTERVAL_MS",
            "CITATION_VERIFIER_VERIFY_PACING_BY_MODEL_MS",
            "CITATION_VERIFIER_VERIFY_SELECTION_SEED",
            "CITATION_VERIFIER_VERIFY_COOLDOWN_SECONDS",
            "CITATION_VERIFIER_VERIFY_COOLDOWN_OVERRIDES",
            "CITATION_VERIFIER_VERIFY_COOLDOWN_MULTIPLIER",
            "CITATION_VERIFIER_VERIFY_COOLDOWN_MAX_SECONDS",
            "CITATION_VERIFIER_VERIFY_COOLDOWN_STABLE_SUCCESSES",
            "CITATION_VERIFIER_VERIFY_JURY1_ONLY",
            "CITATION_VERIFIER_VERIFY_JURY2_ONLY",
            "CITATION_VERIFIER_STATE_DIR",
            "CITATION_VERIFIER_VERIFY_MAX_TOKENS",
                "CITATION_VERIFIER_REASONING",
                "CITATION_VERIFIER_REASONING_EFFORT",
            }
    )
    assert MECHANICAL_REJECTION_CAUSES == {
        "schema_invalid", "evidence_cardinality_invalid", "grounding_invalid", "provenance_invalid",
        "stage_disagreement",
    }
    assert TECHNICAL_FAILURE_CAUSES == {
        "rate_limited", "credential_invalid", "lane_unavailable", "timeout", "transport", "provider_failure",
    }
    assert EXHAUSTION_CAUSES == {
        "jury1_technical", "jury1_guard", "jury2_technical", "no_consensus", "jury2_rejected",
    }


def test_context_settings_require_limit_only_when_rag_can_be_selected():
    assert resolve_context_settings({}).profile == "large"
    settings = resolve_context_settings(
        {
            "CITATION_VERIFIER_CONTEXT_PROFILE": "medium",
            "CITATION_VERIFIER_MAX_SOURCE_CHARS": "12000",
        }
    )
    assert settings.max_source_chars == 12000
    with pytest.raises(ConfigError, match="requires CITATION_VERIFIER_MAX_SOURCE_CHARS"):
        resolve_context_settings(
            {"CITATION_VERIFIER_CONTEXT_PROFILE": "small"}
        )
    with pytest.raises(ConfigError, match="requires CITATION_VERIFIER_MAX_SOURCE_CHARS"):
        resolve_context_settings(
            {"CITATION_VERIFIER_VERIFY_CONTEXT_MODE": "extractive_rag"}
        )


def test_final_contract_and_outcomes_are_current():
    assert CONTRACT_ID == "verify-claim-evidence-v10"
    assert VALID_OUTCOMES < CLAIM_EVIDENCE_OUTCOMES


def test_no_output_budget_is_explicit_and_policy_bound():
    default = resolve({"ALPHA_KEYS": "k1", "ALPHA_MODELS": "m1"})
    configured = resolve(
        {
            "ALPHA_KEYS": "k1",
            "ALPHA_MODELS": "m1",
            "CITATION_VERIFIER_VERIFY_MAX_TOKENS": "none",
        }
    )

    assert configured.max_tokens is None
    assert configured.snapshot()["max_tokens"] is None
    assert configured.execution_policy_hash != default.execution_policy_hash
    with pytest.raises(ConfigError, match="integer or 'none'"):
        resolve(
            {
                "ALPHA_KEYS": "k1",
                "ALPHA_MODELS": "m1",
                "CITATION_VERIFIER_VERIFY_MAX_TOKENS": "unbounded",
            }
        )
