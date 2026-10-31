# core/gui/atlante.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Atlante presentation helpers. Qt is supplied by the optional desktop shell."""
from __future__ import annotations

from collections.abc import Mapping
from .source_filters import (
    configure_source_filter_button,
    create_checkbox_source_menu,
    refresh_checkbox_source_menu,
    set_source_filter_count,
)


def navigation_icon(QtCore, QtGui, index, dark=False):
    """Small monoline navigation glyphs drawn with the same theme ink."""
    import math
    canvas = QtGui.QPixmap(24, 24)
    canvas.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(canvas)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtGui.QPen(QtGui.QColor("#a6b8d1" if dark else "#526780"), 1.7))
    if index == 0:
        painter.drawRoundedRect(QtCore.QRectF(5, 3, 14, 18), 1, 1)
        painter.drawLine(8, 9, 16, 9)
        painter.drawLine(8, 13, 16, 13)
        painter.drawLine(8, 17, 13, 17)
    elif index == 1:
        painter.drawEllipse(QtCore.QRectF(3, 3, 18, 18))
        painter.drawLine(12, 6, 12, 12)
        painter.drawLine(12, 12, 16, 15)
    elif index == 2:
        for x in (4, 10, 16):
            painter.drawRoundedRect(QtCore.QRectF(x, 4, 4, 17), .5, .5)
    else:
        painter.drawEllipse(QtCore.QRectF(5, 5, 14, 14))
        painter.drawEllipse(QtCore.QRectF(9, 9, 6, 6))
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            painter.drawLine(QtCore.QPointF(12+8*math.cos(rad),12+8*math.sin(rad)),
                             QtCore.QPointF(12+11*math.cos(rad),12+11*math.sin(rad)))
    painter.end()
    return QtGui.QIcon(canvas)


def settings_category(row):
    """Curated GUI settings, independent of the engine's environment inventory."""
    name = str(row.get("name") or "")
    if name in {"CITATION_VERIFIER_MAILTO", "CITATION_VERIFIER_VERIFY_TABLE_CITATIONS"}:
        return "general"
    if name in {"CITATION_VERIFIER_OCR_LANG", "CITATION_VERIFIER_OA_ALTERNATES",
                "CITATION_VERIFIER_WAYBACK", "CITATION_VERIFIER_PERMA",
                "CITATION_VERIFIER_GUIDED_SEARCH_URL", "CITATION_VERIFIER_GUIDED_SEARCH_QUERY"}:
        return "fetch"
    if name == "CITATION_VERIFIER_SIGNING_KEY_FILE":
        return "general"
    if row.get("group") == "resolution" and (row.get("secret") or name.endswith(
            ("_API_KEY", "_API_TOKEN", "_ACCESS_TOKEN"))):
        return "resolution"
    if name.startswith("BIBLIO_GLUTTON_") and name.endswith(("_URL", "_HOST")):
        return "resolution"
    return "hidden"


def install_source_filters(window, QtCore, QtWidgets, text, source_page_loader=None):
    def find_layout(layout, widget):
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.widget() is widget:
                return layout
            child = item.layout()
            if child is not None:
                found = find_layout(child, widget)
                if found is not None:
                    return found
        return None

    toolbar = find_layout(window.source_search.parentWidget().layout(), window.source_search)
    if toolbar is None:
        raise RuntimeError("source search field is not attached to the source pane layout")
    toolbar.setContentsMargins(0, 0, 0, 0)
    toolbar.setSpacing(8)
    window._atlas_source_toolbar = toolbar
    # Existing single filters remain internal compatibility controls, hidden.
    window.source_filter.hide()
    window.source_phase_filter.hide()
    window._atlas_filter_keys = set()
    window._atlas_filter_phase = None
    button = QtWidgets.QPushButton(text["atlas_show_sources"], window._atlas_sources)
    button.setObjectName("sourceFiltersButton")
    configure_source_filter_button(button, QtWidgets)
    active = QtWidgets.QLabel(window._atlas_sources)
    active.setObjectName("mutedText")
    active.hide()
    window._atlas_filter_button = button
    window._atlas_filter_menu = None
    window._atlas_filter_label = active
    search_policy = window.source_search.sizePolicy()
    search_policy.setHorizontalPolicy(QtWidgets.QSizePolicy.Policy.Expanding)
    search_policy.setVerticalPolicy(QtWidgets.QSizePolicy.Policy.Fixed)
    window.source_search.setSizePolicy(search_policy)
    toolbar.addWidget(button)
    toolbar.setStretch(toolbar.indexOf(window.source_search), 1)
    toolbar.setStretch(toolbar.indexOf(button), 0)

    def update_toolbar_layout():
        parent = toolbar.parentWidget()
        if parent is None:
            return
        margins = toolbar.contentsMargins()
        available = max(0, parent.width() - margins.left() - margins.right())
        search_minimum = max(220, window.source_search.minimumSizeHint().width())
        required = search_minimum + button.minimumSizeHint().width() + toolbar.spacing()
        direction = (
            QtWidgets.QBoxLayout.Direction.TopToBottom
            if available < required
            else QtWidgets.QBoxLayout.Direction.LeftToRight
        )
        if toolbar.direction() != direction:
            toolbar.setDirection(direction)
        toolbar.setStretch(
            toolbar.indexOf(window.source_search),
            0 if direction == QtWidgets.QBoxLayout.Direction.TopToBottom else 1,
        )
        toolbar.setStretch(toolbar.indexOf(button), 0)
        toolbar.invalidate()
        toolbar.activate()

    class SourceToolbarReflow(QtCore.QObject):
        def eventFilter(self, watched, event):  # noqa: N802 - Qt API name
            if event.type() == QtCore.QEvent.Type.Resize:
                QtCore.QTimer.singleShot(0, update_toolbar_layout)
            return False

    reflow = SourceToolbarReflow(window)
    toolbar.parentWidget().installEventFilter(reflow)
    window._atlas_source_toolbar_reflow = reflow
    window._atlas_update_source_toolbar_layout = update_toolbar_layout
    update_toolbar_layout()
    source_heading = QtWidgets.QLabel(text.get("atlas_sources_title", "Sources"), window._atlas_sources)
    source_heading.setObjectName("sectionTitle")
    source_heading.hide()
    window._atlas_sources.layout().insertWidget(0, source_heading)
    window._atlas_sources.layout().insertWidget(2, active)
    window._atlas_filter_actions = {}
    window._atlas_counts_cancel = None

    def request_counts():
        keys = tuple(window._atlas_filter_actions)
        if source_page_loader is None or not window._run_dir:
            apply_source_counts(window, {key: sum(matches_source(row, filters={key})
                                               for row in window._source_rows) for key in keys})
            return
        import threading
        window._invalidate_load("source_filter_counts")
        if window._atlas_counts_cancel is not None:
            window._atlas_counts_cancel.set()
        run_dir = window._run_dir
        for checkbox in window._atlas_filter_actions.values():
            set_source_filter_count(checkbox, "…")
        if (
            window._child_database_bootstrap_pending(run_dir)
            or window._child_source_page_waiting_for_overview(run_dir)
        ):
            return
        cancellation = threading.Event()
        window._atlas_counts_cancel = cancellation
        def count():
            counts = dict.fromkeys(keys, 0)
            offset = 0
            while not cancellation.is_set():
                page = source_page_loader(run_dir, offset, 80)
                rows = page.get("references") or ()
                if not rows:
                    break
                for row in rows:
                    for key in keys:
                        counts[key] += bool(matches_source(row, filters={key}))
                offset += len(rows)
                if offset >= int(page.get("total", offset)):
                    break
            return (run_dir, None if cancellation.is_set() else counts)
        window._queue_load("source_filter_counts", count)
    window._atlas_request_source_counts = request_counts


def apply_source_counts(window, counts):
    for key, checkbox in window._atlas_filter_actions.items():
        if key in counts:
            set_source_filter_count(checkbox, counts[key])
    refresh_checkbox_source_menu(window._atlas_filter_menu)


def fade_in(widget, QtCore, QtWidgets):
    """A short content transition; it never gates background work or user input."""
    effect = QtWidgets.QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    animation = QtCore.QPropertyAnimation(effect, b"opacity", widget)
    animation.setStartValue(.35)
    animation.setEndValue(1.0)
    animation.setDuration(160)
    animation.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
    widget._atlante_fade = animation
    animation.start()


def empty_state(parent, QtCore, QtWidgets, message):
    view = QtWidgets.QWidget(parent)
    layout = QtWidgets.QVBoxLayout(view)
    layout.addStretch()
    label = QtWidgets.QLabel(message, view)
    label.setObjectName("pageSubtitle")
    label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    layout.addWidget(label)
    layout.addStretch()
    return view


def refresh_source_filters(window, QtWidgets, text):
    if not hasattr(window, "_atlas_filter_menu"):
        return
    complete = bool(window._current_report_path())
    if complete == window._atlas_filter_phase:
        return
    window._atlas_filter_phase = complete
    window._atlas_filter_keys.clear()
    window._atlas_filter_label.clear()
    window._atlas_filter_label.hide()
    choices = [("fulltext", text["fulltext"]), ("abstract", text["abstract"]),
               ("none", text["no_text"]), ("identity", text["atlas_identity_review"])]
    if complete:
        choices = [(key, text["atlas_outcome_"+key]) for key in
                   ("supports", "partial", "contradicts", "related", "off_topic", "non_decidable")]
        choices.append(("technical", text["atlas_technical_errors"]))
    if window._atlas_filter_menu is not None:
        window._atlas_filter_menu.hide()
        window._atlas_filter_menu.deleteLater()

    def changed(key, checked):
        if checked:
            window._atlas_filter_keys.add(key)
        else:
            window._atlas_filter_keys.discard(key)
        window._atlas_filter_label.setText(" · ".join(
            label for value, label in choices if value in window._atlas_filter_keys))
        window._atlas_filter_label.setVisible(bool(window._atlas_filter_keys))
        window._reset_source_page()

    def clear_filters(_checked=False):
        for checkbox in window._atlas_filter_actions.values():
            checkbox.setChecked(False)

    from PySide6 import QtCore, QtGui
    menu, actions = create_checkbox_source_menu(
        window._atlas_filter_button,
        QtCore,
        QtGui,
        QtWidgets,
        choices,
        checked_keys=window._atlas_filter_keys,
        clear_label=text["atlas_clear_filters"],
        on_toggle=changed,
        on_clear=clear_filters,
    )
    menu.aboutToShow.connect(window._atlas_request_source_counts)
    window._atlas_filter_menu = menu
    window._atlas_filter_actions = actions
    window._atlas_update_source_toolbar_layout()


def matches_source(row: Mapping, query: str = "", filters=()) -> bool:
    """Match source facts without inventing a source-level scientific verdict."""
    haystack = " ".join(str(row.get(k) or "") for k in
                        ("title", "source", "ref_id", "status", "result")).casefold()
    if query.strip().casefold() not in haystack:
        return False
    if not filters:
        return True
    availability = str(row.get("text") or row.get("availability") or row.get("tier") or "none").lower()
    outcomes = set(row.get("outcomes") or ())
    technical = bool(row.get("technical_errors"))
    for key in filters:
        if key in {"fulltext", "abstract", "none"} and availability == key:
            return True
        if key == "identity" and (row.get("review_labels") or row.get("status") == "richiede_intervento"):
            return True
        if key == "technical" and technical:
            return True
        if key in outcomes:
            return True
    return False


def filtered_source_page(loader, *, query="", filters=(), phase="", offset=0,
                         limit=30, sort_column=None, ascending=True, cancelled=None):
    """Scan compact bounded pages; retain only the requested result window."""
    import heapq
    import itertools

    def candidates():
        scan_offset = 0
        while cancelled is None or not cancelled.is_set():
            page = loader(scan_offset, limit)
            rows = page.get("references") or ()
            if not rows:
                return
            for row in rows:
                if matches_source(row, query, filters) and (not phase or row.get("phase") == phase):
                    yield row
            scan_offset += len(rows)
            if scan_offset >= int(page.get("total", scan_offset)):
                return

    rows = candidates()
    if sort_column is None:
        selected = list(itertools.islice(rows, offset, offset+limit+1))
    else:
        field = {0: "ref_number", 1: "title", 2: "phase", 3: "status", 4: "availability",
                 5: "risk_signal", 6: "review_labels", 7: "result"}.get(sort_column, "title")
        def key(row):
            value = row.get(field)
            return (int(value or 0) if field == "ref_number" else str(value or "").casefold())
        pick = heapq.nsmallest if ascending else heapq.nlargest
        selected = pick(offset+limit+1, rows, key=key)[offset:]
    return {"references": selected[:limit], "has_more": len(selected) > limit}


def stylesheet(*, dark=False, font_size=14):
    from pathlib import Path
    checkmark = (Path(__file__).parent / "assets" / "checkmark.svg").as_posix()
    bg, surface, ink, muted, line, accent, hover, pressed = (
        ("#111923", "#182330", "#e6edf6", "#9faec4", "#34455b", "#7ab4ff", "#223b59", "#304e70")
        if dark else
        ("#f8fbfe", "#ffffff", "#11183c", "#66769c", "#d1def2", "#175fe6", "#edf4ff", "#e3efff")
    )
    return f"""
    QWidget {{ background: transparent; color: {ink}; font-family: 'Segoe UI', sans-serif; font-size: {font_size+2}px; }}
    QDialog, QMainWindow {{ background: {bg}; }}
    QFrame#brandHeader {{ background: {surface}; border: 0; border-bottom: 1px solid {line}; border-radius: 0; padding: 8px 16px; }}
    QListWidget#navigationSidebar {{ background: {surface}; border: 0; border-radius: 0; padding: 16px 8px; }}
    QListWidget#navigationSidebar::item {{ padding: 14px 12px; margin: 3px 0; border: 0; border-left: 3px solid transparent; border-radius: 5px; }}
    QListWidget#navigationSidebar::item:hover {{ background: {hover}; }}
    QListWidget#navigationSidebar::item:selected {{ background: {pressed}; color: {accent}; border-left: 3px solid {accent}; }}
    QLabel#pageTitle {{ font-size: {font_size+14}px; font-weight: 700; background: transparent; }}
    QLabel#pageSubtitle, QLabel#mutedText {{ color: {muted}; background: transparent; }}
    QLabel#phaseLabel {{ border: 0; background: transparent; padding: 0; }}
    QFrame#startupCard, QFrame#runtimeCard {{ background: transparent; border: 0; }}
    QFrame#optionsDrawer {{ background: {surface}; border: 0; }}
    QLabel#paperDropArea {{ background: {surface}; border: 1px dashed {accent}; border-radius: 8px; padding: 30px 16px; font-size: {font_size+4}px; }}
    QLabel#paperDropArea:hover {{ background: {hover}; }}
    QPushButton, QToolButton {{ background: {surface}; border: 1px solid {line}; border-radius: 6px; padding: 9px 14px; font-weight: 600; }}
    QPushButton:hover, QToolButton:hover {{ background: {hover}; border-color: {accent}; }}
    QPushButton:pressed, QToolButton:pressed {{ background: {pressed}; border-color: {accent}; }}
    QPushButton:checked, QToolButton:checked {{ background: {pressed}; color: {accent}; border-color: {accent}; }}
    QPushButton#jury1ModelsButton, QPushButton#jury2ModelsButton {{ background: {hover}; color: {accent}; border: 1px solid {accent}; padding: 12px 16px; text-align: left; }}
    QPushButton#jury1ModelsButton:hover, QPushButton#jury2ModelsButton:hover {{ background: {pressed}; }}
    QPushButton#jury1ModelsButton:pressed, QPushButton#jury2ModelsButton:pressed {{ background: {line}; }}
    QPushButton#runStartButton {{ background: {accent}; color: {'#102034' if dark else '#ffffff'}; border-color: {accent}; }}
    QPushButton#runStartButton:hover {{ background: {'#a1cbff' if dark else '#1351c4'}; }}
    QPushButton#runStartButton:pressed {{ background: {'#6a9ddd' if dark else '#0f419f'}; }}
    QPushButton#runResumeButton {{ background: {surface}; color: {accent}; border-color: {accent}; }}
    QPushButton#runResumeButton:hover {{ background: {hover}; color: {accent}; border-color: {accent}; }}
    QPushButton#runResumeButton:pressed {{ background: {pressed}; color: {accent}; border-color: {accent}; }}
    QPushButton:disabled, QPushButton#runStartButton:disabled, QPushButton#runResumeButton:disabled {{ background: {line}; color: {muted}; border-color: {line}; }}
    QMenu, #settingsSection QMenu {{ background: {surface}; color: {ink}; border: 1px solid {line}; padding: 6px; }}
    QMenu::item {{ padding: 9px 16px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {hover}; color: {accent}; }}
    QComboBox:hover, QLineEdit:hover, QSpinBox:hover {{ border-color: {accent}; }}
    QComboBox:on {{ background: {pressed}; }}
    QCheckBox::indicator:pressed {{ background: {pressed}; border-color: {accent}; }}
    QAbstractItemView {{ background: {surface}; alternate-background-color: {bg}; border: 1px solid {line}; selection-background-color: {pressed}; selection-color: {ink}; outline: 0; }}
    QAbstractItemView::item:hover {{ background: {hover}; }}
    QAbstractItemView::item:selected {{ background: {pressed}; color: {ink}; }}
    QHeaderView::section {{ background: {surface}; color: {muted}; padding: 12px 8px; border: 0; border-bottom: 1px solid {line}; }}
    QPushButton#loadMoreStrip {{ padding: 5px 10px; border-radius: 0; border-top: 1px solid {line}; color: {accent}; min-height: 16px; }}
    QPushButton#loadMoreStrip[analysisSourceFooter="true"] {{ padding: 8px 14px; border-bottom: 1px solid {line}; }}
    QScrollBar::handle:hover {{ background: {accent}; }}
    QFrame#collectionTablePanel {{ background: {surface}; border: 1px solid {line}; border-radius: 4px; }}
    QFrame#collectionTablePanel QTableWidget {{ border: 0; background: {surface}; }}
    QTableWidget#libraryTable QHeaderView {{ border: 0; }}
    QWidget#collectionFooter {{ background: {surface}; border: 0; border-top: 1px solid {line}; }}
    QWidget#historySelectionBar, QWidget#historyActionStripContent {{ background: transparent; }}
    QTableWidget#libraryTable::item, QTableWidget#historyTable::item {{ padding: 12px; }}
    QLabel#libraryPendingCount {{ background: #f2aa16; color: #ffffff; border-radius: 12px; padding: 3px 7px; font-weight: 600; }}
    QLabel#historyLoadingStatus, QLabel#historySelectionSummary {{ color: {muted}; padding: 6px 0; }}
    QPushButton#libraryImport, QPushButton#libraryPendingReview, QPushButton#libraryPendingButton, QPushButton#libraryArchiveImport, QPushButton#libraryArchiveExport {{ color: {accent}; border-color: {accent}; background: {surface}; }}
    QPushButton#libraryPendingButton {{ padding-right: 48px; }}
    QPushButton#libraryPendingButton:hover {{ background: {hover}; }}
    QPushButton#libraryPendingButton:pressed {{ background: {pressed}; }}
    QPushButton#clearCacheButton, QPushButton#clearBrowserCache, QPushButton#libraryDeleteSelected {{ color: {'#ff9e99' if dark else '#cf3030'}; border-color: {'#b65f62' if dark else '#efb4b4'}; background: {surface}; }}
    QPushButton#clearCacheButton:hover, QPushButton#clearBrowserCache:hover, QPushButton#libraryDeleteSelected:hover {{ background: {'#472931' if dark else '#fff0ef'}; }}
    QPushButton#clearCacheButton:pressed, QPushButton#clearBrowserCache:pressed, QPushButton#libraryDeleteSelected:pressed {{ background: {'#66333d' if dark else '#ffdedb'}; }}
    QPushButton#clearCacheButton {{ background: {'#822c39' if dark else '#be3046'}; color: white; border-color: {'#d66d7e' if dark else '#be3046'}; }}
    QPushButton#clearCacheButton:hover {{ background: {'#a43b4c' if dark else '#a8253a'}; color: white; }}
    QPushButton#clearCacheButton:pressed {{ background: {'#6b2430' if dark else '#8b1e30'}; color: white; }}
    QPushButton#clearCacheButton:disabled, QPushButton#libraryDeleteSelected:disabled, QPushButton#dangerButton:disabled {{ background: {line}; color: {muted}; border-color: {line}; }}
    QLineEdit#librarySearch, QComboBox#libraryFilter {{ min-height: 25px; padding: 3px 12px; background: {surface}; }}
    QComboBox#libraryFilter {{ padding-right: 30px; }}
    QListWidget#settingsCategories {{ border: 0; background: transparent; min-width: 220px; }}
    QListWidget#settingsCategories::item {{ padding: 14px 10px; border-radius: 4px; margin-bottom: 5px; }}
    QListWidget#settingsCategories::item:hover {{ background: {hover}; }}
    QListWidget#settingsCategories::item:selected {{ background: {pressed}; color: {accent}; }}
    QFrame#settingsSection {{ border: 0; border-bottom: 1px solid {line}; border-radius: 0; padding: 16px 8px; }}
    QFrame#settingsSection[compactSettings="true"] {{ background: transparent; border: 0; border-radius: 0; padding: 0; }}
    QFrame#settingsFooter {{ border: 0; border-top: 1px solid {line}; background: {bg}; padding: 0; }}
    QPushButton#appearanceLight {{ background: #f5f8ff; color: #172445; border-color: #a8bbdc; }}
    QPushButton#appearanceLight:hover {{ background: #e3edff; border-color: #3978d3; }}
    QPushButton#appearanceLight:pressed, QPushButton#appearanceLight:checked {{ background: #d7e6ff; color: #103c80; border: 2px solid #3978d3; }}
    QPushButton#appearanceDark {{ background: #202e43; color: #eef5ff; border-color: #7186a6; }}
    QPushButton#appearanceDark:hover {{ background: #2b4260; border-color: #9cc6ff; }}
    QPushButton#appearanceDark:pressed, QPushButton#appearanceDark:checked {{ background: #304c70; color: #ffffff; border: 2px solid #9cc6ff; }}

    QToolButton#libraryHelp {{ border: 0; background: transparent; color: {accent}; }}
    QScrollArea#historyActions {{ background: {surface}; border: 0; }}

    QWidget#atlasShell {{ background: {bg}; }}
    QWidget#sidebarShell {{ background: {surface}; border-right: 1px solid {line}; }}
    QSplitter#globalNavigationSplitter::handle {{ background: transparent; border: 0; }}
    QFrame#sidebarOverlay {{ background: {surface}; border: 1px solid {line}; }}
    QToolButton#navigationToggle {{
        background: transparent; border: 1px solid transparent; border-radius: 5px; padding: 8px;
    }}
    QToolButton#navigationToggle:hover {{ background: {hover}; }}
    QToolButton#navigationToggle:checked {{ background: {pressed}; }}
    QPushButton#settingsNavigationToggle, QPushButton#guideNavigationToggle {{
        background: {surface}; color: {accent}; border: 1px solid {line};
        border-radius: 6px; padding: 9px 14px; font-weight: 600;
    }}
    QPushButton#settingsNavigationToggle:hover, QPushButton#guideNavigationToggle:hover {{
        background: {hover}; border-color: {accent};
    }}
    QPushButton#settingsNavigationToggle:checked, QPushButton#guideNavigationToggle:checked {{
        background: {pressed}; border-color: {accent};
    }}
    QPushButton {{ font-weight: 600; }}
    QPushButton#runStartButton:disabled {{ background: {line}; color: #ffffff; }}
    QPushButton#runStartButton, QPushButton#runResumeButton {{ min-height: 40px; font-size: {font_size+4}px; }}
    QFrame#brandHeader {{ padding: 0; min-height: 54px; }}
    QPushButton#themeToggle, QToolButton {{ border-color: transparent; background: transparent; }}
    QPushButton#themeToggle:hover, QToolButton:hover {{ background: {hover}; border-color: {line}; }}
    QLabel#sourceDetailHeadingTitle {{ color: {ink}; font-size: {font_size+6}px; font-weight: 700; }}
    QToolButton#sourceDetailClose {{ background: {surface}; color: {ink}; border: 1px solid {line}; border-radius: 6px; padding: 0; }}
    QToolButton#sourceDetailClose:hover {{ background: {hover}; border-color: {accent}; }}
    QToolButton#sourceDetailClose:pressed {{ background: {pressed}; border-color: {accent}; }}
    QToolButton#sourceDetailNext {{ background: {surface}; color: {ink}; border: 1px solid {line}; }}
    QToolButton#sourceDetailNext:hover {{ background: {hover}; border-color: {accent}; }}
    QToolButton#sourceDetailNext:pressed {{ background: {pressed}; border-color: {accent}; }}
    QToolButton#sourceDetailNext:disabled {{ background: {line}; color: {muted}; border-color: {line}; }}
    QListWidget#navigationSidebar {{ padding: 18px 6px; }}
    QListWidget#navigationSidebar::item {{ padding: 14px 18px; margin: 3px 0; font-weight: 600; }}
    QPushButton#sidebarAction {{ border: 0; background: transparent; text-align: left; padding: 14px 28px; font-weight: 600; }}
    QPushButton#sidebarAction:hover {{ background: {hover}; }}
    QPushButton#sidebarAction:pressed {{ background: {pressed}; }}
    QLabel#pageTitle, QLabel#reviewPageTitle {{ font-size: {font_size+18}px; font-weight: 700; }}
    QLabel#pageSubtitle {{ font-size: {font_size+5}px; }}
    QLabel#fieldCaption {{ font-weight: 600; }}
    QLabel#paperDropArea {{ padding: 0; background: {bg}; border: 1px dashed {line}; border-radius: 6px; }}
    QLabel#paperDropArea:hover {{ border-color: {accent}; background: {hover}; }}
    QLabel#dropCaption {{ color: {ink}; background: transparent; font-size: {font_size+5}px; font-weight: 600; }}
    QLabel#manuscriptGlyph {{ background: transparent; border: 0; }}
    QPushButton#chooseManuscriptButton {{ background: {hover}; color: {accent}; border: 1px solid {accent}; min-height: 32px; font-size: {font_size+3}px; }}
    QPushButton#chooseManuscriptButton:hover {{ background: {pressed}; }}
    QPushButton#chooseManuscriptButton:pressed {{ background: {line}; }}
    QPushButton#analysisOptionsButton {{ background: {surface}; color: {ink}; min-height: 26px; padding: 9px 24px; }}
    QPushButton#analysisOptionsButton:hover, QPushButton#analysisOptionsButton:checked {{ background: {hover}; color: {accent}; border-color: {accent}; }}
    QPushButton#jury1ModelsButton, QPushButton#jury2ModelsButton {{ background: {surface}; color: {accent}; font-size: {font_size+3}px; font-weight: 600; padding: 17px 22px; }}
    QComboBox#analysisMode {{ padding: 10px 16px; background: {surface}; min-height: 24px; }}
    QToolButton#inlineHelp {{ padding: 3px; border: 0; }}
    QFrame#optionsDrawer, QWidget#optionsBody {{ background: {surface}; }}
    QFrame#sourceDetailPane {{ background: {surface}; border: 0; }}
    QSplitter#atlasSourceSplit::handle {{ background: transparent; width: 18px; }}
    QSplitter#analysisOptionsSplit::handle {{ background: transparent; width: 8px; }}
    QSplitter#analysisOptionsSplit::handle:hover {{ background: {accent}; }}
    QWidget#analysisSourceCard {{ background: transparent; border: 0; }}
    QStackedWidget#analysisSourceStack {{ background: transparent; border: 0; }}
    QTableWidget#sourceTable {{ background: {surface}; border: 1px solid {line}; border-radius: 6px; padding: 0; }}
    QTableWidget#sourceTable QHeaderView {{ border: 0; padding: 0; }}
    QTableWidget#sourceTable::item {{ border: 0; border-bottom: 1px solid {line}; }}
    QTableWidget#sourceTable QHeaderView::section {{ border: 0; border-bottom: 1px solid {line}; border-radius: 0; }}
    QTableWidget#historyTable {{ background: {surface}; padding: 0; }}
    QTableWidget#historyTable QHeaderView {{ border: 0; padding: 0; }}
    QTableWidget#historyTable QHeaderView::section {{ border: 0; border-bottom: 1px solid {line}; border-radius: 0; }}
    QTableWidget#historyTable::item {{ border: 0; border-bottom: 1px solid {line}; }}
    QLabel#juryChevron {{ background: transparent; border: 0; }}
    QLabel#drawerTitle {{ font-size: {font_size+12}px; font-weight: 700; }}
    QLabel#sectionTitle {{ font-size: {font_size+5}px; font-weight: 600; }}
    QFrame#sectionDivider {{ color: {line}; max-height: 1px; margin-top: 7px; margin-bottom: 7px; }}
    QPushButton#severitySegment {{ border-radius: 0; padding: 9px 8px; font-weight: 500; }}
    QPushButton#severitySegment:checked {{ background: {accent}; color: {'#102034' if dark else '#ffffff'}; }}
    QPushButton#openRunReport {{ background: #175fe6; color: #ffffff; border-color: #175fe6; }}
    QPushButton#openRunReport:hover {{ background: #1351c4; color: #ffffff; }}
    QPushButton#openRunReport:pressed {{ background: #0f419f; color: #ffffff; }}
    QPushButton#openParseReviewButton {{ background: {accent}; color: {'#102034' if dark else '#ffffff'}; border-color: {accent}; }}
    QPushButton#openParseReviewButton:hover {{ background: {'#a1cbff' if dark else '#1351c4'}; color: {'#102034' if dark else '#ffffff'}; }}
    QPushButton#openParseReviewButton:pressed {{ background: {'#6a9ddd' if dark else '#0f419f'}; color: {'#102034' if dark else '#ffffff'}; }}
    QPushButton#primaryButton {{ background: {accent}; color: {'#102034' if dark else '#ffffff'}; border-color: {accent}; min-width: 85px; }}
    QPushButton#primaryButton:hover {{ background: {'#a1cbff' if dark else '#1351c4'}; }}
    QPushButton#primaryButton:pressed {{ background: {'#6a9ddd' if dark else '#0f419f'}; }}
    QPushButton#signingKeyCancel {{ background: {surface}; color: {ink}; border-color: {line}; }}
    QPushButton#signingKeyCancel:hover {{ background: {hover}; border-color: {accent}; }}
    QPushButton#signingKeyCancel:pressed {{ background: {pressed}; border-color: {accent}; }}
    QPushButton#dangerButton, QPushButton[text="Cancel"], QPushButton[text="Annulla"], QPushButton[text="&Cancel"], QPushButton[text="&Annulla"] {{ background: {'#822c39' if dark else '#be3046'}; color: white; border-color: {'#d66d7e' if dark else '#be3046'}; }}
    QPushButton#dangerButton:hover, QPushButton[text="Cancel"]:hover, QPushButton[text="Annulla"]:hover, QPushButton[text="&Cancel"]:hover, QPushButton[text="&Annulla"]:hover {{ background: {'#a43b4c' if dark else '#a8253a'}; }}
    QPushButton#dangerButton:pressed, QPushButton[text="Cancel"]:pressed, QPushButton[text="Annulla"]:pressed, QPushButton[text="&Cancel"]:pressed, QPushButton[text="&Annulla"]:pressed {{ background: {'#6b2430' if dark else '#8b1e30'}; }}
    QFrame#optionsDrawer QCheckBox {{ spacing: 12px; }}
    QFrame#optionsDrawer QLabel#mutedText {{ color: {muted}; padding-left: 30px; font-size: {font_size}px; }}
    QCheckBox::indicator {{ width: 20px; height: 20px; border-radius: 3px; }}
    QLabel#emptyStateTitle {{ font-size: {font_size+8}px; font-weight: 600; }}
    QLabel#phaseStep {{ color: {muted}; padding: 8px 0; border: 0; }}
    QLabel#phaseBadge {{ color: {muted}; border: 1px solid {muted}; border-radius: 16px; background: {surface}; }}
    QLabel#phaseBadge[active="true"] {{ color: {accent}; border-color: {accent}; }}
    QLabel#phaseBadge[done="true"] {{ color: #ffffff; border-color: {accent}; background: {accent}; }}
    QFrame#phaseConnector {{ color: {line}; }}
    QLabel#phaseStep[active="true"] {{ color: {accent}; border-bottom-color: {accent}; font-weight: 600; }}
    QLineEdit {{ padding: 9px 12px; }}
    QTableWidget {{ gridline-color: {line}; alternate-background-color: {surface}; padding: 0; }}
    QTableWidget::item {{ padding: 10px 12px; border-bottom: 1px solid {line}; }}
    QTableWidget::indicator {{ width: 20px; height: 20px; border: 1px solid {line}; border-radius: 3px; background: {surface}; }}
    QTableWidget::indicator:checked {{ background: {accent}; border-color: {accent}; image: url("{checkmark}"); }}
    QHeaderView::section {{ font-weight: 600; color: {ink}; }}
    QScrollArea#startupScroll {{ border: 0; background: transparent; }}
    QScrollBar:vertical {{ background: {bg}; width: 12px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {line}; border-radius: 5px; min-height: 32px; }}
    QScrollBar::handle:vertical:hover {{ background: {accent}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QToolButton#windowControl, QToolButton#windowClose {{ padding: 0; border: 0; border-radius: 4px; background: transparent; }}
    QToolButton#windowControl:hover {{ background: {hover}; }}
    QToolButton#windowControl:pressed {{ background: {pressed}; }}
    QToolButton#windowClose:hover {{ background: #fbe7e9; }}
    QToolButton#windowClose:pressed {{ background: #efb8be; }}
    QPushButton#apiKeyAction {{ padding: 0 6px; border: 0; background: transparent; color: {accent}; font-size: {font_size}px; }}
    QPushButton#apiKeyAction:hover {{ background: {hover}; text-decoration: underline; }}
    QPushButton#apiKeyAction:pressed {{ background: {pressed}; }}
    QPushButton#apiKeyAction[checkStatus="success"] {{ background: #dff5e9; color: #167449; }}
    QPushButton#apiKeyAction[checkStatus="error"] {{ background: #fde8e8; color: #b42318; }}
    QComboBox {{ background: {surface}; padding-right: 30px; }}
    QComboBox::drop-down {{ border: 0; width: 30px; background: transparent; }}
    QPushButton#sourceFiltersButton {{ color: {accent}; border-color: {accent}; min-width: 150px; }}
    QPushButton#sourceFiltersButton:hover {{ background: {hover}; }}
    QPushButton#sourceFiltersButton:pressed {{ background: {pressed}; }}
    QTextBrowser#sourceDetailText {{ border: 0; background: transparent; padding: 0; }}
    QLabel#phaseStep {{ font-weight: 600; }}
    QFrame#analysisProgressCard {{ background: {surface}; border: 1px solid {line}; border-radius: 6px; }}
    QFrame#analysisProgressCard QLabel#phaseLabel {{ font-size: {font_size+4}px; font-weight: 600; }}
    QSplitter#atlasSourceSplit::handle:horizontal {{ background: transparent; border: 0; width: 5px; }}
    QSplitter#atlasSourceSplit::handle:horizontal:hover {{ background: {accent}; }}
    QWidget#analysisFooter {{ border-top: 1px solid {line}; }}
    QWidget#analysisFooter QPushButton {{ font-size: {font_size+2}px; min-height: 0; }}
    QPushButton#runtimeContinue {{ background: {surface}; color: {accent}; border-color: {accent}; }}
    QPushButton#runtimeContinue[stopping="true"] {{ color: {'#ff9e99' if dark else '#d53030'}; border-color: {'#b65f62' if dark else '#ef7676'}; }}
    QPushButton#runtimeContinue[stopping="true"]:hover {{ background: {'#472931' if dark else '#fff0ef'}; }}
    QPushButton#runtimeContinue[stopping="true"]:pressed {{ background: {'#66333d' if dark else '#ffdedb'}; }}

    """


def install_source_badges(window,QtCore,QtGui,QtWidgets,text):
    class TextBadge(QtWidgets.QStyledItemDelegate):
        def paint(self,painter,option,index):
            row = index.siblingAtColumn(0).data(QtCore.Qt.ItemDataRole.UserRole)
            if not isinstance(row,Mapping):
                return super().paint(painter,option,index)
            styled=QtWidgets.QStyleOptionViewItem(option)
            self.initStyleOption(styled,index)
            styled.text=""
            styled.backgroundBrush=QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush)
            option.widget.style().drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem,styled,painter,option.widget)
            tier=str(row.get("availability") or row.get("text") or "none").lower()
            dark=window._dark
            ink,bg=("#b1e8cf","#1b4938") if dark else ("#11825a","#e0f4e9")
            if tier != "fulltext":
                ink,bg=("#adbad2","#253344") if dark else ("#78859c","#eef1f7")
            label=str(index.data() or "")
            painter.save()
            badge_font = QtGui.QFont(option.font)
            badge_font.setPixelSize(max(12, window._font_size-1))
            painter.setFont(badge_font)
            metrics=QtGui.QFontMetrics(badge_font)
            width=min(option.rect.width()-12,metrics.horizontalAdvance(label)+20)
            badge=QtCore.QRect(option.rect.x()+6,option.rect.center().y()-16,width,32)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(bg));painter.drawRoundedRect(badge,5,5)
            painter.setPen(QtGui.QColor(ink))
            painter.drawText(badge.adjusted(10,0,-6,0),QtCore.Qt.AlignmentFlag.AlignVCenter,metrics.elidedText(label,QtCore.Qt.TextElideMode.ElideRight,width-16))
            painter.restore()
    window.source_table.setItemDelegateForColumn(4,TextBadge(window.source_table))

    class ResultDot(QtWidgets.QStyledItemDelegate):
        def paint(self, painter, option, index):
            row = index.siblingAtColumn(0).data(QtCore.Qt.ItemDataRole.UserRole)
            outcomes = row.get("outcomes") or () if isinstance(row, Mapping) else ()
            # A marker decorates an existing single pair outcome; mixed source
            # facts keep their original text, without an aggregate verdict.
            colors = {"supports":"#139a63", "partial":"#e3a51c",
                      "contradicts":"#e05858", "related":"#7b89ac",
                      "off_topic":"#8b81aa", "non_decidable":"#8992a1"}
            color = colors.get(outcomes[0]) if len(outcomes) == 1 else None
            if color is None:
                return super().paint(painter, option, index)
            styled = QtWidgets.QStyleOptionViewItem(option)
            self.initStyleOption(styled, index)
            label = styled.text
            styled.text = ""
            option.widget.style().drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, styled, painter, option.widget)
            painter.save()
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(color))
            painter.drawEllipse(QtCore.QPointF(option.rect.left()+13, option.rect.center().y()), 5, 5)
            painter.setFont(option.font)
            painter.setPen(styled.palette.color(QtGui.QPalette.ColorRole.Text))
            rect = option.rect.adjusted(26,0,-5,0)
            painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignVCenter,
                             QtGui.QFontMetrics(option.font).elidedText(label,QtCore.Qt.TextElideMode.ElideRight,rect.width()))
            painter.restore()
    window.source_table.setItemDelegateForColumn(7,ResultDot(window.source_table))


def make_jury_model_button(QtCore, QtGui, QtWidgets, parent):
    """Create a Jury selector whose icon and label align by visible ink."""
    class JuryModelButton(QtWidgets.QPushButton):
        def paintEvent(self, event):
            option = QtWidgets.QStyleOptionButton()
            self.initStyleOption(option)
            style = self.style()
            content_rect = style.subElementRect(
                QtWidgets.QStyle.SubElement.SE_PushButtonContents, option, self
            )
            focus_rect = style.subElementRect(
                QtWidgets.QStyle.SubElement.SE_PushButtonFocusRect, option, self
            )

            bevel = QtWidgets.QStyleOptionButton()
            self.initStyleOption(bevel)
            bevel.text = ""
            bevel.icon = QtGui.QIcon()

            painter = QtGui.QPainter(self)
            style.drawControl(
                QtWidgets.QStyle.ControlElement.CE_PushButtonBevel,
                bevel,
                painter,
                self,
            )

            font = self.font()
            metrics = QtGui.QFontMetrics(font)
            text = option.text
            icon = option.icon
            icon_size = option.iconSize
            mode = (
                QtGui.QIcon.Mode.Disabled
                if not option.state & QtWidgets.QStyle.StateFlag.State_Enabled
                else QtGui.QIcon.Mode.Active
                if option.state & QtWidgets.QStyle.StateFlag.State_MouseOver
                else QtGui.QIcon.Mode.Normal
            )
            icon_state = (
                QtGui.QIcon.State.On
                if option.state & QtWidgets.QStyle.StateFlag.State_On
                else QtGui.QIcon.State.Off
            )
            pixmap = (
                icon.pixmap(icon_size, mode, icon_state)
                if not icon.isNull()
                else QtGui.QPixmap()
            )
            icon_width = (
                min(icon_size.width(), pixmap.width())
                if not pixmap.isNull()
                else 0
            )
            icon_ink_top = icon_ink_bottom = 0.0
            if not pixmap.isNull():
                image = pixmap.toImage()
                ink_top, ink_bottom = image.height(), -1
                for y in range(image.height()):
                    for x in range(image.width()):
                        if image.pixelColor(x, y).alpha():
                            ink_top = min(ink_top, y)
                            ink_bottom = max(ink_bottom, y)
                if ink_bottom >= ink_top:
                    ratio = max(1.0, pixmap.devicePixelRatioF())
                    icon_ink_top = ink_top / ratio
                    icon_ink_bottom = ink_bottom / ratio

            text_x = content_rect.left() + icon_width + (8 if icon_width else 0)
            text_width = max(0, content_rect.right() + 1 - text_x - 34)
            visible_text = metrics.elidedText(
                text, QtCore.Qt.TextElideMode.ElideRight, text_width
            )
            text_path = QtGui.QPainterPath()
            text_path.addText(0, 0, font, visible_text)
            text_ink = text_path.boundingRect()
            ink_center_y = (content_rect.top() + content_rect.bottom()) / 2.0
            baseline = int(
                ink_center_y - (text_ink.top() + text_ink.bottom()) / 2.0 + 0.5
            )
            if not pixmap.isNull():
                icon_ink_center = (icon_ink_top + icon_ink_bottom) / 2.0
                icon_y = round(ink_center_y - icon_ink_center)
                painter.drawPixmap(
                    QtCore.QPointF(content_rect.left(), icon_y), pixmap
                )
            group = (
                QtGui.QPalette.ColorGroup.Active
                if option.state & QtWidgets.QStyle.StateFlag.State_Enabled
                else QtGui.QPalette.ColorGroup.Disabled
            )
            painter.setFont(font)
            painter.setPen(
                option.palette.color(group, QtGui.QPalette.ColorRole.ButtonText)
            )
            painter.drawText(text_x, baseline, visible_text)

            if option.state & QtWidgets.QStyle.StateFlag.State_HasFocus:
                focus = QtWidgets.QStyleOptionFocusRect()
                focus.rect = focus_rect
                focus.state = option.state
                focus.backgroundColor = option.palette.color(
                    group, QtGui.QPalette.ColorRole.Button
                )
                style.drawPrimitive(
                    QtWidgets.QStyle.PrimitiveElement.PE_FrameFocusRect,
                    focus,
                    painter,
                    self,
                )
            painter.end()

    return JuryModelButton(parent)


def install_analysis(window, tab, source_pane, QtCore, QtWidgets, text):
    from PySide6 import QtGui
    from .atlas_icons import icon

    legacy_card = window.paper_drop.parentWidget()
    reused_widgets = (
        window.paper_drop,
        *window.jury_model_buttons.values(),
        window.start_button,
        window.resume_latest_button,
        window.new_analysis_button,
        window.open_guided_fetch_button,
        window.open_parse_review_button,
        window.open_run_report_button,
        window.export_run_report_button,
        window.phase_progress,
        window.current_activity,
        window.cooldown_status,
        window.institutional_access_status,
        window.institutional_login_button,
    )
    legacy_layouts = [
        legacy_card.layout(),
        *legacy_card.findChildren(QtWidgets.QLayout),
    ]
    for widget in reused_widgets:
        for layout in legacy_layouts:
            if layout is not None and layout.indexOf(widget) >= 0:
                layout.removeWidget(widget)
                break

    def tr(key, fallback):
        return text.get(key, fallback)

    def glyph(button, key, size=22):
        button.setProperty("atlasIcon", key)
        button.setProperty("atlasIconSize", size)
        button.setIcon(icon(QtCore, QtGui, key, size=size))
        button.setIconSize(QtCore.QSize(size, size))

    old_layout = tab.layout()
    while old_layout.count():
        item = old_layout.takeAt(0)
        if item.widget():
            item.widget().hide()
    old_splitter = source_pane.parentWidget()
    source_pane.setParent(tab)
    if old_splitter:
        old_splitter.hide()
    workspace = QtWidgets.QWidget(tab)
    workspace.setObjectName("analysisWorkspace")
    page = QtWidgets.QVBoxLayout(workspace)
    page.setContentsMargins(30, 0, 0, 0)
    old_layout.addWidget(workspace)
    window._atlas_analysis_workspace = workspace
    body = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, workspace)
    body.setObjectName("analysisOptionsSplit")
    body.setChildrenCollapsible(False)
    body.setHandleWidth(8)
    page.addWidget(body, 1)
    analysis_panel = QtWidgets.QWidget(body)
    analysis_panel.setMinimumWidth(0)
    analysis_panel.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                 QtWidgets.QSizePolicy.Policy.Expanding)
    analysis_panel_layout = QtWidgets.QVBoxLayout(analysis_panel)
    analysis_panel_layout.setContentsMargins(0, 0, 30, 0)
    analysis_panel_layout.setSpacing(0)
    analysis_scroll = QtWidgets.QScrollArea(analysis_panel)
    analysis_scroll.setObjectName("analysisPageScroll")
    analysis_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    analysis_scroll.setWidgetResizable(True)
    analysis_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QtWidgets.QWidget(analysis_scroll)
    main = QtWidgets.QVBoxLayout(content)
    main.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize)
    main.setContentsMargins(0, 26, 0, 0)
    main.setSpacing(8)
    content.setMinimumWidth(0)
    content.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
    analysis_scroll.setWidget(content)
    analysis_panel_layout.addWidget(analysis_scroll, 1)
    body.addWidget(analysis_panel)
    window._atlas_analysis_scroll = analysis_scroll
    body.setStretchFactor(0, 1)
    title = QtWidgets.QLabel(text["atlas_new_analysis"], content)
    title.setObjectName("pageTitle")
    title.setWordWrap(True)
    heading_row = QtWidgets.QHBoxLayout()
    title_column = QtWidgets.QWidget(content)
    title_column_layout = QtWidgets.QVBoxLayout(title_column)
    title_column_layout.setContentsMargins(0, 0, 0, 0)
    title_column_layout.setSpacing(4)
    title_column_layout.addWidget(title)
    heading_row.addWidget(title_column, 1)
    main.addLayout(heading_row)
    window._atlas_title = title
    window._atlas_heading_row = heading_row
    class StartupCard(QtWidgets.QFrame):
        def minimumSizeHint(self):
            hint = super().minimumSizeHint()
            return QtCore.QSize(0, hint.height())
        def resizeEvent(self, event):
            super().resizeEvent(event)
            callback = getattr(window, "_atlas_reflow_startup", None)
            if callback is not None:
                callback()

    startup = StartupCard(content)
    startup.setObjectName("startupCard")
    form = QtWidgets.QVBoxLayout(startup)
    form.setContentsMargins(0, 0, 0, 0)
    form.setSpacing(26)
    subtitle = QtWidgets.QLabel(text["atlas_choose_manuscript"], startup)
    subtitle.setObjectName("pageSubtitle")
    form.addWidget(subtitle)
    drop = window.paper_drop
    drop.setMinimumHeight(0)
    drop.setMaximumHeight(16777215)
    drop.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
    drop.setProperty("atlasDrop", True)
    drop.setStyleSheet("QLabel#paperDropArea { color: transparent; }")
    drop_layout = QtWidgets.QVBoxLayout(drop)
    drop_layout.setContentsMargins(20, 24, 20, 24)
    drop_layout.setSpacing(16)
    drop_layout.addStretch()
    drop_icon = QtWidgets.QLabel(drop)
    drop_icon.setObjectName("manuscriptGlyph")
    drop_icon.setPixmap(icon(QtCore, QtGui, "file", size=58).pixmap(58, 58))
    drop_icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    drop_icon.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    drop_layout.addWidget(drop_icon)
    caption = QtWidgets.QLabel(tr("atlas_drop_title", "Drop your manuscript here"), drop)
    caption.setObjectName("dropCaption")
    caption.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    caption.setWordWrap(True)
    caption.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    drop_layout.addWidget(caption)
    window._atlas_drop_caption = caption
    choose = QtWidgets.QPushButton(text["atlas_choose_file"], drop)
    choose.setObjectName("chooseManuscriptButton")
    choose.setMinimumWidth(200)
    choose.clicked.connect(window._browse_paper)
    drop_layout.addWidget(choose, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
    drop_layout.addStretch()
    form.addWidget(drop)
    window._atlas_drop_height_floor = 280
    mode_row = QtWidgets.QHBoxLayout()
    mode_row.setSpacing(24)
    mode_label = QtWidgets.QLabel(tr("atlas_mode", "Mode"), startup)
    mode_label.setObjectName("fieldCaption")
    mode_row.addWidget(mode_label)
    modes = QtWidgets.QComboBox(startup)
    modes.setObjectName("analysisMode")
    modes.setMinimumWidth(100)
    modes.setMaximumWidth(300)
    modes.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
    for key, button in window.mode_buttons.items():
        modes.addItem(button.text(), key)
        if button.isChecked():
            modes.setCurrentIndex(modes.count()-1)
        button.toggled.connect(lambda checked, key=key: modes.setCurrentIndex(modes.findData(key)) if checked else None)
    modes.currentIndexChanged.connect(lambda _: window.mode_buttons[modes.currentData()].setChecked(True))
    window._atlas_mode = modes
    help_button = QtWidgets.QToolButton(startup)
    help_button.setObjectName("inlineHelp")
    glyph(help_button, "help", 28)
    help_button.setToolTip(text["setting_CITATION_VERIFIER_ACCURACY_help"])
    help_button.setAccessibleName(tr("atlas_mode_help", "Verification mode help"))
    mode_controls = QtWidgets.QHBoxLayout()
    mode_controls.setSpacing(8)
    mode_controls.addWidget(modes, 1)
    mode_controls.addWidget(help_button, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
    mode_controls.addStretch(1)
    mode_row.addLayout(mode_controls, 1)
    options_button = QtWidgets.QPushButton(text["atlas_options"], startup)
    options_button.setObjectName("analysisOptionsButton")
    options_button.setCheckable(True)
    glyph(options_button, "settings", 20)
    mode_row.addWidget(options_button)
    mode_row.addStretch()
    form.addLayout(mode_row)
    startup_actions = [*window.jury_model_buttons.values(), window.start_button, window.resume_latest_button]
    startup_actions_grid = QtWidgets.QGridLayout()
    startup_actions_grid.setHorizontalSpacing(16)
    startup_actions_grid.setVerticalSpacing(16)
    startup_actions_grid.setColumnStretch(0, 1)
    startup_actions_grid.setColumnStretch(1, 1)
    for button in startup_actions:
        button.setMinimumWidth(0)
        button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
    for index, button in enumerate(window.jury_model_buttons.values()):
        glyph(button, "jury", 24)
        arrow = QtWidgets.QLabel(button)
        arrow.setObjectName("juryChevron")
        arrow.setPixmap(icon(QtCore, QtGui, "chevron", "#175fe6", 18).pixmap(18,18))
        arrow.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        overlay = QtWidgets.QHBoxLayout(button)
        overlay.setContentsMargins(0,0,18,0)
        overlay.addStretch()
        overlay.addWidget(arrow)
        startup_actions_grid.addWidget(button, 0, index, 1, 1)
    for index, (button, key) in enumerate(((window.start_button, "play"), (window.resume_latest_button, "resume"))):
        glyph(button, key, 24)
        startup_actions_grid.addWidget(button, 1, index, 1, 1)
    form.addLayout(startup_actions_grid)
    hints = QtWidgets.QHBoxLayout()
    window._atlas_start_hint = QtWidgets.QLabel(tr("atlas_start_hint", "Choose a manuscript to begin"), startup)
    window._atlas_resume_hint = QtWidgets.QLabel(startup)
    for label in (window._atlas_start_hint, window._atlas_resume_hint):
        label.setObjectName("mutedText")
        label.setWordWrap(True)
        label.setMaximumWidth(380)
        hints.addWidget(label, 1)
    hints.addStretch()
    form.addLayout(hints)
    startup.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum)
    startup_scroll = QtWidgets.QScrollArea(content)
    startup_scroll.setObjectName("startupScroll")
    startup_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    startup_scroll.setWidgetResizable(True)
    startup_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    form.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
    startup_scroll.setWidget(startup)
    main.addWidget(startup_scroll, 1)
    window._atlas_startup_scroll = startup_scroll
    startup_columns = 2
    def reflow_startup():
        nonlocal startup_columns
        narrow = startup_scroll.viewport().width() < 700
        direction = (QtWidgets.QBoxLayout.Direction.TopToBottom if narrow
                     else QtWidgets.QBoxLayout.Direction.LeftToRight)
        for row in (mode_row, hints):
            row.setDirection(direction)
        columns = 1 if startup_scroll.viewport().width() < 520 else 2
        if columns != startup_columns:
            for button in startup_actions:
                startup_actions_grid.removeWidget(button)
            for index, button in enumerate(startup_actions):
                startup_actions_grid.addWidget(button, index // columns, index % columns)
            startup_actions_grid.setColumnStretch(1, 1 if columns == 2 else 0)
            startup_columns = columns
        shared_button_height = max(button.sizeHint().height() for button in startup_actions)
        for button in startup_actions:
            button.setMinimumWidth(0)
            button.setMinimumHeight(shared_button_height)
        drop_layout = drop.layout()
        drop_layout.invalidate()
        drop_layout.activate()
        required_height = drop_layout.sizeHint().height()
        if drop.width() > 0 and drop_layout.hasHeightForWidth():
            required_height = max(
                required_height,
                drop_layout.heightForWidth(drop.width()),
            )
        required_height = max(required_height, drop_layout.minimumSize().height())
        drop.setFixedHeight(max(
            int(getattr(window, "_atlas_drop_height_floor", 280)),
            required_height,
        ))
        form.invalidate()
        form.activate()
        startup.setMinimumHeight(form.minimumSize().height())
        form.activate()
        reflow_analysis = getattr(window, "_atlas_reflow_analysis", None)
        if reflow_analysis is not None:
            reflow_analysis()
    window._atlas_reflow_startup = reflow_startup
    runtime = QtWidgets.QFrame(content)
    runtime.setObjectName("runtimeCard")
    status = QtWidgets.QVBoxLayout(runtime)
    status.setContentsMargins(0, 0, 0, 0)
    status.setSpacing(12)
    window._atlas_manuscript = QtWidgets.QLabel(title_column)
    window._atlas_manuscript.setObjectName("pageSubtitle")
    window._atlas_manuscript.setWordWrap(True)
    window._atlas_manuscript.setMinimumWidth(0)
    window._atlas_manuscript.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred
    )
    window._atlas_manuscript.hide()
    title_column_layout.addWidget(window._atlas_manuscript)
    window._atlas_steps = QtWidgets.QFrame(runtime)
    window._atlas_steps.setObjectName("phaseRail")
    rail = QtWidgets.QHBoxLayout(window._atlas_steps)
    rail.setContentsMargins(0, 0, 0, 0)
    rail.setSpacing(6)
    window._atlas_step_labels = []
    for index, label in enumerate((tr("atlas_step_parse", "Analysis"), tr("atlas_step_resolve", "Identity"), tr("atlas_step_fetch", "Retrieval"), tr("atlas_step_verify", "Verification"), "Report")):
        badge = QtWidgets.QLabel(str(index+1),window._atlas_steps)
        badge.setObjectName("phaseBadge")
        badge.setFixedSize(32,32)
        badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        badge.setToolTip(label)
        badge.setAccessibleName(label)
        name = QtWidgets.QLabel(label,window._atlas_steps)
        name.setObjectName("phaseStep")
        name.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        rail.addWidget(badge)
        rail.addWidget(name)
        if index < 4:
            connector = QtWidgets.QFrame(window._atlas_steps)
            connector.setFrameShape(QtWidgets.QFrame.Shape.HLine)
            connector.setObjectName("phaseConnector")
            connector.setMinimumWidth(16)
            connector.setMaximumWidth(32)
            rail.addWidget(connector, 1)
        window._atlas_step_labels.append(name)
        if not hasattr(window,"_atlas_step_badges"):
            window._atlas_step_badges=[]
        window._atlas_step_badges.append(badge)
    heading_row.addWidget(window._atlas_steps, 2)
    progress_card = QtWidgets.QFrame(runtime)
    progress_card.setObjectName("analysisProgressCard")
    progress_card.setMinimumHeight(max(56, int(getattr(window, "_font_size", 16) * 2.5)))
    progress_row = QtWidgets.QHBoxLayout(progress_card)
    progress_row.setContentsMargins(16, 0, 16, 0)
    progress_row.setSpacing(14)
    window.phase_label.hide()
    progress_row.addWidget(window.current_activity, 2)
    progress_row.addWidget(window.phase_progress, 3)
    progress_row.addWidget(window.phase_progress_percent, 0)
    status.addWidget(progress_card)
    window._atlas_progress_card = progress_card
    status.addWidget(window.cooldown_status)
    actions = QtWidgets.QHBoxLayout()
    actions.setContentsMargins(0, 0, 0, 0)
    actions.setSpacing(10)
    for widget, key in ((window.open_guided_fetch_button,"browser"),
                        (window.open_run_report_button,"external"), (window.export_run_report_button,"download"),
                        (window.new_analysis_button,"analysis")):
        glyph(widget, key)
        widget.setMinimumHeight(42)
        widget.setMaximumWidth(16777215)
        actions.addWidget(widget)
    actions.addStretch()
    window._atlas_runtime_resume = QtWidgets.QPushButton(text["resume_latest"], runtime)
    def resume_current():
        if window._process is not None:
            window._resume_latest_or_stop()
        elif window._run_dir:
            window._resume_run_row({"run_dir": window._run_dir,
                                    "phase": "parse" if window._parse_review_available else getattr(window,"_atlas_current_phase", ""),
                                    "run_status": "paused"})
    window._atlas_runtime_resume.clicked.connect(resume_current)
    window._atlas_runtime_actions = actions
    status.addLayout(actions)
    for button in (window.open_run_report_button, window.export_run_report_button):
        actions.setStretch(actions.indexOf(button), 1)
    source_pane.setObjectName("analysisSourceCard")
    source_layout = source_pane.layout()
    if source_layout is not None:
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(12)
        source_layout.insertWidget(0, runtime)
    window.source_stack.setObjectName("analysisSourceStack")
    window.source_stack.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    source_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, content)
    source_splitter.setObjectName("atlasSourceSplit")
    source_splitter.setChildrenCollapsible(False)
    source_splitter.addWidget(source_pane)
    details = QtWidgets.QFrame(source_splitter)
    details.setObjectName("sourceDetailPane")
    details.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
    details_layout = QtWidgets.QVBoxLayout(details)
    details_layout.setContentsMargins(24, 0, 0, 0)
    detail_header = QtWidgets.QWidget(details)
    detail_header.setObjectName("sourceDetailHeader")
    detail_header_layout = QtWidgets.QHBoxLayout(detail_header)
    detail_header_layout.setContentsMargins(0, 0, 0, 0)
    detail_header_layout.setSpacing(8)
    detail_back = QtWidgets.QToolButton(detail_header)
    detail_back.setObjectName("sourceDetailBack")
    detail_back.setAccessibleName(text.get("back", "Back"))
    detail_back.setToolTip(detail_back.accessibleName())
    detail_back.setIcon(icon(QtCore, QtGui, "arrow-left", size=20))
    detail_back.setIconSize(QtCore.QSize(20, 20))
    detail_back.setFixedSize(44, 44)
    detail_back.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
    detail_back.hide()
    detail_header_layout.addWidget(detail_back, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
    detail_title = QtWidgets.QLabel(tr("atlas_source_detail", "Source details"), details)
    detail_title.setObjectName("sectionTitle")
    detail_header_layout.addWidget(detail_title, 1)
    detail_header_layout.addStretch(1)
    detail_next = QtWidgets.QToolButton(detail_header)
    detail_next.setObjectName("sourceDetailNext")
    detail_next_label = text.get("source_detail_next", "Next source")
    detail_next.setAccessibleName(detail_next_label)
    detail_next.setToolTip(detail_next_label)
    detail_next.setIcon(icon(QtCore, QtGui, "arrow-right", size=20))
    detail_next.setIconSize(QtCore.QSize(20, 20))
    detail_next.setText(text.get("guided_fetch_next_unresolved", "Next"))
    detail_next.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    detail_next.setMinimumHeight(44)
    detail_next.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
    detail_next.hide()
    detail_header_layout.addWidget(detail_next)
    details_layout.addWidget(detail_header)
    detail_back.clicked.connect(window._source_detail_back)
    detail_next.clicked.connect(window._show_next_source_detail)
    window._atlas_detail_stack = QtWidgets.QStackedWidget(details)
    empty = QtWidgets.QWidget(details)
    empty_layout = QtWidgets.QVBoxLayout(empty)
    empty_layout.addStretch()
    empty_icon = QtWidgets.QLabel(empty)
    empty_icon.setPixmap(icon(QtCore, QtGui, "analysis", size=68).pixmap(68,68))
    empty_layout.addWidget(empty_icon, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
    empty_text = QtWidgets.QLabel(tr("atlas_source_detail_empty", "Select a source to see its details."), empty)
    empty_text.setObjectName("fieldCaption")
    empty_text.setWordWrap(True)
    empty_text.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    empty_layout.addWidget(empty_text)
    empty_note = QtWidgets.QLabel(tr("atlas_source_detail_lazy", "The preview loads when you request it."), empty)
    empty_note.setObjectName("mutedText")
    empty_note.setWordWrap(True)
    empty_note.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    empty_layout.addWidget(empty_note)
    empty_layout.addStretch()
    window._atlas_detail_stack.addWidget(empty)
    details_layout.addWidget(window._atlas_detail_stack,1)
    source_splitter.addWidget(details)
    source_splitter.setStretchFactor(0, 55)
    source_splitter.setStretchFactor(1, 45)
    source_splitter.setSizes([670,550])
    main.addWidget(source_splitter, 1)
    window._atlas_source_splitter = source_splitter
    window._atlas_detail_host = details
    window._atlas_source_detail_back_button = detail_back
    window._atlas_source_detail_next_button = detail_next
    window._atlas_source_detail_overlay_open = False
    window._atlas_source_detail_compact = False
    detail_overlay_animation = QtCore.QPropertyAnimation(
        details, b"geometry", window
    )
    detail_overlay_animation.setDuration(200)
    detail_overlay_animation.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
    window._atlas_source_detail_overlay_animation = detail_overlay_animation
    detail_viewport = analysis_scroll.viewport()
    source_overlay_open = False
    source_overlay_compact = False

    def overlay_target():
        return detail_viewport.rect()

    def overlay_offscreen(target=None):
        target = target or overlay_target()
        return QtCore.QRect(target.width(), target.y(), target.width(), target.height())

    def sync_detail_header():
        from core.gui.shell.navigation import set_back_button_presentation
        phase = str(getattr(window, "_atlas_current_phase", "") or "")
        if phase in {"report", "done"} and getattr(window, "_display_references_only", False):
            phase = "resolve"
        else:
            phase = {"gaps":"verify", "style":"verify", "web_research":"verify", "report":"verify", "done":"verify"}.get(phase, phase)
        back_label = text.get("source_detail_back_to_phase", "Back to {phase}").format(
            phase=text.get("phase_" + phase, text.get("tab_analysis", "Analysis")))
        set_back_button_presentation(
            detail_back, QtCore, QtGui, label=back_label,
            dark=bool(getattr(window, "_dark", False)),
            active=source_overlay_compact, text=back_label,
        )
        if source_overlay_compact:
            for button in (detail_back, detail_next):
                button.ensurePolished()
            shared_height = max(
                44,
                detail_back.sizeHint().height(),
                detail_back.minimumSizeHint().height(),
                detail_next.sizeHint().height(),
                detail_next.minimumSizeHint().height(),
            )
            detail_back.setFixedHeight(shared_height)
            detail_next.setFixedHeight(shared_height)
        current_detail = getattr(window, "_detail_dialog", None)
        detail_open = current_detail is not None and not current_detail[0].isHidden()
        detail_back.setVisible(source_overlay_compact and source_overlay_open)
        detail_title.setVisible(not source_overlay_compact and not detail_open)
        if not source_overlay_compact:
            details_layout.setContentsMargins(
                0 if detail_open else 24, 0, 0, 0,
            )
        if current_detail is not None:
            detail_layout = current_detail[0].layout()
            summary_header = current_detail[0].findChild(
                QtWidgets.QWidget, "sourceDetailSummaryHeader",
            )
            if summary_header is not None:
                summary_header.setVisible(not source_overlay_compact)
                close = summary_header.findChild(
                    QtWidgets.QToolButton, "sourceDetailClose",
                )
                if close is not None:
                    close.setVisible(not source_overlay_compact)
            if detail_layout is not None:
                detail_layout.setContentsMargins(
                    *((0, 0, 0, 0) if source_overlay_compact
                      else (20, 16, 20, 24))
                )
                detail_layout.setSpacing(0 if source_overlay_compact else 16)
            current_detail[1].setViewportMargins(
                *((24, 18, 24, 18) if source_overlay_compact else (0, 0, 0, 0))
            )
            heading_in_view = current_detail[2].get(
                "_source_summary_heading_in_view"
            )
            if heading_in_view is not None and bool(heading_in_view) != source_overlay_compact:
                scroll_position = current_detail[1].verticalScrollBar().value()
                render_summary = getattr(window, "_render_source_summary", None)
                if render_summary is not None:
                    render_summary(current_detail[1], current_detail[2])
                    current_detail[1].verticalScrollBar().setValue(scroll_position)
        if not source_overlay_compact:
            detail_next.hide()
        update_navigation = getattr(window, "_update_source_detail_navigation", None)
        if update_navigation is not None:
            update_navigation()

    def show_source_detail_overlay():
        nonlocal source_overlay_open
        if not source_overlay_compact:
            details.show()
            sync_detail_header()
            return
        if source_overlay_open:
            # Advancing to another source keeps the panel stationary.
            detail_overlay_animation.stop()
            details.setGeometry(overlay_target())
            details.show()
            details.raise_()
            sync_detail_header()
            return
        if detail_overlay_animation.state() == QtCore.QAbstractAnimation.State.Running:
            detail_overlay_animation.stop()
        target = overlay_target()
        details.setParent(detail_viewport)
        if not details.isVisible():
            details.setGeometry(overlay_offscreen(target))
        else:
            current = details.geometry()
            details.setGeometry(QtCore.QRect(
                current.x(), target.y(), target.width(), target.height()
            ))
        source_overlay_open = True
        window._atlas_source_detail_overlay_open = True
        details.show()
        details.raise_()
        sync_detail_header()
        detail_overlay_animation.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        detail_overlay_animation.setStartValue(details.geometry())
        detail_overlay_animation.setEndValue(target)
        detail_overlay_animation.start()

    def hide_source_detail_overlay(*, immediate=False):
        nonlocal source_overlay_open
        if detail_overlay_animation.state() == QtCore.QAbstractAnimation.State.Running:
            detail_overlay_animation.stop()
        source_overlay_open = False
        window._atlas_source_detail_overlay_open = False
        sync_detail_header()
        if not source_overlay_compact or details.parentWidget() is not detail_viewport:
            return
        target = overlay_target()
        if immediate or not details.isVisible():
            details.hide()
            details.setGeometry(overlay_offscreen(target))
            return
        details.show()
        details.raise_()
        detail_overlay_animation.setEasingCurve(QtCore.QEasingCurve.Type.InOutCubic)
        detail_overlay_animation.setStartValue(details.geometry())
        detail_overlay_animation.setEndValue(overlay_offscreen(target))
        detail_overlay_animation.start()

    def finish_source_detail_animation():
        target = overlay_target()
        if source_overlay_compact and source_overlay_open:
            details.setGeometry(target)
            details.show()
            details.raise_()
        elif source_overlay_compact:
            details.setGeometry(overlay_offscreen(target))
            details.hide()

    detail_overlay_animation.finished.connect(finish_source_detail_animation)
    window._atlas_show_source_detail_overlay = show_source_detail_overlay
    window._atlas_hide_source_detail_overlay = hide_source_detail_overlay
    window._atlas_reset_source_detail_overlay = lambda: hide_source_detail_overlay(
        immediate=True
    )

    class SourceDetailOverlayWatcher(QtCore.QObject):
        def eventFilter(self, watched, event):
            if watched is detail_viewport and event.type() == QtCore.QEvent.Type.Resize:
                target = overlay_target()
                if details.parentWidget() is detail_viewport:
                    if detail_overlay_animation.state() == QtCore.QAbstractAnimation.State.Running:
                        detail_overlay_animation.setEndValue(
                            target if source_overlay_open else overlay_offscreen(target)
                        )
                        current = details.geometry()
                        details.setGeometry(QtCore.QRect(
                            current.x(), target.y(), target.width(), target.height()
                        ))
                    elif source_overlay_open:
                        details.setGeometry(target)
                    else:
                        details.setGeometry(overlay_offscreen(target))
            return False

    source_detail_overlay_watcher = SourceDetailOverlayWatcher(window)
    detail_viewport.installEventFilter(source_detail_overlay_watcher)
    window._atlas_source_detail_overlay_watcher = source_detail_overlay_watcher
    # Secondary metadata stays in the lazy detail; the inventory is readable.
    for column in (2,3,5,8):
        window.source_table.setColumnHidden(column,True)
    header = window.source_table.horizontalHeader()
    header.setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
    header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)
    window.source_table.setColumnWidth(0, 42)
    window.source_table.horizontalHeaderItem(0).setText("#")
    window.source_table.horizontalHeaderItem(6).setText(tr("atlas_source_review", "Review"))
    for column, width in ((4,126),(6,126),(7,120)):
        header.setSectionResizeMode(column,QtWidgets.QHeaderView.ResizeMode.Fixed)
        window.source_table.setColumnWidth(column,width)
    from core.gui.shell.resize import install_table_resize
    install_table_resize(
        window.source_table, QtCore, QtWidgets, stretch_last_section=True
    )
    install_source_badges(window,QtCore,QtGui,QtWidgets,text)
    window.source_table.verticalHeader().setDefaultSectionSize(64)
    window.source_table.setWordWrap(False)
    window.source_table.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
    window.source_table.setShowGrid(False)
    if window._background_loading:
        window.source_table.cellClicked.connect(window._show_source_details)
    window.source_search.addAction(icon(QtCore, QtGui, "search"), QtWidgets.QLineEdit.ActionPosition.LeadingPosition)

    runtime_footer = QtWidgets.QWidget(analysis_panel)
    runtime_footer.setObjectName("analysisFooter")
    footer_layout = QtWidgets.QHBoxLayout(runtime_footer)
    footer_layout.setContentsMargins(0, 8, 0, 8)
    footer_layout.addStretch(1)
    glyph(window.open_parse_review_button, "file")
    window.open_parse_review_button.setMaximumWidth(300)
    footer_layout.addWidget(window.open_parse_review_button)
    window._atlas_runtime_resume.setObjectName("runtimeContinue")
    window._atlas_runtime_resume.setMinimumHeight(40)
    footer_layout.addWidget(window._atlas_runtime_resume)
    window._atlas_runtime_footer_layout = footer_layout
    analysis_panel_layout.addWidget(runtime_footer)
    window._atlas_runtime_footer = runtime_footer
    source_sizes = {QtCore.Qt.Orientation.Horizontal: [670, 550]}

    def reflow_analysis():
        nonlocal source_overlay_open, source_overlay_compact
        width = analysis_scroll.viewport().width()
        # Startup already owns a scrollable form. Let that viewport absorb
        # height changes instead of also scrolling its outer page.
        if not startup_scroll.isHidden():
            main.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
            content.setFixedHeight(analysis_scroll.viewport().height())
        elif main.sizeConstraint() != QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize:
            content.setMinimumHeight(0)
            content.setMaximumHeight(16777215)
            main.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize)
            main.invalidate()
            main.activate()
        compact_page = workspace.width() < 700
        options_drawer = getattr(window, "_atlas_options_drawer", None)
        options_overlay = bool(getattr(window, "_atlas_options_overlay", False))
        docked_drawer_visible = bool(
            options_drawer is not None
            and options_drawer.isVisible()
            and not options_overlay
        )
        page.setContentsMargins(8 if compact_page else 30, 0, 0, 0)
        analysis_panel_layout.setContentsMargins(
            0, 0,
            0 if docked_drawer_visible else (8 if compact_page else 30),
            0,
        )
        main.setContentsMargins(0, 8 if compact_page else 26, 0, 0)
        compact = width < 700
        details_layout.setContentsMargins(
            12 if compact else 24,
            8 if compact else 0,
            12 if compact else 0,
            12 if compact else 0,
        )
        if compact != source_overlay_compact:
            current_detail = getattr(window, "_detail_dialog", None)
            was_open = bool(
                current_detail is not None
                and current_detail[0].isVisible()
            )
            detail_overlay_animation.stop()
            if compact:
                source_sizes[QtCore.Qt.Orientation.Horizontal] = source_splitter.sizes()
                details.hide()
                details.setParent(detail_viewport)
                details.setGeometry(overlay_offscreen())
                source_overlay_compact = True
                window._atlas_source_detail_compact = True
                sync_detail_header()
                if was_open:
                    show_source_detail_overlay()
            else:
                source_overlay_open = False
                window._atlas_source_detail_overlay_open = False
                details.hide()
                details.setParent(source_splitter)
                source_splitter.setOrientation(QtCore.Qt.Orientation.Horizontal)
                source_splitter.addWidget(details)
                source_overlay_compact = False
                window._atlas_source_detail_compact = False
                source_splitter.setStretchFactor(0, 55)
                source_splitter.setStretchFactor(1, 45)
                source_splitter.setSizes(source_sizes.get(
                    QtCore.Qt.Orientation.Horizontal,
                    [max(1, source_splitter.width() * 55 // 100),
                     max(1, source_splitter.width() * 45 // 100)],
                ))
                details.show()
                sync_detail_header()
        elif compact:
            source_overlay_compact = True
            window._atlas_source_detail_compact = True
            sync_detail_header()
            if details.parentWidget() is detail_viewport:
                if source_overlay_open:
                    if detail_overlay_animation.state() != QtCore.QAbstractAnimation.State.Running:
                        details.setGeometry(overlay_target())
                    details.show()
                    details.raise_()
                else:
                    details.setGeometry(overlay_offscreen())
                    details.hide()
        else:
            source_overlay_compact = False
            window._atlas_source_detail_compact = False
            sync_detail_header()
            if details.parentWidget() is not source_splitter:
                details.hide()
                details.setParent(source_splitter)
                source_splitter.setOrientation(QtCore.Qt.Orientation.Horizontal)
                source_splitter.addWidget(details)
                source_splitter.setSizes(source_sizes.get(
                    QtCore.Qt.Orientation.Horizontal, [670, 550]
                ))
            details.show()
        if source_splitter.orientation() != QtCore.Qt.Orientation.Horizontal:
            source_splitter.setOrientation(QtCore.Qt.Orientation.Horizontal)
        rows = ((heading_row, width),
                (actions, min(width, source_pane.width())),
                (footer_layout, width),
                (getattr(window, "_atlas_source_toolbar", None), min(width, source_pane.width())))
        for row, available in rows:
            if row is None:
                continue
            controls = [row.itemAt(index).widget() for index in range(row.count())
                        if row.itemAt(index).widget() is not None
                        and not row.itemAt(index).widget().isHidden()]
            required = sum(control.sizeHint().width() for control in controls)
            required += row.spacing() * max(0, len(controls) - 1)
            row.setDirection(QtWidgets.QBoxLayout.Direction.TopToBottom
                             if required > available else QtWidgets.QBoxLayout.Direction.LeftToRight)

    window._atlas_sync_detail_header = sync_detail_header
    window._atlas_reflow_analysis = reflow_analysis
    drawer = QtWidgets.QFrame(body)
    drawer.setObjectName("optionsDrawer")
    drawer.setMinimumWidth(0)
    drawer.setMaximumWidth(0)
    root = QtWidgets.QVBoxLayout(drawer)
    root.setContentsMargins(24, 26, 0, 22)
    root.setSpacing(20)
    heading = QtWidgets.QHBoxLayout()
    heading.setContentsMargins(0, 0, 24, 0)
    label = QtWidgets.QLabel(text["atlas_options_title"], drawer)
    label.setObjectName("drawerTitle")
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    heading.addWidget(label, 1)
    close = QtWidgets.QToolButton(drawer)
    close.setObjectName("drawerClose")
    glyph(close, "close")
    close.setAccessibleName(text["close"])
    heading.addWidget(close)
    root.addLayout(heading)
    scroll = QtWidgets.QScrollArea(drawer)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content_options = QtWidgets.QWidget(scroll)
    content_options.setObjectName("optionsBody")
    dl = QtWidgets.QVBoxLayout(content_options)
    dl.setContentsMargins(0, 0, 24, 0)
    dl.setSpacing(18)
    dl.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
    scroll.setWidget(content_options)
    root.addWidget(scroll, 1)

    def section(key, fallback):
        line = QtWidgets.QFrame(content_options)
        line.setObjectName("sectionDivider")
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        dl.addWidget(line)
        label = QtWidgets.QLabel(tr(key, fallback), content_options)
        label.setObjectName("sectionTitle")
        dl.addWidget(label)

    def note(key, fallback):
        label = QtWidgets.QLabel(tr(key, fallback), content_options)
        label.setObjectName("mutedText")
        label.setWordWrap(True)
        dl.addWidget(label)
        return label

    section("atlas_options_verify", "Verification")
    severity_row = QtWidgets.QHBoxLayout()
    severity_caption = QtWidgets.QWidget(content_options)
    caption_row = QtWidgets.QHBoxLayout(severity_caption)
    caption_row.setContentsMargins(0, 0, 0, 0)
    severity_label = QtWidgets.QLabel(text["jury2"], severity_caption)
    caption_row.addWidget(severity_label)
    severity_help = QtWidgets.QToolButton(content_options)
    severity_help.setObjectName("inlineHelp")
    glyph(severity_help, "help", 28)
    severity_help.setToolTip(text["setting_CITATION_VERIFIER_VERIFY_JURY2_LEVEL_help"])
    severity_help.setAccessibleName(severity_help.toolTip())
    caption_row.addWidget(severity_help)
    caption_row.addStretch()
    severity_row.addWidget(severity_caption)
    severity = QtWidgets.QHBoxLayout()
    severity.setSpacing(0)
    group = QtWidgets.QButtonGroup(drawer)
    group.setExclusive(True)
    proxies = []
    for button in window.jury_buttons:
        proxy = QtWidgets.QPushButton(button.text(), drawer)
        proxy.setObjectName("severitySegment")
        proxy.setCheckable(True)
        group.addButton(proxy)
        severity.addWidget(proxy)
        proxies.append(proxy)
    severity_row.addLayout(severity, 1)
    dl.addLayout(severity_row)
    note("atlas_severity_hint", "Controls how Jury 2 handles uncertain responses.")
    section("atlas_options_review", "Manual review")
    parse = QtWidgets.QCheckBox(text["atlas_allow_parse_review"], drawer)
    dl.addWidget(parse)
    note("atlas_parse_hint", "Only when citations or references need review.")
    section("atlas_options_fetch", "Source retrieval")
    fetch = QtWidgets.QCheckBox(text["atlas_allow_guided_fetch"], drawer)
    dl.addWidget(fetch)
    note("atlas_fetch_hint", "Automatic retrieval stays active. Incomplete sources remain unverified.")
    section("atlas_options_institution", "Institutional access")
    window.institutional_access_status.setObjectName("institutionalSessionStatus")
    dl.addWidget(window.institutional_access_status)
    dl.addWidget(window.institutional_login_button, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
    note("atlas_institution_hint", "Sign in to multiple publishers using the saved browser session.")
    # The reset is meaningful only after the user has disabled the startup prompt.
    reset_prompt = QtWidgets.QPushButton(text["institutional_prompt_reset"], drawer)
    reset_prompt.setObjectName("institutionalPromptReset")
    reset_prompt.clicked.connect(window._reset_institutional_prompt_preference)
    from .institutional_access import prompt_is_suppressed
    reset_prompt.setVisible(prompt_is_suppressed(QtCore.QSettings("Callimachus", "Callimachus")))
    dl.addWidget(reset_prompt)
    dl.addStretch()
    footer = QtWidgets.QHBoxLayout()
    footer.setContentsMargins(0, 0, 24, 0)
    footer.addStretch()
    cancel = QtWidgets.QPushButton(text["atlas_cancel"], drawer)
    apply = QtWidgets.QPushButton(text["atlas_apply"], drawer)
    apply.setObjectName("primaryButton")
    footer.addWidget(cancel)
    footer.addWidget(apply)
    root.addLayout(footer)
    body.addWidget(drawer)
    body.setStretchFactor(1, 0)
    animation = QtCore.QPropertyAnimation(drawer, b"maximumWidth", drawer)
    animation.setDuration(230)
    animation.setEasingCurve(QtCore.QEasingCurve.Type.InOutCubic)
    overlay_animation = QtCore.QPropertyAnimation(drawer, b"pos", drawer)
    overlay_animation.setDuration(200)
    overlay_animation.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
    overlay_mode = False
    updating_mode = False
    drawer_compact_presentation = None

    def set_drawer_presentation(compact):
        nonlocal drawer_compact_presentation
        compact = bool(compact)
        back_label = text.get("back", "Back") if compact else text.get("close", "Close")
        if compact:
            from core.gui.shell.navigation import set_back_button_presentation
            close.setProperty("atlasIcon", "arrow-left")
            close.setProperty("atlasIconSize", 20)
            close.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
            set_back_button_presentation(
                close, QtCore, QtGui, label=back_label,
                dark=bool(getattr(window, "_dark", False)), active=True,
            )
        else:
            from core.gui.shell.navigation import set_back_button_presentation
            set_back_button_presentation(
                close, QtCore, QtGui, label=back_label,
                dark=bool(getattr(window, "_dark", False)), active=False,
            )
            close.setText("")
            close.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
            glyph(close, "close")
            close.setAccessibleName(back_label)
            close.setToolTip(back_label)
        if compact == drawer_compact_presentation:
            return
        drawer_compact_presentation = compact
        heading.removeWidget(label)
        heading.removeWidget(close)
        if compact:
            root.setContentsMargins(8, 8, 8, 8)
            root.setSpacing(12)
            heading.setContentsMargins(0, 0, 0, 0)
            dl.setContentsMargins(0, 0, 0, 0)
            footer.setContentsMargins(0, 0, 0, 0)
            heading.addWidget(close)
            heading.addWidget(label, 1)
        else:
            root.setContentsMargins(24, 26, 0, 22)
            root.setSpacing(20)
            heading.setContentsMargins(0, 0, 24, 0)
            dl.setContentsMargins(0, 0, 24, 0)
            footer.setContentsMargins(0, 0, 24, 0)
            heading.addWidget(label, 1)
            heading.addWidget(close)
        heading.activate()
        drawer.updateGeometry()

    def minimum_drawer_width():
        scale = max(1.0, window._font_size / 16.0)
        return max(int(400 * scale), severity_caption.minimumSizeHint().width()
                   + severity.minimumSize().width() + severity_row.spacing() + 66)

    def update_drawer_mode():
        nonlocal overlay_mode, updating_mode
        if updating_mode:
            return
        updating_mode = True
        try:
            scale = max(1.0, window._font_size / 16.0)
            needed = max(980 * scale, 540 * scale + minimum_drawer_width())
            narrow = body.width() < needed
            set_drawer_presentation(narrow)
            if narrow != overlay_mode:
                animation.stop()
                overlay_animation.stop()
                if not overlay_mode and drawer.isVisible() and drawer.width() > 0:
                    window._atlas_options_width = drawer.width()
                drawer.hide()
                if narrow:
                    drawer.setParent(workspace)
                    drawer.setMinimumWidth(0)
                    drawer.setMaximumWidth(16777215)
                else:
                    drawer.setParent(body)
                    body.insertWidget(1, drawer)
                    drawer.setMinimumWidth(0)
                    drawer.setMaximumWidth(target_width() if options_button.isChecked() else 0)
                overlay_mode = narrow
                window._atlas_options_overlay = narrow
                if options_button.isChecked():
                    drawer.show()
                    if narrow:
                        drawer.resize(workspace.size())
                        drawer.move(workspace.width(), 0)
                        overlay_animation.setStartValue(drawer.pos())
                        overlay_animation.setEndValue(QtCore.QPoint(0, 0))
                        overlay_animation.start()
                    else:
                        width = target_width()
                        drawer.setMaximumWidth(0)
                        body.setSizes([body.width(), 0])
                        animation.setStartValue(0)
                        animation.setEndValue(width)
                        animation.start()
            if overlay_mode and options_button.isChecked():
                drawer.setMinimumWidth(0)
                drawer.setMaximumWidth(16777215)
                drawer.resize(workspace.size())
                if overlay_animation.state() != QtCore.QAbstractAnimation.State.Running:
                    drawer.move(0, 0)
                drawer.raise_()
            elif overlay_mode and overlay_animation.state() == QtCore.QAbstractAnimation.State.Running:
                drawer.resize(workspace.size())
                overlay_animation.setEndValue(QtCore.QPoint(workspace.width(), 0))
        finally:
            updating_mode = False

    def splitter_contents_width():
        width = int(body.width() or tab.width())
        return max(0, width - body.handleWidth())

    def drawer_resized(width):
        width = max(0, int(width))
        if not overlay_mode and body.width() > 0:
            body.setSizes([max(0, splitter_contents_width() - width), width])
        QtCore.QTimer.singleShot(0, reflow)
        QtCore.QTimer.singleShot(0, reflow_startup)
    animation.valueChanged.connect(drawer_resized)

    def maximum_drawer_width():
        return max(minimum_drawer_width(), splitter_contents_width()
                   - int(540 * max(1.0, window._font_size / 16.0)))

    def target_width():
        available = maximum_drawer_width()
        remembered = getattr(window, "_atlas_options_width", None)
        preferred = int(remembered) if remembered else min(544, max(300, int((body.width() or tab.width())*.40)))
        return min(available, max(minimum_drawer_width(), preferred))

    def reflow():
        update_drawer_mode()
        available_width = drawer.width() if drawer.isVisible() and drawer.width() else target_width()
        severity_row.setDirection(
            QtWidgets.QBoxLayout.Direction.TopToBottom
            if available_width < minimum_drawer_width()
            else QtWidgets.QBoxLayout.Direction.LeftToRight
        )
        for checkbox, key in ((parse,"atlas_allow_parse_review"),(fetch,"atlas_allow_guided_fetch")):
            metrics = checkbox.fontMetrics()
            lines, line = [], ""
            for word in text[key].split():
                candidate = (line+" "+word).strip()
                if line and metrics.horizontalAdvance(candidate) > available_width-78:
                    lines.append(line); line = word
                else:
                    line = candidate
            checkbox.setText("\n".join([*lines,line]))

    def splitter_moved(_position, _index):
        if not overlay_mode and options_button.isChecked() and animation.state() != QtCore.QAbstractAnimation.State.Running:
            window._atlas_options_width = drawer.width()
        reflow()
        reflow_startup()

    body.splitterMoved.connect(splitter_moved)

    def settle_drawer():
        if overlay_mode:
            drawer.setVisible(options_button.isChecked())
            if options_button.isChecked():
                drawer.setMinimumWidth(0)
                drawer.setMaximumWidth(16777215)
                drawer.setGeometry(workspace.rect())
                drawer.raise_()
            else:
                drawer.setMaximumWidth(0)
            reflow()
            return
        if options_button.isChecked():
            width = drawer.width()
            window._atlas_options_width = width
            drawer.setMinimumWidth(min(minimum_drawer_width(), width))
            drawer.setMaximumWidth(max(width, maximum_drawer_width()))
            body.setSizes([max(0, splitter_contents_width() - width), width])
        else:
            drawer.setMinimumWidth(0)
            drawer.setMaximumWidth(0)
            drawer.hide()
            body.setSizes([body.width(), 0])
        reflow()
        reflow_startup()

    animation.finished.connect(settle_drawer)

    def toggle(opened):
        was_visible = drawer.isVisible()
        update_drawer_mode()
        if opened:
            reflow()
            for button, proxy in zip(window.jury_buttons, proxies):
                proxy.setChecked(button.isChecked()); proxy.setEnabled(button.isEnabled())
            parse.setChecked(not window.skip_parse_review.isChecked())
            fetch.setChecked(not window.skip_manual_checks.isChecked())
            drawer.show()
        animation.stop()
        if overlay_mode:
            overlay_animation.stop()
            drawer.setMinimumWidth(0)
            drawer.setMaximumWidth(16777215)
            drawer.resize(workspace.size())
            if opened and not was_visible:
                drawer.move(workspace.width(), 0)
            drawer.show()
            overlay_animation.setStartValue(drawer.pos())
            overlay_animation.setEndValue(QtCore.QPoint(0, 0) if opened
                                         else QtCore.QPoint(workspace.width(), 0))
            overlay_animation.start()
            if opened:
                drawer.raise_()
                close.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
            reflow()
            return
        current_width = drawer.width()
        drawer.setMinimumWidth(0)
        drawer.setMaximumWidth(current_width)
        animation.setStartValue(current_width)
        animation.setEndValue(target_width() if opened else 0)
        animation.start()

    def finish(accepted):
        if accepted:
            for button, proxy in zip(window.jury_buttons, proxies):
                button.setChecked(proxy.isChecked())
            window.skip_parse_review.setChecked(not parse.isChecked())
            window.skip_manual_checks.setChecked(not fetch.isChecked())
        options_button.setChecked(False)

    options_button.toggled.connect(toggle)
    from core.gui.shell.navigation import install_outside_click_dismissal
    window._atlas_options_outside_dismissal = install_outside_click_dismissal(
        drawer, options_button,
        lambda: options_button.setChecked(False), QtCore, QtWidgets,
        active_when=lambda: overlay_mode,
    )
    close.clicked.connect(lambda: finish(False))
    cancel.clicked.connect(lambda: finish(False))
    apply.clicked.connect(lambda: finish(True))
    escape = QtGui.QShortcut(QtGui.QKeySequence("Escape"), drawer)
    escape.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
    escape.activated.connect(lambda: finish(False))
    def overlay_finished():
        if overlay_mode and not options_button.isChecked():
            drawer.hide()
            drawer.setMaximumWidth(0)
            options_button.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        elif overlay_mode:
            drawer.setGeometry(workspace.rect())

    overlay_animation.finished.connect(overlay_finished)
    class OptionsLayoutWatcher(QtCore.QObject):
        def __init__(self):
            super().__init__(workspace)
            self.timer = QtCore.QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(reflow)

        def eventFilter(self, watched, event):
            if event.type() in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Show):
                self.timer.start(0)
            return False

    watcher = OptionsLayoutWatcher()
    body.installEventFilter(watcher)
    workspace.installEventFilter(watcher)
    window._atlas_options_layout_watcher = watcher
    drawer.hide()
    window._atlas_options_animation = animation
    window._atlas_options_overlay_animation = overlay_animation
    window._atlas_options_drawer = drawer
    window._atlas_startup = startup
    window._atlas_runtime = runtime
    window._atlas_sources = source_pane
    window._atlas_source_card = source_pane
    window._atlas_options_splitter = body
    window._atlas_options_button = options_button
    window._atlas_options_controls = {"parse":parse, "fetch":fetch, "severity":proxies}
    window._atlas_reflow_options = reflow
    from core.gui.shell.scroll import install_edge_scrollbar
    window._atlas_analysis_edge_scrollbar = install_edge_scrollbar(
        tab, [analysis_scroll, startup_scroll, scroll], QtCore, QtWidgets,
    )


def update_analysis(window, text):
    if not hasattr(window, "_atlas_startup"):
        return
    from pathlib import Path
    from PySide6 import QtCore, QtGui, QtWidgets
    from .atlas_icons import icon
    operational = bool(window._run_dir or window._process is not None)
    paper = str(getattr(window, "_paper_path", "") or "")
    drop_caption = Path(paper).name if paper else text.get("atlas_drop_title", "Drop your manuscript here")
    if window._atlas_drop_caption.text() != drop_caption:
        window._atlas_drop_caption.setText(drop_caption)
        reflow_startup = getattr(window, "_atlas_reflow_startup", None)
        if reflow_startup is not None:
            reflow_startup()
    window._atlas_start_hint.setText("" if paper else text.get("atlas_start_hint", "Choose a manuscript to begin"))
    latest = getattr(window, "_latest_run_row", None) or next(iter(getattr(window,"_history_rows",())), {})
    window._atlas_resume_hint.setText(str(latest.get("paper") or ""))
    window._atlas_manuscript.setText(Path(paper).name if paper else str(latest.get("paper") or ""))
    window._atlas_manuscript.setToolTip(window._atlas_manuscript.text())
    window._atlas_manuscript.setVisible(operational)
    window._atlas_startup.setVisible(not operational)
    window._atlas_startup_scroll.setVisible(not operational)
    window._atlas_runtime.setVisible(operational)
    window._atlas_runtime_footer.setVisible(operational and not window._current_report_path())
    window._atlas_sources.setVisible(operational)
    window._atlas_source_splitter.setVisible(operational)
    window._atlas_steps.setVisible(operational)
    complete = bool(window._current_report_path())
    window._atlas_progress_card.setVisible(not complete)
    target_actions = window._atlas_runtime_actions if complete else window._atlas_runtime_footer_layout
    if target_actions.indexOf(window.new_analysis_button) < 0:
        previous_actions = (
            window._atlas_runtime_footer_layout
            if complete else window._atlas_runtime_actions
        )
        previous_actions.removeWidget(window.new_analysis_button)
        if complete:
            target_actions.addWidget(window.new_analysis_button)
        else:
            parse_review_index = target_actions.indexOf(window.open_parse_review_button)
            if parse_review_index < 0:
                parse_review_index = target_actions.indexOf(window._atlas_runtime_resume)
            if parse_review_index < 0:
                parse_review_index = target_actions.count()
            target_actions.insertWidget(parse_review_index, window.new_analysis_button)
    if complete:
        window._atlas_runtime_footer_layout.removeWidget(window.open_parse_review_button)
        if window._atlas_runtime_actions.indexOf(window.open_parse_review_button) < 0:
            action_index = window._atlas_runtime_actions.indexOf(window.new_analysis_button)
            if action_index < 0:
                action_index = max(0, window._atlas_runtime_actions.count() - 1)
            window._atlas_runtime_actions.insertWidget(action_index, window.open_parse_review_button)
    else:
        window._atlas_runtime_actions.removeWidget(window.open_parse_review_button)
        if window._atlas_runtime_footer_layout.indexOf(window.open_parse_review_button) < 0:
            control_index = window._atlas_runtime_footer_layout.indexOf(window._atlas_runtime_resume)
            if control_index < 0:
                control_index = window._atlas_runtime_footer_layout.count()
            window._atlas_runtime_footer_layout.insertWidget(control_index, window.open_parse_review_button)
    phase = getattr(window, "_atlas_current_phase", "parse")
    show_results = complete or phase in {"verify", "gaps", "style", "web_research", "report"}
    window.source_table.setColumnHidden(6, show_results)
    window.source_table.setColumnHidden(7, not show_results)
    window._atlas_title.setText(text.get("atlas_complete_title", "Verification complete") if complete else text.get("atlas_running_title", "Manuscript analysis") if operational else text["atlas_new_analysis"])
    current = {"parse":0,"resolve":1,"fetch":2,"verify":3,"gaps":3,"style":3,"web_research":3,"report":4}.get(phase,0)
    for index, label in enumerate(window._atlas_step_labels):
        label.setProperty("active", index == current or complete and index == 4)
        label.style().unpolish(label); label.style().polish(label)
        badge=window._atlas_step_badges[index]
        done=index < current or complete
        badge.setProperty("active", index == current)
        badge.setProperty("done", done)
        if done:
            badge.setPixmap(icon(QtCore,QtGui,"tick","#ffffff",20).pixmap(20,20))
        else:
            badge.setPixmap(QtGui.QPixmap())
            badge.setText(str(index+1))
        badge.style().unpolish(badge);badge.style().polish(badge)
    control = window._atlas_runtime_resume
    control.setProperty("stopping", window._process is not None)
    control.setObjectName("runtimeContinue")
    key = "stop" if window._process is not None else "resume"
    control_color = ("#ff9e99" if window._dark else "#d53030") if window._process is not None else ("#7ab4ff" if window._dark else "#175fe6")
    control.setIcon(icon(QtCore, QtGui, key, control_color, 22))
    control.setIconSize(QtCore.QSize(22, 22))
    window.resume_latest_button.setIcon(icon(QtCore, QtGui, key, control_color, 24))
    if window._process is None:
        window.start_button.setIcon(icon(QtCore, QtGui, "play", "#ffffff", 24))
    control.style().unpolish(control)
    control.style().polish(control)
    window._atlas_runtime_resume.setText(window.resume_latest_button.text() if window._process is not None else text["resume"])
    footer_buttons = tuple(
        button for button in (window.new_analysis_button, window.open_parse_review_button, control)
        if window._atlas_runtime_footer_layout.indexOf(button) >= 0
    )
    for button in footer_buttons:
        button.setMinimumHeight(0)
        button.setMaximumHeight(16777215)
        button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
    footer_height = max(44, *(button.sizeHint().height() for button in footer_buttons))
    for button in footer_buttons:
        button.setFixedHeight(footer_height)
    window._atlas_runtime_resume.setEnabled(window.resume_latest_button.isEnabled() if window._process is not None else bool(window._run_dir and not window._display_run_done and window._guided_window is None and window._parse_review_window is None and window._commands_process is None and window._institutional_launch_request is None and not window._new_analysis_reset_pending))
    window._atlas_runtime_resume.setVisible(operational and not window._current_report_path())
    window.start_button.setEnabled(window.start_button.isEnabled() and bool(getattr(window, "_paper_path", "")))
    if operational and window._atlas_options_button.isChecked():
        window._atlas_options_button.setChecked(False)
    window._atlas_reflow_analysis()
