from __future__ import annotations

import sqlite3
import json
import re
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


def test_desktop_translations_are_external_and_english_by_default():
    assert load_translations()["tab_analysis"] == "Analysis"
    assert load_translations("en")["tab_history"] == "History"
    assert load_translations("en")["mode_standard_web"] == "Standard web"
    report_locale = Path(__file__).resolve().parents[1] / "core/report/human/assets/locales/en.json"
    assert json.loads(report_locale.read_text(encoding="utf-8"))["messages"]["status.standard_web"] == "Standard web"


@pytest.mark.parametrize(
    ("dark", "hover", "pressed"),
    [(False, "#1b4a93", "#153b76"), (True, "#99bfff", "#5f90d0")],
)
def test_settings_save_button_has_distinct_hover_and_pressed_states(dark, hover, pressed):
    stylesheet = _stylesheet(dark=dark)
    assert f"#primaryButton:hover {{ background: {hover};" in stylesheet
    assert f"#primaryButton:pressed {{ background: {pressed};" in stylesheet


@pytest.mark.parametrize("dark", [False, True])
def test_progress_text_contrasts_with_empty_and_filled_bar(dark):
    stylesheet = _stylesheet(dark=dark)

    def color(selector, property_name):
        rule = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", stylesheet)
        assert rule is not None
        value = re.search(rf"\b{property_name}:\s*(#[0-9a-fA-F]{{6}})", rule.group(1))
        assert value is not None
        return value.group(1)

    def luminance(value):
        channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92 if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return sum(weight * channel for weight, channel in zip((0.2126, 0.7152, 0.0722), linear))

    def contrast(first, second):
        high, low = sorted((luminance(first), luminance(second)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    for bar, chunk in (
        ("#phaseProgress", "#phaseProgress::chunk"),
        ('#phaseProgress[error="true"]', '#phaseProgress[error="true"]::chunk'),
    ):
        foreground = color(bar, "color")
        assert contrast(foreground, color(bar, "background")) >= 4.5
        assert contrast(foreground, color(chunk, "background")) >= 4.5


def test_source_summary_explains_recorded_facts_without_raw_json():
    row = {
        "title": "Test <source>", "phase": "verify", "status": "completato",
        "details": {
            "parsed": {"title": "Test <source>", "doi": "10.1234/example"},
            "resolved": {"status": "unverified", "matched_title": "Candidate", "reason": "Below threshold <0.5"},
            "fetch": {"tier": "abstract", "pending_tasks": [{"task": "manual"}]},
            "verification_pairs": [{"status": "accepted", "terminal_outcome": "supported"}],
        },
    }
    html = _source_summary_html(row, load_translations(), dark=False)
    assert "Test &lt;source&gt;" in html
    assert "Below threshold &lt;0.5" in html
    assert "Pending manual actions:" in html
    assert "supported" in html
    assert "{\"details\"" not in html


def test_source_summary_explicitly_says_when_no_verdict_exists():
    html = _source_summary_html({"title": "Unverified"}, load_translations(), dark=True)
    assert "No verification verdict is recorded" in html
    assert "No bibliographic lookup is recorded" in html


def test_source_summary_does_not_present_an_open_jury_call_as_a_verdict():
    html = _source_summary_html({
        "title": "Unfinished",
        "details": {"verification_pairs": [{
            "claim_id": "c1", "status": "open", "jury1_outcome": "contradicts",
        }]},
    }, load_translations(), dark=False)
    assert "Verification in progress" in html
    assert "contradicts" not in html


def test_source_double_click_opens_readable_summary_instead_of_json(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog, QTextBrowser
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    visible = []

    def inspect_dialog(dialog):
        view = dialog.findChild(QTextBrowser)
        visible.append(view.toPlainText() if view is not None else "")
        return 0

    monkeypatch.setattr(QDialog, "exec", inspect_dialog)
    window = create_desktop_window(snapshot_loader=lambda _run: {
        "phase": "resolve",
        "references": [{
            "ref_id": "r1", "title": "A paper", "phase": "resolve", "status": "completato",
            "details": {"parsed": {"title": "A paper"}, "resolved": {"status": "unverified", "reason": "Low confidence"}},
        }],
    })
    window.set_run_dir("demo")
    window._show_source_details(0, 0)
    assert "Bibliographic lookup" in visible[0]
    assert "Low confidence" in visible[0]
    assert "No verification verdict" in visible[0]
    assert "{'parsed':" not in visible[0]
    window.close()
    assert app is not None


def test_source_numbers_and_text_headers_sort_and_keep_details_row(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    references = [
        {"ref_id": "r10", "ref_number": 10, "title": "zOo", "status": "completato"},
        {"ref_id": "r2", "ref_number": 2, "title": "Apple", "status": "completato"},
        {"ref_id": "r3", "ref_number": 3, "title": "aPpLe", "status": "completato"},
    ]
    snapshot = {"phase": "resolve", "references": references}
    window = create_desktop_window(snapshot_loader=lambda _run: snapshot)
    window.set_run_dir("demo")
    header = window.source_table.horizontalHeader()

    header.sectionClicked.emit(0)
    assert [window.source_table.item(row, 0).text() for row in range(3)] == ["2", "3", "10"]
    header.sectionClicked.emit(0)
    assert [window.source_table.item(row, 0).text() for row in range(3)] == ["10", "3", "2"]
    header.sectionClicked.emit(1)
    assert [window.source_table.item(row, 1).text() for row in range(3)] == ["Apple", "aPpLe", "zOo"]

    window.source_search.setText("apple")
    assert [window.source_table.item(row, 1).text() for row in range(2)] == ["Apple", "aPpLe"]
    window.source_search.setText("")
    references.append(
        {"ref_id": "r1", "ref_number": 1, "title": "Beta", "status": "completato"}
    )
    window._refresh_snapshot()
    assert [window.source_table.item(row, 1).text() for row in range(4)] == [
        "Apple", "aPpLe", "Beta", "zOo",
    ]

    opened = []
    window._show_source_details = lambda row, _column: opened.append(
        window.source_table.item(row, 0).data(Qt.ItemDataRole.UserRole)["ref_id"]
    )
    window.source_table.cellClicked.emit(2, 8)
    assert opened == ["r1"]
    window.close()
    assert app is not None


def test_desktop_window_shell_and_paused_fetch_lifecycle(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
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
    assert [window.navigation.item(index).text() for index in range(window.navigation.count())] == ["Analisi", "Cronologia", "Cache", "Impostazioni", "Console", "Info"]
    assert not window.windowIcon().isNull()
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


@pytest.mark.parametrize("language", ["en", "it"])
def test_commands_tab_shell_is_localized_and_readable_in_both_themes(
    monkeypatch, language
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window, load_translations

    app = QApplication.instance() or QApplication([])
    text = load_translations(language)
    window = create_desktop_window(language=language)
    window.show()
    assert window.navigation.count() == 6
    assert window.navigation.item(4).text() == text["tab_console"]
    assert window.navigation.item(5).text() == text["tab_info"]
    window.navigation.setCurrentRow(4)
    app.processEvents()
    assert window.pages.currentWidget().isVisible()
    assert window.commands_expert_notice.text() == text["commands_expert_notice"]
    assert not hasattr(window, "commands_prefix_label")
    assert not hasattr(window, "commands_arguments_label")
    assert window.commands_output.isReadOnly()
    assert window.commands_status_label.text() == ""
    assert window.commands_status_label.isHidden()
    assert window.commands_output.font().pixelSize() >= 15
    assert window.console_stack.geometry().bottom() < window.commands_input.geometry().top()
    assert abs(
        window.commands_run_button.geometry().center().y()
        - window.commands_input.geometry().center().y()
    ) <= 2

    light_stylesheet = window.styleSheet()
    assert "color: #18202a" in light_stylesheet
    window._apply_theme(True)
    app.processEvents()
    assert window.pages.currentWidget().isVisible()
    assert window.commands_expert_notice.isVisible()
    assert "color: #edf3ff" in window.styleSheet()
    window.close()
    assert app is not None


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


def test_console_capture_write_failure_is_visible_without_interrupting_gui(
    monkeypatch, tmp_path
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    window = desktop.create_desktop_window()
    window.navigation.setCurrentRow(4)
    window._begin_console_capture()
    log_path = window._console_log_path
    assert log_path is not None
    original_open = Path.open

    def fail_console_append(path, mode="r", *args, **kwargs):
        if path == log_path and mode == "ab":
            raise OSError("disk full")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_console_append)
    window._append_console_output("visible text")

    assert window._console_log_path is None
    assert "disk full" in window.commands_status_label.text()
    assert window.commands_output.toPlainText() == "visible text"
    window.close()
    assert not log_path.exists()
    assert app is not None


def test_console_capture_error_survives_successful_command_exit(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop
    import sys

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    monkeypatch.setattr(
        desktop, "application_argv",
        lambda *_args, **_kwargs: [sys.executable, "-c", "print('done')"],
    )
    original_open = Path.open

    def fail_console_append(path, mode="r", *args, **kwargs):
        if path.name.startswith("callimachus-console-") and mode == "ab":
            raise OSError("disk full")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_console_append)
    window = desktop.create_desktop_window()
    window.commands_input.setText("--help")
    window.commands_run_button.click()
    assert _process_events_until(app, lambda: window._commands_process is None)

    assert "code 0" in window.commands_status_label.text()
    assert "disk full" in window.commands_status_label.text()
    assert window.commands_status_label.property("error") is True
    window.close()
    assert app is not None


def test_console_snapshot_apply_error_exits_loading_placeholder(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    window = desktop.create_desktop_window(
        background_loading=True, latest_loader=lambda: None
    )
    window._begin_console_capture()
    path = window._console_log_path
    assert path is not None
    window.navigation.setCurrentRow(4)
    window._invalidate_load("console")
    token = window._load_tokens["console"]
    original_stat = Path.stat

    def fail_console_stat(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError("log unavailable")
        return original_stat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_console_stat)
    window._load_completed(
        "console", token, ("", 0, 0, window._console_generation), None
    )

    assert window.console_stack.currentWidget() is window.commands_output
    assert "log unavailable" in window.commands_status_label.text()
    assert window.commands_status_label.property("error") is True
    window.close()
    assert app is not None


def test_new_command_status_is_not_cleared_by_later_console_load(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    window = desktop.create_desktop_window()
    window._console_load_error = True
    window._set_commands_status("Command exited with code 7.", error=True)
    window._clear_console_load_error()

    assert window.commands_status_label.text() == "Command exited with code 7."
    assert window.commands_status_label.property("error") is True
    window.close()
    assert app is not None


def test_analysis_launch_error_is_visible_in_console(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(desktop, "user_data_root", lambda: tmp_path)
    window = desktop.create_desktop_window()
    window._start_process([str(tmp_path / "missing-callimachus-executable")])
    assert _process_events_until(app, lambda: window._process is None)

    window.navigation.setCurrentRow(4)
    output = window.commands_output.toPlainText()
    assert desktop.load_translations()["process_error"] in output
    assert window.phase_label.text() in output
    window.close()
    assert app is not None


def test_commands_interactive_input_exits_with_visible_error(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import core.gui.desktop as desktop
    import sys

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        desktop,
        "application_argv",
        lambda *_args, **_kwargs: [sys.executable, "-c", "input('Answer: ')"],
    )
    window = desktop.create_desktop_window()
    window.commands_input.setText("parse")
    window.commands_run_button.click()

    assert _process_events_until(app, lambda: window._commands_process is None)
    window.navigation.setCurrentRow(4)
    assert "EOFError" in window.commands_output.toPlainText()
    assert window.commands_status_label.property("error") is True
    assert "code 1" in window.commands_status_label.text()
    window.close()


def test_analysis_resume_is_blocked_and_close_warns_while_command_runs(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    calls = []
    window = create_desktop_window(
        command_builder=lambda *_args: calls.append("start") or ["run"],
        resume_command_builder=lambda *_args: calls.append("resume") or ["resume"],
    )
    process = object()
    window._commands_process = process
    window._update_run_controls()
    window._start_analysis()
    window._resume_latest_or_stop()
    window._resume_run_row({"run_dir": "paused"})
    window._start_process(["python", "run.py", "--help"])

    assert calls == []
    assert not window.start_button.isEnabled()
    assert not window.resume_latest_button.isEnabled()
    assert not window.commands_run_button.isEnabled()

    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args, **_kwargs: warnings.append(args)
    )
    assert window.close() is False
    assert warnings
    assert window._commands_process is process
    window._commands_process = None
    window.close()
    assert app is not None


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


def test_manual_parse_review_is_default_unless_skipped(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    commands = []
    window = create_desktop_window(
        command_builder=lambda paper, _mode, _level: ["callimachus", "--input", paper],
    )
    window._paper_path = "paper.pdf"
    window._start_process = lambda command, **_kwargs: commands.append(command)
    assert window.skip_parse_review.text().replace("\n", " ") == (
        "Skip manual review of citations and bibliography"
    )

    window._start_analysis()
    assert commands[-1].count("--manual-review") == 1
    window.skip_parse_review.setChecked(True)
    window._start_analysis()
    assert "--manual-review" not in commands[-1]
    form = window.skip_parse_review.parentWidget().layout()
    skip_index = form.indexOf(window.skip_parse_review)
    assert skip_index < form.indexOf(window.skip_manual_checks)
    assert form.itemAt(skip_index - 1).layout().indexOf(window.start_button) >= 0
    window.close()
    assert app is not None


def test_analysis_manuscript_column_can_be_resized(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QSplitter
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(language="en")
    try:
        window.show()
        app.processEvents()
        splitter = window.findChild(QSplitter, "analysisSplitter")
        assert splitter is not None
        assert splitter.orientation() == Qt.Orientation.Horizontal
        assert splitter.widget(0).findChild(QLabel, "paperDropArea") is window.paper_drop
        assert not any(label.text() == "Paper to analyse" for label in window.findChildren(QLabel))
        before = splitter.sizes()
        assert 340 <= before[0] <= 370
        scroll = window.findChild(QScrollArea, "analysisControls")
        assert scroll.viewport().width() >= scroll.widget().minimumWidth()
        window.resize(960, 640)
        app.processEvents()
        narrow = splitter.sizes()
        assert 340 <= narrow[0] <= before[0]
        assert scroll.viewport().width() >= scroll.widget().minimumWidth()
        splitter.setSizes([narrow[0] + 100, max(1, narrow[1] - 100)])
        app.processEvents()
        assert splitter.sizes()[0] > narrow[0]
        assert window._analysis_mode_columns == 2
    finally:
        window.close()
    italian = create_desktop_window(language="it")
    try:
        italian.show()
        app.processEvents()
        scroll = italian.findChild(QScrollArea, "analysisControls")
        assert scroll.viewport().width() >= scroll.widget().minimumWidth()
    finally:
        italian.close()


def test_analysis_checkbox_labels_wrap_for_viewport_and_font(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QScrollArea, QSplitter, QStyle
    from core.gui.desktop import create_desktop_window, load_translations

    app = QApplication.instance() or QApplication([])
    for language in ("en", "it"):
        text = load_translations(language)
        window = create_desktop_window(language=language)
        try:
            window._font_size = 13
            window._apply_theme(window._dark)
            window.resize(960, 640)
            window.show()
            app.processEvents()
            scroll = window.findChild(QScrollArea, "analysisControls")
            assert scroll is not None
            for font_size in (13, 18):
                window._font_size = font_size
                window._apply_theme(window._dark)
                if font_size == 18:
                    window.resize(1100, 640)
                    app.processEvents()
                    splitter = window.findChild(QSplitter, "analysisSplitter")
                    sizes = splitter.sizes()
                    splitter.setSizes([470, max(1, sum(sizes) - 470)])
                app.processEvents()
                assert scroll.viewport().width() >= scroll.widget().minimumWidth()
                for checkbox, translation_key in (
                    (window.skip_parse_review, "skip_parse_review"),
                    (window.skip_manual_checks, "skip_manual_checks"),
                ):
                    full_text = text[translation_key].replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
                    assert checkbox.text().replace("\r\n", " ").replace("\r", " ").replace("\n", " ") == full_text
                    checkbox_x = checkbox.mapTo(scroll.viewport(), QPoint(0, 0)).x()
                    content_x = checkbox.mapTo(scroll.widget(), QPoint(0, 0)).x()
                    trailing_space = max(
                        0, scroll.widget().width() - content_x - checkbox.width()
                    )
                    reserved_width = checkbox.style().pixelMetric(
                        QStyle.PixelMetric.PM_IndicatorWidth, None, checkbox
                    ) + checkbox.style().pixelMetric(
                        QStyle.PixelMetric.PM_CheckBoxLabelSpacing, None, checkbox
                    )
                    text_width = (
                        scroll.viewport().width()
                        - checkbox_x
                        - trailing_space
                        - reserved_width
                    )
                    if checkbox.fontMetrics().horizontalAdvance(full_text) > text_width:
                        assert "\n" in checkbox.text()
                    assert all(
                        checkbox.fontMetrics().horizontalAdvance(line) <= text_width
                        for line in checkbox.text().splitlines()
                    )
        finally:
            window.close()


def test_manuscript_drop_area_also_browses_on_click_and_keyboard(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    selected = iter(("first.pdf", "second.pdf"))
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *_args, **_kwargs: (next(selected), "")
    )
    window = create_desktop_window()
    window.show()
    app.processEvents()
    assert window.paper_drop.acceptDrops()
    assert window.paper_drop.text() == "Drop a manuscript here or click to browse"
    assert not any(button.text() == "Browse…" for button in window.findChildren(QPushButton))

    QTest.mouseClick(window.paper_drop, Qt.MouseButton.LeftButton)
    assert window._paper_path == "first.pdf"
    assert window.paper_drop.text() == "first.pdf"
    window.paper_drop.setFocus()
    QTest.keyClick(window.paper_drop, Qt.Key.Key_Return)
    assert window._paper_path == "second.pdf"
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


def test_paused_parse_with_zero_pending_resumes_from_analysis_and_history(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    run_dir = str(tmp_path)
    snapshot = {
        "phase": "parse", "paused": True, "action_required": False,
        "parse_review_pending": 0, "run_status": "paused", "references": [],
    }
    row = {
        "paper": "paper.pdf", "run_dir": run_dir, "available": True,
        "phase": "parse", "run_status": "paused", "action_required": False,
        "parse_review_pending": 0,
    }
    started = []
    opened = []
    controller = SimpleNamespace(pending_count=lambda: 0)
    window = create_desktop_window(
        snapshot_loader=lambda _run: snapshot,
        history_loader=lambda: [row],
        resume_command_builder=lambda run: ["resume", run],
        parse_review_controller_factory=lambda run: controller,
        parse_review_window_factory=lambda *_args, **_kwargs: opened.append(True),
    )
    window._start_process = lambda command, **_kwargs: started.append(command)
    window.set_run_dir(run_dir)
    window._apply_snapshot(snapshot)
    window._update_run_controls()
    assert window.open_parse_review_button.isVisibleTo(window)
    assert window.open_parse_review_button.text() == "Resume"
    window.open_parse_review_button.click()
    assert started == [["resume", run_dir]]
    assert opened == []

    window.navigation.setCurrentRow(1)
    assert _process_events_until(app, lambda: window.history_table.rowCount() == 1)
    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    window._update_history_actions()
    assert window.history_parse_review_button.isVisibleTo(window)
    assert window.history_parse_review_button.text() == "Resume"
    window.history_parse_review_button.click()
    assert started == [["resume", run_dir], ["resume", run_dir]]
    assert opened == []
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


def test_history_reopens_from_a_real_run_summary_row(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QDialog
    from core.app.desktop import load_run_summary
    from core.gui.desktop import create_desktop_window
    from core.infra.db import RunRepository
    from core.parse.parsing_common import _make_reference

    app = QApplication.instance() or QApplication([])
    run_dir = tmp_path / "paused-run"
    repo = RunRepository.create(
        str(run_dir), run_id="paused-run", input_path="paper.pdf",
        input_sha256="input-hash", accuracy="standard", style=None,
        model_id=None, http_profile=None, challenge_mode=None,
        fixture_fingerprint="fixture",
    )
    try:
        reference = _make_reference(1, "Smith. Example title. Journal (2020).")
        reference["id"] = "r1"
        repo.replace_parse_payload(claims=[], references=[reference], citations=[])
        repo.create_manual_parse_review(
            task_id="review-1", review_kind="reference_identity_review",
            target_id="r1", instructions="Review the reference identity.",
        )
        repo.update_run_phase("parse")
        repo.update_run_status("paused")
    finally:
        repo.close()
    row = load_run_summary(str(run_dir))
    assert row["parse_review_pending"] == 1
    opened = []
    controller = SimpleNamespace(
        pending_count=lambda: 1, load_tasks=lambda: [],
        submit_decision=lambda *_args: None,
        skip_remaining_reviews=lambda: None,
    )

    def factory(path, **kwargs):
        opened.append((path, kwargs))
        return QDialog()

    window = create_desktop_window(
        history_loader=lambda: [row],
        resume_command_builder=lambda path: ["resume", path],
        parse_review_controller_factory=lambda _path: controller,
        parse_review_window_factory=factory,
    )
    window.show()
    window.navigation.setCurrentRow(1)
    assert _process_events_until(app, lambda: window.history_table.rowCount() == 1)
    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    window._update_history_actions()
    assert window.history_parse_review_button.isVisibleTo(window)
    assert window.history_parse_review_button.isEnabled()
    window.history_parse_review_button.click()
    assert len(opened) == 1
    assert opened[0][0] == str(run_dir.resolve())
    dialog = window._parse_review_window
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    dialog.close()
    app.processEvents()
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
        verify_fork_selector=lambda _options: ["model"],
        verify_fork_command_builder=lambda _row, _lanes: {
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


def test_skip_manual_checkbox_waives_pause_and_resumes_without_guided_window(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    skipped = []
    started = []
    window = create_desktop_window(
        snapshot_loader=lambda _run: {"phase": "fetch", "fetch_paused": True},
        history_loader=lambda: [{
            "run_dir": "paused", "run_status": "paused", "available": True,
        }],
        resume_command_builder=lambda run: ["resume", run],
        skip_manual_callback=lambda run: skipped.append(run) or 2,
    )
    window.set_run_dir("paused")
    window.skip_manual_checks.setChecked(True)
    window._start_process = lambda command, **kwargs: started.append((command, kwargs))

    window._on_process_finished(10, QProcess.ExitStatus.NormalExit)

    assert skipped == ["paused"]
    assert started == [(["resume", "paused"], {"environment": {}})]
    assert window._guided_window is None
    window._resume_latest_or_stop()
    assert skipped == ["paused", "paused"]
    assert len(started) == 2
    window.close()
    assert app is not None


def test_skip_manual_checkbox_requires_confirmation_when_ocr_is_pending(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    skipped = []
    started = []
    questions = []
    answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda _parent, title, message: questions.append((title, message)) or answers.pop(0),
    )
    window = create_desktop_window(
        snapshot_loader=lambda _run: {"phase": "fetch", "fetch_paused": True},
        resume_command_builder=lambda run: ["resume", run],
        skip_manual_callback=lambda run: skipped.append(run) or 2,
        ocr_pending_count_loader=lambda _run: 2,
    )
    window.set_run_dir("paused")
    window.skip_manual_checks.setChecked(True)
    window._start_process = lambda command, **kwargs: started.append(command)

    window._on_process_finished(10, QProcess.ExitStatus.NormalExit)
    assert skipped == []
    assert started == []
    assert "2 PDF" in questions[0][1]
    assert "OCR" in questions[0][0]

    window._on_process_finished(10, QProcess.ExitStatus.NormalExit)
    assert skipped == ["paused"]
    assert started == [["resume", "paused"]]
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


def test_skip_manual_checkbox_disables_interactive_browser_challenges(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window()
    run_command = ["python", "run.py", "--run", "a", "--proceed"]
    assert window._command_with_manual_policy(run_command) == run_command
    window.skip_manual_checks.setChecked(True)
    assert window._command_with_manual_policy(run_command) == [
        *run_command, "--challenge-mode", "off",
    ]
    assert window._command_with_manual_policy([*run_command, "--challenge-mode", "interactive"]) == [
        *run_command, "--challenge-mode", "off",
    ]
    report_command = ["python", "run.py", "report", "--run", "a"]
    assert window._command_with_manual_policy(report_command) == report_command
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


def test_returning_to_idle_analysis_does_not_reload_unchanged_run(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    calls = []
    window = create_desktop_window(
        background_loading=True,
        latest_loader=lambda: calls.append("latest") or None,
        snapshot_loader=lambda _run: calls.append("snapshot") or {
            "phase": "done", "references": [{"ref_id": "r1", "title": "Source"}],
        },
        history_loader=lambda: [],
    )
    assert _process_events_until(app, lambda: "latest" not in window._loading_kinds)
    window.navigation.setCurrentRow(1)
    window.navigation.setCurrentRow(0)
    assert window.source_stack.currentIndex() == 0
    assert calls.count("latest") == 1
    assert calls.count("snapshot") == 0

    window.set_run_dir("unchanged-run")
    assert _process_events_until(app, lambda: window.source_table.rowCount() == 1)
    window.navigation.setCurrentRow(1)
    window.navigation.setCurrentRow(0)
    assert window.source_stack.currentIndex() == 0
    assert window.source_table.rowCount() == 1
    assert calls.count("snapshot") == 1
    window.close()


def test_analysis_fetches_source_pages_only_when_requested(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    sources = [
        {"ref_id": str(index), "ref_number": index + 1,
         "title": f"Source {index}"}
        for index in range(120)
    ]
    pages = []
    full_reads = []

    def page(_run, offset, limit):
        pages.append((offset, limit))
        return {"total": len(sources), "references": sources[offset:offset + limit]}

    window = create_desktop_window(
        background_loading=True,
        latest_loader=lambda: None,
        overview_loader=lambda _run: {"phase": "done", "run_status": "completed"},
        snapshot_loader=lambda _run: full_reads.append(True) or {
            "references": sources,
        },
        source_page_loader=page,
    )
    window.set_run_dir("run")
    assert _process_events_until(app, lambda: window.source_table.rowCount() > 0)
    first_batch = window.source_table.rowCount()
    assert 0 < first_batch < len(sources)
    assert pages == [(0, first_batch)]
    assert not full_reads

    window._show_more_sources()
    assert _process_events_until(app, lambda: window.source_table.rowCount() > first_batch)
    assert pages[1][0] == first_batch
    loaded_count = window.source_table.rowCount()
    window.source_search.setText("Source 119")
    assert _process_events_until(
        app,
        lambda: window.source_table.rowCount() == 1
        and window.source_table.item(0, 1).text() == "Source 119",
    )
    assert len(full_reads) == 1
    window.source_search.clear()
    assert window.source_table.rowCount() == loaded_count
    assert len(full_reads) == 1
    window.close()


def test_analysis_refreshes_only_loaded_page_after_run_changes(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    revision = {"value": 1}
    pages = []

    def page(_run, offset, limit):
        pages.append((offset, limit))
        return {"total": 40, "references": [
            {"ref_id": str(index), "title": f"Revision {revision['value']} {index}"}
            for index in range(offset, min(offset + limit, 40))
        ]}

    window = create_desktop_window(
        background_loading=True,
        latest_loader=lambda: None,
        overview_loader=lambda _run: {
            "phase": "fetch", "run_status": "active",
            "updated_at": str(revision["value"]),
        },
        source_page_loader=page,
    )
    window.set_run_dir("run")
    assert _process_events_until(
        app, lambda: window.source_table.rowCount() > 0
        and window._snapshot_loaded_run == "run",
    )
    first_count = window.source_table.rowCount()
    revision["value"] = 2
    window._request_snapshot(force=True)
    assert _process_events_until(
        app, lambda: window.source_table.item(0, 1).text() == "Revision 2 0",
    )
    assert len(pages) >= 2
    assert all(page_range == (0, first_count) for page_range in pages)
    window.close()


def test_active_analysis_replaces_placeholder_when_parse_adds_sources(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    sources = []
    pages = []

    def page(_run, offset, limit):
        pages.append((offset, limit))
        return {"total": len(sources), "references": sources[offset:offset + limit]}

    window = create_desktop_window(
        background_loading=True,
        latest_loader=lambda: None,
        overview_loader=lambda _run: {
            "phase": "parse", "run_status": "active",
            "updated_at": "unchanged", "phase_progress": 0,
            "progress_counts": {"sources_total": len(sources)},
        },
        source_page_loader=page,
    )
    # New analyses receive their run path before the worker creates the database.
    # The first successful overview must initiate the source page without navigation.
    window._run_dir = "new-run"
    window._request_snapshot(force=True)
    assert _process_events_until(
        app, lambda: window._source_page_loaded_run == "new-run"
        and window._snapshot_loaded_run == "new-run",
    )
    assert window.source_stack.currentIndex() == 0
    assert window.source_table.rowCount() == 0
    sources.extend([
        {"ref_id": "r1", "title": "Parsed source 1"},
        {"ref_id": "r2", "title": "Parsed source 2"},
    ])
    window._request_snapshot(force=True)
    assert _process_events_until(
        app, lambda: window.source_table.rowCount() == 2
        and window.source_stack.currentIndex() == 0,
    )
    assert len(pages) >= 2
    window._request_snapshot(force=True)
    assert _process_events_until(app, lambda: "snapshot" not in window._loading_kinds)
    assert window.source_stack.currentIndex() == 0
    window.close()


def test_background_source_detail_loads_only_on_open_and_releases_on_close(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    release_detail = threading.Event()
    calls = []

    def detail(run_dir, ref_id):
        calls.append((run_dir, ref_id))
        assert release_detail.wait(3)
        return {"parsed": {"title": "Loaded only on demand"}}

    window = create_desktop_window(
        background_loading=True,
        latest_loader=lambda: None,
        snapshot_loader=lambda _run: {"phase": "done", "references": [
            {"ref_id": "r1", "title": "Source", "status": "completato"}
        ]},
        source_detail_loader=detail,
    )
    window.set_run_dir("old-run")
    assert _process_events_until(app, lambda: window.source_table.rowCount() == 1)
    assert calls == []
    window._show_source_details(0, 0)
    assert window._detail_dialog is not None
    assert window._detail_dialog[3].currentIndex() == 1
    assert _process_events_until(app, lambda: calls == [("old-run", "r1")])
    release_detail.set()
    assert _process_events_until(app, lambda: window._detail_dialog[3].currentIndex() == 0)
    assert "Loaded only on demand" in window._detail_dialog[1].toPlainText()
    window._detail_dialog[0].accept()
    assert window._detail_dialog is None
    window.close()


def test_tables_render_in_viewport_sized_batches(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    history = [{"run_dir": str(index), "paper": f"Paper {index}", "available": True}
               for index in range(120)]
    sources = [{"ref_id": str(index), "title": f"Source {index}"}
               for index in range(120)]
    cache = [{"work_id": str(index), "title": f"Cached {index}"}
             for index in range(120)]
    window = create_desktop_window(
        history_loader=lambda: history,
        cache_loader=lambda: cache,
        snapshot_loader=lambda _run: {"phase": "done", "references": sources},
    )
    window.set_run_dir("old-run")
    initial_source = window.source_table.rowCount()
    initial_history = window.history_table.rowCount()
    initial_cache = window.cache_table.rowCount()
    assert 0 < initial_source < 120
    assert 0 < initial_history < 120
    assert 0 < initial_cache < 120
    window._show_more_sources()
    window._show_more_history()
    window._show_more_cache()
    assert initial_source < window.source_table.rowCount() <= 120
    assert initial_history < window.history_table.rowCount() <= 120
    assert initial_cache < window.cache_table.rowCount() <= 120
    window.close()
    assert app is not None


def test_cache_select_all_includes_unrendered_batches(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    cache = [
        {"ref_id": str(index), "title": f"Cached {index}", "tier": "fulltext"}
        for index in range(120)
    ]
    window = create_desktop_window(cache_loader=lambda: cache)
    assert window.cache_table.rowCount() < len(cache)
    window.cache_select_all.click()
    assert len(window._checked_cache_rows()) == len(cache)
    window.cache_select_all.click()
    assert not window._checked_cache_rows()
    window.close()
    assert app is not None


def test_history_loads_briefs_then_only_visible_run_summaries(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    briefs = [
        {"run_dir": str(index), "paper": f"Paper {index}",
         "available": True, "summary_pending": True}
        for index in range(120)
    ]
    calls = []

    def detail(run_dir):
        calls.append(run_dir)
        return {"source_counts": {"resolved": 1}, "verification_counts": {
            "total": 1, "outcomes": {"supported": 1},
        }}

    window = create_desktop_window(
        background_loading=True,
        latest_loader=lambda: None,
        history_brief_loader=lambda: briefs,
        history_detail_loader=detail,
    )
    window.navigation.setCurrentRow(1)
    assert _process_events_until(app, lambda: window.history_table.rowCount() > 0)
    first_batch = window.history_table.rowCount()
    assert first_batch < len(briefs)
    assert _process_events_until(app, lambda: len(calls) == first_batch)
    assert len(calls) < len(briefs)
    assert _process_events_until(
        app, lambda: "supported 1" in window.history_table.item(0, 2).text()
    )
    window._show_more_history()
    assert _process_events_until(app, lambda: len(calls) > first_batch)
    assert _process_events_until(
        app, lambda: len(calls) == window.history_table.rowCount()
    )
    window.close()


@pytest.mark.parametrize("paged", [False, True])
def test_background_finish_opens_report_after_snapshot_load(tmp_path, monkeypatch, paged):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from threading import Event
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    report = tmp_path / "report.html"
    report.write_text("<html>Done</html>", encoding="utf-8")
    release = Event()
    calls = []
    opened = []

    def snapshot(_run):
        calls.append(1)
        if len(calls) == 2:
            assert release.wait(3)
        return {"phase": "done", "run_status": "completed", "references": []}

    window = create_desktop_window(
        background_loading=True,
        latest_loader=lambda: None,
        snapshot_loader=snapshot,
        overview_loader=snapshot if paged else None,
        source_page_loader=(
            lambda _run, _offset, _limit: {"total": 0, "references": []}
        ) if paged else None,
        open_path_callback=opened.append,
    )
    window.set_run_dir(str(tmp_path))
    assert _process_events_until(app, lambda: len(calls) == 1 and window._display_run_done)
    window._on_process_finished(0, QProcess.ExitStatus.NormalExit)
    assert opened == []
    assert _process_events_until(app, lambda: len(calls) == 2)
    release.set()
    assert _process_events_until(app, lambda: opened == [str(report)])
    window.close()


@pytest.mark.parametrize("dark", [False, True])
def test_pipeline_failure_has_theme_aware_red_state(monkeypatch, dark):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window()
    if dark:
        window.theme_toggle.click()
    window.phase_progress.setRange(0, 0)
    window._on_process_finished(2, QtCore.QProcess.ExitStatus.NormalExit)
    assert window.phase_label.property("error") is True
    assert window.phase_progress.property("error") is True
    assert window.phase_progress.maximum() == 100
    assert '#phaseProgress[error="true"]::chunk' in _stylesheet(dark=dark)
    window.set_run_dir("another")
    assert window.phase_label.property("error") is False
    window.close()
    assert app is not None


def test_desktop_language_preference_and_discovered_llm_choices(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QComboBox
    from core.app.desktop_config import (
        discover_verify_backend_options,
        selected_verify_environment,
    )
    from core.gui.desktop import create_desktop_window, load_translations

    app = QApplication.instance() or QApplication([])
    saved = []
    configured_environment = {
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
        "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "freetoken:local-a",
        "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "freetoken:local-b",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium",
        "FREETOKEN_MODEL": "local-a,local-b",
    }
    italian = load_translations("it")
    window = create_desktop_window(
        preference_loader=lambda: "it",
        preference_saver=saved.append,
        command_builder=lambda paper, _mode, _level: {
            "command": ["run", "--input", paper],
            "environment": {
                "UNCHANGED": "yes",
                "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "freetoken:local-a",
                "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "freetoken:local-b",
            },
        },
        verify_candidates_loader=lambda: discover_verify_backend_options(
            configured_environment
        ),
        verify_selection_preview_loader=lambda roles, level: selected_verify_environment(
            configured_environment, roles, jury2_level=level
        ),
        settings_loader=lambda: [{"group": "general", "label": "Mode", "value": "standard", "source": "env_file"}],
        docs_loader=lambda: "# Guide\n\n## Start\nReadable text",
    )
    assert window.navigation.item(0).text() == "Analisi"
    assert "min-height: 52px" in window.styleSheet()
    assert not hasattr(window, "llm_single_model_hint")
    assert window.llm_choices.rowCount() == 2
    jury1a, jury2a = window.llm_role_choices["freetoken:local-a"]
    jury1b, jury2b = window.llm_role_choices["freetoken:local-b"]
    assert window.llm_choices.horizontalHeaderItem(1).text() == italian["jury1_role"]
    assert window.llm_choices.horizontalHeaderItem(2).text() == italian["jury2_role"]
    assert jury1a.text() == jury2a.text() == ""
    assert jury1a.toolTip() == italian["jury1_lane_tip"]
    assert jury2a.toolTip() == italian["jury2_lane_tip"]
    assert jury1a.accessibleName() == "freetoken:local-a: Jury 1"
    assert jury1a.isChecked() and not jury2a.isChecked()
    assert not jury1b.isChecked() and jury2b.isChecked()
    assert f"{italian['jury1_role']}: freetoken:local-a" in window.jury_models.text()
    assert f"{italian['jury2_role']}: freetoken:local-b" in window.jury_models.text()
    jury1b.setChecked(True)
    jury2a.setChecked(False)
    jury2b.setChecked(False)
    assert window.llm_status.text() == italian["llm_ready"]
    assert f"{italian['jury2_role']}: {italian['jury2_inactive']}" in window.jury_models.text()
    assert all(not button.isEnabled() for button in window.jury_buttons)
    medium = next(
        button for button in window.jury_buttons
        if button.property("jury2_level") == "medium"
    )
    assert medium.isChecked()
    jury2b.setChecked(True)
    assert all(button.isEnabled() for button in window.jury_buttons)
    assert medium.isChecked()
    launched = []
    window._set_paper("paper.pdf")
    window._start_process = lambda command, **kwargs: launched.append((command, kwargs))
    window._start_analysis()
    assert "--references-only" not in launched[-1][0]
    assert launched[-1][1]["environment"] == {
        "UNCHANGED": "yes",
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium",
        "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "freetoken:local-a",
        "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "",
        "FREETOKEN_MODEL": "local-a,local-b",
    }
    jury2a.setChecked(True)
    assert f"{italian['jury1_role']}: freetoken:local-a" in window.jury_models.text()
    assert (
        f"{italian['jury2_role']}: freetoken:local-a, freetoken:local-b"
        in window.jury_models.text()
    )
    window._start_analysis()
    assert "--references-only" not in launched[-1][0]
    assert launched[-1][1]["environment"]["CITATION_VERIFIER_VERIFY_JURY1_ONLY"] == ""
    assert launched[-1][1]["environment"]["CITATION_VERIFIER_VERIFY_JURY2_ONLY"] == ""

    jury1b.setChecked(False)
    jury2b.setChecked(False)
    assert jury1a.isChecked() and jury2a.isChecked()
    jury1a.setChecked(False)
    assert not jury1a.isChecked() and not jury2a.isChecked()
    window._start_analysis()
    assert "--references-only" in launched[-1][0]
    assert launched[-1][1]["environment"]["CITATION_VERIFIER_VERIFY_BACKENDS"] == ""
    assert launched[-1][1]["environment"]["CITATION_VERIFIER_VERIFY_JURY1_ONLY"] == ""
    assert launched[-1][1]["environment"]["CITATION_VERIFIER_VERIFY_JURY2_ONLY"] == ""
    assert window.docs_outline.topLevelItemCount() == 1
    assert window.docs_outline.topLevelItem(0).childCount() == 1
    selector = window.settings_content.findChild(QComboBox)
    # The persisted preference boundary is invoked by the settings selector.
    assert selector is not None
    selector.setCurrentIndex(0)
    assert saved[-1] == "en"
    window.close()


def test_settings_are_staged_and_document_tree_loads_subdocuments(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QPushButton
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    saved = []
    rows = [
        {"name": "GOOGLE_BOOKS_API_KEY", "label": "Google Books key", "group": "resolution",
         "control": "secret", "configured": True, "value": "", "source": "env_file"},
        {"name": "FETCH_RETRIES", "label": "Retries", "group": "fetch", "control": "integer",
         "value": "2", "configured": True, "description": "Number of retries", "source": "env_file"},
        {"name": "OPENAI_MODEL", "label": "Model", "group": "models", "provider": "OpenAI",
         "control": "text", "value": "gpt", "configured": True},
    ]
    window = create_desktop_window(
        settings_loader=lambda: rows, settings_saver=lambda updates: saved.append(dict(updates)),
        docs_loader=lambda: {"README.md": "# Guide\n\n[Quickstart](01-quickstart.md)",
                             "01-quickstart.md": "# Quickstart\n\n## Start\nReadable text"},
    )
    window.navigation.setCurrentRow(3)
    app.processEvents()
    assert window.settings_categories.count() >= 4
    window.settings_categories.setCurrentRow(next(
        index for index in range(window.settings_categories.count())
        if "resolution" in window.settings_categories.item(index).text().lower()
    ))
    secret = window._settings_editors["GOOGLE_BOOKS_API_KEY"]
    assert secret.isReadOnly()
    QTest.mouseClick(secret, Qt.MouseButton.LeftButton)
    assert not secret.isReadOnly()
    assert secret.text() == ""
    secret.setText("replacement")
    window._stage_secret("GOOGLE_BOOKS_API_KEY", "replacement")
    window._save_settings()
    assert saved == [{"GOOGLE_BOOKS_API_KEY": "replacement"}]
    assert window.docs_outline.minimumWidth() == 260
    assert window.docs_outline.maximumWidth() == 380
    assert window.docs_outline.topLevelItemCount() == 2
    window.docs_outline.setCurrentItem(window.docs_outline.topLevelItem(0))
    window._open_doc_link(QUrl("01-quickstart.md#start"))
    assert window._current_doc_path == "01-quickstart.md"
    assert "Quickstart" in window.docs_view.toPlainText()
    size = window._font_size
    window.font_increase.click()
    assert window._font_size >= size
    window.close()


def test_documentation_follows_relative_deployment_links(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    documents = {
        "10-keys-and-credentials.md": "# Keys\n\n[Agent guide](../deployment/agent-guide.md)",
        "../deployment/agent-guide.md": "# Agent guide\n\n[Administrator guide](administrator-guide.md)",
        "../deployment/administrator-guide.md": "# Administrator guide\n\n[Architecture](../architecture/artifact-integrity.md)",
        "../architecture/artifact-integrity.md": "# Artifact integrity\n\n[Deployment](../../DEPLOYMENT.md)",
        "../../DEPLOYMENT.md": "# Deployment\n\n[Architecture](docs/architecture/artifact-integrity.md)",
    }
    window = create_desktop_window(docs_loader=lambda: documents)
    for link, expected in (
        ("../deployment/agent-guide.md", "../deployment/agent-guide.md"),
        ("administrator-guide.md", "../deployment/administrator-guide.md"),
        ("../architecture/artifact-integrity.md", "../architecture/artifact-integrity.md"),
        ("../../DEPLOYMENT.md", "../../DEPLOYMENT.md"),
        ("docs/architecture/artifact-integrity.md", "../architecture/artifact-integrity.md"),
    ):
        window._open_doc_link(QUrl(link))
        assert window._current_doc_path == expected
        assert documents[expected].splitlines()[0][2:] in window.docs_view.toPlainText()
    window.close()
    assert app is not None


def test_settings_have_one_sticky_footer_and_secret_cancel_preserves_value(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QFrame, QPushButton
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    saved = []
    window = create_desktop_window(
        language="en",
        settings_loader=lambda: [{"name": "CORE_API_KEY", "label": "CORE key", "group": "resolution",
                                  "control": "secret", "configured": True, "value": "", "source": "env_file"}],
        settings_saver=lambda updates: saved.append(dict(updates)),
    )
    window.navigation.setCurrentRow(3)
    app.processEvents()
    assert len([button for button in window.findChildren(QPushButton) if button.text() == "Save changes"]) == 1
    for index in range(window.settings_categories.count()):
        window.settings_categories.setCurrentRow(index)
        app.processEvents()
        visible_sections = [section for section in window.settings_content.findChildren(QFrame, "settingsSection")
                            if not section.isHidden()]
        assert len(visible_sections) == 1
    assert len([button for button in window.findChildren(QPushButton) if button.text() == "Save changes"]) == 1
    window.settings_categories.setCurrentRow(next(
        index for index in range(window.settings_categories.count())
        if "resolution" in window.settings_categories.item(index).text().lower()
    ))
    secret = window._settings_editors["CORE_API_KEY"]
    assert secret.isReadOnly() and secret.text() == "••••••••"
    window._begin_secret_replacement("CORE_API_KEY", secret)
    secret.setText("temporary")
    window._stage_secret("CORE_API_KEY", "temporary")
    secret.clear()
    window._stage_secret("CORE_API_KEY", "")
    window._restore_empty_secret("CORE_API_KEY", secret)
    assert secret.isReadOnly() and secret.text() == "••••••••"
    window._save_settings()
    assert saved == []
    window.close()


def test_integrity_settings_can_generate_signing_key_without_exposing_it(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(
        language="en",
        settings_loader=lambda: [{"name": "REPORT_SIGNING", "label": "Signing", "group": "integrity",
                                  "control": "boolean", "choices": [], "value": "0"}],
        signing_key_generator=lambda: {"created": True, "path": ".callimachus/signing.key"},
    )
    window.navigation.setCurrentRow(3)
    app.processEvents()
    window.settings_categories.setCurrentRow(next(
        index for index in range(window.settings_categories.count())
        if "integrity" in window.settings_categories.item(index).text().lower()
    ))
    window._generate_signing_key()
    assert "created" in window.signing_key_status.text().lower()
    assert ".callimachus/signing.key" in window.signing_key_status.text()
    window.close()


def test_settings_preserve_explicit_empty_edits_and_clear_reverted_dirty_state(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    saved = []
    rows = [{
        "name": "CITATION_VERIFIER_MAILTO", "label": "Mailto", "group": "general",
        "control": "text", "value": "person@example.test", "configured": True,
        "source": "env_file",
    }, {
        "name": "CITATION_VERIFIER_FETCH_WORKERS", "label": "Fetch workers", "group": "fetch",
        "control": "integer", "value": "", "configured": False,
        "explicitly_empty": True, "source": "env_file",
    }]
    window = create_desktop_window(
        language="en",
        settings_loader=lambda: rows,
        settings_saver=lambda updates: saved.append(dict(updates)),
    )
    window.navigation.setCurrentRow(3)
    app.processEvents()
    editor = window._settings_editors["CITATION_VERIFIER_MAILTO"]
    editor.setText("")
    window._stage_setting("CITATION_VERIFIER_MAILTO", "")
    assert window._settings_dirty == {"CITATION_VERIFIER_MAILTO": ""}
    window._save_settings()
    assert saved == [{"CITATION_VERIFIER_MAILTO": ""}]

    fetch_index = next(index for index in range(window.settings_categories.count())
                       if "retrieval" in window.settings_categories.item(index).text().lower())
    window.settings_categories.setCurrentRow(fetch_index)
    assert window._settings_editors["CITATION_VERIFIER_FETCH_WORKERS"].text() == "Empty"
    general_index = next(index for index in range(window.settings_categories.count())
                         if "general" in window.settings_categories.item(index).text().lower())
    window.settings_categories.setCurrentRow(general_index)
    window._stage_setting("CITATION_VERIFIER_MAILTO", "changed")
    window._stage_setting("CITATION_VERIFIER_MAILTO", "person@example.test")
    assert window._settings_dirty == {}
    window.close()
    assert app is not None


def test_info_links_have_readable_theme_specific_colors(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(
        docs_loader=lambda: {"README.md": "# Guide\n\n[Documentation link](https://example.org)"},
    )
    link = window.docs_view.document().find("Documentation link")
    assert link.charFormat().foreground().color().name() == "#174a8b"
    window.theme_toggle.click()
    link = window.docs_view.document().find("Documentation link")
    assert link.charFormat().foreground().color().name() == "#a8d1ff"
    window.close()
    assert app is not None


def test_verify_role_and_severity_controls_lock_until_process_finishes(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    real_process = QtCore.QProcess

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeProcess:
        ProcessError = real_process.ProcessError
        ExitStatus = real_process.ExitStatus
        ProcessState = real_process.ProcessState

        def __init__(self, _parent=None):
            self.readyReadStandardOutput = FakeSignal()
            self.readyReadStandardError = FakeSignal()
            self.finished = FakeSignal()
            self.errorOccurred = FakeSignal()
            self.started = None

        def setProcessEnvironment(self, _environment):
            pass

        def start(self, program, arguments):
            self.started = (program, arguments)

        def write(self, _payload):
            return 0

    monkeypatch.setattr(QtCore, "QProcess", FakeProcess)
    window = create_desktop_window(
        verify_candidates_loader=lambda: [{
            "selector": "freetoken:local", "label": "freetoken: local",
            "configured": True,
        }],
        verify_selection_preview_loader=lambda roles, _level: {
            "available": bool(roles["jury1"] and roles["jury2"]),
            "jury1": roles["jury1"],
            "jury2": roles["jury2"],
            "jury2_level": "medium" if roles["jury2"] else "off",
        },
    )
    window._refresh_llm_status()
    jury1, jury2 = window.llm_role_choices["freetoken:local"]
    assert jury1.isEnabled() and jury2.isEnabled()
    assert all(button.isEnabled() for button in window.jury_buttons)

    window._start_process(["fake-worker"])
    assert window._process is not None
    assert not jury1.isEnabled() and not jury2.isEnabled()
    assert all(not button.isEnabled() for button in window.jury_buttons)

    window._resume_latest_or_stop()
    assert window._stopping is True
    assert not jury1.isEnabled() and not jury2.isEnabled()
    assert all(not button.isEnabled() for button in window.jury_buttons)

    window._on_process_finished(0, real_process.ExitStatus.NormalExit)
    assert window._process is None
    assert jury1.isEnabled() and jury2.isEnabled()
    assert all(button.isEnabled() for button in window.jury_buttons)
    window.close()
    assert app is not None


def test_models_saved_in_settings_appear_on_return_to_analysis(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    configured = {"value": False}
    window = create_desktop_window(
        settings_loader=lambda: [{
            "name": "CITATION_VERIFIER_VERIFY_BACKENDS", "label": "Backends",
            "group": "models", "control": "text", "value": "",
        }],
        settings_saver=lambda _updates: configured.update(value=True),
        verify_candidates_loader=lambda: ([
            {"selector": "example:model", "label": "example — model", "configured": True},
            {"selector": "example:other", "label": "example — other", "configured": True},
        ] if configured["value"] else []),
    )
    assert window.llm_choices.rowCount() == 0
    window.navigation.setCurrentRow(3)
    window._stage_setting("CITATION_VERIFIER_VERIFY_BACKENDS", "example")
    assert window._save_settings()
    window.navigation.setCurrentRow(0)
    assert window.llm_choices.rowCount() == 2
    jury1, jury2 = window.llm_role_choices["example:model"]
    assert jury1.isChecked() and jury2.isChecked()
    jury1.setChecked(False)
    window.navigation.setCurrentRow(3)
    window.navigation.setCurrentRow(0)
    jury1, jury2 = window.llm_role_choices["example:model"]
    assert not jury1.isChecked() and jury2.isChecked()
    other_jury1, other_jury2 = window.llm_role_choices["example:other"]
    assert other_jury1.isChecked() and other_jury2.isChecked()
    window.close()
    assert app is not None


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
        jury2a.setChecked(False)
        jury1b.setChecked(False)
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
        assert settings.allKeys() == ["llm_role_selection"]

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


def test_single_discovered_model_is_preselected_without_backend_flag(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(
        verify_candidates_loader=lambda: [{
            "selector": "openai_compatible:deepseek-v4-flash",
            "label": "DeepSeek", "configured": False,
        }],
    )
    jury1, jury2 = window.llm_role_choices["openai_compatible:deepseek-v4-flash"]
    assert jury1.isChecked() and jury2.isChecked()
    jury1.setChecked(False)
    assert not jury1.isChecked() and not jury2.isChecked()
    window.navigation.setCurrentRow(1)
    window.navigation.setCurrentRow(0)
    jury1, jury2 = window.llm_role_choices["openai_compatible:deepseek-v4-flash"]
    assert not jury1.isChecked() and not jury2.isChecked()
    window.close()
    assert app is not None


def test_selected_llm_ready_status_survives_base_environment_snapshot(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window, load_translations

    app = QApplication.instance() or QApplication([])
    launched = []
    window = create_desktop_window(
        language="en",
        command_builder=lambda paper, _mode, _level: {
            "command": ["run", "--input", paper, "--references-only"],
            "environment": {},
        },
        verify_candidates_loader=lambda: [{
            "selector": "openai_compatible:test-model",
            "label": "Test model", "configured": False,
        }],
        verify_selection_preview_loader=lambda roles, _level: {
            "available": bool(roles["jury1"] and roles["jury2"]),
            "jury1": roles["jury1"],
            "jury2": roles["jury2"],
            "jury2_level": "medium" if roles["jury2"] else "off",
            "overlay": {"CITATION_VERIFIER_VERIFY_BACKENDS": "openai_compatible:test-model"},
        },
    )
    window._set_paper("paper.pdf")
    window._start_process = lambda command, **kwargs: launched.append((command, kwargs))

    window._start_analysis()
    assert "--references-only" not in launched[-1][0]
    assert (
        launched[-1][1]["environment"]["CITATION_VERIFIER_VERIFY_BACKENDS"]
        == "openai_compatible:test-model"
    )
    assert window.llm_status.text() == load_translations()["llm_ready"]

    window._apply_snapshot({"phase": "parse", "llm_available": False})
    assert window.llm_status.text() == load_translations()["llm_ready"]
    window.close()
    assert app is not None


def test_combo_popup_palette_and_large_font_keep_pages_visible(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication, QComboBox, QScrollArea
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    settings = [{
            "name": "CITATION_VERIFIER_ACCURACY", "label": "Accuracy", "group": "general",
            "control": "enum", "choices": [{"label": "Standard", "value": "standard"}],
            "value": "standard", "configured": True,
        }]
    settings.extend({
        "name": f"CITATION_VERIFIER_VERIFY_TEST_{index}",
        "label": f"Verify test setting {index}",
        "group": "verify",
        "control": "integer",
        "value": str(index),
        "configured": True,
    } for index in range(18))
    window = create_desktop_window(
        language="en",
        settings_loader=lambda: settings,
    )
    window.resize(960, 640)
    window.show()
    window.navigation.setCurrentRow(3)
    app.processEvents()
    combo = next(item for item in window.settings_content.findChildren(QComboBox)
                 if item.findData("standard") >= 0)
    light = combo.view().palette()
    assert light.color(QPalette.ColorRole.Base).name() == "#ffffff"
    assert light.color(QPalette.ColorRole.Text).name() == "#18202a"
    assert combo.view().viewport().palette().color(
        QPalette.ColorRole.Base
    ).name() == "#ffffff"
    assert combo.view().window().palette().color(
        QPalette.ColorRole.Window
    ).name() == "#ffffff"
    assert "background-color: #ffffff" in combo.view().styleSheet()

    window.theme_toggle.click()
    dark = combo.view().palette()
    assert dark.color(QPalette.ColorRole.Base).name() == "#101722"
    assert dark.color(QPalette.ColorRole.Text).name() == "#edf3ff"
    assert combo.view().viewport().palette().color(
        QPalette.ColorRole.Base
    ).name() == "#101722"
    assert "background-color: #101722" in combo.view().styleSheet()

    window._font_size = 18
    window._apply_theme(window._dark)
    window.navigation.setCurrentRow(0)
    app.processEvents()
    analysis_scroll = window.findChild(QScrollArea, "analysisControls")
    assert analysis_scroll is not None
    assert analysis_scroll.verticalScrollBar().maximum() > 0
    window.navigation.setCurrentRow(3)
    app.processEvents()
    verify_index = next(index for index in range(window.settings_categories.count())
                        if "verify" in window.settings_categories.item(index).text().lower())
    window.settings_categories.setCurrentRow(verify_index)
    app.processEvents()
    assert window.settings_scroll.verticalScrollBar().maximum() > 0
    assert window.settings_content.minimumSizeHint().height() > window.settings_scroll.viewport().height()
    window.navigation.setCurrentRow(1)
    app.processEvents()
    assert window.history_table.height() > 250
    assert window.history_table.horizontalHeader().isVisible()
    assert window.history_actions_scroll.height() >= window.history_resume_button.sizeHint().height()
    window.navigation.setCurrentRow(2)
    app.processEvents()
    assert window.cache_search.isVisible()
    window.close()
    assert app is not None


def test_settings_and_multi_history_cache_checkbox_actions(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextTable
    from PySide6.QtWidgets import QApplication, QMessageBox, QSpinBox
    from core.gui.desktop import _documentation_css, create_desktop_window

    app = QApplication.instance() or QApplication([])
    settings = [{
        "name": "CITATION_VERIFIER_VERIFY_MAX_TOKENS", "label": "Max tokens",
        "group": "verify", "control": "integer", "value": "3",
        "configured": True,
    }, {
        "name": "CITATION_VERIFIER_VERIFY_COOLDOWN_SECONDS", "label": "Cooldown",
        "group": "verify", "control": "integer", "value": "0",
        "configured": True,
    }, {
        "name": "GOOGLE_BOOKS_API_KEY", "label": "Google Books key",
        "group": "resolution", "control": "secret", "value": "",
        "configured": False, "source": "unconfigured",
    }]
    def generate_key():
        settings.append({
            "name": "CITATION_VERIFIER_SIGNING_KEY_FILE", "label": "Signing key file",
            "group": "integrity", "control": "text", "value": "/keys/signing.key",
            "configured": True,
        })
        return {"created": True, "path": "/keys/signing.key"}
    history = [
        {"paper": "one", "run_dir": "one", "available": True, "run_status": "completed", "completed": True},
        {"paper": "two", "run_dir": "two", "available": True, "run_status": "completed", "completed": True},
        {"paper": "active", "run_dir": "active", "available": True, "run_status": "active"},
    ]
    deleted_history, deleted_cache = [], []
    window = create_desktop_window(
        settings_loader=lambda: settings,
        signing_key_generator=generate_key,
        history_loader=lambda: history,
        cache_loader=lambda: [
            {"title": "one", "ref_id": "one", "tier": "fulltext"},
            {"title": "two", "ref_id": "two", "tier": "abstract"},
        ],
        delete_history_callback=lambda rows: deleted_history.extend(rows),
        delete_cache_callback=lambda rows: deleted_cache.extend(rows),
        docs_loader=lambda: "# Guide\n\nFirst paragraph.\n\nSecond paragraph.\n\n```text\nboxed code\n```",
    )
    window.navigation.setCurrentRow(3)
    verify_index = next(index for index in range(window.settings_categories.count())
                        if "verify" in window.settings_categories.item(index).text().lower())
    window.settings_categories.setCurrentRow(verify_index)
    spin = window._settings_editors["CITATION_VERIFIER_VERIFY_MAX_TOKENS"]
    assert isinstance(spin, QSpinBox) and spin.minimum() == 0
    spin.stepDown(); spin.stepDown(); spin.stepDown(); spin.stepDown()
    assert spin.value() == 0
    assert window._settings_editors["CITATION_VERIFIER_VERIFY_COOLDOWN_SECONDS"].text() == "0"
    window._stage_setting("CITATION_VERIFIER_VERIFY_MAX_TOKENS", "7")
    resolution_index = next(index for index in range(window.settings_categories.count())
                            if "resolution" in window.settings_categories.item(index).text().lower())
    window.settings_categories.setCurrentRow(resolution_index)
    assert window._settings_editors["GOOGLE_BOOKS_API_KEY"].property("unconfigured") is True
    integrity_index = next(index for index in range(window.settings_categories.count())
                           if "integrity" in window.settings_categories.item(index).text().lower())
    window.settings_categories.setCurrentRow(integrity_index)
    window._generate_signing_key()
    assert "Signing key created" in window.signing_key_status.text()
    assert window._settings_editors["CITATION_VERIFIER_SIGNING_KEY_FILE"].text() == "/keys/signing.key"
    assert window._settings_dirty["CITATION_VERIFIER_VERIFY_MAX_TOKENS"] == "7"
    window._settings_dirty.clear()

    window.navigation.setCurrentRow(1)
    window.history_select_all.setChecked(True)
    assert not window.history_resume_button.isEnabled()
    assert not window.history_report_button.isEnabled()
    assert not window.history_verify_fork_button.isEnabled()
    assert "active" not in window._history_checked_run_dirs
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window._delete_selected_history()
    assert [row["run_dir"] for row in deleted_history] == ["one", "two"]

    window.navigation.setCurrentRow(2)
    window.cache_select_all.setChecked(True)
    window.cache_search.setText("one")
    assert [row["ref_id"] for row in window._checked_cache_rows()] == ["one"]
    window.cache_search.clear()
    window._delete_selected_cache()
    assert [row["ref_id"] for row in deleted_cache] == ["one", "two"]
    css = _documentation_css(dark=False, font_size=14)
    assert "p { margin:" in css and "pre { background:" in css and "border-radius: 8px" in css
    blocks = []
    block = window.docs_view.document().begin()
    while block.isValid():
        blocks.append(block)
        block = block.next()
    code_table = next(
        frame for frame in window.docs_view.document().rootFrame().childFrames()
        if isinstance(frame, QTextTable)
    )
    assert code_table.frameFormat().background().color().name() == "#e7eef8"
    assert code_table.frameFormat().border() == 1
    paragraph = next(block for block in blocks if block.text() == "First paragraph.")
    assert paragraph.blockFormat().bottomMargin() == 13
    window.close()


def test_history_shows_no_verdict_and_unavailable_schema_distinctly(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(history_loader=lambda: [
        {"paper": "partial.pdf", "available": True, "verification_counts": {"total": 0, "open": 0, "outcomes": {}}},
        {"paper": "old.pdf", "available": False, "unavailable_reason": "incompatible_schema"},
        {"paper": "broken.pdf", "available": False, "error": "corrupt"},
    ])
    assert window.history_table.item(0, 2).text() == "Semantic verification not performed"
    assert "incompatible schema" in window.history_table.item(1, 2).text()
    assert window.history_table.item(2, 2).text() == "Unavailable"
    assert window.history_table.columnCount() == 6
    assert window.findChild(type(window.history_table)).horizontalScrollBar() is not None
    window.close()
    assert app is not None


def test_history_resume_is_disabled_and_guarded_for_non_resumable_runs(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
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
        window.history_table.item(index, 0).setCheckState(Qt.CheckState.Checked)
        app.processEvents()
        assert not window.history_resume_button.isEnabled()
        window._resume_selected_history()
        window.history_table.item(index, 0).setCheckState(Qt.CheckState.Unchecked)
    assert resumed == []
    assert started == []

    window.history_table.item(3, 0).setCheckState(Qt.CheckState.Checked)
    app.processEvents()
    assert window.history_resume_button.isEnabled()
    window._resume_selected_history()
    assert resumed == ["paused"]
    assert started == [["resume", "paused"]]
    window.close()


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


@pytest.mark.parametrize("llm_available", [True, False])
def test_completed_reference_preview_requires_explicit_history_fork(
    monkeypatch, llm_available
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt, QProcess
    from PySide6.QtWidgets import QApplication, QWidget
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    row = {
        "paper": "preview.pdf",
        "run_dir": "parent",
        "available": True,
        "run_status": "completed",
        "phase": "done",
        "completed": True,
        "references_only": True,
    }
    started = []
    guided = {}
    child_snapshot = {}
    expected_environment = (
        {"OPENAI_MODEL": "deepseek-flash"} if llm_available else {}
    )

    def snapshot(run):
        if run == "parent":
            return {"phase": "done", "references_only": True, "references": [{
                "ref_id": "r1", "title": "Existing source", "phase": "resolve",
                "status": "completato",
            }]}
        if run == "child":
            if not child_snapshot:
                raise OSError("child database is not ready")
            return child_snapshot
        return {}

    def guided_window(_run_dir, **callbacks):
        guided.update(callbacks)
        return QWidget()

    window = create_desktop_window(
        history_loader=lambda: [row],
        resume_command_builder=lambda run: (
            {
                "command": ["callimachus", "--fork-reference-only-fetch", "parent"],
                "run_dir": "child",
            }
            if run == "parent"
            else ["callimachus", "--resume", run]
        ),
        snapshot_loader=snapshot,
        verify_candidates_loader=lambda: [{
            "selector": "openai_compatible:deepseek-flash",
            "backend": "openai_compatible",
            "model": "deepseek-flash",
            "label": "DeepSeek Flash",
            "configured": True,
        }],
        verify_selection_preview_loader=lambda _selected, _level: {
            "available": llm_available,
            "overlay": {"OPENAI_MODEL": "deepseek-flash"},
        },
        verify_fork_options_loader=lambda: [{
            "selector": "openai_compatible:deepseek-flash",
            "label": "DeepSeek Flash",
        }],
        verify_fork_selector=lambda options: [options[0]["selector"]],
        verify_fork_command_builder=lambda _row, _lanes: {
            "command": ["callimachus", "--fork-reference-only-fetch", "parent"],
            "run_dir": "child",
            "environment": expected_environment,
        },
        guided_window_factory=guided_window,
        guided_controller_factory=lambda _run: SimpleNamespace(
            proceed=lambda: None,
            stage_source=lambda *_args: None,
            discard_source=lambda *_args: None,
            submit_identity_decision=lambda *_args: None,
            identity_review_allowed=lambda *_args: False,
        ),
    )
    window._start_process = lambda command, **kwargs: started.append(
        (command, kwargs)
    )
    assert not window.resume_latest_button.isEnabled()
    window.navigation.setCurrentRow(1)
    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    app.processEvents()
    assert not window.history_resume_button.isEnabled()
    assert window.history_verify_fork_button.isEnabled()
    window._fork_selected_history_verify()
    assert window._run_dir == "child"
    assert window.navigation.currentRow() == 0
    assert window.source_table.rowCount() == 1
    assert window.source_table.item(0, 1).text() == "Existing source"
    child_snapshot.update({"phase": "fetch", "references": []})
    window._refresh_snapshot()
    assert window.source_table.rowCount() == 1
    assert window.source_table.item(0, 1).text() == "Existing source"
    child_snapshot["references"] = [{
        "ref_id": "r1", "title": "Child source", "phase": "fetch",
        "status": "in_corso",
    }]
    window._refresh_snapshot()
    assert window.source_table.item(0, 1).text() == "Child source"
    assert started == [(
        ["callimachus", "--fork-reference-only-fetch", "parent"],
        {"environment": expected_environment},
    )]

    window.navigation.setCurrentRow(1)
    window._resume_selected_history()
    assert len(started) == 1
    assert window._run_dir == "child"
    assert window.navigation.currentRow() == 1
    child_snapshot.update({"phase": "fetch", "fetch_paused": True})
    window._on_process_finished(10, QProcess.ExitStatus.NormalExit)
    assert "proceed" in guided
    guided["proceed"]()
    assert started[-1] == (
        ["callimachus", "--resume", "child"],
        {"environment": expected_environment},
    )
    window.close()


def test_stale_active_run_is_offered_for_crash_recovery(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    row = {
        "paper": "crashed.pdf",
        "run_dir": "crashed",
        "available": True,
        "run_status": "active",
        "crash_recoverable": True,
    }
    started = []
    window = create_desktop_window(
        history_loader=lambda: [row],
        resume_command_builder=lambda run: ["resume", run],
        snapshot_loader=lambda _run: {
            "phase": "resolve",
            "phase_progress": 30,
            "references": [
                {
                    "ref_id": "ref-1",
                    "title": "Recovered source",
                    "phase": "resolve",
                    "status": "in_corso",
                }
            ],
        },
    )
    window._start_process = lambda command, **kwargs: started.append(
        (command, kwargs)
    )

    assert (
        window.history_table.item(0, 0).flags()
        & Qt.ItemFlag.ItemIsUserCheckable
    )
    assert window.resume_latest_button.isEnabled()
    assert "Recover and resume" in window.resume_latest_button.toolTip()
    window._spinner_frame = 11
    window._resume_latest_or_stop()
    assert started == [(["resume", "crashed"], {"environment": {}})]
    assert window.source_table.rowCount() == 1
    assert "Running" in window.source_table.item(0, 3).text()
    window.close()
    assert app is not None


def test_analysis_controls_resume_latest_and_switch_to_cooperative_stop(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    rows = [{"paper": "paused", "run_dir": "paused", "available": True, "run_status": "paused"}]
    window = create_desktop_window(
        history_loader=lambda: rows,
        resume_command_builder=lambda run: ["resume", run],
        resume_callback=lambda _row: None,
    )
    started = []
    window._start_process = lambda command, **kwargs: started.append((command, kwargs))
    assert window.resume_latest_button.isEnabled()
    window._resume_latest_or_stop()
    assert started == [(["resume", "paused"], {"environment": {}})]

    writes = []
    window._process = type("Process", (), {"write": lambda _self, value: writes.append(value)})()
    window._update_run_controls()
    assert window.start_button.text() == ""
    assert not window.start_button.icon().isNull()
    assert window.resume_latest_button.text() == "Stop"
    window._resume_latest_or_stop()
    assert writes == [b"STOP\n"]
    assert window.resume_latest_button.text() == "Stopping…"
    window._process = None
    window.close()
    assert app is not None


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


def test_new_analysis_stop_clears_without_waiting_for_background_snapshot(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(background_loading=True, latest_loader=lambda: None)
    window._run_dir = "active-run"
    window._set_paper("active-manuscript.pdf")
    process, actions = _desktop_stop_process_stub(QtCore.QProcess)
    window._process = process
    window._update_run_controls()

    window.new_analysis_button.click()
    assert actions["writes"] == [b"STOP\n"]
    assert window._run_dir == "active-run"
    window._request_snapshot = lambda **_kwargs: pytest.fail("obsolete snapshot requested")
    window._on_process_finished(130, QtCore.QProcess.ExitStatus.NormalExit)

    assert window._run_dir is None
    assert window._paper_path == ""
    assert window._pending_finish is None
    assert not window._new_analysis_reset_pending
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
    window.close()
    assert app is not None


def test_desktop_normal_finish_cancels_stop_escalation(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window, load_translations

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window()
    process, actions = _desktop_stop_process_stub(QtCore.QProcess)
    window._process = process
    window._resume_latest_or_stop()
    timer = window._stop_escalation_timer
    assert timer is not None

    window._on_process_finished(130, QtCore.QProcess.ExitStatus.NormalExit)
    timer.timeout.emit()

    assert window._stop_escalation_timer is None
    assert not timer.isActive()
    assert actions["kills"] == 0
    assert window.phase_label.text() == load_translations()["process_interrupted"]
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


def test_active_start_and_stop_buttons_share_geometry_at_larger_font(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(snapshot_loader=lambda _run: {})
    window._font_size = 18
    window._apply_theme(False)
    window._process = object()
    window._update_run_controls()
    window.resize(960, 640)
    window.show()
    app.processEvents()

    start = window.start_button
    stop = window.resume_latest_button
    assert start.text() == ""
    assert not start.isEnabled()
    assert stop.text() == "Stop"
    # Qt assigns the odd spare pixel to one of two equally stretched buttons.
    assert abs(start.width() - stop.width()) <= 1
    assert start.height() == stop.height()
    assert start.minimumHeight() == stop.minimumHeight()
    assert start.maximumHeight() > start.height()
    assert stop.geometry().right() < stop.parentWidget().width()
    window._process = None
    window.close()
    assert app is not None


def test_history_selection_resume_target_brand_spinner_and_activity(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel
    from core.gui.desktop import _stylesheet, create_desktop_window

    app = QApplication.instance() or QApplication([])
    rows = [
        {"paper": "unavailable", "run_dir": "missing", "available": False},
        {"paper": "interrupted", "run_dir": "paused", "available": True, "run_status": "interrupted"},
        {"paper": "active", "run_dir": "active", "available": True, "run_status": "active"},
    ]
    window = create_desktop_window(
        history_loader=lambda: rows,
        resume_command_builder=lambda run: ["resume", run],
        snapshot_loader=lambda _run: {"phase": "resolve", "phase_progress": 30},
    )
    window._run_dir = "paused"
    window._refresh_static_tabs()
    paused_item = window.history_table.item(1, 0)
    active_item = window.history_table.item(2, 0)
    assert paused_item.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert not active_item.flags() & Qt.ItemFlag.ItemIsUserCheckable
    paused_item.setCheckState(Qt.CheckState.Checked)
    assert window.history_select_all.checkState() == Qt.CheckState.Unchecked
    assert not window.history_select_all.isTristate()
    window.history_select_all.setCheckState(Qt.CheckState.Checked)
    assert window._history_checked_run_dirs == {"missing", "paused"}
    assert window.history_delete_button.isEnabled()
    assert not window.history_resume_button.isEnabled()
    assert not window.resume_latest_button.isEnabled()
    assert "unavailable" in window.resume_latest_button.toolTip().lower()

    started = []
    window._start_process = lambda command, **kwargs: started.append((command, kwargs))
    window._resume_latest_or_stop()
    assert started == []
    window.history_select_all.setCheckState(Qt.CheckState.Unchecked)
    paused_item.setCheckState(Qt.CheckState.Checked)
    window._resume_selected_history()
    assert started == [(["resume", "paused"], {"environment": {}})]
    brand_logo = window.findChild(QLabel, "brandLogo")
    assert brand_logo is not None
    assert brand_logo.pixmap() is not None and not brand_logo.pixmap().isNull()

    class Output:
        def readAllStandardOutput(self):
            return b"Resolve DOI token=secret-value for source\n"

        def readAllStandardError(self):
            return b""

    window._process = Output()
    window._update_run_controls()
    phase_before, progress_before = window.phase_label.text(), window.phase_progress.value()
    window._read_output()
    assert "secret-value" not in window.current_activity.text()
    assert "[redacted]" in window.current_activity.text()
    assert (window.phase_label.text(), window.phase_progress.value()) == (phase_before, progress_before)
    assert window.start_button.text() == "" and not window.start_button.icon().isNull()
    assert window.start_button.iconSize().width() == 24
    window._process = None
    window._update_run_controls()
    assert window.start_button.text() == "Start analysis" and window.start_button.icon().isNull()
    assert "QScrollBar:horizontal" in _stylesheet(dark=False)
    window.close()
    assert app is not None


def test_desktop_shows_live_provider_cooldown_separately_from_activity(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import _cooldown_seconds, create_desktop_window

    assert _cooldown_seconds(
        "Semantic Scholar cooldown: reference 12 deferred; retrying in 17s"
    ) == 17
    assert _cooldown_seconds("Processing reference 12") is None

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(history_loader=lambda: [])

    class Output:
        def readAllStandardOutput(self):
            return (
                b"[Fetch] reference [12] waiting 19s for example.org cooldown "
                b"token=secret-value\nProcessing reference 12\n"
            )

        def readAllStandardError(self):
            return b""

    window._process = Output()
    window._read_output()
    assert "Processing reference 12" in window.current_activity.text()
    assert "19s" in window.cooldown_status.text()
    assert "[redacted]" in window.cooldown_status.text()
    assert "secret-value" not in window.cooldown_status.text()
    assert not window.cooldown_status.isHidden()

    import time
    window._cooldown_until = time.monotonic() - 1
    window._update_cooldown_status()
    assert window.cooldown_status.isHidden()
    window._process = None
    window.close()
    assert app is not None


def test_history_and_cache_checkbox_cells_toggle_without_tristate(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(
        history_loader=lambda: [
            {"paper": "one", "run_dir": "one", "available": True, "run_status": "completed"},
            {"paper": "two", "run_dir": "two", "available": True, "run_status": "completed"},
        ],
        cache_loader=lambda: [
            {"title": "one", "ref_id": "one", "tier": "fulltext"},
            {"title": "two", "ref_id": "two", "tier": "abstract"},
        ],
    )
    window._remember_history_checkbox_state(0, 0)
    window._toggle_history_checkbox_cell(0, 0)
    assert window.history_table.item(0, 0).checkState() == Qt.CheckState.Checked
    assert window.history_select_all.checkState() == Qt.CheckState.Unchecked
    assert not window.history_select_all.isTristate()
    window.history_select_all.click()
    assert window._history_checked_run_dirs == {"one", "two"}
    window.history_select_all.click()
    assert not window._history_checked_run_dirs

    window._remember_cache_checkbox_state(0, 0)
    window._toggle_cache_checkbox_cell(0, 0)
    assert window.cache_table.item(0, 0).checkState() == Qt.CheckState.Checked
    assert window.cache_select_all.checkState() == Qt.CheckState.Unchecked
    assert not window.cache_select_all.isTristate()
    window.cache_select_all.click()
    assert len(window._checked_cache_rows()) == 2
    window.cache_select_all.click()
    assert not window._checked_cache_rows()
    window.close()
    assert app is not None


def test_table_checkbox_cells_toggle_on_real_clicks_outside_indicator(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(
        verify_candidates_loader=lambda: [
            {"selector": "a", "label": "A", "configured": True},
            {"selector": "b", "label": "B", "configured": True},
        ],
        history_loader=lambda: [
            {"paper": "one", "run_dir": "one", "available": True,
             "run_status": "completed"},
        ],
        cache_loader=lambda: [
            {"title": "one", "ref_id": "one", "tier": "fulltext"},
        ],
    )
    window.show()
    app.processEvents()

    for column, checkbox in enumerate(window.llm_role_choices["a"], start=1):
        cell = window.llm_choices.cellWidget(0, column)
        assert cell is checkbox.parentWidget()
        assert abs((checkbox.x() + checkbox.width() / 2) - cell.width() / 2) <= 2
        assert abs((checkbox.y() + checkbox.height() / 2) - cell.height() / 2) <= 2
        assert cell.width() > checkbox.width() + 4
        QTest.mouseClick(cell, Qt.MouseButton.LeftButton, pos=QPoint(2, cell.height() // 2))
        assert not checkbox.isChecked()
        QTest.mouseClick(checkbox, Qt.MouseButton.LeftButton)
        assert checkbox.isChecked()
        checkbox.setEnabled(False)
        QTest.mouseClick(cell, Qt.MouseButton.LeftButton, pos=QPoint(2, cell.height() // 2))
        assert checkbox.isChecked()
        checkbox.setEnabled(True)

    for page, table_name in ((1, "history_table"), (2, "cache_table")):
        window.navigation.setCurrentRow(page)
        app.processEvents()
        table = getattr(window, table_name)
        rect = table.visualRect(table.model().index(0, 0))
        assert rect.isValid()
        point = QPoint(rect.right() - 2, rect.center().y())
        item = table.item(0, 0)
        assert item.checkState() == Qt.CheckState.Unchecked
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert item.checkState() == Qt.CheckState.Checked
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert item.checkState() == Qt.CheckState.Unchecked
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        assert item.checkState() == Qt.CheckState.Checked
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        assert item.checkState() == Qt.CheckState.Unchecked
    window.close()


def test_verify_fork_checkbox_row_toggles_outside_indicator(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QListWidget
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window()
    observed = []

    def click_choice(dialog):
        dialog.show()
        app.processEvents()
        choices = dialog.findChild(QListWidget)
        item = choices.item(0)
        rect = choices.visualItemRect(item)
        blank_space = QPoint(rect.right() - 4, rect.center().y())
        for _ in range(2):
            QTest.mouseClick(choices.viewport(), Qt.MouseButton.LeftButton, pos=blank_space)
            observed.append(item.checkState())
        indicator = QPoint(rect.left() + 10, rect.center().y())
        for _ in range(2):
            QTest.mouseClick(choices.viewport(), Qt.MouseButton.LeftButton, pos=indicator)
            observed.append(item.checkState())
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", click_choice)
    selected = window._select_verify_fork_backends([{"selector": "a", "label": "Model A"}])
    assert observed == [
        Qt.CheckState.Unchecked, Qt.CheckState.Checked,
        Qt.CheckState.Unchecked, Qt.CheckState.Checked,
    ]
    assert selected == ["a"]
    window.close()


def test_cache_selection_keeps_duplicate_titles_separate(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    rows = [
        {"work_id": "work-1", "title": "Same title", "availability": "fulltext"},
        {"work_id": "work-2", "title": "Same title", "availability": "fulltext"},
    ]
    deleted = []
    window = create_desktop_window(
        cache_loader=lambda: rows,
        delete_cache_callback=lambda selected: deleted.append(list(selected)),
    )
    window.cache_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert window._checked_cache_rows() == [rows[0]]
    assert window.cache_table.item(1, 0).checkState() == Qt.CheckState.Unchecked
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    window._delete_selected_cache()
    assert deleted == [[rows[0]]]
    window.close()
    assert app is not None


def test_history_opens_report_or_run_folder_and_enables_only_html_report(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    opened = []
    rows = [
        {
            "paper": "with report", "run_dir": "/runs/with-report",
            "available": True, "run_status": "completed", "completed": True,
            "report_html_present": True, "report_html_path": "/runs/with-report/report.html",
        },
        {
            "paper": "without report", "run_dir": "/runs/no-report",
            "available": True, "run_status": "completed", "completed": True,
            "report_html_present": False,
        },
    ]
    window = create_desktop_window(
        history_loader=lambda: rows, open_path_callback=opened.append
    )
    with_report = window.history_table.item(0, 0)
    with_report.setCheckState(Qt.CheckState.Checked)
    assert window.history_open_report_button.isEnabled()
    window._open_selected_history_report()
    assert opened == ["/runs/with-report/report.html"]
    with_report.setCheckState(Qt.CheckState.Unchecked)
    window.history_table.selectRow(0)
    app.processEvents()
    assert window.history_open_report_button.isEnabled()
    window._open_selected_history_report()
    assert opened[-1] == "/runs/with-report/report.html"
    window.history_table.clearSelection()
    assert not window.history_open_report_button.isEnabled()
    window._open_history_row(1, 1)
    assert opened[-1] == "/runs/no-report"
    window.close()
    assert app is not None


def test_public_export_buttons_require_completed_verify(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
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

    def choose_export(_window, _title, default_path, _filter):
        suggested_paths.append(default_path)
        return "/exports/public.html", "HTML (*.html)"

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_export)
    monkeypatch.setattr(
        QMessageBox, "information", lambda *_args: QMessageBox.StandardButton.Ok,
    )
    window = create_desktop_window(
        language="en", history_loader=lambda: rows,
        snapshot_loader=lambda run_dir: {
            "phase": "done", "run_status": "completed",
            "references_only": run_dir == "/runs/preview",
        },
        export_report_callback=lambda run_dir, output: (
            exported.append((run_dir, output)) or output
        ),
    )
    window.set_run_dir("/runs/full")
    assert window.report_actions_layout.stretch(0) == 3
    assert window.report_actions_layout.stretch(1) == 1
    assert window.export_run_report_button.isEnabled()
    assert "redacted copy for publication" in window.export_run_report_button.toolTip()
    window.export_run_report_button.click()
    assert exported == [("/runs/full", "/exports/public.html")]
    assert suggested_paths == [str(Path("/runs/full").parent / "full.public.html")]

    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert window.history_export_report_button.isEnabled()
    window.history_export_report_button.click()
    assert exported[-1] == ("/runs/full", "/exports/public.html")
    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    window.history_table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    assert not window.history_export_report_button.isEnabled()
    window.set_run_dir("/runs/preview")
    assert not window.export_run_report_button.isEnabled()
    window.close()
    assert app is not None


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


def test_running_snapshot_applies_durable_phase_progress(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(snapshot_loader=lambda _run: {
        "phase": "fetch",
        "phase_progress": 52,
        "progress_counts": {"sources_total": 2, "fetch_completed": 1},
    })
    window.set_run_dir(str(tmp_path))
    assert window.phase_progress.value() == 52
    assert window.phase_progress.maximum() == 100
    assert window.phase_progress.text() == "Fetch: 1/2 · 52%"
    window.close()
    assert app is not None


def test_references_only_completion_explains_skipped_fetch_and_verify(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(snapshot_loader=lambda _run: {
        "phase": "done", "run_status": "completed", "phase_progress": 100,
        "references_only": True,
    })
    window.set_run_dir(str(tmp_path))
    assert "Fetch and Verify were not run" in window.phase_label.text()
    assert window.phase_progress.text() == "References only · 100%"
    window.current_activity.setText("DONE · report: C:/a/very/long/path/report.html")
    window._on_process_finished(0, QProcess.ExitStatus.NormalExit)
    assert window.current_activity.text() == "Current activity: Reference-only report ready"
    window.close()
    assert app is not None


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
    window._process = object()
    window._update_run_controls()
    assert not window.start_button.isEnabled()
    window._on_process_finished(10, 0)

    assert opened == []
    assert window.phase_label.text() == load_translations()["snapshot_unavailable"]
    assert window.start_button.isEnabled()
    window.close()
    assert app is not None


def test_unknown_nonzero_exit_uses_a_plain_language_message(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window(snapshot_loader=lambda _run: {"phase": "verify"})
    window.set_run_dir(str(tmp_path))
    window._on_process_finished(7, 0)
    assert window.phase_label.text() == load_translations()["process_failed"].format(exit_code=7)
    assert window.phase_label.toolTip() == load_translations()["process_exit_detail"].format(exit_code=7)
    assert window.start_button.isEnabled()
    window.close()
    assert app is not None


@pytest.mark.parametrize(
    ("exit_code", "message_key"),
    [
        (1, "process_failed_startup"),
        (2, "process_failed_pipeline"),
        (3, "process_run_locked"),
        (20, "process_gate_failed"),
        (21, "process_report_untrusted"),
    ],
)
def test_known_exit_codes_are_explained_in_plain_language(
    monkeypatch, exit_code, message_key
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication
    from core.gui.desktop import create_desktop_window

    app = QApplication.instance() or QApplication([])
    window = create_desktop_window()
    window._on_process_finished(
        exit_code, QtCore.QProcess.ExitStatus.NormalExit
    )

    assert window.phase_label.text() == load_translations()[message_key]
    assert window.phase_label.toolTip() == load_translations()[
        "process_exit_detail"
    ].format(exit_code=exit_code)
    window.close()
    assert app is not None


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
    assert window.phase_label.toolTip() == load_translations()[
        "process_exit_detail"
    ].format(exit_code=2)
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
    window.navigation.setCurrentRow(1)
    window.history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
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
