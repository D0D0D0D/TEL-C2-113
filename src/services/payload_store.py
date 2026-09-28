"""Domain payload-store wiring."""

from __future__ import annotations

from typing import Any, Callable

from src.payload_store import PayloadStore, PayloadStoreError, is_reference, mint_reference
from src.services.contracts import SCOPE_KEYS


def build_payload_store(config: dict[str, Any], *, clock: Callable[[], float] | None = None) -> PayloadStore:
    kwargs: dict[str, Any] = {
        "scope_keys": SCOPE_KEYS,
        "ttl_seconds": int(config.get("payload_ttl_s", 900)),
    }
    if clock is not None:
        kwargs["clock"] = clock
    return PayloadStore(**kwargs)


__all__ = ["PayloadStore", "PayloadStoreError", "build_payload_store", "is_reference", "mint_reference"]
