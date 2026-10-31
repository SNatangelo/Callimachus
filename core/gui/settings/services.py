# core/gui/settings/services.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Small controls for source-service credentials and optional API contact."""
from __future__ import annotations

import re
import unicodedata
from core.app.source_service_checks import KEY_PORTALS as SOURCE_KEY_PORTALS

KEY_PORTALS = {
    **SOURCE_KEY_PORTALS,
    "OPENAI_API_KEY": "https://platform.openai.com/api-keys",
    "ANTHROPIC_API_KEY": "https://platform.claude.com/settings/keys",
    "GEMINI_API_KEY": "https://aistudio.google.com/apikey",
    "OPENROUTER_API_KEY": "https://openrouter.ai/keys",
    "DEEPSEEK_API_KEY": "https://platform.deepseek.com/api_keys",
    "GROQ_API_KEY": "https://console.groq.com/keys",
    "XAI_API_KEY": "https://console.x.ai/",
    "MISTRAL_API_KEY": "https://console.mistral.ai/api-keys",
    "ZHIPUAI_API_KEY": "https://open.bigmodel.cn/",
    "OPENCODE_API_KEY": "https://opencode.ai/zen",
    "FREETOKEN_API_KEY": "https://console.freetoken.ai/users/sign_in",
    "ANTHROPIC_AUTH_TOKEN": "https://platform.claude.com/settings/keys",
    "OLLAMA_API_KEY": "https://ollama.com/settings/keys",
    "TYPESAFE_API_KEY": "https://typesafe.ai/",
}

SEARCH_PRESETS = (
    ("Google", "google", "https://www.google.com/search?q={query}"),
    ("Bing", "bing", "https://www.bing.com/search?q={query}"),
    ("DuckDuckGo", "duckduckgo", "https://duckduckgo.com/?q={query}"),
)

# The resolution page consumes service settings in this fixed provider order.
# Names stay separate because some are aliases and others are distinct scopes.
BIBLIOGRAPHIC_SERVICE_GROUPS = (
    ("books", "books-group", frozenset({"GOOGLE_BOOKS_API_KEY"})),
    ("publishers", "publishers-group", frozenset({
        "ELSEVIER_API_KEY", "SPRINGER_NATURE_META_API_KEY",
        "SPRINGER_NATURE_OPEN_ACCESS_API_KEY", "TDM_API_TOKEN",
    })),
    ("aggregators", "aggregators-group", frozenset({
        "CORE_API_KEY", "LENS_API_KEY", "OPENALEX_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY", "ZENODO_ACCESS_TOKEN",
        "BIBLIO_GLUTTON_URL",
    })),
    ("specialists", "specialist-group", frozenset({
        "COURTLISTENER_API_TOKEN", "ENTREZ_API_KEY", "NCBI_API_KEY",
    })),
    ("search", "search-group", frozenset({"MOJEEK_API_KEY", "TAVILY_API_KEY"})),
)

_SERVICE_GROUP_BY_SETTING = {
    name: group_id
    for group_id, _icon_key, names in BIBLIOGRAPHIC_SERVICE_GROUPS
    for name in names
}


def normalize_settings_search(value):
    """Normalize visible setting metadata for deterministic local matching."""
    decomposed = unicodedata.normalize("NFKD", str(value).casefold())
    return "".join(
        character if character.isalnum() else " "
        for character in decomposed
        if not unicodedata.combining(character)
    )


def search_settings(entries, query):
    """Return entries whose visible metadata contains every query token."""
    tokens = normalize_settings_search(query).split()
    if not tokens:
        return []
    return [
        entry for entry in entries
        if all(token in entry.get("search_text", "") for token in tokens)
    ]


def group_service_rows(rows, text):
    """Return ordered, alphabetized service groups and untouched remaining rows."""
    grouped = {group_id: [] for group_id, _icon_key, _names in BIBLIOGRAPHIC_SERVICE_GROUPS}
    remaining = []
    for row in rows:
        name = str(row.get("name") or "")
        group_id = _SERVICE_GROUP_BY_SETTING.get(name)
        if group_id is None:
            remaining.append(row)
        else:
            grouped[group_id].append(row)

    def display_order(row):
        name = str(row.get("name") or "")
        label = text.get(f"setting_{name}_label") or row.get("label") or name
        return str(label).casefold(), name.casefold()

    groups = tuple(
        (group_id, icon_key, tuple(sorted(grouped[group_id], key=display_order)))
        for group_id, icon_key, _names in BIBLIOGRAPHIC_SERVICE_GROUPS
        if grouped[group_id]
    )
    return groups, tuple(remaining)


def valid_email(value):
    return len(value) <= 254 and re.fullmatch(r"[^\s@\x00-\x1f<>]+@[^\s@\x00-\x1f<>]+\.[^\s@\x00-\x1f<>]+", value) is not None


def ask_contact(parent, QtCore, QtWidgets, text, save):
    """Return False on dismissal; an explicit skip permits this launch."""
    class ContactDialog(QtWidgets.QDialog):
        def showEvent(self, event):
            super().showEvent(event)
            self._fit_contact_page()

        def resizeEvent(self, event):
            super().resizeEvent(event)
            if hasattr(self, "_reflow_contact"):
                self._reflow_contact()

        def eventFilter(self, watched, event):
            if event.type() == QtCore.QEvent.Type.Resize:
                if watched is parent or watched is getattr(parent, "pages", None):
                    if self.isVisible():
                        self._fit_contact_page()
                else:
                    self._reflow_contact()
            return super().eventFilter(watched, event)

        def _fit_contact_page(self):
            if parent is None or not parent.isVisible():
                return
            if parent.width() >= 700:
                if getattr(self, "_contact_compact", False):
                    self._contact_compact = False
                    screen = parent.screen()
                    bounds = screen.availableGeometry() if screen is not None else parent.geometry()
                    width = min(parent.width() - 32, bounds.width() - 32,
                                self._contact_preferred_width)
                    height = min(parent.height() - 32, bounds.height() - 32,
                                 self._contact_height_for_width(width))
                    self.setMaximumSize(max(1, parent.width() - 32), max(1, parent.height() - 32))
                    self.resize(max(1, width), max(1, height))
                    self.move(parent.mapToGlobal(parent.rect().center()) - self.rect().center())
                    self._reflow_contact()
                return
            self._contact_compact = True
            page = getattr(parent, "pages", parent)
            bounds = QtCore.QRect(page.mapToGlobal(QtCore.QPoint()), page.size())
            screen = parent.screen()
            if screen is not None:
                bounds = bounds.intersected(screen.availableGeometry())
            self.setMaximumSize(bounds.size())
            self.setGeometry(bounds)
            self.layout().setContentsMargins(0, 0, 0, 0)
            self._reflow_contact()

    dialog = ContactDialog(parent)
    dialog.setObjectName("sourceContactDialog")
    dialog.setWindowTitle(text["source_contact_title"])
    screen = parent.screen() if parent is not None else QtWidgets.QApplication.primaryScreen()
    bounds = screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 570, 330)
    max_width = max(1, bounds.width() - 32)
    max_height = max(1, bounds.height() - 32)
    if parent is not None and parent.isVisible():
        max_width = min(max_width, max(1, parent.width() - 32))
        max_height = min(max_height, max(1, parent.height() - 32))
    dialog.setMaximumSize(max_width, max_height)
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
    scroll = QtWidgets.QScrollArea(dialog)
    scroll.setObjectName("sourceContactScroll")
    scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(
        QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    layout.addWidget(scroll)
    content = QtWidgets.QWidget()
    content_layout = QtWidgets.QVBoxLayout(content)
    content_layout.setContentsMargins(20, 18, 20, 18)
    content_layout.setSpacing(12)
    content_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
    scroll.setWidget(content)
    explanation = QtWidgets.QLabel(text["source_contact_explanation"], content)
    explanation.setObjectName("sourceContactExplanation")
    explanation.setWordWrap(True)
    explanation.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
    content_layout.addWidget(explanation)
    email = QtWidgets.QLineEdit(content)
    email.setObjectName("sourceContactEmail")
    email.setPlaceholderText("name@example.com")
    content_layout.addWidget(email)
    error = QtWidgets.QLabel(content)
    error.setWordWrap(True)
    error.setObjectName("errorText")
    error.hide()
    content_layout.addWidget(error)
    never = QtWidgets.QPushButton(text["source_contact_never"], content)
    skip = QtWidgets.QPushButton(text["source_contact_skip"], content)
    submit = QtWidgets.QPushButton(text["source_contact_save"], content)
    submit.setObjectName("primaryButton")
    actions = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.TopToBottom)
    actions.setObjectName("sourceContactActions")
    actions.setSpacing(8)
    buttons = (submit, skip, never)
    for button in buttons:
        button.ensurePolished()
        actions.addWidget(button)
    content_layout.addLayout(actions)

    def action_width():
        return sum(max(button.sizeHint().width(), button.minimumSizeHint().width())
                   for button in buttons) + actions.spacing() * (len(buttons) - 1)

    def text_height(label, width):
        # QLabel includes its explicit minimum in heightForWidth; clear the
        # previous wrap height before measuring a new width.
        label.setMinimumHeight(0)
        return max(0, label.heightForWidth(max(1, width)))

    def reflow():
        if getattr(dialog, "_contact_reflowing", False):
            return
        dialog._contact_reflowing = True
        try:
            # Measure without an old scrollbar first, so a former stacked
            # layout cannot keep an otherwise fitting row artificially narrow.
            available = scroll.maximumViewportSize()
            reflow_content(available.width())
            needs_scroll = content_layout.minimumSize().height() > available.height()
            if needs_scroll:
                reflow_content(available.width() - scroll.verticalScrollBar().sizeHint().width())
            scroll.setVerticalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded if needs_scroll
                else QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        finally:
            dialog._contact_reflowing = False

    def reflow_content(viewport_width):
        margins = content_layout.contentsMargins()
        width = max(1, viewport_width - margins.left() - margins.right())
        direction = (QtWidgets.QBoxLayout.Direction.LeftToRight if width >= action_width()
                     else QtWidgets.QBoxLayout.Direction.TopToBottom)
        if actions.direction() != direction:
            actions.setDirection(direction)
        # The full explanation participates in the page's height. Only the
        # complete page scrolls when a physically short window cannot fit it.
        height = text_height(explanation, width)
        if explanation.minimumHeight() != height:
            explanation.setMinimumHeight(height)
        if not error.isHidden():
            error.setMinimumHeight(text_height(error, width))
        content_layout.activate()

    dialog._reflow_contact = reflow
    scroll.viewport().installEventFilter(dialog)
    if parent is not None:
        parent.installEventFilter(dialog)
        page = getattr(parent, "pages", None)
        if page is not None:
            page.installEventFilter(dialog)
    for widget in (dialog, content, explanation, email, error):
        widget.ensurePolished()
    # Reserve room for inline actions and grow to the available width when
    # wrapping would otherwise force scrolling at an ordinary compact size.
    outer = layout.contentsMargins()
    inner = content_layout.contentsMargins()
    horizontal_padding = outer.left() + outer.right() + inner.left() + inner.right()
    vertical_padding = outer.top() + outer.bottom() + inner.top() + inner.bottom()
    header = layout.menuBar()
    if header is not None:
        vertical_padding += header.sizeHint().height()
    width = min(max_width, max(570, action_width() + horizontal_padding))
    def natural_height(width):
        body_width = max(1, width - horizontal_padding)
        body = text_height(explanation, body_width)
        inline = body_width >= action_width()
        button_height = (max(button.sizeHint().height() for button in buttons) if inline
                         else sum(button.sizeHint().height() for button in buttons)
                         + actions.spacing() * (len(buttons) - 1))
        error_height = (text_height(error, body_width) + 12
                        if not error.isHidden() else 0)
        return body + email.sizeHint().height() + button_height + error_height + vertical_padding + 24
    if natural_height(width) > max_height:
        width = max_width
    dialog._contact_preferred_width = max(570, action_width() + horizontal_padding)
    dialog._contact_height_for_width = natural_height
    dialog.resize(width, min(max_height, natural_height(width)))
    layout.activate()
    reflow()

    def show_error(message):
        error.setText(message)
        error.show()
        dialog.resize(dialog.width(), max(dialog.height(), min(
            max_height, natural_height(dialog.width()))))
        reflow()

    def submit_email():
        value = email.text().strip()
        if not valid_email(value):
            show_error(text["source_contact_invalid"])
            return
        try:
            save({"CITATION_VERIFIER_MAILTO": value})
        except (ValueError, RuntimeError, OSError):
            show_error(text["source_contact_save_failed"])
            return
        dialog.accept()

    def suppress():
        QtCore.QSettings("Callimachus", "Callimachus").setValue("source_contact_suppressed", True)
        dialog.accept()
    submit.clicked.connect(submit_email)
    skip.clicked.connect(dialog.accept)
    never.clicked.connect(suppress)
    return dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
