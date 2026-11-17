"""Sensor inventory and live readings for one approved drone."""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import Slot, QTimer
from PySide6.QtWidgets import (QFormLayout, QGroupBox, QLabel, QVBoxLayout, QWidget)

from core.state import DroneRegistry


class SensorPanel(QWidget):
    """Static capabilities + live readings for the currently selected drone."""

    def __init__(self, registry: DroneRegistry, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._registry = registry
        self._did: Optional[int] = None

        self._caps = QLabel("Select a drone.")
        self._caps.setWordWrap(True)

        self._live = QFormLayout()
        self._live_widget = QWidget()
        self._live_widget.setLayout(self._live)

        caps_box = QGroupBox("Sensors detected at pairing")
        cb = QVBoxLayout(caps_box)
        cb.addWidget(self._caps)

        live_box = QGroupBox("Live readings")
        lb = QVBoxLayout(live_box)
        lb.addWidget(self._live_widget)

        lay = QVBoxLayout(self)
        lay.addWidget(caps_box)
        lay.addWidget(live_box)
        lay.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    def set_drone(self, did: Optional[int]) -> None:
        self._did = None if did is None else int(did)
        caps = self._registry.capabilities(self._did) if self._did is not None else {}
        if not caps:
            self._caps.setText("No sensor inventory recorded for this drone.")
        else:
            names = [k.upper() for k in ("gps", "bme280", "mpu6050") if caps.get(k)]
            for entry in caps.get("i2c", []) or []:
                names.append(f"{entry.get('name', '?')} ({entry.get('addr', '?')})")
            self._caps.setText("• " + "\n• ".join(names) if names
                               else "No named sensors reported.")
        self._refresh()

    @Slot()
    def _refresh(self) -> None:
        while self._live.rowCount():
            self._live.removeRow(0)
        if self._did is None:
            return
        d = self._registry.get(self._did)
        if d is None:
            return

        rows = [
            ("Position", f"{d.lat:.6f}, {d.lon:.6f}" if d.has_position else "—"),
            ("Altitude", f"{d.alt:.1f} m" if math.isfinite(d.alt) else "—"),
            ("Battery", f"{d.battery:.0f} %" if math.isfinite(d.battery) else "—"),
            ("RSSI Wi-Fi", f"{d.rssi_wifi:.0f} dBm" if math.isfinite(d.rssi_wifi) else "—"),
            ("RSSI LoRa", f"{d.rssi_lora:.0f} dBm" if math.isfinite(d.rssi_lora) else "—"),
            ("Transport", (d.transport or "—").upper()),
            ("Role", d.role or "—"),
        ]
        for label, value in rows:
            self._live.addRow(QLabel(label), QLabel(value))