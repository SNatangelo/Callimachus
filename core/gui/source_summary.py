# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only source summaries with native rounded QTextDocument decorations."""
from PySide6 import QtCore, QtGui, QtWidgets


_ROLE = int(QtGui.QTextFormat.Property.UserProperty) + 61


def configure_summary_frames(view, *, dark):
    """Tag current document frames, never retaining wrappers after setHtml()."""
    view.setProperty("summaryDark", dark)
    links = []
    block = view.document().begin()
    while block.isValid():
        fragment = block.begin()
        while not fragment.atEnd():
            part = fragment.fragment()
            names = part.charFormat().anchorNames()
            if part.charFormat().anchorHref().startswith("callimachus://source-verdict/"):
                links.append((part.position(), part.length(), part.charFormat().anchorHref()))
            role = next(("claim" if name.startswith("source-claim-frame-") else "verdict"
                         for name in names if name.startswith(("source-claim-frame-", "source-verdict-frame-"))), None)
            if role:
                cursor = QtGui.QTextCursor(view.document())
                cursor.setPosition(part.position())
                frame = cursor.currentTable()
                if frame is not None:
                    style = frame.format()
                    style.setProperty(_ROLE, role)
                    frame.setFormat(style)
            fragment += 1
        block = block.next()
    for position, length, href in links:
        cursor = QtGui.QTextCursor(view.document())
        cursor.setPosition(position)
        cursor.setPosition(position + length, QtGui.QTextCursor.MoveMode.KeepAnchor)
        style = QtGui.QTextCharFormat()
        style.setFontUnderline(False)
        style.setAnchor(True)
        style.setAnchorHref(href)
        cursor.mergeCharFormat(style)
    view.viewport().update()


class SourceSummaryBrowser(QtWidgets.QTextBrowser):
    """Keep Qt text, anchors, selection and accessibility; paint only backgrounds."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().setAutoFillBackground(False)
        self._summary_reflow = None
        self._summary_narrow = None
        self._summary_threshold = 480
        self._summary_pressed = None
        self._summary_hovered = None
        self._summary_timer = QtCore.QTimer(self)
        self._summary_timer.setSingleShot(True)
        self._summary_timer.timeout.connect(self._reflow_summary)
        self.setMouseTracking(True)

    def configure_summary_reflow(self, callback, threshold):
        self._summary_reflow = callback
        self._summary_threshold = threshold
        self._summary_narrow = self.viewport().width() < threshold

    def _reflow_summary(self):
        narrow = self.viewport().width() < self._summary_threshold
        if self._summary_reflow is not None and narrow != self._summary_narrow:
            self._summary_narrow = narrow
            value = self.verticalScrollBar().value()
            self._summary_reflow()
            self.verticalScrollBar().setValue(value)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._summary_reflow is not None:
            self._summary_timer.start(0)

    def summary_decorations(self):
        result = []
        layout = self.document().documentLayout()
        offset = QtCore.QPointF(-self.horizontalScrollBar().value(), -self.verticalScrollBar().value())
        def visit(frame):
            for child in frame.childFrames():
                role = child.format().property(_ROLE)
                if role:
                    href = ""
                    if role == "verdict":
                        block = child.firstCursorPosition().block()
                        while block.isValid() and block.position() <= child.lastPosition():
                            fragment = block.begin()
                            while not fragment.atEnd():
                                href = fragment.fragment().charFormat().anchorHref() or href
                                fragment += 1
                            block = block.next()
                    # QTextTable frame positions include their cell padding;
                    # its extent already includes that padding. Convert back
                    # to the visible table border without growing its width.
                    padding = child.format().cellPadding() if isinstance(child, QtGui.QTextTable) else 0
                    rect = layout.frameBoundingRect(child).translated(offset - QtCore.QPointF(padding, padding))
                    result.append((role, rect, child.format().borderBrush().color(), href))
                visit(child)
        visit(self.document().rootFrame())
        return result

    def paintEvent(self, event):
        dark = bool(self.property("summaryDark"))
        base = QtGui.QColor("#101923" if dark else "#f7fbff")
        card = QtGui.QColor("#192736" if dark else "#ffffff")
        border = QtGui.QColor("#34455b" if dark else "#d1def2")
        painter = QtGui.QPainter(self.viewport())
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(event.rect(), base)
        for role, rect, color, href in self.summary_decorations():
            painter.setPen(QtGui.QPen(border if role == "claim" else color, 1))
            if role == "claim":
                painter.setBrush(card)
            else:
                tint = QtGui.QColor(color)
                tint.setAlpha(52 if href == self._summary_pressed else
                              38 if href == self._summary_hovered else 23)
                painter.setBrush(tint)
            painter.drawRoundedRect(rect.adjusted(.5, .5, -.5, -.5), 10, 10)
        painter.end()
        # Draw the actual document exactly once. Qt retains selection rendering,
        # anchor hit testing, keyboard activation and its accessible text tree.
        super().paintEvent(event)

    def _control_at(self, point):
        return next((href for role, rect, _color, href in self.summary_decorations()
                     if role == "verdict" and rect.contains(point)), None)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        hovered = self._control_at(event.position())
        if hovered != self._summary_hovered:
            self._summary_hovered = hovered
            self.viewport().update()
        if hovered:
            self.viewport().setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def leaveEvent(self, event):
        self._summary_hovered = None
        self.viewport().update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        href = self._control_at(event.position())
        if event.button() == QtCore.Qt.MouseButton.LeftButton and href:
            self._summary_pressed = href
            self.viewport().update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        pressed = self._summary_pressed
        self._summary_pressed = None
        if event.button() == QtCore.Qt.MouseButton.LeftButton and pressed:
            self.viewport().update()
            if self._control_at(event.position()) == pressed:
                self.anchorClicked.emit(QtCore.QUrl(pressed))
            event.accept()
            return
        super().mouseReleaseEvent(event)
