"""Python side of the tactical map: a QQuickWidget plus the QML-facing bridge."""
from __future__ import annotations

import json
import math
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QWidget

from core.models import ROLE_COLORS, STATUS_STALE, display_state
from core.state import DroneRegistry

_GEOFENCE_STYLE = {
    "keepin": ("#22C55E", "#2222C55E"),
    "keepout": ("#EF4444", "#33EF4444"),
}
_DEFAULT_GEOFENCE_STYLE = ("#3B82F6", "#223B82F6")


def tile_host_from_url(tile_url: str) -> str:
    """Strip the ``{z}/{x}/{y}.png`` suffix: ``https://host/path/{z}/{x}/{y}.png`` -> ``https://host/path/``."""
    head = tile_url.split("{z}")[0]
    return head if head.endswith("/") else head + "/"


def _build_geofence(g: dict) -> dict:
    """Shape one geofence for QML, resolving its stroke/fill from the kind."""
    stroke, fill = _GEOFENCE_STYLE.get(g.get("kind", ""), _DEFAULT_GEOFENCE_STYLE)
    return {"name": g.get("name", ""), "lat": float(g["lat"]),
            "lon": float(g["lon"]), "radius": float(g["radius"]),
            "stroke": stroke, "fill": fill}


class MapBridge(QObject):
    """Object exposed to QML as ``bridge``.

    QML reads ``drones`` (list of dicts), ``waypoints``, ``geofences`` and a few
    constants, and calls the ``droneClicked`` / ``mapClicked`` slots. Registry
    updates arrive at telemetry rate (~200/s for 20 drones at 10 Hz) but are only
    *marked dirty*; a 10 Hz timer builds one snapshot per tick, so the UI cost is
    independent of the message rate.
    """

    dronesChanged = Signal()
    waypointsChanged = Signal()
    waypointModeChanged = Signal()
    centerRequested = Signal(float, float)           # Python -> QML

    geofencesChanged = Signal()                      # settings applied live
    tileHostChanged = Signal()
    centerChanged = Signal()
    zoomChanged = Signal()

    drone_clicked = Signal(int)                      # QML -> Python
    waypoint_added = Signal(float, float)

    def __init__(self, registry: DroneRegistry, map_cfg: dict, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._registry = registry
        self._center = (float(map_cfg["center"][0]), float(map_cfg["center"][1]))
        self._zoom = float(map_cfg.get("zoom", 13))
        self._tile_host = tile_host_from_url(map_cfg["tile_url"])
        self._trail_len = int(map_cfg.get("trail_length", 50))
        self._trail_interval = float(map_cfg.get("trail_interval", 1.0))
        self._geofences = [_build_geofence(g) for g in (map_cfg.get("geofences") or [])]

        self._drones: list[dict[str, Any]] = []
        self._rows: dict[int, dict[str, Any]] = {}
        self._dirty: set[int] = set()
        self._trails: dict[int, deque[tuple[float, float]]] = {}
        self._trail_json: dict[int, str] = {}
        self._last_trail_t: dict[int, float] = {}
        self._selected: Optional[int] = None
        self._waypoints: list[dict[str, Any]] = []
        self._waypoint_mode = False

        registry.drone_updated.connect(self._on_drone_updated)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    # -------------------------------------------------------- QML properties
    def _get_drones(self) -> list:
        return self._drones

    def _get_waypoints(self) -> list:
        return self._waypoints

    def _get_waypoint_mode(self) -> bool:
        return self._waypoint_mode

    def _set_waypoint_mode(self, on: bool) -> None:
        if on != self._waypoint_mode:
            self._waypoint_mode = on
            self.waypointModeChanged.emit()

    drones = Property("QVariantList", _get_drones, notify=dronesChanged)
    waypoints = Property("QVariantList", _get_waypoints, notify=waypointsChanged)
    waypointMode = Property(bool, _get_waypoint_mode, _set_waypoint_mode, notify=waypointModeChanged)
    geofences = Property("QVariantList", lambda self: self._geofences, notify=geofencesChanged)
    tileHost = Property(str, lambda self: self._tile_host, notify=tileHostChanged)
    centerLat = Property(float, lambda self: self._center[0], notify=centerChanged)
    centerLon = Property(float, lambda self: self._center[1], notify=centerChanged)
    zoom = Property(float, lambda self: self._zoom, notify=zoomChanged)

    # ------------------------------------------------------------ QML -> Python
    @Slot(int)
    def droneClicked(self, drone_id: int) -> None:
        """Called by QML when a marker is tapped."""
        self.drone_clicked.emit(drone_id)

    @Slot(float, float)
    def mapClicked(self, lat: float, lon: float) -> None:
        """Called by QML for a tap on empty map; drops a waypoint in waypoint mode."""
        if not self._waypoint_mode:
            return
        self._waypoints.append({"index": len(self._waypoints) + 1, "lat": lat, "lon": lon})
        self.waypointsChanged.emit()
        self.waypoint_added.emit(lat, lon)

    # ------------------------------------------------------------ Python API
    def set_selected(self, drone_id: Optional[int]) -> None:
        """Highlight ``drone_id`` on the map (``None`` clears)."""
        for did in (self._selected, drone_id):
            if did is not None:
                self._dirty.add(did)
        self._selected = drone_id

    def center_on(self, drone_id: int) -> None:
        """Pan the map to a drone."""
        d = self._registry.get(drone_id)
        if d is not None and d.has_position:
            self.centerRequested.emit(d.lat, d.lon)

    def clear_waypoints(self) -> None:
        """Remove all waypoint markers."""
        self._waypoints = []
        self.waypointsChanged.emit()

    def last_waypoint(self) -> Optional[tuple[float, float]]:
        """(lat, lon) of the most recently dropped waypoint, if any."""
        if not self._waypoints:
            return None
        w = self._waypoints[-1]
        return w["lat"], w["lon"]

    def apply_settings(self, map_cfg: dict) -> None:
        """Reconfigure tile host, centre, zoom, trails and geofences in place.

        Called from :meth:`MainWindow._on_settings_applied` after the user
        confirms Settings. Every change emits the matching notify signal so
        QML re-reads the property; existing drone trails are preserved and
        only truncated/resized when the trail length changes.

        Note: ``tileHost`` only takes effect if the QML side rebinds its map
        plugin on ``tileHostChanged``. If your QML passes the URL to the OSM
        plugin at construction time, a tile-URL change will require a restart
        and that is reported by ``MainWindow`` as such.
        """
        # --- trail length: rebuild deques with the new maxlen
        new_len = max(0, int(map_cfg.get("trail_length", self._trail_len)))
        if new_len != self._trail_len:
            self._trail_len = new_len
            for did, dq in list(self._trails.items()):
                self._trails[did] = deque(dq, maxlen=new_len)
            self._last_trail_t.clear()          # force the next flush to rewrite JSON
            self._dirty.update(self._trails.keys())
            # Drones that had no trail yet: nothing to do; they'll be created
            # with the correct maxlen by _flush when their next sample arrives.

        self._trail_interval = float(map_cfg.get("trail_interval", self._trail_interval))

        # --- geofences
        new_geo = [_build_geofence(g) for g in (map_cfg.get("geofences") or [])]
        if new_geo != self._geofences:
            self._geofences = new_geo
            self.geofencesChanged.emit()

        # --- tile host
        new_host = tile_host_from_url(map_cfg["tile_url"])
        if new_host != self._tile_host:
            self._tile_host = new_host
            self.tileHostChanged.emit()

        # --- centre / zoom
        lat, lon = (float(v) for v in map_cfg["center"])
        if (lat, lon) != self._center:
            self._center = (lat, lon)
            self.centerChanged.emit()
            self.centerRequested.emit(lat, lon)   # actually pan the map

        new_zoom = float(map_cfg.get("zoom", self._zoom))
        if new_zoom != self._zoom:
            self._zoom = new_zoom
            self.zoomChanged.emit()

    # --------------------------------------------------------------- internals
    def _on_drone_updated(self, drone_id: int) -> None:
        self._dirty.add(drone_id)

    def _flush(self) -> None:
        if not self._dirty:
            return
        ids, self._dirty = self._dirty, set()
        now = time.monotonic()
        for did in ids:
            d = self._registry.get(did)
            if d is None or not d.has_position:
                self._rows.pop(did, None)
                continue
            if now - self._last_trail_t.get(did, 0.0) >= self._trail_interval:
                self._last_trail_t[did] = now
                tr = self._trails.setdefault(did, deque(maxlen=self._trail_len))
                if not tr or tr[-1] != (d.lat, d.lon):
                    tr.append((d.lat, d.lon))
                    self._trail_json[did] = json.dumps(
                        [{"latitude": a, "longitude": b} for a, b in tr], separators=(",", ":"))
            status = self._registry.status(did)
            state = display_state(d, status)
            self._rows[did] = {
                "id": did, "lat": d.lat, "lon": d.lon,
                "color": ROLE_COLORS[state], "state": state,
                "stale": status == STATUS_STALE,
                "selected": did == self._selected,
                "trail": self._trail_json.get(did, "[]"),
            }
        self._drones = [self._rows[k] for k in sorted(self._rows)]
        self.dronesChanged.emit()


class MapView(QQuickWidget):
    """QQuickWidget hosting ``map_view.qml``.

    Signals:
        drone_clicked(int): a marker was clicked on the map.
        waypoint_added(float, float): a waypoint was dropped (lat, lon).
    """

    drone_clicked = Signal(int)
    waypoint_added = Signal(float, float)

    def __init__(self, registry: DroneRegistry, map_cfg: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = MapBridge(registry, map_cfg, self)
        self.bridge.drone_clicked.connect(self.drone_clicked)
        self.bridge.waypoint_added.connect(self.waypoint_added)

        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.setClearColor(QColor("#111827"))
        self.rootContext().setContextProperty("bridge", self.bridge)
        self.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name("map_view.qml"))))

        #: Human-readable QML load errors (``None`` when the map loaded fine).
        self.load_error: Optional[str] = None
        if self.status() == QQuickWidget.Status.Error:
            self.load_error = "; ".join(e.toString() for e in self.errors())

    # Thin wrappers so MainWindow does not need to know about the bridge.
    def set_selected(self, drone_id: Optional[int]) -> None:
        """Highlight a drone on the map."""
        self.bridge.set_selected(drone_id)

    def center_on_drone(self, drone_id: int) -> None:
        """Pan the map to a drone."""
        self.bridge.center_on(drone_id)

    def set_waypoint_mode(self, on: bool) -> None:
        """Enable/disable dropping waypoints by clicking the map."""
        self.bridge.waypointMode = on

    def clear_waypoints(self) -> None:
        """Remove all waypoints."""
        self.bridge.clear_waypoints()

    def last_waypoint(self) -> Optional[tuple[float, float]]:
        """Most recent waypoint (lat, lon) or ``None``."""
        return self.bridge.last_waypoint()

    def apply_settings(self, map_cfg: dict) -> None:
        """Reconfigure the map without recreating the widget (Settings → Apply)."""
        self.bridge.apply_settings(map_cfg)