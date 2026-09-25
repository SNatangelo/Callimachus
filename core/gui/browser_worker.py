# core/gui/browser_worker.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional Qt bridge for a single-threaded external Chrome session.

The module itself has no PySide6 dependency.  The caller creates the worker,
moves it to one QThread, and invokes its slots through queued Qt signals.
"""
from __future__ import annotations

from typing import Callable

from core.fetch.fallbacks.fetch_modes.interactive_browser import (
    AsyncInteractiveBrowserSession,
    InteractiveBrowserClosed,
)


def create_browser_worker(
    run_dir: str,
    *,
    dark_theme: bool = False,
    session_factory: Callable[[str], AsyncInteractiveBrowserSession] = AsyncInteractiveBrowserSession,
):
    """Return a QObject whose session methods run only in its owning Qt thread."""
    from PySide6 import QtCore

    class BrowserSessionWorker(QtCore.QObject):
        availability = QtCore.Signal(dict)
        opened = QtCore.Signal(dict)
        artifact = QtCore.Signal(dict)
        failed = QtCore.Signal(str)
        closed = QtCore.Signal()
        capture_requested = QtCore.Signal(str)
        download_detected = QtCore.Signal(dict)

        def __init__(self):
            super().__init__()
            self._session = None
            self._closing = False
            self._parked = False
            self._capture_timer = QtCore.QTimer(self)
            self._capture_timer.setInterval(250)
            self._capture_timer.timeout.connect(self._poll_capture_requests)

        @QtCore.Slot()
        def start(self):
            try:
                self._session = session_factory(run_dir)
                self._session.set_theme(dark_theme)
                status = self._session.start()
            except Exception as exc:
                self.failed.emit(str(exc))
                return
            self.availability.emit(status)
            if status.get("available"):
                self._parked = False
                self._capture_timer.start()

        @QtCore.Slot(bool)
        def set_theme(self, dark: bool):
            if self._session is None:
                return
            try:
                self._session.set_theme(dark)
            except Exception as exc:
                self.failed.emit(str(exc))

        @QtCore.Slot(str)
        def open(self, url: str):
            if self._session is None:
                self.failed.emit("interactive browser session is not available")
                return
            if self._parked:
                if not self._restart_after_browser_close():
                    return
                self._parked = False
            elif not self._session.available:
                self.failed.emit("interactive browser session is not available")
                return
            try:
                response = self._session.open(url)
            except InteractiveBrowserClosed:
                if not self._restart_after_browser_close():
                    return
                try:
                    response = self._session.open(url)
                except Exception as exc:
                    self.failed.emit(str(exc))
                    return
            except Exception as exc:
                self.failed.emit(str(exc))
                return
            self.opened.emit({"url": url, "response": response})

        def _restart_after_browser_close(self) -> bool:
            """Reopen the closed Chrome context without discarding its profile."""
            self._capture_timer.stop()
            try:
                status = self._session.restart()
            except Exception as exc:
                self.failed.emit(str(exc))
                return False
            if not status.get("available"):
                self.availability.emit(status)
                return False
            self._capture_timer.start()
            return True

        @QtCore.Slot(str, int)
        def capture_html(self, ref_id: str, index: int):
            self._capture("capture_current_html", ref_id, index)

        @QtCore.Slot(str, int)
        def capture_pdf(self, ref_id: str, index: int):
            self._capture("capture_current_pdf", ref_id, index)

        @QtCore.Slot(str, int, int)
        def capture_download(self, ref_id: str, index: int, token: int):
            self._capture("capture_download_pdf", ref_id, index, token)

        @QtCore.Slot()
        def park_visible_context(self):
            """Close visible Chrome after admission while retaining this session."""
            if self._session is None or not self._session.available:
                return
            self._capture_timer.stop()
            try:
                self._session.park()
                self._parked = True
            except Exception as exc:
                if self._session.available:
                    self._capture_timer.start()
                self.failed.emit(str(exc))

        @QtCore.Slot(int)
        def discard_download(self, token: int):
            if self._session is None or not self._session.available:
                return
            try:
                self._session.discard_download(token)
            except Exception as exc:
                self.failed.emit(str(exc))

        def _capture(self, method: str, ref_id: str, index: int, *args):
            if self._session is None or not self._session.available:
                self.failed.emit("interactive browser session is not available")
                return
            try:
                captured = getattr(self._session, method)(ref_id, index, *args)
            except Exception as exc:
                self.failed.emit(str(exc))
                return
            if captured is None:
                self.failed.emit("no usable browser artifact captured")
                return
            self.artifact.emit(captured)

        @QtCore.Slot()
        def _poll_capture_requests(self):
            if self._session is None or not self._session.available:
                return
            try:
                for kind in self._session.drain_capture_requests():
                    if kind in {"html", "pdf"}:
                        self.capture_requested.emit(kind)
                for notice in self._session.drain_download_notices():
                    self.download_detected.emit(notice)
            except Exception as exc:
                self.failed.emit(str(exc))

        @QtCore.Slot()
        def close(self):
            if self._closing:
                return
            self._closing = True
            self._parked = False
            self._capture_timer.stop()
            try:
                if self._session is not None:
                    self._session.close()
                    self._session = None
            except Exception as exc:
                self.failed.emit(str(exc))
            self.closed.emit()

    return BrowserSessionWorker()
