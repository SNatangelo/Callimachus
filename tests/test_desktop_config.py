# tests/test_desktop_config.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from core.app.desktop_config import (
    available_verify_backend_options,
    configured_verify_backend_options,
    discover_verify_backend_options,
    load_settings_inventory,
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
    assert jury1_only["jury1"] == jury1_only["jury2"] == ["freetoken:local-a"]
    assert jury1_only["overlay"]["CITATION_VERIFIER_VERIFY_JURY2_LEVEL"] == "medium"
    assert jury1_only["overlay"]["CITATION_VERIFIER_VERIFY_JURY1_ONLY"] == ""
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
    assert single_model_selected_in_jury2["available"] is True
    assert single_model_selected_in_jury2["jury1"] == ["freetoken:local-a"]
    assert single_model_selected_in_jury2["jury2"] == []


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
    assert available_verify_backend_options(
        {**environment, "OPENAI_BASE_URL": ""}
    ) == []


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
