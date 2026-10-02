# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only

import io

import logging

import sys

import types

from pathlib import Path

from core.fetch.extraction import ocr

def test_rapidocr_setup_suppresses_info_but_preserves_warnings(monkeypatch, tmp_path):
    logger = logging.getLogger("RapidOCR")
    previous_level = logger.level
    previous_handlers = list(logger.handlers)
    previous_propagate = logger.propagate
    output = io.StringIO()
    handler = logging.StreamHandler(output)

    received_params = []
    rapidocr_module = types.ModuleType("rapidocr")
    rapidocr_module.__file__ = str(tmp_path / "rapidocr" / "__init__.py")
    monkeypatch.setitem(sys.modules, "rapidocr", rapidocr_module)
    model_root_dir = str(Path(rapidocr_module.__file__).resolve().parent / "models")

    class Engine:
        def __init__(self, *, params=None):
            received_params.append(params)
            logger.setLevel(logging.INFO)
            if params == {
                "Global.log_level": "warning",
                "Global.model_root_dir": model_root_dir,
            }:
                logger.setLevel(logging.WARNING)
            logger.info("RapidOCR initialized")
            logger.warning("RapidOCR warning")

    Engine.__module__ = "rapidocr.main"

    def load_engine():
        # RapidOCR configures its named logger while the package is imported.
        logger.setLevel(logging.INFO)
        return Engine

    try:
        logger.setLevel(logging.NOTSET)
        logger.handlers = [handler]
        logger.propagate = False
        monkeypatch.setattr(ocr, "_rapidocr_class", load_engine)
        engine = ocr._new_rapidocr_engine()
        assert isinstance(engine, Engine)
        assert "RapidOCR initialized" not in output.getvalue()
        assert "RapidOCR warning" in output.getvalue()
        assert received_params == [{
            "Global.log_level": "warning",
            "Global.model_root_dir": model_root_dir,
        }]
        assert isinstance(received_params[0]["Global.model_root_dir"], str)
        assert logger.level == logging.WARNING
    finally:
        logger.setLevel(previous_level)
        logger.handlers = previous_handlers
        logger.propagate = previous_propagate
