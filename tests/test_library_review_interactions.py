# tests/test_library_review_interactions.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Library review controls must be discoverable, translated and theme-aware."""
import pytest


@pytest.fixture
def review(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtGui, QtTest, QtWidgets
    from core.gui.desktop import load_translations
    from core.gui.library_review import create_library_review_dialog

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    rows = [
        {"file": "identified.pdf", "sha256": "one", "_session_id": "session", "_file_id": "one",
         "title": "A known paper", "doi": "10.1234/one", "status": "identified"},
        {"file": "unknown.pdf", "sha256": "two", "_session_id": "session", "_file_id": "two",
         "title": "", "doi": "", "status": "unidentified"},
        {"file": "saved.pdf", "sha256": "three", "_session_id": "session", "_file_id": "three",
         "title": "Already catalogued", "status": "stored"},
    ]
    dialog = create_library_review_dialog(QtCore, QtWidgets, load_translations("it"), rows)
    dialog.show()
    app.processEvents()
    yield app, QtCore, QtGui, QtTest, QtWidgets, dialog
    for child in dialog.findChildren(QtWidgets.QDialog):
        child.close()
    dialog.close()
    app.processEvents()


def test_select_all_excludes_unverified_and_stored_rows(review):
    app, QtCore, _QtGui, _QtTest, QtWidgets, dialog = review
    select_all = dialog.findChild(QtWidgets.QCheckBox, "librarySelectAll")
    assert select_all is not None
    select_all.click()
    assert {row["sha256"] for row in dialog.selected_rows()} == {"one"}
    dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Unchecked)
    app.processEvents()
    assert select_all.checkState() == QtCore.Qt.CheckState.Unchecked
    select_all.click()
    assert len(dialog.selected_rows()) == 1
    select_all.click()
    assert dialog.selected_rows() == []
    assert not (dialog.table.item(2, 0).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable)
    assert not (dialog.table.item(1, 0).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable)


@pytest.mark.parametrize("dark", [False, True])
def test_checkbox_cell_padding_tick_and_keyboard_toggle_once(review, dark):
    app, QtCore, _QtGui, QtTest, _QtWidgets, dialog = review
    dialog.set_dark_theme(dark)
    app.processEvents()
    checkbox = dialog.table.item(0, 0)
    rect = dialog.table.visualItemRect(checkbox)
    padding = QtCore.QPoint(rect.right() - 3, rect.center().y())
    QtTest.QTest.mouseClick(dialog.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=padding)
    assert checkbox.checkState() == QtCore.Qt.CheckState.Checked
    QtTest.QTest.mouseClick(dialog.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=padding)
    assert checkbox.checkState() == QtCore.Qt.CheckState.Unchecked
    indicator = QtCore.QPoint(rect.left() + 8, rect.center().y())
    QtTest.QTest.mouseClick(dialog.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=indicator)
    assert checkbox.checkState() == QtCore.Qt.CheckState.Checked
    dialog.table.setCurrentCell(0, 0)
    dialog.table.setFocus()
    QtTest.QTest.keyClick(dialog.table, QtCore.Qt.Key.Key_Space)
    app.processEvents()
    assert checkbox.checkState() == QtCore.Qt.CheckState.Unchecked


def test_explicit_editor_assigns_identity_without_saving_or_losing_file_tokens(review):
    app, QtCore, _QtGui, _QtTest, QtWidgets, dialog = review
    saves = []
    dialog.save_requested.connect(saves.append)
    requests = []
    dialog.set_identity_enabled(True)
    dialog.identity_requested.connect(lambda row, scheme, identifier: requests.append((row, scheme, identifier)))
    dialog.table.setCurrentCell(1, 3)
    button = dialog.findChild(QtWidgets.QPushButton, "libraryEditIdentity")
    assert button is not None and button.isEnabled()
    from core.gui.desktop import load_translations
    assert button.text() == load_translations("it")["library_edit_identity"]
    button.click()
    app.processEvents()
    editor = dialog.findChild(QtWidgets.QDialog, "libraryIdentityEditor")
    assert editor is not None and editor.isVisible()
    changes = {
        "Title": "Identità assegnata manualmente",
        "Doi": "10.1234/manual",
        "Pmid": "123456",
        "Year": "2024",
        "Authors": "Ada Ricercatrice; Bruno Autore",
    }
    for suffix, value in changes.items():
        field = editor.findChild(QtWidgets.QLineEdit, f"libraryIdentity{suffix}")
        assert field is not None
        field.setText(value)
    editor.findChild(QtWidgets.QPushButton, "libraryIdentityOk").click()
    app.processEvents()
    assert saves == []
    assert len(requests) == 1 and requests[0][1:] == ("", "")
    assert not dialog.table.item(1, 0).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable
    dialog.apply_identity_result(dict(requests[0][0], status="identified", reason_code=None, reason=None))
    assert dialog.table.item(1, 3).text() == changes["Title"]
    dialog.table.item(1, 0).setCheckState(QtCore.Qt.CheckState.Checked)
    selected = dialog.selected_rows()[0]
    assert selected["title"] == changes["Title"]
    assert selected["doi"] == changes["Doi"]
    assert selected["pmid"] == changes["Pmid"]
    assert selected["year"] == changes["Year"]
    assert selected["authors"] == changes["Authors"]
    assert selected["_session_id"] == "session" and selected["_file_id"] == "two"
    assert not dialog.table.item(2, 3).flags() & QtCore.Qt.ItemFlag.ItemIsEditable


def test_bibliographic_double_click_opens_identity_editor_without_inline_edit(review):
    app, QtCore, _QtGui, QtTest, QtWidgets, dialog = review
    requests = []
    dialog.identity_requested.connect(
        lambda row, scheme, identifier: requests.append((row, scheme, identifier))
    )
    item = dialog.table.item(1, 3)
    assert not item.flags() & QtCore.Qt.ItemFlag.ItemIsEditable
    rect = dialog.table.visualItemRect(item)
    QtTest.QTest.mouseClick(
        dialog.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=rect.center()
    )
    QtTest.QTest.mouseDClick(
        dialog.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=rect.center()
    )
    app.processEvents()
    editor = dialog.findChild(QtWidgets.QDialog, "libraryIdentityEditor")
    assert editor is not None and editor.isVisible()
    assert item.text() == ""
    assert requests == []
    assert dialog._rows[1]["status"] == "unidentified"
    editor.close()
    app.processEvents()


def test_file_double_click_edits_identity_and_open_button_emits_current_full_path(review):
    app, QtCore, _QtGui, QtTest, QtWidgets, dialog = review
    opened = []
    dialog.file_open_requested.connect(opened.append)

    # The button works for a stored row too, using the original full path.
    dialog.table.setCurrentCell(2, 3)
    button = dialog.findChild(QtWidgets.QPushButton, "libraryOpenFile")
    assert button is not None and button.isEnabled()
    button.click()
    assert opened == ["saved.pdf"]
    assert dialog.findChild(QtWidgets.QDialog, "libraryIdentityEditor") is None

    # File-name column is table column 1 (column 0 is the selection checkbox).
    item = dialog.table.item(1, 1)
    rect = dialog.table.visualItemRect(item)
    QtTest.QTest.mouseClick(
        dialog.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=rect.center()
    )
    QtTest.QTest.mouseDClick(
        dialog.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=rect.center()
    )
    app.processEvents()
    assert opened == ["saved.pdf"]
    editor = dialog.findChild(QtWidgets.QDialog, "libraryIdentityEditor")
    assert editor is not None and editor.isVisible()
    editor.findChild(QtWidgets.QPushButton, "libraryIdentityCancel").click()
    app.processEvents()
    button.click()
    assert opened == ["saved.pdf", "unknown.pdf"]


def test_review_table_is_read_only_and_checkbox_remains_usable(review):
    app, QtCore, _QtGui, _QtTest, QtWidgets, dialog = review
    for row in range(dialog.table.rowCount()):
        for column in range(1, dialog.table.columnCount()):
            assert not dialog.table.item(row, column).flags() & QtCore.Qt.ItemFlag.ItemIsEditable
    assert dialog.table.editTriggers() == QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
    dialog.table.item(0, 0).setCheckState(QtCore.Qt.CheckState.Checked)
    app.processEvents()
    assert len(dialog.selected_rows()) == 1


def test_review_uses_light_and_dark_callimachus_colours(review):
    app, _QtCore, QtGui, _QtTest, _QtWidgets, dialog = review
    assert hasattr(dialog, "set_dark_theme")
    dialog.set_dark_theme(False)
    app.processEvents()
    light_background = dialog.palette().color(QtGui.QPalette.ColorRole.Window)
    light_text = dialog.table.palette().color(QtGui.QPalette.ColorRole.Text)
    identified = dialog.table.item(0, 2).background().color()
    manual = dialog.table.item(1, 2).background().color()
    assert identified != manual
    assert manual == dialog.table.item(1, 10).background().color()
    dialog.set_dark_theme(True)
    app.processEvents()
    dark_background = dialog.palette().color(QtGui.QPalette.ColorRole.Window)
    dark_text = dialog.table.palette().color(QtGui.QPalette.ColorRole.Text)
    assert light_background.lightness() > dark_background.lightness() + 80
    assert dark_text.lightness() > light_text.lightness() + 80
    assert dialog.table.item(1, 2).background().color() != manual
    assert dialog.table.item(0, 2).background().color() != dialog.table.item(1, 2).background().color()
    dialog.apply_saved([{"_session_id": "session", "_file_id": "two", "status": "stored"}])
    assert dialog.table.item(1, 2).background().color() == dialog.table.item(0, 2).background().color()


@pytest.mark.parametrize("language", ["en", "it"])
def test_details_are_rendered_from_external_catalogue_instead_of_backend_english(review, language):
    app, QtCore, _QtGui, _QtTest, QtWidgets, _dialog = review
    from core.gui.desktop import load_translations
    from core.gui.library_review import create_library_review_dialog

    text = load_translations(language)
    text["library_reason_choose_doi"] = "TRANSLATED DETAIL FROM CATALOGUE"
    rows = [{
        "file": "ambiguous.pdf", "status": "needs_review",
        "reason_code": "choose_doi", "reason_params": {},
        "reason": "Internal English diagnostic that must never be displayed",
    }]
    dialog = create_library_review_dialog(QtCore, QtWidgets, text, rows)
    try:
        detail = dialog.table.item(0, 10)
        assert detail.text() == text["library_reason_choose_doi"]
        assert "Internal English" not in detail.toolTip()
        dialog.apply_saved([{**rows[0], "status": "needs_review"}])
        assert detail.text() == text["library_reason_choose_doi"]
    finally:
        dialog.close()
        app.processEvents()


def test_all_library_reason_codes_have_external_english_and_italian_messages():
    from core.gui.desktop import load_translations

    codes = (
        "title_conflict", "multiple_identifiers", "multiple_matches", "no_identity",
        "missing_author_year", "identity_inconclusive", "author_mismatch", "unreadable_pdf",
        "choose_doi", "choose_pmid", "doi_conflict", "pmid_conflict", "invalid_doi",
        "invalid_pmid", "invalid_title", "invalid_year", "missing_stable_identity",
        "invalid_review_record", "item_unavailable", "staged_pdf_missing",
        "staged_pdf_changed", "save_failed", "pending_review_unreadable",
        "identity_review_required", "invalid_identity_identifier",
        "identity_metadata_unavailable", "identity_metadata_mismatch",
        "identity_pdf_mismatch", "identity_review_persist_failed",
        "choose_identifier", "identifier_conflict",
    )
    params = {"metadata_title": "Metadata title", "layout_title": "Visible title"}
    for language in ("en", "it"):
        text = load_translations(language)
        for code in codes:
            value = text[f"library_reason_{code}"]
            assert value.strip()
            assert value.format(**params).strip()


def test_open_library_and_identity_editor_follow_main_window_theme(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtGui, QtWidgets
    from core.gui.desktop import create_desktop_window
    import time

    settings_type = QtCore.QSettings

    class Settings(settings_type):
        def __init__(self, *args):
            if args == ("Callimachus", "Callimachus"):
                super().__init__(str(tmp_path / "preferences.ini"), settings_type.Format.IniFormat)
            else:
                super().__init__(*args)

    monkeypatch.setattr(QtCore, "QSettings", Settings)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = create_desktop_window(library_import_callback=lambda paths, _progress: [{
        "file": paths[0], "status": "unidentified", "_file_id": "one",
    }])
    try:
        window._dark = False
        window._apply_theme()
        window._start_library_import(["unknown.pdf"])
        deadline = time.monotonic() + 3
        while window._library_dialog is None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        dialog = window._library_dialog
        assert dialog is not None
        dialog.table.setCurrentCell(0, 3)
        dialog.findChild(QtWidgets.QPushButton, "libraryEditIdentity").click()
        app.processEvents()
        editor = dialog.findChild(QtWidgets.QDialog, "libraryIdentityEditor")
        assert editor is not None and editor.isVisible()
        role = QtGui.QPalette.ColorRole.Window
        initial_dialog = dialog.palette().color(role).lightness()
        initial_editor = editor.palette().color(role).lightness()
        window._dark = True
        window._apply_theme()
        app.processEvents()
        assert initial_dialog > dialog.palette().color(role).lightness() + 80
        assert initial_editor > editor.palette().color(role).lightness() + 80
    finally:
        if window._library_dialog is not None:
            for child in window._library_dialog.findChildren(QtWidgets.QDialog):
                child.close()
            window._library_dialog.close()
        window.close()
        app.processEvents()


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("font_size", [13, 22])
@pytest.mark.parametrize("requested_height", [540, 420])
def test_identity_editor_reserves_readable_fields_and_separated_rows(
    review, dark, font_size, requested_height
):
    app, C, G, Q, W, dialog = review
    dialog.set_dark_theme(dark)
    if font_size != 13:
        from core.gui.atlante import stylesheet as atlante_stylesheet
        from core.gui.desktop import _stylesheet

        dialog.setStyleSheet(
            _stylesheet(dark=dark, font_size=font_size)
            + atlante_stylesheet(dark=dark, font_size=font_size)
        )
    dialog.table.setCurrentCell(1, 3)
    dialog.edit_identity_button.click()
    app.processEvents()
    editor = dialog._identity_editor
    assert editor is not None

    # Exercise a compact requested size; the form scrolls instead of shrinking
    # edit controls into overlapping rows.
    editor.resize(560, requested_height)
    app.processEvents()
    scroll_area = editor.findChild(W.QScrollArea, "libraryIdentityFieldsScroll")
    assert scroll_area is not None and scroll_area.widget() is not None
    fields = [
        editor.findChild(W.QLineEdit, f"libraryIdentity{suffix}")
        for _key, suffix in dialog.identity_fields
    ]
    assert all(fields)
    minimum_gap = max(
        8,
        max((field.fontMetrics().height() + 2) // 3 for field in fields),
    )
    global_tops = [field.mapToGlobal(C.QPoint(0, 0)).y() for field in fields]
    fields_layout = editor.findChild(W.QFormLayout)
    for previous, current, previous_top, current_top in zip(
        fields, fields[1:], global_tops, global_tops[1:]
    ):
        # QWidget height includes its complete frame border, so this checks
        # the visible border-to-border separation in screen coordinates.
        gap = current_top - (previous_top + previous.height())
        assert gap >= minimum_gap, (
            f"{previous.objectName()} to {current.objectName()}: gap={gap}, "
            f"required={minimum_gap}, editor={editor.size()}, "
            f"previous={previous.geometry()} at y={previous_top}, "
            f"current={current.geometry()} at y={current_top}"
        )

    for field in fields:
        option = W.QStyleOptionFrame()
        field.initStyleOption(option)
        contents = field.style().subElementRect(W.QStyle.SubElement.SE_LineEditContents, option, field)
        margins = field.textMargins()
        assert contents.height() - margins.top() - margins.bottom() >= field.fontMetrics().height()
        assert field.height() >= field.sizeHint().height()
        assert contents.height() > 0

    scroll_bar = scroll_area.verticalScrollBar()
    if scroll_bar.maximum() > 0:
        scroll_bar.setValue(scroll_bar.maximum())
        app.processEvents()
        viewport_top = scroll_area.viewport().mapToGlobal(C.QPoint(0, 0)).y()
        last_top = fields[-1].mapToGlobal(C.QPoint(0, 0)).y()
        assert last_top >= viewport_top
        assert (
            last_top + fields[-1].height()
            <= viewport_top + scroll_area.viewport().height()
        )


def test_manual_identity_confirmation_has_distinct_progress_copy(review):
    app, C, G, Q, W, dialog = review
    from core.gui.desktop import load_translations

    dialog.set_identity_enabled(True)
    dialog.table.setCurrentCell(1, 3)
    dialog.edit_identity_button.click()
    app.processEvents()
    editor = dialog._identity_editor
    dialog.identity_requested.connect(lambda *_args: dialog.set_busy(True))
    editor.findChild(W.QPushButton, "libraryIdentityOk").click()
    assert editor.findChild(W.QLabel, "libraryIdentityStatus").text() == load_translations("it")["library_confirming_identity"]
    dialog.set_busy(False)
