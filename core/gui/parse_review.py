# core/gui/parse_review.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional PySide6 dialog for explicit, closed-choice manual Parse reviews.

The dialog owns presentation only. Callers provide durable task loading,
validated answer admission, and the continuation action.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from .atlante import stylesheet as atlante_stylesheet


_TEXT = {
    "title": "Callimachus: Parse review",
    "loading": "Loading pending Parse reviews…",
    "load_error": "Could not load pending reviews: {reason}",
    "refresh": "Retry",
    "empty": "No pending Parse reviews.",
    "task": "Review task",
    "target": "Raw target",
    "choice": "Choose an action",
    "candidate": "Choose a candidate",
    "reason": "Optional note",
    "resume": "Resume analysis",
    "close": "Review later",
    "close_tip": "Hide this window and leave analysis paused. Your selections stay in this app session until you resume.",
    "skip_confirm": "{count} pending review(s) have no valid selection or are explicitly unresolved. Record selected decisions, preserve explicit unresolved decisions, skip any missing reviews, and resume?",
    "submit_error": "Could not submit the Parse review decisions: {reason}",
    "error": "Could not resume analysis: {reason}",
    "refresh_error": "Decisions were recorded, but pending reviews could not be refreshed: {reason}",
    "kind_citation": "Citation to reference",
    "kind_inverse": "Reference to claim",
    "kind_footnote": "Footnote sources",
    "kind_identity": "Reference identity",
    "saved": "Saved",
    "saved_answer": "Saved decision",
    "selected_by_user": "Selected by user",
    "saved_source_count": "{count} exact source texts recorded",
    "original_title": "Original title",
    "original_doi": "Original DOI",
    "claim_text": "Cited claim",
    "missing_claim_text": "Claim text is unavailable.",
    "missing_reference_text": "Reference text is unavailable.",
    "missing_candidate_text": "Candidate source text is unavailable.",
    "missing_citation_marker": "Citation marker is unavailable.",
    "missing_footnote_text": "Footnote text is unavailable.",
    "select_reference": "Assign citation to this reference",
    "keep_unresolved": "Keep unresolved",
    "select_claim": "Assign reference to this claim",
    "no_sources": "Record that this note contains no sources",
    "split_sources": "Enter exact separate source texts",
    "keep_ambiguous": "Keep ambiguous",
    "correct_identity": "Correct reference identity",
    "add_source": "Add source field",
    "remove_source": "Remove last source field",
    "title_field": "Correct title",
    "doi_field": "Correct DOI",
    "action_required": "Choose an action before submitting.",
    "candidate_required": "Choose a listed candidate or keep unresolved.",
    "source_required": "Enter at least two non-empty source texts.",
    "identity_required": "Enter a corrected title and/or DOI.",
    "identity_target": "Reference [{number}] · {title}",
    "citation_target": "Citation {marker} · claim {claim}",
    "reference_target": "Reference [{number}] · {entry}",
    "note_target": "Footnote {number} · {note}",
    "candidate_label": "{identifier} · {origin} · {score}",
    "exact_source_field": "Exact source text {number}",
    "review_progress": "Review {current} of {total}",
    "pending": "To review",
    "draft": "Unsaved selection",
    "ready_to_resume": "All decisions saved. Resume analysis when ready.",
    "choices_ready": "All reviews have a choice. Resume analysis to save them and continue.",
    "choices_pending": "{count} review(s) still need a choice. Resume analysis will ask before leaving them unresolved.",
    "guidance_citation": "Check the citation against the listed references. Select one reference, or leave the citation unresolved.",
    "guidance_inverse": "Check whether this reference supports one of the listed claims. Select one claim, or leave the reference unresolved.",
    "guidance_footnote": "Decide whether this footnote contains separate sources. If it does, enter each source exactly as written.",
    "guidance_identity": "Correct the reference title or DOI if you can confirm it, or keep the identity ambiguous.",
    "candidate_references": "References under consideration",
    "candidate_claims": "Claims under consideration",
    "candidate_option": "Option {number}",
    "overview_heading": "Review tasks",
    "back_to_list": "All reviews",
    "back_to_list_tip": "Return to the review list",
    "next_task": "Next",
    "next_task_tip": "Open the next review task",
    "task_selected": "Selected",
    "task_unresolved": "Unresolved",
    "task_status_unknown": "Saved decision needs review",
    "question_citation": "Which source is cited by this sentence?",
    "question_inverse": "Which sentence in the paper cites this reference?",
    "skip_review": "Skip as unresolved",
}


def create_parse_review_window(
    run_dir: str,
    *,
    load_tasks: Callable[[], list[dict[str, Any]]],
    answer_task: Callable[[str, dict[str, Any]], Any],
    skip_remaining: Callable[[], Any],
    on_complete: Callable[[], Any],
    on_dismiss: Callable[[], Any] | None = None,
    review_text: Mapping[str, str] | None = None,
):
    """Create a lazily loaded Parse review dialog; callbacks own all mutations.

    The task list is read when the dialog opens and again after each accepted
    answer. Candidate choices start unselected; the positive action is shown
    first for binary decisions. Closing releases the projected tasks and detail
    widgets so reopening re-reads current durable state.
    """
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6":
            raise RuntimeError("PySide6 is required for Parse review") from exc
        raise

    provided_text = {
        key.removeprefix("parse_review_"): value
        for key, value in (review_text or {}).items()
        if key.startswith("parse_review_")
    }
    if isinstance(review_text, Mapping) and isinstance(review_text.get("close"), str):
        provided_text["close"] = review_text["close"]
    labels = {**_TEXT, **provided_text}

    class LoadSignals(QtCore.QObject):
        finished = QtCore.Signal(int, object, object)

    class LoadTask(QtCore.QRunnable):
        def __init__(self, generation: int):
            super().__init__()
            self.generation = generation
            self.signals = LoadSignals()

        @QtCore.Slot()
        def run(self):
            try:
                tasks = load_tasks()
                if not isinstance(tasks, list):
                    raise ValueError("task loader must return a list")
                allowed_kinds = {
                    "citation_reference_review", "reference_claim_review",
                    "footnote_source_review", "reference_identity_review",
                }
                for task in tasks:
                    if (
                        not isinstance(task, dict)
                        or not isinstance(task.get("task_id"), str)
                        or task.get("review_kind") not in allowed_kinds
                        or task.get("db_status", task.get("status", "pending"))
                        not in {"pending", "answered"}
                    ):
                        raise ValueError("task loader returned an unsupported Parse review task")
                self.signals.finished.emit(self.generation, tasks, None)
            except Exception as exc:  # surfaced in the dialog for operator action
                self.signals.finished.emit(self.generation, None, str(exc))

    class OverviewTaskCard(QtWidgets.QPushButton):
        """Button card with explicit keyboard activation outside dialog defaults."""
        def __init__(self, parent=None):
            super().__init__(parent)
            policy = QtWidgets.QSizePolicy(
                QtWidgets.QSizePolicy.Policy.Preferred,
                QtWidgets.QSizePolicy.Policy.Maximum,
            )
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)
            self._content_fit_pending = False

        def _schedule_content_fit(self):
            if self._content_fit_pending:
                return
            self._content_fit_pending = True
            QtCore.QTimer.singleShot(0, self._fit_wrapped_text)

        def _fit_wrapped_text(self):
            self._content_fit_pending = False
            layout = self.layout()
            content = self.contentsRect()
            if layout is None or content.width() <= 0:
                return
            margins = layout.contentsMargins()
            label_width = max(0, content.width() - margins.left() - margins.right())
            heights = []
            for index in range(layout.count()):
                widget = layout.itemAt(index).widget()
                if widget is None:
                    continue
                required = (
                    widget.heightForWidth(label_width)
                    if widget.hasHeightForWidth() else widget.sizeHint().height()
                )
                heights.append(max(widget.minimumSizeHint().height(), required))
            if not heights:
                return
            outer_insets = max(0, self.height() - content.height())
            needed = (
                margins.top() + margins.bottom() + sum(heights)
                + max(0, layout.spacing()) * (len(heights) - 1)
                + outer_insets + 2
            )
            needed = max(self.minimumSizeHint().height(), needed)
            if self.minimumHeight() != needed:
                self.setMinimumHeight(needed)

        def resizeEvent(self, event):  # noqa: N802 - Qt API name
            super().resizeEvent(event)
            self._schedule_content_fit()

        def changeEvent(self, event):  # noqa: N802 - Qt API name
            super().changeEvent(event)
            if event.type() in {
                QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange,
            }:
                self._schedule_content_fit()

        def hasHeightForWidth(self):  # noqa: N802 - Qt API name
            layout = self.layout()
            return layout is not None and layout.hasHeightForWidth()

        def heightForWidth(self, width):  # noqa: N802 - Qt API name
            layout = self.layout()
            if layout is None or not layout.hasHeightForWidth():
                return super().sizeHint().height()
            content_rect = self.contentsRect()
            horizontal_insets = max(0, self.width() - content_rect.width())
            vertical_insets = max(0, self.height() - content_rect.height())
            content_width = max(0, width - horizontal_insets)
            layout_height = layout.heightForWidth(content_width)
            return max(
                super().sizeHint().height(),
                layout_height + vertical_insets,
            )

        def keyPressEvent(self, event):  # noqa: N802 - Qt API name
            if event.key() in {
                QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter,
            } and not event.isAutoRepeat():
                self.click()
                event.accept()
                return
            super().keyPressEvent(event)

    class CandidateRow(QtWidgets.QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._base_minimum_height = 120
            self._content_fit_pending = False

        def _schedule_content_fit(self):
            if self._content_fit_pending:
                return
            self._content_fit_pending = True
            QtCore.QTimer.singleShot(0, self._fit_wrapped_text)

        def _fit_wrapped_text(self):
            self._content_fit_pending = False
            layout = self.layout()
            description = self.findChild(QtWidgets.QLabel, "parseCandidateDescription")
            if layout is None or description is None or description.width() <= 0:
                return
            margins = layout.contentsMargins()
            content_height = max(0, description.heightForWidth(description.width()))
            radio = self.findChild(QtWidgets.QRadioButton)
            if radio is not None:
                content_height = max(content_height, radio.sizeHint().height())
            required_height = max(
                self._base_minimum_height,
                margins.top() + content_height + margins.bottom(),
            )
            if self.minimumHeight() != required_height:
                self.setMinimumHeight(required_height)

        def resizeEvent(self, event):  # noqa: N802 - Qt API name
            super().resizeEvent(event)
            self._schedule_content_fit()

        def changeEvent(self, event):  # noqa: N802 - Qt API name
            super().changeEvent(event)
            if event.type() in {
                QtCore.QEvent.Type.FontChange,
                QtCore.QEvent.Type.StyleChange,
            }:
                self._schedule_content_fit()

        def mousePressEvent(self, event):  # noqa: N802 - Qt API name
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseReleaseEvent(self, event):  # noqa: N802 - Qt API name
            if (event.button() == QtCore.Qt.MouseButton.LeftButton
                    and self.rect().contains(event.position().toPoint())
                    and self.radio.isEnabled()):
                self.radio.click()
                event.accept()
                return
            super().mouseReleaseEvent(event)

    class CitedPassage(QtWidgets.QLabel):
        """Keep the original plain text while highlighting its literal marker."""
        def paintEvent(self, event):
            from html import escape
            original = escape(self.text())
            marker = escape(str(self.property("citationMarker") or ""))
            if marker:
                original = original.replace(marker, f'<span style="background-color:#ffe7a3;color:#14243b">{marker}</span>')
            document = QtGui.QTextDocument()
            document.setDocumentMargin(0)
            document.setDefaultFont(self.font())
            foreground = self.palette().color(QtGui.QPalette.ColorRole.WindowText).name()
            document.setHtml(f'<div style="color:{foreground}">{original}</div>')
            document.setTextWidth(self.contentsRect().width())
            painter = QtGui.QPainter(self)
            painter.translate(self.contentsRect().topLeft())
            painter.setPen(self.palette().color(QtGui.QPalette.ColorRole.WindowText))
            context = QtGui.QAbstractTextDocumentLayout.PaintContext()
            context.palette = self.palette()
            if self.hasSelectedText():
                selection = QtGui.QAbstractTextDocumentLayout.Selection()
                selection.cursor = QtGui.QTextCursor(document)
                selection.cursor.setPosition(self.selectionStart())
                selection.cursor.setPosition(
                    self.selectionStart()+len(self.selectedText()),
                    QtGui.QTextCursor.MoveMode.KeepAnchor,
                )
                selection.format.setBackground(self.palette().highlight())
                selection.format.setForeground(self.palette().highlightedText())
                context.selections = [selection]
            document.documentLayout().draw(painter, context)

    class ParseReviewWindow(QtWidgets.QDialog):
        def __init__(self):
            super().__init__()
            # Standalone callers own the dialog lifecycle by default. The
            # desktop keeps it alive explicitly so Review later and title-bar
            # close preserve drafts for this app session.
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
            self.setObjectName("parseReviewDialog")
            self.setWindowTitle(labels["title"])
            self.setMinimumSize(480, 430)
            self.resize(980, 680)
            self._run_dir = run_dir
            self._generation = 0
            self._open = False
            self._tasks: list[dict[str, Any]] = []
            self._current: dict[str, Any] | None = None
            self._refresh_after_answer = False
            self._next_task_id: str | None = None
            self._drafts: dict[str, dict[str, Any]] = {}
            self._status_after_load: tuple[str, str] | None = None
            self._compact_source_fit_pending = False
            self._compact_mode = self.width() < 760
            self._compact_detail_open = False
            self._compact_transition_target = False
            self._compact_animation = None
            self._overview_cards: list[Any] = []
            self._overview_focus_task_id: str | None = None
            self._overview_scroll_value = 0
            self._build()
            self._pulse = QtCore.QTimer(self)
            self._pulse.setInterval(450)
            self._pulse.timeout.connect(self._pulse_loading)
            self._pulse_frame = 0

        def _build(self):
            root = QtWidgets.QVBoxLayout(self)
            root.setContentsMargins(0, 16, 0, 0)
            root.setSpacing(14)
            self.workspace = QtWidgets.QWidget(self)
            self.workspace.setObjectName("parseReviewWorkspace")
            self.workspace.installEventFilter(self)
            root.addWidget(self.workspace, 1)

            self.overview_scroll = QtWidgets.QScrollArea(self.workspace)
            self.overview_scroll.setObjectName("parseReviewOverviewScroll")
            self.overview_scroll.setWidgetResizable(True)
            self.overview_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            self.overview_scroll.setHorizontalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self.overview_content = QtWidgets.QWidget(self.overview_scroll)
            self.overview_content.setObjectName("parseReviewOverview")
            self.overview_layout = QtWidgets.QVBoxLayout(self.overview_content)
            self.overview_layout.setContentsMargins(18, 8, 18, 16)
            self.overview_layout.setSpacing(12)
            self.overview_heading = QtWidgets.QLabel(
                labels["overview_heading"], self.overview_content
            )
            self.overview_heading.setObjectName("parseReviewOverviewHeading")
            self.overview_heading.setWordWrap(True)
            self.overview_layout.addWidget(self.overview_heading)
            self.overview_message = QtWidgets.QLabel(
                labels["loading"], self.overview_content
            )
            self.overview_message.setObjectName("parseReviewOverviewMessage")
            self.overview_message.setWordWrap(True)
            self.overview_layout.addWidget(self.overview_message)
            self.overview_layout.addStretch(1)
            self.overview_scroll.setWidget(self.overview_content)

            self.detail_panel = QtWidgets.QWidget(self.workspace)
            self.detail_panel.setObjectName("parseReviewDetailPanel")
            detail_group = QtWidgets.QVBoxLayout(self.detail_panel)
            detail_group.setContentsMargins(18, 0, 18, 0)
            detail_group.setSpacing(10)

            self.compact_navigation = QtWidgets.QWidget(self.detail_panel)
            self.compact_navigation.setObjectName("parseReviewCompactNavigation")
            nav_layout = QtWidgets.QGridLayout(self.compact_navigation)
            nav_layout.setObjectName("parseReviewNavigationGrid")
            nav_layout.setContentsMargins(0, 0, 0, 0)
            nav_layout.setHorizontalSpacing(8)
            nav_layout.setVerticalSpacing(2)
            nav_layout.setColumnStretch(1, 1)
            self._compact_progress_row = -1
            from .atlas_icons import icon
            self.back_button = QtWidgets.QPushButton(
                labels["back_to_list"], self.compact_navigation
            )
            self.back_button.setObjectName("parseReviewBack")
            from core.gui.shell.navigation import set_back_button_presentation
            set_back_button_presentation(
                self.back_button, QtCore, QtGui,
                label=labels["back_to_list"], text=labels["back_to_list"],
            )
            self.back_button.setToolTip(labels["back_to_list_tip"])
            self.back_button.clicked.connect(self._back_to_overview)
            nav_layout.addWidget(
                self.back_button, 0, 0, QtCore.Qt.AlignmentFlag.AlignLeft
            )
            self.compact_progress = QtWidgets.QLabel(
                "", self.compact_navigation
            )
            self.compact_progress.setObjectName("parseReviewProgress")
            self.compact_progress.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignCenter
            )
            self.next_button = QtWidgets.QPushButton(
                labels["next_task"], self.compact_navigation
            )
            self.next_button.setObjectName("parseReviewNext")
            self.next_button.setAccessibleName(labels["next_task"])
            self.next_button.setToolTip(labels["next_task_tip"])
            self.next_button.setIcon(icon(QtCore, QtGui, "arrow-right"))
            self.next_button.clicked.connect(self._next_task)
            nav_layout.addWidget(
                self.next_button, 0, 2, QtCore.Qt.AlignmentFlag.AlignRight
            )
            detail_group.addWidget(self.compact_navigation)

            self.task_tabs = QtWidgets.QTabBar(self.detail_panel)
            self.task_tabs.setObjectName("parseReviewTaskTabs")
            self.task_tabs.setAccessibleName(labels["overview_heading"])
            self.task_tabs.setExpanding(False)
            self.task_tabs.setUsesScrollButtons(True)
            self.task_tabs.setElideMode(QtCore.Qt.TextElideMode.ElideNone)
            self.task_tabs.setTabsClosable(False)
            detail_group.addWidget(self.task_tabs)
            self.decision_banner = QtWidgets.QLabel(
                labels["action_required"], self.detail_panel
            )
            self.decision_banner.setObjectName("parseDecisionBanner")
            self.decision_banner.setWordWrap(True)
            self.decision_banner.hide()
            from .atlas_icons import icon
            warning = QtWidgets.QLabel(self.decision_banner)
            warning.setPixmap(icon(QtCore, QtGui, "warning", "#c9770d", 22).pixmap(22, 22))
            warning.setGeometry(12, 12, 22, 22)
            warning.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            detail_group.addWidget(self.decision_banner)
            scroll = QtWidgets.QScrollArea(self.detail_panel)
            scroll.setObjectName("parseReviewDetailScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self.detail = QtWidgets.QWidget(scroll)
            self.detail.setObjectName("parseReviewDetail")
            detail_policy = self.detail.sizePolicy()
            detail_policy.setHorizontalPolicy(QtWidgets.QSizePolicy.Policy.Ignored)
            self.detail.setSizePolicy(detail_policy)
            self.detail.setMinimumWidth(0)
            self.detail_layout = QtWidgets.QVBoxLayout(self.detail)
            self.detail_layout.setContentsMargins(12, 12, 12, 12)
            self.detail_layout.setSpacing(14)
            scroll.setWidget(self.detail)
            self.task_tabs.addTab(labels["loading"])
            detail_group.addWidget(scroll, 1)
            from core.gui.shell.scroll import install_edge_scrollbar
            self._detail_edge_scrollbar = install_edge_scrollbar(
                self, [self.overview_scroll, scroll], QtCore, QtWidgets,
                compact_when=lambda: self.width() < 760,
            )
            footer = QtWidgets.QFrame(self)
            footer.setObjectName("parseDecisionFooter")
            footer_layout = QtWidgets.QHBoxLayout(footer)
            self._footer_layout = footer_layout
            footer_layout.setContentsMargins(18, 16, 18, 16)
            self.status = QtWidgets.QLabel("", footer)
            self.status.setObjectName("parseReviewStatus")
            self.status.setWordWrap(True)
            footer_layout.addWidget(self.status, 1)
            actions = QtWidgets.QHBoxLayout()
            self._footer_actions_layout = actions
            actions.addStretch()
            self.retry_button = QtWidgets.QPushButton(labels["refresh"], self)
            self.retry_button.clicked.connect(self._load)
            self.retry_button.hide()
            actions.addWidget(self.retry_button)
            self.resume_button = QtWidgets.QPushButton(labels["resume"], self)
            self.resume_button.setObjectName("parseReviewResume")
            self.resume_button.setEnabled(False)
            self.resume_button.clicked.connect(self._resume)
            self.close_button = QtWidgets.QPushButton(labels["close"], self)
            self.close_button.setToolTip(labels["close_tip"])
            from .atlas_icons import icon
            self.resume_button.setIcon(icon(QtCore, QtGui, "play"))
            self.close_button.setIcon(icon(QtCore, QtGui, "close"))
            self.close_button.clicked.connect(self.hide)
            actions.addWidget(self.close_button)
            actions.addWidget(self.resume_button)
            footer_layout.addLayout(actions)
            root.addWidget(footer)
            self._clear_detail()
            self.task_tabs.currentChanged.connect(self._select_task)
            self._reflow_compact_layout()
            self._apply_workspace_mode()

        def eventFilter(self, watched, event):  # noqa: N802 - Qt API name
            if (
                watched is self.workspace
                and event.type() == QtCore.QEvent.Type.Resize
            ):
                if self._compact_animation is not None:
                    self._stop_compact_animation()
                    self._apply_workspace_mode()
                else:
                    self._workspace_resized()
            return super().eventFilter(watched, event)

        def _workspace_resized(self):
            if not hasattr(self, "workspace"):
                return
            rect = self.workspace.rect()
            self.overview_scroll.setGeometry(rect)
            if not self._compact_animation:
                self.detail_panel.setGeometry(rect)
            if hasattr(self, "_detail_edge_scrollbar"):
                self._detail_edge_scrollbar.schedule()

        def _apply_workspace_mode(self):
            self._compact_mode = self.width() < 760
            rect = self.workspace.rect()
            self.overview_scroll.setGeometry(rect)
            self.detail_panel.setGeometry(rect)
            if self._compact_mode and not self._compact_detail_open:
                self.overview_scroll.setEnabled(True)
                self.overview_scroll.show()
                self.overview_scroll.raise_()
                self.detail_panel.hide()
                self.task_tabs.hide()
                self.compact_navigation.hide()
            else:
                self.overview_scroll.setEnabled(False)
                self.overview_scroll.hide()
                self.detail_panel.setEnabled(True)
                self.detail_panel.show()
                self.detail_panel.raise_()
                self.task_tabs.setVisible(not self._compact_mode)
                self.compact_navigation.setVisible(self._compact_mode)
            self._update_compact_navigation()
            if hasattr(self, "_detail_edge_scrollbar"):
                self._detail_edge_scrollbar.schedule()

        def _rebuild_overview(self):
            for card in self._overview_cards:
                self.overview_layout.removeWidget(card)
                card.hide()
                card.deleteLater()
            self._overview_cards = []
            if not self._tasks:
                if self.overview_message.text() == labels["loading"]:
                    self.overview_message.show()
                else:
                    self.overview_message.setText(labels["empty"])
                    self.overview_message.show()
                return
            self.overview_message.hide()
            for index, task in enumerate(self._tasks):
                card = OverviewTaskCard(self.overview_content)
                card.setObjectName("parseReviewTaskCard")
                card.setProperty("taskId", str(task.get("task_id") or ""))
                card.setProperty("taskIndex", index)
                card.setProperty("selected", index == self.task_tabs.currentIndex())
                card.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
                card.setAutoDefault(False)
                card_layout = QtWidgets.QVBoxLayout(card)
                card_layout.setContentsMargins(12, 10, 12, 10)
                card_layout.setSpacing(6)
                title = QtWidgets.QLabel(
                    self._task_name(task, index + 1), card
                )
                title.setObjectName("parseReviewTaskTitle")
                title.setWordWrap(True)
                title.setAttribute(
                    QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents
                )
                context = QtWidgets.QLabel(self._raw_target(task), card)
                context.setObjectName("parseReviewTaskContext")
                context.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                context.setWordWrap(True)
                context.setAttribute(
                    QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents
                )
                state = QtWidgets.QLabel("", card)
                state.setObjectName("parseReviewTaskState")
                state.setWordWrap(True)
                state.setAttribute(
                    QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents
                )
                card_layout.addWidget(title)
                card_layout.addWidget(context)
                card_layout.addWidget(state)
                card.clicked.connect(
                    lambda _checked=False, row=index: self._open_compact_task(row)
                )
                self._overview_layout_insert_card(card)
                self._overview_cards.append(card)
                self._refresh_overview_card(card, task)
            self._update_overview_selection()

        def _overview_layout_insert_card(self, card):
            stretch_index = max(1, self.overview_layout.count() - 1)
            self.overview_layout.insertWidget(stretch_index, card)

        def _refresh_overview_card(self, card, task):
            state = self._task_choice_state(task)
            text = {
                "selected": labels["task_selected"],
                "unresolved": labels["task_unresolved"],
                "unknown": labels["task_status_unknown"],
            }.get(state, labels["pending"])
            card.setProperty("status", state)
            status_label = card.findChild(QtWidgets.QLabel, "parseReviewTaskState")
            if status_label is not None:
                status_label.setText(text)
                status_label.setProperty("status", state)
                status_label.style().unpolish(status_label)
                status_label.style().polish(status_label)
            if isinstance(card, OverviewTaskCard):
                card._schedule_content_fit()
            status_tip = self._task_tooltip(task)
            card.setToolTip(status_tip)
            card.setAccessibleName(f"{self._task_name(task, int(card.property('taskIndex')) + 1)} · {text}")
            card.setAccessibleDescription(self._raw_target(task))
            card.style().unpolish(card)
            card.style().polish(card)

        def _refresh_overview_cards(self, task_id: str | None = None):
            for card in self._overview_cards:
                if task_id is not None and card.property("taskId") != task_id:
                    continue
                index = int(card.property("taskIndex"))
                if 0 <= index < len(self._tasks):
                    self._refresh_overview_card(card, self._tasks[index])
            self._update_overview_selection()

        def _update_overview_selection(self):
            selected = self.task_tabs.currentIndex()
            for card in self._overview_cards:
                active = int(card.property("taskIndex")) == selected
                if card.property("selected") != active:
                    card.setProperty("selected", active)
                    card.style().unpolish(card)
                    card.style().polish(card)

        def _task_choice_state(self, task: Mapping[str, Any]) -> str:
            task_id = str(task.get("task_id") or "")
            target_hash = task.get("target_sha256")
            if self._task_status(task) == "answered":
                answer = task.get("answer")
                if not isinstance(answer, Mapping):
                    return "unknown"
                action = answer.get("action")
                if (
                    not isinstance(target_hash, str) or not target_hash
                    or answer.get("target_sha256") != target_hash
                ):
                    return "unknown"
                if action == "skip_review":
                    return (
                        "unresolved"
                        if isinstance(answer.get("reason"), str)
                        and answer["reason"].strip()
                        else "unknown"
                    )
                draft = {
                    "target_sha256": answer.get("target_sha256"),
                    "action": action,
                    "candidate_id": answer.get(
                        "ref_id" if action == "select_reference"
                        else "claim_id" if action == "select_claim" else ""
                    ),
                    "fields": {
                        key: answer.get(key) for key in ("title", "doi", "reason")
                    },
                    "source_texts": answer.get("source_texts"),
                }
                if self._draft_payload(task, draft) is None:
                    return "unknown"
                if action in {"keep_unresolved", "keep_ambiguous"}:
                    return "unresolved"
                return "selected"
            if self._task_status(task) != "pending":
                return "unknown"
            draft = self._drafts.get(task_id)
            if draft is None or not isinstance(target_hash, str) or not target_hash:
                return "pending"
            if draft.get("target_sha256") != target_hash:
                return "pending"
            payload = self._draft_payload(task, draft)
            if payload is None:
                return "pending"
            if payload.get("action") in {"keep_unresolved", "keep_ambiguous"}:
                return "unresolved"
            return "selected"

        def _update_compact_navigation(self):
            row = self.task_tabs.currentIndex() if hasattr(self, "task_tabs") else -1
            total = len(self._tasks)
            if 0 <= row < total:
                self.compact_progress.setText(labels["review_progress"].format(
                    current=row + 1, total=total
                ))
                self.next_button.setEnabled(row < total - 1)
            else:
                self.compact_progress.setText("")
                self.next_button.setEnabled(False)
            if not hasattr(self, "compact_navigation"):
                return
            layout = self.compact_navigation.layout()
            layout.activate()
            self.back_button.setMinimumWidth(0)
            self.next_button.setMinimumWidth(0)
            back_width = self.back_button.sizeHint().width()
            next_width = self.next_button.sizeHint().width()
            self.back_button.setMinimumWidth(back_width)
            self.next_button.setMinimumWidth(next_width)
            progress_width = self.compact_progress.sizeHint().width()
            needed_width = (
                back_width + next_width + progress_width
                + 2 * layout.horizontalSpacing()
            )
            available_width = self.compact_navigation.width()
            if back_width + next_width + layout.horizontalSpacing() > available_width:
                progress_row = 2
            else:
                progress_row = 0 if needed_width <= available_width else 1
            if self._compact_progress_row != progress_row:
                for widget in (self.back_button, self.next_button, self.compact_progress):
                    layout.removeWidget(widget)
                if progress_row == 2:
                    layout.addWidget(
                        self.back_button, 0, 0, 1, 3,
                        QtCore.Qt.AlignmentFlag.AlignLeft,
                    )
                    layout.addWidget(
                        self.next_button, 1, 0, 1, 3,
                        QtCore.Qt.AlignmentFlag.AlignRight,
                    )
                else:
                    layout.addWidget(
                        self.back_button, 0, 0, QtCore.Qt.AlignmentFlag.AlignLeft,
                    )
                    layout.addWidget(
                        self.next_button, 0, 2, QtCore.Qt.AlignmentFlag.AlignRight,
                    )
                if progress_row == 0:
                    layout.addWidget(
                        self.compact_progress, 0, 1,
                        QtCore.Qt.AlignmentFlag.AlignCenter,
                    )
                else:
                    layout.addWidget(
                        self.compact_progress, progress_row, 0, 1, 3,
                        QtCore.Qt.AlignmentFlag.AlignHCenter,
                    )
                self._compact_progress_row = progress_row
                layout.activate()

        def _open_compact_task(self, row: int):
            if not self._compact_mode or row < 0 or row >= len(self._tasks):
                return
            if not self._compact_detail_open:
                self._overview_focus_task_id = str(
                    self._tasks[row].get("task_id") or ""
                )
                self._overview_scroll_value = (
                    self.overview_scroll.verticalScrollBar().value()
                )
            if self.task_tabs.currentIndex() != row:
                self.task_tabs.setCurrentIndex(row)
            elif self._current is None:
                self._select_task(row)
            self._update_compact_navigation()
            if self._compact_detail_open:
                return
            self._compact_transition_target = True
            self.overview_scroll.setEnabled(False)
            self.overview_scroll.hide()
            self.task_tabs.hide()
            self.compact_navigation.show()
            self.detail_panel.setEnabled(True)
            self.detail_panel.show()
            self.detail_panel.raise_()
            self.back_button.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
            self._compact_detail_open = True
            self._animate_compact_detail(opening=True)

        def _back_to_overview(self):
            if not self._compact_mode or not self._compact_detail_open:
                return
            self._remember_draft()
            self._compact_transition_target = False
            self._compact_detail_open = False
            self._animate_compact_detail(opening=False)

        def _next_task(self):
            if not self._compact_mode:
                return
            row = self.task_tabs.currentIndex()
            if row < 0 or row + 1 >= len(self._tasks):
                self._update_compact_navigation()
                return
            self._remember_draft()
            self._overview_focus_task_id = str(
                self._tasks[row + 1].get("task_id") or ""
            )
            self._open_compact_task(row + 1)

        def _animate_compact_detail(self, *, opening: bool):
            was_animating = self._compact_animation is not None
            current_rect = self.detail_panel.geometry()
            if self._compact_animation is not None:
                self._stop_compact_animation()
            rect = self.workspace.rect()
            offscreen = QtCore.QRect(
                rect.right() + 1, rect.top(), rect.width(), rect.height()
            )
            if was_animating:
                start = current_rect
            elif opening:
                start = offscreen
            else:
                start = rect
            end = (
                rect
                if opening else
                offscreen
            )
            if rect.width() <= 0 or rect.height() <= 0:
                self._compact_detail_open = self._compact_transition_target
                self._apply_workspace_mode()
                if self._compact_detail_open:
                    self._schedule_compact_source_fit()
                else:
                    self._restore_overview_focus()
                return
            animation = QtCore.QPropertyAnimation(
                self.detail_panel, b"geometry", self
            )
            animation.setDuration(200)
            animation.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
            animation.setStartValue(start)
            animation.setEndValue(end)
            animation.finished.connect(
                lambda active=animation: self._finish_compact_animation(active)
            )
            self._compact_animation = animation
            self.detail_panel.setGeometry(start)
            animation.start()

        def _finish_compact_animation(self, animation):
            if animation is not self._compact_animation:
                return
            self._compact_animation = None
            animation.deleteLater()
            self._compact_detail_open = self._compact_transition_target
            self._apply_workspace_mode()
            if self._compact_detail_open:
                self._schedule_compact_source_fit()
            else:
                self._restore_overview_focus()

        def _stop_compact_animation(self):
            animation = self._compact_animation
            if animation is None:
                return
            self._compact_animation = None
            animation.stop()
            animation.deleteLater()
            self._compact_detail_open = self._compact_transition_target

        def _restore_overview_focus(self):
            self.overview_scroll.setEnabled(True)
            self.overview_scroll.show()
            self.overview_scroll.raise_()
            self.overview_scroll.verticalScrollBar().setValue(
                self._overview_scroll_value
            )
            card = next((
                item for item in self._overview_cards
                if item.property("taskId") == self._overview_focus_task_id
            ), None)
            if card is not None:
                card.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

        def _reflow_compact_layout(self):
            compact = self.width() < 760
            self._compact_mode = compact
            if getattr(self, "_atlas_embedded", False):
                self.layout().setContentsMargins(
                    *((8, 12, 8, 0) if compact else (30, 26, 30, 0))
                )
            self.overview_layout.setContentsMargins(
                *((0, 8, 0, 16) if compact else (18, 8, 18, 16))
            )
            self.detail_panel.layout().setContentsMargins(
                *((0, 0, 0, 0) if compact else (18, 0, 18, 0))
            )
            self.detail_layout.setContentsMargins(
                *((0, 12, 0, 12) if compact else (12, 12, 12, 12))
            )
            self._footer_layout.setContentsMargins(
                *((8, 12, 8, 12) if compact else (18, 16, 18, 16))
            )
            direction = (
                QtWidgets.QBoxLayout.Direction.TopToBottom
                if compact else QtWidgets.QBoxLayout.Direction.LeftToRight
            )
            self._footer_layout.setDirection(direction)
            action_width = (
                self.close_button.sizeHint().width()
                + self.resume_button.sizeHint().width()
                + self._footer_actions_layout.spacing()
            )
            available = max(0, self.width() - 36)
            self._footer_actions_layout.setDirection(
                QtWidgets.QBoxLayout.Direction.LeftToRight
                if action_width <= available
                else QtWidgets.QBoxLayout.Direction.TopToBottom
            )
            for row in getattr(self, "_field_rows", {}).values():
                row.setDirection(direction)
            for container in self.findChildren(QtWidgets.QWidget, "parseReviewBinaryActions"):
                row = container.layout()
                if row is not None:
                    row.setDirection(direction)
            for splitter in self.findChildren(QtWidgets.QSplitter, "parseAssignmentColumns"):
                orientation = (
                    QtCore.Qt.Orientation.Vertical if compact
                    else QtCore.Qt.Orientation.Horizontal
                )
                orientation_changed = splitter.orientation() != orientation
                first_setup = splitter.property("parseCompactLayout") is None
                if orientation_changed:
                    splitter.setOrientation(orientation)
                if orientation_changed or first_setup:
                    left = splitter.widget(0)
                    right = splitter.widget(1)
                    left.layout().setContentsMargins(0, 12, 0 if compact else 24, 18)
                    right.layout().setContentsMargins(0 if compact else 26, 12, 0, 0)
                    left.setStyleSheet(
                        "QFrame#parseAssignmentTarget { border: 0; }" if compact else ""
                    )
                    if compact:
                        # In portrait mode the original passage should take
                        # only its natural wrapped height; the alternatives
                        # and decision controls use the remaining scroll area.
                        left.setMinimumHeight(0)
                        splitter.setStretchFactor(0, 0)
                        splitter.setStretchFactor(1, 1)
                        splitter.setSizes([
                            0,
                            max(1, splitter.height()),
                        ])
                    else:
                        target = left.findChild(
                            QtWidgets.QWidget, "parseReviewOriginalText"
                        )
                        if target is not None:
                            for label in target.findChildren(QtWidgets.QLabel):
                                if label.objectName() != "parseReviewSectionHeading":
                                    label.setMinimumHeight(0)
                        left.setMinimumHeight(340)
                        splitter.setStretchFactor(0, 1)
                        splitter.setStretchFactor(1, 1)
                        splitter.setSizes([440, 440])
                    splitter.setProperty("parseCompactLayout", compact)
            if compact:
                self._schedule_compact_source_fit()
            self._update_compact_navigation()

        def _schedule_compact_source_fit(self):
            if self._compact_source_fit_pending:
                return
            self._compact_source_fit_pending = True
            QtCore.QTimer.singleShot(0, self._fit_compact_assignment_targets)

        def _fit_compact_assignment_targets(self):
            self._compact_source_fit_pending = False
            if self.width() >= 760:
                return
            self.detail_layout.invalidate()
            self.detail_layout.activate()
            for splitter in self.findChildren(
                QtWidgets.QSplitter, "parseAssignmentColumns"
            ):
                if splitter.orientation() != QtCore.Qt.Orientation.Vertical:
                    continue
                left = splitter.widget(0)
                target = left.findChild(
                    QtWidgets.QWidget, "parseReviewOriginalText"
                )
                left_layout = left.layout()
                target_layout = target.layout() if target is not None else None
                if target is None or left_layout is None or target_layout is None:
                    continue

                # A prior narrow width may have left a tall label minimum.
                # Clear it before measuring the settled child width again.
                left.setMinimumHeight(0)
                source_labels = [
                    label for label in target.findChildren(QtWidgets.QLabel)
                    if label.objectName() != "parseReviewSectionHeading"
                ]
                for label in source_labels:
                    label.setMinimumHeight(0)
                target_layout.invalidate()
                target_layout.activate()
                left_layout.invalidate()
                left_layout.activate()

                widths_ready = True
                for label in source_labels:
                    width = label.width()
                    if width <= 0:
                        widths_ready = False
                        continue
                    required_height = (
                        label.heightForWidth(width)
                        if label.hasHeightForWidth()
                        else label.sizeHint().height()
                    )
                    label.setMinimumHeight(max(0, required_height))
                if not widths_ready:
                    continue

                target_layout.invalidate()
                target_layout.activate()
                left_layout.invalidate()
                left_layout.activate()
                required_card_height = max(
                    left.minimumSizeHint().height(),
                    left_layout.sizeHint().height(),
                )
                left.setMinimumHeight(required_card_height)
                left.updateGeometry()
                splitter.updateGeometry()

        def resizeEvent(self, event):  # noqa: N802 - Qt API name
            super().resizeEvent(event)
            if self._compact_animation is not None:
                self._stop_compact_animation()
            self._apply_workspace_mode()
            self._reflow_compact_layout()

        def _set_status(self, message: str, kind: str = "error"):
            self.status.setText(message)
            self.status.setProperty("kind", kind)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)

        def _set_resume_ready(self, ready: bool, *, allow_incomplete: bool = False):
            self.resume_button.setEnabled(ready or allow_incomplete)
            self.resume_button.setProperty("ready", ready)
            self.resume_button.style().unpolish(self.resume_button)
            self.resume_button.style().polish(self.resume_button)

        def _update_review_status(self):
            pending = [
                task for task in self._tasks
                if self._task_status(task) == "pending"
            ]
            if not pending:
                self._set_status(labels["ready_to_resume"], "success")
                self._set_resume_ready(True)
                return
            missing = 0
            for task in pending:
                draft = self._drafts.get(str(task.get("task_id") or ""))
                if (
                    draft is None
                    or draft.get("target_sha256") != task.get("target_sha256")
                    or self._draft_payload(task, draft) is None
                ):
                    missing += 1
            if missing:
                self._set_status(labels["choices_pending"].format(count=missing), "info")
                self._set_resume_ready(False, allow_incomplete=True)
            else:
                self._set_status(labels["choices_ready"], "success")
                self._set_resume_ready(True)

        def showEvent(self, event):  # noqa: N802 - Qt API name
            super().showEvent(event)
            if not self._open:
                self._open = True
                self._compact_detail_open = False
                self._compact_transition_target = False
                self._apply_workspace_mode()
                QtCore.QTimer.singleShot(0, self._load)

        def _pulse_loading(self):
            self._pulse_frame = (self._pulse_frame + 1) % 4
            if self.task_tabs.count():
                self.task_tabs.setTabText(0, labels["loading"] + " ·" * self._pulse_frame)

        def _clear_tabs(self):
            while self.task_tabs.count():
                self.task_tabs.removeTab(0)

        def _load(self):
            self._generation += 1
            generation = self._generation
            self._tasks = []
            self._current = None
            self.overview_message.setText(labels["loading"])
            self.overview_message.show()
            self._rebuild_overview()
            if self.width() < 760:
                self._compact_detail_open = False
                self._compact_transition_target = False
                if self._compact_animation is not None:
                    self._stop_compact_animation()
                self._apply_workspace_mode()
            self._set_status("", "info")
            self.retry_button.hide()
            self._set_resume_ready(False)
            self._clear_detail()
            self._clear_tabs()
            self.task_tabs.addTab(labels["loading"])
            self._pulse.start()
            worker = LoadTask(generation)
            worker.signals.finished.connect(self._loaded)
            # Keep the QRunnable alive until its completion signal is handled.
            self._worker = worker
            QtCore.QThreadPool.globalInstance().start(worker)

        @QtCore.Slot(int, object, object)
        def _loaded(self, generation: int, tasks: Any, error: Any):
            if generation != self._generation or not self._open:
                return
            self._pulse.stop()
            self._worker = None
            self._clear_tabs()
            if error is not None:
                key = "refresh_error" if self._refresh_after_answer else "load_error"
                message = labels[key].format(reason=error)
                self._set_status(message)
                self.overview_message.setText(message)
                self.overview_message.show()
                self.retry_button.show()
                return
            self._refresh_after_answer = False
            self._tasks = list(tasks)
            current_targets = {
                str(task["task_id"]): task.get("target_sha256")
                for task in self._tasks if self._task_status(task) == "pending"
            }
            self._drafts = {
                task_id: draft for task_id, draft in self._drafts.items()
                if task_id in current_targets
                and current_targets[task_id] == draft["target_sha256"]
            }
            if not self._tasks:
                self.task_tabs.addTab(labels["empty"])
                self._clear_detail()
                self.overview_message.setText(labels["empty"])
                self._rebuild_overview()
                self._update_review_status()
                self._show_status_after_load()
                self._update_compact_navigation()
                return
            for index, task in enumerate(self._tasks, start=1):
                tab = self.task_tabs.addTab(self._task_label(task, index))
                self.task_tabs.setTabToolTip(tab, self._task_tooltip(task))
            next_index = next((
                index for index, task in enumerate(self._tasks)
                if task.get("task_id") == self._next_task_id
            ), None)
            if next_index is not None:
                self.task_tabs.setCurrentIndex(next_index)
            else:
                self.task_tabs.setCurrentIndex(next((
                    index for index, task in enumerate(self._tasks)
                    if self._task_status(task) == "pending"
                ), 0))
            self._next_task_id = None
            self._rebuild_overview()
            self._refresh_overview_cards()
            self._update_review_status()
            self._update_compact_navigation()
            self._show_status_after_load()

        def _show_status_after_load(self):
            if self._status_after_load is not None:
                message, kind = self._status_after_load
                self._status_after_load = None
                self._set_status(message, kind)

        def _task_name(self, task: Mapping[str, Any], index: int) -> str:
            kinds = {
                "citation_reference_review": "kind_citation",
                "reference_claim_review": "kind_inverse",
                "footnote_source_review": "kind_footnote",
                "reference_identity_review": "kind_identity",
            }
            kind = labels.get(kinds.get(str(task.get("review_kind")), "task"), labels["task"])
            number = task.get("ref_number", task.get("note_number"))
            suffix = f" [{number}]" if number is not None else ""
            return f"{index}. {kind}{suffix}"

        def _task_label(self, task: Mapping[str, Any], index: int) -> str:
            marker = " ✓" if self._task_status(task) == "answered" else (
                " *" if str(task.get("task_id")) in self._drafts else ""
            )
            return self._task_name(task, index) + marker

        def _task_tooltip(self, task: Mapping[str, Any]) -> str:
            choice_state = self._task_choice_state(task)
            if choice_state == "unknown":
                status = labels["task_status_unknown"]
            elif self._task_status(task) == "answered":
                status = labels["saved"]
            elif str(task.get("task_id") or "") in self._drafts:
                status = labels["draft"]
            else:
                status = labels["pending"]
            return f"{status} · {self._raw_target(task)}"

        def _remember_draft(self):
            task = self._current
            if task is None or self._task_status(task) != "pending":
                return
            task_id = str(task["task_id"])
            candidate = self._candidate_group.checkedButton() if self._candidate_group else None
            draft = {
                "target_sha256": task.get("target_sha256"),
                "action": self._selected_action(),
                "candidate_id": candidate.property("candidate_id") if candidate else None,
                "fields": {key: field.text() for key, field in self._fields.items()},
                "source_texts": [field.toPlainText() for field in self._source_fields],
            }
            default_action = {
                "citation_reference_review": "select_reference",
                "reference_claim_review": "select_claim",
                "reference_identity_review": "correct_identity",
            }.get(task.get("review_kind"))
            changed = (
                draft["action"] != default_action or draft["candidate_id"] is not None
                or any(draft["fields"].values()) or any(draft["source_texts"])
                or len(draft["source_texts"]) > 2
            )
            if changed:
                self._drafts[task_id] = draft
            else:
                self._drafts.pop(task_id, None)
            index = next((i for i, item in enumerate(self._tasks)
                          if item["task_id"] == task_id), None)
            if index is not None and index < self.task_tabs.count():
                self.task_tabs.setTabText(index, self._task_label(task, index + 1))
                self.task_tabs.setTabToolTip(index, self._task_tooltip(task))
            self._refresh_overview_cards(task_id)
            self._update_review_status()

        def _restore_draft(self, task: Mapping[str, Any]):
            draft = self._drafts.get(str(task["task_id"]))
            if draft is None or draft["target_sha256"] != task.get("target_sha256"):
                return
            button = self._action_buttons.get(draft["action"])
            if button is not None:
                button.setChecked(True)
            if self._candidate_group is not None:
                for candidate in self._candidate_group.buttons():
                    if candidate.property("candidate_id") == draft["candidate_id"]:
                        candidate.setChecked(True)
                        break
            for key, value in draft["fields"].items():
                if key in self._fields:
                    self._fields[key].setText(value)
            if self._source_box is not None:
                while len(self._source_fields) < len(draft["source_texts"]):
                    self._add_source_field()
                for field, value in zip(self._source_fields, draft["source_texts"]):
                    field.setPlainText(value)

        @staticmethod
        def _task_status(task: Mapping[str, Any]) -> str:
            return str(task.get("db_status", task.get("status", "pending")))

        def _select_task(self, row: int):
            self._remember_draft()
            if row < 0 or row >= len(self._tasks):
                self._current = None
                self._clear_detail()
                self._update_compact_navigation()
                return
            self._current = self._tasks[row]
            self._current_is_pending = self._task_status(self._current) == "pending"
            self._render_detail(self._current)
            self._reflow_compact_layout()
            self._update_overview_selection()
            self._update_compact_navigation()
            self._update_review_status()

        def _clear_detail(self):
            def clear_layout(layout):
                while layout.count():
                    item = layout.takeAt(0)
                    widget = item.widget()
                    if widget is not None:
                        widget.hide()
                        widget.deleteLater()
                    elif item.layout() is not None:
                        child = item.layout()
                        clear_layout(child)
                        child.deleteLater()

            clear_layout(self.detail_layout)
            self._fields = {}
            self._field_rows = {}
            self._source_fields = []
            self._source_box = None
            self._add_source_button = None
            self._remove_source_button = None
            if getattr(self, "_action_group", None) is not None:
                self._action_group.deleteLater()
            if getattr(self, "_candidate_group", None) is not None:
                self._candidate_group.deleteLater()
            self._action_group = None
            self._action_buttons = {}
            self._candidate_group = None
            self._candidate_container = None
            self._candidate_target_field = None

        def _render_detail(self, task: Mapping[str, Any]):
            self._clear_detail()
            kind = task.get("review_kind")
            is_assignment = kind in {
                "citation_reference_review", "reference_claim_review",
            }
            self.decision_banner.setVisible(is_assignment and self._current_is_pending)
            row = self.task_tabs.currentIndex()
            if not is_assignment or not self._current_is_pending:
                progress = QtWidgets.QLabel(labels["review_progress"].format(
                    current=row + 1, total=len(self._tasks)
                ), self.detail)
                progress.setObjectName("parseReviewProgress")
                self.detail_layout.addWidget(progress)
                title = QtWidgets.QLabel(self._task_name(task, row + 1), self.detail)
                title.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                title.setWordWrap(True)
                title.setObjectName("parseReviewHeading")
                self.detail_layout.addWidget(title)
            if self._current_is_pending and not is_assignment:
                guidance_key = {
                    "citation_reference_review": "guidance_citation",
                    "reference_claim_review": "guidance_inverse",
                    "footnote_source_review": "guidance_footnote",
                    "reference_identity_review": "guidance_identity",
                }.get(kind)
                if guidance_key:
                    guidance = QtWidgets.QLabel(labels[guidance_key], self.detail)
                    guidance.setObjectName("parseReviewGuidance")
                    guidance.setWordWrap(True)
                    self.detail_layout.addWidget(guidance)
            if is_assignment:
                question_key = (
                    "question_citation" if kind == "citation_reference_review"
                    else "question_inverse"
                )
                question = QtWidgets.QLabel(labels[question_key], self.detail)
                question.setObjectName("parseReviewAssignmentQuestion")
                question.setWordWrap(True)
                self.detail_layout.addWidget(question)
            if is_assignment:
                target = self._assignment_target(task)
            else:
                raw = self._raw_target(task)
                target = QtWidgets.QLabel(f"{labels['target']}: {raw}", self.detail)
                target.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                target.setObjectName("parseReviewTarget")
                target.setWordWrap(True)
                target.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            self.detail_layout.addWidget(target)
            if not self._current_is_pending:
                saved_title = QtWidgets.QLabel(labels["saved_answer"], self.detail)
                saved_title.setObjectName("parseReviewSavedHeading")
                self.detail_layout.addWidget(saved_title)
                summary = QtWidgets.QLabel(
                    self._saved_answer_summary(task), self.detail
                )
                summary.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                summary.setObjectName("parseReviewSavedAnswer")
                summary.setWordWrap(True)
                summary.setTextInteractionFlags(
                    QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
                )
                self.detail_layout.addWidget(summary)
                self.detail_layout.addStretch(1)
                return
            decision_label = QtWidgets.QLabel(
                labels["candidate_references"] if kind == "citation_reference_review" else labels["choice"], self.detail
            )
            decision_label.setObjectName("parseReviewSectionHeading")
            decision_label.setWordWrap(True)
            self.detail_layout.addWidget(decision_label)
            self._action_group = QtWidgets.QButtonGroup(self.detail)
            self._action_group.setExclusive(True)
            self._action_group.buttonClicked.connect(self._action_changed)
            if kind == "citation_reference_review":
                self._binary_actions(("select_reference", "keep_unresolved"))
                self._candidate_picker(task, "ref")
            elif kind == "reference_claim_review":
                self._binary_actions(("select_claim", "keep_unresolved"))
                self._candidate_picker(task, "claim")
            elif kind == "footnote_source_review":
                options = QtWidgets.QWidget(self.detail)
                options_layout = QtWidgets.QVBoxLayout(options)
                options_layout.setContentsMargins(0, 0, 0, 0)
                for action in ("no_sources", "split_sources", "keep_ambiguous"):
                    button = QtWidgets.QRadioButton(labels[action], options)
                    button.setObjectName(f"parseReviewAction_{action}")
                    button.setProperty("action", action)
                    options_layout.addWidget(button)
                    self._action_group.addButton(button)
                    self._action_buttons[action] = button
                self.detail_layout.addWidget(options)
                self._source_box = QtWidgets.QWidget(self.detail)
                self._source_layout = QtWidgets.QVBoxLayout(self._source_box)
                self._source_layout.setContentsMargins(0, 0, 0, 0)
                self._source_fields = []
                self._add_source_field()
                self._add_source_field()
                row = QtWidgets.QHBoxLayout()
                self._add_source_button = QtWidgets.QPushButton(labels["add_source"], self.detail)
                self._add_source_button.clicked.connect(self._add_source_field)
                row.addWidget(self._add_source_button)
                self._remove_source_button = QtWidgets.QPushButton(labels["remove_source"], self.detail)
                self._remove_source_button.clicked.connect(self._remove_source_field)
                row.addWidget(self._remove_source_button)
                row.addStretch()
                self._source_layout.addLayout(row)
                self._update_source_buttons()
                self.detail_layout.addWidget(self._source_box)
            elif kind == "reference_identity_review":
                self._binary_actions(("correct_identity", "keep_ambiguous"))
                self._readonly_value("original_title", task.get("title"))
                self._readonly_value("original_doi", task.get("doi"))
                self._fields["title"] = self._line_field("title_field", None)
                self._fields["doi"] = self._line_field("doi_field", None)
            else:
                self._set_status(labels["load_error"].format(
                    reason=f"unsupported review kind: {kind}"
                ))
            self._fields["reason"] = self._line_field("reason", None)
            self._restore_draft(task)
            self._action_changed()
            self.detail_layout.addStretch(1)
            if kind in {"citation_reference_review", "reference_claim_review"}:
                self._arrange_assignment_columns(target, decision_label)

        def _arrange_assignment_columns(self, target, decision_label):
            """Keep the cited passage and alternative works side by side."""
            split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self.detail)
            split.setObjectName("parseAssignmentColumns")
            split.setChildrenCollapsible(False)
            split.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
            split.setMinimumWidth(0)
            left = QtWidgets.QFrame(split)
            left.setObjectName("parseAssignmentTarget")
            left.setMinimumHeight(340)
            left_layout = QtWidgets.QVBoxLayout(left)
            left_layout.setContentsMargins(0, 12, 24, 18)
            left_layout.setSpacing(12)
            self.detail_layout.removeWidget(target)
            left_layout.addWidget(target)
            right = QtWidgets.QWidget(split)
            right_layout = QtWidgets.QVBoxLayout(right)
            right_layout.setContentsMargins(26, 12, 0, 0)
            right_layout.setSpacing(16)
            self.detail_layout.removeWidget(decision_label)
            right_layout.addWidget(decision_label)
            candidate_heading = self._candidate_container.findChild(QtWidgets.QLabel, "parseReviewSectionHeading")
            if candidate_heading is not None:
                candidate_heading.hide()
            self.detail_layout.removeWidget(self._candidate_container)
            right_layout.addWidget(self._candidate_container)
            choice = next(iter(self._action_buttons.values())).parentWidget()
            self.detail_layout.removeWidget(choice)
            right_layout.addWidget(choice)
            reason_row = self._field_rows.get("reason")
            if reason_row is not None:
                self.detail_layout.removeItem(reason_row)
                right_layout.addLayout(reason_row)
            right_layout.addStretch()
            split.setStretchFactor(0,1)
            split.setStretchFactor(1,1)
            split.setSizes([440,440])
            self.detail_layout.insertWidget(self.detail_layout.count()-1, split,1)
            self._reflow_compact_layout()
            split.show()

        def _action_changed(self, *_args):
            action = self._selected_action()
            if self._candidate_container is not None:
                # The candidate cards remain available so choosing one after
                # "Keep unresolved" naturally returns to assignment mode.
                self._candidate_container.setEnabled(True)
                if action == "keep_unresolved" and self._candidate_group is not None:
                    selected = self._candidate_group.checkedButton()
                    if selected is not None:
                        self._candidate_group.setExclusive(False)
                        selected.setChecked(False)
                        self._candidate_group.setExclusive(True)
            if self._source_box is not None:
                self._source_box.setVisible(action == "split_sources")
            if "title" in self._fields:
                self._fields["title"].setEnabled(action == "correct_identity")
                self._fields["doi"].setEnabled(action == "correct_identity")
            self._remember_draft()

        def _selected_action(self) -> str | None:
            button = self._action_group.checkedButton() if self._action_group else None
            return button.property("action") if button is not None else None

        def _binary_actions(self, actions: tuple[str, str]):
            container = QtWidgets.QWidget(self.detail)
            container.setObjectName("parseReviewBinaryActions")
            assignment = actions[0] in {"select_reference", "select_claim"}
            row = QtWidgets.QVBoxLayout(container) if assignment else QtWidgets.QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8 if assignment else 0)
            for action in actions:
                button = QtWidgets.QPushButton(labels[action], container)
                button.setObjectName(f"parseReviewAction_{action}")
                button.setCheckable(True)
                button.setProperty("action", action)
                button.setProperty("role", "choice")
                if assignment and action == actions[0]:
                    # Candidate radios are the visible assignment choice; keep
                    # this checked action as the draft/payload state only.
                    button.hide()
                else:
                    row.addWidget(button, 1 if not assignment else 0)
                self._action_group.addButton(button)
                self._action_buttons[action] = button
            self.detail_layout.addWidget(container)
            self._action_buttons[actions[0]].setChecked(True)

        def _candidate_picker(self, task: Mapping[str, Any], target: str):
            container = QtWidgets.QWidget(self.detail)
            container.setObjectName("parseReviewCandidateList")
            layout = QtWidgets.QVBoxLayout(container)
            layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
            layout.setContentsMargins(0, 0, 0, 0)
            heading = QtWidgets.QLabel(
                labels["candidate_references" if target == "ref" else "candidate_claims"],
                container,
            )
            heading.setObjectName("parseReviewSectionHeading")
            layout.addWidget(heading)
            self._candidate_group = QtWidgets.QButtonGroup(container)
            self._candidate_group.setExclusive(True)
            self._candidate_group.buttonClicked.connect(self._candidate_selected)
            candidates = task.get("candidates") or []
            for candidate in candidates:
                if not isinstance(candidate, dict) or not candidate.get("id"):
                    continue
                identifier = str(candidate["id"])
                provenance = labels["candidate_label"].format(
                    identifier=identifier,
                    origin=candidate.get("origin") or "candidate",
                    score=(
                        f"{candidate['score']:.3f}"
                        if isinstance(candidate.get("score"), (int, float))
                        else ""
                    ),
                ).strip(" ·")
                display = self._candidate_display_text(candidate, identifier)
                option = CandidateRow(container)
                option.setObjectName("parseReviewCandidateRow")
                option.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
                option.setMinimumHeight(120)
                option.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                option.setToolTip(provenance)
                option_layout = QtWidgets.QHBoxLayout(option)
                option_layout.setContentsMargins(24, 18, 24, 18)
                option_layout.setSpacing(24)
                radio = QtWidgets.QRadioButton(
                    "", option,
                )
                radio.setObjectName(f"parseReviewCandidate_{len(self._candidate_group.buttons())}")
                radio.setProperty("candidate_id", identifier)
                radio.setProperty("candidate_origin", candidate.get("origin") or "candidate")
                radio.setProperty("candidate_score", candidate.get("score"))
                radio.setAccessibleName(display)
                radio.setToolTip(provenance)
                option.radio = radio
                self._candidate_group.addButton(radio)
                option_layout.addWidget(radio)
                description = QtWidgets.QLabel(display, option)
                description.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                description.setObjectName("parseCandidateDescription")
                description.setWordWrap(True)
                description.setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Ignored,
                    QtWidgets.QSizePolicy.Policy.Minimum,
                )
                description.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                option_layout.addWidget(description, 1)
                layout.addWidget(option)
                option._schedule_content_fit()
            self.detail_layout.addWidget(container)
            self._candidate_container = container
            self._candidate_target_field = "ref_id" if target == "ref" else "claim_id"

        def _candidate_selected(self, *_args):
            if self._candidate_target_field == "ref_id":
                action = "select_reference"
            else:
                action = "select_claim"
            button = self._action_buttons.get(action)
            if button is not None:
                button.setChecked(True)
            self._remember_draft()

        def _assignment_target(self, task: Mapping[str, Any]):
            container = QtWidgets.QWidget(self.detail)
            container.setObjectName("parseReviewOriginalText")
            layout = QtWidgets.QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)
            heading = QtWidgets.QLabel(labels["target"], container)
            heading.setObjectName("parseReviewSectionHeading")
            layout.addWidget(heading)
            kind = task.get("review_kind")
            if kind == "citation_reference_review":
                marker = self._recorded_text(task, "marker_raw")
                claim = self._recorded_text(task, "claim_text")
                parts = []
                if isinstance(marker, str) and marker.strip():
                    parts.append(("parseReviewCitationMarker", marker))
                else:
                    parts.append(("parseReviewMissingMarker", labels["missing_citation_marker"]))
                if isinstance(claim, str) and claim.strip():
                    parts.append(("parseReviewOriginalClaim", claim))
                else:
                    parts.append(("parseReviewMissingClaim", labels["missing_claim_text"]))
            else:
                entry = self._recorded_text(task, "raw_entry", "title")
                parts = (
                    [("parseReviewOriginalEntry", entry)] if entry is not None else
                    [("parseReviewMissingReference", labels["missing_reference_text"])]
                )
            for object_name, text in parts:
                value = (CitedPassage(text, container) if object_name == "parseReviewOriginalClaim"
                         else QtWidgets.QLabel(text, container))
                if object_name == "parseReviewOriginalClaim":
                    value.setProperty("citationMarker", task.get("marker_raw") or "")
                value.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                value.setWordWrap(True)
                value.setTextInteractionFlags(
                    QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
                )
                value.setObjectName(object_name)
                layout.addWidget(value)
            layout.addStretch(1)
            return container

        def _line_field(self, label_key: str, value: Any):
            label_text = labels.get(label_key, labels["reason"])
            row = QtWidgets.QHBoxLayout()
            label = QtWidgets.QLabel(label_text, self.detail)
            field = QtWidgets.QLineEdit(self.detail)
            field.setObjectName(f"parseReview_{label_key}")
            if isinstance(value, str):
                field.setText(value)
            field.textChanged.connect(self._remember_draft)
            row.addWidget(label)
            row.addWidget(field, 1)
            self.detail_layout.addLayout(row)
            self._field_rows[label_key] = row
            return field

        def _add_source_field(self):
            field = QtWidgets.QPlainTextEdit(self.detail)
            field.setObjectName(f"parseReviewSource{len(self._source_fields) + 1}")
            field.setPlaceholderText(labels["exact_source_field"].format(
                number=len(self._source_fields) + 1
            ))
            field.setMaximumHeight(82)
            field.textChanged.connect(self._remember_draft)
            self._source_fields.append(field)
            self._source_layout.insertWidget(max(0, self._source_layout.count() - 1), field)
            self._update_source_buttons()

        def _remove_source_field(self):
            if len(self._source_fields) <= 2:
                return
            field = self._source_fields.pop()
            self._source_layout.removeWidget(field)
            field.deleteLater()
            self._update_source_buttons()

        def _update_source_buttons(self):
            if self._remove_source_button is not None:
                self._remove_source_button.setEnabled(len(self._source_fields) > 2)

        def _raw_target(self, task: Mapping[str, Any]) -> str:
            kind = task.get("review_kind")
            if kind == "citation_reference_review":
                return labels["citation_target"].format(
                    marker=(self._recorded_text(task, "marker_raw")
                            or labels["missing_citation_marker"]),
                    claim=(self._recorded_text(task, "claim_text")
                           or labels["missing_claim_text"]),
                )
            if kind == "reference_claim_review":
                return labels["reference_target"].format(
                    number=task.get("ref_number", "?"),
                    entry=(self._recorded_text(task, "raw_entry", "title")
                           or labels["missing_reference_text"]),
                )
            if kind == "footnote_source_review":
                return labels["note_target"].format(
                    number=task.get("note_number", "?"),
                    note=(self._recorded_text(task, "raw_note")
                          or labels["missing_footnote_text"]),
                )
            return labels["identity_target"].format(
                number=task.get("ref_number", "?"),
                title=(self._recorded_text(task, "raw_entry", "title")
                       or labels["missing_reference_text"]),
            )

        @staticmethod
        def _recorded_text(source: Mapping[str, Any], *keys: str) -> str | None:
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return None

        def _candidate_display_text(
            self, candidate: Mapping[str, Any], identifier: str,
        ) -> str:
            label = self._recorded_text(candidate, "label")
            if label is not None and label != identifier:
                return label
            return (
                self._recorded_text(candidate, "raw_entry", "claim_text", "title")
                or labels["missing_candidate_text"]
            )

        def _readonly_value(self, label_key: str, value: Any):
            row = QtWidgets.QHBoxLayout()
            label = QtWidgets.QLabel(labels[label_key], self.detail)
            display = QtWidgets.QLabel(str(value or "—"), self.detail)
            display.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            display.setObjectName(f"parseReview_{label_key}")
            display.setWordWrap(True)
            display.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            )
            row.addWidget(label)
            row.addWidget(display, 1)
            self.detail_layout.addLayout(row)

        def _saved_answer_summary(self, task: Mapping[str, Any]) -> str:
            answer = task.get("answer")
            if not isinstance(answer, Mapping):
                answer = {}
            action = str(answer.get("action") or "")
            action_label = labels.get(action, action or labels["saved_answer"])
            lines = [f"{labels['selected_by_user']}: {action_label}"]
            selected_key = (
                "ref_id" if action == "select_reference"
                else "claim_id" if action == "select_claim" else None
            )
            selected_id = answer.get(selected_key) if selected_key else None
            if selected_id:
                candidate = next((
                    item for item in task.get("candidates") or ()
                    if isinstance(item, dict) and item.get("id") == selected_id
                ), None)
                lines.append(
                    self._candidate_display_text(candidate, str(selected_id))
                    if candidate else labels["missing_candidate_text"]
                )
            source_texts = answer.get("source_texts")
            if isinstance(source_texts, list):
                lines.append(labels["saved_source_count"].format(count=len(source_texts)))
            for key, label_key in (("title", "title_field"), ("doi", "doi_field")):
                value = answer.get(key)
                if value:
                    lines.append(f"{labels[label_key]}: {value}")
            reason = answer.get("reason")
            if reason:
                lines.append(f"{labels['reason']}: {reason}")
            return "\n".join(lines)

        def _draft_payload(
            self, task: Mapping[str, Any], draft: Mapping[str, Any],
        ) -> dict[str, Any] | None:
            action = draft.get("action")
            kind = task.get("review_kind")
            if action == "keep_unresolved" and kind in {
                "citation_reference_review", "reference_claim_review",
            }:
                pass
            elif action == "keep_ambiguous" and kind in {
                "footnote_source_review", "reference_identity_review",
            }:
                pass
            elif kind == "citation_reference_review" and action == "select_reference":
                candidate_id = draft.get("candidate_id")
                if not any(
                    isinstance(candidate, Mapping)
                    and candidate.get("id") == candidate_id
                    for candidate in task.get("candidates") or ()
                ):
                    return None
            elif kind == "reference_claim_review" and action == "select_claim":
                candidate_id = draft.get("candidate_id")
                if not any(
                    isinstance(candidate, Mapping)
                    and candidate.get("id") == candidate_id
                    for candidate in task.get("candidates") or ()
                ):
                    return None
            elif kind == "footnote_source_review" and action == "no_sources":
                pass
            elif kind == "footnote_source_review" and action == "split_sources":
                source_texts = draft.get("source_texts")
                if (
                    not isinstance(source_texts, list)
                    or len(source_texts) < 2
                    or any(not isinstance(value, str) or not value.strip() for value in source_texts)
                ):
                    return None
            elif kind == "reference_identity_review" and action == "correct_identity":
                fields = draft.get("fields")
                if not isinstance(fields, Mapping) or not (
                    str(fields.get("title") or "").strip()
                    or str(fields.get("doi") or "").strip()
                ):
                    return None
            else:
                return None

            fields = draft.get("fields")
            if not isinstance(fields, Mapping):
                fields = {}
            payload: dict[str, Any] = {
                "action": action,
                "target_sha256": task.get("target_sha256"),
                "reason": str(fields.get("reason") or "").strip(),
            }
            if action == "select_reference":
                payload["ref_id"] = draft.get("candidate_id")
            elif action == "select_claim":
                payload["claim_id"] = draft.get("candidate_id")
            elif action == "split_sources":
                payload["source_texts"] = list(draft["source_texts"])
            elif action == "correct_identity":
                title = str(fields.get("title") or "").strip()
                doi = str(fields.get("doi") or "").strip()
                payload.update(title=title or None, doi=doi or None)
            return payload

        def _resume(self):
            self._remember_draft()
            pending = [
                task for task in self._tasks
                if self._task_status(task) == "pending"
            ]
            submissions = []
            unresolved = []
            for task in pending:
                task_id = str(task.get("task_id") or "")
                draft = self._drafts.get(task_id)
                if (
                    not task_id or draft is None
                    or draft.get("target_sha256") != task.get("target_sha256")
                ):
                    unresolved.append(task)
                    continue
                payload = self._draft_payload(task, draft)
                if payload is None:
                    unresolved.append(task)
                else:
                    submissions.append((task_id, payload))
            explicit_unresolved = [
                (task_id, payload) for task_id, payload in submissions
                if payload.get("action") in {"keep_unresolved", "keep_ambiguous"}
            ]
            missing = unresolved
            if unresolved or explicit_unresolved:
                reply = QtWidgets.QMessageBox.question(
                    self, labels["resume"],
                    labels["skip_confirm"].format(
                        count=len(missing) + len(explicit_unresolved)
                    ),
                    QtWidgets.QMessageBox.StandardButton.Yes
                    | QtWidgets.QMessageBox.StandardButton.Cancel,
                    QtWidgets.QMessageBox.StandardButton.Cancel,
                )
                if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
            self._set_resume_ready(False)
            try:
                for task_id, payload in submissions:
                    answer_task(task_id, payload)
                    self._drafts.pop(task_id, None)
                if missing:
                    skip_remaining()
            except Exception as exc:
                self._status_after_load = (
                    labels["submit_error"].format(reason=exc), "error",
                )
                self._refresh_after_answer = True
                self._load()
                return
            try:
                on_complete()
            except Exception as exc:
                self._status_after_load = (
                    labels["error"].format(reason=exc), "error",
                )
                self._refresh_after_answer = True
                self._load()

        def hideEvent(self, event):  # noqa: N802 - Qt API name
            if self._open:
                self._remember_draft()
                if self._compact_animation is not None:
                    self._stop_compact_animation()
                self._compact_detail_open = False
                self._compact_transition_target = False
                self._open = False
                self._generation += 1
                self._pulse.stop()
                self._tasks = []
                self._current = None
                self._clear_tabs()
                self._clear_detail()
                self._set_resume_ready(False)
                if on_dismiss is not None:
                    QtCore.QTimer.singleShot(0, on_dismiss)
            super().hideEvent(event)

        def set_dark_theme(self, dark: bool):
            """Apply the desktop's current light or dark surface colors."""
            background, foreground, surface, border, accent, hover, pressed, muted, guidance, success, error = (
                ("#1b1f24", "#edf3ff", "#101722", "#45546a", "#3979c7",
                 "#4c91e4", "#285c9b", "#273140", "#20354d", "#59c896", "#ff908c")
                if dark else
                ("#f8fbfe", "#11183c", "#ffffff", "#d1def2", "#2457a6",
                 "#316dc1", "#184485", "#e8edf5", "#e9f2ff", "#16744b", "#b4232f")
            )
            self.setStyleSheet(
                f"QDialog {{ background: {background}; color: {foreground}; }}"
                f"QWidget {{ color: {foreground}; }}"
                f"QWidget#parseReviewWorkspace {{ background: {background}; }}"
                f"QScrollArea#parseReviewOverviewScroll, QWidget#parseReviewOverview {{ background: {background}; }}"
                f"QScrollArea#parseReviewDetailScroll, QWidget#parseReviewDetail {{ background: {background}; }}"
                f"QScrollArea#parseReviewDetailScroll > QWidget > QWidget {{ background: {background}; }}"
                f"QPlainTextEdit, QLineEdit {{ background: {surface}; "
                f"border: 1px solid {border}; border-radius: 4px; }}"
                "QTabBar#parseReviewTaskTabs { background: transparent; border: none; }"
                f"QTabBar#parseReviewTaskTabs::tab {{ background: {surface}; "
                f"border: 1px solid {border}; border-bottom: none; "
                f"border-top-left-radius: 5px; border-top-right-radius: 5px; "
                f"padding: 8px 12px; min-width: 115px; }}"
                f"QTabBar#parseReviewTaskTabs::tab:hover {{ background: {muted}; }}"
                f"QTabBar#parseReviewTaskTabs::tab:pressed {{ background: {pressed}; color: white; }}"
                f"QTabBar#parseReviewTaskTabs::tab:selected {{ background: {background}; "
                f"border-top: 3px solid {accent}; font-weight: 700; }}"
                f"QTabBar#parseReviewTaskTabs QToolButton {{ background: {surface}; "
                f"border: 1px solid {border}; padding: 4px; }}"
                f"QTabBar#parseReviewTaskTabs QToolButton:hover {{ background: {muted}; }}"
                f"QTabBar#parseReviewTaskTabs QToolButton:pressed {{ background: {pressed}; }}"
                f"QLabel#parseReviewProgress {{ color: {accent}; font-weight: 700; }}"
                f"QLabel#parseReviewAssignmentQuestion {{ color: {accent}; font-size: 18px; font-weight: 700; }}"
                f"QLabel#parseReviewOverviewHeading {{ font-size: 22px; font-weight: 700; }}"
                f"QPushButton#parseReviewTaskCard {{ text-align: left; background: {surface}; border: 1px solid {border}; border-left: 6px solid #8490a3; border-radius: 6px; padding: 2px; }}"
                f"QPushButton#parseReviewTaskCard:hover {{ background: {muted}; border-color: {accent}; }}"
                f"QPushButton#parseReviewTaskCard:focus {{ border-color: {accent}; }}"
                f"QPushButton#parseReviewTaskCard[selected='true'] {{ border-color: {accent}; }}"
                f"QPushButton#parseReviewTaskCard[status='selected'] {{ border-left-color: {success}; }}"
                f"QPushButton#parseReviewTaskCard[status='unresolved'] {{ border-left-color: {'#ffca66' if dark else '#a6600c'}; }}"
                f"QLabel#parseReviewTaskTitle {{ font-weight: 700; }}"
                f"QLabel#parseReviewTaskContext {{ color: {'#c0c9d8' if dark else '#48556a'}; }}"
                f"QLabel#parseReviewTaskState[status='selected'] {{ color: {success}; font-weight: 700; }}"
                f"QLabel#parseReviewTaskState[status='unresolved'] {{ color: {'#ffca66' if dark else '#a6600c'}; font-weight: 700; }}"
                f"QLabel#parseReviewTaskState[status='pending'], QLabel#parseReviewTaskState[status='unknown'] {{ color: {'#a8b1c0' if dark else '#687386'}; }}"
                f"QLabel#parseReviewHeading {{ font-size: 18px; font-weight: 700; }}"
                f"QLabel#parseReviewSectionHeading, QLabel#parseReviewSavedHeading {{ font-weight: 700; }}"
                f"QLabel#parseReviewGuidance {{ background: {guidance}; border-left: 4px solid {accent}; "
                f"padding: 9px; border-radius: 3px; }}"
                f"QLabel#parseReviewTarget {{ background: {surface}; border: 1px solid {border}; "
                f"padding: 9px; border-radius: 3px; }}"
                f"QLabel#parseReviewStatus[kind='error'] {{ color: {error}; font-weight: 600; }}"
                f"QLabel#parseReviewStatus[kind='success'] {{ color: {success}; font-weight: 600; }}"
                f"QWidget#parseReviewCandidateRow {{ background: {surface}; "
                f"border: 1px solid {border}; border-radius: 4px; }}"
                f"QWidget#parseReviewCandidateRow:hover {{ background: {muted}; }}"
                f"QRadioButton {{ spacing: 10px; }}"
                f"QRadioButton::indicator {{ width: 22px; height: 22px; "
                f"border: 2px solid {border}; border-radius: 12px; background: {surface}; }}"
                f"QRadioButton::indicator:hover {{ background: {muted}; border-color: {accent}; }}"
                f"QRadioButton::indicator:checked {{ background: {accent}; border-color: {accent}; }}"
                f"QRadioButton::indicator:checked:hover {{ background: {hover}; border-color: {hover}; }}"
                f"QRadioButton:hover {{ color: {accent}; }}"
                f"QRadioButton:pressed {{ color: {pressed}; }}"
                f"QPushButton {{ background: {surface}; border: 1px solid {border}; "
                f"border-radius: 4px; padding: 7px 12px; }}"
                f"QPushButton:hover {{ background: {muted}; border-color: {accent}; }}"
                f"QPushButton:pressed {{ background: {pressed}; color: white; }}"
                f"QPushButton:disabled {{ color: {border}; border-color: {border}; }}"
                f"QPushButton[role='choice']:checked {{ background: {accent}; border-color: {accent}; color: white; }}"
                f"QPushButton[role='choice']:checked:hover {{ background: {hover}; }}"
                f"QPushButton[role='choice']:checked:pressed {{ background: {pressed}; }}"
                f"QPushButton#parseReviewAnswer:enabled, QPushButton#parseReviewResume[ready='true'] {{ "
                f"background: {accent}; border-color: {accent}; color: white; }}"
                f"QPushButton#parseReviewAnswer:enabled:hover, "
                f"QPushButton#parseReviewResume[ready='true']:hover {{ background: {hover}; }}"
                f"QPushButton#parseReviewAnswer:enabled:pressed, "
                f"QPushButton#parseReviewResume[ready='true']:pressed {{ background: {pressed}; }}"
                f"QPushButton#parseReviewSkip:enabled {{ border-color: {accent}; color: {accent}; }}"
                f"QPushButton#parseReviewSkip:enabled:hover {{ background: {muted}; }}"
                f"QPushButton#parseReviewSkip:enabled:pressed {{ background: {pressed}; color: white; }}"
                + atlante_stylesheet(
                    dark=dark,
                    font_size=getattr(self, "_font_size", 13),
                )
                + f"""
                QFrame#parseAssignmentTarget {{ background: transparent; border: 0; border-right: 1px solid {border}; }}
                QFrame#parseAssignmentTarget QLabel#parseReviewTarget {{ border: 0; font-size: 16px; }}
                QWidget#parseReviewCandidateRow {{ background: {surface}; border: 1px solid {border}; border-radius: 5px; min-height: 120px; }}
                QWidget#parseReviewCandidateRow:hover {{ background: {muted}; border-color: {accent}; }}
                QLabel#parseReviewSectionHeading {{ font-size: {getattr(self, '_font_size', 13)+8}px; font-weight: 700; }}
                QLabel#parseReviewCitationMarker {{ color: {accent}; font-size: {getattr(self, '_font_size', 13)+2}px; padding: 8px 0; }}
                QLabel#parseReviewOriginalClaim {{ font-family: Georgia, serif; font-size: {getattr(self, '_font_size', 13)+10}px; }}
                QLabel#parseCandidateDescription {{ font-size: {getattr(self, '_font_size', 13)+3}px; }}
                QLabel#parseDecisionBanner {{ background: {'#4b3617' if dark else '#fff3d7'}; color: {'#ffd27b' if dark else '#98600c'}; border: 1px solid #e7ac36; border-radius: 5px; padding: 12px 12px 12px 44px; }}
                QFrame#parseDecisionFooter {{ background: {surface}; border: 0; border-top: 1px solid {border}; }}
                QLabel#parseReviewOriginalEntry {{ font-size: 18px; }}
                QPushButton#parseReviewAction_keep_unresolved {{ text-align: left; padding-left: 24px; min-height: 74px; font-weight: 400; }}
                QCheckBox:hover, QRadioButton:hover {{ color: {accent}; }}
                QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
                    background: {muted}; border-color: {accent}; }}
                QCheckBox::indicator:pressed, QRadioButton::indicator:pressed {{
                    background: {pressed}; border-color: {accent}; }}
                """
            )
            from core.gui.shell.navigation import set_back_button_presentation
            set_back_button_presentation(
                self.back_button, QtCore, QtGui, dark=dark,
                label=labels["back_to_list"], text=labels["back_to_list"],
            )
            self.back_button.setToolTip(labels["back_to_list_tip"])
            self._update_compact_navigation()
            self._refresh_overview_cards()

    return ParseReviewWindow()
