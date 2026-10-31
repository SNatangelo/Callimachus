#!/usr/bin/env python3
# core/fetch/institutional_access.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Validated configuration for opt-in institutional browser access."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from core.app.runtime_paths import user_data_root

ENV_ENABLED = "CALLIMACHUS_INSTITUTIONAL_FETCH"
ENV_PROFILE = "CALLIMACHUS_INSTITUTIONAL_PROFILE"
ENV_INSTITUTION = "CALLIMACHUS_INSTITUTION"
ENV_LOGIN_URL = "CALLIMACHUS_INSTITUTIONAL_LOGIN_URL"

_DEFAULT_PROFILE = "guided_fetch"
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _profile_slug(value) -> str:
    slug = str(value or _DEFAULT_PROFILE).strip().lower()
    if not _PROFILE_RE.fullmatch(slug):
        raise ValueError("institutional browser profile must be a 1-48 character slug")
    return slug


def _login_url(value) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if len(url) > 2048 or _has_control_characters(url):
        raise ValueError("institutional login URL must not contain control characters")
    try:
        parsed = urlsplit(url)
        # Accessing port also rejects malformed authority syntax.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("institutional login URL is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(character.isspace() for character in parsed.netloc)
    ):
        raise ValueError("institutional login URL must be HTTPS without user information")
    # Persist the stable library entry point, not a copied SSO callback carrying
    # a short-lived assertion or bearer credential. EZproxy's `url` is allowed.
    sensitive = {"token", "access_token", "id_token", "code", "ticket", "assertion", "samlresponse", "samlrequest", "relaystate", "session", "sessionid"}
    fragment_query = parsed.fragment.partition("?")[2] or parsed.fragment.lstrip("?")
    if any(name.casefold() in sensitive for name, _ in (
        parse_qsl(parsed.query) + parse_qsl(fragment_query)
    )):
        raise ValueError("institutional login URL must be a stable portal entry point")
    return url


def config(environ=None) -> dict:
    """Return normalized settings, rejecting unsafe URL and profile values."""
    env = os.environ if environ is None else environ
    institution = str(env.get(ENV_INSTITUTION) or "").strip()
    if len(institution) > 160 or _has_control_characters(institution):
        raise ValueError("institution label must not contain control characters")
    enabled_value = str(env.get(ENV_ENABLED) or "").strip().lower()
    return {
        "enabled": enabled_value in {"1", "true", "yes", "on"},
        "profile": _profile_slug(env.get(ENV_PROFILE)),
        "institution": institution,
        "login_url": _login_url(env.get(ENV_LOGIN_URL)),
    }


def profile_directory(profile=None) -> Path:
    """Return a persistent profile directory confined to application data."""
    selected = _profile_slug(os.environ.get(ENV_PROFILE) if profile is None else profile)
    base = Path(user_data_root()) / "browser_profiles"
    directory = base / selected
    try:
        directory.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError("institutional profile resolves outside browser profiles") from exc
    return directory
