"""Drone Swarm Cockpit - entry point.

Usage:
    python main.py                      # uses ./config.yaml
    python main.py --config my.yaml
    python main.py --mock               # simulated swarm, no broker needed
(or set mock_mode: true in config.yaml / DRONE_COCKPIT_MOCK=1)
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import os
import sys
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "mock_mode": False,
    "mqtt": {"broker": "127.0.0.1", "port": 1883, "client_id": "drone-cockpit-1", "keepalive": 60},
    "map": {"tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "center": [28.6139, 77.2090], "zoom": 13, "trail_length": 50, "trail_interval": 1.0,
            "geofences": []},
    "theme": "dark",
    "telemetry": {"history_seconds": 300, "stale_timeout": 2.0, "lost_timeout": 5.0, "lora_grace": 6.0},
    "mock": {"drones": 12, "rate_hz": 10},
    "ui": {"software_rendering": False},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively overlay ``override`` onto a copy of ``base``."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path) -> dict:
    """Load ``path`` (YAML) over :data:`DEFAULTS`.

    Raises:
        SystemExit: if the file exists but cannot be parsed.
    """
    if not path.exists():
        logging.getLogger("main").warning("%s not found - using built-in defaults", path)
        return copy.deepcopy(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as fh:
            return _deep_merge(DEFAULTS, yaml.safe_load(fh) or {})
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {path}: {exc}")


def main() -> int:
    """Create the Qt application, wire everything together and run it."""
    parser = argparse.ArgumentParser(description="Drone Swarm Cockpit")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    parser.add_argument("--mock", action="store_true", help="run with a simulated swarm (no broker)")
    args, qt_args = parser.parse_known_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(Path(args.config))
    if args.mock or os.environ.get("DRONE_COCKPIT_MOCK") == "1":
        cfg["mock_mode"] = True
    if cfg["ui"].get("software_rendering"):
        os.environ.setdefault("QT_QUICK_BACKEND", "software")

    # Qt imports come after the environment tweaks above.
    import qasync
    from PySide6.QtWidgets import QApplication

    from core.mqtt_client import MqttClient
    from core.state import DroneRegistry
    from ui.main_window import MainWindow

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Drone Swarm Cockpit")
    app.setStyle("Fusion")

    # qasync: the asyncio loop *is* the Qt loop. (asyncio.get_event_loop() without a
    # running loop is deprecated, so the loop is installed explicitly instead.)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    registry = DroneRegistry(history_seconds=float(cfg["telemetry"]["history_seconds"]))
    client = MqttClient(cfg, registry, loop=loop)
    client.telemetry_received.connect(registry.update)
    client.registration_requested.connect(lambda req: registry.set_capabilities(req["id"], req["payload"].get("capabilities", {})))

    window = MainWindow(cfg, registry, client, config_path=Path(args.config))
    window.show()
    app.aboutToQuit.connect(client.shutdown)

    from PySide6.QtCore import QTimer
    QTimer.singleShot(0, client.start)                  # connect once the loop is running

    with loop:
        loop.run_forever()                              # runs app.exec() under the hood
    return 0


if __name__ == "__main__":
    sys.exit(main())
