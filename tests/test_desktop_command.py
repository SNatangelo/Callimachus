# tests/test_desktop_command.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from pathlib import Path

import pytest

from core.app.commands import desktop
from core.app import runtime_paths
from core.fetch.storage import content_store
from core.infra.db import RunRepository


def test_desktop_command_builds_a_references_only_run_without_llm(
    tmp_path, monkeypatch
):
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
    (root / "docs" / "guide").mkdir(parents=True)
    (root / "docs" / "guide" / "README.md").write_text(
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
                "status": "open", "winner_call_id": "candidate-in-retry",
                "jury1_outcome": "contradicts",
            },
        ],
    })

    assert rows[0]["status"] == "in_corso"
    assert rows[0]["result"] == "Jury1: supports"
    assert "contradicts" not in rows[0]["result"]


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
    (root / "docs" / "guide").mkdir(parents=True)
    (root / "docs" / "guide" / "README.md").write_text("# Guide", encoding="utf-8")
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
    (root / "docs" / "guide").mkdir(parents=True)
    (root / "docs" / "guide" / "README.md").write_text(
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
        "label": "freetoken — local-model",
    }]
    specification = captured["verify_fork_command_builder"](
        {"run_dir": str(root / "runs" / "parent"), "paper": "paper.pdf"},
        ["freetoken:local-model"],
    )

    assert "--fork-completed-verify" in specification["command"]
    assert str(root / "runs" / "parent") in specification["command"]
    assert specification["run_dir"] != str(root / "runs" / "parent")
    assert specification["environment"] == {
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
        "FREETOKEN_MODEL": "local-model",
    }
    with pytest.raises(ValueError, match="currently configured"):
        captured["verify_fork_command_builder"](
            {"run_dir": str(root / "runs" / "parent")}, ["unknown"]
        )


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
