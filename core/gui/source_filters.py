# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared presentation for source availability and review filters."""
from __future__ import annotations

from pathlib import Path


def configure_source_filter_button(button, QtWidgets):
    """Give source-filter triggers a consistent, responsive touch target."""
    size_policy = button.sizePolicy()
    size_policy.setHorizontalPolicy(QtWidgets.QSizePolicy.Policy.Expanding)
    size_policy.setVerticalPolicy(QtWidgets.QSizePolicy.Policy.Fixed)
    button.setSizePolicy(size_policy)
    button.setMinimumWidth(max(180, button.sizeHint().width() + 16))
    button.setMinimumHeight(max(42, button.sizeHint().height() + 4))


def create_checkbox_source_menu(
    button,
    QtCore,
    QtGui,
    QtWidgets,
    choices,
    *,
    checked_keys=(),
    clear_label,
    on_toggle,
    on_clear,
    object_name="sourceFilterPopup",
):
    """Attach a roomy, host-bounded checkbox menu to a filter button."""
    class SourceFilterMenu(QtWidgets.QMenu):
        def __init__(self):
            super().__init__(button)
            self.setObjectName(object_name)
            self._source_filter_button = button
            self._source_filter_checks = []
            self.aboutToShow.connect(self._sync_source_filter_presentation)

        def showEvent(self, event):  # noqa: N802 - Qt API name
            super().showEvent(event)
            self._fit_to_host()

        def _sync_source_filter_presentation(self):
            trigger = self._source_filter_button
            trigger.ensurePolished()
            popup_font = QtGui.QFont(trigger.font())
            point_size = popup_font.pointSizeF()
            pixel_size = popup_font.pixelSize()
            if pixel_size > 0:
                popup_font.setPixelSize(pixel_size + 1)
                font_size_css = f"{pixel_size + 1}px"
            elif point_size > 0:
                popup_font.setPointSizeF(point_size + 1)
                font_size_css = f"{point_size + 1}pt"
            else:
                font_size_css = None

            self.setFont(popup_font)
            metrics = QtGui.QFontMetrics(popup_font)
            for checkbox in self._source_filter_checks:
                checkbox.setFont(popup_font)
                checkbox.setMinimumHeight(metrics.height() + 12)

            palette = trigger.palette()
            surface = palette.color(QtGui.QPalette.ColorRole.Button).name()
            accent = palette.color(QtGui.QPalette.ColorRole.ButtonText).name()
            font_rule = (
                f"font-size: {font_size_css}; " if font_size_css is not None else ""
            )
            checkmark = (Path(__file__).parent / "assets" / "checkmark.svg").as_posix()
            self.setStyleSheet(
                f"QMenu#{object_name}, QCheckBox#sourceFilterChoice "
                f"{{ {font_rule} }}"
                f"QCheckBox#sourceFilterChoice::indicator {{ "
                f"width: 18px; height: 18px; border: 2px solid {accent}; "
                f"border-radius: 3px; background-color: {surface}; }}"
                f"QCheckBox#sourceFilterChoice::indicator:checked {{ "
                f"background-color: {accent}; border-color: {accent}; "
                f"image: url(\"{checkmark}\"); }}"
            )

        def _fit_choice_labels(self, menu_width):
            menu_margin = max(
                0,
                self.style().pixelMetric(
                    QtWidgets.QStyle.PixelMetric.PM_MenuHMargin, None, self
                ),
            )
            action_width = max(0, menu_width - 2 * menu_margin)
            for checkbox in self._source_filter_checks:
                label = str(checkbox.property("filterLabel") or "")
                count = checkbox.property("filterCount")
                suffix = f"  {count}" if count is not None else ""
                full_text = f"{label}{suffix}"
                metrics = QtGui.QFontMetrics(checkbox.font())
                current_text_width = metrics.horizontalAdvance(checkbox.text())
                non_text_width = max(
                    0, checkbox.sizeHint().width() - current_text_width
                )
                text_width = max(0, action_width - non_text_width)
                display_text = full_text
                if metrics.horizontalAdvance(full_text) > text_width:
                    suffix_width = metrics.horizontalAdvance(suffix)
                    label_width = max(0, text_width - suffix_width)
                    display_text = (
                        metrics.elidedText(
                            label,
                            QtCore.Qt.TextElideMode.ElideRight,
                            label_width,
                        )
                        + suffix
                    )
                checkbox.setAccessibleName(full_text)
                checkbox.setToolTip(full_text)
                if checkbox.text() != display_text:
                    checkbox.setText(display_text)

        def _fit_to_host(self):
            trigger = self._source_filter_button
            host = trigger.window()
            if host is None or host.size().isEmpty():
                return
            bounds = QtCore.QRect(
                host.mapToGlobal(QtCore.QPoint(0, 0)), host.size(),
            ).adjusted(1, 1, -1, -1)
            if bounds.isEmpty():
                return

            self._fit_choice_labels(bounds.width())
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.adjustSize()
            preferred = self.sizeHint().expandedTo(trigger.size())
            width = min(max(1, preferred.width()), bounds.width())
            height = min(max(1, preferred.height()), bounds.height())
            self.setFixedSize(width, height)

            trigger_top = trigger.mapToGlobal(QtCore.QPoint(0, 0))
            x = trigger_top.x()
            y = trigger_top.y() + trigger.height()
            if y + height > bounds.bottom() + 1:
                y = trigger_top.y() - height
            x = min(max(x, bounds.left()), bounds.right() - width + 1)
            y = min(max(y, bounds.top()), bounds.bottom() - height + 1)
            self.move(x, y)

    menu = SourceFilterMenu()
    checks = {}
    for key, label in choices:
        checkbox = QtWidgets.QCheckBox(label, menu)
        checkbox.setObjectName("sourceFilterChoice")
        checkbox.setFont(QtGui.QFont(button.font()))
        checkbox.setProperty("filterKey", key)
        checkbox.setProperty("filterLabel", label)
        checkbox.setProperty("filterCount", None)
        checkbox.setAccessibleName(label)
        checkbox.setToolTip(label)
        checkbox.setContentsMargins(12, 4, 12, 4)
        checkbox.setMinimumHeight(QtGui.QFontMetrics(checkbox.font()).height() + 12)
        checkbox.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        checkbox.setChecked(key in checked_keys)
        checkbox.toggled.connect(
            lambda checked, key=key: on_toggle(key, checked)
        )
        action = QtWidgets.QWidgetAction(menu)
        action.setDefaultWidget(checkbox)
        menu.addAction(action)
        checks[key] = checkbox
        menu._source_filter_checks.append(checkbox)

    menu.addSeparator()
    clear_action = menu.addAction(clear_label)
    clear_action.setObjectName("clearSourceFiltersAction")
    clear_action.triggered.connect(on_clear)
    button.setMenu(menu)
    return menu, checks


def set_source_filter_count(checkbox, count):
    """Show an inventory count without changing the checkbox's source label."""
    checkbox.setProperty("filterCount", count)
    full_text = f"{checkbox.property('filterLabel')}  {count}"
    checkbox.setAccessibleName(full_text)
    checkbox.setToolTip(full_text)
    checkbox.setText(full_text)


def refresh_checkbox_source_menu(menu):
    """Resize an open popup after its counted labels change."""
    if menu is not None and menu.isVisible():
        menu._fit_to_host()
