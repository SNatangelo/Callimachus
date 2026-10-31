# tests/test_atlante_jury.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_desktop_gui_optional import _isolated_gui_preferences  # noqa: F401

from core.gui.jury_chooser import (
    JuryChooser,
    _DIRECTORY,
    _QUICK,
    _advanced_row,
    _provider_configuration_row,
    canonical_provider,
)
















def _window():
    from PySide6 import QtCore, QtGui, QtWidgets

    dialog = QtWidgets.QDialog()
    layout = QtWidgets.QVBoxLayout(dialog)
    picker_status = QtWidgets.QLabel("Choose a provider")
    llm_status = QtWidgets.QLabel("No provider")
    tree = QtWidgets.QTreeWidget()
    tree.setColumnCount(3)
    group = QtWidgets.QTreeWidgetItem(["Codex CLI"])
    group.setData(0, QtCore.Qt.ItemDataRole.UserRole, "codex_cli")
    tree.addTopLevelItem(group)
    model = QtWidgets.QTreeWidgetItem(group, ["codex-cli:model-a"])
    model.setData(0, QtCore.Qt.ItemDataRole.UserRole, "codex_cli")
    check = QtWidgets.QPushButton("Check")
    actions = QtWidgets.QHBoxLayout()
    actions.addWidget(check)
    actions.addStretch()
    close = QtWidgets.QPushButton("Close")
    actions.addWidget(close)
    layout.addWidget(picker_status)
    layout.addWidget(llm_status)
    layout.addWidget(tree)
    layout.addLayout(actions)
    selected = {"jury1": ["codex-cli:model-a"], "jury2": []}
    jury_buttons = []
    jury_button_group = QtWidgets.QButtonGroup(dialog)
    jury_button_group.setExclusive(True)
    for level in ("low", "medium", "high"):
        button = QtWidgets.QPushButton(level.title(), dialog)
        button.setCheckable(True)
        button.setProperty("jury2_level", level)
        button.setChecked(level == "medium")
        jury_button_group.addButton(button)
        jury_buttons.append(button)
    rows = [
        {
            "name": "OPENAI_API_KEY", "label": "API key", "group": "models",
            "provider": "OpenAI / compatible", "check_provider": "openai",
            "control": "secret", "value": "", "configured": False,
        },
        {
            "name": "CITATION_VERIFIER_MAX_SOURCE_CHARS", "label": "Maximum source text",
            "group": "advanced", "control": "integer", "value": "20000",
            "configured": True,
        },
        {
            "name": "CITATION_VERIFIER_ACCURACY", "label": "Verification mode",
            "group": "advanced", "control": "enum", "value": "standard",
            "choices": [{"value": "standard", "label": "Standard"}],
            "configured": True,
        },
        {
            "name": "CITATION_VERIFIER_VERIFY_CONTEXT_MODE", "label": "Verification context mode",
            "group": "advanced", "control": "enum", "value": "auto",
            "choices": [{"value": "auto", "label": "Automatic"}],
            "configured": True,
        },
        {
            "name": "CITATION_VERIFIER_REASONING", "label": "Reasoning",
            "group": "advanced", "control": "enum", "value": "on",
            "choices": [{"value": "on", "label": "On"}],
            "configured": True,
        },
        {
            "name": "CITATION_VERIFIER_REASONING_EFFORT", "label": "Reasoning effort",
            "group": "advanced", "control": "enum", "value": "medium",
            "choices": [{"value": "medium", "label": "Medium"}],
            "configured": True,
        },
        {
            "name": "CITATION_VERIFIER_VERIFY_JURY2_LEVEL", "label": "Second-judge policy",
            "group": "advanced", "control": "enum", "value": "medium",
            "choices": [
                {"value": "off", "label": "Off"},
                {"value": "low", "label": "Low"},
                {"value": "medium", "label": "Medium"},
                {"value": "high", "label": "High"},
            ],
            "configured": True,
        },
    ]
    editors = {}
    dirty = {}
    initial = {row["name"]: str(row.get("value") or "") for row in rows}
    saved_settings = {}

    def stage_setting(name, value):
        if value == initial.get(name, ""):
            dirty.pop(name, None)
        else:
            dirty[name] = value

    def add_setting_editor(form, parent, row):
        name = row["name"]
        if row.get("control") == "enum":
            editor = QtWidgets.QComboBox(parent)
            for choice in row.get("choices", ()):
                if isinstance(choice, dict):
                    editor.addItem(choice["label"], choice["value"])
            index = editor.findData(row.get("value", ""))
            if index >= 0:
                editor.setCurrentIndex(index)
            editor.currentIndexChanged.connect(
                lambda _index, key=name, item=editor: stage_setting(key, str(item.currentData()))
            )
        else:
            editor = QtWidgets.QLineEdit(parent)
            editor.setText(row.get("value", ""))
            editor.textEdited.connect(lambda value, key=name: stage_setting(key, value))
        window._settings_editors[name] = editor
        form.addRow(row.get("label", name), editor)

    def save_settings():
        saved_settings.update(dirty)
        initial.update(dirty)
        for row in rows:
            if row["name"] in dirty:
                row["value"] = dirty[row["name"]]
        dirty.clear()
        return True

    window = SimpleNamespace(
        llm_choice_dialog=dialog,
        llm_picker_status=picker_status,
        llm_status=llm_status,
        llm_choices=tree,
        llm_picker_check=check,
        llm_discovery_status=None,
        llm_discovery_details=None,
        _llm_picker_role="jury1",
        jury_buttons=jury_buttons,
        _selected_jury_level=lambda: next(
            (str(button.property("jury2_level")) for button in jury_buttons if button.isChecked()),
            "medium",
        ),
        _llm_option_labels={"codex-cli:model-a": "codex-cli:model-a"},
        _selected_llm_roles=lambda: selected,
        _llm_provider_name=lambda value: value,
        _settings_rows=rows,
        _settings_dirty=dirty,
        _settings_initial=initial,
        _settings_editors=editors,
        _secret_editors={},
        _provider_model_editors={},
        _add_setting_editor=add_setting_editor,
        _settings_category=lambda row: row.get("group"),
        _save_settings=save_settings,
        _stage_setting=stage_setting,
        style=lambda: QtWidgets.QApplication.style(),
        _refresh_jury_picker_check=lambda: None,
    )
    return window, QtCore, QtGui, QtWidgets, selected, group, saved_settings


def _wide_parent_for_dialog(window, QtCore, QtWidgets):
    """Give the fixture dialog a real wide host for geometry assertions."""
    parent = QtWidgets.QMainWindow()
    parent.setCentralWidget(QtWidgets.QWidget(parent))
    parent.resize(1400, 900)
    window.centralWidget = parent.centralWidget
    window.contentsRect = parent.contentsRect
    window.mapToGlobal = parent.mapToGlobal
    window.llm_choice_dialog.setParent(parent, QtCore.Qt.WindowType.Dialog)
    return parent




@pytest.mark.parametrize("locale,expected_tooltip", [
    ("en", "Available only for Jury 2"),
    ("it", "Disponibile solo per Jury 2"),
])
def test_typesafe_is_disabled_for_jury1_and_available_for_jury2(
    monkeypatch, locale, expected_tooltip,
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtGui, QtWidgets
    from core.gui.jury_chooser import install_jury_chooser
    import json
    from pathlib import Path

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window, QtCore, QtGui, QtWidgets, selected, _group, _saved = _window()
    selected["jury2"] = ["typesafe:jev-1.0.0"]
    locale_data = json.loads(
        (Path(__file__).resolve().parents[1] / "core/gui/assets/locales" / f"{locale}.json")
        .read_text(encoding="utf-8")
    )
    tooltip = locale_data["jury_provider_jury2_only"]
    assert tooltip == expected_tooltip
    chooser = install_jury_chooser(
        window, QtCore, QtGui, QtWidgets,
        {"jury_provider_jury2_only": tooltip},
    )
    chooser.added_providers = ["typesafe"]
    chooser._rebuild_navigation()
    directory_item = next(
        chooser.directory_list.item(index)
        for index in range(chooser.directory_list.count())
        if chooser.directory_list.item(index).data(QtCore.Qt.ItemDataRole.UserRole) == "typesafe"
    )
    provider_button = chooser._provider_buttons["typesafe"]

    enabled = QtCore.Qt.ItemFlag.ItemIsEnabled
    selectable = QtCore.Qt.ItemFlag.ItemIsSelectable
    assert not directory_item.flags() & enabled
    assert not directory_item.flags() & selectable
    assert directory_item.toolTip() == tooltip
    assert directory_item.foreground().color() == QtGui.QColor("#7180a4")
    assert chooser.directory_list.testAttribute(
        QtCore.Qt.WidgetAttribute.WA_AlwaysShowToolTips
    )
    assert not provider_button.isEnabled()
    assert provider_button.toolTip() == tooltip
    assert provider_button.testAttribute(
        QtCore.Qt.WidgetAttribute.WA_AlwaysShowToolTips
    )

    chooser.show_provider("typesafe")
    assert chooser.current_provider() == "codex_cli"
    chooser._show_directory()
    chooser.directory_list.itemClicked.emit(directory_item)
    assert chooser.current_provider() == ""
    assert chooser.active_provider == "codex_cli"

    chooser.refresh("jury2")
    assert directory_item.flags() & enabled
    assert directory_item.flags() & selectable
    assert not directory_item.toolTip()
    assert provider_button.isEnabled()
    assert not provider_button.toolTip()
    assert not provider_button.testAttribute(
        QtCore.Qt.WidgetAttribute.WA_AlwaysShowToolTips
    )
    chooser.directory_list.itemClicked.emit(directory_item)
    assert chooser.current_provider() == "typesafe"
    chooser._provider_buttons["codex_cli"].click()
    provider_button.click()
    assert chooser.current_provider() == "typesafe"
    assert selected["jury2"] == ["typesafe:jev-1.0.0"]
    window.llm_choice_dialog.close()
    assert app is not None


@pytest.mark.parametrize("locale,expected_tooltip", [
    ("en", "Not yet supported on macOS"),
    ("it", "Non ancora supportato su macOS"),
])
def test_freetoken_is_disabled_and_explained_on_macos(
    monkeypatch, locale, expected_tooltip,
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets
    from core.gui import jury_chooser
    from core.gui.jury_chooser import install_jury_chooser
    import json
    from pathlib import Path

    monkeypatch.setattr(jury_chooser, "sys", SimpleNamespace(platform="darwin"))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window, QtCore, QtGui, QtWidgets, _selected, _group, _saved = _window()
    locale_data = json.loads(
        (Path(__file__).resolve().parents[1] / "core/gui/assets/locales" / f"{locale}.json")
        .read_text(encoding="utf-8")
    )
    tooltip = locale_data["jury_provider_macos_unsupported"]
    assert tooltip == expected_tooltip
    chooser = install_jury_chooser(
        window, QtCore, QtGui, QtWidgets,
        {"jury_provider_macos_unsupported": tooltip},
    )

    provider_button = chooser._provider_buttons["freetoken"]
    assert not chooser._provider_available_for_role("freetoken", "jury1")
    assert not chooser._provider_available_for_role("freetoken", "jury2")
    assert not provider_button.isEnabled()
    assert provider_button.property("providerAvailable") is False
    assert "color:#7180a4" in provider_button.styleSheet()
    assert provider_button.toolTip() == tooltip
    assert provider_button.testAttribute(
        QtCore.Qt.WidgetAttribute.WA_AlwaysShowToolTips
    )

    # Both the visible quick shortcut and any direct navigation request must
    # leave FreeToken unavailable; it is not offered in the Other providers list.
    provider_button.click()
    assert chooser.current_provider() == "codex_cli"
    chooser._show_directory()
    assert all(
        chooser.directory_list.item(index).data(QtCore.Qt.ItemDataRole.UserRole) != "freetoken"
        for index in range(chooser.directory_list.count())
    )
    chooser.show_provider("freetoken")
    chooser.show_provider("freetoken_authenticated")
    assert chooser.directory_open
    assert chooser.current_provider() == ""
    window.llm_choice_dialog.close()
    assert app is not None


@pytest.mark.parametrize("platform_name", ["win32", "linux"])
def test_freetoken_remains_selectable_outside_macos(monkeypatch, platform_name):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtGui, QtWidgets
    from core.gui import jury_chooser
    from core.gui.jury_chooser import install_jury_chooser

    monkeypatch.setattr(jury_chooser, "sys", SimpleNamespace(platform=platform_name))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window, QtCore, QtGui, QtWidgets, _selected, _group, _saved = _window()
    chooser = install_jury_chooser(window, QtCore, QtGui, QtWidgets, {})

    provider_button = chooser._provider_buttons["freetoken"]
    assert chooser._provider_available_for_role("freetoken", "jury1")
    assert chooser._provider_available_for_role("freetoken", "jury2")
    assert provider_button.isEnabled()
    assert provider_button.property("providerAvailable") is True
    assert not provider_button.toolTip()
    provider_button.click()
    assert chooser.current_provider() == "freetoken"
    window.llm_choice_dialog.close()
    assert app is not None














def _drag_header_section(QTest, QtCore, header, section, delta):
    from PySide6.QtCore import Qt

    start = QtCore.QPoint(
        header.sectionViewportPosition(section) + header.sectionSize(section) - 1,
        max(1, header.height() // 2),
    )
    finish = start + QtCore.QPoint(delta, 0)
    QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(header.viewport(), finish, 20)
    QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=finish)








