# tests/test_production_credentials.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.app import runtime_paths
from core.app.settings import credentials as credential_store
from core.app.commands import desktop
from core.app.settings.config import effective_environment, load_settings_inventory
from core.app.settings.backup import transfer_settings


_BACKEND_MODULES = {
    "Windows": ("keyring.backends.Windows", "WinVaultKeyring"),
    "Darwin": ("keyring.backends.macOS", "Keyring"),
    "Linux": ("keyring.backends.SecretService", "Keyring"),
}


def _fake_vault(monkeypatch, system="Windows", *, failure=None, reads=None):
    values = {}

    class FakeBackend:
        def get_password(self, service, username):
            if reads is not None:
                reads.append((service, username))
            if failure == "read":
                raise RuntimeError("synthetic credential secret in backend error")
            return values.get((service, username))

        def set_password(self, service, username, password):
            if failure == "write":
                raise RuntimeError(
                    "synthetic credential secret in backend error: " + password
                )
            values[(service, username)] = password

        def delete_password(self, service, username):
            values.pop((service, username), None)

    module_name, class_name = _BACKEND_MODULES[system]

    def import_module(name):
        if name != module_name:
            raise ImportError(name)
        return SimpleNamespace(**{class_name: FakeBackend})

    monkeypatch.setattr(credential_store, "platform", SimpleNamespace(system=lambda: system))
    monkeypatch.setattr(
        credential_store,
        "importlib",
        SimpleNamespace(import_module=import_module),
    )
    if system == "Darwin":
        from core.app.settings import macos_keychain
        from contextlib import nullcontext
        from core.app.settings import macos_vault
        monkeypatch.setattr(macos_keychain, "read_without_ui", FakeBackend().get_password)
        def read_item(service, account, *, interactive=False, reference=None):
            value=FakeBackend().get_password(service,account)
            return value, (account.encode() if value is not None else None)
        def write_item(service, account, value, *, reference=None):
            FakeBackend().set_password(service,account,value); return account.encode()
        def delete_item(service, account, reference):
            assert reference==account.encode(); FakeBackend().delete_password(service,account)
        monkeypatch.setattr(macos_keychain,"read_item",read_item)
        monkeypatch.setattr(macos_keychain,"write_value",write_item)
        monkeypatch.setattr(macos_keychain,"delete_item",delete_item)
        monkeypatch.setattr(macos_vault, "_writer_lock", lambda *_a: nullcontext())
    return values


def _write_template(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    template = root / "settings.defaults"
    template.write_text(
        "OPENAI_API_KEY=\n"
        "CITATION_VERIFIER_SIGNING_KEY=\n"
        "CITATION_VERIFIER_ACCURACY=standard\n"
        "CITATION_VERIFIER_VERIFY_BACKENDS=\n",
        encoding="utf-8",
    )
    return template


def _frozen(monkeypatch, bundle: Path, data: Path) -> None:
    _write_template(bundle)
    monkeypatch.setattr(runtime_paths, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime_paths, "resource_root", lambda: bundle)
    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setenv("CALLIMACHUS_DATA_DIR", str(data))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CITATION_VERIFIER_SIGNING_KEY", raising=False)
    monkeypatch.delenv("CITATION_VERIFIER_SIGNING_KEY_FILE", raising=False)
    monkeypatch.delenv("CITATION_VERIFIER_ACCURACY", raising=False)
    monkeypatch.setattr(runtime_paths, "_LOADED_ENV_VALUES", {})


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_frozen_secret_round_trip_uses_only_the_explicit_platform_backend(
    monkeypatch, system,
):
    _fake_vault(monkeypatch, system)
    monkeypatch.delenv("CALLIMACHUS_DATA_DIR", raising=False)

    with credential_store.update_secret_values({
        "OPENAI_API_KEY": "synthetic-vault-key",
        "CITATION_VERIFIER_SIGNING_KEY": "synthetic-signing-key",
    }):
        assert credential_store.load_secret_values({
            "OPENAI_API_KEY", "CITATION_VERIFIER_SIGNING_KEY",
        }) == {
            "OPENAI_API_KEY": "synthetic-vault-key",
            "CITATION_VERIFIER_SIGNING_KEY": "synthetic-signing-key",
        }

    with credential_store.update_secret_values({"OPENAI_API_KEY": ""}):
        assert credential_store.load_secret_values({"OPENAI_API_KEY"}) == {}
    assert credential_store.load_secret_values({
        "CITATION_VERIFIER_SIGNING_KEY",
    }) == {"CITATION_VERIFIER_SIGNING_KEY": "synthetic-signing-key"}


def test_runtime_paths_keep_source_dotenv_and_select_frozen_aliases(
    monkeypatch, tmp_path,
):
    source_root = tmp_path / "source"
    data_root = tmp_path / "profile"
    monkeypatch.setattr(runtime_paths, "is_frozen", lambda: False)
    assert runtime_paths.environment_file(source_root=source_root) == source_root / ".env"
    assert runtime_paths.settings_template_file(source_root=source_root) == source_root / ".env.example"

    monkeypatch.setattr(runtime_paths, "is_frozen", lambda: True)
    monkeypatch.setenv("CALLIMACHUS_DATA_DIR", str(data_root))
    assert runtime_paths.environment_file(source_root=source_root) == data_root / "settings.conf"
    assert runtime_paths.settings_template_file(source_root=source_root) == source_root / "settings.defaults"


def test_source_environment_still_loads_plaintext_without_opening_vault(
    monkeypatch, tmp_path,
):
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=synthetic-dev-key\n", encoding="utf-8")
    monkeypatch.setattr(runtime_paths, "is_frozen", lambda: False)
    monkeypatch.setattr(runtime_paths, "_LOADED_ENV_VALUES", {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def unexpected_backend(_system):
        raise AssertionError("source mode must not open the OS credential store")

    monkeypatch.setattr(credential_store, "_backend_for_system", unexpected_backend)
    runtime_paths.load_environment_file(path)

    assert os.environ["OPENAI_API_KEY"] == "synthetic-dev-key"
    assert "OPENAI_API_KEY" not in runtime_paths.process_environment_without_dotenv()


def test_frozen_loader_ignores_legacy_env_and_plaintext_secret_lines(
    monkeypatch, tmp_path,
):
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    vault = _fake_vault(monkeypatch)
    vault[("Callimachus", credential_store._username("OPENAI_API_KEY"))] = "vault-key"
    legacy = bundle / ".env"
    legacy.write_text("OPENAI_API_KEY=legacy-plaintext-key\n", encoding="utf-8")
    config = data / "settings.conf"
    config.parent.mkdir(parents=True)
    config.write_text(
        "CITATION_VERIFIER_ACCURACY=maximum\n"
        "OPENAI_API_KEY=config-plaintext-key\n",
        encoding="utf-8",
    )

    runtime_paths.load_environment_file(legacy)
    assert os.environ["OPENAI_API_KEY"] == "vault-key"
    assert os.environ["CITATION_VERIFIER_ACCURACY"] == "maximum"
    process_environment = runtime_paths.process_environment_without_dotenv()
    assert "OPENAI_API_KEY" not in process_environment
    assert "CITATION_VERIFIER_ACCURACY" not in process_environment
    assert legacy.read_text(encoding="utf-8") == "OPENAI_API_KEY=legacy-plaintext-key\n"

    rows = {
        row["name"]: row
        for row in load_settings_inventory(
            env_path=config, template_path=bundle / "settings.defaults", environ={},
        )
    }
    assert rows["OPENAI_API_KEY"]["source"] == "credential_store"
    assert rows["OPENAI_API_KEY"]["configured"] is True
    assert rows["OPENAI_API_KEY"]["value"] == ""
    assert rows["OPENAI_API_KEY"]["display_value"] == "••••••••"
    assert "legacy-plaintext-key" not in repr(rows)
    assert "config-plaintext-key" not in repr(rows)


def test_external_process_secret_override_stays_above_vault_value(
    monkeypatch, tmp_path,
):
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    vault = _fake_vault(monkeypatch)
    vault[("Callimachus", credential_store._username("OPENAI_API_KEY"))] = "saved-vault-key"
    config = data / "settings.conf"
    config.parent.mkdir(parents=True)
    config.write_text("OPENAI_API_KEY=config-plaintext-key\n", encoding="utf-8")

    saved = effective_environment(env_path=config, environ={})
    overridden = effective_environment(
        env_path=config, environ={"OPENAI_API_KEY": "external-process-key"},
    )
    assert saved["OPENAI_API_KEY"] == "saved-vault-key"
    assert overridden["OPENAI_API_KEY"] == "external-process-key"
    assert "config-plaintext-key" not in repr(saved)


def test_startup_secret_snapshot_is_invalidated_after_commit_and_rollback(
    monkeypatch, tmp_path,
):
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    vault = _fake_vault(monkeypatch)
    name = "OPENAI_API_KEY"
    vault[("Callimachus", credential_store._username(name))] = "synthetic-old-key"

    with credential_store._capture_startup_secret_snapshot():
        assert credential_store.load_secret_values({name}) == {
            name: "synthetic-old-key",
        }
        with pytest.raises(OSError, match="synthetic settings write failure"):
            with credential_store.update_secret_values({name: "synthetic-new-key"}):
                assert credential_store.load_secret_values({name}) == {
                    name: "synthetic-new-key",
                }
                raise OSError("synthetic settings write failure")
        assert credential_store.load_secret_values({name}) == {
            name: "synthetic-old-key",
        }

        with credential_store.update_secret_values({name: "synthetic-committed-key"}):
            pass
        assert credential_store.load_secret_values({name}) == {
            name: "synthetic-committed-key",
        }


def test_frozen_desktop_startup_reuses_vault_snapshot_only_during_construction(
    monkeypatch, tmp_path,
):
    import run as entrypoint
    from core.gui import desktop as desktop_gui
    from core.gui import startup_splash

    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    (bundle / "settings.defaults").write_text(
        "OPENAI_API_KEY=\n"
        "ANTHROPIC_API_KEY=\n"
        "CITATION_VERIFIER_SIGNING_KEY=\n"
        "CITATION_VERIFIER_VERIFY_BACKENDS=\n",
        encoding="utf-8",
    )
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.conf").write_text(
        "CITATION_VERIFIER_VERIFY_BACKENDS=\n", encoding="utf-8",
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    reads = []
    vault = _fake_vault(monkeypatch, reads=reads)
    initial_values = {
        "OPENAI_API_KEY": "synthetic-openai-key",
        "CITATION_VERIFIER_SIGNING_KEY": "synthetic-signing-key",
    }
    for name, value in initial_values.items():
        vault[("Callimachus", credential_store._username(name))] = value

    monkeypatch.setattr(entrypoint, "is_frozen", lambda: True)
    monkeypatch.setattr(entrypoint, "_prepare_frozen_desktop", lambda: True)
    monkeypatch.setattr(entrypoint, "_check_runtime_dependencies", lambda: None)
    startup_errors = []
    monkeypatch.setattr(entrypoint, "show_startup_error", startup_errors.append)
    monkeypatch.setattr(desktop, "_root", lambda: bundle)
    monkeypatch.setattr(desktop_gui, "_load_language_preference", lambda: None)
    monkeypatch.setattr(
        desktop_gui, "_load_settings_environment_preferences", lambda: [],
    )

    def run_desktop_during_startup(**callbacks):
        assert len(reads) == 3
        current = effective_environment(
            env_path=data / "settings.conf",
            environ=runtime_paths.process_environment_without_dotenv(),
        )
        assert {
            name: current[name] for name in initial_values
        } == initial_values
        assert "ANTHROPIC_API_KEY" not in current
        assert callbacks["verify_candidates_loader"]() == []
        callbacks["verify_catalog_preview_loader"](
            {}, {"jury1": [], "jury2": []}, "medium",
        )
        assert len(reads) == 3

        original_data_dir = os.environ["CALLIMACHUS_DATA_DIR"]
        monkeypatch.setenv("CALLIMACHUS_DATA_DIR", str(tmp_path / "other-profile"))
        try:
            other_profile = callbacks["verify_candidates_loader"]()
            assert other_profile == []
            assert len(reads) == 6
        finally:
            monkeypatch.setenv("CALLIMACHUS_DATA_DIR", original_data_dir)

        assert callbacks["verify_candidates_loader"]() == []
        assert len(reads) == 6
        return 0

    monkeypatch.setattr(desktop_gui, "run_desktop", run_desktop_during_startup)
    monkeypatch.setattr(
        startup_splash,
        "run_with_splash",
        lambda *, prepare, start: (prepare(), start())[1],
    )
    monkeypatch.setattr(entrypoint.sys, "argv", ["Callimachus", "app"])

    assert entrypoint._main() == 0
    assert startup_errors == []
    assert len(reads) == 6
    current = effective_environment(
        env_path=data / "settings.conf",
        environ=runtime_paths.process_environment_without_dotenv(),
    )
    assert current["OPENAI_API_KEY"] == initial_values["OPENAI_API_KEY"]
    assert current["CITATION_VERIFIER_SIGNING_KEY"] == initial_values[
        "CITATION_VERIFIER_SIGNING_KEY"
    ]
    assert len(reads) == 9

    with credential_store._capture_startup_secret_snapshot() as snapshot:
        assert credential_store.load_secret_values({"OPENAI_API_KEY"}) == {
            "OPENAI_API_KEY": initial_values["OPENAI_API_KEY"],
        }
    reads_before_failed_start = len(reads)

    def fail_desktop_construction(*_args):
        assert credential_store.load_secret_values({"OPENAI_API_KEY"}) == {
            "OPENAI_API_KEY": initial_values["OPENAI_API_KEY"],
        }
        raise RuntimeError("synthetic desktop construction failure")

    monkeypatch.setattr(entrypoint, "_dispatch", fail_desktop_construction)
    with pytest.raises(RuntimeError, match="synthetic desktop construction failure"):
        entrypoint._launch_desktop(
            "core.app.commands.desktop", [], "Callimachus app",
            startup_secret_snapshot=snapshot,
        )
    assert snapshot.values == {}
    assert credential_store.load_secret_values({"OPENAI_API_KEY"}) == {
        "OPENAI_API_KEY": initial_values["OPENAI_API_KEY"],
    }
    assert len(reads) == reads_before_failed_start + 1


def test_desktop_save_stores_secrets_in_vault_and_config_only_contains_blanks(
    monkeypatch, tmp_path,
):
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    _fake_vault(monkeypatch)
    captured = {}
    monkeypatch.setattr(desktop, "_root", lambda: bundle)
    monkeypatch.setattr(desktop, "process_environment_without_dotenv", lambda: {})
    monkeypatch.setattr(
        "core.gui.desktop._load_settings_environment_preferences", lambda: [],
    )
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    assert desktop.main(["--runs-root", str(data / "runs")]) == 0
    config = data / "settings.conf"
    captured["settings_saver"]({
        "OPENAI_API_KEY": "synthetic-desktop-secret",
        "CITATION_VERIFIER_ACCURACY": "maximum",
    })

    text = config.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=" in text
    assert "OPENAI_API_KEY=synthetic-desktop-secret" not in text
    assert "synthetic-desktop-secret" not in text
    assert credential_store.load_secret_values({"OPENAI_API_KEY"}) == {
        "OPENAI_API_KEY": "synthetic-desktop-secret",
    }
    row = {
        item["name"]: item for item in captured["settings_loader"]()
    }["OPENAI_API_KEY"]
    assert row["source"] == "credential_store"
    assert row["display_value"] == "••••••••"
    assert "synthetic-desktop-secret" not in repr(row)


def test_desktop_save_rolls_back_vault_when_settings_file_write_fails(
    monkeypatch, tmp_path,
):
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    _fake_vault(monkeypatch)
    captured = {}
    monkeypatch.setattr(desktop, "_root", lambda: bundle)
    monkeypatch.setattr(desktop, "process_environment_without_dotenv", lambda: {})
    monkeypatch.setattr(
        "core.gui.desktop._load_settings_environment_preferences", lambda: [],
    )
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    assert desktop.main(["--runs-root", str(data / "runs")]) == 0

    def fail_settings_file_write(*args, **kwargs):
        raise OSError("synthetic config write failure")

    monkeypatch.setattr(desktop, "write_env_updates", fail_settings_file_write)
    with pytest.raises(OSError, match="synthetic config write failure"):
        captured["settings_saver"]({"OPENAI_API_KEY": "synthetic-rollback-secret"})

    assert credential_store.load_secret_values({"OPENAI_API_KEY"}) == {}
    assert not (data / "settings.conf").exists()


@pytest.mark.parametrize("failure", ["read", "write"])
def test_vault_failure_is_safe_and_never_falls_back_to_disk(
    monkeypatch, tmp_path, failure,
):
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    _fake_vault(monkeypatch, failure=failure)

    with pytest.raises(credential_store.CredentialStoreError) as caught:
        with credential_store.update_secret_values({
            "OPENAI_API_KEY": "synthetic-failing-secret",
        }):
            raise AssertionError("the protected write should fail before file output")
    assert "synthetic-failing-secret" not in str(caught.value)
    assert "synthetic credential secret" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert not (data / "settings.conf").exists()


def test_unavailable_platform_backend_fails_closed_without_disk_fallback(
    monkeypatch, tmp_path,
):
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    monkeypatch.setattr(
        credential_store, "platform", SimpleNamespace(system=lambda: "Windows"),
    )

    def unavailable_backend(_name):
        raise ModuleNotFoundError("synthetic secret from unavailable backend")

    monkeypatch.setattr(
        credential_store,
        "importlib",
        SimpleNamespace(import_module=unavailable_backend),
    )
    with pytest.raises(credential_store.CredentialStoreError) as caught:
        with credential_store.update_secret_values({
            "OPENAI_API_KEY": "synthetic-unavailable-secret",
        }):
            raise AssertionError("the unavailable vault must fail before file output")

    assert "synthetic-unavailable-secret" not in str(caught.value)
    assert "synthetic secret from unavailable backend" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert not (data / "settings.conf").exists()


def test_backup_export_import_and_reset_route_secrets_through_vault(
    monkeypatch, tmp_path,
):
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    vault = _fake_vault(monkeypatch)
    config = data / "settings.conf"
    config.parent.mkdir(parents=True)
    config.write_text("CITATION_VERIFIER_ACCURACY=standard\n", encoding="utf-8")
    template = bundle / "settings.defaults"
    inventory = [
        {"name": "OPENAI_API_KEY", "locked": False},
        {"name": "CITATION_VERIFIER_SIGNING_KEY", "locked": False},
        {"name": "CITATION_VERIFIER_ACCURACY", "locked": False},
        {"name": "CITATION_VERIFIER_VERIFY_BACKENDS", "locked": False},
    ]
    vault[("Callimachus", credential_store._username("OPENAI_API_KEY"))] = "synthetic-backup-secret"
    backup = tmp_path / "settings.json"

    exported = transfer_settings(
        "export", {"path": str(backup), "password": None},
        env_path=config, template_path=template,
        inventory_loader=lambda: inventory,
    )
    assert exported["ok"] is True
    document = json.loads(backup.read_text(encoding="utf-8"))
    assert document["encrypted"] is False
    assert document["payload"]["environment"]["OPENAI_API_KEY"] == "synthetic-backup-secret"

    with credential_store.update_secret_values({"OPENAI_API_KEY": ""}):
        pass
    imported = transfer_settings(
        "import", {"path": str(backup), "password": None},
        env_path=config, template_path=template,
        inventory_loader=lambda: inventory,
    )
    assert imported["ok"] is True
    assert credential_store.load_secret_values({"OPENAI_API_KEY"}) == {
        "OPENAI_API_KEY": "synthetic-backup-secret",
    }
    assert "synthetic-backup-secret" not in config.read_text(encoding="utf-8")

    kept = transfer_settings(
        "reset", {"keep_api_keys": True}, env_path=config,
        template_path=template, inventory_loader=lambda: inventory,
    )
    assert kept["ok"] is True
    assert credential_store.load_secret_values({"OPENAI_API_KEY"}) == {
        "OPENAI_API_KEY": "synthetic-backup-secret",
    }

    cleared = transfer_settings(
        "reset", {"keep_api_keys": False}, env_path=config,
        template_path=template, inventory_loader=lambda: inventory,
    )
    assert cleared["ok"] is True
    assert credential_store.load_secret_values({"OPENAI_API_KEY"}) == {}
    assert "synthetic-backup-secret" not in config.read_text(encoding="utf-8")


@pytest.mark.parametrize("password", [None, "backup-passphrase"])
def test_backup_import_preserves_optional_encryption_behavior(
    monkeypatch, tmp_path, password,
):
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    vault = _fake_vault(monkeypatch)
    config = data / "settings.conf"
    config.parent.mkdir(parents=True)
    config.write_text("CITATION_VERIFIER_ACCURACY=standard\n", encoding="utf-8")
    template = bundle / "settings.defaults"
    inventory = [
        {"name": "OPENAI_API_KEY", "locked": False},
        {"name": "CITATION_VERIFIER_SIGNING_KEY", "locked": False},
        {"name": "CITATION_VERIFIER_ACCURACY", "locked": False},
        {"name": "CITATION_VERIFIER_VERIFY_BACKENDS", "locked": False},
    ]
    vault[("Callimachus", credential_store._username("OPENAI_API_KEY"))] = "synthetic-encrypted-secret"
    backup = tmp_path / "optional-encryption.json"

    result = transfer_settings(
        "export", {"path": str(backup), "password": password},
        env_path=config, template_path=template,
        inventory_loader=lambda: inventory,
    )
    assert result["ok"] is True
    assert json.loads(backup.read_text(encoding="utf-8"))["encrypted"] is (password is not None)
    with credential_store.update_secret_values({"OPENAI_API_KEY": ""}):
        pass
    result = transfer_settings(
        "import", {"path": str(backup), "password": password},
        env_path=config, template_path=template,
        inventory_loader=lambda: inventory,
    )
    assert result["ok"] is True
    assert credential_store.load_secret_values({"OPENAI_API_KEY"}) == {
        "OPENAI_API_KEY": "synthetic-encrypted-secret",
    }


def test_frozen_signing_key_generator_saves_inline_key_to_vault(
    monkeypatch, tmp_path,
):
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    vault = _fake_vault(monkeypatch)
    key_name = "CITATION_VERIFIER_SIGNING_KEY"
    file_path = tmp_path / "existing-signing-key"
    file_path.write_text("synthetic-file-key\n", encoding="utf-8")
    monkeypatch.setenv("CITATION_VERIFIER_SIGNING_KEY_FILE", str(file_path))
    config = data / "settings.conf"
    config.parent.mkdir(parents=True)
    config.write_text(
        "CITATION_VERIFIER_SIGNING_KEY_FILE=" + str(file_path) + "\n",
        encoding="utf-8",
    )
    vault[("Callimachus", credential_store._username(key_name))] = (
        "synthetic-existing-vault-key"
    )
    captured = {}
    monkeypatch.setattr(desktop, "_root", lambda: bundle)
    monkeypatch.setattr(
        "core.gui.desktop._load_settings_environment_preferences", lambda: [],
    )
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    monkeypatch.setattr(
        desktop, "generate_key",
        lambda *_args, **_kwargs: pytest.fail("frozen builds must not create a key file"),
    )

    assert desktop.main(["--runs-root", str(data / "runs")]) == 0
    result = captured["signing_key_generator"]()
    assert result == {"path": "", "created": False, "storage": "keyring"}
    existing_key = credential_store.load_secret_values({key_name})[key_name]
    assert existing_key == (
        "synthetic-existing-vault-key"
    )

    rotated = captured["signing_key_generator"](regenerate=True)
    assert rotated == {
        "path": "", "created": True, "storage": "keyring", "regenerated": True,
    }
    rotated_key = credential_store.load_secret_values({key_name})[key_name]
    assert len(rotated_key) == 64
    assert rotated_key != existing_key
    assert rotated_key not in repr(rotated)

    current_importlib = credential_store.importlib

    class WriteFailingBackend:
        def get_password(self, service, username):
            return vault.get((service, username))

        def set_password(self, service, username, password):
            raise RuntimeError("synthetic credential secret in backend error: " + password)

        def delete_password(self, service, username):
            vault.pop((service, username), None)

    module_name, class_name = _BACKEND_MODULES["Windows"]

    def write_failing_import(name):
        if name != module_name:
            raise ImportError(name)
        return SimpleNamespace(**{class_name: WriteFailingBackend})

    monkeypatch.setattr(
        credential_store, "importlib",
        SimpleNamespace(import_module=write_failing_import),
    )
    with pytest.raises(credential_store.CredentialStoreError) as caught:
        captured["signing_key_generator"](regenerate=True)
    assert credential_store.load_secret_values({key_name})[key_name] == rotated_key
    assert "synthetic credential secret" not in str(caught.value)
    assert rotated_key not in str(caught.value)

    monkeypatch.setattr(credential_store, "importlib", current_importlib)

    with credential_store.update_secret_values({key_name: ""}):
        pass
    result = captured["signing_key_generator"]()
    assert result == {
        "path": "", "created": True, "storage": "keyring", "regenerated": False,
    }
    new_key = credential_store.load_secret_values({key_name})[key_name]
    assert len(new_key) == 64
    assert new_key != rotated_key
    assert new_key not in config.read_text(encoding="utf-8")
    assert "CITATION_VERIFIER_SIGNING_KEY_FILE=" + str(file_path) in (
        config.read_text(encoding="utf-8")
    )
    assert file_path.read_text(encoding="utf-8") == "synthetic-file-key\n"

    monkeypatch.setenv(key_name, "synthetic-process-override")
    generator = captured["signing_key_generator"]
    assert generator() == {
        "path": "", "created": False, "storage": "environment",
    }
    with pytest.raises(RuntimeError, match="process environment"):
        generator(regenerate=True)
    assert credential_store.load_secret_values({key_name})[key_name] == new_key


def test_frozen_reset_clears_removed_loaded_secrets_and_keeps_process_overrides(
    monkeypatch, tmp_path,
):
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    _frozen(monkeypatch, bundle, data)
    (bundle / "settings.defaults").write_text(
        "OPENAI_API_KEY=\n"
        "ANTHROPIC_API_KEY=\n"
        "CITATION_VERIFIER_ACCURACY=standard\n",
        encoding="utf-8",
    )
    vault = _fake_vault(monkeypatch)
    openai = "synthetic-old-openai-key"
    signing = "synthetic-old-signing-key"
    inherited = "synthetic-inherited-anthropic-key"
    vault[("Callimachus", credential_store._username("OPENAI_API_KEY"))] = openai
    vault[("Callimachus", credential_store._username("ANTHROPIC_API_KEY"))] = (
        "synthetic-saved-anthropic-key"
    )
    vault[("Callimachus", credential_store._username("CITATION_VERIFIER_SIGNING_KEY"))] = signing
    monkeypatch.setenv("OPENAI_API_KEY", openai)
    monkeypatch.setenv("CITATION_VERIFIER_SIGNING_KEY", signing)
    monkeypatch.setenv("ANTHROPIC_API_KEY", inherited)
    monkeypatch.setattr(runtime_paths, "_LOADED_ENV_VALUES", {
        "OPENAI_API_KEY": openai,
        "CITATION_VERIFIER_SIGNING_KEY": signing,
    })
    captured = {}
    monkeypatch.setattr(desktop, "_root", lambda: bundle)
    monkeypatch.setattr(
        "core.gui.desktop._load_settings_environment_preferences", lambda: [],
    )
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    assert desktop.main(["--runs-root", str(data / "runs")]) == 0
    result = captured["settings_transfer_callback"](
        "reset", {"keep_api_keys": False},
    )

    assert result["ok"] is True
    assert "OPENAI_API_KEY" not in os.environ
    assert "CITATION_VERIFIER_SIGNING_KEY" not in os.environ
    assert os.environ["ANTHROPIC_API_KEY"] == inherited
    from core.infra.integrity.signing import load_key

    assert load_key() is None
    assert credential_store.load_secret_values({
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CITATION_VERIFIER_SIGNING_KEY",
    }) == {}
