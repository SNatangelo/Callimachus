# tests/test_desktop_library_command.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Desktop library callbacks preserve storage and integrity boundaries."""
from pathlib import Path

import pytest

from core.app.commands import desktop
from core.app import cache_maintenance
from core.fetch.storage import content_store


@pytest.fixture
def callbacks(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env.example").write_text("", encoding="utf-8")
    state = tmp_path / "configured-state"
    (root / ".env").write_text(
        f"{content_store.ENV_STATE_DIR}={state}\n", encoding="utf-8",
    )
    monkeypatch.delenv(content_store.ENV_STATE_DIR, raising=False)
    monkeypatch.delenv("CITATION_VERIFIER_INTEGRITY_SOCKET", raising=False)
    monkeypatch.setattr(desktop, "_root", lambda: root)
    captured = {}
    monkeypatch.setattr(
        "core.gui.desktop.run_desktop", lambda **kwargs: captured.update(kwargs) or 0,
    )
    runs = tmp_path / "runs"
    assert desktop.main(["--runs-root", str(runs)]) == 0
    return captured, runs, state


def test_library_import_and_pending_work_before_any_analysis(callbacks, monkeypatch):
    captured, runs, state = callbacks
    calls = []

    class FakeImporter:
        review_result = {"status": "identified", "doi": "10.1234/reviewed"}

        def __init__(self, anchor, environ, references=()):
            assert anchor == str(runs / ".desktop-cache-anchor")
            assert Path(environ[content_store.ENV_STATE_DIR]) == state
            calls.append(("init", list(references)))

        def inspect(self, paths, *, progress_callback=None):
            calls.append(("inspect", paths, progress_callback))
            progress_callback({"stage": "scanning", "completed": 0, "total": 0})
            return [{"file": paths[0], "status": "needs_review"}]

        def pending_rows(self):
            return [{"file": "pending.pdf", "status": "unidentified"}]

        def review_identity(self, row, scheme="", identifier="", *, operator_attested=False):
            calls.append(("review_identity", row, scheme, identifier, operator_attested))
            return self.review_result

        def save(self, rows, *, progress_callback=None):
            calls.append(("save", rows, progress_callback))
            progress_callback({"stage": "queued", "completed": 0, "total": len(rows)})
            return [{**row, "status": "stored"} for row in rows]

    monkeypatch.setattr(desktop, "LibraryImporter", FakeImporter)
    monkeypatch.setattr(
        desktop, "load_cache_inventory",
        lambda anchor, environ: {"items": [{"title": "Known work", "doi": "10.1234/known"}]},
    )
    progress = []
    collector = progress.append
    rows = captured["library_import_callback"](["one.pdf", "folder"], collector)
    assert calls[:2] == [
        ("init", [{"title": "Known work", "doi": "10.1234/known"}]),
        ("inspect", ["one.pdf", "folder"], collector),
    ]
    review_row = {"_session_id": "session", "_file_id": "a" * 64, "title": "Known work"}
    assert captured["library_identity_callback"](
        review_row, "doi", "10.1234/reviewed"
    ) == FakeImporter.review_result
    assert calls[-2:] == [
        ("init", []),
        ("review_identity", review_row, "doi", "10.1234/reviewed", False),
    ]
    manual_row = dict(review_row, _operator_attested=True)
    assert captured["library_identity_callback"](manual_row, "", "") == FakeImporter.review_result
    assert manual_row["_operator_attested"] is True
    assert calls[-2:] == [
        ("init", []),
        ("review_identity", review_row, "", "", True),
    ]
    assert captured["library_pending_loader"]()[0]["file"] == "pending.pdf"
    assert captured["library_save_callback"](rows, collector)[0]["status"] == "stored"
    assert calls[-1] == ("save", rows, collector)
    assert progress == [
        {"stage": "scanning", "completed": 0, "total": 0},
        {"stage": "queued", "completed": 0, "total": 1},
    ]


@pytest.mark.parametrize("failure", [False, True])
def test_library_save_coordinates_authority_and_keeps_partial_failure_open(
    callbacks, monkeypatch, failure,
):
    captured, runs, _state = callbacks
    events = []
    monkeypatch.setenv("CITATION_VERIFIER_INTEGRITY_SOCKET", "authority-socket")
    monkeypatch.setattr(desktop, "_maintenance_run", lambda root: str(runs / "inactive"))

    class Gate:
        def begin_pipeline_transition(self, run, **kwargs):
            events.append(("begin", kwargs))
            return "lease"

        def commit_pipeline_transition(self, run, lease):
            assert lease == "lease"
            events.append(("commit",))

        def abort_pipeline_transition(self, *args, **kwargs):
            pytest.fail("a partial import must remain open for recovery")

    monkeypatch.setattr(cache_maintenance, "_environment_gate", lambda *a, **kw: Gate())

    class FakeImporter:
        def __init__(self, *args, **kwargs):
            pass

        def save(self, rows, *, progress_callback=None):
            events.append(("save",))
            if failure:
                raise RuntimeError("interrupted after partial write")
            return rows

    monkeypatch.setattr(desktop, "LibraryImporter", FakeImporter)
    if failure:
        with pytest.raises(RuntimeError, match="partial write"):
            captured["library_save_callback"]([{"sha256": "a" * 64}])
    else:
        assert captured["library_save_callback"]([{"sha256": "a" * 64}])
    assert [event[0] for event in events] == (
        ["begin", "save"] if failure else ["begin", "save", "commit"]
    )
    assert events[0][1]["mutates_content_store"] is True


def test_library_protected_save_does_not_fall_back_without_a_run(callbacks, monkeypatch):
    captured, _runs, _state = callbacks
    monkeypatch.setenv("CITATION_VERIFIER_INTEGRITY_SOCKET", "authority-socket")

    def unavailable(*args):
        raise RuntimeError("no inactive run")

    monkeypatch.setattr(desktop, "_maintenance_run", unavailable)
    monkeypatch.setattr(desktop, "LibraryImporter", lambda *a, **kw: pytest.fail("must fail closed"))
    with pytest.raises(RuntimeError, match="no inactive run"):
        captured["library_save_callback"]([{"sha256": "a" * 64}])
