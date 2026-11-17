"""Colour-coded swarm event log with CSV export."""
from __future__ import annotations

import csv
from collections import deque
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Slot
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QMessageBox, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from core.models import SwarmEvent

EVENT_COLORS: dict[str, str] = {
    "leader_election": "#4ADE80",   # green
    "drone_lost": "#F87171",        # red
    "command_sent": "#60A5FA",      # blue
    "failover": "#FACC15",          # yellow
    "drone_recovered": "#2DD4BF",
    "drone_joined": "#9CA3AF",
    "command_failed": "#FB923C",
    "waypoint": "#FBBF24",
    "error": "#FB923C",
    "mqtt": "#9CA3AF",
}
_DEFAULT_COLOR = "#D1D5DB"
_MAX_RECORDS = 20_000


class EventLog(QWidget):
    """Read-only event view (QPlainTextEdit) with Clear and Export-to-CSV buttons."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._records: deque[SwarmEvent] = deque(maxlen=_MAX_RECORDS)

        self._text = QPlainTextEdit(self)
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(_MAX_RECORDS)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._text.setFont(mono)

        clear_btn = QPushButton("Clear", self)
        export_btn = QPushButton("Export to CSV…", self)
        clear_btn.clicked.connect(self.clear)
        export_btn.clicked.connect(self.export_csv)

        buttons = QHBoxLayout()
        buttons.addWidget(clear_btn)
        buttons.addWidget(export_btn)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._text, 1)
        layout.addLayout(buttons)

    @Slot(object)
    def add_event(self, event: SwarmEvent) -> None:
        """Append one event line. Keeps auto-scrolling unless the user scrolled up."""
        self._records.append(event)
        stamp = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")
        who = f"#{event.drone_id}" if event.drone_id is not None else "--"
        line = f"[{stamp}] {event.event_type.upper():<16} {who:<5} {event.message}"

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(EVENT_COLORS.get(event.event_type, _DEFAULT_COLOR)))

        bar = self._text.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        cursor = QTextCursor(self._text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self._text.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(line, fmt)
        if at_bottom:
            bar.setValue(bar.maximum())

    @Slot()
    def clear(self) -> None:
        """Remove all events from the view and memory."""
        self._records.clear()
        self._text.clear()

    @Slot()
    def export_csv(self) -> None:
        """Ask for a file name and write all events as CSV."""
        default = f"swarm_events_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export events", default, "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["timestamp_iso", "epoch_s", "event_type", "drone_id", "message"])
                for e in self._records:
                    w.writerow([datetime.fromtimestamp(e.timestamp).isoformat(timespec="milliseconds"),
                                f"{e.timestamp:.3f}", e.event_type,
                                "" if e.drone_id is None else e.drone_id, e.message])
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
