"""Plain data models and shared constants for the cockpit (no Qt dependency)."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

#: Sentinel meaning "no RSSI reading available".
RSSI_UNKNOWN: float = -127.0

#: Liveness states tracked by :class:`core.state.DroneRegistry`.
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_LOST = "lost"

#: Marker / badge colours for every display state (single source of truth for
#: the QML map and the QWidget list delegate).
ROLE_COLORS: dict[str, str] = {
    "LEADER": "#8B5CF6",
    "FOLLOWER": "#3B82F6",
    "CANDIDATE": "#F59E0B",
    "LOST": "#EF4444",
    "LORA_ONLY": "#14B8A6",
}

#: Commands the operator can issue from the telemetry panel.
COMMANDS: tuple[str, ...] = ("HOVER", "LAND", "RTH", "TRIANGLE", "LINE", "GOTO")


@dataclass
class DroneTelemetry:
    """Merged, latest-known state of one drone.

    Attributes:
        id: Drone identifier (1..254).
        lat: Latitude in degrees (NaN if unknown).
        lon: Longitude in degrees (NaN if unknown).
        alt: Altitude in metres.
        battery: Battery percentage 0..100 (NaN if unknown).
        role: ``LEADER`` / ``FOLLOWER`` / ``CANDIDATE`` (free text tolerated).
        rssi_wifi: Wi-Fi RSSI in dBm (:data:`RSSI_UNKNOWN` if unknown).
        rssi_lora: LoRa RSSI in dBm (:data:`RSSI_UNKNOWN` if unknown).
        transport: Link currently carrying the drone's telemetry: ``"wifi"`` or ``"lora"``.
        last_seen: Local wall-clock time (s) the last message was received.
        timestamp: Time (s) of the data itself (source clock if sane, else receive time).
        speed: Ground speed in m/s; ``None`` means "not reported, estimate it".
    """

    id: int
    lat: float = math.nan
    lon: float = math.nan
    alt: float = 0.0
    battery: float = math.nan
    role: str = "FOLLOWER"
    rssi_wifi: float = RSSI_UNKNOWN
    rssi_lora: float = RSSI_UNKNOWN
    transport: str = "wifi"
    last_seen: float = 0.0
    timestamp: float = 0.0
    speed: Optional[float] = None
    sensors: dict = field(default_factory=dict)   # <-- NEW: capabilities from registration

    @property
    def has_position(self) -> bool:
        """True when both latitude and longitude are finite numbers."""
        return math.isfinite(self.lat) and math.isfinite(self.lon)


@dataclass
class DroneCommand:
    """An operator command addressed to one drone.

    ``GOTO`` uses ``param1=lat``, ``param2=lon``, ``param3=alt``; the
    formation commands (``TRIANGLE``/``LINE``) use ``param1`` as spacing in metres.
    """

    target_id: int
    command: str
    param1: float = 0.0
    param2: float = 0.0
    param3: float = 0.0

    def to_payload(self, seq: int, source: str) -> dict:
        """Serialise to the JSON-able dict published on ``swarm/command/...``."""
        return {
            "target_id": self.target_id,
            "command": self.command,
            "param1": self.param1,
            "param2": self.param2,
            "param3": self.param3,
            "seq": seq,
            "ts": time.time(),
            "source": source,
        }


@dataclass
class SwarmEvent:
    """A discrete swarm event (election, loss, failover, command, ...)."""

    event_type: str
    drone_id: Optional[int] = None
    message: str = ""
    timestamp: float = field(default_factory=time.time)


def display_state(drone: DroneTelemetry, status: str) -> str:
    """Collapse role + liveness + transport into one of :data:`ROLE_COLORS`' keys.

    Priority: LOST > LORA_ONLY > role (unknown roles fall back to FOLLOWER).
    """
    if status == STATUS_LOST:
        return "LOST"
    if drone.transport == "lora":
        return "LORA_ONLY"
    role = (drone.role or "FOLLOWER").upper()
    return role if role in ROLE_COLORS else "FOLLOWER"
