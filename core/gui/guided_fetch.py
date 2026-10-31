# core/gui/guided_fetch.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""PySide6 implementation of the optional guided Fetch desktop window.

Qt is imported only inside the public factories so importing this module stays
safe on server and CI installations where the optional extra is absent.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from core.app.guided_fetch import build_source_inventory, source_payload
from core.shared.filesystem_paths import io_path
from .atlante import stylesheet as atlante_stylesheet
from .guided_fetch_viewmodel import GuidedFetchViewModel
from .source_filters import (
    configure_source_filter_button,
    create_checkbox_source_menu,
    refresh_checkbox_source_menu,
    set_source_filter_count,
)


CHROME_DOWNLOAD_URL = "https://www.google.com/chrome/"
_OCR_DEFAULT = {
    "guided_fetch_browser_toolbar_pending": "Waiting for the publisher page. Complete any check in Chrome; capture controls will appear when ready.",
    "guided_fetch_sources_title": "Session sources",
    "guided_fetch_back_to_sources": "Back to Fetch",
    "guided_fetch_next_unresolved": "Next",
    "guided_fetch_source_count": "{count} sources",
    "guided_fetch_show_sources": "Show sources",
    "guided_fetch_filter_all": "All sources",
    "guided_fetch_filter_fulltext": "Full text",
    "guided_fetch_filter_abstract": "Abstract only",
    "guided_fetch_filter_fulltext_missing": "Full text missing",
    "guided_fetch_filter_none": "No text",
    "guided_fetch_filter_identity_required": "Identity decision required",
    "guided_fetch_clear_filters": "Remove filters",
    "guided_fetch_cloudflare_notice": (
        "Automatic retrieval encountered a Cloudflare challenge for this source. "
        "The assisted browser may not complete it; use the system browser instead, "
        "then provide the acquired file in Callimachus."
    ),
    "guided_fetch_open_source": "Open a source",
    "guided_fetch_provide_source": "Provide a source",
    "guided_fetch_tab_parsed": "Parsed",
    "guided_fetch_tab_resolved": "Resolved",
    "guided_fetch_tab_acquired": "Acquired",
    "guided_fetch_tab_preview": "Preview",
    "guided_fetch_parsed_text_unavailable": "Parsed text unavailable.",
    "guided_fetch_parsed_text_loading": "Loading parsed text…",
    "guided_fetch_parsed_text_close": "Close",
    "guided_fetch_tab_identity": "Identity",
    "guided_fetch_system_browser": "System browser",
    "guided_fetch_assisted_browser": "Assisted Chrome",
    "guided_fetch_continue": "Proceed / skip remaining",
    "guided_fetch_discard": "Discard provided source",
    "guided_fetch_drop_file": "Drop one file here",
    "guided_fetch_detail_column": "Detail",
    "guided_fetch_recorded_value": "Recorded value",
    "guided_fetch_show_original_json": "Show original JSON",
    "guided_fetch_filter_tooltip": "Show sources by text availability or identity review",
    "guided_fetch_select_reference": "Select a reference.",
    "guided_fetch_no_sources_match": "No sources match this filter.",
    "guided_fetch_instructions_tooltip": "Guided Fetch instructions",
    "guided_fetch_instructions": "Guided Fetch instructions",
    "guided_fetch_source_tier_accessible": "Source tier: Full text or Abstract",
    "guided_fetch_source_tier_tooltip": "Off: Full text. On: Abstract.",
    "guided_fetch_queued": "Queued",
    "guided_fetch_original_audit_json": "Original audit JSON",
    "guided_fetch_confirm_source": "Confirm source",
    "guided_fetch_confirm_action": "Confirm",
    "guided_fetch_cancel_action": "Cancel",
    "guided_fetch_capture_hint": "Capture from the assisted Chrome toolbar.",
    "guided_fetch_capture_title": "Capture source",
    "guided_fetch_capture_pdf_check_title": "Check captured PDF",
    "guided_fetch_capture_message": "Capturing source…",
    "guided_fetch_capture_pdf_message": "Capturing PDF…",
    "guided_fetch_capture_text_message": "Capturing text…",
    "guided_fetch_capture_page_message": "Capturing web page…",
    "guided_fetch_capture_pdf_check_message": "Checking captured PDF…",
    "guided_fetch_capture_cancel": "Cancel",
    "guided_fetch_capture_canceling": "Canceling capture…",
    "guided_fetch_proceed_dialog_title": "Proceed",
    "guided_fetch_proceed_question": (
        "Proceed with the available sources and skip every remaining retrieval? "
        "Any identity review that becomes available for sources added during this "
        "Fetch will also be skipped, and those sources will remain unverified. "
        "Skipped sources will be recorded as waived and will remain unavailable "
        "for verification. No source text will be inferred or created."
    ),
    "guided_fetch_proceed_identity_question": (
        "Proceed with available sources and skip every remaining retrieval? "
        "Pending identity reviews for sources {source_numbers}, and any review "
        "that becomes available for sources added during this Fetch, will be "
        "skipped; those sources will remain unverified. Remaining retrievals will be recorded as waived "
        "and unavailable for verification. No source text will be inferred or created."
    ),
    "guided_fetch_summary_pdf_pending": "{count} PDF checks pending",
    "guided_fetch_summary_identity_pending": "{count} identity decisions pending",
    "guided_fetch_summary_ocr_pending": "{count} OCR jobs needed",
    "guided_fetch_status_known_no_text": "No text acquired",
    "guided_fetch_status_identity_uncertain": "Identity uncertain",
    "guided_fetch_continue_tooltip": (
        "Continue with available sources and keep every unresolved retrieval waived."
    ),
    "guided_fetch_identity_required": "Identity review required for source [{number}].",
    "guided_fetch_identity_citation": "Citation",
    "guided_fetch_identity_cited_title": "Cited title",
    "guided_fetch_identity_resolved_title": "Resolved title",
    "guided_fetch_identity_resolved_identifier": "Resolved identifier",
    "guided_fetch_identity_source_tier": "Source tier",
    "guided_fetch_identity_target_sha256": "Target SHA-256",
    "guided_fetch_identity_source_identity": "Recorded source identity",
    "guided_fetch_identity_unavailable": "unavailable",
    "guided_fetch_identity_operator_required": (
        "This process is agent-identified: a separately authenticated human operator "
        "must submit the identity decision."
    ),
    "guided_fetch_identity_refresh_failed": (
        "The identity decision was recorded, but this view could not be refreshed. "
        "Close and reopen Guided Fetch to continue."
    ),
    "guided_fetch_identity_note_placeholder": "Optional note for this identity decision",
    "guided_fetch_identity_confirm": "Confirm: this is the cited work",
    "guided_fetch_identity_keep_unverified": "Keep unverified",
    "guided_fetch_identity_pending_guidance": (
        "Sources {numbers} still need an identity decision. Proceed can record them "
        "as unverified after your confirmation."
    ),
    "guided_fetch_identity_pending_tooltip": (
        "After confirmation, record sources {numbers} as identity unverified, then "
        "waive every remaining retrieval."
    ),
    "guided_fetch_identity_agent_blocked": (
        "This agent-identified session cannot submit source identity reviews. A "
        "separately authenticated human operator must record the pending identity "
        "decision before Proceed is available."
    ),
    "guided_fetch_identity_submission_unavailable": (
        "This session has no identity-review submission action. Reopen Guided Fetch "
        "through the operator-controlled Fetch workflow to record the review."
    ),
    "guided_fetch_pending_ocr_admission": (
        "OCR text staged · pending Fetch checks; not admitted"
    ),
    "guided_fetch_pending_source_admission": (
        "Provided source queued · pending Fetch checks; not admitted"
    ),
    "guided_fetch_staged_ocr_heading": "Staged OCR text",
    "guided_fetch_staged_ocr_message": (
        "This OCR text is staged and pending Fetch checks; it is not an admitted source."
    ),
    "guided_fetch_staged_ocr_status": (
        "Staged OCR text; pending Fetch checks and not admitted for verification."
    ),
    "guided_fetch_staged_source_heading": "Queued source",
    "guided_fetch_staged_source_message": (
        "This provided file is queued pending Fetch checks; it has not been admitted as evidence."
    ),
    "guided_fetch_staged_source_status": (
        "Provided source is queued pending Fetch checks and is not admitted for verification."
    ),
    "guided_fetch_action_guidance_fulltext": (
        "Full text is already available. No action is required unless the acquired "
        "document is visibly the wrong work."
    ),
    "guided_fetch_action_guidance_review_labels": (
        "Review labels: {labels}. They are informational only and not proof of "
        "fabrication. Inspect the recorded evidence, then supply only a captured "
        "file for the exact cited work; otherwise leave it pending and use Proceed / "
        "skip remaining."
    ),
    "guided_fetch_action_guidance_abstract": (
        "Callimachus currently has an abstract. Supply only a captured file for the "
        "exact cited work. If you cannot verify it, leave it pending and use Proceed / "
        "skip remaining; it will remain unavailable for verification."
    ),
    "guided_fetch_action_guidance_no_text": (
        "Callimachus currently has no usable text. Supply only a captured file for the "
        "exact cited work. If you cannot verify it, leave it pending and use Proceed / "
        "skip remaining; it will remain unavailable for verification."
    ),
    "guided_fetch_pdf_checking": "Checking PDF…",
    "guided_fetch_pdf_check_queued": "PDF check queued",
    "guided_fetch_cancel_pdf_check": "Cancel PDF check",
    "guided_fetch_pdf_checking_detail": "Checking {source} for readable text.",
    "guided_fetch_pdf_check_queued_detail": (
        "{source} is waiting for its PDF readability check."
    ),
    "guided_fetch_pdf_check_cancel_tooltip": (
        "Cancel this reference's PDF readability check and discard its result."
    ),
    "guided_fetch_pdf_check_running_note": (
        "You can continue searching while this PDF check runs."
    ),
    "guided_fetch_queued_ocr_done_detail": (
        "{source} captured/selected and queued; OCR text extracted."
    ),
    "guided_fetch_queued_ocr_pending_detail": (
        "{source} captured/selected and queued; OCR is required before its text can be assessed."
    ),
    "guided_fetch_queued_readable_detail": (
        "{source} captured/selected and queued; readability checked before Proceed."
    ),
    "guided_fetch_saved_in_detail": "Saved in {folder}.",
    "guided_fetch_audit_capture_saved_detail": "Audit capture saved at: {path}",
    "fulltext": "Full text",
    "abstract": "Abstract",
    "atlas_choose_file": "Choose file",
    "source": "Source",
    "title": "Title",
    "text_availability": "Text availability",
    "risk_signal": "Risk signal",
    "review_labels": "Review labels",
    "guided_fetch_review": "Review labels",
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


def _preflight_source_file(path: str, tier: str, run_dir: str | None = None) -> str:
    """Check readable source bytes before staging, without admitting evidence."""
    from core.fetch.extraction import fetch_html
    from core.parse import extract as parse_extract

    candidate = Path(path)
    candidate_io = Path(io_path(candidate))
    suffix = candidate.suffix.lower()
    try:
        parse_extract.probe_file(str(candidate_io))
    except (OSError, RuntimeError, ValueError) as exc:
        detail = str(exc).replace("\u2014", ":")
        raise ValueError(f"Cannot read {candidate.name}: {detail}") from exc
    try:
        if suffix in {".htm", ".html", ".xhtml"} and fetch_html.is_challenge_html(
            candidate_io.read_bytes().decode("utf-8", errors="replace")
        ):
            raise ValueError("the HTML contains a browser challenge or login page")
        from core.fetch.extraction import source_pdf
        text, _format, details = source_pdf.extract_text(str(candidate_io), run_dir)
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


def _source_file_stamp(path: str) -> tuple[int, int, int, int] | None:
    """Return a cheap identity stamp used to detect changes while a save waits."""
    candidate = Path(io_path(path))
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return None
        info = candidate.stat()
    except OSError:
        return None
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
    )


def create_guided_fetch_window(
    run_dir: str,
    *,
    submit_source: Callable[[str, dict[str, Any]], Any],
    proceed: Callable[[], Any],
    prepare_proceed: Callable[..., Any] | None = None,
    discard_source: Callable[[str], Any] | None = None,
    run_ocr: Callable[[list[dict[str, Any]], Callable[[dict[str, Any]], None]], list[dict[str, Any]]] | None = None,
    on_ocr_event: Callable[[dict[str, Any]], None] | None = None,
    ocr_text: Mapping[str, str] | None = None,
    submit_identity_review: Callable[[str, str, str, str], Any] | None = None,
    identity_review_allowed: bool = True,
    inventory_loader: Callable[[str], dict[str, Any]] = build_source_inventory,
):
    """Create (but do not show) the window; writes use injected callbacks.

    ``submit_source`` and ``discard_source`` must be Qt-free callbacks. Guided
    Fetch invokes them on one serialized background worker because staging and
    integrity transitions can copy and hash large files.
    """
    qt = _load_qt()
    QtCore, QtGui, QtWidgets = qt
    label_overrides = ocr_text or {}
    labels = {**_OCR_DEFAULT, **label_overrides}
    labels["guided_fetch_confirm_source"] = label_overrides.get(
        "guided_fetch_confirm_source",
        label_overrides.get("confirm_source", labels["guided_fetch_confirm_source"]),
    )

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

    class ParsedTextWorker(QtCore.QObject):
        loaded = QtCore.Signal(object, object)

        def __init__(self, request, path: str):
            super().__init__()
            self._request = request
            self._path = path

        @QtCore.Slot()
        def run(self):
            try:
                text = _read_parsed_text(self._path)
            except Exception:
                text = None
            self.loaded.emit(self._request, text)

    class ElidedLabel(QtWidgets.QLabel):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._full_text = ""

        def set_full_text(self, text: str):
            self._full_text = text
            self.setToolTip(text)
            self.setAccessibleName(text)
            self.setAccessibleDescription(text)
            self._refresh_elided_text()

        def full_text(self) -> str:
            return self._full_text

        def resizeEvent(self, event):  # noqa: N802 - Qt API name
            super().resizeEvent(event)
            self._refresh_elided_text()

        def _refresh_elided_text(self):
            visible = self.fontMetrics().elidedText(
                self._full_text,
                QtCore.Qt.TextElideMode.ElideRight,
                max(0, self.contentsRect().width()),
            )
            super().setText(visible)

    badge_tone_role = int(QtCore.Qt.ItemDataRole.UserRole) + 3
    badge_dark_role = int(QtCore.Qt.ItemDataRole.UserRole) + 4

    class SourceRowDelegate(QtWidgets.QStyledItemDelegate):
        def initStyleOption(self, option, index):  # noqa: N802 - Qt API name
            super().initStyleOption(option, index)
            # Preserve the flagged item text/data while avoiding a raw warning
            # glyph that some platform fonts render as an empty square.
            if index.column() == 5 and option.text.startswith("⚠ "):
                option.text = option.text[2:]

        def paint(self, painter, option, index):
            if index.column() == 1:
                cell_option = QtWidgets.QStyleOptionViewItem(option)
                self.initStyleOption(cell_option, index)
                display_text = cell_option.text
                cell_option.text = ""
                style = option.widget.style() if option.widget else QtWidgets.QApplication.style()
                style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem,
                                  cell_option, painter, option.widget)
                self._paint_loading_pulse(painter, option, index)
                rect = style.subElementRect(QtWidgets.QStyle.SubElement.SE_ItemViewItemText,
                                            cell_option, option.widget).adjusted(3, 5, -4, -5)
                first, separator, rest = display_text.partition("\n")
                document = QtGui.QTextDocument()
                document.setDocumentMargin(0)
                document.setDefaultFont(cell_option.font)
                cursor = QtGui.QTextCursor(document)
                primary = QtGui.QTextCharFormat()
                primary.setFontWeight(QtGui.QFont.Weight.DemiBold)
                primary.setForeground(cell_option.palette.color(QtGui.QPalette.ColorRole.Text))
                cursor.insertText(first, primary)
                if separator:
                    cursor.insertBlock()
                    secondary = QtGui.QTextCharFormat()
                    secondary.setFontWeight(QtGui.QFont.Weight.Normal)
                    secondary.setForeground(QtGui.QColor("#afbed4" if self.parent().property("darkTheme") else "#64789f"))
                    cursor.insertText(rest, secondary)
                document.setTextWidth(max(1, rect.width()))
                painter.save()
                painter.setClipRect(rect)
                painter.translate(rect.left(), rect.top() + max(0, (rect.height() - document.size().height()) / 2))
                document.drawContents(painter)
                painter.restore()
                return
            tone = index.data(badge_tone_role)
            if tone and index.column() in (2, 5):
                cell_option = QtWidgets.QStyleOptionViewItem(option)
                self.initStyleOption(cell_option, index)
                display_text = cell_option.text
                cell_option.text = ""
                cell_option.icon = QtGui.QIcon()
                style = (
                    option.widget.style()
                    if option.widget is not None
                    else QtWidgets.QApplication.style()
                )
                style.drawControl(
                    QtWidgets.QStyle.ControlElement.CE_ItemViewItem,
                    cell_option,
                    painter,
                    option.widget,
                )
                self._paint_loading_pulse(painter, option, index)
                if display_text:
                    self._paint_badge(painter, option, index, display_text, str(tone))
                return

            super().paint(painter, option, index)
            self._paint_loading_pulse(painter, option, index)

        def sizeHint(self, option, index):  # noqa: N802 - Qt API name
            size = super().sizeHint(option, index)
            column = index.column()
            if column in (1, 2, 3, 5):
                width = option.rect.width()
                if width <= 0:
                    table = self.parent()
                    if table is not None and hasattr(table, "columnWidth"):
                        width = table.columnWidth(column)
                tone = index.data(badge_tone_role)
                metrics = QtGui.QFontMetrics(option.font)
                display_text = str(
                    index.data(QtCore.Qt.ItemDataRole.DisplayRole) or ""
                )
                if column in (2, 5) and tone:
                    text_width = max(24, width - 26)
                    fitted = self._fit_badge_text(
                        display_text, metrics, text_width, 3
                    )
                    line_count = max(1, len(fitted.splitlines()))
                    text_height = line_count * metrics.lineSpacing() + 18
                else:
                    text_width = max(
                        24,
                        width - (
                            36 if column in (1, 3) and index.data(
                                QtCore.Qt.ItemDataRole.DecorationRole
                            ) else 12
                        ),
                    )
                    bounds = metrics.boundingRect(
                        QtCore.QRect(0, 0, text_width, 10_000),
                        int(QtCore.Qt.TextFlag.TextWordWrap),
                        display_text,
                    )
                    text_height = bounds.height() + 16
                size.setHeight(max(size.height(), text_height))
            return size

        def _paint_loading_pulse(self, painter, option, index):
            # Selection normally hides item backgrounds. Keep its contrast while
            # allowing the loading cue to remain visible on the selected source.
            pulse = index.data(int(QtCore.Qt.ItemDataRole.UserRole) + 2)
            if pulse is not None and option.state & QtWidgets.QStyle.StateFlag.State_Selected:
                painter.save()
                painter.fillRect(option.rect, pulse)
                painter.restore()

        def _paint_badge(self, painter, option, index, text, tone):
            dark = bool(index.data(badge_dark_role))
            background, border = _status_badge_surfaces(tone, dark=dark)
            foreground = index.data(QtCore.Qt.ItemDataRole.ForegroundRole)
            foreground = (
                foreground.color()
                if isinstance(foreground, QtGui.QBrush)
                else QtGui.QColor("#4b5563")
            )
            metrics = QtGui.QFontMetrics(option.font)
            content = option.rect.adjusted(5, 4, -5, -4)
            max_height = max(24, content.height())
            max_text_width = max(24, content.width() - 16)
            max_lines = min(
                3,
                max(1, (max_height - 8) // max(1, metrics.lineSpacing())),
            )
            fitted = self._fit_badge_text(text, metrics, max_text_width, max_lines)
            lines = fitted.split("\n") or [""]
            badge_width = min(
                content.width(),
                max(34, max(metrics.horizontalAdvance(line) for line in lines) + 16),
            )
            badge_height = min(
                max_height,
                max(24, len(lines) * metrics.lineSpacing() + 8),
            )
            badge = QtCore.QRectF(
                content.center().x() - badge_width / 2,
                content.center().y() - badge_height / 2,
                badge_width,
                badge_height,
            )
            painter.save()
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QtGui.QPen(QtGui.QColor(border), 1))
            painter.setBrush(QtGui.QColor(background))
            painter.drawRoundedRect(badge, 8, 8)
            painter.setPen(QtGui.QColor(foreground))
            painter.setFont(option.font)
            text_rect = badge.adjusted(7, 3, -7, -3)
            painter.drawText(
                text_rect,
                int(QtCore.Qt.AlignmentFlag.AlignCenter)
                | int(QtCore.Qt.TextFlag.TextWordWrap),
                fitted,
            )
            painter.restore()

        @staticmethod
        def _fit_badge_text(text, metrics, width, max_lines):
            lines = []
            for paragraph in str(text).splitlines() or [""]:
                words = paragraph.split()
                if not words:
                    lines.append("")
                    continue
                current = ""
                for word in words:
                    candidate = f"{current} {word}".strip()
                    if metrics.horizontalAdvance(candidate) <= width:
                        current = candidate
                    elif current:
                        lines.append(current)
                        current = word
                    else:
                        lines.append(metrics.elidedText(
                            word, QtCore.Qt.TextElideMode.ElideRight, width
                        ))
                if current:
                    lines.append(current)
            if not lines:
                return ""
            truncated = len(lines) > max_lines
            lines = lines[:max_lines]
            if truncated:
                lines[-1] = metrics.elidedText(
                    lines[-1], QtCore.Qt.TextElideMode.ElideRight, width
                )
            return "\n".join(lines)

    class PreflightWorker(QtCore.QObject):
        finished = QtCore.Signal(object, object)

        def __init__(self, request):
            super().__init__()
            self._request = request

        @QtCore.Slot()
        def run(self):
            try:
                result = {"status": _preflight_source_file(
                    self._request["path"], self._request["tier"], self._request["run_dir"]
                ), "error": None}
            except Exception as exc:
                result = {"status": None, "error": str(exc)}
            self.finished.emit(self._request, result)

    class IdentityReviewWorker(QtCore.QObject):
        finished = QtCore.Signal(object)

        def __init__(self, run_dir, submit_identity_review, inventory_loader, decisions):
            super().__init__()
            self._run_dir = run_dir
            self._submit_identity_review = submit_identity_review
            self._inventory_loader = inventory_loader
            self._decisions = decisions

        @QtCore.Slot()
        def run(self):
            admitted_ref_ids = []
            for decision in self._decisions:
                try:
                    self._submit_identity_review(
                        decision["ref_id"],
                        decision["action"],
                        decision["target_sha256"],
                        decision["reason"],
                    )
                except Exception as exc:
                    self.finished.emit({
                        "admitted_ref_ids": admitted_ref_ids,
                        "failed_decision": decision,
                        "stage": "admission",
                        "error": str(exc),
                    })
                    return
                admitted_ref_ids.append(decision["ref_id"])
            try:
                inventory = self._inventory_loader(self._run_dir)
            except Exception as exc:
                self.finished.emit({
                    "admitted_ref_ids": admitted_ref_ids,
                    "stage": "refresh",
                    "error": str(exc),
                })
                return
            self.finished.emit({
                "admitted_ref_ids": admitted_ref_ids,
                "stage": None,
                "inventory": inventory,
            })

    class ProceedWorker(QtCore.QObject):
        finished = QtCore.Signal(object)

        def __init__(self, action, *, skip_remaining_confirmed: bool):
            super().__init__()
            self._action = action
            self._skip_remaining_confirmed = skip_remaining_confirmed
            self.outcome = None

        @QtCore.Slot()
        def run(self):
            try:
                self.outcome = {
                    "result": self._action(
                        skip_remaining_confirmed=self._skip_remaining_confirmed,
                    ),
                    "error": None,
                }
            except Exception as exc:
                self.outcome = {"result": None, "error": f"{type(exc).__name__}: {exc}"}
            self.finished.emit(self.outcome)

    class SourceMutationWorker(QtCore.QObject):
        finished = QtCore.Signal(object)

        def __init__(self, request, submit_source, discard_source):
            super().__init__()
            self._request = request
            self._submit_source = submit_source
            self._discard_source = discard_source
            self.outcome = None

        @QtCore.Slot()
        def run(self):
            request = self._request
            try:
                if request["action"] == "stage":
                    expected_stamp = request.get("source_stamp")
                    if (
                        expected_stamp is not None
                        and _source_file_stamp(request["path"]) != expected_stamp
                    ):
                        raise ValueError(
                            "The source file changed after confirmation while waiting to be saved. "
                            "Confirm the current file again."
                        )
                    self._submit_source(request["ref_id"], dict(request["payload"]))
                elif request["action"] == "discard":
                    self._discard_source(request["ref_id"])
                else:
                    raise ValueError("Guided Fetch source mutation is invalid")
                outcome = {"request_id": request["request_id"], "error": None}
            except Exception as exc:
                detail = str(exc).strip()
                outcome = {
                    "request_id": request["request_id"],
                    "error": f"{type(exc).__name__}: {detail or 'source mutation failed'}",
                }
            self.outcome = outcome
            self.finished.emit(outcome)

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

    class CaptureSpinner(QtWidgets.QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("browserCaptureSpinner")
            self.setFixedSize(72, 72)
            self._dark = False
            self._angle = 0
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(50)
            self._timer.timeout.connect(self._advance)

        def set_dark_theme(self, dark: bool):
            self._dark = bool(dark)
            self.update()

        def start(self):
            self._timer.start()
            self.update()

        def stop(self):
            self._timer.stop()

        def _advance(self):
            self._angle = (self._angle - 30) % 360
            self.update()

        def paintEvent(self, _event):  # noqa: N802 - Qt API name
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            ring = QtCore.QRectF(7, 7, 58, 58)
            track = "#364152" if self._dark else "#d5deeb"
            painter.setPen(QtGui.QPen(QtGui.QColor(track), 6))
            painter.drawEllipse(ring)
            pen = QtGui.QPen(QtGui.QColor("#7aa7e8" if self._dark else "#2457a6"), 6)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawArc(ring, self._angle * 16, 115 * 16)
            painter.end()

    class CaptureProgressDialog(QtWidgets.QDialog):
        def __init__(self, parent, on_cancel, *, localized_labels=None):
            super().__init__(parent)
            localized_labels = localized_labels or _OCR_DEFAULT
            self.setObjectName("browserCaptureProgressDialog")
            self.setWindowTitle(localized_labels["guided_fetch_capture_title"])
            self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
            self.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
            self.setModal(True)
            self.setFixedWidth(448)
            self._on_cancel = on_cancel
            self._cancel_requested = False
            self._canceling_text = localized_labels["guided_fetch_capture_canceling"]
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(24, 20, 24, 22)
            layout.setSpacing(11)
            layout.addStretch(1)
            self.title = QtWidgets.QLabel(
                localized_labels["guided_fetch_capture_title"], self
            )
            self.title.setObjectName("browserCaptureProgressTitle")
            self.title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self.title)
            self.spinner = CaptureSpinner(self)
            layout.addWidget(self.spinner, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
            self.message = QtWidgets.QLabel(
                localized_labels["guided_fetch_capture_message"], self
            )
            self.message.setObjectName("browserCaptureProgressMessage")
            self.message.setWordWrap(True)
            self.message.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self.message)
            self.source_note = QtWidgets.QLabel("", self)
            self.source_note.setObjectName("browserCaptureProgressSourceNote")
            self.source_note.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.source_note.setWordWrap(True)
            layout.addWidget(self.source_note)
            self.divider = QtWidgets.QFrame(self)
            self.divider.setObjectName("browserCaptureProgressDivider")
            self.divider.setFrameShape(QtWidgets.QFrame.Shape.HLine)
            self.divider.setFrameShadow(QtWidgets.QFrame.Shadow.Plain)
            layout.addWidget(self.divider)
            self.cancel_button = QtWidgets.QPushButton(
                localized_labels["guided_fetch_capture_cancel"], self
            )
            self.cancel_button.setObjectName("cancelBrowserCaptureButton")
            self.cancel_button.clicked.connect(self.request_cancel)
            self.cancel_button.setFixedSize(152, 50)
            layout.addWidget(self.cancel_button, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
            layout.addStretch(1)
            self.setMinimumHeight(390)
            self.set_dark_theme(False)

        def set_dark_theme(self, dark: bool):
            self.spinner.set_dark_theme(dark)
            background = "#1b1f24" if dark else "#ffffff"
            text = "#edf3ff" if dark else "#18202a"
            border = "#364152" if dark else "#cbd5e1"
            disabled_text = "#aeb8c7" if dark else "#52606d"
            cancel = "#ef4444" if dark else "#b42318"
            hover = "#dc2626" if dark else "#912018"
            cancel_surface = "#472931" if dark else "#fff0ef"
            self.setStyleSheet(f"""
                QDialog#browserCaptureProgressDialog {{ background: {background}; color: {text};
                    border: 1px solid {border}; border-radius: 9px; }}
                QLabel#browserCaptureProgressTitle {{ background: transparent; color: {text};
                    font-size: 22px; font-weight: 700; }}
                QLabel#browserCaptureProgressMessage {{ background: transparent; color: {text};
                    font-size: 18px; }}
                QLabel#browserCaptureProgressSourceNote {{ background: transparent; color: {disabled_text};
                    font-size: 16px; }}
                QFrame#browserCaptureProgressDivider {{ background: {border}; border: 0;
                    max-height: 1px; }}
                QPushButton#cancelBrowserCaptureButton {{ background: transparent; color: {cancel};
                    border: 1px solid {cancel}; border-radius: 6px; padding: 6px 14px;
                    min-height: 20px; font-weight: 600; }}
                QPushButton#cancelBrowserCaptureButton:hover {{ background: {cancel_surface};
                    color: {hover}; border-color: {hover}; }}
                QPushButton#cancelBrowserCaptureButton:pressed {{ background: {cancel};
                    color: white; border-color: {cancel}; }}
                QPushButton#cancelBrowserCaptureButton:focus {{ border: 2px solid {cancel}; }}
                QPushButton#cancelBrowserCaptureButton:disabled {{ background: {border};
                    color: {text}; border-color: {border}; }}
            """ + atlante_stylesheet(dark=dark, font_size=getattr(self, "_font_size_px", 13)))

        def set_phase(self, text: str):
            self.message.setText(text)
            self.cancel_button.setEnabled(not self._cancel_requested)

        def set_source_note(self, text: str):
            self.source_note.setText(text)

        def request_cancel(self):
            if self._cancel_requested:
                return
            self._cancel_requested = True
            self.message.setText(self._canceling_text)
            self.cancel_button.setEnabled(False)
            self._on_cancel()

        def reject(self):  # noqa: N802 - Qt API name
            self.request_cancel()

        def closeEvent(self, event):  # noqa: N802 - Qt API name
            if self._cancel_requested:
                event.ignore()
                return
            event.ignore()
            self.request_cancel()

        def dismiss(self):
            self.spinner.stop()
            self.hide()

        def showEvent(self, event):  # noqa: N802 - Qt API name
            self.spinner.start()
            super().showEvent(event)

        def hideEvent(self, event):  # noqa: N802 - Qt API name
            self.spinner.stop()
            super().hideEvent(event)

    class GuidedFetchWindow(QtWidgets.QMainWindow):
        browser_start = QtCore.Signal()
        browser_open = QtCore.Signal(str)
        browser_capture_html = QtCore.Signal(str, int)
        browser_capture_pdf = QtCore.Signal(str, int)
        browser_capture_text = QtCore.Signal(str, int)
        browser_capture_download = QtCore.Signal(str, int, int)
        browser_discard_download = QtCore.Signal(int)
        browser_park = QtCore.Signal()
        browser_theme = QtCore.Signal(bool)
        source_stage_idle = QtCore.Signal()

        def _connect_gui_signal(self, signal, callback):
            # Dynamic PySide window classes can dispatch even queued Python
            # slots in the emitter's thread. Use Qt's explicit QObject context.
            def dispatch(*args):
                QtCore.QTimer.singleShot(0, self, lambda: callback(*args))

            # Post at emission time so results precede the thread's finished
            # notification even if PySide would queue the forwarding callable.
            signal.connect(dispatch, QtCore.Qt.ConnectionType.DirectConnection)

        @staticmethod
        def _join_worker_thread(thread):
            if thread is not None:
                # finished precedes native thread-local cleanup. Keep the old
                # thread alive until that cleanup completes, then retire it.
                thread.wait()
                thread.deleteLater()

        def __init__(self):
            super().__init__()
            self._compact_drawer_mode = False
            self._compact_drawer_open = False
            self._compact_drawer_animation = None
            self._compact_drawer_focus_policies = []
            self._last_wide_splitter_sizes: list[int] | None = None
            self._embedded_split_initialized = False
            self._run_dir = run_dir
            self._inventory_loader = inventory_loader
            inventory = inventory_loader(run_dir)
            self._model = GuidedFetchViewModel(inventory)
            self._submit_source = submit_source
            self._proceed = proceed
            self._prepare_proceed = prepare_proceed
            self._proceed_preparing = False
            self._proceed_thread = None
            self._proceed_worker = None
            self._proceed_outcome = None
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
            self._selection_context_generation = 0
            self._consumed_file_selections: set[tuple[str, int]] = set()
            self._selected_file_ocr_pending = False
            self._selected_file_preflight_pending = False
            self._selected_file_preflight_tier: str | None = None
            self._preflight_thread = None
            self._preflight_worker = None
            self._preflight_active_request = None
            self._preflight_pending: dict[str, dict[str, Any]] = {}
            self._browser_preflight_pending: list[dict[str, Any]] = []
            self._browser_pdf_preflight_jobs: dict[tuple[str, int], dict[str, Any]] = {}
            self._next_preflight_request_id = 0
            self._submit_preflight_request_id: int | None = None
            self._browser_preflight_request_id: int | None = None
            self._preflight_close_when_finished = False
            self._source_mutation_queue: list[dict[str, Any]] = []
            self._source_mutation_active_request: dict[str, Any] | None = None
            self._source_mutation_thread = None
            self._source_mutation_worker = None
            self._source_mutation_close_when_finished = False
            self._next_source_mutation_id = 0
            self._browser_thread = None
            self._browser_worker = None
            self._pending_browser_url: str | None = None
            self._browser_ref_id: str | None = None
            self._browser_tier: str | None = None
            self._browser_toolbar_pending_message: str | None = None
            self._capture_index = 0
            self._capture_tiers: dict[tuple[str, int], str] = {}
            self._preconfirmed_captures: set[tuple[str, int]] = set()
            self._browser_capture_active_key: tuple[str, int] | None = None
            self._browser_capture_pdf_key: tuple[str, int] | None = None
            self._browser_capture_phase: str | None = None
            self._browser_capture_cancelled_keys: set[tuple[str, int]] = set()
            self._browser_capture_progress = None
            self._queued_sources: dict[str, tuple[str, str | None]] = {}
            self._staged_source_records: dict[str, dict[str, Any]] = {}
            self._inventory_staged_source_refs: set[str] = set()
            self._staged_ocr_refs: set[str] = set()
            self._ocr_row_status: dict[str, dict[str, Any]] = {}
            self._ocr_thread = None
            self._ocr_worker = None
            self._ocr_active_jobs: list[dict[str, Any]] = []
            self._pending_auto_ocr_jobs: list[dict[str, Any]] = []
            self._recorded_identity_refs: set[str] = set()
            self._identity_refresh_failed_refs: set[str] = set()
            self._identity_thread = None
            self._identity_worker = None
            self._identity_mode: str | None = None
            self._identity_close_when_finished = False
            self._identity_proceed_after_finish = False
            self._identity_proceed_skip_remaining_confirmed = False
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
            self._parsed_text_thread = None
            self._parsed_text_worker = None
            self._parsed_text_active_request = None
            self._parsed_text_pending_request = None
            self._parsed_text_request = None
            self._parsed_text_request_id = 0
            self._parsed_text_close_when_finished = False
            self._parsed_text_dialog = None
            self._parsed_text_editor = None
            self._parsed_text_dialog_key = None
            self._dark_theme = False
            self._font_size_px = 13
            self._source_pulse_phase = 0
            self._source_pulse_timer = QtCore.QTimer(self)
            self._source_pulse_timer.setInterval(120)
            self._source_pulse_timer.timeout.connect(self._advance_source_row_pulse)
            self._hydrate_staged_sources(inventory)
            self.setWindowTitle("Callimachus: Guided Fetch")
            icon = _callimachus_icon(QtCore, QtGui)
            self.setWindowIcon(icon)
            application = QtWidgets.QApplication.instance()
            if application is not None:
                application.setWindowIcon(icon)
            self.resize(1530, 900)
            self.setMinimumSize(1120, 700)
            self._build()

        def _build(self):
            central = QtWidgets.QWidget(self)
            central.setObjectName("guidedFetchCentral")
            layout = QtWidgets.QVBoxLayout(central)
            layout.setContentsMargins(18, 16, 18, 0)
            layout.setSpacing(12)
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
            self.detail_splitter = QtWidgets.QSplitter(
                QtCore.Qt.Orientation.Horizontal, central
            )
            self.detail_splitter.setObjectName("detailSplitter")
            self.detail_splitter.setChildrenCollapsible(False)
            self.detail_splitter.setHandleWidth(7)

            self.source_panel = QtWidgets.QFrame(self.detail_splitter)
            self.source_panel.setObjectName("sourceListPanel")
            source_layout = QtWidgets.QVBoxLayout(self.source_panel)
            source_layout.setContentsMargins(0, 0, 0, 0)
            source_layout.setSpacing(0)
            self.source_filter_header = QtWidgets.QWidget(self.source_panel)
            self.source_filter_header.setObjectName("sourceFilterHeader")
            source_header = QtWidgets.QBoxLayout(
                QtWidgets.QBoxLayout.Direction.LeftToRight,
                self.source_filter_header,
            )
            source_header.setContentsMargins(12, 10, 12, 10)
            source_header.setSpacing(10)
            source_identity = QtWidgets.QVBoxLayout()
            source_identity.setSpacing(2)
            self.source_title_label = QtWidgets.QLabel(
                labels["guided_fetch_sources_title"], self.source_panel
            )
            self.source_title_label.setObjectName("guidedFetchSourcesTitle")
            source_identity.addWidget(self.source_title_label)
            self.source_count_label = QtWidgets.QLabel(self.source_panel)
            self.source_count_label.setObjectName("guidedFetchSourceCount")
            source_identity.addWidget(self.source_count_label)
            source_header.addLayout(source_identity)
            source_header.addItem(QtWidgets.QSpacerItem(
                0, 0,
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            ))
            self.source_filter = QtWidgets.QPushButton(
                labels["guided_fetch_show_sources"], self.source_filter_header
            )
            self.source_filter.setObjectName("sourceFilter")
            self.source_filter.setAccessibleName("Filter sources")
            self.source_filter.setToolTip(labels["guided_fetch_filter_tooltip"])
            configure_source_filter_button(self.source_filter, QtWidgets)
            source_header.addWidget(self.source_filter)
            self.source_filter_header_layout = source_header
            self._source_filter_identity_layout = source_identity
            self._guided_filter_keys = set()
            self.source_filter_menu, self.source_filter_checks = create_checkbox_source_menu(
                self.source_filter,
                QtCore,
                QtGui,
                QtWidgets,
                (
                    ("fulltext", labels["guided_fetch_filter_fulltext"]),
                    ("abstract", labels["guided_fetch_filter_abstract"]),
                    ("none", labels["guided_fetch_filter_none"]),
                    ("identity_required", labels["guided_fetch_filter_identity_required"]),
                ),
                clear_label=labels.get("guided_fetch_clear_filters", "Remove filters"),
                on_toggle=self._source_filter_toggled,
                on_clear=self._clear_source_filter_selection,
            )
            self.source_filter_menu.aboutToShow.connect(
                self._update_guided_filter_counts
            )
            self.source_filter_header.installEventFilter(self)
            source_layout.addWidget(self.source_filter_header)

            self.table = QtWidgets.QTableWidget(0, 6, self.source_panel)
            self.table.setObjectName("sourceTable")
            self.table.setHorizontalHeaderLabels([
                "#", labels["source"], labels["text_availability"],
                labels["ocr_column"], labels["risk_signal"], labels["guided_fetch_review"],
            ])
            self.table.setWordWrap(True)
            self.table.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
            self.table.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
            )
            self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setAlternatingRowColors(False)
            self.table.setShowGrid(False)
            self.table.setIconSize(QtCore.QSize(18, 18))
            self.table.setItemDelegate(SourceRowDelegate(self.table))
            self.table.verticalHeader().setVisible(False)
            self.table.verticalHeader().setDefaultSectionSize(84)
            self.table.verticalHeader().setMinimumSectionSize(80)
            source_header = self.table.horizontalHeader()
            source_header.setMinimumHeight(40)
            source_header.setDefaultAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            source_header.setSectionResizeMode(
                0, QtWidgets.QHeaderView.ResizeMode.Fixed
            )
            self.table.setColumnWidth(0, 42)
            source_header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
            for column, width in ((2, 140), (3, 126), (4, 104), (5, 120)):
                source_header.setSectionResizeMode(
                    column, QtWidgets.QHeaderView.ResizeMode.Fixed
                )
                self.table.setColumnWidth(column, width)
            source_header.sectionResized.connect(
                lambda *_args: QtCore.QTimer.singleShot(
                    0, self.table.resizeRowsToContents
                )
            )
            self.table.setColumnHidden(4, True)
            self.table.setMinimumWidth(610)
            self.table.setMinimumHeight(140)
            from core.gui.shell.resize import install_table_resize
            install_table_resize(
                self.table, QtCore, QtWidgets, fill_column=1,
                preferred_minimum_widths={0: 42, 1: 200, 2: 140, 3: 126, 4: 104, 5: 120},
            )
            self.table.itemSelectionChanged.connect(self._selected)
            self.table.itemClicked.connect(self._open_compact_details_from_item)
            self.table.itemActivated.connect(self._open_compact_details_from_item)

            self.source_session_summary = QtWidgets.QLabel(self.source_panel)
            self.source_session_summary.setObjectName("sourceSessionSummary")
            self.source_session_summary.setWordWrap(True)
            self.source_session_summary.setVisible(False)
            self.detail_panel = QtWidgets.QFrame(self.detail_splitter)
            self.detail_panel.setObjectName("guidedFetchDetailsPanel")
            detail_panel_layout = QtWidgets.QVBoxLayout(self.detail_panel)
            detail_panel_layout.setContentsMargins(0, 0, 0, 0)
            detail_panel_layout.setSpacing(0)
            self.compact_navigation = QtWidgets.QWidget(self.detail_panel)
            self.compact_navigation.setObjectName("guidedFetchCompactNavigation")
            compact_navigation_layout = QtWidgets.QHBoxLayout(self.compact_navigation)
            compact_navigation_layout.setContentsMargins(0, 0, 0, 0)
            compact_navigation_layout.setSpacing(8)
            self.compact_back_button = QtWidgets.QPushButton(
                labels["guided_fetch_back_to_sources"], self.compact_navigation
            )
            self.compact_back_button.setObjectName("guidedFetchBackToSources")
            self.compact_back_button.setAccessibleName(
                labels["guided_fetch_back_to_sources"]
            )
            self.compact_back_button.setToolTip(
                labels["guided_fetch_back_to_sources"]
            )
            self.compact_back_button.clicked.connect(self._close_compact_details)
            compact_navigation_layout.addWidget(
                self.compact_back_button, 0, QtCore.Qt.AlignmentFlag.AlignLeft
            )
            compact_navigation_layout.addStretch(1)
            self.compact_next_button = QtWidgets.QPushButton(
                labels["guided_fetch_next_unresolved"], self.compact_navigation
            )
            self.compact_next_button.setObjectName("guidedFetchNextUnresolved")
            self.compact_next_button.setAccessibleName(
                labels["guided_fetch_next_unresolved"]
            )
            self.compact_next_button.setToolTip(
                labels["guided_fetch_next_unresolved"]
            )
            self.compact_next_button.clicked.connect(self._next_unresolved)
            compact_navigation_layout.addWidget(
                self.compact_next_button, 0, QtCore.Qt.AlignmentFlag.AlignRight
            )
            self.compact_navigation.hide()
            detail_panel_layout.addWidget(self.compact_navigation)
            self.detail_scroll = QtWidgets.QScrollArea(self.detail_panel)
            self.detail_scroll.setObjectName("guidedFetchDetailScroll")
            self.detail_scroll.setWidgetResizable(True)
            self.detail_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            self.detail_scroll.setHorizontalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self.detail_scroll.viewport().installEventFilter(self)
            self.detail_content = QtWidgets.QWidget()
            self.detail_content.setObjectName("guidedFetchDetailContent")
            detail_layout = QtWidgets.QVBoxLayout(self.detail_content)
            detail_layout.setContentsMargins(10, 0, 0, 0)
            detail_layout.setSpacing(4)
            detail_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
            self.selected_source_heading = ElidedLabel(self.detail_content)
            self.selected_source_heading.setObjectName("selectedSourceHeading")
            self.selected_source_heading.setWordWrap(False)
            self.selected_source_heading.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Ignored,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            self.selected_source_heading.set_full_text("")
            self.selected_source_heading.setMinimumWidth(0)
            detail_layout.addWidget(self.selected_source_heading)
            self.tabs = QtWidgets.QTabWidget(self.detail_content)
            self.tabs.setObjectName("guidedFetchSourceTabs")
            self.tabs.tabBar().setObjectName("guidedFetchSourceTabBar")
            self.tabs.tabBar().setUsesScrollButtons(True)
            self.tabs.tabBar().setExpanding(False)
            self.tabs.setMinimumWidth(0)
            self._tab_text = {}
            self._detail_views = {}
            self._preview_tab_widget = None
            tab_labels = {
                "Parsed": labels["guided_fetch_tab_parsed"],
                "Resolved": labels["guided_fetch_tab_resolved"],
                "Acquired": labels["guided_fetch_tab_acquired"],
                "Preview": labels["guided_fetch_tab_preview"],
            }
            for name in ("Parsed", "Resolved", "Acquired", "Preview"):
                panel, view = self._detail_panel(name)
                self.tabs.addTab(panel, tab_labels[name])
                self._detail_views[name] = view
                self._tab_text[name] = view["raw"]
                if name == "Preview":
                    self._preview_tab_widget = panel
            self.tabs.currentChanged.connect(self._detail_tab_changed)
            self._staged_preview_active = False
            self.tabs.setMinimumHeight(200)
            self.tabs.setMaximumHeight(220)
            self.tabs.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Ignored,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
            self.source_table_stack = QtWidgets.QStackedWidget(self.source_panel)
            self.source_table_stack.setObjectName("sourceTableStack")
            self.source_table_stack.addWidget(self.table)
            self.source_empty_state = QtWidgets.QWidget(self.source_table_stack)
            self.source_empty_state.setObjectName("sourceFilterEmptyState")
            empty_layout = QtWidgets.QVBoxLayout(self.source_empty_state)
            empty_layout.setContentsMargins(16, 16, 16, 16)
            empty_layout.addStretch(1)
            self.source_empty_icon = QtWidgets.QLabel(self.source_empty_state)
            self.source_empty_icon.setObjectName("sourceFilterEmptyIcon")
            self.source_empty_icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            empty_layout.addWidget(self.source_empty_icon)
            self.source_empty_label = QtWidgets.QLabel(
                labels["guided_fetch_no_sources_match"], self.source_empty_state
            )
            self.source_empty_label.setObjectName("sourceFilterEmptyLabel")
            self.source_empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.source_empty_label.setWordWrap(True)
            empty_layout.addWidget(self.source_empty_label)
            self.clear_source_filter_button = QtWidgets.QPushButton(
                labels.get("guided_fetch_clear_filters", "Remove filters"),
                self.source_empty_state,
            )
            self.clear_source_filter_button.setObjectName("clearSourceFiltersButton")
            self.clear_source_filter_button.clicked.connect(
                self._clear_source_filter_selection
            )
            empty_layout.addWidget(
                self.clear_source_filter_button, 0,
                QtCore.Qt.AlignmentFlag.AlignHCenter,
            )
            empty_layout.addStretch(1)
            self.source_table_stack.addWidget(self.source_empty_state)
            self.source_table_stack.setCurrentWidget(self.table)
            source_layout.addWidget(self.source_table_stack, 1)
            source_layout.addWidget(self.source_session_summary)

            self.detail_splitter.addWidget(self.source_panel)
            self.detail_splitter.addWidget(self.detail_panel)
            self.detail_splitter.setStretchFactor(0, 55)
            self.detail_splitter.setStretchFactor(1, 45)
            self.detail_splitter.setSizes([710, 580])
            self.detail_splitter.splitterMoved.connect(
                lambda *_args: self._update_action_layouts()
            )
            layout.addWidget(self.detail_splitter, 1)
            self.guided_fetch_footer = QtWidgets.QFrame(central)
            self.guided_fetch_footer.setObjectName("guidedFetchFooter")

            detail_layout.addWidget(self.tabs, 1)
            self.selection_guidance = QtWidgets.QLabel(
                labels["guided_fetch_select_reference"], self.detail_content
            )
            self.selection_guidance.setObjectName("selectionGuidance")
            self.selection_guidance.setWordWrap(True)
            self.selection_guidance.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Preferred,
                QtWidgets.QSizePolicy.Policy.Maximum,
            )
            detail_layout.addWidget(self.selection_guidance)
            self.identity_review_panel = QtWidgets.QFrame(self.detail_content)
            self.identity_review_panel.setObjectName("identityReviewPanel")
            identity_layout = QtWidgets.QVBoxLayout(self.identity_review_panel)
            identity_layout.setContentsMargins(2, 2, 2, 2)
            identity_layout.setSpacing(8)
            self.identity_facts_scroll = QtWidgets.QScrollArea(self.identity_review_panel)
            self.identity_facts_scroll.setObjectName("identityFactsScroll")
            self.identity_facts_scroll.setWidgetResizable(True)
            self.identity_facts_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            self.identity_facts_scroll.setHorizontalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self.identity_facts_scroll.setMinimumHeight(50)
            self.identity_review_facts = QtWidgets.QLabel(self.identity_facts_scroll)
            self.identity_review_facts.setObjectName("identityReviewFacts")
            self.identity_review_facts.setWordWrap(True)
            self.identity_review_facts.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
            self.identity_review_facts.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.identity_facts_scroll.setWidget(self.identity_review_facts)
            identity_layout.addWidget(self.identity_facts_scroll, 1)
            self.identity_review_reason = QtWidgets.QLineEdit(self.identity_review_panel)
            self.identity_review_reason.setObjectName("identityReviewReason")
            self.identity_review_reason.setPlaceholderText(
                labels["guided_fetch_identity_note_placeholder"]
            )
            identity_layout.addWidget(self.identity_review_reason)
            identity_actions = QtWidgets.QHBoxLayout()
            self.identity_actions = identity_actions
            self.attest_identity_button = QtWidgets.QPushButton(
                labels["guided_fetch_identity_confirm"], self.identity_review_panel
            )
            self.attest_identity_button.setObjectName("attestIdentityButton")
            self.attest_identity_button.clicked.connect(
                lambda: self._submit_identity("attest_identity")
            )
            identity_actions.addWidget(self.attest_identity_button, 1)
            self.keep_unverified_button = QtWidgets.QPushButton(
                labels["guided_fetch_identity_keep_unverified"], self.identity_review_panel
            )
            self.keep_unverified_button.setObjectName("keepUnverifiedButton")
            self.keep_unverified_button.clicked.connect(
                lambda: self._submit_identity("keep_unverified")
            )
            identity_actions.addWidget(self.keep_unverified_button, 1)
            identity_layout.addLayout(identity_actions)
            self.identity_review_panel.setVisible(False)
            self._identity_review_tab = self.tabs.addTab(
                self.identity_review_panel, labels["guided_fetch_tab_identity"]
            )
            self.tabs.setTabEnabled(self._identity_review_tab, False)
            self.capture_status_panel = QtWidgets.QFrame(self.guided_fetch_footer)
            self.capture_status_panel.setObjectName("captureStatusPanel")
            capture_status_layout = QtWidgets.QHBoxLayout(self.capture_status_panel)
            capture_status_layout.setContentsMargins(6, 3, 6, 3)
            capture_status_layout.setSpacing(6)
            self.capture_status = QtWidgets.QLabel("", self.capture_status_panel)
            self.capture_status.setObjectName("captureStatus")
            self.capture_status.setWordWrap(True)
            self.capture_status.setVisible(False)
            capture_status_layout.addWidget(self.capture_status, 1)
            self.discard_source_button = QtWidgets.QPushButton(labels["guided_fetch_discard"])
            self.discard_source_button.setObjectName("discardSourceButton")
            self.discard_source_button.setToolTip(
                "Remove the queued source from this Guided Fetch session without deleting its audit capture."
            )
            self.discard_source_button.clicked.connect(self._discard_queued_source)
            self.discard_source_button.setEnabled(False)
            self.capture_status_panel.setVisible(False)

            self.action_panel = QtWidgets.QFrame(self.detail_content)
            self.action_panel.setObjectName("actionPanel")
            controls = QtWidgets.QVBoxLayout(self.action_panel)
            controls.setContentsMargins(8, 6, 8, 6)
            controls.setSpacing(4)
            self.action_panel.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Preferred,
                QtWidgets.QSizePolicy.Policy.Maximum,
            )
            open_heading = QtWidgets.QLabel(
                labels["guided_fetch_open_source"], self.action_panel
            )
            open_heading.setObjectName("guidedFetchSectionHeading")
            open_heading.setWordWrap(True)
            controls.addWidget(open_heading)
            browser_controls = QtWidgets.QHBoxLayout()
            self.browser_controls = browser_controls
            self.open_link = QtWidgets.QPushButton(
                labels["guided_fetch_system_browser"], self.action_panel
            )
            self.open_link.clicked.connect(self._open_link)
            browser_controls.addWidget(self.open_link)
            self.open_chrome = QtWidgets.QPushButton(
                labels["guided_fetch_assisted_browser"], self.action_panel
            )
            self.open_chrome.clicked.connect(self._open_controlled_browser)
            browser_controls.addWidget(self.open_chrome)
            browser_controls.setStretch(0, 1)
            browser_controls.setStretch(1, 1)
            controls.addLayout(browser_controls)
            self.capture_hint = QtWidgets.QLabel(
                labels["guided_fetch_capture_hint"], self.action_panel
            )
            self.capture_hint.setObjectName("guidedFetchCaptureHint")
            self.capture_hint.setWordWrap(True)
            controls.addWidget(self.capture_hint)

            provide_heading = QtWidgets.QLabel(
                labels["guided_fetch_provide_source"], self.action_panel
            )
            provide_heading.setObjectName("guidedFetchSectionHeading")
            provide_heading.setWordWrap(True)
            controls.addWidget(provide_heading)
            self.provide_source_row = QtWidgets.QWidget(self.action_panel)
            self.provide_source_row.setObjectName("provideSourceRow")
            provide_source_layout = QtWidgets.QHBoxLayout(self.provide_source_row)
            self.provide_source_layout = provide_source_layout
            provide_source_layout.setContentsMargins(0, 0, 0, 0)
            provide_source_layout.setSpacing(10)
            self.provide_source_tier_column = QtWidgets.QWidget(self.provide_source_row)
            self.provide_source_tier_column.setObjectName("provideSourceTierColumn")
            tier_column_layout = QtWidgets.QVBoxLayout(self.provide_source_tier_column)
            tier_column_layout.setContentsMargins(0, 0, 0, 0)
            tier_column_layout.setSpacing(6)
            tier_controls = QtWidgets.QHBoxLayout()
            self.provide_source_tier_layout = tier_controls
            tier_controls.setSpacing(0)
            self.fulltext_label = QtWidgets.QPushButton(
                labels["fulltext"], self.provide_source_tier_column
            )
            self.fulltext_label.setObjectName("tierFullText")
            self.fulltext_label.setCheckable(True)
            self.fulltext_label.setChecked(True)
            self.tier_choice_group = QtWidgets.QButtonGroup(self.action_panel)
            self.tier_choice_group.setExclusive(True)
            self.tier_choice_group.addButton(self.fulltext_label)
            tier_controls.addWidget(self.fulltext_label)
            self.tier_switch = TierSwitch(central)
            self.tier_switch.setObjectName("tierSwitch")
            self.tier_switch.setToolTip(labels["guided_fetch_source_tier_tooltip"])
            self.tier_switch.setAccessibleName(labels["guided_fetch_source_tier_accessible"])
            self.tier_switch.setEnabled(False)
            self.tier_switch.setVisible(False)
            self.tier_switch.toggled.connect(self._tier_changed)
            self.fulltext_label.clicked.connect(
                lambda: self.tier_switch.setChecked(False)
            )
            self.abstract_label = QtWidgets.QPushButton(
                labels["abstract"], self.provide_source_tier_column
            )
            self.abstract_label.setObjectName("tierAbstract")
            self.abstract_label.setCheckable(True)
            self.tier_choice_group.addButton(self.abstract_label)
            self.abstract_label.clicked.connect(
                lambda: self.tier_switch.setChecked(True)
            )
            tier_controls.addWidget(self.abstract_label)
            tier_controls.setStretch(0, 1)
            tier_controls.setStretch(1, 1)
            tier_column_layout.addLayout(tier_controls)
            self.submit = QtWidgets.QPushButton(
                labels["guided_fetch_confirm_source"], self.provide_source_tier_column
            )
            self.submit.setObjectName("confirmSourceButton")
            self.submit.clicked.connect(self._submit_file)
            self.submit.setEnabled(False)
            # Choosing/dropping a file already stages it after preflight. Keep
            # the internal operation state out of the visible acquisition UI.
            self.submit.hide()
            tier_column_layout.addWidget(self.discard_source_button)
            self.provide_source_row.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            provide_source_layout.addWidget(self.provide_source_tier_column, 1)

            self.file_drop_zone = QtWidgets.QFrame(self.provide_source_row)
            self.file_drop_zone.setObjectName("fileDropZone")
            self.file_drop_zone.setAcceptDrops(True)
            self.file_drop_zone.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
            self.file_drop_zone.setAccessibleName("Choose source file")
            self.file_drop_zone.setAccessibleDescription(
                "Click or press Enter or Space to choose a source file, or drop one here."
            )
            self.file_drop_zone.setEnabled(False)
            file_drop_layout = QtWidgets.QHBoxLayout(self.file_drop_zone)
            file_drop_layout.setContentsMargins(8, 7, 8, 7)
            file_drop_layout.setSpacing(8)
            self.file_label = QtWidgets.QLabel(
                labels["guided_fetch_drop_file"], self.file_drop_zone
            )
            self.file_label.setObjectName("fileDropArea")
            # Keep keyboard activation on the container: a focusable QLabel
            # creates a text control whose font cache can break during refresh.
            self.file_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            self.file_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.NoTextInteraction)
            self.file_label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            self.file_label.setWordWrap(True)
            self.file_label.setMinimumWidth(0)
            self.file_label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Ignored,
                QtWidgets.QSizePolicy.Policy.Minimum,
            )
            self.file_label.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            self.file_label.setFocusProxy(self.file_drop_zone)
            self.file_label.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.file_label.setAccessibleName("Choose source file")
            self.file_label.setAccessibleDescription(
                "Click or press Enter or Space to choose a source file, or drop one here."
            )
            self.file_label.setAcceptDrops(True)
            self.file_label.installEventFilter(self)
            self.file_label.setEnabled(False)
            self.file_drop_zone.installEventFilter(self)
            file_drop_layout.addWidget(self.file_label, 1)
            self.choose_file = QtWidgets.QPushButton(
                labels["atlas_choose_file"], self.file_drop_zone
            )
            self.choose_file.clicked.connect(self._choose_file)
            self.choose_file.setEnabled(False)
            file_drop_layout.addWidget(self.choose_file)
            provide_source_layout.addWidget(self.file_drop_zone, 1)
            controls.addWidget(self.provide_source_row)
            self._detail_spacer_index = detail_layout.count()
            detail_layout.addStretch(1)
            detail_layout.addWidget(self.action_panel)
            self.detail_scroll.setWidget(self.detail_content)
            detail_panel_layout.addWidget(self.detail_scroll)

            footer_layout = QtWidgets.QHBoxLayout(self.guided_fetch_footer)
            footer_layout.setContentsMargins(0, 8, 0, 8)
            footer_layout.setSpacing(10)
            self.ocr_button = QtWidgets.QPushButton(labels["ocr_button"], self.guided_fetch_footer)
            self.ocr_button.setObjectName("runOcrButton")
            self.ocr_button.setToolTip(labels["ocr_tooltip"])
            self.ocr_button.clicked.connect(lambda: self._start_ocr())
            self.ocr_button.setVisible(False)
            footer_layout.addWidget(self.ocr_button, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
            footer_layout.addStretch()
            footer_actions = QtWidgets.QVBoxLayout()
            footer_buttons = QtWidgets.QHBoxLayout()
            footer_buttons.addStretch()
            self.proceed_button = QtWidgets.QPushButton(
                labels["guided_fetch_continue"], self.guided_fetch_footer
            )
            self.proceed_button.setObjectName("proceedButton")
            self.proceed_button.setMinimumWidth(280)
            self.proceed_button.setMaximumWidth(360)
            self.proceed_button.setToolTip(
                "Continue with available sources and keep every unresolved retrieval waived."
            )
            self.proceed_button.clicked.connect(self._proceed_clicked)
            footer_buttons.addWidget(self.proceed_button)
            footer_actions.addWidget(self.capture_status_panel)
            footer_actions.addLayout(footer_buttons)
            self.proceed_guidance = QtWidgets.QLabel(self.guided_fetch_footer)
            self.proceed_guidance.setObjectName("proceedGuidance")
            self.proceed_guidance.setWordWrap(True)
            self.proceed_guidance.setVisible(False)
            footer_actions.addWidget(self.proceed_guidance)
            footer_layout.addLayout(footer_actions, 2)
            layout.addWidget(self.guided_fetch_footer)
            self.setCentralWidget(central)
            from core.gui.shell.scroll import install_edge_scrollbar
            self._compact_edge_scrollbar = install_edge_scrollbar(
                central,
                [self.detail_scroll],
                QtCore,
                QtWidgets,
                compact_when=lambda: self._compact_drawer_mode,
            )
            self._set_dark_theme(False)
            self._render_rows()
            self._update_action_layouts()

        def resizeEvent(self, event):  # noqa: N802 - Qt API name
            super().resizeEvent(event)
            self._update_action_layouts()
            if getattr(self, "_atlas_embedded", False):
                QtCore.QTimer.singleShot(0, self._update_action_layouts)

        def keyPressEvent(self, event):  # noqa: N802 - Qt API name
            if (
                self._compact_drawer_mode
                and self._compact_drawer_open
                and event.key() == QtCore.Qt.Key.Key_Escape
            ):
                self._close_compact_details()
                event.accept()
                return
            super().keyPressEvent(event)

        def _update_action_layouts(self):
            if not hasattr(self, "browser_controls"):
                return
            self._update_source_filter_header_layout()
            self._update_identity_action_layout()
            embedded = getattr(self, "_atlas_embedded", False)
            compact = False
            detail_minimum_width = 0
            splitter_available_width = 0
            source_minimum_width = 0
            if embedded:
                if not hasattr(self, "_embedded_wide_margins"):
                    self._embedded_wide_margins = (
                        self.centralWidget().layout().getContentsMargins()
                    )
                    self._embedded_wide_detail_margins = (
                        self.detail_content.layout().getContentsMargins()
                    )
                    self._embedded_wide_action_margins = (
                        self.action_panel.layout().getContentsMargins()
                    )
                self.table.setMinimumWidth(0)
                self.source_panel.setMinimumWidth(0)
                for label in (self.source_title_label, self.source_count_label):
                    label.setWordWrap(True)
                    size_policy = label.sizePolicy()
                    size_policy.setHorizontalPolicy(
                        QtWidgets.QSizePolicy.Policy.Preferred
                    )
                    size_policy.setVerticalPolicy(
                        QtWidgets.QSizePolicy.Policy.Preferred
                    )
                    size_policy.setHeightForWidth(True)
                    label.setSizePolicy(size_policy)
                source_layout = self.source_panel.layout()
                source_layout.invalidate()
                source_layout.activate()
                self.source_panel.updateGeometry()

                tab_bar = self.tabs.tabBar()
                if not getattr(self, "_embedded_tab_style_applied", False):
                    tab_bar.setStyleSheet(
                        "QTabBar#guidedFetchSourceTabBar::tab { "
                        "padding: 3px 4px; margin-right: 1px; }"
                    )
                    self._embedded_tab_style_applied = True
                self.provide_source_layout.setDirection(
                    QtWidgets.QBoxLayout.Direction.TopToBottom
                )
                # Measure eligibility with the wide chrome, so reclaiming compact
                # gutters cannot repeatedly toggle the split/list breakpoint.
                detail_content_margins = QtCore.QMargins(*self._embedded_wide_detail_margins)
                detail_minimum_width = max(
                    tab_bar.sizeHint().width() + 24,
                    self.tabs.sizeHint().width(),
                    self.action_panel.sizeHint().width(),
                )
                detail_minimum_width += (
                    detail_content_margins.left()
                    + detail_content_margins.right()
                    + 2 * self.detail_scroll.frameWidth()
                )
                source_minimum_width = self.source_panel.minimumSizeHint().width()
                central_layout = self.centralWidget().layout()
                central_margins = QtCore.QMargins(*self._embedded_wide_margins)
                splitter_available_width = max(
                    0,
                    self.centralWidget().width()
                    - central_margins.left()
                    - central_margins.right(),
                )
                existing_compact_width = max(
                    0,
                    900 - central_margins.left() - central_margins.right(),
                )
                measured_split_width = (
                    detail_minimum_width
                    + source_minimum_width
                    + self.detail_splitter.handleWidth()
                )
                compact = splitter_available_width < max(
                    existing_compact_width, measured_split_width
                )
                self._embedded_detail_minimum_width = detail_minimum_width
            self._set_compact_drawer_mode(compact)
            if embedded:
                self.centralWidget().layout().setContentsMargins(
                    *((8, 12, 8, 0) if compact else self._embedded_wide_margins)
                )
                self.detail_content.layout().setContentsMargins(
                    *((0, 0, 0, 0) if compact else self._embedded_wide_detail_margins)
                )
                self.action_panel.layout().setContentsMargins(
                    *((0, 6, 0, 6) if compact else self._embedded_wide_action_margins)
                )
                action_style = (
                    "QFrame#actionPanel { background: transparent; border: 0; border-radius: 0; padding: 0; }"
                    if compact else ""
                )
                if self.action_panel.styleSheet() != action_style:
                    self.action_panel.setStyleSheet(action_style)
                from .atlas_collections import _fit_button_label

                if compact:
                    detail_margins = self.detail_panel.layout().contentsMargins()
                    navigation_width = max(
                        1,
                        self.detail_panel.width()
                        - detail_margins.left() - detail_margins.right(),
                    )
                    navigation_spacing = max(
                        0, self.compact_navigation.layout().spacing()
                    )
                    button_width = max(
                        1, (navigation_width - navigation_spacing) // 2
                    )
                    _fit_button_label(self.compact_back_button, button_width)
                    _fit_button_label(self.compact_next_button, button_width)

                self.detail_panel.setMinimumWidth(0)
                self.detail_panel.updateGeometry()
                self.proceed_button.setMinimumWidth(0)
                self.proceed_button.setMaximumWidth(16777215)
                footer_layout = self.guided_fetch_footer.layout()
                footer_margins = footer_layout.contentsMargins()
                footer_available_width = max(
                    1,
                    self.guided_fetch_footer.width()
                    - footer_margins.left() - footer_margins.right(),
                )
                footer_spacing = max(0, footer_layout.spacing())

                def natural_button_width(button):
                    # Restore the full label before measuring so an earlier
                    # wrapped state cannot hide the true horizontal requirement.
                    _fit_button_label(button, 16777215)
                    return max(
                        button.minimumWidth(),
                        button.minimumSizeHint().width(),
                        button.sizeHint().width(),
                    )

                proceed_natural_width = natural_button_width(self.proceed_button)
                ocr_requested = not self.ocr_button.isHidden()
                ocr_natural_width = natural_button_width(self.ocr_button)
                footer_actions = next(
                    (footer_layout.itemAt(index).layout()
                     for index in range(footer_layout.count())
                     if footer_layout.itemAt(index).layout() is not None),
                    None,
                )
                actions_natural_width = max(
                    footer_actions.sizeHint().width() if footer_actions is not None else 0,
                    proceed_natural_width,
                )
                footer_natural_width = actions_natural_width
                if ocr_requested:
                    footer_natural_width += ocr_natural_width + 2 * footer_spacing
                else:
                    # The stretch item remains between the hidden OCR button
                    # and the action layout, retaining one layout gap.
                    footer_natural_width += footer_spacing
                footer_direction = (
                    QtWidgets.QBoxLayout.Direction.LeftToRight
                    if footer_natural_width <= footer_available_width
                    else QtWidgets.QBoxLayout.Direction.TopToBottom
                )
                if footer_layout.direction() != footer_direction:
                    footer_layout.setDirection(footer_direction)
                    footer_layout.invalidate()
                footer_layout.activate()
                if footer_actions is not None:
                    footer_actions.invalidate()
                    footer_actions.activate()
                for button, natural_width in (
                    (self.ocr_button, ocr_natural_width),
                    (self.proceed_button, proceed_natural_width),
                ):
                    if button.isHidden():
                        continue
                    button_width = button.width()
                    if button_width <= 1:
                        button_width = footer_available_width
                        if (
                            footer_direction == QtWidgets.QBoxLayout.Direction.LeftToRight
                            and button is self.proceed_button
                        ):
                            button_width = max(
                                1,
                                footer_available_width
                                - ocr_natural_width - 2 * footer_spacing,
                            )
                    _fit_button_label(button, min(button_width, natural_width))
                footer_layout.invalidate()
                footer_layout.activate()
                orientation = QtCore.Qt.Orientation.Vertical if compact else QtCore.Qt.Orientation.Horizontal
                if self.detail_splitter.orientation() != orientation:
                    self.detail_splitter.setOrientation(orientation)
                    if compact:
                        self.detail_splitter.setSizes(
                            [max(1, self.detail_splitter.height())]
                        )
                    elif self._last_wide_splitter_sizes:
                        self.detail_splitter.setSizes(
                            self._last_wide_splitter_sizes
                        )
            # Word-wrapped QLabel minimumSizeHint only reserves a short line.
            # Reserve its real height at the current width so the containing
            # scroll area grows rather than cropping the explanation.
            for label in (self.selected_source_heading, self.selection_guidance,
                          self.file_label, self.proceed_guidance, self.capture_status):
                # QLabel.heightForWidth includes its current minimum height.
                # Drop the previous narrow-layout floor before measuring again.
                label.setMinimumHeight(0)
                label.setMinimumHeight(max(0, label.heightForWidth(max(1, label.width()))))
            detail_width = self.detail_scroll.viewport().width()
            controls_width = detail_width
            if getattr(self, "_atlas_embedded", False):
                margins = (
                    self.detail_content.layout().contentsMargins(),
                    self.action_panel.contentsMargins(),
                    self.action_panel.layout().contentsMargins(),
                )
                controls_width = max(1, detail_width - sum(m.left() + m.right() for m in margins))
                _fit_button_label(self.discard_source_button, controls_width)
            narrow_direction = (
                QtWidgets.QBoxLayout.Direction.TopToBottom
                if controls_width < 560
                else QtWidgets.QBoxLayout.Direction.LeftToRight
            )
            changed_layouts = []
            flow_layouts = [self.browser_controls, self.provide_source_layout]
            if getattr(self, "_atlas_embedded", False):
                flow_layouts.extend(view["headline_controls"] for view in self._detail_views.values())
                flow_layouts.append(self.file_drop_zone.layout())
                flow_layouts.append(self.provide_source_tier_layout)
            for flow_layout in flow_layouts:
                target_direction = narrow_direction
                if (
                    getattr(self, "_atlas_embedded", False)
                    and flow_layout is self.provide_source_layout
                ):
                    target_direction = QtWidgets.QBoxLayout.Direction.TopToBottom
                elif getattr(self, "_atlas_embedded", False):
                    available_width = controls_width
                    if flow_layout is self.file_drop_zone.layout():
                        # File actions sit inside an additional framed card.
                        # Its padding must fit too, not only the button label.
                        frame_margins = self.file_drop_zone.contentsMargins()
                        available_width -= frame_margins.left() + frame_margins.right()
                    flow_margins = flow_layout.contentsMargins()
                    inner_available_width = max(
                        1,
                        available_width - flow_margins.left() - flow_margins.right(),
                    )
                    item_widths = []
                    for index in range(flow_layout.count()):
                        item = flow_layout.itemAt(index)
                        widget = item.widget()
                        if widget is not None and widget.isHidden():
                            continue
                        if widget is not None:
                            item_width = max(
                                widget.minimumWidth(),
                                widget.minimumSizeHint().width(),
                                widget.sizeHint().width(),
                            )
                            if isinstance(widget, QtWidgets.QPushButton):
                                item_width = natural_button_width(widget)
                        elif item.layout() is not None:
                            item_width = max(
                                item.layout().minimumSize().width(),
                                item.layout().sizeHint().width(),
                            )
                        else:
                            spacer = item.spacerItem()
                            item_width = (
                                spacer.sizeHint().width() if spacer is not None else 0
                            )
                        item_widths.append(max(0, item_width))
                    natural_width = (
                        flow_margins.left() + flow_margins.right()
                        + sum(item_widths)
                        + max(0, flow_layout.spacing()) * max(0, len(item_widths) - 1)
                    )
                    target_direction = (
                        QtWidgets.QBoxLayout.Direction.LeftToRight
                        if natural_width <= max(1, available_width)
                        else QtWidgets.QBoxLayout.Direction.TopToBottom
                    )
                    for index in range(flow_layout.count()):
                        control = flow_layout.itemAt(index).widget()
                        if isinstance(control, QtWidgets.QPushButton):
                            _fit_button_label(control, inner_available_width)
                if flow_layout.direction() != target_direction:
                    flow_layout.setDirection(target_direction)
                    flow_layout.invalidate()
                    changed_layouts.append(flow_layout)
            if changed_layouts:
                for flow_layout in changed_layouts:
                    flow_layout.activate()
                    parent_widget = flow_layout.parentWidget()
                    if parent_widget is not None:
                        parent_widget.updateGeometry()
                self.action_panel.layout().invalidate()
                self.action_panel.layout().activate()
                self.detail_content.layout().invalidate()
                self.detail_content.layout().activate()
            if embedded and not compact and not self._embedded_split_initialized:
                allocatable_width = max(
                    0,
                    splitter_available_width
                    - self.detail_splitter.handleWidth(),
                )
                source_stretch = max(
                    0, self.detail_splitter.widget(0).sizePolicy().horizontalStretch()
                )
                detail_stretch = max(
                    0, self.detail_splitter.widget(1).sizePolicy().horizontalStretch()
                )
                total_stretch = source_stretch + detail_stretch
                detail_ratio = (
                    detail_stretch / total_stretch
                    if total_stretch else 0.5
                )
                detail_width = min(
                    allocatable_width - source_minimum_width,
                    max(
                        detail_minimum_width,
                        round(allocatable_width * detail_ratio),
                    ),
                )
                source_width = allocatable_width - detail_width
                self.detail_splitter.setSizes([source_width, detail_width])
                self._embedded_split_initialized = True
            elif embedded and not compact and self.detail_panel.width() < detail_minimum_width:
                source_width = max(
                    source_minimum_width,
                    splitter_available_width
                    - detail_minimum_width
                    - self.detail_splitter.handleWidth(),
                )
                detail_width = max(
                    detail_minimum_width,
                    splitter_available_width
                    - source_width
                    - self.detail_splitter.handleWidth(),
                )
                self.detail_splitter.setSizes([source_width, detail_width])
            self._apply_detail_tab_height()
            self._update_tab_scroll_controls()
            self._sync_compact_drawer_geometry()

        def _update_identity_action_layout(self):
            layout = getattr(self, "identity_actions", None)
            if layout is None:
                return
            margins = layout.contentsMargins()
            required_width = (
                max(self.attest_identity_button.sizeHint().width(),
                    self.attest_identity_button.minimumSizeHint().width())
                + max(self.keep_unverified_button.sizeHint().width(),
                      self.keep_unverified_button.minimumSizeHint().width())
                + max(0, layout.spacing())
                + margins.left() + margins.right()
            )
            panel_layout = self.identity_review_panel.layout()
            panel_margins = panel_layout.contentsMargins()
            available_width = (
                self.identity_review_panel.width()
                - panel_margins.left() - panel_margins.right()
            )
            direction = (
                QtWidgets.QBoxLayout.Direction.LeftToRight
                if available_width >= required_width
                else QtWidgets.QBoxLayout.Direction.TopToBottom
            )
            if layout.direction() != direction:
                layout.setDirection(direction)
                layout.invalidate()
                panel_layout.activate()

        def _set_compact_drawer_mode(self, compact: bool):
            if compact == self._compact_drawer_mode or not hasattr(self, "detail_panel"):
                return
            central = self.centralWidget()
            if compact:
                sizes = self.detail_splitter.sizes()
                if len(sizes) == 2 and sizes[1] > 0:
                    self._last_wide_splitter_sizes = sizes
            self._stop_compact_drawer_animation()
            self._compact_drawer_open = False
            self._set_compact_background_focusable(True)
            self.compact_navigation.hide()
            self.detail_panel.hide()
            if compact:
                self.detail_panel.setMinimumWidth(0)
                self.detail_panel.layout().setContentsMargins(8, 8, 8, 0)
                self.detail_panel.setParent(central)
                self.detail_panel.setGeometry(central.rect())
            else:
                self.detail_panel.setMinimumWidth(0)
                self.detail_panel.layout().setContentsMargins(0, 0, 0, 0)
                self.detail_panel.setParent(self.detail_splitter)
                if self.detail_splitter.indexOf(self.detail_panel) < 0:
                    self.detail_splitter.addWidget(self.detail_panel)
                self.detail_panel.show()
            self._compact_drawer_mode = compact
            if (
                compact
                and self._embedded_split_initialized
                and self._current_ref_id is not None
            ):
                self._animate_compact_drawer(opening=True, duration=0)

        def _set_compact_background_focusable(self, focusable: bool):
            if focusable:
                for widget, policy in self._compact_drawer_focus_policies:
                    try:
                        widget.setFocusPolicy(policy)
                    except RuntimeError:
                        pass
                self._compact_drawer_focus_policies.clear()
                return
            if self._compact_drawer_focus_policies:
                return
            background_widgets = [self.source_panel, self.guided_fetch_footer]
            background_widgets.extend(self.source_panel.findChildren(QtWidgets.QWidget))
            background_widgets.extend(
                self.guided_fetch_footer.findChildren(QtWidgets.QWidget)
            )
            header = self.brand_logo.parentWidget()
            if header is not None:
                background_widgets.append(header)
                background_widgets.extend(header.findChildren(QtWidgets.QWidget))
            for widget in background_widgets:
                policy = widget.focusPolicy()
                if policy != QtCore.Qt.FocusPolicy.NoFocus:
                    self._compact_drawer_focus_policies.append((widget, policy))
                    widget.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        def _open_compact_details_from_item(self, item):
            if (
                not self._compact_drawer_mode
                or item is None
                or self._compact_drawer_open
            ):
                return
            self.source_filter_menu.hide()
            self._animate_compact_drawer(opening=True)

        def _close_compact_details(self):
            if self._compact_drawer_mode and self._compact_drawer_open:
                self._animate_compact_drawer(opening=False)

        def _stop_compact_drawer_animation(self):
            animation = self._compact_drawer_animation
            self._compact_drawer_animation = None
            if animation is not None:
                animation.stop()
                animation.deleteLater()

        def _animate_compact_drawer(self, *, opening: bool, start_rect=None, duration=200):
            if not self._compact_drawer_mode:
                return
            central = self.centralWidget()
            width, height = central.width(), central.height()
            visible_rect = QtCore.QRect(0, 0, width, height)
            hidden_rect = QtCore.QRect(width, 0, width, height)
            was_visible = self.detail_panel.isVisible()
            current_rect = self.detail_panel.geometry()
            if opening and start_rect is None and was_visible:
                start_rect = current_rect
            self._stop_compact_drawer_animation()
            self._compact_drawer_open = opening
            if opening:
                self._set_compact_background_focusable(False)
                self.compact_navigation.show()
                self.detail_panel.setGeometry(
                    start_rect if start_rect is not None else hidden_rect
                )
                self.detail_panel.show()
                self.detail_panel.raise_()
                self.compact_back_button.setFocus(
                    QtCore.Qt.FocusReason.ShortcutFocusReason
                )
            end_rect = visible_rect if opening else hidden_rect
            if not self.detail_panel.isVisible():
                self.detail_panel.show()
                self.detail_panel.raise_()
            if self.detail_panel.geometry() == end_rect or duration <= 0:
                self.detail_panel.setGeometry(end_rect)
                self._compact_drawer_animation_finished(None, opening)
                return
            animation = QtCore.QPropertyAnimation(
                self.detail_panel, b"geometry", self
            )
            animation.setDuration(max(1, duration))
            animation.setStartValue(self.detail_panel.geometry())
            animation.setEndValue(end_rect)
            animation.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
            animation.finished.connect(
                lambda animation=animation, opening=opening:
                self._compact_drawer_animation_finished(animation, opening)
            )
            self._compact_drawer_animation = animation
            animation.start()

        def _compact_drawer_animation_finished(self, animation, opening: bool):
            if animation is not None and animation is not self._compact_drawer_animation:
                return
            self._compact_drawer_animation = None
            if animation is not None:
                animation.deleteLater()
            if not self._compact_drawer_mode:
                return
            central = self.centralWidget()
            if opening:
                self.detail_panel.setGeometry(central.rect())
                self.detail_panel.raise_()
                QtCore.QTimer.singleShot(0, self._update_action_layouts)
                return
            self.detail_panel.hide()
            self.compact_navigation.hide()
            self._set_compact_background_focusable(True)
            self.table.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

        def _sync_compact_drawer_geometry(self):
            if not self._compact_drawer_mode or not self.detail_panel.isVisible():
                return
            central = self.centralWidget()
            width, height = central.width(), central.height()
            current = self.detail_panel.geometry()
            if current.width() == width and current.height() == height:
                return
            animation = self._compact_drawer_animation
            if animation is None or animation.state() != QtCore.QAbstractAnimation.State.Running:
                if self._compact_drawer_open:
                    self.detail_panel.setGeometry(central.rect())
                    self.detail_panel.raise_()
                return
            old_width = max(1, current.width())
            current_x = round(current.x() * width / old_width)
            start_rect = QtCore.QRect(current_x, 0, width, height)
            remaining = max(1, animation.duration() - animation.currentTime())
            opening = self._compact_drawer_open
            self._stop_compact_drawer_animation()
            self.detail_panel.setGeometry(start_rect)
            self._animate_compact_drawer(
                opening=opening, start_rect=start_rect, duration=remaining
            )

        def _set_staged_preview_height(self, staged: bool):
            self._staged_preview_active = staged
            self._apply_detail_tab_height()

        def _apply_detail_tab_height(self):
            preview_active = self.tabs.currentWidget() is self._preview_tab_widget
            self.detail_content.layout().setStretch(
                self._detail_spacer_index, 0 if preview_active else 1
            )
            identity_active = (
                hasattr(self, "identity_review_panel")
                and self.tabs.currentWidget() is self.identity_review_panel
            )
            if identity_active:
                self.identity_facts_scroll.setMinimumHeight(220)
                height = 420
                if self.identity_actions.direction() == (
                    QtWidgets.QBoxLayout.Direction.TopToBottom
                ):
                    height += (
                        self.keep_unverified_button.sizeHint().height()
                        + max(0, self.identity_actions.spacing())
                    )
                self.tabs.setMinimumHeight(height)
                self.tabs.setMaximumHeight(height)
                return
            self.identity_facts_scroll.setMinimumHeight(50)
            if self._staged_preview_active:
                height = max(224, self._preview_required_tab_height())
                self.tabs.setMaximumHeight(height)
                self.tabs.setMinimumHeight(height)
                return
            if not preview_active:
                height = self._metadata_tab_required_height()
                self.tabs.setMaximumHeight(height)
                self.tabs.setMinimumHeight(height)
                return
            self.tabs.setMaximumHeight(16777215)
            self.tabs.setMinimumHeight(200)

        def _metadata_tab_required_height(self) -> int:
            """Measure a metadata tab's natural height for the visible facts."""
            panel = self.tabs.currentWidget()
            layout = panel.layout() if panel is not None else None
            if layout is None:
                return 220
            margins = layout.contentsMargins()
            panel_width = max(panel.width(), self.tabs.contentsRect().width())
            content_width = max(
                1, panel_width - margins.left() - margins.right()
            )
            content_height = margins.top() + margins.bottom()
            visible_items = 0
            metadata_table = panel.findChild(QtWidgets.QTableWidget, "detailTable")
            if metadata_table is not None:
                metadata_table.resizeRowsToContents()
            for index in range(layout.count()):
                item = layout.itemAt(index)
                widget = item.widget()
                child_layout = item.layout()
                if widget is not None:
                    if widget.isHidden():
                        continue
                    if widget is metadata_table:
                        header = widget.horizontalHeader()
                        horizontal_scrollbar = widget.horizontalScrollBar()
                        header_height = (
                            max(header.height(), header.sizeHint().height())
                            if not header.isHidden() else 0
                        )
                        horizontal_scrollbar_height = (
                            max(
                                horizontal_scrollbar.height(),
                                horizontal_scrollbar.sizeHint().height(),
                            )
                            if horizontal_scrollbar.isVisible() else 0
                        )
                        row_height = sum(
                            widget.rowHeight(row)
                            for row in range(widget.rowCount())
                            if not widget.isRowHidden(row)
                        )
                        item_height = (
                            2 * widget.frameWidth()
                            + header_height
                            + row_height
                            + horizontal_scrollbar_height
                        )
                    elif widget.hasHeightForWidth():
                        wrapped_height = widget.heightForWidth(content_width)
                        item_height = max(
                            widget.minimumSizeHint().height(),
                            wrapped_height if wrapped_height > 0 else 0,
                        )
                    else:
                        item_height = max(
                            widget.minimumSizeHint().height(),
                            widget.sizeHint().height(),
                        )
                elif child_layout is not None:
                    item_height = child_layout.sizeHint().height()
                else:
                    continue
                if visible_items:
                    content_height += max(0, layout.spacing())
                content_height += max(0, item_height)
                visible_items += 1
            return (
                content_height
                + self.tabs.tabBar().sizeHint().height()
                + 4  # Allow for the tab pane frame around the measured panel.
            )

        def _preview_required_tab_height(self) -> int:
            """Measure the staged Preview's visible content at its current width."""
            layout = self._preview_tab_widget.layout()
            margins = layout.contentsMargins()
            page_width = max(
                self._preview_tab_widget.width(),
                self.tabs.contentsRect().width(),
            )
            if page_width <= 0:
                split_sizes = self.detail_splitter.sizes()
                page_width = split_sizes[1] if len(split_sizes) > 1 else self.width()
            content_width = max(
                1,
                page_width - margins.left() - margins.right(),
            )
            content_height = margins.top() + margins.bottom()
            visible_items = 0
            for index in range(layout.count()):
                item = layout.itemAt(index)
                widget = item.widget()
                child_layout = item.layout()
                if widget is not None:
                    if widget.isHidden():
                        continue
                    item_height = widget.minimumSize().height()
                    if widget.hasHeightForWidth():
                        wrapped_height = widget.heightForWidth(content_width)
                        if wrapped_height > 0:
                            item_height = max(item_height, wrapped_height)
                elif child_layout is not None:
                    item_height = child_layout.minimumSize().height()
                else:
                    continue
                if visible_items:
                    content_height += max(0, layout.spacing())
                content_height += item_height
                visible_items += 1

            return content_height + self.tabs.tabBar().sizeHint().height()

        def _detail_panel(self, name: str):
            panel = QtWidgets.QWidget(self.tabs)
            layout = QtWidgets.QVBoxLayout(panel)
            layout.setContentsMargins(8, 7, 8, 7)
            layout.setSpacing(6)
            if name == "Preview":
                layout.setContentsMargins(6, 5, 6, 5)
                layout.setSpacing(4)
            headline = QtWidgets.QLabel(panel)
            headline.setObjectName("detailHeadline")
            headline.setWordWrap(True)
            instructions_button = QtWidgets.QPushButton(panel)
            instructions_button.setObjectName("instructionsButton")
            instructions_button.setText(labels["guided_fetch_instructions"])
            instructions_button.setIcon(self.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_MessageBoxQuestion
            ))
            instructions_button.setToolTip(labels["guided_fetch_instructions_tooltip"])
            instructions_button.setAccessibleName(labels["guided_fetch_instructions"])
            raw_button = QtWidgets.QPushButton(
                labels["guided_fetch_show_original_json"], panel
            )
            raw_button.setObjectName("rawJsonButton")
            headline_controls = QtWidgets.QHBoxLayout()
            headline_controls.addWidget(instructions_button)
            headline_controls.addStretch()
            headline_controls.addWidget(raw_button)
            table = QtWidgets.QTableWidget(0, 2, panel)
            table.setObjectName("detailTable")
            table.setHorizontalHeaderLabels([
                labels["guided_fetch_detail_column"],
                labels["guided_fetch_recorded_value"],
            ])
            table.setWordWrap(True)
            table.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
            table.setHorizontalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            table.setShowGrid(False)
            if name == "Preview":
                # Keep these recorded metadata rows and raw payload available,
                # while giving the actual source text the full preview surface.
                table.setVisible(False)
            table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setDefaultSectionSize(32)
            table.verticalHeader().setMinimumSectionSize(22)
            table.verticalHeader().setSectionResizeMode(
                QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
            table.setVerticalScrollMode(
                QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel
            )
            detail_header = table.horizontalHeader()
            detail_header.setSectionResizeMode(
                0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
            detail_header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
            from core.gui.shell.resize import install_table_resize
            install_table_resize(table, QtCore, QtWidgets)

            class _MetadataItemDelegate(QtWidgets.QStyledItemDelegate):
                def sizeHint(self, option, index):  # noqa: N802 - Qt API name
                    if not (
                        option.features
                        & QtWidgets.QStyleOptionViewItem.ViewItemFeature.WrapText
                    ):
                        return super().sizeHint(option, index)
                    effective = QtWidgets.QStyleOptionViewItem(option)
                    self.initStyleOption(effective, index)
                    styled_widget = effective.widget or table
                    text_rect = styled_widget.style().subElementRect(
                        QtWidgets.QStyle.SubElement.SE_ItemViewItemText,
                        effective,
                        styled_widget,
                    )
                    if effective.rect.width() <= 0 or text_rect.width() <= 0:
                        return super().sizeHint(effective, index)
                    # Item padding narrows the painted text area, while Qt's
                    # row-content hint uses the full cell width. Measure at the
                    # styled text width so wrapped rows reserve enough height.
                    measured = QtWidgets.QStyleOptionViewItem(effective)
                    measured.rect.setWidth(text_rect.width())
                    return super().sizeHint(measured, index)

            table.setItemDelegate(_MetadataItemDelegate(table))

            window = self

            class _DetailRowResizeFilter(QtCore.QObject):
                _RESIZE_EVENTS = {
                    QtCore.QEvent.Type.Show,
                    QtCore.QEvent.Type.Resize,
                    QtCore.QEvent.Type.FontChange,
                    QtCore.QEvent.Type.StyleChange,
                }

                def __init__(self):
                    super().__init__(table)
                    self._queued = False
                    self._resize_timer = QtCore.QTimer(self)
                    self._resize_timer.setSingleShot(True)
                    self._resize_timer.timeout.connect(self._resize_rows)

                def eventFilter(self, watched, event):
                    if watched is table and event.type() in self._RESIZE_EVENTS:
                        self.schedule()
                    return False

                def schedule(self, *_args):
                    if self._queued:
                        return
                    self._queued = True
                    self._resize_timer.start(0)

                def _resize_rows(self):
                    self._queued = False
                    table.resizeRowsToContents()
                    if (
                        window.tabs.currentWidget() is panel
                        and panel is not window._preview_tab_widget
                    ):
                        window._apply_detail_tab_height()

            row_resize_filter = _DetailRowResizeFilter()
            table._detail_row_resize_filter = row_resize_filter
            table.installEventFilter(row_resize_filter)
            detail_header.sectionResized.connect(row_resize_filter.schedule)
            raw_button.clicked.connect(
                lambda checked=False, panel_name=name: self._show_raw_json(panel_name)
            )
            instructions_button.clicked.connect(self._show_instructions)
            layout.addWidget(headline)
            layout.addWidget(table, 1)
            if name == "Preview":
                preview_title = ElidedLabel(panel)
                preview_title.setObjectName("previewSourceTitle")
                preview_title.setWordWrap(False)
                preview_title.setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Ignored,
                    QtWidgets.QSizePolicy.Policy.Fixed,
                )
                preview_title.setMinimumWidth(0)
                preview_tier_button = QtWidgets.QPushButton(panel)
                preview_tier_button.setObjectName("previewTierButton")
                preview_tier_button.setMinimumWidth(0)
                preview_tier_button.clicked.connect(self._open_parsed_text_viewer)
                stack = QtWidgets.QStackedWidget(panel)
                stack.setObjectName("previewStack")
                loader = QtWidgets.QWidget(stack)
                loader.setObjectName("previewLoadingSkeleton")
                loader_layout = QtWidgets.QVBoxLayout(loader)
                loader_layout.setContentsMargins(18, 18, 18, 18)
                loader_layout.setSpacing(13)
                from .atlas_loading import make_loading_bar
                animations = []
                for index in range(6):
                    bar, animation = make_loading_bar(
                        QtCore, QtGui, QtWidgets, loader,
                        lambda: self._dark_theme,
                        duration=900 + index * 80,
                    )
                    bar.setObjectName("previewSkeletonBar")
                    bar.setFixedHeight(20 if index % 3 == 0 else 14)
                    bar.setMaximumWidth(560 if index % 3 == 0 else 350 + index * 18)
                    animations.append(animation)
                    loader_layout.addWidget(bar)
                loader_layout.addStretch()
                preview = QtWidgets.QPlainTextEdit(panel)
                preview.setObjectName("previewText")
                preview.setReadOnly(True)
                preview.setHorizontalScrollBarPolicy(
                    QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                )
                preview.setLineWrapMode(
                    QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth
                )
                preview.setWordWrapMode(
                    QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
                )
                stack.addWidget(loader)
                stack.addWidget(preview)
                stack.setCurrentWidget(preview)
                stack.setMinimumHeight(100)
                preview.setMinimumHeight(100)
                preview_heading_row = QtWidgets.QHBoxLayout()
                preview_heading_row.setSpacing(7)
                preview_icon = QtWidgets.QLabel(panel)
                preview_icon.setObjectName("previewDocumentIcon")
                preview_icon.setFixedSize(20, 20)
                preview_heading_row.addWidget(preview_icon)
                preview_heading_row.addWidget(preview_title, 1)
                preview_heading_row.addWidget(preview_tier_button)
                layout.addLayout(preview_heading_row)
                layout.addWidget(stack, 1)
            else:
                preview_title = None
                preview_tier_button = None
                preview = None
                stack = None
                loader = None
                animations = []
            layout.addLayout(headline_controls)
            return panel, {
                "headline": headline,
                "headline_controls": headline_controls,
                "table": table,
                "raw": "",
                "button": raw_button,
                "preview": preview,
                "preview_heading": preview_tier_button,
                "preview_tier_button": preview_tier_button,
                "preview_title": preview_title,
                "preview_stack": stack,
                "preview_loader": loader,
                "preview_animations": animations,
            }

        def _show_raw_json(self, name: str):
            self._show_text_dialog(
                labels["guided_fetch_original_audit_json"],
                self._detail_views[name]["raw"],
            )

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

        def _open_parsed_text_viewer(self):
            ref_id = self._current_ref_id
            path = self._selected_preview_path
            if not isinstance(ref_id, str):
                return
            dialog_key = (ref_id, path)
            if (
                self._parsed_text_dialog is not None
                and self._parsed_text_dialog_key == dialog_key
                and self._parsed_text_dialog.isVisible()
            ):
                self._parsed_text_dialog.raise_()
                self._parsed_text_dialog.activateWindow()
                return

            self._clear_parsed_text_viewer()
            self._parsed_text_request_id += 1
            request = (self._parsed_text_request_id, ref_id, path)
            self._parsed_text_request = request
            view = self._detail_views["Preview"]
            title = view["preview_title"].full_text()
            tier = view["preview_tier_button"].text()
            dialog = QtWidgets.QDialog(self)
            dialog.setObjectName("parsedTextDialog")
            dialog.setWindowTitle(f"{tier} · {title}" if title else tier)
            dialog.setMinimumSize(560, 360)
            dialog.resize(900, 650)
            dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
            layout = QtWidgets.QVBoxLayout(dialog)
            status_text = view["headline"].toolTip() or view["headline"].text()
            if status_text:
                status = QtWidgets.QLabel(status_text, dialog)
                status.setObjectName("parsedTextStatus")
                status.setWordWrap(True)
                layout.addWidget(status)
            editor = QtWidgets.QPlainTextEdit(dialog)
            editor.setObjectName("parsedTextViewer")
            editor.setReadOnly(True)
            editor.setLineWrapMode(
                QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth
            )
            layout.addWidget(editor, 1)
            close_button = QtWidgets.QPushButton(
                labels["guided_fetch_parsed_text_close"], dialog
            )
            close_button.clicked.connect(dialog.accept)
            layout.addWidget(close_button, 0, QtCore.Qt.AlignmentFlag.AlignRight)
            dialog.finished.connect(
                lambda _result, dialog=dialog, request=request:
                self._parsed_text_dialog_finished(request, dialog)
            )
            self._parsed_text_dialog = dialog
            self._parsed_text_editor = editor
            self._parsed_text_dialog_key = dialog_key
            dialog.show()

            if not isinstance(path, str) or Path(path).suffix.lower() != ".txt":
                editor.setPlainText(labels["guided_fetch_parsed_text_unavailable"])
                return
            editor.setPlainText(labels["guided_fetch_parsed_text_loading"])
            if self._parsed_text_thread is None:
                self._start_parsed_text_worker(request)
            else:
                self._parsed_text_pending_request = request

        def _start_parsed_text_worker(self, request):
            if (
                self._parsed_text_thread is not None
                or request != self._parsed_text_request
                or request[1] != self._current_ref_id
                or request[2] != self._selected_preview_path
            ):
                return
            path = request[2]
            if not isinstance(path, str) or Path(path).suffix.lower() != ".txt":
                return
            thread = QtCore.QThread(self)
            worker = ParsedTextWorker(request, path)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            self._connect_gui_signal(worker.loaded, self._parsed_text_worker_loaded)
            worker.loaded.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            self._connect_gui_signal(thread.finished, self._parsed_text_thread_finished)
            self._parsed_text_thread = thread
            self._parsed_text_worker = worker
            self._parsed_text_active_request = request
            thread.start()

        def _parsed_text_worker_loaded(self, request, text):
            if (
                request != self._parsed_text_request
                or request[1] != self._current_ref_id
                or request[2] != self._selected_preview_path
                or self._parsed_text_editor is None
                or self._parsed_text_dialog is None
            ):
                return
            self._parsed_text_editor.setPlainText(
                text if isinstance(text, str)
                else labels["guided_fetch_parsed_text_unavailable"]
            )

        def _parsed_text_thread_finished(self):
            self._join_worker_thread(self._parsed_text_thread)
            self._parsed_text_thread = None
            self._parsed_text_worker = None
            self._parsed_text_active_request = None
            pending = self._parsed_text_pending_request
            self._parsed_text_pending_request = None
            if pending is not None and pending == self._parsed_text_request:
                self._start_parsed_text_worker(pending)
            elif self._parsed_text_close_when_finished:
                self._parsed_text_close_when_finished = False
                self.close()

        def _parsed_text_dialog_finished(self, request, dialog):
            if self._parsed_text_dialog is not dialog:
                return
            if self._parsed_text_request == request:
                self._parsed_text_request_id += 1
                self._parsed_text_request = None
                self._parsed_text_pending_request = None
            self._parsed_text_dialog = None
            self._parsed_text_editor = None
            self._parsed_text_dialog_key = None

        def _clear_parsed_text_viewer(self):
            self._parsed_text_request_id += 1
            self._parsed_text_request = None
            self._parsed_text_pending_request = None
            dialog = self._parsed_text_dialog
            self._parsed_text_dialog = None
            self._parsed_text_editor = None
            self._parsed_text_dialog_key = None
            if dialog is not None:
                dialog.close()

        def _selected_tier(self) -> str:
            return "abstract" if self.tier_switch.isChecked() else "fulltext"

        @staticmethod
        def _is_pdf_path(path: str | None) -> bool:
            return isinstance(path, str) and Path(path).suffix.lower() == ".pdf"

        def _preflight_busy(self) -> bool:
            active = self._preflight_active_request
            active_busy = self._preflight_thread is not None and not (
                active is not None
                and active.get("kind") == "browser"
                and not self._browser_preflight_job_is_current(active)
            )
            return active_busy or bool(self._preflight_pending) or bool(
                self._browser_preflight_pending
            )

        def _refresh_browser_preflight_request_id(self):
            active = self._preflight_active_request
            if active is not None and active.get("kind") == "browser":
                # Keep the marker through cancellation until the worker has stopped.
                # The worker still owns the shared PDF-check slot during that time.
                self._browser_preflight_request_id = active.get("request_id")
                return
            pending = min(
                self._browser_preflight_pending,
                key=lambda item: item["request_id"],
                default=None,
            )
            self._browser_preflight_request_id = (
                pending.get("request_id") if pending is not None else None
            )

        def _browser_preflight_job_for_ref(self, ref_id: str | None):
            if ref_id is None:
                return None
            return next(
                (
                    job for job in self._browser_pdf_preflight_jobs.values()
                    if job.get("ref_id") == ref_id
                ),
                None,
            )

        def _browser_preflight_job_is_current(self, request) -> bool:
            key = request.get("capture_key")
            job = self._browser_pdf_preflight_jobs.get(key)
            return (
                isinstance(key, tuple)
                and job is not None
                and job.get("request_id") == request.get("request_id")
            )

        def _refresh_browser_preflight_row(self, ref_id: str):
            if not isinstance(ref_id, str):
                return
            selected_ref_id = self._current_ref_id
            self._render_rows(select_ref_id=selected_ref_id)
            if selected_ref_id is not None:
                self._selected()

        def _queue_preflight(self, kind: str, path: str, tier: str, **context):
            self._next_preflight_request_id += 1
            request = {
                "request_id": self._next_preflight_request_id,
                "kind": kind,
                "path": path,
                "tier": tier,
                "run_dir": run_dir,
                **context,
            }
            if kind == "browser":
                capture_key = request.get("capture_key")
                if not isinstance(capture_key, tuple):
                    raise ValueError("Browser PDF preflight needs its capture identity.")
                self._browser_pdf_preflight_jobs[capture_key] = request
            if self._preflight_thread is None:
                self._start_preflight_worker(request)
            else:
                if kind == "browser":
                    self._browser_preflight_pending.append(request)
                    self._refresh_browser_preflight_request_id()
                else:
                    # Selection requests coalesce to the newest file.
                    self._preflight_pending[kind] = request
            if kind == "browser":
                self._refresh_browser_preflight_request_id()
                self._refresh_browser_preflight_row(request.get("ref_id"))
            self._update_ocr_button()
            self._update_proceed_button()
            return request["request_id"]

        def _start_preflight_worker(self, request):
            if self._preflight_thread is not None or self._preflight_close_when_finished:
                if request.get("kind") == "browser":
                    if not any(
                        item.get("request_id") == request.get("request_id")
                        for item in self._browser_preflight_pending
                    ):
                        self._browser_preflight_pending.append(request)
                    self._refresh_browser_preflight_request_id()
                else:
                    self._preflight_pending[request["kind"]] = request
                return
            thread = QtCore.QThread(self)
            worker = PreflightWorker(request)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            self._connect_gui_signal(worker.finished, self._preflight_worker_result)
            worker.finished.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            self._connect_gui_signal(thread.finished, self._preflight_worker_stopped)
            self._preflight_active_request = request
            self._preflight_thread = thread
            self._preflight_worker = worker
            self._refresh_browser_preflight_request_id()
            if request.get("kind") == "browser":
                self._refresh_browser_preflight_row(request.get("ref_id"))
            thread.start()

        def _preflight_request_is_current_selection(self, request) -> bool:
            return (
                request.get("ref_id") == self._current_ref_id
                and request.get("ref_id") == self._selected_file_ref_id
                and request.get("path") == self._selected_file
                and request.get("file_generation") == self._selected_file_generation
                and request.get("context_generation") == self._selection_context_generation
                and request.get("tier") == self._selected_tier()
            )

        def _queue_selected_file_preflight(self):
            if self._identity_operation_busy() or self._proceed_preparing:
                return
            path = self._selected_file
            ref_id = self._selected_file_ref_id
            generation = self._selected_file_generation
            if ref_id is not None and generation is not None and (
                ref_id, generation
            ) in self._consumed_file_selections:
                return
            if (
                not self._is_pdf_path(path)
                or ref_id is None
                or ref_id != self._current_ref_id
                or generation is None
                or not self._model.can_submit_source(ref_id)
            ):
                return
            tier = self._selected_tier()
            if (
                not self._selected_file_preflight_pending
                and self._selected_file_preflight_tier == tier
            ):
                return
            request = {
                "kind": "selection",
                "path": path,
                "ref_id": ref_id,
                "file_generation": generation,
                "context_generation": self._selection_context_generation,
                "tier": tier,
            }
            for active in (self._preflight_active_request, *self._preflight_pending.values()):
                if active is not None and all(
                    active.get(key) == request[key]
                    for key in (
                        "kind", "path", "ref_id", "file_generation",
                        "context_generation", "tier",
                    )
                ):
                    self._selected_file_preflight_pending = True
                    self.file_label.setText("Checking PDF readability…")
                    self.submit.setEnabled(False)
                    return
            self._selected_file_preflight_pending = True
            self._selected_file_preflight_tier = None
            self._selected_file_ocr_pending = False
            self.file_label.setToolTip("")
            self.file_label.setText("Checking PDF readability…")
            self.submit.setText("Checking PDF…")
            self.submit.setEnabled(False)
            self._queue_preflight(
                "selection",
                path,
                tier,
                ref_id=ref_id,
                file_generation=generation,
                context_generation=self._selection_context_generation,
            )

        def _tier_changed(self, _checked: bool):
            self.fulltext_label.setChecked(not self.tier_switch.isChecked())
            self.abstract_label.setChecked(self.tier_switch.isChecked())
            if self._identity_operation_busy() or self._proceed_preparing:
                return
            if self._is_pdf_path(self._selected_file):
                if self._selected_file_ref_id == self._current_ref_id:
                    selection_key = (
                        self._selected_file_ref_id,
                        self._selected_file_generation,
                    )
                    if selection_key in self._consumed_file_selections:
                        self._selected()
                        return
                    self._selected_file_preflight_pending = True
                    self._selected_file_preflight_tier = None
                    self._selected_file_ocr_pending = False
                    self._queue_selected_file_preflight()
                    return
            self._selected()

        def _finish_browser_preflight_job(self, request):
            key = request.get("capture_key")
            if not self._browser_preflight_job_is_current(request):
                return False
            self._browser_pdf_preflight_jobs.pop(key, None)
            self._refresh_browser_preflight_request_id()
            self._refresh_browser_preflight_row(request.get("ref_id"))
            self._update_proceed_button()
            return True

        def _preflight_worker_result(self, request, result):
            active = self._preflight_active_request
            if (
                active is None
                or active.get("request_id") != request.get("request_id")
                or self._preflight_close_when_finished
            ):
                return
            kind = request.get("kind")
            error = result.get("error")
            status = result.get("status")
            if kind == "selection":
                if (
                    not self._preflight_request_is_current_selection(request)
                    or not self._model.can_submit_source(request.get("ref_id"))
                ):
                    return
                self._selected_file_preflight_pending = False
                if error is not None:
                    self._selected_file = None
                    self._selected_file_ref_id = None
                    self._selected_file_generation = None
                    self._selected_file_preflight_tier = None
                    self._selected_file_ocr_pending = False
                    self.file_label.setText("File rejected. Choose another file.")
                    self.file_label.setToolTip(error)
                    QtWidgets.QMessageBox.warning(
                        self, "Source cannot be used", error
                    )
                else:
                    self._selected_file_preflight_tier = request["tier"]
                    self._selected_file_ocr_pending = status == "ocr_pending"
                    self.file_label.setToolTip("")
                    self._complete_pdf_submission(request, status)
                self._selected()
            elif kind == "submit":
                if self._submit_preflight_request_id == request.get("request_id"):
                    self._submit_preflight_request_id = None
                if (
                    not self._preflight_request_is_current_selection(request)
                    or (
                        request.get("ref_id"), request.get("file_generation")
                    ) in self._consumed_file_selections
                    or not self._model.can_submit_source(request.get("ref_id"))
                ):
                    self._selected()
                    return
                if error is not None:
                    QtWidgets.QMessageBox.critical(
                        self, "Source cannot be used", error
                    )
                    self._selected()
                    return
                self._complete_pdf_submission(request, status)
            elif kind == "browser":
                if (
                    self._browser_preflight_request_id != request.get("request_id")
                    or not self._browser_preflight_job_is_current(request)
                ):
                    return
                ref_id = request.get("ref_id")
                if error is not None:
                    self._finish_browser_preflight_job(request)
                    self._application_modal_warning("Browser capture", error)
                    return
                if self._identity_operation_busy() or not self._model.can_submit_source(ref_id):
                    self._finish_browser_preflight_job(request)
                    self._application_modal_warning(
                        "Browser capture",
                        "The PDF was not queued because its Fetch action is no longer available.",
                    )
                    return
                self._finish_browser_preflight_job(request)
                self._queue_browser_source(
                    request["artifact"], ref_id, request["tier"], status,
                    background_preflight=True,
                )

        def _preflight_worker_stopped(self):
            self._join_worker_thread(self._preflight_thread)
            request = self._preflight_active_request
            if request is None:
                return
            self._preflight_active_request = None
            self._preflight_thread = None
            self._preflight_worker = None
            if self._preflight_close_when_finished:
                self._preflight_pending.clear()
                self._browser_preflight_pending.clear()
                self._browser_pdf_preflight_jobs.clear()
                self._refresh_browser_preflight_request_id()
                self._preflight_close_when_finished = False
                self.close()
                return
            self._refresh_browser_preflight_request_id()
            while self._preflight_pending or self._browser_preflight_pending:
                candidates = [
                    *self._preflight_pending.values(),
                    *self._browser_preflight_pending,
                ]
                next_request = min(candidates, key=lambda item: item["request_id"])
                if next_request.get("kind") == "browser":
                    self._browser_preflight_pending = [
                        item for item in self._browser_preflight_pending
                        if item.get("request_id") != next_request.get("request_id")
                    ]
                    if not self._browser_preflight_job_is_current(next_request):
                        self._refresh_browser_preflight_request_id()
                        continue
                    if not self._model.can_submit_source(next_request.get("ref_id")):
                        self._finish_browser_preflight_job(next_request)
                        self._application_modal_warning(
                            "Browser capture",
                            "The PDF was not queued because its Fetch action is no longer available.",
                        )
                        continue
                else:
                    self._preflight_pending.pop(next_request["kind"], None)
                if next_request["kind"] == "selection" and not (
                    self._preflight_request_is_current_selection(next_request)
                ):
                    continue
                self._start_preflight_worker(next_request)
                break
            else:
                self._queue_selected_file_preflight()
            self._selected()
            self._update_ocr_button()
            self._update_proceed_button()
            self._start_pending_auto_ocr()

        def _complete_pdf_submission(self, request, preflight: str):
            self._stage_selected_file(
                request["ref_id"],
                request["path"],
                request["file_generation"],
                request["tier"],
                preflight,
                context_generation=request["context_generation"],
            )

        def _source_stage_busy(self) -> bool:
            """Whether a confirmed stage or discard mutation is active or queued."""
            return bool(
                self._source_mutation_thread is not None
                or self._source_mutation_active_request is not None
                or self._source_mutation_queue
            )

        def _source_mutation_for_ref(self, ref_id: str | None):
            if ref_id is None:
                return None
            active = self._source_mutation_active_request
            if active is not None and active.get("ref_id") == ref_id:
                return active
            return next(
                (
                    request for request in self._source_mutation_queue
                    if request.get("ref_id") == ref_id
                ),
                None,
            )

        def _queue_source_stage(
            self,
            ref_id: str,
            payload: dict[str, Any],
            *,
            display_name: str,
            audit_path: str | None,
            preflight: str,
            file_generation: int | None = None,
            context_generation: int | None = None,
            capture_key: tuple[str, int] | None = None,
        ) -> bool:
            if self._source_mutation_for_ref(ref_id) is not None:
                return False
            self._next_source_mutation_id += 1
            request = {
                "request_id": self._next_source_mutation_id,
                "action": "stage",
                "ref_id": ref_id,
                "path": payload["file_path"],
                "tier": payload.get("source_tier", "fulltext"),
                "source_ref": payload.get("url"),
                "payload": dict(payload),
                "source_stamp": _source_file_stamp(payload["file_path"]),
                "file_generation": file_generation,
                "context_generation": context_generation,
                "capture_key": capture_key,
                "display_name": display_name,
                "audit_path": audit_path,
                "preflight": preflight,
            }
            self._source_mutation_queue.append(request)
            self._start_next_source_mutation()
            self._refresh_source_mutation_ui()
            return True

        def _queue_source_discard(self, ref_id: str, *, source_path: str | None):
            if self._source_mutation_for_ref(ref_id) is not None:
                return False
            self._next_source_mutation_id += 1
            self._source_mutation_queue.append({
                "request_id": self._next_source_mutation_id,
                "action": "discard",
                "ref_id": ref_id,
                "path": source_path,
                "tier": None,
                "source_ref": None,
                "payload": None,
                "file_generation": None,
                "context_generation": None,
                "capture_key": None,
                "display_name": None,
                "audit_path": source_path,
                "preflight": None,
            })
            self._start_next_source_mutation()
            self._refresh_source_mutation_ui()
            return True

        def _start_next_source_mutation(self):
            if (
                self._source_mutation_thread is not None
                or self._source_mutation_active_request is not None
                or not self._source_mutation_queue
            ):
                return
            request = self._source_mutation_queue.pop(0)
            thread = QtCore.QThread(self)
            worker = SourceMutationWorker(
                request, self._submit_source, self._discard_source
            )
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.finished.connect(thread.quit)
            self._connect_gui_signal(thread.finished, self._source_mutation_thread_finished)
            thread.finished.connect(worker.deleteLater)
            self._source_mutation_active_request = request
            self._source_mutation_thread = thread
            self._source_mutation_worker = worker
            thread.start()

        @QtCore.Slot(object)
        def _source_mutation_worker_finished(self, outcome):
            request = self._source_mutation_active_request
            if (
                request is None
                or not isinstance(outcome, dict)
                or outcome.get("request_id") != request.get("request_id")
            ):
                return
            error = outcome.get("error")
            if error is not None:
                title = (
                    "Source not discarded"
                    if request["action"] == "discard" else "Source not captured"
                )
                self._application_modal_critical(title, str(error))
                self.statusBar().showMessage(str(error), 15_000)
                self._refresh_source_mutation_ui()
                return
            if request["action"] == "stage":
                generation = request.get("file_generation")
                if type(generation) is int:
                    self._consumed_file_selections.add((request["ref_id"], generation))
                self._mark_queued(
                    request["ref_id"],
                    request["display_name"],
                    audit_path=request.get("audit_path"),
                    ocr_pending=request.get("preflight") == "ocr_pending",
                    preserve_navigation=True,
                )
                self.statusBar().showMessage(
                    f"{request['display_name']} saved; queued for Fetch checks.", 10_000
                )
                if (
                    request.get("preflight") == "ocr_pending"
                    and self._run_ocr is not None
                ):
                    self._queue_auto_ocr([{
                        "ref_id": request["ref_id"], "scan_id": None,
                    }])
            else:
                self._finish_source_discard(request)
            self._refresh_source_mutation_ui()

        def _finish_source_discard(self, request):
            ref_id = request["ref_id"]
            self._queued_sources.pop(ref_id, None)
            self._staged_source_records.pop(ref_id, None)
            self._inventory_staged_source_refs.discard(ref_id)
            self._staged_ocr_refs.discard(ref_id)
            self._ocr_row_status.pop(ref_id, None)
            if self._selected_file_ref_id == ref_id:
                self._selected_file = None
                self._selected_file_ref_id = None
                self._selected_file_generation = None
                self._selected_file_ocr_pending = False
                self.file_label.setText(labels["guided_fetch_drop_file"])
            self.statusBar().showMessage(
                "Provided source discarded; it is no longer queued.", 10_000
            )
            self._application_modal_information(
                "Source discarded",
                "The artifact is no longer queued. Its captured audit file was not deleted.",
            )

        @QtCore.Slot()
        def _source_mutation_thread_finished(self):
            self._join_worker_thread(self._source_mutation_thread)
            worker = self._source_mutation_worker
            outcome = worker.outcome if worker is not None else None
            request = self._source_mutation_active_request
            if (
                request is not None
                and (
                    not isinstance(outcome, dict)
                    or outcome.get("request_id") != request.get("request_id")
                )
            ):
                outcome = {
                    "request_id": request.get("request_id"),
                    "error": "RuntimeError: source mutation worker returned no result",
                }
            self._source_mutation_worker_finished(outcome)
            self._source_mutation_thread = None
            self._source_mutation_worker = None
            self._source_mutation_active_request = None
            if self._source_mutation_queue:
                self._start_next_source_mutation()
            self._refresh_source_mutation_ui()
            if not self._source_stage_busy():
                if self._source_mutation_close_when_finished:
                    self._source_mutation_close_when_finished = False
                    self.close()
                else:
                    self._start_pending_auto_ocr()
                self.source_stage_idle.emit()

        def _refresh_source_mutation_ui(self):
            self._render_rows(select_ref_id=self._current_ref_id)
            if self._current_ref_id is not None:
                self._selected()
            self._update_ocr_button()
            self._update_proceed_button()

        def _stage_selected_file(
            self,
            ref_id: str,
            file_path: str,
            file_generation: int,
            tier: str,
            preflight: str,
            *,
            context_generation: int,
        ):
            if self._identity_operation_busy() or self._proceed_preparing:
                return
            if self._source_mutation_for_ref(ref_id) is not None or any(
                key[0] == ref_id for key in self._capture_tiers
            ):
                self.statusBar().showMessage(
                    "Finish the current source save or browser capture before replacing this reference.",
                    10_000,
                )
                return
            if (
                self._current_ref_id != ref_id
                or self._selected_file != file_path
                or self._selected_file_ref_id != ref_id
                or self._selected_file_generation != file_generation
                or self._selection_context_generation != context_generation
                or self._selected_tier() != tier
                or (ref_id, file_generation) in self._consumed_file_selections
            ):
                self._selected()
                return
            try:
                payload = source_payload(file_path=file_path, tier=tier)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.critical(self, "Source not captured", str(exc))
                self._selected()
                return
            if not self._queue_source_stage(
                ref_id,
                payload,
                display_name=Path(file_path).name,
                audit_path=None,
                preflight=preflight,
                file_generation=file_generation,
                context_generation=context_generation,
            ):
                self.statusBar().showMessage(
                    "A source save is already pending for this reference.", 10_000
                )

        def _change_font_size(self, delta: int):
            self._font_size_px = min(17, max(11, self._font_size_px + delta))
            self.font_smaller.setEnabled(self._font_size_px > 11)
            self.font_larger.setEnabled(self._font_size_px < 17)
            self._set_dark_theme(self._dark_theme)

        def _set_dark_theme(self, dark: bool):
            self._dark_theme = dark
            self.table.setProperty("darkTheme", dark)
            self.theme_toggle.setText("Light theme" if dark else "Dark theme")
            self.tier_switch.set_dark_theme(dark)
            if self._browser_capture_progress is not None:
                self._browser_capture_progress.set_dark_theme(dark)
            if dark:
                self.setStyleSheet(_theme_stylesheet(dark=True, font_size=self._font_size_px))
            else:
                self.setStyleSheet(_theme_stylesheet(dark=False, font_size=self._font_size_px))
            for view in self._detail_views.values():
                view["table"].resizeRowsToContents()
            self.brand_logo.setPixmap(_callimachus_wordmark(
                QtCore, QtGui, 215, 46, self.devicePixelRatioF(), dark=dark
            ))
            self._set_action_icons()
            self._apply_status_colors()
            self._advance_source_row_pulse()
            QtCore.QTimer.singleShot(0, self._update_action_layouts)
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

        def _set_action_icons(self):
            from .atlas_icons import icon as atlas_icon

            color = "#7ab4ff" if self._dark_theme else "#175fe6"
            icon_size = QtCore.QSize(18, 18)
            controls = (
                (self.open_link, "external"),
                (self.open_chrome, "chrome"),
                (self.choose_file, "file"),
                (self.submit, "check"),
                (self.ocr_button, "settings"),
                (self.proceed_button, "play"),
                (self.discard_source_button, "trash"),
            )
            for button, key in controls:
                button_color = (
                    "#ff9e99" if self._dark_theme else "#c62828"
                ) if button is self.discard_source_button else color
                button.setIcon(atlas_icon(QtCore, QtGui, key, color=button_color, size=18))
                button.setIconSize(icon_size)
            from core.gui.shell.navigation import set_back_button_presentation
            set_back_button_presentation(
                self.compact_back_button, QtCore, QtGui, dark=self._dark_theme,
                label=labels["guided_fetch_back_to_sources"],
                text=labels["guided_fetch_back_to_sources"],
            )
            self.compact_next_button.setIcon(
                atlas_icon(QtCore, QtGui, "arrow-right", color=color, size=20)
            )
            self.compact_next_button.setIconSize(QtCore.QSize(20, 20))
            for view in self._detail_views.values():
                view["button"].setIcon(
                    atlas_icon(QtCore, QtGui, "file", color=color, size=16)
                )
                view["button"].setIconSize(QtCore.QSize(16, 16))
            preview_icon = self._preview_tab_widget.findChild(QtWidgets.QLabel, "previewDocumentIcon")
            preview_icon.setPixmap(atlas_icon(QtCore, QtGui, "file", color=color, size=20).pixmap(20, 20))
            self.source_empty_icon.setPixmap(
                atlas_icon(QtCore, QtGui, "search-group", color=color, size=44)
                .pixmap(44, 44)
            )
            tab_bar = self.tabs.tabBar()
            self._tab_scroll_buttons = {}
            for button in tab_bar.findChildren(QtWidgets.QToolButton):
                direction = button.property("guidedFetchScrollDirection") or {
                    QtCore.Qt.ArrowType.LeftArrow: "left",
                    QtCore.Qt.ArrowType.RightArrow: "right",
                }.get(button.arrowType())
                if direction is None:
                    continue
                button.setProperty("guidedFetchScrollDirection", direction)
                button.setArrowType(QtCore.Qt.ArrowType.NoArrow)
                button.setObjectName(f"guidedFetchTabScroll{direction.title()}")
                button.setMinimumSize(0, 0)
                button.setIcon(atlas_icon(
                    QtCore, QtGui, f"arrow-{direction}", color=color, size=20
                ))
                button.setIconSize(QtCore.QSize(18, 18))
                self._tab_scroll_buttons[direction] = button
            if not getattr(self, "_tab_scroll_controls_connected", False):
                self.tabs.currentChanged.connect(
                    lambda *_args: QtCore.QTimer.singleShot(
                        0, self.tabs, self._update_tab_scroll_controls
                    )
                )
                for button in self._tab_scroll_buttons.values():
                    button.clicked.connect(
                        lambda *_args: QtCore.QTimer.singleShot(
                            0, self.tabs, self._update_tab_scroll_controls
                        )
                    )
                self._tab_scroll_controls_connected = True
            self._update_tab_scroll_controls()
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 1)
                if item is not None:
                    item.setIcon(atlas_icon(QtCore, QtGui, "file", color=color, size=18))
            for index in range(self.tabs.count()):
                panel = self.tabs.widget(index)
                for button in panel.findChildren(QtWidgets.QPushButton, "instructionsButton"):
                    button.setIcon(
                        atlas_icon(QtCore, QtGui, "help", color=color, size=16)
                    )
                    button.setIconSize(QtCore.QSize(16, 16))

        def _update_tab_scroll_controls(self):
            tab_bar = self.tabs.tabBar()
            if tab_bar.width() <= 0 or not self._tab_scroll_buttons:
                return
            content_width = sum(
                tab_bar.tabRect(index).width()
                for index in range(tab_bar.count())
            )
            overflow = content_width > tab_bar.width()
            for button in self._tab_scroll_buttons.values():
                visible = overflow and button.isEnabled()
                if visible:
                    button.setMinimumSize(24, 24)
                    button.setMaximumSize(24, 24)
                else:
                    button.setMinimumSize(0, 0)
                    button.setMaximumSize(16777215, 16777215)
                button.setVisible(visible)
            QtCore.QTimer.singleShot(0, self.tabs, self._position_tab_scroll_controls)

        def _position_tab_scroll_controls(self):
            tab_bar = self.tabs.tabBar()
            for direction, button in self._tab_scroll_buttons.items():
                if not button.isVisible():
                    continue
                width = min(24, tab_bar.width())
                height = min(24, tab_bar.height())
                x = 0 if direction == "left" else tab_bar.width() - width
                y = max(0, (tab_bar.height() - height) // 2)
                button.setGeometry(x, y, width, height)

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
                    availability.setData(badge_dark_role, self._dark_theme)
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
                if review is not None and (row.risk_label or row.review_label):
                    tone = "red" if row.risk_label else "amber"
                    review.setForeground(QtGui.QColor(colors[tone]))
                    review.setData(badge_tone_role, tone)
                    review.setData(badge_dark_role, self._dark_theme)

        def eventFilter(self, watched, event):  # noqa: N802 - Qt API name
            if (
                hasattr(self, "source_filter_header")
                and watched is self.source_filter_header
                and event.type() == QtCore.QEvent.Type.Resize
            ):
                QtCore.QTimer.singleShot(0, self._update_source_filter_header_layout)
            if (
                hasattr(self, "detail_scroll")
                and watched is self.detail_scroll.viewport()
                and event.type() == QtCore.QEvent.Type.Resize
            ):
                QtCore.QTimer.singleShot(0, self._update_action_layouts)
            is_drop_target = watched in (
                getattr(self, "file_label", None),
                getattr(self, "file_drop_zone", None),
            )
            if (
                is_drop_target
                and (self._ocr_thread is not None or self._proceed_preparing)
                and event.type() in (
                    QtCore.QEvent.Type.DragEnter,
                    QtCore.QEvent.Type.Drop,
                )
            ):
                event.ignore()
                return True
            if is_drop_target:
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
            if is_drop_target and event.type() == QtCore.QEvent.Type.DragEnter:
                urls = event.mimeData().urls()
                if len(urls) == 1 and urls[0].isLocalFile():
                    event.acceptProposedAction()
                    return True
            if is_drop_target and event.type() == QtCore.QEvent.Type.Drop:
                urls = event.mimeData().urls()
                if len(urls) == 1 and urls[0].isLocalFile():
                    self._set_file(urls[0].toLocalFile())
                    event.acceptProposedAction()
                    return True
            return super().eventFilter(watched, event)

        def _render_rows(self, select_ref_id: str | None = None):
            rows = self._model.rows()
            self._update_guided_filter_counts(rows)
            self.source_count_label.setText(
                labels["guided_fetch_source_count"].format(count=len(rows))
            )
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
                browser_preflight = self._browser_preflight_job_for_ref(row.ref_id)
                source_mutation = self._source_mutation_for_ref(row.ref_id)
                browser_preflight_label = ""
                if browser_preflight is not None:
                    active = self._preflight_active_request
                    browser_preflight_label = (
                        "Checking PDF readability…"
                        if active is not None
                        and active.get("request_id") == browser_preflight.get("request_id")
                        else "PDF check queued"
                    )
                if source_mutation is not None:
                    browser_preflight_label = (
                        "Saving source…"
                        if source_mutation.get("action") == "stage"
                        and source_mutation is self._source_mutation_active_request
                        else "Source save queued"
                        if source_mutation.get("action") == "stage"
                        else "Discarding source…"
                        if source_mutation is self._source_mutation_active_request
                        else "Source discard queued"
                    )
                status_text = (
                    labels.get(row.status, row.status_label)
                    if row.status in {"fulltext", "abstract"}
                    else row.status_label.replace(" · ", "\n", 1)
                )
                if row.status == "none" and row.status_color == "blue":
                    status_text = labels["guided_fetch_status_known_no_text"]
                elif row.status == "none" and row.status_color == "neutral":
                    status_text = labels["guided_fetch_status_identity_uncertain"]
                if browser_preflight_label:
                    status_text += f"\n{browser_preflight_label}"
                visible_warning = "; ".join(
                    warning for warning in (row.risk_label, row.review_label) if warning
                )
                values = [
                    (
                        f"[{row.ref_number}]\n{labels['guided_fetch_queued']}"
                        if row.ref_id in self._queued_sources and row.ref_number is not None
                        else (f"[{row.ref_number}]" if row.ref_number is not None else row.ref_id)
                    ),
                    (row.title or "(title unavailable)").replace(" · ", "\n", 1),
                    status_text,
                    ocr_label,
                    f"⚠ {row.risk_label}" if row.risk_label else "",
                    f"⚠ {visible_warning}" if visible_warning else "",
                ]
                for column, value in enumerate(values):
                    item = QtWidgets.QTableWidgetItem(value)
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, row.ref_id)
                    item.setTextAlignment(
                        QtCore.Qt.AlignmentFlag.AlignCenter
                        if column == 0
                        else QtCore.Qt.AlignmentFlag.AlignLeft
                        | QtCore.Qt.AlignmentFlag.AlignVCenter
                    )
                    if column == 0 and row.ref_id in self._queued_sources:
                        compact_font = item.font()
                        compact_font.setPixelSize(10)
                        item.setFont(compact_font)
                    if column == 0 and row.ref_number is None:
                        item.setToolTip(row.ref_id)
                    item.setData(
                        int(QtCore.Qt.ItemDataRole.UserRole) + 1, item.background()
                    )
                    if pending_identity:
                        item.setBackground(QtGui.QColor(warning_background))
                        if column < 2:
                            item.setForeground(QtGui.QColor(warning_text))
                    if column == 1:
                        from .atlas_icons import icon as atlas_icon
                        item.setIcon(atlas_icon(
                            QtCore, QtGui, "file",
                            color="#7ab4ff" if self._dark_theme else "#175fe6", size=18,
                        ))
                        title_tooltip = row.title_tooltip
                        if row.risk_tooltip:
                            title_tooltip = (
                                f"{title_tooltip}\n{labels['risk_signal']}: {row.risk_tooltip}"
                            )
                        item.setToolTip(title_tooltip)
                    if column == 2:
                        item.setForeground(QtGui.QColor(colors[row.status_color]))
                        item.setData(badge_tone_role, row.status_color)
                        item.setData(badge_dark_role, self._dark_theme)
                        item.setToolTip(
                            f"{row.status_tooltip}\n{browser_preflight_label}"
                            if browser_preflight_label else row.status_tooltip
                        )
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
                    if column == 5 and (row.risk_label or row.review_label):
                        tone = "red" if row.risk_label else "amber"
                        item.setForeground(QtGui.QColor(colors[tone]))
                        item.setData(badge_tone_role, tone)
                        item.setData(badge_dark_role, self._dark_theme)
                        item.setToolTip("\n".join(filter(
                            None, (row.risk_tooltip, row.review_tooltip)
                        )))
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
                self.table.setRowHidden(index, not self._source_matches_filter(row))
            self.table.resizeRowsToContents()
            visible_rows = [
                (index, row) for index, row in enumerate(rows)
                if not self.table.isRowHidden(index)
            ]
            self.source_table_stack.setCurrentWidget(
                self.source_empty_state
                if rows and not visible_rows else self.table
            )
            if visible_rows:
                target_ref_id = select_ref_id or self._current_ref_id
                target_index = next(
                    (index for index, row in visible_rows if row.ref_id == target_ref_id),
                    visible_rows[0][0],
                )
                self.table.selectRow(target_index)
            else:
                self.table.clearSelection()
            self._update_ocr_button()
            self._sync_source_row_pulse_timer()
            self._update_source_session_summary()
            self._update_next_unresolved_button()

        def _update_source_session_summary(self):
            summary = []
            pending_pdf_count = len(self._pending_source_pdf_refs())
            identity_count = len(self._pending_identity_reviews())
            ocr_count = len(self._ocr_jobs())
            if pending_pdf_count:
                summary.append(labels["guided_fetch_summary_pdf_pending"].format(
                    count=pending_pdf_count
                ))
            if identity_count:
                summary.append(labels["guided_fetch_summary_identity_pending"].format(
                    count=identity_count
                ))
            if ocr_count:
                summary.append(labels["guided_fetch_summary_ocr_pending"].format(
                    count=ocr_count
                ))
            self.source_session_summary.setText("  ·  ".join(summary))
            self.source_session_summary.setVisible(bool(summary))

        def _pending_source_pdf_refs(self) -> set[str]:
            pending: set[str] = set()
            capture_key = self._browser_capture_pdf_key
            if capture_key is not None and capture_key == self._browser_capture_active_key:
                pending.add(capture_key[0])
            pending.update(
                request.get("ref_id")
                for request in self._browser_pdf_preflight_jobs.values()
                if isinstance(request.get("ref_id"), str)
            )
            for request in (
                ([self._source_mutation_active_request]
                 if self._source_mutation_active_request is not None else [])
                + self._source_mutation_queue
            ):
                path = request.get("path")
                ref_id = request.get("ref_id")
                if (
                    request.get("action") == "stage"
                    and isinstance(path, str)
                    and path.lower().endswith(".pdf")
                    and isinstance(ref_id, str)
                ):
                    pending.add(ref_id)
            if self._selected_file_preflight_pending:
                requests = [self._preflight_active_request, *self._preflight_pending.values()]
                for request in requests:
                    if (
                        request is not None
                        and request.get("kind") == "selection"
                        and str(request.get("path", "")).lower().endswith(".pdf")
                        and isinstance(request.get("ref_id"), str)
                    ):
                        pending.add(request["ref_id"])
            return pending

        def _sync_source_row_pulse_timer(self):
            if self._pending_source_pdf_refs():
                if not self._source_pulse_timer.isActive():
                    self._source_pulse_phase = 0
                    self._source_pulse_timer.start()
                self._advance_source_row_pulse()
            else:
                if self._source_pulse_timer.isActive():
                    self._source_pulse_timer.stop()
                self._restore_source_row_backgrounds()

        def _advance_source_row_pulse(self):
            pending = self._pending_source_pdf_refs()
            if not pending:
                self._sync_source_row_pulse_timer()
                return
            self._source_pulse_phase = (self._source_pulse_phase + 1) % 20
            # A slow, low-contrast triangular wave keeps the cue visible without
            # competing with selection and status text.
            step = self._source_pulse_phase if self._source_pulse_phase <= 10 else 20 - self._source_pulse_phase
            strength = step / 10
            accent = QtGui.QColor("#4b8bd8" if self._dark_theme else "#9fc5f3")
            base_colors = (
                ("#101722", "#1f2b3d") if self._dark_theme
                else ("#ffffff", "#eaf2ff")
            )
            role = int(QtCore.Qt.ItemDataRole.UserRole) + 1
            for index, row in enumerate(self._model.rows()):
                if row.ref_id not in pending or self._model.identity_review(row.ref_id) is not None:
                    for column in range(self.table.columnCount()):
                        item = self.table.item(index, column)
                        if item is not None and item.data(role + 1) is not None:
                            item.setBackground(item.data(role) or QtGui.QBrush())
                            item.setData(role + 1, None)
                    continue
                base = QtGui.QColor(base_colors[index % 2])
                for column in range(self.table.columnCount()):
                    item = self.table.item(index, column)
                    if item is None:
                        continue
                    overlay = QtGui.QColor(accent)
                    overlay.setAlpha(round(24 * strength))
                    item.setData(role + 1, overlay)
                    if step == 0:
                        item.setBackground(item.data(role) or QtGui.QBrush())
                        continue
                    background = QtGui.QColor(base)
                    tint = (.10 if self._dark_theme else .20) * strength
                    background.setRed(round(base.red() * (1 - tint) + accent.red() * tint))
                    background.setGreen(round(base.green() * (1 - tint) + accent.green() * tint))
                    background.setBlue(round(base.blue() * (1 - tint) + accent.blue() * tint))
                    item.setBackground(background)

        def _restore_source_row_backgrounds(self):
            role = int(QtCore.Qt.ItemDataRole.UserRole) + 1
            for index, row in enumerate(self._model.rows()):
                if self._model.identity_review(row.ref_id) is not None:
                    continue
                for column in range(self.table.columnCount()):
                    item = self.table.item(index, column)
                    if item is not None:
                        item.setBackground(item.data(role) or QtGui.QBrush())
                        item.setData(role + 1, None)

        def _update_source_filter_header_layout(self):
            layout = getattr(self, "source_filter_header_layout", None)
            header = getattr(self, "source_filter_header", None)
            if layout is None or header is None:
                return
            margins = layout.contentsMargins()
            available = max(0, header.width() - margins.left() - margins.right())
            required = (
                self._source_filter_identity_layout.sizeHint().width()
                + self.source_filter.minimumSizeHint().width()
                + max(0, layout.spacing())
            )
            direction = (
                QtWidgets.QBoxLayout.Direction.TopToBottom
                if available < required
                else QtWidgets.QBoxLayout.Direction.LeftToRight
            )
            if layout.direction() != direction:
                layout.setDirection(direction)
                header.updateGeometry()

        def _update_guided_filter_counts(self, rows=None):
            rows = self._model.rows() if rows is None else rows
            for key, checkbox in self.source_filter_checks.items():
                count = sum(
                    self._source_matches_filter_key(row, key) for row in rows
                )
                set_source_filter_count(checkbox, count)
            refresh_checkbox_source_menu(self.source_filter_menu)

        def _source_filter_toggled(self, key: str, checked: bool):
            if checked:
                self._guided_filter_keys.add(key)
            else:
                self._guided_filter_keys.discard(key)
            self._filter_sources()

        def _clear_source_filter_selection(self, _checked=False):
            for checkbox in self.source_filter_checks.values():
                was_blocked = checkbox.blockSignals(True)
                checkbox.setChecked(False)
                checkbox.blockSignals(was_blocked)
            self._guided_filter_keys.clear()
            self._filter_sources()

        def _source_matches_filter_key(self, row, key: str) -> bool:
            if key == "fulltext":
                return row.status == "fulltext"
            if key == "abstract":
                return row.status == "abstract"
            if key == "none":
                return row.status == "none"
            if key == "identity_required":
                return (
                    self._model.identity_review(row.ref_id) is not None
                    and row.ref_id not in self._recorded_identity_refs
                )
            return False

        def _source_matches_filter(self, row) -> bool:
            return not self._guided_filter_keys or any(
                self._source_matches_filter_key(row, key)
                for key in self._guided_filter_keys
            )

        def _has_available_fulltext(self, ref_id: str) -> bool:
            inventory = self._model.inventory
            references = inventory.get("references") if isinstance(inventory, dict) else None
            if not isinstance(references, list):
                return False
            reference = next((
                entry for entry in references
                if isinstance(entry, dict) and entry.get("ref_id") == ref_id
            ), None)
            fetch = reference.get("fetch") if isinstance(reference, dict) else None
            sources = fetch.get("sources") if isinstance(fetch, dict) else None
            return isinstance(sources, list) and any(
                isinstance(source, dict)
                and source.get("available")
                and source.get("tier") == "fulltext"
                for source in sources
            )

        def _next_visible_unresolved_row(self) -> int | None:
            selected_rows = self.table.selectionModel().selectedRows()
            if not selected_rows:
                return None
            current_row = min(index.row() for index in selected_rows)
            for row in range(current_row + 1, self.table.rowCount()):
                if self.table.isRowHidden(row):
                    continue
                item = self.table.item(row, 0)
                ref_id = (
                    item.data(QtCore.Qt.ItemDataRole.UserRole)
                    if item is not None else None
                )
                if isinstance(ref_id, str) and (
                    not self._has_available_fulltext(ref_id)
                    or (
                        ref_id not in self._recorded_identity_refs
                        and self._model.identity_review(ref_id) is not None
                    )
                ):
                    return row
            return None

        def _update_next_unresolved_button(self):
            button = getattr(self, "compact_next_button", None)
            if button is not None:
                button.setEnabled(self._next_visible_unresolved_row() is not None)

        def _next_unresolved(self):
            row = self._next_visible_unresolved_row()
            if row is not None:
                self.table.selectRow(row)
            else:
                self._update_next_unresolved_button()

        def _filter_sources(self):
            self._render_rows(select_ref_id=self._current_ref_id)
            self._selected()

        def _selected(self, preserve_tab: bool = False):
            selected = self.table.selectedItems()
            self._update_next_unresolved_button()
            if not selected:
                if self._current_ref_id is not None:
                    self._selection_context_generation += 1
                self._current_ref_id = None
                self._selected_preview_path = None
                self._clear_preview()
                self._clear_parsed_text_viewer()
                self._set_identity_selection_style(None)
                self.selected_source_heading.set_full_text("")
                self.selection_guidance.setText(
                    labels["guided_fetch_no_sources_match"]
                    if self._model.rows()
                    else labels["guided_fetch_select_reference"]
                )
                for view in self._detail_views.values():
                    view["headline"].clear()
                    view["table"].setRowCount(0)
                    view["raw"] = "{}"
                    if view.get("preview_title") is not None:
                        view["preview_title"].set_full_text("")
                        view["preview_tier_button"].setText(
                            labels["guided_fetch_tab_preview"]
                        )
                        view["preview_tier_button"].setEnabled(False)
                self._set_staged_preview_height(False)
                self.open_link.setEnabled(False)
                self.open_chrome.setEnabled(False)
                self.tier_switch.setEnabled(False)
                self.fulltext_label.setEnabled(False)
                self.abstract_label.setEnabled(False)
                self.choose_file.setEnabled(False)
                self.file_label.setEnabled(False)
                self.file_drop_zone.setEnabled(False)
                self.submit.setEnabled(False)
                self.submit.setText(labels["guided_fetch_confirm_source"])
                self.file_label.setText(labels["guided_fetch_drop_file"])
                self.file_label.setToolTip("")
                self.discard_source_button.setEnabled(False)
                self.identity_review_reason.clear()
                self.identity_review_reason.setEnabled(False)
                self.attest_identity_button.setEnabled(False)
                self.keep_unverified_button.setEnabled(False)
                self.identity_review_panel.setVisible(False)
                self.tabs.setTabEnabled(self._identity_review_tab, False)
                self.tabs.setTabIcon(self._identity_review_tab, QtGui.QIcon())
                self.tabs.tabBar().setTabTextColor(self._identity_review_tab, QtGui.QColor())
                self._show_queued_status("")
                self._update_proceed_button()
                if preserve_tab:
                    return
                return
            ref_id = selected[0].data(QtCore.Qt.ItemDataRole.UserRole)
            if not isinstance(ref_id, str):
                return
            if self.table.isRowHidden(selected[0].row()):
                first_visible = next(
                    (index for index in range(self.table.rowCount())
                     if not self.table.isRowHidden(index)),
                    None,
                )
                if first_visible is None:
                    self.table.clearSelection()
                else:
                    self.table.selectRow(first_visible)
                return
            selected_tab = self.tabs.currentIndex() if preserve_tab else None
            if ref_id != self._current_ref_id:
                self._selection_context_generation += 1
                if self._is_pdf_path(self._selected_file) and (
                    self._selected_file_ref_id == ref_id
                    and (
                        self._selected_file_ref_id,
                        self._selected_file_generation,
                    ) not in self._consumed_file_selections
                ):
                    self._selected_file_preflight_pending = True
                    self._selected_file_preflight_tier = None
            self._set_identity_selection_style(ref_id)
            selected_row = self._model.row(ref_id)
            number = (
                f" [{selected_row.ref_number}]"
                if selected_row.ref_number is not None else ""
            )
            source_heading = f"{labels['source']}{number} · {selected_row.title}"
            self.selected_source_heading.set_full_text(source_heading)
            detail = self._model.detail_panels(ref_id, labels=labels)
            preview_raw = detail.get("Preview", {}).get("raw", {})
            preview_path = preview_raw.get("path")
            preview_kind = preview_raw.get("kind")
            staged_source = self._staged_source_records.get(ref_id)
            if staged_source is not None and not isinstance(preview_path, str):
                preview_path = staged_source["file_path"].strip()
                preview_kind = "guided_source_staged"
                preview_raw = {
                    "kind": preview_kind,
                    "path": preview_path,
                    "display_name": staged_source["display_name"].strip(),
                    "source_tier": staged_source.get("source_tier"),
                    "sha256": staged_source.get("sha256"),
                    "byte_count": staged_source.get("byte_count"),
                }
                preview_detail = dict(detail.get("Preview", {}))
                preview_detail["raw"] = preview_raw
                preview_detail["rows"] = [
                    ("File name", preview_raw["display_name"]),
                    ("Source tier", preview_raw["source_tier"] or "Unknown"),
                    ("SHA-256", preview_raw["sha256"] or "Unavailable"),
                    (
                        "Bytes",
                        preview_raw["byte_count"]
                        if preview_raw["byte_count"] is not None else "Unknown",
                    ),
                ]
                detail = {**detail, "Preview": preview_detail}
            if not isinstance(preview_path, str):
                preview_path = None
            if ref_id != self._current_ref_id or preview_path != self._selected_preview_path:
                self._clear_preview()
                self._clear_parsed_text_viewer()
            self._current_ref_id = ref_id
            self._selected_preview_path = preview_path
            for name, payload in detail.items():
                view = self._detail_views[name]
                if name == "Preview":
                    pending_preview_copy = {
                        "guided_ocr_staged": labels[
                            "guided_fetch_pending_ocr_admission"
                        ],
                        "guided_source_staged": labels[
                            "guided_fetch_pending_source_admission"
                        ],
                    }.get(preview_kind)
                    view["headline"].setText(pending_preview_copy or "")
                    view["headline"].setVisible(bool(pending_preview_copy))
                    self._set_staged_preview_height(bool(pending_preview_copy))
                else:
                    view["headline"].setText(payload["headline"])
                if name == "Preview":
                    preview_tier = preview_raw.get("source_tier")
                    if preview_kind == "guided_ocr_staged":
                        preview_tier = "fulltext"
                    elif preview_tier not in {"fulltext", "abstract"}:
                        preview_tier = selected_row.status
                    preview_heading = view["preview_heading"]
                    preview_heading.setText(
                        labels[preview_tier]
                        if preview_tier in {"fulltext", "abstract"}
                        else labels["guided_fetch_tab_preview"]
                    )
                    preview_heading.setEnabled(True)
                    view["preview_title"].set_full_text(selected_row.title or "")
                    preview_heading.setAccessibleName(
                        f"Open {preview_heading.text()} parsed text for {selected_row.title}"
                    )
                    preview_heading.setToolTip(
                        f"Open the complete parsed text for {selected_row.title}"
                    )
                    view["headline"].setToolTip(pending_preview_copy or "")
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
                table.resizeRowsToContents()
            if (
                self._compact_drawer_mode
                and self.tabs.currentWidget() is not self._preview_tab_widget
            ):
                self._apply_detail_tab_height()
            if self.tabs.currentWidget() is self._preview_tab_widget:
                self._request_preview()
            self.open_link.setEnabled(True)
            source_mutation = self._source_mutation_for_ref(ref_id)
            self.open_chrome.setEnabled(
                self._ocr_thread is None and source_mutation is None
                and not self._proceed_preparing
            )
            can_submit = self._model.can_submit_source(ref_id)
            controls_enabled = (
                can_submit and self._ocr_thread is None
                and not self._identity_operation_busy()
                and not self._proceed_preparing
                and source_mutation is None
            )
            guidance = self._model.action_guidance(ref_id, labels=labels)
            rejection = self._model.rejected_source_guidance(ref_id)
            guidance_parts = [rejection or guidance]
            compatibility_notice = self._model.browser_compatibility_notice(
                ref_id, labels=labels
            )
            if compatibility_notice:
                guidance_parts.append(compatibility_notice)
            self.selection_guidance.setText("\n\n".join(guidance_parts))
            self.tier_switch.setEnabled(controls_enabled)
            self.fulltext_label.setEnabled(controls_enabled)
            self.abstract_label.setEnabled(controls_enabled)
            self.choose_file.setEnabled(controls_enabled)
            self.file_label.setEnabled(controls_enabled)
            self.file_drop_zone.setEnabled(controls_enabled)
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
            selection_is_pdf = selection_matches and self._is_pdf_path(self._selected_file)
            selection_preflight_ready = (
                not selection_is_pdf
                or selection_consumed
                or (
                    not self._selected_file_preflight_pending
                    and self._selected_file_preflight_tier == self._selected_tier()
                )
            )
            self.submit.setEnabled(
                controls_enabled
                and not self._preflight_busy()
                and self._submit_preflight_request_id is None
                and (
                    self._selected_file is None
                    or (
                        selection_matches
                        and not selection_consumed
                        and selection_preflight_ready
                    )
                )
            )
            if selection_matches:
                if selection_is_pdf and not selection_preflight_ready:
                    self.file_label.setText("Checking PDF readability…")
                else:
                    self.file_label.setText(
                        f"{Path(self._selected_file).name} (OCR needed)"
                        if self._selected_file_ocr_pending and not selection_consumed
                        else Path(self._selected_file).name
                    )
            elif self._selected_file is not None:
                self.file_label.setText(labels["guided_fetch_drop_file"])
            if selection_is_pdf and not selection_preflight_ready:
                self.submit.setText("Checking PDF…")
            elif selection_consumed:
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
                self.submit.setText(labels["guided_fetch_confirm_source"])
            self._show_identity_review(ref_id)
            self._update_proceed_button()
            browser_preflight = self._browser_preflight_job_for_ref(ref_id)
            self.discard_source_button.setEnabled(
                can_submit
                and (
                    browser_preflight is not None
                    or (self._discard_source is not None and ref_id in self._queued_sources)
                    or (
                        source_mutation is not None
                        and source_mutation.get("action") == "stage"
                        and source_mutation is not self._source_mutation_active_request
                    )
                )
                and not self._identity_operation_busy()
                and not self._proceed_preparing
                and not (
                    source_mutation is not None
                    and source_mutation is self._source_mutation_active_request
                )
            )
            self._show_queued_status(ref_id)
            self._queue_selected_file_preflight()
            QtCore.QTimer.singleShot(0, self._update_action_layouts)
            if selected_tab is not None:
                restore_tab = selected_tab
                if not self.tabs.isTabEnabled(restore_tab):
                    restore_tab = 0
                self.tabs.setCurrentIndex(restore_tab)

        def _detail_tab_changed(self, _index: int):
            self._apply_detail_tab_height()
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
            self._connect_gui_signal(worker.loaded, self._preview_worker_loaded)
            worker.loaded.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            self._connect_gui_signal(thread.finished, self._preview_worker_finished)
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
            self._join_worker_thread(self._preview_thread)
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
            stylesheet = ""
            if ref_id is not None and self._model.identity_review(ref_id) is not None:
                background, foreground, _ = _identity_review_colors(dark=self._dark_theme)
                stylesheet = (
                    f"QTableWidget {{ selection-background-color: {background}; "
                    f"selection-color: {foreground}; }}"
                )
            if self.table.styleSheet() != stylesheet:
                self.table.setStyleSheet(stylesheet)

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
            citation_kind = review.get("citation_label_kind")
            citation_key = (
                "guided_fetch_identity_cited_title"
                if citation_kind == "Cited title"
                else "guided_fetch_identity_citation"
            )
            source_tier = review.get("source_tier")
            source_tier_label = (
                labels[source_tier]
                if source_tier in {"fulltext", "abstract"}
                else labels["guided_fetch_identity_unavailable"]
            )
            facts = [
                labels["guided_fetch_identity_required"].format(
                    number=self._model.row(ref_id).ref_number
                ),
                f"{labels[citation_key]}: {review.get('citation_label') or labels['guided_fetch_identity_unavailable']}.",
                f"{labels['guided_fetch_identity_resolved_title']}: {review.get('resolved_title') or labels['guided_fetch_identity_unavailable']}.",
                f"{labels['guided_fetch_identity_resolved_identifier']}: {review.get('resolved_identifier') or labels['guided_fetch_identity_unavailable']}.",
                f"{labels['guided_fetch_identity_source_tier']}: {source_tier_label}.",
                f"{labels['guided_fetch_identity_target_sha256']}: {review['target_sha256']}.",
            ]
            source_identity = review.get("source_identity")
            if isinstance(source_identity, dict):
                source_facts = ", ".join(
                    f"{key}: {value}"
                    for key, value in source_identity.items()
                    if value is not None
                )
                if source_facts:
                    facts.append(
                        f"{labels['guided_fetch_identity_source_identity']}: {source_facts}."
                    )
            if not self._identity_review_allowed:
                facts.append(labels["guided_fetch_identity_operator_required"])
            if ref_id in self._recorded_identity_refs:
                facts.append(labels["guided_fetch_identity_refresh_failed"])
            display_facts = "\n".join(
                re.sub(
                    r"\S{33,}",
                    lambda match: "\u200b".join(
                        match.group(0)[index:index + 28]
                        for index in range(0, len(match.group(0)), 28)
                    ),
                    fact,
                )
                for fact in facts
            )
            self.identity_review_facts.setText(display_facts)
            self.identity_review_facts.setToolTip("\n".join(facts))
            enabled = (
                self._identity_review_allowed
                and self._submit_identity_review is not None
                and ref_id not in self._recorded_identity_refs
                and self._identity_thread is None
                and not self._source_stage_busy()
                and not self._proceed_preparing
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
            refresh_failed = any(
                ref_id in self._identity_refresh_failed_refs
                for ref_id, _number, _review in pending
            )
            has_unrecorded = any(
                ref_id not in self._recorded_identity_refs
                for ref_id, _number, _review in pending
            )
            can_resolve = (
                self._identity_review_allowed
                and self._submit_identity_review is not None
                and not refresh_failed
                and has_unrecorded
            )
            self.proceed_button.setEnabled(
                not self._proceed_preparing
                and not self._capture_tiers and self._ocr_thread is None
                and not self._preflight_busy()
                and self._identity_thread is None
                and not self._source_stage_busy()
                and (not pending or can_resolve)
            )
            ocr_count = len(self._ocr_jobs())
            if ocr_count:
                self.proceed_button.setText(labels["ocr_skip_button"])
            else:
                self.proceed_button.setText(labels["guided_fetch_continue"])
            blocked_explanation = ""
            if not pending:
                self.proceed_button.setToolTip(labels["guided_fetch_continue_tooltip"])
            elif refresh_failed or not has_unrecorded:
                blocked_explanation = labels["guided_fetch_identity_refresh_failed"]
                self.proceed_button.setToolTip(blocked_explanation)
            elif not self._identity_review_allowed:
                blocked_explanation = labels["guided_fetch_identity_agent_blocked"]
                self.proceed_button.setToolTip(blocked_explanation)
            elif self._submit_identity_review is None:
                blocked_explanation = labels["guided_fetch_identity_submission_unavailable"]
                self.proceed_button.setToolTip(blocked_explanation)
            else:
                numbers = ", ".join(
                    f"[{number}]" if number is not None else f"[{ref_id}]"
                    for ref_id, number, _review in pending
                )
                blocked_explanation = labels[
                    "guided_fetch_identity_pending_guidance"
                ].format(numbers=numbers)
                self.proceed_button.setToolTip(
                    labels["guided_fetch_identity_pending_tooltip"].format(
                        numbers=numbers
                    )
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
            was_visible = not self.ocr_button.isHidden()
            previous_text = self.ocr_button.text()
            count = len(self._ocr_jobs())
            active = self._ocr_thread is not None
            self.ocr_button.setVisible(bool(count or active))
            self.ocr_button.setEnabled(bool(
                count and self._run_ocr is not None and not active
                and not self._capture_tiers and not self._preflight_busy()
                and not self._identity_operation_busy()
                and not self._source_stage_busy()
                and not self._proceed_preparing
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
            if getattr(self, "_atlas_embedded", False) and (
                was_visible != (not self.ocr_button.isHidden())
                or previous_text != self.ocr_button.text()
            ):
                QtCore.QTimer.singleShot(0, self._update_action_layouts)

        def _start_ocr(self, jobs: list[dict[str, Any]] | None = None):
            if (
                self._ocr_thread is not None
                or self._capture_tiers
                or self._preflight_busy()
                or self._identity_operation_busy()
                or self._source_stage_busy()
                or self._proceed_preparing
                or self._run_ocr is None
            ):
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
            self._connect_gui_signal(worker.progress, self._ocr_progress)
            self._connect_gui_signal(worker.finished, self._ocr_finished)
            self._connect_gui_signal(worker.failed, self._ocr_failed)
            worker.finished.connect(thread.quit)
            worker.failed.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            self._connect_gui_signal(thread.finished, self._ocr_thread_finished)
            thread.start()
            self._render_rows(select_ref_id=self._current_ref_id)
            self._selected()
            self._update_proceed_button()

        def _queue_auto_ocr(self, jobs: list[dict[str, Any]]):
            for job in jobs:
                key = (job.get("ref_id"), job.get("scan_id"))
                if not any(
                    (pending.get("ref_id"), pending.get("scan_id")) == key
                    for pending in self._pending_auto_ocr_jobs
                ):
                    self._pending_auto_ocr_jobs.append(dict(job))
            self._start_pending_auto_ocr()

        def _start_pending_auto_ocr(self):
            if (
                not self._pending_auto_ocr_jobs
                or self._ocr_thread is not None
                or self._capture_tiers
                or self._preflight_busy()
                or self._identity_operation_busy()
                or self._source_stage_busy()
                or self._proceed_preparing
                or self._run_ocr is None
            ):
                return
            pending_jobs = self._pending_auto_ocr_jobs
            self._pending_auto_ocr_jobs = []
            jobs = [
                job for job in pending_jobs
                if job.get("scan_id") is None
                and job.get("ref_id") in self._staged_ocr_refs
                and self._ocr_row_status.get(job.get("ref_id"), {}).get("status") != "done"
            ]
            if not jobs:
                return
            self._start_ocr(jobs)

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
            self._join_worker_thread(self._ocr_thread)
            self._ocr_thread = None
            self._ocr_worker = None
            self._ocr_active_jobs = []
            self._update_ocr_button()
            self._selected()
            self._update_proceed_button()
            self._start_pending_auto_ocr()

        def _submit_identity(self, action: str):
            if self._identity_thread is not None or self._proceed_preparing:
                return
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
            self._start_identity_worker([{
                "ref_id": ref_id,
                "action": action,
                "target_sha256": review["target_sha256"],
                "reason": reason,
            }], mode="single")

        def _identity_operation_busy(self) -> bool:
            return self._identity_thread is not None

        def _start_identity_worker(
            self,
            decisions: list[dict[str, Any]],
            *,
            mode: str,
            skip_remaining_confirmed: bool = False,
        ) -> bool:
            if (
                self._identity_thread is not None
                or self._ocr_thread is not None
                or self._capture_tiers
                or self._preflight_busy()
                or self._source_stage_busy()
                or self._proceed_preparing
                or not decisions
            ):
                return False
            thread = QtCore.QThread(self)
            worker = IdentityReviewWorker(
                self._run_dir,
                self._submit_identity_review,
                self._inventory_loader,
                [dict(decision) for decision in decisions],
            )
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            self._connect_gui_signal(worker.finished, self._identity_worker_finished)
            worker.finished.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            self._connect_gui_signal(thread.finished, self._identity_thread_finished)
            self._identity_thread = thread
            self._identity_worker = worker
            self._identity_mode = mode
            self._identity_proceed_after_finish = False
            self._identity_proceed_skip_remaining_confirmed = (
                mode == "proceed" and skip_remaining_confirmed
            )
            self._selected(preserve_tab=True)
            self._update_proceed_button()
            self._update_ocr_button()
            self.statusBar().showMessage(
                "Saving the identity decision and refreshing sources. You can keep navigating.",
                0,
            )
            thread.start()
            return True

        def _refresh_inventory_preserving_navigation(self, inventory):
            self._model = GuidedFetchViewModel(inventory)
            self._hydrate_staged_sources(inventory)
            self._rerender_rows_preserving_navigation()

        def _hydrate_staged_sources(self, inventory):
            if not isinstance(inventory, dict):
                return
            staged_sources = inventory.get("staged_sources")
            if not isinstance(staged_sources, list):
                return
            known_ref_ids = {row.ref_id for row in self._model.rows()}
            staged_by_ref: dict[str, dict[str, Any]] = {}
            duplicate_refs: set[str] = set()
            for source in staged_sources:
                if not isinstance(source, dict):
                    continue
                ref_id = source.get("ref_id")
                display_name = source.get("display_name")
                file_path = source.get("file_path")
                if (
                    not isinstance(ref_id, str)
                    or ref_id not in known_ref_ids
                    or not isinstance(display_name, str)
                    or not display_name.strip()
                    or not isinstance(file_path, str)
                    or not file_path.strip()
                ):
                    continue
                if ref_id in staged_by_ref:
                    duplicate_refs.add(ref_id)
                    continue
                staged_by_ref[ref_id] = dict(source)
            for ref_id in duplicate_refs:
                staged_by_ref.pop(ref_id, None)

            for ref_id in self._inventory_staged_source_refs - staged_by_ref.keys():
                self._queued_sources.pop(ref_id, None)
            for ref_id, source in staged_by_ref.items():
                self._queued_sources[ref_id] = (
                    source["display_name"].strip(), source["file_path"].strip()
                )
            self._staged_source_records = staged_by_ref
            self._inventory_staged_source_refs = set(staged_by_ref)

        def _rerender_rows_preserving_navigation(self):
            selected_ref_id = self._current_ref_id
            selected_tab = self.tabs.currentIndex()
            self._render_rows(select_ref_id=selected_ref_id)
            self._selected(preserve_tab=True)
            if selected_tab < self.tabs.count() and self.tabs.isTabEnabled(selected_tab):
                self.tabs.setCurrentIndex(selected_tab)
            elif self.tabs.count() and self.tabs.isTabEnabled(0):
                self.tabs.setCurrentIndex(0)

        @QtCore.Slot(object)
        def _identity_worker_finished(self, result):
            if not isinstance(result, dict):
                result = {
                    "admitted_ref_ids": [],
                    "stage": "admission",
                    "error": "Identity decision worker returned an invalid result.",
                }
            admitted_ref_ids = [
                ref_id for ref_id in result.get("admitted_ref_ids", [])
                if isinstance(ref_id, str)
            ]
            self._recorded_identity_refs.update(admitted_ref_ids)
            mode = self._identity_mode
            stage = result.get("stage")
            if stage == "admission":
                self._rerender_rows_preserving_navigation()
                self._update_proceed_button()
                if mode == "proceed":
                    decision = result.get("failed_decision") or {}
                    ref_id = decision.get("ref_id")
                    number = next(
                        (number for current_ref, number, _review in self._pending_identity_reviews()
                         if current_ref == ref_id),
                        ref_id or "unknown",
                    )
                    source_number = number if number is not None else ref_id or "unknown"
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Unable to record identity decision",
                        f"The identity decision for source [{source_number}] could not be "
                        f"confirmed: {result.get('error')}. Proceed was not submitted.",
                    )
                else:
                    QtWidgets.QMessageBox.critical(
                        self, "Identity decision not stored", str(result.get("error") or "Admission failed.")
                    )
                return
            if stage == "refresh":
                self._identity_refresh_failed_refs.update(admitted_ref_ids)
                self._rerender_rows_preserving_navigation()
                self._update_proceed_button()
                if mode == "proceed":
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Identity decision recorded; refresh failed",
                        "The identity decision was stored, but Guided Fetch could not confirm "
                        f"the updated inventory: {result.get('error')}. Proceed was not "
                        "submitted; close and reopen Guided Fetch before continuing.",
                    )
                else:
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Identity decision recorded; refresh failed",
                        "The decision was stored, but Guided Fetch could not refresh: "
                        f"{result.get('error')}",
                    )
                return
            try:
                self._refresh_inventory_preserving_navigation(result["inventory"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._identity_refresh_failed_refs.update(admitted_ref_ids)
                self._rerender_rows_preserving_navigation()
                self._update_proceed_button()
                if mode == "proceed":
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Identity decision recorded; refresh failed",
                        "The identity decision was stored, but Guided Fetch could not confirm "
                        f"the updated inventory: {exc}. Proceed was not submitted; close and "
                        "reopen Guided Fetch before continuing.",
                    )
                else:
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Identity decision recorded; refresh failed",
                        f"The decision was stored, but Guided Fetch could not refresh: {exc}",
                    )
                return
            still_pending = self._pending_identity_reviews()
            pending_refs = {ref_id for ref_id, _number, _review in still_pending}
            self._identity_refresh_failed_refs.update(
                ref_id for ref_id in pending_refs
                if ref_id in self._recorded_identity_refs
            )
            self._update_proceed_button()
            if mode == "proceed" and self._model.has_pending_identity_reviews():
                QtWidgets.QMessageBox.critical(
                    self,
                    "Identity review still pending",
                    "Guided Fetch still reports a pending identity review after submission. "
                    "Proceed was not submitted; close and reopen Guided Fetch before continuing.",
                )
                return
            if self._identity_refresh_failed_refs.intersection(admitted_ref_ids):
                if mode == "proceed":
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Identity review still pending",
                        "Guided Fetch still reports a pending identity review after submission. "
                        "Proceed was not submitted; close and reopen Guided Fetch before continuing.",
                    )
                else:
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Identity decision recorded; refresh failed",
                        "The decision was stored, but the refreshed inventory still reports the "
                        "identity review as pending. Close and reopen Guided Fetch to continue.",
                    )
                return
            if mode == "proceed":
                if self._model.has_pending_identity_reviews():
                    return
                self._identity_proceed_after_finish = True
            else:
                self.statusBar().showMessage(
                    "Identity decision recorded; Fetch can now continue.", 10_000
                )

        @QtCore.Slot()
        def _identity_thread_finished(self):
            self._join_worker_thread(self._identity_thread)
            self._identity_thread = None
            self._identity_worker = None
            self._identity_mode = None
            proceed_after_finish = self._identity_proceed_after_finish
            self._identity_proceed_after_finish = False
            skip_remaining_confirmed = self._identity_proceed_skip_remaining_confirmed
            self._identity_proceed_skip_remaining_confirmed = False
            close_after_finish = self._identity_close_when_finished
            self._identity_close_when_finished = False
            self._selected(preserve_tab=True)
            self._update_proceed_button()
            self._update_ocr_button()
            if proceed_after_finish:
                proceeded = self._invoke_proceed(
                    skip_remaining_confirmed=skip_remaining_confirmed,
                )
                if not proceeded and not close_after_finish:
                    self._start_pending_auto_ocr()
            elif not close_after_finish:
                self._start_pending_auto_ocr()
            if close_after_finish and not proceed_after_finish and self.isVisible():
                self.close()

        def _invoke_proceed(self, *, skip_remaining_confirmed: bool = False):
            if (
                self._proceed_preparing or self._proceed_thread is not None
                or self._source_stage_busy()
            ):
                return False
            if skip_remaining_confirmed and self._prepare_proceed is None:
                self._application_modal_critical(
                    "Unable to proceed",
                    "The confirmed skip decision cannot be saved by this Guided Fetch window. "
                    "Close and reopen it with the current desktop launcher.",
                )
                return False
            self._proceed_preparing = True
            for widget in (
                self.open_chrome,
                self.tier_switch,
                self.fulltext_label,
                self.abstract_label,
                self.choose_file,
                self.file_label,
                self.file_drop_zone,
                self.submit,
                self.discard_source_button,
                self.identity_review_reason,
                self.attest_identity_button,
                self.keep_unverified_button,
                self.ocr_button,
                self.proceed_button,
            ):
                widget.setEnabled(False)
            if self._prepare_proceed is None:
                return self._finish_proceed()
            self.statusBar().showMessage(
                "Preparing Fetch continuation. You can keep navigating.", 0
            )
            thread = QtCore.QThread(self)
            worker = ProceedWorker(
                self._prepare_proceed,
                skip_remaining_confirmed=skip_remaining_confirmed,
            )
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            self._connect_gui_signal(worker.finished, self._proceed_worker_finished)
            worker.finished.connect(thread.quit)
            self._connect_gui_signal(thread.finished, self._proceed_thread_finished)
            thread.finished.connect(worker.deleteLater)
            self._proceed_thread = thread
            self._proceed_worker = worker
            thread.start()
            return True

        @QtCore.Slot()
        def _proceed_thread_finished(self):
            self._join_worker_thread(self._proceed_thread)
            outcome = self._proceed_outcome
            self._proceed_thread = None
            self._proceed_worker = None
            self._proceed_outcome = None
            self.statusBar().clearMessage()
            if not isinstance(outcome, dict) or outcome.get("error") is not None:
                self._proceed_preparing = False
                self._selected(preserve_tab=True)
                self._update_ocr_button()
                self._update_proceed_button()
                error = (
                    outcome.get("error") if isinstance(outcome, dict)
                    else "Proceed preparation did not return a result."
                )
                self._application_modal_critical("Unable to proceed", str(error))
                return
            # Keep capture mutations blocked while closeEvent waits for the browser worker.
            self._finish_proceed()

        @QtCore.Slot(object)
        def _proceed_worker_finished(self, outcome):
            self._proceed_outcome = outcome

        def _finish_proceed(self):
            self.statusBar().clearMessage()
            try:
                self._proceed()
            except Exception as exc:
                self._proceed_preparing = False
                self._selected(preserve_tab=True)
                self._update_ocr_button()
                self._update_proceed_button()
                self._application_modal_critical("Unable to proceed", str(exc))
                return False
            self.close()
            return True

        def _open_link(self):
            if self._current_ref_id is None:
                return
            link = self._browser_url()
            if link is not None:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(link))

        def _open_controlled_browser(self):
            if self._proceed_preparing or self._current_ref_id is None:
                return
            if self._source_mutation_for_ref(self._current_ref_id) is not None:
                self.statusBar().showMessage(
                    "Wait for this reference's source save to finish before starting another capture.",
                    10_000,
                )
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
                self.browser_capture_text.connect(self._browser_worker.capture_text)
                self.browser_capture_download.connect(self._browser_worker.capture_download)
                self.browser_discard_download.connect(self._browser_worker.discard_download)
                self.browser_park.connect(self._browser_worker.park_visible_context)
                self.browser_theme.connect(self._browser_worker.set_theme)
                self._connect_gui_signal(self._browser_worker.availability, self._browser_available)
                self._connect_gui_signal(self._browser_worker.opened, self._browser_opened)
                self._connect_gui_signal(self._browser_worker.artifact, self._browser_artifact)
                self._connect_gui_signal(self._browser_worker.download_detected, self._browser_download_detected)
                self._connect_gui_signal(
                    self._browser_worker.toolbar_status_changed,
                    self._browser_toolbar_status_changed,
                )
                self._connect_gui_signal(self._browser_worker.failed, self._browser_failed)
                self._connect_gui_signal(self._browser_worker.capture_failed, self._browser_capture_failed)
                self._connect_gui_signal(self._browser_worker.capture_cancelled, self._browser_capture_cancelled)
                self._connect_gui_signal(self._browser_worker.capture_requested, self._capture_browser)
                self._browser_worker.closed.connect(self._browser_thread.quit)
                self._browser_worker.closed.connect(self._browser_worker.deleteLater)
                self._connect_gui_signal(self._browser_thread.finished, self._browser_finished)
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
                self._application_modal_warning("Browser navigation", str(exc))
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
                self._prepare_browser_modal(box)
                box.exec()
                if box.clickedButton() is download:
                    QtGui.QDesktopServices.openUrl(QtCore.QUrl(CHROME_DOWNLOAD_URL))
                elif box.clickedButton() is retry:
                    self.browser_start.emit()
                return
            if code == "browser_launch_failed":
                self._application_modal_critical(
                    "Google Chrome could not be started",
                    str(status.get("reason") or "Google Chrome could not be started."),
                )
                return
            self._application_modal_critical(
                "Assisted browser unavailable",
                str(status.get("reason") or "Browser unavailable."),
            )

        def _browser_opened(self, navigation: dict):
            self.statusBar().showMessage(
                f"Chrome opened at {navigation.get('url') or navigation.get('requested_url')}",
                10_000,
            )

        def _position_browser_modal(self, dialog):
            dialog.adjustSize()
            application = QtWidgets.QApplication.instance()
            cursor = QtGui.QCursor.pos()
            screen = application.screenAt(cursor) if application is not None else None
            if screen is None and application is not None:
                screen = application.primaryScreen()
            if screen is not None:
                bounds = screen.availableGeometry()
                dialog.move(bounds.center() - dialog.rect().center())

        def _prepare_browser_capture_progress(self, dialog, *, modal: bool):
            modality = (
                QtCore.Qt.WindowModality.ApplicationModal
                if modal else QtCore.Qt.WindowModality.NonModal
            )
            dialog.setModal(modal)
            dialog.setWindowModality(modality)
            dialog.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
            self._position_browser_modal(dialog)
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            # Paint synchronously so the operator sees progress before capture work begins.
            dialog.repaint()
            dialog.spinner.repaint()

        def _begin_browser_capture(self, key: tuple[str, int], phase: str):
            self._browser_capture_active_key = key
            self._browser_capture_phase = phase
            if self._browser_capture_progress is None:
                self._browser_capture_progress = CaptureProgressDialog(
                    self,
                    self._cancel_browser_capture,
                    localized_labels=labels,
                )
            dialog = self._browser_capture_progress
            dialog.set_dark_theme(self._dark_theme)
            if phase.startswith("Checking"):
                progress_text = labels["guided_fetch_capture_pdf_check_message"]
                window_title = labels["guided_fetch_capture_pdf_check_title"]
            elif "PDF" in phase:
                progress_text = labels["guided_fetch_capture_pdf_message"]
                window_title = labels["guided_fetch_capture_title"]
            elif "text" in phase.lower():
                progress_text = labels["guided_fetch_capture_text_message"]
                window_title = labels["guided_fetch_capture_title"]
            elif "HTML" in phase.upper():
                progress_text = labels["guided_fetch_capture_page_message"]
                window_title = labels["guided_fetch_capture_title"]
            else:
                progress_text = labels["guided_fetch_capture_message"]
                window_title = labels["guided_fetch_capture_title"]
            dialog.set_phase(progress_text)
            source_row = self._model.row(key[0])
            source_number = (
                f" [{source_row.ref_number}]"
                if source_row.ref_number is not None else ""
            )
            dialog.set_source_note(
                f"{labels['source']}{source_number} · {source_row.title}"
            )
            dialog._cancel_requested = False
            dialog.cancel_button.setEnabled(True)
            dialog.setWindowTitle(window_title)
            self._prepare_browser_capture_progress(
                dialog, modal=not phase.startswith("Checking")
            )
            self._sync_source_row_pulse_timer()

        def _set_browser_capture_phase(self, key: tuple[str, int], phase: str):
            if self._browser_capture_active_key not in {None, key}:
                return
            self._browser_capture_active_key = key
            self._browser_capture_phase = phase
            self._begin_browser_capture(key, phase)

        def _hide_browser_capture_progress(self, key: tuple[str, int] | None = None):
            if key is not None and self._browser_capture_active_key not in {None, key}:
                return
            if self._browser_capture_progress is not None:
                self._browser_capture_progress.dismiss()

        def _finish_browser_capture(self, key: tuple[str, int] | None = None):
            active_key = self._browser_capture_active_key
            target_key = key if key is not None else active_key
            if target_key is not None:
                self._capture_tiers.pop(target_key, None)
                self._preconfirmed_captures.discard(target_key)
            if target_key is None or active_key == target_key:
                self._browser_capture_active_key = None
                if self._browser_capture_pdf_key == target_key:
                    self._browser_capture_pdf_key = None
                self._browser_capture_phase = None
                self._hide_browser_capture_progress()
                self._sync_source_row_pulse_timer()
            self._update_proceed_button()

        def _cancel_browser_capture(self):
            key = self._browser_capture_active_key
            if key is None:
                self._hide_browser_capture_progress()
                return
            self._browser_capture_cancelled_keys.add(key)
            if self._browser_worker is None:
                self._browser_capture_cancelled(*key)
                return
            # The worker's entry point is intentionally a direct, thread-safe
            # Python call because its Qt event loop may be blocked in capture.
            self._browser_worker.request_cancel_capture(*key)

        def _browser_capture_cancelled(self, ref_id: str, index: int):
            key = (ref_id, index)
            if key != self._browser_capture_active_key:
                return
            self.statusBar().showMessage("Browser capture cancelled.", 5_000)
            self._finish_browser_capture(key)

        def _browser_capture_failed(self, ref_id: str, index: int, reason: str):
            key = (ref_id, index)
            if key in self._browser_capture_cancelled_keys:
                self._finish_browser_capture(key)
                return
            if self._browser_capture_active_key not in {None, key}:
                return
            self._finish_browser_capture(key)
            self._application_modal_warning("Browser capture", reason)

        def _capture_browser(self, kind: str):
            if self._proceed_preparing:
                return
            if self._identity_operation_busy():
                self.statusBar().showMessage(
                    "Wait for the identity decision to finish before capturing a source.", 10_000
                )
                return
            if kind not in {"html", "pdf", "text"}:
                return
            if self._browser_ref_id is None or self._browser_worker is None:
                return
            if not self._model.can_submit_source(self._browser_ref_id):
                return
            if self._source_mutation_for_ref(self._browser_ref_id) is not None:
                self.statusBar().showMessage(
                    "Wait for this reference's source save to finish before starting another capture.",
                    10_000,
                )
                return
            if self._capture_tiers:
                return
            if self._browser_preflight_job_for_ref(self._browser_ref_id) is not None:
                self.statusBar().showMessage(
                    "A PDF check is already pending for this reference.", 10_000
                )
                return
            index = self._capture_index
            self._capture_index += 1
            ref_id = self._browser_ref_id
            key = (ref_id, index)
            self._capture_tiers[key] = self._browser_tier or "fulltext"
            self._browser_capture_pdf_key = key if kind == "pdf" else None
            self._update_proceed_button()
            self._begin_browser_capture(key, "Capturing source…")
            signal = {
                "html": self.browser_capture_html,
                "pdf": self.browser_capture_pdf,
                "text": self.browser_capture_text,
            }[kind]
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
            if self._proceed_preparing:
                self.browser_discard_download.emit(token)
                return
            if self._identity_operation_busy():
                self.browser_discard_download.emit(token)
                return
            if self._browser_ref_id is None or not self._model.can_submit_source(self._browser_ref_id):
                self.browser_discard_download.emit(token)
                return
            ref_id = self._browser_ref_id
            tier = self._browser_tier or "fulltext"
            if self._browser_preflight_job_for_ref(ref_id) is not None:
                self.browser_discard_download.emit(token)
                self.statusBar().showMessage(
                    "A PDF check is already pending for this reference.", 10_000
                )
                return
            question = f"You downloaded {filename}. Is this the exact cited source you wanted?"
            if not self._application_modal_question("Confirm downloaded source", question):
                self.browser_discard_download.emit(token)
                return
            if self._capture_tiers or self._browser_preflight_job_for_ref(ref_id) is not None:
                self.browser_discard_download.emit(token)
                return
            index = self._capture_index
            self._capture_index += 1
            self._capture_tiers[(ref_id, index)] = tier
            self._preconfirmed_captures.add((ref_id, index))
            self._browser_capture_pdf_key = (ref_id, index)
            self.proceed_button.setEnabled(False)
            self._begin_browser_capture((ref_id, index), "Capturing downloaded PDF…")
            self.browser_capture_download.emit(ref_id, index, token)

        def _browser_artifact(self, artifact: dict):
            ref_id = artifact.get("ref_id")
            index = artifact.get("capture_index")
            path = artifact.get("artifact_path")
            if not isinstance(ref_id, str) or not isinstance(index, int) or not isinstance(path, str):
                self._browser_failed("Invalid browser artifact.")
                return
            key = (ref_id, index)
            if self._proceed_preparing:
                if key in self._capture_tiers:
                    self._finish_browser_capture(key)
                self.statusBar().showMessage(
                    "The browser capture finished while Proceed was being prepared; it was not queued.",
                    10_000,
                )
                return
            if key in self._browser_capture_cancelled_keys:
                self._finish_browser_capture(key)
                return
            if (
                self._browser_capture_active_key != key
                or key not in self._capture_tiers
            ):
                return
            self._hide_browser_capture_progress(key)
            if self._identity_operation_busy():
                self._capture_tiers.pop(key, None)
                self._preconfirmed_captures.discard(key)
                self._finish_browser_capture(key)
                return
            tier = self._capture_tiers.pop((ref_id, index), "fulltext")
            preconfirmed = (ref_id, index) in self._preconfirmed_captures
            self._preconfirmed_captures.discard((ref_id, index))
            try:
                row = self._model.row(ref_id)
            except KeyError:
                self._finish_browser_capture(key)
                self._browser_failed("Browser artifact has an unknown reference target.")
                return
            question = (
                f"Confirm that the captured browser artifact is the exact cited work "
                f"for source [{row.ref_number}]?"
            )
            if not preconfirmed and not self._application_modal_question(
                "Confirm captured source", question
            ):
                self._finish_browser_capture(key)
                return
            if self._identity_operation_busy():
                self._finish_browser_capture(key)
                self.statusBar().showMessage(
                    "The captured artifact was not queued because an identity decision is in progress.",
                    10_000,
                )
                return
            if self._is_pdf_path(path):
                # Acquisition is complete. Free the browser capture slot before
                # the background readability check so another reference can start.
                self._finish_browser_capture(key)
                self._queue_preflight(
                    "browser",
                    path,
                    tier,
                    ref_id=ref_id,
                    capture_key=key,
                    capture_index=index,
                    artifact=dict(artifact),
                )
                self.statusBar().showMessage(
                    "Checking the captured PDF in the background.", 10_000
                )
                return
            try:
                preflight = _preflight_source_file(path, tier)
            except (OSError, ValueError) as exc:
                self._finish_browser_capture(key)
                self._application_modal_warning("Browser capture", str(exc))
                return
            self._queue_browser_source(artifact, ref_id, tier, preflight)

        def _queue_browser_source(
            self, artifact: dict, ref_id: str, tier: str, preflight: str,
            *, background_preflight: bool = False,
        ):
            if self._proceed_preparing:
                if not background_preflight:
                    self._finish_browser_capture()
                self.statusBar().showMessage(
                    "The browser capture was not queued while Proceed was being prepared.",
                    10_000,
                )
                return
            if self._identity_operation_busy():
                if not background_preflight:
                    self._finish_browser_capture()
                else:
                    self._application_modal_warning(
                        "Browser capture",
                        "The PDF was not queued because an identity decision is in progress.",
                    )
                return
            if self._source_mutation_for_ref(ref_id) is not None:
                if not background_preflight:
                    self._finish_browser_capture()
                self._application_modal_warning(
                    "Browser capture",
                    "A source save is already pending for this reference. "
                    "The captured file remains available in the browser audit folder.",
                )
                return
            path = artifact["artifact_path"]
            try:
                payload = source_payload(
                    file_path=path,
                    tier=tier,
                    source_ref=artifact.get("url") or artifact.get("requested_url"),
                )
            except (OSError, ValueError) as exc:
                if not background_preflight:
                    self._finish_browser_capture()
                    self._browser_failed(str(exc))
                else:
                    self._application_modal_warning("Browser capture", str(exc))
                return
            display_name = (
                _normalized_download_name(artifact.get("display_name"))
                or Path(path).name
            )
            capture_index = artifact.get("capture_index")
            capture_key = (
                (ref_id, capture_index) if type(capture_index) is int else None
            )
            if not self._queue_source_stage(
                ref_id,
                payload,
                display_name=display_name,
                audit_path=path,
                preflight=preflight,
                capture_key=capture_key,
            ):
                if not background_preflight:
                    self._finish_browser_capture()
                self._application_modal_warning(
                    "Browser capture",
                    "A source save is already pending for this reference. "
                    "The captured file remains available in the browser audit folder.",
                )
                return
            if not background_preflight:
                self._finish_browser_capture()
                self.browser_park.emit()

        def _browser_failed(self, reason: str):
            self._clear_browser_toolbar_pending_message()
            if self._capture_tiers:
                self._capture_tiers.clear()
                self._preconfirmed_captures.clear()
                self._finish_browser_capture()
            self._application_modal_warning("Browser capture", reason)

        def _clear_browser_toolbar_pending_message(self):
            message = self._browser_toolbar_pending_message
            self._browser_toolbar_pending_message = None
            if message and self.statusBar().currentMessage() == message:
                self.statusBar().clearMessage()

        def _browser_toolbar_status_changed(self, status):
            from core.fetch.fallbacks.fetch_modes.interactive_browser import (
                CaptureToolbarStatus,
            )

            if status is CaptureToolbarStatus.PENDING:
                message = labels["guided_fetch_browser_toolbar_pending"]
                self._browser_toolbar_pending_message = message
                self.statusBar().showMessage(message)
            elif status is CaptureToolbarStatus.READY:
                self._clear_browser_toolbar_pending_message()

        def _choose_file(self):
            if (
                self._ocr_thread is not None or self._identity_operation_busy()
                or self._proceed_preparing
            ):
                return
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose source")
            if path:
                self._set_file(path)

        def _set_file(self, path: str):
            if self._proceed_preparing:
                return
            if self._identity_operation_busy():
                self.statusBar().showMessage(
                    "Wait for the identity decision to finish before replacing the selected source.",
                    10_000,
                )
                return
            if self._ocr_thread is not None:
                self.statusBar().showMessage(
                    "Wait for OCR to finish before replacing the selected source.", 10_000
                )
                return
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
            if self._source_mutation_for_ref(ref_id) is not None:
                self.statusBar().showMessage(
                    "Wait for this reference's source save to finish before replacing it.",
                    10_000,
                )
                return
            self._next_file_generation += 1
            generation = self._next_file_generation
            candidate = Path(path)
            self._selected_file = None
            self._selected_file_ref_id = None
            self._selected_file_generation = None
            self._selected_file_ocr_pending = False
            self._selected_file_preflight_pending = False
            self._selected_file_preflight_tier = None
            self.file_label.setText(labels["guided_fetch_drop_file"])
            if not Path(io_path(candidate)).is_file():
                QtWidgets.QMessageBox.warning(self, "Invalid file", "Choose one existing file.")
                return
            tier = self._selected_tier()
            if self._is_pdf_path(str(candidate)):
                self._selected_file = str(candidate)
                self._selected_file_ref_id = ref_id
                self._selected_file_generation = generation
                self._selected_file_preflight_pending = True
                self.file_label.setToolTip("")
                self.file_label.setText("Checking PDF readability…")
                self.submit.setText("Checking PDF…")
                self.submit.setEnabled(False)
                self._queue_preflight(
                    "selection",
                    str(candidate),
                    tier,
                    ref_id=ref_id,
                    file_generation=generation,
                    context_generation=self._selection_context_generation,
                )
                return
            try:
                preflight = _preflight_source_file(str(candidate), tier)
            except (OSError, ValueError) as exc:
                self.file_label.setText("File rejected. Choose another file.")
                self.file_label.setToolTip(str(exc))
                QtWidgets.QMessageBox.warning(self, "Source cannot be used", str(exc))
                return
            self._selected_file = str(candidate)
            self._selected_file_ref_id = ref_id
            self._selected_file_generation = generation
            self._selected_file_ocr_pending = preflight == "ocr_pending"
            self._selected_file_preflight_tier = tier
            self.file_label.setToolTip("")
            self.file_label.setText(
                f"{candidate.name} (OCR needed)" if preflight == "ocr_pending"
                else candidate.name
            )
            self.submit.setText(
                labels["ocr_confirm_button"] if preflight == "ocr_pending" and self._run_ocr is not None
                else labels["guided_fetch_confirm_source"]
            )
            self.submit.setEnabled(self._ocr_thread is None and not self._preflight_busy())
            self._stage_selected_file(
                ref_id,
                str(candidate),
                generation,
                tier,
                preflight,
                context_generation=self._selection_context_generation,
            )

        def _submit_file(self):
            if self._identity_operation_busy() or self._proceed_preparing:
                return
            if self._ocr_thread is not None:
                return
            ref_id = self._current_ref_id
            file_path = self._selected_file
            file_generation = self._selected_file_generation
            if ref_id is None or file_path is None:
                QtWidgets.QMessageBox.information(self, "Source required", "Choose a source and a file.")
                return
            if self._source_mutation_for_ref(ref_id) is not None:
                self.statusBar().showMessage(
                    "Wait for this reference's source save to finish before confirming another source.",
                    10_000,
                )
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
            if self._preflight_busy():
                self.statusBar().showMessage(
                    "Wait for the current PDF check to finish before confirming another source.",
                    10_000,
                )
                return
            tier = self._selected_tier()
            if self._is_pdf_path(file_path):
                if self._selected_file_preflight_pending or (
                    self._selected_file_preflight_tier != tier
                ):
                    self._queue_selected_file_preflight()
                    self.statusBar().showMessage(
                        "Checking the selected PDF before confirmation.", 10_000
                    )
                    return
                if self._preflight_busy():
                    self.statusBar().showMessage(
                        "Another PDF check is still running. Confirm the source when it finishes.",
                        10_000,
                    )
                    return
                self._submit_preflight_request_id = self._queue_preflight(
                    "submit",
                    file_path,
                    tier,
                    ref_id=ref_id,
                    file_generation=file_generation,
                    context_generation=self._selection_context_generation,
                )
                self.submit.setEnabled(False)
                self.statusBar().showMessage(
                    "Rechecking PDF readability before confirmation.", 10_000
                )
                return
            try:
                preflight = _preflight_source_file(file_path, tier)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.critical(self, "Source cannot be used", str(exc))
                return
            self._stage_selected_file(
                ref_id,
                file_path,
                file_generation,
                tier,
                preflight,
                context_generation=self._selection_context_generation,
            )

        def _mark_queued(
            self, ref_id: str, source_name: str, *, audit_path: str | None = None,
            ocr_pending: bool = False, preserve_navigation: bool = False,
        ):
            self._queued_sources[ref_id] = (source_name, audit_path)
            self._ocr_row_status.pop(ref_id, None)
            if ocr_pending:
                self._staged_ocr_refs.add(ref_id)
            else:
                self._staged_ocr_refs.discard(ref_id)
            self._render_rows(
                select_ref_id=self._current_ref_id if preserve_navigation else ref_id
            )
            self._selected()
            self._show_queued_status(self._current_ref_id or ref_id)

        def _discard_queued_source(self):
            if self._proceed_preparing:
                return
            ref_id = self._current_ref_id
            if ref_id is None:
                return
            mutation = self._source_mutation_for_ref(ref_id)
            if mutation is not None:
                if (
                    mutation is not self._source_mutation_active_request
                    and mutation.get("action") == "stage"
                ):
                    self._source_mutation_queue = [
                        request for request in self._source_mutation_queue
                        if request.get("request_id") != mutation.get("request_id")
                    ]
                    self.statusBar().showMessage(
                        "Queued source save cancelled; any previously saved source is unchanged.",
                        10_000,
                    )
                    self._refresh_source_mutation_ui()
                else:
                    self.statusBar().showMessage(
                        "The source operation has started or is already queued; wait for it to finish.",
                        10_000,
                    )
                return
            browser_preflight = self._browser_preflight_job_for_ref(ref_id)
            if browser_preflight is not None and (
                self._queued_sources.get(ref_id, (None, None))[1]
                != browser_preflight.get("path")
            ):
                if not self._identity_operation_busy():
                    self._cancel_browser_preflight_job(browser_preflight)
                return
            if ref_id not in self._queued_sources:
                return
            if self._identity_operation_busy() or self._discard_source is None:
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
            if (
                self._source_mutation_for_ref(ref_id) is not None
                or self._identity_operation_busy()
                or self._proceed_preparing
            ):
                self.statusBar().showMessage(
                    "The source changed while the discard confirmation was open. Review it again.",
                    10_000,
                )
                return
            if not self._queue_source_discard(
                ref_id,
                source_path=self._queued_sources.get(ref_id, (None, None))[1],
            ):
                self.statusBar().showMessage(
                    "A source operation is already pending for this reference.", 10_000
                )

        def _cancel_browser_preflight_job(self, request):
            if not self._browser_preflight_job_is_current(request):
                return
            request_id = request.get("request_id")
            key = request.get("capture_key")
            self._browser_pdf_preflight_jobs.pop(key, None)
            self._browser_preflight_pending = [
                item for item in self._browser_preflight_pending
                if item.get("request_id") != request_id
            ]
            self._refresh_browser_preflight_request_id()
            self._refresh_browser_preflight_row(request.get("ref_id"))
            self.statusBar().showMessage(
                "PDF readability check cancelled; its result will be discarded.", 10_000
            )
            self._update_ocr_button()
            self._update_proceed_button()

        def _show_queued_status(self, ref_id: str):
            mutation = self._source_mutation_for_ref(ref_id)
            if mutation is not None:
                active = mutation is self._source_mutation_active_request
                if mutation.get("action") == "stage":
                    source_name = mutation.get("display_name") or Path(
                        mutation.get("path") or "source"
                    ).name
                    retained = (
                        " The previously saved source remains queued until this save completes."
                        if ref_id in self._queued_sources else ""
                    )
                    status_detail = (
                        f"{'Saving' if active else 'Waiting to save'} {source_name}; "
                        f"copying and checking its SHA-256 in the background.{retained}"
                    )
                    self.capture_status.setText("Saving source…" if active else "Source save queued")
                    self.capture_status.setToolTip(
                        f"{status_detail}\nNavigation and captures for other references remain available."
                    )
                    self.discard_source_button.setText(
                        "Saving source…" if active else "Cancel queued save"
                    )
                    self.discard_source_button.setToolTip(
                        "An active confirmed save will finish. A queued save can be cancelled "
                        "without changing the previously saved source."
                    )
                    self.discard_source_button.setEnabled(
                        not active and not self._identity_operation_busy()
                        and not self._proceed_preparing
                    )
                else:
                    self.capture_status.setText("Discarding source…")
                    self.capture_status.setToolTip(
                        "Discarding this source in the background; the source remains queued "
                        "until the integrity record is saved. The durable discard record is being written."
                    )
                    self.discard_source_button.setText("Discarding source…")
                    self.discard_source_button.setEnabled(False)
                self.capture_status.setVisible(True)
                self.discard_source_button.setVisible(True)
                self.capture_status_panel.setVisible(True)
                return
            browser_preflight = self._browser_preflight_job_for_ref(ref_id)
            if browser_preflight is not None and (
                self._queued_sources.get(ref_id, (None, None))[1]
                != browser_preflight.get("path")
            ):
                active = self._preflight_active_request
                checking = (
                    active is not None
                    and active.get("request_id") == browser_preflight.get("request_id")
                )
                source_name = (
                    _normalized_download_name(
                        browser_preflight.get("artifact", {}).get("display_name")
                    )
                    or Path(browser_preflight["path"]).name
                )
                status_detail = (
                    labels["guided_fetch_pdf_checking_detail"].format(
                        source=source_name
                    )
                    if checking
                    else labels["guided_fetch_pdf_check_queued_detail"].format(
                        source=source_name
                    )
                )
                self.capture_status.setText(
                    labels[
                        "guided_fetch_pdf_checking"
                        if checking else "guided_fetch_pdf_check_queued"
                    ]
                )
                self.capture_status.setToolTip(
                    f"{status_detail}\n{labels['guided_fetch_pdf_check_running_note']}"
                )
                self.capture_status.setVisible(True)
                self.discard_source_button.setText(labels["guided_fetch_cancel_pdf_check"])
                self.discard_source_button.setToolTip(
                    labels["guided_fetch_pdf_check_cancel_tooltip"]
                )
                self.discard_source_button.setVisible(True)
                self.capture_status_panel.setVisible(True)
                self.discard_source_button.setEnabled(
                    not self._identity_operation_busy()
                    and self._model.can_submit_source(ref_id)
                )
                return
            queued = self._queued_sources.get(ref_id)
            if queued is None:
                self.capture_status_panel.setVisible(False)
                self.capture_status.setVisible(False)
                self.file_label.setToolTip("")
                self.file_label.setAccessibleDescription(
                    "Click or press Enter or Space to choose a source file, or drop one here."
                )
                self.file_drop_zone.setAccessibleDescription(self.file_label.accessibleDescription())
                self.discard_source_button.setVisible(True)
                self.discard_source_button.setEnabled(False)
                self.discard_source_button.setText(labels["guided_fetch_discard"])
                self.discard_source_button.setToolTip(
                    "Remove the queued source from this Guided Fetch session without deleting its audit capture."
                )
                return
            source_name, audit_path = queued
            audit_suffix = (
                f" {labels['guided_fetch_saved_in_detail'].format(folder=Path(audit_path).parent.name)}"
                if audit_path else ""
            )
            ocr_status = self._ocr_row_status.get(ref_id, {}).get("status")
            if ocr_status == "done":
                status_text = labels["guided_fetch_queued_ocr_done_detail"].format(
                    source=source_name
                )
            elif ref_id in self._staged_ocr_refs or ocr_status in {"queued", "running", "failed"}:
                status_text = labels[
                    "guided_fetch_queued_ocr_pending_detail"
                ].format(source=source_name)
            else:
                status_text = labels["guided_fetch_queued_readable_detail"].format(
                    source=source_name
                )
            self.capture_status.setText(labels["guided_fetch_queued"])
            self.capture_status.setToolTip(
                f"{status_text}{audit_suffix}"
                + (
                    f"\n{labels['guided_fetch_audit_capture_saved_detail'].format(path=audit_path)}"
                    if audit_path else ""
                )
            )
            queued_detail = self.capture_status.toolTip()
            self.file_label.setToolTip(queued_detail)
            self.file_label.setAccessibleDescription(
                f"Queued source: {source_name}. {queued_detail}"
            )
            self.file_drop_zone.setAccessibleDescription(self.file_label.accessibleDescription())
            self.capture_status.setVisible(False)
            self.discard_source_button.setText(labels["guided_fetch_discard"])
            self.discard_source_button.setToolTip(
                "Remove the queued source from this Guided Fetch session without deleting its audit capture."
            )
            self.discard_source_button.setVisible(True)
            self.capture_status_panel.setVisible(False)

        def _application_modal_question(self, title: str, text: str) -> bool:
            from .shell.messages import compact_message_dialog
            if title == labels["guided_fetch_proceed_dialog_title"]:
                text = labels.get("guided_fetch_proceed_short", "Continue with the available sources? Remaining sources and unresolved identities will stay unverified.")
            box = compact_message_dialog(
                self, QtCore, QtGui, QtWidgets, title=title, message=text,
                dark=self._dark_theme, confirm_label=labels["guided_fetch_confirm_action"],
                cancel_label=labels["guided_fetch_cancel_action"],
                object_name="guidedFetchConfirmationDialog",
            )
            self._prepare_browser_modal(box)
            return box.exec() == QtWidgets.QDialog.DialogCode.Accepted

        def _application_modal_information(self, title: str, text: str):
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Icon.Information)
            box.setWindowTitle(title)
            box.setText(text)
            box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
            self._prepare_browser_modal(box)
            box.exec()

        def _application_modal_warning(self, title: str, text: str):
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            box.setWindowTitle(title)
            box.setText(text)
            box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
            self._prepare_browser_modal(box)
            box.exec()

        def _application_modal_critical(self, title: str, text: str):
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Icon.Critical)
            box.setWindowTitle(title)
            box.setText(text)
            box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
            self._prepare_browser_modal(box)
            box.exec()

        def _prepare_browser_modal(self, box):
            box.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
            box.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
            self._position_browser_modal(box)
            box.show()
            box.raise_()
            box.activateWindow()

        def _proceed_clicked(self):
            if (
                self._proceed_preparing
                or self._capture_tiers
                or self._ocr_thread is not None
                or self._preflight_busy()
                or self._identity_operation_busy()
                or self._source_stage_busy()
            ):
                return
            pending_reviews = self._pending_identity_reviews()
            refresh_failed = any(
                ref_id in self._identity_refresh_failed_refs
                for ref_id, _number, _review in pending_reviews
            )
            retryable_reviews = [
                (ref_id, number, review)
                for ref_id, number, review in pending_reviews
                if ref_id not in self._recorded_identity_refs
            ]
            if refresh_failed or (pending_reviews and not retryable_reviews):
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
            )
            if pending_reviews:
                source_numbers = ", ".join(
                    f"[{number}]" if number is not None else f"[{ref_id}]"
                    for ref_id, number, _review in pending_reviews
                )
                question = (
                    labels["guided_fetch_proceed_identity_question"].format(
                        source_numbers=source_numbers
                    )
                )
            else:
                question = labels["guided_fetch_proceed_question"]
            ocr_count = len(self._ocr_jobs())
            if ocr_count:
                question += labels["ocr_skip_note"].format(count=ocr_count)
            confirmed_skip_remaining = False
            if not manual_work_resolved and not self._application_modal_question(
                labels["guided_fetch_proceed_dialog_title"], question
            ):
                return
            if not manual_work_resolved:
                confirmed_skip_remaining = True
            if (
                self._proceed_preparing
                or self._capture_tiers
                or self._ocr_thread is not None
                or self._preflight_busy()
                or self._identity_operation_busy()
                or self._source_stage_busy()
            ):
                self._update_proceed_button()
                self.statusBar().showMessage(
                    "A source check started while Proceed was open. Finish or cancel it before continuing.",
                    10_000,
                )
                return
            if pending_reviews:
                decisions = []
                for ref_id, ref_number, review in retryable_reviews:
                    source_number = ref_number if ref_number is not None else ref_id
                    reason = (
                        "The operator confirmed Guided Fetch Proceed / skip remaining; "
                        f"the identity of source [{source_number}] remains unverified."
                    )
                    decisions.append({
                        "ref_id": ref_id,
                        "action": "keep_unverified",
                        "target_sha256": review["target_sha256"],
                        "reason": reason,
                    })
                self._start_identity_worker(
                    decisions,
                    mode="proceed",
                    skip_remaining_confirmed=confirmed_skip_remaining,
                )
                return
            self._invoke_proceed(
                skip_remaining_confirmed=confirmed_skip_remaining,
            )

        def _browser_finished(self):
            self._join_worker_thread(self._browser_thread)
            if self._browser_thread is not None and not self._browser_thread.isRunning():
                self._browser_worker = None
                self._browser_thread = None
            if self._closing:
                self._closing = False
                self.close()

        def closeEvent(self, event):  # noqa: N802 - Qt API name
            if self._proceed_thread is not None:
                self.statusBar().showMessage(
                    "Closing after Fetch preparation finishes.", 10_000
                )
                event.ignore()
                return
            if self._source_stage_busy():
                self._source_mutation_close_when_finished = True
                self.statusBar().showMessage(
                    "Closing after confirmed source saves and discards finish.", 10_000
                )
                event.ignore()
                return
            if self._identity_thread is not None:
                self._identity_close_when_finished = True
                self.statusBar().showMessage(
                    "Closing after the identity decision finishes.", 10_000
                )
                event.ignore()
                return
            if self._ocr_thread is not None:
                self.statusBar().showMessage(labels["ocr_close_busy"], 10_000)
                event.ignore()
                return
            if self._preflight_thread is not None:
                self._preflight_close_when_finished = True
                self._preflight_pending.clear()
                self._browser_preflight_pending.clear()
                self._browser_pdf_preflight_jobs.clear()
                self._submit_preflight_request_id = None
                self._refresh_browser_preflight_request_id()
                self.statusBar().showMessage(
                    "Closing after the current PDF check finishes.", 10_000
                )
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
            if self._parsed_text_thread is not None:
                if self._parsed_text_thread.isRunning():
                    self._clear_parsed_text_viewer()
                    self._parsed_text_close_when_finished = True
                    event.ignore()
                    return
                self._parsed_text_thread.wait()
                self._parsed_text_thread = None
                self._parsed_text_worker = None
                self._parsed_text_active_request = None
            self._clear_parsed_text_viewer()
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
            self._source_pulse_timer.stop()
            super().closeEvent(event)

    return GuidedFetchWindow()


def run_guided_fetch_window(
    run_dir: str,
    *,
    submit_source: Callable[[str, dict[str, Any]], Any],
    proceed: Callable[[], Any],
    prepare_proceed: Callable[..., Any] | None = None,
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
        prepare_proceed=prepare_proceed,
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
            "#111923", "#e6edf6", "#182330", "#223b59", "#34455b",
            "#7ab4ff", "#a1cbff", "#274b73", "#7ab4ff", "#adbed7",
            "#e9f1ff", "#175fe6", "#3679ee", "#124cba",
        )
        if dark else
        (
            "#f8fbfe", "#11183c", "#ffffff", "#edf4ff", "#d1def2",
            "#175fe6", "#1351c4", "#e3efff", "#175fe6", "#66769c",
            "#1351c4", "#175fe6", "#1351c4", "#0f419f",
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
    cancel_background = "#822c39" if dark else "#be3046"
    cancel_hover = "#a43b4c" if dark else "#a8253a"
    cancel_pressed = "#6b2430" if dark else "#8b1e30"
    return f"""
        QWidget {{ background: {background}; color: {text}; font-family: 'Segoe UI', sans-serif; font-size: {font_size}px; }}
        #brandHeader, #actionPanel {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; padding: 7px; }}
        #selectionGuidance {{ background: {accent_surface}; border: 0; border-left: 3px solid {blue}; border-radius: 3px; padding: 4px 7px; color: {disabled_text}; }}
        QFrame#sourceListPanel {{ background: {surface}; border: 1px solid {border}; border-radius: 6px; }}
        QWidget#sourceFilterEmptyState {{ background: {surface}; }}
        QLabel#sourceFilterEmptyLabel {{ color: {text}; padding: 6px; }}
        QFrame#guidedFetchDetailsPanel {{ background: {background}; border: 0; }}
        QFrame#guidedFetchFooter {{ background: {background}; border: 0; border-top: 1px solid {border}; }}
        QLabel#guidedFetchSourcesTitle {{ color: {text}; font-size: {font_size + 3}px; font-weight: 700; }}
        QLabel#guidedFetchSourceCount, QLabel#guidedFetchCaptureHint {{ color: {disabled_text}; }}
        QLabel#guidedFetchFilterLabel {{ color: {text}; font-weight: 600; }}
        #brandHeader QWidget, #brandHeader QLabel {{ background: transparent; }}
        #selectionGuidance {{ font-size: {max(font_size, 16)}px; line-height: 1.2; }}
        QPushButton#sourceFilter {{ min-width: 180px; min-height: 42px; max-height: 42px;
            background: {surface}; color: {blue}; border: 1px solid {blue}; border-radius: 5px;
            padding: 6px 26px 6px 10px; font-weight: 600; }}
        QPushButton#sourceFilter:hover {{ background: {accent_surface}; border-color: {blue_dark}; }}
        QPushButton#sourceFilter:focus {{ border: 2px solid {blue}; padding: 5px 25px 5px 9px; }}
        QPushButton#sourceFilter:pressed {{ background: {selection}; color: {hover_text}; }}
        QMenu#sourceFilterPopup {{ background: {surface}; color: {text};
            border: 1px solid {border}; padding: 6px; }}
        QMenu#sourceFilterPopup::item {{ min-height: 28px; padding: 7px 12px;
            border-radius: 3px; }}
        QMenu#sourceFilterPopup::item:selected {{ background: {selection};
            color: {hover_text}; }}
        QCheckBox#sourceFilterChoice {{ background: transparent; color: {text};
            spacing: 9px; padding: 5px 12px; min-height: 24px; }}
        QCheckBox#sourceFilterChoice:hover {{ background: {selection};
            color: {hover_text}; border-radius: 3px; }}
        QCheckBox#sourceFilterChoice::indicator {{ width: 16px; height: 16px; }}
        #captureStatusPanel {{ background: {surface}; border: 1px solid {border}; border-radius: 6px; }}
        #captureStatus {{ background: transparent; color: {text}; border: 0; padding: 0; font-size: {max(font_size - 1, 11)}px; font-weight: 400; }}
        QFrame#identityReviewPanel {{ border: 0; }}
        QScrollArea#identityFactsScroll {{ background: {surface}; border: 1px solid {border}; border-radius: 8px; }}
        QLabel#identityReviewFacts {{ background: {surface}; color: {text}; padding: 12px; }}
        QLineEdit#identityReviewReason {{ background: {surface}; color: {text}; border: 1px solid {border};
            border-radius: 6px; padding: 8px 10px; min-height: 28px; }}
        QPushButton#attestIdentityButton {{ background: {active_button}; color: white; border-color: {blue}; min-height: 40px; }}
        QPushButton#attestIdentityButton:hover {{ background: {active_button_hover}; color: white; }}
        QPushButton#attestIdentityButton:pressed {{ background: {pressed_button}; color: white; }}
        QPushButton#attestIdentityButton:disabled, QPushButton#keepUnverifiedButton:disabled {{
            background: {background}; color: {disabled_text}; border-color: {border}; }}
        QPushButton#keepUnverifiedButton {{ background: {surface}; color: {blue}; border-color: {blue}; min-height: 40px; }}
        QPushButton#keepUnverifiedButton:hover {{ background: {selection}; color: {blue_dark}; }}
        QPushButton#keepUnverifiedButton:pressed {{ background: {pressed_button}; color: white; }}
        QPushButton#attestIdentityButton, QPushButton#keepUnverifiedButton {{
            padding-left: 3px; padding-right: 3px; }}
        QPlainTextEdit, QTableWidget {{ background: {surface}; color: {text}; border: 1px solid {border}; border-radius: 8px; }}
        QTableWidget {{ gridline-color: {border}; alternate-background-color: {accent_surface}; selection-background-color: {selection}; selection-color: {text}; }}
        QHeaderView::section {{ background: {accent_surface}; color: {text}; border: 0; border-bottom: 1px solid {border}; padding: 4px 5px; font-weight: 600; }}
        QTabBar#guidedFetchSourceTabBar::tab {{ background: {surface}; color: {text};
            border: 1px solid {border}; border-bottom-color: {border};
            border-top-left-radius: 7px; border-top-right-radius: 7px;
            border-bottom-left-radius: 0; border-bottom-right-radius: 0;
            padding: 4px 6px; margin-right: 2px;
            font-size: {max(font_size - 2, 12)}px; }}
        QTabBar#guidedFetchSourceTabBar::tab:hover {{ background: {selection};
            color: {hover_text}; border-color: {blue}; }}
        QTabBar#guidedFetchSourceTabBar::tab:pressed {{ background: {pressed_button};
            color: white; border-color: {pressed_button}; }}
        QTabBar#guidedFetchSourceTabBar::tab:focus {{ border: 2px solid {blue}; padding: 3px 5px; }}
        QTabBar#guidedFetchSourceTabBar::tab:selected {{ background: {accent_surface};
            color: {blue}; border: 1px solid {blue};
            border-bottom-color: {accent_surface}; margin-bottom: -1px; font-weight: 600; }}
        QTabBar#guidedFetchSourceTabBar::tab:selected:hover {{ background: {selection};
            color: {blue_dark}; border-color: {blue}; }}
        QTabBar#guidedFetchSourceTabBar::tab:selected:focus {{ border: 2px solid {blue};
            padding: 3px 5px; }}
        QTabBar#guidedFetchSourceTabBar::tab:disabled {{ color: {disabled_text};
            border-color: {border}; }}
        QToolButton#guidedFetchTabScrollLeft, QToolButton#guidedFetchTabScrollRight {{
            padding: 1px;
            background: {accent_surface}; color: {blue};
            border: 1px solid {blue}; border-radius: 5px; }}
        QToolButton#guidedFetchTabScrollLeft:hover, QToolButton#guidedFetchTabScrollRight:hover {{
            background: {selection}; border-color: {blue_dark}; }}
        QToolButton#guidedFetchTabScrollLeft:pressed, QToolButton#guidedFetchTabScrollRight:pressed {{
            background: {pressed_button}; border-color: {pressed_button}; }}
        QToolButton#guidedFetchTabScrollLeft:disabled, QToolButton#guidedFetchTabScrollRight:disabled {{
            background: {surface}; color: {disabled_text}; border-color: {border}; }}
        #detailHeadline {{ font-size: 16px; font-weight: 700; padding: 0; border: 0; }}
        #selectedSourceHeading {{ font-size: 18px; font-weight: 700; padding: 0 0 2px 0; border: 0; }}
        #previewSourceTitle {{ font-size: 14px; font-weight: 700; padding: 0; border: 0; }}
        QPushButton#previewTierButton {{ min-height: 22px; padding: 3px 9px; border-radius: 6px; }}
        QPushButton {{ background: {accent_surface}; color: {button_text}; border: 1px solid {border}; border-radius: 7px; padding: 6px 9px; font-weight: 600; }}
        QPushButton:hover {{ background: {selection}; color: {hover_text}; border-color: {blue}; }}
        QPushButton:pressed {{ background: {pressed_button}; color: white; border-color: {pressed_button}; }}
        QPushButton:disabled {{ background: {background}; color: {disabled_text}; border-color: {border}; }}
        QPushButton#dangerButton {{ background: {cancel_background}; color: white; border-color: {cancel_background}; }}
        QPushButton#dangerButton:hover {{ background: {cancel_hover}; border-color: {cancel_hover}; }}
        QPushButton#dangerButton:pressed {{ background: {cancel_pressed}; border-color: {cancel_pressed}; }}
        QPushButton#instructionsButton, QPushButton#rawJsonButton {{
            background: transparent; color: {blue}; border: 0; border-radius: 3px;
            padding: 2px 4px; font-weight: 400; }}
        QPushButton#instructionsButton:hover, QPushButton#rawJsonButton:hover {{
            background: {selection}; color: {blue_dark}; border: 0; }}
        QPushButton:checked {{ background: {active_button}; color: white; border-color: {blue}; }}
        QPushButton:checked:hover {{ background: {active_button_hover}; color: white; }}
        QPushButton:checked:pressed {{ background: {pressed_button}; color: white; border-color: {pressed_button}; }}
        #themeToggle {{ min-width: 96px; }}
        #proceedButton {{ background: {active_button}; color: white; border-color: {blue}; }}
        #proceedButton:hover {{ background: {active_button_hover}; color: white; }}
        #proceedButton:pressed {{ background: {pressed_button}; color: white; border-color: {pressed_button}; }}
        #proceedButton:disabled, #discardSourceButton:disabled {{ background: {background}; color: {disabled_text}; border-color: {border}; }}
        #tierFullText, #tierAbstract {{ min-height: 24px; padding: 4px 12px; border-radius: 0; }}
        #tierFullText {{ border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
        #tierAbstract {{ margin-left: -1px; border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}
        #runOcrButton {{ background: {ocr_background}; color: {ocr_foreground}; border-color: {ocr_border}; }}
        #runOcrButton:hover {{ background: {ocr_hover}; color: {ocr_foreground}; }}
        #runOcrButton:disabled {{ background: {background}; color: {disabled_text}; border-color: {border}; }}
        #ocrRowProgress {{ background: {surface}; border: 1px solid {ocr_border}; border-radius: 4px; min-height: 8px; }}
        #ocrRowProgress::chunk {{ background: {ocr_border}; border-radius: 3px; }}
        QFrame#previewSkeletonBar {{ background: transparent; border: 0; }}
        QFrame#fileDropZone {{ background: {surface}; border: 2px dashed {blue}; border-radius: 8px; min-height: 56px; }}
        QFrame#fileDropZone:focus {{ border-style: solid; }}
        QLabel#fileDropArea {{ background: transparent; border: 0; border-radius: 0; padding: 0; }}
        QPushButton#confirmSourceButton {{ min-height: 38px; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 3px 1px; }}
        QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 28px; }}
        QScrollBar::handle:vertical:hover {{ background: {blue}; }}
        QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 1px 3px; }}
        QScrollBar::handle:horizontal {{ background: {border}; border-radius: 5px; min-width: 28px; }}
        QScrollBar::handle:horizontal:hover {{ background: {blue}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; background: transparent; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """ + atlante_stylesheet(dark=dark, font_size=font_size) + f"""
        QLabel#guidedFetchSectionHeading {{ color: {blue}; font-size: {font_size + 2}px;
            font-weight: 700; padding-top: 3px; padding-bottom: 2px; }}
        QCheckBox:hover, QRadioButton:hover {{ color: {blue}; }}
        QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
            background: {selection}; border-color: {blue}; }}
        QCheckBox::indicator:pressed, QRadioButton::indicator:pressed {{
            background: {pressed_button}; border-color: {blue}; }}
        QFrame#actionPanel {{ background: {surface}; border: 1px solid {border};
            border-radius: 6px; }}
        QTabWidget#guidedFetchSourceTabs::pane {{ border: 1px solid {border}; border-left: 0;
            border-radius: 0 0 6px 0; background: {surface}; }}
        QTableWidget#detailTable, QPlainTextEdit#previewText, QFrame#fileDropZone,
        QScrollArea#identityFactsScroll {{ border-radius: 6px; }}
        QTableWidget#detailTable {{ border: 1px solid {border}; gridline-color: transparent; }}
        QTableWidget#detailTable::item {{ border: 0; border-bottom: 1px solid {border}; padding: 4px 6px; }}
        QTableWidget#detailTable QHeaderView::section {{ border: 0; border-bottom: 1px solid {border}; border-radius: 0; }}
        QTableWidget#sourceTable {{ border: 0; border-top: 1px solid {border}; border-radius: 0;
            selection-background-color: {selection}; selection-color: {text}; }}
        QTableWidget#sourceTable::item {{ border: 0; border-bottom: 1px solid {border}; padding: 3px; }}
        QPushButton#instructionsButton, QPushButton#rawJsonButton {{
            font-size: {max(11, font_size - 2)}px; font-weight: 400; padding: 2px 3px;
            color: {blue}; border: 0; background: transparent; }}
        QPushButton#instructionsButton:hover, QPushButton#rawJsonButton:hover {{ background: {selection}; }}
        QPushButton#instructionsButton:pressed, QPushButton#rawJsonButton:pressed {{ background: {border}; }}
        QPushButton#discardSourceButton {{ color: {'#ff9e99' if dark else '#c62828'};
            border: 1px solid {'#934548' if dark else '#efb2b2'}; background: {surface};
            font-size: {max(11, font_size - 1)}px; padding: 4px 7px; }}
        QPushButton#discardSourceButton:hover {{ background: {'#452b31' if dark else '#fff0f0'}; }}
        QPushButton#discardSourceButton:pressed {{ background: {'#61353d' if dark else '#ffdcdc'}; }}
        QPushButton#discardSourceButton:disabled {{ color: {disabled_text}; border-color: {border}; background: {background}; }}
        QSplitter#detailSplitter::handle:horizontal {{ background: {background}; width: 7px; }}
        QSplitter#detailSplitter::handle:horizontal:hover {{ background: {selection}; }}
        QSplitter#detailSplitter::handle:horizontal:pressed {{ background: {blue}; }}
        QFrame#previewSkeletonBar {{ background: transparent; border: 0; }}
    """


def _identity_review_colors(*, dark: bool) -> tuple[str, str, str]:
    """Readable warning surface, text, and border for a pending identity decision."""
    return ("#3d331c", "#fff0c2", "#e0ad45") if dark else (
        "#fff4d1", "#3b2d0a", "#c28a00"
    )


def _status_badge_surfaces(tone: str, *, dark: bool) -> tuple[str, str]:
    """Return subdued fill and border colors for compact source status badges."""
    light = {
        "green": ("#e7f6ec", "#b8e4c5"),
        "yellow": ("#fff3cf", "#f0d98c"),
        "amber": ("#fff3cf", "#f0d98c"),
        "red": ("#fde8e7", "#f4c5c3"),
        "purple": ("#f1eafe", "#ddccfa"),
        "blue": ("#e8f1ff", "#c5dcff"),
        "neutral": ("#eef1f5", "#d9dee8"),
    }
    dark_palette = {
        "green": ("#20382b", "#356447"),
        "yellow": ("#40371f", "#6a5928"),
        "amber": ("#40371f", "#6a5928"),
        "red": ("#432a2a", "#704242"),
        "purple": ("#352b48", "#554571"),
        "blue": ("#26384e", "#3b5778"),
        "neutral": ("#303846", "#495465"),
    }
    return (dark_palette if dark else light).get(
        tone, (dark_palette if dark else light)["neutral"]
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
        with Path(io_path(path)).open("r", encoding="utf-8", errors="replace") as source:
            return source.read(100_000)
    except (OSError, ValueError):
        return "Preview unavailable."


def _read_parsed_text(path: str | None) -> str | None:
    """Read an existing parsed text sidecar completely, without source extraction."""
    if not isinstance(path, str) or Path(path).suffix.lower() != ".txt":
        return None
    try:
        with Path(io_path(path)).open("r", encoding="utf-8", errors="replace") as source:
            return source.read()
    except (OSError, ValueError):
        return None
