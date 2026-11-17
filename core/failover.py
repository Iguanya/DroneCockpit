"""Transport selection and failover decisions (pure functions, no Qt)."""
from __future__ import annotations

from typing import Sequence

from core.models import DroneTelemetry

#: Wi-Fi RSSI below this is considered unusable (matches the node firmware).
RSSI_FAIL_DBM: float = -80.0
#: Wi-Fi RSSI above this is considered recovered (hysteresis, matches firmware).
RSSI_RECOVER_DBM: float = -70.0
#: Number of consecutive readings required to declare failover / recovery.
HISTORY_SAMPLES: int = 3


def select_transport(drone: DroneTelemetry) -> str:
    """Choose the link a command to ``drone`` should be sent on.

    Returns ``"lora"`` if the drone is already on LoRa or its Wi-Fi RSSI is
    below :data:`RSSI_FAIL_DBM`; otherwise ``"wifi"``.
    """
    if drone.transport == "lora" or drone.rssi_wifi < RSSI_FAIL_DBM:
        return "lora"
    return "wifi"


def should_failover(drone: DroneTelemetry, history: Sequence[float]) -> bool:
    """True if a Wi-Fi drone should be moved to LoRa.

    Requires the drone to currently be on Wi-Fi and its last
    :data:`HISTORY_SAMPLES` Wi-Fi RSSI readings (``history`` is oldest-first)
    to all be below :data:`RSSI_FAIL_DBM`. Fewer than 3 readings -> ``False``.
    """
    if drone.transport != "wifi" or len(history) < HISTORY_SAMPLES:
        return False
    return all(r < RSSI_FAIL_DBM for r in list(history)[-HISTORY_SAMPLES:])


def should_recover(drone: DroneTelemetry, history: Sequence[float]) -> bool:
    """True if a LoRa drone should return to Wi-Fi (last 3 readings > -70 dBm)."""
    if drone.transport != "lora" or len(history) < HISTORY_SAMPLES:
        return False
    return all(r > RSSI_RECOVER_DBM for r in list(history)[-HISTORY_SAMPLES:])
