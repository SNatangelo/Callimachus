# core/app/settings/config.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only configuration projections for the desktop application."""
from __future__ import annotations

import os
import re
import urllib.parse
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
    "CALLIMACHUS_INSTITUTIONAL_FETCH",
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
    "OPENCODE_CLI_": "OpenCode CLI",
    "GEMINI_CLI_": "Gemini CLI", "ANTIGRAVITY_CLI_": "Antigravity CLI",
    "OPENAI_": "OpenAI / compatible", "ANTHROPIC_": "Anthropic",
    "GEMINI_": "Google Gemini", "OPENROUTER_": "OpenRouter",
    "DEEPSEEK_": "DeepSeek", "GROQ_": "Groq", "XAI_": "Grok (xAI)",
    "TYPESAFE_": "TypeSafe AI", "OLLAMA_": "Ollama",
    "FREETOKEN_": "FreeToken", "ZHIPUAI_": "GLM (ZhipuAI)",
    "MISTRAL_": "Mistral AI", "OPENCODE_": "OpenCode Zen",
    "CLAUDE_CLI_": "Claude Code", "CODEX_CLI_": "Codex CLI",
}
_MODEL_PROVIDER = {
    "OPENAI_MODEL": "openai",
    "DEEPSEEK_MODEL": "deepseek",
    "GROQ_MODEL": "groq",
    "XAI_MODEL": "xai",
    "ANTHROPIC_MODEL": "anthropic",
    "GEMINI_MODEL": "gemini",
    "OPENROUTER_MODEL": "openrouter",
    "TYPESAFE_MODEL": "typesafe",
    "OLLAMA_MODEL": "ollama",
    "FREETOKEN_MODEL": "freetoken",
    "ZHIPUAI_MODEL": "glm",
    "MISTRAL_MODEL": "mistral",
    "OPENCODE_MODEL": "opencode",
    "CLAUDE_CLI_MODEL": "claude_cli",
    "CODEX_CLI_MODEL": "codex_cli",
    "GEMINI_CLI_MODEL": "gemini_cli",
    "ANTIGRAVITY_CLI_MODEL": "antigravity",
    "OPENCODE_CLI_MODEL": "opencode_cli",
}
_CHECK_PROVIDER = {
    "OPENAI_API_KEY": "openai", "OPENAI_BASE_URL": "openai",
    "OPENAI_MODEL": "openai",
    "DEEPSEEK_API_KEY": "deepseek", "DEEPSEEK_MODEL": "deepseek",
    "GROQ_API_KEY": "groq", "GROQ_MODEL": "groq",
    "XAI_API_KEY": "xai", "XAI_MODEL": "xai",
    "ANTHROPIC_API_KEY": "anthropic",
    "ANTHROPIC_BASE_URL": "anthropic", "ANTHROPIC_MODEL": "anthropic",
    "GEMINI_API_KEY": "gemini", "GEMINI_MODEL": "gemini",
    "OPENROUTER_API_KEY": "openrouter", "OPENROUTER_MODEL": "openrouter",
    "TYPESAFE_API_KEY": "typesafe", "TYPESAFE_MODEL": "typesafe",
    "OLLAMA_API_KEY": "ollama", "OLLAMA_HOST": "ollama", "OLLAMA_MODEL": "ollama",
    "FREETOKEN_API_KEY": "freetoken", "FREETOKEN_HOST": "freetoken", "FREETOKEN_MODEL": "freetoken",
    "ZHIPUAI_API_KEY": "glm", "ZHIPUAI_MODEL": "glm",
    "MISTRAL_API_KEY": "mistral", "MISTRAL_MODEL": "mistral",
    "OPENCODE_API_KEY": "opencode", "OPENCODE_MODEL": "opencode",
    "CLAUDE_CLI_MODEL": "claude_cli", "CODEX_CLI_MODEL": "codex_cli",
    "GEMINI_CLI_MODEL": "gemini_cli", "ANTIGRAVITY_CLI_MODEL": "antigravity",
    "OPENCODE_CLI_MODEL": "opencode_cli",
    "CITATION_VERIFIER_LLM_HOST_COMMAND": "host",
    "CITATION_VERIFIER_MODEL": "host",
}
_SAFE_CATALOG_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-\[\]()]{0,199}$")

# The GUI exposes these choices directly. Engine defaults apply only to the
# remaining versioned Callimachus settings, which are not GUI controls.
_GUI_OPTION_NAMES = frozenset({
    "CITATION_VERIFIER_ACCURACY",
    "CITATION_VERIFIER_VERIFY_TABLE_CITATIONS",
    "CITATION_VERIFIER_VERIFY_BACKENDS",
    "CITATION_VERIFIER_VERIFY_JURY1_ONLY",
    "CITATION_VERIFIER_VERIFY_JURY2_ONLY",
    "CITATION_VERIFIER_VERIFY_JURY2_LEVEL",
    "CITATION_VERIFIER_REASONING",
    "CITATION_VERIFIER_REASONING_EFFORT",
    "CITATION_VERIFIER_MAILTO",
    "CITATION_VERIFIER_OCR_LANG",
    "CITATION_VERIFIER_OA_ALTERNATES",
    "CITATION_VERIFIER_WAYBACK",
    "CITATION_VERIFIER_PERMA",
    "CITATION_VERIFIER_SIGNING_KEY_FILE",
    "CITATION_VERIFIER_GUIDED_SEARCH_URL",
    "CITATION_VERIFIER_GUIDED_SEARCH_QUERY",
    "CITATION_VERIFIER_LLM_HOST_COMMAND",
    "CITATION_VERIFIER_MODEL",
})


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
        or name.startswith("CALLIMACHUS_INSTITUTIONAL_")
        or name == "CALLIMACHUS_INSTITUTION"
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
    if name.startswith("CALLIMACHUS_INSTITUTION"):
        value = name.removeprefix("CALLIMACHUS_")
    return value.replace("_", " ").title()


def _is_secret(name: str) -> bool:
    from core.app.settings.credentials import is_secret_setting

    return is_secret_setting(name)


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
    from core.app.runtime_paths import is_frozen, settings_template_file

    frozen = is_frozen()
    if frozen and template_path is None:
        template_path = settings_template_file()
    process = os.environ if environ is None else environ
    file_values = _dotenv_values(env_path)
    template_values = _dotenv_values(template_path)
    template_order, descriptions = _dotenv_descriptions(template_path)
    vault_values: dict[str, str] = {}
    if frozen:
        from core.app.settings.credentials import load_secret_values

        known_secrets = {
            name for name in template_values if _is_secret(name)
        } | {"CITATION_VERIFIER_SIGNING_KEY"}
        vault_values = load_secret_values(known_secrets - set(process))
    process_names = {
        key for key in process if key.startswith("CITATION_VERIFIER_")
    }
    names = list(dict.fromkeys([
        *template_order,
        *file_values,
        *sorted(process_names - set(template_order) - set(file_values)),
        *sorted(set(vault_values) - set(template_order) - set(file_values)),
    ]))
    rows = []
    for name in names:
        secret = _is_secret(name)
        file_present = name in file_values and not (frozen and secret)
        if name in process:
            raw, source = str(process[name]), "environment"
        elif frozen and secret and name in vault_values:
            raw, source = vault_values[name], "credential_store"
        elif frozen and secret:
            raw, source = "", "unconfigured"
        elif file_present:
            raw, source = file_values[name], "env_file"
        else:
            raw = template_values.get(name, "")
            source = "default" if raw else "unconfigured"
        control, choices = _control(name, secret)
        rows.append({
            "name": name,
            "label": _label(name),
            "group": _group(name),
            "provider": _provider(name),
            **({"model_provider": _MODEL_PROVIDER[name]} if name in _MODEL_PROVIDER else {}),
            **({"check_provider": _CHECK_PROVIDER[name]} if name in _CHECK_PROVIDER else {}),
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


def gui_engine_defaults(
    template_path: str | os.PathLike[str] | None,
) -> dict[str, str]:
    """Return versioned engine settings the GUI must pin to template defaults."""
    return {
        name: value
        for name, value in _dotenv_values(template_path).items()
        if name.startswith("CITATION_VERIFIER_") and name not in _GUI_OPTION_NAMES
    }


def gui_dispatch_defaults(
    defaults: Mapping[str, str],
    launch_overlay: Mapping[str, object],
) -> dict[str, str]:
    """Omit the GUI token cap only when every selected provider supports it."""
    result = dict(defaults)
    selected_names = tuple(dict.fromkeys(
        name.strip().lower()
        for name in str(
            launch_overlay.get("CITATION_VERIFIER_VERIFY_BACKENDS") or ""
        ).split(",")
        if name.strip()
    ))
    if not selected_names:
        return result

    import core.verify.backends  # noqa: F401
    from core.verify.backends._registry import all_specs

    specs = {spec.name: spec for spec in all_specs() if spec.call is not None}
    selected_specs = [specs.get(name) for name in selected_names]
    if (
        all(spec is not None for spec in selected_specs)
        and all(spec.supports_omitted_max_tokens for spec in selected_specs if spec is not None)
    ):
        token_name = "CITATION_VERIFIER_VERIFY_MAX_TOKENS"
        if token_name in result:
            result[token_name] = "none"
    return result


def load_gui_settings_inventory(
    *,
    env_path: str | os.PathLike[str] | None,
    template_path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    """Return GUI settings, keeping source-only key files out of packaged apps."""
    from core.app.runtime_paths import is_frozen

    rows = load_settings_inventory(
        env_path=env_path, template_path=template_path, environ=environ,
    )
    if is_frozen():
        rows = [row for row in rows if row.get("name") != "CITATION_VERIFIER_SIGNING_KEY_FILE"]
    defaults = gui_engine_defaults(template_path)
    for row in rows:
        name = str(row.get("name") or "")
        if name not in defaults:
            continue
        value = defaults[name]
        secret = bool(row.get("secret"))
        row.update({
            "value": "" if secret else value,
            "display_value": "••••••••" if secret and value else value,
            "configured": bool(value),
            "source": "default",
            "explicitly_empty": False,
            "shadowed_by_environment": False,
            "locked": True,
        })
    return rows


def effective_environment(
    *,
    env_path: str | os.PathLike[str] | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge a local dotenv beneath the current process environment."""
    from core.app.runtime_paths import is_frozen, saved_secret_values

    values = _dotenv_values(env_path)
    process = os.environ if environ is None else environ
    if is_frozen():
        values = {
            name: value for name, value in values.items()
            if not _is_secret(name)
        }
        values.update(saved_secret_values(overrides=process))
    values.update(
        {str(name): str(value) for name, value in process.items()}
    )
    return values


def institutional_access_config(
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return the sanitized institutional Fetch configuration for the GUI."""
    from core.fetch.institutional_access import config

    return config(environ)


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
    openai_base = str(environ.get("OPENAI_BASE_URL") or "").strip()
    try:
        openai_base_host = urllib.parse.urlsplit(openai_base).hostname if openai_base else None
    except ValueError:
        openai_base_host = None
    for name, key_env, model_env, credentialless, _capability in registered_provider_metadata():
        if name == "ollama_local" and str(environ.get("OLLAMA_API_KEY") or "").strip():
            continue
        if name == "freetoken" and str(environ.get("FREETOKEN_API_KEY") or "").strip():
            continue
        if name == "openai" and openai_base_host not in {None, "api.openai.com"}:
            continue
        if name == "openai_compatible" and (
            not openai_base or openai_base_host == "api.openai.com"
        ):
            continue
        model_name = model_env or GENERIC_MODEL_ENV
        raw_models = str(environ.get(model_name) or environ.get(GENERIC_MODEL_ENV) or "").strip()
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

    effective_jury2_level = jury2_level
    if not jury2_selected:
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


def catalog_verify_preview(
    environ: Mapping[str, str],
    catalogs: Mapping[str, Mapping[str, object]],
    selected: Mapping[str, Sequence[str]],
    *,
    jury2_level: str,
) -> dict[str, object]:
    """Preview selected catalog models without persisting provider inventories."""
    if not isinstance(environ, Mapping) or not isinstance(catalogs, Mapping):
        return {"available": False, "reason": "no_verify_backend", "overlay": {}}
    values = dict(environ)
    safe_settings = {
        "openai": {"base_url": "OPENAI_BASE_URL"},
        "openai_compatible": {"base_url": "OPENAI_BASE_URL"},
        "anthropic": {"base_url": "ANTHROPIC_BASE_URL"},
        "ollama": {"host": "OLLAMA_HOST"},
        "ollama_local": {"host": "OLLAMA_HOST"},
        "freetoken": {"host": "FREETOKEN_HOST"},
        "freetoken_authenticated": {"host": "FREETOKEN_HOST"},
    }
    allowed_model_envs = {
        GENERIC_MODEL_ENV, "OPENAI_MODEL", "ANTHROPIC_MODEL", "GEMINI_MODEL",
        "OPENROUTER_MODEL", "TYPESAFE_MODEL", "OLLAMA_MODEL", "FREETOKEN_MODEL",
        "ZHIPUAI_MODEL", "MISTRAL_MODEL", "OPENCODE_MODEL", "CLAUDE_CLI_MODEL",
        "CODEX_CLI_MODEL",
    }
    allowed_model_envs.update(
        model_env for _name, _key, model_env, _credentialless, _capability
        in registered_provider_metadata() if model_env
    )
    catalog_models: dict[str, list[str]] = {}
    accepted_selectors = {
        str(item["selector"]) for item in discover_verify_backend_options(environ)
    }
    for provider, catalog in catalogs.items():
        if not isinstance(provider, str) or not isinstance(catalog, Mapping):
            continue
        if catalog.get("provider") != provider or catalog.get("ok") is not True:
            continue
        models = catalog.get("models")
        model_env = catalog.get("model_env")
        if (
            not isinstance(model_env, str)
            or model_env not in allowed_model_envs
            or not isinstance(models, Sequence)
            or isinstance(models, (str, bytes))
            or any(not isinstance(item, str) for item in models)
        ):
            return {"available": False, "reason": "invalid_catalog", "overlay": {}}
        if any(
            not item.strip() or not _SAFE_CATALOG_MODEL.fullmatch(item.strip())
            for item in models
        ):
            return {"available": False, "reason": "invalid_catalog", "overlay": {}}
        clean_models = [item.strip() for item in models if item.strip()]
        if clean_models:
            catalog_models.setdefault(model_env, []).extend(clean_models)
        options = catalog.get("options")
        if isinstance(options, Sequence) and not isinstance(options, (str, bytes)):
            for option in options:
                if not isinstance(option, Mapping):
                    continue
                backend = option.get("backend")
                model = option.get("model")
                selector = option.get("selector")
                if (
                    isinstance(backend, str) and isinstance(model, str)
                    and isinstance(selector, str) and model in clean_models
                    and selector == f"{backend}:{model}"
                ):
                    accepted_selectors.add(selector)
        settings = catalog.get("settings")
        if isinstance(settings, Mapping):
            env_fields = safe_settings.get(provider, {})
            for field, env_name in env_fields.items():
                value = settings.get(field)
                if isinstance(value, str) and value.strip():
                    from core.app.providers.discovery import normalize_endpoint
                    try:
                        values[env_name] = normalize_endpoint(value)
                    except ValueError:
                        return {"available": False, "reason": "invalid_catalog", "overlay": {}}
                elif field in settings:
                    return {"available": False, "reason": "invalid_catalog", "overlay": {}}
    if not isinstance(selected, Mapping):
        return {"available": False, "reason": "no_verify_backend", "overlay": {}}
    for role in ("jury1", "jury2"):
        raw = selected.get(role, ())
        if isinstance(raw, str) or not isinstance(raw, Sequence):
            return {"available": False, "reason": "no_verify_backend", "overlay": {}}
        if any(str(selector).strip() not in accepted_selectors for selector in raw if str(selector).strip()):
            return {"available": False, "reason": "no_verify_backend", "overlay": {}}
    for name, models in catalog_models.items():
        values[name] = ",".join(dict.fromkeys(models))
    preview = selected_verify_environment(values, selected, jury2_level=jury2_level)
    if not preview.get("available"):
        return preview
    overlay = dict(preview.get("overlay") or {})
    selected_backends = set(
        item.strip().lower()
        for item in str(overlay.get("CITATION_VERIFIER_VERIFY_BACKENDS") or "").split(",")
        if item.strip()
    )
    for provider in selected_backends:
        catalog = next((item for item in catalogs.values()
                        if isinstance(item, Mapping)
                        and any(isinstance(option, Mapping) and option.get("backend") == provider
                                for option in item.get("options", ()))), None)
        if not isinstance(catalog, Mapping) or catalog.get("ok") is not True:
            continue
        settings = catalog.get("settings")
        if not isinstance(settings, Mapping):
            continue
        for field, env_name in safe_settings.get(provider, {}).items():
            value = settings.get(field)
            if isinstance(value, str) and value.strip():
                from core.app.providers.discovery import normalize_endpoint
                try:
                    overlay[env_name] = normalize_endpoint(value)
                except ValueError:
                    return {"available": False, "reason": "invalid_catalog", "overlay": {}}
    return {**preview, "overlay": overlay}
