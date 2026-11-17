"""Pending drone pairing requests with Approve / Deny."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.mqtt_client import MqttClient


class PairingPanel(QWidget):
    """Shows drones that have asked to join; emits approve/deny to the client."""

    approved = Signal(int)

    def __init__(self, client: MqttClient, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._client = client
        self._rows: dict[int, int] = {}     # drone id -> table row

        self._info = QLabel("No pending pairing requests.")
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Drone", "Sensors", "Action"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)

        lay = QVBoxLayout(self)
        lay.addWidget(self._info)
        lay.addWidget(self._table)

        client.registration_requested.connect(self._on_request)

    @Slot(object)
    def _on_request(self, req: dict) -> None:
        did = int(req["id"])
        if did in self._rows:
            return                                      # already awaiting a decision
        caps = (req.get("payload") or {}).get("capabilities", {}) or {}

        row = self._table.rowCount()
        self._table.insertRow(row)
        self._rows[did] = row

        self._table.setItem(row, 0, QTableWidgetItem(f"#{did}"))
        self._table.setItem(row, 1, QTableWidgetItem(self._describe(caps)))

        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        ok = QPushButton("Approve")
        ok.setProperty("primary", True)
        no = QPushButton("Deny")
        ok.clicked.connect(lambda _, i=did: self._decide(i, True))
        no.clicked.connect(lambda _, i=did: self._decide(i, False))
        h.addWidget(ok)
        h.addWidget(no)
        self._table.setCellWidget(row, 2, box)

        self._info.setText(f"{len(self._rows)} pending request(s)")

    @staticmethod
    def _describe(caps: dict) -> str:
        names = [k.upper() for k in ("gps", "bme280", "mpu6050") if caps.get(k)]
        for entry in caps.get("i2c", []) or []:
            n = entry.get("name", "?")
            if n not in names:
                names.append(n)
        return ", ".join(names) or "(no sensors reported)"

    def _decide(self, did: int, allow: bool) -> None:
        self._client.approve_drone(did, allow)
        row = self._rows.pop(did, None)
        if row is not None:
            self._table.removeRow(row)
            # Re-index rows above the removed one.
            self._rows = {d: (r - 1 if r > row else r) for d, r in self._rows.items()}
        self._info.setText(
            f"{len(self._rows)} pending request(s)" if self._rows
            else "No pending pairing requests."
        )
        if allow:
            self.approved.emit(did)