"""Domain contracts for TEL-C2-113.

Closed enums live here so implementation and tests share one source.
"""

from __future__ import annotations

from hashlib import sha256

AGENT_NAME = "InterconnectIncidentHandoverAgent"
TEMPLATE_ID = "TEL-C2-113"

APPROVED_TEMPLATE_REF = "HO-TMPL-INTERCONNECT"
APPROVED_TEMPLATE_VERSION = "3.2"
APPROVED_TEMPLATE_HASH = "sha256:" + sha256(
    f"{TEMPLATE_ID}:{APPROVED_TEMPLATE_REF}:{APPROVED_TEMPLATE_VERSION}".encode("utf-8")
).hexdigest()

SCOPE_KEYS = ("counterpart_carrier_id",)

RECORD_KINDS = frozenset({"incident_note", "event_reference", "partner_contact_log"})
SUPPORT_STATUSES = frozenset({"supported", "contradicted", "insufficient", "unknown"})
AFFIRMATIVE_SUPPORT_STATUSES = frozenset({"supported", "contradicted"})
UNRESOLVED_KINDS = frozenset(
    {
        "unsupported_assertion",
        "missing_check",
        "ambiguous_correlation",
        "missing_timestamp",
        "counterpart_input_pending",
    }
)
CONTRADICTION_KINDS = frozenset({"timeline_conflict", "scope_conflict", "check_result_conflict"})
QUESTION_TOPICS = frozenset(
    {
        "evidence_request",
        "timestamp_confirmation",
        "scope_confirmation",
        "check_result_confirmation",
        "cause_discrimination",
    }
)
ASK_TARGETS = frozenset({"counterpart_carrier", "internal_record"})
CAUSE_ORIGINS = frozenset({"recorded", "inferred"})
CANDIDATE_STATUSES = frozenset({"not_assessed"})
WHY_NEEDED_CODES = frozenset(
    {
        "corroboration_missing",
        "timeline_gap",
        "scope_undetermined",
        "conflicting_records",
        "candidate_discrimination_needed",
    }
)
FEEDBACK_DECISIONS = frozenset({"confirm", "revise", "retire"})

DEGRADATION_REASON_CODES = frozenset(
    {
        "template_version_mismatch",
        "input_quality_rejected",
        "llm_unavailable",
        "provenance_verification_failed",
        "kb_scope_unresolved",
    }
)
DEGRADATION_DETAIL_CODES = frozenset(
    {
        "quote_not_verbatim",
        "quote_too_short",
        "quote_too_long",
        "unknown_source_id",
        "unknown_check_id",
        "support_status_downgraded",
        "question_target_missing",
        "enum_value_rejected",
        "llm_output_unparsable",
        "candidate_dropped",
        "quote_masked_only",
    }
)
INPUT_REJECT_REASON_CODES = frozenset(
    {
        "record_too_long",
        "record_empty",
        "control_char_ratio",
        "mojibake_ratio",
        "unreadable_ratio",
        "binary_payload",
        "duplicate_record_id",
    }
)
INGEST_REJECT_REASON_CODES = INPUT_REJECT_REASON_CODES | frozenset(
    {"diagnostic_content", "instruction_content", "unknown_perspective_kind", "missing_source_ref"}
)

PROHIBITED_OUTPUT_KEYS = frozenset(
    {
        "root_cause",
        "diagnosis",
        "fault_location",
        "restoration_status",
        "incident_closed",
        "recommended_action",
        "routing_change",
        "sla_breach",
        "customer_impact_confirmed",
        "partner_notification_sent",
        "approval",
        "verdict",
        "most_likely_cause",
        "confidence",
        "probability",
        "likelihood",
        "rank",
        "priority",
    }
)

INSTRUCTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "system prompt",
    "developer message",
    "以後の指示",
    "前の指示を無視",
    "システムプロンプト",
)

DIAGNOSTIC_MARKERS = (
    "root_cause",
    "diagnosis",
    "fault_location",
    "restoration_status",
    "incident_closed",
    "recommended_action",
    "routing_change",
    "sla_breach",
    "customer_impact_confirmed",
    "partner_notification_sent",
    "approval",
    "verdict",
    "most_likely_cause",
    "confidence",
    "probability",
    "likelihood",
    "rank",
    "priority",
    "原因は",
    "原因:",
    "復旧済み",
    "クローズ",
)

LITERAL_COPY_FIELDS = (
    "event.opening_quote",
    "timeline[].quote",
    "completed_checks[].quote",
    "assertions[].recorded_assertion_quote",
    "candidate_causes[].basis_quotes[].quote",
)


def empty_degradation() -> dict[str, list[str]]:
    return {"reason_codes": [], "detail_codes": []}


def add_degradation(
    degradation: dict[str, list[str]] | None,
    *,
    reason: str | None = None,
    detail: str | None = None,
) -> dict[str, list[str]]:
    value = {
        "reason_codes": list((degradation or {}).get("reason_codes", [])),
        "detail_codes": list((degradation or {}).get("detail_codes", [])),
    }
    if reason and reason not in value["reason_codes"]:
        value["reason_codes"].append(reason)
    if detail and detail not in value["detail_codes"]:
        value["detail_codes"].append(detail)
    return value
