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
    def make(*, language="en", **kwargs):
        window = create_desktop_window(language=language, settings_loader=lambda: [], **kwargs)
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


def _history_action_buttons(window):
    return (
        window.history_resume_button,
        window.history_open_report_button,
        window.history_parse_review_button,
        window.history_report_button,
        window.history_verify_fork_button,
        window.history_report_folder_button,
        window.history_export_report_button,
        window.history_delete_run_button,
    )


def _history_action_rows(buttons):
    rows = []
    for button in buttons:
        if not rows or abs(button.geometry().top() - rows[-1][0].geometry().top()) > 1:
            rows.append([])
        rows[-1].append(button)
    return rows


def _assert_history_action_geometry(window, buttons, expected_columns):
    content = window.history_actions_widget
    layout = content.layout()
    rect = layout.contentsRect()
    scroll = window.history_actions_scroll
    rows = _history_action_rows(buttons)
    wrapped = expected_columns < len(buttons)
    expected_rows = [
        min(expected_columns, len(buttons) - offset)
        for offset in range(0, len(buttons), expected_columns)
    ]

    assert scroll.horizontalScrollBar().maximum() == 0
    if wrapped:
        assert content.width() == scroll.viewport().width()
    else:
        assert content.width() <= scroll.viewport().width()
    assert scroll.widgetResizable() is wrapped
    assert [len(row) for row in rows] == expected_rows
    widths = [button.geometry().width() for button in buttons]
    heights = [button.geometry().height() for button in buttons]
    assert max(widths) - min(widths) <= 1, widths
    assert max(heights) - min(heights) <= 1, heights
    if not wrapped:
        natural_width = max(button.sizeHint().width() for button in buttons)
        assert all(abs(width - natural_width) <= 1 for width in widths)
    assert rows[0][0].geometry().left() == rect.left()
    assert rows[0][-1].geometry().right() == rect.right()
    for row_index, row in enumerate(rows):
        assert row[0].geometry().left() == rect.left()
        if row_index:
            for column, button in enumerate(row):
                upper = rows[0][column]
                assert abs(button.geometry().left() - upper.geometry().left()) <= 1
                assert abs(button.geometry().width() - upper.geometry().width()) <= 1
                assert abs(button.geometry().height() - upper.geometry().height()) <= 1

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
def test_compact_history_actions_share_uniform_rows(collection_ui, width):
    app, C, W, T, make = collection_ui
    window = make()
    window.resize(width, 950)
    window.navigation.setCurrentRow(1)
    T.QTest.qWait(300)
    app.processEvents()
    tab = window.pages.widget(1)
    assert tab.width() < 640 or tab.height() < 480, (tab.width(), tab.height())
    buttons = [button for button in _history_action_buttons(window) if not button.isHidden()]
    actions = window.history_actions_widget.layout()
    common_width = max(button.sizeHint().width() for button in buttons)
    spacing = max(0, actions.horizontalSpacing())
    columns = max(
        candidate for candidate in range(1, len(buttons) + 1)
        if candidate * common_width + spacing * (candidate - 1)
        <= window.history_actions_scroll.viewport().width()
    )
    _assert_history_action_geometry(window, buttons, columns)


@pytest.mark.parametrize("width", [760, 1100])
def test_history_actions_fit_at_boundary_and_wide_sizes(collection_ui, width):
    app, C, W, T, make = collection_ui
    window = make()
    window.resize(width, 950)
    window.navigation.setCurrentRow(1)
    T.QTest.qWait(300)
    app.processEvents()

    buttons = [button for button in _history_action_buttons(window) if not button.isHidden()]
    assert buttons
    actions = window.history_actions_widget.layout()
    common_width = max(button.sizeHint().width() for button in buttons)
    spacing = max(0, actions.horizontalSpacing())
    columns = max(
        candidate for candidate in range(1, len(buttons) + 1)
        if candidate * common_width + spacing * (candidate - 1)
        <= window.history_actions_scroll.viewport().width()
    )
    _assert_history_action_geometry(window, buttons, columns)


@pytest.mark.parametrize("language", ["en", "it"])
@pytest.mark.parametrize("visible_count", [7, 8])
def test_history_actions_use_maximum_sequential_columns(collection_ui, language, visible_count):
    app, C, W, T, make = collection_ui
    window = make(language=language)
    window.resize(2200, 1400)
    window.navigation.setCurrentRow(1)
    T.QTest.qWait(250)
    app.processEvents()
    action_buttons = _history_action_buttons(window)
    assert window.history_resume_button.text() == (
        "Resume" if language == "en" else "Riprendi"
    )
    for button in action_buttons:
        button.setVisible(True)
    window.history_parse_review_button.setVisible(visible_count == 8)
    T.QTest.qWait(30)
    app.processEvents()
    buttons = [button for button in action_buttons if not button.isHidden()]
    assert len(buttons) == visible_count

    actions = window.history_actions_widget.layout()
    spacing = max(0, actions.horizontalSpacing())
    wide_width = window.history_actions_scroll.viewport().width()
    common_width = max(button.sizeHint().width() for button in buttons)
    required_wide_width = visible_count * common_width + spacing * (visible_count - 1)
    if wide_width < required_wide_width + 100:
        window.resize(
            window.width() + required_wide_width + 100 - wide_width,
            window.height(),
        )
        T.QTest.qWait(50)
        app.processEvents()
        window._atlas_history_reflow.sync()
        T.QTest.qWait(25)
        app.processEvents()
    wide_width = window.history_actions_scroll.viewport().width()
    assert wide_width >= required_wide_width + 100
    assert window.history_actions_widget.width() < wide_width
    _assert_history_action_geometry(window, buttons, visible_count)

    def resize_to_viewport(target_width):
        for _attempt in range(10):
            current_width = window.history_actions_scroll.viewport().width()
            if abs(current_width - target_width) <= 1:
                break
            window.resize(
                max(360, window.width() + target_width - current_width),
                window.height(),
            )
            T.QTest.qWait(25)
            app.processEvents()
        window._atlas_history_reflow.sync()
        T.QTest.qWait(25)
        app.processEvents()
        return window.history_actions_scroll.viewport().width()

    for columns in (6, 5, 4, 3):
        if columns >= visible_count:
            continue
        target_width = columns * common_width + (columns - 1) * spacing + 12
        actual_width = resize_to_viewport(target_width)
        assert abs(actual_width - target_width) <= 2, (target_width, actual_width)
        assert actual_width < (columns + 1) * common_width + columns * spacing
        _assert_history_action_geometry(window, buttons, columns)


def test_history_action_reflows_after_visibility_and_font_changes(collection_ui):
    app, C, W, T, make = collection_ui
    window = make(language="it")
    window.resize(900, 1200)
    window.navigation.setCurrentRow(1)
    T.QTest.qWait(250)
    app.processEvents()
    buttons = _history_action_buttons(window)
    for button in buttons:
        button.setVisible(True)
    T.QTest.qWait(30)
    app.processEvents()

    window.history_parse_review_button.hide()
    T.QTest.qWait(30)
    app.processEvents()
    visible = [button for button in buttons if not button.isHidden()]
    assert len(visible) == 7
    actions = window.history_actions_widget.layout()
    spacing = max(0, actions.horizontalSpacing())
    common_width = max(button.sizeHint().width() for button in visible)
    columns = max(
        candidate for candidate in range(1, len(visible) + 1)
        if candidate * common_width + spacing * (candidate - 1)
        <= window.history_actions_scroll.viewport().width()
    )
    _assert_history_action_geometry(window, visible, columns)

    window._font_size = 22
    window._apply_theme(False)
    T.QTest.qWait(50)
    app.processEvents()
    visible = [button for button in buttons if not button.isHidden()]
    common_width = max(button.sizeHint().width() for button in visible)
    columns = max(
        candidate for candidate in range(1, len(visible) + 1)
        if candidate * common_width + spacing * (candidate - 1)
        <= window.history_actions_scroll.viewport().width()
    )
    _assert_history_action_geometry(window, visible, columns)

    original_labels = {
        button: button.property("_atlasOriginalText") or button.text()
        for button in visible
    }
    original_styles = {button: button.styleSheet() for button in visible}
    for button, label in original_labels.items():
        button.setText(label)
    natural_width = max(button.sizeHint().width() for button in visible)
    for _attempt in range(10):
        viewport_width = window.history_actions_scroll.viewport().width()
        if viewport_width > natural_width:
            break
        window.resize(
            window.width() + natural_width + 32 - viewport_width,
            window.height(),
        )
        T.QTest.qWait(50)
        app.processEvents()
        window._atlas_history_reflow.sync()
        T.QTest.qWait(50)
        app.processEvents()
    assert window.history_actions_scroll.viewport().width() > natural_width
    assert all(button.text() == original_labels[button] for button in visible)

    font22_wide_window_width = window.width()
    window.resize(380, 850)
    T.QTest.qWait(50)
    app.processEvents()
    window._atlas_history_reflow.sync()
    T.QTest.qWait(50)
    app.processEvents()
    assert window.pages.widget(1).width() < 640
    narrow_viewport_width = window.history_actions_scroll.viewport().width()
    assert narrow_viewport_width > 0

    window.resize(font22_wide_window_width, 1200)
    T.QTest.qWait(50)
    app.processEvents()
    window._atlas_history_reflow.sync()
    T.QTest.qWait(50)
    app.processEvents()

    # Keep the real 22 px layout phase above, then force portable overflow with
    # a temporary button-local font. Choose its size from the measured labels so
    # the full caption exceeds the narrow viewport while each word still fits.
    font_pixel_size = 64
    natural_width = 0
    widest_word_with_decoration = 0
    found_wrapping_font = False
    for _attempt in range(12):
        for button in visible:
            selector = (
                f"QPushButton#{button.objectName()}"
                if button.objectName() else "QPushButton"
            )
            base_style = original_styles[button].rstrip()
            test_rule = f"{selector} {{ font-size: {font_pixel_size}px; }}"
            button.setStyleSheet(
                f"{base_style}\n{test_rule}" if base_style else test_rule
            )
        T.QTest.qWait(30)
        app.processEvents()
        for button, label in original_labels.items():
            button.setText(label)
        app.processEvents()

        natural_width = max(
            max(button.sizeHint().width(), button.minimumSizeHint().width())
            for button in visible
        )
        widest_word_with_decoration = 0
        for button, label in original_labels.items():
            metrics = button.fontMetrics()
            text_width = metrics.horizontalAdvance(label)
            decoration_width = max(0, button.sizeHint().width() - text_width)
            widest_word_with_decoration = max(
                widest_word_with_decoration,
                *(metrics.horizontalAdvance(word) + decoration_width
                  for word in label.split()),
            )
        if (
            natural_width > narrow_viewport_width
            and widest_word_with_decoration <= narrow_viewport_width
        ):
            found_wrapping_font = True
            break
        if natural_width <= narrow_viewport_width:
            font_pixel_size = min(
                256,
                max(
                    font_pixel_size + 1,
                    font_pixel_size * (narrow_viewport_width + 16)
                    // max(1, natural_width) + 1,
                ),
            )
        else:
            font_pixel_size = max(
                8,
                min(
                    font_pixel_size - 1,
                    font_pixel_size * max(1, narrow_viewport_width - 16)
                    // max(1, widest_word_with_decoration),
                ),
            )
    assert found_wrapping_font, (
        font_pixel_size, narrow_viewport_width, natural_width,
        widest_word_with_decoration,
    )
    assert natural_width > narrow_viewport_width
    assert widest_word_with_decoration <= narrow_viewport_width

    for _attempt in range(10):
        for button, label in original_labels.items():
            button.setText(label)
        viewport_width = window.history_actions_scroll.viewport().width()
        if viewport_width > natural_width:
            break
        window.resize(
            window.width() + natural_width + 32 - viewport_width,
            window.height(),
        )
        T.QTest.qWait(50)
        app.processEvents()
        window._atlas_history_reflow.sync()
        T.QTest.qWait(50)
        app.processEvents()
    assert window.history_actions_scroll.viewport().width() > natural_width
    assert all(button.text() == original_labels[button] for button in visible)
    wide_window_width = window.width()

    window.resize(380, 850)
    T.QTest.qWait(50)
    app.processEvents()
    window._atlas_history_reflow.sync()
    T.QTest.qWait(50)
    app.processEvents()
    visible = [button for button in buttons if not button.isHidden()]
    assert window.pages.widget(1).width() < 640
    viewport_width = window.history_actions_scroll.viewport().width()
    assert viewport_width < natural_width
    assert widest_word_with_decoration <= viewport_width
    assert any("\n" in button.text() for button in visible)
    _assert_history_action_geometry(window, visible, 1)
    for button in visible:
        assert " ".join(button.text().split()) == " ".join(original_labels[button].split())

    window.resize(wide_window_width, 1200)
    T.QTest.qWait(50)
    app.processEvents()
    window._atlas_history_reflow.sync()
    T.QTest.qWait(50)
    app.processEvents()
    for button in visible:
        assert button.text() == original_labels[button]

    for button, style in original_styles.items():
        button.setStyleSheet(style)
    T.QTest.qWait(30)
    app.processEvents()
    window._atlas_history_reflow.sync()
    T.QTest.qWait(50)
    app.processEvents()
    for button in visible:
        assert button.text() == original_labels[button]

    for button in buttons:
        button.show()
    T.QTest.qWait(30)
    app.processEvents()
    visible = [button for button in buttons if not button.isHidden()]
    assert len(visible) == 8
    common_width = max(button.sizeHint().width() for button in visible)
    columns = max(
        candidate for candidate in range(1, len(visible) + 1)
        if candidate * common_width + spacing * (candidate - 1)
        <= window.history_actions_scroll.viewport().width()
    )
    _assert_history_action_geometry(window, visible, columns)

    for index, button in enumerate(buttons):
        button.setVisible(index < 3)
    common_width = max(button.sizeHint().width() for button in buttons[:3])
    required_compact_width = 3 * common_width + 2 * spacing + 100
    window.resize(max(window.width(), required_compact_width + 600), 400)
    T.QTest.qWait(50)
    app.processEvents()
    visible = [button for button in buttons if not button.isHidden()]
    assert len(visible) == 3
    assert window.pages.widget(1).height() < 480
    _assert_history_action_geometry(window, visible, 3)
    assert not window.history_actions_scroll.widgetResizable()
    assert window.history_actions_widget.width() < window.history_actions_scroll.viewport().width()

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
