# core/gui/jury_chooser.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Provider navigation and configuration panel for the desktop Jury chooser.

Qt is supplied by the caller so importing the desktop module stays optional-Qt.
The existing desktop window remains the owner of selection and provider-check
semantics; this module only arranges its widgets and staged setting editors.
"""
from __future__ import annotations

from collections.abc import Mapping
import sys
from typing import Any
from core.gui.shell.chrome import _MAC_NATIVE_LEADING_RESERVE


_ALIASES = {
    "ollama_local": "ollama", "freetoken_authenticated": "freetoken",
    "antigravity_cli": "antigravity", "openai_compatible": "openai",
    "codex-cli": "codex_cli", "claude-code": "claude_cli",
}
_QUICK = (
    ("codex_cli", "Codex CLI"),
    ("claude_cli", "Claude Code"),
    ("openai", "OpenAI / compatible"),
    ("ollama", "Ollama"),
    ("freetoken", "FreeToken"),
)
_DIRECTORY = (
    ("anthropic", "Anthropic API"),
    ("antigravity", "Antigravity CLI"),
    ("deepseek", "DeepSeek"),
    ("gemini", "Gemini API"),
    ("gemini_cli", "Gemini CLI"),
    ("glm", "GLM (ZhipuAI)"),
    ("xai", "Grok (xAI)"),
    ("groq", "Groq"),
    ("mistral", "Mistral AI"),
    ("opencode_cli", "OpenCode CLI"),
    ("opencode", "OpenCode Zen"),
    ("openrouter", "OpenRouter"),
    ("typesafe", "TypeSafe AI"),
    ("host", "Host integration"),
)
_JURY2_ONLY_PROVIDERS = frozenset({"typesafe"})
_MACOS_UNSUPPORTED_PROVIDERS = frozenset({"freetoken"})
_ADVANCED_NAMES = {
    "REASONING", "REASONING_EFFORT",
}


def canonical_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    return _ALIASES.get(provider, provider)


def _provider_label(window, provider: str) -> str:
    for provider_id, label in (*_QUICK, *_DIRECTORY):
        if canonical_provider(provider_id) == canonical_provider(provider):
            return label
    try:
        label = str(window._llm_provider_name(provider) or "").strip()
    except (AttributeError, TypeError, ValueError):
        label = ""
    return label or provider.replace("_", " ").title()


def _setting_provider_ids(row: Mapping[str, Any]) -> set[str]:
    ids = {
        canonical_provider(row.get("check_provider")),
        canonical_provider(row.get("model_provider")),
    }
    name = str(row.get("name") or "").upper()
    if name in {"CITATION_VERIFIER_LLM_HOST_COMMAND", "CITATION_VERIFIER_MODEL"}:
        ids.add("host")
    prefixes = (
        ("OPENAI_", "openai"), ("ANTHROPIC_", "anthropic"),
        ("GEMINI_CLI_", "gemini_cli"), ("GEMINI_", "gemini"),
        ("OPENROUTER_", "openrouter"), ("OLLAMA_", "ollama"),
        ("DEEPSEEK_", "deepseek"), ("GROQ_", "groq"), ("XAI_", "xai"),
        ("FREETOKEN_", "freetoken"), ("TYPESAFE_", "typesafe"),
        ("ZHIPUAI_", "glm"), ("MISTRAL_", "mistral"),
        ("OPENCODE_CLI_", "opencode_cli"), ("OPENCODE_", "opencode"),
        ("CLAUDE_CLI_", "claude_cli"), ("CODEX_CLI_", "codex_cli"),
        ("ANTIGRAVITY_CLI_", "antigravity"), ("CALLIMACHUS_HOST_", "host"),
    )
    for prefix, provider in prefixes:
        if name.startswith(prefix):
            ids.add(canonical_provider(provider))
            break
    ids.discard("")
    return ids


def _advanced_row(row: Mapping[str, Any]) -> bool:
    name = str(row.get("name") or "").upper()
    return name in {"CITATION_VERIFIER_" + item for item in _ADVANCED_NAMES}


def _provider_configuration_row(row: Mapping[str, Any], provider: str) -> bool:
    """Expose provider credentials, endpoints, and the explicit host command."""
    if canonical_provider(provider) not in _setting_provider_ids(row):
        return False
    name = str(row.get("name") or "").upper()
    if row.get("control") == "secret" and name.endswith("_API_KEY"):
        return True
    return name in {
        "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "OLLAMA_HOST", "FREETOKEN_HOST",
        "CITATION_VERIFIER_LLM_HOST_COMMAND", "CITATION_VERIFIER_MODEL",
    }


class JuryChooser:
    """A view adapter over the desktop window's existing Jury widgets."""

    def __init__(self, window, QtCore, QtGui, QtWidgets, text):
        self.window = window
        self.QtCore, self.QtGui, self.QtWidgets = QtCore, QtGui, QtWidgets
        self.text = text
        self.active_provider = "codex_cli"
        self.added_providers: list[str] = []
        self.directory_open = False
        self.other_button = None
        # Explicitly detach reused controls before reparenting: Qt's implicit
        # removal can leave borrowed QWidgetItems valid in the binding cache.
        legacy_layouts = window.llm_choice_dialog.findChildren(QtWidgets.QLayout)
        for widget in (window.llm_picker_check, window.llm_picker_status, window.llm_status):
            for layout in legacy_layouts:
                if layout.indexOf(widget) >= 0:
                    layout.removeWidget(widget)
                    break
        self.root = QtWidgets.QWidget(window.llm_choice_dialog)
        self.root.setObjectName("juryChooserWorkspace")
        self.layout = QtWidgets.QVBoxLayout(self.root)
        self.layout.setSizeConstraint(
            QtWidgets.QLayout.SizeConstraint.SetNoConstraint
        )
        self.layout.setContentsMargins(18, 8, 18, 0)
        self.layout.setSpacing(10)
        self.header = QtWidgets.QWidget(self.root)
        self.header.setObjectName("juryChooserHeader")
        header_layout = QtWidgets.QHBoxLayout(self.header)
        self.header_layout = header_layout
        header_layout.setContentsMargins(
            _MAC_NATIVE_LEADING_RESERVE if sys.platform == "darwin" else 0,
            0,
            0,
            0,
        )
        self.navigation_toggle = QtWidgets.QToolButton(self.header)
        self.navigation_toggle.setObjectName("juryNavigationToggle")
        self.navigation_toggle.setProperty("atlasIcon", "menu")
        self.navigation_toggle.setAccessibleName(
            text.get("navigation_toggle", "Show or hide provider navigation")
        )
        self.navigation_toggle.setToolTip(self.navigation_toggle.accessibleName())
        self.navigation_toggle.setAutoRaise(True)
        self.navigation_toggle.hide()
        header_layout.addWidget(self.navigation_toggle)
        self.title = QtWidgets.QLabel(self.header)
        self.title.setObjectName("juryChooserTitle")
        header_layout.addWidget(self.title)
        header_layout.addStretch(1)
        self.close_header = QtWidgets.QToolButton(self.header)
        self.close_header.setObjectName("juryChooserClose")
        self.close_header.setToolTip(text.get("close", "Chiudi"))
        self.close_header.clicked.connect(window.llm_choice_dialog.accept)
        header_layout.addWidget(self.close_header)
        self.layout.addWidget(self.header)
        self.header_separator = QtWidgets.QFrame(self.root)
        self.header_separator.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        self.layout.addWidget(self.header_separator)
        body = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self.root)
        body.setObjectName("juryProviderSplitter")
        body.setChildrenCollapsible(False)
        body.setHandleWidth(5)
        self.provider_splitter = body
        self.nav_scroll = QtWidgets.QScrollArea(body)
        self.nav_scroll.setObjectName("juryProviderNavigationScroll")
        self.nav_scroll.setWidgetResizable(True)
        self.nav_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.nav_scroll.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.nav_scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.nav_scroll.setMinimumWidth(0)
        self.nav_scroll.setMaximumWidth(440)
        self.nav = QtWidgets.QWidget(self.nav_scroll)
        self.nav.setObjectName("juryProviderNavigation")
        self.nav.setMinimumWidth(0)
        self.nav.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.nav_layout = QtWidgets.QVBoxLayout(self.nav)
        self.nav_layout.setContentsMargins(0, 0, 0, 0)
        self.nav_layout.setSpacing(8)
        self.nav_scroll.setWidget(self.nav)
        chooser = self

        class ContentScrollArea(QtWidgets.QScrollArea):
            def event(scroll_area, event):
                result = super().event(event)
                if (
                    hasattr(chooser, "sidebar_navigation")
                    and event.type() in (
                        QtCore.QEvent.Type.FontChange,
                        QtCore.QEvent.Type.StyleChange,
                    )
                ):
                    QtCore.QTimer.singleShot(0, chooser._refresh_sidebar_sizing)
                return result

            def resizeEvent(scroll_area, event):
                super().resizeEvent(event)
                if hasattr(chooser, "content"):
                    viewport_width = scroll_area.viewport().width()
                    chooser._update_action_row_layout(viewport_width)
                    if hasattr(chooser, "sidebar_navigation"):
                        chooser._update_header_controls()
                        QtCore.QTimer.singleShot(
                            0, chooser._update_header_controls,
                        )

        self.content_scroll = ContentScrollArea(body)
        self.content_scroll.setObjectName("juryProviderPageScroll")
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.content_scroll.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.content_scroll.setMinimumWidth(0)
        self.content_scroll.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.content_scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        class ContentWidget(QtWidgets.QWidget):
            def minimumSizeHint(content_widget):
                hint = super().minimumSizeHint()
                return QtCore.QSize(0, hint.height())

        self.content = ContentWidget(self.content_scroll)
        self.content.setObjectName("juryProviderContent")
        self.content.setMinimumWidth(0)
        self.content.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.content_scroll.setWidget(self.content)
        self.content_layout = QtWidgets.QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(10)
        self.config = QtWidgets.QWidget(self.content)
        self.config_layout = QtWidgets.QVBoxLayout(self.config)
        self.config_layout.setContentsMargins(0, 0, 0, 0)
        self.provider_heading_row = QtWidgets.QWidget(self.content)
        self.provider_heading_row.setObjectName("juryProviderHeadingRow")
        self.provider_heading_layout = QtWidgets.QHBoxLayout(self.provider_heading_row)
        self.provider_heading_layout.setContentsMargins(0, 0, 0, 0)
        self.provider_heading_layout.setSpacing(8)
        self.provider_heading = QtWidgets.QLabel(self.provider_heading_row)
        self.provider_heading.setObjectName("juryProviderHeading")
        self.provider_heading.setWordWrap(True)
        self.provider_heading_layout.addWidget(self.provider_heading, 1)
        self.actions_row = QtWidgets.QWidget(self.content)
        self.actions_layout = QtWidgets.QHBoxLayout(self.actions_row)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.save_button = QtWidgets.QPushButton(text.get("jury_save_configuration", "Salva configurazione"), self.actions_row)
        self.save_button.setObjectName("juryProviderSave")
        self.save_button.clicked.connect(self._save_settings)
        self.actions_layout.addWidget(self.save_button, 1)
        window.llm_picker_check.setParent(self.actions_row)
        self.actions_layout.addWidget(window.llm_picker_check, 1)
        self.config_scope = QtWidgets.QLabel(
            text.get("jury_provider_shared_scope", "Le selezioni degli altri provider restano salvate.\nChiavi ed endpoint sono condivisi; i modelli dei due Jury sono indipendenti."),
            self.content,
        )
        self.config_scope.setObjectName("juryProviderScope")
        self.config_scope.setWordWrap(True)
        self.model_stack = QtWidgets.QStackedWidget(self.content)
        self.model_stack.setObjectName("juryProviderModelStack")
        self.model_stack.setMinimumWidth(0)
        self.model_stack.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.models_page = QtWidgets.QWidget(self.model_stack)
        self.models_page.setMinimumWidth(0)
        models_layout = QtWidgets.QVBoxLayout(self.models_page)
        models_layout.setContentsMargins(0, 0, 0, 0)
        self.models_heading = QtWidgets.QLabel(self.models_page)
        self.models_heading.setObjectName("juryModelsHeading")
        self.models_heading.setWordWrap(True)
        models_layout.addWidget(self.models_heading)
        window.llm_picker_status.setParent(self.models_page)
        window.llm_status.setParent(self.models_page)
        self.directory_page = QtWidgets.QWidget(self.model_stack)
        self.directory_page.setMinimumWidth(0)
        directory_layout = QtWidgets.QVBoxLayout(self.directory_page)
        directory_layout.setContentsMargins(0, 0, 0, 0)
        directory_layout.addWidget(QtWidgets.QLabel(
            text.get("jury_provider_directory_help", "Choose a supported provider"),
            self.directory_page,
        ))
        self.directory_list = QtWidgets.QListWidget(self.directory_page)
        self.directory_list.setObjectName("juryProviderDirectory")
        self.directory_list.setIconSize(QtCore.QSize(28, 28))
        self.directory_list.setMouseTracking(True)
        self.directory_list.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_AlwaysShowToolTips, True,
        )
        self.directory_list.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.directory_list.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.directory_list.setSizeAdjustPolicy(
            QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents
        )
        class DirectoryDelegate(QtWidgets.QStyledItemDelegate):
            def paint(delegate, painter, option, index):
                super().paint(painter, option, index)
                from .atlas_icons import icon
                arrow = QtCore.QRect(option.rect.right()-28, option.rect.center().y()-10, 20, 20)
                icon(QtCore, QtGui, "chevron-right", self._palette()["blue"]).paint(painter, arrow)
        self.directory_list.setItemDelegate(DirectoryDelegate(self.directory_list))
        directory_layout.addWidget(self.directory_list, 1)
        self.model_stack.addWidget(self.models_page)
        self.model_stack.addWidget(self.directory_page)

        self.advanced_toggle = QtWidgets.QToolButton(self.content)
        self.advanced_toggle.setObjectName("juryAdvancedToggle")
        self.advanced_toggle.setText(text.get("jury_advanced", "Advanced"))
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self.advanced_panel = QtWidgets.QWidget(self.content)
        self.advanced_panel.setObjectName("juryAdvancedPanel")
        self.advanced_panel.setVisible(False)
        self.advanced_form_host = QtWidgets.QWidget(self.advanced_panel)
        self.advanced_form = QtWidgets.QFormLayout(self.advanced_form_host)
        self.advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_layout = QtWidgets.QVBoxLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.addWidget(QtWidgets.QLabel(text.get("jury_advanced_scope", "Shared by the whole analysis"), self.advanced_panel))
        advanced_layout.addWidget(self.advanced_form_host)
        self.selection_summary = QtWidgets.QLabel(self.root)
        self.selection_summary.setObjectName("jurySelectionSummary")
        self.selection_summary.setWordWrap(True)
        self.content_layout.addWidget(self.provider_heading_row)
        self.content_layout.addWidget(self.config)
        self.content_layout.addWidget(self.actions_row)
        self.content_layout.addWidget(self.config_scope)
        self.content_layout.addWidget(self.model_stack)
        self.content_layout.addStretch(1)
        self._advanced_space = self.content_layout.itemAt(self.content_layout.count() - 1)
        self._advanced_space_index = self.content_layout.count() - 1
        self.content_layout.addWidget(self.advanced_toggle)
        self.content_layout.addWidget(self.advanced_panel)
        body.addWidget(self.nav_scroll)
        body.addWidget(self.content_scroll)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        self.layout.addWidget(body, 1)
        footer = QtWidgets.QWidget(self.root)
        footer_layout = QtWidgets.QHBoxLayout(footer)
        self.footer_layout = footer_layout
        footer_layout.setContentsMargins(0, 8, 0, 0)
        footer_layout.addWidget(self.selection_summary, 1)
        self.close_footer = QtWidgets.QPushButton(
            text.get("jury_save_close", "Salva e chiudi"), footer,
        )
        self.close_footer.setObjectName("juryChooserCloseFooter")
        self.close_footer.clicked.connect(self._save_and_close)
        footer_layout.addWidget(self.close_footer)
        self.footer_separator = QtWidgets.QFrame(self.root)
        self.footer_separator.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        self.layout.addWidget(self.footer_separator)
        self.layout.addWidget(footer)
        self._install_into_dialog()
        self._populate_directory()
        self._rebuild_navigation()
        self._build_advanced()
        self._provider_columns_initialized = False
        self.show_provider(self.active_provider)
        self.root.ensurePolished()
        self._sidebar_minimum_content_width = (
            self._provider_content_required_width()
        )
        self._sidebar_preferred_width = self._provider_navigation_required_width()
        from core.gui.shell.navigation import install_sidebar_toggle
        self.sidebar_navigation = install_sidebar_toggle(
            self.provider_splitter,
            self.nav_scroll,
            self.navigation_toggle,
            QtCore,
            QtWidgets,
            minimum_content_width=self._sidebar_minimum_content_width,
            preferred_width=self._sidebar_preferred_width,
            compact_only=True,
            edge_drawer=True,
            available_width=self._navigation_available_width,
        )
        self.provider_splitter.setSizes([
            self._sidebar_preferred_width,
            self._sidebar_minimum_content_width,
        ])
        self.apply_theme()
        self._update_header_controls()
        from core.gui.shell.scroll import install_edge_scrollbar
        self.edge_scrollbar = install_edge_scrollbar(
            window.llm_choice_dialog,
            [self.content_scroll],
            QtCore,
            QtWidgets,
            compact_when=lambda: self.sidebar_navigation.is_overlay,
        )
        self.navigation_toggle.installEventFilter(self.edge_scrollbar)
        from core.gui.shell.resize import install_table_resize
        install_table_resize(window.llm_choices, QtCore, QtWidgets)
        self._install_dialog_geometry_tracking()

    def _install_dialog_geometry_tracking(self):
        dialog = self.window.llm_choice_dialog
        if not callable(getattr(self.window, "installEventFilter", None)):
            return
        chooser = self

        class DialogGeometryFilter(self.QtCore.QObject):
            def eventFilter(self, watched, event):
                kind = event.type()
                if watched is dialog and kind == chooser.QtCore.QEvent.Type.Show:
                    chooser._fit_dialog_to_parent()
                elif (
                    watched is not dialog
                    and kind in (
                        chooser.QtCore.QEvent.Type.Resize,
                        chooser.QtCore.QEvent.Type.Move,
                    )
                    and dialog.isVisible()
                ):
                    chooser.QtCore.QTimer.singleShot(
                        0, chooser._fit_dialog_to_parent,
                    )
                return False

        tracker = DialogGeometryFilter(self.window)
        self.window.installEventFilter(tracker)
        dialog.installEventFilter(tracker)
        central_widget = getattr(self.window, "centralWidget", None)
        central_widget = central_widget() if callable(central_widget) else None
        if central_widget is not None:
            central_widget.installEventFilter(tracker)
        self._dialog_geometry_filter = tracker

    def _fit_dialog_to_parent(self):
        dialog = self.window.llm_choice_dialog
        central_widget = getattr(self.window, "centralWidget", None)
        host = central_widget() if callable(central_widget) else None
        if host is None:
            host = self.window
        if not callable(getattr(host, "contentsRect", None)):
            return
        contents = host.contentsRect()
        window_contents = self.window.contentsRect()
        if host is not self.window and (
            contents.isEmpty()
            or contents.width() < max(1, window_contents.width() // 2)
            or contents.height() < max(1, window_contents.height() // 2)
        ):
            host = self.window
            contents = window_contents
        if contents.isEmpty():
            return
        origin = host.mapToGlobal(contents.topLeft())
        host_rect = self.QtCore.QRect(origin, contents.size())
        if host_rect.width() <= 640 or host_rect.height() <= 520:
            frame_target = host_rect
        else:
            width = min(1000, max(1, round(host_rect.width() * .9)))
            height = min(840, max(1, round(host_rect.height() * .9)))
            frame_target = self.QtCore.QRect(
                host_rect.x() + (host_rect.width() - width) // 2,
                host_rect.y() + (host_rect.height() - height) // 2,
                width,
                height,
            )
        frame_rect = dialog.frameGeometry()
        client_rect = dialog.geometry()
        left = max(0, client_rect.left() - frame_rect.left())
        top = max(0, client_rect.top() - frame_rect.top())
        right = max(0, frame_rect.right() - client_rect.right())
        bottom = max(0, frame_rect.bottom() - client_rect.bottom())
        dialog.setGeometry(frame_target.adjusted(left, top, -right, -bottom))
        navigation = getattr(self, "sidebar_navigation", None)
        if navigation is not None:
            navigation.sync()
            self.QtCore.QTimer.singleShot(0, self._update_header_controls)

    def _navigation_available_width(self):
        dialog = self.window.llm_choice_dialog
        dialog_width = dialog.contentsRect().width()
        outer = dialog.layout().contentsMargins()
        inner = self.layout.contentsMargins()
        gutters = outer.left() + outer.right() + inner.left() + inner.right()
        available = max(0, dialog_width - gutters)
        available -= self.provider_splitter.handleWidth()
        pixels = self.provider_splitter.font().pixelSize()
        scale = max(1.0, pixels / 16.0) if pixels > 0 else 1.0
        scaled_baseline = int(self._sidebar_minimum_content_width * scale)
        current_minimum = self._provider_content_required_width()
        navigation = getattr(self, "sidebar_navigation", None)
        docked_width = int(getattr(
            navigation, "_docked_width", self._sidebar_preferred_width,
        ))
        navigation_growth = max(
            0, self._provider_navigation_required_width() - docked_width,
        )
        # Match the sidebar helper's baseline font scaling while correcting for
        # the actual editor, control, and provider button hints after a style or
        # accessibility font change.
        return max(
            0, available + scaled_baseline - current_minimum - navigation_growth,
        )

    @staticmethod
    def _required_widget_width(widget):
        return max(widget.sizeHint().width(), widget.minimumSizeHint().width())

    def _provider_navigation_required_width(self):
        widths = []
        for provider, button in self._provider_buttons.items():
            row = button.parentWidget()
            row_layout = row.layout() if row is not None else None
            row_width = self._required_widget_width(button)
            if row_layout is not None:
                margins = row_layout.contentsMargins()
                row_width += margins.left() + margins.right()
                badge = self._provider_badges.get(provider)
                if badge is not None:
                    row_width += row_layout.spacing() + max(
                        badge.minimumWidth(),
                        self._required_widget_width(badge),
                    )
            widths.append(row_width)
        if self.other_button is not None:
            widths.append(self._required_widget_width(self.other_button))
        section_title = self.nav.findChild(self.QtWidgets.QLabel, "sectionTitle")
        if section_title is not None:
            widths.append(self._required_widget_width(section_title))
        margins = self.nav_layout.contentsMargins()
        scrollbar_width = self.nav_scroll.verticalScrollBar().sizeHint().width()
        return max(widths, default=0) + margins.left() + margins.right() + (
            2 * self.nav_scroll.frameWidth() + scrollbar_width
        )

    def _provider_content_required_width(self):
        actions = [button for button in (self.save_button, self.window.llm_picker_check)
                   if not button.isHidden()]
        widths = [self._required_widget_width(widget) for widget in actions]
        action_margins = self.actions_layout.contentsMargins()
        widths.append(
            sum(button.sizeHint().width() for button in actions)
            + self.actions_layout.spacing() * max(0, len(actions) - 1)
            + action_margins.left()
            + action_margins.right()
        )
        if self.model_stack.currentWidget() is self.directory_page:
            # The directory hides configuration actions, but still needs room
            # for provider labels; treating it as empty collapses the sidebar.
            widths.append(
                max(0, self.directory_list.sizeHintForColumn(0))
                + 2 * self.directory_list.frameWidth()
            )
        for class_name in (
            "QLineEdit", "QComboBox", "QSpinBox", "QDoubleSpinBox",
        ):
            widget_type = getattr(self.QtWidgets, class_name, None)
            if widget_type is None:
                continue
            for widget in self.config.findChildren(widget_type):
                if not widget.isHidden():
                    widths.append(max(
                        widget.sizeHint().width(),
                        widget.minimumSizeHint().width(),
                    ))
        return max(widths, default=1)

    def _update_action_row_layout(self, width: int | None = None):
        if width is None:
            width = self.content_scroll.viewport().width()
        margins = self.actions_layout.contentsMargins()
        actions = [button for button in (self.save_button, self.window.llm_picker_check)
                   if not button.isHidden()]
        required_width = (
            sum(button.sizeHint().width() for button in actions)
            + self.actions_layout.spacing() * max(0, len(actions) - 1)
        )
        available_width = max(0, width - margins.left() - margins.right())
        compact = available_width < required_width
        direction = (
            self.QtWidgets.QBoxLayout.Direction.TopToBottom
            if compact else self.QtWidgets.QBoxLayout.Direction.LeftToRight
        )
        if self.actions_layout.direction() == direction:
            return
        self.actions_layout.setDirection(direction)
        stretch = 0 if compact else 1
        self.actions_layout.setStretch(0, stretch)
        self.actions_layout.setStretch(1, stretch)
        self.actions_layout.invalidate()

    def _update_header_controls(self):
        native_macos = sys.platform == "darwin"
        leading_reserve = _MAC_NATIVE_LEADING_RESERVE if native_macos else 0
        compact = bool(
            getattr(getattr(self, "sidebar_navigation", None), "is_overlay", False)
        )
        header_leading = 16 if native_macos and compact else leading_reserve
        tight_gutters = self.window.llm_choice_dialog.contentsRect().width() < 900
        if getattr(self, "_header_layout_mode", None) != (compact, tight_gutters):
            self._header_layout_mode = (compact, tight_gutters)
            self._header_compact = compact
            self.header_layout.setContentsMargins(
                header_leading, 4 if compact else 0, 0, 4 if compact else 0,
            )
            self.window.llm_choice_dialog.layout().setContentsMargins(
                *((0, 0, 0, 0) if tight_gutters else self._wide_dialog_margins)
            )
            self.layout.setContentsMargins(
                0 if tight_gutters else 18,
                4 if compact else 8,
                0 if tight_gutters else 18,
                0,
            )
            footer_gutter = 16 if tight_gutters else 0
            self.footer_layout.setContentsMargins(
                footer_gutter, 8, footer_gutter, 16,
            )
            self.header_layout.removeWidget(self.close_header)
            if compact and not native_macos:
                self.header_layout.insertWidget(0, self.close_header)
            else:
                self.header_layout.addWidget(self.close_header)
            self.title.setProperty("compactHeader", compact)
            self.title.style().unpolish(self.title)
            self.title.style().polish(self.title)
            self.layout.setSpacing(6 if compact else 10)
            self.content_layout.setSpacing(6 if compact else 10)
            self.config_layout.setContentsMargins(0, 14 if compact else 0, 0, 0)
            self.models_page.layout().setContentsMargins(0, 18 if compact else 0, 0, 0)
            self._advanced_space.changeSize(
                0, 18 if compact else 0,
                self.QtWidgets.QSizePolicy.Policy.Minimum,
                self.QtWidgets.QSizePolicy.Policy.Expanding,
            )
            self.content_layout.invalidate()
            for card in self.config.findChildren(
                self.QtWidgets.QFrame, "juryProviderSettings",
            ):
                card.setProperty("compactPage", compact)
                card.style().unpolish(card)
                card.style().polish(card)
                card.updateGeometry()
        if native_macos:
            dialog = self.window.llm_choice_dialog
            handle = dialog.windowHandle()
            safe = (
                handle.safeAreaMargins()
                if handle is not None
                else self.QtCore.QMargins()
            )
            # Compact content fills the host except where macOS reports a
            # horizontal safe inset; keep only the top titlebar clearance.
            margins = (
                (
                    safe.left(), max(16, safe.top()), safe.right(), 0,
                )
                if tight_gutters
                else (
                    max(16, safe.left()), max(16, safe.top()),
                    max(16, safe.right()), 16,
                )
            )
            outer = dialog.layout()
            if outer.getContentsMargins() != margins:
                outer.setContentsMargins(*margins)
            if self.layout.getContentsMargins() != (0, 0, 0, 0):
                self.layout.setContentsMargins(0, 0, 0, 0)
            header_margins = (header_leading, 0, 0, 0)
            if self.header_layout.getContentsMargins() != header_margins:
                self.header_layout.setContentsMargins(*header_margins)
        label = self.text.get("close", "Close")
        self.close_header.setVisible(not native_macos)
        self.navigation_toggle.setVisible(compact)
        palette = self._palette()
        from core.gui.shell.navigation import set_back_button_presentation
        set_back_button_presentation(
            self.close_header,
            self.QtCore,
            self.QtGui,
            label=label,
            dark=bool(getattr(self.window, "_dark", False)),
            active=compact,
            text="",
        )
        self.close_header.setAccessibleName(label)
        self.close_header.setToolTip(label)
        if native_macos and compact:
            self.close_header.setFixedSize(30, 30)
            self.header_layout.setAlignment(
                self.close_header, self.QtCore.Qt.AlignmentFlag.AlignTop,
            )
        elif not compact:
            self._set_icon(self.close_header, "close", palette["icon"], 24)
            self.close_header.setIconSize(self.QtCore.QSize(24, 24))
        self.header_layout.invalidate()
        self.provider_heading_layout.invalidate()
        self.header.updateGeometry()
        self.provider_heading_row.updateGeometry()

    def _close_sidebar_overlay(self):
        navigation = getattr(self, "sidebar_navigation", None)
        if navigation is not None:
            navigation.close_overlay()

    def _save_and_close(self):
        if self._save_settings() is True:
            self.window.llm_choice_dialog.accept()

    def _install_into_dialog(self):
        dialog = self.window.llm_choice_dialog
        dialog.setMinimumSize(0, 0)
        outer = dialog.layout()
        self._wide_dialog_margins = outer.getContentsMargins()
        self._wide_root_margins = self.layout.getContentsMargins()
        outer.setSizeConstraint(
            self.QtWidgets.QLayout.SizeConstraint.SetNoConstraint
        )
        old = []
        while outer.count():
            item = outer.takeAt(0)
            if item.widget() is not None:
                old.append(item.widget())
            elif item.layout() is not None:
                old.append(item.layout())
        discovery = getattr(self.window, "llm_discovery_status", None)
        details = getattr(self.window, "llm_discovery_details", None)
        for widget in (discovery, details):
            if widget is not None:
                widget.hide()
                widget.setParent(dialog)
        old_tree = self.window.llm_choices
        old_tree.setParent(self.models_page)
        old_tree.setHeaderHidden(False)
        model_header = old_tree.header()
        model_header.setObjectName("juryProviderModelsHeader")
        model_header.setFixedHeight(30)
        model_header.setDefaultAlignment(
            self.QtCore.Qt.AlignmentFlag.AlignLeft
            | self.QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        old_tree.setIndentation(0)
        old_tree.setRootIsDecorated(False)
        old_tree.setObjectName("juryProviderModels")
        old_tree.setMinimumWidth(0)
        old_tree.setSizePolicy(
            self.QtWidgets.QSizePolicy.Policy.Ignored,
            self.QtWidgets.QSizePolicy.Policy.Preferred,
        )
        old_tree.setSelectionMode(self.QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        old_tree.clearSelection()
        old_tree.setMaximumHeight(220)
        self.empty_catalog_hint = self.QtWidgets.QLabel(old_tree.viewport())
        self.empty_catalog_hint.setObjectName("juryEmptyCatalogHint")
        self.empty_catalog_hint.setWordWrap(True)
        self.empty_catalog_hint.setAlignment(self.QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty_catalog_hint.setAttribute(
            self.QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents,
        )
        empty_layout = self.QtWidgets.QVBoxLayout(old_tree.viewport())
        empty_layout.setContentsMargins(12, 12, 12, 12)
        empty_layout.addWidget(self.empty_catalog_hint)
        self.empty_catalog_hint.hide()
        models_layout = self.models_page.layout()
        models_layout.addWidget(old_tree)
        self.window.llm_picker_status.setObjectName("juryPickerStatus")
        models_layout.addWidget(self.window.llm_picker_status)
        self.window.llm_status.hide()
        for widget in dialog.findChildren(self.QtWidgets.QWidget):
            if widget is not self.root and not self.root.isAncestorOf(widget):
                widget.hide()
        outer.addWidget(self.root, 1)
        self._set_title()

    def _populate_directory(self):
        for provider, label in _DIRECTORY:
            item = self.QtWidgets.QListWidgetItem(label)
            item.setData(self.QtCore.Qt.ItemDataRole.UserRole, provider)
            item.setIcon(self._provider_icon(provider))
            self.directory_list.addItem(item)
        self.directory_list.itemClicked.connect(
            lambda item: self.show_provider(str(item.data(self.QtCore.Qt.ItemDataRole.UserRole)))
        )

    def _set_title(self):
        role = str(getattr(self.window, "_llm_picker_role", "jury1"))
        number = 1 if role == "jury1" else 2
        title = self.text.get("atlas_choose_jury", "Scegli Jury {number}").format(number=number)
        self.title.setText(title)
        self.provider_heading.setText(_provider_label(self.window, self.active_provider))
        self.models_heading.setText(
            self.text.get("jury_models_heading", "Modelli {provider} per Jury {number}").format(
                provider=_provider_label(self.window, self.active_provider), number=number,
            )
        )

    def _set_icon(self, widget, key: str, color="#52617d", size=24):
        from .atlas_icons import icon
        widget.setIcon(icon(self.QtCore, self.QtGui, key, color=color, size=size))

    def _palette(self):
        if bool(getattr(self.window, "_dark", False)):
            return {
                "background": "#101722", "surface": "#1f2b3d", "text": "#edf3ff",
                "muted": "#b6c4d8", "border": "#364152", "accent": "#1f2b3d",
                "blue": "#8db8ff", "selected": "#243e62", "hover": "#243349",
                "badge": "#1c314d", "icon": "#c9d6e8",
            }
        return {
            "background": "#ffffff", "surface": "#ffffff", "text": "#172554",
            "muted": "#7180a4", "border": "#d7deed", "accent": "#f5f7fb",
            "blue": "#1268df", "selected": "#eaf2ff", "hover": "#f1f5fc",
            "badge": "#e8f1ff", "icon": "#53627d",
        }

    def apply_theme(self):
        """Refresh chooser-specific colors and icon contrast after theme changes."""
        self._applied_dark = bool(getattr(self.window, "_dark", False))
        palette = self._palette()
        navigation = getattr(self, "sidebar_navigation", None)
        if navigation is not None:
            navigation.overlay.setStyleSheet(
                "QFrame#sidebarOverlay { background-color:%(background)s; "
                "border:none; border-right:1px solid %(border)s; "
                "border-radius:0px; }" % palette
            )
        self.root.setStyleSheet(
            "QWidget#juryChooserWorkspace, QWidget#juryProviderNavigation, "
            "QWidget#juryProviderContent { background:%(background)s; color:%(text)s; }"
            "QFrame#juryProviderSettings { background:%(surface)s; border:1px solid %(border)s; "
            "border-radius:8px; padding:6px; }"
            "QFrame#juryProviderSettings[compactPage='true'] { background:transparent; "
            "border:0; border-radius:0; padding:0; }"
            "QTreeWidget#juryProviderModels, QListWidget#juryProviderDirectory { "
            "background:%(background)s; color:%(text)s; border:1px solid %(border)s; "
            "alternate-background-color:%(accent)s; }"
            "QTreeWidget#juryProviderModels::item, QListWidget#juryProviderDirectory::item { color:%(text)s; }"
            "QListWidget#juryProviderDirectory::item:disabled { color:%(muted)s; }"
            "QListWidget#juryProviderDirectory::item { padding:14px 32px 14px 12px; border-bottom:1px solid %(border)s; }"
            "QListWidget#juryProviderDirectory::item:hover { background:%(accent)s; }"
            "QListWidget#juryProviderDirectory::item:selected { color:%(blue)s; background:%(accent)s; }"
            "QLabel#juryChooserTitle { font-size:30px; font-weight:700; color:%(text)s; background:transparent; }"
            "QLabel#juryChooserTitle[compactHeader='true'] { font-size:22px; }"
            "QLabel#juryProviderHeading { font-size:32px; font-weight:700; color:%(text)s; background:transparent; }"
            "QLabel#juryModelsHeading { font-size:20px; font-weight:650; color:%(text)s; background:transparent; }"
            "QLabel#juryProviderConfigurationHeading { font-size:18px; font-weight:600; color:%(text)s; background:transparent; }"
            "QLabel#juryProviderScope, QLabel#juryProviderSharedScope, QLabel#juryFixedServiceHint, "
            "QLabel#juryEmptyCatalogHint { "
            "color:%(muted)s; background:transparent; }"
            "QLineEdit, QComboBox, QSpinBox { color:%(text)s; background:%(surface)s; "
            "placeholder-text-color:%(muted)s; selection-background-color:%(blue)s; }"
            "QLabel#settingCaption { color:%(text)s; background:transparent; }"
            "QLabel#settingNote { color:%(muted)s; background:transparent; }"
            "QLabel#juryPickerStatus { color:%(text)s; background:transparent; }"
            "QLabel#jurySelectionSummary { color:%(muted)s; background:transparent; }"
            "QToolButton#juryAdvancedToggle { color:%(text)s; background:transparent; }"
            "QHeaderView#juryProviderModelsHeader { background:%(background)s; }"
            "QHeaderView#juryProviderModelsHeader::section { color:%(muted)s; "
            "background:%(accent)s; border:0; border-bottom:1px solid %(border)s; "
            "padding:3px 8px; font-size:12px; font-weight:600; }"
            "QSplitter#juryProviderSplitter::handle { background:transparent; "
            "border-left:1px solid %(border)s; }"
            "QSplitter#juryProviderSplitter::handle:hover { background:%(blue)s; border-left:0; }"
            "QPushButton#juryChooserCloseFooter { color:%(text)s; background:%(accent)s; "
            "border:1px solid %(border)s; border-radius:7px; padding:7px 10px; font-weight:600; }"
            "QPushButton#juryChooserCloseFooter:hover { border-color:%(blue)s; }"
            "QPushButton#juryProviderSave:disabled, QPushButton#juryModelsCheck:disabled { "
            "color:%(muted)s; background:%(accent)s; border-color:%(border)s; }"
            "QLabel#sectionTitle { color:%(muted)s; font-weight:700; background:transparent; }" % palette
        )
        tree = self.window.llm_choices
        tree_palette = tree.palette()
        for group in (
            self.QtGui.QPalette.ColorGroup.Active,
            self.QtGui.QPalette.ColorGroup.Inactive,
            self.QtGui.QPalette.ColorGroup.Disabled,
        ):
            tree_palette.setColor(group, self.QtGui.QPalette.ColorRole.Base, self.QtGui.QColor(palette["background"]))
            tree_palette.setColor(group, self.QtGui.QPalette.ColorRole.AlternateBase, self.QtGui.QColor(palette["accent"]))
            tree_palette.setColor(group, self.QtGui.QPalette.ColorRole.Text, self.QtGui.QColor(palette["text"]))
            tree_palette.setColor(group, self.QtGui.QPalette.ColorRole.WindowText, self.QtGui.QColor(palette["text"]))
            tree_palette.setColor(group, self.QtGui.QPalette.ColorRole.Highlight, self.QtGui.QColor(palette["blue"]))
            tree_palette.setColor(group, self.QtGui.QPalette.ColorRole.HighlightedText, self.QtGui.QColor("#ffffff"))
        tree.setPalette(tree_palette)
        tree.viewport().setPalette(tree_palette)
        tree.viewport().setAutoFillBackground(True)
        for container in (self.config, self.advanced_panel):
            for widget_type in (
                self.QtWidgets.QLineEdit, self.QtWidgets.QComboBox, self.QtWidgets.QSpinBox,
            ):
                for editor in container.findChildren(widget_type):
                    editor_palette = editor.palette()
                    for group in (
                        self.QtGui.QPalette.ColorGroup.Active,
                        self.QtGui.QPalette.ColorGroup.Inactive,
                        self.QtGui.QPalette.ColorGroup.Disabled,
                    ):
                        editor_palette.setColor(
                            group, self.QtGui.QPalette.ColorRole.Base,
                            self.QtGui.QColor(palette["surface"]),
                        )
                        editor_palette.setColor(
                            group, self.QtGui.QPalette.ColorRole.Text,
                            self.QtGui.QColor(palette["text"]),
                        )
                        editor_palette.setColor(
                            group, self.QtGui.QPalette.ColorRole.WindowText,
                            self.QtGui.QColor(palette["text"]),
                        )
                        editor_palette.setColor(
                            group, self.QtGui.QPalette.ColorRole.PlaceholderText,
                            self.QtGui.QColor(palette["muted"]),
                        )
                    editor.setPalette(editor_palette)
        self.header_separator.setStyleSheet(f"color:{palette['border']};")
        self.footer_separator.setStyleSheet(f"color:{palette['border']};")
        self._set_icon(self.navigation_toggle, "menu", palette["icon"], 22)
        self.navigation_toggle.setIconSize(self.QtCore.QSize(22, 22))
        label = self.text.get("close", "Close")
        from core.gui.shell.navigation import set_back_button_presentation
        set_back_button_presentation(
            self.close_header,
            self.QtCore,
            self.QtGui,
            label=label,
            dark=self._applied_dark,
            active=False,
        )
        self.close_header.setAccessibleName(label)
        self.close_header.setToolTip(label)
        self._set_icon(self.close_header, "close", palette["icon"], 24)
        self.close_header.setIconSize(self.QtCore.QSize(24, 24))
        self.directory_list.setIconSize(self.QtCore.QSize(24, 24))
        for index in range(self.directory_list.count()):
            item = self.directory_list.item(index)
            provider = str(item.data(self.QtCore.Qt.ItemDataRole.UserRole))
            item.setIcon(self._provider_icon(provider))
        for provider, button in self._provider_buttons.items():
            button.setIcon(self._provider_icon(provider))
            button.setIconSize(self.QtCore.QSize(28, 28))
            button.setStyleSheet(
                "QPushButton { color:%(text)s; text-align:left; padding:9px 9px; "
                "border:0; border-left:3px solid transparent; background:transparent; "
                "border-radius:4px; min-height:50px; font-weight:400; }"
                "QPushButton[providerAvailable='false'] { color:%(muted)s; background:transparent; }"
                "QPushButton:hover { background:%(hover)s; }"
                "QPushButton:pressed { background:%(selected)s; color:%(blue)s; }"
                "QPushButton[providerSelected='true'] { color:%(blue)s; background:%(selected)s; "
                "border-left:3px solid %(blue)s; font-weight:600; }" % palette
            )
        if self.other_button is not None:
            self.other_button.setIcon(self._provider_icon("other"))
            self.other_button.setIconSize(self.QtCore.QSize(28, 28))
            self.other_button.setStyleSheet(
                "QPushButton { color:%(text)s; text-align:left; padding:9px 10px; "
                "border:0; background:transparent; min-height:50px; font-weight:400; }"
                "QPushButton:hover { background:%(hover)s; }"
                "QPushButton:pressed { background:%(selected)s; color:%(blue)s; }"
                "QPushButton[groupSelected='true'] { color:%(blue)s; background:%(selected)s; "
                "border-left:3px solid %(blue)s; font-weight:600; }" % palette
            )
        for badge in self._provider_badges.values():
            badge.setStyleSheet(
                "QLabel { color:%(blue)s; background:%(badge)s; border-radius:10px; "
                "padding:2px 6px; font-size:11px; font-weight:600; }" % palette
            )
        self._refresh_provider_availability()
        self._refresh_sidebar_sizing()

    def _refresh_sidebar_sizing(self):
        navigation = getattr(self, "sidebar_navigation", None)
        if navigation is None:
            return
        # Theme and font changes can alter Qt size hints after controller setup.
        required_width = self._provider_navigation_required_width()
        required_content_width = self._provider_content_required_width()
        last_required_width = getattr(
            self, "_sidebar_last_required_width", self._sidebar_preferred_width,
        )
        current_docked_width = int(
            getattr(navigation, "_docked_width", self._sidebar_preferred_width)
        )
        sizes = self.provider_splitter.sizes()
        current_width = sizes[0] if sizes and not navigation.is_overlay else 0
        auto_sized = current_docked_width == last_required_width and (
            navigation.is_overlay
            or current_width in (0, current_docked_width, last_required_width)
        )
        if auto_sized:
            docked_width = required_width
        else:
            docked_width = max(required_width, current_docked_width, current_width)
        navigation._docked_width = docked_width
        self._sidebar_last_required_width = required_width
        if not navigation.is_overlay and current_width != docked_width:
            self.provider_splitter.setSizes([
                docked_width, required_content_width,
            ])
        navigation.sync()

    def _provider_icon(self, provider: str):
        from .atlas_icons import icon
        provider = canonical_provider(provider)
        key = "more" if provider == "other" else provider
        palette = self._palette()
        return icon(
            self.QtCore, self.QtGui, key, color=palette["icon"], size=24,
            background=palette["background"],
        )

    def _provider_available_for_role(self, provider: str, role: str | None = None) -> bool:
        role = str(role or getattr(self.window, "_llm_picker_role", "jury1"))
        provider = canonical_provider(provider)
        if sys.platform == "darwin" and provider in _MACOS_UNSUPPORTED_PROVIDERS:
            return False
        return not (
            provider in _JURY2_ONLY_PROVIDERS
            and role == "jury1"
        )

    def _provider_role_tooltip(self, provider: str, role: str | None = None) -> str:
        if self._provider_available_for_role(provider, role):
            return ""
        if (
            sys.platform == "darwin"
            and canonical_provider(provider) in _MACOS_UNSUPPORTED_PROVIDERS
        ):
            return self.text.get(
                "jury_provider_macos_unsupported", "Not yet supported on macOS",
            )
        return self.text.get(
            "jury_provider_jury2_only", "Available only for Jury 2",
        )

    def _refresh_provider_availability(self, role: str | None = None):
        role = str(role or getattr(self.window, "_llm_picker_role", "jury1"))
        palette = self._palette()
        enabled = self.QtCore.Qt.ItemFlag.ItemIsEnabled
        selectable = self.QtCore.Qt.ItemFlag.ItemIsSelectable
        for index in range(self.directory_list.count()):
            item = self.directory_list.item(index)
            provider = canonical_provider(
                item.data(self.QtCore.Qt.ItemDataRole.UserRole)
            )
            available = self._provider_available_for_role(provider, role)
            flags = item.flags()
            if available:
                flags |= enabled | selectable
            else:
                flags &= ~(enabled | selectable)
                item.setSelected(False)
            item.setFlags(flags)
            item.setToolTip(self._provider_role_tooltip(provider, role))
            item.setForeground(self.QtGui.QBrush(self.QtGui.QColor(
                palette["text"] if available else palette["muted"]
            )))
        for provider, button in getattr(self, "_provider_buttons", {}).items():
            available = self._provider_available_for_role(provider, role)
            button.setEnabled(available)
            button.setAttribute(
                self.QtCore.Qt.WidgetAttribute.WA_AlwaysShowToolTips,
                not available,
            )
            button.setToolTip(self._provider_role_tooltip(provider, role))
            button.setProperty("providerAvailable", available)
            if not available:
                button.setChecked(False)
                button.setProperty("providerSelected", False)
            button.style().unpolish(button)
            button.style().polish(button)

    def _rebuild_navigation(self):
        while self.nav_layout.count():
            item = self.nav_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        title = self.QtWidgets.QLabel(self.text.get("jury_providers", "Providers"), self.nav)
        title.setObjectName("sectionTitle")
        self.nav_layout.addWidget(title)
        self._provider_buttons = {}
        self._provider_badges = {}
        for provider, label in _QUICK:
            self._add_nav_button(provider, label)
        other = self.QtWidgets.QPushButton(self.text.get("jury_other_providers", "Other providers"), self.nav)
        other.setObjectName("juryOtherProviders")
        other.setIcon(self._provider_icon("other"))
        other.setProperty(
            "groupSelected",
            self.directory_open or self.active_provider in {provider for provider, _label in _DIRECTORY},
        )
        other.clicked.connect(self._show_directory)
        self.other_button = other
        self.nav_layout.addWidget(other)
        for provider in self.added_providers:
            self._add_nav_button(provider, _provider_label(self.window, provider), extra=True)
        self.nav_layout.addStretch(1)
        self._refresh_badges()
        self.apply_theme()

    def _add_nav_button(self, provider: str, label: str, *, extra: bool = False):
        row = self.QtWidgets.QWidget(self.nav)
        row_layout = self.QtWidgets.QHBoxLayout(row)
        row_layout.setContentsMargins(18 if extra else 0, 0, 0, 0)
        row_layout.setSpacing(6)
        button = self.QtWidgets.QPushButton(label, row)
        button.setObjectName("juryProvider_" + provider)
        button.setCheckable(True)
        button.setProperty("providerId", provider)
        selected = provider == self.active_provider and not self.directory_open
        button.setChecked(selected)
        button.setProperty("providerSelected", selected)
        available = self._provider_available_for_role(provider)
        button.setProperty("providerAvailable", available)
        button.setEnabled(available)
        button.setToolTip(self._provider_role_tooltip(provider))
        button.setIcon(self._provider_icon(provider))
        button.setIconSize(self.QtCore.QSize(28, 28))
        button.setMinimumHeight(50)
        button.clicked.connect(lambda _checked=False, value=provider: self.show_provider(value))
        row_layout.addWidget(button, 1)
        badge = self.QtWidgets.QLabel(row)
        badge.setObjectName("juryProviderBadge_" + provider)
        badge.setAlignment(self.QtCore.Qt.AlignmentFlag.AlignCenter)
        badge.setMinimumWidth(22)
        badge.setFixedHeight(28)
        badge.hide()
        row_layout.addWidget(badge, 0, self.QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.nav_layout.addWidget(row)
        self._provider_buttons[provider] = button
        self._provider_badges[provider] = badge

    def _show_directory(self):
        self._close_sidebar_overlay()
        self.directory_open = True
        for button in self._provider_buttons.values():
            button.setChecked(False)
            button.setProperty("providerSelected", False)
            button.style().unpolish(button)
            button.style().polish(button)
        self.other_button.setProperty("groupSelected", True)
        self.other_button.style().unpolish(self.other_button)
        self.other_button.style().polish(self.other_button)
        self.model_stack.setCurrentWidget(self.directory_page)
        self.content_layout.setStretch(self.content_layout.indexOf(self.model_stack), 1)
        self.content_layout.setStretch(self._advanced_space_index, 0)
        self.models_heading.setText(self.text.get("jury_other_providers", "Altri provider"))
        self.provider_heading.setText(self.text.get("jury_other_providers", "Altri provider"))
        self.config.hide()
        self.advanced_toggle.hide()
        self.advanced_panel.hide()
        self.actions_row.hide()
        self.config_scope.hide()
        self.window.llm_picker_check.hide()
        self.content_scroll.verticalScrollBar().setValue(0)
        self.window.llm_picker_status.setText(
            self.text.get("jury_provider_directory_help", "Choose a supported provider")
        )

    def _row_matches(self, row: Mapping[str, Any], provider: str) -> bool:
        return canonical_provider(provider) in _setting_provider_ids(row)

    def _clear_form(self, form):
        while form.count():
            item = form.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                self._clear_form(item.layout())

    def _render_provider_settings(self, provider: str):
        for name, editor in getattr(self, "_chooser_editors", {}).items():
            for registry in (self.window._settings_editors, self.window._secret_editors):
                if registry.get(name) is editor:
                    registry.pop(name, None)
        self._clear_form(self.config_layout)
        built_editors = {}
        rows = [
            row for row in getattr(self.window, "_settings_rows", ())
            if isinstance(row, Mapping)
            and str(row.get("group") or "").lower() == "models"
            and _provider_configuration_row(row, provider)
        ]
        card = self.QtWidgets.QFrame(self.config)
        card.setObjectName("juryProviderSettings")
        card.setProperty(
            "compactPage", bool(getattr(self, "_header_compact", False)),
        )
        card_layout = self.QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_heading = self.QtWidgets.QLabel(
            self.text.get("jury_provider_configuration", "Configurazione del provider"), card,
        )
        card_heading.setObjectName("juryProviderConfigurationHeading")
        card_layout.addWidget(card_heading)
        scope = self.QtWidgets.QLabel(
            self.text.get("jury_provider_shared", "Configurazione condivisa tra Jury 1 e Jury 2."), card,
        )
        scope.setObjectName("juryProviderSharedScope")
        scope.setWordWrap(True)
        card_layout.addWidget(scope)
        form_host = self.QtWidgets.QWidget(card)
        form = self.QtWidgets.QFormLayout(form_host)
        form.setContentsMargins(0, 6, 0, 0)
        form.setRowWrapPolicy(self.QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(self.QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        card_layout.addWidget(form_host)
        if rows:
            previous_editors = dict(getattr(self.window, "_settings_editors", {}))
            previous_secrets = dict(getattr(self.window, "_secret_editors", {}))
            previous_model_editors = {
                key: list(value) for key, value in getattr(self.window, "_provider_model_editors", {}).items()
            }
            built_editors = {}
            built_secrets = {}
            built_model_editors = {}
            try:
                for row in rows:
                    name = str(row.get("name") or "").upper()
                    api_label = self.text.get("jury_api_key_label")
                    if not api_label:
                        api_label = (
                            "Chiave API"
                            if str(self.text.get("setting_OPENAI_API_KEY_label", "")).lower().startswith("chiave")
                            else "API key"
                        )
                    label = "Endpoint" if name.endswith(("_BASE_URL", "_HOST")) else api_label
                    if name == "CITATION_VERIFIER_LLM_HOST_COMMAND":
                        label = self.text.get(
                            "setting_CITATION_VERIFIER_LLM_HOST_COMMAND_label", "Host integration command"
                        )
                    if name == "CITATION_VERIFIER_MODEL":
                        label = self.text.get(
                            "setting_CITATION_VERIFIER_MODEL_label", "Fallback model IDs"
                        )
                    if provider == "freetoken" and name == "FREETOKEN_API_KEY":
                        label = self.text.get("jury_freetoken_api_key_label", "API key (optional)")
                    self.window._add_setting_editor(form, form_host, {**row, "label": label})
                    label_item = form.itemAt(form.rowCount() - 1, self.QtWidgets.QFormLayout.ItemRole.LabelRole)
                    if label_item and label_item.widget():
                        label_item.widget().setText(label)
                    field_item = form.itemAt(form.rowCount() - 1, self.QtWidgets.QFormLayout.ItemRole.FieldRole)
                    field = field_item.widget() if field_item else None
                    if field is not None:
                        note = field.findChild(self.QtWidgets.QLabel, "settingNote")
                        help_icon = field.findChild(self.QtWidgets.QLabel, "settingHelpIcon")
                        if help_icon is not None:
                            help_icon.hide()
                        if note is not None:
                            if provider == "freetoken" and name == "FREETOKEN_API_KEY":
                                note.setText(self.text.get(
                                    "jury_freetoken_api_key_hint",
                                    "Leave blank if your FreeToken server does not require authentication.",
                                ))
                                note.show()
                            elif label == "Endpoint":
                                note.setText(self.text.get("jury_endpoint_hint", "Lascia vuoto per il servizio standard."))
                                note.show()
                            elif name == "CITATION_VERIFIER_LLM_HOST_COMMAND":
                                note.setText(self.text.get(
                                    "setting_CITATION_VERIFIER_LLM_HOST_COMMAND_help",
                                    "The command receives JSON on standard input and returns the response on standard output. Keep credentials outside the command.",
                                ))
                                note.show()
                            elif name == "CITATION_VERIFIER_MODEL":
                                note.setText(self.text.get(
                                    "setting_CITATION_VERIFIER_MODEL_help",
                                    "Enter comma-separated model IDs; provider-specific choices take precedence.",
                                ))
                                note.show()
                            else:
                                note.hide()
                    if label == "Endpoint" and field is not None:
                        editor = field.findChild(self.QtWidgets.QLineEdit)
                        if editor is None:
                            editor = field.findChild(self.QtWidgets.QComboBox)
                        if editor is not None:
                            editor.setPlaceholderText(self._endpoint_placeholder(name))
                built_editors = {
                    key: value for key, value in self.window._settings_editors.items()
                    if key not in previous_editors or value is not previous_editors[key]
                }
                built_secrets = {
                    key: value for key, value in self.window._secret_editors.items()
                    if key not in previous_secrets or value is not previous_secrets[key]
                }
                built_model_editors = {
                    key: [editor for editor in values if editor not in previous_model_editors.get(key, ())]
                    for key, values in self.window._provider_model_editors.items()
                }
            finally:
                self.window._settings_editors = {**previous_editors, **built_editors}
                self.window._secret_editors = {**previous_secrets, **built_secrets}
                chooser_models = getattr(self, "_chooser_model_editors", {})
                for key, editors in chooser_models.items():
                    previous_model_editors[key] = [
                        editor for editor in previous_model_editors.get(key, ())
                        if editor not in editors
                    ]
                for key, editors in built_model_editors.items():
                    previous_model_editors.setdefault(key, []).extend(editors)
                self.window._provider_model_editors = previous_model_editors
                self._chooser_model_editors = built_model_editors
        else:
            note = self.QtWidgets.QLabel(
                self._provider_configuration_hint(provider),
                card,
            )
            note.setWordWrap(True)
            card_layout.addWidget(note)
        fixed_endpoints = {
            "deepseek": "https://api.deepseek.com",
            "groq": "https://api.groq.com/openai/v1",
            "xai": "https://api.x.ai/v1",
            "mistral": "https://api.mistral.ai/v1",
        }
        if provider in fixed_endpoints:
            endpoint = self.QtWidgets.QLineEdit(fixed_endpoints[provider], form_host)
            endpoint.setReadOnly(True)
            endpoint.setObjectName("juryProviderFixedEndpoint")
            form.addRow(self.text.get("jury_fixed_service_label", "Servizio"), endpoint)
            service_note = self.QtWidgets.QLabel(
                self.text.get("jury_fixed_service_hint", "Endpoint predefinito di Mistral."), form_host,
            )
            service_note.setObjectName("juryFixedServiceHint")
            form.addRow("", service_note)
        self._chooser_editors = built_editors
        cli_without_settings = provider in {
            "codex_cli", "claude_cli", "gemini_cli", "antigravity", "opencode_cli",
        } and not built_editors
        self.save_button.setVisible(not cli_without_settings)
        self.config_layout.addWidget(card)
        self.save_button.setEnabled(bool(getattr(self.window, "_settings_dirty", {})))
        for editor in built_editors.values():
            for signal_name in (
                "textEdited", "currentTextChanged", "valueChanged", "currentIndexChanged",
            ):
                signal = getattr(editor, signal_name, None)
                if signal is not None:
                    signal.connect(self._sync_save_state)

    @staticmethod
    def _endpoint_placeholder(name: str) -> str:
        return {
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com/v1",
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "FREETOKEN_HOST": "http://127.0.0.1:1919",
        }.get(name, "https://")

    def _provider_configuration_hint(self, provider: str) -> str:
        cli_providers = {"codex_cli", "claude_cli", "gemini_cli", "antigravity", "opencode_cli"}
        if provider == "host":
            return self.text.get(
                "jury_host_integration_hint",
                "Host integration uses a command configured outside Callimachus; it is not an API service.",
            )
        if provider in cli_providers:
            return self.text.get(
                "jury_cli_configuration_hint",
                "Usa la CLI installata e l’account già configurato sul dispositivo.",
            )
        return self.text.get(
            "jury_provider_no_settings",
            "Questo provider non richiede ulteriori campi di configurazione.",
        )

    def _save_settings(self):
        saved = self.window._save_settings()
        self.refresh_settings()
        self.refresh(self.window._llm_picker_role)
        return saved

    def _build_advanced(self):
        for name, editor in getattr(self, "_advanced_editors", {}).items():
            if self.window._settings_editors.get(name) is editor:
                self.window._settings_editors.pop(name, None)
        self._clear_form(self.advanced_form)
        rows = [
            row for row in getattr(self.window, "_settings_rows", ())
            if isinstance(row, Mapping) and _advanced_row(row)
        ]
        if not rows:
            self.advanced_toggle.setEnabled(False)
            return
        previous_editors = dict(getattr(self.window, "_settings_editors", {}))
        previous_secrets = dict(getattr(self.window, "_secret_editors", {}))
        previous_model_editors = {
            key: list(value) for key, value in getattr(self.window, "_provider_model_editors", {}).items()
        }
        try:
            for row in rows:
                previous_count = self.advanced_form.rowCount()
                self.window._add_setting_editor(self.advanced_form, self.advanced_form_host, row)
                if self.advanced_form.rowCount() > previous_count:
                    item = self.advanced_form.itemAt(previous_count, self.QtWidgets.QFormLayout.ItemRole.FieldRole)
                    field = item.widget() if item else None
                    note = field.findChild(self.QtWidgets.QLabel, "settingNote") if field else None
                    summary = self.text.get("setting_" + str(row.get("name") or "") + "_summary")
                    if note is not None and summary:
                        note.setText(summary)
            advanced_editors = {
                key: value for key, value in self.window._settings_editors.items()
                if key not in previous_editors or value is not previous_editors[key]
            }
        finally:
            self.window._settings_editors = {**previous_editors, **advanced_editors}
            self.window._secret_editors = previous_secrets
            self.window._provider_model_editors = previous_model_editors
        self._advanced_editors = advanced_editors
        for editor in advanced_editors.values():
            for signal_name in (
                "textEdited", "currentTextChanged", "valueChanged", "currentIndexChanged",
            ):
                signal = getattr(editor, signal_name, None)
                if signal is not None:
                    signal.connect(self._sync_save_state)
        self.advanced_toggle.setEnabled(True)

    def _sync_save_state(self, *_args):
        if hasattr(self, "save_button"):
            self.save_button.setEnabled(bool(getattr(self.window, "_settings_dirty", {})))

    def refresh_settings(self):
        """Rebuild the chooser's staged forms after settings metadata loads."""
        self._render_provider_settings(self.active_provider)
        self._build_advanced()
        self.apply_theme()

    def _toggle_advanced(self, checked: bool):
        self.advanced_panel.setVisible(checked)
        self.advanced_toggle.setArrowType(
            self.QtCore.Qt.ArrowType.DownArrow if checked else self.QtCore.Qt.ArrowType.RightArrow
        )

    def show_provider(self, provider: str):
        provider = canonical_provider(provider)
        if not self._provider_available_for_role(provider):
            return
        self.directory_open = False
        if provider not in {item[0] for item in _QUICK}:
            if provider not in {item[0] for item in _DIRECTORY}:
                return
            self.directory_open = False
            if provider not in self.added_providers:
                self.added_providers.append(provider)
                self._rebuild_navigation()
        self._close_sidebar_overlay()
        self.active_provider = provider
        self.other_button.setProperty(
            "groupSelected", provider in {item[0] for item in _DIRECTORY}
        )
        self.content_scroll.verticalScrollBar().setValue(0)
        self.model_stack.setCurrentWidget(self.models_page)
        self.content_layout.setStretch(self.content_layout.indexOf(self.model_stack), 0)
        self.content_layout.setStretch(self._advanced_space_index, 1)
        self.config.show()
        self.config.layout().activate()
        self.advanced_toggle.show()
        self.advanced_panel.setVisible(self.advanced_toggle.isChecked())
        self.actions_row.show()
        self.config_scope.show()
        self.window.llm_picker_check.show()
        self._render_provider_settings(provider)
        for key, button in self._provider_buttons.items():
            button.setChecked(key == provider)
            button.setProperty("providerSelected", key == provider)
            button.style().unpolish(button)
            button.style().polish(button)
        self.other_button.style().unpolish(self.other_button)
        self.other_button.style().polish(self.other_button)
        self._set_title()
        self.refresh(self.window._llm_picker_role)
        self.apply_theme()

    def current_provider(self) -> str:
        if self.directory_open:
            return ""
        return self.active_provider

    def _refresh_empty_catalog_hint(self) -> bool:
        tree = self.window.llm_choices
        child_count = 0
        for index in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(index)
            if canonical_provider(group.data(0, self.QtCore.Qt.ItemDataRole.UserRole)) == self.active_provider:
                child_count = group.childCount()
        empty = self.active_provider == "freetoken" and not self.directory_open and child_count == 0
        state = next((
            value for provider_id, value in getattr(self.window, "_provider_check_state", {}).items()
            if canonical_provider(provider_id) == self.active_provider
        ), {})
        suffix = {
            "checking": "checking", "error": "error", "stale": "stale", "success": "empty",
        }.get(state.get("status"), "idle")
        self.empty_catalog_hint.setText(self.text.get(
            "jury_freetoken_catalog_" + suffix,
            "Press Check to load the models available from FreeToken.",
        ))
        self.empty_catalog_hint.setVisible(empty)
        return empty

    def refresh(self, role: str | None = None):
        if getattr(self, "_applied_dark", None) != bool(getattr(self.window, "_dark", False)):
            self.apply_theme()
        tree = self.window.llm_choices
        if role in {"jury1", "jury2"}:
            self.window._llm_picker_role = role
        active_role = str(getattr(self.window, "_llm_picker_role", "jury1"))
        self._refresh_provider_availability(active_role)
        if (
            not self.directory_open
            and not self._provider_available_for_role(self.active_provider, active_role)
        ):
            self.show_provider("codex_cli")
            return
        selected_column = 1 if active_role == "jury1" else 2
        tree.setColumnHidden(1, selected_column != 1)
        tree.setColumnHidden(2, selected_column != 2)
        header = tree.header()
        visual_column = header.visualIndex(selected_column)
        if visual_column != 0:
            header.moveSection(visual_column, 0)
        if not self._provider_columns_initialized:
            header.setSectionResizeMode(0, self.QtWidgets.QHeaderView.ResizeMode.Stretch)
            for role_column in (1, 2):
                header.setSectionResizeMode(role_column, self.QtWidgets.QHeaderView.ResizeMode.Fixed)
                tree.setColumnWidth(role_column, 52)
            self._provider_columns_initialized = True
        selected = self.active_provider
        visible_group = None
        for index in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(index)
            provider = canonical_provider(group.data(0, self.QtCore.Qt.ItemDataRole.UserRole))
            group.setHidden(provider != selected or self.directory_open)
            if provider == selected and not self.directory_open:
                visible_group = group
                tree.setRootIndex(tree.indexFromItem(group))
        if visible_group is None:
            tree.setRootIndex(self.QtCore.QModelIndex())
        child_count = visible_group.childCount() if visible_group is not None else 0
        empty_catalog = self._refresh_empty_catalog_hint()
        tree_height = max(110 if empty_catalog else 72, min(220, 34 + child_count * 44))
        tree.setMinimumHeight(tree_height)
        tree.setMaximumHeight(tree_height)
        if not self.directory_open:
            self.model_stack.setCurrentWidget(self.models_page)
        extra_providers = {provider for provider, _ in _DIRECTORY}
        for index in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(index)
            provider = canonical_provider(group.data(0, self.QtCore.Qt.ItemDataRole.UserRole))
            if provider in extra_providers and provider not in self.added_providers and self._selected_count(provider):
                self.added_providers.append(provider)
                self._rebuild_navigation()
        previous = tuple(self.added_providers)
        self.added_providers = [
            provider for provider in self.added_providers
            if provider == self.active_provider or self._selected_count(provider) > 0
        ]
        if tuple(self.added_providers) != previous:
            self._rebuild_navigation()
        self._refresh_badges()
        if hasattr(self.window, "_refresh_jury_picker_check"):
            self.window._refresh_jury_picker_check()
        self._update_summary()

    def _selected_count(self, provider: str) -> int:
        role = str(getattr(self.window, "_llm_picker_role", "jury1"))
        selected = getattr(self.window, "_selected_llm_roles", lambda: {})().get(role, ())
        expected = canonical_provider(provider)
        return sum(
            canonical_provider(str(selector).partition(":")[0]) == expected
            for selector in selected
        )

    def _refresh_badges(self):
        for provider, button in getattr(self, "_provider_buttons", {}).items():
            count = self._selected_count(provider)
            badge = self._provider_badges.get(provider)
            if badge is not None:
                badge.setText(str(count))
                badge.setVisible(count > 0)

    def _update_summary(self):
        role = str(getattr(self.window, "_llm_picker_role", "jury1"))
        selected = getattr(self.window, "_selected_llm_roles", lambda: {})().get(role, ())
        service_ids = [provider for provider, _ in (*_QUICK, *_DIRECTORY)]
        provider_count = sum(1 for provider in service_ids if self._selected_count(provider))
        role_label = self.text.get(role + "_role", role)
        summary = self.text.get(
            "jury_selection_summary",
            "{role}: {models} models selected across {providers} providers",
        )
        self.selection_summary.setText(summary.format(
            role=role_label, models=len(selected), providers=provider_count,
        ))
        self._set_title()
        self.save_button.setEnabled(bool(getattr(self.window, "_settings_dirty", {})))


def install_jury_chooser(window, QtCore, QtGui, QtWidgets, text) -> JuryChooser:
    """Install the provider workspace and return its refresh/navigation adapter."""
    chooser = JuryChooser(window, QtCore, QtGui, QtWidgets, text)
    window._atlante_jury_chooser = chooser
    return chooser
