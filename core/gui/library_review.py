# core/gui/library_review.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Review dialog for files imported into the user Library."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .atlante import stylesheet as atlante_stylesheet


def create_library_review_dialog(QtCore, QtWidgets, text, rows: Sequence[Mapping[str, Any]], parent=None):
    """Create a Qt dialog while keeping PySide optional at module import time."""
    class LibraryReviewDialog(QtWidgets.QDialog):
        save_requested = QtCore.Signal(object)
        identity_requested = QtCore.Signal(object, str, str)
        file_open_requested = QtCore.Signal(str)

        columns = (
            ("file", "library_file", False),
            ("status", "library_status", False),
            ("title", "library_title", True),
            ("doi", "library_doi", True),
            ("pmid", "library_pmid", True),
            ("year", "library_year", True),
            ("authors", "library_authors", True),
            ("method", "library_method", False),
            ("score", "library_score", False),
            ("reason", "library_reason", False),
            ("pmcid", "library_pmcid", True),
            ("arxiv_id", "library_arxiv", True),
            ("isbn", "library_isbn", True),
        )

        identity_fields = (
            ("title", "Title"),
            ("doi", "Doi"),
            ("pmid", "Pmid"),
            ("pmcid", "Pmcid"),
            ("arxiv_id", "Arxiv"),
            ("isbn", "Isbn"),
            ("year", "Year"),
            ("authors", "Authors"),
        )

        def __init__(self):
            super().__init__(parent)
            self.setObjectName("libraryReviewDialog")
            self.setWindowTitle(text["library_review_title"])
            self.resize(1100, 600)
            self._rows = [dict(row) for row in rows]
            self._dark = False
            self._busy = False
            self._checkbox_pressed_state = None
            self._identity_editor = None
            self._identity_available = False
            self._identity_request_index = None
            self._identity_lookup = False
            self._accepted_identity_snapshots = {}

            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(20, 18, 20, 18)
            layout.setSpacing(12)
            self.status = QtWidgets.QLabel("", self)
            self.status.setObjectName("libraryReviewStatus")
            self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            self.progress_bar = QtWidgets.QProgressBar(self)
            self.progress_bar.setObjectName("libraryImportProgressBar")
            self.progress_bar.setFormat(text["library_progress_count"])
            self.progress_bar.setAccessibleName(text["library_saving"])
            self.progress_bar.hide()
            layout.addWidget(self.progress_bar)
            self.help_label = QtWidgets.QLabel(text["library_review_help"], self)
            self.help_label.setObjectName("libraryReviewHelp")
            self.help_label.setWordWrap(True)
            layout.addWidget(self.help_label)

            controls = QtWidgets.QHBoxLayout()
            self._controls_layout = controls
            self.select_all = QtWidgets.QCheckBox(text["select_all"], self)
            self.select_all.setObjectName("librarySelectAll")
            self.select_all.setTristate(True)
            self.select_all.clicked.connect(self._toggle_all)
            controls.addWidget(self.select_all)
            controls.addStretch()
            self.open_file_button = QtWidgets.QPushButton(
                text["library_open_file"], self
            )
            self.open_file_button.setObjectName("libraryOpenFile")
            self.open_file_button.setEnabled(False)
            self.open_file_button.clicked.connect(self._open_current_file)
            controls.addWidget(self.open_file_button)
            self.edit_identity_button = QtWidgets.QPushButton(
                text["library_edit_identity"], self
            )
            self.edit_identity_button.setObjectName("libraryEditIdentity")
            self.edit_identity_button.setEnabled(False)
            self.edit_identity_button.clicked.connect(self._open_identity_editor)
            controls.addWidget(self.edit_identity_button)
            self._controls_widgets = (
                self.select_all, self.open_file_button, self.edit_identity_button,
            )
            for control in self._controls_widgets:
                control.setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Ignored,
                    QtWidgets.QSizePolicy.Policy.Fixed,
                )
            layout.addLayout(controls)

            self.table = QtWidgets.QTableWidget(self)
            self.table.setObjectName("libraryReviewTable")
            self.table.setWordWrap(True)
            self.table.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
            self.table.setAlternatingRowColors(True)
            self.table.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
            )
            self.table.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
            )
            self.table.setColumnCount(len(self.columns) + 1)
            self.table.setHorizontalHeaderLabels(
                [text["library_include"]]
                + [text[label] for _key, label, _editable in self.columns]
            )
            for column, width in enumerate((42, 120, 132, 185, 130, 82, 58, 130, 185, 85, 220)):
                self.table.setColumnWidth(column, width)
            self.table.horizontalHeader().setStretchLastSection(True)
            layout.addWidget(self.table, 1)

            actions = QtWidgets.QHBoxLayout()
            self.save_button = QtWidgets.QPushButton(text["library_save_selected"], self)
            self.save_button.setObjectName("primaryButton")
            self._save_available = True
            self.save_button.clicked.connect(self._request_save)
            actions.addWidget(self.save_button)
            self.close_button = QtWidgets.QPushButton(text["close"], self)
            self.close_button.clicked.connect(self.close)
            actions.addWidget(self.close_button)
            actions.addStretch()
            layout.addLayout(actions)

            self._fill()
            self._color_statuses()
            self.table.itemChanged.connect(self._table_item_changed)
            self.table.cellPressed.connect(self._remember_checkbox_state)
            self.table.cellClicked.connect(self._toggle_checkbox_cell_once)
            self.table.currentCellChanged.connect(self._current_cell_changed)
            self.table.cellDoubleClicked.connect(self._cell_double_clicked)
            self.table.resizeRowsToContents()
            self._sync_select_all()
            self._sync_open_file_button()
            self._sync_identity_button()

        def resizeEvent(self, event):
            super().resizeEvent(event)
            if not hasattr(self, "_controls_widgets"):
                return
            margins = self.layout().contentsMargins()
            available = self.contentsRect().width() - margins.left() - margins.right()
            required = sum(control.sizeHint().width() for control in self._controls_widgets)
            required += self._controls_layout.spacing() * (len(self._controls_widgets) - 1)
            self._controls_layout.setDirection(
                QtWidgets.QBoxLayout.Direction.TopToBottom
                if required > available else QtWidgets.QBoxLayout.Direction.LeftToRight
            )

        def _is_stored(self, row: Mapping[str, Any]) -> bool:
            return bool(row.get("saved")) or str(row.get("status") or "").casefold() in {
                "stored", "saved",
            }

        def _eligible_indices(self):
            return [
                index for index, row in enumerate(self._rows)
                if not self._is_stored(row) and row.get("status") == "identified"
            ]

        def _fill(self):
            self.table.setRowCount(len(self._rows))
            selectable = QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable
            for index, row in enumerate(self._rows):
                checkbox = QtWidgets.QTableWidgetItem()
                checkbox.setFlags(selectable)
                if not self._is_stored(row) and row.get("status") == "identified":
                    checkbox.setFlags(checkbox.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                checkbox.setCheckState(QtCore.Qt.CheckState.Unchecked)
                checkbox.setData(QtCore.Qt.ItemDataRole.UserRole, index)
                self.table.setItem(index, 0, checkbox)
                for column, (key, _label, _editable) in enumerate(self.columns, start=1):
                    value = row.get(key, "")
                    tooltip = None
                    if key == "file":
                        tooltip = str(value or "")
                        value = Path(tooltip).name if tooltip else ""
                    elif key == "status":
                        raw = str(value or "other")
                        value = text.get(f"library_status_{raw}", text["library_status_other"])
                    elif key == "method":
                        value = _method_label(str(value or ""), text)
                    elif key == "reason":
                        value = _row_reason_text(text, row)
                    item = QtWidgets.QTableWidgetItem("" if value is None else str(value))
                    if tooltip:
                        item.setToolTip(tooltip)
                    if key == "status":
                        item.setData(QtCore.Qt.ItemDataRole.UserRole, row.get(key))
                        item.setToolTip(_row_reason_text(text, row))
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                    self.table.setItem(index, column, item)

        def selected_rows(self):
            selected = []
            for index in self._eligible_indices():
                checkbox = self.table.item(index, 0)
                if (
                    checkbox is None
                    or not checkbox.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable
                    or checkbox.checkState() != QtCore.Qt.CheckState.Checked
                ):
                    continue
                row = dict(self._rows[index])
                selected.append(row)
            return selected

        def _request_save(self):
            selected = self.selected_rows()
            if selected:
                self.save_requested.emit(selected)

        def _remember_checkbox_state(self, row: int, column: int):
            if column != 0 or row not in self._eligible_indices():
                self._checkbox_pressed_state = None
                return
            item = self.table.item(row, 0)
            self._checkbox_pressed_state = (row, item.checkState()) if item else None

        def _toggle_checkbox_cell_once(self, row: int, column: int):
            remembered = self._checkbox_pressed_state
            self._checkbox_pressed_state = None
            if column != 0 or remembered is None or remembered[0] != row:
                return
            item = self.table.item(row, 0)
            if item is None or row not in self._eligible_indices():
                return
            before = remembered[1]
            if item.checkState() == before:
                item.setCheckState(
                    QtCore.Qt.CheckState.Unchecked
                    if before == QtCore.Qt.CheckState.Checked
                    else QtCore.Qt.CheckState.Checked
                )
            self._sync_select_all()

        def _table_item_changed(self, item):
            if item.column() == 0:
                self._sync_select_all()

        def _invalidate_identity(self, index: int):
            if self._is_stored(self._rows[index]):
                return
            self._accepted_identity_snapshots.pop(index, None)
            self._rows[index].update(status="needs_review", reason_code="identity_review_required", reason_params={})
            self.table.blockSignals(True)
            checkbox = self.table.item(index, 0)
            checkbox.setCheckState(QtCore.Qt.CheckState.Unchecked)
            checkbox.setFlags(checkbox.flags() & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            status = self.table.item(index, 2)
            status.setText(text["library_status_needs_review"])
            status.setData(QtCore.Qt.ItemDataRole.UserRole, "needs_review")
            reason = _row_reason_text(text, self._rows[index])
            status.setToolTip(reason)
            self.table.item(index, 10).setText(reason)
            self._color_statuses()
            self.table.blockSignals(False)
            self._sync_select_all()

        def _sync_select_all(self):
            eligible = self._eligible_indices()
            checkable = [
                index for index in eligible
                if self.table.item(index, 0) is not None
                and self.table.item(index, 0).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable
            ]
            selected = sum(
                self.table.item(index, 0).checkState() == QtCore.Qt.CheckState.Checked
                for index in checkable
            )
            if selected == 0:
                state = QtCore.Qt.CheckState.Unchecked
            elif selected == len(checkable):
                state = QtCore.Qt.CheckState.Checked
            else:
                state = QtCore.Qt.CheckState.PartiallyChecked
            self.select_all.blockSignals(True)
            self.select_all.setCheckState(state)
            self.select_all.blockSignals(False)
            self.select_all.setEnabled(bool(checkable) and not self._busy)
            self.save_button.setEnabled(bool(selected) and self._save_available and not self._busy)

        def _toggle_all(self, checked: bool):
            state = QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
            for index in self._eligible_indices():
                item = self.table.item(index, 0)
                if item is not None and item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(state)
            self._sync_select_all()

        def _current_cell_changed(self, *_args):
            self._sync_open_file_button()
            self._sync_identity_button()

        def _sync_open_file_button(self):
            index = self.table.currentRow()
            enabled = (
                not self._busy
                and 0 <= index < len(self._rows)
                and bool(str(self._rows[index].get("file") or "").strip())
            )
            self.open_file_button.setEnabled(enabled)

        def _open_current_file(self):
            index = self.table.currentRow()
            if index < 0 or index >= len(self._rows):
                return
            path = str(self._rows[index].get("file") or "").strip()
            if path:
                self.file_open_requested.emit(path)

        def _cell_double_clicked(self, row: int, column: int):
            if row < 0 or row >= len(self._rows):
                return
            self.table.setCurrentCell(row, column)
            if column == 0:
                return
            self._open_identity_editor()

        def _sync_identity_button(self):
            index = self.table.currentRow()
            enabled = (
                not self._busy
                and 0 <= index < len(self._rows)
                and not self._is_stored(self._rows[index])
            )
            self.edit_identity_button.setEnabled(enabled)

        def _open_identity_editor(self):
            if self._identity_editor is not None and self._identity_editor.isVisible():
                self._identity_editor.raise_()
                self._identity_editor.activateWindow()
                return
            index = self.table.currentRow()
            if index < 0 or index >= len(self._rows) or self._is_stored(self._rows[index]):
                return

            class IdentityEditor(QtWidgets.QDialog):
                lookup_busy = False

                def reject(window):
                    if not window.lookup_busy:
                        super().reject()

                def closeEvent(window, event):
                    if window.lookup_busy:
                        event.ignore()
                    else:
                        super().closeEvent(event)

            editor = IdentityEditor(self)
            editor.setObjectName("libraryIdentityEditor")
            editor.setWindowTitle(text["library_identity_editor_title"])
            editor.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
            editor.setModal(True)
            editor.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
            form_layout = QtWidgets.QVBoxLayout(editor)
            help_label = QtWidgets.QLabel(text["library_identity_editor_help"], editor)
            help_label.setWordWrap(True)
            form_layout.addWidget(help_label)

            fields_scroll = QtWidgets.QScrollArea(editor)
            fields_scroll.setObjectName("libraryIdentityFieldsScroll")
            fields_scroll.setWidgetResizable(True)
            fields_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            fields_widget = QtWidgets.QWidget(fields_scroll)
            fields = QtWidgets.QFormLayout(fields_widget)
            edits = {}
            field_font_height = 0
            for key, suffix in self.identity_fields:
                edit = QtWidgets.QLineEdit(fields_widget)
                edit.setObjectName(f"libraryIdentity{suffix}")
                edit.setText(self.table.item(index, self._column_index(key)).text())
                edit.ensurePolished()
                edit.setMinimumHeight(edit.sizeHint().height())
                field_font_height = max(field_font_height, edit.fontMetrics().height())
                edits[key] = edit
                fields.addRow(text[f"library_identity_{key}_label"], edit)
            fields.setVerticalSpacing(
                max(fields.verticalSpacing(), 8, (field_font_height + 2) // 3)
            )
            fields_scroll.setWidget(fields_widget)
            form_layout.addWidget(fields_scroll)
            lookup = QtWidgets.QHBoxLayout()
            scheme = QtWidgets.QComboBox(editor)
            scheme.setObjectName("libraryIdentityScheme")
            for key, label in (("doi", "DOI"), ("pmid", "PMID"), ("pmcid", "PMCID"), ("arxiv", "arXiv"), ("isbn", "ISBN")):
                scheme.addItem(label, key)
            preferred = next((key for key in ("doi", "pmid", "pmcid", "arxiv", "isbn") if edits["arxiv_id" if key == "arxiv" else key].text().strip()), "doi")
            scheme.setCurrentIndex(scheme.findData(preferred))
            lookup.addWidget(scheme)
            lookup_button = QtWidgets.QPushButton(text["library_lookup_metadata"], editor)
            lookup_button.setObjectName("libraryIdentityLookup")
            lookup_button.setEnabled(self._identity_available)
            lookup_button.clicked.connect(lambda: self._request_identity(index, edits, str(scheme.currentData())))
            lookup.addWidget(lookup_button)
            lookup.addStretch()
            form_layout.addLayout(lookup)
            editor_status = QtWidgets.QLabel(text["library_lookup_help"], editor)
            editor_status.setObjectName("libraryIdentityStatus")
            editor_status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            editor_status.setWordWrap(True)
            form_layout.addWidget(editor_status)
            buttons = QtWidgets.QHBoxLayout()
            apply_button = QtWidgets.QPushButton(text["library_identity_ok"], editor)
            apply_button.setObjectName("libraryIdentityOk")
            apply_button.setDefault(True)
            apply_button.setEnabled(self._identity_available)
            apply_button.clicked.connect(
                lambda _checked=False, row_index=index, values=edits, window=editor:
                self._apply_identity(row_index, values, window)
            )
            buttons.addWidget(apply_button)
            cancel_button = QtWidgets.QPushButton(text["library_identity_cancel"], editor)
            cancel_button.setObjectName("libraryIdentityCancel")
            cancel_button.clicked.connect(editor.close)
            buttons.addWidget(cancel_button)
            buttons.addStretch()
            form_layout.addLayout(buttons)
            editor.finished.connect(self._identity_editor_closed)
            self._identity_editor = editor
            self._identity_edits = edits
            editor.resize(560, 540)
            editor.show()
            editor.raise_()
            editor.activateWindow()

        def _apply_identity(self, index: int, edits: Mapping[str, Any], editor):
            if index < 0 or index >= len(self._rows) or self._is_stored(self._rows[index]):
                editor.close()
                return
            values = self._identity_edit_snapshot(edits)
            if (
                str(self._rows[index].get("status") or "").casefold() == "identified"
                and self._accepted_identity_snapshots.get(index) == values
            ):
                self._select_identity_for_save(index, editor)
                return
            self._request_identity(index, edits, "", operator_attested=True)

        @staticmethod
        def _identity_edit_snapshot(edits: Mapping[str, Any]) -> dict[str, str]:
            return {key: str(edit.text()).strip() for key, edit in edits.items()}

        def _select_identity_for_save(self, index: int, editor):
            checkbox = self.table.item(index, 0)
            if (
                checkbox is not None
                and checkbox.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable
                and str(self._rows[index].get("status") or "").casefold() == "identified"
            ):
                checkbox.setCheckState(QtCore.Qt.CheckState.Checked)
                self._sync_select_all()
                editor.accept()

        def _request_identity(
            self,
            index: int,
            edits: Mapping[str, Any],
            scheme: str,
            *,
            operator_attested: bool = False,
        ):
            if self._busy or not self._identity_available:
                return
            identifier = edits["arxiv_id" if scheme == "arxiv" else scheme].text().strip() if scheme else ""
            if scheme and not identifier:
                self._identity_editor.findChild(QtWidgets.QLabel, "libraryIdentityStatus").setText(text["library_identifier_required"])
                return
            row = dict(self._rows[index])
            row.update({key: edit.text().strip() for key, edit in edits.items()})
            if operator_attested:
                row["_operator_attested"] = True
            self._identity_request_index = index
            self._identity_lookup = bool(scheme)
            self._identity_operator_attested = operator_attested
            self._invalidate_identity(index)
            self.identity_requested.emit(row, scheme, identifier)

        def set_identity_enabled(self, enabled: bool):
            self._identity_available = bool(enabled)

        def apply_identity_result(self, result: Mapping[str, Any] | None, error: str | None = None):
            index = self._identity_request_index
            editor = self._identity_editor
            if index is None or editor is None:
                return
            self._identity_request_index = None
            status_label = editor.findChild(QtWidgets.QLabel, "libraryIdentityStatus")
            if error:
                status_label.setText(error)
                return
            original = self._rows[index]
            if not result or _row_key(original) != _row_key(result):
                status_label.setText(text["library_operation_failed"])
                return
            self.table.blockSignals(True)
            for key in (*[key for key, _suffix in self.identity_fields], "status", "reason", "reason_code", "reason_params", "method", "score"):
                if key in result:
                    original[key] = result[key]
            for key, edit in self._identity_edits.items():
                value = str(original.get(key) or "")
                edit.setText(value)
                edit.setCursorPosition(0)
                self.table.item(index, self._column_index(key)).setText(value)
            raw_status = str(original.get("status") or "needs_review")
            status_item = self.table.item(index, 2)
            status_item.setData(QtCore.Qt.ItemDataRole.UserRole, raw_status)
            status_item.setText(text.get(f"library_status_{raw_status}", text["library_status_other"]))
            reason = _row_reason_text(text, original)
            status_item.setToolTip(reason)
            self.table.item(index, 10).setText(reason)
            checkbox = self.table.item(index, 0)
            flags = checkbox.flags() & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable
            if raw_status == "identified":
                flags |= QtCore.Qt.ItemFlag.ItemIsUserCheckable
            checkbox.setFlags(flags)
            checkbox.setCheckState(QtCore.Qt.CheckState.Unchecked)
            self._color_statuses()
            self.table.blockSignals(False)
            self.table.resizeRowsToContents()
            self._sync_select_all()
            if raw_status == "identified":
                self._accepted_identity_snapshots[index] = self._identity_edit_snapshot(
                    self._identity_edits
                )
                status_label.setText(text["library_identity_ready"])
                if not self._identity_lookup:
                    self._select_identity_for_save(index, editor)
            else:
                self._accepted_identity_snapshots.pop(index, None)
                status_label.setText(reason or text["library_reason_identity_inconclusive"])

        def _identity_editor_closed(self, _result):
            self._identity_editor = None

        def _column_index(self, key: str) -> int:
            return next(index for index, (column_key, _label, _editable) in enumerate(self.columns, start=1) if column_key == key)

        def set_busy(self, busy: bool):
            self._busy = bool(busy)
            self.progress_bar.setVisible(busy)
            if busy:
                self.progress_bar.setRange(0, 0)
            self.save_button.setEnabled(not busy and self._save_available)
            self.close_button.setEnabled(not busy)
            self.table.setEnabled(not busy)
            self._sync_select_all()
            self._sync_open_file_button()
            self._sync_identity_button()
            if self._identity_editor is not None:
                editor = self._identity_editor
                editor.lookup_busy = busy
                for control in editor.findChildren(QtWidgets.QLineEdit) + editor.findChildren(QtWidgets.QComboBox) + editor.findChildren(QtWidgets.QPushButton):
                    control.setEnabled(not busy)
                if busy:
                    key = "library_confirming_identity" if getattr(self, "_identity_operator_attested", False) else "library_validating_identity"
                    editor.findChild(QtWidgets.QLabel, "libraryIdentityStatus").setText(text[key])

        def set_save_enabled(self, enabled: bool):
            self._save_available = bool(enabled)
            self._sync_select_all()

        def set_message(self, message: str):
            self.status.setText(message)

        def set_progress(self, completed: int, total: int, message: str):
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(completed)
            self.set_message(message)

        def _color_statuses(self):
            from PySide6 import QtGui

            for index, row in enumerate(self._rows):
                status = str(row.get("status") or "needs_review")
                if self._is_stored(row) or status == "identified":
                    background, foreground = ("#14382c", "#b5eed3") if self._dark else ("#e1f3e8", "#175b36")
                elif status in {"unreadable", "error"}:
                    background, foreground = ("#3b1d24", "#ffb8bd") if self._dark else ("#fff0f0", "#9f1828")
                else:
                    background, foreground = ("#43351a", "#ffe0a1") if self._dark else ("#fff2d6", "#744b00")
                for column in (2, 10):
                    item = self.table.item(index, column)
                    if item is not None:
                        item.setBackground(QtGui.QColor(background))
                        item.setForeground(QtGui.QColor(foreground))

        def apply_saved(self, rows: Sequence[Mapping[str, Any]]):
            by_key = {_row_key(row): dict(row) for row in rows}
            for index, original in enumerate(self._rows):
                for column, (key, _label, editable) in enumerate(self.columns, start=1):
                    if editable:
                        original[key] = self.table.item(index, column).text().strip()
                saved = by_key.get(_row_key(original))
                if not saved:
                    continue
                for key in (
                    "status", "reason", "reason_code", "reason_params",
                    "library_item_id", "work_id", "parsed_text_id",
                ):
                    if key in saved:
                        original[key] = saved[key]
                status_item = self.table.item(index, 2)
                if status_item is not None:
                    raw_status = str(saved.get("status") or "needs_review")
                    status_item.setText(
                        text.get(f"library_status_{raw_status}", text["library_status_other"])
                    )
                    status_item.setData(QtCore.Qt.ItemDataRole.UserRole, raw_status)
                    status_item.setToolTip(_row_reason_text(text, original))
                reason_item = self.table.item(index, 10)
                if reason_item is not None:
                    reason_item.setText(_row_reason_text(text, original))
                    reason_item.setToolTip("")
                if self._is_stored(original):
                    checkbox = self.table.item(index, 0)
                    checkbox.setCheckState(QtCore.Qt.CheckState.Unchecked)
                    checkbox.setFlags(
                        checkbox.flags() & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable
                    )
                    for column, (_key, _label, editable) in enumerate(self.columns, start=1):
                        if editable:
                            item = self.table.item(index, column)
                            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                elif original.get("status") != "identified":
                    checkbox = self.table.item(index, 0)
                    checkbox.setCheckState(QtCore.Qt.CheckState.Unchecked)
                    checkbox.setFlags(checkbox.flags() & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            self.table.setEnabled(True)
            self._color_statuses()
            self.table.resizeRowsToContents()
            self._sync_select_all()
            self._sync_identity_button()

        def set_dark_theme(self, dark: bool):
            self._dark = bool(dark)
            from .desktop import _stylesheet

            font_size = getattr(parent, "_font_size", 13)
            self.setStyleSheet(
                _stylesheet(dark=self._dark, font_size=font_size)
                + atlante_stylesheet(dark=self._dark, font_size=font_size)
                + f"""
                QLabel#libraryReviewHelp {{ background: {'#20354d' if self._dark else '#e9f2ff'};
                    border-left: 4px solid {'#7ab4ff' if self._dark else '#175fe6'};
                    border-radius: 4px; padding: 10px 12px; }}
                QLabel#libraryReviewStatus {{ padding: 2px 0; }}
                QProgressBar#libraryImportProgressBar {{ min-height: 14px; border-radius: 7px; }}
                QCheckBox:hover {{ color: {'#7ab4ff' if self._dark else '#175fe6'}; }}
                QCheckBox::indicator:hover {{ background: {'#223b59' if self._dark else '#edf4ff'};
                    border-color: {'#7ab4ff' if self._dark else '#175fe6'}; }}
                QCheckBox::indicator:pressed {{ background: {'#304e70' if self._dark else '#d9e8fc'};
                    border-color: {'#7ab4ff' if self._dark else '#175fe6'}; }}
                """
            )
            self._color_statuses()

        def closeEvent(self, event):  # noqa: N802
            if not self.close_button.isEnabled():
                event.ignore()
                return
            super().closeEvent(event)

    return LibraryReviewDialog()


def library_reason_text(
    text: Mapping[str, str], reason_code: Any, reason_params: Any = None
) -> str:
    """Return a localized reason without exposing backend diagnostics or codes."""
    fallback = text["library_reason_generic"]
    code = str(reason_code or "")
    template = text.get(f"library_reason_{code}")
    if not template:
        return fallback
    if code == "title_conflict":
        params = reason_params if isinstance(reason_params, Mapping) else {}
        return template.format(
            metadata_title=str(params.get("metadata_title") or ""),
            layout_title=str(params.get("layout_title") or ""),
        )
    return template


def _row_reason_text(text: Mapping[str, str], row: Mapping[str, Any]) -> str:
    if not row.get("reason_code") and row.get("reason") in (None, ""):
        return ""
    return library_reason_text(text, row.get("reason_code"), row.get("reason_params"))


def _row_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("_session_id") or ""),
        str(row.get("_file_id") or row.get("sha256") or ""),
    )


def _method_label(method: str, text: Mapping[str, str]) -> str:
    if not method:
        return text["library_method_unavailable"]
    labels = {
        "pymupdf": "library_method_pdf_text",
        "fallback_stdlib": "library_method_basic_text",
        "pdf_metadata_normalized_layout": "library_method_metadata_layout",
        "pdf_layout_prominent_lines": "library_method_layout_title",
        "pdf_metadata": "library_method_pdf_title",
        "front_matter_identifier": "library_method_front_matter",
        "title_author_year": "library_method_title_author_year",
        "doi": "library_method_doi",
        "pmid": "library_method_pmid",
        "conflicting_pdf_title_evidence": "library_method_conflicting_pdf_title_evidence",
    }
    found = [
        text[label] for code, label in labels.items()
        if code in method
    ]
    if method.startswith("ocr:") or "ocr:" in method:
        found.append(text["library_method_ocr"])
    if found:
        return " · ".join(dict.fromkeys(found))
    return text["library_method_local"]
