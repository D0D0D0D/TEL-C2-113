from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.server import app, example_template_pin


def _invoke_body() -> dict:
    return {
        "incident_ref": "INC-2026-0817-014",
        "handover_template": example_template_pin(),
        "records": [
            {
                "record_id": "REC-001",
                "record_kind": "incident_note",
                "recorded_at": "2026-08-17T12:40:00+09:00",
                "text": "GW-TKY-03 で SIP 503 が増加。相手確認待ち",
            }
        ],
    }


def test_startup_wires_services_and_invoke_without_context_uses_defaults(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_COUNTERPART_CARRIER_ID", "cp-api")
    with TestClient(app) as client:
        response = client.post("/invoke", json=_invoke_body(), headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    draft = body["output"]["handover_draft"]
    assert draft["counterpart_label"] == "unspecified"
    assert body["output"]["mode"] == "invoke"


def test_invoke_rejects_unknown_context_key_422(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_COUNTERPART_CARRIER_ID", "cp-api")
    body = _invoke_body()
    body["context"] = {"business_timezone": "Asia/Tokyo", "unknown": "x"}
    with TestClient(app) as client:
        response = client.post("/invoke", json=body, headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_request_key"


def test_ingest_requires_internal_trust(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_COUNTERPART_CARRIER_ID", "cp-api")
    body = {
        "perspectives": [
            {
                "record_id": "PSP-001",
                "perspective_kind": "missing_check",
                "perspective_text": "切り戻し時刻の記録が不足しやすい",
                "source_ref": "HO-2026-0611-002",
            }
        ]
    }
    with TestClient(app) as client:
        response = client.post("/ingest", json=body, headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 403


def test_ingest_internal_runner_and_diagnostic_reject(monkeypatch):
    monkeypatch.setenv("STG_INTERNAL_RUNNER_TOKEN", "mock-internal-token")
    monkeypatch.setenv("STG_DEFAULT_COUNTERPART_CARRIER_ID", "cp-api")
    body = {
        "perspectives": [
            {
                "record_id": "PSP-001",
                "perspective_kind": "missing_check",
                "perspective_text": "原因は相手側装置です",
                "source_ref": "HO-2026-0611-002",
            }
        ]
    }
    with TestClient(app) as client:
        response = client.post("/ingest", json=body, headers={"authorization": "Bearer mock-internal-token"})

    assert response.status_code == 200
    rejected = response.json()["output"]["ingest_result"]["rejected"]
    assert rejected[0]["reason_code"] == "diagnostic_content"


def test_feedback_rejects_revise_without_text_422(monkeypatch):
    monkeypatch.setenv("STG_INTERNAL_RUNNER_TOKEN", "mock-internal-token")
    monkeypatch.setenv("STG_DEFAULT_COUNTERPART_CARRIER_ID", "cp-api")
    body = {"source_record_id": "PSP-001", "decision": "revise", "rationale_ref": "HO-2026-0817-014"}
    with TestClient(app) as client:
        response = client.post("/feedback", json=body, headers={"authorization": "Bearer mock-internal-token"})

    assert response.status_code == 422
