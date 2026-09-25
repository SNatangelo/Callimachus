# core/report/human/render.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic, self-contained HTML rendering for a human report companion."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from .assets import AssetCatalog, AssetValidationError, default_asset_root, load_asset_catalog
from .privacy import reject_local_paths, sanitize_local_paths
from .projection import HumanReportProjection


@dataclass(frozen=True)
class RenderedHumanReport:
    """Unsealed report body and the immutable inputs that produced it."""

    body: str
    locale: str
    theme: str
    preview: bool
    projection_sha256: str
    asset_sha256: Mapping[str, str]


def render_human_report(
    projection: HumanReportProjection,
    *,
    catalog: AssetCatalog | None = None,
    locale: str | None = None,
    theme: str | None = None,
    preview: bool = False,
    preview_failures: tuple[str, ...] = (),
) -> RenderedHumanReport:
    """Render a report without writing files, network access, or hidden defaults."""
    catalog = catalog or load_asset_catalog()
    selected_locale = locale if locale is not None else catalog.default_locale
    selected_theme = theme if theme is not None else catalog.default_theme
    if selected_locale not in catalog.locales:
        raise AssetValidationError("requested locale is not installed")
    if selected_theme not in catalog.themes:
        raise AssetValidationError("requested theme is not installed")
    if not isinstance(preview, bool) or any(not isinstance(item, str) for item in preview_failures):
        raise ValueError("human report preview settings are invalid")

    assets = _source_assets()
    asset_sha256 = {name: _sha256(content) for name, content in assets.items()}
    brand_logo = base64.b64encode(assets["logo.svg"].encode("utf-8")).decode("ascii")
    source_catalog = _catalog_value(catalog)
    for identifier, value in source_catalog["locales"].items():
        asset_sha256[f"embedded/locales/{identifier}.json"] = _sha256(
            _canonical_json(value)
        )
    for identifier, value in source_catalog["themes"].items():
        asset_sha256[f"embedded/themes/{identifier}.json"] = _sha256(
            _canonical_json(value)
        )
    projection_value = projection.as_dict()
    reject_local_paths(projection_value)
    projection_sha256 = _sha256(_canonical_json(projection_value))
    data = {
        "projection": projection_value,
        "catalog": source_catalog,
        "brand_logo": f"data:image/svg+xml;base64,{brand_logo}",
        "initial_locale": selected_locale,
        "initial_theme": selected_theme,
        "preview": preview,
        "preview_failures": tuple(sorted(sanitize_local_paths(preview_failures))),
        "render_metadata": {
            "projection_sha256": projection_sha256,
            "asset_sha256": asset_sha256,
        },
    }
    # This is the final boundary for every dynamic value embedded in the HTML,
    # including preview diagnostics supplied by the caller.
    reject_local_paths(data)
    template = assets["template.html"]
    replacements = {
        "{{LANG}}": catalog.locales[selected_locale].language_tag,
        "{{DIR}}": catalog.locales[selected_locale].direction,
        "{{STYLE}}": assets["app.css"],
        "{{DATA}}": _script_json(data),
        "{{SCRIPT}}": assets["app.js"],
    }
    for marker, value in replacements.items():
        if template.count(marker) != 1:
            raise ValueError(f"human report template marker is invalid: {marker}")
        template = template.replace(marker, value)
    if any(marker in template for marker in replacements):
        raise ValueError("human report template has an unresolved marker")
    return RenderedHumanReport(
        body=template,
        locale=selected_locale,
        theme=selected_theme,
        preview=preview,
        projection_sha256=projection_sha256,
        asset_sha256=asset_sha256,
    )


def _source_assets() -> dict[str, str]:
    root = default_asset_root()
    names = ("template.html", "app.css", "app.js", "logo.svg", "manifest.json")
    assets: dict[str, str] = {}
    for name in names:
        try:
            assets[name] = (root / name).read_text(encoding="utf-8")
        except OSError as exc:
            raise AssetValidationError(f"human report source asset is missing: {name}") from exc
    for directory in ("locales", "themes"):
        paths = sorted((root / directory).glob("*.json"), key=lambda path: path.name)
        if not paths:
            raise AssetValidationError(f"human report {directory} source assets are missing")
        for path in paths:
            assets[f"{directory}/{path.name}"] = path.read_text(encoding="utf-8")
    return assets


def _catalog_value(catalog: AssetCatalog) -> dict[str, Any]:
    return {
        "schema_version": catalog.schema_version,
        "locales": {
            identifier: {
                "language_tag": locale.language_tag,
                "direction": locale.direction,
                "native_name": locale.native_name,
                "messages": {
                    key: dict(message) if isinstance(message, Mapping) else message
                    for key, message in sorted(locale.messages.items())
                },
            }
            for identifier, locale in sorted(catalog.locales.items())
        },
        "themes": {
            identifier: {
                "id": theme.identifier,
                "label_key": theme.label_key,
                "tokens": dict(theme.tokens),
            }
            for identifier, theme in sorted(catalog.themes.items())
        },
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _script_json(value: Any) -> str:
    return _canonical_json(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
