# core/gui/shell/resize.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Small helpers for keeping Qt table columns user-resizable."""
from __future__ import annotations


def install_table_resize(
    table, QtCore, QtWidgets, *, stretch_last_section: bool = False,
    preferred_minimum_widths: dict[int, int] | None = None,
    fill_column: int | None = None,
) -> None:
    """Keep initial column proportions, then allow users to resize every section.

    Call this after the view has received its initial column widths. The first
    visible layout is fitted before the stretch modes are frozen. Repeated
    calls are intentionally harmless so refreshes cannot reset user widths.
    An optional fill_column uses spare viewport width on resize without freezing
    the other sections or shrinking them below their preferred minimum widths.
    """
    if (getattr(table, "_atlas_resize_installed", False)
            or getattr(table, "_atlas_resize_filter", None) is not None):
        return

    header_getter = getattr(table, "horizontalHeader", None)
    if not callable(header_getter):
        header_getter = getattr(table, "header", None)
    if not callable(header_getter):
        return
    header = header_getter()
    if header is None:
        return
    if fill_column is not None and (not 0 <= fill_column < header.count() or stretch_last_section):
        raise ValueError("fill_column must name one section and cannot combine with trailing stretch")
    preferred_minimum_widths = {
        column: max(1, int(width))
        for column, width in (preferred_minimum_widths or {}).items()
    }
    header_viewport = header.viewport()
    requested_widths = [header.sectionSize(column) for column in range(header.count())]
    if stretch_last_section:
        header.setStretchLastSection(True)

    class _InitialLayoutFilter(QtCore.QObject):
        def __init__(self):
            super().__init__(table)
            self._queued = False
            self._abandoned = False
            table.destroyed.connect(self._mark_destroyed)

        def eventFilter(self, watched, event):
            destroy_type = getattr(QtCore.QEvent.Type, "Destroy", None)
            if destroy_type is not None and event.type() == destroy_type:
                self._abandoned = True
                return False
            if (watched is table
                    and event.type() == QtCore.QEvent.Type.Show):
                self._queue_freeze()
            elif (watched is header_viewport
                  and event.type() == QtCore.QEvent.Type.MouseButtonPress):
                # If the user reaches the header before the queued first-show
                # callback, freeze its already-laid-out sections first.
                self._freeze()
            return False

        def _queue_freeze(self):
            if self._queued or self._abandoned:
                return
            self._queued = True
            QtCore.QTimer.singleShot(0, self._freeze)

        def _freeze(self):
            self._queued = False
            if self._abandoned or getattr(table, "_atlas_resize_installed", False):
                return
            widths = [header.sectionSize(column) for column in range(header.count())]
            stretch_columns = []
            stretch_mode = QtWidgets.QHeaderView.ResizeMode.Stretch
            for column in range(header.count()):
                hidden = bool(
                    getattr(table, "isColumnHidden", lambda _column: False)(column)
                )
                if not hidden and header.sectionResizeMode(column) == stretch_mode:
                    stretch_columns.append(column)
            if stretch_last_section:
                last_visible = next((
                    header.logicalIndex(visual)
                    for visual in range(header.count() - 1, -1, -1)
                    if not getattr(table, "isColumnHidden", lambda _column: False)(
                        header.logicalIndex(visual)
                    )
                ), None)
                if (last_visible is not None and last_visible not in stretch_columns
                        and last_visible < len(requested_widths)):
                    # Native trailing fill may expand this section before the
                    # first Show. Fit the title against the requested metadata
                    # width, not that temporary share of the empty viewport.
                    widths[last_visible] = requested_widths[last_visible]
            # Preserve the pre-existing first-show fit for callers that set
            # stretchLastSection themselves. The opt-in fill section stays
            # independent so it does not take width from the title.
            if not stretch_last_section and header.stretchLastSection():
                last_visible = next((
                    header.logicalIndex(visual)
                    for visual in range(header.count() - 1, -1, -1)
                    if not getattr(table, "isColumnHidden", lambda _column: False)(
                        header.logicalIndex(visual)
                    )
                ), None)
                if last_visible is not None and last_visible not in stretch_columns:
                    stretch_columns.append(last_visible)
            # A section marked Stretch can still report its pre-show width
            # during the first Show event. Compute the final initial fit from
            # the now-visible viewport before switching to Interactive.
            if stretch_columns:
                visible_width = max(0, header_viewport.width())
                fixed_width = sum(
                    widths[column]
                    for column in range(header.count())
                    if column not in stretch_columns
                    and not getattr(table, "isColumnHidden", lambda _column: False)(column)
                )
                remaining = max(0, visible_width - fixed_width)
                quotient, remainder = divmod(remaining, len(stretch_columns))
                for offset, column in enumerate(stretch_columns):
                    widths[column] = max(
                        preferred_minimum_widths.get(column, 1),
                        quotient + (1 if offset < remainder else 0),
                    )

            header.setStretchLastSection(stretch_last_section)
            for column, width in enumerate(widths):
                header.setSectionResizeMode(
                    column, QtWidgets.QHeaderView.ResizeMode.Interactive
                )
                if width > 0:
                    header.resizeSection(column, width)
            table._atlas_resize_installed = True
            content_fill = getattr(table, "_atlas_content_fill_filter", None)
            if content_fill is not None:
                content_fill._fit()
            table._atlas_resize_filter = None
            table.removeEventFilter(self)
            header_viewport.removeEventFilter(self)
            self.deleteLater()
            if stretch_last_section:
                fill_filter = _LastVisibleSectionFillFilter(stretch_columns)
                table._atlas_last_section_fill_filter = fill_filter
                table.installEventFilter(fill_filter)
                header.installEventFilter(fill_filter)
                header_viewport.installEventFilter(fill_filter)
                fill_filter._adjust()

        def _mark_destroyed(self, *_args):
            self._abandoned = True

    class _LastVisibleSectionFillFilter(QtCore.QObject):
        """Keep the opted-in trailing section fitted after the first layout."""

        def __init__(self, preferred_columns):
            super().__init__(table)
            self._queued = False
            self._adjusting = False
            self._abandoned = False
            self._preferred_columns = set(preferred_columns)
            self._preferred_minimum_widths = preferred_minimum_widths
            self._base_widths = {
                column: max(
                    self._minimum_width(column),
                    header.sectionSize(column),
                    self._label_width(column)
                    if column in self._preferred_columns else 0,
                )
                for column in range(header.count())
            }
            table.destroyed.connect(self._mark_destroyed)
            header.sectionResized.connect(self._section_resized)

        def _label_width(self, column):
            label = header.model().headerData(
                column,
                QtCore.Qt.Orientation.Horizontal,
                QtCore.Qt.ItemDataRole.DisplayRole,
            )
            label = "" if label is None else str(label)
            if not label:
                return 0
            indicator_width = 0
            if (
                header.isSortIndicatorShown()
                and header.sortIndicatorSection() == column
            ):
                indicator_width = 16
            return max(
                header.fontMetrics().horizontalAdvance(label) + 16 + indicator_width,
                header.sectionSizeHint(column),
            )

        def _minimum_width(self, column):
            minimum = max(1, header.minimumSectionSize(), self._preferred_minimum_widths.get(column, 1))
            if column in self._preferred_columns:
                return max(
                    minimum,
                    self._preferred_minimum_widths.get(column, minimum),
                )
            return max(minimum, self._label_width(column))

        def eventFilter(self, watched, event):
            event_type = event.type()
            if (
                not self._abandoned
                and watched in (table, header, header_viewport)
                and event_type in (
                    QtCore.QEvent.Type.Resize,
                    QtCore.QEvent.Type.FontChange,
                    QtCore.QEvent.Type.StyleChange,
                )
            ):
                self._queue_adjust()
            return False

        def _last_visible(self):
            return next((
                header.logicalIndex(visual)
                for visual in range(header.count() - 1, -1, -1)
                if not getattr(table, "isColumnHidden", lambda _column: False)(
                    header.logicalIndex(visual)
                )
            ), None)

        def _section_resized(self, column, _old_size, _new_size):
            if self._adjusting or self._abandoned:
                return
            if column != self._last_visible() and not getattr(
                table, "isColumnHidden", lambda _column: False
            )(column):
                self._base_widths[column] = _new_size
                self._queue_adjust()

        def _queue_adjust(self):
            if self._queued or self._abandoned:
                return
            self._queued = True
            QtCore.QTimer.singleShot(0, self._adjust)

        def _adjust(self):
            self._queued = False
            if self._abandoned or not getattr(table, "_atlas_resize_installed", False):
                return
            last_visible = self._last_visible()
            if last_visible is None:
                return
            visible = [
                header.logicalIndex(visual)
                for visual in range(header.count())
                if not getattr(table, "isColumnHidden", lambda _column: False)(
                    header.logicalIndex(visual)
                )
            ]
            section_minimum = max(1, header.minimumSectionSize())
            minimums = [self._minimum_width(column) for column in visible]
            if sum(minimums) > max(0, header_viewport.width()):
                # Preserve label widths when the table can fit them. On a
                # narrower viewport, let the header labels elide while keeping
                # any explicitly preferred title width usable.
                minimums = [
                    max(
                        section_minimum,
                        self._preferred_minimum_widths.get(
                            column, section_minimum
                        ),
                    )
                    for column in visible
                ]
            base_widths = [
                max(minimum, self._base_widths.get(
                    column, header.sectionSize(column)
                ), self._label_width(column)
                if column in self._preferred_columns else 0)
                for column, minimum in zip(visible, minimums)
            ]
            available = max(0, header_viewport.width())
            deficit = max(0, sum(base_widths) - available)
            widths = list(base_widths)
            if deficit:
                last_capacity = widths[-1] - minimums[-1]
                last_reduction = min(deficit, last_capacity)
                widths[-1] -= last_reduction
                deficit -= last_reduction
                capacities = [
                    width - minimum
                    for width, minimum in zip(widths[:-1], minimums[:-1])
                ]
                total_capacity = sum(capacities)
                if deficit and total_capacity:
                    if deficit >= total_capacity:
                        widths[:-1] = minimums[:-1]
                    else:
                        reductions = [
                            deficit * capacity // total_capacity
                            for capacity in capacities
                        ]
                        remainder = deficit - sum(reductions)
                        for index, capacity in enumerate(capacities):
                            if remainder <= 0:
                                break
                            if reductions[index] < capacity:
                                reductions[index] += 1
                                remainder -= 1
                        widths[:-1] = [
                            width - reduction
                            for width, reduction in zip(widths[:-1], reductions)
                        ]
            else:
                widths[-1] = max(
                    minimums[-1], available - sum(widths[:-1])
                )

            self._adjusting = True
            try:
                for column, width in zip(visible, widths):
                    if header.sectionSize(column) != width:
                        header.resizeSection(column, width)
            finally:
                self._adjusting = False

        def _mark_destroyed(self, *_args):
            self._abandoned = True

    class _ContentColumnFillFilter(QtCore.QObject):
        """Fit an opted-in content column; leave explicit user widths on refresh."""

        def __init__(self):
            super().__init__(table)
            self._adjusting = False
            self._abandoned = False
            self._last_width = None
            self._manual_content_width = None
            self._viewport = table.viewport()
            self._timer = QtCore.QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._fit)
            table.destroyed.connect(self._mark_destroyed)
            header.sectionResized.connect(self._section_resized)

        def _minimum(self, column):
            return max(
                header.minimumSectionSize(), header.sectionSizeHint(column),
                preferred_minimum_widths.get(column, 1),
            )

        def eventFilter(self, watched, event):
            if not self._abandoned and (
                event.type() in (QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange)
                or (watched is self._viewport and event.type() == QtCore.QEvent.Type.Resize
                    and self._viewport.width() != self._last_width)
            ):
                self._timer.start(0)
            return False

        def _section_resized(self, column, _old, width):
            if self._adjusting or self._abandoned or not getattr(table, "_atlas_resize_installed", False):
                return
            if column == fill_column:
                self._manual_content_width = width
            if column != fill_column or width < self._minimum(column):
                self._timer.start(0)

        def _fit(self):
            if self._abandoned or not getattr(table, "_atlas_resize_installed", False):
                return
            if table.isColumnHidden(fill_column):
                return
            self._last_width = table.viewport().width()
            self._adjusting = True
            try:
                other_width = 0
                for column in range(header.count()):
                    if column == fill_column or table.isColumnHidden(column):
                        continue
                    width = max(self._minimum(column), header.sectionSize(column))
                    header.resizeSection(column, width)
                    other_width += width
                available = min(self._last_width, header_viewport.width())
                content_width = (
                    available - other_width if self._manual_content_width is None
                    else self._manual_content_width
                )
                header.resizeSection(fill_column, max(self._minimum(fill_column), content_width))
            finally:
                self._adjusting = False

        def _mark_destroyed(self, *_args):
            self._abandoned = True

    if fill_column is not None:
        content_filter = _ContentColumnFillFilter()
        table._atlas_content_fill_filter = content_filter
        table.installEventFilter(content_filter)
        table.viewport().installEventFilter(content_filter)

    resize_filter = _InitialLayoutFilter()
    table._atlas_resize_filter = resize_filter
    table.installEventFilter(resize_filter)
    header_viewport.installEventFilter(resize_filter)
    if table.isVisible():
        resize_filter._queue_freeze()
