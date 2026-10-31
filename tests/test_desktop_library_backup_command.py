# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path

import pytest

from core.app import cache_maintenance
from core.app.library import backup as library_backup
from core.fetch.storage import content_store
from tests.test_desktop_library_command import callbacks  # noqa: F401


@pytest.mark.parametrize("action", ["import", "export"])
def test_archive_callback_uses_configured_store(callbacks, monkeypatch, tmp_path, action):
    captured, runs, state = callbacks
    calls = []

    def transfer(path, *, run_dir, environ, before_mutation=None):
        calls.append(path)
        assert run_dir == str(runs / ".desktop-cache-anchor")
        assert Path(environ[content_store.ENV_STATE_DIR]) == state
        if before_mutation is not None:
            before_mutation()
        return {"originals": 1, "texts": 2, "skipped": 0}

    monkeypatch.setattr(library_backup, action + "_library", transfer)
    path = str(tmp_path / "library.zip")
    assert captured["library_transfer_callback"](action, path)["texts"] == 2
    assert calls == [path]


@pytest.mark.parametrize("failure", [False, True])
def test_archive_import_coordinates_shared_store_authority(callbacks, monkeypatch, failure):
    captured, runs, _ = callbacks
    monkeypatch.setenv("CITATION_VERIFIER_INTEGRITY_SOCKET", "authority")
    from core.app.commands import desktop
    monkeypatch.setattr(desktop, "_maintenance_run", lambda root: str(runs / "inactive"))
    events = []

    class Gate:
        def begin_pipeline_transition(self, run, **kwargs):
            assert kwargs["mutates_content_store"] is True
            events.append("begin")
            return "lease"

        def commit_pipeline_transition(self, run, lease):
            assert lease == "lease"
            events.append("commit")

    monkeypatch.setattr(cache_maintenance, "_environment_gate", lambda *a, **k: Gate())

    def restore(*a, **k):
        events.append("validate")
        if failure:
            raise ValueError("invalid archive")
        k["before_mutation"]()
        events.append("import")
        return {"texts": 1}

    monkeypatch.setattr(library_backup, "import_library", restore)
    if failure:
        with pytest.raises(ValueError, match="invalid archive"):
            captured["library_transfer_callback"]("import", "backup.zip")
        assert events == ["validate"]
    else:
        assert captured["library_transfer_callback"]("import", "backup.zip") == {"texts": 1}
        assert events == ["validate", "begin", "import", "commit"]


def test_unknown_archive_action_rejected(callbacks):
    captured, _, _ = callbacks
    with pytest.raises(ValueError, match="unsupported"):
        captured["library_transfer_callback"]("replace", "backup.zip")


def test_invalid_archive_does_not_block_subsequent_protected_import(callbacks, monkeypatch, tmp_path):
    captured, runs, state = callbacks
    from core.app.commands import desktop

    monkeypatch.setenv("CITATION_VERIFIER_INTEGRITY_SOCKET", "authority")
    monkeypatch.setattr(desktop, "_maintenance_run", lambda root: str(runs / "inactive"))
    events = []

    class Gate:
        def begin_pipeline_transition(self, *a, **k):
            events.append("begin")
            return "lease"

        def commit_pipeline_transition(self, *a):
            events.append("commit")

    monkeypatch.setattr(cache_maintenance, "_environment_gate", lambda *a, **k: Gate())
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not an archive")
    with pytest.raises(library_backup.LibraryBackupError):
        captured["library_transfer_callback"]("import", str(invalid))
    assert events == []
    valid = tmp_path / "valid.zip"
    source_env = {content_store.ENV_STATE_DIR: str(tmp_path / "source")}
    content_store.record_parsed_text(
        str(runs / "source"), {"doi": "10.5555/protected.restore", "title": "Local archive test"},
        "abstract", "user", "A healthy abstract for a local archive test.",
        supplied_by="user", environ=source_env,
    )
    library_backup.export_library(valid, run_dir=str(runs / "empty"),
                                  environ=source_env)
    assert captured["library_transfer_callback"]("import", str(valid))["texts"] == 1
    assert events == ["begin", "commit"]


def test_interruption_after_restore_starts_keeps_transition_recoverable(callbacks, monkeypatch):
    captured, runs, _ = callbacks
    from core.app.commands import desktop

    monkeypatch.setenv("CITATION_VERIFIER_INTEGRITY_SOCKET", "authority")
    monkeypatch.setattr(desktop, "_maintenance_run", lambda root: str(runs / "inactive"))
    events = []

    class Gate:
        def begin_pipeline_transition(self, *a, **k):
            events.append("begin")
            return "lease"

        def commit_pipeline_transition(self, *a):
            pytest.fail("failed import must not commit")

        def abort_pipeline_transition(self, *a, **k):
            pytest.fail("an interrupted store mutation must remain recoverable")

    monkeypatch.setattr(cache_maintenance, "_environment_gate", lambda *a, **k: Gate())

    def interrupted(*a, **k):
        k["before_mutation"]()
        raise OSError("interrupted restore")

    monkeypatch.setattr(library_backup, "import_library", interrupted)
    with pytest.raises(OSError, match="interrupted restore"):
        captured["library_transfer_callback"]("import", "valid.zip")
    assert events == ["begin"]
