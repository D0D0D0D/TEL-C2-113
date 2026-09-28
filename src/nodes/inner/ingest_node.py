"""Ingest mode: append non-diagnostic perspectives."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.inner.common import envelope_from_state, resolve_json_payload
from src.services.contracts import UNRESOLVED_KINDS
from src.services.perspective_ledger import PerspectiveLedgerRejected


class IngestNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("ingest_rows_rejected", {"reason": "upstream_error"}, state)
            return {}
        envelope = envelope_from_state(state)
        scope = envelope.get("request_scope", {})
        payload = resolve_json_payload(self._config, state, envelope.get("canonical_payload_ref"), scope)
        rows = payload.get("perspectives", []) if isinstance(payload.get("perspectives"), list) else []
        ledger = self._config.get("perspective_ledger")
        written_ids: list[str] = []
        rejected: list[dict[str, str]] = list((envelope.get("input_quality") or {}).get("rejected", []))
        for row in rows:
            record_id = str(row.get("record_id", ""))
            kind = row.get("perspective_kind")
            text = row.get("perspective_text")
            source_ref = row.get("source_ref")
            if kind not in UNRESOLVED_KINDS:
                rejected.append({"record_id": record_id, "reason_code": "unknown_perspective_kind"})
                continue
            if not isinstance(source_ref, str) or not source_ref.strip():
                rejected.append({"record_id": record_id, "reason_code": "missing_source_ref"})
                continue
            if not isinstance(text, str) or not text.strip():
                rejected.append({"record_id": record_id, "reason_code": "record_empty"})
                continue
            if ledger is None:
                rejected.append({"record_id": record_id, "reason_code": "instruction_content"})
                continue
            try:
                written_ids.append(
                    ledger.write_perspective(
                        scope=scope,
                        perspective_kind=kind,
                        perspective_text=text,
                        source_ref=source_ref,
                        provenance={"source": "ingest", "input_record_id": record_id},
                        state=state,
                    )
                )
            except PerspectiveLedgerRejected as exc:
                rejected.append({"record_id": record_id, "reason_code": exc.reason_code})
            except Exception:  # noqa: BLE001
                rejected.append({"record_id": record_id, "reason_code": "instruction_content"})
                continue
        result = {"written_ids": written_ids, "rejected": rejected}
        emit_trace_event("ingest_rows_rejected", {"count": len(rejected)}, state)
        return {"mode": "ingest", "ingest_result": result, "status": AgentStatus.SUCCESS.value}
