# core/gui/parse_review.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional PySide6 dialog for explicit, closed-choice manual Parse reviews.

The dialog owns presentation only. Callers provide durable task loading,
validated answer admission, and the continuation action.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping


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
        from PySide6 import QtCore, QtWidgets
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6":
            raise RuntimeError("PySide6 is required for Parse review") from exc
        raise

    provided_text = {
        key.removeprefix("parse_review_"): value
        for key, value in (review_text or {}).items()
        if key.startswith("parse_review_")
    }
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

    class CandidateRow(QtWidgets.QWidget):
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

    class ParseReviewWindow(QtWidgets.QDialog):
        def __init__(self):
            super().__init__()
            # Standalone callers own the dialog lifecycle by default. The
            # desktop keeps it alive explicitly so Review later and title-bar
            # close preserve drafts for this app session.
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
            self.setWindowTitle(labels["title"])
            self.setMinimumSize(680, 430)
            self.resize(760, 560)
            self._run_dir = run_dir
            self._generation = 0
            self._open = False
            self._tasks: list[dict[str, Any]] = []
            self._current: dict[str, Any] | None = None
            self._refresh_after_answer = False
            self._next_task_id: str | None = None
            self._drafts: dict[str, dict[str, Any]] = {}
            self._status_after_load: tuple[str, str] | None = None
            self._build()
            self._pulse = QtCore.QTimer(self)
            self._pulse.setInterval(450)
            self._pulse.timeout.connect(self._pulse_loading)
            self._pulse_frame = 0

        def _build(self):
            root = QtWidgets.QVBoxLayout(self)
            root.setSpacing(10)
            detail_group = QtWidgets.QVBoxLayout()
            detail_group.setSpacing(0)
            root.addLayout(detail_group, 1)
            self.task_tabs = QtWidgets.QTabBar(self)
            self.task_tabs.setObjectName("parseReviewTaskTabs")
            self.task_tabs.setExpanding(False)
            self.task_tabs.setUsesScrollButtons(True)
            self.task_tabs.setElideMode(QtCore.Qt.TextElideMode.ElideNone)
            self.task_tabs.setTabsClosable(False)
            detail_group.addWidget(self.task_tabs)
            scroll = QtWidgets.QScrollArea(self)
            scroll.setObjectName("parseReviewDetailScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            self.detail = QtWidgets.QWidget(scroll)
            self.detail.setObjectName("parseReviewDetail")
            self.detail_layout = QtWidgets.QVBoxLayout(self.detail)
            self.detail_layout.setContentsMargins(4, 4, 4, 4)
            self.detail_layout.setSpacing(10)
            scroll.setWidget(self.detail)
            self.task_tabs.addTab(labels["loading"])
            detail_group.addWidget(scroll, 1)
            self.status = QtWidgets.QLabel("", self)
            self.status.setObjectName("parseReviewStatus")
            self.status.setWordWrap(True)
            root.addWidget(self.status)
            actions = QtWidgets.QHBoxLayout()
            actions.addStretch()
            self.retry_button = QtWidgets.QPushButton(labels["refresh"], self)
            self.retry_button.clicked.connect(self._load)
            self.retry_button.hide()
            actions.addWidget(self.retry_button)
            self.resume_button = QtWidgets.QPushButton(labels["resume"], self)
            self.resume_button.setObjectName("parseReviewResume")
            self.resume_button.setEnabled(False)
            self.resume_button.clicked.connect(self._resume)
            actions.addWidget(self.resume_button)
            self.close_button = QtWidgets.QPushButton(labels["close"], self)
            self.close_button.setToolTip(labels["close_tip"])
            self.close_button.clicked.connect(self.hide)
            actions.addWidget(self.close_button)
            root.addLayout(actions)
            self._clear_detail()
            self.task_tabs.currentChanged.connect(self._select_task)

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
                self._set_status(labels[key].format(reason=error))
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
                self._update_review_status()
                self._show_status_after_load()
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
            self._update_review_status()
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
            status = (labels["saved"] if self._task_status(task) == "answered" else
                      labels["draft"] if str(task.get("task_id")) in self._drafts else
                      labels["pending"])
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
                return
            self._current = self._tasks[row]
            self._current_is_pending = self._task_status(self._current) == "pending"
            self._render_detail(self._current)
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
            row = self.task_tabs.currentIndex()
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
            if self._current_is_pending:
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
            decision_label = QtWidgets.QLabel(labels["choice"], self.detail)
            decision_label.setObjectName("parseReviewSectionHeading")
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

        def _action_changed(self, *_args):
            action = self._selected_action()
            if self._candidate_container is not None:
                target_action = (
                    "select_reference" if self._candidate_target_field == "ref_id"
                    else "select_claim"
                )
                self._candidate_container.setEnabled(action == target_action)
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
            row = QtWidgets.QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(0)
            for action in actions:
                button = QtWidgets.QPushButton(labels[action], container)
                button.setObjectName(f"parseReviewAction_{action}")
                button.setCheckable(True)
                button.setProperty("action", action)
                button.setProperty("role", "choice")
                row.addWidget(button, 1)
                self._action_group.addButton(button)
                self._action_buttons[action] = button
            self.detail_layout.addWidget(container)
            self._action_buttons[actions[0]].setChecked(True)

        def _candidate_picker(self, task: Mapping[str, Any], target: str):
            container = QtWidgets.QWidget(self.detail)
            container.setObjectName("parseReviewCandidateList")
            layout = QtWidgets.QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            heading = QtWidgets.QLabel(
                labels["candidate_references" if target == "ref" else "candidate_claims"],
                container,
            )
            heading.setObjectName("parseReviewSectionHeading")
            layout.addWidget(heading)
            self._candidate_group = QtWidgets.QButtonGroup(container)
            self._candidate_group.setExclusive(True)
            self._candidate_group.buttonClicked.connect(
                lambda *_args: self._remember_draft()
            )
            candidates = task.get("candidates") or []
            for candidate in candidates:
                if not isinstance(candidate, dict) or not candidate.get("id"):
                    continue
                identifier = str(candidate["id"])
                display = labels["candidate_label"].format(
                    identifier=identifier,
                    origin=candidate.get("origin") or "candidate",
                    score=(
                        f"{candidate['score']:.3f}"
                        if isinstance(candidate.get("score"), (int, float))
                        else ""
                    ),
                ).strip(" ·")
                display = str(candidate.get("label") or display)
                option = CandidateRow(container)
                option.setObjectName("parseReviewCandidateRow")
                option.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                option_layout = QtWidgets.QHBoxLayout(option)
                option_layout.setContentsMargins(8, 6, 8, 6)
                radio = QtWidgets.QRadioButton(
                    labels["candidate_option"].format(number=len(self._candidate_group.buttons()) + 1),
                    option,
                )
                radio.setObjectName(f"parseReviewCandidate_{len(self._candidate_group.buttons())}")
                radio.setProperty("candidate_id", identifier)
                radio.setAccessibleName(display)
                option.radio = radio
                self._candidate_group.addButton(radio)
                option_layout.addWidget(radio)
                description = QtWidgets.QLabel(display, option)
                description.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                description.setWordWrap(True)
                description.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                option_layout.addWidget(description, 1)
                layout.addWidget(option)
            self.detail_layout.addWidget(container)
            self._candidate_container = container
            self._candidate_target_field = "ref_id" if target == "ref" else "claim_id"

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
                    marker=task.get("marker_raw") or task.get("occurrence_id") or "?",
                    claim=task.get("claim_text") or task.get("claim_id") or "?",
                )
            if kind == "reference_claim_review":
                return labels["reference_target"].format(
                    number=task.get("ref_number", "?"),
                    entry=task.get("raw_entry") or task.get("ref_id") or "?",
                )
            if kind == "footnote_source_review":
                return labels["note_target"].format(
                    number=task.get("note_number", "?"),
                    note=task.get("raw_note") or task.get("note_id") or "?",
                )
            return labels["identity_target"].format(
                number=task.get("ref_number", "?"),
                title=task.get("raw_entry") or task.get("title") or task.get("ref_id") or "?",
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
                lines.append(str(candidate.get("label") if candidate else selected_id))
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
                ("#f4f6fa", "#18202a", "#ffffff", "#b7c6da", "#2457a6",
                 "#316dc1", "#184485", "#e8edf5", "#e9f2ff", "#16744b", "#b4232f")
            )
            self.setStyleSheet(
                f"QDialog {{ background: {background}; color: {foreground}; }}"
                f"QWidget {{ color: {foreground}; }}"
                f"QScrollArea#parseReviewDetailScroll, QWidget#parseReviewDetail {{ background: {background}; }}"
                f"QScrollArea#parseReviewDetailScroll > QWidget > QWidget {{ background: {background}; }}"
                f"QPlainTextEdit, QLineEdit {{ background: {surface}; "
                f"border: 1px solid {border}; border-radius: 4px; }}"
                "QTabBar#parseReviewTaskTabs { background: transparent; border: none; }"
                f"QTabBar#parseReviewTaskTabs::tab {{ background: {surface}; "
                f"border: 1px solid {border}; border-bottom: none; "
                f"border-top-left-radius: 5px; border-top-right-radius: 5px; "
                f"padding: 8px 12px; min-width: 115px; max-width: 220px; }}"
                f"QTabBar#parseReviewTaskTabs::tab:hover {{ background: {muted}; }}"
                f"QTabBar#parseReviewTaskTabs::tab:pressed {{ background: {pressed}; color: white; }}"
                f"QTabBar#parseReviewTaskTabs::tab:selected {{ background: {background}; "
                f"border-top: 3px solid {accent}; font-weight: 700; }}"
                f"QTabBar#parseReviewTaskTabs QToolButton {{ background: {surface}; "
                f"border: 1px solid {border}; padding: 4px; }}"
                f"QTabBar#parseReviewTaskTabs QToolButton:hover {{ background: {muted}; }}"
                f"QTabBar#parseReviewTaskTabs QToolButton:pressed {{ background: {pressed}; }}"
                f"QLabel#parseReviewProgress {{ color: {accent}; font-weight: 700; }}"
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
            )

    return ParseReviewWindow()
