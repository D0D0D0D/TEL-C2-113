"""Common helpers for inner nodes."""

from __future__ import annotations

import json
from typing import Any

from src.services.domain_utils import ensure_clean_scope


def envelope_from_state(state: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    raw = state.get("user_input")
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = {}
        if isinstance(value, dict):
            parsed.update(value)
    for key in (
        "mode",
        "incident_ref",
        "canonical_payload_ref",
        "request_scope",
        "validated_input",
        "template_check",
        "input_quality",
        "degradation",
    ):
        if key in state and key not in parsed:
            parsed[key] = state[key]
    if "request_scope" in parsed:
        parsed["request_scope"] = ensure_clean_scope(parsed["request_scope"])
        parsed["counterpart_carrier_id"] = parsed["request_scope"]["counterpart_carrier_id"]
    return parsed


def resolve_json_payload(config: dict[str, Any], state: dict[str, Any], ref: str | None, scope: dict[str, str]) -> dict[str, Any]:
    if not ref:
        return {}
    store = config.get("payload_store")
    if store is None:
        raise RuntimeError("payload_store_not_configured")
    raw = store.resolve(ref, scope=scope, session_id=state.get("session_id", ""), consume=False)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        value = json.loads(raw)
    else:
        value = raw
    return dict(value) if isinstance(value, dict) else {}
