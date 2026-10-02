# tests/test_startup_splash_gui_optional.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Startup splash handoff, cancellation and error handling."""

from __future__ import annotations
import threading
import time
import pytest
from core.gui.startup_splash import run_with_splash
def _application(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
def test_splash_is_visible_while_prepare_runs_and_closes_after_handoff(monkeypatch):
    app = _application(monkeypatch)
    from PySide6.QtCore import QThread, QTimer
    from PySide6.QtWidgets import QWidget
    from core.gui import desktop

    previous_quit_setting = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(True)
    prepare_release = threading.Event()
    prepare_finished = threading.Event()
    observations = {}
    main_windows = []

    class MainWindow(QWidget):
        def paintEvent(self, event):
            session = app._callimachus_startup_session
            observations.setdefault(
                "splash_visible_during_main_paint", session.splash.isVisible()
            )
            super().paintEvent(event)

        def closeEvent(self, event):
            observations["main_window_closed"] = True
            super().closeEvent(event)

    def create_main_window(**kwargs):
        assert kwargs["background_loading"] is True
        window = MainWindow()
        main_windows.append(window)
        return window

    monkeypatch.setattr(desktop, "create_desktop_window", create_main_window)

    def prepare():
        prepare_release.wait(3)
        prepare_finished.set()

    def inspect_splash_before_prepare_finishes():
        session = app._callimachus_startup_session
        observations["splash_visible_while_preparing"] = session.splash.isVisible()
        observations["prepare_still_running"] = not prepare_finished.is_set()
        prepare_release.set()

    QTimer.singleShot(40, inspect_splash_before_prepare_finishes)

    def start():
        observations["start_ran_on_gui_thread"] = QThread.currentThread() == app.thread()
        result = desktop.run_desktop()
        window = main_windows[0]

        def close_main_window():
            observations["splash_closed_after_handoff"] = not (
                app._callimachus_startup_session.splash.isVisible()
            )
            observations["main_window_visible"] = window.isVisible()
            window.close()

        QTimer.singleShot(50, close_main_window)
        return result

    watchdog = QTimer(app)
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: app.exit(99))

    try:
        watchdog.start(1500)
        assert run_with_splash(prepare=prepare, start=start) == 0
        watchdog.stop()
        assert observations == {
            "splash_visible_while_preparing": True,
            "prepare_still_running": True,
            "start_ran_on_gui_thread": True,
            "splash_visible_during_main_paint": True,
            "splash_closed_after_handoff": True,
            "main_window_visible": True,
            "main_window_closed": True,
        }
        assert app.quitOnLastWindowClosed() is True
    finally:
        watchdog.stop()
        prepare_release.set()
        app.setQuitOnLastWindowClosed(previous_quit_setting)
def test_native_close_cancels_promptly_without_waiting_for_prepare(monkeypatch):
    app = _application(monkeypatch)
    from PySide6.QtCore import Qt, QTimer

    previous_quit_setting = app.quitOnLastWindowClosed()
    prepare_release = threading.Event()
    prepare_started = threading.Event()
    prepare_finished = threading.Event()
    start_called = threading.Event()
    observations = {}

    def prepare():
        prepare_started.set()
        prepare_release.wait(5)
        prepare_finished.set()

    def close_native_splash_window():
        session = app._callimachus_startup_session
        observations["accessible_name"] = session.splash.accessibleName()
        observations["window_icon"] = not session.splash.windowIcon().isNull()
        observations["native_close_button"] = bool(
            session.splash.windowFlags() & Qt.WindowType.WindowCloseButtonHint
        )
        session.splash.close()

    QTimer.singleShot(40, close_native_splash_window)
    started_at = time.monotonic()
    try:
        result = run_with_splash(
            prepare=prepare,
            start=lambda: start_called.set() or 0,
        )
    finally:
        prepare_release.set()
        app.setQuitOnLastWindowClosed(previous_quit_setting)

    elapsed = time.monotonic() - started_at
    assert result == 130
    assert elapsed < 1.5
    assert prepare_started.is_set()
    assert prepare_finished.wait(1)
    assert not start_called.is_set()
    assert observations == {
        "accessible_name": "Callimachus startup",
        "window_icon": True,
        "native_close_button": True,
    }
def test_prepare_and_start_errors_are_reraised_and_zero_requires_handoff(monkeypatch):
    app = _application(monkeypatch)
    previous_quit_setting = app.quitOnLastWindowClosed()
    prepare_failure = RuntimeError("prepare failure")
    start_failure = RuntimeError("start failure")

    def fail_prepare():
        raise prepare_failure

    with pytest.raises(RuntimeError, match="prepare failure") as caught:
        run_with_splash(prepare=fail_prepare, start=lambda: 0)
    assert caught.value is prepare_failure

    def fail_start():
        raise start_failure

    with pytest.raises(RuntimeError, match="start failure") as caught:
        run_with_splash(prepare=lambda: None, start=fail_start)
    assert caught.value is start_failure

    with pytest.raises(RuntimeError, match="success without a window handoff"):
        run_with_splash(prepare=lambda: None, start=lambda: 0)

    assert run_with_splash(prepare=lambda: None, start=lambda: 7) == 7
    assert app.quitOnLastWindowClosed() is previous_quit_setting
def test_close_during_initial_show_skips_prepare_and_event_loop(monkeypatch):
    app = _application(monkeypatch)
    from PySide6.QtCore import QTimer

    prepare_called = threading.Event()
    start_called = threading.Event()

    def close_splash_during_initial_event_pump():
        session = app._callimachus_startup_session
        session.splash.close()

    QTimer.singleShot(0, close_splash_during_initial_event_pump)
    result = run_with_splash(
        prepare=lambda: prepare_called.set(),
        start=lambda: start_called.set() or 0,
    )
    assert result == 130
    assert not prepare_called.is_set()
    assert not start_called.is_set()
