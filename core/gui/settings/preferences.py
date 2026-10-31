# core/gui/settings/preferences.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Qt-free registry and validation for portable desktop preferences."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any


_PROFILE_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,47})$")
_MAX_ROLE_SELECTORS = 256
_MAX_SELECTOR_LENGTH = 256

_PREFERENCE_SPECS: dict[str, dict[str, Any]] = {
    "language": {"type": "enum", "default": "en", "choices": ["en", "it"]},
    "font_size": {"type": "integer", "default": 16, "minimum": 11, "maximum": 18},
    "dark_theme": {"type": "boolean", "default": False},
    "cli_console_enabled": {"type": "boolean", "default": False},
    "browser_cache_policy": {
        "type": "enum", "default": "never", "choices": ["never", "fetch", "close"],
    },
    "source_contact_suppressed": {"type": "boolean", "default": False},
    # Stored in QSettings as compact JSON text; accepting objects makes the
    # backup envelope easier to inspect and validate without changing the GUI format.
    "llm_role_selection": {
        "type": "json", "default": {"jury1": [], "jury2": []},
    },
    "institutional_access/dont_ask_again": {"type": "boolean", "default": False},
    "institutional_access/profile_history": {
        "type": "string_list", "default": [], "maximum_items": 8,
    },
}


def preference_registry() -> dict[str, dict[str, Any]]:
    """Return independent metadata describing current GUI preferences."""
    return deepcopy(_PREFERENCE_SPECS)


def default_preferences() -> dict[str, Any]:
    """Return fresh default preference values for the current application."""
    return {name: deepcopy(spec["default"]) for name, spec in _PREFERENCE_SPECS.items()}


def validate_preferences(
    values: Mapping[str, Any],
    *,
    include_defaults: bool = True,
    ignore_unknown: bool = False,
) -> dict[str, Any]:
    """Validate known settings and return canonical values safe for QSettings."""
    if not isinstance(values, Mapping):
        raise ValueError("preferences must be a mapping")
    unknown = set(values) - set(_PREFERENCE_SPECS)
    if unknown and not ignore_unknown:
        raise ValueError("unknown preference")
    result = default_preferences() if include_defaults else {}
    for name, raw in values.items():
        if name not in _PREFERENCE_SPECS:
            continue
        spec = _PREFERENCE_SPECS[name]
        kind = spec["type"]
        if kind == "boolean":
            result[name] = _boolean(raw)
        elif kind == "integer":
            result[name] = _integer(raw, spec["minimum"], spec["maximum"])
        elif kind == "enum":
            if type(raw) is not str or _has_control(raw) or raw not in spec["choices"]:
                raise ValueError("invalid preference")
            result[name] = raw
        elif kind == "json":
            result[name] = _role_selection(raw)
        elif kind == "string_list":
            result[name] = _profile_history(raw, spec["maximum_items"])
        else:  # pragma: no cover - registry is static and covered by tests.
            raise ValueError("unsupported preference type")
    return result


def _boolean(value: Any) -> bool:
    # QSettings can surface INI-backed booleans as strings even when the
    # declared default is a bool. Accept only unambiguous representations.
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if type(value) is str and value.casefold() in {"true", "false", "1", "0"}:
        return value.casefold() in {"true", "1"}
    raise ValueError("invalid preference")


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is int:
        number = value
    elif type(value) is str and re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", value):
        number = int(value)
    else:
        raise ValueError("invalid preference")
    if not minimum <= number <= maximum:
        raise ValueError("invalid preference")
    return number


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _role_selection(value: Any) -> str:
    if type(value) is str:
        if len(value.encode("utf-8")) > 128 * 1024 or _has_control(value):
            raise ValueError("invalid preference")
        try:
            data = json.loads(value)
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("invalid preference") from None
    else:
        data = value
    if type(data) is not dict or set(data) != {"jury1", "jury2"}:
        raise ValueError("invalid preference")
    cleaned: dict[str, list[str]] = {}
    for role in ("jury1", "jury2"):
        selectors = data[role]
        if type(selectors) is not list or len(selectors) > _MAX_ROLE_SELECTORS:
            raise ValueError("invalid preference")
        seen: set[str] = set()
        normalized: list[str] = []
        for selector in selectors:
            if (
                type(selector) is not str
                or not selector
                or len(selector) > _MAX_SELECTOR_LENGTH
                or _has_control(selector)
                or selector in seen
            ):
                raise ValueError("invalid preference")
            seen.add(selector)
            normalized.append(selector)
        cleaned[role] = normalized
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))


def _profile_history(value: Any, maximum_items: int) -> list[str]:
    if type(value) not in (list, tuple) or len(value) > maximum_items:
        raise ValueError("invalid preference")
    result: list[str] = []
    for item in value:
        if type(item) is not str or not _PROFILE_SLUG.fullmatch(item) or item in result:
            raise ValueError("invalid preference")
        result.append(item)
    return result


__all__ = ["default_preferences", "preference_registry", "validate_preferences"]
