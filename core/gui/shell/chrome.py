# core/gui/shell/chrome.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Application-owned window controls, shared by Windows, Linux and macOS."""
from __future__ import annotations
import ctypes
import sys
import weakref
import warnings
from ctypes import wintypes


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _NccalcsizeParams(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


_WS_POPUP = 0x80000000
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_WS_SYSMENU = 0x00080000
_WS_MINIMIZEBOX = 0x00020000
_WS_MAXIMIZEBOX = 0x00010000
_WS_OVERLAPPEDWINDOW = (
    _WS_CAPTION | _WS_THICKFRAME | _WS_SYSMENU
    | _WS_MINIMIZEBOX | _WS_MAXIMIZEBOX
)

_WM_NCCALCSIZE = 0x0083
_WM_NCHITTEST = 0x0084
_HTCLIENT = 1
_HTCAPTION = 2
_HTLEFT = 10
_HTRIGHT = 11
_HTTOP = 12
_HTTOPLEFT = 13
_HTTOPRIGHT = 14
_HTBOTTOM = 15
_HTBOTTOMLEFT = 16
_HTBOTTOMRIGHT = 17
_RESIZE_BORDER = 7
_MAC_NATIVE_LEADING_RESERVE = 72


def _paint_smooth_corners(painter, QtCore, QtGui, rect, radius=6):
    """Alpha-clip only the corner pixels after the window's children paint.

    A QRegion is binary and cannot antialias an outer window edge. Small
    high-DPI alpha tiles retain coverage without compositing the entire UI.
    """
    import math

    radius = min(radius, rect.width() / 2, rect.height() / 2)
    if radius <= 0:
        return
    ratio = painter.device().devicePixelRatioF()
    pixels = math.ceil(radius * ratio)
    extent = pixels / ratio
    painter.save()
    painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_DestinationIn)
    for x in (0, rect.width() - extent):
        for y in (0, rect.height() - extent):
            tile = QtGui.QImage(pixels, pixels, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            tile.setDevicePixelRatio(ratio)
            tile.fill(QtCore.Qt.GlobalColor.transparent)
            mask = QtGui.QPainter(tile)
            mask.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            mask.setPen(QtCore.Qt.PenStyle.NoPen)
            mask.setBrush(QtCore.Qt.GlobalColor.white)
            mask.translate(-x, -y)
            mask.drawRoundedRect(QtCore.QRectF(rect), radius, radius)
            mask.end()
            painter.drawImage(QtCore.QPointF(x, y), tile)
    painter.restore()


def _native_frame_style(style: int) -> int:
    """Keep the Qt client window while restoring Windows' snap/resize styles."""
    return (int(style) & 0xFFFFFFFF & ~_WS_POPUP) | _WS_OVERLAPPEDWINDOW


def _apply_windows_frame(target, *, user32=None, platform_name=None) -> bool:
    """Add the native overlapped-window styles without changing Qt flags."""
    if (platform_name or sys.platform) != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        if user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_style = getattr(user32, "GetWindowLongPtrW", None)
        set_style = getattr(user32, "SetWindowLongPtrW", None)
        if get_style is None:
            get_style = user32.GetWindowLongW
        if set_style is None:
            set_style = user32.SetWindowLongW
        hwnd = wintypes.HWND(int(target.winId()))
        try:
            get_style.argtypes = (wintypes.HWND, ctypes.c_int)
            get_style.restype = ctypes.c_ssize_t
            set_style.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
            set_style.restype = ctypes.c_ssize_t
            user32.SetWindowPos.argtypes = (
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT,
            )
            user32.SetWindowPos.restype = wintypes.BOOL
        except AttributeError:
            # Test doubles do not expose ctypes function metadata.
            pass

        current = int(get_style(hwnd, -16)) & 0xFFFFFFFF
        updated = _native_frame_style(current)
        set_style(hwnd, -16, updated)
        frame_changed = (
            0x0001  # SWP_NOSIZE
            | 0x0002  # SWP_NOMOVE
            | 0x0004  # SWP_NOZORDER
            | 0x0010  # SWP_NOACTIVATE
            | 0x0020  # SWP_FRAMECHANGED
        )
        return bool(user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, frame_changed))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _native_message_hwnd(message) -> int:
    """Read the HWND field exposed by ctypes.wintypes.MSG."""
    return int(message.hWnd or 0)


def _set_maximized_work_area(message, *, user32=None) -> bool:
    """Use the monitor work area as the client rect for maximized windows."""
    try:
        if user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        try:
            user32.IsZoomed.argtypes = (wintypes.HWND,)
            user32.IsZoomed.restype = wintypes.BOOL
            user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = (
                wintypes.HANDLE, ctypes.POINTER(_MonitorInfo),
            )
            user32.GetMonitorInfoW.restype = wintypes.BOOL
        except AttributeError:
            # Test doubles do not expose ctypes function metadata.
            pass

        hwnd = _native_message_hwnd(message)
        if not user32.IsZoomed(hwnd):
            return True
        monitor = user32.MonitorFromWindow(hwnd, 2)  # nearest monitor
        if not monitor:
            return False
        monitor_info = _MonitorInfo()
        monitor_info.cbSize = ctypes.sizeof(monitor_info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(monitor_info)):
            return False

        if message.wParam:
            params = ctypes.cast(
                int(message.lParam), ctypes.POINTER(_NccalcsizeParams),
            ).contents
            client_rect = params.rgrc[0]
        else:
            client_rect = ctypes.cast(
                int(message.lParam), ctypes.POINTER(wintypes.RECT),
            ).contents
        work_rect = monitor_info.rcWork
        client_rect.left = work_rect.left
        client_rect.top = work_rect.top
        client_rect.right = work_rect.right
        client_rect.bottom = work_rect.bottom
        return True
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _windows_hit_test(window, header, global_pos, QtCore, QtWidgets):
    """Return a Win32 hit-test code for a main window's custom frame."""
    point = window.mapFromGlobal(global_pos)
    bounds = window.rect()
    if not bounds.contains(point):
        return None

    if not window.isMaximized() and not window.isFullScreen():
        x, y = point.x(), point.y()
        left = x < _RESIZE_BORDER
        right = x >= bounds.width() - _RESIZE_BORDER
        top = y < _RESIZE_BORDER
        bottom = y >= bounds.height() - _RESIZE_BORDER
        if top and left:
            return _HTTOPLEFT
        if top and right:
            return _HTTOPRIGHT
        if bottom and left:
            return _HTBOTTOMLEFT
        if bottom and right:
            return _HTBOTTOMRIGHT
        if left:
            return _HTLEFT
        if right:
            return _HTRIGHT
        if top:
            return _HTTOP
        if bottom:
            return _HTBOTTOM

    header_origin = header.mapTo(window, QtCore.QPoint(0, 0))
    header_rect = QtCore.QRect(header_origin, header.size())
    if header_rect.contains(point):
        navigation = getattr(window, "_atlas_navigation_controller", None)
        if navigation is not None and navigation.is_overlay and navigation.overlay.isVisible():
            # The main drawer reparents its toggle out of this header. Keep
            # header presses client-side until it closes, so the moved toggle
            # and outside-dismissal receive clicks instead of native drag input.
            return _HTCLIENT
        for control in header.findChildren(QtWidgets.QAbstractButton):
            if not control.isVisible():
                continue
            control_origin = control.mapToGlobal(QtCore.QPoint(0, 0))
            control_rect = QtCore.QRect(control_origin, control.size())
            if control_rect.contains(global_pos):
                return _HTCLIENT
        return _HTCAPTION
    return _HTCLIENT


def _qt_resize_edges(window, position, QtCore):
    """Return the resizable Qt window-system edges at a client position."""
    if window.isMaximized() or window.isFullScreen():
        return None
    bounds = window.rect()
    if not bounds.contains(position):
        return None

    minimum = window.minimumSize()
    maximum = window.maximumSize()
    horizontal = minimum.width() < maximum.width()
    vertical = minimum.height() < maximum.height()
    x, y = position.x(), position.y()
    border = 12 if sys.platform.startswith("linux") else _RESIZE_BORDER
    horizontal_border = min(border, bounds.width() // 2)
    vertical_border = min(border, bounds.height() // 2)
    edges = None
    for active, edge in (
        (horizontal and x < horizontal_border, QtCore.Qt.Edge.LeftEdge),
        (
            horizontal and x >= bounds.width() - horizontal_border,
            QtCore.Qt.Edge.RightEdge,
        ),
        (vertical and y < vertical_border, QtCore.Qt.Edge.TopEdge),
        (
            vertical and y >= bounds.height() - vertical_border,
            QtCore.Qt.Edge.BottomEdge,
        ),
    ):
        if active:
            edges = edge if edges is None else edges | edge
    return edges


def configure_style(QtWidgets):
    app = QtWidgets.QApplication.instance()
    if app is None or app.property("atlasStyleReady"):
        return
    class FastTooltips(QtWidgets.QProxyStyle):
        def styleHint(self, hint, option=None, widget=None, returnData=None):
            if hint in {QtWidgets.QStyle.StyleHint.SH_ToolTip_WakeUpDelay, QtWidgets.QStyle.StyleHint.SH_ToolTip_FallAsleepDelay}:
                return 150
            return super().styleHint(hint, option, widget, returnData)
    app._atlas_style = FastTooltips("Fusion")
    app.setStyle(app._atlas_style)
    app.setProperty("atlasStyleReady", True)


def install(window, QtCore, QtGui, QtWidgets, text):
    from core.gui.atlas_icons import icon

    class Chrome(QtCore.QObject):
        def __init__(self):
            super().__init__(window)
            # Static QMessageBox helpers create C++ dialogs whose temporary
            # Python wrappers otherwise disappear between polish and show.
            self.managed = set()
            self.headers = weakref.WeakKeyDictionary()
            self.outlines = weakref.WeakKeyDictionary()
            self.maximize_button = None
            self.native_macos = sys.platform == "darwin"
            self.native_message_fit_events = {
                QtCore.QEvent.Type.Show,
                QtCore.QEvent.Type.Polish,
                QtCore.QEvent.Type.PolishRequest,
                QtCore.QEvent.Type.FontChange,
                QtCore.QEvent.Type.StyleChange,
            }
            for event_name in (
                "ApplicationFontChange",
                "DevicePixelRatioChange",
                "SafeAreaMarginsChange",
                "ScreenChangeInternal",
            ):
                event_type = getattr(QtCore.QEvent.Type, event_name, None)
                if event_type is not None:
                    self.native_message_fit_events.add(event_type)
            QtWidgets.QApplication.instance().installEventFilter(self)
            self.native_event_filter = None

        def manage(self, target):
            if target not in self.managed:
                self.managed.add(target)
                target.destroyed.connect(lambda *_args: self.managed.discard(target))

        def safe_area_margins(self, target):
            handle = target.windowHandle()
            return handle.safeAreaMargins() if handle is not None else QtCore.QMargins()

        def update_main_safe_area(self):
            # Branding is ordinary client content below the native titlebar.
            header = window.brand_logo.parentWidget()
            header.layout().setContentsMargins(10, 6, 10, 6)
            shell = window.centralWidget()
            if shell is not None and shell.layout() is not None:
                layout = shell.layout()
                margins = layout.contentsMargins()
                top = 0 if window.isFullScreen() else 8
                if margins.top() != top:
                    layout.setContentsMargins(margins.left(), top, margins.right(), margins.bottom())

        def install_macos_frame(self, target, *, expanded=False, main=False):
            # Use integer masks: enum inversion may omit unrelated Qt hints.
            cleared = (QtCore.Qt.WindowType.FramelessWindowHint
                       | QtCore.Qt.WindowType.ExpandedClientAreaHint
                       | QtCore.Qt.WindowType.NoTitleBarBackgroundHint)
            flags = QtCore.Qt.WindowType(int(target.windowFlags()) & ~int(cleared))
            flags |= QtCore.Qt.WindowType.WindowCloseButtonHint
            flags |= QtCore.Qt.WindowType.WindowTitleHint
            if expanded:
                flags |= QtCore.Qt.WindowType.ExpandedClientAreaHint
                flags |= QtCore.Qt.WindowType.NoTitleBarBackgroundHint
            target.setWindowFlags(flags)
            if not main:
                target.setAttribute(
                    QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, False,
                )
            if expanded:
                target.setAttribute(
                    QtCore.Qt.WidgetAttribute.WA_LayoutOnEntireRect, True,
                )
            if main:
                target.setUnifiedTitleAndToolBarOnMac(False)
                target.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, False)
                central = target.centralWidget()
                if central is not None:
                    central.setAttribute(QtCore.Qt.WidgetAttribute.WA_LayoutOnEntireRect, False)

        def button(self, parent, key, label, callback):
            button = QtWidgets.QToolButton(parent)
            button.setObjectName("windowClose" if key == "close" else "windowControl")
            button.setProperty("atlasIcon", key)
            button.setIcon(icon(QtCore, QtGui, key, "#e6edf6" if window._dark else "#455374"))
            button.setIconSize(QtCore.QSize(20, 20))
            button.setFixedSize(38, 34)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.clicked.connect(callback)
            return button

        def attach(self, target, header, layout=None, *, main=False, expanded=False):
            self.manage(target)
            if self.native_macos:
                self.install_macos_frame(
                    target, expanded=expanded, main=main,
                )
            else:
                target.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)
            native_windows = (
                main
                and sys.platform == "win32"
                and QtWidgets.QApplication.platformName().lower()
                not in {"offscreen", "minimal"}
            )
            # Keep Qt's layered presentation off the native main HWND; it does
            # not refresh the custom-frame client reliably after maximize.
            if main and not native_windows and not self.native_macos:
                target.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.headers[header] = weakref.ref(target)
            header.installEventFilter(self)
            for label in header.findChildren(QtWidgets.QLabel):
                self.headers[label] = weakref.ref(target)
                label.installEventFilter(self)
            if layout is not None:
                if main and not self.native_macos:
                    # Keep the wordmark centered while the controls touch the
                    # upper-right edge, independently of the header height.
                    # The icon's own padding balances the gap to the wordmark.
                    layout.setContentsMargins(5, 0, 0, 0)
                    controls = QtWidgets.QWidget(header)
                    controls.setObjectName("windowControls")
                    controls_layout = QtWidgets.QHBoxLayout(controls)
                    controls_layout.setContentsMargins(0, 0, 0, 0)
                    controls_layout.setSpacing(0)
                    controls_layout.addWidget(self.button(controls, "window-minimize", text.get("window_minimize", "Minimize"), target.showMinimized))
                    self.maximize_button = self.button(header, "window-maximize", text.get("window_maximize", "Maximize / restore"), self.toggle_maximized)
                    controls_layout.addWidget(self.maximize_button)
                    controls_layout.addWidget(self.button(controls, "close", text.get("close", "Close"), target.close))
                    layout.addWidget(controls, 0, QtCore.Qt.AlignmentFlag.AlignTop)
                elif not main:
                    # Native macOS supplies its wide-view close control, but a
                    # compact dialog still needs an in-content back button.
                    close_button = self.button(
                        header, "close", text.get("close", "Close"),
                        target.close,
                    )
                    layout.addWidget(close_button)
                    if self.native_macos:
                        close_button.hide()
            if isinstance(target, QtWidgets.QDialog) and not self.native_macos:
                target.setSizeGripEnabled(target.minimumSize() != target.maximumSize())
            if not self.native_macos:
                self.add_outline(target)
            if native_windows:
                self.install_windows_frame(target, header)
            if main and not self.native_macos:
                self.update_main_corner_mask(target)

        def add_outline(self, target):
            class Outline(QtWidgets.QFrame):
                def paintEvent(self, event):
                    super().paintEvent(event)
                    if (
                        target is window
                        and target.testAttribute(
                            QtCore.Qt.WidgetAttribute.WA_TranslucentBackground
                        )
                        and not (target.isMaximized() or target.isFullScreen())
                    ):
                        painter = QtGui.QPainter(self)
                        _paint_smooth_corners(painter, QtCore, QtGui, self.rect())
                        painter.end()

            outline = Outline(target)
            # Dialog stylesheet borders do not render while this attribute is set.
            outline.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_NoSystemBackground,
                target is window and sys.platform != "linux",
            )
            outline.setObjectName("windowOutline")
            outline.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            outline.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            self.outlines[target] = weakref.ref(outline)
            target._atlas_window_outline = outline
            self.update_outline(target)

        def update_outline_style(self, outline, target):
            line, width = ("#6b86a6", 2) if window._dark else ("#aebdce", 1)
            radius = (
                0
                if target is window
                and (target.isMaximized() or target.isFullScreen())
                else 6
            )
            stylesheet = (
                "QFrame#windowOutline { background: transparent; "
                f"border: {width}px solid {line}; border-radius: {radius}px; "
                "}"
            )
            if outline.styleSheet() != stylesheet:
                outline.setStyleSheet(stylesheet)

        def update_outline(self, target):
            reference = self.outlines.get(target)
            outline = reference() if reference is not None else None
            if outline is None:
                return
            outline.setGeometry(target.rect())
            self.update_outline_style(outline, target)
            outline.raise_()

        def update_main_corner_mask(self, target):
            if target is not window:
                return
            target.clearMask()
            target._atlas_window_outline.update()

        def install_windows_frame(self, target, header):
            # Realize the HWND before installing the native filter. Calling
            # QWidget.winId() from nativeEventFilter can re-enter Qt while it
            # is creating or updating the window handle.
            target_hwnd = int(target.winId())

            class NativeFrameFilter(QtCore.QAbstractNativeEventFilter):
                def __init__(self):
                    super().__init__()
                    self.target_ref = weakref.ref(target)
                    self.header_ref = weakref.ref(header)
                    self.target_hwnd = target_hwnd

                def nativeEventFilter(self, event_type, message):
                    active_window = self.target_ref()
                    active_header = self.header_ref()
                    if active_window is None or active_header is None:
                        return False, 0
                    try:
                        import ctypes
                        from ctypes import wintypes

                        raw_message = ctypes.cast(
                            int(message), ctypes.POINTER(wintypes.MSG),
                        ).contents
                        if _native_message_hwnd(raw_message) != self.target_hwnd:
                            return False, 0
                        if raw_message.message == _WM_NCCALCSIZE:
                            # Keep normal windows fully client-side. Maximized
                            # windows use the monitor work area, in native pixels.
                            if not _set_maximized_work_area(raw_message):
                                return False, 0
                            return True, 0
                        if raw_message.message == _WM_NCHITTEST:
                            result = _windows_hit_test(
                                active_window, active_header, QtGui.QCursor.pos(),
                                QtCore, QtWidgets,
                            )
                            if result is not None:
                                return True, int(result)
                    except (AttributeError, OSError, TypeError, ValueError):
                        return False, 0
                    return False, 0

            self.native_event_filter = NativeFrameFilter()
            app = QtWidgets.QApplication.instance()
            app.installNativeEventFilter(self.native_event_filter)
            if not _apply_windows_frame(target):
                warnings.warn(
                    "Callimachus could not restore native Windows frame styles; "
                    "native snap behavior may be unavailable.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            if QtWidgets.QApplication.platformName().lower() not in {"offscreen", "minimal"}:
                try:
                    from ctypes import wintypes

                    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
                    set_window_attribute = dwmapi.DwmSetWindowAttribute
                    corner_preference = wintypes.DWORD(2)  # DWMWCP_ROUND
                    set_window_attribute.argtypes = (
                        wintypes.HWND,
                        wintypes.DWORD,
                        ctypes.c_void_p,
                        wintypes.DWORD,
                    )
                    set_window_attribute.restype = ctypes.c_long
                    # Windows versions without this DWM attribute keep their
                    # default corners; native-frame startup must still succeed.
                    set_window_attribute(
                        wintypes.HWND(target_hwnd),
                        33,  # DWMWA_WINDOW_CORNER_PREFERENCE
                        ctypes.byref(corner_preference),
                        ctypes.sizeof(corner_preference),
                    )
                except (AttributeError, OSError, TypeError, ValueError):
                    pass

        def toggle_maximized(self):
            window.showNormal() if window.isMaximized() else window.showMaximized()

        def uninstall(self):
            app = QtWidgets.QApplication.instance()
            app.removeEventFilter(self)
            if self.native_event_filter is not None:
                app.removeNativeEventFilter(self.native_event_filter)
                self.native_event_filter = None
            for header in list(self.headers):
                try:
                    header.removeEventFilter(self)
                except RuntimeError:
                    pass
            self.headers.clear()
            self.managed.clear()

        def owns(self, receiver):
            current = receiver
            while current is not None:
                if current is window:
                    return True
                current = current.parent()
            return False

        def resize_target(self, receiver):
            if not isinstance(receiver, QtWidgets.QWidget):
                return None
            target = receiver.window()
            if (
                not target.isWindow()
                or (target is not window and target not in self.managed)
                or not self.owns(target)
            ):
                return None
            return target

        def resize_control_at(self, target, receiver, global_pos):
            local_pos = target.mapFromGlobal(global_pos)
            candidates = (receiver, target.childAt(local_pos))
            for candidate in candidates:
                current = candidate
                while current is not None and current is not target:
                    if (
                        isinstance(
                            current,
                            (QtWidgets.QAbstractButton, QtWidgets.QSizeGrip),
                        )
                        or current.objectName() == "windowControls"
                    ):
                        return True
                    current = current.parentWidget()
            return False

        def dialog(self, target):
            if target in self.managed:
                return
            # Chooser/institutional dialogs already have the approved header/X.
            own_header = target.findChild(QtWidgets.QWidget, "juryChooserHeader")
            if own_header is None:
                own_header = target.findChild(QtWidgets.QWidget, "institutionalDialogHeader")
            if own_header is not None:
                jury_header = own_header.objectName() == "juryChooserHeader"
                self.attach(
                    target,
                    own_header,
                    expanded=self.native_macos and jury_header,
                )
                if self.native_macos:
                    for name in ("institutionalDialogClose",):
                        button = own_header.findChild(QtWidgets.QAbstractButton, name)
                        if button is not None:
                            button.hide()
                return
            if self.native_macos and isinstance(target, QtWidgets.QMessageBox):
                self.manage(target)
                theme_style = window.styleSheet()
                target._atlas_theme_prefix = theme_style
                target.setStyleSheet(theme_style)
                self.install_macos_frame(target, expanded=False)
                if target.layout() is not None:
                    target.layout().setContentsMargins(20, 18, 20, 18)
                if isinstance(target, QtWidgets.QMessageBox):
                    self.schedule_native_message_fit(target)
                return
            layout = target.layout()
            if layout is None or layout.menuBar() is not None:
                return
            if not hasattr(target, "_atlas_wide_dialog_margins"):
                target._atlas_wide_dialog_margins = layout.getContentsMargins()
            # Applying a stylesheet can synchronously polish a QMessageBox.
            # Register first so re-entrant polish cannot add a second header.
            self.manage(target)
            theme_style = window.styleSheet()
            target._atlas_theme_prefix = theme_style
            if target.styleSheet():
                target.setStyleSheet(f"{theme_style}\n{target.styleSheet()}")
            else:
                target.setStyleSheet(theme_style)
            header = QtWidgets.QFrame(target)
            header.setObjectName("dialogTitleBar")
            row = QtWidgets.QHBoxLayout(header)
            row.setContentsMargins(16, 8, 16, 8)
            title = QtWidgets.QLabel(target.windowTitle(), header)
            title.setObjectName("sectionTitle")
            row.addWidget(title, 1)
            target._atlas_dialog_header = header
            self.attach(target, header, row, expanded=False)
            # Styling or changing flags can rebuild QMessageBox's grid.
            # Never keep its pre-polish layout wrapper across those calls.
            layout = target.layout()
            if layout is not None and layout.menuBar() is not header:
                layout.setMenuBar(header)
            self.sync_dialog_navigation(target)

        def schedule_native_message_fit(self, target):
            if getattr(target, "_atlas_native_message_fit_pending", False):
                return
            target._atlas_native_message_fit_pending = True

            def refit():
                try:
                    if target in self.managed:
                        self.fit_native_message(target)
                finally:
                    target._atlas_native_message_fit_pending = False

            QtCore.QTimer.singleShot(0, target, refit)

        def fit_native_message(self, target):
            target.ensurePolished()
            button_box = target.findChild(QtWidgets.QDialogButtonBox, "qt_msgbox_buttonbox")
            if button_box is not None:
                button_box.ensurePolished()
                for button in button_box.buttons():
                    button.ensurePolished()

            # Polishing QMessageBox children can rebuild its internal grid.
            # Reacquire the layout only after those style changes settle.
            layout = target.layout()
            if layout is None:
                return
            screen = target.screen() or window.screen()
            width = min(540, max(240, window.width() - 40), screen.availableGeometry().width() - 40)
            if layout.getContentsMargins() != (20, 18, 20, 18):
                layout.setContentsMargins(20, 18, 20, 18)
            for name in ("qt_msgbox_label", "qt_msgbox_informativelabel"):
                label = target.findChild(QtWidgets.QLabel, name)
                if label is not None:
                    label.setWordWrap(True)
                    label.setMinimumHeight(0)
                    label.setMaximumHeight(16777215)
                    label_width = max(100, width - 100)
                    if label.width() != label_width:
                        label.setFixedWidth(label_width)
                    label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)

            if button_box is not None:
                if not hasattr(target, "_atlas_native_message_button_orientation"):
                    target._atlas_native_message_button_orientation = button_box.orientation()
                    target._atlas_native_message_button_minimum_widths = {}
                    target._atlas_native_message_box_minimum_width = button_box.minimumWidth()
                original_orientation = target._atlas_native_message_button_orientation
                original_button_minimums = target._atlas_native_message_button_minimum_widths
                if button_box.minimumWidth() != target._atlas_native_message_box_minimum_width:
                    button_box.setMinimumWidth(target._atlas_native_message_box_minimum_width)
                if button_box.orientation() != original_orientation:
                    button_box.setOrientation(original_orientation)
                for button in button_box.buttons():
                    original_button_minimums.setdefault(button, button.minimumWidth())
                    original_minimum = original_button_minimums[button]
                    if button.minimumWidth() != original_minimum:
                        button.setMinimumWidth(original_minimum)
                    natural_width = max(
                        button.sizeHint().width(),
                        button.minimumSizeHint().width(),
                    )
                    if button.minimumWidth() < natural_width:
                        button.setMinimumWidth(natural_width)
                row_width = max(
                    button_box.sizeHint().width(),
                    button_box.minimumSizeHint().width(),
                )
                margins = layout.getContentsMargins()
                available_row_width = max(1, width - margins[0] - margins[2])
                if (
                    original_orientation == QtCore.Qt.Orientation.Horizontal
                    and row_width > available_row_width
                ):
                    button_box.setOrientation(QtCore.Qt.Orientation.Vertical)
                row_width = max(
                    button_box.sizeHint().width(),
                    button_box.minimumSizeHint().width(),
                )
                if button_box.minimumWidth() < row_width:
                    button_box.setMinimumWidth(row_width)
                button_box.updateGeometry()

            # Button polish or orientation changes can also refresh the dialog
            # grid, so never retain the pre-fit layout wrapper here.
            layout = target.layout()
            if layout is None:
                return
            if isinstance(layout, QtWidgets.QGridLayout):
                for row in range(layout.rowCount()):
                    layout.setRowStretch(row, 0)
                    layout.setRowMinimumHeight(row, 0)
            for index in range(layout.count()):
                spacer = layout.itemAt(index).spacerItem()
                if spacer is not None:
                    spacer.changeSize(0, 0, QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Minimum)
            target.setMinimumSize(0, 0)
            target.setMaximumSize(16777215, 16777215)
            layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetFixedSize)
            layout.invalidate()
            layout.activate()
            target.adjustSize()

        def sync_dialog_navigation(self, target):
            header = getattr(target, "_atlas_dialog_header", None)
            if header is None:
                return
            layout = target.layout()
            if layout is None:
                return
            if not hasattr(target, "_atlas_wide_dialog_margins"):
                target._atlas_wide_dialog_margins = layout.getContentsMargins()
            compact = window.width() < 700
            compact_navigation = (
                compact
                and (target.property("compactMessageDialog") or target.width() >= window.width() - 64)
                and target.objectName() != "reportExportProgressDialog"
            )
            if self.native_macos:
                safe = self.safe_area_margins(target)
                margins = (
                    max(16, safe.left()),
                    16,
                    max(16, safe.right()),
                    16,
                )
                header.layout().setContentsMargins(
                    16, 16, 16, 8,
                )
            else:
                margins = (
                    (16, 16, 16, 16)
                    if compact
                    else tuple(
                        max(16, margin)
                        for margin in target._atlas_wide_dialog_margins
                    )
                )
            if layout.getContentsMargins() != margins:
                layout.setContentsMargins(*margins)
            if isinstance(target, QtWidgets.QMessageBox):
                if self.native_macos:
                    close_button = header.findChild(
                        QtWidgets.QToolButton, "windowClose",
                    )
                    if close_button is not None:
                        close_button.hide()
                return
            row = header.layout()
            close_button = header.findChild(QtWidgets.QToolButton, "windowClose")
            if close_button is None:
                return
            key = "arrow-left" if compact_navigation else "close"
            label = (
                text.get("back", "Back") if compact_navigation
                else text.get("close", "Close")
            )
            close_button.setProperty("atlasIcon", key)
            from core.gui.shell.navigation import set_back_button_presentation
            if compact_navigation:
                set_back_button_presentation(
                    close_button,
                    QtCore,
                    QtGui,
                    label=label,
                    dark=window._dark,
                    active=True,
                )
            else:
                set_back_button_presentation(
                    close_button,
                    QtCore,
                    QtGui,
                    label=label,
                    dark=window._dark,
                    active=False,
                )
                close_button.setIcon(icon(
                    QtCore, QtGui, key,
                    "#e6edf6" if window._dark else "#455374",
                ))
                close_button.setToolTip(label)
                close_button.setAccessibleName(label)
            if self.native_macos:
                close_button.setVisible(compact_navigation)
                if compact_navigation:
                    close_button.setFixedSize(30, 30)
            if getattr(target, "_atlas_dialog_compact", None) != compact_navigation:
                target._atlas_dialog_compact = compact_navigation
                row.removeWidget(close_button)
                row.insertWidget(0 if compact_navigation else row.count(), close_button)

        def eventFilter(self, receiver, event):
            kind = event.type()
            resize_edges = None
            resize_target = None
            if (
                sys.platform == "linux"
                and kind == QtCore.QEvent.Type.MouseButtonPress
                and event.button() == QtCore.Qt.MouseButton.LeftButton
            ):
                resize_target = self.resize_target(receiver)
                if resize_target is not None:
                    global_pos = event.globalPosition().toPoint()
                    resize_edges = _qt_resize_edges(
                        resize_target,
                        resize_target.mapFromGlobal(global_pos),
                        QtCore,
                    )
                    if (
                        resize_edges is not None
                        and not self.resize_control_at(
                            resize_target, receiver, global_pos
                        )
                    ):
                        handle = resize_target.windowHandle()
                        if (
                            handle is not None
                            and handle.startSystemResize(resize_edges)
                        ):
                            return True
            managed_events = {
                QtCore.QEvent.Type.Resize,
                QtCore.QEvent.Type.Show,
                QtCore.QEvent.Type.LayoutRequest,
            }
            if self.native_macos:
                managed_events.add(QtCore.QEvent.Type.SafeAreaMarginsChange)
            if receiver in self.managed and kind in managed_events:
                if isinstance(receiver, QtWidgets.QMessageBox):
                    header = getattr(receiver, "_atlas_dialog_header", None)
                    layout = receiver.layout()
                    # QMessageBox can rebuild its internal grid after polish
                    # when question()/warning() finish setting text/buttons.
                    if header is not None and layout is not None and layout.menuBar() is not header:
                        layout.setMenuBar(header)
                        layout.activate()
                        receiver.adjustSize()
                self.update_outline(receiver)
                self.sync_dialog_navigation(receiver)
                if (
                    self.native_macos
                    and receiver is getattr(window, "llm_choice_dialog", None)
                    and kind in {
                        QtCore.QEvent.Type.Resize,
                        QtCore.QEvent.Type.Show,
                        QtCore.QEvent.Type.SafeAreaMarginsChange,
                    }
                ):
                    chooser = getattr(window, "_atlante_jury_chooser", None)
                    if chooser is not None:
                        chooser._update_header_controls()
            elif receiver is window and kind == QtCore.QEvent.Type.StyleChange:
                theme_style = window.styleSheet()
                for target in list(self.managed):
                    # Dialogs retain a copied theme for their custom frame.
                    # Refresh that prefix without replacing their local rules,
                    # or overwriting a dialog that manages its own stylesheet.
                    previous = getattr(target, "_atlas_theme_prefix", None)
                    current = target.styleSheet()
                    if previous and current.startswith(previous):
                        target._atlas_theme_prefix = theme_style
                        target.setStyleSheet(theme_style + current[len(previous):])
                    self.update_outline(target)
                    self.sync_dialog_navigation(target)
            if (kind == QtCore.QEvent.Type.MouseButtonPress
                and isinstance(receiver, QtWidgets.QWidget)
                and self.owns(receiver)
                and (receiver.objectName() in {"inlineHelp", "settingHelpIcon"} or receiver.property("atlasIcon") == "help")
                and receiver.toolTip()):
                QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), receiver.toolTip(), receiver)
                return True
            if (
                kind in {QtCore.QEvent.Type.Polish, QtCore.QEvent.Type.PolishRequest}
                and isinstance(receiver, QtWidgets.QDialog)
                and receiver.isWindow()
                and receiver not in self.managed
                and self.owns(receiver)
                and not isinstance(receiver, QtWidgets.QFileDialog)
            ):
                self.dialog(receiver)
            if (
                self.native_macos
                and kind in self.native_message_fit_events
                and isinstance(receiver, QtWidgets.QWidget)
            ):
                message = receiver if isinstance(receiver, QtWidgets.QMessageBox) else receiver.window()
                if isinstance(message, QtWidgets.QMessageBox) and message in self.managed:
                    self.schedule_native_message_fit(message)

            reference = self.headers.get(receiver)
            target = reference() if reference is not None else None
            if target is not None:
                if (
                    kind == QtCore.QEvent.Type.MouseButtonDblClick
                    and target is window
                    and not self.native_macos
                ):
                    self.toggle_maximized()
                    return True
                if kind == QtCore.QEvent.Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton:
                    if resize_target is target and resize_edges is not None:
                        return False
                    handle = target.windowHandle()
                    if handle is not None:
                        handle.startSystemMove()
                    return True
            if receiver is window and kind == QtCore.QEvent.Type.WindowStateChange and self.maximize_button is not None:
                key = "window-restore" if window.isMaximized() else "window-maximize"
                self.maximize_button.setProperty("atlasIcon", key)
                self.maximize_button.setIcon(icon(QtCore, QtGui, key, "#e6edf6" if window._dark else "#455374"))
                window._atlas_size_grip.setVisible(not window.isMaximized())
                self.update_main_corner_mask(window)
                self.update_outline(window)
            if (
                receiver is window
                and self.native_macos
                and kind in {
                    QtCore.QEvent.Type.Resize,
                    QtCore.QEvent.Type.Show,
                    QtCore.QEvent.Type.SafeAreaMarginsChange,
                    QtCore.QEvent.Type.WindowStateChange,
                }
            ):
                self.update_main_safe_area()
            if receiver is window and kind == QtCore.QEvent.Type.Resize:
                if self.native_macos:
                    return False
                grip = window._atlas_size_grip
                grip.move(window.width() - grip.width(), window.height() - grip.height())
                grip.raise_()
                self.update_outline(window)
                self.update_main_corner_mask(window)
            return False

    controller = Chrome()
    if not controller.native_macos:
        window._atlas_size_grip = QtWidgets.QSizeGrip(window)
        window._atlas_size_grip.setFixedSize(18, 18)
    header = window.brand_logo.parentWidget()
    controller.attach(window, header, header.layout(), main=True)
    # The Jury chooser is constructed (and may be polished) during _build,
    # before this shared event filter is installed.
    for dialog in window.findChildren(QtWidgets.QDialog):
        if dialog.isWindow() and not isinstance(dialog, QtWidgets.QFileDialog):
            controller.dialog(dialog)
    window._atlas_window_chrome = controller
