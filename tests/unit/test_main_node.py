from __future__ import annotations

import inspect
import json

from framework.schemas.agent_status import AgentStatus
from src.nodes.main_node import MainNode


class TestMainNode:
    def test_upstream_error_passthrough_does_not_build_subgraph(self):
        node = MainNode()
        assert node.execute({"status": AgentStatus.ERROR.value}) == {}
        assert node._subgraph is None

    def test_extract_input_uses_explicit_json_envelope_without_free_text(self):
        node = MainNode()
        state = {
            "mode": "invoke",
            "incident_ref": "INC-2026-0817-014",
            "canonical_payload_ref": "abcdefghijklmnopabcdefghijklmnop",
            "request_scope": {"counterpart_carrier_id": "cp-a"},
            "validated_input": {"record_count": 1},
            "template_check": {"ok": True},
            "input_quality": {"accepted_count": 1, "rejected": []},
            "degradation": {"reason_codes": [], "detail_codes": []},
        }

        envelope = json.loads(node.extract_input(state))

        assert set(envelope) == {
            "mode",
            "incident_ref",
            "canonical_payload_ref",
            "request_scope",
            "validated_input",
            "template_check",
            "input_quality",
            "degradation",
        }
        assert "text" not in json.dumps(envelope, ensure_ascii=False)

    def test_merge_output_returns_mode_scoped_fields(self):
        node = MainNode()
        delta = node.merge_output(
            {"mode": "invoke"},
            {
                "status": AgentStatus.SUCCESS.value,
                "handover_draft": {"sources": []},
                "evidence_map": [],
                "node_history": ["inner"],
            },
        )

        assert delta["status"] == AgentStatus.SUCCESS.value
        assert delta["gate_kind"] == "handover_draft"
        assert delta["handover_draft"] == {"sources": []}
        assert "node_history" not in delta

    def test_execute_method_signature(self):
        assert hasattr(MainNode, "execute")
        sig = inspect.signature(MainNode.execute)
        params = list(sig.parameters.keys())
        assert len(params) >= 2
        assert params[1] == "state"
        assert "_invoke_impl" not in MainNode.__dict__

    def test_llm_threaded_through_constructor(self):
        sentinel = object()
        node = MainNode(llm=sentinel)
        assert node._llm is sentinel
        assert MainNode()._llm is None
