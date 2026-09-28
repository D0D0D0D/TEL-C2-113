"""State schema for TEL-C2-113."""

from __future__ import annotations

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState, total=False):
    """Flat msgpack-safe state.

    Free-form record bodies are never stored here; only opaque payload refs and
    structured primitive/list/dict values are allowed.
    """

    mode: str
    incident_ref: str
    request_scope: dict
    counterpart_carrier_id: str
    canonical_payload_ref: str
    validated_input: dict
    template_check: dict
    input_quality: dict
    handover_draft: dict
    evidence_map: list[dict]
    unresolved: list[dict]
    requires_human_review: bool
    degradation: dict
    ingest_result: NotRequired[dict]
    feedback_result: NotRequired[dict]


class InnerWorkflowState(State, total=False):
    """State used by the inner BaseGraph."""

    source_registry: list[dict]
    completed_checks: list[dict]
    timeline: list[dict]
    observed_identifiers: list[dict]
    event: dict
    structured_query: dict
    structure_quality: dict
    recalled_perspectives: list[dict]
    prior_ids: list[str]
    draft_candidate: dict
    cited_source_ids: list[str]
