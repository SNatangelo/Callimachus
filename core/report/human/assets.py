# core/report/human/assets.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed loading for external human-report presentation assets.

The report projection contains canonical values only.  Themes and locales are
presentation inputs and are deliberately kept outside Python so that they can
be added without changing renderer code.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping


_ASSET_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_LOCALE_ID = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_TOKEN_VALUE = re.compile(r"^[#A-Za-z0-9][#A-Za-z0-9%., _-]{0,160}$")
_PLURAL_CATEGORIES = frozenset({"zero", "one", "two", "few", "many", "other"})


class AssetValidationError(ValueError):
    """An external presentation asset is absent, malformed, or unsafe."""


@dataclass(frozen=True)
class LocaleAsset:
    language_tag: str
    direction: str
    native_name: str
    messages: Mapping[str, str | Mapping[str, str]]


@dataclass(frozen=True)
class ThemeAsset:
    identifier: str
    label_key: str
    tokens: Mapping[str, str]


@dataclass(frozen=True)
class AssetCatalog:
    schema_version: int
    default_locale: str
    default_theme: str
    theme_tokens: tuple[str, ...]
    locales: Mapping[str, LocaleAsset]
    themes: Mapping[str, ThemeAsset]


def default_asset_root() -> Path:
    return Path(__file__).with_name("assets")


def load_asset_catalog(asset_root: Path | str | None = None) -> AssetCatalog:
    """Load all presentation assets in a deterministic, validated form."""
    root = Path(asset_root) if asset_root is not None else default_asset_root()
    manifest = _read_json(root / "manifest.json", "asset manifest")
    _require_exact_keys(
        manifest,
        {"schema_version", "default_locale", "default_theme", "theme_tokens"},
        "asset manifest",
    )
    if manifest["schema_version"] != 1:
        raise AssetValidationError("asset manifest schema_version must be 1")
    default_locale = _require_id(manifest["default_locale"], "default_locale", _LOCALE_ID)
    default_theme = _require_id(manifest["default_theme"], "default_theme", _ASSET_ID)
    token_values = manifest["theme_tokens"]
    if not isinstance(token_values, list) or not token_values:
        raise AssetValidationError("asset manifest theme_tokens must be a non-empty list")
    theme_tokens = tuple(_require_theme_token(value) for value in token_values)
    if len(set(theme_tokens)) != len(theme_tokens):
        raise AssetValidationError("asset manifest theme_tokens contains duplicates")

    locales = _load_locales(root / "locales")
    themes = _load_themes(root / "themes", theme_tokens, locales["en"].messages)
    if default_locale not in locales:
        raise AssetValidationError("asset manifest default_locale is not installed")
    if default_theme not in themes:
        raise AssetValidationError("asset manifest default_theme is not installed")
    return AssetCatalog(
        schema_version=1,
        default_locale=default_locale,
        default_theme=default_theme,
        theme_tokens=theme_tokens,
        locales=MappingProxyType(locales),
        themes=MappingProxyType(themes),
    )


def _load_locales(directory: Path) -> dict[str, LocaleAsset]:
    if not directory.is_dir():
        raise AssetValidationError("locale asset directory is missing")
    paths = sorted(directory.glob("*.json"), key=lambda path: path.name)
    if not paths:
        raise AssetValidationError("locale asset directory is empty")
    locales: dict[str, LocaleAsset] = {}
    english_messages: Mapping[str, str | Mapping[str, str]] | None = None
    for path in paths:
        data = _read_json(path, "locale asset")
        _require_exact_keys(data, {"language_tag", "direction", "native_name", "messages"}, f"locale {path.name}")
        language_tag = _require_id(data["language_tag"], f"locale {path.name} language_tag", _LOCALE_ID)
        if path.stem != language_tag:
            raise AssetValidationError(f"locale filename does not match language_tag: {path.name}")
        if language_tag in locales:
            raise AssetValidationError(f"duplicate locale: {language_tag}")
        direction = data["direction"]
        if direction not in {"ltr", "rtl"}:
            raise AssetValidationError(f"locale {language_tag} direction must be ltr or rtl")
        native_name = _require_safe_text(data["native_name"], f"locale {language_tag} native_name")
        messages = _validate_messages(data["messages"], f"locale {language_tag}")
        if language_tag == "en":
            english_messages = messages
        locales[language_tag] = LocaleAsset(language_tag, direction, native_name, MappingProxyType(messages))
    if english_messages is None:
        raise AssetValidationError("English locale is required as the message contract")
    for language_tag, locale in locales.items():
        _validate_message_contract(english_messages, locale.messages, language_tag)
    return dict(sorted(locales.items()))


def _load_themes(
    directory: Path,
    required_tokens: tuple[str, ...],
    english_messages: Mapping[str, str | Mapping[str, str]],
) -> dict[str, ThemeAsset]:
    if not directory.is_dir():
        raise AssetValidationError("theme asset directory is missing")
    paths = sorted(directory.glob("*.json"), key=lambda path: path.name)
    if not paths:
        raise AssetValidationError("theme asset directory is empty")
    themes: dict[str, ThemeAsset] = {}
    for path in paths:
        data = _read_json(path, "theme asset")
        _require_exact_keys(data, {"id", "label_key", "tokens"}, f"theme {path.name}")
        identifier = _require_id(data["id"], f"theme {path.name} id", _ASSET_ID)
        if path.stem != identifier:
            raise AssetValidationError(f"theme filename does not match id: {path.name}")
        if identifier in themes:
            raise AssetValidationError(f"duplicate theme: {identifier}")
        label_key = data["label_key"]
        if not isinstance(label_key, str) or label_key not in english_messages:
            raise AssetValidationError(f"theme {identifier} label_key is not a message key")
        tokens = data["tokens"]
        if not isinstance(tokens, dict) or set(tokens) != set(required_tokens):
            raise AssetValidationError(f"theme {identifier} tokens do not match the token contract")
        validated = {}
        for token in required_tokens:
            value = tokens[token]
            if not isinstance(value, str) or not _TOKEN_VALUE.fullmatch(value):
                raise AssetValidationError(f"theme {identifier} has unsafe value for {token}")
            validated[token] = value
        themes[identifier] = ThemeAsset(identifier, label_key, MappingProxyType(validated))
    return dict(sorted(themes.items()))


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetValidationError(f"cannot read {label}: {path.name}") from exc
    if not isinstance(value, dict):
        raise AssetValidationError(f"{label} must be a JSON object")
    return value


def _require_exact_keys(value: Mapping[str, Any], required: set[str], label: str) -> None:
    if set(value) != required:
        raise AssetValidationError(f"{label} has an invalid schema")


def _require_id(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AssetValidationError(f"{label} is invalid")
    return value


def _require_theme_token(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"--cv-[a-z0-9-]+", value):
        raise AssetValidationError("theme token is invalid")
    return value


def _require_safe_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "<" in value or ">" in value:
        raise AssetValidationError(f"{label} must be non-empty text without markup")
    return value


def _validate_messages(value: Any, label: str) -> dict[str, str | Mapping[str, str]]:
    if not isinstance(value, dict) or not value:
        raise AssetValidationError(f"{label} messages must be a non-empty object")
    messages: dict[str, str | Mapping[str, str]] = {}
    for key, message in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]+", key):
            raise AssetValidationError(f"{label} has invalid message key")
        if isinstance(message, str):
            messages[key] = _require_safe_text(message, f"{label} message {key}")
        elif isinstance(message, dict):
            categories = set(message)
            if (
                not categories
                or "other" not in categories
                or not categories.issubset(_PLURAL_CATEGORIES)
            ):
                raise AssetValidationError(
                    f"{label} plural {key} must use valid categories and include other"
                )
            plural = {category: _require_safe_text(text, f"{label} plural {key}") for category, text in message.items()}
            expected_placeholders = _placeholders(plural["other"])
            if any(_placeholders(text) != expected_placeholders for text in plural.values()):
                raise AssetValidationError(f"{label} plural {key} has inconsistent placeholders")
            messages[key] = MappingProxyType(plural)
        else:
            raise AssetValidationError(f"{label} message {key} has invalid value")
    return messages


def _placeholders(value: str) -> tuple[str, ...]:
    remaining = _PLACEHOLDER.sub("", value)
    if "{" in remaining or "}" in remaining:
        raise AssetValidationError("message has invalid placeholder syntax")
    return tuple(sorted(_PLACEHOLDER.findall(value)))


def _validate_message_contract(
    english: Mapping[str, str | Mapping[str, str]],
    candidate: Mapping[str, str | Mapping[str, str]],
    language_tag: str,
) -> None:
    if set(english) != set(candidate):
        raise AssetValidationError(f"locale {language_tag} messages do not match English")
    for key, source in english.items():
        target = candidate[key]
        if isinstance(source, str) != isinstance(target, str):
            raise AssetValidationError(f"locale {language_tag} message shape differs for {key}")
        if isinstance(source, str):
            if _placeholders(source) != _placeholders(target):  # type: ignore[arg-type]
                raise AssetValidationError(f"locale {language_tag} placeholders differ for {key}")
        else:
            source_placeholders = _placeholders(source["other"])
            for target_text in target.values():  # type: ignore[union-attr]
                if _placeholders(target_text) != source_placeholders:
                    raise AssetValidationError(f"locale {language_tag} placeholders differ for {key}")
