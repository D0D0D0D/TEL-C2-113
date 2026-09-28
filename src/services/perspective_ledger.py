"""Local KB/DB seams for accumulated non-diagnostic handover perspectives."""

from __future__ import annotations

import copy
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from shared.utils.audit_logger import emit_trace_event

from src.services.contracts import SCOPE_KEYS
from src.services.domain_utils import (
    contains_diagnostic_content,
    contains_instruction_content,
    mask_framework_pii,
    normalize_query,
)


class PerspectiveLedgerRejected(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class InMemoryPerspectiveLedger:
    """Append-only in-memory ledger used for tests and local STG.

    Production deployments replace this seam with scoped DB/KB connections.
    """

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []
        self._sequence = 0

    def write_perspective(
        self,
        *,
        scope: Mapping[str, str],
        perspective_kind: str,
        perspective_text: str,
        source_ref: str,
        provenance: Mapping[str, Any],
        state: dict | None = None,
    ) -> str:
        bound_scope = _scope(scope)
        text_values = _sanitize_persistent_text(perspective_text)
        self._sequence += 1
        record_id = f"perspective_{self._sequence:06d}"
        record = {
            "record_id": record_id,
            "record_kind": "handover_perspective",
            "scope": bound_scope,
            "counterpart_carrier_id": bound_scope["counterpart_carrier_id"],
            "perspective_kind": perspective_kind,
            "perspective_text_audit": text_values["audit"],
            "perspective_text_safe": text_values["safe"],
            "source_ref": source_ref,
            "retrieval_text": normalize_query(
                {
                    "perspective_kind": perspective_kind,
                    "source_ref": source_ref,
                }
            ),
            "provenance": dict(provenance),
            "recorded_period": _utc_now(),
            "status": "active",
        }
        self._records.append(record)
        emit_trace_event("perspective_written", {"record_id": record_id, "scope": bound_scope}, state or {})
        return record_id

    def append_feedback(
        self,
        *,
        scope: Mapping[str, str],
        source_record_id: str,
        decision: str,
        decided_by: str,
        decided_at: str,
        rationale_ref: str,
        revised_perspective_text: str | None = None,
        state: dict | None = None,
    ) -> dict[str, Any]:
        bound_scope = _scope(scope)
        text_values = _sanitize_persistent_text(revised_perspective_text) if revised_perspective_text is not None else None
        self._sequence += 1
        record_id = f"feedback_{self._sequence:06d}"
        record = {
            "record_id": record_id,
            "record_kind": "feedback_audit_event",
            "scope": bound_scope,
            "counterpart_carrier_id": bound_scope["counterpart_carrier_id"],
            "source_record_id": source_record_id,
            "decision": decision,
            "decided_by": decided_by,
            "decided_at": decided_at,
            "rationale_ref": rationale_ref,
            "perspective_text_audit": text_values["audit"] if text_values is not None else None,
            "perspective_text_safe": text_values["safe"] if text_values is not None else None,
            "sequence": self._sequence,
            "status": "active" if decision != "retire" else "retired",
        }
        self._records.append(record)
        emit_trace_event("feedback_recorded", {"record_id": record_id, "scope": bound_scope}, state or {})
        return {
            "record_id": record_id,
            "decision": decision,
            "recorded_at": decided_at,
            "sequence": self._sequence,
        }

    def search(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]:
        bound_scope = _scope(scope)
        normalized_query = normalize_query(query).casefold()
        hits: list[dict[str, Any]] = []
        for record in self._records:
            if record.get("record_kind") != "handover_perspective":
                continue
            if record.get("scope") != bound_scope or record.get("status") != "active":
                continue
            haystack = normalize_query(
                {
                    "perspective_kind": record.get("perspective_kind"),
                    "source_ref": record.get("source_ref"),
                    "text": record.get("perspective_text_safe"),
                }
            ).casefold()
            score = 1.0 if normalized_query and normalized_query in haystack else 0.1
            hits.append(
                {
                    "source_id": record["record_id"],
                    "summary": record["perspective_text_safe"],
                    "kind": "handover_perspective",
                    "score": score,
                    "metadata": {
                        "record_id": record["record_id"],
                        "source_id": record["source_ref"],
                        "provenance": str(record.get("provenance", {}).get("source", "approved")),
                        "recorded_period": record["recorded_period"],
                        "counterpart_carrier_id": bound_scope["counterpart_carrier_id"],
                    },
                }
            )
        hits.sort(key=lambda item: (-float(item["score"]), str(item["source_id"])))
        return copy.deepcopy(hits[:top_k])

    def audit_records(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._records)


def _scope(scope: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(scope, Mapping) or set(scope) != set(SCOPE_KEYS):
        raise ValueError("perspective_scope_missing")
    value = scope.get("counterpart_carrier_id")
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("perspective_scope_missing")
    return {"counterpart_carrier_id": value}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize_persistent_text(value: Any) -> dict[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise PerspectiveLedgerRejected("record_empty")
    normalized_input = unicodedata.normalize("NFKC", value)
    if contains_instruction_content(normalized_input):
        raise PerspectiveLedgerRejected("instruction_content")
    if contains_diagnostic_content(normalized_input):
        raise PerspectiveLedgerRejected("diagnostic_content")
    audit, _finding_count = mask_framework_pii(value)
    safe = unicodedata.normalize("NFKC", audit)
    return {"audit": audit, "safe": safe}


__all__ = ["InMemoryPerspectiveLedger", "PerspectiveLedgerRejected"]
