# tests/test_atlas_navigation.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Responsive sidebar and options behavior at remote-desktop widths."""
from __future__ import annotations

import pytest


@pytest.fixture
def qt(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from pathlib import Path
    from PySide6 import QtCore, QtGui, QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    old_font = app.font()
    for filename in ("segoeui.ttf", "segoeuib.ttf"):
        font_path = Path("C:/Windows/Fonts") / filename
        if font_path.is_file():
            QtGui.QFontDatabase.addApplicationFont(str(font_path))
    font = QtGui.QFont("Segoe UI")
    font.setPixelSize(16)
    app.setFont(font)
    original = QtCore.QSettings

    def settings(*_args, **_kwargs):
        return original(str(tmp_path / "prefs.ini"), original.Format.IniFormat)

    settings.Status = original.Status
    monkeypatch.setattr(QtCore, "QSettings", settings)
    yield app
    app.setFont(old_font)






def _settle(qt, *, owner, timeout_ms=2000):
    from PySide6 import QtCore
    from PySide6.QtTest import QTest
    timer = QtCore.QElapsedTimer()
    timer.start()
    quiet_frames = 0
    while timer.elapsed() < timeout_ms:
        qt.processEvents()
        animations = owner.findChildren(QtCore.QAbstractAnimation)
        running = [animation for animation in animations
                   if animation.state() == QtCore.QAbstractAnimation.State.Running]
        if running:
            quiet_frames = 0
        else:
            quiet_frames += 1
            if quiet_frames >= 2:
                return
        QTest.qWait(10)
    assert not running, "Qt animations did not finish before the bounded wait expired"


def _visible_list_content_width(list_widget):
    row_widths = [
        list_widget.sizeHintForIndex(list_widget.indexFromItem(item)).width()
        for index in range(list_widget.count())
        if not (item := list_widget.item(index)).isHidden()
    ]
    return max(232, max(row_widths, default=0)
               + list_widget.width() - list_widget.viewport().width())






def test_main_navigation_can_overlay_during_run_without_replacing_run_state(qt):
    from core.gui.desktop import create_desktop_window
    window = create_desktop_window(settings_loader=lambda: [])
    window.resize(984, 900)
    window.show()
    try:
        _settle(qt, owner=window)
        controller = window._atlas_navigation_controller
        assert controller.is_overlay
        assert window.navigation_toggle.isVisible()
        window._run_dir = "fixture-run"
        window._update_run_controls()
        run_page = window.pages.widget(0)
        width = run_page.width()
        navigation_splitter = window._atlas_global_splitter
        nav_shell = window.navigation.parentWidget()
        visible_rows = [
            window.navigation.sizeHintForIndex(
                window.navigation.indexFromItem(window.navigation.item(index))
            ).width()
            for index in range(window.navigation.count())
            if not window.navigation.item(index).isHidden()
        ]
        visible_actions = [
            button for button in window._atlas_nav_buttons if not button.isHidden()
        ]
        margins = nav_shell.layout().contentsMargins()
        expected_width = max(
            max(visible_rows) + window.navigation.width() - window.navigation.viewport().width(),
            max((button.sizeHint().width() for button in visible_actions), default=0),
        ) + margins.left() + margins.right()
        expected_width = max(232, expected_width, controller._cover_header_minimum_width())
        window.navigation_toggle.click()
        _settle(qt, owner=window)
        assert controller.overlay.isVisible()
        assert controller.overlay.width() < navigation_splitter.width()
        assert abs(controller.overlay.width() - expected_width) <= 3
        assert run_page.width() == width
        window.navigation.setCurrentRow(3)
        _settle(qt, owner=window)
        assert not controller.overlay.isVisible()
        assert window._run_dir == "fixture-run"
        assert window.settings_navigation_toggle.isVisible()
    finally:
        window._run_dir = None
        window.close()
        qt.processEvents()


def test_settings_categories_overlay_and_keep_unsaved_editor(qt):
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtTest import QTest
    from core.gui.desktop import create_desktop_window
    name = "CITATION_VERIFIER_MAILTO"
    window = create_desktop_window(settings_loader=lambda: [{
        "name": name, "control": "text", "value": "", "group": "resolution",
    }])
    window.setMinimumWidth(600)
    window.resize(760, 900)
    window.show()
    window.navigation.setCurrentRow(3)
    try:
        _settle(qt, owner=window)
        controller = window._settings_navigation_controller
        assert controller.is_overlay
        editor = window._settings_editors[name]
        editor.setFocus()
        QTest.keyClicks(editor, "unsaved@example.test")
        width = window.settings_stack.width()
        window.settings_navigation_toggle.click()
        _settle(qt, owner=window)
        assert controller.overlay.isVisible()
        assert window.settings_stack.width() == width
        assert editor.text() == "unsaved@example.test"
        assert name in window._settings_dirty

        outside_action = QtWidgets.QPushButton("outside action", window)
        outside_action.setGeometry(window.width() - 80, 8, 72, 32)
        outside_calls = []
        outside_action.clicked.connect(lambda: outside_calls.append(True))
        qt.processEvents()
        QTest.mouseClick(outside_action, QtCore.Qt.MouseButton.LeftButton)
        _settle(qt, owner=window)
        assert not outside_calls
        assert not controller.overlay.isVisible()
        assert editor.text() == "unsaved@example.test"
        window.settings_navigation_toggle.click()
        _settle(qt, owner=window)
        assert controller.overlay.isVisible()

        window.settings_categories.setCurrentRow(1)
        _settle(qt, owner=window)
        assert not controller.overlay.isVisible()
        assert window.settings_categories.currentRow() == 1
        window.resize(1500, 900)
        _settle(qt, owner=window)
        assert not controller.is_overlay
        assert window.settings_categories.isVisible()
        assert not window.settings_navigation_toggle.isVisible()
        assert not window.navigation_toggle.isVisible()
        window.settings_categories.setCurrentRow(0)
        _settle(qt, owner=window)
        assert window._settings_editors[name].text() == "unsaved@example.test"

        controller.overlay.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(
            None, QtCore.QEvent.Type.DeferredDelete,
        )
        qt.processEvents()
        QTest.mouseClick(outside_action, QtCore.Qt.MouseButton.LeftButton)
        assert outside_calls == [True]
    finally:
        window._settings_dirty.clear()
        window.close()
        qt.processEvents()


def test_narrow_options_cover_content_and_keep_staged_choices_across_resize(qt):
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtTest import QTest
    from core.gui.desktop import create_desktop_window
    window = create_desktop_window(settings_loader=lambda: [])
    window.resize(984, 900)
    window.show()
    try:
        _settle(qt, owner=window)
        startup_width = window._atlas_startup.width()
        window._atlas_options_button.click()
        _settle(qt, owner=window)
        assert window._atlas_options_overlay
        drawer = window._atlas_options_drawer
        body = window._atlas_options_splitter
        assert drawer.isVisible()
        assert drawer.geometry() == window._atlas_analysis_workspace.rect()
        assert window._atlas_startup.width() >= startup_width
        original = window.skip_manual_checks.isChecked()
        controls = window._atlas_options_controls
        controls["fetch"].setChecked(original)
        window.resize(1600, 1000)
        QTest.qWait(300)
        _settle(qt, owner=window)
        assert not window._atlas_options_overlay
        assert drawer.parentWidget() is body
        assert controls["fetch"].isChecked() == original
        window.resize(984, 900)
        _settle(qt, owner=window)
        assert window._atlas_options_overlay
        apply = next(b for b in drawer.findChildren(QtWidgets.QPushButton)
                     if b.objectName() == "primaryButton")
        apply.click()
        _settle(qt, owner=window)
        assert not drawer.isVisible()
        assert window.skip_manual_checks.isChecked() != original
        assert not window._atlas_options_button.isChecked()
    finally:
        window.close()
        qt.processEvents()
