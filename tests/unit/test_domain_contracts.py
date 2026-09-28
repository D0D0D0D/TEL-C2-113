from __future__ import annotations

import ast
import json
from pathlib import Path

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.api.server import example_template_pin
from src.graph.graph import InterconnectIncidentHandoverGraph
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.services.contracts import APPROVED_TEMPLATE_REF, APPROVED_TEMPLATE_VERSION, SCOPE_KEYS
from src.services.wiring import load_runtime_config, wire_services


class FakeLlm:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    def complete(self, messages):
        self.calls += 1
        self.messages.append(messages)
        return {"content": json.dumps(self.payload, ensure_ascii=False)}


def _invoke_payload() -> dict:
    return {
        "incident_ref": "INC-2026-0817-014",
        "handover_template": example_template_pin(),
        "records": [
            {
                "record_id": "REC-001",
                "record_kind": "incident_note",
                "recorded_at": "2026-08-17T12:40:00+09:00",
                "text": "GW-TKY-03 で SIP 503 が増加。相手確認待ち",
            },
            {
                "record_id": "REC-002",
                "record_kind": "event_reference",
                "recorded_at": "2026-08-17T12:50:00+09:00",
                "text": "12:50 JST GW-TKY-03 疎通確認 test 実施 verified。",
            },
        ],
    }


def _valid_judgment() -> dict:
    return {
        "assertions": [
            {
                "assertion_id": "A-01",
                "recorded_assertion_quote": "GW-TKY-03 で SIP 503 が増加",
                "assertion_source_ids": ["S-001"],
                "support_status": "supported",
                "corroborating_check_ids": ["C-01"],
                "evidence_source_ids": ["S-002"],
            }
        ],
        "unresolved": [],
        "contradictions": [],
        "candidate_causes": [
            {
                "candidate_id": "H-99",
                "hypothesis_text": "GW-TKY-03 の経路状態変化を確認する候補です。",
                "origin": "inferred",
                "basis_source_ids": ["S-001"],
                "basis_quotes": [{"source_id": "S-001", "quote": "GW-TKY-03 で SIP 503 が増加"}],
                "discriminating_check_id": "C-01",
                "resolves_by_question_id": "Q-01",
                "status": "not_assessed",
            }
        ],
        "confirmation_questions": [
            {
                "question_id": "Q-01",
                "question_topic": "cause_discrimination",
                "ask_target": "counterpart_carrier",
                "question_text": "相手側の同時刻の経路変更有無を確認してください。",
                "resolves_unresolved_id": None,
                "resolves_candidate_id": "H-99",
                "why_needed_code": "candidate_discrimination_needed",
                "draft_only": True,
            }
        ],
    }


def _run_graph(llm_payload: dict) -> dict:
    llm = FakeLlm(llm_payload)
    graph = InterconnectIncidentHandoverGraph(config={"llm": llm})
    graph.compile()
    ctx = InvocationContext(
        session_id="unit-session",
        caller_id="caller-1",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )
    result = graph.invoke(
        json.dumps(_invoke_payload(), ensure_ascii=False),
        ctx=ctx,
        input_context={
            "mode": "invoke",
            "request_scope": {"counterpart_carrier_id": "cp-unit"},
            "caller_id": "caller-1",
        },
    )
    assert llm.calls == 1
    return result


def test_preprocess_state_contract_excludes_raw_free_text():
    config = wire_services(load_runtime_config())
    node = PreProcessNode(config=config)
    state = {
        "user_input": json.dumps(_invoke_payload(), ensure_ascii=False),
        "input_context": {"mode": "invoke", "request_scope": {"counterpart_carrier_id": "cp-unit"}},
        "session_id": "unit-session",
    }

    result = node.execute(state)

    assert result["status"] == "success"
    assert "GW-TKY-03" not in json.dumps(result["validated_input"], ensure_ascii=False)
    assert set(result["request_scope"]) == set(SCOPE_KEYS)


def test_stage4_support_status_downgrades_without_grounding():
    payload = _valid_judgment()
    payload["assertions"][0]["corroborating_check_ids"] = []

    result = _run_graph(payload)
    draft = result["output"]["handover_draft"]

    assert draft["assertions"][0]["support_status"] == "insufficient"
    assert draft["unresolved"][0]["unresolved_kind"] == "unsupported_assertion"
    assert "support_status_downgraded" in result["output"]["degradation"]["detail_codes"]


def test_stage4_drops_vacuous_assertion_quote_without_fact_only():
    payload = _valid_judgment()
    payload["assertions"][0]["recorded_assertion_quote"] = ""

    result = _run_graph(payload)

    assert result["output"]["requires_human_review"] is False
    assert result["output"]["handover_draft"]["assertions"] == []
    assert result["output"]["handover_draft"]["candidate_causes"]
    assert "quote_too_short" in result["output"]["degradation"]["detail_codes"]
    assert "provenance_verification_failed" not in result["output"]["degradation"]["reason_codes"]


def test_stage4_drops_only_short_quote_items_and_keeps_remaining_layer():
    payload = _valid_judgment()
    payload["assertions"] = [
        {
            "assertion_id": "A-SHORT",
            "recorded_assertion_quote": "SIP 503",
            "assertion_source_ids": ["S-001"],
            "support_status": "insufficient",
            "corroborating_check_ids": [],
            "evidence_source_ids": [],
        },
        {
            "assertion_id": "A-OK",
            "recorded_assertion_quote": "GW-TKY-03 で SIP 503 が増加",
            "assertion_source_ids": ["S-001"],
            "support_status": "supported",
            "corroborating_check_ids": ["C-01"],
            "evidence_source_ids": ["S-002"],
        },
    ]
    payload["candidate_causes"][0]["candidate_id"] = "H-LOCAL"
    payload["candidate_causes"][0]["basis_source_ids"] = ["S-001", "S-002"]
    payload["candidate_causes"][0]["basis_quotes"] = [
        {"source_id": "S-001", "quote": "SIP 503"},
        {"source_id": "S-002", "quote": "GW-TKY-03 疎通確認 test 実施 verified"},
    ]
    payload["confirmation_questions"][0]["resolves_candidate_id"] = "H-LOCAL"

    result = _run_graph(payload)
    output = result["output"]
    draft = output["handover_draft"]

    assert output["requires_human_review"] is False
    assert [item["assertion_id"] for item in draft["assertions"]] == ["A-OK"]
    assert draft["candidate_causes"][0]["candidate_id"] == "H-LOCAL"
    assert draft["candidate_causes"][0]["basis_source_ids"] == ["S-002"]
    assert draft["candidate_causes"][0]["basis_quotes"] == [
        {"source_id": "S-002", "quote": "GW-TKY-03 疎通確認 test 実施 verified"}
    ]
    assert draft["confirmation_questions"]
    assert "quote_too_short" in output["degradation"]["detail_codes"]
    assert "provenance_verification_failed" not in output["degradation"]["reason_codes"]


def test_candidate_constraints_preserve_id_and_have_no_rank_or_confidence():
    result = _run_graph(_valid_judgment())
    candidate = result["output"]["handover_draft"]["candidate_causes"][0]

    assert candidate["candidate_id"] == "H-99"
    assert candidate["status"] == "not_assessed"
    assert {"rank", "confidence", "probability", "likelihood"}.isdisjoint(candidate)


def test_live_llm_fixture_reaches_stage3_with_fake_llm(case_payload, live_input_context):
    llm = FakeLlm(
        {
            "assertions": [],
            "unresolved": [],
            "contradictions": [],
            "candidate_causes": [],
            "confirmation_questions": [],
        }
    )
    graph = InterconnectIncidentHandoverGraph(config={"llm": llm})
    graph.compile()
    ctx = InvocationContext(
        session_id="live-fixture-session",
        caller_id="live-check-caller",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )

    result = graph.invoke(
        json.dumps(case_payload, ensure_ascii=False),
        ctx=ctx,
        input_context=live_input_context,
    )

    assert llm.calls == 1
    assert result["status"] == "success"
    assert result["output"]["mode"] == "invoke"


def test_live_fixture_prompt_contract_and_literal_example(case_payload, live_input_context):
    llm = FakeLlm(
        {
            "assertions": [],
            "unresolved": [],
            "contradictions": [],
            "candidate_causes": [],
            "confirmation_questions": [],
        }
    )
    graph = InterconnectIncidentHandoverGraph(config={"llm": llm})
    graph.compile()
    ctx = InvocationContext(
        session_id="live-prompt-session",
        caller_id="live-check-caller",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )

    graph.invoke(
        json.dumps(case_payload, ensure_ascii=False),
        ctx=ctx,
        input_context=live_input_context,
    )

    prompt = json.loads(llm.messages[0][0]["content"])
    constraints = "\n".join(prompt["constraints"])
    example = prompt["one_valid_output_example"]
    schema = prompt["output_schema"]
    example_quote = example["assertions"][0]["recorded_assertion_quote"]
    example_source_id = example["assertions"][0]["assertion_source_ids"][0]
    canonical_by_source = {
        item["source_id"]: item["canonical_text"]
        for item in prompt["canonical_records_for_quote_selection"]
    }
    assert "exactly one of resolves_unresolved_id or resolves_candidate_id" in constraints
    assert "question_topic MUST be cause_discrimination" in constraints
    assert "List possible causes readable from the records" in constraints
    assert schema["candidate_causes"]
    assert schema["candidate_causes"][0]["status"] == "not_assessed"
    assert schema["candidate_causes"][0]["basis_source_ids"]
    assert schema["candidate_causes"][0]["basis_quotes"]
    assert example_quote in canonical_by_source[example_source_id]


def test_live_like_llm_judgment_layer_survives_kb_empty(case_payload, live_input_context):
    llm = FakeLlm(
        {
            "assertions": [
                {
                    "assertion_id": "A-10",
                    "recorded_assertion_quote": "GW-TKY-03 で SIP 503 増加",
                    "assertion_source_ids": ["S-001"],
                    "support_status": "supported",
                    "corroborating_check_ids": ["C-01"],
                    "evidence_source_ids": ["S-002"],
                }
            ],
            "unresolved": [],
            "contradictions": [],
            "candidate_causes": [
                {
                    "candidate_id": "H-10",
                    "hypothesis_text": "TG-INT-001の応答遅延に関連する接続状態を確認する候補です。",
                    "origin": "inferred",
                    "basis_source_ids": ["S-002"],
                    "basis_quotes": [{"source_id": "S-002", "quote": "TG-INT-001側は応答遅延"}],
                    "discriminating_check_id": "C-01",
                    "resolves_by_question_id": "Q-10",
                    "status": "not_assessed",
                }
            ],
            "confirmation_questions": [
                {
                    "question_id": "Q-10",
                    "question_topic": "cause_discrimination",
                    "ask_target": "counterpart_carrier",
                    "question_text": "同時刻のTG-INT-001側の接続状態を確認してください。",
                    "resolves_unresolved_id": None,
                    "resolves_candidate_id": "H-10",
                    "why_needed_code": "candidate_discrimination_needed",
                    "draft_only": True,
                }
            ],
        }
    )
    graph = InterconnectIncidentHandoverGraph(config={"llm": llm})
    graph.compile()
    ctx = InvocationContext(
        session_id="live-like-session",
        caller_id="live-check-caller",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )

    result = graph.invoke(
        json.dumps(case_payload, ensure_ascii=False),
        ctx=ctx,
        input_context=live_input_context,
    )

    output = result["output"]
    draft = output["handover_draft"]
    assert llm.calls == 1
    assert draft["assertions"]
    assert draft["confirmation_questions"]
    assert output["requires_human_review"] is False
    assert "kb_scope_unresolved" in output["degradation"]["reason_codes"]
    assert "provenance_verification_failed" not in output["degradation"]["reason_codes"]


def test_invalid_unlinked_question_is_dropped_without_fact_only():
    payload = _valid_judgment()
    payload["confirmation_questions"].append(
        {
            "question_id": "Q-BAD",
            "question_topic": "evidence_request",
            "ask_target": "counterpart_carrier",
            "question_text": "追加確認事項を確認してください。",
            "resolves_unresolved_id": None,
            "resolves_candidate_id": None,
            "why_needed_code": "corroboration_missing",
            "draft_only": True,
        }
    )

    result = _run_graph(payload)
    draft = result["output"]["handover_draft"]

    assert result["output"]["requires_human_review"] is False
    assert draft["assertions"]
    assert [item["question_id"] for item in draft["confirmation_questions"]] == ["Q-01"]
    assert "question_target_missing" in result["output"]["degradation"]["detail_codes"]


def test_s3_rejects_prohibited_output_key():
    node = PostProcessNode(config=load_runtime_config())
    state = {
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "s3-test",
        "mode": "invoke",
        "incident_ref": "INC-2026-0817-014",
        "template_check": {"template_ref": APPROVED_TEMPLATE_REF, "template_version": APPROVED_TEMPLATE_VERSION},
        "handover_draft": {
            "sources": [],
            "timeline": [],
            "completed_checks": [],
            "observed_identifiers": [],
            "counterpart_label": "unspecified",
            "event": {},
            "assertions": [],
            "unresolved": [],
            "contradictions": [],
            "candidate_causes": [],
            "confirmation_questions": [],
            "candidate_causes_disclaimer": load_runtime_config()["candidate_causes_disclaimer"],
            "assessment_complete": False,
            "root_cause": "blocked",
        },
        "evidence_map": [],
        "requires_human_review": False,
        "degradation": {"reason_codes": [], "detail_codes": []},
    }

    result = node(state)

    assert result["status"] == "error"
    assert "prohibited output key" in result["error_log"][0]


def test_s3_error_envelope_redacts_pii_and_traceback():
    node = PostProcessNode(config=load_runtime_config())
    result = node._extra_security_gate_output(
        {
            "formatted_output": {
                "mode": "invoke",
                "gate_kind": "error",
                "status": "error",
                "error_log": ['[Node] Taro Yamada failed\nTraceback File "/Users/example/app.py", line 1'],
            }
        }
    )

    assert result["formatted_output"]["error_log"] == ["error_log_redacted"]


def test_invocation_context_not_constructed_inside_nodes():
    violations = []
    for path in Path("src/nodes").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "InvocationContext":
                violations.append(f"{path}:{node.lineno}")
    assert violations == []


def test_execute_methods_do_not_emit_lifecycle_events():
    forbidden = {"node_start", "node_complete", "node_error"}
    violations = []
    for path in Path("src/nodes").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in forbidden:
                violations.append(f"{path}:{node.lineno}:{node.value}")
    assert violations == []
