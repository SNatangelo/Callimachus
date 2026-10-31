# tests/test_desktop_config.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest

from core.app.settings.config import (
    available_verify_backend_options,
    catalog_verify_preview,
    configured_verify_backend_options,
    discover_verify_backend_options,
    gui_engine_defaults,
    gui_dispatch_defaults,
    load_settings_inventory,
    load_gui_settings_inventory,
    preview_verify_configuration,
    selected_verify_environment,
)


def test_settings_inventory_masks_secrets_and_preserves_effective_source(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text(
        "CITATION_VERIFIER_ACCURACY=standard\n"
        "OPENAI_API_KEY=\n"
        "CITATION_VERIFIER_FETCH_WORKERS=4\n",
        encoding="utf-8",
    )
    local = tmp_path / ".env"
    local.write_text(
        "OPENAI_API_KEY=local-secret\n"
        "CITATION_VERIFIER_ACCURACY=abstract\n",
        encoding="utf-8",
    )

    rows = load_settings_inventory(
        env_path=local,
        template_path=template,
        environ={"CITATION_VERIFIER_ACCURACY": "maximum"},
    )
    by_name = {row["name"]: row for row in rows}

    assert by_name["OPENAI_API_KEY"]["value"] == ""
    assert by_name["OPENAI_API_KEY"]["display_value"] == "••••••••"
    assert "local-secret" not in repr(rows)
    assert by_name["CITATION_VERIFIER_ACCURACY"]["value"] == "maximum"
    assert by_name["CITATION_VERIFIER_ACCURACY"]["source"] == "environment"
    assert by_name["CITATION_VERIFIER_FETCH_WORKERS"]["group"] == "fetch"


def test_settings_inventory_identifies_provider_model_and_check_fields(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text(
        "OPENAI_API_KEY=\nOPENAI_BASE_URL=\nOPENAI_MODEL=\n"
        "DEEPSEEK_API_KEY=\nDEEPSEEK_MODEL=\nGROQ_API_KEY=\nGROQ_MODEL=\n"
        "XAI_API_KEY=\nXAI_MODEL=\n"
        "OLLAMA_HOST=\nOLLAMA_MODEL=\nFREETOKEN_API_KEY=\nFREETOKEN_MODEL=\n"
        "CLAUDE_CLI_MODEL=\nCODEX_CLI_MODEL=\n",
        encoding="utf-8",
    )
    rows = load_settings_inventory(
        env_path=None, template_path=template,
        environ={"FREETOKEN_API_KEY": "private-value", "XAI_API_KEY": "xai-secret"}
    )
    by_name = {row["name"]: row for row in rows}
    assert by_name["OPENAI_API_KEY"]["check_provider"] == "openai"
    assert by_name["OPENAI_BASE_URL"]["check_provider"] == "openai"
    assert by_name["OPENAI_MODEL"]["model_provider"] == "openai"
    assert by_name["DEEPSEEK_API_KEY"]["check_provider"] == "deepseek"
    assert by_name["DEEPSEEK_MODEL"]["check_provider"] == "deepseek"
    assert by_name["DEEPSEEK_MODEL"]["model_provider"] == "deepseek"
    assert by_name["GROQ_API_KEY"]["check_provider"] == "groq"
    assert by_name["GROQ_MODEL"]["check_provider"] == "groq"
    assert by_name["GROQ_MODEL"]["model_provider"] == "groq"
    assert by_name["XAI_API_KEY"]["check_provider"] == "xai"
    assert by_name["XAI_MODEL"]["check_provider"] == "xai"
    assert by_name["XAI_MODEL"]["model_provider"] == "xai"
    assert by_name["OLLAMA_MODEL"]["model_provider"] == "ollama"
    assert by_name["FREETOKEN_MODEL"]["model_provider"] == "freetoken"
    assert by_name["CLAUDE_CLI_MODEL"]["model_provider"] == "claude_cli"
    assert by_name["CODEX_CLI_MODEL"]["model_provider"] == "codex_cli"
    assert "private-value" not in repr(rows)
    assert by_name["FREETOKEN_API_KEY"]["value"] == ""
    assert by_name["FREETOKEN_API_KEY"]["display_value"] == "••••••••"
    assert by_name["XAI_API_KEY"]["value"] == ""
    assert by_name["XAI_API_KEY"]["display_value"] == "••••••••"
    assert "xai-secret" not in repr(rows)


def test_setup_environment_template_keeps_dedicated_provider_credentials_separate():
    from core.app.commands.configure import render_env

    rendered = render_env("user@example.test", "books-key", "standard")
    for setting in (
        "DEEPSEEK_API_KEY=", "DEEPSEEK_MODEL=",
        "GROQ_API_KEY=", "GROQ_MODEL=",
        "XAI_API_KEY=", "XAI_MODEL=",
    ):
        assert setting in rendered
    assert "OPENAI_BASE_URL=" in rendered
    assert "OPENAI_API_KEY=" in rendered
    assert "OPENAI_MODEL=" in rendered
    assert "DeepSeek / OpenAI-compatible" not in rendered


def test_catalog_preview_uses_accepted_models_and_returns_only_selected_overlay():
    environ = {"CITATION_VERIFIER_MODEL": "saved-generic", "FREETOKEN_HOST": ""}
    before = dict(environ)
    catalog = {
        "provider": "freetoken", "ok": True, "model_env": "FREETOKEN_MODEL",
        "models": ["served-model"], "settings": {"host": "http://127.0.0.1:1919"},
        "options": [{
            "backend": "freetoken", "model": "served-model",
            "selector": "freetoken:served-model",
        }],
    }
    preview = catalog_verify_preview(
        environ, {"freetoken": catalog},
        {"jury1": ["freetoken:served-model"], "jury2": ["freetoken:served-model"]},
        jury2_level="medium",
    )
    assert preview["available"] is True
    assert preview["overlay"]["FREETOKEN_MODEL"] == "served-model"
    assert preview["overlay"]["FREETOKEN_HOST"] == "http://127.0.0.1:1919"
    assert preview["overlay"]["CITATION_VERIFIER_VERIFY_BACKENDS"] == "freetoken"
    assert "models" not in preview and "options" not in preview
    assert environ == before


def test_catalog_preview_accepts_tagged_ollama_model_and_exact_roles():
    catalog = {
        "provider": "ollama", "ok": True, "model_env": "OLLAMA_MODEL",
        "models": ["llama3:8b"], "settings": {"host": "http://127.0.0.1:11434"},
        "options": [{
            "backend": "ollama_local", "model": "llama3:8b",
            "selector": "ollama_local:llama3:8b",
        }],
    }
    preview = catalog_verify_preview(
        {"OLLAMA_API_KEY": ""}, {"ollama": catalog},
        {"jury1": ["ollama_local:llama3:8b"], "jury2": ["ollama_local:llama3:8b"]},
        jury2_level="medium",
    )
    assert preview["available"] is True
    assert preview["overlay"]["OLLAMA_MODEL"] == "llama3:8b"
    assert preview["overlay"]["CITATION_VERIFIER_VERIFY_BACKENDS"] == "ollama_local"


def test_local_routes_are_not_offered_when_provider_key_is_configured():
    ollama = discover_verify_backend_options({
        "OLLAMA_API_KEY": "ollama-key", "OLLAMA_MODEL": "cloud-model",
        "OLLAMA_HOST": "https://ollama.com",
    })
    assert {item["backend"] for item in ollama} == {"ollama"}
    freetoken = discover_verify_backend_options({
        "FREETOKEN_API_KEY": "ft-key", "FREETOKEN_MODEL": "hosted-model",
        "FREETOKEN_HOST": "https://ft.example.test",
    })
    assert {item["backend"] for item in freetoken} == {"freetoken_authenticated"}


def test_catalog_preview_preserves_existing_configuration_without_catalogs():
    environ = {"OPENAI_API_KEY": "saved-key", "CITATION_VERIFIER_MODEL": "saved-model"}
    preview = catalog_verify_preview(
        environ, {}, {"jury1": ["openai:saved-model"], "jury2": []},
        jury2_level="off",
    )
    assert preview["available"] is True
    assert preview["jury1"] == ["openai:saved-model"]
    assert preview["overlay"]["OPENAI_MODEL"] == "saved-model"


def test_catalog_preview_separates_models_for_two_http_providers():
    catalogs = {
        provider: {
            "provider": provider, "ok": True, "model_env": model_env,
            "models": [model], "options": [{
                "backend": provider, "model": model, "selector": f"{provider}:{model}",
            }],
        }
        for provider, model_env, model in (
            ("openai", "OPENAI_MODEL", "gpt-test"),
            ("anthropic", "ANTHROPIC_MODEL", "claude-test"),
        )
    }
    preview = catalog_verify_preview(
        {"OPENAI_API_KEY": "openai-key", "ANTHROPIC_API_KEY": "anthropic-key"},
        catalogs, {"jury1": ["openai:gpt-test"], "jury2": ["anthropic:claude-test"]},
        jury2_level="medium",
    )
    assert preview["available"] is True
    assert preview["jury1"] == ["openai:gpt-test"]
    assert preview["jury2"] == ["anthropic:claude-test"]
    assert preview["overlay"]["OPENAI_MODEL"] == "gpt-test"
    assert preview["overlay"]["ANTHROPIC_MODEL"] == "claude-test"
    assert "CITATION_VERIFIER_MODEL" not in preview["overlay"]


def test_settings_inventory_preserves_order_provenance_controls_and_resolver_group(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text(
        "# The optional Google Books credential.\nGOOGLE_BOOKS_API_KEY=\n"
        "# Keep no second judge.\nCITATION_VERIFIER_VERIFY_JURY2_LEVEL=off\n"
        "# Turn OCR on or off.\nCITATION_VERIFIER_OCR_AUTO=1\n"
        "# A numeric fetch limit.\nCITATION_VERIFIER_FETCH_WORKERS=4\n"
        "CORE_API_KEY=template-key\n",
        encoding="utf-8",
    )
    local = tmp_path / ".env"
    local.write_text("GOOGLE_BOOKS_API_KEY=\nCORE_API_KEY=local-secret\n", encoding="utf-8")

    rows = load_settings_inventory(
        env_path=local, template_path=template,
        environ={"CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "high"},
    )
    assert [row["name"] for row in rows] == [
        "GOOGLE_BOOKS_API_KEY", "CITATION_VERIFIER_VERIFY_JURY2_LEVEL",
        "CITATION_VERIFIER_OCR_AUTO", "CITATION_VERIFIER_FETCH_WORKERS", "CORE_API_KEY",
    ]
    by_name = {row["name"]: row for row in rows}
    google = by_name["GOOGLE_BOOKS_API_KEY"]
    assert google["group"] == "resolution"
    assert google["source"] == "env_file"
    assert google["configured"] is False
    assert google["explicitly_empty"] is True
    assert google["file_present"] is True
    assert google["value"] == google["display_value"] == ""
    core = by_name["CORE_API_KEY"]
    assert core["group"] == "resolution"
    assert core["value"] == "" and core["display_value"] == "••••••••"
    assert "local-secret" not in repr(rows)
    jury = by_name["CITATION_VERIFIER_VERIFY_JURY2_LEVEL"]
    assert jury["source"] == "environment" and jury["shadowed_by_environment"] is False
    assert jury["control"] == "enum" and jury["choices"][0] == {"value": "off", "label": "Off"}
    assert by_name["CITATION_VERIFIER_OCR_AUTO"]["control"] == "boolean"
    assert by_name["CITATION_VERIFIER_FETCH_WORKERS"]["control"] == "integer"
    assert google["description"] == "The optional Google Books credential."


def test_empty_template_secret_is_unconfigured_not_a_default(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text("OPENAI_API_KEY=\nCITATION_VERIFIER_MODEL=default-model\n", encoding="utf-8")
    rows = load_settings_inventory(env_path=None, template_path=template, environ={})
    by_name = {row["name"]: row for row in rows}
    assert by_name["OPENAI_API_KEY"]["source"] == "unconfigured"
    assert by_name["OPENAI_API_KEY"]["configured"] is False
    assert by_name["CITATION_VERIFIER_MODEL"]["source"] == "default"
    assert by_name["CITATION_VERIFIER_MODEL"]["group"] == "models"
    assert by_name["CITATION_VERIFIER_MODEL"]["provider"] == "Generic fallback"


def test_settings_inventory_keeps_token_limit_and_key_file_visible(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text(
        "CITATION_VERIFIER_VERIFY_MAX_TOKENS=4000\n"
        "CITATION_VERIFIER_SIGNING_KEY_FILE=./protected/signing.key\n"
        "OPENAI_API_KEY=\n",
        encoding="utf-8",
    )

    rows = {
        row["name"]: row
        for row in load_settings_inventory(
            env_path=None, template_path=template, environ={}
        )
    }

    token_limit = rows["CITATION_VERIFIER_VERIFY_MAX_TOKENS"]
    assert token_limit["secret"] is False
    assert token_limit["control"] == "integer"
    assert token_limit["value"] == token_limit["display_value"] == "4000"
    assert rows["CITATION_VERIFIER_SIGNING_KEY_FILE"]["secret"] is False
    assert rows["CITATION_VERIFIER_SIGNING_KEY_FILE"]["control"] == "text"
    assert rows["CITATION_VERIFIER_SIGNING_KEY_FILE"]["value"] == "./protected/signing.key"
    assert rows["OPENAI_API_KEY"]["secret"] is True
    assert rows["OPENAI_API_KEY"]["source"] == "unconfigured"


@pytest.mark.parametrize("frozen", [False, True])
def test_gui_signing_key_file_setting_is_only_shown_for_source_runs(tmp_path, monkeypatch, frozen):
    from core.app import runtime_paths
    from core.app.settings import credentials as credential_store

    monkeypatch.setattr(runtime_paths, "is_frozen", lambda: frozen)
    monkeypatch.setattr(credential_store, "load_secret_values", lambda _names: {})
    template = tmp_path / "settings.defaults"
    template.write_text(
        "CITATION_VERIFIER_SIGNING_KEY_FILE=./protected/signing.key\n"
        "CITATION_VERIFIER_MAILTO=reader@example.test\n",
        encoding="utf-8",
    )
    rows = load_gui_settings_inventory(env_path=None, template_path=template, environ={})
    names = {row["name"] for row in rows}
    assert ("CITATION_VERIFIER_SIGNING_KEY_FILE" in names) is (not frozen)
    assert "CITATION_VERIFIER_MAILTO" in names


def test_gui_engine_defaults_are_versioned_and_inventory_locks_their_overlay(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text(
        "CITATION_VERIFIER_ACCURACY=standard\n"
        "CITATION_VERIFIER_VERIFY_TABLE_CITATIONS=0\n"
        "CITATION_VERIFIER_VERIFY_BACKENDS=\n"
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL=\n"
        "CITATION_VERIFIER_VERIFY_JURY1_ONLY=\n"
        "CITATION_VERIFIER_VERIFY_JURY2_ONLY=\n"
        "CITATION_VERIFIER_REASONING=auto\n"
        "CITATION_VERIFIER_REASONING_EFFORT=medium\n"
        "CITATION_VERIFIER_MAILTO=\n"
        "CITATION_VERIFIER_OCR_LANG=eng\n"
        "CITATION_VERIFIER_OA_ALTERNATES=1\n"
        "CITATION_VERIFIER_WAYBACK=1\n"
        "CITATION_VERIFIER_PERMA=1\n"
        "CITATION_VERIFIER_SIGNING_KEY_FILE=\n"
        "CITATION_VERIFIER_GUIDED_SEARCH_URL=https://example.test/?q={query}\n"
        "CITATION_VERIFIER_GUIDED_SEARCH_QUERY={raw_entry}\n"
        "CITATION_VERIFIER_LLM_HOST_COMMAND=\n"
        "CITATION_VERIFIER_MODEL=\n"
        "CITATION_VERIFIER_VERIFY_CONTEXT_MODE=auto\n"
        "CITATION_VERIFIER_CONTEXT_PROFILE=large\n"
        "CITATION_VERIFIER_MAX_SOURCE_CHARS=\n"
        "CITATION_VERIFIER_VERIFY_MAX_TOKENS=4000\n"
        "CITATION_VERIFIER_FETCH_WORKERS=3\n",
        encoding="utf-8",
    )
    local = tmp_path / ".env"
    local.write_text(
        "CITATION_VERIFIER_VERIFY_CONTEXT_MODE=extractive_rag\n"
        "CITATION_VERIFIER_VERIFY_TABLE_CITATIONS=1\n"
        "CITATION_VERIFIER_CONTEXT_PROFILE=small\n"
        "CITATION_VERIFIER_MAX_SOURCE_CHARS=12000\n"
        "CITATION_VERIFIER_VERIFY_MAX_TOKENS=9000\n",
        encoding="utf-8",
    )

    defaults = gui_engine_defaults(template)
    assert defaults == {
        "CITATION_VERIFIER_VERIFY_CONTEXT_MODE": "auto",
        "CITATION_VERIFIER_CONTEXT_PROFILE": "large",
        "CITATION_VERIFIER_MAX_SOURCE_CHARS": "",
        "CITATION_VERIFIER_VERIFY_MAX_TOKENS": "4000",
        "CITATION_VERIFIER_FETCH_WORKERS": "3",
    }
    rows = {
        row["name"]: row
        for row in load_gui_settings_inventory(
            env_path=local,
            template_path=template,
            environ={
                "CITATION_VERIFIER_VERIFY_CONTEXT_MODE": "full_text",
                "CITATION_VERIFIER_ACCURACY": "abstract",
                "CITATION_VERIFIER_MAILTO": "contact@example.test",
                "CITATION_VERIFIER_RUNTIME_ONLY": "not-versioned",
            },
        )
    }

    assert rows["CITATION_VERIFIER_VERIFY_CONTEXT_MODE"]["value"] == "auto"
    assert rows["CITATION_VERIFIER_VERIFY_CONTEXT_MODE"]["locked"] is True
    assert rows["CITATION_VERIFIER_VERIFY_CONTEXT_MODE"]["source"] == "default"
    assert rows["CITATION_VERIFIER_CONTEXT_PROFILE"]["value"] == "large"
    assert rows["CITATION_VERIFIER_MAX_SOURCE_CHARS"]["value"] == ""
    assert rows["CITATION_VERIFIER_VERIFY_MAX_TOKENS"]["value"] == "4000"
    assert rows["CITATION_VERIFIER_VERIFY_MAX_TOKENS"]["locked"] is True
    assert rows["CITATION_VERIFIER_ACCURACY"]["value"] == "abstract"
    assert rows["CITATION_VERIFIER_VERIFY_TABLE_CITATIONS"]["value"] == "1"
    assert rows["CITATION_VERIFIER_VERIFY_TABLE_CITATIONS"]["control"] == "boolean"
    assert rows["CITATION_VERIFIER_VERIFY_TABLE_CITATIONS"].get("locked") is not True
    assert rows["CITATION_VERIFIER_MAILTO"]["value"] == "contact@example.test"
    assert rows["CITATION_VERIFIER_RUNTIME_ONLY"].get("locked") is not True
    assert len(rows) == 24


def test_gui_dispatch_omits_token_cap_only_for_selected_capable_providers():
    from core.verify.claim_evidence.adapters.llm.registry import (
        RegistryError,
        provider_registry,
    )

    defaults = {"CITATION_VERIFIER_VERIFY_MAX_TOKENS": "4000"}
    for backend in ("anthropic", "codex_cli", "openai"):
        dispatch = gui_dispatch_defaults(
            defaults,
            {"CITATION_VERIFIER_VERIFY_BACKENDS": backend},
        )
        assert dispatch["CITATION_VERIFIER_VERIFY_MAX_TOKENS"] == "4000"
        assert provider_registry([backend], max_tokens=4000).get(backend)
        with pytest.raises(RegistryError, match="requires a max_tokens budget"):
            provider_registry([backend], max_tokens=None)

    capable = gui_dispatch_defaults(
        defaults,
        {"CITATION_VERIFIER_VERIFY_BACKENDS": "openai_compatible"},
    )
    assert capable["CITATION_VERIFIER_VERIFY_MAX_TOKENS"] == "none"
    assert provider_registry(["openai_compatible"], max_tokens=None).get("openai_compatible")

    mixed = gui_dispatch_defaults(
        defaults,
        {"CITATION_VERIFIER_VERIFY_BACKENDS": "openai_compatible,anthropic"},
    )
    assert mixed["CITATION_VERIFIER_VERIFY_MAX_TOKENS"] == "4000"
    assert gui_dispatch_defaults(defaults, {}) == defaults
    assert gui_dispatch_defaults(
        defaults, {"CITATION_VERIFIER_VERIFY_BACKENDS": "unknown-provider"}
    ) == defaults


def test_verify_preview_reports_deterministic_only_without_backend():
    assert preview_verify_configuration(
        {"CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium"}
    ) == {
        "available": False,
        "reason": "no_verify_backend",
        "jury1": [],
        "jury2": [],
    }


def test_discovered_models_require_explicit_exact_selection():
    environment = {"FREETOKEN_MODEL": "local-a,local-b"}
    options = discover_verify_backend_options(environment)

    assert [item["selector"] for item in options] == [
        "freetoken:local-a", "freetoken:local-b",
    ]
    assert not any(item["configured"] for item in options)
    selected = selected_verify_environment(
        environment,
        {"jury1": ["freetoken:local-a"], "jury2": ["freetoken:local-a"]},
        jury2_level="medium",
    )
    assert selected["available"] is True
    assert selected["overlay"] == {
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium",
        "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "",
        "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "",
        "FREETOKEN_MODEL": "local-a",
    }
    assert selected["jury1"] == ["freetoken:local-a"]
    assert selected["jury2"] == ["freetoken:local-a"]


def test_dedicated_provider_namespaces_allow_openai_deepseek_groq_and_xai_together():
    environment = {
        "CITATION_VERIFIER_VERIFY_BACKENDS": "openai,deepseek,groq,xai",
        "OPENAI_API_KEY": "openai-secret",
        "OPENAI_MODEL": "openai-model",
        "DEEPSEEK_API_KEY": "deepseek-secret",
        "DEEPSEEK_MODEL": "deepseek-model",
        "GROQ_API_KEY": "groq-secret",
        "GROQ_MODEL": "groq-model",
        "XAI_API_KEY": "xai-secret",
        "XAI_MODEL": "xai-model",
    }
    options = discover_verify_backend_options(environment)
    by_selector = {str(item["selector"]): item for item in options}
    assert set(by_selector) >= {
        "openai:openai-model",
        "deepseek:deepseek-model",
        "groq:groq-model",
        "xai:xai-model",
    }
    assert by_selector["deepseek:deepseek-model"]["model_env"] == "DEEPSEEK_MODEL"
    assert by_selector["groq:groq-model"]["model_env"] == "GROQ_MODEL"
    assert by_selector["xai:xai-model"]["model_env"] == "XAI_MODEL"
    assert by_selector["openai:openai-model"]["model_env"] == "OPENAI_MODEL"

    selected = selected_verify_environment(
        environment,
        {
            "jury1": ["openai:openai-model", "deepseek:deepseek-model"],
            "jury2": ["deepseek:deepseek-model", "groq:groq-model", "xai:xai-model"],
        },
        jury2_level="medium",
    )
    assert selected["available"] is True
    assert set(selected["jury1"]) == {"openai:openai-model", "deepseek:deepseek-model"}
    assert set(selected["jury2"]) == {
        "deepseek:deepseek-model", "groq:groq-model", "xai:xai-model",
    }
    overlay = selected["overlay"]
    assert set(overlay["CITATION_VERIFIER_VERIFY_BACKENDS"].split(",")) == {
        "openai", "deepseek", "groq", "xai",
    }
    assert overlay["OPENAI_MODEL"] == "openai-model"
    assert overlay["DEEPSEEK_MODEL"] == "deepseek-model"
    assert overlay["GROQ_MODEL"] == "groq-model"
    assert overlay["XAI_MODEL"] == "xai-model"
    assert "secret" not in repr(selected)


def test_desktop_role_selections_map_all_four_states_exactly():
    environment = {
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium",
        "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "freetoken:local-a",
        "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "freetoken:local-b",
        "FREETOKEN_MODEL": "local-a,local-b",
    }

    neither = selected_verify_environment(
        environment, {"jury1": [], "jury2": []}, jury2_level="medium"
    )
    assert neither["available"] is False

    jury1_only = selected_verify_environment(
        environment,
        {"jury1": ["freetoken:local-a"], "jury2": []},
        jury2_level="medium",
    )
    assert jury1_only["available"] is True
    assert jury1_only["jury1"] == ["freetoken:local-a"]
    assert jury1_only["jury2"] == []
    assert jury1_only["overlay"]["CITATION_VERIFIER_VERIFY_JURY2_LEVEL"] == "off"
    assert jury1_only["overlay"]["CITATION_VERIFIER_VERIFY_JURY1_ONLY"] == "freetoken:local-a"
    assert jury1_only["overlay"]["CITATION_VERIFIER_VERIFY_JURY2_ONLY"] == ""

    jury2_only = selected_verify_environment(
        environment,
        {"jury1": ["freetoken:local-a"], "jury2": ["freetoken:local-a", "freetoken:local-b"]},
        jury2_level="medium",
    )
    assert jury2_only["available"] is True
    assert jury2_only["jury1"] == ["freetoken:local-a"]
    assert jury2_only["jury2"] == ["freetoken:local-a", "freetoken:local-b"]
    assert jury2_only["overlay"]["CITATION_VERIFIER_VERIFY_JURY1_ONLY"] == ""
    assert jury2_only["overlay"]["CITATION_VERIFIER_VERIFY_JURY2_ONLY"] == "freetoken:local-b"

    multiple_jury1_only = selected_verify_environment(
        environment,
        {"jury1": ["freetoken:local-a", "freetoken:local-b"], "jury2": []},
        jury2_level="medium",
    )
    assert multiple_jury1_only["available"] is True
    assert multiple_jury1_only["jury1"] == [
        "freetoken:local-a", "freetoken:local-b",
    ]
    assert multiple_jury1_only["jury2"] == []
    assert multiple_jury1_only["jury2_level"] == "off"
    assert multiple_jury1_only["overlay"]["CITATION_VERIFIER_VERIFY_JURY2_LEVEL"] == "off"

    jury2_without_jury1 = selected_verify_environment(
        environment,
        {"jury1": [], "jury2": ["freetoken:local-a", "freetoken:local-b"]},
        jury2_level="medium",
    )
    assert jury2_without_jury1["available"] is False
    assert "Jury1" in str(jury2_without_jury1["reason"])

    both = selected_verify_environment(
        environment,
        {"jury1": ["freetoken:local-a"], "jury2": ["freetoken:local-a"]},
        jury2_level="medium",
    )
    assert both["available"] is True
    assert both["jury1"] == both["jury2"] == ["freetoken:local-a"]
    assert both["overlay"]["CITATION_VERIFIER_VERIFY_JURY1_ONLY"] == ""
    assert both["overlay"]["CITATION_VERIFIER_VERIFY_JURY2_ONLY"] == ""


def test_discovered_models_use_configured_role_restrictions_as_initial_state():
    options = discover_verify_backend_options({
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
        "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "freetoken:local-a",
        "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "freetoken:local-b",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium",
        "FREETOKEN_MODEL": "local-a,local-b",
    })

    assert [(item["jury1_selected"], item["jury2_selected"]) for item in options] == [
        (True, False), (False, True),
    ]


def test_jury2_off_is_reflected_in_the_exact_selected_preview():
    environment = {
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
        "FREETOKEN_MODEL": "local-a",
    }

    both_selected = selected_verify_environment(
        environment,
        {"jury1": ["freetoken:local-a"], "jury2": ["freetoken:local-a"]},
        jury2_level="off",
    )
    assert both_selected["available"] is True
    assert both_selected["jury1"] == ["freetoken:local-a"]
    assert both_selected["jury2"] == []

    single_model_selected_in_jury2 = selected_verify_environment(
        environment,
        {"jury1": [], "jury2": ["freetoken:local-a"]},
        jury2_level="off",
    )
    assert single_model_selected_in_jury2["available"] is False


def test_verify_preview_accepts_a_configured_credentialless_backend():
    preview = preview_verify_configuration({
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
        "FREETOKEN_MODEL": "local-model",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "high",
    })

    assert preview["available"] is True
    assert preview["jury1"] == ["freetoken:local-model"]
    assert preview["jury2"] == ["freetoken:local-model"]


def test_deepseek_flash_openai_compatible_lane_can_fill_both_juries():
    preview = preview_verify_configuration({
        "CITATION_VERIFIER_VERIFY_BACKENDS": "openai_compatible",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium",
        "OPENAI_BASE_URL": "https://api.deepseek.com",
        "OPENAI_MODEL": "deepseek-v4-flash",
        "OPENAI_API_KEY": "test-key",
    })

    assert preview["available"] is True
    assert preview["jury1"] == ["openai_compatible:deepseek-v4-flash"]
    assert preview["jury2"] == ["openai_compatible:deepseek-v4-flash"]


def test_history_verify_discovers_deepseek_without_saved_backend_policy():
    environment = {
        "OPENAI_BASE_URL": "https://api.deepseek.com",
        "OPENAI_MODEL": "deepseek-v4-flash",
        "OPENAI_API_KEY": "test-key",
    }
    expected = [{
        "selector": "openai_compatible:deepseek-v4-flash",
        "backend": "openai_compatible",
        "model": "deepseek-v4-flash",
        "model_env": "OPENAI_MODEL",
        "label": "openai_compatible: deepseek-v4-flash",
    }]
    assert available_verify_backend_options(environment) == expected
    assert available_verify_backend_options(
        environment, jury2_level="off"
    ) == expected
    assert available_verify_backend_options(
        {**environment, "OPENAI_API_KEY": ""}
    ) == []
    # OPENAI_MODEL now also configures the native OpenAI provider. Discovery
    # advertises the explicit value; a catalog/readiness check decides access.
    native = available_verify_backend_options({**environment, "OPENAI_BASE_URL": ""})
    assert [item["backend"] for item in native] == ["openai"]


def test_verify_fork_options_expose_only_configured_valid_backends():
    options = configured_verify_backend_options({
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
        "FREETOKEN_MODEL": "local-a,local-b",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
    })

    assert options == [
        {
            "selector": "freetoken:local-a",
            "backend": "freetoken",
            "model": "local-a",
            "model_env": "FREETOKEN_MODEL",
            "label": "freetoken: local-a",
        },
        {
            "selector": "freetoken:local-b",
            "backend": "freetoken",
            "model": "local-b",
            "model_env": "FREETOKEN_MODEL",
            "label": "freetoken: local-b",
        },
    ]


def test_institutional_fetch_settings_are_editable_and_grouped_without_secrets(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text(
        "CALLIMACHUS_INSTITUTIONAL_FETCH=\n"
        "CALLIMACHUS_INSTITUTIONAL_PROFILE=guided_fetch\n"
        "CALLIMACHUS_INSTITUTION=\n"
        "CALLIMACHUS_INSTITUTIONAL_LOGIN_URL=\n",
        encoding="utf-8",
    )
    rows = load_settings_inventory(
        env_path=None, template_path=template, environ={},
    )
    by_name = {row["name"]: row for row in rows}

    assert by_name["CALLIMACHUS_INSTITUTIONAL_FETCH"]["control"] == "boolean"
    assert by_name["CALLIMACHUS_INSTITUTIONAL_FETCH"]["group"] == "fetch"
    assert by_name["CALLIMACHUS_INSTITUTIONAL_PROFILE"]["group"] == "fetch"
    assert by_name["CALLIMACHUS_INSTITUTION"]["group"] == "fetch"
    assert by_name["CALLIMACHUS_INSTITUTIONAL_LOGIN_URL"]["group"] == "fetch"
    assert all(not row["secret"] for row in rows)
