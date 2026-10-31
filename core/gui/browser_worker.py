# core/gui/browser_worker.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional Qt bridge for a single-threaded external Chrome session.

The module itself has no PySide6 dependency.  The caller creates the worker,
moves it to one QThread, and invokes its slots through queued Qt signals.
"""
from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping
from typing import Callable

from core.fetch.fallbacks.fetch_modes.interactive_browser import (
    AsyncInteractiveBrowserSession,
    CaptureToolbarStatus,
    InteractiveBrowserClosed,
    InteractiveCaptureCancelled,
)


def create_browser_worker(
    run_dir: str,
    *,
    dark_theme: bool = False,
    session_factory: Callable[..., AsyncInteractiveBrowserSession] = AsyncInteractiveBrowserSession,
):
    """Return a QObject whose session methods run only in its owning Qt thread."""
    from PySide6 import QtCore

    def safe_cache_result(value, *, fallback_error="browser_cache_operation_failed"):
        """Keep worker results structured and prevent exception text leaking paths."""
        if not isinstance(value, Mapping):
            return {
                "ok": False, "cleared": 0, "deferred": 0,
                "errors": [fallback_error],
            }
        cleared = value.get("cleared", 0)
        deferred = value.get("deferred", 0)
        if type(cleared) is not int or cleared < 0 or type(deferred) is not int or deferred < 0:
            return {
                "ok": False, "cleared": 0, "deferred": 0,
                "errors": [fallback_error],
            }
        raw_errors = value.get("errors", [])
        if not isinstance(raw_errors, (list, tuple)):
            raw_errors = [fallback_error]
        errors = []
        for error in raw_errors:
            if isinstance(error, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error):
                errors.append(error)
            else:
                errors.append(fallback_error)
        return {
            "ok": value.get("ok") is True and not deferred and not errors,
            "cleared": cleared,
            "deferred": deferred,
            "errors": errors,
        }

    class BrowserSessionWorker(QtCore.QObject):
        availability = QtCore.Signal(dict)
        opened = QtCore.Signal(dict)
        artifact = QtCore.Signal(dict)
        failed = QtCore.Signal(str)
        capture_failed = QtCore.Signal(str, int, str)
        capture_cancelled = QtCore.Signal(str, int)
        closed = QtCore.Signal()
        capture_requested = QtCore.Signal(str)
        download_detected = QtCore.Signal(dict)
        cache_cleared = QtCore.Signal(dict)
        toolbar_status_changed = QtCore.Signal(object)

        def __init__(self):
            super().__init__()
            self._session = None
            self._toolbar_status = CaptureToolbarStatus.READY
            self._closing = False
            self._parked = False
            self._capture_lock = threading.Lock()
            self._active_capture_key = None
            self._cancelled_capture_keys = set()
            self._diagnostic_no_toolbar = os.environ.get("CALLIMACHUS_BROWSER_DIAGNOSTIC_NO_TOOLBAR") == "1"
            self._capture_timer = QtCore.QTimer(self)
            self._capture_timer.setInterval(100)
            self._capture_timer.timeout.connect(self._poll_capture_requests)

        @QtCore.Slot()
        def start(self):
            if self._closing:
                return
            if self._session is not None:
                self._capture_timer.stop()
            previous, self._session = self._session, None
            try:
                if previous is not None:
                    previous.close()
                if self._diagnostic_no_toolbar:
                    self._session = session_factory(run_dir, capture_toolbar=False)
                else:
                    self._session = session_factory(run_dir)
                self._session.set_theme(dark_theme)
                status = self._session.start()
                self._publish_toolbar_status()
            except Exception as exc:
                self._dispose_failed_session()
                self.failed.emit(str(exc))
                return
            if not status.get("available"):
                self._dispose_failed_session()
            self.availability.emit(status)
            if status.get("available"):
                self._parked = False
                self._start_capture_timer()

        def _dispose_failed_session(self):
            session, self._session = self._session, None
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

        def _start_capture_timer(self):
            if not self._diagnostic_no_toolbar:
                self._capture_timer.start()

        def _publish_toolbar_status(self):
            status = (
                self._session.toolbar_status
                if self._session is not None
                else CaptureToolbarStatus.READY
            )
            if status is not self._toolbar_status:
                self._toolbar_status = status
                self.toolbar_status_changed.emit(status)

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
            if self._closing:
                return
            if self._session is None or (not self._parked and not self._session.available):
                self.start()
                if self._session is None or not self._session.available:
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
            except Exception:
                # A dead Playwright driver needs a fresh transport, not just a tab.
                self.start()
                return self._session is not None and self._session.available
            if not status.get("available"):
                self.availability.emit(status)
                return False
            self._publish_toolbar_status()
            self._start_capture_timer()
            return True

        @QtCore.Slot(str, int)
        def capture_html(self, ref_id: str, index: int):
            self._capture("capture_current_html", ref_id, index)

        @QtCore.Slot(str, int)
        def capture_pdf(self, ref_id: str, index: int):
            self._capture("capture_current_pdf", ref_id, index)

        @QtCore.Slot(str, int)
        def capture_text(self, ref_id: str, index: int):
            self._capture("capture_current_text", ref_id, index)

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
                self._publish_toolbar_status()
                self._parked = True
            except Exception as exc:
                if self._session.available:
                    self._start_capture_timer()
                self.failed.emit(str(exc))

        @QtCore.Slot(int)
        def discard_download(self, token: int):
            if self._session is None or not self._session.available:
                return
            try:
                self._session.discard_download(token)
            except Exception as exc:
                self.failed.emit(str(exc))

        @QtCore.Slot()
        def clear_cache(self):
            """Clear the active profile's network cache and emit a safe result."""
            if self._session is None:
                self.cache_cleared.emit(safe_cache_result(
                    {}, fallback_error="browser_session_unavailable",
                ))
                return
            try:
                result = self._session.clear_browser_cache()
            except Exception:
                result = {
                    "ok": False, "cleared": 0, "deferred": 0,
                    "errors": ["browser_cache_operation_failed"],
                }
            self.cache_cleared.emit(safe_cache_result(result))

        def _capture(self, method: str, ref_id: str, index: int, *args):
            if self._session is None or not self._session.available:
                self.failed.emit("interactive browser session is not available")
                return
            key = (str(ref_id), int(index))
            with self._capture_lock:
                self._active_capture_key = key
                cancelled_before_start = key in self._cancelled_capture_keys
            if cancelled_before_start:
                self.capture_cancelled.emit(ref_id, index)
                with self._capture_lock:
                    self._cancelled_capture_keys.discard(key)
                    self._active_capture_key = None
                return
            try:
                try:
                    captured = getattr(self._session, method)(ref_id, index, *args)
                except InteractiveCaptureCancelled:
                    self.capture_cancelled.emit(ref_id, index)
                    return
                except Exception as exc:
                    with self._capture_lock:
                        cancelled = key in self._cancelled_capture_keys
                    if cancelled:
                        self.capture_cancelled.emit(ref_id, index)
                    else:
                        self.capture_failed.emit(ref_id, index, str(exc))
                    return
                with self._capture_lock:
                    cancelled = key in self._cancelled_capture_keys
                if cancelled:
                    self.capture_cancelled.emit(ref_id, index)
                elif captured is None:
                    self.capture_failed.emit(
                        ref_id, index, "no usable browser artifact captured"
                    )
                else:
                    self.artifact.emit(captured)
            finally:
                with self._capture_lock:
                    self._cancelled_capture_keys.discard(key)
                    if self._active_capture_key == key:
                        self._active_capture_key = None

        def request_cancel_capture(self, ref_id: str, index: int):
            """Thread-safe cancellation entry point callable directly by the GUI."""
            key = (str(ref_id), int(index))
            with self._capture_lock:
                self._cancelled_capture_keys.add(key)
                active = self._active_capture_key == key
                session = self._session if active else None
            if session is not None:
                session.cancel_capture(ref_id, index)

        @QtCore.Slot()
        def _poll_capture_requests(self):
            if self._diagnostic_no_toolbar or self._session is None or not self._session.available:
                return
            try:
                requests = self._session.drain_capture_requests()
                self._publish_toolbar_status()
                for kind in requests:
                    if kind in {"html", "pdf", "text"}:
                        self.capture_requested.emit(kind)
                for notice in self._session.drain_download_notices():
                    self.download_detected.emit(notice)
            except InteractiveBrowserClosed:
                self._parked = True
                self._capture_timer.stop()
            except Exception as exc:
                self._capture_timer.stop()
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
                    self._publish_toolbar_status()
                    self._session = None
            except Exception as exc:
                self.failed.emit(str(exc))
            self.closed.emit()

    return BrowserSessionWorker()
