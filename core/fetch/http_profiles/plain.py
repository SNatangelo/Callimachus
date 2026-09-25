#!/usr/bin/env python3
# core/fetch/http_profiles/plain.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Minimal deterministic HTTP profile."""

from __future__ import annotations

NAME = "plain"


def build_headers(*, url: str, accept: str, user_agent: str,
                  accept_language: str, referer: str | None = None,
                  profile: str = "document") -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": accept,
    }
    if referer:
        headers["Referer"] = referer
    return headers


def consent_cookie(url: str) -> str | None:
    return None
