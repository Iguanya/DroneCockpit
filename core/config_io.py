"""Read/write the cockpit config as YAML."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def save_config(path: Path, cfg: dict) -> bool:
    """Write ``cfg`` to ``path`` as YAML (in place, sorted keys preserved).

    Returns True on success, False on any I/O error. Never raises — the caller
    is a UI slot and must not crash the event loop.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False,
                           allow_unicode=True)
        return True
    except OSError as exc:
        log.error("Could not save %s: %s", path, exc)
        return False