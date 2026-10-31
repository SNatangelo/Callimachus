"""Library deletion targets and compact collection control geometry."""
import pytest

@pytest.fixture
def collection_ui(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore as C, QtWidgets as W, QtTest as T
    from core.gui.desktop import create_desktop_window
    base = C.QSettings
    monkeypatch.setattr(C, "QSettings", lambda *_a, **_k: base(str(tmp_path / "settings.ini"), base.Format.IniFormat))
    app = W.QApplication.instance() or W.QApplication([])
    windows = []
    def make(**kwargs):
        window = create_desktop_window(language="en", settings_loader=lambda: [], **kwargs)
        windows.append(window)
        window.resize(540, 850)
        window.show()
        window.navigation.setCurrentRow(2)
        T.QTest.qWait(250)
        app.processEvents()
        return window
    yield app, C, W, T, make
    for window in windows:
        window.close()
    app.processEvents()

@pytest.mark.parametrize("checked", [False, True])
def test_library_deletes_highlighted_row_or_prioritizes_checkmarks(collection_ui, monkeypatch, checked):
    app, C, W, T, make = collection_ui
    rows = [{"work_id": str(i), "title": "Repeated title", "tier": "fulltext"} for i in range(3)]
    deleted = []
    window = make(cache_loader=lambda: rows, delete_cache_callback=lambda selected: deleted.append(list(selected)))
    assert not window.cache_delete_button.isEnabled()
    if checked:
        for i in (0, 2):
            window.cache_table.item(i, 0).setCheckState(C.Qt.CheckState.Checked)
    window.cache_table.selectRow(1)
    app.processEvents()
    assert window.cache_delete_button.isEnabled()
    monkeypatch.setattr(window, "_confirm_library_action", lambda *_a, **_kw: False)
    window.cache_delete_button.click()
    assert not deleted
    monkeypatch.setattr(window, "_confirm_library_action", lambda *_a, **_kw: True)
    window.cache_delete_button.click()
    assert [[r["work_id"] for r in batch] for batch in deleted] == ([["0", "2"]] if checked else [["1"]])

def test_library_selection_does_not_transfer_to_replaced_rows(collection_ui):
    app, C, W, T, make = collection_ui
    rows = [{"work_id": str(i), "title": str(i), "tier": "fulltext"} for i in range(2)]
    window = make(cache_loader=lambda: rows)
    window.cache_table.selectRow(0)
    app.processEvents()
    assert window.cache_delete_button.isEnabled()
    window.cache_table.clearSelection()
    assert not window.cache_delete_button.isEnabled()
    window.cache_table.selectRow(0)
    window._cache_rows = list(reversed(rows))
    window._render_cache()
    assert not window.cache_delete_button.isEnabled()
    assert not window.cache_table.selectedItems()

@pytest.mark.parametrize("width", [380, 540])
def test_compact_history_delete_uses_the_whole_action_row(collection_ui, width):
    app, C, W, T, make = collection_ui
    window = make()
    window.resize(width, 950)
    window.navigation.setCurrentRow(1)
    T.QTest.qWait(300)
    app.processEvents()
    tab = window.pages.widget(1)
    assert tab.width() < 640 or tab.height() < 480, (tab.width(), tab.height())
    button = window.history_delete_run_button
    content = window.history_actions_widget
    rect = content.layout().contentsRect()
    assert abs(button.geometry().left() - rect.left()) <= 1
    assert abs(button.geometry().right() - rect.right()) <= 1
    assert window.history_actions_scroll.horizontalScrollBar().maximum() == 0


@pytest.mark.parametrize("width", [760, 1100])
def test_history_actions_fit_at_boundary_and_wide_sizes(collection_ui, width):
    app, C, W, T, make = collection_ui
    window = make()
    window.resize(width, 950)
    window.navigation.setCurrentRow(1)
    T.QTest.qWait(300)
    app.processEvents()
    tab = window.pages.widget(1)
    compact = tab.width() < 640 or tab.height() < 480

    content = window.history_actions_widget
    rect = content.layout().contentsRect()
    assert window.history_actions_scroll.horizontalScrollBar().maximum() == 0
    for button in (
        window.history_resume_button,
        window.history_open_report_button,
        window.history_parse_review_button,
        window.history_report_button,
        window.history_verify_fork_button,
        window.history_report_folder_button,
        window.history_export_report_button,
        window.history_delete_run_button,
    ):
        if button.isHidden():
            continue
        assert button.geometry().left() >= rect.left(), (button.objectName(), button.geometry(), rect)
        assert button.geometry().right() <= rect.right(), (button.objectName(), button.geometry(), rect)
    if compact:
        assert abs(window.history_delete_run_button.geometry().left() - rect.left()) <= 1
        assert abs(window.history_delete_run_button.geometry().right() - rect.right()) <= 1

@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("font_size", [16, 22])
def test_library_checkbox_fits_its_cell_at_compact_sizes(collection_ui, dark, font_size):
    app, C, W, T, make = collection_ui
    window = make(cache_loader=lambda: [{"work_id": "one", "title": "Synthetic source", "tier": "fulltext"}])
    window._font_size = font_size
    window._apply_theme(dark)
    window.resize(400, 850)
    T.QTest.qWait(200)
    app.processEvents()
    table = window.cache_table
    index = table.model().index(0, 0)
    option = W.QStyleOptionViewItem()
    delegate = table.itemDelegateForColumn(0) or table.itemDelegate()
    delegate.initStyleOption(option, index)
    option.widget = table
    option.rect = table.visualRect(index)
    indicator = table.style().subElementRect(W.QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, table)
    assert option.rect.contains(indicator), (option.rect, indicator)
    assert indicator.width() >= 20

@pytest.mark.parametrize("dark", [False, True])
def test_disabled_library_delete_has_neutral_text_and_icon(collection_ui, dark):
    app, C, W, T, make = collection_ui
    from PySide6 import QtGui as G
    window = make(cache_loader=lambda: [{"work_id": "one", "title": "One", "tier": "fulltext"}])
    window._apply_theme(dark)
    app.processEvents()
    button = window.cache_delete_button
    assert not button.isEnabled()
    assert "Select at least one" in button.toolTip()
    option = W.QStyleOptionButton()
    button.initStyleOption(option)
    text = option.palette.color(G.QPalette.ColorGroup.Disabled, G.QPalette.ColorRole.ButtonText)
    assert text.red() <= text.blue() + 20
    image = button.icon().pixmap(button.iconSize(), G.QIcon.Mode.Disabled).toImage()
    assert not image.isNull()
    opaque = [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height()) if image.pixelColor(x, y).alpha() > 128]
    assert opaque and all(color.red() <= color.blue() + 20 for color in opaque)
    window.cache_table.selectRow(0)
    assert button.isEnabled()
    assert "Select at least one" not in button.toolTip()

@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_source_next_has_a_visible_button_boundary(collection_ui, dark, enabled):
    app, C, W, T, make = collection_ui
    from PySide6 import QtGui as G
    window = make()
    window._apply_theme(dark)
    button = window._atlas_source_detail_next_button
    button.setEnabled(enabled)
    button.resize(140, 44)
    button.ensurePolished()
    option = W.QStyleOptionToolButton()
    button.initStyleOption(option)
    option.state &= ~(W.QStyle.StateFlag.State_MouseOver | W.QStyle.StateFlag.State_Sunken)
    image = G.QImage(button.size(), G.QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = G.QPainter(image)
    button.style().drawComplexControl(W.QStyle.ComplexControl.CC_ToolButton, option, painter, button)
    painter.end()
    assert image.pixelColor(0, image.height() // 2).name() == ("#34455b" if dark else "#d1def2")
