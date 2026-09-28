"""Stage 4: deterministic evidence verification and final draft shaping."""

from __future__ import annotations

import copy
from typing import Any, ClassVar, Mapping

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.inner.common import resolve_json_payload
from src.services import contracts
from src.services.domain_utils import (
    canonical_by_source,
    has_grounding_path,
    quote_in_any_source,
    reject_prohibited_keys,
    source_recorded_at,
)


class EvidenceVerifyNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("provenance_verification_failed", {"reason": "upstream_error"}, state)
            return {}

        facts = _facts_from_state(state, self._config)
        degradation = state.get("degradation") or contracts.empty_degradation()
        business_degraded = bool(
            set(degradation.get("reason_codes", []))
            & {"template_version_mismatch", "input_quality_rejected", "llm_unavailable"}
        )
        if business_degraded or not state.get("draft_candidate"):
            return self._fact_only(state, facts, degradation)

        try:
            verified, detail_codes, judgment_partial = self._verify_judgment_layer(state, facts)
        except _LayerRejected as exc:
            degradation = contracts.add_degradation(
                degradation,
                reason="provenance_verification_failed",
                detail=exc.detail_code,
            )
            emit_trace_event("provenance_verification_failed", {"detail_code": exc.detail_code}, state)
            return self._fact_only(state, facts, degradation)

        for detail in detail_codes:
            degradation = contracts.add_degradation(degradation, detail=detail)
        assessment_complete = not judgment_partial and not degradation.get("reason_codes")
        draft = {
            **facts,
            **verified,
            "candidate_causes_disclaimer": str(self._config.get("candidate_causes_disclaimer", "")),
            "assessment_complete": assessment_complete,
        }
        if "candidate_dropped" in detail_codes:
            emit_trace_event("candidate_dropped", {"count": 1}, state)
        if any(assertion.get("_downgraded") for assertion in verified["assertions"]):
            count = sum(1 for assertion in verified["assertions"] if assertion.get("_downgraded"))
            emit_trace_event("support_status_downgraded", {"count": count}, state)
            for assertion in verified["assertions"]:
                assertion.pop("_downgraded", None)
        emitted = {"recorded": 0, "inferred": 0}
        for candidate in verified["candidate_causes"]:
            emitted[candidate["origin"]] += 1
        emit_trace_event("candidate_causes_emitted", emitted, state)
        evidence_map = [
            {"claim_id": assertion["assertion_id"], "source_ids": assertion.get("assertion_source_ids", [])}
            for assertion in verified["assertions"]
        ]
        return {
            "handover_draft": draft,
            "evidence_map": evidence_map,
            "unresolved": verified["unresolved"],
            "requires_human_review": False,
            "degradation": degradation,
            "status": AgentStatus.SUCCESS.value,
        }

    def _fact_only(self, state: dict, facts: dict[str, Any], degradation: dict[str, list[str]]) -> dict[str, Any]:
        draft = {
            **facts,
            "assertions": [],
            "unresolved": [],
            "contradictions": [],
            "candidate_causes": [],
            "confirmation_questions": [],
            "candidate_causes_disclaimer": str(self._config.get("candidate_causes_disclaimer", "")),
            "assessment_complete": False,
        }
        emit_trace_event(
            "provenance_verification_failed",
            {"reason_codes": degradation.get("reason_codes", []), "detail_codes": degradation.get("detail_codes", [])},
            state,
        )
        return {
            "handover_draft": draft,
            "evidence_map": [],
            "unresolved": [],
            "requires_human_review": True,
            "degradation": degradation,
            "status": AgentStatus.SUCCESS.value,
        }

    def _verify_judgment_layer(
        self, state: dict, facts: dict[str, Any]
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str], bool]:
        draft = copy.deepcopy(state.get("draft_candidate") or {})
        if reject_prohibited_keys(draft):
            raise _LayerRejected("enum_value_rejected")
        for key in ("assertions", "unresolved", "contradictions", "candidate_causes", "confirmation_questions"):
            if not isinstance(draft.get(key), list):
                raise _LayerRejected("enum_value_rejected")
        source_ids = {source["source_id"] for source in facts["sources"]}
        check_ids = {check["check_id"] for check in facts["completed_checks"]}
        records = _resolve_records(self._config, state)
        canonical = canonical_by_source(records, facts["sources"])
        _validate_fact_quotes(facts, canonical, self._config)
        recorded_at_by_sid = source_recorded_at(facts["sources"])

        assertions = [_assertion(item) for item in draft["assertions"]]
        unresolved = [_unresolved(item) for item in draft["unresolved"]]
        contradictions = [_contradiction(item) for item in draft["contradictions"]]
        questions = [_question(item) for item in draft["confirmation_questions"]]

        detail_codes: list[str] = []
        assertions, assertion_quote_details = _filter_assertions_by_quote(
            assertions,
            source_ids=source_ids,
            canonical=canonical,
            config=self._config,
        )
        detail_codes.extend(assertion_quote_details)
        judgment_partial = bool(assertion_quote_details)
        for item in assertions:
            _require_sources(item.get("assertion_source_ids", []), source_ids)
            _require_sources(item.get("evidence_source_ids", []), source_ids)
        for item in unresolved + contradictions:
            _require_sources(item.get("source_ids", []), source_ids)

        completed_by_id = {check["check_id"]: check for check in facts["completed_checks"]}
        for assertion in assertions:
            corroborating = assertion.get("corroborating_check_ids", [])
            unknown_checks = [check_id for check_id in corroborating if check_id not in check_ids]
            if unknown_checks and assertion["support_status"] not in contracts.AFFIRMATIVE_SUPPORT_STATUSES:
                raise _LayerRejected("unknown_check_id")
            if assertion["support_status"] in contracts.AFFIRMATIVE_SUPPORT_STATUSES:
                grounded = False
                for check_id in corroborating:
                    check = completed_by_id.get(check_id)
                    if check and has_grounding_path(
                        assertion,
                        check,
                        canonical_by_sid=canonical,
                        recorded_at_by_sid=recorded_at_by_sid,
                        window_minutes=int(self._config.get("corroboration_window_minutes", 120)),
                    ):
                        grounded = True
                        break
                if not corroborating or unknown_checks or not grounded:
                    assertion["support_status"] = "insufficient"
                    assertion["_downgraded"] = True
                    if "support_status_downgraded" not in detail_codes:
                        detail_codes.append("support_status_downgraded")
                    _ensure_unresolved_for_assertion(unresolved, assertion)

        candidates, dropped_ids, candidate_quote_details = self._verify_candidates(
            draft["candidate_causes"],
            questions,
            source_ids,
            check_ids,
            canonical,
        )
        if candidate_quote_details:
            detail_codes.extend(candidate_quote_details)
            judgment_partial = True
        order = {source["source_id"]: index for index, source in enumerate(facts["sources"])}
        candidates.sort(key=lambda item: (min(order.get(source_id, 10_000) for source_id in item["basis_source_ids"]), item["candidate_id"]))

        if dropped_ids:
            detail_codes.append("candidate_dropped")
        questions = [question for question in questions if question.get("resolves_candidate_id") not in dropped_ids]
        valid_unresolved_ids = {item["unresolved_id"] for item in unresolved}
        questions, question_dropped, invalid_candidate_ids = _filter_questions(
            questions,
            valid_unresolved_ids=valid_unresolved_ids,
            valid_candidate_ids={item["candidate_id"] for item in candidates},
        )
        if invalid_candidate_ids:
            candidates = [candidate for candidate in candidates if candidate["candidate_id"] not in invalid_candidate_ids]
            questions = [question for question in questions if question.get("resolves_candidate_id") not in invalid_candidate_ids]
            dropped_ids.update(invalid_candidate_ids)
            if "candidate_dropped" not in detail_codes:
                detail_codes.append("candidate_dropped")
        if question_dropped and "question_target_missing" not in detail_codes:
            detail_codes.append("question_target_missing")
        return (
            {
                "assertions": assertions,
                "unresolved": unresolved,
                "contradictions": contradictions,
                "candidate_causes": candidates,
                "confirmation_questions": questions,
            },
            detail_codes,
            judgment_partial or bool(dropped_ids or question_dropped),
        )

    def _verify_candidates(
        self,
        raw_candidates: list[Any],
        questions: list[dict[str, Any]],
        source_ids: set[str],
        check_ids: set[str],
        canonical: dict[str, str],
    ) -> tuple[list[dict[str, Any]], set[str], list[str]]:
        question_by_id = {question["question_id"]: question for question in questions}
        result: list[dict[str, Any]] = []
        dropped: set[str] = set()
        detail_codes: list[str] = []
        seen_ids: set[str] = set()
        for raw in raw_candidates:
            try:
                candidate = _candidate(raw)
                if candidate["candidate_id"] in seen_ids:
                    raise ValueError("duplicate")
                seen_ids.add(candidate["candidate_id"])
                basis_ids = candidate["basis_source_ids"]
                if not basis_ids or not set(basis_ids) <= source_ids:
                    raise ValueError("source")
                quote_source_ids = [item["source_id"] for item in candidate["basis_quotes"]]
                if set(quote_source_ids) != set(basis_ids):
                    raise ValueError("basis_quote_set")
                kept_quotes: list[dict[str, str]] = []
                quote_details: list[str] = []
                for item in candidate["basis_quotes"]:
                    detail = quote_in_any_source(
                        item["quote"],
                        [item["source_id"]],
                        canonical_by_source=canonical,
                        min_quote_chars=int(self._config.get("min_quote_chars", 8)),
                        max_quote_chars=int(self._config.get("max_quote_chars", 300)),
                    )
                    if not detail:
                        kept_quotes.append(item)
                    elif detail not in quote_details:
                        quote_details.append(detail)
                if not kept_quotes:
                    detail_codes.extend(detail for detail in quote_details if detail not in detail_codes)
                    raise ValueError("basis_quotes")
                if len(kept_quotes) != len(candidate["basis_quotes"]):
                    detail_codes.extend(detail for detail in quote_details if detail not in detail_codes)
                candidate["basis_quotes"] = kept_quotes
                candidate["basis_source_ids"] = sorted({item["source_id"] for item in kept_quotes})
                check_id = candidate.get("discriminating_check_id")
                if check_id is not None and check_id not in check_ids:
                    raise ValueError("check")
                question = question_by_id.get(candidate["resolves_by_question_id"])
                if question is None or question.get("resolves_candidate_id") != candidate["candidate_id"]:
                    raise ValueError("question")
                if candidate["status"] != "not_assessed":
                    raise ValueError("status")
                result.append(candidate)
            except Exception:  # noqa: BLE001
                candidate_id = raw.get("candidate_id") if isinstance(raw, Mapping) else ""
                if isinstance(candidate_id, str) and candidate_id:
                    dropped.add(candidate_id)
        if len(result) > int(self._config.get("max_candidate_causes", 5)):
            for candidate in result[int(self._config.get("max_candidate_causes", 5)) :]:
                dropped.add(candidate["candidate_id"])
            result = result[: int(self._config.get("max_candidate_causes", 5))]
        return result, dropped, detail_codes


class _LayerRejected(ValueError):
    def __init__(self, detail_code: str) -> None:
        self.detail_code = detail_code
        super().__init__(detail_code)


def _facts_from_state(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    return {
        "sources": list(state.get("source_registry", [])),
        "timeline": list(state.get("timeline", [])),
        "completed_checks": list(state.get("completed_checks", [])),
        "observed_identifiers": list(state.get("observed_identifiers", [])),
        "counterpart_label": str(state.get("validated_input", {}).get("counterpart_label", "unspecified")),
        "event": dict(state.get("event", {})),
    }


def _resolve_records(config: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        payload = resolve_json_payload(config, state, state.get("canonical_payload_ref"), state.get("request_scope", {}))
    except Exception:  # noqa: BLE001
        return []
    return payload.get("records", []) if isinstance(payload.get("records"), list) else []


def _require_sources(values: Any, source_ids: set[str]) -> None:
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise _LayerRejected("unknown_source_id")
    if not set(values) <= source_ids:
        raise _LayerRejected("unknown_source_id")


def _validate_fact_quotes(facts: dict[str, Any], canonical: dict[str, str], config: dict[str, Any]) -> None:
    event = facts.get("event") if isinstance(facts.get("event"), dict) else {}
    if event.get("opening_quote"):
        _validate_quote_or_raise(event.get("opening_quote"), event.get("source_ids", []), canonical, config)
    for item in facts.get("timeline", []):
        if isinstance(item, dict) and item.get("quote"):
            _validate_quote_or_raise(item.get("quote"), item.get("source_ids", []), canonical, config)
    for item in facts.get("completed_checks", []):
        if isinstance(item, dict) and item.get("quote"):
            _validate_quote_or_raise(item.get("quote"), item.get("source_ids", []), canonical, config)


def _validate_quote_or_raise(quote: Any, source_ids: Any, canonical: dict[str, str], config: dict[str, Any]) -> None:
    if not isinstance(source_ids, list) or any(not isinstance(item, str) for item in source_ids):
        raise _LayerRejected("unknown_source_id")
    detail = quote_in_any_source(
        str(quote),
        source_ids,
        canonical_by_source=canonical,
        min_quote_chars=int(config.get("min_quote_chars", 8)),
        max_quote_chars=int(config.get("max_quote_chars", 300)),
    )
    if detail:
        raise _LayerRejected(detail)


def _filter_assertions_by_quote(
    assertions: list[dict[str, Any]],
    *,
    source_ids: set[str],
    canonical: dict[str, str],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    kept: list[dict[str, Any]] = []
    detail_codes: list[str] = []
    for assertion in assertions:
        _require_sources(assertion.get("assertion_source_ids", []), source_ids)
        _require_sources(assertion.get("evidence_source_ids", []), source_ids)
        detail = quote_in_any_source(
            assertion["recorded_assertion_quote"],
            assertion["assertion_source_ids"],
            canonical_by_source=canonical,
            min_quote_chars=int(config.get("min_quote_chars", 8)),
            max_quote_chars=int(config.get("max_quote_chars", 300)),
        )
        if detail:
            if detail not in detail_codes:
                detail_codes.append(detail)
            continue
        kept.append(assertion)
    return kept, detail_codes


def _assertion(item: Any) -> dict[str, Any]:
    allowed = {
        "assertion_id",
        "recorded_assertion_quote",
        "assertion_source_ids",
        "support_status",
        "corroborating_check_ids",
        "evidence_source_ids",
    }
    if not isinstance(item, dict) or set(item) != allowed:
        raise _LayerRejected("enum_value_rejected")
    if item["support_status"] not in contracts.SUPPORT_STATUSES:
        raise _LayerRejected("enum_value_rejected")
    if not isinstance(item["assertion_id"], str) or not isinstance(item["recorded_assertion_quote"], str):
        raise _LayerRejected("enum_value_rejected")
    if not isinstance(item["corroborating_check_ids"], list) or any(
        not isinstance(check_id, str) for check_id in item["corroborating_check_ids"]
    ):
        raise _LayerRejected("unknown_check_id")
    return dict(item)


def _unresolved(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != {"unresolved_id", "unresolved_kind", "source_ids"}:
        raise _LayerRejected("enum_value_rejected")
    if item["unresolved_kind"] not in contracts.UNRESOLVED_KINDS:
        raise _LayerRejected("enum_value_rejected")
    return dict(item)


def _contradiction(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != {"contradiction_id", "contradiction_kind", "source_ids"}:
        raise _LayerRejected("enum_value_rejected")
    if item["contradiction_kind"] not in contracts.CONTRADICTION_KINDS:
        raise _LayerRejected("enum_value_rejected")
    return dict(item)


def _question(item: Any) -> dict[str, Any]:
    allowed = {
        "question_id",
        "question_topic",
        "ask_target",
        "question_text",
        "resolves_unresolved_id",
        "resolves_candidate_id",
        "why_needed_code",
        "draft_only",
    }
    if not isinstance(item, dict) or set(item) != allowed:
        raise _LayerRejected("enum_value_rejected")
    if item["question_topic"] not in contracts.QUESTION_TOPICS or item["ask_target"] not in contracts.ASK_TARGETS:
        raise _LayerRejected("enum_value_rejected")
    if item["why_needed_code"] not in contracts.WHY_NEEDED_CODES or item["draft_only"] is not True:
        raise _LayerRejected("enum_value_rejected")
    if not isinstance(item["question_id"], str) or not isinstance(item["question_text"], str):
        raise _LayerRejected("enum_value_rejected")
    return dict(item)


def _candidate(item: Any) -> dict[str, Any]:
    allowed = {
        "candidate_id",
        "hypothesis_text",
        "origin",
        "basis_source_ids",
        "basis_quotes",
        "discriminating_check_id",
        "resolves_by_question_id",
        "status",
    }
    if not isinstance(item, dict) or set(item) != allowed:
        raise ValueError("candidate_shape")
    if item["origin"] not in contracts.CAUSE_ORIGINS or item["status"] not in contracts.CANDIDATE_STATUSES:
        raise ValueError("candidate_enum")
    if not isinstance(item["basis_source_ids"], list) or any(not isinstance(sid, str) for sid in item["basis_source_ids"]):
        raise ValueError("candidate_basis")
    if not isinstance(item["basis_quotes"], list) or not item["basis_quotes"]:
        raise ValueError("candidate_basis_quotes")
    for quote in item["basis_quotes"]:
        if not isinstance(quote, dict) or set(quote) != {"source_id", "quote"}:
            raise ValueError("candidate_basis_quote")
        if not isinstance(quote["source_id"], str) or not isinstance(quote["quote"], str):
            raise ValueError("candidate_basis_quote")
    if item["discriminating_check_id"] is not None and not isinstance(item["discriminating_check_id"], str):
        raise ValueError("candidate_check")
    if not isinstance(item["candidate_id"], str) or not isinstance(item["hypothesis_text"], str):
        raise ValueError("candidate_text")
    if not isinstance(item["resolves_by_question_id"], str):
        raise ValueError("candidate_question")
    return dict(item)


def _filter_questions(
    questions: list[dict[str, Any]],
    *,
    valid_unresolved_ids: set[str],
    valid_candidate_ids: set[str],
) -> tuple[list[dict[str, Any]], bool, set[str]]:
    kept: list[dict[str, Any]] = []
    dropped = False
    invalid_candidate_ids: set[str] = set()
    for question in questions:
        has_unresolved = question.get("resolves_unresolved_id") is not None
        has_candidate = question.get("resolves_candidate_id") is not None
        candidate_id = question.get("resolves_candidate_id")
        if has_unresolved == has_candidate:
            dropped = True
            if isinstance(candidate_id, str) and candidate_id in valid_candidate_ids:
                invalid_candidate_ids.add(candidate_id)
            continue
        if has_unresolved and question["resolves_unresolved_id"] not in valid_unresolved_ids:
            dropped = True
            continue
        if has_candidate:
            if question["resolves_candidate_id"] not in valid_candidate_ids:
                dropped = True
                continue
            if (
                question["question_topic"] != "cause_discrimination"
                or question["why_needed_code"] != "candidate_discrimination_needed"
            ):
                dropped = True
                invalid_candidate_ids.add(question["resolves_candidate_id"])
                continue
        kept.append(question)
    return kept, dropped, invalid_candidate_ids


def _ensure_unresolved_for_assertion(unresolved: list[dict[str, Any]], assertion: dict[str, Any]) -> None:
    source_ids = assertion.get("assertion_source_ids", [])
    for item in unresolved:
        if item.get("unresolved_kind") == "unsupported_assertion" and item.get("source_ids") == source_ids:
            return
    unresolved.append(
        {
            "unresolved_id": f"U-{len(unresolved) + 1:02d}",
            "unresolved_kind": "unsupported_assertion",
            "source_ids": list(source_ids),
        }
    )
