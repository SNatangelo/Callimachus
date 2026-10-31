# tests/test_atlante_gui.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Regression checks for approved Atlante navigation and data lifetime."""
from __future__ import annotations

import threading
import time

import pytest

from core.gui.atlante import filtered_source_page, matches_source, settings_category
















def test_operational_resume_targets_displayed_run_and_keeps_latest_on_startup(qt):
    from core.gui.desktop import create_desktop_window
    window = create_desktop_window(history_loader=lambda: [{"run_dir":"other-latest", "run_status":"paused"}])
    resumed = []
    window._resume_run_row = lambda row: resumed.append(row["run_dir"])
    try:
        window._run_dir = "displayed-run"
        window._update_run_controls()
        window._atlas_runtime_resume.click()
        assert resumed == ["displayed-run"]
        window._run_dir = None
        window._update_run_controls()
        window.resume_latest_button.click()
        assert resumed == ["displayed-run", "other-latest"]
    finally:
        window.close()


@pytest.fixture
def qt(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings_class = QtCore.QSettings
    def settings_factory(*_a, **_k):
        return settings_class(str(tmp_path / "preferences.ini"), settings_class.Format.IniFormat)
    settings_factory.Status = settings_class.Status
    monkeypatch.setattr(QtCore, "QSettings", settings_factory)
    yield application


def _until(app, predicate):
    deadline = time.monotonic()+3
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert predicate()






















def test_runtime_action_layout_items_are_invalidated_when_new_analysis_moves(qt):
    import shiboken6
    from core.gui.desktop import create_desktop_window, load_translations
    from core.gui.atlante import refresh_source_filters, update_analysis
    from PySide6 import QtWidgets

    window = create_desktop_window()
    window._run_dir = "fixture-run"
    button = window.new_analysis_button
    actions = window._atlas_runtime_actions
    footer = window._atlas_runtime_footer_layout
    text = load_translations("en")
    try:
        assert footer.indexOf(button) >= 0
        old_item = footer.itemAt(footer.indexOf(button))
        window._current_report_path = lambda: "fixture-run/report.html"
        update_analysis(window, text)

        assert not shiboken6.isValid(old_item)
        assert footer.indexOf(button) == -1
        assert actions.indexOf(button) >= 0
        assert shiboken6.isValid(button)
        assert button.parentWidget() is actions.parentWidget()
        refresh_source_filters(window, QtWidgets, text)
        assert set(window._atlas_filter_actions) == {
            "supports", "partial", "contradicts", "related", "off_topic",
            "non_decidable", "technical",
        }

        old_item = actions.itemAt(actions.indexOf(button))
        window._current_report_path = lambda: None
        update_analysis(window, text)

        assert not shiboken6.isValid(old_item)
        assert actions.indexOf(button) == -1
        assert footer.indexOf(button) >= 0
        assert shiboken6.isValid(button)
        assert button.parentWidget() is footer.parentWidget()
        refresh_source_filters(window, QtWidgets, text)
        assert set(window._atlas_filter_actions) == {
            "fulltext", "abstract", "none", "identity",
        }
    finally:
        window.close()


def test_install_analysis_invalidates_moved_legacy_card_items(qt, monkeypatch):
    import shiboken6
    from core.gui import atlante
    from core.gui.desktop import create_desktop_window

    original_install_analysis = atlante.install_analysis

    def capture_legacy_items(window, tab, source_pane, QtCore, QtWidgets, text):
        legacy_card = window.paper_drop.parentWidget()
        layouts = [legacy_card.layout(), *legacy_card.findChildren(QtWidgets.QLayout)]
        borrowed = []
        for layout in layouts:
            if layout is None:
                continue
            for index in range(layout.count()):
                item = layout.itemAt(index)
                widget = item.widget()
                if widget is not None:
                    borrowed.append((item, widget, widget.parentWidget()))

        original_install_analysis(
            window, tab, source_pane, QtCore, QtWidgets, text
        )

        moved_items = [
            item
            for item, widget, old_parent in borrowed
            if widget.parentWidget() is not old_parent
        ]
        assert moved_items
        assert all(not shiboken6.isValid(item) for item in moved_items)

    monkeypatch.setattr(atlante, "install_analysis", capture_legacy_items)
    window = create_desktop_window()
    window.close()








def test_library_pages_are_bounded_and_late_results_cannot_repopulate_departed_view(qt):
    from core.gui.desktop import create_desktop_window
    release, entered = threading.Event(), threading.Event()
    calls = []
    def cache_page(offset, limit, **kwargs):
        calls.append((offset, limit, kwargs))
        entered.set()
        release.wait(2)
        return {"rows":[{"work_id":"work-a","title":"Article","availability":"fulltext"}],
                "total":100,"has_more":True,"offset":offset}
    window = create_desktop_window(background_loading=True, cache_page_loader=cache_page,
                                   cache_loader=lambda: pytest.fail("unbounded loader used"))
    window.show()
    try:
        window.navigation.setCurrentRow(2)
        _until(qt, entered.is_set)
        assert len(calls) == 1 and calls[0][1] <= 80
        assert window.cache_stack.currentIndex() == 1
        window.navigation.setCurrentRow(1)
        assert window._cache_rows == []
        release.set()
        for _ in range(20):
            qt.processEvents(); time.sleep(.005)
        assert window._cache_rows == [] and window.cache_table.rowCount() == 0
    finally:
        release.set(); window.close()










def test_failed_library_page_leaves_loading_and_offers_retry(qt):
    from core.gui.desktop import create_desktop_window
    attempts = []
    def fail_page(*_args, **_kwargs):
        attempts.append(True)
        raise RuntimeError("fixture page failure")
    window = create_desktop_window(background_loading=True, cache_page_loader=fail_page)
    window.show()
    try:
        window.navigation.setCurrentRow(2)
        _until(qt, lambda: bool(attempts) and "cache_page" not in window._loading_kinds)
        assert window.cache_stack.currentIndex() == 0
        assert window.cache_more_button.isEnabled() and window.cache_more_button.isVisible()
        assert "fixture page failure" in window.cache_table.item(0,1).text()
    finally:
        window.close()


def test_provider_save_shares_configuration_without_selecting_other_jury(qt):
    from core.app.settings.config import load_settings_inventory
    from core.gui.desktop import create_desktop_window
    environment, saved = {}, []
    def save(updates):
        saved.append(dict(updates))
        environment.update(updates)
    window = create_desktop_window(
        settings_loader=lambda: load_settings_inventory(env_path=None, template_path=".env.example", environ=environment),
        settings_saver=save,
        verify_candidates_loader=lambda: [{"provider":"freetoken","selector":"freetoken:fixture-model",
                                            "label":"Fixture model","configured":False}],
    )
    try:
        window.llm_role_choices["freetoken:fixture-model"][0].setChecked(True)
        selected = window._selected_llm_roles()
        assert selected == {"jury1":["freetoken:fixture-model"],"jury2":[]}
        window._show_jury_models("jury1")
        window._atlante_jury_chooser.show_provider("openai")
        window._stage_secret("OPENAI_API_KEY", "fixture-noncredential")
        window._stage_setting("OPENAI_BASE_URL", "https://endpoint.example/v1")
        assert window._save_settings()
        window._show_jury_models("jury2")
        window._atlante_jury_chooser.show_provider("openai")
        assert environment["OPENAI_BASE_URL"] == "https://endpoint.example/v1"
        assert window._selected_llm_roles() == selected
        assert window._settings_editors["OPENAI_BASE_URL"].currentText() == environment["OPENAI_BASE_URL"]
        assert window._secret_editors["OPENAI_API_KEY"].isReadOnly()
        assert "fixture-noncredential" not in window.llm_picker_status.text()
    finally:
        window.close()


def test_a_discovered_single_model_does_not_select_itself(qt):
    from core.gui.desktop import create_desktop_window
    window = create_desktop_window(verify_candidates_loader=lambda: [{
        "selector":"openai_compatible:fixture-flash", "label":"Fixture", "configured":False}])
    try:
        assert window._selected_llm_roles() == {"jury1":[],"jury2":[]}
    finally:
        window.close()




def test_options_apply_commits_staged_values_and_cancel_keeps_current_values(qt):
    from core.gui.desktop import create_desktop_window
    from PySide6 import QtWidgets
    window = create_desktop_window(language="en")
    try:
        initial = [button.isChecked() for button in window.jury_buttons]
        changed = next(index for index, checked in enumerate(initial) if not checked)
        window._atlas_options_button.click()
        controls = window._atlas_options_controls
        controls["severity"][changed].setChecked(True)
        controls["parse"].setChecked(False)
        assert [button.isChecked() for button in window.jury_buttons] == initial
        buttons = window._atlas_options_drawer.findChildren(QtWidgets.QPushButton)
        next(button for button in buttons if button.text() == "Cancel").click()
        assert [button.isChecked() for button in window.jury_buttons] == initial
        assert not window.skip_parse_review.isChecked()
        window._atlas_options_button.click()
        controls["severity"][changed].setChecked(True)
        controls["parse"].setChecked(False)
        next(button for button in buttons if button.text() == "Apply").click()
        assert window.jury_buttons[changed].isChecked()
        assert window.skip_parse_review.isChecked()
    finally:
        window.close()




def test_leaving_analysis_closes_details_and_discards_pending_full_content(qt):
    from core.gui.desktop import create_desktop_window
    from PySide6 import QtCore, QtWidgets
    entered, release = threading.Event(), threading.Event()
    def details(*_args):
        entered.set()
        release.wait(2)
        return {"text":"Full source content"}
    window = create_desktop_window(background_loading=True, source_detail_loader=details)
    window.show()
    try:
        window._run_dir = "fixture-run"
        window.source_table.setRowCount(1)
        item = QtWidgets.QTableWidgetItem("1")
        item.setData(QtCore.Qt.ItemDataRole.UserRole, {"ref_id":"ref-a", "title":"Article"})
        window.source_table.setItem(0, 0, item)
        window._show_source_details(0, 0)
        _until(qt, entered.is_set)
        assert window._detail_dialog is not None
        window.navigation.setCurrentRow(2)
        assert window._detail_dialog is None
        assert "detail" not in window._loading_kinds
        release.set()
        for _ in range(20):
            qt.processEvents(); time.sleep(.005)
        assert window._detail_dialog is None
    finally:
        release.set()
        window.close()












