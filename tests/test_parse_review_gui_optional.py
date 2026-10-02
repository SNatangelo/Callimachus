# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import pytest

_APP = None

_WINDOWS = []

def _qt(monkeypatch):
    global _APP
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication([])
    app = _APP
    return app, Qt

def _wait(app, predicate, timeout_ms=3000):
    from PySide6.QtTest import QTest

    elapsed = 0
    while elapsed < timeout_ms and not predicate():
        QTest.qWait(10)
        app.processEvents()
        elapsed += 10
    assert predicate(), "timed out waiting for Parse Review UI update"

def _task(kind, **values):
    base = {
        "task_id": f"review:{kind}",
        "review_kind": kind,
        "target_sha256": "abc123",
        "instructions": "Choose only after reviewing the visible target.",
    }
    base.update(values)
    return base

def _window(monkeypatch, **kwargs):
    from core.gui.parse_review import create_parse_review_window

    window = create_parse_review_window(
        "run", load_tasks=kwargs["load_tasks"],
        answer_task=kwargs["answer_task"],
        skip_remaining=kwargs.get("skip_remaining", lambda: None),
        on_complete=kwargs["on_complete"],
    )
    _WINDOWS.append(window)
    return window

def test_parse_review_missing_selection_confirms_and_keeps_unresolved_is_hash_bound(monkeypatch):
    app, _Qt = _qt(monkeypatch)
    from PySide6.QtWidgets import QMessageBox

    task = _task("citation_reference_review", candidates=[{"id": "r1", "origin": "parser", "score": 1.0}])
    answers = []
    skipped = []
    completed = []

    def answer(task_id, payload):
        answers.append((task_id, payload))

    window = _window(
        monkeypatch, load_tasks=lambda: [task], answer_task=answer,
        skip_remaining=lambda: skipped.append(True),
        on_complete=lambda: completed.append(True),
    )
    window.show()
    _wait(app, lambda: window._current is not None)
    assert window._selected_action() == "select_reference"
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Cancel)
    window.resume_button.click()
    assert answers == [] and skipped == [] and completed == []
    window._action_buttons["keep_unresolved"].click()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window.resume_button.click()
    assert answers == [(task["task_id"], {
        "action": "keep_unresolved", "target_sha256": "abc123", "reason": "",
    })]
    assert skipped == [] and completed == [True]
    window.close()
    app.processEvents()

def test_parse_review_refreshes_after_partial_batch_failure_without_resubmitting_saved_task(monkeypatch):
    app, _Qt = _qt(monkeypatch)
    tasks = [
        _task("citation_reference_review", task_id="first", candidates=[{"id": "r1"}]),
        _task("citation_reference_review", task_id="second", candidates=[{"id": "r2"}]),
    ]
    loads = []
    submitted = []

    def answer(task_id, payload):
        submitted.append(task_id)
        if task_id == "second":
            raise RuntimeError("second answer rejected")
        next(task for task in tasks if task["task_id"] == task_id)["db_status"] = "answered"

    window = _window(
        monkeypatch, load_tasks=lambda: loads.append(True) or list(tasks),
        answer_task=answer, on_complete=lambda: pytest.fail("partial batch must not resume"),
    )
    window.show()
    _wait(app, lambda: window._current is not None)
    window._candidate_group.buttons()[0].click()
    window.task_tabs.setCurrentIndex(1)
    window._candidate_group.buttons()[0].click()
    window.resume_button.click()
    _wait(app, lambda: "second answer rejected" in window.status.text())
    assert len(loads) == 2
    assert window._current["task_id"] == "second"
    assert window._candidate_group.checkedButton().property("candidate_id") == "r2"
    window.resume_button.click()
    _wait(app, lambda: len(loads) == 3 and "second answer rejected" in window.status.text())
    assert submitted == ["first", "second", "second"]
    window.close()
    app.processEvents()

def test_parse_review_close_reopen_reloads_durable_tasks(monkeypatch):
    app, _Qt = _qt(monkeypatch)
    pending = [_task("reference_identity_review", ref_number=1, raw_entry="first")]
    loads = []
    window = _window(
        monkeypatch,
        load_tasks=lambda: loads.append(True) or list(pending),
        answer_task=lambda *_: None,
        on_complete=lambda: None,
    )
    window.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose, False)
    window.show()
    _wait(app, lambda: window._current is not None)
    assert len(loads) == 1
    window.close()
    app.processEvents()
    pending[:] = [_task("citation_reference_review", marker_raw="[9]", candidates=[])]
    window.show()
    _wait(app, lambda: window._current is not None and window._current["review_kind"] == "citation_reference_review")
    assert len(loads) == 2
    window.close()
    app.processEvents()

def test_parse_review_shows_answered_tasks_as_saved_and_requires_resume_click(monkeypatch):
    app, _Qt = _qt(monkeypatch)
    from PySide6.QtWidgets import QLabel

    task = _task(
        "citation_reference_review", db_status="answered", marker_raw="[5]",
        candidates=[{"id": "r2", "label": "[2] Example reference"}],
        answer={"action": "select_reference", "ref_id": "r2", "reason": ""},
    )
    completed = []
    window = _window(
        monkeypatch, load_tasks=lambda: [task],
        answer_task=lambda *_: pytest.fail("saved task must be read-only"),
        on_complete=lambda: completed.append(True),
    )
    window.show()
    _wait(app, lambda: window._current is not None)
    assert "✓" in window.task_tabs.tabText(0)
    assert window.resume_button.isEnabled() is True
    saved_answer = window.findChild(QLabel, "parseReviewSavedAnswer")
    assert saved_answer.textFormat() == _Qt.TextFormat.PlainText
    assert "Selected by user: Assign citation to this reference" in saved_answer.text()
    assert "[2] Example reference" in saved_answer.text()
    assert "{\"action\"" not in saved_answer.text()
    assert completed == []
    window.resume_button.click()
    assert completed == [True]
    window.close()
    app.processEvents()

def test_parse_review_confirms_only_for_missing_tasks_and_skips_only_those(monkeypatch):
    app, _Qt = _qt(monkeypatch)
    from PySide6.QtWidgets import QMessageBox

    tasks = [
        _task("citation_reference_review", task_id="pending-1", candidates=[{"id": "r1"}]),
        _task("reference_claim_review", task_id="pending-2", candidates=[]),
        _task("reference_identity_review", task_id="saved", db_status="answered",
              answer={"action": "keep_ambiguous"}),
    ]
    skipped = []
    completed = []
    decisions = []
    window = _window(monkeypatch, load_tasks=lambda: tasks,
                     answer_task=lambda *args: decisions.append(args),
                     skip_remaining=lambda: skipped.append(True),
                     on_complete=lambda: completed.append(True))
    window.show()
    _wait(app, lambda: window.task_tabs.count() == 3)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Cancel)
    window._candidate_group.buttons()[0].click()
    window.resume_button.click()
    assert skipped == [] and completed == []
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window.resume_button.click()
    assert skipped == [True] and completed == [True]
    assert len(decisions) == 1
    assert decisions[0][0] == "pending-1"
    assert decisions[0][1]["ref_id"] == "r1"
    window.close()
    app.processEvents()

def test_parse_review_skip_failure_keeps_analysis_paused_and_offers_refresh(monkeypatch):
    app, _Qt = _qt(monkeypatch)
    from PySide6.QtWidgets import QMessageBox

    completed = []
    window = _window(
        monkeypatch,
        load_tasks=lambda: [_task("reference_identity_review", task_id="pending")],
        answer_task=lambda *_: None,
        skip_remaining=lambda: (_ for _ in ()).throw(RuntimeError("admission failed")),
        on_complete=lambda: completed.append(True),
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window.show()
    _wait(app, lambda: window._current is not None)
    window.resume_button.click()
    _wait(app, lambda: "admission failed" in window.status.text())
    assert completed == []
    window.close()
    app.processEvents()
