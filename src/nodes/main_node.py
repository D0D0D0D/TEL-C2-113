"""Main GraphNode wrapping the inner handover workflow."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.cat2_graph_skeleton import DomainWorkflowGraphNode, ModeSpec
from src.services.contracts import DEGRADATION_REASON_CODES
from src.services.wiring import load_runtime_config, wire_services


class InterconnectHandoverGraphNode(DomainWorkflowGraphNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False
    known_failure_codes: ClassVar[tuple[str, ...]] = tuple(sorted(DEGRADATION_REASON_CODES))
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "invoke": ModeSpec(
            gate_kind="handover_draft",
            fields={
                "handover_draft": "handover_draft",
                "evidence_map": "evidence_map",
                "unresolved": "unresolved",
                "requires_human_review": "requires_human_review",
                "degradation": "degradation",
                "error_log": "error_log",
            },
        ),
        "ingest": ModeSpec(
            gate_kind="ingest_summary",
            fields={"ingest_result": "ingest_result", "degradation": "degradation", "error_log": "error_log"},
        ),
        "feedback": ModeSpec(
            gate_kind="feedback_summary",
            fields={"feedback_result": "feedback_result", "degradation": "degradation", "error_log": "error_log"},
        ),
    }

    def __init__(self, *, config: dict[str, Any] | None = None, llm: Any = None) -> None:
        node_config = load_runtime_config(config)
        if "llm" not in node_config:
            node_config["llm"] = llm
        wire_services(node_config)
        self._llm = node_config.get("llm")
        super().__init__(
            config=node_config,
            subgraph_factory=_build_workflow_graph,
            compile_subgraph=True,
            use_inner_checkpointer=False,
        )

    def _parent_config(self) -> dict[str, Any]:
        return dict(self._config)

    def extract_input(self, state: dict) -> str:
        envelope = {
            "mode": state.get("mode", ""),
            "incident_ref": state.get("incident_ref", ""),
            "canonical_payload_ref": state.get("canonical_payload_ref", ""),
            "request_scope": state.get("request_scope", {}),
            "validated_input": state.get("validated_input", {}),
            "template_check": state.get("template_check", {}),
            "input_quality": state.get("input_quality", {}),
            "degradation": state.get("degradation", {"reason_codes": [], "detail_codes": []}),
        }
        return json.dumps(envelope, ensure_ascii=False, default=str, sort_keys=True)

    def merge_output(self, state: dict, sub_result: dict) -> dict:
        if isinstance(sub_result.get("output"), dict):
            merged = {**sub_result["output"], **{key: value for key, value in sub_result.items() if key != "output"}}
        else:
            merged = sub_result
        mode = state.get("mode") or merged.get("mode")
        spec = self.mode_specs.get(mode)
        if spec is None:
            return {"status": AgentStatus.ERROR.value, "error_log": [f"InterconnectHandoverGraphNode: unknown mode {mode!r}"]}
        delta: dict[str, Any] = {"gate_kind": spec.gate_kind, "status": merged.get("status", AgentStatus.SUCCESS.value)}
        for outer_key, inner_key in spec.fields.items():
            if inner_key in merged and merged[inner_key] is not None:
                delta[outer_key] = merged[inner_key]
        return delta


MainNode = InterconnectHandoverGraphNode


def _build_workflow_graph(config: dict[str, Any]):
    from src.graph.domain_workflow_graph import InterconnectHandoverWorkflowGraph

    return InterconnectHandoverWorkflowGraph(config)
