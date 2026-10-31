# core/gui/shell/navigation.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Responsive presentation of the desktop's existing navigation panels."""
from __future__ import annotations


_ANIMATION_DURATION_MS = 200


def set_back_button_presentation(button, QtCore, QtGui, *, label, dark=False,
                                 active=True, text=""):
    """Use one Back affordance, restoring the original control outside Back mode."""
    original = getattr(button, "_atlas_back_original", None)
    if not active:
        if original is not None:
            minimum, maximum, icon_size, stylesheet, original_text, tool_style = original
            button.setMinimumSize(minimum)
            button.setMaximumSize(maximum)
            button.setIconSize(icon_size)
            button.setText(original_text)
            if tool_style is not None:
                button.setToolButtonStyle(tool_style)
            button.setProperty("atlasBackButton", None)
            if button.styleSheet() != stylesheet:
                button.setStyleSheet(stylesheet)
            del button._atlas_back_original
        return
    if original is None:
        button._atlas_back_original = (
            QtCore.QSize(button.minimumSize()), QtCore.QSize(button.maximumSize()),
            QtCore.QSize(button.iconSize()), button.styleSheet(), button.text(),
            button.toolButtonStyle() if hasattr(button, "toolButtonStyle") else None,
        )
    from core.gui.atlas_icons import icon

    accent = "#7ab4ff" if dark else "#175fe6"
    foreground = "#edf3ff" if dark else "#11183c"
    line = "#34465e" if dark else "#c9d7eb"
    hover = "#20334d" if dark else "#edf4ff"
    pressed = "#2c4567" if dark else "#dceaff"
    button.setProperty("atlasBackButton", True)
    button.setText(text)
    if hasattr(button, "setToolButtonStyle"):
        button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            if text else QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly
        )
    button.setToolTip(label)
    button.setAccessibleName(label)
    button.setIcon(icon(QtCore, QtGui, "arrow-left", color=accent, size=20))
    button.setIconSize(QtCore.QSize(20, 20))
    if text:
        button.setMinimumWidth(44)
        button.setMaximumWidth(16777215)
        button.setMinimumHeight(44)
        button.setMaximumHeight(16777215)
    else:
        button.setFixedSize(44, 44)
    padding = "6px 12px" if text else "0"
    stylesheet = (
        f"QAbstractButton {{ background: transparent; color: {foreground}; border: 1px solid {line}; "
        f"border-radius: 6px; padding: {padding}; font-weight: 600; }}"
        f"QAbstractButton:hover {{ background: {hover}; border-color: {accent}; }}"
        f"QAbstractButton:pressed {{ background: {pressed}; border-color: {accent}; }}"
        f"QAbstractButton:focus {{ border: 2px solid {accent}; }}"
        "QAbstractButton:disabled { border-color: transparent; }"
    )
    if button.styleSheet() != stylesheet:
        button.setStyleSheet(stylesheet)


def install_outside_click_dismissal(overlay, toggle_button, dismiss,
                                    QtCore, QtWidgets, *, active_when=None):
    """Dismiss an open overlay on a press outside it within its owning window."""
    class OutsideClickDismissal(QtCore.QObject):
        def __init__(self):
            super().__init__(overlay)
            self._owner_anchor = toggle_button
            self._overlay = overlay
            self._toggle_button = toggle_button
            self._dismiss = dismiss
            self._listening = False
            toggle_button.destroyed.connect(self.deleteLater)
            overlay.installEventFilter(self)
            self._listen(overlay.isVisible())

        def _listen(self, enabled):
            app = QtWidgets.QApplication.instance()
            if app is not None and enabled != self._listening:
                if enabled:
                    app.installEventFilter(self)
                else:
                    app.removeEventFilter(self)
                self._listening = enabled

        def eventFilter(self, watched, event):
            kind = event.type()
            if watched is self._overlay:
                if kind == QtCore.QEvent.Type.Show:
                    self._listen(True)
                elif kind == QtCore.QEvent.Type.Hide:
                    self._listen(False)
            if (kind == QtCore.QEvent.Type.MouseButtonPress
                  and isinstance(watched, QtWidgets.QWidget)
                  and watched.window() == self._owner_anchor.window()
                  and (active_when is None or active_when())
                  and self._overlay.isVisible()):
                inside_overlay = (
                    watched is self._overlay
                    or self._overlay.isAncestorOf(watched)
                )
                on_toggle = (
                    watched is self._toggle_button
                    or self._toggle_button.isAncestorOf(watched)
                )
                if not inside_overlay and not on_toggle:
                    self._dismiss()
                    return True
            return False

    return OutsideClickDismissal()


def install_sidebar_toggle(
    splitter, panel, button, QtCore, QtWidgets, *,
    minimum_content_width=560, preferred_width=232,
    compact_only=False, edge_drawer=False, cover_header=False, content_height=False,
    available_width=None,
):
    """Dock on wide pages and reuse the same panel as an overlay on narrow ones."""

    class SidebarToggle(QtCore.QObject):
        def __init__(self):
            super().__init__(splitter)
            self.is_overlay = False
            self._open = False
            self._docked_visible = True
            self._docked_width = preferred_width
            self._minimum = panel.minimumWidth()
            self._maximum = panel.maximumWidth()
            self._index = splitter.indexOf(panel)
            self._changing = False
            self._dock_animating = False
            self._host = splitter.window() if edge_drawer else splitter.parentWidget()
            self._button_home = button.parentWidget() if cover_header else None
            self._button_home_layout = (
                self._button_home.layout() if self._button_home is not None else None
            )
            self._button_home_index = (
                self._button_home_layout.indexOf(button)
                if self._button_home_layout is not None else -1
            )
            self._button_placeholder = None
            self._content_widgets = [panel]
            self._content_widgets.extend(panel.findChildren(QtWidgets.QListWidget))
            self._content_widgets.extend(panel.findChildren(QtWidgets.QAbstractButton))
            self._content_widgets = tuple(dict.fromkeys(self._content_widgets))
            self.overlay = QtWidgets.QFrame(self._host)
            self.overlay.setObjectName("sidebarOverlay")
            if edge_drawer:
                self.overlay.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
                self.overlay.setAttribute(
                    QtCore.Qt.WidgetAttribute.WA_StyledBackground, True,
                )
                if not cover_header:
                    self.overlay.setStyleSheet(
                        "QFrame#sidebarOverlay { background-color: palette(window); "
                        "border: none; border-right: 1px solid palette(mid); "
                        "border-radius: 0px; }"
                    )
            overlay_layout = QtWidgets.QVBoxLayout(self.overlay)
            overlay_layout.setContentsMargins(0, 0, 0, 0)
            overlay_layout.setSpacing(0)
            self._edge_button_row = None
            if cover_header:
                self._edge_button_row = QtWidgets.QWidget(self.overlay)
                self._edge_button_row.setObjectName("sidebarOverlayToggleRow")
                button_row_layout = QtWidgets.QHBoxLayout(self._edge_button_row)
                button_row_layout.setContentsMargins(8, 4, 8, 4)
                button_row_layout.addStretch(1)
                self._edge_button_row.hide()
                self._sync_cover_header_surface()
            self.overlay.hide()
            self._overlay_animation = QtCore.QPropertyAnimation(
                self.overlay, b"geometry", self
            )
            self._overlay_animation.setDuration(_ANIMATION_DURATION_MS)
            self._overlay_animation.finished.connect(self._finish_overlay_animation)
            self._dock_animation = QtCore.QPropertyAnimation(
                panel, b"maximumWidth", self
            )
            self._dock_animation.setDuration(_ANIMATION_DURATION_MS)
            self._dock_animation.finished.connect(self._finish_dock_animation)
            self._timer = QtCore.QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._sync)
            button.setCheckable(True)
            button.clicked.connect(self.toggle)
            self.overlay.installEventFilter(self)
            for widget in (splitter, self._host, *self._content_widgets):
                widget.installEventFilter(self)
                if isinstance(widget, QtWidgets.QListWidget):
                    widget.itemChanged.connect(lambda *_args: self.sync())
            self._outside_dismissal = install_outside_click_dismissal(
                self.overlay, button, self.close_overlay,
                QtCore, QtWidgets,
            )
            splitter.splitterMoved.connect(self._remember_width)
            self.sync()

        def sync(self):
            self._timer.start(0)

        def _remember_width(self, *_args):
            if (not self.is_overlay and not self._changing and not self._dock_animating
                    and self._docked_visible):
                width = splitter.sizes()[self._index]
                if width > 0:
                    self._docked_width = width

        def _set_button(self, checked):
            blocked = button.blockSignals(True)
            button.setChecked(checked)
            button.blockSignals(blocked)

        def _animation_running(self, animation):
            return animation.state() == QtCore.QAbstractAnimation.State.Running

        def _offscreen_geometry(self, target):
            return QtCore.QRect(target.x() - target.width(), target.y(),
                                target.width(), target.height())

        def _layout_content_width(self, layout):
            widths = []
            for index in range(layout.count()):
                item = layout.itemAt(index)
                widget = item.widget()
                if widget is not None:
                    if not widget.isHidden():
                        widths.append(self._widget_content_width(widget))
                elif item.layout() is not None:
                    widths.append(self._layout_content_width(item.layout()))
            margins = layout.contentsMargins()
            if isinstance(layout, QtWidgets.QBoxLayout) and layout.direction() in (
                    QtWidgets.QBoxLayout.Direction.LeftToRight,
                    QtWidgets.QBoxLayout.Direction.RightToLeft):
                return (sum(widths) + max(0, len(widths) - 1) * max(0, layout.spacing())
                        + margins.left() + margins.right())
            return max(widths, default=0) + margins.left() + margins.right()

        def _widget_content_width(self, widget):
            if isinstance(widget, QtWidgets.QListWidget):
                row_widths = [
                    widget.sizeHintForIndex(widget.indexFromItem(item)).width()
                    for index in range(widget.count())
                    if not (item := widget.item(index)).isHidden()
                ]
                viewport_chrome = max(0, widget.width() - widget.viewport().width())
                if content_height:
                    vertical_bar = widget.verticalScrollBar()
                    if vertical_bar.isVisible():
                        viewport_chrome = max(0, viewport_chrome - vertical_bar.width())
                    viewport_chrome += vertical_bar.sizeHint().width()
                return max(row_widths, default=0) + viewport_chrome
            if isinstance(widget, QtWidgets.QScrollArea) and widget.widget() is not None:
                content = widget.widget()
                content_width = max(
                    self._widget_content_width(content),
                    content.minimumSizeHint().width(), content.minimumWidth(),
                )
                viewport_chrome = max(0, widget.width() - widget.viewport().width())
                return content_width + viewport_chrome
            layout = widget.layout()
            if layout is not None:
                return self._layout_content_width(layout)
            return max(0, widget.sizeHint().width())

        def _sync_cover_header_surface(self):
            if not cover_header:
                return
            navigation_list = panel.findChild(QtWidgets.QListWidget)
            surface = (
                navigation_list.palette().color(
                    navigation_list.backgroundRole()
                ).name()
                if navigation_list is not None else "#ffffff"
            )
            stylesheet = (
                f"QFrame#sidebarOverlay {{ background-color: {surface}; "
                "border: none; border-right: 1px solid palette(mid); "
                "border-radius: 0px; }"
                f"QWidget#sidebarOverlayToggleRow {{ background-color: {surface}; "
                "border: none; }"
            )
            if self.overlay.styleSheet() != stylesheet:
                self.overlay.setStyleSheet(stylesheet)

        def _cover_header_minimum_width(self):
            layout = self._button_home_layout
            if layout is None:
                return preferred_width
            prefix_widgets = []
            logo_seen = False
            for index in range(layout.count()):
                widget = layout.itemAt(index).widget()
                if widget is None:
                    break
                if widget.isHidden():
                    continue
                prefix_widgets.append(widget)
                if widget.objectName() == "brandLogo":
                    logo_seen = True
                elif logo_seen and isinstance(widget, QtWidgets.QLabel):
                    break
            if not logo_seen:
                return preferred_width
            margins = layout.contentsMargins()
            return (
                margins.left()
                + sum(max(widget.width(), widget.sizeHint().width())
                      for widget in prefix_widgets)
                + max(0, len(prefix_widgets) - 1) * max(0, layout.spacing())
                + margins.right()
                + 12
            )

        def _cover_header_width_cap(self):
            available = max(0, self._host.width())
            controls = self._host.findChild(QtWidgets.QWidget, "windowControls")
            if controls is not None and controls.isVisibleTo(self._host):
                controls_left = controls.mapTo(self._host, QtCore.QPoint()).x()
                available = min(available, max(0, controls_left - 8))
            return available

        def _overlay_target_geometry(self):
            self._sync_cover_header_surface()
            if cover_header:
                target = QtCore.QRect(self._host.rect())
            elif edge_drawer:
                origin = splitter.mapTo(self._host, QtCore.QPoint())
                target = QtCore.QRect(origin, splitter.size())
                target.moveLeft(0)
            else:
                target = QtCore.QRect(splitter.geometry())
            natural_width = max(preferred_width, self._widget_content_width(panel))
            if content_height:
                overlay_margins = self.overlay.contentsMargins()
                natural_width += overlay_margins.left() + overlay_margins.right()
            available_width = splitter.width()
            if cover_header:
                natural_width = max(natural_width, self._cover_header_minimum_width())
                available_width = self._cover_header_width_cap()
            target.setWidth(min(natural_width, available_width))
            if content_height:
                margins = panel.contentsMargins()
                row_heights = [
                    panel.sizeHintForRow(index)
                    for index in range(panel.count())
                    if not panel.item(index).isHidden()
                ]
                content_height_px = (
                    sum(row_heights)
                    + max(0, len(row_heights) - 1) * max(0, panel.spacing())
                    + 2 * panel.frameWidth()
                    + margins.top()
                    + margins.bottom()
                )
                target.setHeight(min(target.height(), content_height_px))
            return target

        def _move_toggle_to_drawer(self):
            if not cover_header or button.parentWidget() is self._edge_button_row:
                return
            if self._button_home_layout is not None and self._button_placeholder is None:
                placeholder = QtWidgets.QWidget(self._button_home)
                placeholder.setObjectName("navigationTogglePlaceholder")
                placeholder.setAttribute(
                    QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True,
                )
                placeholder.setStyleSheet(
                    "QWidget#navigationTogglePlaceholder { "
                    "background: transparent; border: none; }"
                )
                placeholder.setFixedSize(button.sizeHint())
                self._button_home_layout.insertWidget(self._button_home_index, placeholder)
                self._button_placeholder = placeholder
            if self._button_home_layout is not None:
                self._button_home_layout.removeWidget(button)
            button.setParent(self._edge_button_row)
            self._edge_button_row.layout().addWidget(button)
            self.overlay.layout().insertWidget(0, self._edge_button_row)
            self._edge_button_row.show()
            button.show()

        def _restore_toggle_home(self):
            if not cover_header or button.parentWidget() is self._button_home:
                return
            if self.overlay.layout().indexOf(self._edge_button_row) >= 0:
                self.overlay.layout().removeWidget(self._edge_button_row)
            self._edge_button_row.hide()
            self._edge_button_row.layout().removeWidget(button)
            button.setParent(self._button_home)
            if self._button_home_layout is not None:
                index = max(0, min(self._button_home_index, self._button_home_layout.count()))
                self._button_home_layout.insertWidget(index, button)
                if self._button_placeholder is not None:
                    self._button_home_layout.removeWidget(self._button_placeholder)
                    self._button_placeholder.hide()
                    self._button_placeholder.deleteLater()
                    self._button_placeholder = None
            button.show()

        def _raise_overlay(self):
            self.overlay.raise_()
            if cover_header:
                outline = getattr(self._host, "_atlas_window_outline", None)
                if outline is not None:
                    outline.raise_()

        def _set_splitter_width(self, width):
            sizes = splitter.sizes()
            if len(sizes) < 2:
                return
            available = max(0, splitter.width() - splitter.handleWidth())
            width = min(max(0, int(width)), available)
            sizes[self._index] = width
            sizes[1 - self._index] = available - width
            splitter.setSizes(sizes)

        def _restore_docked_constraints(self):
            panel.setMinimumWidth(self._minimum)
            panel.setMaximumWidth(self._maximum)

        def _animate_docked_visibility(self, visible):
            if self._animation_running(self._dock_animation):
                self._dock_animation.stop()
            current_width = panel.width() if panel.isVisible() else 0
            panel.setMinimumWidth(0)
            if visible:
                panel.setVisible(True)
                panel.setMaximumWidth(current_width)
                self._set_splitter_width(current_width)
                target_width = min(self._docked_width,
                                   max(0, splitter.width() - splitter.handleWidth()))
                easing = QtCore.QEasingCurve.Type.OutCubic
            else:
                panel.setVisible(True)
                current_width = max(0, current_width)
                panel.setMaximumWidth(current_width)
                target_width = 0
                easing = QtCore.QEasingCurve.Type.InOutCubic
            self._dock_animating = True
            self._dock_animation.setEasingCurve(easing)
            self._dock_animation.setStartValue(current_width)
            self._dock_animation.setEndValue(target_width)
            self._dock_animation.start()

        def _finish_dock_animation(self):
            self._dock_animating = False
            self._restore_docked_constraints()
            if self._docked_visible and not self.is_overlay:
                panel.setVisible(True)
                self._set_splitter_width(self._docked_width)
            else:
                panel.hide()

        def _cancel_dock_animation(self):
            if self._animation_running(self._dock_animation):
                self._dock_animation.stop()
            self._dock_animating = False
            self._restore_docked_constraints()

        def _start_overlay_animation(self, opening):
            if self._animation_running(self._overlay_animation):
                self._overlay_animation.stop()
            if opening:
                self._move_toggle_to_drawer()
            target = self._overlay_target_geometry()
            was_visible = self.overlay.isVisible()
            if opening:
                if was_visible:
                    current = self.overlay.geometry()
                    start = QtCore.QRect(current.x(), target.y(),
                                         target.width(), target.height())
                else:
                    start = self._offscreen_geometry(target)
                end = target
                self._open = True
                self.overlay.setGeometry(start)
                self.overlay.show()
                self._raise_overlay()
                focus_target = panel
                if cover_header:
                    focus_target = next(
                        (
                            widget for widget in self._content_widgets
                            if widget.focusPolicy() != QtCore.Qt.FocusPolicy.NoFocus
                        ),
                        panel,
                    )
                focus_target.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
                self._overlay_animation.setEasingCurve(
                    QtCore.QEasingCurve.Type.OutCubic
                )
            else:
                if not was_visible:
                    self.overlay.setGeometry(target)
                    self.overlay.show()
                start = self.overlay.geometry()
                end = self._offscreen_geometry(target)
                self._open = False
                button.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
                self._overlay_animation.setEasingCurve(
                    QtCore.QEasingCurve.Type.InOutCubic
                )
            self._overlay_animation.setStartValue(start)
            self._overlay_animation.setEndValue(end)
            self._overlay_animation.start()

        def _finish_overlay_animation(self):
            target = self._overlay_target_geometry()
            self.overlay.setGeometry(target)
            if self.is_overlay and self._open:
                self.overlay.show()
                self._raise_overlay()
                self._set_button(True)
            else:
                self.overlay.hide()
                self._set_button(False)
                self._restore_toggle_home()
                button.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

        def _sync_overlay_geometry(self):
            target = self._overlay_target_geometry()
            if self._animation_running(self._overlay_animation):
                end = target if self._open else self._offscreen_geometry(target)
                self._overlay_animation.setEndValue(end)
                current = self.overlay.geometry()
                self.overlay.setGeometry(QtCore.QRect(
                    current.x(), target.y(), target.width(), target.height()
                ))
            else:
                self.overlay.setGeometry(target)

        def _sync(self):
            if self._changing or not splitter.isVisible():
                return
            self._changing = True
            try:
                # Use logical pixels and font metrics, so remote desktops and
                # larger accessibility fonts need no physical-DPI assumptions.
                pixels = splitter.font().pixelSize()
                scale = max(1.0, pixels / 16.0) if pixels > 0 else 1.0
                required = int(minimum_content_width * scale) + self._docked_width
                page_width = splitter.width() if available_width is None else available_width()
                narrow = page_width < required
                if narrow != self.is_overlay:
                    self._overlay_animation.stop()
                    self._cancel_dock_animation()
                    self._open = False
                    if narrow:
                        panel.hide()
                        panel.setParent(self.overlay)
                        panel.setMinimumWidth(0)
                        panel.setMaximumWidth(16777215)
                        self.overlay.layout().addWidget(panel)
                        panel.show()
                    else:
                        self._restore_toggle_home()
                        self.overlay.hide()
                        self.overlay.setGeometry(self._overlay_target_geometry())
                        panel.hide()
                        self.overlay.layout().removeWidget(panel)
                        panel.setParent(splitter)
                        panel.setMinimumWidth(self._minimum)
                        panel.setMaximumWidth(self._maximum)
                        splitter.insertWidget(self._index, panel)
                        if self._docked_visible:
                            panel.setMinimumWidth(0)
                            panel.setMaximumWidth(0)
                            panel.show()
                            self._set_splitter_width(0)
                        else:
                            panel.hide()
                    self.is_overlay = narrow
                    if not narrow and self._docked_visible:
                        self._animate_docked_visibility(True)
                elif self.is_overlay:
                    self._sync_overlay_geometry()
                    if self._open or self._animation_running(self._overlay_animation):
                        self.overlay.show()
                    else:
                        self.overlay.hide()
                    if self._open:
                        self._raise_overlay()
                elif self._animation_running(self._dock_animation) and self._docked_visible:
                    target_width = min(self._docked_width,
                                       max(0, splitter.width() - splitter.handleWidth()))
                    self._dock_animation.setEndValue(target_width)
                self._set_button(self._open if self.is_overlay else self._docked_visible)
                if compact_only:
                    button.setVisible(self.is_overlay)
                    sync_brand = getattr(button.window(), "_sync_brand_logo_inset", None)
                    if sync_brand is not None:
                        sync_brand()
            finally:
                self._changing = False

        def toggle(self, *_args):
            self._sync()
            if compact_only and not self.is_overlay:
                return
            if self.is_overlay:
                self._start_overlay_animation(not self._open)
            else:
                if self._docked_visible:
                    self._remember_width()
                self._docked_visible = not self._docked_visible
                self._animate_docked_visibility(self._docked_visible)
            self._set_button(self._open if self.is_overlay else self._docked_visible)

        def close_overlay(self):
            if self.is_overlay and self._open:
                self._start_overlay_animation(False)
                self._set_button(False)

        def eventFilter(self, watched, event):
            kind = event.type()
            if (kind == QtCore.QEvent.Type.KeyPress
                    and event.key() == QtCore.Qt.Key.Key_Escape
                    and self.is_overlay and self._open):
                self.close_overlay()
                return True
            if (watched in (splitter, self._host)
                    or watched in self._content_widgets):
                if kind in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Show,
                            QtCore.QEvent.Type.Hide, QtCore.QEvent.Type.Move,
                            QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange,
                            QtCore.QEvent.Type.LayoutRequest):
                    if (edge_drawer and watched is splitter
                            and kind in (QtCore.QEvent.Type.Resize,
                                         QtCore.QEvent.Type.Move)):
                        self._sync()
                    else:
                        self.sync()
            return False

    controller = SidebarToggle()
    return controller
