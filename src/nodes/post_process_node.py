"""Post-process envelope and S-3 domain output gate."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security import detect_pii
from shared.utils.audit_logger import emit_trace_event

from src.output_envelope import OutputEnvelopeNode
from src.services import contracts
from src.services.domain_utils import reject_prohibited_keys, walk_string_leaves
from src.services.wiring import load_runtime_config


class PostProcessNode(OutputEnvelopeNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    gate_kind_by_mode: ClassVar[dict[str, str]] = {
        "invoke": "handover_draft",
        "ingest": "ingest_summary",
        "feedback": "feedback_summary",
    }
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": (
            "mode",
            "gate_kind",
            "incident_ref",
            "template_ref",
            "template_version",
            "requires_human_review",
            "degradation",
        ),
        "ingest": ("mode", "gate_kind", "ingest_result"),
        "feedback": ("mode", "gate_kind", "feedback_result"),
    }
    max_field_chars: ClassVar[int | None] = None

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = load_runtime_config(config)

    def execute(self, state: dict) -> dict:
        result = super().execute(state)
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("post_process_error_formatted", {"mode": state.get("mode", "")}, state)
        return result

    def format_invoke(self, state: dict) -> dict[str, Any]:
        template = state.get("template_check", {}) if isinstance(state.get("template_check"), dict) else {}
        validated = state.get("validated_input", {}) if isinstance(state.get("validated_input"), dict) else {}
        return {
            "incident_ref": state.get("incident_ref", validated.get("incident_ref", "")),
            "template_ref": template.get("template_ref", ""),
            "template_version": template.get("template_version", ""),
            "handover_draft": state.get("handover_draft", {}),
            "evidence_map": state.get("evidence_map", []),
            "requires_human_review": bool(state.get("requires_human_review", False)),
            "degradation": state.get("degradation", contracts.empty_degradation()),
        }

    def format_ingest(self, state: dict) -> dict[str, Any]:
        return {"ingest_result": state.get("ingest_result", {"written_ids": [], "rejected": []})}

    def format_feedback(self, state: dict) -> dict[str, Any]:
        return {"feedback_result": state.get("feedback_result", {})}

    def _extra_security_gate_output(self, result: dict) -> dict:
        gated = super()._extra_security_gate_output(result)
        formatted = gated.get("formatted_output", {})
        if formatted.get("gate_kind") == "error":
            formatted["error_log"] = _sanitize_error_log(formatted.get("error_log", []))
            for path, value in walk_string_leaves(formatted):
                if detect_pii(value):
                    raise SecurityViolationError(f"S-3 gate: PII remains at {path}")
            gated["formatted_output"] = formatted
            return gated
        try:
            self._validate_domain_output(formatted)
        except SecurityViolationError:
            emit_trace_event("s3_output_rejected", {"mode": formatted.get("mode", "")}, formatted)
            raise
        return gated

    def _validate_domain_output(self, formatted: dict[str, Any]) -> None:
        violations = reject_prohibited_keys(formatted)
        if violations:
            raise SecurityViolationError(f"S-3 gate: prohibited output key(s): {violations}")
        unknown = _unknown_output_keys(formatted)
        if unknown:
            raise SecurityViolationError(f"S-3 gate: unknown output key(s): {unknown}")
        mode = formatted.get("mode")
        if mode == "invoke":
            self._validate_invoke(formatted)
        elif mode == "ingest":
            self._validate_ingest(formatted)
        elif mode == "feedback":
            self._validate_feedback(formatted)
        else:
            raise SecurityViolationError("S-3 gate: unknown mode")
        for path, value in walk_string_leaves(formatted):
            if detect_pii(value):
                raise SecurityViolationError(f"S-3 gate: PII remains at {path}")

    def _validate_invoke(self, formatted: dict[str, Any]) -> None:
        degradation = formatted.get("degradation")
        _validate_degradation(degradation)
        draft = formatted.get("handover_draft")
        if not isinstance(draft, dict):
            raise SecurityViolationError("S-3 gate: handover_draft must be object")
        if draft.get("candidate_causes_disclaimer") != self._config.get("candidate_causes_disclaimer", ""):
            raise SecurityViolationError("S-3 gate: candidate disclaimer mismatch")
        sources = {item.get("source_id") for item in draft.get("sources", []) if isinstance(item, dict)}
        checks = {item.get("check_id") for item in draft.get("completed_checks", []) if isinstance(item, dict)}
        unresolved = {item.get("unresolved_id") for item in draft.get("unresolved", []) if isinstance(item, dict)}
        candidates = draft.get("candidate_causes", [])
        questions = draft.get("confirmation_questions", [])
        if not isinstance(candidates, list) or len(candidates) > int(self._config.get("max_candidate_causes", 5)):
            raise SecurityViolationError("S-3 gate: candidate count invalid")
        question_ids = {item.get("question_id") for item in questions if isinstance(item, dict)}
        candidate_ids: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise SecurityViolationError("S-3 gate: candidate must be object")
            if candidate.get("status") != "not_assessed":
                raise SecurityViolationError("S-3 gate: candidate status invalid")
            if candidate.get("candidate_id") in candidate_ids:
                raise SecurityViolationError("S-3 gate: duplicate candidate_id")
            candidate_ids.add(candidate.get("candidate_id"))
            if not candidate.get("basis_source_ids") or not candidate.get("basis_quotes"):
                raise SecurityViolationError("S-3 gate: candidate basis missing")
            if not set(candidate["basis_source_ids"]) <= sources:
                raise SecurityViolationError("S-3 gate: candidate basis source unknown")
            if {item.get("source_id") for item in candidate["basis_quotes"]} != set(candidate["basis_source_ids"]):
                raise SecurityViolationError("S-3 gate: candidate quote source set mismatch")
            if candidate.get("resolves_by_question_id") not in question_ids:
                raise SecurityViolationError("S-3 gate: candidate question missing")
            check_id = candidate.get("discriminating_check_id")
            if check_id is not None and check_id not in checks:
                raise SecurityViolationError("S-3 gate: candidate check unknown")
            if len(str(candidate.get("hypothesis_text", ""))) > int(self._config.get("max_hypothesis_chars", 160)):
                raise SecurityViolationError("S-3 gate: hypothesis_text too long")
        for question in questions:
            if question.get("draft_only") is not True:
                raise SecurityViolationError("S-3 gate: draft_only must be true")
            if len(str(question.get("question_text", ""))) > int(self._config.get("max_question_chars", 120)):
                raise SecurityViolationError("S-3 gate: question_text too long")
            if "\n" in str(question.get("question_text", "")):
                raise SecurityViolationError("S-3 gate: question_text newline")
            has_unresolved = question.get("resolves_unresolved_id") is not None
            has_candidate = question.get("resolves_candidate_id") is not None
            if has_unresolved == has_candidate:
                raise SecurityViolationError("S-3 gate: question target xor invalid")
            if has_unresolved and question.get("resolves_unresolved_id") not in unresolved:
                raise SecurityViolationError("S-3 gate: unresolved target unknown")
            if has_candidate:
                if question.get("resolves_candidate_id") not in candidate_ids:
                    raise SecurityViolationError("S-3 gate: candidate target unknown")
                if (
                    question.get("question_topic") != "cause_discrimination"
                    or question.get("why_needed_code") != "candidate_discrimination_needed"
                ):
                    raise SecurityViolationError("S-3 gate: candidate question code invalid")
        for path, value in walk_string_leaves(draft):
            if path.endswith("question_text") or path.endswith("hypothesis_text"):
                continue
            if ".quote" in path and len(value) > int(self._config.get("max_quote_chars", 300)):
                raise SecurityViolationError("S-3 gate: quote too long")
        _validate_enums_in_draft(draft)

    def _validate_ingest(self, formatted: dict[str, Any]) -> None:
        result = formatted.get("ingest_result")
        if not isinstance(result, dict) or not isinstance(result.get("written_ids", []), list):
            raise SecurityViolationError("S-3 gate: ingest_result shape invalid")
        for item in result.get("rejected", []):
            if not isinstance(item, dict) or item.get("reason_code") not in contracts.INGEST_REJECT_REASON_CODES:
                raise SecurityViolationError("S-3 gate: ingest reason_code invalid")

    def _validate_feedback(self, formatted: dict[str, Any]) -> None:
        result = formatted.get("feedback_result")
        if not isinstance(result, dict) or result.get("decision") not in contracts.FEEDBACK_DECISIONS:
            raise SecurityViolationError("S-3 gate: feedback_result invalid")


def _unknown_output_keys(value: Any, path: str = "") -> list[str]:
    allowed = {
        "mode",
        "gate_kind",
        "status",
        "error_log",
        "incident_ref",
        "template_ref",
        "template_version",
        "handover_draft",
        "sources",
        "source_id",
        "record_id",
        "record_kind",
        "recorded_at",
        "char_offset_range",
        "timeline",
        "at",
        "quote",
        "source_ids",
        "completed_checks",
        "check_id",
        "observed_identifiers",
        "identifier",
        "occurrence_count",
        "counterpart_label",
        "event",
        "opening_quote",
        "assertions",
        "assertion_id",
        "recorded_assertion_quote",
        "assertion_source_ids",
        "support_status",
        "corroborating_check_ids",
        "evidence_source_ids",
        "unresolved",
        "unresolved_id",
        "unresolved_kind",
        "contradictions",
        "contradiction_id",
        "contradiction_kind",
        "candidate_causes",
        "candidate_id",
        "hypothesis_text",
        "origin",
        "basis_source_ids",
        "basis_quotes",
        "discriminating_check_id",
        "resolves_by_question_id",
        "candidate_causes_disclaimer",
        "confirmation_questions",
        "question_id",
        "question_topic",
        "ask_target",
        "question_text",
        "resolves_unresolved_id",
        "resolves_candidate_id",
        "why_needed_code",
        "draft_only",
        "assessment_complete",
        "evidence_map",
        "claim_id",
        "requires_human_review",
        "degradation",
        "reason_codes",
        "detail_codes",
        "ingest_result",
        "written_ids",
        "rejected",
        "reason_code",
        "feedback_result",
        "decision",
        "recorded_at",
        "sequence",
    }
    violations: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_str = str(key)
            current = f"{path}.{key_str}" if path else key_str
            if key_str not in allowed:
                violations.append(current)
            violations.extend(_unknown_output_keys(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            violations.extend(_unknown_output_keys(item, f"{path}[{index}]"))
    return violations


def _validate_degradation(value: Any) -> None:
    if not isinstance(value, dict):
        raise SecurityViolationError("S-3 gate: degradation must be object")
    if set(value) != {"reason_codes", "detail_codes"}:
        raise SecurityViolationError("S-3 gate: degradation keys invalid")
    if not set(value.get("reason_codes", [])) <= contracts.DEGRADATION_REASON_CODES:
        raise SecurityViolationError("S-3 gate: degradation reason invalid")
    if not set(value.get("detail_codes", [])) <= contracts.DEGRADATION_DETAIL_CODES:
        raise SecurityViolationError("S-3 gate: degradation detail invalid")


def _validate_enums_in_draft(draft: dict[str, Any]) -> None:
    for assertion in draft.get("assertions", []):
        if assertion.get("support_status") not in contracts.SUPPORT_STATUSES:
            raise SecurityViolationError("S-3 gate: support_status invalid")
    for item in draft.get("unresolved", []):
        if item.get("unresolved_kind") not in contracts.UNRESOLVED_KINDS:
            raise SecurityViolationError("S-3 gate: unresolved_kind invalid")
    for item in draft.get("contradictions", []):
        if item.get("contradiction_kind") not in contracts.CONTRADICTION_KINDS:
            raise SecurityViolationError("S-3 gate: contradiction_kind invalid")
    for item in draft.get("candidate_causes", []):
        if item.get("origin") not in contracts.CAUSE_ORIGINS or item.get("status") not in contracts.CANDIDATE_STATUSES:
            raise SecurityViolationError("S-3 gate: candidate enum invalid")
    for item in draft.get("confirmation_questions", []):
        if item.get("question_topic") not in contracts.QUESTION_TOPICS:
            raise SecurityViolationError("S-3 gate: question_topic invalid")
        if item.get("ask_target") not in contracts.ASK_TARGETS:
            raise SecurityViolationError("S-3 gate: ask_target invalid")
        if item.get("why_needed_code") not in contracts.WHY_NEEDED_CODES:
            raise SecurityViolationError("S-3 gate: why_needed_code invalid")


def _sanitize_error_log(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    sanitized: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str):
            continue
        cleaned = entry.split("\nTraceback", 1)[0].strip()
        if detect_pii(cleaned) or "/Users/" in cleaned or "/private/" in cleaned or 'File "' in cleaned:
            cleaned = "error_log_redacted"
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            sanitized.append(cleaned)
    return sanitized
