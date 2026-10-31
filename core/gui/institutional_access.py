# core/gui/institutional_access.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional login-only browser dialog for institutional Fetch access."""
from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from core.fetch.institutional_access import (
    ENV_ENABLED,
    ENV_INSTITUTION,
    ENV_LOGIN_URL,
    ENV_PROFILE,
)

_PROFILE_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,47})$")
_PROMPT_SUPPRESSED_KEY = "institutional_access/dont_ask_again"
_PROFILE_HISTORY_KEY = "institutional_access/profile_history"
_PROFILE_HISTORY_LIMIT = 8

# These destinations let a user navigate to their institution's sign-in page.
# They are not tied to a university or treated as proof of access.
DEFAULT_LIBRARY_PAGES = (
    ("ScienceDirect", "https://www.sciencedirect.com/user/institution/login"),
    ("Springer", "https://link.springer.com/"),
    ("Nature", "https://www.nature.com/"),
    ("Wiley", "https://onlinelibrary.wiley.com/"),
)


def sanitize_profile_slug(value: str) -> str | None:
    """Return a valid profile slug, without silently rewriting user input."""
    slug = str(value or "").strip().lower()
    return slug if _PROFILE_SLUG.fullmatch(slug) else None


def valid_login_url(value: str) -> bool:
    url = str(value or "").strip()
    if not url:
        return True
    from core.fetch.institutional_access import config as access_config
    try:
        access_config({ENV_LOGIN_URL: url})
    except ValueError:
        return False
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in url):
        return False
    try:
        parsed = urlsplit(url)
        _ = parsed.port
        return bool(
            parsed.scheme.casefold() == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not any(character.isspace() for character in url)
        )
    except ValueError:
        return False


def prompt_is_suppressed(settings: Any) -> bool:
    value = settings.value(_PROMPT_SUPPRESSED_KEY, False)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def suppress_prompt(settings: Any) -> None:
    settings.setValue(_PROMPT_SUPPRESSED_KEY, True)
    settings.sync()


def reset_prompt_preference(settings: Any) -> None:
    settings.remove(_PROMPT_SUPPRESSED_KEY)
    settings.sync()


def profile_history(settings: Any) -> list[str]:
    value = settings.value(_PROFILE_HISTORY_KEY, [])
    values = value if isinstance(value, (list, tuple)) else [value]
    result: list[str] = []
    for item in values:
        slug = sanitize_profile_slug(str(item or ""))
        if slug and slug not in result:
            result.append(slug)
    return result[:_PROFILE_HISTORY_LIMIT]


def remember_profile(settings: Any, profile: str) -> None:
    slug = sanitize_profile_slug(profile)
    if not slug:
        return
    history = [slug, *[item for item in profile_history(settings) if item != slug]]
    settings.setValue(_PROFILE_HISTORY_KEY, history[:_PROFILE_HISTORY_LIMIT])
    settings.sync()


def _default_session_factory(*args, **kwargs):
    from core.fetch.fallbacks.fetch_modes.interactive_browser import (
        AsyncInteractiveBrowserSession,
    )

    return AsyncInteractiveBrowserSession(*args, **kwargs)


def create_institutional_login_dialog(
    parent,
    *,
    text: Mapping[str, str],
    config: Mapping[str, Any] | None = None,
    settings_saver: Callable[[Mapping[str, str]], Any] | None = None,
    session_factory: Callable[..., Any] | None = None,
    settings: Any = None,
):
    """Build a non-modal login dialog whose worker closes before completion."""
    from PySide6 import QtCore, QtGui, QtWidgets

    initial = dict(config or {})
    if settings is None:
        settings = QtCore.QSettings("Callimachus", "Callimachus")
    session_factory = session_factory or _default_session_factory

    class LoginWorker(QtCore.QThread):
        browser_ready = QtCore.Signal(bool, str)

        def __init__(
            self, profile: str, publisher_urls: list[str], parent=None
        ):
            super().__init__(parent)
            self.profile = profile
            self.publisher_urls = list(publisher_urls)
            self._finish_requested = threading.Event()
            self._success_requested = False
            self.success = False
            self.error = ""
            self._session = None

        def request_finish(self, *, success: bool) -> None:
            self._success_requested = bool(success)
            self._finish_requested.set()

        def run(self):
            try:
                self._session = session_factory(
                    None, login_only=True, profile_id=self.profile
                )
                result = self._session.start()
                if not isinstance(result, Mapping) or result.get("available") is not True:
                    reason = result.get("reason") if isinstance(result, Mapping) else None
                    self.error = str(reason or text["institutional_browser_unavailable"])
                    self.browser_ready.emit(False, self.error)
                    return

                if self._finish_requested.is_set():
                    return

                try:
                    opened_pages = self._session.open_login_pages(self.publisher_urls)
                except Exception as exc:
                    opened_pages = {"opened": [], "failed": [
                        {"url": url, "reason": "navigation_failed"}
                        for url in self.publisher_urls
                    ]}
                if self._finish_requested.is_set():
                    return

                opened = opened_pages.get("opened") if isinstance(opened_pages, Mapping) else None
                failed = opened_pages.get("failed") if isinstance(opened_pages, Mapping) else None
                if not isinstance(opened, list) or not isinstance(failed, list):
                    self.error = text["institutional_browser_unavailable"]
                    self.browser_ready.emit(False, self.error)
                    return

                failed_hosts = []
                for failure in failed:
                    if not isinstance(failure, Mapping):
                        continue
                    host = urlsplit(str(failure.get("url") or "")).hostname
                    if host and host not in failed_hosts:
                        failed_hosts.append(host)
                if not opened:
                    self.error = text["institutional_browser_failed"].format(
                        reason=", ".join(failed_hosts)
                        or text["institutional_browser_unavailable"]
                    )
                    self.browser_ready.emit(False, self.error)
                    return

                warning = ""
                if failed_hosts:
                    warning = text["institutional_browser_partial"].format(
                        domains=", ".join(failed_hosts)
                    )

                self.browser_ready.emit(True, warning)
                self._finish_requested.wait()
                self.success = self._success_requested
            except Exception as exc:
                self.error = str(exc) or text["institutional_browser_unavailable"]
                self.browser_ready.emit(False, self.error)
            finally:
                if self._session is not None:
                    try:
                        self._session.close()
                    except Exception as exc:
                        self.success = False
                        self.error = self.error or str(exc)
                self._session = None

    class InstitutionalLoginDialog(QtWidgets.QDialog):
        completed = QtCore.Signal(str, str)

        def __init__(self):
            super().__init__(parent)
            self.setObjectName("institutionalLoginDialog")
            self.setWindowTitle(text["institutional_dialog_title"])
            self.setModal(False)
            screen = parent.screen() if parent is not None else None
            screen = screen or QtWidgets.QApplication.primaryScreen()
            available = (
                screen.availableGeometry()
                if screen is not None
                else QtCore.QRect(0, 0, 740, 800)
            )
            dialog_width = max(320, min(700, available.width() - 40))
            dialog_height = max(320, min(780, available.height() - 40))
            self.setMinimumSize(0, 0)
            self.resize(dialog_width, dialog_height)
            self._worker = None
            self._requested_outcome: str | None = None
            self._finalized = False

            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(26, 20, 26, 22)
            layout.setSpacing(14)

            header_widget = QtWidgets.QWidget(self)
            header_widget.setObjectName("institutionalDialogHeader")
            header = QtWidgets.QHBoxLayout(header_widget)
            header.setContentsMargins(0, 0, 0, 0)
            self._header_layout = header
            self.heading = QtWidgets.QLabel(text["institutional_dialog_title"], self)
            self.heading.setObjectName("institutionalDialogTitle")
            self.heading.setWordWrap(True)
            header.addWidget(self.heading, 1)
            from .atlas_icons import icon as atlas_icon
            dark = bool(getattr(parent, "_dark", False))
            palette = {
                "background": "#101722" if dark else "#ffffff",
                "surface": "#1f2b3d" if dark else "#ffffff",
                "text": "#edf3ff" if dark else "#172554",
                "muted": "#b6c4d8" if dark else "#7180a4",
                "border": "#364152" if dark else "#d7deed",
                "blue": "#8db8ff" if dark else "#1268df",
                "accent": "#243e62" if dark else "#eaf2ff",
            }
            self.close_button = QtWidgets.QToolButton(self)
            self.close_button.setObjectName("institutionalDialogClose")
            self.close_button.setIcon(atlas_icon(
                QtCore, QtGui, "close", color=palette["muted"], size=22,
            ))
            self.close_button.setIconSize(QtCore.QSize(22, 22))
            self.close_button.setFixedSize(36, 36)
            self.close_button.setToolTip(text.get("close", "Close"))
            self.close_button.setAccessibleName(text.get("close", "Close"))
            self.close_button.clicked.connect(self.close)
            self._navigation_color = palette["muted"]
            header.addWidget(self.close_button, 0, QtCore.Qt.AlignmentFlag.AlignTop)
            layout.addWidget(header_widget)

            separator = QtWidgets.QFrame(self)
            separator.setFrameShape(QtWidgets.QFrame.Shape.HLine)
            separator.setObjectName("institutionalDialogSeparator")
            layout.addWidget(separator)

            self.body_scroll = QtWidgets.QScrollArea(self)
            self.body_scroll.setObjectName("institutionalBodyScroll")
            self.body_scroll.setWidgetResizable(True)
            self.body_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            self.body_scroll.setVerticalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            self.body_scroll.setHorizontalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self.body_content = QtWidgets.QWidget(self.body_scroll)
            self.body_content.setMinimumWidth(0)
            self.body_content.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
            self.body_scroll.setWidget(self.body_content)
            body_layout = QtWidgets.QVBoxLayout(self.body_content)
            body_layout.setContentsMargins(0, 0, 0, 0)
            body_layout.setSpacing(14)
            layout.addWidget(self.body_scroll, 1)

            intro = QtWidgets.QLabel(text["institutional_dialog_help"], self)
            intro.setObjectName("institutionalDialogIntro")
            intro.setWordWrap(True)
            intro.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            body_layout.addWidget(intro)

            profile_row = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.LeftToRight)
            self._profile_row = profile_row
            profile_row.setContentsMargins(0, 0, 0, 0)
            self.profile_label = QtWidgets.QLabel(text["institutional_profile"], self)
            self.profile_label.setObjectName("institutionalProfileLabel")
            self.profile_label.setWordWrap(True)
            self.profile_label.setMaximumWidth(
                420 if dialog_width >= 900 else 240
            )
            profile_row.addWidget(self.profile_label)
            self.profile = QtWidgets.QComboBox(self)
            self.profile.setObjectName("institutionalProfile")
            self.profile.setEditable(True)
            self.profile.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
            self.profile.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            self.profile.setMinimumContentsLength(10)
            self.profile.setMaximumWidth(600)
            self.profile.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            history = profile_history(settings)
            selected_profile = sanitize_profile_slug(str(initial.get("profile") or "guided_fetch"))
            if selected_profile and selected_profile not in history:
                history.insert(0, selected_profile)
            self.profile.addItems(history)
            self.profile.setCurrentText(selected_profile or "guided_fetch")
            self.profile.setToolTip(text["institutional_profile_help"])
            profile_editor = QtWidgets.QWidget(self)
            profile_editor.setMinimumWidth(0)
            profile_editor_layout = QtWidgets.QHBoxLayout(profile_editor)
            profile_editor_layout.setContentsMargins(0, 0, 0, 0)
            profile_editor_layout.addWidget(self.profile, 1)
            profile_row.addWidget(profile_editor, 1)
            self.profile_help_button = QtWidgets.QToolButton(self)
            self.profile_help_button.setObjectName("institutionalProfileHelpButton")
            self.profile_help_button.setIcon(atlas_icon(
                QtCore, QtGui, "help", color=palette["muted"], size=20,
            ))
            self.profile_help_button.setIconSize(QtCore.QSize(20, 20))
            self.profile_help_button.setFixedSize(32, 32)
            self.profile_help_button.setToolTip(text["institutional_profile_help"])
            self.profile_help_button.setAccessibleName(
                text.get("setting_help_for", "Help for {setting}").format(
                    setting=text["institutional_profile"]
                )
            )
            self.profile_help_button.setAccessibleDescription(
                text["institutional_profile_help"]
            )
            profile_editor_layout.addWidget(self.profile_help_button)
            body_layout.addLayout(profile_row)

            self.publishers_heading = QtWidgets.QLabel(
                text["institutional_publisher"], self,
            )
            self.publishers_heading.setObjectName("institutionalPublishersHeading")
            body_layout.addWidget(self.publishers_heading)
            self.publishers = QtWidgets.QListWidget(self)
            self.publishers.setObjectName("institutionalPublishers")
            self.publishers.setIconSize(QtCore.QSize(26, 26))
            self.publishers.setMinimumHeight(204)
            self.publishers.setMaximumHeight(224)
            self.publishers.setSpacing(0)
            self.publishers.setSelectionMode(
                QtWidgets.QAbstractItemView.SelectionMode.NoSelection
            )
            for label, url in DEFAULT_LIBRARY_PAGES:
                item = QtWidgets.QListWidgetItem(label)
                item.setIcon(atlas_icon(QtCore, QtGui, "publisher_" + label.lower(), color=palette["muted"], size=26))
                item.setData(QtCore.Qt.ItemDataRole.UserRole, url)
                item.setFlags(
                    item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(QtCore.Qt.CheckState.Checked)
                item.setSizeHint(QtCore.QSize(0, 46))
                self.publishers.addItem(item)
            self.publishers.setToolTip(text["institutional_publishers_help"])
            body_layout.addWidget(self.publishers)

            self.publishers_help = QtWidgets.QLabel(
                text["institutional_publishers_help"], self
            )
            self.publishers_help.setObjectName("institutionalPublishersHelp")
            self.publishers_help.setWordWrap(True)
            self.publishers_help.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            body_layout.addWidget(self.publishers_help)

            self.access_button = QtWidgets.QPushButton(
                text["institutional_access_button"], self
            )
            self.access_button.setObjectName("institutionalAccessButton")
            self.access_button.setIcon(atlas_icon(
                QtCore, QtGui, "external", color="#ffffff", size=20,
            ))
            self.access_button.setIconSize(QtCore.QSize(20, 20))
            self.access_button.setMinimumHeight(48)
            self.access_button.clicked.connect(self._start_login)
            access_row = QtWidgets.QHBoxLayout()
            access_row.setContentsMargins(0, 0, 0, 0)
            access_row.addWidget(self.access_button)
            access_row.addStretch(1)
            body_layout.addStretch(1)
            body_layout.addLayout(access_row)

            self.information = QtWidgets.QFrame(self)
            self.information.setObjectName("institutionalInfoBanner")
            information_layout = QtWidgets.QHBoxLayout(self.information)
            information_layout.setContentsMargins(14, 10, 14, 10)
            information_layout.setSpacing(10)
            self.information_icon = QtWidgets.QLabel(self.information)
            self.information_icon.setObjectName("institutionalInfoIcon")
            self.information_icon.setPixmap(atlas_icon(
                QtCore, QtGui, "help", color=palette["blue"], size=22,
            ).pixmap(QtCore.QSize(22, 22)))
            information_layout.addWidget(
                self.information_icon, 0, QtCore.Qt.AlignmentFlag.AlignTop,
            )
            self.information_text = QtWidgets.QLabel(
                text["institutional_access_optional"], self.information,
            )
            self.information_text.setObjectName("institutionalInfoText")
            self.information_text.setWordWrap(True)
            self.information_text.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            information_layout.addWidget(self.information_text, 1)
            body_layout.addWidget(self.information)

            self.status = QtWidgets.QLabel("", self)
            self.status.setObjectName("institutionalLoginStatus")
            self.status.setWordWrap(True)
            self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            body_layout.addWidget(self.status)

            buttons = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.LeftToRight)
            self._footer_buttons = buttons
            buttons.setContentsMargins(0, 0, 0, 0)
            buttons.setSpacing(10)
            self.never_button = QtWidgets.QPushButton(text["institutional_never_button"], self)
            self.never_button.setObjectName("institutionalNeverButton")
            self.never_button.clicked.connect(self._never_ask_again)
            buttons.addWidget(self.never_button)
            self._footer_spacer = QtWidgets.QSpacerItem(0, 0, QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Minimum)
            buttons.addItem(self._footer_spacer)
            self.continue_button = QtWidgets.QPushButton(text["institutional_continue_button"], self)
            self.continue_button.setObjectName("institutionalContinueButton")
            self.continue_button.clicked.connect(lambda: self._request_close("continue"))
            buttons.addWidget(self.continue_button)
            self.finished_button = QtWidgets.QPushButton(text["institutional_finished_button"], self)
            self.finished_button.setObjectName("institutionalFinishedButton")
            self.finished_button.setEnabled(False)
            self.finished_button.clicked.connect(self._finish_login)
            buttons.addWidget(self.finished_button)
            footer_separator = QtWidgets.QFrame(self)
            footer_separator.setFrameShape(QtWidgets.QFrame.Shape.HLine)
            footer_separator.setObjectName("institutionalFooterSeparator")
            layout.addWidget(footer_separator)
            layout.addLayout(buttons)

            self.setStyleSheet(
                "QLabel#institutionalDialogTitle { font-size:30px; font-weight:700; }"
                "QLabel#institutionalDialogIntro { color:%(muted)s; font-size:16px; }"
                "QLabel#institutionalProfileLabel { font-weight:600; }"
                "QLabel#institutionalPublishersHeading { font-size:20px; font-weight:650; }"
                "QLabel#institutionalPublishersHelp { color:%(muted)s; font-size:15px; }"
                "QLabel#institutionalInfoText { color:%(text)s; font-size:15px; }"
                "QLabel#institutionalLoginStatus { color:%(muted)s; }"
                "QFrame#institutionalDialogSeparator { color:%(border)s; }"
                "QFrame#institutionalFooterSeparator { color:%(border)s; }"
                "QFrame#institutionalInfoBanner { background:%(accent)s; "
                "border:1px solid %(border)s; border-radius:9px; }"
                "QLabel#institutionalInfoIcon { background:transparent; }"
                "QListWidget#institutionalPublishers::item { padding:6px 8px; "
                "border-bottom:1px solid %(border)s; }"
                "QListWidget#institutionalPublishers::indicator { width:19px; height:19px; "
                "border:1px solid %(border)s; border-radius:5px; background:%(surface)s; }"
                "QListWidget#institutionalPublishers::indicator:checked { background:%(blue)s; "
                "border-color:%(blue)s; image:url(%(checkmark)s); }"
                "QPushButton#institutionalAccessButton { color:white; background:%(blue)s; "
                "border:1px solid %(blue)s; border-radius:7px; padding:9px 18px; font-weight:600; }"
                "QPushButton#institutionalAccessButton:hover { background:%(muted)s; border-color:%(blue)s; }"
                "QPushButton#institutionalAccessButton:pressed { background:%(text)s; border-color:%(blue)s; }"
                "QPushButton#institutionalContinueButton, QPushButton#institutionalNeverButton { "
                "color:%(blue)s; background:%(surface)s; border:1px solid %(blue)s; "
                "border-radius:7px; padding:8px 14px; font-weight:600; }"
                "QPushButton#institutionalContinueButton:hover, QPushButton#institutionalNeverButton:hover { background:%(accent)s; }"
                "QPushButton#institutionalContinueButton:pressed, QPushButton#institutionalNeverButton:pressed { background:%(border)s; }"
                "QToolButton#institutionalDialogClose, QToolButton#institutionalProfileHelpButton { "
                "background:transparent; border:0; padding:4px; }"
                "QToolButton#institutionalDialogClose:hover, QToolButton#institutionalProfileHelpButton:hover { background:%(accent)s; }"
                "QToolButton#institutionalDialogClose:pressed, QToolButton#institutionalProfileHelpButton:pressed { background:%(border)s; }"
                "QListWidget#institutionalPublishers::item:hover { background:%(accent)s; }"
                "QListWidget#institutionalPublishers::indicator:pressed { background:%(accent)s; border-color:%(blue)s; }"
                "QPushButton#institutionalFinishedButton:disabled { color:%(muted)s; "
                "background:%(border)s; border-color:%(border)s; }"
                % {
                    **palette,
                    "checkmark": str(
                        Path(__file__).parent / "assets" / "checkmark.svg"
                    ).replace("\\", "/"),
                }
            )
            self._body_viewport = self.body_scroll.viewport()
            self._body_viewport.installEventFilter(self)
            self._update_header_navigation()
            if parent is not None:
                parent.installEventFilter(self)
                self._fit_to_parent()

        def _fit_to_parent(self):
            if parent is None:
                return
            central = getattr(parent, "centralWidget", None)
            host = central() if callable(central) else parent
            host = host or parent
            rect = QtCore.QRect(host.mapToGlobal(host.contentsRect().topLeft()), host.contentsRect().size())
            brand = getattr(parent, "brand_logo", None)
            if brand is not None and brand.isVisible():
                header = brand.parentWidget()
                rect.setTop(max(rect.top(), header.mapToGlobal(QtCore.QPoint(0, header.height())).y()))
            screen = parent.screen() or QtWidgets.QApplication.primaryScreen()
            if screen is not None:
                rect = rect.intersected(screen.availableGeometry())
            if rect.isEmpty():
                return

            compact = rect.width() <= 700 or rect.height() <= 620
            width = rect.width() if compact else min(700, round(rect.width() * .9))
            self.resize(width, self.height())
            self._update_header_navigation(width)
            self.layout().activate()

            body_layout = self.body_content.layout()
            body_width = max(0, self.body_scroll.width())
            body_height = (
                body_layout.heightForWidth(body_width)
                if body_layout.hasHeightForWidth()
                else body_layout.sizeHint().height()
            )
            if body_height < 0:
                body_height = body_layout.sizeHint().height()
            natural_height = max(
                0,
                self.layout().sizeHint().height()
                - self.body_scroll.sizeHint().height()
                + body_height,
            )
            frame_height = max(
                0, self.frameGeometry().height() - self.height()
            )
            height = (
                rect.height()
                if compact
                else min(rect.height(), natural_height + frame_height)
            )
            rect = QtCore.QRect(
                rect.x() + (rect.width() - width) // 2,
                rect.y() + (rect.height() - height) // 2,
                width,
                height,
            )

            self.setMinimumSize(0, 0)
            frame, client = self.frameGeometry(), self.geometry()
            self.setGeometry(rect.adjusted(max(0, client.left() - frame.left()),
                                           max(0, client.top() - frame.top()),
                                           -max(0, frame.right() - client.right()),
                                           -max(0, frame.bottom() - client.bottom())))

        def eventFilter(self, watched, event):
            if watched is parent and self.isVisible() and event.type() in {
                QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Move,
            }:
                QtCore.QTimer.singleShot(0, self, self._fit_to_parent)
            elif (watched is self._body_viewport
                  and event.type() == QtCore.QEvent.Type.Resize):
                QtCore.QTimer.singleShot(0, self, self._update_header_navigation)
            return super().eventFilter(watched, event)

        def showEvent(self, event):
            super().showEvent(event)
            self._fit_to_parent()

        def _update_header_navigation(self, available_width=None):
            if not hasattr(self, "_footer_buttons"):
                return
            from .atlas_icons import icon as atlas_icon

            width = self.width() if available_width is None else available_width
            compact = width < 900
            self.layout().setContentsMargins(
                *((16, 16, 16, 16) if compact else (26, 20, 26, 22))
            )
            available = width - self.layout().contentsMargins().left() - self.layout().contentsMargins().right()
            footer = self._footer_buttons
            required = sum(button.sizeHint().width() for button in (
                self.never_button, self.continue_button, self.finished_button,
            )) + footer.spacing() * 3
            stacked = available < required
            direction = (QtWidgets.QBoxLayout.Direction.TopToBottom if stacked
                         else QtWidgets.QBoxLayout.Direction.LeftToRight)
            if footer.direction() != direction:
                footer.setDirection(direction)
                self._footer_spacer.changeSize(0, 0,
                    QtWidgets.QSizePolicy.Policy.Minimum if stacked else QtWidgets.QSizePolicy.Policy.Expanding,
                    QtWidgets.QSizePolicy.Policy.Minimum)
                footer.invalidate()
            profile_required = self.profile_label.sizeHint().width() + self.profile.minimumSizeHint().width() + self.profile_help_button.width() + 24
            profile_available = min(available, self.body_scroll.viewport().width())
            self._profile_row.setDirection(
                QtWidgets.QBoxLayout.Direction.TopToBottom if profile_available < profile_required
                else QtWidgets.QBoxLayout.Direction.LeftToRight)
            index = self._header_layout.count() - 1
            if self._header_layout.indexOf(self.close_button) != index:
                self._header_layout.removeWidget(self.close_button)
                self._header_layout.insertWidget(
                    index, self.close_button, 0, QtCore.Qt.AlignmentFlag.AlignTop,
                )
            label = text.get("close", "Close")
            self.close_button.setProperty("atlasIcon", "close")
            self.close_button.setToolTip(label)
            self.close_button.setAccessibleName(label)
            self.close_button.setIcon(atlas_icon(
                QtCore, QtGui, "close",
                color=self._navigation_color, size=22,
            ))
            self.close_button.setIconSize(QtCore.QSize(22, 22))

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._update_header_navigation()

        def _start_login(self):
            if self._worker is not None:
                return
            slug = sanitize_profile_slug(self.profile.currentText())
            if not slug:
                self.status.setText(text["institutional_profile_invalid"])
                return
            publisher_urls = [
                str(self.publishers.item(index).data(QtCore.Qt.ItemDataRole.UserRole) or "")
                for index in range(self.publishers.count())
                if self.publishers.item(index).checkState() == QtCore.Qt.CheckState.Checked
            ]
            if not publisher_urls:
                self.status.setText(text["institutional_no_destinations"])
                return
            if not all(valid_login_url(url) for url in publisher_urls):
                self.status.setText(text["institutional_configuration_invalid"])
                return

            updates = {
                ENV_ENABLED: "1",
                ENV_PROFILE: slug,
            }
            if settings_saver is not None:
                try:
                    settings_saver(updates)
                except (OSError, RuntimeError, ValueError) as exc:
                    self.status.setText(
                        text["institutional_save_failed"].format(reason=str(exc))
                    )
                    return

            remember_profile(settings, slug)
            self.status.setText(text["institutional_browser_starting"])
            for editor in (self.profile, self.publishers):
                editor.setEnabled(False)
            self.access_button.setEnabled(False)
            self.continue_button.setEnabled(False)
            self.never_button.setEnabled(False)
            self._worker = LoginWorker(
                slug, publisher_urls, self
            )
            self._worker.browser_ready.connect(self._browser_ready)
            self._worker.finished.connect(self._worker_finished)
            self._worker.start()

        def _browser_ready(self, available: bool, reason: str):
            if available:
                if self._requested_outcome is not None:
                    return
                self.status.setText(
                    reason + "\n" + text["institutional_browser_open"]
                    if reason else text["institutional_browser_open"]
                )
                self.finished_button.setEnabled(True)
                return
            self.status.setText(
                text["institutional_browser_failed"].format(reason=reason)
            )
            self._requested_outcome = "failed"

        def _finish_login(self):
            if self._worker is None:
                return
            self.status.setText(text["institutional_browser_closing"])
            self._requested_outcome = "prepared"
            self.finished_button.setEnabled(False)
            self._worker.request_finish(success=True)

        def _never_ask_again(self):
            suppress_prompt(settings)
            self._request_close("never")

        def _request_close(self, outcome: str):
            if self._finalized:
                return
            if self._worker is not None and self._worker.isRunning():
                if self._requested_outcome is None:
                    self._requested_outcome = outcome
                self.status.setText(text["institutional_browser_closing"])
                for button in (self.access_button, self.finished_button, self.continue_button, self.never_button):
                    button.setEnabled(False)
                self._worker.request_finish(success=False)
                return
            self._finalize(outcome, "")

        def _worker_finished(self):
            if self._finalized:
                return
            worker = self._worker
            outcome = self._requested_outcome
            profile = worker.profile if worker is not None and worker.success else ""
            if outcome == "prepared" and not profile:
                outcome = "failed"
            if outcome is None:
                outcome = "failed"
            self._finalize(outcome, profile)

        def _finalize(self, outcome: str, profile: str):
            if self._finalized:
                return
            self._finalized = True
            self.completed.emit(outcome, profile)
            self.accept()

        def closeEvent(self, event):
            if self._finalized:
                event.accept()
                return
            event.ignore()
            self._request_close("continue")

    return InstitutionalLoginDialog()


def institutional_environment_overlay(config: Mapping[str, Any]) -> dict[str, str]:
    """Translate the sanitized GUI projection back to the child-process env."""
    return {
        ENV_ENABLED: "1" if config.get("enabled") else "0",
        ENV_PROFILE: str(config.get("profile") or "guided_fetch"),
        ENV_INSTITUTION: str(config.get("institution") or ""),
        ENV_LOGIN_URL: str(config.get("login_url") or ""),
    }
