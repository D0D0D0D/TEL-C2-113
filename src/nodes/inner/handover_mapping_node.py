"""Stage 3: LLM handover mapping."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.llm_draft_stage import DraftStageHooks, LlmDraftStageNode
from src.nodes.inner.common import resolve_json_payload
from src.services import contracts
from src.services.domain_utils import first_sentence, substantial_quote_text


class HandoverMappingNode(LlmDraftStageNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        self._domain_config = dict(config or {})
        super().__init__(
            config=self._domain_config,
            hooks=DraftStageHooks(
                build_prompt=self._build_prompt,
                build_fallback=self._build_fallback,
                build_evidence=self._build_evidence,
            ),
        )

    def execute(self, state: dict) -> dict:
        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        draft = _normalize_draft(result.get("draft") or {})
        degradation = state.get("degradation") or {"reason_codes": [], "detail_codes": []}
        if result.get("degradation_code") == "llm_not_configured":
            degradation = contracts.add_degradation(degradation, reason="llm_unavailable")
        emit_trace_event(
            "handover_draft_generated",
            {"assertion_count": len(draft.get("assertions", [])), "candidate_count": len(draft.get("candidate_causes", []))},
            state,
        )
        return {
            "draft_candidate": draft,
            "cited_source_ids": result.get("cited_source_ids", []),
            "degradation": degradation,
            "status": AgentStatus.SUCCESS.value,
        }

    def _build_prompt(self, state: dict[str, Any]) -> str:
        records = []
        try:
            scope = state.get("request_scope") if isinstance(state.get("request_scope"), dict) else {}
            payload = resolve_json_payload(self._domain_config, state, state.get("canonical_payload_ref"), scope)
            records = payload.get("records", []) if isinstance(payload.get("records"), list) else []
        except Exception:  # noqa: BLE001
            records = []
        source_registry = state.get("source_registry", [])
        example = _build_live_valid_example(records, source_registry, self._domain_config)
        prompt = {
            "task": "Map interconnect incident notes into the approved handover judgment layer. Use only existing source_ids.",
            "return_json_only": True,
            "top_level_keys": [
                "assertions",
                "unresolved",
                "contradictions",
                "candidate_causes",
                "confirmation_questions",
            ],
            "output_schema": example,
            "enums": {
                "support_status": sorted(contracts.SUPPORT_STATUSES),
                "unresolved_kind": sorted(contracts.UNRESOLVED_KINDS),
                "contradiction_kind": sorted(contracts.CONTRADICTION_KINDS),
                "question_topic": sorted(contracts.QUESTION_TOPICS),
                "ask_target": sorted(contracts.ASK_TARGETS),
                "cause_origin": sorted(contracts.CAUSE_ORIGINS),
                "candidate_status": sorted(contracts.CANDIDATE_STATUSES),
                "why_needed_code": sorted(contracts.WHY_NEEDED_CODES),
            },
            "constraints": [
                "Do not output root_cause, diagnosis, confidence, probability, likelihood, rank, priority, or recommended_action.",
                "Every quote must be a literal substring of canonical_text for the cited source.",
                "Each assertion_source_ids, source_ids, evidence_source_ids, and basis_source_ids value MUST exist in source_registry.",
                "For every confirmation_questions item, set exactly one of resolves_unresolved_id or resolves_candidate_id to an existing id; set the other field to null.",
                "If a question cannot be linked to an existing unresolved item or candidate, omit that question.",
                "If resolves_candidate_id is not null, question_topic MUST be cause_discrimination and why_needed_code MUST be candidate_discrimination_needed.",
                "candidate_causes are not assessed; status MUST be not_assessed, basis_source_ids and basis_quotes MUST be non-empty, and resolves_by_question_id MUST point to a confirmation question whose resolves_candidate_id points back to the candidate_id.",
                "List possible causes readable from the records, with evidence. Use origin=recorded for explicit recorded cause statements and origin=inferred for possibilities derived from observations. Return an empty candidate_causes array only when no candidate is genuinely visible.",
                "When records contain observable incident statements or pending confirmations, draft at least one assertion and one linked confirmation question.",
            ],
            "source_registry": source_registry,
            "completed_checks": state.get("completed_checks", []),
            "recalled_perspectives": state.get("recalled_perspectives", []),
            "canonical_records_for_quote_selection": [
                {
                    "source_id": _source_id_for_record(record, source_registry),
                    "record_id": record.get("record_id"),
                    "record_kind": record.get("record_kind"),
                    "recorded_at": record.get("recorded_at"),
                    "canonical_text": record.get("canonical_text"),
                }
                for record in records
            ],
            "one_valid_output_example": example,
        }
        return json.dumps(prompt, ensure_ascii=False, default=str)

    def _build_fallback(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "assertions": [],
            "unresolved": [],
            "contradictions": [],
            "candidate_causes": [],
            "confirmation_questions": [],
        }

    def _build_evidence(self, _state: dict[str, Any], draft: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        draft = _normalize_draft(draft)
        cited: set[str] = set()
        evidence = []
        for assertion in draft.get("assertions", []):
            source_ids = [str(item) for item in assertion.get("assertion_source_ids", []) if item]
            cited.update(source_ids)
            evidence.append({"claim_id": assertion.get("assertion_id", ""), "source_ids": source_ids})
        for item in draft.get("unresolved", []) + draft.get("contradictions", []):
            cited.update(str(source_id) for source_id in item.get("source_ids", []) if source_id)
        for candidate in draft.get("candidate_causes", []):
            cited.update(str(source_id) for source_id in candidate.get("basis_source_ids", []) if source_id)
        return evidence, sorted(cited)


def _normalize_draft(value: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value.get("handover_draft"), dict):
        value = value["handover_draft"]
    return {
        "assertions": list(value.get("assertions", [])) if isinstance(value.get("assertions", []), list) else [],
        "unresolved": list(value.get("unresolved", [])) if isinstance(value.get("unresolved", []), list) else [],
        "contradictions": list(value.get("contradictions", [])) if isinstance(value.get("contradictions", []), list) else [],
        "candidate_causes": list(value.get("candidate_causes", [])) if isinstance(value.get("candidate_causes", []), list) else [],
        "confirmation_questions": list(value.get("confirmation_questions", []))
        if isinstance(value.get("confirmation_questions", []), list)
        else [],
    }


def _source_id_for_record(record: dict[str, Any], source_registry: list[dict[str, Any]]) -> str:
    for source in source_registry:
        if source.get("record_id") == record.get("record_id"):
            return str(source.get("source_id", ""))
    return ""


def _build_live_valid_example(
    records: list[dict[str, Any]], source_registry: list[dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    source_id = _source_id_for_record(records[0], source_registry) if records else "S-001"
    quote = _example_quote(records[0], config) if records else "canonical text literal quote"
    return {
        "assertions": [
            {
                "assertion_id": "A-01",
                "recorded_assertion_quote": quote,
                "assertion_source_ids": [source_id],
                "support_status": "insufficient",
                "corroborating_check_ids": [],
                "evidence_source_ids": [],
            }
        ],
        "unresolved": [
            {"unresolved_id": "U-01", "unresolved_kind": "unsupported_assertion", "source_ids": [source_id]}
        ],
        "contradictions": [],
        "candidate_causes": [
            {
                "candidate_id": "H-01",
                "hypothesis_text": "記録された事象に関連する接続状態を確認する候補です。",
                "origin": "inferred",
                "basis_source_ids": [source_id],
                "basis_quotes": [{"source_id": source_id, "quote": quote}],
                "discriminating_check_id": None,
                "resolves_by_question_id": "Q-01",
                "status": "not_assessed",
            }
        ],
        "confirmation_questions": [
            {
                "question_id": "Q-01",
                "question_topic": "cause_discrimination",
                "ask_target": "counterpart_carrier",
                "question_text": "該当時間帯の接続状態の変化有無を確認してください。",
                "resolves_unresolved_id": None,
                "resolves_candidate_id": "H-01",
                "why_needed_code": "candidate_discrimination_needed",
                "draft_only": True,
            }
        ],
    }


def _example_quote(record: dict[str, Any], config: dict[str, Any]) -> str:
    text = str(record.get("canonical_text", ""))
    max_chars = int(config.get("max_quote_chars", 300))
    min_chars = int(config.get("min_quote_chars", 8))
    quote = first_sentence(text, max_chars=max_chars)
    if substantial_quote_text(quote, min_quote_chars=min_chars):
        return quote
    return text[:max(min(len(text), max_chars), min_chars)]
