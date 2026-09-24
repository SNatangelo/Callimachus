# tests/test_guided_fetch_gui_optional.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core.gui import GuidedFetchViewModel
from core.gui import launcher
from core.gui.launcher import launch_guided_fetch


def test_gui_modules_are_importable_without_initializing_qt():
    assert GuidedFetchViewModel({"references": []}).rows() == []


def test_qt_window_smoke_when_optional_extra_is_installed(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPlainTextEdit, QPushButton
    from core.gui.guided_fetch import create_guided_fetch_window

    app = QApplication.instance() or QApplication([])
    window = create_guided_fetch_window(
        str(tmp_path), submit_source=lambda _ref, _payload: None, proceed=lambda: None,
        inventory_loader=lambda _run: {
            "references": [{
                "ref_id": "r1",
                "ref_number": 1,
                "parsed": {"title": "Claimed paper"},
                "resolve": {},
                "fetch": {"tier": None, "pending_tasks": []},
                "fabrication_suspicion": {
                    "suspected": True,
                    "reason": "The indexed journal does not contain this article",
                },
                "bibliographic_review_labels": [{
                    "code": "not_found_after_completed_searches",
                    "providers": ["crossref", "openalex"],
                }],
            }],
        },
    )
    assert window.windowTitle() == "Callimachus — Guided Fetch"
    logo = window.findChild(QLabel, "brandLogo")
    selection = window.findChild(QLabel, "selectionGuidance")
    assert logo is not None and logo.pixmap() is not None and not logo.pixmap().isNull()
    assert window.findChild(QLabel, "workflowGuidance") is None
    assert window.findChild(QLabel, "detailMessage") is None
    assert selection is not None
    assert "Why this is flagged:" in selection.text()
    assert "indexed journal does not contain this article" in selection.text()
    assert window.table.columnCount() == 5
    assert window.table.horizontalHeaderItem(2).text() == "Text availability"
    assert window.table.horizontalHeaderItem(3).text() == "Risk signal"
    assert window.table.horizontalHeaderItem(4).text() == "Review labels"
    assert window.table.item(0, 3).toolTip() == (
        "The indexed journal does not contain this article"
    )
    assert window.table.item(0, 4).text() == (
        "⚠ Not found after completed bibliographic searches"
    )
    assert "informational only" in window.table.item(0, 4).toolTip().lower()
    assert window.proceed_button.text() == "Proceed / skip remaining"
    assert all(
        button.text() not in {"Capture HTML", "Capture PDF"}
        for button in window.findChildren(QPushButton)
    )
    assert window.choose_file.isEnabled() is False
    assert window.submit.isEnabled() is False
    assert not window.windowIcon().isNull()
    assert app.windowIcon().isNull() is False
    show_json = next(
        button for button in window.tabs.widget(0).findChildren(QPushButton)
        if button.text() == "Show original JSON"
    )
    instructions = window.tabs.widget(0).findChild(QPushButton, "instructionsButton")
    assert instructions is not None
    assert instructions.text() == ""
    assert not instructions.icon().isNull()
    assert instructions.toolTip() == "Guided Fetch instructions"
    assert instructions.accessibleName() == "Guided Fetch instructions"
    dialogs = []
    monkeypatch.setattr(QDialog, "exec", lambda dialog: dialogs.append(dialog))
    show_json.click()
    assert dialogs[-1].windowTitle() == "Original audit JSON"
    assert '"title": "Claimed paper"' in dialogs[-1].findChild(QPlainTextEdit).toPlainText()
    instructions.click()
    assert dialogs[-1].windowTitle() == "Guided Fetch instructions"
    assert "Full text" in dialogs[-1].findChild(QPlainTextEdit).toPlainText()
    assert "toolbar control" in dialogs[-1].findChild(QPlainTextEdit).toPlainText()
    window.close()
    assert app is not None


def test_qt_identity_review_submits_closed_decision_and_blocks_proceed(
    monkeypatch, tmp_path,
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.guided_fetch import create_guided_fetch_window

    app = QApplication.instance() or QApplication([])
    state = {"pending": True}
    submitted = []

    def inventory(_run):
        pending = []
        if state["pending"]:
            pending.append({
                "task_id": "identity:r1",
                "task_kind": "source_identity_attestation",
                "target_sha256": "a" * 64,
                "source_text_id": "source-1",
                "source_tier": "fulltext",
                "reference": {"title": "Cited work"},
                "source_identity": {
                    "origin": "publisher",
                    "identity_status": "unverified",
                },
                "resolve_identity": {"matched_title": "Resolved work"},
            })
        return {"references": [{
            "ref_id": "r1",
            "ref_number": 1,
            "parsed": {"title": "Cited work"},
            "resolve": {},
            "fetch": {
                "tier": "fulltext",
                "pending_tasks": pending,
                "sources": [],
                "attempts": [],
            },
            "fabrication_suspicion": {},
        }]}

    def submit_identity(ref_id, action, target_sha256, reason):
        submitted.append((ref_id, action, target_sha256, reason))
        state["pending"] = False

    window = create_guided_fetch_window(
        str(tmp_path),
        submit_source=lambda *_: None,
        proceed=lambda: None,
        submit_identity_review=submit_identity,
        identity_review_allowed=True,
        inventory_loader=inventory,
    )

    assert not window.identity_review_panel.isHidden()
    assert window.proceed_button.isEnabled() is False
    assert window.attest_identity_button.isEnabled() is True
    assert "Cited work" in window.identity_review_facts.text()
    assert "Resolved work" in window.identity_review_facts.text()
    assert "a" * 64 in window.identity_review_facts.text()

    window.identity_review_reason.setText("Title, author, and venue match.")
    window.attest_identity_button.click()
    app.processEvents()

    assert submitted == [(
        "r1", "attest_identity", "a" * 64,
        "Title, author, and venue match.",
    )]
    assert window.identity_review_panel.isHidden()
    assert window.proceed_button.isEnabled() is True
    window.close()

    state["pending"] = True
    keep_window = create_guided_fetch_window(
        str(tmp_path),
        submit_source=lambda *_: None,
        proceed=lambda: None,
        submit_identity_review=submit_identity,
        identity_review_allowed=True,
        inventory_loader=inventory,
    )
    keep_window.identity_review_reason.setText("The available facts are inconclusive.")
    keep_window.keep_unverified_button.click()
    app.processEvents()
    assert submitted[-1] == (
        "r1", "keep_unverified", "a" * 64,
        "The available facts are inconclusive.",
    )
    assert keep_window.identity_review_panel.isHidden()
    keep_window.close()

    state["pending"] = True
    agent_window = create_guided_fetch_window(
        str(tmp_path),
        submit_source=lambda *_: None,
        proceed=lambda: None,
        submit_identity_review=lambda *_: pytest.fail("agent must not attest"),
        identity_review_allowed=False,
        inventory_loader=inventory,
    )
    assert not agent_window.identity_review_panel.isHidden()
    assert agent_window.attest_identity_button.isEnabled() is False
    assert agent_window.keep_unverified_button.isEnabled() is False
    assert agent_window.identity_review_reason.isEnabled() is False
    assert agent_window.proceed_button.isEnabled() is False
    assert "separately authenticated human operator" in agent_window.identity_review_facts.text()
    agent_window.close()


def test_qt_identity_review_does_not_retry_after_successful_admission_refresh_failure(
    monkeypatch, tmp_path,
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.guided_fetch import create_guided_fetch_window

    app = QApplication.instance() or QApplication([])
    calls = []
    refreshes = 0

    def inventory(_run):
        nonlocal refreshes
        refreshes += 1
        if refreshes > 1:
            raise OSError("transient refresh failure")
        return {"references": [{
            "ref_id": "r1", "ref_number": 1,
            "parsed": {"title": "Cited work"}, "resolve": {},
            "fetch": {"tier": "fulltext", "sources": [], "attempts": [],
                      "pending_tasks": [{
                          "task_id": "identity:r1",
                          "task_kind": "source_identity_attestation",
                          "target_sha256": "a" * 64,
                          "reference": {"title": "Cited work"},
                      }]},
            "fabrication_suspicion": {},
        }]}

    errors = []
    monkeypatch.setattr(
        QMessageBox, "critical",
        lambda _parent, title, message: errors.append((title, message)),
    )
    window = create_guided_fetch_window(
        str(tmp_path), submit_source=lambda *_: None, proceed=lambda: None,
        submit_identity_review=lambda *args: calls.append(args),
        inventory_loader=inventory,
    )
    window.identity_review_reason.setText("Exact metadata inspected.")
    window.attest_identity_button.click()
    app.processEvents()

    assert len(calls) == 1
    assert errors and errors[0][0] == "Identity decision recorded; refresh failed"
    assert window.attest_identity_button.isEnabled() is False
    assert window.keep_unverified_button.isEnabled() is False
    assert "decision was recorded" in window.identity_review_facts.text()
    window.attest_identity_button.click()
    assert len(calls) == 1
    window.close()


def test_qt_theme_toggle_and_task_kind_enable_file_controls(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel
    from core.gui.guided_fetch import create_guided_fetch_window

    app = QApplication.instance() or QApplication([])
    window = create_guided_fetch_window(
        str(tmp_path), submit_source=lambda *_: None, proceed=lambda: None,
        inventory_loader=lambda _run: {"references": [{
            "ref_id": "r1", "parsed": {"title": "Cited work"},
            "resolve": {"status": "resolved"},
            "fetch": {"tier": None, "pending_tasks": [{"task_kind": "fetch"}]},
            "bibliographic_concern": {
                "level": "elevated_bibliographic_suspicion",
                "conclusion": "completed_search_misses",
            },
            "bibliographic_review_labels": [{
                "code": "not_found_after_completed_searches",
            }],
        }]},
    )
    assert window.choose_file.isEnabled() and window.file_label.isEnabled()
    assert "#ffffff" in window.styleSheet()
    assert "QPushButton:hover" in window.styleSheet()
    assert "QPushButton:pressed" in window.styleSheet()
    assert "QScrollBar::handle:vertical" in window.styleSheet()
    assert window.tier_switch.isEnabled() and not window.tier_switch.isChecked()
    assert window.fulltext_label.text() == "Full text"
    assert window.abstract_label.text() == "Abstract"
    window.tier_switch.setChecked(True)
    assert window.tier_switch.isChecked() and window._selected_tier() == "abstract"
    assert window.tier_switch.isCheckable()
    assert window.tier_switch.size().width() == 42
    assert "#brandLogoTile" in window.styleSheet()
    assert (
        window.findChild(QLabel, "brandLogo").parentWidget().layout().itemAt(0).alignment()
        == Qt.AlignmentFlag.AlignCenter
    )
    assert window.font_smaller.text() == "A−" and window.font_larger.text() == "A+"
    window.font_larger.click()
    assert "font-size: 14px" in window.styleSheet()
    assert "QPushButton:hover { background: #dcecff; color: #1d4f91;" in window.styleSheet()
    assert "QPushButton:pressed { background: #163f75; color: white;" in window.styleSheet()
    assert "QPushButton:checked:hover { background: #1d4f91; color: white;" in window.styleSheet()
    assert "#proceedButton, #discardSourceButton { background: #2457a6; color: white;" in window.styleSheet()
    assert "#proceedButton:hover, #discardSourceButton:hover { background: #1d4f91; color: white;" in window.styleSheet()
    assert "#proceedButton:pressed, #discardSourceButton:pressed { background: #163f75; color: white;" in window.styleSheet()
    assert "#proceedButton:disabled, #discardSourceButton:disabled { background: #f5f7fb; color: #52606d;" in window.styleSheet()
    assert "QTabBar::tab:hover { background: #dcecff; color: #1d4f91;" in window.styleSheet()
    availability = window.table.item(0, 2)
    assert availability.toolTip() == (
        "The bibliographic identity was identified, but no usable text was acquired."
    )
    assert availability.foreground().color().name() == "#1d4f91"
    assert window.table.item(0, 3).foreground().color().name() == "#b42318"
    assert window.table.item(0, 4).foreground().color().name() == "#9a6700"
    window.theme_toggle.setChecked(True)
    assert "#1b1f24" in window.styleSheet()
    assert "QPushButton { background: #1f2b3d; color: #edf3ff;" in window.styleSheet()
    assert "QPushButton:hover { background: #2d4d73; color: #edf3ff;" in window.styleSheet()
    assert "QPushButton:pressed { background: #183d78; color: white;" in window.styleSheet()
    assert "QPushButton:checked:hover { background: #3974c6; color: white;" in window.styleSheet()
    assert "QPushButton:disabled { background: #1b1f24; color: #aeb8c7;" in window.styleSheet()
    assert "#proceedButton, #discardSourceButton { background: #2457a6; color: white;" in window.styleSheet()
    assert "#proceedButton:pressed, #discardSourceButton:pressed { background: #183d78; color: white;" in window.styleSheet()
    assert "#proceedButton:disabled, #discardSourceButton:disabled { background: #1b1f24; color: #aeb8c7;" in window.styleSheet()
    assert "QTabBar::tab:hover { background: #2d4d73; color: #edf3ff;" in window.styleSheet()
    assert window.tier_switch._dark_theme is True
    assert "QPushButton:hover" in window.styleSheet()
    assert "QPushButton:pressed" in window.styleSheet()
    assert availability.foreground().color().name() == "#7dd3fc"
    assert window.table.item(0, 3).foreground().color().name() == "#ff8787"
    assert window.table.item(0, 4).foreground().color().name() == "#ffd166"
    window.close()
    assert app is not None


def test_qt_discard_queued_source_unstages_and_clears_selection(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.guided_fetch import create_guided_fetch_window

    discarded = []
    notices = []
    app = QApplication.instance() or QApplication([])
    window = create_guided_fetch_window(
        str(tmp_path),
        submit_source=lambda *_: None,
        proceed=lambda: None,
        discard_source=discarded.append,
        inventory_loader=lambda _run: {"references": [{
            "ref_id": "r1", "ref_number": 1, "parsed": {"title": "Cited work"},
            "resolve": {}, "fetch": {"tier": None, "pending_tasks": [{"task_kind": "fetch"}]},
        }]},
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, text: notices.append((title, text)),
    )
    window._selected_file = "/chosen/exact.pdf"
    window.file_label.setText("exact.pdf")
    assert window.capture_status_panel.isHidden()
    assert window.discard_source_button.parentWidget() is window.capture_status_panel
    window._mark_queued("r1", "exact.pdf", audit_path="/audit/exact.pdf")

    assert window.discard_source_button.isEnabled()
    assert not window.discard_source_button.isHidden()
    assert not window.capture_status_panel.isHidden()
    assert not window.capture_status.isHidden()
    window.discard_source_button.click()

    assert discarded == ["r1"]
    assert "r1" not in window._queued_sources
    assert window._selected_file is None
    assert window.file_label.text() == "Drop one file here"
    assert window.capture_status_panel.isHidden()
    assert window.discard_source_button.isHidden()
    assert window.capture_status.isHidden()
    assert "Queued" not in window.table.item(0, 0).text()
    assert notices == [("Source discarded", "The artifact is no longer queued. Its captured audit file was not deleted.")]
    window.close()
    assert app is not None


def test_qt_window_marks_retracted_resolved_source(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.guided_fetch import create_guided_fetch_window

    app = QApplication.instance() or QApplication([])
    window = create_guided_fetch_window(
        str(tmp_path),
        submit_source=lambda *_: None,
        proceed=lambda: None,
        inventory_loader=lambda _run: {"references": [{
            "ref_id": "r1",
            "parsed": {"title": "Retracted work"},
            "resolve": {"status": "resolved", "retracted": True},
            "fetch": {"tier": None, "pending_tasks": []},
        }]},
    )

    assert window.table.item(0, 3).text() == "⚠ Retracted source"
    assert window.table.item(0, 3).toolTip() == (
        "The resolved cited work is recorded as retracted."
    )
    assert "recorded as retracted" in window.selection_guidance.text()
    window.close()
    assert app is not None


def test_qt_browser_capture_stays_bound_to_opened_reference_after_selection_changes(
    monkeypatch, tmp_path
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.guided_fetch import create_guided_fetch_window

    submitted = []
    app = QApplication.instance() or QApplication([])
    window = create_guided_fetch_window(
        str(tmp_path), submit_source=lambda ref_id, payload: submitted.append((ref_id, payload)),
        proceed=lambda: None,
        inventory_loader=lambda _run: {"references": [
            {"ref_id": "r1", "ref_number": 1, "parsed": {"raw_entry": "First cited work", "title": "First"},
             "resolve": {}, "fetch": {"tier": None, "pending_tasks": [{"task_kind": "fetch"}]}},
            {"ref_id": "r2", "ref_number": 2, "parsed": {"raw_entry": "Second cited work", "title": "Second"},
             "resolve": {}, "fetch": {"tier": None, "pending_tasks": [{"task_kind": "fetch"}]}},
        ]},
    )
    confirmations = []
    monkeypatch.setattr(
        window, "_application_modal_question",
        lambda _title, question: confirmations.append(question) or True,
    )
    monkeypatch.setattr(window, "_application_modal_information", lambda *_args: None)
    window._browser_worker = object()
    window._open_controlled_browser()
    window.table.selectRow(1)
    window.tier_switch.setChecked(True)
    window._capture_browser("html")
    window._browser_artifact({
        "ref_id": "r1", "capture_index": 0, "artifact_path": "/captured/r1.html",
        "url": "https://example.test/r1",
    })

    assert submitted[0][0] == "r1"
    assert submitted[0][1]["source_tier"] == "fulltext"
    assert "[1]" in confirmations[0]
    assert not window.capture_status_panel.isHidden()
    assert not window.capture_status.isHidden()
    assert not window.discard_source_button.isHidden()
    assert "captured/selected and queued; validated only when Proceed is clicked" in window.capture_status.text()
    assert "Queued" in window.table.item(0, 0).text()
    window.close()
    assert app is not None


def test_qt_browser_toolbar_dialogs_are_application_modal_and_on_top(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.guided_fetch import create_guided_fetch_window

    app = QApplication.instance() or QApplication([])
    window = create_guided_fetch_window(
        str(tmp_path), submit_source=lambda *_: None, proceed=lambda: None,
        inventory_loader=lambda _run: {"references": []},
    )
    boxes = []
    monkeypatch.setattr(QMessageBox, "raise_", lambda box: boxes.append(box))
    monkeypatch.setattr(QMessageBox, "activateWindow", lambda _box: None)
    monkeypatch.setattr(
        QMessageBox, "exec", lambda _box: QMessageBox.StandardButton.Yes
    )

    assert window._application_modal_question("Confirm captured source", "Exact work?")
    window._application_modal_information("Source queued", "Queued for validation.")

    assert len(boxes) == 2
    assert all(box.windowModality() == Qt.WindowModality.ApplicationModal for box in boxes)
    assert all(
        bool(box.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) for box in boxes
    )
    window.close()
    assert app is not None


def test_qt_download_notice_confirms_once_then_queues_or_discards(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.guided_fetch import create_guided_fetch_window

    submitted = []
    app = QApplication.instance() or QApplication([])
    window = create_guided_fetch_window(
        str(tmp_path), submit_source=lambda ref_id, payload: submitted.append((ref_id, payload)),
        proceed=lambda: None,
        inventory_loader=lambda _run: {"references": [{
            "ref_id": "r1", "ref_number": 1, "parsed": {"title": "Cited work"},
            "resolve": {}, "fetch": {"tier": None, "pending_tasks": [{"task_kind": "fetch"}]},
        }]},
    )
    captures = []
    discards = []
    questions = []
    window.browser_capture_download.connect(
        lambda ref_id, index, token: captures.append((ref_id, index, token))
    )
    window.browser_discard_download.connect(discards.append)
    window._browser_ref_id = "r1"
    window._browser_tier = "fulltext"
    monkeypatch.setattr(
        window, "_application_modal_question",
        lambda _title, question: questions.append(question) or True,
    )
    monkeypatch.setattr(window, "_application_modal_information", lambda *_args: None)

    window._browser_download_detected({
        "token": 1, "suggested_filename": r"C:\Downloads\Exact source.pdf",
    })

    assert questions == [
        "You downloaded Exact source.pdf. Is this the exact cited source you wanted?"
    ]
    assert captures == [("r1", 0, 1)]
    window._browser_artifact({
        "ref_id": "r1", "capture_index": 0,
        "artifact_path": "/run/audit/download-1.pdf",
        "display_name": "Exact source.pdf", "url": "https://example.test/source",
    })
    assert len(submitted) == 1
    assert questions == [
        "You downloaded Exact source.pdf. Is this the exact cited source you wanted?"
    ]
    assert "Exact source.pdf" in window.capture_status.text()
    assert "Saved in audit" in window.capture_status.text()
    assert window.capture_status.toolTip() == "Audit capture saved at: /run/audit/download-1.pdf"

    monkeypatch.setattr(window, "_application_modal_question", lambda *_args: False)
    window._browser_download_detected({"token": 2, "suggested_filename": "wrong.pdf"})
    assert discards == [2]
    assert len(submitted) == 1
    assert captures == [("r1", 0, 1)]
    window.close()
    assert app is not None


def test_qt_browser_availability_consumes_pending_url_once(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.guided_fetch import create_guided_fetch_window

    app = QApplication.instance() or QApplication([])
    window = create_guided_fetch_window(
        str(tmp_path),
        submit_source=lambda *_: None,
        proceed=lambda: None,
        inventory_loader=lambda _run: {"references": []},
    )
    opened = []
    window.browser_open.connect(opened.append)
    window._pending_browser_url = "https://publisher.test/article"

    window._browser_available({"available": True})
    window._browser_available({"available": True})

    assert opened == ["https://publisher.test/article"]
    assert window._pending_browser_url is None
    window.close()
    assert app is not None


def test_qt_browser_navigation_shows_per_reference_search_error(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.guided_fetch import create_guided_fetch_window

    warnings = []
    app = QApplication.instance() or QApplication([])
    window = create_guided_fetch_window(
        str(tmp_path), submit_source=lambda *_: None, proceed=lambda: None,
        inventory_loader=lambda _run: {"references": [{
            "ref_id": "r1", "parsed": {}, "resolve": {},
            "fetch": {"tier": None, "pending_tasks": [{"task_kind": "fetch"}]},
        }]},
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(_args))
    window._open_controlled_browser()

    assert warnings and warnings[0][1] == "Browser navigation"
    assert "rendered an empty value" in warnings[0][2]
    window.close()
    assert app is not None


def test_qt_browser_launch_failure_shows_preserved_detail(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.guided_fetch import create_guided_fetch_window

    messages = []
    app = QApplication.instance() or QApplication([])
    window = create_guided_fetch_window(
        str(tmp_path), submit_source=lambda *_: None, proceed=lambda: None,
        inventory_loader=lambda _run: {"references": []},
    )
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))
    window._browser_available({
        "available": False,
        "reason_code": "browser_launch_failed",
        "reason": "Google Chrome could not be started: browser dispatcher stopped",
    })

    assert messages[0][1] == "Google Chrome could not be started"
    assert "browser dispatcher stopped" in messages[0][2]
    window.close()
    assert app is not None


def test_qt_missing_chrome_dialog_preserves_runtime_detail(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.guided_fetch import create_guided_fetch_window

    details = []
    app = QApplication.instance() or QApplication([])
    window = create_guided_fetch_window(
        str(tmp_path), submit_source=lambda *_: None, proceed=lambda: None,
        inventory_loader=lambda _run: {"references": []},
    )
    monkeypatch.setattr(
        QMessageBox,
        "setInformativeText",
        lambda _box, detail: details.append(detail),
    )
    monkeypatch.setattr(QMessageBox, "exec", lambda _box: None)
    window._browser_available({
        "available": False,
        "reason_code": "google_chrome_unavailable",
        "reason": "Linux/WSL needs Linux Google Chrome; Windows chrome.exe is not usable",
    })

    assert details == [
        "Linux/WSL needs Linux Google Chrome; Windows chrome.exe is not usable"
    ]
    window.close()
    assert app is not None


def test_launcher_reports_missing_optional_qt(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def missing_qt(name, *args, **kwargs):
        if name == "PySide6":
            raise ModuleNotFoundError("No module named 'PySide6'", name="PySide6")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_qt)
    result = launch_guided_fetch("run", submit_source=lambda *_: None, proceed=lambda: None)
    assert result.started is False and "PySide6" in result.reason


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux display guard")
def test_launcher_without_linux_display_returns_without_native_qt_abort(tmp_path):
    pytest.importorskip("PySide6")
    environment = os.environ.copy()
    project_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (project_root, environment.get("PYTHONPATH")) if value
    )
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        environment.pop(name, None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from core.gui.launcher import launch_guided_fetch\n"
                "result = launch_guided_fetch('run', submit_source=lambda *_: None, proceed=lambda: None)\n"
                "assert not result.started\n"
                "assert 'No Linux graphical display' in result.reason\n"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr


def test_launcher_reports_qt_probe_failure(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(launcher, "_probe_qapplication_startup", lambda: "Qt probe failed")
    result = launch_guided_fetch("run", submit_source=lambda *_: None, proceed=lambda: None)
    assert result == launcher.GuiLaunchResult(False, "Qt probe failed")


def test_launcher_reports_window_exception(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(launcher, "_probe_qapplication_startup", lambda: None)

    def fail_window(*_args, **_kwargs):
        raise RuntimeError("window setup failed")

    monkeypatch.setitem(
        sys.modules,
        "core.gui.guided_fetch",
        SimpleNamespace(run_guided_fetch_window=fail_window),
    )
    result = launch_guided_fetch("run", submit_source=lambda *_: None, proceed=lambda: None)
    assert result == launcher.GuiLaunchResult(
        False,
        "Guided Fetch could not be launched: window setup failed",
    )


@pytest.mark.parametrize(
    ("runner", "expected"),
    [
        (
            lambda _argv, **_kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(["python"], 5),
            ),
            "timed out",
        ),
        (
            lambda _argv, **_kwargs: (_ for _ in ()).throw(OSError("unavailable")),
            "failed",
        ),
        (lambda _argv, **_kwargs: SimpleNamespace(returncode=134), "exited with 134"),
    ],
)
def test_qt_probe_reports_child_startup_failures(runner, expected):
    result = launcher._probe_qapplication_startup(runner=runner)
    assert result is not None and expected in result


def test_optional_dependency_install_uses_active_interpreter_and_never_browser_install(
    monkeypatch,
):
    calls = []

    def runner(argv, *, check):
        calls.append((argv, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher, "missing_guided_dependencies", lambda: [])
    installed, reason = launcher.install_guided_dependencies(runner=runner)

    assert installed is True and reason is None
    assert calls == [
        ([
            launcher.sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(launcher.GUIDED_REQUIREMENTS),
        ], False)
    ]
    assert "playwright install" not in " ".join(calls[0][0])
