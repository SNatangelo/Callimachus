# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import time

import pytest

from core.app.commands.desktop import _docs_library
from core.gui.desktop import create_desktop_window, load_translations


@pytest.fixture(autouse=True)
def isolated_preferences(monkeypatch, tmp_path):
    QtCore = pytest.importorskip("PySide6.QtCore")
    original = QtCore.QSettings

    class LocalSettings(original):
        def __init__(self, *args):
            if args == ("Callimachus", "Callimachus"):
                super().__init__(str(tmp_path / "preferences.ini"), original.Format.IniFormat)
            else:
                super().__init__(*args)

    monkeypatch.setattr(QtCore, "QSettings", LocalSettings)


def test_privacy_documents_are_in_the_bundled_library():
    documents = _docs_library(Path(__file__).resolve().parents[1])
    assert "## Your controls" in documents["privacy.md"]
    assert "## Controlli disponibili" in documents["privacy-it.md"]


@pytest.mark.parametrize("language,document", [("en", "privacy.md"), ("it", "privacy-it.md")])
def test_info_privacy_link_loads_docs_only_when_requested(monkeypatch, language, document):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton

    app = QApplication.instance() or QApplication([])
    calls = []
    documents = _docs_library(Path(__file__).resolve().parents[1])
    documents["Third-party notices.md"] = "# Third-party notices\n\nBundled license index."
    window = create_desktop_window(
        language=language,
        background_loading=True,
        docs_loader=lambda: calls.append("docs") or documents,
    )
    try:
        assert not calls
        assert all(
            label.text() != load_translations(language)["online_use_notice"]
            for label in window.start_button.parentWidget().findChildren(QLabel)
        )
        assert all(
            button.text() != load_translations(language)["privacy_policy"]
            for button in window.start_button.parentWidget().findChildren(QPushButton)
        )
        window.privacy_button.click()
        deadline = time.monotonic() + 3
        while window.docs_stack.currentIndex() != 0 and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert calls == ["docs"]
        assert window.navigation.currentRow() == 5
        assert window._current_doc_path == document
        guide_paths = [
            f"{index:02d}-{name}.md"
            for index, name in enumerate((
                "quickstart", "cli-reference", "configuration", "pipeline",
                "tasks-and-recovery", "verification-and-evidence",
                "artifacts-and-provenance", "capabilities-and-limits",
                "troubleshooting", "keys-and-credentials", "environment-reference",
                "desktop-packages",
            ), 1)
        ]
        menu_paths = [
            window.docs_outline.topLevelItem(index).data(
                0, QtCore.Qt.ItemDataRole.UserRole
            )[0]
            for index in range(window.docs_outline.topLevelItemCount())
        ]
        expected_paths = [
            "README.md", *guide_paths, document, "Third-party notices.md"
        ]
        assert menu_paths == expected_paths
        assert sum(path in {"privacy.md", "privacy-it.md"} for path in menu_paths) == 1
        hidden_paths = {
            "../deployment/agent-guide.md",
            "../deployment/administrator-guide.md",
            "../deployment/microsoft-store.md",
            "../architecture/artifact-integrity.md",
            "../../DEPLOYMENT.md",
        }
        assert hidden_paths <= window._documents.keys()
        assert not hidden_paths.intersection(menu_paths)

        chapter_item = next(
            window.docs_outline.topLevelItem(index)
            for index in range(window.docs_outline.topLevelItemCount())
            if window.docs_outline.topLevelItem(index).data(
                0, QtCore.Qt.ItemDataRole.UserRole
            )[0] == "10-keys-and-credentials.md"
        )
        window.docs_outline.setCurrentItem(chapter_item)
        window._open_doc_link(QtCore.QUrl("../../DEPLOYMENT.md"))
        assert window._current_doc_path == "../../DEPLOYMENT.md"
        deployment_title = next(
            line[2:].strip()
            for line in documents["../../DEPLOYMENT.md"].splitlines()
            if line.startswith("# ")
        )
        assert deployment_title in window.docs_view.toPlainText()
        assert window.docs_outline.currentItem() is None
        window._open_doc_link(QtCore.QUrl("docs/guide/10-keys-and-credentials.md"))
        assert window._current_doc_path == "10-keys-and-credentials.md"

        window.privacy_button.click()
        assert calls == ["docs"]
        assert window._current_doc_path == document
    finally:
        window.close()
        app.processEvents()


def test_ai_reporting_opens_only_a_static_issue_form(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtGui
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    opened = []
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    window = create_desktop_window(language="en")
    try:
        window._paper_path = "private-manuscript.pdf"
        window.ai_report_button.click()
        assert opened == ["https://github.com/SNatangelo/Callimachus/issues/new?title=AI content concern"]
        assert "public" in window.ai_report_button.toolTip()
    finally:
        window.close()
        app.processEvents()
