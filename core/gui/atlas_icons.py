# core/gui/atlas_icons.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Atlante's small vector glyphs; no font glyphs or bitmap placeholders."""
from __future__ import annotations


import re
from pathlib import Path

_ASSETS = Path(__file__).parent / "assets" / "atlante"
_THEME_MONOCHROME = {"provider-openai", "provider-ollama", "provider-openrouter"}
_NO_BADGE = _THEME_MONOCHROME | {
    "provider-mistral", "provider-xai", "provider-deepseek",
    "provider-antigravity", "provider-glm", "provider-groq",
}
_RASTER_BADGE = {"provider-freetoken", "publisher-wiley", "publisher-springer"}
_CLI_PROVIDERS = frozenset({
    "codex_cli", "claude_cli", "gemini_cli", "antigravity_cli", "antigravity",
    "opencode_cli",
})


def icon(QtCore, QtGui, key, color="#52617d", size=24, *, background=None):
    """Return a theme-aware icon; Qt remains optional at import."""
    is_cli = key in _CLI_PROVIDERS
    brand = {"codex_cli": "openai", "openai_compatible": "openai", "openai": "openai",
             "claude_cli": "claude", "anthropic": "claude", "gemini_cli": "gemini",
             "gemini": "gemini", "ollama_local": "ollama", "ollama": "ollama",
             "freetoken_authenticated": "freetoken", "freetoken": "freetoken",
             "mistral": "mistral", "deepseek": "deepseek", "groq": "groq", "xai": "xai", "google": "google",
             "bing": "bing", "duckduckgo": "duckduckgo", "openrouter": "openrouter",
             "opencode": "opencode", "opencode_cli": "opencode", "glm": "glm",
             "antigravity": "antigravity", "antigravity_cli": "antigravity",
             "typesafe": "typesafe"}.get(key)
    stem = "provider-" + brand if brand else key.replace("publisher_", "publisher-") if key.startswith("publisher_") else ""
    if stem:
        extensions = ("png", "svg", "ico") if brand == "freetoken" else ("svg", "png", "webp", "ico")
        for extension in extensions:
            source = _ASSETS / f"{stem}.{extension}"
            if source.is_file():
                if extension == "svg":
                    svg = source.read_text(encoding="utf-8")
                    if brand == "mistral":
                        svg = _mistral_emblem_svg(svg)
                    if stem in _THEME_MONOCHROME:
                        svg = _recolor_monochrome(svg, color)
                    badge = stem not in _NO_BADGE
                    overlay_engine = (
                        _terminal_badge_engine(
                            QtCore, QtGui, color, background or "#ffffff",
                        )
                        if is_cli else None
                    )
                    return QtGui.QIcon(_svg_engine(
                        QtCore, QtGui, svg, brand=badge, preserve_aspect=True,
                        overlay_engine=overlay_engine,
                    ))
                if is_cli:
                    image = QtGui.QImage(str(source))
                    if not image.isNull():
                        terminal_badge = _terminal_badge_engine(
                            QtCore, QtGui, color, background or "#ffffff",
                        )
                        return QtGui.QIcon(_raster_engine(
                            QtCore, QtGui, image, overlay_engine=terminal_badge
                        ))
                if stem in _RASTER_BADGE:
                    image = QtGui.QImage(str(source))
                    if not image.isNull():
                        return QtGui.QIcon(_raster_engine(
                            QtCore, QtGui, image, badge=True
                        ))
                return QtGui.QIcon(str(source))
    key = {"openai_compatible":"openai", "freetoken_authenticated":"freetoken",
           "ollama_local":"ollama", "antigravity_cli":"antigravity",
           "gemini_cli":"gemini", "opencode_cli":"opencode", "host":"console",
           "freetoken":"database"}.get(key, key)
    path = _ASSETS / (key + ".svg")
    if not path.is_file():
        path = _ASSETS / "file.svg"
    svg = path.read_text(encoding="utf-8").replace("currentColor", color)
    # QIcon keeps the SVG engine: Qt rasterizes for the actual screen DPI.
    overlay_engine = (
        _terminal_badge_engine(QtCore, QtGui, color, background or "#ffffff")
        if is_cli else None
    )
    return QtGui.QIcon(_svg_engine(QtCore, QtGui, svg, overlay_engine=overlay_engine))


def _recolor_monochrome(svg, color):
    """Replace only the source's known monochrome paint colors."""
    return (svg.replace('fill="black"', f'fill="{color}"')
            .replace("fill='black'", f"fill='{color}'")
            .replace('fill="#03080A"', f'fill="{color}"')
            .replace("fill='#03080A'", f"fill='{color}'"))


def _mistral_emblem_svg(svg):
    """Show the original pixel M from the left side of Mistral's logo SVG."""
    cropped_root = '<svg xmlns="http://www.w3.org/2000/svg" width="150" height="145" viewBox="0 35 150 145">'
    return re.sub(r"<svg\b[^>]*>", cropped_root, svg, count=1, flags=re.IGNORECASE)


def _terminal_badge_engine(QtCore, QtGui, color, background):
    svg = (_ASSETS / "console.svg").read_text(encoding="utf-8")
    svg = svg.replace("currentColor", color)
    return _svg_engine(
        QtCore, QtGui, svg, preserve_aspect=True, badge_background=background,
    )


def _svg_engine(
    QtCore, QtGui, svg, *, brand=False, preserve_aspect=None,
    overlay_engine=None, badge_background=None,
):
    """Use Qt's scalable SVG icon engine with an in-memory themed document."""
    from PySide6.QtSvg import QSvgRenderer
    if preserve_aspect is None:
        preserve_aspect = brand

    class SvgIconEngine(QtGui.QIconEngine):
        def clone(self):
            return SvgIconEngine()
        def paint(self, painter, rect, mode, state):
            if rect.isEmpty() or not painter.isActive():
                return
            painter.save()
            if mode == QtGui.QIcon.Mode.Disabled:
                painter.setOpacity(painter.opacity()*.45)
            painter.setClipRect(
                QtCore.QRectF(rect), QtCore.Qt.ClipOperation.IntersectClip
            )
            icon_rect = QtCore.QRectF(rect)
            target = QtCore.QRectF(icon_rect)
            if overlay_engine is not None:
                inset = min(target.width(), target.height()) * .05
                target.adjust(inset, inset, -inset, -inset)
            if brand:
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(QtGui.QColor("#ffffff"))
                painter.drawRoundedRect(target, 4, 4)
                target.adjust(2, 2, -2, -2)
            if badge_background is not None:
                badge_size = min(target.width(), target.height())
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(QtGui.QColor(badge_background))
                radius = badge_size * .18
                painter.drawRoundedRect(target, radius, radius)
                inset = badge_size * .04
                target.adjust(inset, inset, -inset, -inset)
            renderer = QSvgRenderer(QtCore.QByteArray(svg.encode()))
            if preserve_aspect:
                renderer.setAspectRatioMode(QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            renderer.render(painter, target)
            if overlay_engine is not None:
                badge_size = min(rect.width(), rect.height()) * .44
                margin = min(rect.width(), rect.height()) * .015
                badge_rect = QtCore.QRectF(
                    icon_rect.right() - badge_size - margin,
                    icon_rect.bottom() - badge_size - margin,
                    badge_size,
                    badge_size,
                )
                # The parent engine applies disabled opacity to both layers.
                overlay_engine.paint(
                    painter, badge_rect, QtGui.QIcon.Mode.Normal, state
                )
            painter.restore()
        def pixmap(self, size, mode, state):
            if size.isEmpty():
                return QtGui.QPixmap()
            canvas = QtGui.QPixmap(size)
            canvas.fill(QtCore.Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(canvas)
            self.paint(painter, canvas.rect(), mode, state)
            painter.end()
            return canvas
        def scaledPixmap(self, size, mode, state, scale):
            if size.isEmpty() or scale <= 0:
                return QtGui.QPixmap()
            pixels = self.pixmap(size * scale, mode, state)
            pixels.setDevicePixelRatio(scale)
            return pixels
    return SvgIconEngine()


def _raster_engine(QtCore, QtGui, image, *, badge=False, overlay_engine=None):
    """Draw original raster marks at the requested size and device scale."""
    class RasterIconEngine(QtGui.QIconEngine):
        def clone(self):
            return RasterIconEngine()

        def paint(self, painter, rect, mode, state):
            if rect.isEmpty() or not painter.isActive() or image.isNull():
                return
            painter.save()
            if mode == QtGui.QIcon.Mode.Disabled:
                painter.setOpacity(painter.opacity() * .45)
            icon_rect = QtCore.QRectF(rect)
            target = QtCore.QRectF(icon_rect)
            if overlay_engine is not None:
                inset = min(target.width(), target.height()) * .05
                target.adjust(inset, inset, -inset, -inset)
            if badge:
                radius = min(target.width(), target.height()) * .17
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(QtGui.QColor("#ffffff"))
                painter.drawRoundedRect(target, radius, radius)
                inset = min(target.width(), target.height()) * .09
                target.adjust(inset, inset, -inset, -inset)
            scale = min(target.width() / image.width(), target.height() / image.height())
            width, height = image.width() * scale, image.height() * scale
            fitted = QtCore.QRectF(
                target.center().x() - width / 2,
                target.center().y() - height / 2,
                width,
                height,
            )
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(fitted, image, QtCore.QRectF(image.rect()))
            if overlay_engine is not None:
                badge_size = min(rect.width(), rect.height()) * .44
                margin = min(rect.width(), rect.height()) * .015
                badge_rect = QtCore.QRectF(
                    icon_rect.right() - badge_size - margin,
                    icon_rect.bottom() - badge_size - margin,
                    badge_size,
                    badge_size,
                )
                # The parent engine already applies disabled opacity to both layers.
                overlay_engine.paint(
                    painter, badge_rect, QtGui.QIcon.Mode.Normal, state
                )
            painter.restore()

        def pixmap(self, size, mode, state):
            if size.isEmpty():
                return QtGui.QPixmap()
            canvas = QtGui.QPixmap(size)
            canvas.fill(QtCore.Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(canvas)
            self.paint(painter, canvas.rect(), mode, state)
            painter.end()
            return canvas

        def scaledPixmap(self, size, mode, state, scale):
            if size.isEmpty() or scale <= 0:
                return QtGui.QPixmap()
            pixels = self.pixmap(size * scale, mode, state)
            pixels.setDevicePixelRatio(scale)
            return pixels

    return RasterIconEngine()
