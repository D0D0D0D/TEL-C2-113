"""Inner workflow graph for TEL-C2-113."""

from __future__ import annotations

import json

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.inner.canonical_structure_node import CanonicalStructureNode
from src.nodes.inner.evidence_verify_node import EvidenceVerifyNode
from src.nodes.inner.feedback_node import FeedbackNode
from src.nodes.inner.handover_mapping_node import HandoverMappingNode
from src.nodes.inner.ingest_node import IngestNode
from src.nodes.inner.perspective_recall_node import PerspectiveRecallNode
from src.schemas.state import InnerWorkflowState


class InterconnectHandoverWorkflowGraph(BaseGraph):
    @property
    def name(self) -> str:
        return "interconnect_handover_workflow"

    @property
    def state_schema(self) -> type:
        return InnerWorkflowState

    def _validate_config(self) -> None:
        required = (
            "llm",
            "kb",
            "payload_store",
            "perspective_ledger",
            "recall_top_k",
            "min_quote_chars",
            "max_quote_chars",
            "max_question_chars",
            "max_hypothesis_chars",
            "max_candidate_causes",
            "candidate_causes_disclaimer",
            "check_action_markers",
            "corroboration_window_minutes",
        )
        missing = [key for key in required if key not in self.config]
        if missing:
            raise ValueError(f"InterconnectHandoverWorkflowGraph: missing config keys {missing}")

    def register_nodes(self) -> None:
        self._nodes["stage1"] = CanonicalStructureNode(config=self.config)
        self._nodes["stage2"] = PerspectiveRecallNode(config=self.config)
        self._nodes["stage3"] = HandoverMappingNode(config=self.config)
        self._nodes["stage4"] = EvidenceVerifyNode(config=self.config)
        self._nodes["ingest"] = IngestNode(config=self.config)
        self._nodes["feedback"] = FeedbackNode(config=self.config)

    def add_edges(self) -> None:
        self._sg.add_conditional_edges(START, self.route)
        self._sg.add_edge("stage1", "stage2")
        self._sg.add_edge("stage2", "stage3")
        self._sg.add_edge("stage3", "stage4")
        self._sg.add_edge("stage4", END)
        self._sg.add_edge("ingest", END)
        self._sg.add_edge("feedback", END)

    def route(self, state: InnerWorkflowState) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        mode = state.get("mode")
        if not mode:
            raw = state.get("user_input", "")
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    mode = parsed.get("mode") if isinstance(parsed, dict) else ""
                except json.JSONDecodeError:
                    mode = ""
        if mode == "invoke":
            return "stage1"
        if mode == "ingest":
            return "ingest"
        if mode == "feedback":
            return "feedback"
        return END

    def get_output(self, state: InnerWorkflowState) -> dict:
        return {
            "mode": state.get("mode"),
            "handover_draft": state.get("handover_draft"),
            "evidence_map": state.get("evidence_map"),
            "unresolved": state.get("unresolved"),
            "requires_human_review": state.get("requires_human_review"),
            "degradation": state.get("degradation"),
            "ingest_result": state.get("ingest_result"),
            "feedback_result": state.get("feedback_result"),
            "status": state.get("status", AgentStatus.SUCCESS.value),
            "error_log": state.get("error_log", []),
        }
