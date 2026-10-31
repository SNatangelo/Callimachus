# core/gui/desktop_guide.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Native, local-first guide pages for the Callimachus desktop app."""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

_ASSET_DIR = Path(__file__).parent / "assets" / "guide"

_UI = {
    "it": {
        "guide": "Guida",
        "home": "Inizio",
        "directory": "Argomenti e FAQ",
        "search": "Cerca nella guida",
        "search_hint": "Cerca in articoli e FAQ",
        "search_local": "La ricerca resta su questo dispositivo.",
        "search_results": "Risultati: {count}",
        "no_results": "Nessun risultato. Prova con altre parole.",
        "all_topics": "Tutti gli argomenti",
        "topics": "Argomenti",
        "entry_heading": "Da dove vuoi cominciare?",
        "entry_start": "Prepara la prima analisi",
        "entry_start_body": "Scegli un manoscritto e controlla le opzioni prima di avviare.",
        "entry_resume": "Riprendi un’analisi",
        "entry_resume_body": "Ritrova una run nella Cronologia e controlla le azioni disponibili.",
        "entry_trouble": "Risolvi un problema",
        "entry_trouble_body": "Parti dal messaggio mostrato e controlla la fase dell’analisi.",
        "phases_heading": "Le cinque fasi dell’analisi",
        "phase_names": ["Parse", "Risoluzione", "Recupero", "Verifica", "Report"],
        "featured_topics": "Argomenti in evidenza",
        "frequent_faq": "Domande frequenti",
        "all_topics_link": "Vedi tutti gli argomenti",
        "topics_heading": "Argomenti della guida",
        "faq_count": "{count} FAQ",
        "steps": "Passaggi",
        "example": "Esempio illustrativo",
        "glossary": "Parole del report",
        "faq": "Domande frequenti",
        "related": "Approfondisci",
        "back": "Torna alla guida",
        "reference": "Documentazione completa",
        "privacy_reference": "Apri l’informativa privacy completa",
        "cli_reference": "Apri il riferimento tecnico completo",
        "reference_missing": "La documentazione completa non è disponibile in questa schermata.",
        "privacy_callback": "Apri l’informativa completa",
        "toggle_navigation": "Mostra/nascondi argomenti della guida",
    },
    "en": {
        "guide": "Guide",
        "home": "Home",
        "directory": "Topics and FAQ",
        "search": "Search the guide",
        "search_hint": "Search articles and FAQ",
        "search_local": "Search stays on this device.",
        "search_results": "Results: {count}",
        "no_results": "No results. Try different words.",
        "all_topics": "All topics",
        "topics": "Topics",
        "entry_heading": "Where would you like to start?",
        "entry_start": "Prepare your first analysis",
        "entry_start_body": "Choose a manuscript and review the options before starting.",
        "entry_resume": "Resume an analysis",
        "entry_resume_body": "Find a run in History and check which actions are available.",
        "entry_trouble": "Troubleshoot a problem",
        "entry_trouble_body": "Start with the message shown and check the analysis phase.",
        "phases_heading": "The five analysis phases",
        "phase_names": ["Parse", "Resolve", "Fetch", "Verify", "Report"],
        "featured_topics": "Featured topics",
        "frequent_faq": "Frequently asked questions",
        "all_topics_link": "Browse all topics",
        "topics_heading": "Guide topics",
        "faq_count": "{count} FAQ",
        "steps": "Steps",
        "example": "Illustrative example",
        "glossary": "Report terms",
        "faq": "Frequently asked questions",
        "related": "Read next",
        "back": "Back to guide",
        "reference": "Full documentation",
        "privacy_reference": "Open the full privacy information",
        "cli_reference": "Open the full technical reference",
        "reference_missing": "Full documentation is not available in this view.",
        "privacy_callback": "Open full privacy information",
        "toggle_navigation": "Show/hide guide topics",
    },
}

_SHORT_TITLES = {
    "it": [
        "Prima analisi", "Cinque fasi", "Modelli e Jury", "Identità fonti",
        "Fetch assistito", "OCR", "Esiti report", "Copertura", "Riprendi",
        "Libreria", "Problemi", "Servizi", "Dati e privacy", "CLI",
    ],
    "en": [
        "First analysis", "Five phases", "Models and Juries", "Source identity",
        "Assisted Fetch", "OCR", "Report outcomes", "Coverage", "Resume",
        "Library", "Troubleshooting", "Services", "Data and privacy", "CLI",
    ],
}


def load_guide_topics(language: str = "it") -> list[dict]:
    """Load the packaged editorial topics for Italian or English."""
    language = str(language).lower().split("-", 1)[0]
    if language not in _UI:
        raise ValueError(f"Unsupported guide language: {language}")
    payload = json.loads((_ASSET_DIR / f"{language}.json").read_text(encoding="utf-8"))
    topics = payload.get("topics")
    if not isinstance(topics, list) or not topics:
        raise ValueError(f"Guide content is empty for language: {language}")
    return topics


def normalize_search_text(value: str) -> str:
    """Case- and accent-insensitive form used by the local guide search."""
    decomposed = unicodedata.normalize("NFKD", str(value).casefold())
    return "".join(character for character in decomposed
                   if not unicodedata.combining(character))


def search_guide_topics(topics: list[dict], query: str) -> list[dict]:
    """Return topic matches when every query word occurs in its full content."""
    tokens = normalize_search_text(query).split()
    if not tokens:
        return []
    visible_fields = (
        "title", "category", "lead", "steps", "notice", "faq",
        "glossary", "example",
    )
    matches = []
    for topic in topics:
        visible_text = " ".join(
            text
            for field in visible_fields
            for text in _nested_text(topic.get(field))
        )
        haystack = normalize_search_text(visible_text)
        if all(token in haystack for token in tokens):
            matches.append(topic)
    return matches


def _nested_text(value):
    """Yield rendered strings from nested article fields, without schema keys."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _nested_text(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _nested_text(nested)


def create_desktop_guide(
    QtCore,
    QtGui,
    QtWidgets,
    *,
    parent=None,
    language="it",
    reference_widget=None,
    privacy_callback=None,
):
    """Create the native guide and host an existing technical reference panel.

    The Qt modules are injected by the desktop shell so importing this module
    does not make Qt a requirement for command-line use.
    """
    language = str(language).lower().split("-", 1)[0]
    if language not in _UI:
        raise ValueError(f"Unsupported guide language: {language}")

    from .atlas_icons import icon as atlas_icon

    labels = _UI[language]
    topics = load_guide_topics(language)
    topic_by_id = {topic["id"]: topic for topic in topics}
    if len(topic_by_id) != len(topics):
        raise ValueError("Guide topic identifiers must be unique")
    for topic in topics:
        if any(link not in topic_by_id for link in topic.get("links", [])):
            raise ValueError(f"Unknown related topic in {topic['id']}")

    class GuideActionCard(QtWidgets.QPushButton):
        """A push button whose size follows its wrapped content layout."""

        def sizeHint(self):
            layout = self.layout()
            if layout is None:
                return super().sizeHint()
            hint = layout.sizeHint()
            return QtCore.QSize(hint.width() + 2, hint.height() + 2)

        def minimumSizeHint(self):
            layout = self.layout()
            if layout is None:
                return super().minimumSizeHint()
            hint = layout.minimumSize()
            return QtCore.QSize(hint.width() + 2, hint.height() + 2)

        def hasHeightForWidth(self):
            layout = self.layout()
            return bool(layout is not None and layout.hasHeightForWidth())

        def heightForWidth(self, width):
            layout = self.layout()
            if layout is None or not layout.hasHeightForWidth():
                return self.sizeHint().height()
            return layout.heightForWidth(max(0, width - 2)) + 2

    class DesktopGuide(QtWidgets.QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("desktopGuide")
            self._language = language
            self._labels = labels
            self._topics = topics
            self._topic_by_id = topic_by_id
            self._privacy_callback = privacy_callback
            self._dark = False
            self._font_size = 14
            self._compact_spacing = False
            self._current_page_id = "home"
            self._reference_title = labels["reference"]
            self._privacy_browser = None
            self._privacy_source_browser = None
            self._expanded_faq = set()
            self._topic_buttons = {}
            self._faq_buttons = []
            self._nav_buttons = []
            self._article_wrap_buttons = []
            self._home_wrap_buttons = []
            self._directory_wrap_buttons = []
            self._home_entry_cards = []
            self._phase_cards = []
            self._featured_topic_buttons = []

            self._views = QtWidgets.QStackedWidget(self)
            self._views.setObjectName("guideViews")
            self._views.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                      QtWidgets.QSizePolicy.Policy.Expanding)
            root_layout = QtWidgets.QVBoxLayout(self)
            root_layout.setContentsMargins(0, 0, 0, 0)
            root_layout.addWidget(self._views)

            self._primary_view = QtWidgets.QWidget(self._views)
            primary_layout = QtWidgets.QVBoxLayout(self._primary_view)
            primary_layout.setContentsMargins(0, 0, 0, 0)
            self._navigation_toggle = QtWidgets.QPushButton(self._primary_view)
            self._navigation_toggle.setObjectName("guideNavigationToggle")
            self._navigation_toggle.setCheckable(True)
            self._navigation_toggle.setText(labels["topics"])
            self._navigation_toggle.setProperty("atlasIcon", "context-book")
            self._navigation_toggle.setProperty("atlasIconSize", 28)
            self._navigation_toggle.setAccessibleName(labels["toggle_navigation"])
            self._navigation_toggle.setToolTip(labels["toggle_navigation"])
            self._navigation_toggle.setIcon(atlas_icon(
                QtCore, QtGui, "context-book", size=28
            ))
            self._navigation_toggle.setIconSize(QtCore.QSize(28, 24))
            self._navigation_toggle.setMinimumSize(0, 0)
            self._navigation_toggle.setMaximumWidth(320)
            self._navigation_toggle.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Fixed,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            primary_layout.addWidget(
                self._navigation_toggle, 0, QtCore.Qt.AlignmentFlag.AlignLeft
            )
            splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self._primary_view)
            splitter.setObjectName("guideSplitter")
            splitter.setChildrenCollapsible(False)
            splitter.setHandleWidth(6)
            splitter.setOpaqueResize(True)
            primary_layout.addWidget(splitter)
            self._splitter = splitter

            self._nav_panel = self._build_navigation(splitter)
            self._content_stack = QtWidgets.QStackedWidget(splitter)
            self._content_stack.setObjectName("guideContentStack")
            self._content_stack.setMinimumWidth(0)
            self._home_scroll, self._home_page = self._build_home()
            self._directory_scroll, self._directory_page = self._build_directory()
            self._article_scroll = self._new_scroll_area()
            self._content_stack.addWidget(self._home_scroll)
            self._content_stack.addWidget(self._directory_scroll)
            self._content_stack.addWidget(self._article_scroll)
            self._splitter.addWidget(self._nav_panel)
            self._splitter.addWidget(self._content_stack)
            self._splitter.setStretchFactor(0, 0)
            self._splitter.setStretchFactor(1, 1)
            self._splitter.setSizes([235, 850])
            from core.gui.shell.navigation import install_sidebar_toggle
            self._navigation_controller = install_sidebar_toggle(
                splitter, self._nav_panel, self._navigation_toggle,
                QtCore, QtWidgets, minimum_content_width=560,
                preferred_width=235, compact_only=True,
            )
            self._layout_home_grids()

            self._wrap_update_timer = QtCore.QTimer(self)
            self._wrap_update_timer.setSingleShot(True)
            self._wrap_update_timer.timeout.connect(self._update_visible_button_wrap)
            self._reflow_timer = QtCore.QTimer(self)
            self._reflow_timer.setSingleShot(True)
            self._reflow_timer.timeout.connect(self._reflow)
            self._content_stack.installEventFilter(self)
            for scroll in (self._home_scroll, self._directory_scroll, self._article_scroll):
                scroll.viewport().installEventFilter(self)
            self._nav_items.installEventFilter(self)

            self._reference_page = self._build_reference_page(reference_widget)
            self._views.addWidget(self._primary_view)
            self._views.addWidget(self._reference_page)
            self.set_theme(False, 14)
            self.show_page("home")

        def _new_scroll_area(self):
            scroll = QtWidgets.QScrollArea(self)
            scroll.setWidgetResizable(True)
            scroll.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                 QtWidgets.QSizePolicy.Policy.Expanding)
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            scroll.setMinimumWidth(0)
            return scroll

        def _set_scroll_content(self, scroll, content, margin=24):
            content.setObjectName("guidePageContent")
            content.setMinimumWidth(0)
            content.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                  QtWidgets.QSizePolicy.Policy.Preferred)
            layout = QtWidgets.QVBoxLayout(content)
            layout.setContentsMargins(margin, margin, margin, margin)
            layout.setSpacing(14)
            layout._guide_wide_margins = (margin, margin, margin, margin)
            scroll.setWidget(content)
            return layout

        def _build_navigation(self, parent_widget):
            panel = QtWidgets.QScrollArea(parent_widget)
            panel.setObjectName("guideNav")
            panel.setMinimumWidth(136)
            panel.setMaximumWidth(420)
            panel.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            panel.setWidgetResizable(True)
            panel.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            body = QtWidgets.QWidget(panel)
            layout = QtWidgets.QVBoxLayout(body)
            layout.setContentsMargins(12, 16, 10, 12)
            layout.setSpacing(8)

            heading = QtWidgets.QLabel(labels["guide"], panel)
            heading.setObjectName("guideNavHeading")
            layout.addWidget(heading)

            self._home_button = self._nav_button(labels["home"], "analysis", panel)
            self._home_button.clicked.connect(lambda: self.show_page("home"))
            layout.addWidget(self._home_button)
            self._directory_button = self._nav_button(labels["directory"], "help", panel)
            self._directory_button.clicked.connect(lambda: self.show_page("directory"))
            layout.addWidget(self._directory_button)
            self._nav_buttons.extend((self._home_button, self._directory_button))

            self._search_label = QtWidgets.QLabel(labels["search"], panel)
            self._search_label.setObjectName("guideSearchLabel")
            layout.addWidget(self._search_label)
            self._search = QtWidgets.QLineEdit(panel)
            self._search.setObjectName("guideSearch")
            self._search.setPlaceholderText(labels["search_hint"])
            self._search.setClearButtonEnabled(True)
            self._search.setAccessibleName(labels["search"])
            self._search.addAction(
                atlas_icon(QtCore, QtGui, "search", color="#66769c", size=16),
                QtWidgets.QLineEdit.ActionPosition.LeadingPosition,
            )
            self._search.textChanged.connect(self._update_navigation)
            layout.addWidget(self._search)
            self._search_note = QtWidgets.QLabel(labels["search_local"], panel)
            self._search_note.setObjectName("guideMuted")
            self._search_note.setWordWrap(True)
            layout.addWidget(self._search_note)

            self._search_status = QtWidgets.QLabel(labels["all_topics"], panel)
            self._search_status.setObjectName("guideSearchStatus")
            self._search_status.setWordWrap(True)
            layout.addWidget(self._search_status)
            self._nav_items = QtWidgets.QWidget(body)
            self._nav_items.setObjectName("guideNavItems")
            self._nav_items.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                          QtWidgets.QSizePolicy.Policy.Preferred)
            self._nav_items_layout = QtWidgets.QVBoxLayout(self._nav_items)
            self._nav_items_layout.setContentsMargins(0, 0, 2, 0)
            self._nav_items_layout.setSpacing(4)
            self._nav_items_layout.addStretch(1)
            layout.addWidget(self._nav_items, 1)
            self._update_navigation("")
            panel.setWidget(body)
            return panel

        def _nav_button(self, text, icon_key, parent_widget):
            button = QtWidgets.QPushButton(text, parent_widget)
            button.setObjectName("guideNavButton")
            button.setIcon(atlas_icon(QtCore, QtGui, icon_key, color="#526780", size=16))
            button.setMinimumHeight(36)
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                 QtWidgets.QSizePolicy.Policy.Preferred)
            button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            button.setProperty("guideWrapText", text)
            return button

        def _clear_nav_items(self):
            while self._nav_items_layout.count() > 1:
                item = self._nav_items_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.hide()
                    widget.deleteLater()

        def _update_navigation(self, query):
            self._clear_nav_items()
            self._topic_buttons.clear()
            self._nav_buttons = [self._home_button, self._directory_button]
            cleaned = str(query).strip()
            if cleaned:
                matches = search_guide_topics(self._topics, cleaned)
                self._search_status.setText(labels["search_results"].format(count=len(matches)))
                if not matches:
                    empty = QtWidgets.QLabel(labels["no_results"], self._nav_items)
                    empty.setObjectName("guideMuted")
                    empty.setWordWrap(True)
                    self._nav_items_layout.insertWidget(0, empty)
                for topic in matches:
                    self._add_topic_button(topic, self._short_title(topic["id"]))
                self._wrap_nav_buttons()
                return

            self._search_status.setText(labels["all_topics"])
            for topic in self._topics:
                self._add_topic_button(topic, self._short_title(topic["id"]))
            self._sync_nav_selection()
            self._wrap_nav_buttons()

        def _short_title(self, topic_id):
            topic_ids = [topic["id"] for topic in self._topics]
            try:
                return _SHORT_TITLES[self._language][topic_ids.index(topic_id)]
            except (IndexError, ValueError):
                return self._topic_by_id[topic_id]["title"]

        def _add_topic_button(self, topic, title):
            button = self._nav_button(title, "file", self._nav_items)
            button.setObjectName("guideTopicButton")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, topic_id=topic["id"]: self.show_page(topic_id)
            )
            self._nav_items_layout.insertWidget(self._nav_items_layout.count() - 1, button)
            self._topic_buttons[topic["id"]] = button
            self._nav_buttons.append(button)

        def _sync_nav_selection(self):
            for topic_id, button in self._topic_buttons.items():
                button.setChecked(topic_id == self._current_page_id)
            self._home_button.setChecked(self._current_page_id == "home")
            self._directory_button.setChecked(self._current_page_id == "directory")

        def _build_home(self):
            scroll = self._new_scroll_area()
            page = QtWidgets.QWidget(scroll)
            layout = self._set_scroll_content(scroll, page, 28)

            eyebrow = QtWidgets.QLabel("CALLIMACHUS  ·  " + labels["guide"].upper(), page)
            eyebrow.setObjectName("guideEyebrow")
            layout.addWidget(eyebrow)
            title = QtWidgets.QLabel(labels["entry_heading"], page)
            title.setObjectName("guidePageTitle")
            title.setWordWrap(True)
            layout.addWidget(title)

            entries = [
                (labels["entry_start"], labels["entry_start_body"], "start", "play"),
                (labels["entry_resume"], labels["entry_resume_body"], "resume", "history"),
                (labels["entry_trouble"], labels["entry_trouble_body"], "trouble", "help"),
            ]
            self._entry_grid = QtWidgets.QGridLayout()
            self._entry_grid.setContentsMargins(0, 0, 0, 0)
            self._entry_grid.setHorizontalSpacing(10)
            self._entry_grid.setVerticalSpacing(10)
            for title_text, body_text, target, icon_key in entries:
                card = GuideActionCard(page)
                policy = card.sizePolicy()
                policy.setHorizontalPolicy(QtWidgets.QSizePolicy.Policy.Ignored)
                policy.setVerticalPolicy(QtWidgets.QSizePolicy.Policy.Preferred)
                policy.setHeightForWidth(True)
                card.setSizePolicy(policy)
                card.setObjectName("guideEntryCard")
                card.setAccessibleName(title_text)
                card.setAccessibleDescription(body_text)
                card.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
                card.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                card.clicked.connect(
                    lambda _checked=False, page_id=target: self.show_page(page_id)
                )
                card_layout = QtWidgets.QVBoxLayout(card)
                card_layout.setContentsMargins(16, 13, 16, 13)
                card_layout.setSpacing(6)
                card_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
                title_row = QtWidgets.QHBoxLayout()
                title_row.setContentsMargins(0, 0, 0, 0)
                title_row.setSpacing(8)
                icon_label = QtWidgets.QLabel(card)
                icon_label.setPixmap(
                    atlas_icon(QtCore, QtGui, icon_key, color="#526780", size=16).pixmap(16, 16)
                )
                icon_label.setFixedSize(16, 16)
                icon_label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                title_row.addWidget(icon_label, 0, QtCore.Qt.AlignmentFlag.AlignTop)
                title_label = QtWidgets.QLabel(title_text, card)
                title_label.setObjectName("guideEntryTitle")
                title_label.setWordWrap(True)
                title_label.setAlignment(
                    QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
                )
                title_label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                title_row.addWidget(title_label, 1)
                card_layout.addLayout(title_row)
                body = QtWidgets.QLabel(body_text, card)
                body.setObjectName("guideMuted")
                body.setWordWrap(True)
                body.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                card_layout.addWidget(body)
                self._entry_grid.addWidget(card, 0, len(self._home_entry_cards))
                self._home_entry_cards.append(card)
            layout.addLayout(self._entry_grid)

            phases_heading = QtWidgets.QLabel(labels["phases_heading"], page)
            phases_heading.setObjectName("guideSectionTitle")
            phases_heading.setWordWrap(True)
            layout.addWidget(phases_heading)
            self._phase_grid = QtWidgets.QGridLayout()
            self._phase_grid.setContentsMargins(0, 0, 0, 0)
            self._phase_grid.setHorizontalSpacing(8)
            self._phase_grid.setVerticalSpacing(8)
            for index, name in enumerate(labels["phase_names"]):
                phase = QtWidgets.QFrame(page)
                phase.setObjectName("guidePhaseCard")
                phase_layout = QtWidgets.QHBoxLayout(phase)
                phase_layout.setContentsMargins(10, 9, 10, 9)
                phase_layout.setSpacing(8)
                number = QtWidgets.QLabel(str(index + 1), phase)
                number.setObjectName("guidePhaseNumber")
                phase_layout.addWidget(number, 0, QtCore.Qt.AlignmentFlag.AlignTop)
                phase_name = QtWidgets.QLabel(name, phase)
                phase_name.setObjectName("guidePhaseName")
                phase_name.setWordWrap(True)
                phase_layout.addWidget(phase_name, 1)
                self._phase_grid.addWidget(phase, 0, index)
                self._phase_cards.append(phase)
            layout.addLayout(self._phase_grid)

            topics_heading = QtWidgets.QLabel(labels["featured_topics"], page)
            topics_heading.setObjectName("guideSectionTitle")
            layout.addWidget(topics_heading)
            self._featured_grid = QtWidgets.QGridLayout()
            self._featured_grid.setContentsMargins(0, 0, 0, 0)
            self._featured_grid.setHorizontalSpacing(8)
            self._featured_grid.setVerticalSpacing(6)
            for index, topic_id in enumerate(("start", "models", "workflow", "fetch", "report", "trouble")):
                button = QtWidgets.QPushButton(self._short_title(topic_id), page)
                button.setObjectName("guideFeaturedTopic")
                button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                button.setProperty("guideWrapText", self._short_title(topic_id))
                button.clicked.connect(
                    lambda _checked=False, page_id=topic_id: self.show_page(page_id)
                )
                self._featured_grid.addWidget(button, index // 3, index % 3)
                self._featured_topic_buttons.append(button)
                self._home_wrap_buttons.append(button)
            layout.addLayout(self._featured_grid)
            all_topics = QtWidgets.QPushButton(labels["all_topics_link"], page)
            all_topics.setObjectName("guideLinkButton")
            all_topics.setProperty("guideWrapText", labels["all_topics_link"])
            all_topics.clicked.connect(lambda: self.show_page("directory"))
            layout.addWidget(all_topics)
            self._home_wrap_buttons.append(all_topics)

            faq_heading = QtWidgets.QLabel(labels["frequent_faq"], page)
            faq_heading.setObjectName("guideSectionTitle")
            layout.addWidget(faq_heading)
            self._home_faq_buttons = []
            for topic_id, faq_index in (("start", 0), ("fetch", 0), ("report", 0)):
                question, answer = self._topic_by_id[topic_id]["faq"][faq_index]
                key = (topic_id, faq_index)
                card = QtWidgets.QFrame(page)
                card.setObjectName("guideHomeFaqCard")
                card_layout = QtWidgets.QVBoxLayout(card)
                card_layout.setContentsMargins(12, 9, 12, 9)
                card_layout.setSpacing(4)
                button = QtWidgets.QPushButton(question, card)
                button.setObjectName("guideHomeFaqQuestion")
                button.setCheckable(True)
                button.setFlat(True)
                button.setChecked(key in self._expanded_faq)
                button.setAccessibleName(question)
                button.setAccessibleDescription(answer)
                button.setProperty("guideWrapText", question)
                answer_label = QtWidgets.QLabel(answer, card)
                answer_label.setObjectName("guideBody")
                answer_label.setWordWrap(True)
                answer_label.setVisible(key in self._expanded_faq)
                button.clicked.connect(
                    lambda expanded, faq_key=key, answer_widget=answer_label:
                    self._toggle_faq(faq_key, expanded, answer_widget, self._home_scroll)
                )
                card_layout.addWidget(button)
                card_layout.addWidget(answer_label)
                self._home_faq_buttons.append(button)
                layout.addWidget(card)
            layout.addStretch(1)
            scroll.setWidget(page)
            return scroll, page

        def _layout_home_grids(self):
            width = self._content_stack.width()
            entry_columns = 3 if width >= 760 else 1
            phase_columns = 5 if width >= 800 else 2 if width >= 360 else 1
            topic_columns = 3 if width >= 760 else 2
            for row in range(len(self._home_entry_cards)):
                self._entry_grid.setRowMinimumHeight(row, 0)
            self._reposition_grid(self._entry_grid, self._home_entry_cards, entry_columns)
            self._reposition_grid(self._phase_grid, self._phase_cards, phase_columns)
            self._reposition_grid(self._featured_grid, self._featured_topic_buttons, topic_columns)

        def _update_entry_card_row_heights(self):
            rows = {}
            for index, card in enumerate(self._home_entry_cards):
                row = self._entry_grid.getItemPosition(index)[0]
                rows.setdefault(row, []).append(card)
            for row in range(len(self._home_entry_cards)):
                self._entry_grid.setRowMinimumHeight(row, 0)
            for row, cards in rows.items():
                widths = [card.width() for card in cards]
                if not widths or min(widths) <= 0:
                    continue
                for card in cards:
                    for object_name in ("guideEntryTitle", "guideMuted"):
                        label = card.findChild(QtWidgets.QLabel, object_name)
                        if label is not None and label.width() > 0 and label.hasHeightForWidth():
                            label.setMinimumHeight(0)
                            required_height = label.heightForWidth(label.width())
                            label.setMinimumHeight(required_height)
                required_height = max(
                    card.heightForWidth(card.width()) for card in cards
                )
                self._entry_grid.setRowMinimumHeight(row, required_height)
            self._entry_grid.invalidate()
            self._home_page.layout().invalidate()
            self._home_page.layout().activate()

        @staticmethod
        def _reposition_grid(grid, widgets, columns):
            previous_columns = grid.columnCount()
            while grid.count():
                grid.takeAt(0)
            for index, widget in enumerate(widgets):
                grid.addWidget(widget, index // columns, index % columns)
            for column in range(max(previous_columns, columns)):
                grid.setColumnStretch(column, 1 if column < columns else 0)

        def _build_directory(self):
            scroll = self._new_scroll_area()
            page = QtWidgets.QWidget(scroll)
            layout = self._set_scroll_content(scroll, page, 28)
            title = QtWidgets.QLabel(labels["topics_heading"], page)
            title.setObjectName("guidePageTitle")
            title.setWordWrap(True)
            layout.addWidget(title)

            for topic in self._topics:
                card = QtWidgets.QFrame(page)
                card.setObjectName("guideDirectoryCard")
                card_layout = QtWidgets.QVBoxLayout(card)
                card_layout.setContentsMargins(14, 10, 14, 10)
                card_layout.setSpacing(5)
                category = QtWidgets.QLabel(topic.get("category", ""), card)
                category.setObjectName("guideEyebrow")
                category.setWordWrap(True)
                card_layout.addWidget(category)
                title_button = QtWidgets.QPushButton(topic["title"], card)
                title_button.setObjectName("guideLinkButton")
                title_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                title_button.setProperty("guideWrapText", topic["title"])
                title_button.clicked.connect(
                    lambda _checked=False, topic_id=topic["id"]: self.show_page(topic_id)
                )
                card_layout.addWidget(title_button)
                self._directory_wrap_buttons.append(title_button)
                faq_count = len(topic.get("faq", []))
                if faq_count:
                    count = QtWidgets.QLabel(labels["faq_count"].format(count=faq_count), card)
                    count.setObjectName("guideMuted")
                    card_layout.addWidget(count)
                layout.addWidget(card)
            layout.addStretch(1)
            scroll.setWidget(page)
            return scroll, page

        def _build_reference_page(self, reference_widget):
            page = QtWidgets.QWidget(self._views)
            page.setObjectName("guideReferencePage")
            layout = QtWidgets.QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            header = QtWidgets.QFrame(page)
            header.setObjectName("guideReferenceHeader")
            header_layout = QtWidgets.QHBoxLayout(header)
            header_layout.setContentsMargins(12, 10, 16, 10)
            self._reference_back = QtWidgets.QPushButton(labels["back"], header)
            self._reference_back.setObjectName("guideReferenceBack")
            from core.gui.shell.navigation import set_back_button_presentation
            set_back_button_presentation(
                self._reference_back, QtCore, QtGui,
                label=labels["back"], text=labels["back"],
            )
            self._reference_back.clicked.connect(self._return_from_reference)
            header_layout.addWidget(self._reference_back)
            self._reference_heading = QtWidgets.QLabel(self._reference_title, header)
            self._reference_heading.setObjectName("guideSectionTitle")
            self._reference_heading.setWordWrap(True)
            header_layout.addWidget(self._reference_heading, 1)
            layout.addWidget(header)
            if reference_widget is None:
                missing = QtWidgets.QLabel(labels["reference_missing"], page)
                missing.setObjectName("guideMuted")
                missing.setWordWrap(True)
                missing.setContentsMargins(24, 24, 24, 24)
                layout.addWidget(missing, 1)
            else:
                reference_widget.setParent(page)
                layout.addWidget(reference_widget, 1)
            return page

        def _return_from_reference(self):
            self._views.setCurrentWidget(self._primary_view)
            self.show_page(self._current_page_id)

        def show_page(self, topic_id="home"):
            """Show home, the topic directory, or a topic by its stable id."""
            self._navigation_controller.close_overlay()
            if topic_id == "home":
                self._current_page_id = "home"
                self._content_stack.setCurrentWidget(self._home_scroll)
            elif topic_id == "directory":
                self._current_page_id = "directory"
                self._content_stack.setCurrentWidget(self._directory_scroll)
            else:
                if topic_id not in self._topic_by_id:
                    raise ValueError(f"Unknown guide topic: {topic_id}")
                self._current_page_id = topic_id
                self._render_article(self._topic_by_id[topic_id])
                self._content_stack.setCurrentWidget(self._article_scroll)
            self._views.setCurrentWidget(self._primary_view)
            self._navigation_toggle.setVisible(self._navigation_controller.is_overlay)
            self._sync_nav_selection()
            self._apply_compact_page_spacing()
            self._queue_visible_button_wrap()

        def show_reference(self, title=None):
            """Show the injected full technical or legal reference panel."""
            self._reference_title = str(title or labels["reference"])
            self._reference_heading.setText(self._reference_title)
            self._views.setCurrentWidget(self._reference_page)
            self._navigation_toggle.hide()

        def show_privacy_document(self, source_browser):
            """Read the complete existing disclosure within the native Guide."""
            self._navigation_controller.close_overlay()
            self._privacy_source_browser = source_browser
            if self._privacy_browser is None:
                self._privacy_browser = QtWidgets.QTextBrowser(self._content_stack)
                self._privacy_browser.setObjectName("guidePrivacyDocument")
                self._privacy_browser.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
                self._privacy_browser.setViewportMargins(28, 24, 28, 24)
                self._privacy_browser.setReadOnly(True)
                self._privacy_browser.setOpenLinks(False)
                self._privacy_browser.setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Ignored,
                    QtWidgets.QSizePolicy.Policy.Expanding,
                )
                self._privacy_browser.anchorClicked.connect(source_browser.anchorClicked.emit)
                self._content_stack.addWidget(self._privacy_browser)
                edge_scrollbar = getattr(self, "_edge_scrollbar", None)
                if edge_scrollbar is not None:
                    edge_scrollbar.add_scroll(self._privacy_browser)
            # The source reader can be hidden and nearly zero-width while this
            # page is active. A shared QTextDocument has only one layout width,
            # so letting both browsers own it causes the hidden reader to
            # collapse the legal text to one character per line during paint.
            self._privacy_browser.setPlainText("")
            self.format_privacy_document()
            self._privacy_browser.verticalScrollBar().setValue(0)
            self._current_page_id = "privacy"
            self._content_stack.setCurrentWidget(self._privacy_browser)
            self._views.setCurrentWidget(self._primary_view)
            self._navigation_toggle.setVisible(self._navigation_controller.is_overlay)
            self._sync_nav_selection()
            self._queue_visible_button_wrap()

        def format_privacy_document(self):
            """Lay out the unabridged legal text with the Guide's type scale."""
            source_browser = self._privacy_source_browser
            if self._privacy_browser is None or source_browser is None:
                return
            expected_path = "privacy-it.md" if self._language == "it" else "privacy.md"
            source_window = source_browser.window()
            if getattr(source_window, "_current_doc_path", "") != expected_path:
                return

            source_document = source_browser.document()
            document = source_document.clone(self._privacy_browser)
            self._privacy_browser.setDocument(document)
            ink = QtGui.QColor("#e6edf6" if self._dark else "#11183c")
            muted = QtGui.QColor("#9faec4" if self._dark else "#66769c")
            accent = QtGui.QColor("#7ab4ff" if self._dark else "#175fe6")
            document.setIndentWidth(self._font_size * 1.25)
            cursor = QtGui.QTextCursor(document)
            cursor.beginEditBlock()
            block = document.begin()
            while block.isValid():
                fmt = block.blockFormat()
                heading = fmt.headingLevel()
                fmt.setTopMargin(0 if block == document.begin() else 24 if heading else 2)
                fmt.setBottomMargin(14 if heading == 1 else 10 if heading else 8)
                fmt.setLineHeight(130, QtGui.QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
                cursor.setPosition(block.position())
                cursor.setBlockFormat(fmt)
                font = QtGui.QFont("Segoe UI")
                font.setPixelSize(self._font_size + (10 if heading == 1 else 4 if heading else 0))
                # Keep links and emphasis intact while matching the Guide palette.
                fragment = block.begin()
                fragments = []
                while not fragment.atEnd():
                    item = fragment.fragment()
                    if item.isValid():
                        fragments.append((item.position(), item.length(), item.charFormat()))
                    fragment += 1
                metadata = block.previous().isValid() and block.previous().blockFormat().headingLevel() == 1 and not heading
                for position, length, character in fragments:
                    character.clearProperty(QtGui.QTextFormat.Property.FontSizeAdjustment)
                    character.clearProperty(QtGui.QTextFormat.Property.FontPointSize)
                    character.clearProperty(QtGui.QTextFormat.Property.FontPixelSize)
                    character.setFont(font, QtGui.QTextCharFormat.FontPropertiesInheritanceBehavior.FontPropertiesSpecifiedOnly)
                    character.setForeground(accent if character.isAnchor() else muted if metadata else ink)
                    cursor.setPosition(position)
                    cursor.setPosition(position + length, QtGui.QTextCursor.MoveMode.KeepAnchor)
                    cursor.setCharFormat(character)
                block = block.next()
            cursor.endEditBlock()
            document.setTextWidth(self._privacy_browser.viewport().width())

        def _clear_layout(self, layout):
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
                child_layout = item.layout()
                if child_layout is not None:
                    self._clear_layout(child_layout)

        def _render_article(self, topic):
            self._faq_buttons = []
            self._article_wrap_buttons = []
            content = QtWidgets.QWidget(self._article_scroll)
            layout = self._set_scroll_content(self._article_scroll, content, 30)
            category = QtWidgets.QLabel(topic.get("category", ""), content)
            category.setObjectName("guideEyebrow")
            category.setWordWrap(True)
            layout.addWidget(category)
            title = QtWidgets.QLabel(topic["title"], content)
            title.setObjectName("guidePageTitle")
            title.setWordWrap(True)
            layout.addWidget(title)
            lead = QtWidgets.QLabel(topic.get("lead", ""), content)
            lead.setObjectName("guideLead")
            lead.setWordWrap(True)
            layout.addWidget(lead)

            if topic.get("notice"):
                notice = QtWidgets.QFrame(content)
                notice.setObjectName("guideNotice")
                notice_layout = QtWidgets.QHBoxLayout(notice)
                notice_layout.setContentsMargins(14, 12, 14, 12)
                notice_label = QtWidgets.QLabel(topic["notice"], notice)
                notice_label.setWordWrap(True)
                notice_layout.addWidget(notice_label)
                layout.addWidget(notice)

            if topic.get("steps"):
                heading = QtWidgets.QLabel(labels["steps"], content)
                heading.setObjectName("guideSectionTitle")
                layout.addWidget(heading)
                for number, (step_title, step_body) in enumerate(topic["steps"], 1):
                    card = QtWidgets.QFrame(content)
                    card.setObjectName("guideStepCard")
                    row = QtWidgets.QHBoxLayout(card)
                    row.setContentsMargins(14, 12, 14, 12)
                    row.setSpacing(12)
                    badge = QtWidgets.QLabel(str(number), card)
                    badge.setObjectName("guideStepNumber")
                    badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                    badge.setMinimumSize(32, 32)
                    badge.setMaximumWidth(40)
                    row.addWidget(badge, 0, QtCore.Qt.AlignmentFlag.AlignTop)
                    detail = QtWidgets.QWidget(card)
                    detail_layout = QtWidgets.QVBoxLayout(detail)
                    detail_layout.setContentsMargins(0, 0, 0, 0)
                    detail_layout.setSpacing(5)
                    label = QtWidgets.QLabel(step_title, detail)
                    label.setObjectName("guideStepTitle")
                    label.setWordWrap(True)
                    detail_layout.addWidget(label)
                    body = QtWidgets.QLabel(step_body, detail)
                    body.setObjectName("guideBody")
                    body.setWordWrap(True)
                    detail_layout.addWidget(body)
                    row.addWidget(detail, 1)
                    layout.addWidget(card)

            if topic.get("example"):
                card = QtWidgets.QFrame(content)
                card.setObjectName("guideExampleCard")
                example_layout = QtWidgets.QVBoxLayout(card)
                example_layout.setContentsMargins(16, 13, 16, 13)
                example_layout.setSpacing(6)
                tag = QtWidgets.QLabel(labels["example"], card)
                tag.setObjectName("guideExampleTag")
                example_layout.addWidget(tag)
                title_label = QtWidgets.QLabel(topic["example"][0], card)
                title_label.setObjectName("guideCardTitle")
                title_label.setWordWrap(True)
                example_layout.addWidget(title_label)
                body = QtWidgets.QLabel(topic["example"][1], card)
                body.setObjectName("guideBody")
                body.setWordWrap(True)
                example_layout.addWidget(body)
                layout.addWidget(card)

            if topic.get("glossary"):
                heading = QtWidgets.QLabel(labels["glossary"], content)
                heading.setObjectName("guideSectionTitle")
                layout.addWidget(heading)
                glossary = QtWidgets.QFrame(content)
                glossary.setObjectName("guideGlossaryCard")
                glossary_layout = QtWidgets.QVBoxLayout(glossary)
                glossary_layout.setContentsMargins(14, 10, 14, 10)
                glossary_layout.setSpacing(8)
                for term, definition in topic["glossary"]:
                    row = QtWidgets.QWidget(glossary)
                    row_layout = QtWidgets.QVBoxLayout(row)
                    row_layout.setContentsMargins(0, 0, 0, 0)
                    row_layout.setSpacing(2)
                    term_label = QtWidgets.QLabel(term, row)
                    term_label.setObjectName("guideGlossaryTerm")
                    term_label.setWordWrap(True)
                    row_layout.addWidget(term_label)
                    meaning = QtWidgets.QLabel(definition, row)
                    meaning.setObjectName("guideBody")
                    meaning.setWordWrap(True)
                    row_layout.addWidget(meaning)
                    glossary_layout.addWidget(row)
                layout.addWidget(glossary)

            if topic.get("faq"):
                heading = QtWidgets.QLabel(labels["faq"], content)
                heading.setObjectName("guideSectionTitle")
                layout.addWidget(heading)
                for index, (question, answer) in enumerate(topic["faq"]):
                    pair = QtWidgets.QFrame(content)
                    pair.setObjectName("guideArticleFaqCard")
                    pair_layout = QtWidgets.QVBoxLayout(pair)
                    pair_layout.setContentsMargins(12, 8, 12, 8)
                    pair_layout.setSpacing(2)
                    key = (topic["id"], index)
                    button = QtWidgets.QPushButton(question, pair)
                    button.setObjectName("guideFaqQuestion")
                    button.setCheckable(True)
                    button.setFlat(True)
                    button.setChecked(key in self._expanded_faq)
                    button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                    button.setAccessibleName(question)
                    button.setAccessibleDescription(answer)
                    button.setProperty("guideQuestion", question)
                    button.setProperty("guideWrapText", question)
                    pair_layout.addWidget(button)
                    answer_label = QtWidgets.QLabel(answer, pair)
                    answer_label.setObjectName("guideFaqAnswer")
                    answer_label.setWordWrap(True)
                    answer_label.setVisible(key in self._expanded_faq)
                    answer_label.setContentsMargins(8, 4, 8, 8)
                    pair_layout.addWidget(answer_label)
                    button.clicked.connect(
                        lambda expanded, faq_key=key, answer_widget=answer_label:
                        self._toggle_faq(faq_key, expanded, answer_widget)
                    )
                    self._faq_buttons.append(button)
                    layout.addWidget(pair)

            related_ids = topic.get("links", [])
            if related_ids:
                heading = QtWidgets.QLabel(labels["related"], content)
                heading.setObjectName("guideSectionTitle")
                layout.addWidget(heading)
                for related_id in related_ids:
                    related = self._topic_by_id[related_id]
                    button = QtWidgets.QPushButton(related["title"], content)
                    button.setObjectName("guideLinkButton")
                    button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                    button.setProperty("guideWrapText", related["title"])
                    button.clicked.connect(
                        lambda _checked=False, page_id=related_id: self.show_page(page_id)
                    )
                    layout.addWidget(button)
                    self._article_wrap_buttons.append(button)

            if topic["id"] == "privacy":
                privacy = QtWidgets.QPushButton(labels["privacy_reference"], content)
                privacy.setObjectName("guideReferenceLink")
                privacy.clicked.connect(self._open_privacy_reference)
                layout.addWidget(privacy)
            if topic["id"] == "cli":
                reference = QtWidgets.QPushButton(labels["cli_reference"], content)
                reference.setObjectName("guideReferenceLink")
                reference.clicked.connect(lambda: self.show_reference("CLI" if language == "it" else "CLI"))
                layout.addWidget(reference)

            layout.addStretch(1)
            self._article_scroll.setWidget(content)

        def _toggle_faq(self, key, expanded, answer_widget, scroll=None):
            scroll = scroll or self._article_scroll
            scroll_bar = scroll.verticalScrollBar()
            scroll_position = scroll_bar.value()
            if expanded:
                self._expanded_faq.add(key)
            else:
                self._expanded_faq.discard(key)
            answer_widget.setVisible(expanded)
            QtCore.QTimer.singleShot(0, lambda: scroll_bar.setValue(scroll_position))

        def _open_privacy_reference(self):
            if self._privacy_callback is not None:
                self._privacy_callback()
            else:
                self.show_reference(labels["privacy_reference"])

        def _update_visible_button_wrap(self):
            self._update_entry_card_row_heights()
            page = self._content_stack.currentWidget()
            if page is self._home_scroll:
                buttons = self._home_wrap_buttons
                special_faqs = self._home_faq_buttons
            elif page is self._directory_scroll:
                buttons = self._directory_wrap_buttons
                special_faqs = []
            elif page is self._article_scroll:
                buttons = self._article_wrap_buttons
                special_faqs = self._faq_buttons
            else:
                buttons = []
                special_faqs = []
            for button in [*buttons, *special_faqs]:
                self._wrap_button(button, self._button_text_width(button))
            self._wrap_nav_buttons()

        @staticmethod
        def _button_text_width(button):
            padding = {
                "guideFeaturedTopic": 20,
                "guideHomeFaqQuestion": 12,
                "guideFaqQuestion": 12,
                "guideEntryButton": 4,
                "guideLinkButton": 4,
            }.get(button.objectName(), 16)
            if not button.icon().isNull():
                padding += button.iconSize().width() + 6
            return max(60, button.width() - padding)

        def _wrap_nav_buttons(self):
            self._search_status.setMinimumHeight(
                max(1, self._search_status.heightForWidth(max(1, self._search_status.width())))
            )
            nav_width = max(60, self._nav_items.width() - 32)
            for button in [self._home_button, self._directory_button, *self._topic_buttons.values()]:
                self._wrap_button(button, nav_width)

        def _queue_visible_button_wrap(self):
            self._wrap_update_timer.start(0)

        def _wrap_button(self, button, width):
            original = button.property("guideWrapText")
            if not original:
                return
            lines = self._wrap_text(original, width, button.font())
            button.setText(lines)
            metrics = QtGui.QFontMetrics(button.font())
            button.setMinimumHeight(max(40, metrics.lineSpacing() * (lines.count("\n") + 1) + 14))

        @staticmethod
        def _wrap_text(text, max_width, font):
            metrics = QtGui.QFontMetrics(font)
            lines = []
            current = ""
            for word in str(text).split():
                candidate = f"{current} {word}".strip()
                if current and metrics.horizontalAdvance(candidate) > max_width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                lines.append(current)
            return "\n".join(lines)

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._navigation_controller.sync()
            self._reflow_timer.start(0)

        def showEvent(self, event):
            super().showEvent(event)
            # The initial resize can precede the splitter becoming visible.
            # Sync after this show without another timer hop through sync().
            QtCore.QTimer.singleShot(0, self._navigation_controller._sync)

        def eventFilter(self, watched, event):
            if event.type() == QtCore.QEvent.Type.Resize:
                if (watched is self._content_stack
                        and event.size() != event.oldSize()):
                    self._reflow_timer.start(0)
                if (watched is self._nav_items
                        and event.size().width() != event.oldSize().width()):
                    self._queue_visible_button_wrap()
                current_page = self._content_stack.currentWidget()
                if (current_page in (self._home_scroll, self._directory_scroll, self._article_scroll)
                        and watched is current_page.viewport()
                        and event.size().width() != event.oldSize().width()):
                    self._queue_visible_button_wrap()
            return super().eventFilter(watched, event)

        def _reflow(self):
            self._apply_compact_page_spacing()
            self._layout_home_grids()
            self._queue_visible_button_wrap()

        def _apply_compact_page_spacing(self):
            compact = self.width() < 700 or self.height() < 480
            parent = self.parentWidget()
            parent_layout = parent.layout() if parent is not None else None
            if parent_layout is not None and parent_layout.count() >= 3:
                heading = parent_layout.itemAt(0).widget()
                last = parent_layout.itemAt(parent_layout.count() - 1).widget()
                if (last is self and isinstance(heading, QtWidgets.QLabel)
                        and heading.objectName() == "sectionTitle"):
                    if not hasattr(self, "_guide_parent_wide_margins"):
                        margins = parent_layout.contentsMargins()
                        self._guide_parent_wide_margins = (
                            margins.left(), margins.top(),
                            margins.right(), margins.bottom(),
                        )
                    left, top, right, bottom = self._guide_parent_wide_margins
                    siblings = [
                        parent_layout.itemAt(index)
                        for index in range(parent_layout.count() - 1)
                        if not parent_layout.itemAt(index).isEmpty()
                    ]
                    # Measure against the unchanged wide host chrome. Using our
                    # current size would feed reclaimed margins into the next
                    # mode decision and oscillate around either breakpoint.
                    available_width = parent.width() - left - right
                    available_height = (
                        parent.height() - top - bottom
                        - sum(max(0, item.sizeHint().height()) for item in siblings)
                        - max(0, parent_layout.spacing()) * len(siblings)
                    )
                    compact = available_width < 700 or available_height < 480
                    parent_layout.setContentsMargins(
                        8, 8, 8, 0
                    ) if compact else parent_layout.setContentsMargins(
                        *self._guide_parent_wide_margins
                    )
            self._compact_spacing = compact
            for content in self.findChildren(QtWidgets.QWidget, "guidePageContent"):
                layout = content.layout()
                if layout is None:
                    continue
                wide = getattr(layout, "_guide_wide_margins", None)
                if wide is None:
                    margins = layout.contentsMargins()
                    wide = (margins.left(), margins.top(), margins.right(), margins.bottom())
                    layout._guide_wide_margins = wide
                layout.setContentsMargins(8, 8, 8, 8) if compact else layout.setContentsMargins(*wide)

            decorative_panels = {
                "guidePhaseCard", "guideHomeFaqCard", "guideDirectoryCard",
                "guideStepCard", "guideExampleCard", "guideGlossaryCard",
                "guideArticleFaqCard",
            }
            for panel in self.findChildren(QtWidgets.QFrame):
                name = panel.objectName()
                if name not in decorative_panels and name != "guideNotice":
                    continue
                layout = panel.layout()
                if layout is not None:
                    wide = getattr(layout, "_guide_wide_margins", None)
                    if wide is None:
                        margins = layout.contentsMargins()
                        wide = (margins.left(), margins.top(), margins.right(), margins.bottom())
                        layout._guide_wide_margins = wide
                    margins = ((8, 6, 8, 6) if name == "guideNotice" else (0, 0, 0, 0))
                    layout.setContentsMargins(*margins) if compact else layout.setContentsMargins(*wide)
                flattened = compact and name in decorative_panels
                if panel.property("compactGuidePanel") != flattened:
                    panel.setProperty("compactGuidePanel", flattened)
                    panel.style().unpolish(panel)
                    panel.style().polish(panel)
                    panel.updateGeometry()

        def set_theme(self, dark=False, font_size=14):
            """Apply the application's light/dark palette and root font size."""
            self._dark = bool(dark)
            self._font_size = max(11, int(font_size))
            bg, surface, ink, muted, line, accent, hover, pressed = (
                ("#111923", "#182330", "#e6edf6", "#9faec4", "#34455b",
                 "#7ab4ff", "#223b59", "#304e70")
                if self._dark else
                ("#f8fbfe", "#ffffff", "#11183c", "#66769c", "#d1def2",
                 "#175fe6", "#edf4ff", "#e3efff")
            )
            self._navigation_toggle.setIcon(atlas_icon(
                QtCore, QtGui, "context-book",
                color="#7ab4ff" if self._dark else accent, size=28,
            ))
            self._navigation_toggle.setIconSize(QtCore.QSize(28, 24))
            self._navigation_controller.sync()
            self.setStyleSheet(f"""
                QWidget#desktopGuide {{ background: {bg}; color: {ink};
                    font-family: 'Segoe UI', sans-serif; font-size: {self._font_size}px; }}
                QWidget#guideNav {{ background: {surface}; border-right: 1px solid {line}; }}
                QPushButton#guideNavigationToggle {{ color: {accent}; background: {surface};
                    border: 1px solid {line}; border-radius: 6px;
                    padding: 9px 14px; font-weight: 600; }}
                QPushButton#guideNavigationToggle:hover {{ background: {hover};
                    border-color: {accent}; }}
                QPushButton#guideNavigationToggle:checked {{ background: {pressed};
                    border-color: {accent}; }}
                QFrame#sidebarOverlay {{ background: {surface}; border: 1px solid {line}; }}
                QWidget#guideNavItems {{ background: {surface}; }}
                QWidget#guidePageContent QLabel {{ color: {ink}; background: transparent; }}
                QWidget#guideNav QLabel {{ color: {ink}; background: transparent; }}
                QWidget#guideNav QLabel#guideMuted {{ color: {muted}; }}
                QLabel#guideNavHeading {{ font-size: {self._font_size + 6}px; font-weight: 700; padding: 2px 4px 8px; }}
                QLabel#guideSearchLabel, QLabel#guideSearchStatus {{ font-size: {self._font_size}px; font-weight: 600; }}
                QLabel#guideSearchStatus {{ color: {muted}; padding-top: 4px; }}
                QLabel#guideMuted {{ color: {muted}; }}
                QLineEdit#guideSearch {{ background: {bg}; color: {ink}; border: 1px solid {line};
                    border-radius: 6px; padding: 7px 8px; min-height: {self._font_size + 14}px; }}
                QSplitter#guideSplitter::handle {{ background: transparent; }}
                QSplitter#guideSplitter::handle:hover {{ background: {accent}; }}
                QScrollArea {{ background: {bg}; border: 0; }}
                QTextBrowser#guidePrivacyDocument {{ background: {bg}; color: {ink};
                    border: 0; padding: 0; }}
                QWidget#guidePageContent {{ background: {bg}; color: {ink}; }}
                QPushButton#guideNavButton, QPushButton#guideTopicButton {{ text-align: left;
                    color: {ink}; background: transparent; border: 0; border-radius: 6px;
                    padding: 7px 8px; }}
                QPushButton#guideNavButton:hover, QPushButton#guideTopicButton:hover {{ background: {hover}; }}
                QPushButton#guideNavButton:checked, QPushButton#guideTopicButton:checked {{
                    color: {accent}; background: {pressed}; font-weight: 600; }}
                QLabel#guideEyebrow {{ color: {accent}; font-size: {max(11, self._font_size - 2)}px;
                    font-weight: 700; letter-spacing: 1px; }}
                QLabel#guidePageTitle {{ color: {ink}; font-size: {self._font_size + 10}px; font-weight: 700; }}
                QLabel#guideLead {{ color: {muted}; font-size: {self._font_size + 2}px; }}
                QLabel#guideSectionTitle {{ color: {ink}; font-size: {self._font_size + 4}px; font-weight: 650; }}
                QFrame#guideStepCard, QFrame#guideDirectoryCard,
                QFrame#guideGlossaryCard {{ background: {surface}; border: 1px solid {line}; border-radius: 9px; }}
                QPushButton#guideEntryCard {{ color: {ink}; background: {surface};
                    border: 1px solid {line}; border-radius: 9px; padding: 0; text-align: left; }}
                QPushButton#guideEntryCard:hover {{ background: {hover}; border-color: {accent}; }}
                QPushButton#guideEntryCard:pressed {{ background: {pressed}; border-color: {accent}; }}
                QPushButton#guideEntryCard:focus {{ border: 2px solid {accent}; }}
                QFrame#guidePhaseCard {{ background: {surface}; border: 1px solid {line}; border-radius: 7px; }}
                QFrame#guideNotice {{ background: {hover}; border: 1px solid {line};
                    border-left: 4px solid {accent}; border-radius: 6px; }}
                QFrame#guideExampleCard {{ background: {hover}; border: 1px solid {line};
                    border-radius: 9px; }}
                QLabel#guideCardTitle, QLabel#guideStepTitle, QLabel#guideGlossaryTerm {{
                    color: {ink}; font-size: {self._font_size + 1}px; font-weight: 650; }}
                QLabel#guideEntryTitle {{ color: {accent}; font-weight: 650; }}
                QLabel#guidePhaseName {{ color: {ink}; }}
                QLabel#guideBody, QLabel#guideFaqAnswer {{ color: {ink}; }}
                QLabel#guidePhaseNumber, QLabel#guideStepNumber {{ color: {accent}; font-weight: 700;
                    background: {pressed}; border-radius: 16px; }}
                QLabel#guideExampleTag {{ color: {accent}; font-weight: 700; }}
                QPushButton#guideEntryButton, QPushButton#guideLinkButton,
                QPushButton#guideReferenceLink {{ color: {accent}; background: transparent; border: 0;
                    text-align: left; padding: 7px 2px; font-weight: 600; }}
                QPushButton#guideFeaturedTopic {{ color: {accent}; background: {surface};
                    border: 1px solid {line}; border-radius: 6px; text-align: left; padding: 8px 10px; }}
                QPushButton#guideFeaturedTopic:hover {{ background: {hover}; border-color: {accent}; }}
                QPushButton#guideEntryButton:hover, QPushButton#guideLinkButton:hover,
                QPushButton#guideReferenceLink:hover {{ text-decoration: underline; }}
                QPushButton#guideFaqQuestion, QPushButton#guideHomeFaqQuestion {{
                    color: {ink}; background: transparent; border: 0;
                    border-radius: 5px; text-align: left; padding: 7px 6px; font-weight: 600; }}
                QPushButton#guideFaqQuestion:hover, QPushButton#guideFaqQuestion:checked,
                QPushButton#guideHomeFaqQuestion:hover, QPushButton#guideHomeFaqQuestion:checked {{
                    color: {accent}; background: {hover}; }}
                QPushButton#guideFaqQuestion:focus, QPushButton#guideHomeFaqQuestion:focus {{
                    color: {accent}; background: {pressed}; }}
                QFrame#guideHomeFaqCard, QFrame#guideArticleFaqCard {{
                    background: {surface}; border: 1px solid {line}; border-radius: 8px; }}
                QFrame#guidePhaseCard[compactGuidePanel="true"],
                QFrame#guideHomeFaqCard[compactGuidePanel="true"],
                QFrame#guideDirectoryCard[compactGuidePanel="true"],
                QFrame#guideStepCard[compactGuidePanel="true"],
                QFrame#guideExampleCard[compactGuidePanel="true"],
                QFrame#guideGlossaryCard[compactGuidePanel="true"],
                QFrame#guideArticleFaqCard[compactGuidePanel="true"] {{
                    background: transparent; border: 0; border-radius: 0; }}
                QFrame#guideReferenceHeader {{ background: {surface}; border-bottom: 1px solid {line}; }}
                QFrame#guideReferenceHeader QLabel {{ color: {ink}; background: transparent; }}
            """)
            if self._current_page_id == "privacy":
                self.format_privacy_document()
            from core.gui.shell.navigation import set_back_button_presentation
            set_back_button_presentation(
                self._reference_back, QtCore, QtGui, dark=dark,
                label=labels["back"], text=labels["back"],
            )
            self._apply_compact_page_spacing()
            self._queue_visible_button_wrap()

    return DesktopGuide(parent)
