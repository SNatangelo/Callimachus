#!/usr/bin/env python3
# setup_gui.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
setup_gui.py — double-click launcher for the Citation Verifier setup window.

Equivalent to `python run.py configure`, but runnable directly (e.g. double-click where
Python is associated with .py files, or `python3 setup_gui.py`). For servers / ssh use the
headless form:  `python run.py configure --headless --help`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.app.commands.configure import launch_gui  # noqa: E402

if __name__ == "__main__":
    launch_gui()
