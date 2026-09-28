"""Service wiring for standalone server and tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.utils.config_loader import load_config
from src.services.payload_store import build_payload_store
from src.services.perspective_ledger import InMemoryPerspectiveLedger


def load_runtime_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    config = load_config(str(path)) if path.exists() else {}
    config.update(dict(overrides or {}))
    return config


def wire_services(config: dict[str, Any]) -> dict[str, Any]:
    """Attach default local service seams when production seams are absent."""

    if config.get("payload_store") is None:
        config["payload_store"] = build_payload_store(config)
    if config.get("perspective_ledger") is None:
        config["perspective_ledger"] = InMemoryPerspectiveLedger()
    if config.get("kb") is None:
        config["kb"] = config["perspective_ledger"]
    return config


__all__ = ["load_runtime_config", "wire_services"]
