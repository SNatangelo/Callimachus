# core/gui/settings/transfer.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Dialogs for explicit settings export, replacement and reset."""
from __future__ import annotations


def options(parent, QtWidgets, text, action, *, encrypted=False):
    """Return operation options only after explicit confirmation."""
    dialog = QtWidgets.QDialog(parent)
    dialog.setObjectName("settingsTransferDialog")
    dialog.setWindowTitle(text["settings_" + action])
    dialog.setMinimumWidth(520)
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.setContentsMargins(20, 12, 20, 16)
    layout.setSpacing(12)
    note_key = "settings_import_encrypted_note" if action == "import" and encrypted else "settings_" + action + "_note"
    note = QtWidgets.QLabel(text[note_key], dialog)
    note.setWordWrap(True)
    layout.addWidget(note)
    result = {}
    password = None
    if action == "export" or (action == "import" and encrypted):
        if action == "export":
            encrypted = QtWidgets.QCheckBox(text["settings_encrypt"], dialog)
            encrypted.setObjectName("encryptSettings")
            encrypted.setChecked(True)
            layout.addWidget(encrypted)
        password = QtWidgets.QLineEdit(dialog)
        password.setObjectName("settingsPassword")
        password.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        password.setPlaceholderText(text["settings_password"])
        layout.addWidget(password)
        if action == "export":
            repeat = QtWidgets.QLineEdit(dialog)
            repeat.setObjectName("settingsPasswordRepeat")
            repeat.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            repeat.setPlaceholderText(text["settings_password_repeat"])
            layout.addWidget(repeat)
            encrypted.toggled.connect(password.setEnabled)
            encrypted.toggled.connect(repeat.setEnabled)
        warning = QtWidgets.QLabel(text["settings_password_warning"], dialog)
        warning.setWordWrap(True)
        layout.addWidget(warning)
    elif action == "reset":
        keep = QtWidgets.QCheckBox(text["settings_keep_keys"], dialog)
        keep.setObjectName("keepSettingsKeys")
        keep.setChecked(True)
        layout.addWidget(keep)
    error = QtWidgets.QLabel(dialog)
    error.setObjectName("errorText")
    error.setWordWrap(True)
    error.hide()
    layout.addWidget(error)
    buttons = QtWidgets.QDialogButtonBox(dialog)
    buttons.addButton(text["atlas_cancel"], QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
    confirm = buttons.addButton(text["settings_" + action], QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
    confirm.setObjectName("dangerButton" if action in {"reset", "import"} else "primaryButton")
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    def accept():
        if action == "export":
            value = password.text() if encrypted.isChecked() else None
            if value is not None and (len(value) < 6 or value != repeat.text()):
                error.setText(text["settings_password_invalid"])
                error.show()
                dialog.adjustSize()
                return
            result["password"] = value
        elif action == "import":
            result["password"] = (password.text() or None) if password is not None else None
        else:
            result["keep_api_keys"] = keep.isChecked()
        dialog.accept()
    confirm.clicked.connect(accept)
    accepted = dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
    if password is not None:
        password.clear()
        if action == "export":
            repeat.clear()
    dialog.deleteLater()
    # Passwords are kept only in this operation's payload, never preferences.
    return result if accepted else None
