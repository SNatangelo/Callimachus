from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from core.gui.desktop import load_translations


def test_desktop_translations_are_external_and_italian_by_default():
    assert load_translations()["tab_analysis"] == "Analisi"
    assert load_translations("en")["tab_history"] == "History"


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
        snapshot_loader=lambda _run: snapshot,
        resume_command_builder=lambda run: ["python", "run.py", "--run", run, "--resume"],
        history_loader=lambda: [{"paper": "old.pdf", "run_dir": "old"}],
        cache_loader=lambda: [{"title": "Cached", "tier": "fulltext"}],
        settings_loader=lambda: {"OPENAI_API_KEY": "secret", "MODE": "standard"},
        docs_loader=lambda: "Local docs",
        guided_controller_factory=lambda _run: controller,
        guided_window_factory=factory,
    )
    assert [window.tabs.tabText(index) for index in range(window.tabs.count())] == ["Analisi", "Cronologia", "Cache", "Impostazioni", "Info"]
    assert set(window.mode_buttons) == {"maximum", "abstract", "standard", "standard_web"}
    assert window.source_filter.count() == 4
    assert window.cache_filter.count() == 4
    assert window.source_table.columnCount() == 8
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
