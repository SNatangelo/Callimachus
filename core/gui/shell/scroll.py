# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Place compact-page scrolling at the window edge, outside the content card."""


def install_edge_scrollbar(page, scrolls, QtCore, QtWidgets, *, compact_when=None):
    from shiboken6 import isValid
    owner = page.window()

    class EdgeScrollbar(QtCore.QObject):
        def __init__(self):
            super().__init__(page)
            self._disposed = False
            page.destroyed.connect(self.dispose)
            if owner is not page:
                owner.destroyed.connect(self.dispose)
            self.scrolls = []
            self.policies = {}
            self.active = None
            self.compact = None
            self.bar = QtWidgets.QScrollBar(QtCore.Qt.Orientation.Vertical, page)
            self.bar.setObjectName("compactPageScrollbar")
            self.bar.hide()
            self.bar.valueChanged.connect(self.set_value)
            self.timer = QtCore.QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(self.sync)
            page.installEventFilter(self)
            for scroll in scrolls:
                self.add_scroll(scroll)

        def dispose(self, *_args):
            self._disposed = True
            self.active = None
            timer = getattr(self, "timer", None)
            if timer is not None and isValid(timer):
                timer.stop()

        def _live(self):
            if self._disposed:
                return False
            if not all(isValid(item) for item in (self, page, owner, self.bar, self.timer)):
                self.dispose()
                return False
            return True

        def add_scroll(self, scroll):
            if not self._live():
                return
            self.scrolls.append(scroll)
            self.policies[scroll] = scroll.verticalScrollBarPolicy()
            scroll.installEventFilter(self)
            scroll.viewport().installEventFilter(self)
            source = scroll.verticalScrollBar()
            source.rangeChanged.connect(lambda *_args, active_scroll=scroll:
                                        self.range_changed(active_scroll))
            source.valueChanged.connect(self.source_value)
            self.schedule()

        def schedule(self, *_args):
            if not self._live():
                return
            self.timer.start(0)

        def source_value(self, *_args):
            if not self._live():
                return
            if self.active is not None and isValid(self.active):
                with QtCore.QSignalBlocker(self.bar):
                    self.bar.setValue(self.active.verticalScrollBar().value())

        def copy_state(self):
            if not self._live():
                return
            if self.active is None or not isValid(self.active):
                return
            source = self.active.verticalScrollBar()
            with QtCore.QSignalBlocker(self.bar):
                self.bar.setRange(source.minimum(), source.maximum())
                self.bar.setPageStep(source.pageStep())
                self.bar.setSingleStep(source.singleStep())
                self.bar.setValue(source.value())

        def range_changed(self, scroll):
            if not self._live():
                return
            if scroll is self.active:
                self.copy_state()
            self.schedule()

        def set_value(self, value):
            if not self._live():
                return
            if self.active is not None and isValid(self.active):
                self.active.verticalScrollBar().setValue(value)

        def sync(self):
            if not self._live():
                return
            self.scrolls = [scroll for scroll in self.scrolls if isValid(scroll)]
            self.policies = {scroll: policy for scroll, policy in self.policies.items() if isValid(scroll)}
            compact = (
                bool(compact_when()) if compact_when is not None
                else page.width() < 640 or page.height() < 480
            )
            if compact:
                if self.compact is not True:
                    self.policies = {
                        scroll: scroll.verticalScrollBarPolicy()
                        for scroll in self.scrolls
                    }
                for scroll in self.scrolls:
                    policy = QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                    if scroll.verticalScrollBarPolicy() != policy:
                        scroll.setVerticalScrollBarPolicy(policy)
            elif self.compact is True:
                for scroll in self.scrolls:
                    suppressed = QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                    if scroll.verticalScrollBarPolicy() == suppressed:
                        policy = self.policies[scroll]
                        if policy != suppressed:
                            scroll.setVerticalScrollBarPolicy(policy)
            self.compact = compact
            self.active = next((scroll for scroll in reversed(self.scrolls)
                                if scroll.isVisibleTo(page)), None)
            if not compact or self.active is None or not page.isVisible():
                self.bar.hide()
                return
            source = self.active.verticalScrollBar()
            self.copy_state()
            self.bar.setAccessibleName(self.active.verticalScrollBar().accessibleName())
            top = self.active.mapTo(page, QtCore.QPoint()).y()
            bottom = min(page.height(), top + self.active.height())
            width = self.bar.sizeHint().width()
            self.bar.setGeometry(page.width() - width, max(0, top),
                                 width, max(0, bottom - max(0, top)))
            self.bar.setVisible(source.maximum() > source.minimum())
            if self.bar.isVisible():
                self.bar.raise_()

        def eventFilter(self, watched, event):
            if not self._live():
                return False
            kind = event.type()
            if (compact_when is not None
                    and kind in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Move)
                    and watched is page):
                self.sync()
            elif (compact_when is not None
                  and kind in (QtCore.QEvent.Type.Show, QtCore.QEvent.Type.Hide)
                  and bool(compact_when()) != self.compact):
                self.sync()
            elif kind in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Move,
                          QtCore.QEvent.Type.Show, QtCore.QEvent.Type.Hide,
                          QtCore.QEvent.Type.LayoutRequest):
                self.schedule()
            return False

    return EdgeScrollbar()
