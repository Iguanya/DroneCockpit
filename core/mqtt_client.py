"""MQTT bridge between the Mosquitto broker and the Qt UI.

Threading model
---------------
No threads are used. paho-mqtt's *network loop is driven manually* from the Qt
event loop (the pattern paho documents for asyncio):

  * ``on_socket_open`` / ``on_socket_register_write`` -> ``QSocketNotifier`` objects
    call ``loop_read()`` / ``loop_write()`` when the socket is ready;
  * a 1 Hz asyncio task (running on the qasync loop) calls ``loop_misc()`` for
    keep-alive and computes message-rate / latency statistics;
  * the connect/reconnect state machine is an asyncio coroutine.

Every paho callback therefore runs on the GUI thread and only emits Qt signals.
QSocketNotifier is used instead of ``loop.add_reader`` because it behaves the same
on Windows, macOS and Linux (qasync's Windows loop is proactor based and has no
``add_reader``).

The one blocking call, ``Client.connect()``, is preceded by an asyncio TCP probe
with a timeout, so an unreachable broker can never freeze the UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import replace
from typing import Any, Optional

import paho.mqtt.client as mqtt
from PySide6.QtCore import QObject, QSocketNotifier, Signal

from core.failover import select_transport, should_failover
from core.mock import MockSwarm
from core.models import RSSI_UNKNOWN, DroneCommand, DroneTelemetry, SwarmEvent
from core.state import DroneRegistry

log = logging.getLogger(__name__)

TRANSPORTS = ("wifi", "lora")
#: A drone counts as "on Wi-Fi" while Wi-Fi telemetry is younger than this (s).
WIFI_HOLD_S = 3.0
#: How long an RSSI reading stays valid, per transport (s).
RSSI_TTL_S = {"wifi": 5.0, "lora": 15.0}
#: LoRa data younger than this keeps a drone considered alive after Wi-Fi goes quiet (s).
LORA_ALIVE_S = 8.0
PROBE_TIMEOUT_S = 3.0
CONNACK_TIMEOUT_S = 8.0


def _num(data: dict, *keys: str) -> Optional[float]:
    """First finite float found under any of ``keys``, else ``None``."""
    for k in keys:
        v = data.get(k)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            return f
    return None


def _num_or_nan(data: dict, *keys: str) -> float:
    """Like :func:`_num` but returns NaN when no key is present."""
    v = _num(data, *keys)
    return math.nan if v is None else v


def _normalize_role(value: Any) -> str:
    """Role text -> upper case. Integers use the node firmware's mesh type
    (1 = root -> LEADER, anything else -> FOLLOWER). Empty string if absent."""
    if isinstance(value, str):
        return value.strip().upper().replace(" ", "_")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "LEADER" if int(value) == 1 else "FOLLOWER"
    return ""


class _Link:
    """Per-drone record of the latest sample on each transport."""

    __slots__ = ("latest", "rssi", "transport")

    def __init__(self) -> None:
        self.latest: dict[str, DroneTelemetry] = {}
        self.rssi: dict[str, tuple[float, float]] = {}     # transport -> (dBm, wall time)
        self.transport: Optional[str] = None               # last transport we reported


class MqttClient(QObject):
    """Async MQTT client for the swarm topics.

    Signals:
        telemetry_received(DroneTelemetry): merged state after each message.
        event_received(SwarmEvent): broker events plus locally generated ones
            (joined, failover, command_sent, mqtt connection changes, pairing).
        connection_changed(str): ``"connected"``, ``"connecting"`` or ``"disconnected"``.
        stats_updated(float, float): message rate (msg/s) and broker latency (ms, NaN if unknown).
        registration_requested(dict): a drone asked to join (payload has id + capabilities).
    """

    telemetry_received = Signal(object)
    event_received = Signal(object)
    connection_changed = Signal(str)
    stats_updated = Signal(float, float)
    registration_requested = Signal(object)

    def __init__(
        self,
        config: dict,
        registry: DroneRegistry,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """Create the client (does not connect; call :meth:`start`).

        Args:
            config: Full application config dict.
            registry: Registry used to look up drones when choosing a command transport.
            loop: The qasync event loop (defaults to ``asyncio.get_event_loop()``).
        """
        super().__init__(parent)
        self._cfg = config
        m = config["mqtt"]
        self._host: str = m["broker"]
        self._port: int = int(m["port"])
        self._keepalive: int = int(m.get("keepalive", 60))
        self._client_id: str = m.get("client_id", "drone-cockpit")
        self._registry = registry
        self._loop = loop or asyncio.get_event_loop()
        self._mock_mode = bool(config.get("mock_mode", False))

        # ---- pairing / authorization ----
        sec = config.get("security", {}) or {}
        #: Empty allow-list = accept any drone (lab / mock mode).
        self._allowed_drones: set[int] = {int(x) for x in sec.get("allowed_drones", [])}
        self._pending_drones: dict[int, dict] = {}
        self._rejected_ids: set[int] = set()

        self._ping_topic = f"swarm/_cockpit/ping/{self._client_id}"
        self._subscriptions: list[tuple[str, int]] = [
            ("swarm/register/+", 1),             # pairing requests
            ("swarm/telemetry/wifi/+", 0),
            ("swarm/telemetry/lora/+", 0),
            ("swarm/telemetry/+", 0),            # legacy: node firmware publishing swarm/telemetry/{id}
            ("swarm/events/+", 1),
            ("swarm/heartbeat/+/+", 0),
            (self._ping_topic, 0),
        ]

        self._state = "disconnected"
        self._task: Optional[asyncio.Task] = None
        self._housekeeping_task: Optional[asyncio.Task] = None
        self._connack_evt: Optional[asyncio.Event] = None
        self._disc_evt: Optional[asyncio.Event] = None
        self._connack_ok = False
        self._outage_reported = False

        self._links: dict[int, _Link] = {}
        self._seen: set[int] = set()
        self._msg_count = 0
        self._latency_ms = math.nan
        self._cmd_seq = 0
        self._swarm: Optional[MockSwarm] = None

        self._rd: Optional[QSocketNotifier] = None
        self._wr: Optional[QSocketNotifier] = None
        self._client = self._build_paho(m)

    # -------------------------------------------------------------- properties
    @property
    def is_connected(self) -> bool:
        """True while the broker connection (or the mock feed) is up."""
        return self._state == "connected"

    @property
    def is_running(self) -> bool:
        """True while the connect/reconnect task is active."""
        return self._task is not None and not self._task.done()

    @property
    def mock_mode(self) -> bool:
        """True when telemetry comes from the built-in simulator."""
        return self._mock_mode

    @property
    def broker_label(self) -> str:
        """``host:port`` (or ``mock``) for display."""
        return "simulated swarm" if self._mock_mode else f"{self._host}:{self._port}"

    # ------------------------------------------------------------- public API
    def start(self) -> None:
        """Connect (or start the mock feed). Reconnects automatically until :meth:`stop`."""
        if self.is_running:
            return
        coro = self._run_mock() if self._mock_mode else self._run()
        self._task = self._loop.create_task(coro)

    def stop(self) -> None:
        """Disconnect and stop reconnecting."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._state != "disconnected":
            self._teardown_socket()
            self._set_state("disconnected")

    def shutdown(self) -> None:
        """Synchronous stop for application exit."""
        self.stop()

    def publish_command(self, command: DroneCommand) -> bool:
        """Publish ``command`` to ``swarm/command/{transport}/{target_id}``.

        The transport comes from :func:`core.failover.select_transport` applied to
        the drone's *live* merged state (so a Wi-Fi link that just went quiet is
        already treated as LoRa), plus :func:`core.failover.should_failover` on the
        recent RSSI history. Emits a ``command_sent`` or ``command_failed`` event.

        Returns:
            True if the command was handed to the broker (or the simulator).
        """
        did = command.target_id
        drone = self._live_view(did)
        transport = select_transport(drone) if drone else "wifi"
        if drone and transport == "wifi" and should_failover(drone, self._registry.rssi_history(did)):
            transport = "lora"
        topic = f"swarm/command/{transport}/{did}"

        ok = False
        if self._mock_mode:
            if self._swarm is not None and self.is_connected:
                self._swarm.apply_command(command)
                ok = True
        elif self.is_connected:
            self._cmd_seq += 1
            payload = json.dumps(command.to_payload(self._cmd_seq, self._client_id))
            try:
                ok = self._client.publish(topic, payload, qos=1).rc == mqtt.MQTT_ERR_SUCCESS
            except (ValueError, OSError) as exc:
                log.error("publish failed: %s", exc)

        detail = f"{command.command} → #{did} via {transport.upper()}"
        if command.command == "GOTO":
            detail += f" ({command.param1:.6f}, {command.param2:.6f}, {command.param3:.0f} m)"
        elif command.command in ("TRIANGLE", "LINE"):
            detail += f" (spacing {command.param1:.0f} m)"
        if ok:
            self.event_received.emit(SwarmEvent("command_sent", did, detail))
        else:
            self.event_received.emit(SwarmEvent("command_failed", did, detail + " — NOT SENT (not connected)"))
        return ok

    def approve_drone(self, did: int, allowed: bool) -> bool:
        """Publish ``swarm/register/<did>/ack`` with ``{"allowed": bool}``.

        On approval the drone is added to the runtime allow-list so its telemetry
        starts flowing. Emits ``paired`` / ``denied`` events.

        Returns:
            True if the ack was handed to the broker (or the simulator).
        """
        did = int(did)
        if self._mock_mode or not self.is_connected:
            # Still update local state so the mock UI stays consistent.
            if allowed:
                self._allowed_drones.add(did)
            self._pending_drones.pop(did, None)
            self.event_received.emit(SwarmEvent("paired" if allowed else "denied", did,
                                                f"Drone #{did} {'approved' if allowed else 'denied'} (offline)"))
            return False

        payload = json.dumps({"allowed": bool(allowed)})
        try:
            ok = self._client.publish(f"swarm/register/{did}/ack", payload, qos=1).rc == mqtt.MQTT_ERR_SUCCESS
        except (ValueError, OSError) as exc:
            log.error("approve_drone publish failed: %s", exc)
            return False
        if ok:
            if allowed:
                self._allowed_drones.add(did)
            self._pending_drones.pop(did, None)
            self.event_received.emit(SwarmEvent("paired" if allowed else "denied", did,
                                                f"Drone #{did} {'approved' if allowed else 'denied'}"))
        return ok

    def approved_drone_ids(self) -> set[int]:
        """Drone IDs currently allowed to publish telemetry."""
        return set(self._allowed_drones)

    def set_allowed_drones(self, ids: set[int]) -> None:
        """Replace the runtime allow-list (empty set = accept anything)."""
        self._allowed_drones = {int(x) for x in ids}
        self._rejected_ids.clear()          # so previously-rejected IDs can be re-evaluated

    # ------------------------------------------------------------ paho set-up
    def _build_paho(self, m: dict) -> mqtt.Client:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self._client_id,
                        protocol=mqtt.MQTTv311)
        if m.get("username"):
            c.username_pw_set(m["username"], m.get("password") or None)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.on_socket_open = self._on_socket_open
        c.on_socket_close = self._on_socket_close
        c.on_socket_register_write = self._on_socket_register_write
        c.on_socket_unregister_write = self._on_socket_unregister_write
        return c

    @staticmethod
    def _make_notifier(fd: int, kind: QSocketNotifier.Type, parent: QObject) -> QSocketNotifier:
        try:
            return QSocketNotifier(fd, kind, parent)
        except TypeError:                                   # Qt builds without the fd constructor
            n = QSocketNotifier(kind, parent)
            n.setSocket(fd)
            return n

    # ---- socket integration (all run on the GUI thread)
    def _on_socket_open(self, client: mqtt.Client, userdata: Any, sock: Any) -> None:
        self._drop_notifiers()
        self._rd = self._make_notifier(sock.fileno(), QSocketNotifier.Type.Read, self)
        self._rd.activated.connect(self._on_readable)

    def _on_socket_close(self, client: mqtt.Client, userdata: Any, sock: Any) -> None:
        self._drop_notifiers()

    def _on_socket_register_write(self, client: mqtt.Client, userdata: Any, sock: Any) -> None:
        if self._wr is None:
            self._wr = self._make_notifier(sock.fileno(), QSocketNotifier.Type.Write, self)
            self._wr.activated.connect(self._on_writable)

    def _on_socket_unregister_write(self, client: mqtt.Client, userdata: Any, sock: Any) -> None:
        if self._wr is not None:
            self._wr.setEnabled(False)
            self._wr.deleteLater()
            self._wr = None

    def _drop_notifiers(self) -> None:
        for name in ("_rd", "_wr"):
            n = getattr(self, name)
            if n is not None:
                n.setEnabled(False)
                n.deleteLater()
                setattr(self, name, None)

    def _on_readable(self, *_: Any) -> None:
        try:
            self._client.loop_read(64)
        except Exception:                                   # noqa: BLE001 - never kill the GUI loop
            log.exception("loop_read failed")

    def _on_writable(self, *_: Any) -> None:
        try:
            self._client.loop_write()
        except Exception:                                   # noqa: BLE001
            log.exception("loop_write failed")

    # ---- MQTT callbacks
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error("Broker refused connection: %s", reason_code)
            self._connack_ok = False
        else:
            self._connack_ok = True
            client.subscribe(self._subscriptions)
        if self._connack_evt is not None:
            self._connack_evt.set()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        if self._disc_evt is not None:
            self._disc_evt.set()

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        if msg.topic == self._ping_topic:
            try:
                ms = (time.monotonic() - float(msg.payload)) * 1000.0
                self._latency_ms = ms if math.isnan(self._latency_ms) else 0.7 * self._latency_ms + 0.3 * ms
            except ValueError:
                pass
            return
        self._msg_count += 1
        try:
            self._dispatch(msg.topic, msg.payload)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Dropped malformed message on %s: %s", msg.topic, exc)

    # ------------------------------------------------------- connection tasks
    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.connection_changed.emit(state)

    async def _probe_broker(self) -> None:
        """Cheap async reachability check so ``connect()`` cannot block the GUI."""
        _, writer = await asyncio.wait_for(asyncio.open_connection(self._host, self._port),
                                           PROBE_TIMEOUT_S)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    async def _run(self) -> None:
        backoff = 1.0
        self._housekeeping_task = self._loop.create_task(self._housekeeping())
        try:
            while True:
                self._set_state("connecting")
                self._connack_evt, self._disc_evt = asyncio.Event(), asyncio.Event()
                self._connack_ok = False
                try:
                    await self._probe_broker()
                    self._client.connect(self._host, self._port, self._keepalive)
                    await asyncio.wait_for(self._connack_evt.wait(), CONNACK_TIMEOUT_S)
                    if not self._connack_ok:
                        raise ConnectionError("broker refused the connection")
                    backoff = 1.0
                    self._outage_reported = False
                    self._set_state("connected")
                    self._emit_system_event(f"Connected to broker {self.broker_label}")
                    await self._disc_evt.wait()
                    self._emit_system_event("Connection to broker lost — reconnecting")
                    self._outage_reported = True
                except asyncio.CancelledError:
                    raise
                except (OSError, asyncio.TimeoutError, ConnectionError, ValueError) as exc:
                    if not self._outage_reported:
                        self._outage_reported = True
                        self._emit_system_event(f"Broker unavailable ({exc or type(exc).__name__}); retrying")
                finally:
                    self._teardown_socket()
                self._set_state("connecting")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
        finally:
            if self._housekeeping_task is not None:
                self._housekeeping_task.cancel()
                self._housekeeping_task = None
            self._teardown_socket()
            self._set_state("disconnected")

    async def _run_mock(self) -> None:
        mcfg = self._cfg.get("mock", {})
        cx, cy = self._cfg["map"]["center"]
        self._swarm = MockSwarm((float(cx), float(cy)), int(mcfg.get("drones", 12)),
                                float(mcfg.get("rate_hz", 10)),
                                self._on_mock_telemetry, self.event_received.emit)
        self._housekeeping_task = self._loop.create_task(self._housekeeping())
        self._set_state("connected")
        self._emit_system_event("MOCK MODE: simulated swarm running")
        try:
            await self._swarm.run()
        finally:
            if self._housekeeping_task is not None:
                self._housekeeping_task.cancel()
                self._housekeeping_task = None
            self._swarm = None
            self._set_state("disconnected")

    def _teardown_socket(self) -> None:
        if not self._mock_mode:
            try:
                self._client.disconnect()
            except Exception:                               # noqa: BLE001
                pass
        self._drop_notifiers()

    async def _housekeeping(self) -> None:
        """1 Hz: paho keep-alive, ping for latency, stats, Wi-Fi->LoRa re-evaluation."""
        last = time.monotonic()
        count0 = self._msg_count
        tick = 0
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            dt = max(1e-3, now - last)
            last = now
            tick += 1
            if not self._mock_mode and self._client.is_connected():
                try:
                    self._client.loop_misc()
                    if tick % 2 == 0:
                        self._client.publish(self._ping_topic, f"{time.monotonic():.6f}", qos=0)
                except Exception:                           # noqa: BLE001
                    log.exception("keep-alive failed")
            self._reevaluate_transports()
            rate = (self._msg_count - count0) / dt
            count0 = self._msg_count
            self.stats_updated.emit(rate, self._latency_ms)

    # ------------------------------------------------------ message handling
    def _on_mock_telemetry(self, transport: str, payload: dict) -> None:
        self._msg_count += 1
        self._on_telemetry(transport, str(payload["id"]), payload)

    def _dispatch(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        data = json.loads(payload) if payload else {}
        if not isinstance(data, dict):
            raise ValueError("payload is not a JSON object")
        if parts[:2] == ["swarm", "register"] and len(parts) == 3:
            self._on_registration(parts[2], data)
            return
        if parts[:2] == ["swarm", "telemetry"]:
            if len(parts) == 4 and parts[2] in TRANSPORTS:
                self._on_telemetry(parts[2], parts[3], data)
            elif len(parts) == 3:
                via = str(data.get("via", "wifi")).lower()
                self._on_telemetry("lora" if via == "lora" else "wifi", parts[2], data)
        elif parts[:2] == ["swarm", "events"] and len(parts) == 3:
            self._on_event_message(parts[2], data)
        elif parts[:2] == ["swarm", "heartbeat"] and len(parts) == 4:
            if parts[2] in TRANSPORTS:
                self._on_heartbeat(parts[2], parts[3], data)
            elif parts[3] in TRANSPORTS:
                self._on_heartbeat(parts[3], parts[2], data)

    @staticmethod
    def _drone_id(id_text: str, data: dict) -> int:
        try:
            return int(id_text)
        except ValueError:
            return int(data["id"])

    def _is_authorized(self, did: int) -> bool:
        """True if the drone is allowed to appear in the cockpit.

        An empty allow-list means "accept anything" (lab / mock mode).
        Unknown IDs are reported once as ``auth_rejected`` events.
        """
        if not self._allowed_drones or did in self._allowed_drones:
            return True
        if did not in self._rejected_ids:
            self._rejected_ids.add(did)
            log.warning("Rejected unauthorized drone #%d", did)
            self.event_received.emit(
                SwarmEvent("auth_rejected", did, f"Unauthorized drone #{did} ignored"))
        return False

    def _on_registration(self, id_text: str, data: dict) -> None:
        """A drone has asked to join (topic ``swarm/register/<id>``)."""
        try:
            did = int(id_text)
        except ValueError:
            did = int(data.get("id", -1))
        if did < 0:
            return
        self._pending_drones[did] = data
        log.info("Pairing request from drone #%d", did)
        self.registration_requested.emit({"id": did, "payload": data})

    def _build_sample(self, did: int, via: str, data: dict) -> DroneTelemetry:
        now = time.time()
        ts = _num(data, "ts", "timestamp", "time")
        if ts is not None and ts > 1e11:                    # milliseconds
            ts /= 1000.0
        timestamp = ts if ts is not None and abs(ts - now) < 60.0 else now
        bat = _num(data, "battery", "bat", "battery_pct")
        if bat is not None and not 0.0 <= bat <= 100.0:
            bat = None                                      # 255 = unknown on the nodes
        generic = _num(data, "rssi")
        rw = _num(data, "rssi_wifi", "wifi_rssi")
        rl = _num(data, "rssi_lora", "lora_rssi")
        if generic is not None:
            if via == "wifi" and rw is None:
                rw = generic
            elif via == "lora" and rl is None:
                rl = generic
        nan = math.nan
        return DroneTelemetry(
            id=did,
            lat=_num_or_nan(data, "lat", "latitude"),
            lon=_num_or_nan(data, "lon", "lng", "longitude"),
            alt=_num_or_nan(data, "alt", "altitude"),
            battery=bat if bat is not None else nan,
            role=_normalize_role(data.get("role")),
            rssi_wifi=rw if rw is not None else RSSI_UNKNOWN,
            rssi_lora=rl if rl is not None else RSSI_UNKNOWN,
            transport=via,
            last_seen=now,
            timestamp=timestamp,
            speed=_num(data, "speed", "groundspeed"),
        )

    def _on_telemetry(self, via: str, id_text: str, data: dict) -> None:
        did = self._drone_id(id_text, data)
        if not self._is_authorized(did):
            return
        self._ingest(self._build_sample(did, via, data), via)

    def _on_heartbeat(self, via: str, id_text: str, data: dict) -> None:
        """A heartbeat refreshes liveness (and RSSI/role if supplied) without position."""
        did = self._drone_id(id_text, data)
        if not self._is_authorized(did):
            return
        sample = self._build_sample(did, via, data)
        sample.lat = sample.lon = sample.alt = math.nan     # never override real data
        sample.timestamp = 0.0                              # so position samples always win
        self._ingest(sample, via)

    def _on_event_message(self, tail: str, data: dict) -> None:
        etype = str(data.get("event_type") or data.get("type") or ("event" if tail.isdigit() else tail))
        did = data.get("drone_id", data.get("id"))
        if did is None and tail.isdigit():
            did = int(tail)
        ts = _num(data, "timestamp", "ts")
        self.event_received.emit(SwarmEvent(
            etype.lower(), int(did) if did is not None else None,
            str(data.get("message", data.get("msg", ""))), ts if ts else time.time()))

    # ------------------------------------------------------ dual-link merging
    def _ingest(self, sample: DroneTelemetry, via: str) -> None:
        link = self._links.setdefault(sample.id, _Link())
        link.latest[via] = sample
        for t, val in (("wifi", sample.rssi_wifi), ("lora", sample.rssi_lora)):
            if val > RSSI_UNKNOWN:
                link.rssi[t] = (val, sample.last_seen)
        if sample.id not in self._seen:
            self._seen.add(sample.id)
            self.event_received.emit(SwarmEvent("drone_joined", sample.id,
                                                f"Drone #{sample.id} online via {via.upper()}"))
        self._emit_merged(link, sample.id, sample.last_seen)

    def _merge(self, link: _Link, now: float) -> DroneTelemetry:
        """Combine the latest Wi-Fi and LoRa samples.

        Data fields come from the most recent *timestamp* that carries them;
        RSSI values expire; ``transport`` is ``wifi`` while Wi-Fi data is fresh,
        otherwise ``lora`` (so LoRa's occasional beacon cannot flip a healthy
        Wi-Fi drone to LORA_ONLY).
        """
        samples = sorted(link.latest.values(), key=lambda s: s.timestamp, reverse=True)
        pos = next((s for s in samples if s.has_position), None)
        wifi_s = link.latest.get("wifi")
        if wifi_s is not None and now - wifi_s.last_seen <= WIFI_HOLD_S:
            transport = "wifi"
        elif "lora" in link.latest:
            transport = "lora"
        else:
            transport = samples[0].transport

        def fresh(t: str) -> float:
            v = link.rssi.get(t)
            return v[0] if v and now - v[1] <= RSSI_TTL_S[t] else RSSI_UNKNOWN

        return DroneTelemetry(
            id=samples[0].id,
            lat=pos.lat if pos else math.nan,
            lon=pos.lon if pos else math.nan,
            alt=next((s.alt for s in samples if math.isfinite(s.alt)), 0.0),
            battery=next((s.battery for s in samples if math.isfinite(s.battery)), math.nan),
            role=next((s.role for s in samples if s.role), "FOLLOWER"),
            rssi_wifi=fresh("wifi"),
            rssi_lora=fresh("lora"),
            transport=transport,
            last_seen=max(s.last_seen for s in samples),
            timestamp=pos.timestamp if pos else samples[0].timestamp,
            speed=next((s.speed for s in samples if s.speed is not None), None),
        )

    def _emit_merged(self, link: _Link, did: int, now: float) -> None:
        merged = self._merge(link, now)
        prev, link.transport = link.transport, merged.transport
        if prev is not None and prev != merged.transport:
            if merged.transport == "lora":
                why = (f"Wi-Fi RSSI {merged.rssi_wifi:.0f} dBm"
                       if merged.rssi_wifi > RSSI_UNKNOWN else "Wi-Fi telemetry lost")
                self.event_received.emit(SwarmEvent("failover", did, f"Drone #{did}: Wi-Fi → LoRa ({why})"))
            else:
                self.event_received.emit(SwarmEvent("failover", did, f"Drone #{did}: LoRa → Wi-Fi (link recovered)"))
        self.telemetry_received.emit(merged)

    def _reevaluate_transports(self) -> None:
        """Flip drones whose Wi-Fi went quiet to LoRa without waiting for the next beacon."""
        now = time.time()
        for did, link in self._links.items():
            lora_s = link.latest.get("lora")
            wifi_s = link.latest.get("wifi")
            # Only flip if LoRa was heard *after* Wi-Fi went quiet; otherwise the drone
            # is simply silent (radio loss / power loss), not failed over.
            if (link.transport == "wifi" and lora_s is not None and wifi_s is not None
                    and now - wifi_s.last_seen > WIFI_HOLD_S
                    and lora_s.last_seen > wifi_s.last_seen
                    and now - lora_s.last_seen <= LORA_ALIVE_S):
                self._emit_merged(link, did, now)

    def _live_view(self, did: int) -> Optional[DroneTelemetry]:
        link = self._links.get(did)
        if link and link.latest:
            return self._merge(link, time.time())
        return self._registry.get(did)

    def _emit_system_event(self, text: str) -> None:
        self.event_received.emit(SwarmEvent("mqtt", None, text))