from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.gui.desktop import _source_summary_html, _stylesheet, load_translations


def _process_events_until(app, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_desktop_translations_are_external_and_english_by_default():
    assert load_translations()["tab_analysis"] == "Analysis"
    assert load_translations("en")["tab_history"] == "History"
    assert load_translations("it")["tab_analysis"] == "Analisi"
    assert load_translations("it")["tab_history"] == "Cronologia"


def test_desktop_window_shell_and_paused_fetch_lifecycle(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    opened = []
    proceeded = []
    controller = SimpleNamespace(
        stage_source=lambda *_args: None,
        discard_source=lambda *_args: None,
        submit_identity_decision=lambda *_args: None,
        identity_review_allowed=True,
        proceeded=False,
        proceed=lambda: proceeded.append(True),
    )

    class Window:
        def __init__(self):
            self.destroyed = SimpleNamespace(connect=lambda _callback: None)
        def setAttribute(self, *_args):
            pass
        def show(self):
            opened.append("show")
        def raise_(self):
            pass
        def activateWindow(self):
            pass

    snapshot = {
        "phase": "fetch", "fetch_paused": True, "llm_available": False,
        "references": [
            {"ref_id": "r1", "title": "A source", "phase": "resolve", "status": "done", "tier": "fulltext", "result": "—"},
            {"ref_id": "r2", "title": "Abstract source", "phase": "fetch", "status": "running", "tier": "abstract", "result": "—"},
        ],
    }

    def factory(*_args, **kwargs):
        opened.append(kwargs)
        return Window()

    window = create_desktop_window(
        language="it",
        snapshot_loader=lambda _run: snapshot,
        resume_command_builder=lambda run: ["python", "run.py", "--run", run, "--resume"],
        history_loader=lambda: [{"paper": "old.pdf", "run_dir": "old"}],
        cache_loader=lambda: [{"title": "Cached", "tier": "fulltext"}],
        settings_loader=lambda: {"OPENAI_API_KEY": "secret", "MODE": "standard"},
        docs_loader=lambda: "Local docs",
        guided_controller_factory=lambda _run: controller,
        guided_window_factory=factory,
    )
    assert [
        window.navigation.item(index).text()
        for index in range(window.navigation.count())
    ] == ["Analisi", "Cronologia", "Cache", "Impostazioni", "Console", "Info"]
    assert window.pages.count() == 6
    assert set(window.mode_buttons) == {"maximum", "abstract", "standard", "standard_web"}
    assert window.source_filter.count() == 4
    assert window.cache_filter.count() == 4
    assert window.source_table.columnCount() == 9
    assert all(not button.isEnabled() for button in window.jury_buttons)
    window.set_run_dir(str(tmp_path))
    assert window.source_table.rowCount() == 2
    window.source_search.setText("abstract")
    assert window.source_table.rowCount() == 1
    window.theme_toggle.click()
    assert window.theme_toggle.text() == "Tema chiaro"
    resumed = []
    window._start_process = lambda command, **_kwargs: resumed.append(command)
    window._on_process_finished(10, 0)
    launch = next(item for item in opened if isinstance(item, dict))
    assert launch["identity_review_allowed"] is True
    assert "Fetch assistito" in window.phase_label.text()
    assert proceeded == []
    assert resumed == []
    launch["proceed"]()
    assert proceeded == [True]
    assert resumed[-1][-1] == "--resume"
    window.close()
    assert app is not None


def test_history_resume_is_disabled_and_guarded_for_non_resumable_runs(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    resumed = []
    window = create_desktop_window(
        history_loader=lambda: [
            {"paper": "missing", "run_dir": "missing", "available": False},
            {"paper": "active", "run_dir": "active", "available": True, "run_status": "active"},
            {"paper": "done", "run_dir": "done", "available": True, "run_status": "completed", "completed": True},
            {"paper": "paused", "run_dir": "paused", "available": True, "run_status": "paused"},
        ],
        resume_command_builder=lambda run: ["resume", run],
        resume_callback=lambda row: resumed.append(row["run_dir"]),
    )
    started = []
    window._start_process = lambda command, **_kwargs: started.append(command)

    for index in range(3):
        window.history_table.selectRow(index)
        app.processEvents()
        assert not window.history_resume_button.isEnabled()
        window._resume_selected_history()
    assert resumed == []
    assert started == []

    window.history_table.selectRow(3)
    app.processEvents()
    assert window.history_resume_button.isEnabled()
    window._resume_selected_history()
    assert resumed == ["paused"]
    assert started == [["resume", "paused"]]
    window.close()


def test_history_resume_rechecks_current_durable_row_before_starting(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
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
    window.history_table.selectRow(0)
    app.processEvents()
    history_rows[:] = [{"paper": "paused", "run_dir": "paused", "available": True, "run_status": "completed", "completed": True}]

    window._resume_selected_history()

    assert resumed == []
    assert started == []
    window.close()


@pytest.mark.parametrize("snapshot_error", [OSError, sqlite3.DatabaseError])
def test_exit_code_ten_with_unreadable_snapshot_does_not_open_guided_fetch(
    monkeypatch, tmp_path, snapshot_error
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    opened = []

    def snapshot_loader(_run):
        raise snapshot_error("corrupt run")

    window = create_desktop_window(
        snapshot_loader=snapshot_loader,
        guided_window_factory=lambda *_args, **_kwargs: opened.append(True),
    )
    window.set_run_dir(str(tmp_path))
    window._on_process_finished(10, 0)

    assert opened == []
    assert window.phase_label.text() == load_translations()["snapshot_unavailable"]
    assert window.start_button.isEnabled()
    window.close()
    assert app is not None


def test_nonzero_exit_reports_code_after_refresh(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(snapshot_loader=lambda _run: {"phase": "verify"})
    window.set_run_dir(str(tmp_path))
    window._on_process_finished(7, 0)
    assert window.phase_label.text() == load_translations()["process_failed"].format(exit_code=7)
    assert window.start_button.isEnabled()
    window.close()
    assert app is not None


def test_snapshot_refresh_handles_runtime_error(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])

    def unavailable_snapshot(_run):
        raise RuntimeError("run.sqlite is not ready")

    window = create_desktop_window(snapshot_loader=unavailable_snapshot)
    window.phase_label.setText("existing state")
    window.set_run_dir(str(tmp_path))
    assert window._refresh_snapshot() is None
    assert window.phase_label.text() == "existing state"
    window.close()
    assert app is not None
def test_history_completed_run_forks_verify_with_selected_backends(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
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

    def build(row, selectors):
        built.append((dict(row), list(selectors)))
        return {
            "command": ["callimachus", "--fork-completed-verify", "parent"],
            "run_dir": "child",
            "environment": {
                "CITATION_VERIFIER_VERIFY_BACKENDS": ",".join(
                    selector.split(":", 1)[0] for selector in selectors
                ),
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
        verify_fork_selector=lambda _options: [
            "openai_compatible:deepseek-flash"
        ],
        verify_fork_command_builder=build,
    )
    started = []
    window._start_process = lambda command, **kwargs: started.append(
        (command, kwargs)
    )
    window.history_table.selectRow(0)
    app.processEvents()

    assert window.history_verify_fork_button.isEnabled()
    window._fork_selected_history_verify()
    assert built == [(
        rows[0], ["openai_compatible:deepseek-flash"]
    )]
    assert started == [(
        ["callimachus", "--fork-completed-verify", "parent"],
        {"environment": {
            "CITATION_VERIFIER_VERIFY_BACKENDS": "openai_compatible",
        }},
    )]
    assert window._run_dir == "child"

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


def test_windows_html_report_uses_edge_when_file_association_is_missing(monkeypatch, tmp_path):
    from core.gui import desktop

    report = tmp_path / "report.preview.html"
    report.write_text("<html>Preview</html>", encoding="utf-8")
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"browser")
    opened = []

    def missing_association(_path):
        raise OSError("No HTML association")

    monkeypatch.setattr(desktop.os, "startfile", missing_association, raising=False)
    monkeypatch.setattr(desktop, "_edge_candidates", lambda: (edge,))
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda argv: opened.append(argv))

    assert desktop._open_local_path(str(report), None, None, platform_name="win32")
    assert opened == [[str(edge), report.resolve().as_uri()]]
    assert not desktop._open_local_path(
        str(tmp_path / "missing.html"), None, None, platform_name="win32"
    )

@pytest.mark.parametrize("prefix", ["py run.py", "python run.py", "python3 run.py"])
def test_commands_parser_matches_optional_prefix_and_preserves_quoted_paths(prefix):
    from core.gui.desktop import _parse_commands_arguments

    suffix = r'parse --input "C:\Users\Researcher\Paper Files\paper.pdf"'
    expected = ["parse", "--input", r"C:\Users\Researcher\Paper Files\paper.pdf"]
    plain, plain_error = _parse_commands_arguments(
        suffix, displayed_prefix="py run.py"
    )
    prefixed, prefixed_error = _parse_commands_arguments(
        f"{prefix} {suffix}", displayed_prefix="py run.py"
    )

    assert plain_error is None
    assert prefixed_error is None
    assert plain == prefixed == expected

def test_commands_parser_strips_only_one_prefix_at_the_start():
    from core.gui.desktop import _parse_commands_arguments

    duplicate, error = _parse_commands_arguments(
        "py run.py python3 run.py app", displayed_prefix="py run.py"
    )
    later, later_error = _parse_commands_arguments(
        "inspect py run.py app", displayed_prefix="py run.py"
    )

    assert error is None
    assert duplicate == ["python3", "run.py", "app"]
    assert later_error is None
    assert later == ["inspect", "py", "run.py", "app"]

def test_commands_parser_accepts_the_displayed_frozen_executable_prefix(monkeypatch):
    import core.gui.desktop as desktop

    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(
        desktop.sys, "executable", str(Path.cwd() / "Callimachus Desktop.exe")
    )
    displayed = desktop._commands_displayed_prefix()
    arguments, error = desktop._parse_commands_arguments(
        '"Callimachus Desktop.exe" --version', displayed_prefix=displayed
    )

    assert displayed == "Callimachus Desktop.exe"
    assert error is None
    assert arguments == ["--version"]

@pytest.mark.parametrize(
    ("source", "expected_path"),
    [
        (r"parse --input C:\papers\paper.pdf", r"C:\papers\paper.pdf"),
        (
            r"parse --input 'C:\papers with spaces\paper.pdf'",
            r"C:\papers with spaces\paper.pdf",
        ),
    ],
)
def test_windows_commands_parser_preserves_unquoted_and_single_quoted_paths(
    monkeypatch, source, expected_path
):
    import core.gui.desktop as desktop

    monkeypatch.setattr(desktop.sys, "platform", "win32")
    arguments, error = desktop._parse_commands_arguments(
        source, displayed_prefix="py run.py"
    )

    assert error is None
    assert arguments == ["parse", "--input", expected_path]

def test_windows_commands_parser_rejects_an_unclosed_quote(monkeypatch):
    import core.gui.desktop as desktop

    monkeypatch.setattr(desktop.sys, "platform", "win32")
    arguments, error = desktop._parse_commands_arguments(
        r'parse --input "C:\papers\paper.pdf', displayed_prefix="py run.py"
    )

    assert arguments is None
    assert error == "commands_status_invalid_arguments"

@pytest.mark.parametrize(
    ("platform_name", "expected"),
    [("win32", "py run.py"), ("linux", "python3 run.py")],
)
def test_commands_source_prefix_matches_platform(monkeypatch, platform_name, expected):
    import core.gui.desktop as desktop

    monkeypatch.setattr(desktop, "is_frozen", lambda: False)
    monkeypatch.setattr(desktop.sys, "platform", platform_name)

    assert desktop._commands_displayed_prefix() == expected

@pytest.mark.parametrize(
    ("arguments", "status_key"),
    [
        ("", "commands_status_invalid_empty"),
        ("   ", "commands_status_invalid_empty"),
        ("app", "commands_status_invalid_app"),
        ("py run.py app", "commands_status_invalid_app"),
        ("python3 run.py app --help", "commands_status_invalid_app"),
    ],
)
def test_commands_rejects_empty_and_nested_gui_arguments_before_launch(
    monkeypatch, arguments, status_key
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window, load_translations
    import core.gui.desktop as desktop

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window()
    monkeypatch.setattr(
        desktop,
        "application_argv",
        lambda *_args, **_kwargs: pytest.fail("invalid input reached process launch"),
    )
    window.commands_input.setText(arguments)
    window._run_commands()

    assert window._commands_process is None
    assert window.commands_status_label.text() == load_translations()[status_key]
    window.close()
    assert app is not None

@pytest.mark.parametrize(
    ("active_kind", "status_key"),
    [
        ("analysis", "commands_status_busy_analysis"),
        ("guided", "commands_status_busy_guided"),
    ],
)
def test_commands_cannot_start_during_analysis_or_guided_fetch(
    monkeypatch, active_kind, status_key
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window, load_translations

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window()
    active = object()
    if active_kind == "analysis":
        window._process = active
    else:
        window._guided_window = active
    window._update_run_controls()
    window.commands_input.setText("--help")
    window._run_commands()

    assert window._commands_process is None
    assert window.commands_status_label.text() == load_translations()[status_key]
    assert not window.commands_run_button.isEnabled()
    window._process = None
    window._guided_window = None
    window.close()
    assert app is not None

@pytest.mark.parametrize("frozen", [False, True])
def test_commands_process_uses_argv_displays_both_streams_and_closes_stdin(
    monkeypatch, frozen
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop
    import os
    import sys

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    root = Path.cwd()
    data_root = root.parent
    expected_working_directory = data_root if frozen else root
    calls = []
    child_script = (
        "import os, sys; sys.stdin.read(); "
        "print('stdout from stub: 東京 café'); "
        "print('cwd:' + os.getcwd()); "
        "print('stderr from stub: 東京 café', file=sys.stderr); "
        "print('encoding:' + os.environ.get('PYTHONIOENCODING', '')); sys.exit(7)"
    )

    def child_argv(*args, source_root):
        calls.append((args, source_root))
        return [sys.executable, "-c", child_script]

    monkeypatch.setattr(desktop, "is_frozen", lambda: frozen)
    monkeypatch.setattr(desktop, "resource_root", lambda: root)
    monkeypatch.setattr(desktop, "user_data_root", lambda: data_root)
    monkeypatch.setattr(desktop, "application_argv", child_argv)

    window = desktop.create_desktop_window()
    window.commands_input.setText(
        r'inspect --paper "C:\Paper Files\paper.pdf"'
    )
    window.commands_input.returnPressed.emit()

    assert calls == [
        (("inspect", "--paper", r"C:\Paper Files\paper.pdf"), root)
    ]
    assert window.commands_status_label.text() == desktop.load_translations()["commands_status_running"]
    assert _process_events_until(app, lambda: window._commands_process is None)
    log_path = window._console_log_path
    assert log_path is not None and log_path.parent.name == "logs"
    assert window.commands_output.toPlainText() == ""
    window.navigation.setCurrentRow(4)
    app.processEvents()
    assert window._console_loaded
    output = window.commands_output.toPlainText()
    assert "stdout from stub: 東京 café" in output
    assert "[stderr] stderr from stub: 東京 café" in output
    assert "encoding:utf-8:replace" in output
    assert os.environ["PYTHONIOENCODING"] == "cp1252"
    assert f"cwd:{expected_working_directory}".casefold() in output.casefold()
    assert window.commands_status_label.text() == "Command exited with code 7."
    assert window.commands_status_label.property("error") is True
    assert window._commands_process is None
    window.navigation.setCurrentRow(0)
    assert window.commands_output.toPlainText() == ""
    window.navigation.setCurrentRow(4)
    assert window.commands_output.toPlainText() == output
    window.close()
    assert not log_path.exists()
    assert app is not None

def test_commands_process_decodes_utf8_split_between_reads(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop

    class Signal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in tuple(self.callbacks):
                callback(*args)

    class StubProcess:
        ExitStatus = QtCore.QProcess.ExitStatus
        ProcessError = QtCore.QProcess.ProcessError

        def __init__(self, _parent):
            self.readyReadStandardOutput = Signal()
            self.readyReadStandardError = Signal()
            self.finished = Signal()
            self.errorOccurred = Signal()
            self.stdout = b""
            self.stderr = b""

        def setWorkingDirectory(self, _value):
            pass

        def setProcessEnvironment(self, _value):
            pass

        def start(self, *_args):
            pass

        def closeWriteChannel(self):
            pass

        def readAllStandardOutput(self):
            value, self.stdout = self.stdout, b""
            return value

        def readAllStandardError(self):
            value, self.stderr = self.stderr, b""
            return value

        def deleteLater(self):
            pass

    app = QApplication.instance() or QApplication([])
    root = Path.cwd()
    monkeypatch.setattr(desktop, "is_frozen", lambda: False)
    monkeypatch.setattr(desktop, "resource_root", lambda: root)
    monkeypatch.setattr(
        desktop, "application_argv", lambda *args, source_root: ["python", *args]
    )
    monkeypatch.setattr(QtCore, "QProcess", StubProcess)

    window = desktop.create_desktop_window()
    window.navigation.setCurrentRow(4)
    window.commands_input.setText("--help")
    window._run_commands()
    process = window._commands_process
    encoded = "東京".encode("utf-8")
    process.stdout = encoded[:2]
    process.readyReadStandardOutput.emit()
    process.stdout = encoded[2:]
    process.readyReadStandardOutput.emit()
    process.stderr = "é".encode("utf-8")[:1]
    process.readyReadStandardError.emit()
    process.stderr = "é".encode("utf-8")[1:]
    process.readyReadStandardError.emit()
    process.finished.emit(0, StubProcess.ExitStatus.NormalExit)

    output = window.commands_output.toPlainText()
    assert "東京" in output
    assert "[stderr] é" in output
    assert "�" not in output
    window.close()
    assert app is not None

def test_analysis_output_is_captured_lazily_and_follows_console_live(
    monkeypatch, tmp_path
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop
    import sys

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    child_script = (
        "import sys,time; "
        "sys.stdout.buffer.write(b'analysis before: '); sys.stdout.flush(); "
        "time.sleep(0.15); "
        "word='東京'.encode('utf-8'); "
        "sys.stdout.buffer.write(word[:2]); sys.stdout.flush(); time.sleep(0.05); "
        "sys.stdout.buffer.write(word[2:] + b'\\n'); sys.stdout.flush(); "
        "sys.stderr.write('analysis stderr: diagnostic\\n'); sys.stderr.flush()"
    )
    window = desktop.create_desktop_window()
    snapshot_reads = []
    original_snapshot = window._read_console_snapshot

    def record_snapshot_read(*args):
        snapshot_reads.append(args)
        return original_snapshot(*args)

    monkeypatch.setattr(window, "_read_console_snapshot", record_snapshot_read)
    window._start_process([sys.executable, "-c", child_script], auto_open_report=False)
    log_path = window._console_log_path
    assert log_path is not None
    assert _process_events_until(app, lambda: log_path.stat().st_size > 0)
    assert window.commands_output.toPlainText() == ""
    assert snapshot_reads == []

    window.navigation.setCurrentRow(4)
    assert len(snapshot_reads) == 1
    assert "analysis before:" in window.commands_output.toPlainText()
    assert _process_events_until(app, lambda: window._process is None)
    output = window.commands_output.toPlainText()
    assert "東京" in output
    assert "analysis stderr: diagnostic" in output
    assert "�" not in output

    window.navigation.setCurrentRow(0)
    assert window.commands_output.toPlainText() == ""
    window.navigation.setCurrentRow(4)
    assert window.commands_output.toPlainText() == output
    window.close()
    assert not log_path.exists()
    assert app is not None

def test_console_tail_is_bounded_and_older_page_prepends_with_spinner_and_anchor(
    monkeypatch, tmp_path
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_CONSOLE_PAGE_BYTES", 4096)
    monkeypatch.setattr(desktop, "_CONSOLE_LIVE_WINDOW_BYTES", 4096)
    window = desktop.create_desktop_window(
        background_loading=True, latest_loader=lambda: None
    )
    window.show()
    window._begin_console_capture()
    content = "".join(
        f"line-{index:05d} café 東京 🍕 " + "x" * 70 + "\n"
        for index in range(180)
    )
    window._append_console_output(content)
    log_path = window._console_log_path
    assert log_path is not None

    page_started = threading.Event()
    release_page = threading.Event()
    read_page = window._read_console_history_page

    def delayed_page(*args):
        payload = read_page(*args)
        page_started.set()
        assert release_page.wait(3)
        return payload

    monkeypatch.setattr(window, "_read_console_history_page", delayed_page)
    window.navigation.setCurrentRow(4)
    assert _process_events_until(app, lambda: window._console_loaded)
    tail = window.commands_output.toPlainText()
    assert len(tail.encode("utf-8")) <= 4096
    raw = content.encode("utf-8")
    tail_start = len(raw) - 4096
    newline = raw.find(b"\n", tail_start)
    if newline >= 0:
        tail_start = newline + 1
    else:
        while tail_start < len(raw) and raw[tail_start] & 0xC0 == 0x80:
            tail_start += 1
    assert tail == raw[tail_start:].decode("utf-8")
    assert "�" not in tail
    scrollbar = window.commands_output.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(scrollbar.minimum())
    assert _process_events_until(app, page_started.is_set)
    assert window.console_history_spinner.isVisibleTo(window)
    spinner_frame = window.console_history_spinner.text()
    window._advance_spinner()
    assert window.console_history_spinner.text() != spinner_frame
    assert window.commands_output.toPlainText() == tail
    anchor = window.commands_output.cursorForPosition(QtCore.QPoint(2, 2))
    anchor_block = anchor.block().text()
    anchor_in_block = anchor.positionInBlock()
    release_page.set()
    assert _process_events_until(app, lambda: not window._console_page_loading)
    anchor_after = window.commands_output.cursorForPosition(QtCore.QPoint(2, 2))
    assert anchor_after.block().text() == anchor_block
    assert anchor_after.positionInBlock() == anchor_in_block
    expanded = window.commands_output.toPlainText()
    assert tail in expanded
    assert expanded.count("line-00179") == 1
    assert window._console_loaded_start > 0
    loaded_start = window._console_loaded_start
    live_start_offset = window._console_live_start_offset
    window._append_console_output("🍕" * 2000)
    assert window._console_loaded_start == loaded_start
    assert window._console_live_start_offset > live_start_offset
    window.close()
    assert not log_path.exists()
    assert app is not None

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

def test_console_live_trim_gap_can_be_loaded_between_history_and_tail(
    monkeypatch, tmp_path
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_CONSOLE_PAGE_BYTES", 64)
    monkeypatch.setattr(desktop, "_CONSOLE_LIVE_WINDOW_BYTES", 128)
    window = desktop.create_desktop_window()
    window.show()
    window._begin_console_capture()
    path = window._console_log_path
    assert path is not None

    content = "".join(
        f"base-{index:04d} {chr(233)} {chr(26481)}{chr(20140)} "
        f"{chr(127829)} {chr(120) * 8}{chr(10)}"
        for index in range(80)
    )
    window._append_console_output(content)
    window.navigation.setCurrentRow(4)
    assert window._console_loaded
    window._load_older_console_page()
    assert window._console_live_start > 0
    assert window._console_history_end_offset == window._console_live_start_offset
    oldest_loaded = window._console_loaded_start
    assert window.commands_gap_button.isHidden()

    appended = "".join(
        f"tail-{index:04d} {chr(127829)} {chr(122) * 8}{chr(10)}"
        for index in range(24)
    )
    window._append_console_output(appended)
    raw = path.read_bytes()
    assert window._console_loaded_start == oldest_loaded
    assert window._console_history_end_offset < window._console_live_start_offset
    assert window.commands_history_button.isVisible()
    assert not window.commands_gap_button.isHidden()

    while window._console_history_end_offset < window._console_live_start_offset:
        previous_end = window._console_history_end_offset
        window.commands_gap_button.click()
        assert window._console_history_end_offset > previous_end

    assert window.commands_gap_button.isHidden()
    assert window.commands_output.toPlainText().encode("utf-8") == raw[oldest_loaded:]
    window.close()
    assert not path.exists()
    assert app is not None

def test_console_first_history_page_keeps_gap_when_live_trim_occurs_during_load(
    monkeypatch, tmp_path
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_CONSOLE_PAGE_BYTES", 64)
    monkeypatch.setattr(desktop, "_CONSOLE_LIVE_WINDOW_BYTES", 128)
    window = desktop.create_desktop_window(
        background_loading=True, latest_loader=lambda: None
    )
    window.show()
    window._begin_console_capture()
    path = window._console_log_path
    assert path is not None
    window._append_console_output(
        "".join(f"base-{index:04d} café 東京 🍕\n" for index in range(80))
    )
    window.navigation.setCurrentRow(4)
    assert _process_events_until(app, lambda: window._console_loaded)
    original_start = window._console_loaded_start
    page_started = threading.Event()
    release_page = threading.Event()
    read_page = window._read_console_history_page

    def delayed_page(*args):
        payload = read_page(*args)
        page_started.set()
        assert release_page.wait(3)
        return payload

    monkeypatch.setattr(window, "_read_console_history_page", delayed_page)
    window._load_older_console_page()
    assert _process_events_until(app, page_started.is_set)
    window._append_console_output(
        "".join(f"tail-{index:04d} 🍕\n" for index in range(30))
    )
    assert window._console_loaded_start > original_start
    assert window._console_history_end_offset == window._console_live_start_offset
    release_page.set()
    assert _process_events_until(app, lambda: not window._console_page_loading)
    assert window._console_history_end_offset == original_start
    assert not window.commands_gap_button.isHidden()

    while window._console_history_end_offset < window._console_live_start_offset:
        prior_end = window._console_history_end_offset
        window.commands_gap_button.click()
        assert _process_events_until(app, lambda: not window._console_page_loading)
        assert window._console_history_end_offset > prior_end

    assert window.commands_output.toPlainText().encode("utf-8") == (
        path.read_bytes()[window._console_loaded_start:]
    )
    window.close()
    assert not path.exists()

def test_console_page_reader_makes_progress_when_newline_is_page_end(
    monkeypatch, tmp_path
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    window = desktop.create_desktop_window()
    window._begin_console_capture()
    path = window._console_log_path
    assert path is not None
    path.write_bytes(b"abcdefg\n")

    page = window._read_console_page(
        path, window._console_generation, limit_bytes=1
    )
    assert page == ("\n", 7, 8, window._console_generation)
    window.close()
    assert not path.exists()
    assert app is not None

def test_console_loading_placeholder_captures_output_across_load_boundary(
    monkeypatch, tmp_path
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QFrame
    import core.gui.desktop as desktop
    import sys

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    window = desktop.create_desktop_window(
        background_loading=True, latest_loader=lambda: None
    )
    child_script = (
        "import time; print('before console load', flush=True); "
        "time.sleep(0.2); print('during console load', flush=True)"
    )
    window._start_process([sys.executable, "-c", child_script], auto_open_report=False)
    log_path = window._console_log_path
    assert log_path is not None
    assert _process_events_until(app, lambda: log_path.stat().st_size > 0)

    snapshot_started = threading.Event()
    release_snapshot = threading.Event()
    read_snapshot = window._read_console_snapshot

    def delayed_snapshot(path, generation):
        payload = read_snapshot(path, generation)
        snapshot_started.set()
        assert release_snapshot.wait(3)
        return payload

    monkeypatch.setattr(window, "_read_console_snapshot", delayed_snapshot)
    window.navigation.setCurrentRow(4)
    assert _process_events_until(app, snapshot_started.is_set)
    skeleton = window.console_stack.widget(1)
    assert window.console_stack.currentWidget() is skeleton
    effects = [
        frame.graphicsEffect()
        for frame in skeleton.findChildren(QFrame)
        if frame.graphicsEffect() is not None
    ]
    assert effects
    initial_opacity = effects[0].opacity()
    time.sleep(0.05)
    window._advance_spinner()
    assert effects[0].opacity() != initial_opacity
    assert _process_events_until(app, lambda: window._process is None)

    release_snapshot.set()
    assert _process_events_until(app, lambda: window._console_loaded)
    output = window.commands_output.toPlainText()
    assert output.count("before console load") == 1
    assert output.count("during console load") == 1
    window.close()
    assert not log_path.exists()
    assert app is not None
