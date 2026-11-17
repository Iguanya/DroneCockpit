"""Drone list: QListView + model + custom delegate."""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QPointF, QRectF, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QListView, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QWidget)

from core.models import (ROLE_COLORS, STATUS_ACTIVE, STATUS_LOST, STATUS_STALE, display_state)
from core.state import DroneRegistry

#: Row background per liveness state (Tailwind gray-800 / yellow-900 / red-900).
ROW_BG = {STATUS_ACTIVE: "#1F2937", STATUS_STALE: "#713F12", STATUS_LOST: "#7F1D1D"}
ROW_HEIGHT = 54


@dataclass(frozen=True)
class DroneRow:
    """Immutable view of one drone for painting."""

    id: int
    state: str          # LEADER / FOLLOWER / CANDIDATE / LOST / LORA_ONLY
    status: str         # active / stale / lost
    battery: float
    rssi: float
    transport: str


def rssi_level(rssi: float, transport: str) -> int:
    """Signal strength 0..4 bars (LoRa has a much lower noise floor than Wi-Fi)."""
    steps = (-90, -80, -70, -60) if transport == "wifi" else (-120, -110, -100, -90)
    return sum(1 for s in steps if rssi > s) if rssi > -127 else 0


class DroneListModel(QAbstractListModel):
    """Sorted list of drones fed by the registry.

    Registry signals only mark ids pending; a 200 ms timer applies them, so a
    flood of telemetry costs one model update per drone per tick.
    """

    ROW_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, registry: DroneRegistry, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._registry = registry
        self._ids: list[int] = []
        self._rows: dict[int, DroneRow] = {}
        self._pending: set[int] = set()
        registry.drone_updated.connect(self._on_updated)
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    def _on_updated(self, drone_id: int) -> None:
        self._pending.add(drone_id)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:      # noqa: N802
        return 0 if parent.isValid() else len(self._ids)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._ids):
            return None
        row = self._rows[self._ids[index.row()]]
        if role == self.ROW_ROLE:
            return row
        if role == Qt.ItemDataRole.DisplayRole:
            return f"Drone #{row.id}"
        return None

    def row_of(self, drone_id: int) -> int:
        """Row index of ``drone_id`` or -1."""
        i = bisect.bisect_left(self._ids, drone_id)
        return i if i < len(self._ids) and self._ids[i] == drone_id else -1

    def id_at(self, row: int) -> Optional[int]:
        """Drone id at ``row`` or ``None``."""
        return self._ids[row] if 0 <= row < len(self._ids) else None

    def _build(self, drone_id: int) -> Optional[DroneRow]:
        d = self._registry.get(drone_id)
        if d is None:
            return None
        status = self._registry.status(drone_id)
        rssi = d.rssi_lora if d.transport == "lora" else d.rssi_wifi
        return DroneRow(drone_id, display_state(d, status), status, d.battery, rssi, d.transport)

    def _flush(self) -> None:
        if not self._pending:
            return
        pending, self._pending = self._pending, set()
        for did in sorted(pending):
            row = self._build(did)
            if row is None:
                continue
            pos = self.row_of(did)
            if pos < 0:
                pos = bisect.bisect_left(self._ids, did)
                self.beginInsertRows(QModelIndex(), pos, pos)
                self._ids.insert(pos, did)
                self._rows[did] = row
                self.endInsertRows()
            elif row != self._rows[did]:
                self._rows[did] = row
                idx = self.index(pos)
                self.dataChanged.emit(idx, idx)


class DroneDelegate(QStyledItemDelegate):
    """Paints: [quad icon] Drone #ID [role badge] / [battery bar] % ... [transport] [RSSI bars]."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:   # noqa: N802
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row: Optional[DroneRow] = index.data(DroneListModel.ROW_ROLE)
        if row is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        r = QRectF(option.rect).adjusted(3, 2, -3, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(QPen(QColor("#60A5FA"), 2) if selected else Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ROW_BG.get(row.status, ROW_BG[STATUS_ACTIVE])))
        painter.drawRoundedRect(r, 6, 6)

        accent = QColor(ROLE_COLORS.get(row.state, "#9CA3AF"))
        self._paint_icon(painter, QPointF(r.left() + 24, r.center().y()), accent)

        # --- line 1: id (+ STALE tag) and role badge
        base = QFont(option.font)
        bold = QFont(base)
        bold.setBold(True)
        top = QRectF(r.left() + 48, r.top() + 4, r.width() - 52, 20)
        painter.setFont(bold)
        painter.setPen(QColor("#F9FAFB"))
        painter.drawText(top, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         f"Drone #{row.id}")
        if row.status == STATUS_STALE:
            painter.setPen(QColor("#FDE047"))
            painter.drawText(top.adjusted(78, 0, 0, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "STALE")

        small = QFont(base)
        small.setPointSizeF(max(7.0, base.pointSizeF() - 2.5))
        small.setBold(True)
        painter.setFont(small)
        badge_w = painter.fontMetrics().horizontalAdvance(row.state) + 14
        badge = QRectF(r.right() - badge_w - 6, r.top() + 6, badge_w, 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(badge, 8, 8)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, row.state)

        # --- line 2: battery bar + %, transport letter, RSSI bars
        y2 = r.top() + 33
        bar = QRectF(r.left() + 48, y2, 70, 9)
        painter.setPen(QPen(QColor("#6B7280"), 1))
        painter.setBrush(QColor("#111827"))
        painter.drawRoundedRect(bar, 3, 3)
        if math.isfinite(row.battery):
            frac = max(0.0, min(1.0, row.battery / 100.0))
            color = "#22C55E" if row.battery > 50 else "#F59E0B" if row.battery > 20 else "#EF4444"
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(bar.left() + 1, bar.top() + 1,
                                           max(0.0, (bar.width() - 2) * frac), bar.height() - 2), 2, 2)
            label = f"{row.battery:.0f}%"
        else:
            label = "—"
        painter.setFont(base)
        painter.setPen(QColor("#D1D5DB"))
        painter.drawText(QRectF(bar.right() + 6, y2 - 4, 44, 17),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)

        level = rssi_level(row.rssi, row.transport)
        bx = r.right() - 28
        by = r.bottom() - 8
        for i in range(4):
            h = 4 + 3 * i
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#22C55E" if level >= 3 else "#F59E0B" if level == 2 else "#EF4444")
                             if i < level else QColor("#4B5563"))
            painter.drawRect(QRectF(bx + i * 6, by - h, 4, h))
        painter.setFont(small)
        painter.setPen(QColor("#9CA3AF"))
        painter.drawText(QRectF(bx - 18, y2 - 4, 16, 17), Qt.AlignmentFlag.AlignCenter,
                         "W" if row.transport == "wifi" else "L")
        painter.restore()

    @staticmethod
    def _paint_icon(p: QPainter, c: QPointF, color: QColor) -> None:
        """Tiny quadcopter glyph."""
        p.setPen(QPen(color, 2))
        p.drawLine(QPointF(c.x() - 8, c.y() - 8), QPointF(c.x() + 8, c.y() + 8))
        p.drawLine(QPointF(c.x() - 8, c.y() + 8), QPointF(c.x() + 8, c.y() - 8))
        p.setBrush(QColor("#111827"))
        for dx, dy in ((-9, -9), (9, -9), (-9, 9), (9, 9)):
            p.drawEllipse(QPointF(c.x() + dx, c.y() + dy), 4.5, 4.5)
        p.setBrush(color)
        p.drawEllipse(c, 3.5, 3.5)


class DroneListWidget(QListView):
    """QListView of drones.

    Signals:
        drone_selected(int): the user clicked a row or moved the selection with the keyboard.
    """

    drone_selected = Signal(int)

    def __init__(self, registry: DroneRegistry, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = DroneListModel(registry, self)
        self.setModel(self._model)
        self.setItemDelegate(DroneDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)
        self.setSpacing(2)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._programmatic = False
        self.clicked.connect(self._on_clicked)                     # also fires when re-clicking the same row
        self.selectionModel().currentChanged.connect(self._on_current_changed)

    def selected_id(self) -> Optional[int]:
        """Currently selected drone id."""
        return self._model.id_at(self.currentIndex().row()) if self.currentIndex().isValid() else None

    def select_drone(self, drone_id: int) -> None:
        """Select a drone programmatically (does not emit ``drone_selected``)."""
        row = self._model.row_of(drone_id)
        if row < 0:
            return
        self._programmatic = True
        try:
            idx = self._model.index(row)
            self.setCurrentIndex(idx)
            self.scrollTo(idx)
        finally:
            self._programmatic = False

    def _on_clicked(self, index: QModelIndex) -> None:
        did = self._model.id_at(index.row())
        if did is not None:
            self.drone_selected.emit(did)

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._programmatic or not current.isValid():
            return
        did = self._model.id_at(current.row())
        if did is not None:
            self.drone_selected.emit(did)
