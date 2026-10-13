# tests/test_parse_review_gui_optional.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest


_APP = None
# Desktop owns a strong dialog reference while Qt drains queued signals. Keep
# standalone test dialogs alive for the same lifetime across test functions.
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


def test_switching_review_tasks_removes_previous_nested_fields(monkeypatch):
    app, _Qt = _qt(monkeypatch)
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QLabel, QLineEdit

    tasks = [
        _task("reference_identity_review", ref_number=1, ref_id="r1",
              raw_entry="Old entry", title="Old title", doi="10.1/old"),
        _task("citation_reference_review", marker_raw="[2]", claim_id="c2",
              claim_text="A different claim.", candidates=[]),
    ]
    window = _window(
        monkeypatch, load_tasks=lambda: tasks,
        answer_task=lambda *_args: None, on_complete=lambda: None,
    )
    window.show()
    _wait(app, lambda: window.task_tabs.count() == 2 and window._current is not None)
    assert window.findChild(QLabel, "parseReview_original_title") is not None
    assert window.findChild(QLineEdit, "parseReview_title_field") is not None
    window.task_tabs.setCurrentIndex(1)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    assert window.findChild(QLabel, "parseReview_original_title") is None
    assert window.findChild(QLabel, "parseReview_original_doi") is None
    assert window.findChild(QLineEdit, "parseReview_title_field") is None
    assert window.findChild(QLineEdit, "parseReview_doi_field") is None
    window.close()


@pytest.mark.parametrize(
    ("task", "action", "select_payload", "expected"),
    [
        (
            _task("citation_reference_review", marker_raw="[7]", claim_id="c1",
                  claim_text="The sample was randomly assigned.",
                  candidates=[{"id": "r2", "origin": "parser", "score": 1.0}]),
            "select_reference", "candidate", {"ref_id": "r2"},
        ),
        (
            _task("reference_claim_review", ref_number=3, ref_id="r3",
                  raw_entry="Lee 2020", candidates=[{"id": "c5", "origin": "orphan_match_inverse", "score": 0.8}]),
            "select_claim", "candidate", {"claim_id": "c5"},
        ),
        (
            _task("footnote_source_review", note_number=2, raw_note="See Smith and Jones."),
            "split_sources", "sources", {"source_texts": ["Smith source", "Jones source"]},
        ),
        (
            _task("reference_identity_review", ref_number=4, ref_id="r4",
                  raw_entry="Old entry", title="Old title", doi="10.1/old"),
            "correct_identity", "identity", {"title": "Correct title", "doi": "10.1/new"},
        ),
    ],
)
def test_parse_review_explicit_choices_build_closed_payloads(
    monkeypatch, task, action, select_payload, expected,
):
    app, _Qt = _qt(monkeypatch)
    from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

    pending = [task]
    answers = []
    completed = []

    def answer(task_id, payload):
        answers.append((task_id, payload))
        pending.clear()

    window = _window(
        monkeypatch,
        load_tasks=lambda: list(pending),
        answer_task=answer,
        on_complete=lambda: completed.append(True),
    )
    window.show()
    _wait(app, lambda: window.task_tabs.count() == 1 and window._current is not None)
    if task["review_kind"] == "footnote_source_review":
        assert window._selected_action() is None
    else:
        assert window._selected_action() in {
            "select_reference", "select_claim", "correct_identity",
        }
    assert window.findChild(QPushButton, "parseReviewAnswer") is None
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args: pytest.fail("complete decisions should resume without confirmation"),
    )
    if task["review_kind"] == "citation_reference_review":
        target = window.findChild(QLabel, "parseReviewTarget")
        assert "The sample was randomly assigned." in target.text()
        assert target.textFormat() == _Qt.TextFormat.PlainText
    window._action_buttons[action].click()
    if select_payload == "candidate":
        assert window._candidate_group.checkedButton() is None
        window._candidate_group.buttons()[0].click()
    elif select_payload == "sources":
        window._source_fields[0].setPlainText("Smith source")
        window._source_fields[1].setPlainText("Jones source")
    elif select_payload == "identity":
        assert window._fields["title"].text() == ""
        assert window._fields["doi"].text() == ""
        original_title = window.findChild(QLabel, "parseReview_original_title")
        assert original_title.text() == "Old title"
        window._fields["title"].setText("Correct title")
        window._fields["doi"].setText("10.1/new")
    window._fields["reason"].clear()
    window.resume_button.click()
    assert completed == [True]
    task_id, payload = answers[0]
    assert task_id == task["task_id"]
    assert payload["action"] == action
    assert payload["target_sha256"] == "abc123"
    assert payload["reason"] == ""
    for key, value in expected.items():
        assert payload[key] == value
    window.close()
    app.processEvents()


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


@pytest.mark.parametrize("review_kind", [
    "footnote_source_review", "reference_identity_review",
])
def test_parse_review_keep_ambiguous_is_confirmed_then_submitted_as_decision(
    monkeypatch, review_kind,
):
    app, _Qt = _qt(monkeypatch)
    from PySide6.QtWidgets import QMessageBox

    task = _task(review_kind, task_id="ambiguous")
    recorded = []
    skipped = []
    completed = []
    window = _window(
        monkeypatch, load_tasks=lambda: [task],
        answer_task=lambda task_id, payload: recorded.append((task_id, payload)),
        skip_remaining=lambda: skipped.append(True),
        on_complete=lambda: completed.append(True),
    )
    window.show()
    _wait(app, lambda: window._current is not None)
    window._action_buttons["keep_ambiguous"].click()
    questions = []
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args: questions.append(True) or QMessageBox.StandardButton.Yes,
    )
    window.resume_button.click()
    assert questions == [True]
    assert recorded == [("ambiguous", {
        "action": "keep_ambiguous", "target_sha256": "abc123", "reason": "",
    })]
    assert skipped == [] and completed == [True]
    window.close()
    app.processEvents()


def test_parse_review_failure_and_refresh_are_visible_and_retryable(monkeypatch):
    app, _Qt = _qt(monkeypatch)
    attempts = {"load": 0}
    task = _task("reference_identity_review", ref_number=1, raw_entry="entry")

    def loader():
        attempts["load"] += 1
        if attempts["load"] == 1:
            raise RuntimeError("database unavailable")
        return [task]

    answers = []
    window = _window(
        monkeypatch, load_tasks=loader,
        answer_task=lambda *_: (_ for _ in ()).throw(ValueError("answer rejected")),
        on_complete=lambda: None,
    )
    window.show()
    _wait(app, lambda: window.retry_button.isVisible())
    assert "database unavailable" in window.status.text()
    window.retry_button.click()
    _wait(app, lambda: window._current is not None)
    window._action_buttons["keep_ambiguous"].click()
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window.resume_button.click()
    _wait(app, lambda: "Could not submit" in window.status.text()
          and "answer rejected" in window.status.text())
    assert window._current is not None
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


def test_parse_review_identity_correction_requires_explicit_new_input(monkeypatch):
    app, _Qt = _qt(monkeypatch)
    task = _task(
        "reference_identity_review", ref_number=4, ref_id="r4",
        raw_entry="Old entry", title="Old title", doi="10.1/old",
    )
    answers = []
    window = _window(
        monkeypatch, load_tasks=lambda: [task],
        answer_task=lambda *args: answers.append(args), on_complete=lambda: None,
    )
    window.show()
    _wait(app, lambda: window._current is not None)
    assert window._selected_action() == "correct_identity"
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Cancel)
    window.resume_button.click()
    assert answers == []
    window.close()
    app.processEvents()


def test_parse_review_lists_all_candidates_and_advances_until_resume(monkeypatch):
    app, _Qt = _qt(monkeypatch)
    from PySide6.QtWidgets import QLabel, QScrollArea

    tasks = [
        _task("citation_reference_review", task_id="first", marker_raw="(Example 2020)",
              candidates=[
                  {"id": "r1", "label": "[1] First complete bibliography entry"},
                  {"id": "r2", "label": "[2] Second complete bibliography entry"},
              ]),
        _task("reference_claim_review", task_id="second", ref_number=3,
              raw_entry="[3] Third reference", candidates=[
                  {"id": "c1", "label": "First possible claim"},
              ]),
    ]
    recorded = []

    def answer(task_id, payload):
        recorded.append((task_id, payload))
        next(task for task in tasks if task["task_id"] == task_id)["db_status"] = "answered"

    completed = []
    window = _window(monkeypatch, load_tasks=lambda: list(tasks),
                     answer_task=answer,
                     on_complete=lambda: completed.append(True))
    window.set_dark_theme(False)
    window.show()
    _wait(app, lambda: window._current is not None)
    assert window.task_tabs.usesScrollButtons()
    assert window.task_tabs.expanding() is False
    scroll = window.findChild(QScrollArea, "parseReviewDetailScroll")
    assert scroll is not None
    assert scroll.geometry().top() == window.task_tabs.geometry().bottom() + 1
    assert "QTabBar#parseReviewTaskTabs { background: transparent; border: none; }" in window.styleSheet()
    assert window.width() < 1000 and window.height() < 700
    assert "parse-review-" not in window.task_tabs.tabText(0)
    assert window._selected_action() == "select_reference"
    assert window._candidate_group.checkedButton() is None
    assert len(window._candidate_group.buttons()) == 2
    assert "2 review(s) still need a choice" in window.status.text()
    assert window.resume_button.isEnabled()
    assert window.resume_button.property("ready") is False
    descriptions = [label.text() for label in
                    window.findChildren(QLabel) if "bibliography entry" in label.text()]
    assert descriptions == ["[1] First complete bibliography entry",
                            "[2] Second complete bibliography entry"]
    window.task_tabs.setCurrentIndex(1)
    window.task_tabs.setCurrentIndex(0)
    assert "All decisions saved" not in window.status.text()
    assert window.resume_button.property("ready") is False
    assert recorded == []
    assert window.findChild(QLabel, "parseReviewGuidance").wordWrap()
    assert "QPushButton:hover" in window.styleSheet()
    assert "QPushButton:pressed" in window.styleSheet()

    window._candidate_group.buttons()[1].click()
    window.task_tabs.setCurrentIndex(1)
    assert window.task_tabs.currentIndex() == 1
    assert window._selected_action() == "select_claim"
    window._candidate_group.buttons()[0].click()
    assert "All reviews have a choice" in window.status.text()
    assert window.resume_button.property("ready") is True
    assert recorded == []
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args: pytest.fail("complete decisions should resume without confirmation"),
    )
    window.resume_button.click()
    assert [item[0] for item in recorded] == ["first", "second"]
    assert recorded[0][1]["ref_id"] == "r2"
    assert recorded[1][1]["claim_id"] == "c1"
    assert completed == [True]
    window.close()
    app.processEvents()


def test_parse_review_tabs_keep_drafts_after_review_later_and_resume_as_batch(monkeypatch):
    app, Qt = _qt(monkeypatch)
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QWidget

    tasks = [
        _task("citation_reference_review", task_id=f"review-{number}",
              marker_raw=f"(Example {number})", candidates=[
                  {"id": f"r{number}", "label": f"[{number}] Complete reference {number}"},
              ])
        for number in (1, 2)
    ]
    answers = []
    completed = []
    window = _window(monkeypatch, load_tasks=lambda: list(tasks),
                     answer_task=lambda *args: answers.append(args),
                     on_complete=lambda: completed.append(True))
    window.show()
    _wait(app, lambda: window._current is not None)
    row = window.findChild(QWidget, "parseReviewCandidateRow")
    _wait(app, lambda: row.isVisible())
    QTest.mouseClick(row, Qt.MouseButton.LeftButton,
                     pos=QPoint(row.width() - 8, row.height() // 2))
    assert window._candidate_group.checkedButton().property("candidate_id") == "r1"
    window._fields["reason"].setText("checked locally")
    window.task_tabs.setCurrentIndex(1)
    assert "*" in window.task_tabs.tabText(0)
    assert "Unsaved selection" in window.task_tabs.tabToolTip(0)
    window.task_tabs.setCurrentIndex(0)
    assert window._candidate_group.checkedButton().property("candidate_id") == "r1"
    assert window._fields["reason"].text() == "checked locally"
    assert window.close_button.text() == "Review later"
    assert "analysis paused" in window.close_button.toolTip()
    window.close_button.click()
    app.processEvents()
    assert answers == [] and completed == []
    assert window.isHidden()

    window.show()
    _wait(app, lambda: window._current is not None)
    assert window._candidate_group.checkedButton().property("candidate_id") == "r1"
    assert window._fields["reason"].text() == "checked locally"
    assert "*" in window.task_tabs.tabText(0)
    window.task_tabs.setCurrentIndex(1)
    window._candidate_group.buttons()[0].click()
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args: pytest.fail("complete decisions should resume without confirmation"),
    )
    window.resume_button.click()
    assert [task_id for task_id, _payload in answers] == ["review-1", "review-2"]
    assert completed == [True]
    window.close()
    app.processEvents()


def test_parse_review_title_bar_close_preserves_current_draft_for_reopen(monkeypatch):
    app, Qt = _qt(monkeypatch)
    task = _task("citation_reference_review", candidates=[{"id": "r1"}])
    window = _window(
        monkeypatch, load_tasks=lambda: [task], answer_task=lambda *_args: None,
        on_complete=lambda: None,
    )
    window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
    window.show()
    _wait(app, lambda: window._current is not None)
    window._candidate_group.buttons()[0].click()
    window.close()
    app.processEvents()
    assert window.isHidden()
    window.show()
    _wait(app, lambda: window._current is not None)
    assert window._candidate_group.checkedButton().property("candidate_id") == "r1"
    window.close()
    app.processEvents()


def test_parse_review_tabs_show_scroll_controls_when_tasks_overflow(monkeypatch):
    app, _Qt = _qt(monkeypatch)
    from PySide6.QtWidgets import QToolButton

    tasks = [
        _task("citation_reference_review", task_id=f"review-{number}",
              marker_raw=f"({number})", candidates=[])
        for number in range(12)
    ]
    window = _window(monkeypatch, load_tasks=lambda: tasks,
                     answer_task=lambda *_: None, on_complete=lambda: None)
    window.show()
    _wait(app, lambda: window.task_tabs.count() == 12)
    assert window.task_tabs.usesScrollButtons()
    _wait(app, lambda: len([
        control for control in window.task_tabs.findChildren(QToolButton)
        if control.isVisible()
    ]) == 2)
    window.task_tabs.setCurrentIndex(11)
    assert window._current["task_id"] == "review-11"
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
