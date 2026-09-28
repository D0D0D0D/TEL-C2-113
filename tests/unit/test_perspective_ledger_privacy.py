from __future__ import annotations

import json

import pytest

from framework.security import detect_pii
from src.nodes.inner.feedback_node import FeedbackNode
from src.nodes.inner.ingest_node import IngestNode
from src.services.payload_store import build_payload_store
from src.services.perspective_ledger import InMemoryPerspectiveLedger


SCOPE = {"counterpart_carrier_id": "cp-ledger"}
PII_TEXT = "担当者: 山田太郎 noc@example.jp 03-1234-5678 の確認待ち"


def _assert_no_raw_text(record: dict) -> None:
    assert "perspective_text_raw" not in record
    assert "revised_text_raw" not in record
    assert not any(key.endswith("_raw") for key in record)


def _assert_persistent_text_is_sanitized(record: dict) -> None:
    for key in ("perspective_text_audit", "perspective_text_safe"):
        value = record[key]
        assert isinstance(value, str)
        assert not list(detect_pii(value) or [])
        assert "山田太郎" not in value
        assert "noc@example.jp" not in value
        assert "03-1234-5678" not in value


def _node_config_and_state(payload: dict) -> tuple[dict, dict, InMemoryPerspectiveLedger]:
    ledger = InMemoryPerspectiveLedger()
    payload_store = build_payload_store({"payload_ttl_s": 900})
    session_id = "ledger-privacy-session"
    payload_ref = payload_store.put(json.dumps(payload, ensure_ascii=False), scope=SCOPE, session_id=session_id)
    state = {
        "session_id": session_id,
        "canonical_payload_ref": payload_ref,
        "request_scope": SCOPE,
    }
    return {"payload_store": payload_store, "perspective_ledger": ledger}, state, ledger


def test_ingest_ledger_writes_only_audit_and_safe_text_values():
    ledger = InMemoryPerspectiveLedger()

    record_id = ledger.write_perspective(
        scope=SCOPE,
        perspective_kind="missing_check",
        perspective_text=PII_TEXT,
        source_ref="HO-2026-0817-014",
        provenance={"source": "unit"},
    )

    record = ledger.audit_records()[0]
    assert record["record_id"] == record_id
    _assert_no_raw_text(record)
    _assert_persistent_text_is_sanitized(record)
    hits = ledger.search(scope=SCOPE, query="missing_check", top_k=1)
    assert hits[0]["summary"] == record["perspective_text_safe"]
    assert not list(detect_pii(hits[0]["summary"]) or [])


def test_ingest_node_persists_only_ledger_owned_sanitized_values():
    config, state, ledger = _node_config_and_state(
        {
            "perspectives": [
                {
                    "record_id": "PSP-PII-001",
                    "perspective_kind": "missing_check",
                    "perspective_text": PII_TEXT,
                    "source_ref": "HO-2026-0817-014",
                }
            ]
        }
    )

    result = IngestNode(config=config).execute(state)

    assert result["status"] == "success"
    assert result["ingest_result"]["rejected"] == []
    record = ledger.audit_records()[0]
    _assert_no_raw_text(record)
    _assert_persistent_text_is_sanitized(record)


def test_feedback_ledger_writes_only_audit_and_safe_text_values():
    ledger = InMemoryPerspectiveLedger()

    result = ledger.append_feedback(
        scope=SCOPE,
        source_record_id="perspective_000001",
        decision="revise",
        decided_by="operator-1",
        decided_at="2026-08-20T12:00:00+09:00",
        rationale_ref="HO-2026-0817-014",
        revised_perspective_text=PII_TEXT,
    )

    record = ledger.audit_records()[0]
    assert record["record_id"] == result["record_id"]
    _assert_no_raw_text(record)
    _assert_persistent_text_is_sanitized(record)


def test_feedback_node_persists_only_ledger_owned_sanitized_values():
    config, state, ledger = _node_config_and_state({"feedback": {"revised_perspective_text": PII_TEXT}})
    state["validated_input"] = {
        "source_record_id": "perspective_000001",
        "decision": "revise",
        "decided_by": "operator-1",
        "decided_at": "2026-08-20T12:00:00+09:00",
        "rationale_ref": "HO-2026-0817-014",
    }

    result = FeedbackNode(config=config).execute(state)

    assert result["status"] == "success"
    record = ledger.audit_records()[0]
    _assert_no_raw_text(record)
    _assert_persistent_text_is_sanitized(record)


def test_ledger_callers_cannot_label_text_as_audit_or_safe():
    ledger = InMemoryPerspectiveLedger()

    with pytest.raises(TypeError):
        ledger.write_perspective(
            scope=SCOPE,
            perspective_kind="missing_check",
            perspective_text_audit=PII_TEXT,
            perspective_text_safe=PII_TEXT,
            source_ref="HO-2026-0817-014",
            provenance={"source": "unit"},
        )
    with pytest.raises(TypeError):
        ledger.append_feedback(
            scope=SCOPE,
            source_record_id="perspective_000001",
            decision="revise",
            decided_by="operator-1",
            decided_at="2026-08-20T12:00:00+09:00",
            rationale_ref="HO-2026-0817-014",
            revised_text_audit=PII_TEXT,
            revised_text_safe=PII_TEXT,
        )
