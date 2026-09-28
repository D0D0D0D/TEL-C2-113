"""Outer AgentBaseGraph for TEL-C2-113."""

from __future__ import annotations

from typing import Any

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import MainNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.wiring import load_runtime_config, wire_services


class InterconnectIncidentHandoverGraph(AgentBaseGraph):
    """Fixed five-node outer graph for interconnect incident handover."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(wire_services(load_runtime_config(config)))

    @property
    def name(self) -> str:
        return "interconnect_incident_handover"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = PreProcessNode(config=self.config)
        self._nodes["main"] = MainNode(config=self.config)
        self._nodes["post_process"] = PostProcessNode(config=self.config)

    def get_output(self, state: dict) -> dict:
        output = super().get_output(state)
        error_log = _public_error_log(state.get("error_log", []))
        if error_log:
            output["error_log"] = error_log
        return output


def _public_error_log(entries: object) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, str):
            continue
        cleaned = entry.split("\nTraceback", 1)[0]
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


Graph = InterconnectIncidentHandoverGraph
