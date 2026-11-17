"""Main window: toolbar, 3-pane splitter and status bar."""
from __future__ import annotations

import math
import logging
from pathlib import Path

from core.config_io import save_config
from ui.settings_dialog import SettingsDialog

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (QApplication, QLabel, QMainWindow, QPushButton, QSizePolicy,
                               QSplitter, QTabWidget, QToolBar, QWidget)

from core.models import SwarmEvent
from core.mqtt_client import MqttClient
from core.state import DroneRegistry
from ui.drone_list import DroneListWidget
from ui.event_log import EventLog
from ui.map_view import MapView
from ui.telemetry_panel import TelemetryPanel
from ui.pairing_panel import PairingPanel
from ui.sensor_panel import SensorPanel

_STYLE_PATH = Path(__file__).with_name("style.qss")


log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """The operator cockpit.

    Layout: [DroneListWidget 280 px | MapView (flex) | Telemetry/Events/Pairing/Sensors tabs].
    """

    def __init__(self, config: dict, registry: DroneRegistry, client: MqttClient, config_path: Optional[Path] = None, parent: Optional[QWidget] = None) -> None:
        """Build the UI and wire registry/client signals.

        Args:
            config: Application config dict (see ``config.yaml``).
            registry: Shared drone registry.
            client: MQTT client (real or mock).
        """
        super().__init__(parent)
        self._cfg = config
        self._config_path = Path(config_path) if config_path else None
        self._registry = registry
        self._client = client
        tel = config["telemetry"]
        self._stale_timeout = float(tel["stale_timeout"])
        self._lost_timeout = float(tel["lost_timeout"])
        self._lora_grace = float(tel.get("lora_grace", 0.0))
        dark = config.get("theme", "dark") == "dark"

        self.setWindowTitle("Drone Swarm Cockpit" + ("  [MOCK MODE]" if client.mock_mode else ""))
        self.resize(1600, 900)
        self.apply_theme(config.get("theme", "dark"))

        # ---- panes
        self.drone_list = DroneListWidget(registry)
        self.drone_list.setMinimumWidth(240)
        self.map_view = MapView(registry, config["map"])
        self.telemetry_panel = TelemetryPanel(registry, client, float(tel["history_seconds"]), dark)
        self.event_log = EventLog()
        self.telemetry_panel.set_waypoint_provider(self.map_view.last_waypoint)

        self.pairing_panel = PairingPanel(client)
        self.sensor_panel = SensorPanel(registry)

        tabs = QTabWidget()
        tabs.addTab(self.telemetry_panel, "Telemetry")
        tabs.addTab(self.event_log, "Events")
        tabs.addTab(self.pairing_panel, "Pairing")
        tabs.addTab(self.sensor_panel, "Sensors")
        tabs.setMinimumWidth(320)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.drone_list)
        splitter.addWidget(self.map_view)
        splitter.addWidget(tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setCollapsible(1, False)
        splitter.setSizes([280, 1000, 340])
        self.setCentralWidget(splitter)

        self._build_toolbar()
        self._build_statusbar()

        # ---- signals
        registry.drone_lost.connect(self._on_drone_lost)
        registry.drone_recovered.connect(self._on_drone_recovered)
        client.event_received.connect(self._on_event)
        client.connection_changed.connect(self._on_connection)
        client.stats_updated.connect(self._on_stats)
        self.drone_list.drone_selected.connect(self._on_list_selected)
        self.map_view.drone_clicked.connect(self._on_map_selected)
        self.map_view.waypoint_added.connect(self._on_waypoint)
        self.pairing_panel.approved.connect(self._on_drone_approved)

        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        self._on_connection("connected" if client.is_connected else "disconnected")
        self._update_health()

        if self.map_view.load_error:
            self.event_log.add_event(SwarmEvent("error", None, f"Map failed to load: {self.map_view.load_error}"))

    # --------------------------------------------------------------- theming
    @staticmethod
    def apply_theme(theme: str) -> None:
        """Apply ``style.qss`` for the dark theme, or reset to the platform look."""
        app = QApplication.instance()
        if app is None:
            return
        app.setStyleSheet(_STYLE_PATH.read_text(encoding="utf-8") if theme == "dark" else "")

    # ----------------------------------------------------------- construction
    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        bar.setFloatable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)

        settings_act = QAction("Settings…", self)
        settings_act.setShortcut("Ctrl+,")
        settings_act.triggered.connect(self._open_settings)
        bar.addAction(settings_act)
        bar.addSeparator()

        self._mission_lbl = QLabel("MISSION: IDLE")
        self._mission_lbl.setStyleSheet("font-weight: 600;")
        self._health_lbl = QLabel("0/0 active")
        bar.addWidget(self._mission_lbl)
        bar.addSeparator()
        bar.addWidget(self._health_lbl)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        self._wp_action = QAction("Waypoint mode", self)
        self._wp_action.setCheckable(True)
        self._wp_action.setToolTip("Click the map to drop waypoints (used to prefill GOTO)")
        self._wp_action.toggled.connect(self.map_view.set_waypoint_mode)
        bar.addAction(self._wp_action)
        clear = QAction("Clear waypoints", self)
        clear.triggered.connect(self.map_view.clear_waypoints)
        bar.addAction(clear)
        bar.addSeparator()

        self._conn_btn = QPushButton("Connect")
        self._conn_btn.setProperty("primary", True)
        self._conn_btn.clicked.connect(self._toggle_connection)
        bar.addWidget(self._conn_btn)

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self._mqtt_lbl = QLabel("MQTT: disconnected")
        self._latency_lbl = QLabel("Latency: —")
        self._rate_lbl = QLabel("0 msg/s")
        sb.addWidget(self._mqtt_lbl, 1)
        sb.addPermanentWidget(self._latency_lbl)
        sb.addPermanentWidget(self._rate_lbl)

    # ------------------------------------------------------------------ slots
    @Slot()
    def _toggle_connection(self) -> None:
        if self._client.is_running:
            self._client.stop()
        else:
            self._client.start()

    @Slot(str)
    def _on_connection(self, state: str) -> None:
        if self._client.mock_mode:
            text = {"connected": "MOCK feed running", "connecting": "MOCK starting…",
                    "disconnected": "MOCK feed stopped"}[state]
        else:
            text = {"connected": f"connected to {self._client.broker_label}",
                    "connecting": f"connecting to {self._client.broker_label}…",
                    "disconnected": "disconnected"}[state]
        color = {"connected": "#4ADE80", "connecting": "#FACC15", "disconnected": "#F87171"}[state]
        self._mqtt_lbl.setText(f"MQTT: <span style='color:{color}'>{text}</span>")
        self._conn_btn.setText("Disconnect" if self._client.is_running else "Connect")
        if state != "connected":
            self._latency_lbl.setText("Latency: —")
            self._rate_lbl.setText("0 msg/s")

    @Slot(float, float)
    def _on_stats(self, rate: float, latency_ms: float) -> None:
        self._rate_lbl.setText(f"{rate:.0f} msg/s")
        self._latency_lbl.setText("Latency: —" if math.isnan(latency_ms) else f"Latency: {latency_ms:.0f} ms")

    @Slot(object)
    def _on_event(self, event: SwarmEvent) -> None:
        self.event_log.add_event(event)
        if event.event_type == "command_sent" and event.message:
            self._mission_lbl.setText(f"MISSION: {event.message.split(' via ')[0]}")

    @Slot(int)
    def _on_drone_lost(self, drone_id: int) -> None:
        self._on_event(SwarmEvent("drone_lost", drone_id,
                                  f"Drone #{drone_id}: no telemetry for {self._lost_timeout:.0f} s"))

    @Slot(int)
    def _on_drone_recovered(self, drone_id: int) -> None:
        self._on_event(SwarmEvent("drone_recovered", drone_id, f"Drone #{drone_id}: telemetry resumed"))

    @Slot(int)
    def _on_drone_approved(self, drone_id: int) -> None:
        self._on_event(SwarmEvent("paired", drone_id,
                                  f"Drone #{drone_id} approved; awaiting telemetry"))

    @Slot(int)
    def _on_list_selected(self, drone_id: int) -> None:
        self.telemetry_panel.set_drone(drone_id)
        self.sensor_panel.set_drone(drone_id)
        self.map_view.set_selected(drone_id)
        self.map_view.center_on_drone(drone_id)          # list click centres the map

    @Slot(int)
    def _on_map_selected(self, drone_id: int) -> None:
        self.drone_list.select_drone(drone_id)           # no re-centering for map clicks
        self.telemetry_panel.set_drone(drone_id)
        self.sensor_panel.set_drone(drone_id)
        self.map_view.set_selected(drone_id)

    @Slot(float, float)
    def _on_waypoint(self, lat: float, lon: float) -> None:
        self._on_event(SwarmEvent("waypoint", None, f"Waypoint dropped at {lat:.6f}, {lon:.6f}"))

    @Slot()
    def _on_tick(self) -> None:
        self._registry.mark_stale(self._stale_timeout, self._lora_grace)
        self._registry.mark_lost(self._lost_timeout, self._lora_grace)
        self._update_health()

    @Slot()
    def _open_settings(self) -> None:
        """Open the settings dialog, seeded with config + runtime approvals."""
        import copy as _copy
        cfg = _copy.deepcopy(self._cfg)
        runtime = self._client.approved_drone_ids()
        sec = cfg.setdefault("security", {})
        merged = set(int(x) for x in sec.get("allowed_drones", []) or []) | runtime
        sec["allowed_drones"] = sorted(merged)

        dlg = SettingsDialog(cfg, self)
        dlg.settings_applied.connect(self._on_settings_applied)
        dlg.exec()

    @Slot(dict)
    def _on_settings_applied(self, new_cfg: dict) -> None:
        """Distribute the new config to components that can pick it up live."""
        old = self._cfg
        applied: list[str] = []

        if self._config_path is not None:
            if save_config(self._config_path, new_cfg):
                applied.append("saved")
            else:
                self._on_event(SwarmEvent("error", None, "Failed to save config.yaml"))

        self._cfg = new_cfg

        tel = new_cfg["telemetry"]
        self._stale_timeout = float(tel["stale_timeout"])
        self._lost_timeout = float(tel["lost_timeout"])
        self._lora_grace = float(tel.get("lora_grace", 0.0))

        if new_cfg.get("theme") != old.get("theme"):
            self.apply_theme(new_cfg.get("theme", "dark"))
            applied.append("theme")

        if new_cfg.get("map") != old.get("map"):
            apply = getattr(self.map_view, "apply_settings", None)
            if callable(apply):
                try:
                    apply(new_cfg["map"])
                    applied.append("map")
                except Exception as exc:  # noqa: BLE001
                    log.exception("map_view.apply_settings failed")
                    self._on_event(SwarmEvent("error", None, f"Map update failed: {exc}"))
            else:
                self._on_event(SwarmEvent(
                    "settings", None,
                    "Map settings changed — restart to take effect "
                    "(MapView has no apply_settings())"))

        if new_cfg.get("security", {}).get("allowed_drones") != old.get("security", {}).get("allowed_drones"):
            self._client.set_allowed_drones(set(new_cfg["security"]["allowed_drones"]))
            applied.append("security")

        if new_cfg.get("mqtt") != old.get("mqtt"):
            self._on_event(SwarmEvent("settings", None,
                                      "MQTT settings changed — restart to take effect"))
        if bool(new_cfg["ui"].get("software_rendering")) != bool(old["ui"].get("software_rendering")):
            self._on_event(SwarmEvent("settings", None,
                                      "software_rendering changed — restart to take effect"))

        if applied:
            self._on_event(SwarmEvent("settings", None,
                                      "Settings updated (" + ", ".join(applied) + ")"))

    def _update_health(self) -> None:
        h = self._registry.health_summary()
        parts = [f"{h['active']}/{h['total']} active"]
        if h["lora_only"]:
            parts.append(f"{h['lora_only']} LORA_ONLY")
        if h["stale"]:
            parts.append(f"{h['stale']} STALE")
        if h["lost"]:
            parts.append(f"{h['lost']} LOST")
        color = "#F87171" if h["lost"] else "#FACC15" if (h["stale"] or h["lora_only"]) else "#4ADE80"
        self._health_lbl.setText(f"<span style='color:{color}; font-weight:600'>{', '.join(parts)}</span>")

    def closeEvent(self, event: QCloseEvent) -> None:    # noqa: N802
        """Stop MQTT cleanly before the window closes."""
        self._client.shutdown()
        event.accept()