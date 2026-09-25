# core/report/human/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic, presentation-neutral report projections."""

from .projection import HumanReportProjection, build_human_report_projection
from .render import RenderedHumanReport, render_human_report

__all__ = (
    "HumanReportProjection",
    "RenderedHumanReport",
    "build_human_report_projection",
    "render_human_report",
)
