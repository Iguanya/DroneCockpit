"""Simulated swarm used by MOCK_MODE so the UI can be developed without a broker.

The simulator behaves like the real nodes:
  * every drone sends Wi-Fi telemetry at ``rate_hz`` while its Wi-Fi link is good;
  * every drone sends a LoRa beacon every 5 s, always;
  * Wi-Fi RSSI < -80 dBm for 3 s -> Wi-Fi telemetry stops (LoRa only);
    RSSI > -70 dBm for 3 s -> Wi-Fi telemetry resumes (same hysteresis as the firmware).

Scripted scenarios (ids refer to the default 12-drone swarm):
  * #1 is LEADER, #2 is a CANDIDATE for the first 8 s.
  * #4's Wi-Fi RSSI sweeps -60..-90 dBm every 80 s -> repeated failover/recovery.
  * #5 goes completely silent from t=40 s to t=58 s -> STALE -> LOST -> recovered.
  * #6 and #7 start with low batteries.
Operator commands (HOVER, LAND, RTH, GOTO, TRIANGLE, LINE) really move the drones.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass
from typing import Callable

from core.models import DroneCommand, SwarmEvent

EARTH_R = 6_371_000.0
ORBIT_RADIUS_M = 120.0
ORBIT_SPEED_MS = 6.0
CRUISE_ALT_M = 30.0
V_MAX = 9.0
BEACON_PERIOD_S = 5.0

TelemetryEmitter = Callable[[str, dict], None]
EventEmitter = Callable[[SwarmEvent], None]


@dataclass
class _MockDrone:
    id: int
    x: float                     # metres east of centre
    y: float                     # metres north of centre
    z: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    vz: float = 0.0
    mode: str = "follow"         # follow | hover | land | landed | rth | goto
    tx: float = 0.0
    ty: float = 0.0
    tz: float = 0.0
    battery: float = 100.0
    drain: float = 0.03          # percent per second
    role: str = "FOLLOWER"
    wifi_ok: bool = True
    bad_s: float = 0.0
    good_s: float = 0.0
    rssi_wifi: float = -60.0
    rssi_lora: float = -90.0
    next_beacon: float = 0.0


class MockSwarm:
    """Kinematic swarm simulator producing MQTT-like telemetry callbacks."""

    FAILOVER_ID = 4
    DROPOUT_ID = 5
    DROPOUT_WINDOW = (40.0, 58.0)

    def __init__(
        self,
        center: tuple[float, float],
        count: int,
        rate_hz: float,
        emit_telemetry: TelemetryEmitter,
        emit_event: EventEmitter,
    ) -> None:
        """Create ``count`` drones around ``center`` (lat, lon).

        Args:
            center: Map centre / home position in degrees.
            count: Number of simulated drones (>= 2).
            rate_hz: Wi-Fi telemetry rate per drone.
            emit_telemetry: Called as ``f(transport, payload_dict)`` per message.
            emit_event: Called with each :class:`SwarmEvent` the swarm raises.
        """
        self._lat0, self._lon0 = center
        self._rate = max(1.0, float(rate_hz))
        self._emit = emit_telemetry
        self._emit_event = emit_event
        n = max(2, int(count))
        self._drones: list[_MockDrone] = []
        for i in range(n):
            ang = 2 * math.pi * i / n
            d = _MockDrone(id=i + 1, x=30 * math.cos(ang), y=30 * math.sin(ang))
            d.battery = 100.0 - random.uniform(0, 25)
            d.next_beacon = random.uniform(0, BEACON_PERIOD_S)
            self._drones.append(d)
        self._by_id = {d.id: d for d in self._drones}
        self._by_id[1].role = "LEADER"
        if 2 in self._by_id:
            self._by_id[2].role = "CANDIDATE"
        for did, pct in ((6, 24.0), (7, 14.0)):
            if did in self._by_id:
                self._by_id[did].battery = pct
                self._by_id[did].drain = 0.06
        self._leader_id = 1
        self._formation = "RING"
        self._spacing = 15.0
        self._withdrew = False

    # ----------------------------------------------------------------- control
    def apply_command(self, cmd: DroneCommand) -> None:
        """Apply an operator command to the simulation."""
        d = self._by_id.get(cmd.target_id)
        if d is None:
            return
        name = cmd.command.upper()
        if name == "HOVER":
            d.mode, d.tx, d.ty, d.tz = "hover", d.x, d.y, d.z
        elif name == "LAND":
            d.mode, d.tx, d.ty, d.tz = "land", d.x, d.y, 0.0
        elif name == "RTH":
            d.mode, d.tx, d.ty, d.tz = "rth", 0.0, 0.0, 25.0
        elif name == "GOTO":
            east = math.radians(cmd.param2 - self._lon0) * EARTH_R * math.cos(math.radians(self._lat0))
            north = math.radians(cmd.param1 - self._lat0) * EARTH_R
            d.mode, d.tx, d.ty, d.tz = "goto", east, north, max(2.0, cmd.param3)
        elif name in ("TRIANGLE", "LINE"):
            self._formation = name
            self._spacing = cmd.param1 if cmd.param1 > 0 else 15.0
            for other in self._drones:
                if other.mode in ("hover", "goto"):
                    other.mode = "follow"
            d.mode = "follow"

    async def run(self) -> None:
        """Run the simulation until cancelled."""
        interval = 1.0 / self._rate
        t0 = last = time.monotonic()
        self._emit_event(SwarmEvent(
            "leader_election", 1,
            f"Election term 1: drone #1 elected LEADER ({len(self._drones)}/{len(self._drones)} votes)"))
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            dt = min(now - last, 0.5)
            last = now
            t = now - t0
            self._step(t, dt)
            self._publish(t)

    # -------------------------------------------------------------- simulation
    def _to_latlon(self, x: float, y: float) -> tuple[float, float]:
        lat = self._lat0 + math.degrees(y / EARTH_R)
        lon = self._lon0 + math.degrees(x / (EARTH_R * math.cos(math.radians(self._lat0))))
        return lat, lon

    def _offset(self, k: int, n: int) -> tuple[float, float]:
        """Formation slot ``k`` (0-based) of ``n`` followers, relative to the leader."""
        s = self._spacing
        if self._formation == "LINE":
            return (k - (n - 1) / 2) * s, -s
        if self._formation == "TRIANGLE":
            row = int((math.sqrt(8 * k + 1) - 1) // 2)       # triangular-number row
            col = k - row * (row + 1) // 2
            return (col - row / 2) * s, -(row + 1) * s * 0.87
        ang = 2 * math.pi * k / max(1, n)                     # RING (default)
        return 40 * math.cos(ang), 40 * math.sin(ang)

    @staticmethod
    def _move(d: _MockDrone, tx: float, ty: float, tz: float, dt: float) -> None:
        ex, ey = tx - d.x, ty - d.y
        dist = math.hypot(ex, ey)
        kp = 0.8
        scale = min(1.0, V_MAX / (kp * dist)) if dist > 1e-6 else 0.0
        a = min(1.0, dt * 2.0)
        d.vx += (kp * ex * scale - d.vx) * a
        d.vy += (kp * ey * scale - d.vy) * a
        d.vz += (max(-3.0, min(3.0, tz - d.z)) - d.vz) * a
        d.x += d.vx * dt
        d.y += d.vy * dt
        d.z = max(0.0, d.z + d.vz * dt)

    def _step(self, t: float, dt: float) -> None:
        leader = self._by_id[self._leader_id]
        ang = t * ORBIT_SPEED_MS / ORBIT_RADIUS_M
        orbit = (ORBIT_RADIUS_M * math.cos(ang), ORBIT_RADIUS_M * math.sin(ang))
        followers = [d for d in self._drones if d.id != leader.id]
        slot = {d.id: k for k, d in enumerate(followers)}

        if t >= 8.0 and not self._withdrew:
            self._withdrew = True
            if 2 in self._by_id:
                self._by_id[2].role = "FOLLOWER"
                self._emit_event(SwarmEvent("leader_election", 2,
                                            "Drone #2 withdrew candidacy; #1 remains LEADER"))

        for d in self._drones:
            if d.mode == "follow":
                if d.id == leader.id:
                    self._move(d, orbit[0], orbit[1], CRUISE_ALT_M, dt)
                else:
                    ox, oy = self._offset(slot[d.id], len(followers))
                    alt = CRUISE_ALT_M + 5 * math.sin(t / 20 + d.id)
                    self._move(d, leader.x + ox, leader.y + oy, alt, dt)
            elif d.mode in ("hover", "goto"):
                self._move(d, d.tx, d.ty, d.tz, dt)
            elif d.mode == "rth":
                self._move(d, d.tx, d.ty, d.tz, dt)
                if math.hypot(d.x, d.y) < 3.0:
                    d.mode, d.tx, d.ty, d.tz = "land", d.x, d.y, 0.0
            elif d.mode == "land":
                self._move(d, d.tx, d.ty, 0.0, dt)
                if d.z < 0.2:
                    d.mode, d.vx, d.vy, d.vz = "landed", 0.0, 0.0, 0.0
            drain = d.drain * (0.2 if d.mode == "landed" else 1.0)
            d.battery = max(0.0, d.battery - drain * dt)
            self._update_link(d, t, dt)

    def _update_link(self, d: _MockDrone, t: float, dt: float) -> None:
        dist = max(5.0, math.hypot(d.x, d.y))
        rssi = -45.0 - 18.0 * math.log10(dist / 5.0)
        if d.id == self.FAILOVER_ID:
            sweep = 0.5 + 0.5 * math.sin(2 * math.pi * t / 80.0 - math.pi / 2)
            rssi = -60.0 - 30.0 * sweep
        d.rssi_wifi = rssi + random.uniform(-1.5, 1.5)
        d.rssi_lora = -88.0 + random.uniform(-3, 3)
        if d.rssi_wifi < -80.0:
            d.bad_s += dt
            d.good_s = 0.0
        elif d.rssi_wifi > -70.0:
            d.good_s += dt
            d.bad_s = 0.0
        else:
            d.bad_s = d.good_s = 0.0
        if d.wifi_ok and d.bad_s >= 3.0:
            d.wifi_ok = False
        elif not d.wifi_ok and d.good_s >= 3.0:
            d.wifi_ok = True

    def _publish(self, t: float) -> None:
        lo, hi = self.DROPOUT_WINDOW
        for d in self._drones:
            if d.id == self.DROPOUT_ID and lo <= t < hi:
                continue                                    # radio silence
            lat, lon = self._to_latlon(d.x, d.y)
            base = {
                "id": d.id, "lat": round(lat, 7), "lon": round(lon, 7),
                "alt": round(d.z, 1), "bat": round(d.battery, 1), "role": d.role,
                "speed": round(math.hypot(d.vx, d.vy), 2), "ts": time.time(),
            }
            if d.wifi_ok:
                self._emit("wifi", {**base, "wifi_rssi": round(d.rssi_wifi, 1),
                                    "lora_rssi": round(d.rssi_lora, 1)})
            if t >= d.next_beacon:
                d.next_beacon = t + BEACON_PERIOD_S
                self._emit("lora", {**base, "lora_rssi": round(d.rssi_lora, 1)})
