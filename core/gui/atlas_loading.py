# core/gui/atlas_loading.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Lightweight painted loading bars for the optional Qt interfaces."""
from __future__ import annotations


def make_loading_bar(
    QtCore,
    QtGui,
    QtWidgets,
    parent,
    dark_getter,
    *,
    duration: int = 900,
):
    """Create a transparent pulse bar and its animation without graphics effects."""

    class _PulseBar(QtWidgets.QFrame):
        def __init__(self, owner):
            super().__init__(owner)
            self._pulse = 0.6
            self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)

        def _get_pulse(self):
            return self._pulse

        def _set_pulse(self, value):
            self._pulse = min(1.0, max(0.0, float(value)))
            self.update()

        pulse = QtCore.Property(float, _get_pulse, _set_pulse)

        def paintEvent(self, event):
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            color = QtGui.QColor("#40516b" if dark_getter() else "#d7e2f1")
            color.setAlphaF(self._pulse)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(color)
            rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            radius = min(7.0, rect.height() / 2.0)
            painter.drawRoundedRect(rect, radius, radius)

    bar = _PulseBar(parent)
    animation = QtCore.QPropertyAnimation(bar, b"pulse", bar)
    animation.setDuration(max(1, int(duration)))
    animation.setStartValue(0.35)
    animation.setKeyValueAt(0.5, 0.9)
    animation.setEndValue(0.35)
    animation.setLoopCount(-1)
    return bar, animation
