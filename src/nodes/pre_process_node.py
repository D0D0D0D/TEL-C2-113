"""Pre-process node for TEL-C2-113."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.contracts import (
    APPROVED_TEMPLATE_HASH,
    APPROVED_TEMPLATE_REF,
    APPROVED_TEMPLATE_VERSION,
    FEEDBACK_DECISIONS,
    RECORD_KINDS,
    UNRESOLVED_KINDS,
    add_degradation,
    empty_degradation,
)
from src.services.domain_utils import (
    HASH_RE,
    INCIDENT_REF_RE,
    RECORD_ID_RE,
    RequestValidationError,
    closed_keys,
    ensure_clean_scope,
    mask_framework_pii,
    parse_aware_datetime,
    parse_json_object,
    quality_reasons,
)


class PreProcessNode(FunctionNode):
    """Validate entry input and keep free text out of State."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("input_rejected", {"error_code": "upstream_error"}, state)
            return {}
        try:
            payload = parse_json_object(state.get("user_input", "{}"))
            mode = str(state.get("input_context", {}).get("mode") or "")
            scope = ensure_clean_scope(state.get("input_context", {}).get("request_scope"))
            if mode not in {"invoke", "ingest", "feedback"}:
                raise RequestValidationError("invalid_mode", detail="mode must be supplied by the entry adapter")
            if mode == "invoke":
                result = self._process_invoke(payload, state, scope)
            elif mode == "ingest":
                result = self._process_ingest(payload, state, scope)
            else:
                result = self._process_feedback(payload, state, scope)
        except RequestValidationError as exc:
            emit_trace_event("input_rejected", {"error_code": exc.error_code, "keys": exc.keys}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_code": exc.error_code,
                "error_log": [f"PreProcessNode: {exc.error_code}"],
            }
        except Exception as exc:  # noqa: BLE001
            emit_trace_event("input_rejected", {"error_code": type(exc).__name__}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_code": "preprocess_failed",
                "error_log": [f"PreProcessNode: preprocess_failed ({type(exc).__name__})"],
            }
        emit_trace_event(
            "records_canonicalized",
            {
                "mode": result["mode"],
                "accepted_count": result.get("input_quality", {}).get("accepted_count", 0),
                "rejected_count": len(result.get("input_quality", {}).get("rejected", [])),
            },
            state,
        )
        rejected = result.get("input_quality", {}).get("rejected", [])
        if rejected:
            emit_trace_event("input_quality_rejected", {"mode": result["mode"], "count": len(rejected)}, state)
        if result.get("template_check", {}).get("ok") is False:
            emit_trace_event("template_version_mismatch", {"incident_ref": result.get("incident_ref", "")}, state)
        return result

    def _process_invoke(self, payload: dict[str, Any], state: dict, scope: dict[str, str]) -> dict[str, Any]:
        closed_keys(payload, {"incident_ref", "handover_template", "records", "context"}, reserved=_reserved_keys())
        incident_ref = payload.get("incident_ref")
        if not isinstance(incident_ref, str) or not INCIDENT_REF_RE.fullmatch(incident_ref):
            raise RequestValidationError("invalid_incident_ref")
        context = _context(payload.get("context"), self._config)
        template_check = _template_check(payload.get("handover_template"))

        records = payload.get("records")
        if not isinstance(records, list) or not 1 <= len(records) <= int(self._config.get("max_records", 200)):
            raise RequestValidationError("invalid_records")

        accepted, rejected, mask_count = self._canonicalize_records(records)
        ref = self._put_payload(
            {"records": accepted},
            scope=scope,
            session_id=state.get("session_id", ""),
        )
        input_quality = {"ok": not rejected and bool(accepted), "accepted_count": len(accepted), "rejected": rejected}
        degradation = empty_degradation()
        if rejected:
            degradation = add_degradation(degradation, reason="input_quality_rejected")
        if not template_check["ok"]:
            degradation = add_degradation(degradation, reason="template_version_mismatch")
        return {
            "mode": "invoke",
            "incident_ref": incident_ref,
            "request_scope": scope,
            "counterpart_carrier_id": scope["counterpart_carrier_id"],
            "canonical_payload_ref": ref,
            "validated_input": {
                "incident_ref": incident_ref,
                "handover_template": {
                    "ref": str(payload["handover_template"].get("ref", "")),
                    "version": str(payload["handover_template"].get("version", "")),
                },
                "record_count": len(records),
                "accepted_record_ids": [record["record_id"] for record in accepted],
                "record_kind_counts": _record_kind_counts(accepted),
                "business_timezone": context["business_timezone"],
                "counterpart_label": context["counterpart_label"],
                "pii_masked_finding_count": mask_count,
            },
            "template_check": template_check,
            "input_quality": input_quality,
            "degradation": degradation,
            "status": AgentStatus.SUCCESS.value,
        }

    def _process_ingest(self, payload: dict[str, Any], state: dict, scope: dict[str, str]) -> dict[str, Any]:
        closed_keys(payload, {"perspectives", "context"}, reserved=_reserved_keys())
        _context(payload.get("context"), self._config)
        rows = payload.get("perspectives")
        if not isinstance(rows, list) or not 1 <= len(rows) <= int(self._config.get("max_records", 200)):
            raise RequestValidationError("invalid_perspectives")
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        seen: set[str] = set()
        mask_count = 0
        for row in rows:
            if not isinstance(row, dict):
                rejected.append({"record_id": "", "reason_code": "record_empty"})
                continue
            closed_keys(row, {"record_id", "perspective_kind", "perspective_text", "source_ref"}, reserved=_reserved_keys())
            record_id = row.get("record_id")
            if not isinstance(record_id, str) or not RECORD_ID_RE.fullmatch(record_id) or record_id in seen:
                rejected.append({"record_id": str(record_id or ""), "reason_code": "duplicate_record_id"})
                continue
            seen.add(record_id)
            kind = row.get("perspective_kind")
            if kind not in UNRESOLVED_KINDS:
                rejected.append({"record_id": record_id, "reason_code": "unknown_perspective_kind"})
                continue
            source_ref = row.get("source_ref")
            if not isinstance(source_ref, str) or not source_ref.strip():
                rejected.append({"record_id": record_id, "reason_code": "missing_source_ref"})
                continue
            reasons = quality_reasons(row.get("perspective_text"), max_record_chars=int(self._config.get("max_record_chars", 8000)))
            if reasons:
                rejected.append({"record_id": record_id, "reason_code": reasons[0]})
                continue
            masked, count = mask_framework_pii(row["perspective_text"])
            mask_count += count
            accepted.append(
                {
                    "record_id": record_id,
                    "perspective_kind": kind,
                    "perspective_text": masked,
                    "source_ref": source_ref,
                }
            )
        ref = self._put_payload({"perspectives": accepted}, scope=scope, session_id=state.get("session_id", ""))
        return {
            "mode": "ingest",
            "request_scope": scope,
            "counterpart_carrier_id": scope["counterpart_carrier_id"],
            "canonical_payload_ref": ref,
            "validated_input": {
                "perspective_count": len(rows),
                "accepted_record_ids": [row["record_id"] for row in accepted],
                "pii_masked_finding_count": mask_count,
            },
            "input_quality": {"ok": not rejected and bool(accepted), "accepted_count": len(accepted), "rejected": rejected},
            "degradation": add_degradation(empty_degradation(), reason="input_quality_rejected") if rejected else empty_degradation(),
            "status": AgentStatus.SUCCESS.value,
        }

    def _process_feedback(self, payload: dict[str, Any], state: dict, scope: dict[str, str]) -> dict[str, Any]:
        closed_keys(
            payload,
            {"source_record_id", "decision", "revised_perspective_text", "rationale_ref", "context"},
            reserved=_reserved_keys(),
        )
        _context(payload.get("context"), self._config)
        source_record_id = payload.get("source_record_id")
        decision = payload.get("decision")
        rationale_ref = payload.get("rationale_ref")
        if not isinstance(source_record_id, str) or not source_record_id.strip():
            raise RequestValidationError("invalid_source_record_id")
        if decision not in FEEDBACK_DECISIONS:
            raise RequestValidationError("invalid_feedback_decision")
        if not isinstance(rationale_ref, str) or not rationale_ref.strip():
            raise RequestValidationError("invalid_rationale_ref")
        if decision == "revise" and not isinstance(payload.get("revised_perspective_text"), str):
            raise RequestValidationError("revised_perspective_text_required")
        if decision != "revise" and "revised_perspective_text" in payload:
            raise RequestValidationError("revised_perspective_text_forbidden")
        ref = ""
        mask_count = 0
        if decision == "revise":
            reasons = quality_reasons(
                payload.get("revised_perspective_text"),
                max_record_chars=int(self._config.get("max_record_chars", 8000)),
            )
            if reasons:
                raise RequestValidationError(reasons[0])
            masked, mask_count = mask_framework_pii(payload["revised_perspective_text"])
            ref = self._put_payload(
                {"feedback": {"revised_perspective_text": masked}},
                scope=scope,
                session_id=state.get("session_id", ""),
            )
        decided_at = str(state.get("input_context", {}).get("decided_at") or "1970-01-01T00:00:00+00:00")
        parse_aware_datetime(decided_at)
        return {
            "mode": "feedback",
            "request_scope": scope,
            "counterpart_carrier_id": scope["counterpart_carrier_id"],
            "canonical_payload_ref": ref,
            "validated_input": {
                "source_record_id": source_record_id,
                "decision": decision,
                "rationale_ref": rationale_ref,
                "decided_by": str(state.get("input_context", {}).get("caller_id") or state.get("caller_id") or ""),
                "decided_at": decided_at,
                "pii_masked_finding_count": mask_count,
            },
            "input_quality": {"ok": True, "accepted_count": 1, "rejected": []},
            "degradation": empty_degradation(),
            "status": AgentStatus.SUCCESS.value,
        }

    def _canonicalize_records(self, rows: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        seen: set[str] = set()
        mask_count = 0
        for row in rows:
            if not isinstance(row, dict):
                rejected.append({"record_id": "", "reason_code": "record_empty"})
                continue
            closed_keys(row, {"record_id", "record_kind", "recorded_at", "text"}, reserved=_reserved_keys())
            record_id = row.get("record_id")
            if not isinstance(record_id, str) or not RECORD_ID_RE.fullmatch(record_id) or record_id in seen:
                rejected.append({"record_id": str(record_id or ""), "reason_code": "duplicate_record_id"})
                continue
            seen.add(record_id)
            if row.get("record_kind") not in RECORD_KINDS:
                raise RequestValidationError("invalid_record_kind")
            try:
                parse_aware_datetime(row.get("recorded_at", ""))
            except Exception as exc:  # noqa: BLE001
                raise RequestValidationError("invalid_recorded_at") from exc
            reasons = quality_reasons(row.get("text"), max_record_chars=int(self._config.get("max_record_chars", 8000)))
            if reasons:
                rejected.append({"record_id": record_id, "reason_code": reasons[0]})
                continue
            canonical_text, count = mask_framework_pii(row["text"])
            mask_count += count
            accepted.append(
                {
                    "record_id": record_id,
                    "record_kind": row["record_kind"],
                    "recorded_at": row["recorded_at"],
                    "canonical_text": canonical_text,
                }
            )
        return accepted, rejected, mask_count

    def _put_payload(self, value: dict[str, Any], *, scope: dict[str, str], session_id: str) -> str:
        store = self._config.get("payload_store")
        if store is None:
            raise RuntimeError("payload_store_not_configured")
        return store.put(json.dumps(value, ensure_ascii=False, default=str), scope=scope, session_id=session_id)

    def _extra_security_gate_input(self, state: dict) -> dict:
        validated = state.get("validated_input")
        if isinstance(validated, dict) and _contains_free_text_key(validated):
            state = dict(state)
            state["status"] = AgentStatus.ERROR.value
            state.setdefault("error_log", []).append("PreProcessNode: free text key present in validated_input")
        return state


def _reserved_keys() -> set[str]:
    return {"payload_ref", "canonical_payload_ref"}


def _context(value: Any, config: dict[str, Any]) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise RequestValidationError("invalid_context")
    closed_keys(value, {"business_timezone", "counterpart_label"})
    business_timezone = value.get("business_timezone", config.get("default_business_timezone", "Asia/Tokyo"))
    counterpart_label = value.get("counterpart_label", config.get("default_counterpart_label", "unspecified"))
    if not isinstance(business_timezone, str) or not business_timezone.strip():
        raise RequestValidationError("invalid_business_timezone")
    if not isinstance(counterpart_label, str) or "\n" in counterpart_label or "\r" in counterpart_label:
        raise RequestValidationError("invalid_counterpart_label")
    return {"business_timezone": business_timezone, "counterpart_label": counterpart_label}


def _template_check(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RequestValidationError("handover_template_required")
    closed_keys(value, {"ref", "version", "content_hash"})
    ref = value.get("ref")
    version = value.get("version")
    content_hash = value.get("content_hash")
    if not all(isinstance(item, str) and item for item in (ref, version, content_hash)):
        raise RequestValidationError("invalid_handover_template")
    if not HASH_RE.fullmatch(content_hash):
        raise RequestValidationError("invalid_handover_template_hash")
    ok = ref == APPROVED_TEMPLATE_REF and version == APPROVED_TEMPLATE_VERSION and content_hash == APPROVED_TEMPLATE_HASH
    mismatches = []
    if ref != APPROVED_TEMPLATE_REF:
        mismatches.append("ref")
    if version != APPROVED_TEMPLATE_VERSION:
        mismatches.append("version")
    if content_hash != APPROVED_TEMPLATE_HASH:
        mismatches.append("content_hash")
    return {
        "ok": ok,
        "template_ref": ref,
        "template_version": version,
        "content_hash_match": content_hash == APPROVED_TEMPLATE_HASH,
        "mismatches": mismatches,
    }


def _record_kind_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {kind: 0 for kind in sorted(RECORD_KINDS)}
    for record in records:
        counts[record["record_kind"]] += 1
    return counts


def _contains_free_text_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"text", "canonical_text", "perspective_text", "revised_perspective_text"}:
                return True
            if _contains_free_text_key(item):
                return True
    if isinstance(value, list):
        return any(_contains_free_text_key(item) for item in value)
    return False
