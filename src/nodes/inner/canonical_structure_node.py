"""Stage 1: deterministic canonical structure extraction."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.inner.common import envelope_from_state, resolve_json_payload
from src.services.contracts import add_degradation
from src.services.domain_utils import build_fact_layer


class CanonicalStructureNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("canonical_structure_skipped", {"reason": "upstream_error"}, state)
            return {}

        envelope = envelope_from_state(state)
        scope = envelope.get("request_scope", {})
        payload: dict[str, Any] = {}
        records: list[dict[str, Any]] = []
        try:
            payload = resolve_json_payload(self._config, state, envelope.get("canonical_payload_ref"), scope)
            records = payload.get("records", []) if isinstance(payload.get("records"), list) else []
        except Exception:  # noqa: BLE001
            records = []

        facts = build_fact_layer(
            records,
            min_quote_chars=int(self._config.get("min_quote_chars", 8)),
            max_quote_chars=int(self._config.get("max_quote_chars", 300)),
            check_action_markers=list(self._config.get("check_action_markers", [])),
            counterpart_label=str(envelope.get("validated_input", {}).get("counterpart_label", "unspecified")),
        )
        degradation = envelope.get("degradation") or {"reason_codes": [], "detail_codes": []}
        input_quality = envelope.get("input_quality") or {}
        if input_quality.get("accepted_count", 0) == 0:
            degradation = add_degradation(degradation, reason="input_quality_rejected")
        emit_trace_event(
            "canonical_structure_built",
            {
                "source_count": len(facts["source_registry"]),
                "check_count": len(facts["completed_checks"]),
                "identifier_count": len(facts["observed_identifiers"]),
            },
            state,
        )
        return {
            "mode": "invoke",
            "incident_ref": envelope.get("incident_ref", ""),
            "canonical_payload_ref": envelope.get("canonical_payload_ref", ""),
            "request_scope": scope,
            "counterpart_carrier_id": scope.get("counterpart_carrier_id", ""),
            "validated_input": envelope.get("validated_input", {}),
            "template_check": envelope.get("template_check", {}),
            "input_quality": input_quality,
            "degradation": degradation,
            **facts,
            "status": AgentStatus.SUCCESS.value,
        }
