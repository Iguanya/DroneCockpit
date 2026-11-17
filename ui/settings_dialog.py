"""Application settings dialog. Emits a validated config copy on Apply/OK."""
from __future__ import annotations

import copy
import logging
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

log = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    """Edit map / connection / telemetry / security / interface settings."""

    settings_applied = Signal(dict)

    def __init__(self, config: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._cfg = copy.deepcopy(config)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(620)

        tabs = QTabWidget()
        tabs.addTab(self._build_map_tab(), "Map")
        tabs.addTab(self._build_connection_tab(), "Connection")
        tabs.addTab(self._build_telemetry_tab(), "Telemetry")
        tabs.addTab(self._build_security_tab(), "Security")
        tabs.addTab(self._build_interface_tab(), "Interface")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._on_apply)

        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)

    # ------------------------------------------------------------------- map
    def _build_map_tab(self) -> QWidget:
        m = self._cfg.get("map", {})
        center = m.get("center", [0.0, 0.0])
        form = QFormLayout()

        self._tile_url = QLineEdit(str(m.get("tile_url", "")))
        self._tile_url.setMinimumWidth(360)
        self._center_lat = self._dspin(-90.0, 90.0, 6, float(center[0]))
        self._center_lon = self._dspin(-180.0, 180.0, 6, float(center[1]))
        self._zoom = self._spin(1, 22, int(m.get("zoom", 13)))
        self._trail_length = self._spin(0, 1000, int(m.get("trail_length", 50)))
        self._trail_interval = self._dspin(0.1, 60.0, 1, float(m.get("trail_interval", 1.0)), suffix=" s")

        form.addRow("Tile URL:", self._tile_url)
        form.addRow("Center latitude:", self._center_lat)
        form.addRow("Center longitude:", self._center_lon)
        form.addRow("Zoom level:", self._zoom)
        form.addRow("Trail length (points):", self._trail_length)
        form.addRow("Trail interval:", self._trail_interval)

        geo_box = QGroupBox("Geofences")
        gv = QVBoxLayout(geo_box)
        self._geo_table = QTableWidget(0, 5)
        self._geo_table.setHorizontalHeaderLabels(["Name", "Kind (keepin/keepout)",
                                                    "Lat", "Lon", "Radius (m)"])
        self._geo_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._geo_table.verticalHeader().setVisible(False)
        for gf in m.get("geofences", []) or []:
            self._add_geofence_row(gf)
        gv.addWidget(self._geo_table)

        row = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(lambda: self._add_geofence_row({}))
        rm = QPushButton("Remove selected")
        rm.clicked.connect(self._remove_selected_geofences)
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch(1)
        gv.addLayout(row)

        outer = QVBoxLayout()
        outer.addLayout(form)
        outer.addWidget(geo_box, 1)
        w = QWidget()
        w.setLayout(outer)
        return w

    def _add_geofence_row(self, gf: dict) -> None:
        row = self._geo_table.rowCount()
        self._geo_table.insertRow(row)
        self._geo_table.setItem(row, 0, QTableWidgetItem(str(gf.get("name", "zone"))))
        self._geo_table.setItem(row, 1, QTableWidgetItem(str(gf.get("kind", "keepin"))))
        self._geo_table.setItem(row, 2, QTableWidgetItem(str(gf.get("lat", 0.0))))
        self._geo_table.setItem(row, 3, QTableWidgetItem(str(gf.get("lon", 0.0))))
        self._geo_table.setItem(row, 4, QTableWidgetItem(str(gf.get("radius", 100.0))))

    def _remove_selected_geofences(self) -> None:
        rows = sorted({i.row() for i in self._geo_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self._geo_table.removeRow(r)

    # ------------------------------------------------------------ connection
    def _build_connection_tab(self) -> QWidget:
        m = self._cfg.get("mqtt", {})
        form = QFormLayout()

        self._broker = QLineEdit(str(m.get("broker", "")))
        self._port = self._spin(1, 65535, int(m.get("port", 1883)))
        self._client_id = QLineEdit(str(m.get("client_id", "drone-cockpit")))
        self._keepalive = self._spin(10, 3600, int(m.get("keepalive", 60)), suffix=" s")
        self._username = QLineEdit(str(m.get("username", "")))
        self._password = QLineEdit(str(m.get("password", "")))
        self._password.setEchoMode(QLineEdit.EchoMode.Password)

        form.addRow("Broker host:", self._broker)
        form.addRow("Broker port:", self._port)
        form.addRow("Client ID:", self._client_id)
        form.addRow("Keep-alive:", self._keepalive)
        form.addRow("Username:", self._username)
        form.addRow("Password:", self._password)

        note = QLabel("Connection changes are saved to disk and take effect on the next launch.")
        note.setWordWrap(True)

        outer = QVBoxLayout()
        outer.addLayout(form)
        outer.addWidget(note)
        outer.addStretch(1)
        w = QWidget()
        w.setLayout(outer)
        return w

    # ------------------------------------------------------------- telemetry
    def _build_telemetry_tab(self) -> QWidget:
        t = self._cfg.get("telemetry", {})
        form = QFormLayout()
        self._history = self._spin(30, 3600, int(t.get("history_seconds", 300)), suffix=" s")
        self._stale = self._dspin(0.5, 60.0, 1, float(t.get("stale_timeout", 2.0)), suffix=" s")
        self._lost = self._dspin(1.0, 300.0, 1, float(t.get("lost_timeout", 5.0)), suffix=" s")
        self._lora = self._dspin(0.0, 60.0, 1, float(t.get("lora_grace", 6.0)), suffix=" s")

        form.addRow("History window:", self._history)
        form.addRow("Stale timeout:", self._stale)
        form.addRow("Lost timeout:", self._lost)
        form.addRow("LoRa grace:", self._lora)

        note = QLabel("STALE fires after the stale timeout with no telemetry; "
                      "LOST after the lost timeout. LoRa drones get the grace added on top.")
        note.setWordWrap(True)

        outer = QVBoxLayout()
        outer.addLayout(form)
        outer.addWidget(note)
        outer.addStretch(1)
        w = QWidget()
        w.setLayout(outer)
        return w

    # -------------------------------------------------------------- security
    def _build_security_tab(self) -> QWidget:
        sec = self._cfg.get("security", {}) or {}
        allowed = sec.get("allowed_drones", []) or []

        self._allowed_table = QTableWidget(len(allowed), 1)
        self._allowed_table.setHorizontalHeaderLabels(["Allowed drone ID"])
        self._allowed_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._allowed_table.verticalHeader().setVisible(False)
        for i, v in enumerate(allowed):
            self._allowed_table.setItem(i, 0, QTableWidgetItem(str(int(v))))

        row = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(self._add_allowed_row)
        rm = QPushButton("Remove selected")
        rm.clicked.connect(self._remove_selected_allowed)
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch(1)

        note = QLabel(
            "Empty list = accept any drone (dev / lab). Non-empty = whitelist enforced "
            "on top of the broker's ACLs. Drones you approve from the Pairing tab are "
            "merged into this list when you open Settings."
        )
        note.setWordWrap(True)

        outer = QVBoxLayout()
        outer.addWidget(self._allowed_table, 1)
        outer.addLayout(row)
        outer.addWidget(note)
        w = QWidget()
        w.setLayout(outer)
        return w

    def _add_allowed_row(self) -> None:
        r = self._allowed_table.rowCount()
        self._allowed_table.insertRow(r)
        self._allowed_table.setItem(r, 0, QTableWidgetItem("1"))

    def _remove_selected_allowed(self) -> None:
        rows = sorted({i.row() for i in self._allowed_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self._allowed_table.removeRow(r)

    # ------------------------------------------------------------- interface
    def _build_interface_tab(self) -> QWidget:
        ui = self._cfg.get("ui", {})
        form = QFormLayout()

        self._theme = QComboBox()
        self._theme.addItems(["dark", "light"])
        self._theme.setCurrentText(str(self._cfg.get("theme", "dark")))

        self._sw_rendering = QCheckBox("Force software rendering (QT_QUICK_BACKEND)")
        self._sw_rendering.setChecked(bool(ui.get("software_rendering", False)))

        form.addRow("Theme:", self._theme)
        form.addRow("", self._sw_rendering)

        note = QLabel("Software rendering is picked before QApplication starts, so a change "
                      "requires restarting the cockpit.")
        note.setWordWrap(True)

        outer = QVBoxLayout()
        outer.addLayout(form)
        outer.addWidget(note)
        outer.addStretch(1)
        w = QWidget()
        w.setLayout(outer)
        return w

    # -------------------------------------------------------------- widgets
    @staticmethod
    def _spin(lo: int, hi: int, val: int, suffix: str = "") -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(val)
        if suffix:
            s.setSuffix(suffix)
        return s

    @staticmethod
    def _dspin(lo: float, hi: float, decimals: int, val: float, suffix: str = "") -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(decimals)
        s.setValue(val)
        if suffix:
            s.setSuffix(suffix)
        return s

    # -------------------------------------------------------------- collect
    def _collect_geofences(self) -> list[dict]:
        out: list[dict] = []
        for r in range(self._geo_table.rowCount()):
            try:
                name = (self._geo_table.item(r, 0).text() if self._geo_table.item(r, 0) else "").strip()
                kind = (self._geo_table.item(r, 1).text() if self._geo_table.item(r, 1) else "keepin").strip().lower()
                lat = float(self._geo_table.item(r, 2).text()) if self._geo_table.item(r, 2) else 0.0
                lon = float(self._geo_table.item(r, 3).text()) if self._geo_table.item(r, 3) else 0.0
                radius = float(self._geo_table.item(r, 4).text()) if self._geo_table.item(r, 4) else 100.0
            except (ValueError, AttributeError):
                raise ValueError(f"Geofence row {r + 1} has a non-numeric lat/lon/radius")
            if kind not in ("keepin", "keepout"):
                raise ValueError(f"Geofence row {r + 1}: kind must be 'keepin' or 'keepout'")
            out.append({"name": name or "zone", "kind": kind,
                        "lat": lat, "lon": lon, "radius": radius})
        return out

    def _collect_allowed(self) -> list[int]:
        out: set[int] = set()
        for r in range(self._allowed_table.rowCount()):
            item = self._allowed_table.item(r, 0)
            if item is None or not item.text().strip():
                continue
            try:
                out.add(int(item.text().strip()))
            except ValueError:
                raise ValueError(f"Allowed drone row {r + 1}: '{item.text()}' is not an integer")
        return sorted(out)

    def _collect(self) -> dict:
        cfg = copy.deepcopy(self._cfg)

        cfg.setdefault("map", {})
        cfg["map"]["tile_url"] = self._tile_url.text().strip()
        cfg["map"]["center"] = [self._center_lat.value(), self._center_lon.value()]
        cfg["map"]["zoom"] = int(self._zoom.value())
        cfg["map"]["trail_length"] = int(self._trail_length.value())
        cfg["map"]["trail_interval"] = float(self._trail_interval.value())
        cfg["map"]["geofences"] = self._collect_geofences()

        cfg.setdefault("mqtt", {})
        cfg["mqtt"]["broker"] = self._broker.text().strip()
        cfg["mqtt"]["port"] = int(self._port.value())
        cfg["mqtt"]["client_id"] = self._client_id.text().strip()
        cfg["mqtt"]["keepalive"] = int(self._keepalive.value())
        cfg["mqtt"]["username"] = self._username.text().strip()
        cfg["mqtt"]["password"] = self._password.text()

        cfg.setdefault("telemetry", {})
        cfg["telemetry"]["history_seconds"] = int(self._history.value())
        cfg["telemetry"]["stale_timeout"] = float(self._stale.value())
        cfg["telemetry"]["lost_timeout"] = float(self._lost.value())
        cfg["telemetry"]["lora_grace"] = float(self._lora.value())

        cfg.setdefault("security", {})
        cfg["security"]["allowed_drones"] = self._collect_allowed()

        cfg["theme"] = self._theme.currentText()
        cfg.setdefault("ui", {})
        cfg["ui"]["software_rendering"] = bool(self._sw_rendering.isChecked())

        return cfg

    def _validate(self, cfg: dict) -> Optional[str]:
        if not cfg["mqtt"]["broker"]:
            return "Broker host cannot be empty."
        if not cfg["mqtt"]["client_id"]:
            return "Client ID cannot be empty."
        if cfg["telemetry"]["stale_timeout"] >= cfg["telemetry"]["lost_timeout"]:
            return "Stale timeout must be smaller than the lost timeout."
        if not cfg["map"]["tile_url"]:
            return "Tile URL cannot be empty."
        return None

    # --------------------------------------------------------------- events
    def _on_apply(self) -> None:
        try:
            cfg = self._collect()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid setting", str(exc))
            return
        err = self._validate(cfg)
        if err:
            QMessageBox.warning(self, "Invalid setting", err)
            return
        self._cfg = cfg
        self.settings_applied.emit(cfg)

    def _on_ok(self) -> None:
        self._on_apply()
        self.accept()