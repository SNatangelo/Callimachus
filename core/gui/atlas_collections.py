# core/gui/atlas_collections.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Presentation helpers for the existing Library and History pages."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.gui.shell.resize import install_table_resize


def install(window, QtCore, QtGui, QtWidgets, text) -> None:
    """Restyle the existing collection widgets without replacing their behavior."""
    if getattr(window, "_atlas_collections_installed", False):
        refresh_rows(window, QtCore, QtGui, QtWidgets, text)
        return

    _install_library(window, QtCore, QtGui, QtWidgets, text)
    _install_history(window, QtCore, QtGui, QtWidgets, text)
    window._atlas_collections_installed = True
    refresh_rows(window, QtCore, QtGui, QtWidgets, text)


def refresh_rows(window, QtCore, QtGui, QtWidgets, text) -> None:
    """Refresh presentation derived from the rows already held by each table."""
    if not getattr(window, "_atlas_collections_installed", False):
        return
    _synchronize_library_filter_row_height(window)
    _set_icons(window, QtCore, QtGui)
    _refresh_history(window, text, QtCore, QtGui)
    _refresh_library(window, QtCore, QtWidgets, text)


def _synchronize_library_filter_row_height(window) -> None:
    """Keep the search and filter controls aligned to their live font metrics."""
    controls = (window.cache_search, window.cache_filter)
    original_minimums = []
    for control in controls:
        original_minimum = control.property("_atlas_row_original_minimum_height")
        if original_minimum is None:
            original_minimum = control.minimumHeight()
            control.setProperty("_atlas_row_original_minimum_height", original_minimum)
        original_minimums.append(int(original_minimum))
        # Remove only the minimum height this helper may have applied before
        # measuring again; this lets a smaller font produce a shorter row.
        control.setMinimumHeight(int(original_minimum))
        control.ensurePolished()
        control.updateGeometry()

    shared_height = max(
        *(control.sizeHint().height() for control in controls),
        *(control.minimumSizeHint().height() for control in controls),
        *original_minimums,
    )
    for control in controls:
        control.setMinimumHeight(shared_height)


def set_pending_count(window, count: int | None) -> None:
    """Show a known pending-review count supplied by the existing app layer."""
    try:
        value = int(count) if count is not None else None
    except (TypeError, ValueError, OverflowError):
        value = None
    normalized = value if value is not None and value >= 0 else None
    window._atlas_library_pending_count = normalized
    badge = getattr(window, "_atlas_library_pending_badge", None)
    if badge is None:
        return
    visible = normalized is not None and normalized > 0
    badge.setText(str(normalized) if visible else "")
    badge.setVisible(visible)


def _localized(text, key: str, language: str, italian: str, english: str) -> str:
    value = text.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return italian if language == "it" else english


def _fit_button_label(button, available_width: int) -> None:
    """Break an oversized action label across lines without losing its name."""
    original = button.property("_atlasOriginalText")
    if original is None or (
        button.text() != original and "\n" not in button.text()
    ):
        original = button.text()
        button.setProperty("_atlasOriginalText", original)
        button.setProperty("_atlasOriginalAccessibleName", button.accessibleName())
        button.setProperty("_atlasOriginalToolTip", button.toolTip())
    if not original:
        return

    original_accessible_name = button.property("_atlasOriginalAccessibleName") or ""
    original_tool_tip = button.property("_atlasOriginalToolTip") or ""
    button.setText(original)
    button.setAccessibleName(original_accessible_name)
    button.setToolTip(original_tool_tip)
    metrics = button.fontMetrics()
    text_width = metrics.horizontalAdvance(original)
    decoration_width = max(0, button.sizeHint().width() - text_width)
    available_text_width = max(1, available_width - decoration_width)
    if button.sizeHint().width() <= available_width:
        return

    words = original.split()
    if len(words) < 2:
        return
    lines = []
    line = words[0]
    for word in words[1:]:
        candidate = f"{line} {word}"
        if metrics.horizontalAdvance(candidate) <= available_text_width:
            line = candidate
        else:
            lines.append(line)
            line = word
    lines.append(line)
    wrapped = "\n".join(lines)
    if wrapped != original:
        button.setText(wrapped)
        button.setAccessibleName(original_accessible_name or original)


def _clear_layout(layout, QtCore) -> None:
    while layout.count():
        item = layout.takeAt(layout.count() - 1)
        widget = item.widget()
        if widget is not None:
            continue
        nested = item.layout()
        if nested is not None:
            _clear_layout(nested, QtCore)
            nested.deleteLater()


def _install_library(window, QtCore, QtGui, QtWidgets, text) -> None:
    tab = window.cache_stack.parentWidget()
    layout = tab.layout()
    _clear_layout(layout, QtCore)
    layout.setContentsMargins(30, 26, 30, 0)
    layout.setSpacing(12)

    language = getattr(window, "_language", "en")
    heading = tab.findChild(QtWidgets.QLabel, "pageTitle")
    if heading is None:
        raise RuntimeError("Library page title is missing")
    subtitle = QtWidgets.QLabel(
        _localized(text, "atlas_library_subtitle", language,
                   "Fonti e testi salvati", "Saved sources and texts"), tab
    )
    subtitle.setObjectName("pageSubtitle")
    window._atlas_library_subtitle = subtitle

    layout.addWidget(heading)
    layout.addWidget(subtitle)
    window.cache_summary.setObjectName("mutedText")
    window.cache_summary.hide()
    layout.addWidget(window.cache_summary)

    controls_content = QtWidgets.QWidget(tab)
    controls_content.setObjectName("libraryControlsContent")
    controls_content.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Expanding,
        QtWidgets.QSizePolicy.Policy.Preferred,
    )
    controls_layout = QtWidgets.QVBoxLayout(controls_content)
    controls_layout.setContentsMargins(0, 0, 0, 0)
    controls_layout.setSpacing(12)

    actions = QtWidgets.QHBoxLayout()
    actions.setSpacing(10)
    window.library_import_button.setObjectName("libraryImport")
    window.library_pending_button.setObjectName("libraryPendingButton")
    short_review = _localized(
        text, "atlas_reviews_short", language,
        "Revisioni in sospeso", "Pending reviews",
    )
    window.library_pending_button.setText(short_review)
    window.library_pending_button.setToolTip(text.get(
        "library_pending_review", short_review
    ))

    pending_group = QtWidgets.QWidget(tab)
    pending_layout = QtWidgets.QHBoxLayout(pending_group)
    pending_layout.setContentsMargins(0, 0, 0, 0)
    pending_layout.setSpacing(7)
    pending_layout.addWidget(window.library_pending_button)
    badge = QtWidgets.QLabel(window.library_pending_button)
    badge.setObjectName("libraryPendingCount")
    badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    badge.setMinimumWidth(25)
    badge.setFixedHeight(25)
    badge.setVisible(False)
    badge.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    badge_layout = QtWidgets.QHBoxLayout(window.library_pending_button)
    badge_layout.setContentsMargins(0, 0, 12, 0)
    badge_layout.addStretch()
    badge_layout.addWidget(badge)
    window._atlas_library_pending_badge = badge
    window._atlas_library_pending_group = pending_group

    actions.addWidget(window.library_import_button)
    actions.addWidget(pending_group)

    archive_buttons = (window.library_archive_import_button, window.library_archive_export_button)
    archive_actions = QtWidgets.QHBoxLayout()
    archive_actions.setSpacing(10)
    for button in archive_buttons:
        archive_actions.addWidget(button)
    action_groups = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.LeftToRight)
    action_groups.addLayout(actions)
    action_groups.addLayout(archive_actions)
    action_groups.addStretch(1)
    controls_layout.addLayout(action_groups)

    controls_layout.addWidget(window.library_progress)
    controls_layout.addWidget(window.library_progress_bar)

    filters = QtWidgets.QHBoxLayout()
    filters.setSpacing(10)
    window.cache_search.setObjectName("librarySearch")
    window.cache_search.setPlaceholderText(_localized(
        text, "atlas_library_search", language, "Cerca nella libreria…", "Search the library…"
    ))
    window.cache_search.setClearButtonEnabled(True)
    window.cache_search.setMaximumWidth(645)
    search_action = window.cache_search.addAction(
        _atlas_icon(QtCore, QtGui, "search", window),
        QtWidgets.QLineEdit.ActionPosition.LeadingPosition,
    )
    search_action.setToolTip(text.get("search_sources", "Search sources"))
    window._atlas_library_search_action = search_action
    filters.addWidget(window.cache_search, 3)
    window.cache_filter.setObjectName("libraryFilter")
    window.cache_filter.setMinimumWidth(240)
    window.cache_filter.setMaximumWidth(275)
    filter_preferred_minimum_width = window.cache_filter.minimumWidth()
    window.cache_filter.setItemDelegate(QtWidgets.QStyledItemDelegate(window.cache_filter))

    help_button = QtWidgets.QToolButton(tab)
    help_button.setObjectName("libraryHelp")
    help_button.setAutoRaise(True)
    help_button.setIcon(_atlas_icon(QtCore, QtGui, "help", window))
    help_button.setToolTip(_localized(
        text, "atlas_library_help", language,
        "Importa paper locali, filtra i testi disponibili e rivedi le identità in sospeso.",
        "Import local papers, filter available text, and review pending identities.",
    ))
    help_button.setAccessibleName(text.get("info_title", "Help"))
    help_button.clicked.connect(lambda _checked=False: _open_help(window, text))
    window._atlas_library_help_button = help_button
    filter_help = QtWidgets.QHBoxLayout()
    filter_help.setSpacing(10)
    filter_help.addWidget(window.cache_filter, 1)
    filter_help.addWidget(help_button)
    filters.addLayout(filter_help, 1)
    filters.addStretch(1)
    controls_layout.addLayout(filters)

    controls_scroll = QtWidgets.QScrollArea(tab)
    controls_scroll.setObjectName("libraryControlsScroll")
    controls_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    controls_scroll.setWidgetResizable(True)
    controls_scroll.setHorizontalScrollBarPolicy(
        QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    controls_scroll.setVerticalScrollBarPolicy(
        QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    controls_scroll.setWidget(controls_content)
    layout.addWidget(controls_scroll)

    panel = QtWidgets.QFrame(tab)
    panel.setObjectName("collectionTablePanel")
    panel_layout = QtWidgets.QVBoxLayout(panel)
    panel_layout.setContentsMargins(0, 0, 0, 0)
    panel_layout.setSpacing(0)

    window.cache_table.setObjectName("libraryTable")
    _style_table(window.cache_table, QtWidgets, row_height=80)
    header = window.cache_table.horizontalHeader()
    header.setDefaultAlignment(
        QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
    )
    header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)
    window.cache_table.setColumnWidth(0, 56)
    header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed)
    window.cache_table.setColumnWidth(2, 180)
    header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Fixed)
    window.cache_table.setColumnWidth(3, 190)
    install_table_resize(
        window.cache_table,
        QtCore,
        QtWidgets,
        stretch_last_section=True,
        preferred_minimum_widths={0: 56, 1: 180},
    )
    window.cache_table.horizontalHeaderItem(2).setText(_localized(
        text, "atlas_library_availability", language, "Testo disponibile", "Available text"
    ))
    window.cache_table.setItemDelegateForColumn(
        1, _title_delegate(QtCore, QtGui, QtWidgets, window)
    )
    window.cache_table.setItemDelegateForColumn(
        2, _availability_delegate(QtCore, QtGui, QtWidgets, text, window)
    )
    _hide_library_last_separator(window.cache_table, QtCore, QtWidgets)
    panel_layout.addWidget(window.cache_stack, 1)
    window.cache_stack.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Expanding,
        QtWidgets.QSizePolicy.Policy.Expanding,
    )
    window.cache_more_button.setObjectName("loadMoreStrip")
    window.cache_more_button.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Expanding,
        QtWidgets.QSizePolicy.Policy.Fixed,
    )
    window.cache_more_button.setFixedHeight(36)
    count = QtWidgets.QLabel(window.cache_more_button)
    count.setObjectName("loadMoreCount")
    count.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    overlay = QtWidgets.QHBoxLayout(window.cache_more_button)
    overlay.setContentsMargins(12,0,12,0)
    overlay.addWidget(count)
    overlay.addStretch()
    window._atlas_library_loaded_count = count
    panel_layout.addWidget(window.cache_more_button)

    footer = QtWidgets.QWidget(panel)
    footer.setObjectName("collectionFooter")
    footer_layout = QtWidgets.QHBoxLayout(footer)
    footer_layout.setContentsMargins(12, 7, 12, 7)
    footer_layout.setSpacing(10)
    footer_content = QtWidgets.QWidget(footer)
    footer_content.setObjectName("libraryFooterActions")
    footer_content.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred
    )
    footer_content_layout = QtWidgets.QVBoxLayout(footer_content)
    footer_content_layout.setContentsMargins(0, 0, 0, 0)
    footer_content_layout.setSpacing(10)
    window.cache_select_all.setObjectName("librarySelectAll")
    window.cache_delete_button.setObjectName("libraryDeleteSelected")
    clear_button = next((button for button in tab.findChildren(QtWidgets.QPushButton)
                         if button.text() == text["clear_cache"]), None)
    if clear_button is None:
        raise RuntimeError("Library clear-cache action is missing")
    clear_button.setObjectName("clearCacheButton")
    clear_button.setText(_localized(text, "atlas_clear_cache", language, "Svuota cache", "Clear cache"))
    window.cache_clear_button = clear_button
    footer_action_buttons = (
        window.cache_select_all,
        window.cache_delete_button,
        window.cache_clear_button,
    )
    footer_action_rows = []
    for row_index in range(len(footer_action_buttons)):
        row_widget = QtWidgets.QWidget(footer_content)
        row_widget.setObjectName(f"libraryFooterActionsRow{row_index + 1}")
        row_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        row_layout = QtWidgets.QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(10)
        footer_content_layout.addWidget(row_widget)
        footer_action_rows.append((row_widget, row_layout))
    for button in footer_action_buttons:
        footer_action_rows[0][1].addWidget(button)
    footer_action_rows[0][0].show()
    for row_widget, _row_layout in footer_action_rows[1:]:
        row_widget.hide()
    footer_content.adjustSize()
    footer_scroll = QtWidgets.QScrollArea(footer)
    footer_scroll.setObjectName("libraryFooterActionsScroll")
    footer_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    footer_scroll.setWidgetResizable(True)
    footer_scroll.setHorizontalScrollBarPolicy(
        QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    footer_scroll.setVerticalScrollBarPolicy(
        QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    footer_scroll.setWidget(footer_content)
    footer_scroll.setFixedHeight(58)
    footer_layout.addWidget(footer_scroll)
    panel_layout.addWidget(footer)
    footer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum)
    layout.addWidget(panel, 1)

    window._atlas_library_panel = panel
    window._atlas_library_controls_scroll = controls_scroll
    window._atlas_library_controls_content = controls_content
    window._atlas_library_footer = footer
    window._atlas_library_footer_scroll = footer_scroll
    window._atlas_library_footer_content = footer_content
    window._atlas_library_help_text = help_button.toolTip()
    _install_library_empty_state(window, QtCore, QtWidgets)

    def reflow_library_controls(compact):
        short_window = compact and tab.height() < 480
        button_width = max(
            1,
            tab.width() - (24 if compact else 84) - (12 if short_window else 0),
        )
        for button in (
            window.library_import_button, window.library_pending_button,
            *archive_buttons,
            *footer_action_buttons,
        ):
            if isinstance(button, QtWidgets.QPushButton):
                _fit_button_label(button, button_width)
        action_spacing = 10
        actions_required_width = (
            window.library_import_button.sizeHint().width()
            + pending_group.sizeHint().width() + action_spacing
        )
        actions.setDirection(
            QtWidgets.QBoxLayout.Direction.LeftToRight
            if actions_required_width <= button_width
            else QtWidgets.QBoxLayout.Direction.TopToBottom
        )
        actions.setSpacing(action_spacing)
        archive_actions.setSpacing(action_spacing)
        archive_actions.setDirection(
            QtWidgets.QBoxLayout.Direction.LeftToRight
            if sum(button.sizeHint().width() for button in archive_buttons) + archive_actions.spacing() <= button_width
            else QtWidgets.QBoxLayout.Direction.TopToBottom
        )
        all_actions_width = (
            actions_required_width
            + sum(button.sizeHint().width() for button in archive_buttons)
            + archive_actions.spacing() + 14
        )
        action_groups.setDirection(
            QtWidgets.QBoxLayout.Direction.LeftToRight
            if all_actions_width <= button_width
            else QtWidgets.QBoxLayout.Direction.TopToBottom
        )
        action_groups.setSpacing(14)
        filters.setSpacing(4 if compact else 10)
        filter_help.setSpacing(4 if compact else 10)
        window.cache_filter.setMaximumWidth(
            16777215 if compact else 275
        )
        page_margins = layout.contentsMargins()
        filters_available_width = max(
            1,
            tab.width()
            - page_margins.left() - page_margins.right()
            - controls_scroll.frameWidth() * 2,
        )
        filter_minimum_width = max(
            filter_preferred_minimum_width,
            window.cache_filter.sizeHint().width(),
        )
        filters_required_width = (
            window.cache_search.sizeHint().width()
            + filter_minimum_width
            + help_button.sizeHint().width()
            + filters.spacing()
            + filter_help.spacing()
        )
        filters_fit_inline = filters_required_width <= filters_available_width
        window.cache_filter.setMinimumWidth(
            filter_minimum_width if compact and filters_fit_inline
            else 0 if compact else 240
        )
        filters.setDirection(
            QtWidgets.QBoxLayout.Direction.LeftToRight
            if filters_fit_inline
            else QtWidgets.QBoxLayout.Direction.TopToBottom
        )
        filters.setStretch(2, 0 if compact else 1)
        footer_content_layout.setSpacing(4 if compact else 10)
        for _row_widget, row_layout in footer_action_rows:
            row_layout.setSpacing(4 if compact else 10)
        controls_layout.activate()
        controls_content.adjustSize()
        controls_natural_height = controls_layout.sizeHint().height()
        controls_max_height = (
            max(120, tab.height() - 180) if compact else 16777215
        )
        controls_scroll.setMaximumHeight(
            controls_max_height
        )
        controls_scroll.setMinimumHeight(
            min(controls_natural_height, controls_max_height)
        )
        footer_layout.setContentsMargins(
            *((4, 2, 4, 2) if compact else (12, 7, 12, 7))
        )
        spacing = max(0, footer_action_rows[0][1].spacing())
        groups = [[]]
        group_width = 0
        for button in footer_action_buttons:
            width = button.sizeHint().width()
            if groups[-1] and group_width + spacing + width > button_width:
                groups.append([])
                group_width = 0
            group_width += (spacing if groups[-1] else 0) + width
            groups[-1].append(button)
        for row_widget, row_layout in footer_action_rows:
            for button in footer_action_buttons:
                row_layout.removeWidget(button)
            row_widget.hide()
        for row_index, group in enumerate(groups):
            row_widget, row_layout = footer_action_rows[row_index]
            for button in group:
                row_layout.addWidget(button)
            row_widget.show()
        footer_content_layout.activate()
        footer_content.adjustSize()
        content_height = footer_content_layout.sizeHint().height()
        footer_scroll.setFixedHeight(
            min(58, max(52, content_height + 4)) if short_window
            else max(52, content_height + (4 if compact else 14))
        )

    _install_compact_page_reflow(
        window, tab, subtitle, layout, QtCore,
        attr_name="_atlas_library_reflow",
        compact_insets=(8, 12, 8, 0),
        reflow_widgets=(
            window.library_import_button,
            window.library_pending_button,
            *archive_buttons,
            window.cache_select_all,
            window.cache_delete_button,
            window.cache_clear_button,
        ),
        compact_callback=lambda: reflow_library_controls(True),
        wide_callback=lambda: reflow_library_controls(False),
    )


def _install_history(window, QtCore, QtGui, QtWidgets, text) -> None:
    tab = window.history_stack.parentWidget()
    layout = tab.layout()
    _clear_layout(layout, QtCore)
    layout.setContentsMargins(30, 26, 30, 0)
    layout.setSpacing(12)

    language = getattr(window, "_language", "en")
    heading = tab.findChild(QtWidgets.QLabel, "pageTitle")
    if heading is None:
        raise RuntimeError("History page title is missing")
    heading_row = QtWidgets.QHBoxLayout()
    heading_row.addWidget(heading)
    heading_row.addStretch(1)
    loading = QtWidgets.QLabel(tab)
    loading.setObjectName("historyLoadingStatus")
    loading.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
    loading.hide()
    spinner = QtWidgets.QLabel(tab)
    spinner.setFixedSize(22,22)
    spinner.hide()
    heading_row.addWidget(spinner)
    heading_row.addWidget(loading)
    window._atlas_history_spinner = spinner
    window._atlas_history_loading = loading
    layout.addLayout(heading_row)

    subtitle = QtWidgets.QLabel(
        _localized(text, "atlas_history_subtitle", language,
                   "Le analisi eseguite in Callimachus",
                   "Analyses completed in Callimachus"), tab
    )
    subtitle.setObjectName("pageSubtitle")
    layout.addWidget(subtitle)
    window._atlas_history_subtitle = subtitle

    selection_bar = QtWidgets.QWidget(tab)
    selection_bar.setObjectName("historySelectionBar")
    selection_layout = QtWidgets.QHBoxLayout(selection_bar)
    selection_layout.setContentsMargins(0, 0, 0, 0)
    selection_layout.setSpacing(12)
    window.history_select_all.setObjectName("historySelectAll")
    window.history_delete_button.setObjectName("historyDeleteSelected")
    selection_layout.addWidget(window.history_select_all)
    selection_layout.addWidget(window.history_delete_button)
    selection_layout.addStretch(1)
    layout.addWidget(selection_bar)
    window._atlas_history_selection_bar = selection_bar

    panel = QtWidgets.QFrame(tab)
    panel.setObjectName("collectionTablePanel")
    panel_layout = QtWidgets.QVBoxLayout(panel)
    panel_layout.setContentsMargins(0, 0, 0, 0)
    panel_layout.setSpacing(0)
    window.history_table.setObjectName("historyTable")
    _style_table(window.history_table, QtWidgets, row_height=98)
    window.history_table.setWordWrap(False)
    window.history_table.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
    header = window.history_table.horizontalHeader()
    header.setDefaultAlignment(
        QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
    )
    for column, key, italian, english in (
        (1, "atlas_history_manuscript", "Manoscritto", "Manuscript"),
        (5, "atlas_history_completed", "Completata", "Completed"),
    ):
        item = window.history_table.horizontalHeaderItem(column)
        if item is not None:
            item.setText(_localized(text, key, language, italian, english))
    header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)
    window.history_table.setColumnWidth(0, 76)
    for column, width in ((2, 243), (3, 274), (4, 260), (5, 113)):
        header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Fixed)
        window.history_table.setColumnWidth(column, width)
    header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
    install_table_resize(
        window.history_table,
        QtCore,
        QtWidgets,
        stretch_last_section=True,
        preferred_minimum_widths={1: 180},
    )
    panel_layout.addWidget(window.history_stack, 1)
    panel_layout.addWidget(window.history_more_button)
    window._atlas_history_panel = panel
    _install_history_empty_state(window, QtCore, QtGui, QtWidgets, text)

    actions = window.history_actions_widget.layout()
    history_action_buttons = (
        window.history_resume_button,
        window.history_open_report_button,
        window.history_parse_review_button,
        window.history_report_button,
        window.history_verify_fork_button,
        window.history_report_folder_button,
        window.history_export_report_button,
        window.history_delete_run_button,
    )
    if actions is not None:
        actions.setSizeConstraint(
            QtWidgets.QLayout.SizeConstraint.SetFixedSize
        )
        for button in (
            window.history_select_all,
            window.history_delete_button,
            *history_action_buttons,
        ):
            actions.removeWidget(button)
        actions.setColumnStretch(2, 0)
        for column, button in enumerate((
            *history_action_buttons,
        )):
            actions.addWidget(button, 0, column)
    window.history_actions_widget.setObjectName("historyActionStripContent")
    window.history_actions_scroll.setObjectName("historyActions")
    window.history_actions_scroll.setWidgetResizable(False)
    window.history_actions_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    window.history_actions_scroll.setHorizontalScrollBarPolicy(
        QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    window.history_actions_scroll.setVerticalScrollBarPolicy(
        QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    window.history_actions_scroll.setFixedHeight(58)
    window.history_actions_widget.adjustSize()

    def reflow_history_controls(compact):
        page_margins = layout.contentsMargins()
        selection_margins = selection_layout.contentsMargins()
        selection_width = max(
            1,
            tab.width()
            - page_margins.left() - page_margins.right()
            - selection_margins.left() - selection_margins.right(),
        )
        _fit_button_label(window.history_delete_button, selection_width)
        selection_fits = (
            window.history_select_all.sizeHint().width()
            + window.history_delete_button.sizeHint().width()
            + max(0, selection_layout.spacing())
            <= selection_width
        )
        selection_layout.setDirection(
            QtWidgets.QBoxLayout.Direction.LeftToRight
            if selection_fits
            else QtWidgets.QBoxLayout.Direction.TopToBottom
        )
        if actions is not None:
            for button in history_action_buttons:
                actions.removeWidget(button)
            if compact:
                panel_margins = panel_layout.contentsMargins()
                footer_margins = footer_layout.contentsMargins()
                available_width = max(
                    1,
                    tab.width()
                    - page_margins.left() - page_margins.right()
                    - panel.frameWidth() * 2
                    - panel_margins.left() - panel_margins.right()
                    - footer_margins.left() - footer_margins.right()
                    - window.history_actions_scroll.frameWidth() * 2
                )
            else:
                available_width = max(1, tab.width() - 108)
            for button in history_action_buttons:
                if not button.isHidden():
                    _fit_button_label(button, available_width)
            spacing = max(0, actions.horizontalSpacing())
            visible_buttons = tuple(
                button for button in history_action_buttons
                if not button.isHidden()
            )
            widths = [button.sizeHint().width() for button in visible_buttons]
            natural_column_count = None
            if visible_buttons:
                for candidate in range(
                    len(visible_buttons),
                    (len(visible_buttons) + 1) // 2 - 1,
                    -1,
                ):
                    column_widths = [
                        max(widths[index] for index in range(column, len(widths), candidate))
                        for column in range(candidate)
                    ]
                    if sum(column_widths) + spacing * (candidate - 1) <= available_width:
                        natural_column_count = candidate
                        break
            natural_layout = natural_column_count is not None
            regular_buttons = tuple(
                button for button in visible_buttons
                if natural_layout or button is not window.history_delete_run_button
            )
            column_count = natural_column_count or len(regular_buttons) or 1
            maximum = max((button.sizeHint().width() for button in regular_buttons), default=0)
            wrapped = not natural_layout
            if wrapped:
                column_count = 1
                for candidate in range(2, min(3, len(regular_buttons)) + 1):
                    if maximum * candidate + spacing * (candidate - 1) <= available_width:
                        column_count = candidate
            actions.setSizeConstraint(
                QtWidgets.QLayout.SizeConstraint.SetMinimumSize
                if wrapped else QtWidgets.QLayout.SizeConstraint.SetFixedSize
            )
            window.history_actions_scroll.setWidgetResizable(wrapped)
            if wrapped:
                # SetMinimumSize does not clear the maximum left by a former
                # SetFixedSize layout; release it so wrapped rows fill the pane.
                window.history_actions_widget.setMaximumSize(16777215, 16777215)
            for column in range(len(history_action_buttons)):
                actions.setColumnStretch(column, 1 if wrapped and column < column_count else 0)
                actions.setColumnMinimumWidth(column, maximum if wrapped and column < column_count else 0)
            for index, button in enumerate(regular_buttons):
                actions.addWidget(
                    button, index // column_count, index % column_count,
                    QtCore.Qt.AlignmentFlag.AlignLeft if natural_layout else QtCore.Qt.AlignmentFlag(0),
                )
            if wrapped and window.history_delete_run_button in visible_buttons:
                actions.addWidget(
                    window.history_delete_run_button,
                    (len(regular_buttons) + column_count - 1) // column_count,
                    0, 1, column_count,
                )
            actions.activate()
        _fit_button_label(
            window.history_delete_button,
            selection_width,
        )
        window.history_actions_widget.adjustSize()
        content_height = window.history_actions_widget.sizeHint().height()
        max_height = (
            max(120, min(tab.height() - 360, tab.height() // 3))
            if compact else 240
        )
        window.history_actions_scroll.setFixedHeight(
            min(max_height, max(58, content_height + 8))
        )

    footer = QtWidgets.QWidget(panel)
    footer.setObjectName("historyActionFooter")
    footer_layout = QtWidgets.QVBoxLayout(footer)
    footer_layout.setContentsMargins(20, 17, 20, 16)
    footer_layout.setSpacing(8)
    selected_label = QtWidgets.QLabel(footer)
    selected_label.setObjectName("historySelectionSummary")
    footer_layout.addWidget(selected_label)
    footer_layout.addWidget(window.history_actions_scroll)
    panel_layout.addWidget(footer)
    window._atlas_history_actions_footer = footer
    window._atlas_history_selection_summary = selected_label
    layout.addWidget(panel, 1)
    _install_compact_page_reflow(
        window, tab, subtitle, layout, QtCore,
        attr_name="_atlas_history_reflow",
        compact_insets=(8, 12, 8, 0),
        visibility_widgets=history_action_buttons,
        compact_callback=lambda: (
            footer_layout.setContentsMargins(0, 6, 0, 6),
            reflow_history_controls(True),
        ),
        wide_callback=lambda: (
            footer_layout.setContentsMargins(20, 17, 20, 16),
            reflow_history_controls(False),
        ),
    )

    window._atlas_history_heading = heading
    window.history_table.itemSelectionChanged.connect(
        lambda: _refresh_history(window, text, QtCore, QtGui)
    )
    window.history_table.itemSelectionChanged.connect(
        lambda: window._atlas_history_reflow.sync()
    )
    _set_history_tooltips(window, text)
    class SummaryDelegate(QtWidgets.QStyledItemDelegate):
        def paint(self,painter,option,index):
            payload=index.siblingAtColumn(0).data(QtCore.Qt.ItemDataRole.UserRole)
            if isinstance(payload,Mapping) and payload.get("summary_pending"):
                _paint_loading(painter,option,QtCore,QtGui,window)
            else:
                super().paint(painter,option,index)
    for column in (2,4):
        window.history_table.setItemDelegateForColumn(column,SummaryDelegate(window.history_table))

    class OutcomeDelegate(QtWidgets.QStyledItemDelegate):
        """Keep run-state text legible when the row selection palette is active."""
        def paint(self, painter, option, index):
            text_value = index.data(QtCore.Qt.ItemDataRole.DisplayRole)
            if text_value is None or not str(text_value):
                super().paint(painter, option, index)
                return

            payload = index.siblingAtColumn(0).data(
                QtCore.Qt.ItemDataRole.UserRole
            )
            color = _history_outcome_color(
                payload, bool(getattr(window, "_dark", False))
            )
            widget = option.widget
            style = widget.style() if widget is not None else QtWidgets.QApplication.style()
            text_option = QtWidgets.QStyleOptionViewItem(option)
            self.initStyleOption(text_option, index)
            text_rect = style.subElementRect(
                QtWidgets.QStyle.SubElement.SE_ItemViewItemText,
                text_option,
                widget,
            )

            background_option = QtWidgets.QStyleOptionViewItem(text_option)
            background_option.text = ""
            style.drawControl(
                QtWidgets.QStyle.ControlElement.CE_ItemViewItem,
                background_option,
                painter,
                widget,
            )

            painter.save()
            painter.setPen(QtGui.QColor(color))
            painter.setFont(text_option.font)
            display_text = " ".join(str(text_value).split())
            display_text = QtGui.QFontMetrics(text_option.font).elidedText(
                display_text,
                QtCore.Qt.TextElideMode.ElideRight,
                max(1, text_rect.width()),
            )
            painter.drawText(
                text_rect,
                QtCore.Qt.AlignmentFlag.AlignLeft
                | QtCore.Qt.AlignmentFlag.AlignVCenter,
                display_text,
            )
            painter.restore()

    window.history_table.setItemDelegateForColumn(
        3, OutcomeDelegate(window.history_table)
    )


def _install_compact_page_reflow(
    window, tab, subtitle, layout, QtCore, *, attr_name, compact_insets,
    compact_callback=None, wide_callback=None, reflow_widgets=(),
    visibility_widgets=(),
) -> None:
    """Reclaim page chrome at small sizes while keeping every control present."""
    class Reflow(QtCore.QObject):
        def sync(self):
            compact = tab.height() < 480 or tab.width() < 640
            if compact:
                layout.setContentsMargins(*compact_insets)
                layout.setSpacing(2)
                subtitle.hide()
                if compact_callback is not None:
                    compact_callback()
            else:
                layout.setContentsMargins(30, 26, 30, 0)
                layout.setSpacing(12)
                subtitle.show()
                if wide_callback is not None:
                    wide_callback()

        def eventFilter(self, watched, event):
            if watched is tab and event.type() in (
                QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Show,
            ):
                self.sync()
            elif (
                watched in visibility_widgets
                and event.type() in (
                    QtCore.QEvent.Type.Show, QtCore.QEvent.Type.Hide,
                )
            ):
                self.sync()
            elif (
                watched in reflow_widgets
                and event.type() in (
                    QtCore.QEvent.Type.FontChange,
                    QtCore.QEvent.Type.StyleChange,
                )
            ):
                QtCore.QTimer.singleShot(0, self.sync)
            return False

    reflow = Reflow(tab)
    tab.installEventFilter(reflow)
    for widget in (*reflow_widgets, *visibility_widgets):
        widget.installEventFilter(reflow)
    setattr(window, attr_name, reflow)


def _paint_loading(painter,option,QtCore,QtGui,window):
    import math
    painter.save()
    color=QtGui.QColor("#40516b" if getattr(window,"_dark",False) else "#d7e2f1")
    color.setAlphaF(.55+.2*math.sin(getattr(window,"_spinner_frame",0)*.5))
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for offset,width in ((-11,.75),(9,.57)):
        rect=QtCore.QRectF(option.rect.x()+14,option.rect.center().y()+offset,max(20,(option.rect.width()-28)*width),10)
        painter.drawRoundedRect(rect,4,4)
    painter.restore()



def _hide_library_last_separator(table, QtCore, QtWidgets):
    """The footer owns the final boundary; keep separators between entries."""
    class FinalRowDelegate(QtWidgets.QStyledItemDelegate):
        def __init__(self, delegate):
            super().__init__(table)
            self.delegate = delegate

        def paint(self, painter, option, index):
            painter.save()
            try:
                if index.row() == index.model().rowCount() - 1:
                    painter.setClipRect(option.rect.adjusted(0, 0, 0, -1),
                                        QtCore.Qt.ClipOperation.IntersectClip)
                self.delegate.paint(painter, option, index)
            finally:
                painter.restore()

        def sizeHint(self, option, index):
            return self.delegate.sizeHint(option, index)

        def editorEvent(self, event, model, option, index):
            return self.delegate.editorEvent(event, model, option, index)

        def helpEvent(self, event, view, option, index):
            return self.delegate.helpEvent(event, view, option, index)

    for column in range(table.columnCount()):
        existing = table.itemDelegateForColumn(column) or table.itemDelegate()
        table.setItemDelegateForColumn(column, FinalRowDelegate(existing))

def _style_table(table, QtWidgets, *, row_height: int) -> None:
    table.verticalHeader().setDefaultSectionSize(row_height)
    table.verticalHeader().setMinimumSectionSize(row_height)
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.setWordWrap(True)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    table.horizontalHeader().setMinimumHeight(42)
    table.horizontalHeader().setStretchLastSection(False)


def _install_library_empty_state(window, QtCore, QtWidgets) -> None:
    empty_page = window.cache_stack.widget(2)
    layout = empty_page.layout()
    icon_label = QtWidgets.QLabel(empty_page)
    icon_label.setObjectName("emptyStateIcon")
    icon_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    icon_label.setFixedSize(96, 96)
    layout.insertWidget(1, icon_label, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
    message = empty_page.findChild(QtWidgets.QLabel, "pageSubtitle")
    message.setObjectName("emptyStateTitle")
    window._atlas_library_empty_icon = icon_label


def _install_history_empty_state(window, QtCore, QtGui, QtWidgets, text) -> None:
    if window.history_stack.count() < 3:
        return
    empty_page = window.history_stack.widget(2)
    layout = empty_page.layout()
    if layout is None or getattr(window, "_atlas_history_empty_title", None) is not None:
        return

    language = getattr(window, "_language", "en")
    icon_label = QtWidgets.QLabel(empty_page)
    icon_label.setObjectName("emptyStateIcon")
    icon_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    icon_label.setFixedSize(96, 96)
    title = QtWidgets.QLabel(
        _localized(
            text, "atlas_history_empty_title", language,
            "Nessuna analisi nella cronologia", "No analyses in history",
        ),
        empty_page,
    )
    title.setObjectName("emptyStateTitle")
    title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    title_font = _adjust_font(
        title.font(), QtGui, pixel_delta=2, min_pixel=18,
        point_delta=2.0, min_point=13.0,
    )
    title_font.setBold(True)
    title.setFont(title_font)
    layout.insertWidget(1, icon_label, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
    layout.insertWidget(2, title, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
    window._atlas_history_empty_icon = icon_label
    window._atlas_history_empty_title = title


def _adjust_font(font, QtGui, *, pixel_delta: int, min_pixel: int,
                 point_delta: float, min_point: float):
    adjusted = QtGui.QFont(font)
    if adjusted.pixelSize() > 0:
        adjusted.setPixelSize(max(min_pixel, adjusted.pixelSize() + pixel_delta))
    else:
        point_size = adjusted.pointSizeF()
        if point_size <= 0:
            point_size = 10.0
        adjusted.setPointSizeF(max(min_point, point_size + point_delta))
    return adjusted


def _title_delegate(QtCore, QtGui, QtWidgets, window):
    class LibraryTitleDelegate(QtWidgets.QStyledItemDelegate):
        def paint(self, painter, option, index):
            if index.siblingAtColumn(0).data(QtCore.Qt.ItemDataRole.UserRole + 1):
                _paint_loading(painter, option, QtCore, QtGui, window)
                return
            row = index.data(QtCore.Qt.ItemDataRole.UserRole)
            if not isinstance(row, Mapping):
                return super().paint(painter, option, index)

            title = str(row.get("title") or row.get("paper") or row.get("ref_id") or "")
            metadata = _library_metadata(row)
            styled = QtWidgets.QStyleOptionViewItem(option)
            self.initStyleOption(styled, index)
            styled.text = ""
            style = styled.widget.style() if styled.widget is not None else QtWidgets.QApplication.style()
            style.drawControl(
                QtWidgets.QStyle.ControlElement.CE_ItemViewItem,
                styled,
                painter,
                styled.widget,
            )

            rect = option.rect.adjusted(12, 4, -8, -4)
            selected = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
            palette = option.palette
            title_color = palette.color(
                QtGui.QPalette.ColorRole.HighlightedText if selected
                else QtGui.QPalette.ColorRole.Text
            )
            meta_color = title_color if selected else QtGui.QColor(
                "#9faec4" if getattr(window, "_dark", False) else "#61728b"
            )
            title_font = QtGui.QFont(option.font)
            title_font.setBold(True)
            title_font = _adjust_font(
                title_font, QtGui, pixel_delta=2, min_pixel=14,
                point_delta=2.0, min_point=11.0,
            )
            painter.save()
            painter.setFont(title_font)
            painter.setPen(title_color)
            fm = QtGui.QFontMetrics(title_font)
            if metadata:
                title_rect = QtCore.QRect(rect.x(), rect.y(), rect.width(), max(22, rect.height() // 2))
                meta_rect = QtCore.QRect(rect.x(), title_rect.bottom(), rect.width(), rect.bottom() - title_rect.bottom() + 1)
                painter.drawText(
                    title_rect,
                    QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                    fm.elidedText(title, QtCore.Qt.TextElideMode.ElideRight, title_rect.width()),
                )
                meta_font = _adjust_font(
                    option.font, QtGui, pixel_delta=-1, min_pixel=12,
                    point_delta=-0.5, min_point=9.0,
                )
                painter.setFont(meta_font)
                painter.setPen(meta_color)
                meta_fm = QtGui.QFontMetrics(meta_font)
                painter.drawText(
                    meta_rect,
                    QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                    meta_fm.elidedText(metadata, QtCore.Qt.TextElideMode.ElideRight, meta_rect.width()),
                )
            else:
                painter.drawText(
                    rect,
                    QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                    fm.elidedText(title, QtCore.Qt.TextElideMode.ElideRight, rect.width()),
                )
            painter.restore()

    return LibraryTitleDelegate(window.cache_table)


def _library_metadata(row: Mapping[str, Any]) -> str:
    author = row.get("authors") or row.get("author")
    if isinstance(author, (list, tuple)):
        author = ", ".join(
            str(value.get("name") or value.get("family") or value.get("author") or "")
            if isinstance(value, Mapping) else str(value)
            for value in author
            if value
        )
    elif isinstance(author, Mapping):
        author = author.get("name") or author.get("family") or author.get("author")
    parts = [str(value).strip() for value in (author, row.get("year")) if value]
    return " · ".join(part for part in parts if part)


def _availability_delegate(QtCore, QtGui, QtWidgets, text, window):
    class AvailabilityDelegate(QtWidgets.QStyledItemDelegate):
        def paint(self, painter, option, index):
            if index.siblingAtColumn(0).data(QtCore.Qt.ItemDataRole.UserRole + 1):
                _paint_loading(painter, option, QtCore, QtGui, window)
                return
            row = index.data(QtCore.Qt.ItemDataRole.UserRole)
            if not isinstance(row, Mapping):
                return super().paint(painter, option, index)
            tier = str(row.get("tier") or row.get("availability") or "none").lower()
            label = {
                "fulltext": text.get("fulltext", "Full text"),
                "abstract": text.get("abstract", "Abstract"),
                "none": text.get("no_text", "No text"),
            }.get(tier, tier)

            styled = QtWidgets.QStyleOptionViewItem(option)
            self.initStyleOption(styled, index)
            styled.text = ""
            style = styled.widget.style() if styled.widget is not None else QtWidgets.QApplication.style()
            style.drawControl(
                QtWidgets.QStyle.ControlElement.CE_ItemViewItem,
                styled,
                painter,
                styled.widget,
            )
            dark = bool(getattr(window, "_dark", False))
            foreground, background = {
                "fulltext": (("#a6ebca", "#174d35") if dark else ("#087846", "#dcf5e8")),
                "abstract": (("#b8c4d7", "#253447") if dark else ("#61728b", "#eff3f8")),
                "none": (("#b8c4d7", "#253447") if dark else ("#61728b", "#eff3f8")),
            }.get(tier, (("#b8c4d7", "#253447") if dark else ("#61728b", "#eff3f8")))
            badge_font = _adjust_font(
                option.font, QtGui, pixel_delta=-1, min_pixel=12,
                point_delta=-0.5, min_point=9.0,
            )
            metrics = QtGui.QFontMetrics(badge_font)
            badge_width = min(option.rect.width() - 20, metrics.horizontalAdvance(label) + 34)
            badge_height = 28
            badge = QtCore.QRect(
                option.rect.x() + 10,
                option.rect.y() + (option.rect.height() - badge_height) // 2,
                max(62, badge_width),
                badge_height,
            )
            painter.save()
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(background))
            painter.drawRoundedRect(badge, 6, 6)
            dot_color = {
                "fulltext": "#16a56a" if not dark else "#3dc889",
                "abstract": "#98a6bc",
                "none": "#98a6bc",
            }.get(tier, "#98a6bc")
            painter.setBrush(QtGui.QColor(dot_color))
            painter.drawEllipse(QtCore.QRect(badge.x() + 10, badge.y() + 10, 8, 8))
            painter.setPen(QtGui.QColor(foreground))
            painter.setFont(badge_font)
            text_rect = badge.adjusted(24, 0, -5, 0)
            painter.drawText(
                text_rect,
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(label, QtCore.Qt.TextElideMode.ElideRight, text_rect.width()),
            )
            painter.restore()

    return AvailabilityDelegate(window.cache_table)


def _refresh_library(window, QtCore, QtWidgets, text) -> None:
    if not hasattr(window, "cache_table"):
        return
    window.cache_summary.setVisible(bool(getattr(window,"_library_busy",False)))
    count = getattr(window, "_atlas_library_pending_count", None)
    set_pending_count(window, count)
    window._atlas_library_loaded_count.setText(text.get("atlas_loaded_count", "{count} loaded").format(count=len(getattr(window,"_cache_rows",()))))
    loading = "cache_page" in getattr(window, "_loading_kinds", set())
    if loading:
        loading_text = _localized(
            text, "atlas_loading_more", getattr(window, "_language", "en"),
            "Caricamento…", "Loading…",
        )
        window.cache_more_button.setText(loading_text)
    else:
        base = text.get("atlas_load_more", "Load more")
        total = int(getattr(window, "_atlas_cache_total", 0) or 0)
        loaded = len(getattr(window, "_cache_rows", ()))
        window.cache_more_button.setText(base)
    table = window.cache_table
    ready = len(getattr(window,"_cache_filtered_rows",())) + len(getattr(window,"_library_import_rows",()))
    if loading and ready and table.rowCount() <= ready:
        table.blockSignals(True)
        table.setRowCount(ready+3)
        for row_index in range(ready,ready+3):
            for column in range(table.columnCount()):
                item=QtWidgets.QTableWidgetItem("")
                item.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
                if column == 0:
                    item.setData(QtCore.Qt.ItemDataRole.UserRole+1,True)
                table.setItem(row_index,column,item)
        table.blockSignals(False)
    for row_index in range(table.rowCount()):
        item = table.item(row_index, 0)
        payload = item.data(QtCore.Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(payload, Mapping):
            continue
        availability_item = table.item(row_index, 2)
        if availability_item is not None:
            tier = str(payload.get("tier") or payload.get("availability") or "none").lower()
            if tier == "abstract":
                availability_item.setToolTip(text.get("abstract_retry_tip", ""))
    # Keep the table expanding between the search/filter controls and the
    # bottom-anchored load-more strip and action footer.


def _refresh_history(window, text, QtCore, QtGui) -> None:
    if not hasattr(window, "history_table"):
        return
    table = window.history_table
    selected_row = window._single_history_action_row()
    language = getattr(window, "_language", "en")
    if selected_row:
        selected = _localized(text, "history_selected", language,
                              "Run selezionata", "Selected run")
        paper = str(selected_row.get("paper") or selected_row.get("input") or "")
        window._atlas_history_selection_summary.setText(f"{selected}: {paper}")
    else:
        window._atlas_history_selection_summary.setText(_localized(
            text, "history_select_prompt", language,
            "Seleziona una run per vedere le azioni disponibili.",
            "Select a run to see available actions.",
        ))

    pending = any(
        isinstance(row, Mapping) and row.get("summary_pending")
        for row in getattr(window, "_history_rows", ())
    )
    loading = bool(
        getattr(window, "_loading_kinds", set())
        & {"history", "history_page", "history_details"}
    ) or pending
    if loading:
        message = _localized(text, "history_loading_summaries", language,
                             "Caricamento dei riepiloghi visibili…",
                             "Loading visible summaries…")
        window._atlas_history_loading.setText(message)
    window._atlas_history_loading.setVisible(loading)
    window._atlas_history_spinner.setVisible(loading)

    table.setWordWrap(False)
    for row_index in range(table.rowCount()):
        outcome_item = table.item(row_index, 3)
        payload_item = table.item(row_index, 0)
        payload = (
            payload_item.data(QtCore.Qt.ItemDataRole.UserRole)
            if payload_item is not None else None
        )
        if outcome_item is not None:
            color = _history_outcome_color(
                payload, bool(getattr(window, "_dark", False))
            )
            outcome_item.setForeground(QtGui.QBrush(QtGui.QColor(color)))
            outcome_item.setToolTip(outcome_item.text())
        for column in (2, 4):
            item = table.item(row_index, column)
            if item is not None and "\n" not in item.text():
                parts = item.text().split(" · ")
                if len(parts) >= 3:
                    item.setText("\n".join((" · ".join(parts[:2]), " · ".join(parts[2:]))))
                elif len(parts) == 2:
                    item.setText("\n".join(parts))


def _history_outcome_color(row, dark: bool) -> str:
    """Color durable run states without changing any verdict semantics."""
    muted = "#b6c4d8" if dark else "#61728b"
    if not isinstance(row, Mapping) or row.get("available", True) is not True:
        return muted

    status = str(row.get("run_status") or "").strip().lower()
    if status in {"paused", "interrupted"}:
        return "#f7bd54" if dark else "#b45309"
    if (status == "completed" and str(row.get("phase") or "").lower() == "done"
            and row.get("completed", True) is not False):
        return "#a5f3c5" if dark else "#087846"
    return muted


def _set_history_tooltips(window, text) -> None:
    icons = (
        (window.library_import_button, "paper-add"),
        (window.library_pending_button, "review-check"),
        (window.library_archive_import_button, "library-import"),
        (window.library_archive_export_button, "library-export"),
        (window.history_resume_button, "play"),
        (window.history_parse_review_button, "file"),
        (window.history_report_button, "history"),
        (window.history_verify_fork_button, "check"),
        (window.history_open_report_button, "file"),
        (window.history_report_folder_button, "folder"),
        (window.history_export_report_button, "download"),
        (window.history_delete_button, "trash"),
        (window.cache_delete_button, "trash"),
        (window.cache_clear_button, "database"),
    )
    window._atlas_collection_icon_keys = icons
    for button, key in icons:
        button.setProperty("atlasIcon", key)


def _set_icons(window, QtCore, QtGui) -> None:
    color = "#7ab4ff" if getattr(window, "_dark", False) else "#175fe6"
    empty_color = "#a4bad7" if getattr(window, "_dark", False) else "#61728b"
    for name, key in (("_atlas_history_empty_icon", "history"),
                      ("_atlas_library_empty_icon", "library")):
        label = getattr(window, name, None)
        if label is not None:
            label.setPixmap(_atlas_icon(QtCore, QtGui, key, window, color=empty_color).pixmap(
                QtCore.QSize(96, 96), window.devicePixelRatioF(),
            ))
    if hasattr(window, "_atlas_history_spinner"):
        window._atlas_history_spinner.setPixmap(_atlas_icon(QtCore, QtGui, "loading", window, color=color).pixmap(22, 22))
    for button, key in getattr(window, "_atlas_collection_icon_keys", ()):
        if button.property("primaryAction"):
            button.setIcon(_history_primary_icon(QtCore, QtGui, key, window))
        elif button is window.cache_delete_button:
            button.setIcon(_history_primary_icon(QtCore, QtGui, key, window, color="#ff9e99" if window._dark else "#cf3030"))
        else:
            button.setIcon(_atlas_icon(QtCore, QtGui, key, window, color=color))
    clear_button = getattr(window, "cache_clear_button", None)
    if clear_button is not None:
        clear_color = "#ffffff"
        clear_button.setIcon(_atlas_icon(
            QtCore, QtGui, "database", window, color=clear_color
        ))
    action = getattr(window, "_atlas_library_search_action", None)
    if action is not None:
        action.setIcon(_atlas_icon(QtCore, QtGui, "search", window, color=color))
    help_button = getattr(window, "_atlas_library_help_button", None)
    if help_button is not None:
        help_button.setIcon(_atlas_icon(QtCore, QtGui, "help", window, color=color))


def _atlas_icon(QtCore, QtGui, key: str, window, *, color: str | None = None):
    from .atlas_icons import icon

    if color is None:
        color = "#7ab4ff" if getattr(window, "_dark", False) else "#175fe6"
    return icon(QtCore, QtGui, key, color=color, size=18)


def _history_primary_icon(QtCore, QtGui, key: str, window, *, color="#ffffff"):
    """Keep action glyphs legible, with a neutral disabled state."""
    size = QtCore.QSize(18, 18)
    dpr = window.devicePixelRatioF()
    normal = _atlas_icon(QtCore, QtGui, key, window, color=color)
    disabled = _atlas_icon(QtCore, QtGui, key, window, color="#8795a8")
    result = QtGui.QIcon()
    for mode in (
        QtGui.QIcon.Mode.Normal,
        QtGui.QIcon.Mode.Active,
        QtGui.QIcon.Mode.Selected,
    ):
        result.addPixmap(
            normal.pixmap(size, dpr), mode, QtGui.QIcon.State.Off
        )
    result.addPixmap(
        disabled.pixmap(size, dpr),
        QtGui.QIcon.Mode.Disabled,
        QtGui.QIcon.State.Off,
    )
    return result


def _open_help(window, text) -> None:
    navigation = getattr(window, "navigation", None)
    if navigation is None:
        return
    target = text.get("tab_info")
    for index in range(navigation.count()):
        item = navigation.item(index)
        if item is not None and item.text() == target:
            navigation.setCurrentRow(index)
            return
