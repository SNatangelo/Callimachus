# tests/test_desktop_command.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.app.commands import desktop
from core.app import runtime_paths
from core.app.settings.config import _dotenv_values
from core.fetch.storage import content_store
from core.infra.db import RunRepository


@pytest.fixture(autouse=True)
def no_saved_language_preference(monkeypatch):
    monkeypatch.setattr("core.gui.desktop._load_language_preference", lambda: None)
    monkeypatch.setattr("core.gui.desktop._load_settings_environment_preferences", lambda: [])


@pytest.mark.parametrize("updates", [
    {"CALLIMACHUS_INSTITUTIONAL_PROFILE": "../other-profile"},
    {"CALLIMACHUS_INSTITUTIONAL_LOGIN_URL": "https://user:password@login.example.edu/"},
    {"CALLIMACHUS_INSTITUTIONAL_LOGIN_URL": "https://login.example.edu/callback?access_token=secret"},
])
def test_settings_saver_rejects_unsafe_institutional_values_before_writing(tmp_path, monkeypatch, updates):
    root = tmp_path / "project"
    root.mkdir()
    original = "CALLIMACHUS_INSTITUTIONAL_PROFILE=campus\n"
    env_path = root / ".env"
    env_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(desktop, "process_environment_without_dotenv", lambda: {})
    monkeypatch.setattr(desktop, "load_environment_file", lambda _path: None)
    captured = {}
    monkeypatch.setattr("core.gui.desktop.run_desktop", lambda **kwargs: captured.update(kwargs) or 0)
    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    with pytest.raises(ValueError):
        captured["settings_saver"](updates)
    assert env_path.read_text(encoding="utf-8") == original


def test_model_check_callbacks_use_saved_env_and_only_transient_check_updates(
    tmp_path, monkeypatch,
):
    root = tmp_path / "project"
    root.mkdir()
    env_path = root / ".env"
    original = "OPENAI_API_KEY=saved-key\nOPENAI_MODEL=saved-model\n"
    env_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(desktop, "process_environment_without_dotenv", lambda: {})
    captured = {}
    calls = []
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    monkeypatch.setattr(
        desktop, "discover_local_providers",
        lambda env: calls.append(("preflight", env)) or {"providers": []},
    )
    monkeypatch.setattr(
        desktop, "institutional_access_config",
        lambda env: {
            "enabled": env.get("CALLIMACHUS_INSTITUTIONAL_FETCH") == "1",
            "profile": env.get("CALLIMACHUS_INSTITUTIONAL_PROFILE", "guided_fetch"),
            "institution": env.get("CALLIMACHUS_INSTITUTION", ""),
            "login_url": env.get("CALLIMACHUS_INSTITUTIONAL_LOGIN_URL", ""),
        },
    )
    monkeypatch.setattr(
        desktop, "check_provider",
        lambda provider, env: calls.append((provider, env)) or {"ok": True},
    )
    monkeypatch.setattr(
        desktop, "catalog_verify_preview",
        lambda env, catalogs, selected, **kwargs: calls.append(
            ("preview", env, catalogs, selected, kwargs)
        ) or {"available": True, "overlay": {}},
    )

    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    assert captured["llm_preflight_loader"]() == {"providers": []}
    assert captured["institutional_access_config_loader"]() == {
        "enabled": False,
        "profile": "guided_fetch",
        "institution": "",
        "login_url": "",
    }
    assert captured["institutional_settings_saver"] is captured["settings_saver"]
    assert captured["provider_check_callback"](
        "openai", {"OPENAI_API_KEY": "staged-key"},
    ) == {"ok": True}
    catalogs = {"openai": {"ok": True, "models": ["checked-model"]}}
    selected = {"jury1": ["openai:checked-model"], "jury2": []}
    assert captured["verify_catalog_preview_loader"](
        catalogs, selected, "off",
    )["available"] is True
    assert calls[0][1]["OPENAI_API_KEY"] == "saved-key"
    assert calls[1][1]["OPENAI_API_KEY"] == "staged-key"
    assert calls[2][1]["OPENAI_API_KEY"] == "saved-key"
    assert calls[2][2:] == (catalogs, selected, {"jury2_level": "off"})
    assert env_path.read_text(encoding="utf-8") == original


def test_installed_settings_save_replaces_startup_loaded_env_value(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    env_path = root / ".env"
    env_path.write_text("CITATION_VERIFIER_ACCURACY=standard\n", encoding="utf-8")
    (root / ".env.example").write_text(
        "CITATION_VERIFIER_ACCURACY=standard\n", encoding="utf-8",
    )
    monkeypatch.delenv("CITATION_VERIFIER_ACCURACY", raising=False)
    monkeypatch.setattr(runtime_paths, "_LOADED_ENV_VALUES", {})
    runtime_paths.load_environment_file(env_path)
    captured = {}
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    before = {row["name"]: row for row in captured["settings_loader"]()}
    assert before["CITATION_VERIFIER_ACCURACY"]["source"] == "env_file"
    assert captured["settings_saver"]({"CITATION_VERIFIER_ACCURACY": "maximum"}) == env_path
    after = {row["name"]: row for row in captured["settings_loader"]()}
    assert after["CITATION_VERIFIER_ACCURACY"]["value"] == "maximum"
    assert after["CITATION_VERIFIER_ACCURACY"]["source"] == "env_file"
    assert env_path.read_text(encoding="utf-8") == "CITATION_VERIFIER_ACCURACY=maximum\n"
    assert runtime_paths.os.environ["CITATION_VERIFIER_ACCURACY"] == "maximum"


def test_installed_settings_do_not_silently_shadow_external_override(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    env_path = root / ".env"
    env_path.write_text("CITATION_VERIFIER_ACCURACY=standard\n", encoding="utf-8")
    monkeypatch.setenv("CITATION_VERIFIER_ACCURACY", "external")
    monkeypatch.setattr(runtime_paths, "_LOADED_ENV_VALUES", {})
    runtime_paths.load_environment_file(env_path)
    captured = {}
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    with pytest.raises(ValueError, match="controlled by the process environment"):
        captured["settings_saver"]({"CITATION_VERIFIER_ACCURACY": "maximum"})
    assert env_path.read_text(encoding="utf-8") == "CITATION_VERIFIER_ACCURACY=standard\n"
    assert runtime_paths.os.environ["CITATION_VERIFIER_ACCURACY"] == "external"


def test_desktop_command_builds_a_references_only_run_without_llm(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CITATION_VERIFIER_VERIFY_BACKENDS", raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text(
        "CITATION_VERIFIER_ACCURACY=standard\n",
        encoding="utf-8",
    )
    (root / ".env.example").write_text(
        "CITATION_VERIFIER_VERIFY_BACKENDS=\n"
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium\n",
        encoding="utf-8",
    )
    (root / "docs" / "user" / "en").mkdir(parents=True)
    (root / "docs" / "user" / "en" / "README.md").write_text(
        "# Guide", encoding="utf-8"
    )
    captured = {}

    def fake_desktop(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr("core.gui.desktop.run_desktop", fake_desktop)

    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    specification = captured["command_builder"](
        str(root / "paper.pdf"), "standard", "high"
    )

    assert "--references-only" in specification["command"]
    assert specification["environment"][
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL"
    ] == "high"
    assert Path(specification["run_dir"]).parent == root / "runs"


def test_desktop_command_loads_and_saves_env_and_complete_guide(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_BOOKS_API_KEY", raising=False)
    monkeypatch.delenv("CITATION_VERIFIER_FETCH_WORKERS", raising=False)
    root = tmp_path / "project"
    guide = root / "docs" / "user" / "en"
    guide.mkdir(parents=True)
    (root / ".env").write_text("GOOGLE_BOOKS_API_KEY=\n", encoding="utf-8")
    (root / ".env.example").write_text(
        "GOOGLE_BOOKS_API_KEY=\nCITATION_VERIFIER_FETCH_WORKERS=4\n",
        encoding="utf-8",
    )
    (guide / "README.md").write_text("# Complete guide", encoding="utf-8")
    (guide / "01-quickstart.md").write_text("# Quickstart", encoding="utf-8")
    captured = {}

    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    assert desktop.main(["--language", "en"]) == 0
    assert captured["language"] == "en"
    rows = {row["name"]: row for row in captured["settings_loader"]()}
    google = rows["GOOGLE_BOOKS_API_KEY"]
    assert google["group"] == "resolution"
    assert google["explicitly_empty"] is True
    assert google["source"] == "env_file"
    assert google["value"] == google["display_value"] == ""
    assert rows["CITATION_VERIFIER_FETCH_WORKERS"]["source"] == "default"

    captured["settings_saver"]({"GOOGLE_BOOKS_API_KEY": " \treplacement\r\n"})
    assert (root / ".env").read_text(encoding="utf-8") == (
        "GOOGLE_BOOKS_API_KEY=replacement\n"
    )
    monkeypatch.setattr("core.gui.desktop._load_language_preference", lambda: "it")
    assert captured["docs_loader"]() == {
        "README.md": "# Complete guide",
        "01-quickstart.md": "# Quickstart",
    }


def test_gui_command_uses_versioned_engine_defaults_and_source_service_callbacks(
    tmp_path, monkeypatch,
):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text(
        "CITATION_VERIFIER_VERIFY_CONTEXT_MODE=extractive_rag\n"
        "CITATION_VERIFIER_CONTEXT_PROFILE=small\n"
        "CITATION_VERIFIER_MAX_SOURCE_CHARS=12000\n"
        "CITATION_VERIFIER_VERIFY_MAX_TOKENS=9000\n"
        "CITATION_VERIFIER_MAILTO=person@example.test\n"
        "CORE_API_KEY=source-secret\n"
        "OPENAI_API_KEY=llm-secret\n",
        encoding="utf-8",
    )
    (root / ".env.example").write_text(
        "CITATION_VERIFIER_ACCURACY=standard\n"
        "CITATION_VERIFIER_VERIFY_BACKENDS=\n"
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium\n"
        "CITATION_VERIFIER_VERIFY_CONTEXT_MODE=auto\n"
        "CITATION_VERIFIER_CONTEXT_PROFILE=large\n"
        "CITATION_VERIFIER_MAX_SOURCE_CHARS=\n"
        "CITATION_VERIFIER_VERIFY_MAX_TOKENS=4000\n"
        "CITATION_VERIFIER_MAILTO=\n"
        "CORE_API_KEY=\n",
        encoding="utf-8",
    )
    captured = {}
    preview_environments = []
    check_calls = []
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(desktop, "process_environment_without_dotenv", lambda: {})
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    monkeypatch.setattr(
        desktop,
        "preview_verify_configuration",
        lambda env, **kwargs: preview_environments.append(dict(env)) or {"available": True},
    )
    monkeypatch.setattr(
        desktop,
        "check_source_service_key",
        lambda name, updates, *, environ: check_calls.append(
            (name, dict(updates), dict(environ))
        ) or {"ok": True, "error": None},
    )

    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    defaults = captured["run_environment_defaults"]
    assert defaults == {
        "CITATION_VERIFIER_VERIFY_CONTEXT_MODE": "auto",
        "CITATION_VERIFIER_CONTEXT_PROFILE": "large",
        "CITATION_VERIFIER_MAX_SOURCE_CHARS": "",
        "CITATION_VERIFIER_VERIFY_MAX_TOKENS": "4000",
    }
    assert "CITATION_VERIFIER_ACCURACY" not in defaults
    assert "CITATION_VERIFIER_VERIFY_BACKENDS" not in defaults

    rows = {row["name"]: row for row in captured["settings_loader"]()}
    assert rows["CITATION_VERIFIER_VERIFY_CONTEXT_MODE"]["value"] == "auto"
    assert rows["CITATION_VERIFIER_VERIFY_CONTEXT_MODE"]["locked"] is True
    assert rows["CITATION_VERIFIER_VERIFY_MAX_TOKENS"]["value"] == "4000"
    assert rows["CITATION_VERIFIER_MAILTO"]["value"] == "person@example.test"

    captured["verify_preview_loader"]("medium")
    effective = preview_environments[-1]
    assert effective["CITATION_VERIFIER_VERIFY_CONTEXT_MODE"] == "auto"
    assert effective["CITATION_VERIFIER_CONTEXT_PROFILE"] == "large"
    assert effective["CITATION_VERIFIER_MAX_SOURCE_CHARS"] == ""
    assert effective["CITATION_VERIFIER_VERIFY_MAX_TOKENS"] == "4000"
    assert captured["source_contact_loader"]() == {"has_mail": True, "has_keys": True}
    assert captured["source_service_check_callback"](
        "CORE_API_KEY", {"CORE_API_KEY": "staged-source-key"},
    ) == {"ok": True, "error": None}
    assert check_calls[0][0:2] == (
        "CORE_API_KEY", {"CORE_API_KEY": "staged-source-key"},
    )
    assert check_calls[0][2]["CORE_API_KEY"] == "source-secret"
    assert "OPENAI_API_KEY" not in repr(captured["source_contact_loader"]())


def test_import_or_reset_masks_stale_process_environment_only_in_gui(
    tmp_path, monkeypatch,
):
    root = tmp_path / "project"
    root.mkdir()
    env_path = root / ".env"
    env_path.write_text(
        "OPENAI_API_KEY=file-key\nCORE_API_KEY=file-source-key\n",
        encoding="utf-8",
    )
    (root / ".env.example").write_text(
        "OPENAI_API_KEY=\nCORE_API_KEY=\nCITATION_VERIFIER_VERIFY_BACKENDS=\n",
        encoding="utf-8",
    )
    captured = {}
    process_environment = {
        "OPENAI_API_KEY": "stale-process-key",
        "CORE_API_KEY": "stale-process-source-key",
    }
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        desktop, "process_environment_without_dotenv", lambda: dict(process_environment),
    )
    monkeypatch.setattr(desktop, "load_environment_file", lambda _path: None)
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    assert desktop.main(["--runs-root", str(root / "runs")]) == 0

    result = captured["settings_transfer_callback"](
        "reset", {"keep_api_keys": False},
    )
    assert result["ok"] is True
    assert set(result["environment_names"]) == {
        "OPENAI_API_KEY", "CORE_API_KEY", "CITATION_VERIFIER_VERIFY_BACKENDS",
    }
    assert "stale-process-key" not in repr(result)
    assert captured["source_contact_loader"]() == {"has_mail": False, "has_keys": False}
    rows = {row["name"]: row for row in captured["settings_loader"]()}
    assert rows["OPENAI_API_KEY"]["value"] == ""
    assert rows["CORE_API_KEY"]["value"] == ""
    # The running user's actual process environment was only masked for this GUI.
    assert process_environment["OPENAI_API_KEY"] == "stale-process-key"
    assert process_environment["CORE_API_KEY"] == "stale-process-source-key"


def test_persisted_gui_environment_mask_is_template_limited_and_used_by_commands(
    tmp_path, monkeypatch,
):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text(
        "OPENAI_API_KEY=\nCITATION_VERIFIER_VERIFY_BACKENDS=openai\n",
        encoding="utf-8",
    )
    (root / ".env.example").write_text(
        "OPENAI_API_KEY=\nCITATION_VERIFIER_VERIFY_BACKENDS=\n",
        encoding="utf-8",
    )
    captured = {}
    process_environment = {
        "OPENAI_API_KEY": "stale-process-key",
        "PATH": "kept-process-path",
    }
    provider_environments = []
    preview_environments = []
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        desktop, "process_environment_without_dotenv", lambda: dict(process_environment),
    )
    monkeypatch.setattr(desktop, "load_environment_file", lambda _path: None)
    monkeypatch.setattr(
        "core.gui.desktop._load_settings_environment_preferences",
        lambda: ["OPENAI_API_KEY", "PATH"],
    )
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    monkeypatch.setattr(
        desktop, "check_provider",
        lambda _provider, environ: provider_environments.append(dict(environ)) or {"ok": True},
    )
    monkeypatch.setattr(
        desktop, "preview_verify_configuration",
        lambda environ, **_kwargs: preview_environments.append(dict(environ)) or {"available": True},
    )

    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    rows = {row["name"]: row for row in captured["settings_loader"]()}
    assert rows["OPENAI_API_KEY"]["source"] == "env_file"
    captured["provider_check_callback"]("openai", {})
    assert provider_environments[-1]["OPENAI_API_KEY"] == ""
    assert provider_environments[-1]["PATH"] == "kept-process-path"
    captured["command_builder"]("paper.pdf", "standard", "medium")
    assert preview_environments[-1]["OPENAI_API_KEY"] == ""
    assert preview_environments[-1]["PATH"] == "kept-process-path"
    assert process_environment["OPENAI_API_KEY"] == "stale-process-key"


def test_settings_backup_preserves_effective_process_only_api_values(
    tmp_path, monkeypatch,
):
    root = tmp_path / "project"
    root.mkdir()
    env_path = root / ".env"
    env_path.write_text("OPENAI_API_KEY=\nOPENAI_BASE_URL=\n", encoding="utf-8")
    (root / ".env.example").write_text(
        "OPENAI_API_KEY=\nOPENAI_BASE_URL=https://api.openai.com/v1\n",
        encoding="utf-8",
    )
    captured = {}
    process_environment = {
        "OPENAI_API_KEY": "process-only-api-secret",
        "OPENAI_BASE_URL": "https://process-endpoint.example/v1",
        "PATH": "unrelated-path-secret",
    }
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        desktop, "process_environment_without_dotenv", lambda: dict(process_environment),
    )
    monkeypatch.setattr(desktop, "load_environment_file", lambda _path: None)
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    assert desktop.main(["--runs-root", str(root / "runs")]) == 0

    result = captured["settings_transfer_callback"](
        "reset", {"keep_api_keys": True},
    )
    assert result["ok"] is True
    values = _dotenv_values(env_path)
    assert values["OPENAI_API_KEY"] == "process-only-api-secret"
    assert values["OPENAI_BASE_URL"] == "https://process-endpoint.example/v1"
    assert "PATH" not in values
    assert "process-only-api-secret" not in repr(result)
    assert "unrelated-path-secret" not in env_path.read_text(encoding="utf-8")


def test_docs_library_excludes_cli_and_deployment_material(tmp_path):
    root = tmp_path / "project"
    paths = {
        "README.md": root / "docs" / "user" / "en" / "README.md",
        "01-quickstart.md": root / "docs" / "user" / "en" / "01-quickstart.md",
        "02-cli-reference.md": root / "docs" / "reference" / "02-cli-reference.md",
        "../deployment/agent-guide.md": root / "docs" / "deployment" / "agent-guide.md",
        "../deployment/administrator-guide.md": root / "docs" / "deployment" / "administrator-guide.md",
        "../architecture/artifact-integrity.md": root / "docs" / "architecture" / "artifact-integrity.md",
        "../../DEPLOYMENT.md": root / "DEPLOYMENT.md",
    }
    for key, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {key}", encoding="utf-8")
    legacy_guide = root / "docs" / "reference"
    legacy_guide.mkdir(parents=True, exist_ok=True)
    (legacy_guide / "README.md").write_text("# CLI guide", encoding="utf-8")
    (legacy_guide / "01-quickstart.md").write_text("# CLI quickstart", encoding="utf-8")

    documents = desktop._docs_library(root, "en")
    assert documents == {
        "README.md": "# README.md",
        "01-quickstart.md": "# 01-quickstart.md",
    }


@pytest.mark.parametrize(
    "language,privacy,readme_title",
    [("en", "privacy.md", "Callimachus Desktop Guide"),
     ("it", "privacy-it.md", "Guida desktop di Callimachus")],
)
def test_docs_library_loads_only_selected_language(language, privacy, readme_title):
    root = Path(__file__).resolve().parents[1]
    documents = desktop._docs_library(root, language)
    chapter_paths = (
        "01-quickstart.md",
        "02-analysis.md",
        "03-settings.md",
        "04-reviews-and-sources.md",
        "05-results-and-history.md",
        "06-library-and-cache.md",
        "07-capabilities-and-limits.md",
        "08-troubleshooting.md",
    )

    assert all(path in documents and documents[path].strip() for path in chapter_paths)
    assert documents["README.md"].splitlines()[0].lstrip("# ") == readme_title
    assert privacy in documents and documents[privacy].strip()
    assert not ({"privacy.md", "privacy-it.md"} - {privacy}).intersection(documents)
    assert not {"02-cli-reference.md", "10-keys-and-credentials.md"} & documents.keys()


def test_docs_library_has_no_cross_language_fallback(tmp_path):
    root = tmp_path / "project"
    english = root / "docs" / "user" / "en"
    english.mkdir(parents=True)
    (english / "README.md").write_text("# English guide", encoding="utf-8")
    italian = root / "docs" / "user" / "it"
    italian.mkdir(parents=True)
    (italian / "README.md").write_text("# Guida italiana", encoding="utf-8")
    (english / "01-quickstart.md").write_text("# English quickstart", encoding="utf-8")

    documents = desktop._docs_library(root, "it")
    assert documents["README.md"] == "# Guida italiana"
    assert "01-quickstart.md" not in documents


@pytest.mark.parametrize("language", ["", "auto", "fr", "EN"])
def test_docs_library_rejects_unsupported_language(tmp_path, language):
    with pytest.raises(ValueError, match="unsupported documentation language"):
        desktop._docs_library(tmp_path, language)


@pytest.mark.parametrize(
    "explicit,saved,expected",
    [("en", "it", "en"), ("it", "en", "it"),
     (None, "it", "it"), (None, "en", "en"), (None, None, "en")],
)
def test_desktop_language_precedence_and_frozen_docs_loader(
    tmp_path, monkeypatch, explicit, saved, expected,
):
    root = tmp_path / "project"
    guide = root / "docs" / "user" / expected
    guide.mkdir(parents=True)
    (guide / "README.md").write_text(f"# {expected} guide", encoding="utf-8")
    (root / ".env.example").write_text("", encoding="utf-8")
    captured = {}
    preference = {"language": saved}
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        "core.gui.desktop._load_language_preference", lambda: preference["language"],
    )
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    argv = ["--language", explicit] if explicit else []
    assert desktop.main(argv) == 0
    assert captured["language"] == expected
    preference["language"] = "it" if expected == "en" else "en"
    assert captured["docs_loader"]()["README.md"] == f"# {expected} guide"


def test_desktop_signing_key_generator_persists_only_key_path(tmp_path, monkeypatch):
    root = tmp_path / "project"
    guide = root / "docs" / "user" / "en"
    guide.mkdir(parents=True)
    (guide / "README.md").write_text("# Guide", encoding="utf-8")
    (root / ".env.example").write_text(
        "CITATION_VERIFIER_SIGNING_KEY_FILE=\n", encoding="utf-8"
    )
    key_path = tmp_path / "protected" / "signing.key"
    captured = {}

    monkeypatch.delenv("CITATION_VERIFIER_SIGNING_KEY_FILE", raising=False)
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(desktop, "default_key_path", lambda: str(key_path))
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    assert desktop.main([]) == 0
    created = captured["signing_key_generator"]()
    assert created == {"path": str(key_path), "created": True}
    assert (root / ".env").read_text(encoding="utf-8") == (
        f"CITATION_VERIFIER_SIGNING_KEY_FILE={key_path}\n"
    )
    original_key = key_path.read_bytes()

    retained = captured["signing_key_generator"]()
    assert retained == {"path": str(key_path), "created": False}
    assert key_path.read_bytes() == original_key
    assert original_key.decode("ascii").strip() not in repr(retained)


def test_delete_history_rows_rejects_outside_root_and_active_runs(tmp_path, monkeypatch):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    valid = runs_root / "completed"
    active = runs_root / "active"
    paused = runs_root / "paused"
    unavailable = runs_root / "unavailable"
    outside = tmp_path / "outside"
    for path in (valid, active, paused, unavailable, outside):
        path.mkdir()

    monkeypatch.setattr(desktop, "discover_run_briefs", lambda _root: [
        {"run_dir": str(valid), "available": True, "run_status": "completed"},
        {"run_dir": str(active), "available": True, "run_status": "active"},
        {"run_dir": str(paused), "available": True, "run_status": "paused",
         "action_required": True},
        {"run_dir": str(unavailable), "available": False},
    ])

    with pytest.raises(ValueError):
        desktop._delete_history_rows(runs_root, [{"run_dir": str(runs_root)}])
    with pytest.raises(ValueError):
        desktop._delete_history_rows(runs_root, [{"run_dir": str(outside)}])
    with pytest.raises(ValueError):
        desktop._delete_history_rows(runs_root, [{"run_dir": str(active)}])
    desktop._delete_history_rows(
        runs_root, [{"run_dir": str(paused)}], protected_run_dir=str(paused)
    )
    assert not paused.exists()
    desktop._delete_history_rows(
        runs_root,
        [{"run_dir": str(valid)}],
        protected_run_dir=str(valid),
    )
    assert not valid.exists() and active.exists() and unavailable.exists() and outside.exists()

    with pytest.raises(ValueError):
        desktop._delete_history_rows(
            runs_root,
            [{"run_dir": str(active)}],
            protected_run_dir=str(active),
        )

    desktop._delete_history_rows(
        runs_root,
        [
            {"run_dir": str(unavailable)},
        ],
    )
    assert not valid.exists() and not unavailable.exists()


def test_reference_rows_keep_source_and_verdict_lifecycles_separate():
    rows = desktop._reference_rows({
        "phase": "verify",
        "source_inventory": [{
            "ref_id": "r1",
            "parsed": {"title": "A source"},
            "resolve": {"status": "resolved"},
            "fetch": {"tier": "fulltext", "pending_tasks": []},
            "bibliographic_concern": {
                "level": "elevated_bibliographic_suspicion",
                "conclusion": "completed_search_misses",
            },
            "bibliographic_review_labels": [
                {"code": "incomplete_bibliographic_source"},
            ],
        }],
        "verification_pairs": [{
            "ref_id": "r1",
            "claim_id": "c1",
            "status": "terminal",
            "terminal_outcome": "supported",
        }],
    })

    assert rows == [{
        "ref_id": "r1",
        "ref_number": 1,
        "title": "A source",
        "phase": "verify",
        "status": "completato",
        "availability": "fulltext",
        "risk_signal": "Elevated bibliographic suspicion",
        "risk_tooltip": (
            "Non-diagnostic; not a fabrication finding. "
            "completed_search_misses"
        ),
        "review_labels": "Incomplete bibliographic source",
        "review_tooltip": "Informational only; not proof of fabrication.",
        "result": "supported",
        "verification_in_progress": False,
        "verification_pending": False,
        "details": {
            "parsed": {"title": "A source"},
            "resolved": {"status": "resolved"},
            "fetch": {"tier": "fulltext", "pending_tasks": []},
            "verification_pairs": [{
                "ref_id": "r1",
                "claim_id": "c1",
                "status": "terminal",
                "terminal_outcome": "supported",
            }],
        },
    }]


def test_desktop_snapshot_only_passes_display_rows_to_gui(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("", encoding="utf-8")
    captured = {}
    calls = []
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    def load_snapshot(run_dir, *, include_fetch_attempts=True):
        calls.append((run_dir, include_fetch_attempts))
        return {
            "phase": "resolve",
            "source_inventory": [{
                "ref_id": "r1", "parsed": {"title": "One"},
                "fetch": {"tier": "none", "attempts": ["heavy detail"]},
            }],
            "verification_pairs": [],
        }

    monkeypatch.setattr(desktop, "load_run_snapshot", load_snapshot)
    monkeypatch.setattr(desktop, "load_run_overview", lambda _run: {
        "phase": "resolve", "run_status": "active", "updated_at": "revision-1",
    })
    page_calls = []

    def load_page(run_dir, *, offset, limit):
        page_calls.append((run_dir, offset, limit))
        return {
            "total": 30,
            "source_inventory": [{
                "ref_id": "r13", "parsed": {"title": "Page two"},
                "fetch": {"tier": "none"},
            }],
            "verification_pairs": [],
        }

    monkeypatch.setattr(desktop, "load_source_page", load_page)
    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    payload = captured["snapshot_loader"]("run")
    assert calls == [("run", False)]
    assert "source_inventory" not in payload
    assert "verification_pairs" not in payload
    assert "details" not in payload["references"][0]
    assert payload["references"][0]["title"] == "One"
    assert captured["overview_loader"]("run")["updated_at"] == "revision-1"
    page = captured["source_page_loader"]("run", 12, 12)
    assert page_calls == [("run", 12, 12)]
    assert page["total"] == 30
    assert len(page["references"]) == 1
    assert page["references"][0]["ref_number"] == 13
    assert "details" not in page["references"][0]
    monkeypatch.setattr(desktop, "discover_run_briefs", lambda _root: [{
        "run_dir": "run", "run_id": "run-1", "available": True,
        "input": {"label": "Paper"},
    }])
    monkeypatch.setattr(desktop, "load_run_summary", lambda _run: {
        "run_dir": "run", "available": True,
        "source_counts": {"resolved": 1},
    })
    assert captured["history_brief_loader"]()[0]["summary_pending"] is True
    assert captured["history_detail_loader"]("run")["source_counts"] == {
        "resolved": 1,
    }


def test_reference_rows_use_integral_ref_number_or_appearance_order():
    rows = desktop._reference_rows({
        "source_inventory": [
            {
                "ref_id": "r1",
                "ref_number": 2,
                "parsed": {"title": "First", "ref_number": 10},
            },
            {
                "ref_id": "r2",
                "parsed": {"title": "Second", "ref_number": 10.0},
            },
            {
                "ref_id": "r3",
                "ref_number": 0,
                "parsed": {"title": "Third", "ref_number": 3.5},
            },
            {
                "ref_id": "r4",
                "ref_number": True,
                "parsed": {"title": "Fourth", "ref_number": None},
            },
        ],
    })

    assert [row["ref_number"] for row in rows] == [2, 10, 3, 4]
    page = desktop._reference_rows({
        "source_inventory": [{"ref_id": "r13", "parsed": {"title": "Later"}}],
    }, appearance_offset=12)
    assert page[0]["ref_number"] == 13


def test_reference_rows_show_jury1_only_after_jury2_acceptance():
    rows = desktop._reference_rows({
        "phase": "verify",
        "source_inventory": [{
            "ref_id": "r1", "parsed": {"title": "A source"},
            "resolve": {}, "fetch": {},
        }],
        "verification_pairs": [
            {
                "ref_id": "r1", "claim_id": "c1", "scope": "fulltext_complete",
                "status": "accepted", "winner_call_id": "winner-1",
                "terminal_outcome": "supports", "terminal_cause": "jury2_accepted",
                "jury1_outcome": "supports",
            },
            {
                "ref_id": "r1", "claim_id": "c2", "scope": "fulltext_complete",
                "status": "open", "in_progress": True,
                "winner_call_id": "candidate-in-retry",
                "jury1_outcome": "contradicts",
            },
        ],
    })

    assert rows[0]["status"] == "in_corso"
    assert rows[0]["result"] == "Jury1: supports"
    assert "contradicts" not in rows[0]["result"]


def test_reference_rows_mark_queued_open_verification_pairs_waiting():
    rows = desktop._reference_rows({
        "phase": "verify",
        "source_inventory": [{
            "ref_id": "r1", "parsed": {"title": "A source"},
            "resolve": {}, "fetch": {},
        }],
        "verification_pairs": [{
            "ref_id": "r1", "claim_id": "c1", "status": "open",
        }],
    })

    assert rows[0]["phase"] == "verify"
    assert rows[0]["status"] == "in_attesa"


def test_reference_rows_leave_missing_verdict_empty_for_localized_gui_fallback():
    rows = desktop._reference_rows({
        "phase": "resolve",
        "source_inventory": [{
            "ref_id": "r1", "parsed": {"title": "A source"},
            "resolve": {}, "fetch": {},
        }],
    })
    assert rows[0]["result"] == ""


def test_cache_anchor_uses_the_selected_runs_root_until_a_run_exists(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(content_store.ENV_STATE_DIR, raising=False)
    runs_root = tmp_path / "external-runs"
    anchor = desktop._cache_anchor(runs_root, None)
    configured = {content_store.ENV_STATE_DIR: str(tmp_path / "state")}

    assert Path(anchor).parent == runs_root
    assert content_store.storage_root(anchor) == str(runs_root / "storage")
    assert content_store.storage_root(anchor, configured) == str(
        tmp_path / "state" / content_store.STORE_SUBDIR
    )


def test_desktop_cache_loader_keeps_external_root_after_run_selection(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    runs_root = tmp_path / "external-runs"
    root.mkdir()
    (root / ".env.example").write_text("", encoding="utf-8")
    (root / "docs" / "user" / "en").mkdir(parents=True)
    (root / "docs" / "user" / "en" / "README.md").write_text("# Guide", encoding="utf-8")
    monkeypatch.delenv(content_store.ENV_STATE_DIR, raising=False)
    captured = {}
    cache_loads = []

    def fake_desktop(**kwargs):
        captured.update(kwargs)
        return 0

    def fake_inventory(run_dir, environ=None):
        cache_loads.append((run_dir, environ))
        return {"items": []}

    def select_created_run() -> tuple[str, str]:
        captured["cache_loader"]()
        initial_run, initial_environment = cache_loads[-1]
        specification = captured["command_builder"](
            str(root / "paper.pdf"), "standard", "medium"
        )
        repo = RunRepository.create(
            specification["run_dir"],
            run_id="run-1",
            input_path="paper.pdf",
            input_sha256="a" * 64,
            accuracy="standard",
            style=None,
            model_id=None,
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture",
        )
        repo.close()
        captured["snapshot_loader"](specification["run_dir"])
        captured["cache_loader"]()
        selected_run, selected_environment = cache_loads[-1]
        return (
            content_store.storage_root(initial_run, initial_environment),
            content_store.storage_root(selected_run, selected_environment),
        )

    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr("core.gui.desktop.run_desktop", fake_desktop)
    monkeypatch.setattr(desktop, "load_cache_inventory", fake_inventory)

    (root / ".env").write_text("", encoding="utf-8")
    assert desktop.main(["--runs-root", str(runs_root)]) == 0
    initial, selected = select_created_run()
    expected_root = str(runs_root / "storage")
    assert initial == expected_root
    assert selected == expected_root

    state_root = tmp_path / "explicit-state"
    (root / ".env").write_text(
        f"{content_store.ENV_STATE_DIR}={state_root}\n", encoding="utf-8"
    )
    captured.clear()
    assert desktop.main(["--runs-root", str(runs_root)]) == 0
    initial, selected = select_created_run()
    expected_root = str(state_root / content_store.STORE_SUBDIR)
    assert initial == expected_root
    assert selected == expected_root
def test_desktop_builds_completed_verify_fork_with_selected_backends(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    (root / "docs" / "user" / "en").mkdir(parents=True)
    (root / "docs" / "user" / "en" / "README.md").write_text(
        "# Guide", encoding="utf-8"
    )
    captured = {}

    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        desktop,
        "effective_environment",
        lambda **_kwargs: {
            "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
            "FREETOKEN_MODEL": "local-model",
            "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
        },
    )
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    assert captured["verify_fork_options_loader"]() == [{
        "selector": "freetoken:local-model",
        "backend": "freetoken",
        "model": "local-model",
        "model_env": "FREETOKEN_MODEL",
        "label": "freetoken: local-model",
    }]
    specification = captured["verify_fork_command_builder"](
        {"run_dir": str(root / "runs" / "parent"), "paper": "paper.pdf"},
        {"jury1": ["freetoken:local-model"], "jury2": []},
        {},
    )

    assert "--fork-completed-verify" in specification["command"]
    assert str(root / "runs" / "parent") in specification["command"]
    assert specification["run_dir"] != str(root / "runs" / "parent")
    assert specification["environment"] == {
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
        "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "freetoken:local-model",
        "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "",
        "FREETOKEN_MODEL": "local-model",
    }
    reference_preview = captured["verify_fork_command_builder"](
        {
            "run_dir": str(root / "runs" / "preview"),
            "paper": "paper.pdf",
            "references_only": True,
        },
        {"jury1": ["freetoken:local-model"], "jury2": []},
        {},
    )
    assert "--fork-reference-only-fetch" in reference_preview["command"]
    assert "--fork-completed-verify" not in reference_preview["command"]
    assert str(root / "runs" / "preview") in reference_preview["command"]
    with pytest.raises(ValueError, match="valid policy"):
        captured["verify_fork_command_builder"](
            {"run_dir": str(root / "runs" / "parent")},
            {"jury1": ["unknown"], "jury2": []},
            {},
        )


def test_desktop_history_verify_uses_deepseek_without_backend_flag(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    (root / "docs" / "user" / "en").mkdir(parents=True)
    (root / "docs" / "user" / "en" / "README.md").write_text(
        "# Guide", encoding="utf-8"
    )
    captured = {}
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        desktop, "effective_environment", lambda **_kwargs: {
            "OPENAI_BASE_URL": "https://api.deepseek.com",
            "OPENAI_API_KEY": "test-key",
            "OPENAI_MODEL": "deepseek-v4-flash",
        },
    )
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    options = captured["verify_fork_options_loader"]()
    assert [item["selector"] for item in options] == [
        "openai_compatible:deepseek-v4-flash"
    ]
    specification = captured["verify_fork_command_builder"](
        {"run_dir": str(root / "runs" / "parent"), "paper": "paper.pdf"},
        {
            "jury1": [options[0]["selector"]],
            "jury2": [],
        },
        {},
    )
    assert specification["environment"]["CITATION_VERIFIER_VERIFY_BACKENDS"] == (
        "openai_compatible"
    )
    assert specification["environment"]["OPENAI_MODEL"] == "deepseek-v4-flash"
    assert specification["environment"]["CITATION_VERIFIER_VERIFY_JURY2_LEVEL"] == (
        "off"
    )


def test_desktop_history_verify_uses_independent_roles_and_catalog_model(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    (root / "docs" / "user" / "en").mkdir(parents=True)
    (root / "docs" / "user" / "en" / "README.md").write_text(
        "# Guide", encoding="utf-8"
    )
    captured = {}
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        desktop, "effective_environment", lambda **_kwargs: {
            "OPENAI_BASE_URL": "https://api.deepseek.com",
            "OPENAI_API_KEY": "synthetic-key",
            "OPENAI_MODEL": "deepseek-v4-flash",
            "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium",
            "OLLAMA_HOST": "http://127.0.0.1:11434",
        },
    )
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    ollama_catalog = {
        "provider": "ollama_local", "ok": True,
        "models": ["catalog-only-model"], "model_env": "OLLAMA_MODEL",
        "options": [{
            "selector": "ollama_local:catalog-only-model", "backend": "ollama_local",
            "model": "catalog-only-model", "model_env": "OLLAMA_MODEL",
        }],
    }
    specification = captured["verify_fork_command_builder"](
        {"run_dir": str(root / "runs" / "parent"), "paper": "paper.pdf"},
        {
            "jury1": ["openai_compatible:deepseek-v4-flash"],
            "jury2": ["ollama_local:catalog-only-model"],
        },
        {"ollama_local": ollama_catalog},
    )

    overlay = specification["environment"]
    assert set(overlay["CITATION_VERIFIER_VERIFY_BACKENDS"].split(",")) == {
        "openai_compatible", "ollama_local",
    }
    assert overlay["CITATION_VERIFIER_VERIFY_JURY1_ONLY"] == (
        "openai_compatible:deepseek-v4-flash"
    )
    assert overlay["CITATION_VERIFIER_VERIFY_JURY2_ONLY"] == (
        "ollama_local:catalog-only-model"
    )
    assert overlay["OPENAI_MODEL"] == "deepseek-v4-flash"
    assert overlay["OLLAMA_MODEL"] == "catalog-only-model"
    assert overlay["CITATION_VERIFIER_VERIFY_JURY2_LEVEL"] == "medium"


@pytest.mark.parametrize("references_only", [True, False])
def test_desktop_resume_rejects_completed_run(
    tmp_path, monkeypatch, references_only
):
    root = tmp_path / "project"
    (root / "docs" / "user" / "en").mkdir(parents=True)
    (root / "docs" / "user" / "en" / "README.md").write_text(
        "# Guide", encoding="utf-8"
    )
    captured = {}
    parent = str(root / "runs" / "preview")
    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(
        desktop, "load_run_brief",
        lambda _run_dir: {
            "run_status": "completed",
            "phase": "done",
            "references_only": references_only,
            "run_id": "preview",
            "input": {"label": "paper.pdf"},
        },
    )
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    with pytest.raises(ValueError, match="completed run cannot be resumed"):
        captured["resume_command_builder"](parent)


def test_desktop_skip_manual_waives_retrieval_and_keeps_identity_unverified(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    captured = {}
    calls = []
    pending = [
        SimpleNamespace(task_kind="fetch", task_id="fetch:r1"),
        SimpleNamespace(task_kind="source_identity_attestation", task_id="identity:r2"),
        SimpleNamespace(task_kind="claim_evidence", task_id="verify:r3"),
        SimpleNamespace(task_kind="web_research", task_id="research:r4"),
    ]

    class Repo:
        def list_pending_tasks(self):
            return pending

        def close(self):
            pass

    monkeypatch.setattr(desktop, "_root", lambda: root)
    monkeypatch.setattr(desktop.RunRepository, "open_readonly", lambda _run: Repo())
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop", lambda **kwargs: captured.update(kwargs) or 0
    )
    monkeypatch.setattr(
        "core.app.commands.tasks.cmd_guided_proceed",
        lambda args: calls.append(("retrieval", args.run)) or 0,
    )
    monkeypatch.setattr(
        "core.app.commands.tasks.cmd_skip_identity",
        lambda args: calls.append(("identity", args.reason)) or 0,
    )

    assert desktop.main(["--runs-root", str(root / "runs")]) == 0
    assert captured["skip_manual_callback"]("run") == 2
    assert calls[0] == ("retrieval", "run")
    assert calls[1][0] == "identity"
    assert "not manually reviewed" in calls[1][1]

    pending.append(SimpleNamespace(task_kind="manual_parse_review", task_id="parse:review"))
    calls.clear()
    with pytest.raises(ValueError, match="automatic manual-task skip cannot handle"):
        captured["skip_manual_callback"]("run")
    assert calls == []

def test_reference_rows_use_full_citation_when_parsed_title_is_placeholder():
    citation = "Bruner. The process of education. 1960. ISBN 978-0-306-40615-7."
    rows = desktop._reference_rows({
        "source_inventory": [{
            "ref_id": "r1",
            "ref_number": 1,
            "parsed": {"title": "non identificato", "raw_entry": citation},
            "resolve": {"matched_title": "Unverified resolver suggestion"},
        }],
    })

    assert rows[0]["title"] == citation
