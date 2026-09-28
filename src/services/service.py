"""Service facade for TEL-C2-113."""

from __future__ import annotations

from typing import Any

from src.services.wiring import wire_services


class Service:
    """Small facade used by local integrations that need the wired seams."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = wire_services(dict(config or {}))

    def get(self, name: str) -> Any:
        return self.config.get(name)
