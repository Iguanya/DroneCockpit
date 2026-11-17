"""Drone registry: latest state, liveness tracking, RSSI history and capabilities.

Pure Qt + ``core.models`` — no MQTT dependency. Fed by ``MqttClient`` via
``telemetry_received`` (in ``main.py``) and asked by the UI on a 2 Hz timer to
promote drones to STALE / LOST.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, Signal

from core.models import (DroneTelemetry, RSSI_UNKNOWN, STATUS_ACTIVE,
                         STATUS_LOST, STATUS_STALE)


class DroneRegistry(QObject):
    """In-memory store of the last-known state of every drone.

    Signals:
        drone_lost(int): emitted when a drone transitions to :data:`STATUS_LOST`.
        drone_recovered(int): emitted when a LOST drone reports again.
        drone_updated(int): emitted on every accepted telemetry / capability update.
    """

    drone_lost = Signal(int)
    drone_recovered = Signal(int)
    drone_updated = Signal(int)

    def __init__(self, history_seconds: float = 300.0,
                 parent: Optional[QObject] = None) -> None:
        """Create an empty registry.

        Args:
            history_seconds: How long RSSI / battery / altitude samples are kept (s).
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._history_seconds = float(history_seconds)
        self._drones: dict[int, DroneTelemetry] = {}
        self._status: dict[int, str] = {}
        self._rssi: dict[int, deque[tuple[float, float, str]]] = {}
        self._hist: dict[int, deque[tuple[float, float, float]]] = {}

    # ------------------------------------------------------------- ingestion
    def update(self, telemetry: DroneTelemetry) -> None:
        """Merge a fresh :class:`DroneTelemetry` into the registry.

        Called from the MQTT client on every merged sample. Preserves the
        capability dict captured at pairing time if the incoming sample has none.
        """
        did = telemetry.id
        prev_status = self._status.get(did)
        old = self._drones.get(did)
        if old is not None and old.sensors and not telemetry.sensors:
            telemetry.sensors = old.sensors

        self._drones[did] = telemetry
        self._status[did] = STATUS_ACTIVE

        if prev_status == STATUS_LOST:
            self.drone_recovered.emit(did)

        now = telemetry.last_seen or time.time()
        cutoff = now - self._history_seconds

        # RSSI history (per transport) with time-based trimming.
        rssi_hist = self._rssi.setdefault(did, deque())
        if telemetry.rssi_wifi > RSSI_UNKNOWN:
            rssi_hist.append((now, telemetry.rssi_wifi, "wifi"))
        if telemetry.rssi_lora > RSSI_UNKNOWN:
            rssi_hist.append((now, telemetry.rssi_lora, "lora"))
        while rssi_hist and rssi_hist[0][0] < cutoff:
            rssi_hist.popleft()

        # Battery / altitude history for the rolling charts.
        hist = self._hist.setdefault(did, deque())
        hist.append((now, telemetry.battery, telemetry.alt))
        while hist and hist[0][0] < cutoff:
            hist.popleft()

        self.drone_updated.emit(did)

    # -------------------------------------------------------------- queries
    def get(self, did: int) -> Optional[DroneTelemetry]:
        """Return the latest known state of ``did``, or ``None``."""
        return self._drones.get(int(did))

    def all(self) -> list[DroneTelemetry]:
        """Every known drone, in insertion order."""
        return list(self._drones.values())

    def ids(self) -> list[int]:
        """All known drone IDs."""
        return list(self._drones.keys())

    def __contains__(self, did: int) -> bool:
        return int(did) in self._drones

    def status(self, did: int) -> str:
        """Liveness state of ``did`` (``active`` / ``stale`` / ``lost``)."""
        return self._status.get(int(did), STATUS_ACTIVE)

    def rssi_history(self, did: int,
                     transport: Optional[str] = None) -> list[tuple[float, float]]:
        """Recent ``(wall_time, dBm)`` samples for ``did``.

        Args:
            did: Drone ID.
            transport: ``"wifi"`` / ``"lora"`` to filter; ``None`` for all.
        """
        hist = self._rssi.get(int(did))
        if not hist:
            return []
        if transport is None:
            return [(t, r) for t, r, _ in hist]
        return [(t, r) for t, r, tr in hist if tr == transport]

    def history(self, did: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Rolling ``(t, battery, altitude)`` arrays for ``did``.

        Returns three equal-length float arrays: wall-clock timestamps (s),
        battery percentage, altitude in metres. Empty arrays if the drone is
        unknown or has no samples yet.
        """
        hist = self._hist.get(int(did))
        if not hist:
            empty = np.empty(0, dtype=float)
            return empty, empty, empty
        arr = np.asarray(hist, dtype=float)
        return arr[:, 0], arr[:, 1], arr[:, 2]

    # ---------------------------------------------------------- capabilities
    def set_capabilities(self, did: int, caps: dict) -> None:
        """Record the sensor inventory a drone reported at pairing time.

        Creates a stub :class:`DroneTelemetry` if the drone has not sent any
        telemetry yet, so ``capabilities(did)`` works before the first position.
        """
        did = int(did)
        drone = self._drones.get(did)
        if drone is None:
            drone = DroneTelemetry(id=did)
            self._drones[did] = drone
            self._status[did] = STATUS_ACTIVE
        drone.sensors = dict(caps or {})
        self.drone_updated.emit(did)

    def capabilities(self, did: int) -> dict:
        """Sensor inventory for ``did`` (empty dict if unknown)."""
        drone = self._drones.get(int(did))
        return dict(drone.sensors) if drone else {}

    # ------------------------------------------------------------ liveness
    def _effective_timeout(self, base: float, drone: DroneTelemetry,
                           lora_grace: float) -> float:
        """LoRa beacons are slow, so give LoRa drones extra slack."""
        return base + (lora_grace if drone.transport == "lora" else 0.0)

    def mark_stale(self, stale_timeout: float, lora_grace: float = 0.0) -> None:
        """Promote ACTIVE drones to STALE once they go quiet past ``stale_timeout``."""
        now = time.time()
        for did, d in self._drones.items():
            if self._status.get(did) != STATUS_ACTIVE:
                continue
            limit = self._effective_timeout(stale_timeout, d, lora_grace)
            if now - d.last_seen > limit:
                self._status[did] = STATUS_STALE

    def mark_lost(self, lost_timeout: float, lora_grace: float = 0.0) -> None:
        """Promote ACTIVE / STALE drones to LOST once they go quiet past ``lost_timeout``."""
        now = time.time()
        for did, d in list(self._drones.items()):
            if self._status.get(did) == STATUS_LOST:
                continue
            limit = self._effective_timeout(lost_timeout, d, lora_grace)
            if now - d.last_seen > limit:
                self._status[did] = STATUS_LOST
                self.drone_lost.emit(did)

    def health_summary(self) -> dict:
        """Counts used by the toolbar status badge."""
        active = stale = lost = lora_only = 0
        for did, d in self._drones.items():
            s = self._status.get(did, STATUS_ACTIVE)
            if s == STATUS_ACTIVE:
                active += 1
            elif s == STATUS_STALE:
                stale += 1
            elif s == STATUS_LOST:
                lost += 1
            if d.transport == "lora":
                lora_only += 1
        return {
            "active": active,
            "total": len(self._drones),
            "stale": stale,
            "lost": lost,
            "lora_only": lora_only,
        }