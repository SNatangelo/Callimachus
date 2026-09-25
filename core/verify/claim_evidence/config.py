# core/verify/claim_evidence/config.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Declarative, fail-closed policy resolution for claim-evidence Verify."""
from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Mapping, Sequence

from .capabilities import resolve_confidence_policy
from .context_config import ConfigError, resolve_context_settings
from .domain.types import ProviderLane, ProviderPolicy
from .contracts.jury1_flow import JURY1_PROMPT_SPEC
from .contracts.jury2 import JURY2_PROMPT_SPEC


CONTRACT_ID = "verify-claim-evidence-v10"
GENERIC_MODEL_ENV = "CITATION_VERIFIER_MODEL"
SELECTOR_ALGORITHM = "sha256-counter-v1"
SEED_DERIVATION = "sha256-run-contract-policy-v1"
CURSOR_SEMANTICS = "provider-credential-cycle-and-model-draw-v1"
COOLDOWN_POLICY_VERSION = "cooldown-policy-v1"
ENV_NAMES = frozenset(
    """CITATION_VERIFIER_VERIFY_BACKENDS CITATION_VERIFIER_VERIFY_CONTEXT_MODE CITATION_VERIFIER_CONTEXT_PROFILE CITATION_VERIFIER_MAX_SOURCE_CHARS
    CITATION_VERIFIER_VERIFY_MAX_CANDIDATE_CYCLES CITATION_VERIFIER_VERIFY_JURY2_LEVEL CITATION_VERIFIER_VERIFY_JURY1_MAX_TECHNICAL_ATTEMPTS CITATION_VERIFIER_VERIFY_JURY2_MAX_TECHNICAL_ATTEMPTS
    CITATION_VERIFIER_VERIFY_MAX_IN_FLIGHT CITATION_VERIFIER_VERIFY_PACING_INTERVAL_MS CITATION_VERIFIER_VERIFY_PACING_BY_MODEL_MS CITATION_VERIFIER_VERIFY_SELECTION_SEED CITATION_VERIFIER_VERIFY_COOLDOWN_SECONDS
    CITATION_VERIFIER_VERIFY_COOLDOWN_OVERRIDES CITATION_VERIFIER_VERIFY_COOLDOWN_MULTIPLIER CITATION_VERIFIER_VERIFY_COOLDOWN_MAX_SECONDS CITATION_VERIFIER_VERIFY_COOLDOWN_STABLE_SUCCESSES
    CITATION_VERIFIER_VERIFY_JURY1_ONLY CITATION_VERIFIER_VERIFY_JURY2_ONLY CITATION_VERIFIER_STATE_DIR
    CITATION_VERIFIER_VERIFY_MAX_TOKENS CITATION_VERIFIER_REASONING CITATION_VERIFIER_REASONING_EFFORT""".split()
)

def select_contract(persisted: object, environ: Mapping[str, str]) -> str:
    raw = environ.get("CITATION_VERIFIER_VERIFY_CONTRACT")
    requested = raw.strip().lower() if isinstance(raw, str) and raw.strip() else None
    if persisted not in {None, CONTRACT_ID}: raise ConfigError("persisted verify contract is not supported")
    if requested not in {None, CONTRACT_ID}: raise ConfigError("requested verify contract is not supported")
    return CONTRACT_ID


@dataclass(frozen=True, slots=True)
class ProviderEnvSpec:
    name: str
    key_env: str
    model_env: str
    credentialless: bool = False
    claim_evidence_capabilities: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class CooldownPolicy:
    baseline_seconds: int
    multiplier: int
    local_max_seconds: int
    stable_successes: int
    retry_after_is_lower_bound: bool = True
    retry_after_not_truncated: bool = True
    version: str = COOLDOWN_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class ClaimEvidenceConfig:
    providers: tuple[ProviderPolicy, ...]
    context_mode: str
    profile: str
    max_tokens: int | None
    reasoning: str
    reasoning_effort: str
    candidate_cap: int
    jury1_technical_cap: int
    jury2_technical_cap: int
    jury2_level: str
    aggregate_in_flight: int
    global_pacing_ms: int
    pacing_by_model_ms: tuple[tuple[str, int], ...]
    jury1_only: frozenset[str]
    jury2_only: frozenset[str]
    execution_policy_hash: str
    selection_seed: str
    selector_algorithm: str
    seed_derivation: str
    cursor_semantics: str
    credential_cursor: int
    model_draw_index: int
    cooldown: CooldownPolicy
    cooldown_overrides: tuple[tuple[str, int], ...]
    jury1_prompt_id: str
    jury1_prompt_version: int
    jury1_prompt_sha256: str
    jury2_prompt_id: str
    jury2_prompt_version: int
    jury2_prompt_sha256: str
    provider_confidence_threshold: float | None = None
    confidence_policy_id: str | None = None

    def snapshot(self) -> dict[str, object]:
        def encode(value: object) -> object:
            if isinstance(value, frozenset):
                return sorted(value)
            raise TypeError(f"unsupported snapshot value: {type(value)!r}")
        return json.loads(json.dumps(asdict(self), sort_keys=True, default=encode)) | {"contract_id": CONTRACT_ID}


def _csv(raw: str | None, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if raw is None:
        raw = ""
    if allow_empty and raw.strip() == "":
        return ()
    if not isinstance(raw, str):
        raise ConfigError(f"{field} must be CSV")
    values = tuple(item.strip() for item in raw.split(","))
    if not values or any(not item for item in values) or len(values) != len(set(values)):
        raise ConfigError(f"{field} must be non-empty unique CSV")
    return values


def _positive(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    zero: bool = False,
    allow_none: bool = False,
) -> int | None:
    raw = env.get(name, str(default))
    if allow_none and isinstance(raw, str) and raw.strip().lower() == "none":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        suffix = " or 'none'" if allow_none else ""
        raise ConfigError(f"{name} must be an integer{suffix}") from exc
    if value < 0 or (not zero and value == 0):
        raise ConfigError(f"{name} must be {'non-negative' if zero else 'positive'}")
    return value


def _choice(
    env: Mapping[str, str], name: str, default: str, allowed: frozenset[str]
) -> str:
    raw = env.get(name, default)
    if not isinstance(raw, str) or raw.strip().lower() not in allowed:
        raise ConfigError(f"{name} has an invalid value")
    return raw.strip().lower()


def _mapping(raw: str | None, name: str, known: frozenset[str], *, positive: bool) -> tuple[tuple[str, int], ...]:
    if raw is None or not raw.strip():
        return ()
    result: list[tuple[str, int]] = []
    for item in _csv(raw, name):
        if item.count("=") != 1:
            raise ConfigError(f"{name} must use provider:model=integer")
        selector, raw_value = (piece.strip() for piece in item.split("=", 1))
        if selector not in known:
            raise ConfigError(f"{name} contains an unknown provider:model")
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ConfigError(f"{name} must contain integer values") from exc
        if value < 0 or (positive and not value):
            raise ConfigError(f"{name} has an invalid value")
        result.append((selector, value))
    if len({selector for selector, _ in result}) != len(result):
        raise ConfigError(f"{name} contains duplicate selectors")
    return tuple(result)


def _selectors(raw: str | None, name: str, known: frozenset[str]) -> frozenset[str]:
    if raw is None or not raw.strip():
        return frozenset()
    result = frozenset(_csv(raw, name))
    if any(item.count(":") != 1 or item not in known for item in result):
        raise ConfigError(f"{name} contains an unknown provider:model")
    return result


def _validate_specs(provider_specs: Sequence[ProviderEnvSpec]) -> dict[str, ProviderEnvSpec]:
    if not provider_specs:
        raise ConfigError("provider specs are required")
    names = [spec.name for spec in provider_specs]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ConfigError("provider specs must have non-empty names")
    if len(names) != len(set(names)):
        raise ConfigError("provider spec names must be unique")
    for spec in provider_specs:
        if not isinstance(spec.model_env, str) or not spec.model_env.strip():
            raise ConfigError("provider model env names must be non-empty")
        if not spec.credentialless and (
            not isinstance(spec.key_env, str) or not spec.key_env.strip()
        ):
            raise ConfigError("credentialed providers require a key env name")
        if spec.key_env and spec.key_env == spec.model_env:
            raise ConfigError("one provider cannot share its key and model env")
    return {spec.name: spec for spec in provider_specs}


RawProvider = tuple[str, tuple[str, ...], tuple[str | None, ...], tuple[str, ...], str]
RuntimeValues = tuple[tuple[object, ...], tuple[tuple[str, int], ...], tuple[tuple[str, int], ...], CooldownPolicy]


def _provider_inputs(
    env: Mapping[str, str],
    specs: Mapping[str, ProviderEnvSpec],
    names: tuple[str, ...],
) -> tuple[tuple[RawProvider, ...], frozenset[str]]:
    raw_providers: list[RawProvider] = []
    selectors: set[str] = set()
    for name in names:
        spec = specs[name]
        if spec.credentialless:
            raw_keys = env.get(spec.key_env) if spec.key_env else None
            if isinstance(raw_keys, str) and raw_keys.strip():
                raise ConfigError(f"{name} is credentialless and forbids key values")
            keys = ()
        else:
            keys = _csv(env.get(spec.key_env), spec.key_env)
        aliases = (
            (f"{name}:credentialless",)
            if not keys
            else tuple(f"{name}:{index}" for index in range(1, len(keys) + 1))
        )
        fingerprints = (
            (None,)
            if not keys
            else tuple(hashlib.sha256(key.encode()).hexdigest() for key in keys)
        )
        raw_models = env.get(spec.model_env) or (env.get(GENERIC_MODEL_ENV)
            if spec.model_env != GENERIC_MODEL_ENV else None)
        models = _csv(raw_models, spec.model_env)
        pairing = (
            "positional_exclusive"
            if len(aliases) == len(models)
            else "seeded_cross_product"
        )
        raw_providers.append((name, aliases, fingerprints, models, pairing))
        selectors.update(f"{name}:{model}" for model in models)
    return tuple(raw_providers), frozenset(selectors)


def _apply_roles(
    raw_providers: tuple[RawProvider, ...],
    jury1_only: frozenset[str],
    jury2_only: frozenset[str],
) -> tuple[ProviderPolicy, ...]:
    providers: list[ProviderPolicy] = []
    for name, aliases, fingerprints, models, pairing in raw_providers:
        if pairing == "positional_exclusive":
            lane_pairs = zip(aliases, fingerprints, models)
        else:
            lane_pairs = (
                (alias, fingerprint, model)
                for alias, fingerprint in zip(aliases, fingerprints)
                for model in models
            )
        lanes = tuple(
            ProviderLane(
                alias,
                fingerprint,
                model,
                f"{name}:{model}" not in jury2_only,
                f"{name}:{model}" not in jury1_only,
            )
            for alias, fingerprint, model in lane_pairs
        )
        providers.append(
            ProviderPolicy(name, aliases, fingerprints, models, pairing, lanes)
        )
    return tuple(providers)


def _policy_identity(
    policy: dict[str, object],
    explicit_seed: str | None,
    run_id: str,
) -> tuple[str, str]:
    if explicit_seed == "":
        explicit_seed = None
    material = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    material_hash = hashlib.sha256(material.encode()).hexdigest()
    if explicit_seed is not None and not explicit_seed.strip():
        raise ConfigError("selection seed must be non-empty when supplied")
    seed = explicit_seed or hashlib.sha256(
        f"{SEED_DERIVATION}\x1f{run_id}\x1f{CONTRACT_ID}\x1f{material_hash}".encode()
    ).hexdigest()
    final_policy = json.dumps(
        {"material": policy, "selection_seed": seed},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(final_policy.encode()).hexdigest(), seed


def _runtime_values(
    env: Mapping[str, str],
    known: frozenset[str],
    providers: tuple[ProviderPolicy, ...],
) -> RuntimeValues:
    context = resolve_context_settings(env)
    level = env.get("CITATION_VERIFIER_VERIFY_JURY2_LEVEL")
    if level not in {"off", "low", "medium", "high"}:
        raise ConfigError(
            "invalid context, profile, or explicit Jury2 level"
        )
    all_lanes = tuple(lane for provider in providers for lane in provider.lanes)
    if not any(lane.jury1_eligible for lane in all_lanes):
        raise ConfigError("configuration has no Jury1-eligible lane")
    if level != "off" and not any(lane.jury2_eligible for lane in all_lanes):
        raise ConfigError("enabled Jury2 has no eligible lane")
    pacing = _mapping(
        env.get("CITATION_VERIFIER_VERIFY_PACING_BY_MODEL_MS"),
        "CITATION_VERIFIER_VERIFY_PACING_BY_MODEL_MS",
        known,
        positive=False,
    )
    overrides = _mapping(
        env.get("CITATION_VERIFIER_VERIFY_COOLDOWN_OVERRIDES"),
        "CITATION_VERIFIER_VERIFY_COOLDOWN_OVERRIDES",
        known,
        positive=True,
    )
    cooldown = CooldownPolicy(
        _positive(env, "CITATION_VERIFIER_VERIFY_COOLDOWN_SECONDS", 5),
        _positive(env, "CITATION_VERIFIER_VERIFY_COOLDOWN_MULTIPLIER", 2),
        _positive(env, "CITATION_VERIFIER_VERIFY_COOLDOWN_MAX_SECONDS", 300),
        _positive(env, "CITATION_VERIFIER_VERIFY_COOLDOWN_STABLE_SUCCESSES", 2),
    )
    values = (
        context.mode,
        context.profile,
        _positive(env, "CITATION_VERIFIER_VERIFY_MAX_TOKENS", 4000, allow_none=True),
        _choice(env, "CITATION_VERIFIER_REASONING", "auto", frozenset({"auto", "on", "off"})),
        _choice(env, "CITATION_VERIFIER_REASONING_EFFORT", "medium", frozenset({"low", "medium", "high", "max", "xhigh"})),
        _positive(env, "CITATION_VERIFIER_VERIFY_MAX_CANDIDATE_CYCLES", 5),
        _positive(env, "CITATION_VERIFIER_VERIFY_JURY1_MAX_TECHNICAL_ATTEMPTS", 3),
        _positive(env, "CITATION_VERIFIER_VERIFY_JURY2_MAX_TECHNICAL_ATTEMPTS", 3),
        level,
        _positive(env, "CITATION_VERIFIER_VERIFY_MAX_IN_FLIGHT", 4),
        _positive(
            env,
            "CITATION_VERIFIER_VERIFY_PACING_INTERVAL_MS",
            0,
            zero=True,
        ),
    )
    return values, pacing, overrides, cooldown


def resolve_config(
    env: Mapping[str, str], provider_specs: Sequence[ProviderEnvSpec], *, run_id: str
) -> ClaimEvidenceConfig:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ConfigError("run_id must be non-empty")
    specs = _validate_specs(provider_specs)
    names = _csv(
        env.get("CITATION_VERIFIER_VERIFY_BACKENDS"),
        "CITATION_VERIFIER_VERIFY_BACKENDS",
    )
    if any(name not in specs for name in names):
        raise ConfigError("enabled provider lacks a declarative spec")
    raw_providers, known = _provider_inputs(env, specs, names)
    jury1_only = _selectors(env.get("CITATION_VERIFIER_VERIFY_JURY1_ONLY"), "CITATION_VERIFIER_VERIFY_JURY1_ONLY", known)
    jury2_only = _selectors(env.get("CITATION_VERIFIER_VERIFY_JURY2_ONLY"), "CITATION_VERIFIER_VERIFY_JURY2_ONLY", known)
    if jury1_only & jury2_only:
        raise ConfigError("Jury role selectors conflict")
    providers = _apply_roles(raw_providers, jury1_only, jury2_only)
    values, pacing, overrides, cooldown = _runtime_values(env, known, providers)
    threshold, confidence_policy_id = resolve_confidence_policy(
        env, providers, specs, values[0]
    )
    policy: dict[str, object] = {
        "contract_id": CONTRACT_ID,
        "providers": [asdict(provider) for provider in providers],
        "values": values,
        "pacing": pacing,
        "jury1_only": sorted(jury1_only),
        "jury2_only": sorted(jury2_only),
        "selector_algorithm": SELECTOR_ALGORITHM,
        "seed_derivation": SEED_DERIVATION,
        "cursor_semantics": CURSOR_SEMANTICS,
        "initial_cursors": (0, 0),
        "cooldown": asdict(cooldown),
        "overrides": overrides,
        "provider_confidence_threshold": threshold,
        "confidence_policy_id": confidence_policy_id,
        "jury1_prompt": {"prompt_id": JURY1_PROMPT_SPEC.prompt_id, "version": JURY1_PROMPT_SPEC.version, "sha256": JURY1_PROMPT_SPEC.sha256},
        "jury2_prompt": {"prompt_id": JURY2_PROMPT_SPEC.prompt_id, "version": JURY2_PROMPT_SPEC.version, "sha256": JURY2_PROMPT_SPEC.sha256},
    }
    policy_hash, seed = _policy_identity(
        policy, env.get("CITATION_VERIFIER_VERIFY_SELECTION_SEED"), run_id,
    )
    return ClaimEvidenceConfig(
        tuple(providers), *values, pacing, jury1_only, jury2_only,
        policy_hash, seed, SELECTOR_ALGORITHM, SEED_DERIVATION, CURSOR_SEMANTICS,
        0, 0, cooldown, overrides,
        JURY1_PROMPT_SPEC.prompt_id, JURY1_PROMPT_SPEC.version, JURY1_PROMPT_SPEC.sha256,
        JURY2_PROMPT_SPEC.prompt_id, JURY2_PROMPT_SPEC.version, JURY2_PROMPT_SPEC.sha256, threshold, confidence_policy_id,
    )
