"""Feedback mode: append human decisions for perspectives."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.inner.common import envelope_from_state, resolve_json_payload
from src.services.perspective_ledger import PerspectiveLedgerRejected


class FeedbackNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("feedback_recorded", {"skipped": True}, state)
            return {}
        envelope = envelope_from_state(state)
        scope = envelope.get("request_scope", {})
        validated = envelope.get("validated_input", {})
        decision = validated.get("decision", "")
        revised_text = None
        if decision == "revise":
            payload = resolve_json_payload(self._config, state, envelope.get("canonical_payload_ref"), scope)
            text = (payload.get("feedback") or {}).get("revised_perspective_text")
            if not isinstance(text, str) or not text.strip():
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["FeedbackNode: revised_perspective_text invalid"],
                }
            revised_text = text
        ledger = self._config.get("perspective_ledger")
        if ledger is None:
            return {"status": AgentStatus.ERROR.value, "error_log": ["FeedbackNode: perspective_ledger_not_configured"]}
        try:
            result = ledger.append_feedback(
                scope=scope,
                source_record_id=str(validated.get("source_record_id", "")),
                decision=str(decision),
                decided_by=str(validated.get("decided_by", "")),
                decided_at=str(validated.get("decided_at", "")),
                rationale_ref=str(validated.get("rationale_ref", "")),
                revised_perspective_text=revised_text,
                state=state,
            )
        except PerspectiveLedgerRejected as exc:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"FeedbackNode: revised_perspective_text invalid ({exc.reason_code})"],
            }
        emit_trace_event("feedback_recorded", {"decision": decision, "record_id": result.get("record_id", "")}, state)
        return {"mode": "feedback", "feedback_result": result, "status": AgentStatus.SUCCESS.value}
