# PB-6: Invoke Execution Order and S-1 Denial Verification
# Verifies BaseNode.__call__() enforces the authorised path: S-1 trust gate ->
# S-4 node_start -> S-2 _security_gate_input() -> execute() -> S-3
# _security_gate_output() -> S-4 node_complete, for every concrete node under
# src/nodes/. It also verifies an under-privileged caller is denied at S-1
# before execute() or normal lifecycle events can run.

import importlib
import inspect
import pkgutil
from typing import ClassVar

import pytest
from framework.nodes.base_node import BaseNode
from framework.schemas.trust_level import TrustLevel
from src.services.contracts import APPROVED_TEMPLATE_REF, APPROVED_TEMPLATE_VERSION
from src.services.wiring import load_runtime_config


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError as exc:
        pytest.fail(f"PB-6 cannot import src.nodes; framework/template setup is broken: {exc}")

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


class TestInvokeOrder:
    """PB-6: __call__ must run S-1 -> node_start -> S-2 -> execute() -> S-3 -> node_complete."""

    def test_s1_denial_refuses_execution_before_execute(self, monkeypatch):
        """TC-08: an always-present privileged node proves the negative S-1 path."""
        import framework.nodes.base_node as base_node_module

        events: list[str] = []
        execute_calls: list[object] = []
        monkeypatch.setattr(
            base_node_module,
            "emit_trace_event",
            lambda event_type, _payload, _state: events.append(event_type),
        )
        original_execute = _PrivilegedTrustGateFixture.execute

        def spy_execute(self, state):
            execute_calls.append(state)
            return original_execute(self, state)

        monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
        result = _PrivilegedTrustGateFixture()(
            {
                "caller_trust_level": _trust_predecessor(
                    _PrivilegedTrustGateFixture.required_trust_level
                ).value,
                "correlation_id": "tc08-s1-denial",
            }
        )

        assert result["status"] == "error"
        assert "S-1 trust gate denied" in result["error_log"][0]
        assert events == ["s1_denied"]
        assert not execute_calls

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        assert len(node_classes) >= 9, "PB-6 collected too few nodes — the src.nodes scan is broken"

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, payload, _state, _o=order, _node=node_cls.__name__: (
                    _o.append(f"event:{event_type}") if isinstance(payload, dict) and payload.get("node") == _node else None
                ),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
                "session_id": "pb6-session",
                "trace_id": "pb6-trace",
                "thread_id": "pb6-thread",
                "hitl_allowed": False,
                "mode": "invoke",
                "status": "pending",
                "error_log": [],
                "user_input": "{}",
                "input_context": {
                    "mode": "invoke",
                    "request_scope": {"counterpart_carrier_id": "cp-pb6"},
                },
                "request_scope": {"counterpart_carrier_id": "cp-pb6"},
                "validated_input": {
                    "incident_ref": "INC-2026-0817-014",
                    "counterpart_label": "unspecified",
                    "business_timezone": "Asia/Tokyo",
                },
                "template_check": {
                    "ok": True,
                    "template_ref": APPROVED_TEMPLATE_REF,
                    "template_version": APPROVED_TEMPLATE_VERSION,
                },
                "input_quality": {"ok": True, "accepted_count": 0, "rejected": []},
                "degradation": {"reason_codes": [], "detail_codes": []},
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
                },
                "evidence_map": [],
                "requires_human_review": False,
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)
