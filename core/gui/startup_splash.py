# core/gui/startup_splash.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only

"""Optional Qt startup splash for the desktop application."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys
import threading
from types import TracebackType
from typing import Any


def _callimachus_icon(QtCore: Any, QtGui: Any) -> Any:
    """Load the packaged square mark for native Qt chrome."""
    icon_path = Path(__file__).resolve().parent / "assets" / "app-icon.png"
    if not icon_path.is_file():
        raise FileNotFoundError(f"Callimachus application icon is missing: {icon_path}")
    pixmap = QtGui.QPixmap(str(icon_path))
    if pixmap.isNull():
        raise RuntimeError(f"Callimachus application icon could not be loaded: {icon_path}")
    icon = QtGui.QIcon(pixmap)
    if icon.isNull():
        raise RuntimeError(f"Callimachus application icon could not be loaded: {icon_path}")
    return icon


class _StartupSession:
    def __init__(
        self,
        *,
        app: Any,
        splash: Any,
        prepare: Callable[[], object],
        start: Callable[[], int],
        quit_on_last_window_closed: bool,
        QtCore: Any,
    ) -> None:
        self.app = app
        self.splash = splash
        self._prepare = prepare
        self._start = start
        self._QtCore = QtCore
        self._quit_on_last_window_closed = quit_on_last_window_closed
        self._quit_setting_restored = False
        self._prepare_finished = threading.Event()
        self._prepare_exception: tuple[BaseException, TracebackType | None] | None = None
        self._exception: tuple[BaseException, TracebackType | None] | None = None
        self._thread: threading.Thread | None = None
        self._cancelled = False
        self._ready = False
        self.window: Any | None = None
        self.start_return_code: int | None = None

        self._timer = QtCore.QTimer(app)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._poll_prepare)
        self.splash.set_close_handler(self.cancel)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def ready(self) -> bool:
        return self._ready

    def begin_prepare(self) -> None:
        self._thread = threading.Thread(
            target=self._prepare_in_background,
            name="callimachus-startup-prepare",
            daemon=True,
        )
        self._thread.start()
        self._timer.start()

    def _prepare_in_background(self) -> None:
        try:
            self._prepare()
        except BaseException as exc:
            self._prepare_exception = (exc, exc.__traceback__)
        finally:
            self._prepare_finished.set()

    def _poll_prepare(self) -> None:
        if self._cancelled:
            self._timer.stop()
            return
        if not self._prepare_finished.is_set():
            return

        self._timer.stop()
        if self._prepare_exception is not None:
            self._fail(*self._prepare_exception)
            return

        try:
            self.start_return_code = int(self._start())
        except BaseException as exc:
            self._fail(exc, exc.__traceback__)
            return

        if not self._ready:
            if self.start_return_code == 0:
                self._fail(
                    RuntimeError("desktop startup returned success without a window handoff"),
                    None,
                )
            else:
                self._terminate()

    def window_ready(self, window: Any) -> None:
        """Finish the splash handoff after the desktop has had an initial paint."""
        if self._cancelled:
            return
        if self._ready:
            if self.window is window:
                return
            self._fail(RuntimeError("startup session received more than one main window"), None)
            return

        self._ready = True
        self.window = window
        try:
            if not window.isVisible():
                raise RuntimeError("desktop window must be shown before startup handoff")
            window.repaint()
            self._QtCore.QTimer.singleShot(0, self._finish_handoff)
        except BaseException as exc:
            self._fail(exc, exc.__traceback__)

    def cancel(self) -> None:
        """Abort promptly when the native splash window is closed by the user."""
        if self._cancelled or self._ready:
            return
        self._cancelled = True
        self._timer.stop()
        self.app.quit()

    def _finish_handoff(self) -> None:
        try:
            if self.window is None or not self.window.isVisible():
                self._terminate()
                return
            self.window.repaint()
            self.app.processEvents()
            self._restore_quit_setting()
            self.splash.close_programmatically()
            if not self.window.isVisible():
                self.app.quit()
        except BaseException as exc:
            self._fail(exc, exc.__traceback__)

    def _fail(self, exc: BaseException, traceback: TracebackType | None) -> None:
        if self._exception is None:
            self._exception = (exc, traceback)
        self._terminate()

    def _terminate(self) -> None:
        self._timer.stop()
        self._restore_quit_setting()
        self.splash.close_programmatically()
        self.app.quit()

    def _restore_quit_setting(self) -> None:
        if self._quit_setting_restored:
            return
        self.app.setQuitOnLastWindowClosed(self._quit_on_last_window_closed)
        self._quit_setting_restored = True

    def cleanup(self) -> None:
        self._timer.stop()
        self._restore_quit_setting()
        self.splash.close_programmatically()
        if getattr(self.app, "_callimachus_startup_session", None) is self:
            delattr(self.app, "_callimachus_startup_session")


def _create_splash(QtCore: Any, QtGui: Any, QtWidgets: Any) -> Any:
    settings = QtCore.QSettings("Callimachus", "Callimachus")
    dark = bool(settings.value("dark_theme", False, type=bool))
    palette = (
        {
            "surface": "#182330",
            "ink": "#e6edf6",
            "muted": "#9faec4",
            "line": "#34455b",
            "accent": "#7ab4ff",
        }
        if dark
        else {
            "surface": "#ffffff",
            "ink": "#11183c",
            "muted": "#66769c",
            "line": "#d1def2",
            "accent": "#175fe6",
        }
    )
    asset_dir = Path(__file__).resolve().parent / "assets"
    logo_path = asset_dir / ("logo-dark.png" if dark else "logo.png")
    if not logo_path.is_file():
        raise FileNotFoundError(f"Callimachus startup logo is missing: {logo_path}")
    logo = QtGui.QPixmap(str(logo_path))
    if logo.isNull():
        raise RuntimeError(f"Callimachus startup logo could not be loaded: {logo_path}")
    scaled_logo = logo.scaled(
        270,
        106,
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation,
    )
    scaled_image = scaled_logo.toImage()
    visible_pixels = [
        (x, y)
        for y in range(scaled_image.height())
        for x in range(scaled_image.width())
        if scaled_image.pixelColor(x, y).alpha() > 0
    ]
    visible_bounds = QtCore.QRect(
        min(x for x, _y in visible_pixels),
        min(y for _x, y in visible_pixels),
        max(x for x, _y in visible_pixels) - min(x for x, _y in visible_pixels) + 1,
        max(y for _x, y in visible_pixels) - min(y for _x, y in visible_pixels) + 1,
    ) if visible_pixels else QtCore.QRect()
    if visible_bounds.isEmpty():
        raise RuntimeError(f"Callimachus startup logo has no visible pixels: {logo_path}")
    visible_logo = scaled_logo.copy(visible_bounds)
    icon = _callimachus_icon(QtCore, QtGui)
    app = QtWidgets.QApplication.instance()
    if app is not None:
        app.setWindowIcon(icon)

    class _GlyphOnlyCloseButton(QtWidgets.QPushButton):
        def __init__(self, parent) -> None:
            super().__init__(parent)
            from .atlas_icons import icon as atlante_icon

            self._normal_glyph = atlante_icon(
                QtCore, QtGui, "close", color=palette["accent"], size=18
            )
            self._active_glyph = atlante_icon(
                QtCore, QtGui, "close", color=palette["ink"], size=18
            )
            self.setIcon(self._normal_glyph)
            self.setIconSize(QtCore.QSize(18, 18))
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)

        def enterEvent(self, event: Any) -> None:
            super().enterEvent(event)
            self.update()

        def leaveEvent(self, event: Any) -> None:
            super().leaveEvent(event)
            self.update()

        def focusInEvent(self, event: Any) -> None:
            super().focusInEvent(event)
            self.update()

        def focusOutEvent(self, event: Any) -> None:
            super().focusOutEvent(event)
            self.update()

        def _paint_glyph(self, painter, *, active: bool) -> None:
            glyph = self._active_glyph if active else self._normal_glyph
            width = min(18, self.width(), self.height())
            rect = QtCore.QRect(
                (self.width() - width) // 2,
                (self.height() - width) // 2,
                width,
                width,
            )
            glyph.paint(
                painter,
                rect,
                QtCore.Qt.AlignmentFlag.AlignCenter,
                QtGui.QIcon.Mode.Normal,
                QtGui.QIcon.State.Off,
            )

        def _glyph_is_active(self) -> bool:
            return self.underMouse() or self.isDown() or self.hasFocus()

        def paintEvent(self, event: Any) -> None:
            del event
            painter = QtGui.QPainter(self)
            self._paint_glyph(
                painter,
                active=self._glyph_is_active(),
            )
            painter.end()

    class _SplashDialog(QtWidgets.QDialog):
        def __init__(self) -> None:
            flags = (
                QtCore.Qt.WindowType.Dialog
                | QtCore.Qt.WindowType.WindowStaysOnTopHint
            )
            native_macos = sys.platform == "darwin"
            if native_macos:
                flags |= QtCore.Qt.WindowType.WindowTitleHint
                flags |= QtCore.Qt.WindowType.WindowCloseButtonHint
            else:
                flags |= QtCore.Qt.WindowType.FramelessWindowHint
            super().__init__(None, flags)
            if not native_macos:
                self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self._close_handler: Callable[[], None] | None = None
            self._programmatic_close = False
            self.setWindowTitle("Callimachus")
            self.setWindowIcon(icon)
            self.setAccessibleName("Callimachus startup")
            self.setObjectName("startupSplash")
            self.setProperty("darkTheme", dark)
            self.setFixedSize(390, 205)
            self.setStyleSheet(
                "QDialog#startupSplash { "
                f"background: transparent; color: {palette['ink']}; "
                "}"
                "QLabel { background: transparent; }"
                f"QLabel#startupStatus {{ color: {palette['muted']}; font-size: 13px; }}"
            )

            layout = QtWidgets.QGridLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

            self.close_button = None
            if not native_macos:
                header = QtWidgets.QWidget(self)
                header_layout = QtWidgets.QHBoxLayout(header)
                header_layout.setContentsMargins(18, 0, 0, 0)
                header_layout.setSpacing(8)
                header_layout.addStretch(1)

                close_button = _GlyphOnlyCloseButton(header)
                close_button.setObjectName("startupSplashClose")
                close_button.setAccessibleName("Close startup splash")
                close_button.setToolTip("Close")
                close_button.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
                close_button.setAutoDefault(False)
                close_button.setDefault(False)
                close_button.setFixedSize(30, 30)
                close_button.clicked.connect(self.close)
                header_layout.addWidget(close_button)
                self.close_button = close_button
                layout.addWidget(
                    header,
                    0,
                    0,
                    QtCore.Qt.AlignmentFlag.AlignTop,
                )

            logo_label = QtWidgets.QLabel(self)
            logo_label.setObjectName("startupLogo")
            logo_label.setAccessibleName("Callimachus logo")
            logo_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            logo_label.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                True,
            )
            logo_label.setPixmap(visible_logo)
            layout.addWidget(logo_label, 0, 0)

            status = QtWidgets.QLabel("Preparing Callimachus…", self)
            status.setObjectName("startupStatus")
            status.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            status.setContentsMargins(0, 0, 0, 14)
            layout.addWidget(
                status,
                0,
                0,
                QtCore.Qt.AlignmentFlag.AlignBottom,
            )

        def paintEvent(self, event: Any) -> None:
            painter = QtGui.QPainter(self)
            if sys.platform == "darwin":
                painter.fillRect(self.rect(), QtGui.QColor(palette["surface"]))
                painter.end()
                return
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setPen(QtGui.QPen(QtGui.QColor(palette["line"]), 1))
            painter.setBrush(QtGui.QColor(palette["surface"]))
            painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 8, 8)
            painter.end()

        def set_close_handler(self, handler: Callable[[], None]) -> None:
            self._close_handler = handler

        def close_programmatically(self) -> None:
            self._programmatic_close = True
            self.close()

        def closeEvent(self, event: Any) -> None:
            if not self._programmatic_close and self._close_handler is not None:
                self._close_handler()
            event.accept()

    return _SplashDialog()


def run_with_splash(*, prepare: Callable[[], object], start: Callable[[], int]) -> int:
    """Prepare desktop imports in the background, then hand off on the Qt thread."""
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    if getattr(app, "_callimachus_startup_session", None) is not None:
        raise RuntimeError("a Callimachus startup session is already active")

    quit_on_last_window_closed = bool(app.quitOnLastWindowClosed())
    splash = _create_splash(QtCore, QtGui, QtWidgets)
    session = _StartupSession(
        app=app,
        splash=splash,
        prepare=prepare,
        start=start,
        quit_on_last_window_closed=quit_on_last_window_closed,
        QtCore=QtCore,
    )
    app.setQuitOnLastWindowClosed(False)
    app._callimachus_startup_session = session

    event_loop_result = 0
    try:
        splash.show()
        splash.raise_()
        app.processEvents()
        if not session.cancelled:
            session.begin_prepare()
            event_loop_result = int(app.exec())
    finally:
        session.cleanup()

    if session.cancelled:
        return 130
    if session._exception is not None:
        exc, traceback = session._exception
        raise exc.with_traceback(traceback)
    if not session.ready and session.start_return_code is None:
        raise RuntimeError("startup event loop exited before desktop handoff")
    if not session.ready:
        return session.start_return_code or 0
    return event_loop_result
