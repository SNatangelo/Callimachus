# tests/test_desktop_gui_optional.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import sqlite3
import json
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.gui.desktop import _source_summary_html, _stylesheet, load_translations


@pytest.fixture(autouse=True)
def _isolated_gui_preferences(tmp_path):
    try:
        from PySide6 import QtCore
    except ImportError:
        yield
        return

    QSettings = QtCore.QSettings

    class IsolatedQSettings(QSettings):
        def __init__(self, *args):
            if args == ("Callimachus", "Callimachus"):
                super().__init__(str(tmp_path / "callimachus.ini"), QSettings.Format.IniFormat)
            else:
                super().__init__(*args)

    QtCore.QSettings = IsolatedQSettings
    try:
        isolated = QtCore.QSettings("Callimachus", "Callimachus")
        assert Path(isolated.fileName()).is_relative_to(tmp_path)
        assert not isolated.contains("llm_role_selection")
        yield
    finally:
        QtCore.QSettings = QSettings


def _process_events_until(app, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False




def _select_gui_settings_category(window, category):
    if category == "models":
        rows = window._settings_rows
        provider = next((row.get("model_provider") or row.get("check_provider")
                         for row in rows if row.get("group") == "models"), "openai")
        aliases = {"claude_cli": "claude", "google": "gemini", "freetoken": "freetoken"}
        provider = aliases.get(provider, provider)
        _select_jury_provider(window, provider)
        return
    index = next(
        index for index in range(window.settings_categories.count())
        if window._settings_categories_key(index) == category
    )
    window.settings_categories.setCurrentRow(index)


def _general_setting(window, object_name):
    from PySide6 import QtWidgets

    window.navigation.setCurrentRow(3)
    QtWidgets.QApplication.instance().processEvents()
    _select_gui_settings_category(window, "general")
    QtWidgets.QApplication.instance().processEvents()
    editors = window.findChildren(QtWidgets.QWidget, object_name)
    assert editors
    # Rendering a settings category replaces its old widgets with deleteLater;
    # use the newest editor while Qt drains deferred deletions.
    return editors[-1]


def _personalization_setting(window, object_name):
    from PySide6 import QtWidgets

    window.navigation.setCurrentRow(3)
    QtWidgets.QApplication.instance().processEvents()
    _select_gui_settings_category(window, "personalization")
    QtWidgets.QApplication.instance().processEvents()
    editors = window.findChildren(QtWidgets.QWidget, object_name)
    assert editors
    return editors[-1]


def _enable_cli_console(window):
    from PySide6 import QtWidgets

    assert not window._console_enabled
    control = _personalization_setting(window, "enableCliConsole")
    assert isinstance(control, QtWidgets.QCheckBox)
    assert not control.isChecked()
    control.setChecked(True)
    QtWidgets.QApplication.instance().processEvents()
    assert window._console_enabled
    return control


def _set_theme(window, dark):
    from PySide6 import QtWidgets

    control = _personalization_setting(window, "appearanceDark" if dark else "appearanceLight")
    assert isinstance(control, QtWidgets.QPushButton)
    control.click()
    QtWidgets.QApplication.instance().processEvents()
    assert window._dark == bool(dark)
    return control


def _select_jury_provider(window, provider, role="jury1"):
    from PySide6 import QtWidgets
    window._show_jury_models(role)
    chooser = window._atlante_jury_chooser
    chooser.refresh_settings()
    chooser.show_provider(provider)
    # Provider settings are now rendered in the chooser. Expose the currently
    # rendered fields through the legacy test lookup map for existing contracts.
    for caption in chooser.config.findChildren(QtWidgets.QLabel, "settingCaption"):
        layout = caption.parentWidget().layout()
        row = layout.getWidgetPosition(caption)[0]
        field = layout.itemAt(row, QtWidgets.QFormLayout.ItemRole.FieldRole).widget()
        name = caption.property("settingName")
        if field is not None and name:
            editors = [item for item in field.findChildren(QtWidgets.QWidget)
                       if isinstance(item, (QtWidgets.QComboBox, QtWidgets.QLineEdit,
                                            QtWidgets.QSpinBox, QtWidgets.QCheckBox))]
            window._settings_editors[name] = editors[0] if editors else field
    return chooser


def _jury_setting_editor(chooser, name):
    from PySide6 import QtWidgets

    for caption in chooser.config.findChildren(QtWidgets.QLabel, "settingCaption"):
        if caption.property("settingName") != name:
            continue
        layout = caption.parentWidget().layout()
        row = layout.getWidgetPosition(caption)[0]
        field = layout.itemAt(row, QtWidgets.QFormLayout.ItemRole.FieldRole).widget()
        combos = field.findChildren(QtWidgets.QComboBox)
        if combos:
            return combos[0]
        lines = field.findChildren(QtWidgets.QLineEdit)
        if lines:
            return lines[0]
    raise AssertionError(f"Jury chooser has no editor for {name}")


def test_jury_response_check_is_async_blocks_failure_and_retries_after_fix(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets
    from core.gui.desktop import create_desktop_window

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    entered, release = threading.Event(), threading.Event()
    calls, launched, warnings, ticks = [], [], [], []

    def check(environment):
        calls.append(dict(environment))
        entered.set()
        assert release.wait(3)
        return ({"ok": False, "selector": "freetoken:local", "status_code": 401}
                if len(calls) == 1 else {"ok": True, "checked": ["freetoken:local"]})

    window = create_desktop_window(jury_readiness_callback=check, language="it")
    window._paper_path = "paper.pdf"
    monkeypatch.setattr(window, "_launch_process", lambda command, **kwargs: launched.append((command, kwargs)))
    monkeypatch.setattr(window, "_show_provider_check_warning", warnings.append)
    try:
        environment = {"CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken", "FREETOKEN_MODEL": "local"}
        window._start_process(["worker"], environment=environment)
        assert entered.wait(1)
        assert not window.start_button.isEnabled() and not launched
        checking = window.llm_status.text()
        assert checking == window.phase_label.text()
        assert checking in window.current_activity.text()
        QtCore.QTimer.singleShot(0, lambda: ticks.append(True))
        window.navigation.setCurrentRow(1)
        assert _process_events_until(app, lambda: bool(ticks))
        assert window._jury_readiness_request is not None
        release.set()
        assert _process_events_until(app, lambda: window._jury_readiness_request is None)
        assert not launched and warnings
        assert "chiave" in warnings[0].lower()
        assert window.llm_status.text() != checking
        assert window.llm_status.text() == window.phase_label.text()
        assert window.llm_status.text() in window.current_activity.text()
        assert window.start_button.isEnabled()
        window._start_process(["worker"], environment=environment)
        assert _process_events_until(app, lambda: bool(launched))
        assert len(calls) == 2 and len(launched) == 1
        assert launched[0][1]["environment"] == environment
    finally:
        release.set()
        window.close()




@pytest.mark.parametrize("change", ["deselect", "new_analysis", "settings"])
def test_pending_jury_check_cannot_launch_after_selection_or_analysis_changes(monkeypatch, change):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    from core.gui.desktop import create_desktop_window

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    entered, release = threading.Event(), threading.Event()
    launched, calls = [], []

    def check(environment):
        calls.append(environment)
        entered.set()
        assert release.wait(3)
        return {"ok": True, "checked": ["freetoken:local"]}

    window = create_desktop_window(
        jury_readiness_callback=check,
        verify_candidates_loader=lambda: [{
            "selector": "freetoken:local", "backend": "freetoken", "model": "local",
            "model_env": "FREETOKEN_MODEL", "configured": True,
        }],
        verify_selection_preview_loader=lambda selected, level: {
            "available": bool(selected["jury1"]), "overlay": {},
        },
        command_builder=lambda *args: {"command": ["worker"], "environment": {}},
    )
    window._paper_path = "paper.pdf"
    monkeypatch.setattr(window, "_launch_process", lambda command, **kwargs: launched.append(command))
    try:
        if change == "deselect":
            window.llm_role_choices["freetoken:local"][0].setChecked(True)
        window._start_process(["worker"], environment={"CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken"})
        assert entered.wait(1)
        checking = window.llm_status.text()
        assert checking == window.phase_label.text()
        assert checking in window.current_activity.text()
        if change == "deselect":
            window.llm_role_choices["freetoken:local"][0].setChecked(False)
        elif change == "settings":
            window._stage_setting("FREETOKEN_MODEL", "changed")
        else:
            window._clear_analysis()
        release.set()
        assert _process_events_until(app, lambda: "jury_readiness" not in window._loading_kinds)
        assert not launched
        assert window.llm_status.text() != checking
        assert window.llm_status.text() == window.phase_label.text()
        assert window.llm_status.text() in window.current_activity.text()
        if change == "deselect":
            window._paper_path = "paper.docx"
            window._confirm_no_jury_run = lambda: True
            window._start_analysis()
            assert len(calls) == 1
            assert "--references-only" in launched[0]
    finally:
        release.set()
        window._settings_dirty.clear()
        window.close()














@pytest.mark.parametrize(
    ("configured_role", "choice"),
    [
        (role, choice)
        for role in ("jury1", "jury2")
        for choice in ("choose", "references_only", "cancel")
    ],
)
def test_one_jury_selection_requires_an_explicit_missing_role_choice(
    monkeypatch, configured_role, choice,
):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets
    from core.gui.desktop import create_desktop_window

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    selector = "freetoken:local"
    preview_calls, readiness_calls, builds, launched, warnings, opened, dialogs = (
        [], [], [], [], [], [], []
    )

    def preview(*args):
        preview_calls.append(args)
        return {"available": False}

    def readiness(*args):
        readiness_calls.append(args)
        return {"ok": True}

    def build(*args):
        builds.append(args)
        return {"command": ["worker"], "environment": {
            "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
            "CITATION_VERIFIER_VERIFY_JURY1_ONLY": selector,
            "CITATION_VERIFIER_VERIFY_JURY2_ONLY": selector,
        }}

    window = create_desktop_window(
        language="en",
        verify_candidates_loader=lambda: [{"selector": selector, "configured": True}],
        verify_selection_preview_loader=preview,
        jury_readiness_callback=readiness,
        command_builder=build,
    )
    window._paper_path = "paper.pdf"
    missing_role = "jury2" if configured_role == "jury1" else "jury1"
    window.llm_role_choices[selector][0 if configured_role == "jury1" else 1].setChecked(True)
    preview_calls.clear()
    readiness_calls.clear()
    monkeypatch.setattr(window, "_show_provider_check_warning", warnings.append)
    monkeypatch.setattr(
        window, "_show_jury_models", lambda role: opened.append(role),
    )

    def confirm(dialog):
        translations = load_translations("en")
        expected_role = translations[f"{configured_role}_role"]
        missing_label = translations[f"{missing_role}_role"]
        labels = {
            "choose": translations["choose_missing_jury"].format(role=missing_label),
            "references_only": translations["references_check_only"],
            "cancel": translations["cancel_action"],
        }
        assert dialog.objectName() == "oneJuryConfirmationDialog"
        assert dialog.textFormat() == QtCore.Qt.TextFormat.PlainText
        assert expected_role in dialog.text() and missing_label in dialog.text()
        buttons = {button.text(): button for button in dialog.buttons()}
        assert set(buttons) == set(labels.values())
        cancel = buttons[labels["cancel"]]
        assert dialog.defaultButton() is cancel
        assert dialog.escapeButton() is cancel
        image = dialog.iconPixmap().toImage()
        assert not image.isNull()
        dialogs.append(dialog)
        buttons[labels[choice]].click()
        return 0

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", confirm)
    monkeypatch.setattr(
        window, "_launch_process",
        lambda command, **kwargs: launched.append((list(command), kwargs)),
    )
    try:
        window._start_analysis()
        assert len(dialogs) == 1
        assert not warnings and not preview_calls and not readiness_calls
        if choice == "choose":
            assert opened == [missing_role]
            assert builds == [] and launched == []
        elif choice == "cancel":
            assert opened == []
            assert builds == [] and launched == []
        else:
            assert opened == []
            assert len(builds) == len(launched) == 1
            command, kwargs = launched[0]
            assert "--references-only" in command
            assert kwargs["environment"] == {
                "CITATION_VERIFIER_VERIFY_BACKENDS": "",
                "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "",
                "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "",
            }
        assert app is not None
    finally:
        window.close()


def test_invalid_preview_still_blocks_when_both_juries_are_selected(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    from core.gui.desktop import create_desktop_window

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    selector = "freetoken:local"
    launched, warnings = [], []
    window = create_desktop_window(
        verify_candidates_loader=lambda: [{"selector": selector, "configured": True}],
        verify_selection_preview_loader=lambda *args: {"available": False},
        command_builder=lambda *args: {"command": ["worker"]},
    )
    window._paper_path = "paper.pdf"
    for checkbox in window.llm_role_choices[selector]:
        checkbox.setChecked(True)
    monkeypatch.setattr(window, "_start_process", lambda *args, **kwargs: launched.append(True))
    monkeypatch.setattr(window, "_show_provider_check_warning", warnings.append)
    monkeypatch.setattr(
        window, "_confirm_no_jury_run", lambda: pytest.fail("both roles are configured"),
    )
    monkeypatch.setattr(
        window, "_confirm_one_jury_run",
        lambda *_args: pytest.fail("both roles are configured"), raising=False,
    )
    try:
        assert window._selected_llm_roles() == {
            "jury1": [selector], "jury2": [selector],
        }
        window._start_analysis()
        assert warnings and not launched
        assert app is not None
    finally:
        window.close()






















def test_source_summary_does_not_fabricate_acceptance_and_distinguishes_other_results():
    translations = load_translations("en")
    pairs = [
        {
            "claim_id": "claim-pending", "ref_id": "ref-1",
            "status": "accepted", "terminal_cause": "jury2_accepted",
            "winner_call_id": "winner-pending", "jury1_outcome": "supports",
            "jury2_record": {"result": "pending", "reason": None},
        },
        {
            "claim_id": "claim-rejected", "ref_id": "ref-1",
            "status": "uncertain", "terminal_outcome": "supports",
            "terminal_cause": "jury2_rejected_nonbinding",
            "winner_call_id": "winner-rejected",
            "jury1_outcome": "supports",
            "jury2_record": {"result": "rejected", "reason": "Recorded rejection."},
        },
        {
            "claim_id": "claim-not-evaluated", "ref_id": "ref-1",
            "status": "accepted", "terminal_outcome": "supports",
            "terminal_cause": "jury2_off", "winner_call_id": "winner-off",
            "jury2_record": {"result": "not_evaluated", "reason": None},
        },
    ]
    expanded = {
        (pair["claim_id"], pair["ref_id"], "") for pair in pairs
    }

    html = _source_summary_html({
        "ref_id": "ref-1", "title": "A source",
        "_summary_expanded_verification_pairs": expanded,
        "details": {"verification_pairs": pairs},
    }, translations, dark=False)

    assert translations["summary_jury2_accepted"] not in html
    assert translations["summary_jury2_pending"] in html
    assert translations["summary_jury2_rejected"] in html
    assert translations["summary_jury2_not_evaluated"] in html
    assert "Recorded rejection." in html


def test_source_verdict_assessments_expand_by_keyboard_and_do_not_leak_between_sources(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtTest, QtWidgets
    from core.gui.desktop import create_desktop_window

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    translations = load_translations("en")

    def reference(ref_id, explanation, passage):
        pair = {
            "claim_id": f"claim-{ref_id}", "ref_id": ref_id,
            "scope": "fulltext_complete", "status": "accepted",
            "terminal_outcome": "supports", "terminal_cause": "jury2_accepted",
            "winner_call_id": f"call-{ref_id}", "jury1_outcome": "supports",
            "claim_text": ("The source substantiates the recorded claim. " * 30),
            "jury1_record": {
                "outcome": "supports", "explanation": explanation,
                "evidence": [{
                    "quotation": passage, "source_text": passage,
                    "raw_start": 0, "raw_end": len(passage),
                }],
            },
            "jury2_record": {"result": "accepted", "reason": f"Jury 2: {ref_id}"},
        }
        return {
            "ref_id": ref_id, "title": f"Source {ref_id}",
            "phase": "verify", "status": "completato",
            "details": {"parsed": {"title": f"Source {ref_id}"},
                        "verification_pairs": [pair]},
        }

    window = create_desktop_window(snapshot_loader=lambda _run: {
        "phase": "verify", "references": [
            reference("r1", "Jury 1 explanation for source one.", "Evidence one."),
            reference("r2", "Jury 1 explanation for source two.", "Evidence two."),
        ],
    })
    dialogs = []
    scroll_checks = []
    toggle_verdict = window._toggle_source_summary_verdict

    def track_toggle_scroll(view, row, url):
        before = view.verticalScrollBar().value()
        toggle_verdict(view, row, url)
        scroll_checks.append((before, view.verticalScrollBar().value()))

    window._toggle_source_summary_verdict = track_toggle_scroll

    def inspect_dialog(dialog):
        view = dialog.findChild(QtWidgets.QTextBrowser, "sourceDetailText")
        assert view is not None
        dialog.resize(360, 240)
        dialog.show()
        QtTest.QTest.qWait(20)
        dialogs.append(dialog)
        current_explanation = (
            "Jury 1 explanation for source one."
            if "Source r1" in dialog.windowTitle()
            else "Jury 1 explanation for source two."
        )
        current_passage = (
            "Evidence one." if "Source r1" in dialog.windowTitle()
            else "Evidence two."
        )
        assert current_explanation not in view.toPlainText()
        view.setFocus()
        QtTest.QTest.keyClick(view, QtCore.Qt.Key.Key_Tab)
        QtTest.QTest.keyClick(view, QtCore.Qt.Key.Key_Return)
        QtTest.QTest.qWait(20)
        assert current_explanation in view.toPlainText()
        assert current_passage in view.toPlainText()
        assert translations["summary_jury2_accepted"] in view.toPlainText()
        assert "Jury 2: " in view.toPlainText()
        if "Source r1" in dialog.windowTitle():
            collapse = view.document().find(
                translations["summary_collapse_recorded_verdict"]
            )
            assert not collapse.isNull()
            assert view.verticalScrollBar().maximum() > 0
            view.verticalScrollBar().setValue(24)
            QtTest.QTest.keyClick(view, QtCore.Qt.Key.Key_Tab)
            QtTest.QTest.keyClick(view, QtCore.Qt.Key.Key_Return)
            QtTest.QTest.qWait(20)
            assert current_explanation not in view.toPlainText()
            assert scroll_checks[-1][0] == scroll_checks[-1][1]
        dialog.close()
        return 0

    monkeypatch.setattr(QtWidgets.QDialog, "exec", inspect_dialog)
    try:
        window.set_run_dir("synthetic-run")
        window._show_source_details(0, 0)
        window._show_source_details(1, 0)
        assert len(dialogs) == 2
    finally:
        window.close()
        app.processEvents()






































def test_console_live_tail_stays_bounded_and_export_is_byte_identical(
    monkeypatch, tmp_path
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_CONSOLE_LIVE_WINDOW_BYTES", 4096)
    window = desktop.create_desktop_window()
    _enable_cli_console(window)
    window._begin_console_capture()
    log_path = window._console_log_path
    assert log_path is not None
    window.navigation.setCurrentRow(4)
    source = ("inizio\n" + "é🍕東京" * 2000).encode("utf-8")
    window._append_console_output(source.decode("utf-8"))

    visible = window.commands_output.toPlainText()
    visible_bytes = visible.encode("utf-8")
    trim_at = max(0, len(source) - 4096)
    while trim_at < len(source) and source[trim_at] & 0xC0 == 0x80:
        trim_at += 1
    assert visible_bytes == source[trim_at:]
    assert len(visible_bytes) <= 4096
    assert window._console_loaded_start == window._console_live_start_offset
    assert window._console_loaded_start > 0
    assert log_path.read_bytes() == source

    window._process = object()
    destination = tmp_path / "console-export.log"
    assert window._export_console_log_to(destination)
    assert destination.read_bytes() == log_path.read_bytes()
    assert "console-export.log" in window.commands_status_label.text()
    window._process = None
    window.close()
    assert not log_path.exists()
    assert app is not None


def test_header_version_uses_frozen_resource_root_version_file(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    from core.app import runtime_paths
    import core.gui.desktop as desktop

    bundle_root = tmp_path / "frozen-resource-root"
    bundle_root.mkdir()
    version = "9.8.7-preview+bundle-fixture"
    (bundle_root / "VERSION").write_text(version + "\n", encoding="utf-8")
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_paths.sys, "_MEIPASS", str(bundle_root), raising=False)
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path / "private-user-data")
    monkeypatch.setattr(desktop, "_load_settings_environment_preferences", lambda: [])
    assert desktop.resource_root() == bundle_root.resolve()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = desktop.create_desktop_window(language="en")
    try:
        header = window.findChild(QtWidgets.QFrame, "brandHeader")
        assert header is not None
        labels = header.findChildren(QtWidgets.QLabel)
        assert [label.text() for label in labels if label.text() == version] == [version]
    finally:
        window.close()
        app.processEvents()
























def test_paused_fetch_can_be_reopened_and_resume_routes_to_guided_window(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtWidgets import QApplication, QDialog
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    opened = []
    started = []
    row = {"paper": "paused.pdf", "run_dir": "paused", "available": True, "run_status": "paused"}
    controller = SimpleNamespace(
        stage_source=lambda *_args: None,
        discard_source=lambda *_args: None,
        submit_identity_decision=lambda *_args: None,
        identity_review_allowed=True,
        proceed=lambda: None,
    )

    def factory(*_args, **_kwargs):
        opened.append(True)
        return QDialog()

    window = create_desktop_window(
        snapshot_loader=lambda _run: {"phase": "fetch", "fetch_paused": True},
        history_loader=lambda: [row],
        resume_command_builder=lambda run: ["resume", run],
        guided_controller_factory=lambda _run: controller,
        guided_window_factory=factory,
    )
    window._start_process = lambda command, **_kwargs: started.append(command)
    window.set_run_dir("paused")
    assert window.open_guided_fetch_button.isVisibleTo(window)
    window.open_guided_fetch_button.click()
    assert opened == [True]
    assert not window.open_guided_fetch_button.isVisibleTo(window)
    window._guided_window.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    assert window.open_guided_fetch_button.isVisibleTo(window)

    window._resume_latest_or_stop()
    assert opened == [True, True]
    assert started == []
    window._guided_window.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    window._resume_selected_history()
    assert opened == [True, True, True]
    assert started == []
    window._guided_window.close()
    window.close()










def test_paused_parse_opens_review_before_skip_and_history_can_reopen(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtWidgets import QApplication, QDialog
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    run_dir = str(tmp_path)
    snapshot = {
        "phase": "parse", "paused": True, "action_required": True,
        "parse_review_pending": 1, "run_status": "paused", "references": [],
    }
    row = {
        "paper": "paper.pdf", "run_dir": run_dir, "available": True,
        "phase": "parse", "run_status": "paused", "action_required": True,
        "parse_review_pending": 1,
    }
    pending = [1]
    opened = []
    started = []
    controller = SimpleNamespace(
        pending_count=lambda: pending[0],
        load_tasks=lambda: [],
        submit_decision=lambda *_args: None,
        skip_remaining_reviews=lambda: None,
    )

    def factory(*_args, **kwargs):
        opened.append(kwargs)
        return QDialog()

    window = create_desktop_window(
        snapshot_loader=lambda _run: snapshot,
        history_loader=lambda: [row],
        resume_command_builder=lambda run: ["resume", run],
        parse_review_controller_factory=lambda _run: controller,
        parse_review_window_factory=factory,
    )
    window._start_process = lambda command, **_kwargs: started.append(command)
    window.set_run_dir(run_dir)
    window.skip_manual_checks.setChecked(True)
    window._finish_process_after_snapshot(
        snapshot, 10, None, False, False,
    )
    assert len(opened) == 1
    assert started == []
    dialog = window._parse_review_window
    dialog.close()
    opened[0]["on_dismiss"]()
    app.processEvents()
    assert window._parse_review_window is dialog
    assert window.open_parse_review_button.isVisibleTo(window)

    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    window._update_history_actions()
    window.navigation.setCurrentRow(1)
    assert window.history_parse_review_button.isVisibleTo(window)
    window._open_selected_history_parse_review()
    assert len(opened) == 1
    assert not dialog.isHidden()
    pending[0] = 0
    destroyed = []
    dialog.destroyed.connect(lambda: destroyed.append(True))
    opened[0]["on_complete"]()
    assert started == [["resume", run_dir]]
    assert destroyed == []
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    assert destroyed == [True]
    window.close()




def test_parse_review_resume_from_its_button_defers_dialog_deletion(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    run_dir = str(tmp_path)
    task = {
        "task_id": "review-1", "review_kind": "citation_reference_review",
        "target_sha256": "abc123", "marker_raw": "[1]",
        "candidates": [{"id": "ref-1", "label": "[1] Test source"}],
    }
    pending = [task]

    def submit(task_id, payload):
        assert task_id == "review-1" and payload["ref_id"] == "ref-1"
        pending.clear()

    controller = SimpleNamespace(
        pending_count=lambda: len(pending),
        load_tasks=lambda: list(pending),
        submit_decision=submit,
        skip_remaining_reviews=lambda: pytest.fail("complete choice must not skip"),
    )
    window = create_desktop_window(
        snapshot_loader=lambda _run: {
            "phase": "parse", "paused": True, "run_status": "paused",
            "action_required": True, "parse_review_pending": 1,
            "references": [],
        },
        resume_command_builder=lambda run: ["resume", run],
        parse_review_controller_factory=lambda _run: controller,
    )
    started = []
    destroyed = []
    window._start_process = lambda command, **_kwargs: (
        started.append(command),
        pytest.fail("dialog deleted inside its click handler") if destroyed else None,
    )
    window.show()
    window.set_run_dir(run_dir)
    window._open_parse_review(run_dir)
    dialog = window._parse_review_window
    dialog.destroyed.connect(lambda: destroyed.append(True))
    for _ in range(100):
        if dialog._current is not None:
            break
        QTest.qWait(10)
        app.processEvents()
    assert dialog._current is not None
    dialog._candidate_group.buttons()[0].click()
    dialog.resume_button.click()
    assert started == [["resume", run_dir]]
    assert destroyed == []
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    assert destroyed == [True]
    window.close()




def test_history_cannot_switch_fork_or_delete_run_with_open_parse_review(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QDialog
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    run_a = str(tmp_path / "run-a")
    run_b = str(tmp_path / "run-b")
    rows = [
        {"run_dir": run, "paper": "paper.pdf", "available": True,
         "phase": "parse", "run_status": "paused", "action_required": True,
         "parse_review_pending": 1}
        for run in (run_a, run_b)
    ]
    fork_row = {
        "run_dir": str(tmp_path / "completed"), "paper": "done.pdf",
        "available": True, "phase": "done", "run_status": "completed",
        "completed": True,
    }
    rows.append(fork_row)
    opened = []
    started = []

    def factory(run_dir, **kwargs):
        opened.append((run_dir, kwargs["on_complete"]))
        return QDialog()

    window = create_desktop_window(
        snapshot_loader=lambda _run: {"phase": "parse", "paused": True,
                                      "action_required": True, "parse_review_pending": 1,
                                      "run_status": "paused", "references": []},
        history_loader=lambda: rows,
        resume_command_builder=lambda run: ["resume", run],
        parse_review_controller_factory=lambda _run: SimpleNamespace(
            pending_count=lambda: 1, load_tasks=lambda: [],
            submit_decision=lambda *_args: None,
        ),
        parse_review_window_factory=factory,
        verify_fork_options_loader=lambda: [{"selector": "model", "label": "Model"}],
        verify_fork_selector=lambda _options: {"jury1": ["model"], "jury2": []},
        verify_fork_command_builder=lambda _row, _roles, _catalogs: {
            "command": ["fork", fork_row["run_dir"]],
            "run_dir": str(tmp_path / "fork"),
        },
    )
    window._start_process = lambda command, **_kwargs: started.append(command)
    window.set_run_dir(run_a)
    window._open_parse_review(run_a)
    assert opened[0][0] == run_a
    window._resume_run_row(rows[1])
    assert window._run_dir == run_a
    assert len(opened) == 1
    assert started == []
    assert not window._history_row_is_deletable(rows[0])
    window.navigation.setCurrentRow(1)
    app.processEvents()
    window.history_table.selectRow(0)
    window._update_history_actions()
    assert not window.history_delete_run_button.isEnabled()
    window._single_history_action_row = lambda: fork_row
    window._update_history_actions()
    assert not window.history_verify_fork_button.isEnabled()
    window._fork_selected_history_verify()
    assert window._run_dir == run_a
    assert started == []
    window._parse_review_window.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    window.close()




def test_finished_run_shows_and_opens_final_report(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    opened = []
    row = {
        "run_dir": "finished", "run_status": "completed", "phase": "done",
        "completed": True, "report_html_present": True,
        "report_html_path": "finished/report.html",
    }
    window = create_desktop_window(
        history_loader=lambda: [row],
        snapshot_loader=lambda _run: {"phase": "done", "run_status": "completed"},
        open_path_callback=opened.append,
    )
    window.set_run_dir("finished")
    assert window.open_run_report_button.isVisibleTo(window)
    assert not window.open_guided_fetch_button.isVisibleTo(window)
    window.open_run_report_button.click()
    assert opened == ["finished/report.html"]

    window._on_process_finished(0, QProcess.ExitStatus.NormalExit)
    assert opened == ["finished/report.html", "finished/report.html"]
    assert not window.resume_latest_button.isEnabled()
    window.close()
    assert app is not None


@pytest.mark.parametrize(
    ("phase", "run_status", "completed", "report_visible"),
    [
        ("done", "completed", True, True),
        ("verify", "failed", False, False),
        ("fetch", "interrupted", False, False),
    ],
)
@pytest.mark.parametrize("language", ["en", "it"])
def test_new_analysis_clears_completed_failed_and_interrupted_runs(
    monkeypatch, phase, run_status, completed, report_visible, language
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window, load_translations

    app = QApplication.instance() or QApplication([])
    translations = load_translations(language)
    run = {
        "run_dir": "old-run", "phase": "done" if completed else phase,
        "run_status": run_status, "completed": completed,
        "report_html_present": completed,
        "report_html_path": "old-run/report.html" if completed else None,
    }
    snapshot = {
        "phase": phase, "run_status": run_status, "phase_progress": 63,
        "references": [{
            "ref_id": "r1", "title": "Old source", "phase": phase,
            "status": "done",
        }],
    }
    window = create_desktop_window(
        language=language,
        history_loader=lambda: [run],
        snapshot_loader=lambda _run_dir: snapshot,
    )
    window._set_paper("old-manuscript.pdf")
    window.set_run_dir("old-run")

    assert window.new_analysis_button.text() == translations["new_analysis"]
    assert window.new_analysis_button.accessibleName() == translations["new_analysis"]
    assert window.new_analysis_button.isEnabled()
    assert window.source_table.rowCount() == 1
    assert window.open_run_report_button.isVisibleTo(window) is report_visible

    window.new_analysis_button.click()

    assert window._run_dir is None
    assert window._paper_path == ""
    assert window.paper_drop.text() == translations["drop_paper"]
    assert window.source_table.rowCount() == 0
    assert window._source_rows == []
    assert window.phase_progress.value() == 0
    assert window.phase_progress.maximum() == 100
    assert window.phase_label.text() == translations["phase_waiting"]
    assert window.current_activity.text() == translations["current_activity"].format(
        activity=translations["activity_waiting"]
    )
    assert not window.open_run_report_button.isVisibleTo(window)
    assert not window.export_run_report_button.isVisibleTo(window)
    assert not window.open_guided_fetch_button.isVisibleTo(window)
    assert not window.open_parse_review_button.isVisibleTo(window)
    assert window._history_rows == [run]
    assert not window.new_analysis_button.isEnabled()

    window._set_paper("next-manuscript.pdf")
    assert window.new_analysis_button.isEnabled()

    window._commands_process = object()
    window._update_run_controls()
    assert not window.new_analysis_button.isEnabled()
    window._commands_process = None
    window._guided_window = object()
    window._update_run_controls()
    assert not window.new_analysis_button.isEnabled()
    window._guided_window = None
    window._parse_review_window = object()
    window._update_run_controls()
    assert not window.new_analysis_button.isEnabled()
    window._parse_review_window = None
    window._update_run_controls()
    window._set_paper("")
    assert not window.new_analysis_button.isEnabled()
    window.close()
    assert app is not None




def test_identity_review_pause_resumes_without_fabricating_an_operator_answer(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    events = []
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: events.append("question"))
    window = create_desktop_window(
        snapshot_loader=lambda _run: {
            "phase": "verify", "paused": True, "action_required": True,
            "identity_review_only": True,
        },
        skip_manual_callback=lambda run: events.append(("skip", run)) or 1,
        resume_command_builder=lambda run: ["resume", run],
    )
    try:
        window.set_run_dir("paused")
        window._start_process = lambda command, **_kwargs: events.append(("resume", command))
        window._on_process_finished(10, QProcess.ExitStatus.NormalExit)
        assert events == [("resume", ["resume", "paused"])]
    finally:
        window.close()
    assert app is not None


def test_identity_review_history_resume_does_not_submit_a_skip_answer(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    events = []
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: events.append("question"))
    window = create_desktop_window(
        resume_command_builder=lambda run: ["resume", run],
        skip_manual_callback=lambda run: events.append(("skip", run)) or 1,
        resume_callback=lambda _row: events.append("resume_callback"),
    )
    try:
        window._start_process = lambda command, **_kwargs: events.append(("resume", command))
        window._resume_run_row({
            "run_dir": "paused", "run_status": "paused", "available": True,
            "phase": "verify", "action_required": True, "identity_review_only": True,
        })
        assert events == ["resume_callback", ("resume", ["resume", "paused"])]
    finally:
        window.close()
    assert app is not None






def test_skip_manual_checkbox_stops_on_unsupported_task(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    started = []
    window = create_desktop_window(
        snapshot_loader=lambda _run: {"phase": "fetch", "fetch_paused": True},
        skip_manual_callback=lambda _run: (_ for _ in ()).throw(ValueError("unsupported task")),
    )
    window.set_run_dir("paused")
    window.skip_manual_checks.setChecked(True)
    window._start_process = lambda command, **kwargs: started.append((command, kwargs))

    window._on_process_finished(10, QProcess.ExitStatus.NormalExit)

    assert started == []
    assert window.phase_label.property("error") is True
    assert "unsupported task" in window.phase_label.text()
    window.close()
    assert app is not None




def test_background_tabs_open_before_loading_and_discard_stale_results(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    history_started = threading.Event()
    release_history = threading.Event()
    calls = []

    def history():
        calls.append("history")
        history_started.set()
        assert release_history.wait(3)
        return [{"run_dir": "old", "paper": "old.pdf", "available": True}]

    window = create_desktop_window(
        background_loading=True,
        latest_loader=lambda: None,
        history_loader=history,
        cache_loader=lambda: calls.append("cache") or [],
        settings_loader=lambda: calls.append("settings") or {},
        docs_loader=lambda: calls.append("docs") or {"README.md": "# Guide"},
    )
    assert "history" not in calls
    assert "cache" not in calls
    assert "settings" not in calls
    assert "docs" not in calls

    window.navigation.setCurrentRow(1)
    assert window.pages.currentIndex() == 1
    assert window.history_stack.currentIndex() == 1
    assert _process_events_until(app, history_started.is_set)
    assert window.history_table.rowCount() == 0

    window.navigation.setCurrentRow(5)
    assert window.pages.currentIndex() == 5
    assert window.docs_stack.currentIndex() == 1
    release_history.set()
    assert _process_events_until(app, lambda: window.docs_stack.currentIndex() == 0)
    assert window.history_table.rowCount() == 0
    assert calls.count("docs") == 1
    assert "cache" not in calls
    assert "settings" not in calls
    window.close()










def test_source_page_order_filter_and_run_changes_drop_stale_source_identity(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtTest
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    rows = {
        "run": [
            {"ref_id": f"r{index}", "title": f"Source {index}",
             "text": "fulltext" if index % 2 == 0 else "none"}
            for index in range(40)
        ],
        "another-run": [
            {"ref_id": f"n{index}", "title": f"New source {index}",
             "text": "fulltext" if index % 2 == 0 else "none"}
            for index in range(20)
        ],
    }
    page_calls = []
    detail_calls = []

    def page(run_dir, offset, limit):
        page_calls.append((run_dir, offset, limit))
        inventory = rows[run_dir]
        return {"total": len(inventory), "references": inventory[offset:offset + limit]}

    def detail(run_dir, ref_id):
        detail_calls.append((run_dir, ref_id))
        return {"parsed": {"title": f"Details for {ref_id}"}}

    window = create_desktop_window(
        background_loading=True,
        latest_loader=lambda: None,
        overview_loader=lambda run_dir: {
            "phase": "fetch", "run_status": "active", "updated_at": run_dir,
        },
        source_page_loader=page,
        source_detail_loader=detail,
    )
    window.resize(900, 760)
    window.show()
    app.processEvents()
    window._page_batch_size = lambda _table: 12

    def click_source(row=0):
        item = window.source_table.item(row, 0)
        rect = window.source_table.visualItemRect(item)
        assert rect.isValid()
        QtTest.QTest.mouseClick(
            window.source_table.viewport(),
            QtCore.Qt.MouseButton.LeftButton,
            pos=rect.center(),
        )

    def click_title_header():
        header = window.source_table.horizontalHeader()
        point = QtCore.QPoint(
            header.sectionViewportPosition(1) + header.sectionSize(1) // 2,
            max(1, header.height() // 2),
        )
        QtTest.QTest.mouseClick(
            header.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point,
        )

    def visible_ref_ids():
        return [
            window.source_table.item(index, 0).data(QtCore.Qt.ItemDataRole.UserRole)["ref_id"]
            for index in range(window.source_table.rowCount())
        ]

    window.set_run_dir("run")
    assert _process_events_until(
        app, lambda: window._source_page_loaded_run == "run"
        and window.source_table.rowCount() == 12,
    )
    click_source()
    assert _process_events_until(app, lambda: window._detail_dialog is not None)
    assert window._detail_dialog[2]["ref_id"] == "r0"

    # Sort through the actual table header. Descending order moves r0 out of
    # the loaded window, so the old detail and row selection must not linger.
    before_sort = visible_ref_ids()
    click_title_header()
    assert _process_events_until(
        app, lambda: "source_query" not in window._loading_kinds
        and visible_ref_ids() != before_sort,
    )
    ascending_ids = visible_ref_ids()
    assert ascending_ids[0] == "r0"
    click_title_header()
    assert _process_events_until(
        app, lambda: "source_query" not in window._loading_kinds
        and visible_ref_ids() and visible_ref_ids()[0] != "r0",
    )
    assert window._detail_dialog is None
    assert window.source_table.currentRow() == -1
    assert visible_ref_ids()[0] == "r9"
    assert window.source_table.item(0, 1).text() == "Source 9"

    # A real filter query excludes the selected detail identity and rebuilds
    # only the visible result window with the matching source payloads.
    click_source()
    assert _process_events_until(app, lambda: window._detail_dialog is not None)
    filtered_out_id = window._detail_dialog[2]["ref_id"]
    window._atlas_filter_actions["fulltext"].setChecked(True)
    assert _process_events_until(
        app, lambda: "source_query" not in window._loading_kinds
        and visible_ref_ids() and visible_ref_ids()[0] != filtered_out_id,
    )
    assert window._detail_dialog is None
    assert all(int(ref_id[1:]) % 2 == 0 for ref_id in visible_ref_ids())

    click_source()
    assert _process_events_until(app, lambda: window._detail_dialog is not None)
    window.set_run_dir("another-run")
    loaded_new_run = _process_events_until(
        app, lambda: window.source_table.rowCount() > 0
        and visible_ref_ids() and visible_ref_ids()[0].startswith("n"),
    )
    assert loaded_new_run, (
        window._run_dir, window._source_page_loaded_run, window._loading_kinds,
        window._source_query_signature, page_calls[-5:],
        window.source_stack.currentIndex(),
        window.source_table.item(0, 1).text() if window.source_table.item(0, 1) else None,
    )
    assert window._detail_dialog is None
    assert window.source_table.currentRow() == -1
    assert visible_ref_ids()[0].startswith("n")
    assert page_calls and all(0 < limit <= 80 for _run, _offset, limit in page_calls)
    assert detail_calls[0] == ("run", "r0")
    assert all(run_dir == "run" for run_dir, _ref_id in detail_calls)
    window.close()














def test_deleting_current_paused_run_detaches_analysis_and_ignores_stale_history_page(
    monkeypatch,
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    row = {
        "run_dir": "paused-run", "paper": "Synthetic paused run",
        "available": True, "run_status": "paused", "phase": "verify",
        "action_required": True,
    }
    stored_rows = [dict(row)]
    page_calls = []
    stale_started, release_stale = threading.Event(), threading.Event()
    fresh_started, stale_finished = threading.Event(), threading.Event()

    def history_page(offset, limit):
        call_number = len(page_calls) + 1
        page_calls.append((offset, limit))
        if call_number == 2:
            stale_started.set()
            assert release_stale.wait(2.5)
            stale_finished.set()
            return {"rows": [dict(row)], "total": 2, "has_more": False}
        if call_number == 3:
            fresh_started.set()
        return {
            "rows": [dict(item) for item in stored_rows[offset:offset + limit]],
            "total": 2 if call_number == 1 else len(stored_rows),
            "has_more": call_number == 1,
        }

    deleted = []
    window = create_desktop_window(
        background_loading=True, latest_loader=lambda: None,
        overview_loader=lambda _run: {"phase": "verify", "run_status": "paused"},
        history_page_loader=history_page,
        delete_history_callback=lambda rows: (deleted.extend(rows), stored_rows.clear()),
    )
    stale_delivered = threading.Event()
    try:
        window.set_run_dir("paused-run")
        for name, busy_value in (
            ("_commands_process", object()),
            ("_institutional_launch_request", object()),
            ("_library_busy", True),
        ):
            initial = getattr(window, name)
            setattr(window, name, busy_value)
            assert not window._history_row_is_deletable(row), name
            setattr(window, name, initial)
        assert window._history_row_is_deletable(row)
        window.navigation.setCurrentRow(1)
        assert _process_events_until(app, lambda: window.history_table.rowCount() == 1)
        window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        assert [item["run_dir"] for item in window._checked_history_rows()] == ["paused-run"]

        window._show_more_history()
        assert _process_events_until(app, stale_started.is_set)
        stale_token = window._load_tokens["history_page"]
        window._load_signals.completed.connect(
            lambda kind, token, *_args: (
                stale_delivered.set()
                if kind == "history_page" and token == stale_token else None
            )
        )
        def become_busy_during_confirmation(*_args, **_kwargs):
            window._commands_process = object()
            return True

        monkeypatch.setattr(window, "_confirm_library_action", become_busy_during_confirmation)
        window._delete_selected_history()
        assert not deleted
        assert window._run_dir == "paused-run"
        window._commands_process = None
        monkeypatch.setattr(
            window, "_confirm_library_action", lambda *_args, **_kwargs: True,
        )

        window._delete_selected_history()

        assert [item["run_dir"] for item in deleted] == ["paused-run"]
        assert window._run_dir is None
        assert _process_events_until(app, fresh_started.is_set)
        assert _process_events_until(
            app, lambda: "history_page" not in window._loading_kinds
            and window._atlas_history_total == 0,
        )
        assert len(page_calls) == 3
        assert page_calls[0][0] == 0 and page_calls[-1][0] == 0
        assert window._history_rows == []
        assert window.history_table.rowCount() == 0

        release_stale.set()
        assert _process_events_until(app, stale_delivered.is_set)
        assert stale_finished.is_set()
        assert window._history_rows == []
        assert window.history_table.rowCount() == 0
        assert window._run_dir is None
    finally:
        release_stale.set()
        window._commands_process = None
        window._institutional_launch_request = None
        window._library_busy = False
        window.close()










































def test_llm_role_choices_persist_across_refresh_and_new_window(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CITATION_VERIFIER_OPENAI_API_KEY", "secret-not-for-settings")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    settings = QSettings("Callimachus", "Callimachus")
    windows = []

    try:
        candidates = [
            {"selector": "example:model-a", "label": "Model A", "configured": True},
            {"selector": "example:model-b", "label": "Model B", "configured": True},
        ]
        first_window = create_desktop_window(
            verify_candidates_loader=lambda: candidates,
        )
        windows.append(first_window)
        jury1a, jury2a = first_window.llm_role_choices["example:model-a"]
        jury1b, jury2b = first_window.llm_role_choices["example:model-b"]
        jury1a.setChecked(True)
        jury2b.setChecked(True)
        assert first_window._selected_llm_roles() == {
            "jury1": ["example:model-a"],
            "jury2": ["example:model-b"],
        }

        stored = settings.value("llm_role_selection")
        assert json.loads(stored) == {
            "jury1": ["example:model-a"],
            "jury2": ["example:model-b"],
        }
        assert "secret-not-for-settings" not in stored
        assert settings.allKeys() == ["dark_theme", "llm_role_selection"]
        assert settings.value("dark_theme", True, type=bool) is False

        first_window._refresh_llm_choices()
        jury1a, jury2a = first_window.llm_role_choices["example:model-a"]
        jury1b, jury2b = first_window.llm_role_choices["example:model-b"]
        assert jury1a.isChecked() and not jury2a.isChecked()
        assert not jury1b.isChecked() and jury2b.isChecked()

        candidates[:] = [
            candidates[0],
            {"selector": "example:new-model", "label": "New model", "configured": True},
        ]
        first_window._refresh_llm_choices()
        new_jury1, new_jury2 = first_window.llm_role_choices["example:new-model"]
        assert not new_jury1.isChecked() and not new_jury2.isChecked()
        first_window.close()
        windows.remove(first_window)

        candidates[:] = [
            {"selector": "example:model-a", "label": "Model A", "configured": True},
            {"selector": "example:model-b", "label": "Model B", "configured": True},
            {"selector": "example:new-model", "label": "New model", "configured": True},
        ]
        second_window = create_desktop_window(
            verify_candidates_loader=lambda: candidates,
        )
        windows.append(second_window)
        jury1a, jury2a = second_window.llm_role_choices["example:model-a"]
        jury1b, jury2b = second_window.llm_role_choices["example:model-b"]
        new_jury1, new_jury2 = second_window.llm_role_choices["example:new-model"]
        assert jury1a.isChecked() and not jury2a.isChecked()
        assert not jury1b.isChecked() and jury2b.isChecked()
        assert not new_jury1.isChecked() and not new_jury2.isChecked()
        second_window.close()
        windows.remove(second_window)

        settings.setValue("llm_role_selection", json.dumps({
            "jury1": ["example:removed-model"],
            "jury2": ["example:removed-model"],
        }))
        settings.sync()
        unknown_window = create_desktop_window(
            verify_candidates_loader=lambda: [
                {"selector": "example:new-a", "label": "New A", "configured": True},
                {"selector": "example:new-b", "label": "New B", "configured": True},
            ],
        )
        windows.append(unknown_window)
        assert all(
            not checkbox.isChecked()
            for roles in unknown_window.llm_role_choices.values()
            for checkbox in roles
        )
        unknown_window.close()
        windows.remove(unknown_window)

        settings.setValue("llm_role_selection", '{"jury1":[7],"jury2":[]}')
        settings.sync()
        invalid_window = create_desktop_window(
            verify_candidates_loader=lambda: [
                {"selector": "example:model-a", "label": "Model A", "configured": True},
                {"selector": "example:model-b", "label": "Model B", "configured": True},
            ],
        )
        windows.append(invalid_window)
        assert all(
            not checkbox.isChecked()
            for roles in invalid_window.llm_role_choices.values()
            for checkbox in roles
        )
        assert "Saved model choices are invalid" in invalid_window.llm_status.text()
    finally:
        for window in reversed(windows):
            window.close()
    assert app is not None














def test_history_resume_rechecks_current_durable_row_before_starting(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    history_rows = [{"paper": "paused", "run_dir": "paused", "available": True, "run_status": "paused"}]
    resumed = []
    window = create_desktop_window(
        history_loader=lambda: history_rows,
        resume_command_builder=lambda run: ["resume", run],
        resume_callback=lambda row: resumed.append(row["run_dir"]),
    )
    started = []
    window._start_process = lambda command, **_kwargs: started.append(command)
    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    app.processEvents()
    history_rows[:] = [{"paper": "paused", "run_dir": "paused", "available": True, "run_status": "completed", "completed": True}]

    window._resume_selected_history()

    assert resumed == []
    assert started == []
    window.close()








def _desktop_stop_process_stub(qprocess_type):
    actions = {"writes": [], "kills": 0}

    class ProcessStub:
        running = True

        def write(self, payload):
            actions["writes"].append(payload)

        def state(self):
            state = qprocess_type.ProcessState
            return state.Running if self.running else state.NotRunning

        def kill(self):
            actions["kills"] += 1
            self.running = False

    return ProcessStub(), actions


def test_new_analysis_requests_stop_and_clears_only_after_process_exit(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window, load_translations

    app = QApplication.instance() or QApplication([])
    translations = load_translations("en")
    window = create_desktop_window(
        snapshot_loader=lambda _run_dir: {
            "phase": "verify", "run_status": "active", "phase_progress": 41,
            "references": [{
                "ref_id": "r1", "title": "Active source", "phase": "verify",
                "status": "in_corso",
            }],
        },
    )
    window._set_paper("active-manuscript.pdf")
    window.set_run_dir("active-run")
    process, actions = _desktop_stop_process_stub(QtCore.QProcess)
    window._process = process
    window._update_run_controls()

    assert window.new_analysis_button.isEnabled()
    assert window.new_analysis_button.text() == translations["new_analysis_stop"]
    window.new_analysis_button.click()

    timer = window._stop_escalation_timer
    assert actions["writes"] == [b"STOP\n"]
    assert timer is not None and timer.interval() == 10_000 and timer.isActive()
    assert window._process is process
    assert window._new_analysis_reset_pending
    assert window._stopping
    assert window._run_dir == "active-run"
    assert window._paper_path == "active-manuscript.pdf"
    assert window.source_table.rowCount() == 1
    assert not window.new_analysis_button.isEnabled()
    assert not window.start_button.isEnabled()

    window._on_process_finished(130, QtCore.QProcess.ExitStatus.NormalExit)

    assert window._stop_escalation_timer is None
    assert window._run_dir is None
    assert window._paper_path == ""
    assert window.source_table.rowCount() == 0
    assert window.phase_label.text() == translations["phase_waiting"]
    assert window.phase_progress.value() == 0
    assert window.new_analysis_button.text() == translations["new_analysis"]
    assert not window.new_analysis_button.isEnabled()
    window._set_paper("next-manuscript.pdf")
    assert window.new_analysis_button.isEnabled()
    window.close()
    assert app is not None




@pytest.mark.parametrize("language", ["en", "it"])
def test_desktop_stop_escalates_after_grace_and_reports_unclean_stop(
    monkeypatch, language
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window, load_translations

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(language=language)
    process, actions = _desktop_stop_process_stub(QtCore.QProcess)
    window._process = process
    window._update_run_controls()

    window._resume_latest_or_stop()

    timer = window._stop_escalation_timer
    assert actions["writes"] == [b"STOP\n"]
    assert actions["kills"] == 0
    assert timer.interval() == 10_000
    assert timer.isActive()

    # Emit the single-shot timeout directly so the test does not wait ten seconds.
    timer.timeout.emit()
    assert actions["kills"] == 1

    window._on_process_finished(9, QtCore.QProcess.ExitStatus.CrashExit)
    translations = load_translations(language)
    assert window.phase_label.text() == translations["process_stopped_unclean"]
    assert window.phase_label.text() != translations["process_crashed"]
    assert window.phase_label.text() != translations["process_interrupted"]
    for widget in (
        window.phase_label, window.current_activity, window.phase_progress,
        window.phase_progress_percent,
    ):
        assert widget.property("error") is False
        assert widget.property("warning") is True
    assert window.phase_progress.maximum() == 100
    window.close()
    assert app is not None




def test_stale_stop_escalation_does_not_kill_a_subsequent_process(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window()
    old_process, old_actions = _desktop_stop_process_stub(QtCore.QProcess)
    new_process, new_actions = _desktop_stop_process_stub(QtCore.QProcess)
    window._process = old_process
    window._resume_latest_or_stop()
    old_timer = window._stop_escalation_timer

    # Model an already queued timeout arriving after a new process became current.
    window._process = new_process
    window._stopping = True
    old_timer.timeout.emit()

    assert old_actions["kills"] == 0
    assert new_actions["kills"] == 0
    window._process = None
    window._stopping = False
    window.close()
    assert app is not None


















def test_history_single_selected_run_delete_is_separate_and_protected(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    rows = [
        {"paper": "selected", "run_dir": "/runs/selected", "run_status": "completed"},
        {"paper": "active", "run_dir": "/runs/active", "run_status": "active"},
        {"paper": "other", "run_dir": "/runs/other", "run_status": "completed"},
    ]
    deleted = []
    window = create_desktop_window(
        history_loader=lambda: rows,
        delete_history_callback=lambda selected: deleted.extend(selected),
    )
    confirmation_calls = []
    monkeypatch.setattr(
        window,
        "_confirm_library_action",
        lambda message, **kwargs: confirmation_calls.append((message, kwargs)) or True,
    )
    window.navigation.setCurrentRow(1)
    window.history_table.selectRow(0)
    app.processEvents()
    assert window.history_delete_run_button.text() == "Delete selected run"
    assert window.history_delete_run_button.isEnabled()
    delete_color = window.history_delete_run_button.palette().color(
        QPalette.ColorRole.Button
    )
    assert delete_color.red() > delete_color.green()
    assert delete_color.red() > delete_color.blue()
    assert not window.history_delete_button.isEnabled()
    # Bulk checkboxes must not change the dedicated highlighted-row action.
    window.history_table.item(2, 0).setCheckState(Qt.CheckState.Checked)
    window.history_table.selectRow(1)
    app.processEvents()
    assert not window.history_delete_run_button.isEnabled()
    window.history_table.selectRow(0)
    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    app.processEvents()
    assert window.history_delete_button.isEnabled()
    assert window.history_delete_run_button.isEnabled()
    window.history_delete_run_button.click()
    assert [row["run_dir"] for row in deleted] == ["/runs/selected"]
    history_text = load_translations(window._language)
    assert confirmation_calls == [(
        history_text["confirm_delete_runs"],
        {"title": history_text["tab_history"], "icon_name": "trash"},
    )]

    window.history_table.selectRow(1)
    app.processEvents()
    assert not window.history_delete_run_button.isEnabled()
    window.close()
    assert app is not None




def test_public_export_buttons_require_completed_verify(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QLabel, QApplication, QFileDialog, QMessageBox
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    rows = [
        {
            "run_dir": "/runs/full", "available": True,
            "run_status": "completed", "phase": "done", "references_only": False,
            "report_html_present": True, "report_html_path": "/runs/full/report.html",
        },
        {
            "run_dir": "/runs/preview", "available": True,
            "run_status": "completed", "phase": "done", "references_only": True,
            "report_html_present": True,
            "report_html_path": "/runs/preview/report.preview.html",
        },
    ]
    exported = []
    suggested_paths = []
    completed = []

    def choose_export(_window, _title, default_path, _filter):
        suggested_paths.append(default_path)
        return "/exports/public.html", "HTML (*.html)"

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_export)
    monkeypatch.setattr(
        QDialog, "exec",
        lambda box: completed.append((box.windowTitle(), box.findChild(QLabel, "messageBody").text() + " " + box.findChild(QLabel, "messagePath").text()))
        or QDialog.DialogCode.Accepted,
    )

    def export_report(run_dir, output, *, cancel_check, overwrite, historical):
        assert not cancel_check()
        assert overwrite is True
        exported.append((run_dir, output, historical))
        return output

    window = create_desktop_window(
        language="en", history_loader=lambda: rows,
        snapshot_loader=lambda run_dir: {
            "phase": "done", "run_status": "completed",
            "references_only": run_dir == "/runs/preview",
        },
        export_report_callback=export_report,
    )
    window.set_run_dir("/runs/full")
    window.show()
    window.navigation.setCurrentRow(0)
    app.processEvents()
    assert window.export_run_report_button.isVisible()
    assert window.export_run_report_button.isEnabled()
    assert "redacted copy for publication" in window.export_run_report_button.toolTip()
    window.export_run_report_button.click()
    assert _process_events_until(app, lambda: window._report_export_cancel_event is None)
    assert exported == [("/runs/full", "/exports/public.html", False)]
    assert "Public report exported" in completed[-1][1]
    assert "/exports/public.html" in completed[-1][1]
    assert suggested_paths == [str(Path("/runs/full").parent / "full.public.html")]

    window.navigation.setCurrentRow(1)
    app.processEvents()
    assert window.history_export_report_button.isVisible()
    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert window.history_export_report_button.isEnabled()
    window.history_export_report_button.click()
    assert _process_events_until(app, lambda: window._report_export_cancel_event is None)
    assert exported[-1] == ("/runs/full", "/exports/public.html", False)
    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    window.history_table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    assert not window.history_export_report_button.isEnabled()
    window.set_run_dir("/runs/preview")
    assert not window.export_run_report_button.isEnabled()
    window.close()
    assert app is not None


def test_public_export_progress_is_themed_cancellable_and_finishes_before_close(
    monkeypatch, tmp_path,
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QDialog, QLabel, QApplication, QFileDialog, QMessageBox, QToolButton
    from core.gui.desktop import create_desktop_window
    from core.report.human.public_export import PublicExportCancelled

    app = QApplication.instance() or QApplication([])
    cancel_started, cancel_release = threading.Event(), threading.Event()
    committed_started, committed_release = threading.Event(), threading.Event()
    close_started, close_release = threading.Event(), threading.Event()
    warnings, completed = [], []
    selected_outputs = iter((
        str(tmp_path / "cancelled.html"), str(tmp_path / "error.html"),
        str(tmp_path / "success.html"), str(tmp_path / "committed.html"),
        str(tmp_path / "close.html"),
    ))
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        lambda *_args: (next(selected_outputs), "HTML (*.html)"),
    )
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda *_args: warnings.append(_args[2]) or QMessageBox.StandardButton.Ok,
    )
    monkeypatch.setattr(
        QDialog, "exec",
        lambda box: completed.append((box.windowTitle(), box.findChild(QLabel, "messageBody").text() + " " + box.findChild(QLabel, "messagePath").text()))
        or QDialog.DialogCode.Accepted,
    )

    def export_report(run_dir, output, *, cancel_check, overwrite, historical):
        assert overwrite is True
        assert historical is False
        if run_dir == "/runs/cancel":
            cancel_started.set()
            assert cancel_release.wait(3)
            if cancel_check():
                raise PublicExportCancelled("cancelled by the operator")
        elif run_dir == "/runs/error":
            raise ValueError("controlled export failure")
        elif run_dir == "/runs/committed":
            Path(output).write_bytes(b"committed public report")
            committed_started.set()
            assert committed_release.wait(3)
        elif run_dir == "/runs/close":
            close_started.set()
            assert close_release.wait(3)
            if cancel_check():
                raise PublicExportCancelled("cancelled while closing")
        return output

    window = create_desktop_window(language="en", export_report_callback=export_report)
    window._apply_theme(True)
    window.show()
    try:
        window._export_report_for_run("/runs/cancel")
        assert _process_events_until(app, cancel_started.is_set)
        dialog = window._report_export_dialog
        assert dialog is not None and dialog.isModal() and dialog.isVisible()
        assert dialog.windowFlags() & QtCore.Qt.WindowType.WindowCloseButtonHint
        assert dialog.width() < 900
        header = dialog._atlas_dialog_header
        close_control = header.findChild(QToolButton, "windowClose")
        if sys.platform == "darwin":
            assert not dialog.windowFlags() & QtCore.Qt.WindowType.FramelessWindowHint
            assert close_control is None or close_control.isHidden()
        else:
            assert close_control is not None and close_control.isVisible()
            assert close_control.property("atlasIcon") == "close"
            assert close_control.accessibleName() == "Close"
            assert header.layout().indexOf(close_control) == header.layout().count() - 1
        assert window._report_export_message.text() == "Creating a public redacted copy…"
        assert window._report_export_cancel_button.text() == "Cancel"
        assert window._report_export_cancel_button.objectName() == "dangerButton"
        cancel_color = window._report_export_cancel_button.palette().color(
            QPalette.ColorRole.Button
        )
        assert cancel_color.red() > cancel_color.green()
        assert cancel_color.red() > cancel_color.blue()
        assert (
            dialog.palette().color(QPalette.ColorRole.Window)
            == window.palette().color(QPalette.ColorRole.Window)
        )
        pixmap = window._report_export_spinner.pixmap()
        image = pixmap.toImage()
        colors = {
            image.pixelColor(x, y).name()
            for x in range(pixmap.width()) for y in range(pixmap.height())
            if image.pixelColor(x, y).alpha() > 0
        }
        assert QColor("#7ab4ff").name() in colors

        window._report_export_cancel_button.click()
        assert window._report_export_cancel_event.is_set()
        assert window._report_export_message.text() == "Cancelling export…"
        assert not window._report_export_cancel_button.isEnabled()
        cancel_release.set()
        assert _process_events_until(app, lambda: window._report_export_cancel_event is None)
        assert window.statusBar().currentMessage() == "Report export cancelled.", (
            warnings, completed, window.statusBar().currentMessage(),
        )
        assert not warnings and not completed

        window._export_report_for_run("/runs/error")
        assert _process_events_until(app, lambda: window._report_export_cancel_event is None)
        assert warnings and "controlled export failure" in warnings[-1]
        assert not completed

        window._export_report_for_run("/runs/success")
        assert _process_events_until(app, lambda: window._report_export_cancel_event is None)
        assert completed and str(tmp_path / "success.html") in completed[-1][1]

        committed_path = tmp_path / "committed.html"
        window._export_report_for_run("/runs/committed")
        assert _process_events_until(app, committed_started.is_set)
        window._report_export_cancel_button.click()
        assert window._report_export_cancel_event.is_set()
        committed_release.set()
        assert _process_events_until(app, lambda: window._report_export_cancel_event is None)
        assert committed_path.read_bytes() == b"committed public report"
        assert str(committed_path) in completed[-1][1]
        assert window.statusBar().currentMessage() != "Report export cancelled."

        window._export_report_for_run("/runs/close")
        assert _process_events_until(app, close_started.is_set)
        assert not window._report_export_dialog.close()
        assert window._report_export_cancel_event.is_set()
        assert not window.close()
        assert window.isVisible()
        assert window._report_export_cancel_event.is_set()
        close_release.set()
        assert _process_events_until(app, lambda: window._load_closed)
        assert window._report_export_cancel_event is None
        assert "report_export" not in window._load_futures
    finally:
        cancel_release.set()
        close_release.set()
        if window._report_export_cancel_event is not None:
            window._cancel_report_export()
            _process_events_until(app, lambda: window._report_export_cancel_event is None)
        if not window._load_closed:
            window.close()
            app.processEvents()


def test_public_export_cancel_finishes_while_queued_behind_busy_load_pool(
    monkeypatch, tmp_path,
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QDialog, QLabel, QApplication, QFileDialog, QMessageBox
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    release_workers = threading.Event()
    workers_started = [threading.Event() for _ in range(3)]
    callback_calls = []
    completed = []
    output_paths = iter((str(tmp_path / "queued.html"), str(tmp_path / "retry.html")))

    def hold_worker(started):
        started.set()
        return release_workers.wait(5)

    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        lambda *_args: (next(output_paths), "HTML (*.html)"),
    )
    monkeypatch.setattr(
        QDialog, "exec",
        lambda box: completed.append((box.windowTitle(), box.findChild(QLabel, "messageBody").text() + " " + box.findChild(QLabel, "messagePath").text()))
        or QDialog.DialogCode.Accepted,
    )

    def export_report(run_dir, output, *, cancel_check, overwrite, historical):
        callback_calls.append((run_dir, output))
        assert not cancel_check()
        assert overwrite is True
        assert historical is False
        return output

    window = create_desktop_window(language="en", export_report_callback=export_report)
    window.show()
    try:
        pool = window._load_pool
        assert pool is not None
        occupied = [pool.submit(hold_worker, started) for started in workers_started]
        assert _process_events_until(app, lambda: all(item.is_set() for item in workers_started))

        window._export_report_for_run("/runs/queued")
        queued_future = window._load_futures["report_export"]
        assert not queued_future.running()
        assert not callback_calls
        assert window._report_export_dialog is not None

        window._report_export_cancel_button.click()

        assert queued_future.cancelled()
        assert window._report_export_cancel_event is None
        assert "report_export" not in window._loading_kinds
        assert "report_export" not in window._load_futures
        assert window.statusBar().currentMessage() == "Report export cancelled."
        assert not callback_calls

        release_workers.set()
        assert all(future.result(timeout=3) for future in occupied)
        app.processEvents()
        assert not callback_calls

        window._export_report_for_run("/runs/retry")
        assert _process_events_until(app, lambda: window._report_export_cancel_event is None)
        assert callback_calls == [("/runs/retry", str(tmp_path / "retry.html"))]
        assert str(tmp_path / "retry.html") in completed[-1][1]
    finally:
        release_workers.set()
        for future in locals().get("occupied", ()):
            future.result(timeout=3)
        if window._report_export_cancel_event is not None:
            window._cancel_report_export()
            _process_events_until(app, lambda: window._report_export_cancel_event is None)
        if not window._load_closed:
            window.close()
            app.processEvents()
















def test_crashed_process_is_not_misclassified_by_numeric_exit_code(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window()
    window._on_process_finished(2, QtCore.QProcess.ExitStatus.CrashExit)

    assert window.phase_label.text() == load_translations()["process_crashed"]
    for widget in (
        window.phase_label, window.current_activity, window.phase_progress,
        window.phase_progress_percent,
    ):
        assert widget.property("error") is True
        assert widget.property("warning") is False
    assert window.phase_label.toolTip() == load_translations()[
        "process_exit_detail"
    ].format(exit_code=2)
    assert window.phase_label.text() in window.current_activity.text()
    assert window.phase_label.toolTip() in window.current_activity.toolTip()
    window.close()
    assert app is not None


def test_history_completed_run_forks_verify_with_selected_backends(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    rows = [{
        "paper": "done.pdf",
        "run_dir": "parent",
        "available": True,
        "run_status": "completed",
        "phase": "done",
        "completed": True,
    }]
    built = []
    warnings = []
    failures = {"history": False, "options": False}

    def load_history():
        if failures["history"]:
            raise OSError("history unavailable")
        return rows

    def load_options():
        if failures["options"]:
            raise RuntimeError("options unavailable")
        return [{
            "selector": "openai_compatible:deepseek-flash",
            "backend": "openai_compatible",
            "model": "deepseek-flash",
            "model_env": "OPENAI_MODEL",
            "label": "openai_compatible — deepseek-flash",
        }]

    def build(row, selected_roles, _catalogs):
        built.append((dict(row), {
            role: list(selected_roles[role]) for role in ("jury1", "jury2")
        }))
        return {
            "command": ["callimachus", "--fork-completed-verify", "parent"],
            "run_dir": "child",
            "environment": {
                "CITATION_VERIFIER_VERIFY_BACKENDS": ",".join(dict.fromkeys(
                    selector.split(":", 1)[0]
                    for role in ("jury1", "jury2")
                    for selector in selected_roles[role]
                )),
            },
        }

    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args: warnings.append(_args[-1]),
    )
    window = create_desktop_window(
        history_loader=load_history,
        verify_fork_options_loader=load_options,
        verify_fork_selector=lambda _options: {
            "jury1": ["openai_compatible:deepseek-flash"], "jury2": [],
        },
        verify_fork_command_builder=build,
    )
    started = []
    window._start_process = lambda command, **kwargs: started.append(
        (command, kwargs)
    )
    window.navigation.setCurrentRow(1)
    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    app.processEvents()

    assert window.history_verify_fork_button.isEnabled()
    window._fork_selected_history_verify()
    assert built == [(
        rows[0], {
            "jury1": ["openai_compatible:deepseek-flash"], "jury2": [],
        }
    )]
    assert started == [(
        ["callimachus", "--fork-completed-verify", "parent"],
        {
            "environment": {
                "CITATION_VERIFIER_VERIFY_BACKENDS": "openai_compatible",
            },
            "readiness_selection": (
                {"jury1": ["openai_compatible:deepseek-flash"], "jury2": []},
                "medium",
            ),
        },
    )]
    assert window._run_dir == "child"
    assert window.navigation.currentRow() == 0

    window._run_dir = "active-run"
    window._process = object()
    window._fork_selected_history_verify()
    assert window._run_dir == "active-run"
    assert len(started) == 1
    window._process = None

    failures["history"] = True
    window._fork_selected_history_verify()
    assert len(started) == 1
    failures["history"] = False
    failures["options"] = True
    window._fork_selected_history_verify()
    assert len(started) == 1
    failures["options"] = False

    rows[0] = {**rows[0], "run_status": "active", "completed": False}
    window._fork_selected_history_verify()
    assert len(started) == 1
    assert warnings
    window.close()


def test_llm_startup_discovery_keeps_roles_explicit_and_persists_valid_overlay(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QWidget
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    catalog = {
        "provider": "ollama", "ok": True,
        "models": ["qwen2.5", "llama3"], "model_env": "OLLAMA_MODEL",
        "options": [
            {
                "selector": "ollama:qwen2.5", "backend": "ollama",
                "model": "qwen2.5", "model_env": "OLLAMA_MODEL",
                "label": "ollama: qwen2.5", "jury1_selected": False,
                "jury2_selected": False, "configured": False,
            },
            {
                "selector": "ollama:llama3", "backend": "ollama",
                "model": "llama3", "model_env": "OLLAMA_MODEL",
                "label": "ollama: llama3", "jury1_selected": False,
                "jury2_selected": False, "configured": False,
            },
        ],
    }
    settings_rows = [{
        "name": "OLLAMA_MODEL", "label": "Model", "group": "models",
        "provider": "Ollama", "model_provider": "ollama",
        "check_provider": "ollama", "control": "text", "value": "",
    }]
    overlays = []
    preview_calls = []

    def preview(catalogs, selected, level):
        preview_calls.append((catalogs, selected, level))
        return {"available": bool(selected["jury1"]), "overlay": {
            "CITATION_VERIFIER_VERIFY_BACKENDS": "ollama",
            "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": level,
            "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "",
            "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "",
            "OLLAMA_MODEL": "qwen2.5",
        }}

    windows = []

    def make_window():
        window = create_desktop_window(
            settings_loader=lambda: settings_rows,
            settings_saver=lambda updates: overlays.append(dict(updates)),
            verify_candidates_loader=lambda: [],
            llm_preflight_loader=lambda: {"providers": [
                catalog,
                {
                    "provider": "claude_cli", "ok": False, "models": [],
                    "installed": True, "authenticated": False,
                    "message": "sign-in required",
                },
                {
                    "provider": "antigravity", "ok": True, "models": [],
                    "installed": True, "options": [],
                },
                {
                    "provider": "gemini_cli", "ok": False, "models": [],
                    "installed": False, "authenticated": False,
                    "options": [],
                },
            ]},
            verify_catalog_preview_loader=preview,
        )
        windows.append(window)
        return window

    try:
        first = make_window()
        assert _process_events_until(app, lambda: "ollama" in first._llm_catalogs)
        discovery_details = first.llm_discovery_details.toPlainText()
        assert "Claude Code: Not signed in" in discovery_details
        assert "Antigravity: Detected, but no supported jury adapter" in discovery_details
        assert "Gemini CLI: Not installed" in discovery_details
        assert not hasattr(first, "jury_models")
        analysis_controls = first.findChild(QWidget, "analysisControlsContent")
        assert analysis_controls is not None
        assert analysis_controls.findChild(QWidget, "llmStatus") is None
        assert analysis_controls.findChild(QWidget, "llmDiscoveryStatus") is None
        assert analysis_controls.findChild(QWidget, "llmDiscoveryDetails") is None
        assert first.llm_choice_dialog.isAncestorOf(first.llm_status)
        assert first.llm_discovery_status.parentWidget() is first.llm_choice_dialog
        assert first.llm_preflight_button.parentWidget() is first.llm_choice_dialog
        assert first.llm_discovery_details.parentWidget() is first.llm_choice_dialog
        first.show()
        first._show_jury_models("jury1")
        app.processEvents()
        assert first.llm_choice_dialog.isVisible()
        assert not first.llm_discovery_status.isVisible()
        assert not first.llm_discovery_details.isVisible()
        assert not first.llm_status.isVisible()
        assert first.llm_picker_check.isVisible() and first.llm_picker_check.isEnabled()
        assert first.llm_picker_status.isVisible()
        first.llm_choice_dialog.close()
        jury1, jury2 = first.llm_role_choices["ollama:qwen2.5"]
        assert not jury1.isChecked() and not jury2.isChecked()
        first.navigation.setCurrentRow(3)
        app.processEvents()
        _select_gui_settings_category(first, "models")
        assert "OLLAMA_MODEL" not in first._settings_editors
        assert first._llm_catalogs["ollama"]["models"] == ["qwen2.5", "llama3"]
        assert "ollama:qwen2.5" in first.llm_role_choices

        # A previously staged model setting must not override the explicit
        # catalog-backed role choice when its validated overlay is persisted.
        first._stage_setting("OLLAMA_MODEL", "llama3")
        first._stage_setting("ANOTHER_SETTING", "keep-me")
        jury1, jury2 = first.llm_role_choices["ollama:qwen2.5"]
        jury1.setChecked(True)
        assert jury1.isChecked() and not jury2.isChecked()
        selected_overlay = overlays[-1]
        assert selected_overlay["OLLAMA_MODEL"] == "qwen2.5"
        assert first._settings_dirty == {"ANOTHER_SETTING": "keep-me"}
        assert first._settings_initial["OLLAMA_MODEL"] == "qwen2.5"
        assert preview_calls[-1][0]["ollama"]["models"] == catalog["models"]
        settings = QSettings("Callimachus", "Callimachus")
        assert json.loads(settings.value("llm_role_selection")) == {
            "jury1": ["ollama:qwen2.5"], "jury2": [],
        }
        assert "OLLAMA_MODEL" not in settings.allKeys()
        assert first._save_settings()
        assert overlays[-1] == {"ANOTHER_SETTING": "keep-me"}
        first.close()
        windows.remove(first)

        second = make_window()
        assert _process_events_until(app, lambda: "ollama" in second._llm_catalogs)
        jury1, jury2 = second.llm_role_choices["ollama:qwen2.5"]
        assert jury1.isChecked() and not jury2.isChecked()
        second.close()
        windows.remove(second)
    finally:
        for window in windows:
            window._discard_settings()
            window.close()








@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(401, "invalid or expired"), (403, "rejected or lacks permission")],
)
def test_provider_check_auth_errors_are_localized_and_never_show_key(
    monkeypatch, status_code, expected,
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    dialogs = []
    def capture_dialog(dialog):
        dialogs.append((str(dialog.windowTitle()), str(dialog.text()), dialog.textFormat()))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "exec", capture_dialog)
    rows = [{
        "name": "OPENAI_API_KEY", "label": "API key", "group": "models",
        "provider": "OpenAI", "check_provider": "openai", "control": "secret",
        "configured": False, "value": "",
    }]
    window = create_desktop_window(
        settings_loader=lambda: rows,
        provider_check_callback=lambda provider, _updates: {
            "provider": provider, "ok": False, "models": [],
            "status_code": status_code, "message": "safe provider error",
        },
    )
    window.navigation.setCurrentRow(3)
    _select_gui_settings_category(window, "models")
    window._stage_secret("OPENAI_API_KEY", "do-not-display-this-key")
    window.llm_picker_check.click()
    assert _process_events_until(
        app,
        lambda: (window._provider_check_state.get("openai") or {}).get("status") == "error",
    )
    status = window.llm_picker_status
    assert window.llm_picker_check.property("providerCheckState") == "error"
    assert '#juryModelsCheck[providerCheckState="error"]' in _stylesheet(dark=False)
    assert len(dialogs) == 1 and expected in dialogs[0][1]
    assert dialogs[0][2] == QtCore.Qt.TextFormat.PlainText
    assert "do-not-display-this-key" not in status.text()
    assert "do-not-display-this-key" not in dialogs[0][1]
    window._discard_settings()
    window.close()


def test_provider_check_discards_stale_response_after_input_edit(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    release = threading.Event()
    finished = threading.Event()
    captured = []
    rows = [{
        "name": "OLLAMA_HOST", "label": "Host", "group": "models",
        "provider": "Ollama", "check_provider": "ollama", "control": "text",
        "configured": True, "value": "http://saved-host:11434",
    }]

    def check(provider, updates):
        captured.append(dict(updates))
        release.wait(2)
        finished.set()
        return {
            "provider": provider, "ok": True, "models": ["stale-model"],
            "options": [{
                "selector": "ollama:stale-model", "backend": "ollama",
                "model": "stale-model", "model_env": "OLLAMA_MODEL",
                "label": "ollama: stale-model", "configured": False,
            }],
        }

    window = create_desktop_window(
        settings_loader=lambda: rows,
        provider_check_callback=check,
        verify_candidates_loader=lambda: [],
    )
    window.navigation.setCurrentRow(3)
    _select_gui_settings_category(window, "models")
    window._stage_setting("OLLAMA_HOST", "http://first-host:11434")
    button = window.llm_picker_check
    button.click()
    window._stage_setting("OLLAMA_HOST", "http://second-host:11434")
    assert window._provider_check_state["ollama"]["status"] == "stale"
    assert button.isEnabled()
    assert captured == [{"OLLAMA_HOST": "http://first-host:11434"}]
    release.set()
    assert finished.wait(2)
    app.processEvents()
    assert "ollama" not in window._llm_catalogs
    assert window._provider_check_state["ollama"]["status"] == "stale"
    assert "ollama:stale-model" not in window.llm_role_choices
    window._discard_settings()
    window.close()


def test_provider_check_survives_navigation_and_close_cancels_future(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    gates = [threading.Event(), threading.Event()]
    started = [threading.Event(), threading.Event()]
    calls = []
    rows = [{
        "name": "MISTRAL_MODEL", "label": "Model", "group": "models",
        "provider": "Mistral", "model_provider": "mistral",
        "check_provider": "mistral", "control": "text", "value": "",
    }]

    def check(provider, _updates):
        index = len(calls)
        calls.append(index)
        started[index].set()
        gates[index].wait(2)
        return {
            "provider": provider, "ok": True, "models": [f"model-{index}"],
            "options": [{
                "selector": f"mistral:model-{index}", "backend": "mistral",
                "model": f"model-{index}", "model_env": "MISTRAL_MODEL",
                "label": f"mistral: model-{index}", "configured": False,
            }],
        }

    window = create_desktop_window(
        settings_loader=lambda: rows,
        provider_check_callback=check,
        verify_candidates_loader=lambda: [],
    )
    try:
        window.navigation.setCurrentRow(3)
        _select_gui_settings_category(window, "models")
        button = window.llm_picker_check
        button.click()
        assert started[0].wait(2)
        window.navigation.setCurrentRow(0)
        window.navigation.setCurrentRow(3)
        assert window._provider_check_state["mistral"]["status"] == "checking"
        gates[0].set()
        assert _process_events_until(
            app,
            lambda: (window._provider_check_state.get("mistral") or {}).get("status") == "success",
        )

        button = window.llm_picker_check
        button.click()
        assert started[1].wait(2)
        assert not button.isEnabled()
        window.close()
        assert window._load_closed
    finally:
        for gate in gates:
            gate.set()


def test_codex_catalog_startup_and_restart_keep_only_configured_luna(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.app.providers.discovery import _catalog_result
    from core.app.settings.config import catalog_verify_preview, discover_verify_backend_options
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    environ = {
        "CITATION_VERIFIER_VERIFY_BACKENDS": "codex_cli",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium",
        "CODEX_CLI_MODEL": "gpt-6-luna",
    }
    models = ["gpt-6-luna", "gpt-5.6-luna"]
    windows = []

    def catalog():
        return _catalog_result("codex_cli", environ, ok=True, models=list(models),
                               backend="codex_cli", message="synthetic catalog")

    def make_window():
        window = create_desktop_window(
            verify_candidates_loader=lambda: discover_verify_backend_options(environ),
            llm_preflight_loader=lambda: {"providers": [catalog()]},
            settings_saver=lambda updates: environ.update(updates),
            verify_catalog_preview_loader=lambda catalogs, selected, level: (
                catalog_verify_preview(environ, catalogs, selected, jury2_level=level)
            ),
        )
        windows.append(window)
        assert _process_events_until(app, lambda: "codex_cli" in window._llm_catalogs)
        return window

    expected = {"jury1": ["codex_cli:gpt-6-luna"], "jury2": ["codex_cli:gpt-6-luna"]}
    try:
        first = make_window()
        # No Qt preference yet: the configured model must still win over the catalog.
        assert first._selected_llm_roles() == expected
        assert first._save_llm_role_preferences()
        first.close()
        windows.remove(first)
        models.append("gpt-6-astra")
        second = make_window()
        assert second._selected_llm_roles() == expected
        assert all(not box.isChecked() for box in second.llm_role_choices["codex_cli:gpt-6-astra"])
        second._llm_catalogs["codex_cli"] = catalog()
        second._refresh_llm_choices()
        assert second._selected_llm_roles() == expected
        preview = second._selected_llm_preview()
        assert preview["available"] is True
        assert preview["overlay"]["CODEX_CLI_MODEL"] == "gpt-6-luna"
    finally:
        for window in windows:
            window._discard_settings()
            window.close()


