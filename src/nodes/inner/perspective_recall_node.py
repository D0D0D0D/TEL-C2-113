"""Stage 2: scoped recall of non-diagnostic handover perspectives."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.scoped_retrieval import ScopedRetrievalNode
from src.services.contracts import add_degradation
from src.services.domain_utils import normalize_query

TOP_LEVEL_SAFE_KEYS = frozenset({"summary", "kind", "score"})
METADATA_SAFE_KEYS = frozenset({"record_id", "source_id", "provenance", "recorded_period", "counterpart_carrier_id"})


class PerspectiveRecallNode(ScopedRetrievalNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        super().__init__(
            scope_keys=("counterpart_carrier_id",),
            backend=self._config.get("kb"),
            normalize_query=normalize_query,
            top_k=int(self._config.get("recall_top_k", 5)),
        )

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("perspective_recall_degraded", {"reason": "upstream_error"}, state)
            return {}
        scope = state.get("request_scope") if isinstance(state.get("request_scope"), dict) else {}
        scope_value = scope.get("counterpart_carrier_id")
        if not isinstance(scope_value, str) or not scope_value.strip():
            degradation = add_degradation(state.get("degradation"), reason="kb_scope_unresolved")
            emit_trace_event("perspective_recall_degraded", {"reason": "scope_missing"}, state)
            return {
                "recalled_perspectives": [],
                "prior_ids": [],
                "degradation": degradation,
                "status": AgentStatus.SUCCESS.value,
            }
        working = dict(state)
        working["counterpart_carrier_id"] = scope_value
        working["query"] = state.get("structured_query")
        base = super().execute(working)
        if base.get("status") == AgentStatus.ERROR.value:
            projected = []
        else:
            projected = [_safe_projection(hit, {"counterpart_carrier_id": scope_value}) for hit in base.get("retrieved_records", [])]
            projected = [hit for hit in projected if hit is not None]
        degradation = state.get("degradation") or {"reason_codes": [], "detail_codes": []}
        if not projected:
            degradation = add_degradation(degradation, reason="kb_scope_unresolved")
            emit_trace_event("perspective_recall_degraded", {"reason": "empty"}, state)
        else:
            emit_trace_event("perspective_recall_complete", {"count": len(projected)}, state)
        return {
            "recalled_perspectives": projected,
            "prior_ids": [item["record_id"] for item in projected],
            "degradation": degradation,
            "status": AgentStatus.SUCCESS.value,
        }


def _safe_projection(hit: Any, scope: dict[str, str]) -> dict[str, Any] | None:
    if not isinstance(hit, dict):
        return None
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    if metadata.get("counterpart_carrier_id") != scope["counterpart_carrier_id"]:
        return None
    record_id = metadata.get("record_id") or hit.get("source_id")
    if not isinstance(record_id, str) or not record_id:
        return None
    result = {
        "record_id": record_id,
        "perspective_text": str(hit.get("summary", ""))[:512],
        "metadata": {
            key: metadata[key]
            for key in METADATA_SAFE_KEYS
            if key in metadata and isinstance(metadata[key], (str, int, float, bool))
        },
    }
    for forbidden in ("support_status", "verdict", "diagnosis", "root_cause"):
        result.pop(forbidden, None)
        result["metadata"].pop(forbidden, None)
    return result
