# core/gui/guided_fetch.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""PySide6 implementation of the optional guided Fetch desktop window.

Qt is imported only inside the public factories so importing this module stays
safe on server and CI installations where the optional extra is absent.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from core.app.guided_fetch import build_source_inventory, source_payload
from .guided_fetch_viewmodel import GuidedFetchViewModel


CHROME_DOWNLOAD_URL = "https://www.google.com/chrome/"
_OCR_DEFAULT = {
    "ocr_column": "OCR",
    "ocr_button": "Run OCR",
    "ocr_button_count": "Run OCR · {count} PDF",
    "ocr_button_active": "OCR in progress · {done}/{total}",
    "ocr_tooltip": "Extract text from the PDFs awaiting OCR.",
    "ocr_pending": "PDF needs OCR",
    "ocr_queued": "OCR queued",
    "ocr_running": "OCR in progress",
    "ocr_done": "Text extracted",
    "ocr_failed": "OCR failed",
    "ocr_progress_accessible": "OCR in progress for source {source}",
    "ocr_confirm_button": "Confirm and run OCR",
    "ocr_confirm_note": " OCR will start after confirmation.",
    "ocr_skip_button": "Proceed / skip Fetch and OCR",
    "ocr_skip_note": " {count} PDF(s) still need OCR. Skipping Fetch and OCR leaves their full text unavailable for verification. The PDFs remain saved; cancel to provide another file or run OCR.",
    "ocr_finished_summary": "OCR finished: {done} text(s) extracted, {failed} failed.",
    "ocr_stopped_summary": "OCR stopped: {reason}",
    "ocr_close_busy": "OCR is still running; wait for it to finish.",
}


def _preflight_source_file(path: str, tier: str) -> str:
    """Check readable source bytes before staging, without admitting evidence."""
    from core.fetch.extraction import fetch_html
    from core.parse import extract as parse_extract

    candidate = Path(path)
    suffix = candidate.suffix.lower()
    try:
        parse_extract.probe_file(str(candidate))
    except (OSError, RuntimeError, ValueError) as exc:
        detail = str(exc).replace("\u2014", ":")
        raise ValueError(f"Cannot read {candidate.name}: {detail}") from exc
    try:
        if suffix in {".htm", ".html", ".xhtml"} and fetch_html.is_challenge_html(
            candidate.read_bytes().decode("utf-8", errors="replace")
        ):
            raise ValueError("the HTML contains a browser challenge or login page")
        text, _format, details = parse_extract.extract_text(str(candidate))
    except (OSError, RuntimeError, ValueError) as exc:
        if suffix == ".pdf" and tier == "fulltext":
            # A valid PDF with no extractable text can enter the existing OCR path.
            return "ocr_pending"
        detail = str(exc).replace("\u2014", ":")
        raise ValueError(f"Cannot read text from {candidate.name}: {detail}") from exc
    if not text.strip():
        if suffix == ".pdf" and tier == "fulltext":
            return "ocr_pending"
        raise ValueError(f"{candidate.name} contains no readable text")
    if suffix in {".htm", ".html", ".xhtml"} and tier == "fulltext" and (
        details.get("html_outcome") == "abstract_only"
        or not fetch_html.html_fulltext_ok(text)
    ):
        raise ValueError(
            f"{candidate.name} does not contain readable full text. "
            "Choose Abstract if it is an authentic abstract, or capture the full article."
        )
    return "readable"


def create_guided_fetch_window(
    run_dir: str,
    *,
    submit_source: Callable[[str, dict[str, Any]], Any],
    proceed: Callable[[], Any],
    discard_source: Callable[[str], Any] | None = None,
    run_ocr: Callable[[list[dict[str, Any]], Callable[[dict[str, Any]], None]], list[dict[str, Any]]] | None = None,
    on_ocr_event: Callable[[dict[str, Any]], None] | None = None,
    ocr_text: Mapping[str, str] | None = None,
    submit_identity_review: Callable[[str, str, str, str], Any] | None = None,
    identity_review_allowed: bool = True,
    inventory_loader: Callable[[str], dict[str, Any]] = build_source_inventory,
):
    """Create (but do not show) the window; all writes use injected callbacks."""
    qt = _load_qt()
    QtCore, QtGui, QtWidgets = qt
    labels = {**_OCR_DEFAULT, **(ocr_text or {})}

    def pdf_icon(color: str):
        pixmap = QtGui.QPixmap(17, 19)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor(color), 1.3))
        paper = QtGui.QPainterPath()
        paper.moveTo(2, 1)
        paper.lineTo(11, 1)
        paper.lineTo(15, 5)
        paper.lineTo(15, 18)
        paper.lineTo(2, 18)
        paper.closeSubpath()
        painter.drawPath(paper)
        painter.drawLine(11, 1, 11, 5)
        painter.drawLine(11, 5, 15, 5)
        font = painter.font()
        font.setPixelSize(6)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QtCore.QRect(2, 8, 13, 8), QtCore.Qt.AlignmentFlag.AlignCenter, "PDF")
        painter.end()
        return QtGui.QIcon(pixmap)

    class OcrWorker(QtCore.QObject):
        progress = QtCore.Signal(object)
        finished = QtCore.Signal(object)
        failed = QtCore.Signal(str)

        def __init__(self, action, jobs):
            super().__init__()
            self._action = action
            self._jobs = jobs

        @QtCore.Slot()
        def run(self):
            try:
                self.finished.emit(self._action(self._jobs, self.progress.emit))
            except Exception as exc:
                self.failed.emit(str(exc))

    class PreviewWorker(QtCore.QObject):
        loaded = QtCore.Signal(object, str)

        def __init__(self, request, path: str):
            super().__init__()
            self._request = request
            self._path = path

        @QtCore.Slot()
        def run(self):
            try:
                text = _read_preview(self._path)
            except Exception:
                text = "Preview unavailable."
            self.loaded.emit(self._request, text)

    class TierSwitch(QtWidgets.QAbstractButton):
        """Compact two-state switch; unchecked is Full text, checked is Abstract."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self._hovered = False
            self._dark_theme = False
            self.setCheckable(True)
            self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.setFixedSize(42, 24)

        def set_dark_theme(self, dark: bool):
            self._dark_theme = dark
            self.update()

        def enterEvent(self, event):  # noqa: N802 - Qt API name
            self._hovered = True
            self.update()
            super().enterEvent(event)

        def leaveEvent(self, event):  # noqa: N802 - Qt API name
            self._hovered = False
            self.update()
            super().leaveEvent(event)

        def paintEvent(self, _event):  # noqa: N802 - Qt API name
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            track = QtCore.QRectF(1, 3, 40, 18)
            if not self.isEnabled():
                track_color = QtGui.QColor("#364152" if self._dark_theme else "#cbd5e1")
            elif self.isDown():
                if self.isChecked():
                    track_color = QtGui.QColor("#183d78" if self._dark_theme else "#163f75")
                else:
                    track_color = QtGui.QColor("#364152" if self._dark_theme else "#64748b")
            elif self.isChecked():
                track_color = QtGui.QColor(
                    "#3974c6" if self._hovered and self._dark_theme else
                    "#1d4f91" if self._hovered else "#2457a6"
                )
            else:
                track_color = QtGui.QColor(
                    "#6b7a90" if self._hovered and self._dark_theme else
                    "#8795a8" if self._hovered else
                    "#52606d" if self._dark_theme else "#a9b4c4"
                )
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(track_color)
            painter.drawRoundedRect(track, 9, 9)
            knob_x = 22 if self.isChecked() else 4
            painter.setBrush(QtGui.QColor("#ffffff"))
            painter.drawEllipse(QtCore.QRectF(knob_x, 5, 14, 14))
            painter.end()

    class GuidedFetchWindow(QtWidgets.QMainWindow):
        browser_start = QtCore.Signal()
        browser_open = QtCore.Signal(str)
        browser_capture_html = QtCore.Signal(str, int)
        browser_capture_pdf = QtCore.Signal(str, int)
        browser_capture_download = QtCore.Signal(str, int, int)
        browser_discard_download = QtCore.Signal(int)
        browser_park = QtCore.Signal()
        browser_theme = QtCore.Signal(bool)

        def __init__(self):
            super().__init__()
            self._run_dir = run_dir
            self._inventory_loader = inventory_loader
            self._model = GuidedFetchViewModel(inventory_loader(run_dir))
            self._submit_source = submit_source
            self._proceed = proceed
            self._discard_source = discard_source
            self._run_ocr = run_ocr
            self._on_ocr_event = on_ocr_event
            self._submit_identity_review = submit_identity_review
            self._identity_review_allowed = identity_review_allowed
            self._current_ref_id: str | None = None
            self._selected_file: str | None = None
            self._selected_file_ref_id: str | None = None
            self._selected_file_generation: int | None = None
            self._next_file_generation = 0
            self._consumed_file_selections: set[tuple[str, int]] = set()
            self._selected_file_ocr_pending = False
            self._browser_thread = None
            self._browser_worker = None
            self._pending_browser_url: str | None = None
            self._browser_ref_id: str | None = None
            self._browser_tier: str | None = None
            self._capture_index = 0
            self._capture_tiers: dict[tuple[str, int], str] = {}
            self._preconfirmed_captures: set[tuple[str, int]] = set()
            self._queued_sources: dict[str, tuple[str, str | None]] = {}
            self._staged_ocr_refs: set[str] = set()
            self._ocr_row_status: dict[str, dict[str, Any]] = {}
            self._ocr_thread = None
            self._ocr_worker = None
            self._ocr_active_jobs: list[dict[str, Any]] = []
            self._recorded_identity_refs: set[str] = set()
            self._closing = False
            self._preview_thread = None
            self._preview_worker = None
            self._preview_inflight_request = None
            self._preview_pending_request = None
            self._preview_request = None
            self._preview_loaded_key = None
            self._preview_request_id = 0
            self._preview_close_when_finished = False
            self._selected_preview_path: str | None = None
            self._dark_theme = False
            self._font_size_px = 13
            self.setWindowTitle("Callimachus: Guided Fetch")
            icon = _callimachus_icon(QtCore, QtGui)
            self.setWindowIcon(icon)
            application = QtWidgets.QApplication.instance()
            if application is not None:
                application.setWindowIcon(icon)
            self.resize(1180, 820)
            self.setMinimumSize(900, 680)
            self._build()

        def _build(self):
            central = QtWidgets.QWidget(self)
            central.setObjectName("guidedFetchCentral")
            layout = QtWidgets.QVBoxLayout(central)
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(8)
            header = QtWidgets.QFrame(central)
            header.setObjectName("brandHeader")
            header_layout = QtWidgets.QHBoxLayout(header)
            header_layout.setContentsMargins(8, 5, 8, 5)
            logo = QtWidgets.QLabel(header)
            logo.setObjectName("brandLogo")
            logo.setPixmap(_callimachus_wordmark(
                QtCore, QtGui, 215, 46, self.devicePixelRatioF(), dark=False
            ))
            logo.setFixedSize(215, 46)
            logo.setScaledContents(False)
            self.brand_logo = logo
            header_layout.addWidget(logo)
            header_layout.addStretch()
            self.font_smaller = QtWidgets.QPushButton("A−", header)
            self.font_smaller.setObjectName("fontSmaller")
            self.font_smaller.setToolTip("Decrease interface font size")
            self.font_smaller.setAccessibleName("Decrease interface font size")
            self.font_smaller.clicked.connect(lambda: self._change_font_size(-1))
            header_layout.addWidget(self.font_smaller)
            self.font_larger = QtWidgets.QPushButton("A+", header)
            self.font_larger.setObjectName("fontLarger")
            self.font_larger.setToolTip("Increase interface font size")
            self.font_larger.setAccessibleName("Increase interface font size")
            self.font_larger.clicked.connect(lambda: self._change_font_size(1))
            header_layout.addWidget(self.font_larger)
            self.theme_toggle = QtWidgets.QPushButton("Dark theme", header)
            self.theme_toggle.setObjectName("themeToggle")
            self.theme_toggle.setCheckable(True)
            self.theme_toggle.toggled.connect(self._set_dark_theme)
            header_layout.addWidget(self.theme_toggle)
            layout.addWidget(header)
            self.table = QtWidgets.QTableWidget(0, 6, central)
            self.table.setObjectName("sourceTable")
            self.table.setHorizontalHeaderLabels(
                ["Source", "Title", "Text availability", labels["ocr_column"], "Risk signal", "Review labels"]
            )
            self.table.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
            )
            self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setAlternatingRowColors(True)
            self.table.verticalHeader().setVisible(False)
            self.table.verticalHeader().setDefaultSectionSize(24)
            self.table.verticalHeader().setMinimumSectionSize(22)
            source_header = self.table.horizontalHeader()
            source_header.setSectionResizeMode(
                0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
            source_header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
            source_header.setSectionResizeMode(
                2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
            source_header.setSectionResizeMode(
                3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
            source_header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            source_header.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeMode.Stretch)
            self.table.setMinimumHeight(140)
            self.table.itemSelectionChanged.connect(self._selected)

            self.tabs = QtWidgets.QTabWidget(central)
            self._tab_text = {}
            self._detail_views = {}
            self._preview_tab_widget = None
            for name in ("Parsed", "Resolved", "Acquired", "Preview"):
                panel, view = self._detail_panel(name)
                self.tabs.addTab(panel, name)
                self._detail_views[name] = view
                self._tab_text[name] = view["raw"]
                if name == "Preview":
                    self._preview_tab_widget = panel
            self.tabs.currentChanged.connect(self._detail_tab_changed)
            self.tabs.setMinimumHeight(210)
            detail_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical, central)
            detail_splitter.setObjectName("detailSplitter")
            detail_splitter.addWidget(self.table)
            detail_splitter.addWidget(self.tabs)
            detail_splitter.setStretchFactor(0, 2)
            detail_splitter.setStretchFactor(1, 3)
            detail_splitter.setSizes([220, 400])
            layout.addWidget(detail_splitter, 1)

            self.selection_guidance = QtWidgets.QLabel("Select a reference.", central)
            self.selection_guidance.setObjectName("selectionGuidance")
            self.selection_guidance.setWordWrap(True)
            layout.addWidget(self.selection_guidance)
            self.identity_review_panel = QtWidgets.QFrame(central)
            self.identity_review_panel.setObjectName("identityReviewPanel")
            identity_layout = QtWidgets.QVBoxLayout(self.identity_review_panel)
            identity_layout.setContentsMargins(8, 6, 8, 6)
            identity_layout.setSpacing(5)
            identity_facts_scroll = QtWidgets.QScrollArea(self.identity_review_panel)
            identity_facts_scroll.setObjectName("identityFactsScroll")
            identity_facts_scroll.setWidgetResizable(True)
            identity_facts_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            identity_facts_scroll.setMinimumHeight(50)
            self.identity_review_facts = QtWidgets.QLabel(identity_facts_scroll)
            self.identity_review_facts.setObjectName("identityReviewFacts")
            self.identity_review_facts.setWordWrap(True)
            self.identity_review_facts.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
            self.identity_review_facts.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            )
            identity_facts_scroll.setWidget(self.identity_review_facts)
            identity_layout.addWidget(identity_facts_scroll, 1)
            self.identity_review_reason = QtWidgets.QLineEdit(self.identity_review_panel)
            self.identity_review_reason.setObjectName("identityReviewReason")
            self.identity_review_reason.setPlaceholderText("Optional note for this identity decision")
            identity_layout.addWidget(self.identity_review_reason)
            identity_actions = QtWidgets.QHBoxLayout()
            self.attest_identity_button = QtWidgets.QPushButton(
                "Confirm: this is the cited work", self.identity_review_panel
            )
            self.attest_identity_button.setObjectName("attestIdentityButton")
            self.attest_identity_button.clicked.connect(
                lambda: self._submit_identity("attest_identity")
            )
            identity_actions.addWidget(self.attest_identity_button)
            self.keep_unverified_button = QtWidgets.QPushButton(
                "Keep unverified", self.identity_review_panel
            )
            self.keep_unverified_button.setObjectName("keepUnverifiedButton")
            self.keep_unverified_button.clicked.connect(
                lambda: self._submit_identity("keep_unverified")
            )
            identity_actions.addWidget(self.keep_unverified_button)
            identity_actions.addStretch()
            identity_layout.addLayout(identity_actions)
            self.identity_review_panel.setVisible(False)
            self._identity_review_tab = self.tabs.addTab(
                self.identity_review_panel, "Identity decision"
            )
            self.tabs.setTabEnabled(self._identity_review_tab, False)
            self.capture_status_panel = QtWidgets.QFrame(central)
            self.capture_status_panel.setObjectName("captureStatusPanel")
            capture_status_layout = QtWidgets.QHBoxLayout(self.capture_status_panel)
            capture_status_layout.setContentsMargins(8, 6, 8, 6)
            capture_status_layout.setSpacing(8)
            self.capture_status = QtWidgets.QLabel("", self.capture_status_panel)
            self.capture_status.setObjectName("captureStatus")
            self.capture_status.setWordWrap(True)
            self.capture_status.setVisible(False)
            capture_status_layout.addWidget(self.capture_status, 1)
            self.discard_source_button = QtWidgets.QPushButton(
                "Discard provided source", self.capture_status_panel
            )
            self.discard_source_button.setObjectName("discardSourceButton")
            self.discard_source_button.setToolTip(
                "Remove the queued source from this Guided Fetch session without deleting its audit capture."
            )
            self.discard_source_button.clicked.connect(self._discard_queued_source)
            self.discard_source_button.setEnabled(False)
            self.discard_source_button.setVisible(False)
            capture_status_layout.addWidget(self.discard_source_button)
            self.capture_status_panel.setVisible(False)
            layout.addWidget(self.capture_status_panel)

            controls_panel = QtWidgets.QFrame(central)
            controls_panel.setObjectName("actionPanel")
            controls = QtWidgets.QVBoxLayout(controls_panel)
            controls.setContentsMargins(8, 7, 8, 7)
            controls.setSpacing(6)
            browser_controls = QtWidgets.QHBoxLayout()
            self.open_link = QtWidgets.QPushButton("Open in system browser", central)
            self.open_link.clicked.connect(self._open_link)
            browser_controls.addWidget(self.open_link)
            self.open_chrome = QtWidgets.QPushButton("Open in assisted Chrome", central)
            self.open_chrome.clicked.connect(self._open_controlled_browser)
            browser_controls.addWidget(self.open_chrome)
            browser_controls.addStretch()
            self.ocr_button = QtWidgets.QPushButton(labels["ocr_button"], central)
            self.ocr_button.setObjectName("runOcrButton")
            self.ocr_button.setToolTip(labels["ocr_tooltip"])
            self.ocr_button.clicked.connect(lambda: self._start_ocr())
            self.ocr_button.setVisible(False)
            browser_controls.addWidget(self.ocr_button)
            self.proceed_button = QtWidgets.QPushButton("Proceed / skip remaining", central)
            self.proceed_button.setObjectName("proceedButton")
            self.proceed_button.setToolTip(
                "Continue with available sources and keep every unresolved retrieval waived."
            )
            self.proceed_button.clicked.connect(self._proceed_clicked)
            browser_controls.addWidget(self.proceed_button)
            controls.addLayout(browser_controls)
            self.proceed_guidance = QtWidgets.QLabel(central)
            self.proceed_guidance.setObjectName("proceedGuidance")
            self.proceed_guidance.setWordWrap(True)
            self.proceed_guidance.setVisible(False)
            controls.addWidget(self.proceed_guidance)

            source_controls = QtWidgets.QHBoxLayout()
            source_controls.addWidget(QtWidgets.QLabel("Provide an exact source:", central))
            self.fulltext_label = QtWidgets.QLabel("Full text", central)
            source_controls.addWidget(self.fulltext_label)
            self.tier_switch = TierSwitch(central)
            self.tier_switch.setObjectName("tierSwitch")
            self.tier_switch.setToolTip("Off: Full text. On: Abstract.")
            self.tier_switch.setAccessibleName("Source tier: Full text or Abstract")
            self.tier_switch.setEnabled(False)
            source_controls.addWidget(self.tier_switch)
            self.abstract_label = QtWidgets.QLabel("Abstract", central)
            source_controls.addWidget(self.abstract_label)
            self.choose_file = QtWidgets.QPushButton("Choose file", central)
            self.choose_file.clicked.connect(self._choose_file)
            self.choose_file.setEnabled(False)
            self.file_label = QtWidgets.QLabel("Drop one file here", central)
            self.file_label.setObjectName("fileDropArea")
            self.file_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.file_label.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
            self.file_label.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.file_label.setAccessibleName("Choose source file")
            self.file_label.setAccessibleDescription(
                "Click or press Enter or Space to choose a source file, or drop one here."
            )
            self.file_label.setAcceptDrops(True)
            self.file_label.installEventFilter(self)
            self.file_label.setEnabled(False)
            source_controls.addWidget(self.file_label, 1)
            source_controls.addWidget(self.choose_file)
            self.submit = QtWidgets.QPushButton("Confirm source", central)
            self.submit.clicked.connect(self._submit_file)
            self.submit.setEnabled(False)
            source_controls.addWidget(self.submit)
            controls.addLayout(source_controls)
            layout.addWidget(controls_panel)
            self.setCentralWidget(central)
            self._set_dark_theme(False)
            self._render_rows()

        def _detail_panel(self, name: str):
            panel = QtWidgets.QWidget(self.tabs)
            layout = QtWidgets.QVBoxLayout(panel)
            layout.setContentsMargins(8, 7, 8, 7)
            layout.setSpacing(6)
            headline = QtWidgets.QLabel(panel)
            headline.setObjectName("detailHeadline")
            instructions_button = QtWidgets.QPushButton(panel)
            instructions_button.setObjectName("instructionsButton")
            instructions_button.setIcon(self.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_MessageBoxQuestion
            ))
            instructions_button.setToolTip("Guided Fetch instructions")
            instructions_button.setAccessibleName("Guided Fetch instructions")
            raw_button = QtWidgets.QPushButton("Show original JSON", panel)
            headline_controls = QtWidgets.QHBoxLayout()
            headline_controls.addWidget(headline)
            headline_controls.addStretch()
            headline_controls.addWidget(instructions_button)
            headline_controls.addWidget(raw_button)
            table = QtWidgets.QTableWidget(0, 2, panel)
            table.setObjectName("detailTable")
            table.setHorizontalHeaderLabels(["Detail", "Recorded value"])
            table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setDefaultSectionSize(24)
            table.verticalHeader().setMinimumSectionSize(22)
            detail_header = table.horizontalHeader()
            detail_header.setSectionResizeMode(
                0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
            detail_header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
            raw_button.clicked.connect(
                lambda checked=False, panel_name=name: self._show_raw_json(panel_name)
            )
            instructions_button.clicked.connect(self._show_instructions)
            layout.addLayout(headline_controls)
            layout.addWidget(table)
            if name == "Preview":
                stack = QtWidgets.QStackedWidget(panel)
                stack.setObjectName("previewStack")
                loader = QtWidgets.QWidget(stack)
                loader.setObjectName("previewLoadingSkeleton")
                loader_layout = QtWidgets.QVBoxLayout(loader)
                loader_layout.setContentsMargins(18, 18, 18, 18)
                loader_layout.setSpacing(13)
                animations = []
                for index in range(6):
                    bar = QtWidgets.QFrame(loader)
                    bar.setObjectName("previewSkeletonBar")
                    bar.setFixedHeight(20 if index % 3 == 0 else 14)
                    bar.setMaximumWidth(560 if index % 3 == 0 else 350 + index * 18)
                    effect = QtWidgets.QGraphicsOpacityEffect(bar)
                    effect.setOpacity(0.6)
                    bar.setGraphicsEffect(effect)
                    animation = QtCore.QPropertyAnimation(effect, b"opacity", bar)
                    animation.setDuration(900 + index * 80)
                    animation.setStartValue(0.35)
                    animation.setKeyValueAt(0.5, 0.9)
                    animation.setEndValue(0.35)
                    animation.setLoopCount(-1)
                    animations.append(animation)
                    loader_layout.addWidget(bar)
                loader_layout.addStretch()
                preview = QtWidgets.QPlainTextEdit(panel)
                preview.setObjectName("previewText")
                preview.setReadOnly(True)
                stack.addWidget(loader)
                stack.addWidget(preview)
                stack.setCurrentWidget(preview)
                layout.addWidget(stack)
            else:
                preview = None
                stack = None
                loader = None
                animations = []
            return panel, {
                "headline": headline,
                "table": table,
                "raw": "",
                "button": raw_button,
                "preview": preview,
                "preview_stack": stack,
                "preview_loader": loader,
                "preview_animations": animations,
            }

        def _show_raw_json(self, name: str):
            self._show_text_dialog("Original audit JSON", self._detail_views[name]["raw"])

        def _show_instructions(self):
            task = self.selection_guidance.text()
            self._show_text_dialog(
                "Guided Fetch instructions",
                f"{task}\n\n"
                "Provide only the exact cited work. Full text is an authentic document containing "
                "the work; Abstract is an authentic abstract or bibliographic record when full text "
                "is unavailable. Open assisted Chrome to inspect a source, then use its Capture "
                "HTML or Capture PDF toolbar control only after confirming it is exact. You can "
                "also choose one local file. "
                "Proceed records every unresolved retrieval as waived; Callimachus never fills in "
                "missing source text.",
            )

        def _show_text_dialog(self, title: str, text: str):
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle(title)
            dialog.setMinimumSize(520, 320)
            dialog.resize(720, 520)
            layout = QtWidgets.QVBoxLayout(dialog)
            viewer = QtWidgets.QPlainTextEdit(dialog)
            viewer.setReadOnly(True)
            viewer.setPlainText(text)
            layout.addWidget(viewer)
            close_button = QtWidgets.QPushButton("Close", dialog)
            close_button.clicked.connect(dialog.accept)
            layout.addWidget(close_button, 0, QtCore.Qt.AlignmentFlag.AlignRight)
            dialog.exec()

        def _selected_tier(self) -> str:
            return "abstract" if self.tier_switch.isChecked() else "fulltext"

        def _change_font_size(self, delta: int):
            self._font_size_px = min(17, max(11, self._font_size_px + delta))
            self.font_smaller.setEnabled(self._font_size_px > 11)
            self.font_larger.setEnabled(self._font_size_px < 17)
            self._set_dark_theme(self._dark_theme)

        def _set_dark_theme(self, dark: bool):
            self._dark_theme = dark
            self.theme_toggle.setText("Light theme" if dark else "Dark theme")
            self.tier_switch.set_dark_theme(dark)
            if dark:
                self.setStyleSheet(_theme_stylesheet(dark=True, font_size=self._font_size_px))
            else:
                self.setStyleSheet(_theme_stylesheet(dark=False, font_size=self._font_size_px))
            self.brand_logo.setPixmap(_callimachus_wordmark(
                QtCore, QtGui, 215, 46, self.devicePixelRatioF(), dark=dark
            ))
            self._apply_status_colors()
            selected = self.table.selectedItems()
            self._set_identity_selection_style(
                selected[0].data(QtCore.Qt.ItemDataRole.UserRole) if selected else None
            )
            if self.tabs.isTabEnabled(self._identity_review_tab):
                self.tabs.tabBar().setTabTextColor(
                    self._identity_review_tab,
                    QtGui.QColor(_status_foregrounds(dark=dark)["amber"]),
                )
            if self._browser_worker is not None:
                self.browser_theme.emit(dark)

        def _apply_status_colors(self):
            colors = _status_foregrounds(dark=self._dark_theme)
            warning_background, warning_text, _ = _identity_review_colors(dark=self._dark_theme)
            for index, row in enumerate(self._model.rows()):
                if self._model.identity_review(row.ref_id) is not None:
                    for column in range(self.table.columnCount()):
                        item = self.table.item(index, column)
                        if item is not None:
                            item.setBackground(QtGui.QColor(warning_background))
                            if column < 2:
                                item.setForeground(QtGui.QColor(warning_text))
                availability = self.table.item(index, 2)
                if availability is not None:
                    availability.setForeground(QtGui.QColor(colors[row.status_color]))
                ocr = self.table.item(index, 3)
                ocr_status = self._ocr_row_status.get(row.ref_id, {}).get("status")
                if not ocr_status and row.ref_id in self._staged_ocr_refs:
                    ocr_status = "pending"
                if not ocr_status and row.ref_id not in self._queued_sources:
                    statuses = {
                        scan["status"] for scan in self._model.ocr_entries()
                        if scan["ref_id"] == row.ref_id
                    }
                    ocr_status = next(
                        (status for status in ("done", "failed", "pending") if status in statuses),
                        None,
                    )
                if ocr is not None and ocr_status:
                    ocr_color = (
                        colors["red"] if ocr_status == "failed" else
                        colors["green"] if ocr_status == "done" else colors["amber"]
                    )
                    ocr.setForeground(QtGui.QColor(ocr_color))
                    ocr.setIcon(pdf_icon(ocr_color))
                risk = self.table.item(index, 4)
                if risk is not None and row.risk_label:
                    risk.setForeground(QtGui.QColor(colors["red"]))
                review = self.table.item(index, 5)
                if review is not None and row.review_label:
                    review.setForeground(QtGui.QColor(colors["amber"]))

        def eventFilter(self, watched, event):  # noqa: N802 - Qt API name
            if watched is self.file_label:
                if (
                    event.type() == QtCore.QEvent.Type.MouseButtonRelease
                    and event.button() == QtCore.Qt.MouseButton.LeftButton
                ):
                    self._choose_file()
                    return True
                if event.type() == QtCore.QEvent.Type.KeyPress and event.key() in (
                    QtCore.Qt.Key.Key_Return,
                    QtCore.Qt.Key.Key_Enter,
                    QtCore.Qt.Key.Key_Space,
                ):
                    self._choose_file()
                    return True
            if watched is self.file_label and event.type() == QtCore.QEvent.Type.DragEnter:
                urls = event.mimeData().urls()
                if len(urls) == 1 and urls[0].isLocalFile():
                    event.acceptProposedAction()
                    return True
            if watched is self.file_label and event.type() == QtCore.QEvent.Type.Drop:
                urls = event.mimeData().urls()
                if len(urls) == 1 and urls[0].isLocalFile():
                    self._set_file(urls[0].toLocalFile())
                    event.acceptProposedAction()
                    return True
            return super().eventFilter(watched, event)

        def _render_rows(self, select_ref_id: str | None = None):
            rows = self._model.rows()
            queued_ocr = self._model.ocr_entries()
            ocr_by_ref: dict[str, list[dict[str, Any]]] = {}
            for scan in queued_ocr:
                if scan["ref_id"] not in self._queued_sources:
                    ocr_by_ref.setdefault(scan["ref_id"], []).append(scan)
            colors = _status_foregrounds(dark=self._dark_theme)
            warning_background, warning_text, _ = _identity_review_colors(dark=self._dark_theme)
            self.table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                pending_identity = self._model.identity_review(row.ref_id) is not None
                ocr_state = self._ocr_row_status.get(row.ref_id, {})
                ocr_status = ocr_state.get("status")
                if not ocr_status and row.ref_id in self._staged_ocr_refs:
                    ocr_status = "pending"
                if not ocr_status and ocr_by_ref.get(row.ref_id):
                    statuses = {scan["status"] for scan in ocr_by_ref[row.ref_id]}
                    ocr_status = next(
                        status for status in ("done", "failed", "pending")
                        if status in statuses
                    )
                ocr_label = {
                    "pending": labels["ocr_pending"],
                    "queued": labels["ocr_queued"],
                    "running": labels["ocr_running"],
                    "done": labels["ocr_done"],
                    "failed": labels["ocr_failed"],
                }.get(ocr_status, "")
                ocr_tooltip = str(ocr_state.get("reason") or "")
                if not ocr_tooltip and ocr_by_ref.get(row.ref_id):
                    ocr_tooltip = ocr_by_ref[row.ref_id][0]["reason"]
                values = [
                    (
                        f"[{row.ref_number}] · Queued"
                        if row.ref_id in self._queued_sources and row.ref_number is not None
                        else (f"[{row.ref_number}]" if row.ref_number is not None else row.ref_id)
                    ),
                    row.title or "(title unavailable)",
                    (
                        f"{row.status_marker} {row.status_label} · Source queued"
                        if row.ref_id in self._queued_sources
                        else f"{row.status_marker} {row.status_label}"
                    ),
                    ocr_label,
                    f"⚠ {row.risk_label}" if row.risk_label else "",
                    f"⚠ {row.review_label}" if row.review_label else "",
                ]
                for column, value in enumerate(values):
                    item = QtWidgets.QTableWidgetItem(value)
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, row.ref_id)
                    if pending_identity:
                        item.setBackground(QtGui.QColor(warning_background))
                        if column < 2:
                            item.setForeground(QtGui.QColor(warning_text))
                    if column == 2:
                        item.setForeground(QtGui.QColor(colors[row.status_color]))
                        item.setToolTip(row.status_tooltip)
                    if column == 3 and ocr_status:
                        ocr_color = (
                            colors["red"] if ocr_status == "failed" else
                            colors["green"] if ocr_status == "done" else colors["amber"]
                        )
                        item.setForeground(QtGui.QColor(ocr_color))
                        item.setIcon(pdf_icon(ocr_color))
                        item.setToolTip(ocr_tooltip or ocr_label)
                    if column == 4 and row.risk_label:
                        item.setForeground(QtGui.QColor(colors["red"]))
                        item.setToolTip(row.risk_tooltip)
                    if column == 5 and row.review_label:
                        item.setForeground(QtGui.QColor(colors["amber"]))
                        item.setToolTip(row.review_tooltip)
                    self.table.setItem(index, column, item)
                if ocr_status == "running":
                    progress = QtWidgets.QProgressBar(self.table)
                    progress.setObjectName("ocrRowProgress")
                    progress.setRange(0, 0)
                    progress.setTextVisible(False)
                    progress.setAccessibleName(labels["ocr_progress_accessible"].format(
                        source=row.ref_number or row.ref_id
                    ))
                    self.table.setCellWidget(index, 3, progress)
                else:
                    self.table.removeCellWidget(index, 3)
            if rows:
                target_ref_id = select_ref_id or self._current_ref_id
                target_index = next(
                    (index for index, row in enumerate(rows) if row.ref_id == target_ref_id), 0
                )
                self.table.selectRow(target_index)
            self._update_ocr_button()

        def _selected(self):
            selected = self.table.selectedItems()
            if not selected:
                self._current_ref_id = None
                self._selected_preview_path = None
                self._clear_preview()
                self._set_identity_selection_style(None)
                return
            ref_id = selected[0].data(QtCore.Qt.ItemDataRole.UserRole)
            if not isinstance(ref_id, str):
                return
            self._set_identity_selection_style(ref_id)
            detail = self._model.detail_panels(ref_id)
            preview_raw = detail.get("Preview", {}).get("raw", {})
            preview_path = preview_raw.get("path")
            preview_kind = preview_raw.get("kind")
            if not isinstance(preview_path, str):
                preview_path = None
            if ref_id != self._current_ref_id or preview_path != self._selected_preview_path:
                self._clear_preview()
            self._current_ref_id = ref_id
            self._selected_preview_path = preview_path
            for name, payload in detail.items():
                view = self._detail_views[name]
                view["headline"].setText(
                    (
                        "OCR text staged · pending Fetch checks; not admitted"
                        if preview_kind == "guided_ocr_staged"
                        else "Extracted text from selected source"
                    )
                    if name == "Preview" else payload["headline"]
                )
                view["raw"] = json.dumps(
                    payload["raw"],
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                table = view["table"]
                table.setRowCount(len(payload["rows"]))
                for index, (label, value) in enumerate(payload["rows"]):
                    table.setItem(index, 0, QtWidgets.QTableWidgetItem(label))
                    table.setItem(index, 1, QtWidgets.QTableWidgetItem(value))
            if self.tabs.currentWidget() is self._preview_tab_widget:
                self._request_preview()
            self.open_link.setEnabled(True)
            self.open_chrome.setEnabled(self._ocr_thread is None)
            can_submit = self._model.can_submit_source(ref_id)
            controls_enabled = can_submit and self._ocr_thread is None
            guidance = self._model.action_guidance(ref_id)
            rejection = self._model.rejected_source_guidance(ref_id)
            self.selection_guidance.setText(rejection or guidance)
            self.tier_switch.setEnabled(controls_enabled)
            self.fulltext_label.setEnabled(controls_enabled)
            self.abstract_label.setEnabled(controls_enabled)
            self.choose_file.setEnabled(controls_enabled)
            self.file_label.setEnabled(controls_enabled)
            selection_matches = (
                self._selected_file is not None
                and self._selected_file_ref_id == ref_id
                and self._selected_file_generation is not None
            )
            selection_key = (
                (ref_id, self._selected_file_generation) if selection_matches else None
            )
            selection_consumed = (
                selection_key in self._consumed_file_selections
                if selection_key is not None else False
            )
            self.submit.setEnabled(
                controls_enabled
                and (
                    self._selected_file is None
                    or (selection_matches and not selection_consumed)
                )
            )
            if selection_matches:
                self.file_label.setText(
                    f"{Path(self._selected_file).name} (OCR needed)"
                    if self._selected_file_ocr_pending and not selection_consumed
                    else Path(self._selected_file).name
                )
            elif self._selected_file is not None:
                self.file_label.setText("Drop one file here")
            if selection_consumed:
                ocr_status = self._ocr_row_status.get(ref_id, {}).get("status")
                self.submit.setText(
                    {
                        "queued": labels["ocr_queued"],
                        "running": labels["ocr_running"],
                        "done": labels["ocr_done"],
                        "failed": labels["ocr_failed"],
                    }.get(ocr_status, "Source queued")
                    if self._selected_file_ocr_pending else "Source queued"
                )
            elif selection_matches and self._selected_file_ocr_pending and self._run_ocr is not None:
                self.submit.setText(labels["ocr_confirm_button"])
            else:
                self.submit.setText("Confirm source")
            self._show_identity_review(ref_id)
            self._update_proceed_button()
            self.discard_source_button.setEnabled(
                can_submit
                and self._discard_source is not None
                and ref_id in self._queued_sources
            )
            self._show_queued_status(ref_id)

        def _detail_tab_changed(self, _index: int):
            if self.tabs.currentWidget() is self._preview_tab_widget:
                self._request_preview()
            else:
                self._clear_preview()

        def _clear_preview(self):
            self._preview_request_id += 1
            self._preview_request = None
            self._preview_pending_request = None
            self._preview_loaded_key = None
            view = self._detail_views.get("Preview")
            if view is None:
                return
            for animation in view["preview_animations"]:
                animation.stop()
            view["preview"].clear()
            view["preview_stack"].setCurrentWidget(view["preview"])

        def _request_preview(self):
            if (
                self.tabs.currentWidget() is not self._preview_tab_widget
                or self._current_ref_id is None
            ):
                return
            ref_id = self._current_ref_id
            path = self._selected_preview_path
            key = (ref_id, path)
            if self._preview_loaded_key == key:
                return
            if (
                self._preview_request is not None
                and self._preview_request[1:] == key
                and self._preview_request in (
                    self._preview_inflight_request, self._preview_pending_request
                )
            ):
                return

            self._preview_request_id += 1
            request = (self._preview_request_id, ref_id, path)
            self._preview_request = request
            self._preview_pending_request = None
            view = self._detail_views["Preview"]
            view["preview"].clear()
            if path is None:
                view["preview"].setPlainText("No preview available.")
                view["preview_stack"].setCurrentWidget(view["preview"])
                self._preview_loaded_key = key
                return

            self._start_preview_loading(view)
            self._preview_pending_request = request
            if self._preview_thread is None:
                self._start_preview_worker(request)

        def _start_preview_loading(self, view):
            view["preview_stack"].setCurrentWidget(view["preview_loader"])
            for animation in view["preview_animations"]:
                animation.start()

        def _start_preview_worker(self, request):
            if self._preview_thread is not None or request != self._preview_request:
                return
            path = request[2]
            if not isinstance(path, str):
                return
            self._preview_pending_request = None
            self._preview_inflight_request = request
            thread = QtCore.QThread(self)
            worker = PreviewWorker(request, path)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.loaded.connect(self._preview_worker_loaded)
            worker.loaded.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            thread.finished.connect(self._preview_worker_finished)
            thread.finished.connect(thread.deleteLater)
            self._preview_thread = thread
            self._preview_worker = worker
            thread.start()

        def _preview_worker_loaded(self, request, text: str):
            if (
                request != self._preview_request
                or self.tabs.currentWidget() is not self._preview_tab_widget
                or self._current_ref_id != request[1]
                or self._selected_preview_path != request[2]
            ):
                return
            view = self._detail_views["Preview"]
            view["preview"].setPlainText(text)
            view["preview_stack"].setCurrentWidget(view["preview"])
            for animation in view["preview_animations"]:
                animation.stop()
            self._preview_loaded_key = (request[1], request[2])

        def _preview_worker_finished(self):
            self._preview_thread = None
            self._preview_worker = None
            self._preview_inflight_request = None
            pending = self._preview_pending_request
            if pending is not None and pending == self._preview_request:
                self._start_preview_worker(pending)
            elif self._preview_close_when_finished:
                self._preview_close_when_finished = False
                self.close()

        def _set_identity_selection_style(self, ref_id: str | None):
            if ref_id is not None and self._model.identity_review(ref_id) is not None:
                background, foreground, _ = _identity_review_colors(dark=self._dark_theme)
                self.table.setStyleSheet(
                    f"QTableWidget {{ selection-background-color: {background}; "
                    f"selection-color: {foreground}; }}"
                )
            else:
                self.table.setStyleSheet("")

        def _show_identity_review(self, ref_id: str):
            review = self._model.identity_review(ref_id)
            if review is None:
                if self.tabs.currentIndex() == self._identity_review_tab:
                    self.tabs.setCurrentIndex(0)
                self.tabs.setTabEnabled(self._identity_review_tab, False)
                self.tabs.setTabIcon(self._identity_review_tab, QtGui.QIcon())
                self.tabs.tabBar().setTabTextColor(self._identity_review_tab, QtGui.QColor())
                self.identity_review_panel.setVisible(False)
                self.identity_review_reason.clear()
                return
            facts = [
                f"Identity review required for source [{self._model.row(ref_id).ref_number}].",
                f"Cited title: {review.get('cited_title') or 'unavailable'}.",
                f"Resolved title: {review.get('resolved_title') or 'unavailable'}.",
                f"Source tier: {review.get('source_tier') or 'unavailable'}.",
                f"Target SHA-256: {review['target_sha256']}.",
            ]
            source_identity = review.get("source_identity")
            if isinstance(source_identity, dict):
                source_facts = ", ".join(
                    f"{key}: {value}"
                    for key, value in source_identity.items()
                    if value is not None
                )
                if source_facts:
                    facts.append(f"Recorded source identity: {source_facts}.")
            if not self._identity_review_allowed:
                facts.append(
                    "This process is agent-identified: a separately authenticated human operator "
                    "must submit the identity decision."
                )
            if ref_id in self._recorded_identity_refs:
                facts.append(
                    "The identity decision was recorded, but this view could not be refreshed. "
                    "Close and reopen Guided Fetch to continue."
                )
            self.identity_review_facts.setText("\n".join(facts))
            enabled = (
                self._identity_review_allowed
                and self._submit_identity_review is not None
                and ref_id not in self._recorded_identity_refs
            )
            self.identity_review_reason.setEnabled(enabled)
            self.attest_identity_button.setEnabled(enabled)
            self.keep_unverified_button.setEnabled(enabled)
            self.identity_review_panel.setVisible(True)
            self.tabs.setTabEnabled(self._identity_review_tab, True)
            self.tabs.setTabIcon(
                self._identity_review_tab,
                self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MessageBoxWarning),
            )
            self.tabs.tabBar().setTabTextColor(
                self._identity_review_tab,
                QtGui.QColor(_status_foregrounds(dark=self._dark_theme)["amber"]),
            )
            self.tabs.setCurrentIndex(self._identity_review_tab)

        def _pending_identity_reviews(self):
            pending = []
            for row in self._model.rows():
                review = self._model.identity_review(row.ref_id)
                if review is not None:
                    pending.append((row.ref_id, row.ref_number, review))
            return pending

        def _update_proceed_button(self):
            pending = self._pending_identity_reviews()
            already_recorded = any(
                ref_id in self._recorded_identity_refs for ref_id, _number, _review in pending
            )
            can_resolve = (
                self._identity_review_allowed
                and self._submit_identity_review is not None
                and not already_recorded
            )
            self.proceed_button.setEnabled(
                not self._capture_tiers and self._ocr_thread is None
                and (not pending or can_resolve)
            )
            ocr_count = len(self._ocr_jobs())
            if ocr_count:
                self.proceed_button.setText(labels["ocr_skip_button"])
            else:
                self.proceed_button.setText("Proceed / skip remaining")
            blocked_explanation = ""
            if not pending:
                self.proceed_button.setToolTip(
                    "Continue with available sources and keep every unresolved retrieval waived."
                )
            elif already_recorded:
                blocked_explanation = (
                    "An identity decision was recorded, but the inventory could not be refreshed. "
                    "Close and reopen Guided Fetch before continuing."
                )
                self.proceed_button.setToolTip(
                    blocked_explanation
                )
            elif not self._identity_review_allowed:
                blocked_explanation = (
                    "This agent-identified session cannot submit source identity reviews. "
                    "A separately authenticated human operator must record the pending "
                    "identity decision before Proceed is available."
                )
                self.proceed_button.setToolTip(
                    blocked_explanation
                )
            elif self._submit_identity_review is None:
                blocked_explanation = (
                    "This session has no identity-review submission action. Reopen Guided Fetch "
                    "through the operator-controlled Fetch workflow to record the review."
                )
                self.proceed_button.setToolTip(
                    blocked_explanation
                )
            else:
                numbers = ", ".join(
                    f"[{number}]" if number is not None else f"[{ref_id}]"
                    for ref_id, number, _review in pending
                )
                blocked_explanation = (
                    f"Sources {numbers} still need an identity decision. Proceed can record "
                    "them as unverified after your confirmation."
                )
                self.proceed_button.setToolTip(
                    f"After confirmation, record sources {numbers} as identity unverified, then "
                    "waive every remaining retrieval."
                )
            self.proceed_guidance.setText(blocked_explanation)
            self.proceed_guidance.setVisible(bool(blocked_explanation))

        def _ocr_jobs(self) -> list[dict[str, Any]]:
            jobs = [
                {"ref_id": scan["ref_id"], "scan_id": scan["scan_id"]}
                for scan in self._model.ocr_queue()
                if scan["ref_id"] not in self._staged_ocr_refs
                and scan["ref_id"] not in self._queued_sources
                and self._ocr_row_status.get(scan["ref_id"], {}).get("status") != "done"
            ]
            jobs.extend(
                {"ref_id": ref_id, "scan_id": None}
                for ref_id in sorted(self._staged_ocr_refs)
                if self._ocr_row_status.get(ref_id, {}).get("status") != "done"
            )
            return jobs

        def _update_ocr_button(self):
            count = len(self._ocr_jobs())
            active = self._ocr_thread is not None
            self.ocr_button.setVisible(bool(count or active))
            self.ocr_button.setEnabled(bool(
                count and self._run_ocr is not None and not active and not self._capture_tiers
            ))
            if active:
                completed = sum(
                    self._ocr_row_status.get(job["ref_id"], {}).get("status") in {"done", "failed"}
                    for job in self._ocr_active_jobs
                )
                self.ocr_button.setText(labels["ocr_button_active"].format(
                    done=completed, total=len(self._ocr_active_jobs)
                ))
            else:
                self.ocr_button.setText(
                    labels["ocr_button_count"].format(count=count) if count else labels["ocr_button"]
                )

        def _start_ocr(self, jobs: list[dict[str, Any]] | None = None):
            if self._ocr_thread is not None or self._capture_tiers or self._run_ocr is None:
                return
            jobs = list(jobs if jobs is not None else self._ocr_jobs())
            if not jobs:
                return
            self._ocr_active_jobs = jobs
            for job in jobs:
                self._ocr_row_status[job["ref_id"]] = {"status": "queued"}
            thread = QtCore.QThread(self)
            worker = OcrWorker(self._run_ocr, jobs)
            worker.moveToThread(thread)
            self._ocr_thread = thread
            self._ocr_worker = worker
            thread.started.connect(worker.run)
            worker.progress.connect(self._ocr_progress)
            worker.finished.connect(self._ocr_finished)
            worker.failed.connect(self._ocr_failed)
            worker.finished.connect(thread.quit)
            worker.failed.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            thread.finished.connect(self._ocr_thread_finished)
            thread.start()
            self._render_rows(select_ref_id=self._current_ref_id)
            self._selected()
            self._update_proceed_button()

        def _ocr_progress(self, event: dict[str, Any]):
            ref_id = event.get("ref_id") if isinstance(event, dict) else None
            if not isinstance(ref_id, str):
                return
            status = event.get("status")
            if status not in {"queued", "running", "done", "failed"}:
                return
            self._ocr_row_status[ref_id] = {
                "status": status,
                "reason": str(event.get("reason") or ""),
            }
            if self._on_ocr_event is not None:
                self._on_ocr_event(event)
            self._render_rows(select_ref_id=self._current_ref_id)
            if ref_id == self._current_ref_id:
                self._show_queued_status(ref_id)

        def _ocr_finished(self, results: Any):
            if not isinstance(results, list):
                self._ocr_failed("OCR returned an invalid result.")
                return
            reported = set()
            completed_refs = set()
            for result in results:
                if not isinstance(result, dict) or not isinstance(result.get("ref_id"), str):
                    continue
                reported.add(result["ref_id"])
                if result.get("status") == "done":
                    completed_refs.add(result["ref_id"])
                self._ocr_row_status[result["ref_id"]] = {
                    "status": result.get("status") if result.get("status") in {"done", "failed"} else "failed",
                    "reason": str(result.get("reason") or ""),
                }
                if result.get("status") == "done":
                    self._staged_ocr_refs.discard(result["ref_id"])
            for job in self._ocr_active_jobs:
                if job["ref_id"] not in reported:
                    self._ocr_row_status[job["ref_id"]] = {
                        "status": "failed", "reason": "OCR returned no result for this PDF."
                    }
            done = sum(item.get("status") == "done" for item in results if isinstance(item, dict))
            failed = len(self._ocr_active_jobs) - done
            self.statusBar().showMessage(labels["ocr_finished_summary"].format(
                done=done, failed=failed
            ), 15_000)
            refresh_error = None
            if completed_refs:
                try:
                    self._model = GuidedFetchViewModel(
                        self._inventory_loader(self._run_dir)
                    )
                except Exception as exc:
                    refresh_error = str(exc)
            self._render_rows(select_ref_id=self._current_ref_id)
            if self._current_ref_id is not None:
                self._selected()
            if refresh_error is not None:
                self.statusBar().showMessage(
                    "OCR completed, but Guided Fetch could not refresh its preview: "
                    f"{refresh_error}. Close and reopen Guided Fetch.",
                    15_000,
                )
            self._update_proceed_button()

        def _ocr_failed(self, reason: str):
            for job in self._ocr_active_jobs:
                if self._ocr_row_status.get(job["ref_id"], {}).get("status") in {"queued", "running"}:
                    self._ocr_row_status[job["ref_id"]] = {"status": "failed", "reason": reason}
            self.statusBar().showMessage(labels["ocr_stopped_summary"].format(reason=reason), 15_000)
            self._render_rows(select_ref_id=self._current_ref_id)
            self._update_proceed_button()

        def _ocr_thread_finished(self):
            self._ocr_thread = None
            self._ocr_worker = None
            self._ocr_active_jobs = []
            self._update_ocr_button()
            self._selected()
            self._update_proceed_button()

        def _submit_identity(self, action: str):
            ref_id = self._current_ref_id
            if ref_id is None:
                return
            review = self._model.identity_review(ref_id)
            if review is None:
                return
            if not self._identity_review_allowed or self._submit_identity_review is None:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Human operator required",
                    "Source identity attestation requires a separately authenticated human operator.",
                )
                return
            reason = self.identity_review_reason.text().strip() or (
                "Operator confirmed the cited-work identity in Guided Fetch without an additional note."
                if action == "attest_identity"
                else "Operator kept the source identity unverified in Guided Fetch without an additional note."
            )
            self.attest_identity_button.setEnabled(False)
            self.keep_unverified_button.setEnabled(False)
            try:
                self._submit_identity_review(ref_id, action, review["target_sha256"], reason)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.critical(self, "Identity decision not stored", str(exc))
                self._show_identity_review(ref_id)
                return
            self._recorded_identity_refs.add(ref_id)
            try:
                self._model = GuidedFetchViewModel(self._inventory_loader(self._run_dir))
                self._render_rows(select_ref_id=ref_id)
                self._selected()
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.critical(
                    self,
                    "Identity decision recorded; refresh failed",
                    f"The decision was stored, but Guided Fetch could not refresh: {exc}",
                )
                self._show_identity_review(ref_id)
                return
            self.statusBar().showMessage("Identity decision recorded; Fetch can now continue.", 10_000)

        def _open_link(self):
            if self._current_ref_id is None:
                return
            link = self._browser_url()
            if link is not None:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(link))

        def _open_controlled_browser(self):
            if self._current_ref_id is None:
                return
            link = self._browser_url()
            if link is None:
                return
            self._browser_ref_id = self._current_ref_id
            self._browser_tier = self._selected_tier()
            self._pending_browser_url = link
            if self._browser_worker is None:
                from .browser_worker import create_browser_worker

                self._browser_thread = QtCore.QThread(self)
                self._browser_worker = create_browser_worker(self._run_dir, dark_theme=self._dark_theme)
                self._browser_worker.moveToThread(self._browser_thread)
                self.browser_start.connect(self._browser_worker.start)
                self.browser_open.connect(self._browser_worker.open)
                self.browser_capture_html.connect(self._browser_worker.capture_html)
                self.browser_capture_pdf.connect(self._browser_worker.capture_pdf)
                self.browser_capture_download.connect(self._browser_worker.capture_download)
                self.browser_discard_download.connect(self._browser_worker.discard_download)
                self.browser_park.connect(self._browser_worker.park_visible_context)
                self.browser_theme.connect(self._browser_worker.set_theme)
                self._browser_worker.availability.connect(self._browser_available)
                self._browser_worker.opened.connect(self._browser_opened)
                self._browser_worker.artifact.connect(self._browser_artifact)
                self._browser_worker.download_detected.connect(self._browser_download_detected)
                self._browser_worker.failed.connect(self._browser_failed)
                self._browser_worker.capture_requested.connect(self._capture_browser)
                self._browser_worker.closed.connect(self._browser_thread.quit)
                self._browser_worker.closed.connect(self._browser_worker.deleteLater)
                self._browser_thread.finished.connect(self._browser_finished)
                self._browser_thread.start()
                self.browser_start.emit()
                return
            self.browser_open.emit(link)

        def _browser_url(self) -> str | None:
            if self._current_ref_id is None:
                return None
            try:
                return self._model.browser_url(self._current_ref_id)
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(self, "Browser navigation", str(exc))
                return None

        def _browser_available(self, status: dict):
            if status.get("available"):
                pending_url, self._pending_browser_url = self._pending_browser_url, None
                if pending_url:
                    self.browser_open.emit(pending_url)
                return
            code = status.get("reason_code")
            if code == "google_chrome_unavailable":
                box = QtWidgets.QMessageBox(self)
                box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
                box.setWindowTitle("Google Chrome required")
                box.setText("Guided Fetch requires Google Chrome to be installed.")
                box.setInformativeText(str(status.get("reason") or "Chrome is unavailable."))
                download = box.addButton("Open official page", QtWidgets.QMessageBox.ButtonRole.ActionRole)
                retry = box.addButton("Check again", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
                box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
                box.exec()
                if box.clickedButton() is download:
                    QtGui.QDesktopServices.openUrl(QtCore.QUrl(CHROME_DOWNLOAD_URL))
                elif box.clickedButton() is retry:
                    self.browser_start.emit()
                return
            if code == "browser_launch_failed":
                QtWidgets.QMessageBox.critical(
                    self,
                    "Google Chrome could not be started",
                    str(status.get("reason") or "Google Chrome could not be started."),
                )
                return
            QtWidgets.QMessageBox.critical(
                self,
                "Assisted browser unavailable",
                str(status.get("reason") or "Browser unavailable."),
            )

        def _browser_opened(self, navigation: dict):
            self.statusBar().showMessage(
                f"Chrome opened at {navigation.get('url') or navigation.get('requested_url')}",
                10_000,
            )

        def _capture_browser(self, kind: str):
            if self._browser_ref_id is None or self._browser_worker is None:
                return
            if not self._model.can_submit_source(self._browser_ref_id):
                return
            if self._capture_tiers:
                return
            index = self._capture_index
            self._capture_index += 1
            ref_id = self._browser_ref_id
            self._capture_tiers[(ref_id, index)] = self._browser_tier or "fulltext"
            self._update_proceed_button()
            signal = self.browser_capture_pdf if kind == "pdf" else self.browser_capture_html
            signal.emit(ref_id, index)

        def _browser_download_detected(self, notice: dict):
            token = notice.get("token")
            filename = _normalized_download_name(
                notice.get("display_name")
                or notice.get("suggested_filename")
                or notice.get("filename")
            )
            if isinstance(token, bool) or not isinstance(token, int) or filename is None:
                self._browser_failed("Invalid browser download notice.")
                return
            if self._browser_ref_id is None or not self._model.can_submit_source(self._browser_ref_id):
                self.browser_discard_download.emit(token)
                return
            question = f"You downloaded {filename}. Is this the exact cited source you wanted?"
            if not self._application_modal_question("Confirm downloaded source", question):
                self.browser_discard_download.emit(token)
                return
            if self._capture_tiers:
                self.browser_discard_download.emit(token)
                return
            index = self._capture_index
            self._capture_index += 1
            ref_id = self._browser_ref_id
            self._capture_tiers[(ref_id, index)] = self._browser_tier or "fulltext"
            self._preconfirmed_captures.add((ref_id, index))
            self.proceed_button.setEnabled(False)
            self.browser_capture_download.emit(ref_id, index, token)

        def _browser_artifact(self, artifact: dict):
            ref_id = artifact.get("ref_id")
            index = artifact.get("capture_index")
            path = artifact.get("artifact_path")
            if not isinstance(ref_id, str) or not isinstance(index, int) or not isinstance(path, str):
                self._browser_failed("Invalid browser artifact.")
                return
            tier = self._capture_tiers.pop((ref_id, index), "fulltext")
            preconfirmed = (ref_id, index) in self._preconfirmed_captures
            self._preconfirmed_captures.discard((ref_id, index))
            try:
                row = self._model.row(ref_id)
            except KeyError:
                self._finish_browser_capture()
                self._browser_failed("Browser artifact has an unknown reference target.")
                return
            question = (
                f"Confirm that the captured browser artifact is the exact cited work "
                f"for source [{row.ref_number}]?"
            )
            if not preconfirmed and not self._application_modal_question(
                "Confirm captured source", question
            ):
                self._finish_browser_capture()
                return
            try:
                preflight = _preflight_source_file(path, tier)
                payload = source_payload(
                    file_path=path,
                    tier=tier,
                    source_ref=artifact.get("url") or artifact.get("requested_url"),
                )
                self._submit_source(ref_id, payload)
            except (OSError, ValueError) as exc:
                self._finish_browser_capture()
                self._browser_failed(str(exc))
                return
            self._finish_browser_capture()
            self._mark_queued(
                ref_id,
                _normalized_download_name(artifact.get("display_name")) or Path(path).name,
                audit_path=path,
                ocr_pending=preflight == "ocr_pending",
            )
            self.browser_park.emit()
            if preflight == "ocr_pending" and self._run_ocr is not None:
                self._start_ocr([{"ref_id": ref_id, "scan_id": None}])
            else:
                self._application_modal_information(
                    "Source queued",
                    "You confirmed this artifact is the exact cited work. "
                    + (
                        "This PDF needs OCR before its text can be assessed. "
                        if preflight == "ocr_pending" else "Readable text was detected. "
                    )
                    + "Final evidence checks will run when Fetch resumes.",
                )

        def _browser_failed(self, reason: str):
            if self._capture_tiers:
                self._capture_tiers.clear()
                self._preconfirmed_captures.clear()
                self._finish_browser_capture()
            QtWidgets.QMessageBox.warning(self, "Browser capture", reason)

        def _finish_browser_capture(self):
            self._update_proceed_button()

        def _choose_file(self):
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose source")
            if path:
                self._set_file(path)

        def _set_file(self, path: str):
            if (
                self._current_ref_id is None
                or not self._model.can_submit_source(self._current_ref_id)
            ):
                QtWidgets.QMessageBox.information(
                    self,
                    "No Fetch action",
                    "Callimachus is not requesting a source for this reference.",
                )
                return
            ref_id = self._current_ref_id
            self._next_file_generation += 1
            generation = self._next_file_generation
            candidate = Path(path)
            self._selected_file = None
            self._selected_file_ref_id = None
            self._selected_file_generation = None
            self._selected_file_ocr_pending = False
            self.file_label.setText("Drop one file here")
            if not candidate.is_file():
                QtWidgets.QMessageBox.warning(self, "Invalid file", "Choose one existing file.")
                return
            try:
                preflight = _preflight_source_file(str(candidate), self._selected_tier())
            except (OSError, ValueError) as exc:
                self.file_label.setText("File rejected. Choose another file.")
                self.file_label.setToolTip(str(exc))
                QtWidgets.QMessageBox.warning(self, "Source cannot be used", str(exc))
                return
            self._selected_file = str(candidate)
            self._selected_file_ref_id = ref_id
            self._selected_file_generation = generation
            self._selected_file_ocr_pending = preflight == "ocr_pending"
            self.file_label.setToolTip("")
            self.file_label.setText(
                f"{candidate.name} (OCR needed)" if preflight == "ocr_pending"
                else candidate.name
            )
            self.submit.setText(
                labels["ocr_confirm_button"] if preflight == "ocr_pending" and self._run_ocr is not None
                else "Confirm source"
            )
            self.submit.setEnabled(self._ocr_thread is None)

        def _submit_file(self):
            if self._ocr_thread is not None:
                return
            ref_id = self._current_ref_id
            file_path = self._selected_file
            file_generation = self._selected_file_generation
            if ref_id is None or file_path is None:
                QtWidgets.QMessageBox.information(self, "Source required", "Choose a source and a file.")
                return
            if self._selected_file_ref_id != ref_id or file_generation is None:
                QtWidgets.QMessageBox.information(
                    self, "Source required", "Choose a file for the selected source."
                )
                return
            selection_key = (ref_id, file_generation)
            if selection_key in self._consumed_file_selections:
                return
            if not self._model.can_submit_source(ref_id):
                QtWidgets.QMessageBox.information(
                    self,
                    "No Fetch action",
                    "Callimachus is not requesting a source for this reference.",
                )
                return
            try:
                tier = self._selected_tier()
                preflight = _preflight_source_file(file_path, tier)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.critical(self, "Source cannot be used", str(exc))
                return
            row = self._model.row(ref_id)
            question = (
                f"Confirm that '{Path(file_path).name}' is the exact cited work "
                f"for source [{row.ref_number}]?"
            )
            if preflight == "ocr_pending" and self._run_ocr is not None:
                question += labels["ocr_confirm_note"]
            if (
                QtWidgets.QMessageBox.question(self, "Confirm source", question)
                != QtWidgets.QMessageBox.StandardButton.Yes
            ):
                return
            if (
                self._current_ref_id != ref_id
                or self._selected_file != file_path
                or self._selected_file_ref_id != ref_id
                or self._selected_file_generation != file_generation
            ):
                return
            try:
                payload = source_payload(file_path=file_path, tier=tier)
                self._submit_source(ref_id, payload)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.critical(self, "Source not captured", str(exc))
                return
            self._consumed_file_selections.add(selection_key)
            self._mark_queued(ref_id, Path(file_path).name,
                              ocr_pending=preflight == "ocr_pending")
            if preflight == "ocr_pending" and self._run_ocr is not None:
                self._start_ocr([{"ref_id": ref_id, "scan_id": None}])
            else:
                QtWidgets.QMessageBox.information(
                    self,
                    "Source queued",
                    "You confirmed this file is the exact cited work. "
                    + (
                        "This PDF needs OCR before its text can be assessed. "
                        if preflight == "ocr_pending" else "Readable text was detected. "
                    )
                    + "Final evidence checks will run when Fetch resumes.",
                )

        def _mark_queued(self, ref_id: str, source_name: str, *, audit_path: str | None = None,
                         ocr_pending: bool = False):
            self._queued_sources[ref_id] = (source_name, audit_path)
            self._ocr_row_status.pop(ref_id, None)
            if ocr_pending:
                self._staged_ocr_refs.add(ref_id)
            else:
                self._staged_ocr_refs.discard(ref_id)
            self._render_rows(select_ref_id=ref_id)
            self._selected()
            self._show_queued_status(ref_id)

        def _discard_queued_source(self):
            ref_id = self._current_ref_id
            if (
                ref_id is None
                or ref_id not in self._queued_sources
                or self._discard_source is None
            ):
                return
            if (
                QtWidgets.QMessageBox.question(
                    self,
                    "Discard provided source",
                    "Discard this queued source? Its captured audit file will be kept, "
                    "but this reference will be waived if you proceed without providing another source.",
                )
                != QtWidgets.QMessageBox.StandardButton.Yes
            ):
                return
            try:
                self._discard_source(ref_id)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.critical(self, "Source not discarded", str(exc))
                return
            self._queued_sources.pop(ref_id, None)
            self._staged_ocr_refs.discard(ref_id)
            self._ocr_row_status.pop(ref_id, None)
            self._selected_file = None
            self._selected_file_ref_id = None
            self._selected_file_generation = None
            self._selected_file_ocr_pending = False
            self.file_label.setText("Drop one file here")
            self._render_rows(select_ref_id=ref_id)
            self._selected()
            self._show_queued_status(ref_id)
            self.statusBar().showMessage("Provided source discarded; it is no longer queued.", 10_000)
            QtWidgets.QMessageBox.information(
                self,
                "Source discarded",
                "The artifact is no longer queued. Its captured audit file was not deleted.",
            )

        def _show_queued_status(self, ref_id: str):
            queued = self._queued_sources.get(ref_id)
            if queued is None:
                self.capture_status_panel.setVisible(False)
                self.capture_status.setVisible(False)
                self.discard_source_button.setVisible(False)
                return
            source_name, audit_path = queued
            audit_suffix = (
                f" Saved in {Path(audit_path).parent.name}." if audit_path else ""
            )
            ocr_status = self._ocr_row_status.get(ref_id, {}).get("status")
            if ocr_status == "done":
                status_text = f"{source_name} captured/selected and queued; OCR text extracted."
            elif ref_id in self._staged_ocr_refs or ocr_status in {"queued", "running", "failed"}:
                status_text = f"{source_name} captured/selected and queued; OCR is required before its text can be assessed."
            else:
                status_text = f"{source_name} captured/selected and queued; readability checked before Proceed."
            self.capture_status.setText(
                f"{status_text}{audit_suffix}"
            )
            self.capture_status.setToolTip(
                f"Audit capture saved at: {audit_path}" if audit_path else ""
            )
            self.capture_status.setVisible(True)
            self.discard_source_button.setVisible(True)
            self.capture_status_panel.setVisible(True)

        def _application_modal_question(self, title: str, text: str) -> bool:
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Icon.Question)
            box.setWindowTitle(title)
            box.setText(text)
            box.setStandardButtons(
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
            )
            box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
            self._prepare_browser_modal(box)
            return box.exec() == QtWidgets.QMessageBox.StandardButton.Yes

        def _application_modal_information(self, title: str, text: str):
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Icon.Information)
            box.setWindowTitle(title)
            box.setText(text)
            box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
            self._prepare_browser_modal(box)
            box.exec()

        def _prepare_browser_modal(self, box):
            box.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
            box.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
            box.show()
            box.raise_()
            box.activateWindow()

        def _proceed_clicked(self):
            if self._capture_tiers or self._ocr_thread is not None:
                return
            pending_reviews = self._pending_identity_reviews()
            already_recorded = any(
                ref_id in self._recorded_identity_refs
                for ref_id, _number, _review in pending_reviews
            )
            if already_recorded:
                QtWidgets.QMessageBox.critical(
                    self,
                    "Identity decision recorded; refresh failed",
                    "An identity decision was recorded, but Guided Fetch could not confirm the "
                    "updated inventory. Close and reopen Guided Fetch before continuing.",
                )
                self._update_proceed_button()
                return
            if pending_reviews and (
                not self._identity_review_allowed or self._submit_identity_review is None
            ):
                self._update_proceed_button()
                QtWidgets.QMessageBox.warning(
                    self,
                    "Human operator required",
                    "Pending source identity reviews must be submitted by a separately "
                    "authenticated human operator before Proceed can waive the remaining "
                    "Fetch tasks.",
                )
                return
            all_fetch_answers_staged = all(
                not self._model.can_submit_source(row.ref_id)
                or row.ref_id in self._queued_sources
                for row in self._model.rows()
            )
            manual_work_resolved = (
                all_fetch_answers_staged
                and not self._ocr_jobs()
                and not pending_reviews
                and not already_recorded
            )
            if pending_reviews:
                source_numbers = ", ".join(
                    f"[{number}]" if number is not None else f"[{ref_id}]"
                    for ref_id, number, _review in pending_reviews
                )
                question = (
                    "Proceed with available sources and skip every remaining retrieval? "
                    f"Pending identity reviews for sources {source_numbers} will be recorded "
                    "as identity unverified. Remaining retrievals will be recorded as waived "
                    "and unavailable for verification. No source text will be inferred or created."
                )
            else:
                question = (
                    "Proceed with the available sources and skip every remaining retrieval? "
                    "Skipped sources will be recorded as waived and will remain unavailable "
                    "for verification. No source text will be inferred or created."
                )
            ocr_count = len(self._ocr_jobs())
            if ocr_count:
                question += labels["ocr_skip_note"].format(count=ocr_count)
            if (
                not manual_work_resolved
                and QtWidgets.QMessageBox.question(self, "Proceed", question)
                != QtWidgets.QMessageBox.StandardButton.Yes
            ):
                return
            if pending_reviews:
                self.proceed_button.setEnabled(False)
                for ref_id, ref_number, review in pending_reviews:
                    source_number = ref_number if ref_number is not None else ref_id
                    reason = (
                        "The operator confirmed Guided Fetch Proceed / skip remaining; "
                        f"the identity of source [{source_number}] remains unverified."
                    )
                    try:
                        self._submit_identity_review(
                            ref_id,
                            "keep_unverified",
                            review["target_sha256"],
                            reason,
                        )
                    except Exception as exc:
                        self._update_proceed_button()
                        QtWidgets.QMessageBox.critical(
                            self,
                            "Unable to record identity decision",
                            f"The identity decision for source [{source_number}] could not be "
                            f"confirmed: {exc}. Proceed was not submitted.",
                        )
                        return
                    self._recorded_identity_refs.add(ref_id)
                try:
                    self._model = GuidedFetchViewModel(self._inventory_loader(self._run_dir))
                    self._render_rows(select_ref_id=self._current_ref_id)
                    self._selected()
                except Exception as exc:
                    self._update_proceed_button()
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Identity decision recorded; refresh failed",
                        "The identity decision was stored, but Guided Fetch could not confirm "
                        f"the updated inventory: {exc}. Proceed was not submitted; close and "
                        "reopen Guided Fetch before continuing.",
                    )
                    return
                if self._model.has_pending_identity_reviews():
                    self._update_proceed_button()
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Identity review still pending",
                        "Guided Fetch still reports a pending identity review after submission. "
                        "Proceed was not submitted; close and reopen Guided Fetch before continuing.",
                    )
                    return
            try:
                self._proceed()
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.critical(self, "Unable to proceed", str(exc))
                return
            self.close()

        def _browser_finished(self):
            if self._browser_thread is not None and not self._browser_thread.isRunning():
                self._browser_worker = None
                self._browser_thread = None
            if self._closing:
                self._closing = False
                self.close()

        def closeEvent(self, event):  # noqa: N802 - Qt API name
            if self._ocr_thread is not None:
                self.statusBar().showMessage(labels["ocr_close_busy"], 10_000)
                event.ignore()
                return
            if self._preview_thread is not None:
                if self._preview_thread.isRunning():
                    self._clear_preview()
                    self._preview_close_when_finished = True
                    event.ignore()
                    return
                self._preview_thread.wait()
                self._preview_thread = None
                self._preview_worker = None
                self._preview_inflight_request = None
            if self._browser_worker is not None and self._browser_thread is not None:
                if self._browser_thread.isRunning():
                    QtCore.QMetaObject.invokeMethod(
                        self._browser_worker,
                        "close",
                        QtCore.Qt.ConnectionType.QueuedConnection,
                    )
                    self._closing = True
                    event.ignore()
                    return
            super().closeEvent(event)

    return GuidedFetchWindow()


def run_guided_fetch_window(
    run_dir: str,
    *,
    submit_source: Callable[[str, dict[str, Any]], Any],
    proceed: Callable[[], Any],
    discard_source: Callable[[str], Any] | None = None,
    submit_identity_review: Callable[[str, str, str, str], Any] | None = None,
    identity_review_allowed: bool = True,
) -> int:
    """Run the window with the caller-provided write actions."""
    _, _, QtWidgets = _load_qt()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = create_guided_fetch_window(
        run_dir,
        submit_source=submit_source,
        proceed=proceed,
        discard_source=discard_source,
        submit_identity_review=submit_identity_review,
        identity_review_allowed=identity_review_allowed,
    )
    window.show()
    return app.exec()


def _load_qt():
    from PySide6 import QtCore, QtGui, QtWidgets
    return QtCore, QtGui, QtWidgets


def _callimachus_icon(QtCore, QtGui):
    """Load the shared application icon without duplicating splash logic."""
    from .startup_splash import _callimachus_icon as load_icon

    return load_icon(QtCore, QtGui)


def _callimachus_wordmark(QtCore, QtGui, width: int, height: int, device_pixel_ratio: float = 1.0, *, dark: bool = False):
    """Render the official transparent wordmark in the compact header."""
    name = "logo-dark.png" if dark else "logo.png"
    path = Path(__file__).resolve().parent / "assets" / name
    source = QtGui.QPixmap(str(path))
    if source.isNull():
        return source
    source = source.copy(QtCore.QRect(145, 109, 2196, 360))
    pixmap = source.scaled(
        max(1, round(width * device_pixel_ratio)),
        max(1, round(height * device_pixel_ratio)),
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation,
    )
    pixmap.setDevicePixelRatio(device_pixel_ratio)
    return pixmap


def _theme_stylesheet(*, dark: bool, font_size: int = 13) -> str:
    colors = (
        (
            "#1b1f24", "#edf3ff", "#101722", "#1f2b3d", "#364152",
            "#7aa7e8", "#2457a6", "#2d4d73", "#edf3ff", "#aeb8c7",
            "#edf3ff", "#2457a6", "#3974c6", "#183d78",
        )
        if dark else
        (
            "#f5f7fb", "#18202a", "#ffffff", "#eaf2ff", "#cbd5e1",
            "#2457a6", "#1d4f91", "#dcecff", "#1d4f91", "#52606d",
            "#1d4f91", "#2457a6", "#1d4f91", "#163f75",
        )
    )
    (
        background, text, surface, accent_surface, border, blue, blue_dark,
        selection, button_text, disabled_text, hover_text, active_button,
        active_button_hover, pressed_button,
    ) = colors
    ocr_background = "#694a16" if dark else "#fff0bf"
    ocr_hover = "#7b581c" if dark else "#ffe39b"
    ocr_foreground = "#fff4da" if dark else "#613d00"
    ocr_border = "#b4832b" if dark else "#b87713"
    return f"""
        QWidget {{ background: {background}; color: {text}; font-family: 'Segoe UI', sans-serif; font-size: {font_size}px; }}
        #brandHeader, #selectionGuidance, #actionPanel {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; padding: 7px; }}
        #brandHeader QWidget, #brandHeader QLabel {{ background: transparent; }}
        #selectionGuidance {{ line-height: 1.25; }}
        #captureStatusPanel {{ background: {accent_surface}; border: 1px solid {blue}; border-radius: 8px; }}
        #captureStatus {{ background: transparent; color: {text}; border: 0; padding: 0; font-weight: 600; }}
        QPlainTextEdit, QTableWidget {{ background: {surface}; color: {text}; border: 1px solid {border}; border-radius: 8px; }}
        QTableWidget {{ gridline-color: {border}; alternate-background-color: {accent_surface}; selection-background-color: {selection}; selection-color: {text}; }}
        QHeaderView::section {{ background: {accent_surface}; color: {text}; border: 0; border-bottom: 1px solid {border}; padding: 5px 7px; font-weight: 600; }}
        QTabBar::tab {{ background: transparent; padding: 6px 11px; margin-right: 2px; }}
        QTabBar::tab:hover {{ background: {selection}; color: {hover_text}; border-radius: 6px; }}
        QTabBar::tab:pressed {{ background: {pressed_button}; color: white; }}
        QTabBar::tab:selected {{ color: {blue}; border-bottom: 3px solid {blue}; font-weight: 600; }}
        #detailHeadline {{ font-size: 16px; font-weight: 700; padding: 0; border: 0; }}
        QPushButton {{ background: {accent_surface}; color: {button_text}; border: 1px solid {border}; border-radius: 7px; padding: 6px 9px; font-weight: 600; }}
        QPushButton:hover {{ background: {selection}; color: {hover_text}; border-color: {blue}; }}
        QPushButton:pressed {{ background: {pressed_button}; color: white; border-color: {pressed_button}; }}
        QPushButton:disabled {{ background: {background}; color: {disabled_text}; border-color: {border}; }}
        QPushButton:checked {{ background: {active_button}; color: white; border-color: {blue}; }}
        QPushButton:checked:hover {{ background: {active_button_hover}; color: white; }}
        QPushButton:checked:pressed {{ background: {pressed_button}; color: white; border-color: {pressed_button}; }}
        #themeToggle {{ min-width: 96px; }}
        #proceedButton, #discardSourceButton {{ background: {active_button}; color: white; border-color: {blue}; }}
        #proceedButton:hover, #discardSourceButton:hover {{ background: {active_button_hover}; color: white; }}
        #proceedButton:pressed, #discardSourceButton:pressed {{ background: {pressed_button}; color: white; border-color: {pressed_button}; }}
        #proceedButton:disabled, #discardSourceButton:disabled {{ background: {background}; color: {disabled_text}; border-color: {border}; }}
        #runOcrButton {{ background: {ocr_background}; color: {ocr_foreground}; border-color: {ocr_border}; }}
        #runOcrButton:hover {{ background: {ocr_hover}; color: {ocr_foreground}; }}
        #runOcrButton:disabled {{ background: {background}; color: {disabled_text}; border-color: {border}; }}
        #ocrRowProgress {{ background: {surface}; border: 1px solid {ocr_border}; border-radius: 4px; min-height: 8px; }}
        #ocrRowProgress::chunk {{ background: {ocr_border}; border-radius: 3px; }}
        QFrame#previewSkeletonBar {{ background-color: {'#40516b' if dark else '#d7e2f1'}; border: 0; border-radius: 7px; }}
        #fileDropArea {{ border: 2px dashed {blue}; border-radius: 8px; padding: 6px; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 3px 1px; }}
        QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 28px; }}
        QScrollBar::handle:vertical:hover {{ background: {blue}; }}
        QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 1px 3px; }}
        QScrollBar::handle:horizontal {{ background: {border}; border-radius: 5px; min-width: 28px; }}
        QScrollBar::handle:horizontal:hover {{ background: {blue}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; background: transparent; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """


def _identity_review_colors(*, dark: bool) -> tuple[str, str, str]:
    """Readable warning surface, text, and border for a pending identity decision."""
    return ("#3d331c", "#fff0c2", "#e0ad45") if dark else (
        "#fff4d1", "#3b2d0a", "#c28a00"
    )


def _status_foregrounds(*, dark: bool) -> dict[str, str]:
    """Readable semantic foregrounds for the source-availability column."""
    return (
        {
            "green": "#69db9c", "yellow": "#ffd166", "red": "#ff8787",
            "amber": "#ffd166", "purple": "#d0bfff", "blue": "#7dd3fc",
            "neutral": "#e5e7eb",
        }
        if dark
        else {
            "green": "#147a3d", "yellow": "#8a6200", "red": "#b42318",
            "amber": "#9a6700", "purple": "#7048a8", "blue": "#1d4f91",
            "neutral": "#4b5563",
        }
    )


def _normalized_download_name(value: Any) -> str | None:
    """Return a display-safe filename without altering the audited artifact path."""
    if not isinstance(value, str) or not value.strip():
        return None
    name = Path(value.replace("\\", "/")).name.strip()
    return name or None


def _read_preview(path: str | None) -> str:
    if path is None:
        return "No preview available."
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as source:
            return source.read(100_000)
    except (OSError, ValueError):
        return "Preview unavailable."
