# core/app/desktop_config.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only configuration projections for the desktop application."""
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Mapping

from core.verify.claim_evidence.config import (
    GENERIC_MODEL_ENV,
    ProviderEnvSpec,
    resolve_config,
)
from core.verify.claim_evidence.context_config import ConfigError
from core.verify.claim_evidence.adapters.llm.registry import (
    registered_provider_metadata,
)


_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$"
)
# Configuration limits and file paths can contain words such as ``KEY`` and
# ``TOKEN`` without being credentials.  Keep the masking rule deliberately
# narrow: provider/resolver credentials have one of these terminal forms, and
# the inline HMAC key is the only project-specific exceptional name.
_SECRET_SUFFIXES = (
    "_API_KEY",
    "_API_TOKEN",
    "_AUTH_TOKEN",
    "_ACCESS_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIAL",
)
_SECRET_NAMES = {"CITATION_VERIFIER_SIGNING_KEY"}

# These controls have a defined UI meaning.  Do not infer booleans from arbitrary
# numeric values: many numeric environment variables are limits and budgets.
_CONTROL_METADATA: dict[str, dict[str, object]] = {
    "CITATION_VERIFIER_ACCURACY": {"control": "enum", "choices": [
        ("maximum", "Maximum: full text only"),
        ("maximum_fallback", "Maximum with abstract fallback"),
        ("standard", "Standard: full text preferred"),
        ("abstract", "Abstract"),
        ("standard_web", "Standard web"),
    ]},
    "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": {"control": "enum", "choices": [
        ("off", "Off"), ("low", "Low"), ("medium", "Medium"), ("high", "High"),
    ]},
    "CITATION_VERIFIER_REASONING": {"control": "enum", "choices": [
        ("auto", "Automatic"), ("on", "Enabled"), ("off", "Disabled"),
    ]},
    "CITATION_VERIFIER_REASONING_EFFORT": {"control": "enum", "choices": [
        ("low", "Low"), ("medium", "Medium"), ("high", "High"),
    ]},
    "CITATION_VERIFIER_VERIFY_CONTEXT_MODE": {"control": "enum", "choices": [
        ("auto", "Automatic"), ("full_text", "Full text"), ("extractive_rag", "Extractive RAG"),
    ]},
    "CITATION_VERIFIER_CONTEXT_PROFILE": {"control": "enum", "choices": [
        ("large", "Large"), ("medium", "Medium"), ("small", "Small"),
    ]},
    "CITATION_VERIFIER_HTTP_PROFILE": {"control": "enum", "choices": [
        ("plain", "Plain"), ("browser_like", "Browser-like"),
    ]},
    "CITATION_VERIFIER_FETCH_CHALLENGE_MODE": {"control": "enum", "choices": [
        ("off", "Off"), ("queue", "Queue"), ("interactive_challenge", "Interactive challenge"),
        ("interactive_closed", "Interactive closed"),
        ("interactive_fallback", "Interactive fallback"),
        ("interactive_fallback_closed", "Interactive fallback closed"),
    ]},
}
_BOOLEAN_NAMES = {
    "CITATION_VERIFIER_OCR_AUTO", "CITATION_VERIFIER_VERIFY_TABLE_CITATIONS",
    "CITATION_VERIFIER_OA_ALTERNATES", "CITATION_VERIFIER_WAYBACK",
    "CITATION_VERIFIER_PERMA", "CITATION_VERIFIER_REPORT_HTML",
    "CITATION_VERIFIER_DEBUG_RUN", "CITATION_VERIFIER_PERF",
    "CITATION_VERIFIER_HTTP_MEMO", "CALLIMACHUS_JOURNAL_AUTHORITY_AUTO_UPDATE",
}
_INTEGER_NAMES = {
    "CITATION_VERIFIER_OCR_AUTO_MAX_PAGES", "CITATION_VERIFIER_OCR_WORKERS",
    "CITATION_VERIFIER_VERIFY_MAX_IN_FLIGHT", "CITATION_VERIFIER_VERIFY_MAX_TOKENS",
    "CITATION_VERIFIER_VERIFY_MAX_CANDIDATE_CYCLES",
    "CITATION_VERIFIER_VERIFY_JURY1_MAX_TECHNICAL_ATTEMPTS",
    "CITATION_VERIFIER_VERIFY_JURY2_MAX_TECHNICAL_ATTEMPTS",
    "CITATION_VERIFIER_MAX_SOURCE_CHARS", "CITATION_VERIFIER_VERIFY_PACING_INTERVAL_MS",
    "CITATION_VERIFIER_VERIFY_COOLDOWN_SECONDS",
    "CITATION_VERIFIER_VERIFY_COOLDOWN_MAX_SECONDS",
    "CITATION_VERIFIER_VERIFY_COOLDOWN_STABLE_SUCCESSES",
    "CITATION_VERIFIER_FETCH_WORKERS", "CITATION_VERIFIER_RESOLVE_WORKERS",
    "CITATION_VERIFIER_FETCH_PDF_TIMEOUT", "CITATION_VERIFIER_FETCH_BUDGET_S",
    "CALLIMACHUS_JOURNAL_AUTHORITY_TTL_DAYS", "CALLIMACHUS_RESOLVER_COVERAGE_TTL_DAYS",
}
_PROVIDERS = {
    "OPENAI_": "OpenAI / compatible", "ANTHROPIC_": "Anthropic",
    "GEMINI_": "Google Gemini", "OPENROUTER_": "OpenRouter",
    "TYPESAFE_": "TypeSafe AI", "OLLAMA_": "Ollama",
    "FREETOKEN_": "FreeToken", "ZHIPUAI_": "GLM (ZhipuAI)",
    "MISTRAL_": "Mistral AI", "OPENCODE_": "OpenCode Zen",
}


def _dotenv_values(path: str | os.PathLike[str] | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if path is None:
        return values
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        value = match.group("value").strip()
        if value and value[0:1] in {"'", '"'} and value[-1:] == value[0]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[match.group("name")] = value
    return values


def _dotenv_descriptions(path: str | os.PathLike[str] | None) -> tuple[list[str], dict[str, str]]:
    """Return template order and the contiguous comments immediately above entries."""
    if path is None:
        return [], {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], {}
    order: list[str] = []
    descriptions: dict[str, str] = {}
    comments: list[str] = []
    for line in lines:
        match = _ASSIGNMENT.match(line)
        if match:
            name = match.group("name")
            order.append(name)
            useful = [item for item in comments if not set(item) <= {"=", "-", " "}]
            if useful:
                description = " ".join(useful).strip()
                sentence_end = description.find(". ")
                descriptions[name] = (
                    description[:sentence_end + 1] if sentence_end >= 0 else description[:280]
                )
            comments = []
        elif line.lstrip().startswith("#"):
            comments.append(line.lstrip()[1:].strip())
        elif line.strip():
            comments = []
        else:
            comments = []
    return order, descriptions


def _group(name: str) -> str:
    if name in {
        "CITATION_VERIFIER_ACCURACY",
        "CITATION_VERIFIER_MAILTO",
        "CITATION_VERIFIER_STATE_DIR",
        "CITATION_VERIFIER_REPORT_HTML",
    }:
        return "general"
    if any(name.startswith(prefix) for prefix in _PROVIDERS) or name in {
        "CITATION_VERIFIER_MODEL", "CITATION_VERIFIER_LLM_HOST_COMMAND",
    }:
        return "models"
    if "VERIFY" in name or "REASONING" in name or name in {
        "CITATION_VERIFIER_CONTEXT_PROFILE",
        "CITATION_VERIFIER_MAX_SOURCE_CHARS",
    }:
        return "verify"
    if "SIGNING" in name or "INTEGRITY" in name:
        return "integrity"
    if (
        "FETCH" in name
        or "OCR" in name
        or "HTTP" in name
    ):
        return "fetch"
    if (
        "RESOLVE" in name
        or name.endswith("_API_KEY")
        or name.endswith(("_API_TOKEN", "_ACCESS_TOKEN"))
        or name.startswith(("OPENALEX_", "SEMANTIC_SCHOLAR_", "LENS_", "BIBLIO_GLUTTON_", "COURTLISTENER_", "TAVILY_", "MOJEEK_"))
    ):
        return "resolution"
    return "advanced"


def _label(name: str) -> str:
    value = name.removeprefix("CITATION_VERIFIER_")
    return value.replace("_", " ").title()


def _is_secret(name: str) -> bool:
    return name in _SECRET_NAMES or name.endswith(_SECRET_SUFFIXES)


def _provider(name: str) -> str:
    for prefix, label in _PROVIDERS.items():
        if name.startswith(prefix):
            return label
    if name == "CITATION_VERIFIER_MODEL":
        return "Generic fallback"
    return "Host bridge" if name == "CITATION_VERIFIER_LLM_HOST_COMMAND" else ""


def _control(name: str, secret: bool) -> tuple[str, list[dict[str, str]]]:
    if secret:
        return "secret", []
    metadata = _CONTROL_METADATA.get(name)
    if metadata:
        return str(metadata["control"]), [
            {"value": value, "label": label} for value, label in metadata["choices"]
        ]
    if name in _BOOLEAN_NAMES:
        return "boolean", [{"value": "1", "label": "Enabled"}, {"value": "0", "label": "Disabled"}]
    if name in _INTEGER_NAMES:
        return "integer", []
    return "text", []


def load_settings_inventory(
    *,
    env_path: str | os.PathLike[str] | None,
    template_path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    """Project every versioned/local dotenv setting without exposing secrets."""
    process = os.environ if environ is None else environ
    file_values = _dotenv_values(env_path)
    template_values = _dotenv_values(template_path)
    template_order, descriptions = _dotenv_descriptions(template_path)
    process_names = {
        key for key in process if key.startswith("CITATION_VERIFIER_")
    }
    names = list(dict.fromkeys([*template_order, *file_values, *sorted(process_names - set(template_order) - set(file_values))]))
    rows = []
    for name in names:
        file_present = name in file_values
        if name in process:
            raw, source = str(process[name]), "environment"
        elif file_present:
            raw, source = file_values[name], "env_file"
        else:
            raw = template_values.get(name, "")
            source = "default" if raw else "unconfigured"
        secret = _is_secret(name)
        control, choices = _control(name, secret)
        rows.append({
            "name": name,
            "label": _label(name),
            "group": _group(name),
            "provider": _provider(name),
            "description": descriptions.get(name, ""),
            "control": control,
            "choices": choices,
            "value": "" if secret else raw,
            "display_value": "••••••••" if secret and raw else raw,
            "configured": bool(raw),
            "source": source,
            "secret": secret,
            "explicitly_empty": not bool(raw) and (name in process or file_present),
            "file_present": file_present,
            "shadowed_by_environment": name in process and file_present,
        })
    return rows


def effective_environment(
    *,
    env_path: str | os.PathLike[str] | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge a local dotenv beneath the current process environment."""
    values = _dotenv_values(env_path)
    values.update(
        {str(name): str(value) for name, value in (
            os.environ if environ is None else environ
        ).items()}
    )
    return values


def preview_verify_configuration(
    environ: Mapping[str, str],
    *,
    jury2_level: str | None = None,
) -> dict[str, object]:
    """Validate the effective Verify lanes without dispatching an LLM call."""
    values = dict(environ)
    if jury2_level is not None:
        values["CITATION_VERIFIER_VERIFY_JURY2_LEVEL"] = jury2_level
    metadata = registered_provider_metadata()
    specs = tuple(
        ProviderEnvSpec(
            name,
            key,
            model or GENERIC_MODEL_ENV,
            credentialless,
            capability,
        )
        for name, key, model, credentialless, capability in metadata
    )
    if not str(values.get("CITATION_VERIFIER_VERIFY_BACKENDS") or "").strip():
        return {
            "available": False,
            "reason": "no_verify_backend",
            "jury1": [],
            "jury2": [],
        }
    try:
        config = resolve_config(values, specs, run_id="desktop-config-preview")
    except ConfigError as exc:
        return {
            "available": False,
            "reason": str(exc),
            "jury1": [],
            "jury2": [],
        }
    jury1: list[str] = []
    jury2: list[str] = []
    for provider in config.providers:
        for lane in provider.lanes:
            label = f"{provider.name}:{lane.model}"
            if lane.jury1_eligible and label not in jury1:
                jury1.append(label)
            if lane.jury2_eligible and label not in jury2:
                jury2.append(label)
    return {
        "available": True,
        "reason": None,
        "jury1": jury1,
        "jury2": jury2 if config.jury2_level != "off" else [],
        "jury2_level": config.jury2_level,
    }


def configured_verify_backend_options(
    environ: Mapping[str, str],
    *,
    jury2_level: str | None = None,
) -> list[dict[str, object]]:
    """Return the configured backend/model choices accepted by Verify."""
    preview = preview_verify_configuration(environ, jury2_level=jury2_level)
    if not preview.get("available"):
        return []
    configured = {
        item.strip().lower()
        for item in str(
            environ.get("CITATION_VERIFIER_VERIFY_BACKENDS") or ""
        ).split(",")
        if item.strip()
    }
    model_env_by_backend = {
        name: model_env or GENERIC_MODEL_ENV
        for name, _key, model_env, _credentialless, _capability
        in registered_provider_metadata()
    }
    labels = [
        str(item)
        for item in (*preview.get("jury1", []), *preview.get("jury2", []))
    ]
    options = []
    for selector in dict.fromkeys(labels):
        backend, separator, model = selector.partition(":")
        if not separator or backend not in configured or not model:
            continue
        options.append({
            "selector": selector,
            "backend": backend,
            "model": model,
            "model_env": model_env_by_backend[backend],
            "label": f"{backend}: {model}",
        })
    return options


def available_verify_backend_options(
    environ: Mapping[str, str],
    *,
    jury2_level: str = "medium",
) -> list[dict[str, object]]:
    """Expose usable desktop lanes even before a backend policy is saved."""
    options = []
    for item in discover_verify_backend_options(environ):
        selector = str(item["selector"])
        roles = {
            "jury1": [selector],
            "jury2": [] if jury2_level == "off" else [selector],
        }
        if not selected_verify_environment(
            environ, roles, jury2_level=jury2_level
        ).get("available"):
            continue
        options.append({key: item[key] for key in (
            "selector", "backend", "model", "model_env", "label",
        )})
    return options


def discover_verify_backend_options(
    environ: Mapping[str, str],
) -> list[dict[str, object]]:
    """List locally usable provider/model choices without enabling Verify.

    Discovery is deliberately not policy resolution: callers must construct an
    explicit overlay and pass it through :func:`preview_verify_configuration`
    before treating a selection as usable.
    """
    options: list[dict[str, object]] = []
    configured_backends = {
        item.strip().lower()
        for item in str(environ.get("CITATION_VERIFIER_VERIFY_BACKENDS") or "").split(",")
        if item.strip()
    }
    configured_preview = preview_verify_configuration(environ)
    configured_jury1 = set(configured_preview.get("jury1") or ())
    configured_jury2 = set(configured_preview.get("jury2") or ())
    for name, key_env, model_env, credentialless, _capability in registered_provider_metadata():
        if name == "openai_compatible" and not str(
            environ.get("OPENAI_BASE_URL") or ""
        ).strip():
            continue
        model_name = model_env or GENERIC_MODEL_ENV
        raw_models = str(environ.get(model_name) or "").strip()
        credential_present = credentialless or bool(str(environ.get(key_env) or "").strip())
        if not raw_models or not credential_present:
            continue
        for model in (item.strip() for item in raw_models.split(",")):
            if model:
                selector = f"{name}:{model}"
                options.append({
                    "selector": selector,
                    "backend": name,
                    "model": model,
                    "model_env": model_name,
                    "label": f"{name}: {model}",
                    "configured": name in configured_backends,
                    "jury1_selected": selector in configured_jury1,
                    "jury2_selected": selector in configured_jury2,
                })
    return options


def selected_verify_environment(
    environ: Mapping[str, str],
    selected: Mapping[str, Sequence[str]],
    *,
    jury2_level: str,
) -> dict[str, object]:
    """Return an exact validated overlay for explicit Jury 1/Jury 2 choices."""
    if not isinstance(selected, Mapping) or any(
        name not in {"jury1", "jury2"} for name in selected
    ):
        return {"available": False, "reason": "no_verify_backend", "overlay": {}}

    def normalized_role(name: str) -> list[str] | None:
        raw = selected.get(name, ())
        if isinstance(raw, str) or not isinstance(raw, Sequence):
            return None
        return list(dict.fromkeys(
            str(item).strip() for item in raw if str(item).strip()
        ))

    jury1_selected = normalized_role("jury1")
    jury2_selected = normalized_role("jury2")
    if jury1_selected is None or jury2_selected is None:
        return {"available": False, "reason": "no_verify_backend", "overlay": {}}

    available_options = discover_verify_backend_options(environ)
    options = {str(item["selector"]): item for item in available_options}
    requested = set(jury1_selected) | set(jury2_selected)
    if any(item not in options for item in requested):
        return {"available": False, "reason": "no_verify_backend", "overlay": {}}

    # A single globally selected model is shared across both roles. This keeps
    # the one-model case viable while the desktop exposes independent choices
    # once more than one model is selected.
    if len(requested) == 1:
        sole_selector = next(iter(requested))
        jury1_selected = [sole_selector]
        jury2_selected = [sole_selector]

    effective_jury2_level = jury2_level
    if len(requested) > 1 and not jury2_selected:
        effective_jury2_level = "off"
    active_jury2 = [] if effective_jury2_level == "off" else jury2_selected
    jury1_set = set(jury1_selected)
    jury2_set = set(active_jury2)
    chosen = [
        item for item in available_options
        if str(item["selector"]) in jury1_set | jury2_set
    ]
    if not chosen:
        return {"available": False, "reason": "no_verify_backend", "overlay": {}}

    expected_jury1 = [
        str(item["selector"]) for item in available_options
        if str(item["selector"]) in jury1_set
    ]
    expected_jury2 = [
        str(item["selector"]) for item in available_options
        if str(item["selector"]) in jury2_set
    ] if effective_jury2_level != "off" else []
    jury1_only = [item for item in expected_jury1 if item not in jury2_set]
    jury2_only = [item for item in expected_jury2 if item not in jury1_set]
    overlay = {
        "CITATION_VERIFIER_VERIFY_BACKENDS": ",".join(dict.fromkeys(str(item["backend"]) for item in chosen)),
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": effective_jury2_level,
        "CITATION_VERIFIER_VERIFY_JURY1_ONLY": ",".join(jury1_only),
        "CITATION_VERIFIER_VERIFY_JURY2_ONLY": ",".join(jury2_only),
    }
    models_by_env: dict[str, list[str]] = {}
    for item in chosen:
        models_by_env.setdefault(str(item["model_env"]), []).append(str(item["model"]))
    overlay.update({name: ",".join(dict.fromkeys(models)) for name, models in models_by_env.items()})
    preview = preview_verify_configuration(
        {**environ, **overlay}, jury2_level=effective_jury2_level
    )
    if (
        not preview.get("available")
        or preview.get("jury1") != expected_jury1
        or preview.get("jury2") != expected_jury2
    ):
        return {"available": False, "reason": preview.get("reason") or "selection is not exact", "overlay": {}}
    return {**preview, "overlay": overlay}
