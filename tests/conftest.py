from __future__ import annotations

import pytest

from src.services.contracts import APPROVED_TEMPLATE_HASH, APPROVED_TEMPLATE_REF, APPROVED_TEMPLATE_VERSION


@pytest.fixture
def case_payload() -> dict:
    """Valid live-LLM invoke payload used by orchestrator-side smoke checks."""

    return {
        "incident_ref": "INC-2026-0817-014",
        "handover_template": {
            "ref": APPROVED_TEMPLATE_REF,
            "version": APPROVED_TEMPLATE_VERSION,
            "content_hash": APPROVED_TEMPLATE_HASH,
        },
        "records": [
            {
                "record_id": "REC-001",
                "record_kind": "incident_note",
                "recorded_at": "2026-08-17T12:40:00+09:00",
                "text": "GW-TKY-03 で SIP 503 増加\n相手側ログはまだ未着、利用者影響は未確認。",
            },
            {
                "record_id": "REC-002",
                "record_kind": "event_reference",
                "recorded_at": "2026-08-17T12:50:00+09:00",
                "text": "12:50 JST GW-TKY-03 疎通確認 test 実施 verified ただしTG-INT-001側は応答遅延。",
            },
            {
                "record_id": "REC-003",
                "record_kind": "partner_contact_log",
                "recorded_at": "2026-08-17T13:05:00+09:00",
                "text": "Partner call 13:05 相手NOCはTG-INT-001の経路変更なしと回答、GW-TKY-03の503継続は確認中",
            },
        ],
        "context": {"business_timezone": "Asia/Tokyo", "counterpart_label": "PARTNER-A"},
    }


@pytest.fixture
def live_input_context() -> dict:
    return {
        "mode": "invoke",
        "request_scope": {"counterpart_carrier_id": "cp-live-check"},
        "caller_id": "live-check-caller",
    }
