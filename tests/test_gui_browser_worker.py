# tests/test_gui_browser_worker.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from tests._bootstrap import *  # noqa: F401,F403

import sys
import threading
import types
import unittest
from unittest import mock


class _Signal:
    def __init__(self, *_args):
        self._name = None

    def __set_name__(self, _owner, name):
        self._name = name

    def __get__(self, instance, _owner):
        if instance is None:
            return self
        return instance.__dict__.setdefault(self._name, _BoundSignal())


class _BoundSignal:
    def __init__(self):
        self.values = []

    def emit(self, *value):
        self.values.append(value)

    def connect(self, _callback):
        pass


class _QObject:
    def __init__(self):
        pass


class _Timer:
    def __init__(self, *_args):
        self.timeout = _BoundSignal()
    def setInterval(self, _interval):
        pass
    def start(self):
        pass
    def stop(self):
        pass


class _Session:
    def __init__(self, _run_dir):
        self.available = False
        self.calls = []
        self.dark_theme = False

    def set_theme(self, dark):
        self.dark_theme = dark
        self._record("theme")

    def _record(self, name):
        self.calls.append((name, threading.get_ident()))

    def start(self):
        self._record("start")
        self.available = True
        return {"available": True, "reason": None}

    def open(self, url):
        self._record("open")
        return {"url": url}

    def capture_current_html(self, ref_id, index):
        self._record("html")
        return {"ref_id": ref_id, "index": index, "body": b"html"}

    def capture_current_pdf(self, ref_id, index):
        self._record("pdf")
        return {"ref_id": ref_id, "index": index, "body": b"%PDF"}

    def capture_download_pdf(self, ref_id, index, token):
        self._record("download")
        return {"ref_id": ref_id, "index": index, "token": token, "body": b"%PDF"}

    def discard_download(self, _token):
        self._record("discard")

    def close(self):
        self._record("close")
        self.available = False

    def drain_capture_requests(self):
        return ["html", "pdf"]

    def drain_download_notices(self):
        return []


class _FailingSession(_Session):
    def open(self, _url):
        self._record("open")
        raise RuntimeError("navigation failed")


class TestGuiBrowserWorker(unittest.TestCase):
    def test_optional_worker_keeps_session_calls_on_its_own_qt_thread(self):
        from core.gui.browser_worker import create_browser_worker

        qtcore = types.SimpleNamespace(
            QObject=_QObject,
            QTimer=_Timer,
            Signal=_Signal,
            Slot=lambda *_types: lambda function: function,
        )
        fake_pyside = types.SimpleNamespace(QtCore=qtcore)
        sessions = []

        def factory(run_dir):
            session = _Session(run_dir)
            sessions.append(session)
            return session

        with mock.patch.dict(sys.modules, {"PySide6": fake_pyside}):
            worker = create_browser_worker("run", session_factory=factory)
            worker.start()
            worker.open("https://publisher.test/article")
            worker.capture_html("r1", 0)
            worker.capture_pdf("r1", 1)
            worker._poll_capture_requests()
            worker.close()

        self.assertEqual(worker.availability.values, [({"available": True, "reason": None},)])
        self.assertEqual(worker.artifact.values[0][0]["body"], b"html")
        self.assertEqual(worker.artifact.values[1][0]["body"], b"%PDF")
        self.assertEqual(worker.capture_requested.values, [("html",), ("pdf",)])
        self.assertEqual({thread_id for _, thread_id in sessions[0].calls}, {threading.get_ident()})

    def test_worker_applies_initial_and_changed_theme_to_browser_session(self):
        from core.gui.browser_worker import create_browser_worker

        qtcore = types.SimpleNamespace(
            QObject=_QObject, QTimer=_Timer, Signal=_Signal,
            Slot=lambda *_types: lambda function: function,
        )
        fake_pyside = types.SimpleNamespace(QtCore=qtcore)
        with mock.patch.dict(sys.modules, {"PySide6": fake_pyside}):
            worker = create_browser_worker("run", dark_theme=True, session_factory=_Session)
            worker.start()
            self.assertTrue(worker._session.dark_theme)
            worker.set_theme(False)
            self.assertFalse(worker._session.dark_theme)
            self.assertEqual([name for name, _ in worker._session.calls[:2]], ["theme", "start"])

    def test_download_notice_can_be_captured_or_discarded(self):
        from core.gui.browser_worker import create_browser_worker

        class DownloadSession(_Session):
            def drain_download_notices(self):
                self._record("notices")
                return [{"token": 7, "suggested_filename": "paper.pdf", "url": None}]

        qtcore = types.SimpleNamespace(
            QObject=_QObject, QTimer=_Timer, Signal=_Signal,
            Slot=lambda *_types: lambda function: function,
        )
        fake_pyside = types.SimpleNamespace(QtCore=qtcore)
        with mock.patch.dict(sys.modules, {"PySide6": fake_pyside}):
            worker = create_browser_worker("run", session_factory=DownloadSession)
            worker.start()
            worker._poll_capture_requests()
            worker.capture_download("r1", 0, 7)
            worker.discard_download(7)

        self.assertEqual(
            worker.download_detected.values,
            [({"token": 7, "suggested_filename": "paper.pdf", "url": None},)],
        )
        self.assertEqual(worker.artifact.values[0][0]["token"], 7)

    def test_navigation_failure_emits_failed_without_opened_and_close_is_idempotent(self):
        from core.gui.browser_worker import create_browser_worker

        qtcore = types.SimpleNamespace(
            QObject=_QObject, QTimer=_Timer, Signal=_Signal,
            Slot=lambda *_types: lambda function: function,
        )
        fake_pyside = types.SimpleNamespace(QtCore=qtcore)
        with mock.patch.dict(sys.modules, {"PySide6": fake_pyside}):
            worker = create_browser_worker("run", session_factory=_FailingSession)
            worker.start()
            worker.open("https://publisher.test/article")
            worker.close()
            worker.close()

        self.assertEqual(worker.opened.values, [])
        self.assertEqual(worker.failed.values, [("navigation failed",)])
        self.assertEqual(worker.closed.values, [()])

    def test_user_closed_browser_restarts_its_existing_session_and_opens_once(self):
        from core.fetch.fallbacks.fetch_modes.interactive_browser import InteractiveBrowserClosed
        from core.gui.browser_worker import create_browser_worker

        class ClosedSession(_Session):
            def __init__(self, run_dir):
                super().__init__(run_dir)
                self.profile_dir = "run/profile"
                self._closed_once = False

            def open(self, url):
                self._record("open")
                if not self._closed_once:
                    self._closed_once = True
                    raise InteractiveBrowserClosed("interactive browser was closed by the user")
                return {"url": url, "profile_dir": self.profile_dir}

            def restart(self):
                self._record("restart")
                self.available = True
                return {"available": True, "reason": None}

        qtcore = types.SimpleNamespace(
            QObject=_QObject, QTimer=_Timer, Signal=_Signal,
            Slot=lambda *_types: lambda function: function,
        )
        fake_pyside = types.SimpleNamespace(QtCore=qtcore)
        sessions = []

        def factory(run_dir):
            session = ClosedSession(run_dir)
            sessions.append(session)
            return session

        with mock.patch.dict(sys.modules, {"PySide6": fake_pyside}):
            worker = create_browser_worker("run", session_factory=factory)
            worker.start()
            worker.open("https://publisher.test/article")
            worker.close()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(
            [call[0] for call in sessions[0].calls],
            ["theme", "start", "open", "restart", "open", "close"],
        )
        self.assertEqual(worker.availability.values, [({"available": True, "reason": None},)])
        self.assertEqual(worker.failed.values, [])
        self.assertEqual(worker.opened.values, [(
            {"url": "https://publisher.test/article", "response": {
                "url": "https://publisher.test/article", "profile_dir": "run/profile",
            }},
        )])

    def test_accepted_capture_parks_visible_browser_and_reopens_same_session(self):
        from core.gui.browser_worker import create_browser_worker

        class ParkSession(_Session):
            def __init__(self, run_dir):
                super().__init__(run_dir)
                self.profile_dir = "run/profile"

            def park(self):
                self._record("park")
                self.available = False

            def restart(self):
                self._record("restart")
                self.available = True
                return {"available": True, "reason": None}

        qtcore = types.SimpleNamespace(
            QObject=_QObject, QTimer=_Timer, Signal=_Signal,
            Slot=lambda *_types: lambda function: function,
        )
        sessions = []
        with mock.patch.dict(sys.modules, {"PySide6": types.SimpleNamespace(QtCore=qtcore)}):
            worker = create_browser_worker(
                "run", session_factory=lambda run_dir: sessions.append(ParkSession(run_dir)) or sessions[-1]
            )
            worker.start()
            worker.open("https://publisher.test/first")
            worker.park_visible_context()
            worker.open("https://publisher.test/second")
            worker.close()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(
            [name for name, _thread in sessions[0].calls],
            ["theme", "start", "open", "park", "restart", "open", "close"],
        )
        self.assertEqual(sessions[0].profile_dir, "run/profile")
        self.assertEqual(worker.failed.values, [])

    def test_unavailable_initial_browser_does_not_restart_on_open(self):
        from core.gui.browser_worker import create_browser_worker

        class UnavailableSession(_Session):
            def start(self):
                self._record("start")
                return {"available": False, "reason": "Chrome unavailable"}

            def restart(self):
                self._record("restart")
                raise AssertionError("initial failure must not restart")

        qtcore = types.SimpleNamespace(
            QObject=_QObject, QTimer=_Timer, Signal=_Signal,
            Slot=lambda *_types: lambda function: function,
        )
        sessions = []
        with mock.patch.dict(sys.modules, {"PySide6": types.SimpleNamespace(QtCore=qtcore)}):
            worker = create_browser_worker(
                "run", session_factory=lambda run_dir: sessions.append(UnavailableSession(run_dir)) or sessions[-1]
            )
            worker.start()
            worker.open("https://publisher.test/article")

        self.assertEqual([name for name, _thread in sessions[0].calls], ["theme", "start"])
        self.assertEqual(worker.failed.values, [("interactive browser session is not available",)])


if __name__ == "__main__":
    unittest.main()
