# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Short themed notifications with natural height and native macOS frames."""
from __future__ import annotations


def compact_message_dialog(parent, QtCore, QtGui, QtWidgets, *, title, message,
                           confirm_label, dark=False, cancel_label=None, path=None,
                           object_name="compactMessageDialog", icon_name="help",
                           details=None, details_label="Details"):
    from core.gui.atlas_icons import icon

    dialog = QtWidgets.QDialog(parent)
    dialog.setObjectName(object_name)
    dialog.setProperty("compactMessageDialog", True)
    dialog.setWindowTitle(title)
    dialog.setFont(parent.font())
    dialog.setStyleSheet(parent.styleSheet())
    screen = parent.screen() or QtWidgets.QApplication.primaryScreen()
    screen_width = screen.availableGeometry().width() if screen else 800
    width = min(520, max(240, parent.width() - 40), max(240, screen_width - 40))
    root = QtWidgets.QVBoxLayout(dialog)
    root.setContentsMargins(20, 18, 20, 18)
    root.setSpacing(16)
    root.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetFixedSize)
    content = QtWidgets.QHBoxLayout()
    content.setSpacing(12)
    symbol = QtWidgets.QLabel(dialog)
    symbol.setObjectName("messageIcon")
    symbol.setPixmap(icon(QtCore, QtGui, icon_name,
                          color="#e6edf6" if dark else "#175fe6", size=32).pixmap(32, 32))
    symbol.setFixedSize(32, 32)
    content.addWidget(symbol, 0, QtCore.Qt.AlignmentFlag.AlignTop)
    label = QtWidgets.QLabel(message, dialog)
    label.setObjectName("messageBody")
    label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setFixedWidth(max(100, width - 84))
    content.addWidget(label)
    root.addLayout(content)
    if path is not None:
        location = QtWidgets.QLabel(path, dialog)
        location.setObjectName("messagePath")
        location.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        location.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        location.setWordWrap(True)
        location.setFixedWidth(max(140, width - 40))
        root.addWidget(location)
    if details:
        toggle = QtWidgets.QToolButton(dialog)
        toggle.setObjectName("messageDetailsToggle")
        toggle.setText(details_label)
        toggle.setCheckable(True)
        root.addWidget(toggle, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        extra = QtWidgets.QPlainTextEdit(str(details), dialog)
        extra.setObjectName("messageDetails")
        extra.setReadOnly(True)
        extra.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        extra.setFixedWidth(max(140, width - 40))
        extra.setFixedHeight(160)
        extra.hide()
        toggle.toggled.connect(extra.setVisible)
        root.addWidget(extra)
    buttons = QtWidgets.QHBoxLayout()
    buttons.setSpacing(8)
    buttons.addStretch()
    if cancel_label is not None:
        cancel = QtWidgets.QPushButton(cancel_label, dialog)
        cancel.setObjectName("dangerButton")
        cancel.clicked.connect(dialog.reject)
        cancel.setDefault(True)
        buttons.addWidget(cancel)
    confirm = QtWidgets.QPushButton(confirm_label, dialog)
    confirm.setProperty("primaryAction", True)
    confirm.setObjectName("messageConfirm")
    confirm.clicked.connect(dialog.accept)
    if cancel_label is None:
        confirm.setDefault(True)
    buttons.addWidget(confirm)
    root.addLayout(buttons)
    dialog.adjustSize()
    return dialog
