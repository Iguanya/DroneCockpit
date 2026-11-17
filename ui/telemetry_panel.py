"""Selected-drone telemetry: labels, rolling charts and command buttons."""
from __future__ import annotations

import math
import time
from functools import partial
from typing import Callable, Optional

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGridLayout,
                               QInputDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from core.models import STATUS_ACTIVE, DroneCommand, display_state
from core.mqtt_client import MqttClient
from core.state import DroneRegistry

#: Commands that need an explicit "are you sure".
_CONFIRM = {"LAND", "RTH"}


class GotoDialog(QDialog):
    """Latitude / longitude / altitude input for GOTO."""

    def __init__(self, lat: float, lon: float, alt: float, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("GOTO")
        form = QFormLayout()
        self._lat = self._spin(-90.0, 90.0, 7, lat, "°")
        self._lon = self._spin(-180.0, 180.0, 7, lon, "°")
        self._alt = self._spin(1.0, 500.0, 1, alt, " m")
        form.addRow("Latitude", self._lat)
        form.addRow("Longitude", self._lon)
        form.addRow("Altitude", self._alt)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)

    @staticmethod
    def _spin(lo: float, hi: float, decimals: int, value: float, suffix: str) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(decimals)
        s.setSuffix(suffix)
        s.setValue(value)
        s.setMinimumWidth(160)
        return s

    def values(self) -> tuple[float, float, float]:
        """(lat, lon, alt) entered by the operator."""
        return self._lat.value(), self._lon.value(), self._alt.value()


class TelemetryPanel(QWidget):
    """Details, charts and command buttons for the selected drone."""

    def __init__(self, registry: DroneRegistry, client: MqttClient, history_seconds: float = 300.0,
                 dark: bool = True, parent: Optional[QWidget] = None) -> None:
        """Create the panel.

        Args:
            registry: Source of drone state and history.
            client: Used to publish commands.
            history_seconds: Width of the rolling chart window.
            dark: Use dark chart colours.
        """
        super().__init__(parent)
        self._registry = registry
        self._client = client
        self._history = float(history_seconds)
        self._drone_id: Optional[int] = None
        self._wp_provider: Callable[[], Optional[tuple[float, float]]] = lambda: None

        self._names = ("ID", "Role", "Transport", "Battery", "Altitude", "Speed",
                       "Lat / Lon", "RSSI Wi-Fi / LoRa", "Last heartbeat")
        self._labels = {n: QLabel("—") for n in self._names}
        form = QFormLayout()
        form.setContentsMargins(8, 8, 8, 4)
        for n in self._names:
            self._labels[n].setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(f"{n}:", self._labels[n])

        bg, fg = ("#111827", "#9CA3AF") if dark else ("#FFFFFF", "#374151")
        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground(bg)
        self._p_bat = self._glw.addPlot(row=0, col=0)
        self._p_alt = self._glw.addPlot(row=1, col=0)
        self._p_alt.setXLink(self._p_bat)
        for plot, title, unit in ((self._p_bat, "Battery", "%"), (self._p_alt, "Altitude", "m")):
            plot.setLabel("left", title, units=unit)
            plot.showGrid(x=True, y=True, alpha=0.2)
            plot.setXRange(-self._history, 0, padding=0)
            plot.setMouseEnabled(x=False, y=False)
            for axis in ("left", "bottom"):
                ax = plot.getAxis(axis)
                ax.setPen(fg)
                ax.setTextPen(fg)
        self._p_bat.setYRange(0, 100, padding=0)
        self._p_alt.setLabel("bottom", "seconds ago")
        self._c_bat = self._p_bat.plot(pen=pg.mkPen("#22C55E", width=2), connect="finite")
        self._c_alt = self._p_alt.plot(pen=pg.mkPen("#3B82F6", width=2), connect="finite")
        self._glw.setMinimumHeight(220)

        grid = QGridLayout()
        self._buttons: dict[str, QPushButton] = {}
        for i, name in enumerate(("HOVER", "LAND", "RTH", "TRIANGLE", "LINE", "GOTO")):
            b = QPushButton(name)
            b.setProperty("danger", name in _CONFIRM)
            b.clicked.connect(partial(self._on_command, name))
            self._buttons[name] = b
            grid.addWidget(b, i // 3, i % 3)
        self._feedback = QLabel("")
        self._feedback.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(form)
        lay.addWidget(self._glw, 1)
        lay.addLayout(grid)
        lay.addWidget(self._feedback)

        client.connection_changed.connect(lambda _s: self._update_buttons())
        self._ticks = 0
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._update_buttons()

    # ------------------------------------------------------------------ API
    def set_drone(self, drone_id: Optional[int]) -> None:
        """Show ``drone_id`` (``None`` clears the panel)."""
        self._drone_id = drone_id
        self._feedback.setText("")
        self._refresh(force_plots=True)
        self._update_buttons()

    def set_waypoint_provider(self, provider: Callable[[], Optional[tuple[float, float]]]) -> None:
        """``provider()`` returns the latest map waypoint (lat, lon) or ``None``; used to prefill GOTO."""
        self._wp_provider = provider

    # -------------------------------------------------------------- refresh
    def _refresh(self, force_plots: bool = False) -> None:
        self._ticks += 1
        d = self._registry.get(self._drone_id) if self._drone_id is not None else None
        if d is None:
            for lbl in self._labels.values():
                lbl.setText("—")
            if force_plots or self._ticks % 2 == 0:
                for c in (self._c_bat, self._c_alt):
                    c.setData([], [])
            return
        status = self._registry.status(d.id)
        state = display_state(d, status)
        now = time.time()
        L = self._labels
        L["ID"].setText(f"#{d.id}")
        L["Role"].setText(d.role or "—" if status == STATUS_ACTIVE else f"{d.role} ({status.upper()})")
        L["Transport"].setText(d.transport.upper() + ("  (LORA_ONLY)" if state == "LORA_ONLY" else ""))
        L["Battery"].setText("—" if not math.isfinite(d.battery) else f"{d.battery:.0f} %")
        L["Altitude"].setText(f"{d.alt:.1f} m")
        L["Speed"].setText(f"{(d.speed or 0.0):.1f} m/s")
        L["Lat / Lon"].setText(f"{d.lat:.6f}, {d.lon:.6f}" if d.has_position else "—")
        w = f"{d.rssi_wifi:.0f}" if d.rssi_wifi > -127 else "n/a"
        l = f"{d.rssi_lora:.0f}" if d.rssi_lora > -127 else "n/a"
        L["RSSI Wi-Fi / LoRa"].setText(f"{w} / {l} dBm")
        L["Last heartbeat"].setText(f"{max(0.0, now - d.last_seen):.1f} s ago")

        if force_plots or self._ticks % 2 == 0:             # charts at 2 Hz
            t, bat, alt = self._registry.history(d.id)
            x = t - now
            self._c_bat.setData(x, bat)
            self._c_alt.setData(x, alt)
            if len(alt) and math.isfinite(alt.max()):
                self._p_alt.setYRange(0, max(10.0, float(alt.max()) * 1.2), padding=0)

    def _update_buttons(self) -> None:
        enabled = self._drone_id is not None and self._client.is_connected
        for b in self._buttons.values():
            b.setEnabled(enabled)

    # ------------------------------------------------------------- commands
    def _on_command(self, name: str) -> None:
        did = self._drone_id
        if did is None:
            return
        if name in _CONFIRM:
            answer = QMessageBox.question(
                self, f"Confirm {name}", f"Send {name} to drone #{did}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return
        p1 = p2 = p3 = 0.0
        if name in ("TRIANGLE", "LINE"):
            val, ok = QInputDialog.getDouble(self, f"{name} formation", "Spacing between drones (m):",
                                             15.0, 3.0, 200.0, 1)
            if not ok:
                return
            p1 = val
        elif name == "GOTO":
            d = self._registry.get(did)
            wp = self._wp_provider()
            lat, lon = wp if wp else ((d.lat, d.lon) if d and d.has_position else (0.0, 0.0))
            alt = max(5.0, d.alt) if d else 30.0
            dlg = GotoDialog(lat, lon, alt, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            p1, p2, p3 = dlg.values()
        ok = self._client.publish_command(DroneCommand(did, name, p1, p2, p3))
        self._feedback.setText(f"{name} → #{did}: " + ("sent" if ok else "NOT SENT — not connected"))
