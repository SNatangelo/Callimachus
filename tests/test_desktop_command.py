# tests/test_desktop_command.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.app.commands import desktop
from core.app import runtime_paths
from core.fetch.storage import content_store
from core.infra.db import RunRepository


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


def test_desktop_command_loads_and_saves_env_and_complete_guide(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_BOOKS_API_KEY", raising=False)
    monkeypatch.delenv("CITATION_VERIFIER_FETCH_WORKERS", raising=False)
    root = tmp_path / "project"
    guide = root / "docs" / "guide"
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
    rows = {row["name"]: row for row in captured["settings_loader"]()}
    google = rows["GOOGLE_BOOKS_API_KEY"]
    assert google["group"] == "resolution"
    assert google["explicitly_empty"] is True
    assert google["source"] == "env_file"
    assert google["value"] == google["display_value"] == ""
    assert rows["CITATION_VERIFIER_FETCH_WORKERS"]["source"] == "default"

    captured["settings_saver"]({"GOOGLE_BOOKS_API_KEY": "replacement"})
    assert (root / ".env").read_text(encoding="utf-8") == (
        "GOOGLE_BOOKS_API_KEY=replacement\n"
    )
    assert captured["docs_loader"]() == {
        "README.md": "# Complete guide",
        "01-quickstart.md": "# Quickstart",
    }


def test_docs_library_includes_linked_deployment_guides(tmp_path):
    root = tmp_path / "project"
    paths = {
        "README.md": root / "docs" / "guide" / "README.md",
        "../deployment/agent-guide.md": root / "docs" / "deployment" / "agent-guide.md",
        "../deployment/administrator-guide.md": root / "docs" / "deployment" / "administrator-guide.md",
        "../architecture/artifact-integrity.md": root / "docs" / "architecture" / "artifact-integrity.md",
        "../../DEPLOYMENT.md": root / "DEPLOYMENT.md",
    }
    for key, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {key}", encoding="utf-8")

    assert desktop._docs_library(root) == {
        key: f"# {key}" for key in paths
    }


def test_docs_library_loads_real_guide_chapters_including_cli_reference():
    root = Path(__file__).resolve().parents[1]
    documents = desktop._docs_library(root)
    chapter_paths = [
        f"{index:02d}-{name}.md"
        for index, name in enumerate((
            "quickstart", "cli-reference", "configuration", "pipeline",
            "tasks-and-recovery", "verification-and-evidence",
            "artifacts-and-provenance", "capabilities-and-limits",
            "troubleshooting", "keys-and-credentials", "environment-reference",
            "desktop-packages",
        ), 1)
    ]

    assert all(path in documents and documents[path].strip() for path in chapter_paths)
    assert "CLI" in documents["02-cli-reference.md"]


def test_desktop_signing_key_generator_persists_only_key_path(tmp_path, monkeypatch):
    root = tmp_path / "project"
    guide = root / "docs" / "guide"
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
    with pytest.raises(ValueError):
        desktop._delete_history_rows(
            runs_root, [{"run_dir": str(paused)}], protected_run_dir=str(paused)
        )
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
                "status": "open", "winner_call_id": "candidate-in-retry",
                "jury1_outcome": "contradicts",
            },
        ],
    })

    assert rows[0]["status"] == "in_corso"
    assert rows[0]["result"] == "Jury1: supports"
    assert "contradicts" not in rows[0]["result"]


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
        "label": "freetoken: local-model",
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
        ["freetoken:local-model"],
    )
    assert "--fork-reference-only-fetch" in reference_preview["command"]
    assert "--fork-completed-verify" not in reference_preview["command"]
    assert str(root / "runs" / "preview") in reference_preview["command"]
    with pytest.raises(ValueError, match="available Verify"):
        captured["verify_fork_command_builder"](
            {"run_dir": str(root / "runs" / "parent")}, ["unknown"]
        )


def test_desktop_history_verify_uses_deepseek_without_backend_flag(
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
        [options[0]["selector"]],
    )
    assert specification["environment"]["CITATION_VERIFIER_VERIFY_BACKENDS"] == (
        "openai_compatible"
    )
    assert specification["environment"]["OPENAI_MODEL"] == "deepseek-v4-flash"
    assert specification["environment"]["CITATION_VERIFIER_VERIFY_JURY2_LEVEL"] == (
        "medium"
    )


@pytest.mark.parametrize("references_only", [True, False])
def test_desktop_resume_rejects_completed_run(
    tmp_path, monkeypatch, references_only
):
    root = tmp_path / "project"
    (root / "docs" / "guide").mkdir(parents=True)
    (root / "docs" / "guide" / "README.md").write_text(
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
