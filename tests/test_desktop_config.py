# tests/test_desktop_config.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from core.app.desktop_config import (
    configured_verify_backend_options,
    load_settings_inventory,
    preview_verify_configuration,
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

    assert by_name["OPENAI_API_KEY"]["value"] == "••••••••"
    assert "local-secret" not in repr(rows)
    assert by_name["CITATION_VERIFIER_ACCURACY"]["value"] == "maximum"
    assert by_name["CITATION_VERIFIER_ACCURACY"]["source"] == "environment"
    assert by_name["CITATION_VERIFIER_FETCH_WORKERS"]["group"] == "fetch"


def test_verify_preview_reports_deterministic_only_without_backend():
    assert preview_verify_configuration(
        {"CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium"}
    ) == {
        "available": False,
        "reason": "no_verify_backend",
        "jury1": [],
        "jury2": [],
    }


def test_verify_preview_accepts_a_configured_credentialless_backend():
    preview = preview_verify_configuration({
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
        "FREETOKEN_MODEL": "local-model",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "high",
    })

    assert preview["available"] is True
    assert preview["jury1"] == ["freetoken:local-model"]
    assert preview["jury2"] == ["freetoken:local-model"]


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
            "label": "freetoken — local-a",
        },
        {
            "selector": "freetoken:local-b",
            "backend": "freetoken",
            "model": "local-b",
            "model_env": "FREETOKEN_MODEL",
            "label": "freetoken — local-b",
        },
    ]
