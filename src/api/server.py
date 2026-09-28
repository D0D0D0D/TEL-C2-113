"""Standalone HTTP adapter for TEL-C2-113."""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError as FastAPIRequestValidationError
from fastapi.responses import JSONResponse
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, ConfigDict, Field, model_validator

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_config
from shared.secrets import factory as secrets_factory
from src.entry_adapter import Operation, authenticate
from src.graph.graph import Graph
from src.services.azure_llm import REQUIRED_SECRET_KEYS, LazyAzureOpenAILLM, client_available
from src.services.contracts import (
    AGENT_NAME,
    APPROVED_TEMPLATE_HASH,
    APPROVED_TEMPLATE_REF,
    APPROVED_TEMPLATE_VERSION,
)
from src.services.wiring import wire_services

app = FastAPI(title=AGENT_NAME)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
_config = load_config(str(_CONFIG_PATH)) if _CONFIG_PATH.exists() else {}

_secrets_provider = secrets_factory(namespace="tel", agent_name=AGENT_NAME)
# Both entry paths (this HTTP adapter and cli.py's Marketplace adapter) use the
# same Azure client. Only presence is checked here — the values are resolved per
# call inside LazyAzureOpenAILLM, so no credential is held on a module global.
# `client_available()` も見る理由: wheel の版によっては azure_openai_client
# モジュールが無い。接続値が揃っていても import で落ちるため、ここで判定して
# 既存の `llm=None` 縮退契約へ落とす（呼び出しごとの ImportError にしない）。
_llm = None
if not all(_secrets_provider.get(key) for key in REQUIRED_SECRET_KEYS):
    logging.getLogger(__name__).warning(
        "Azure OpenAI config incomplete (%s); TEL-C2-113 will use deterministic fallback.",
        ", ".join(REQUIRED_SECRET_KEYS),
    )
elif not client_available():
    logging.getLogger(__name__).warning(
        "Azure OpenAI config present but AzureOpenAIClient is absent from the installed "
        "agentcore wheel (registry build); TEL-C2-113 will use deterministic fallback."
    )
else:
    _llm = LazyAzureOpenAILLM()
_config["llm"] = _llm
wire_services(_config)

agent = Graph(config=_config)
_hitl_enabled = agent.config.get("hitl", {}).get("enabled", False)
_needs_checkpointer = agent.config.get("memory_enabled") or _hitl_enabled
agent.compile(checkpointer=MemorySaver() if _needs_checkpointer else None)
agent.provision_secrets(_secrets_provider)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestContext(StrictModel):
    business_timezone: str = Field(default="Asia/Tokyo")
    counterpart_label: str = Field(default="unspecified")


class HandoverTemplate(StrictModel):
    ref: str
    version: str
    content_hash: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")


class InvokeRecord(StrictModel):
    record_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    record_kind: Literal["incident_note", "event_reference", "partner_contact_log"]
    recorded_at: str
    text: str = Field(min_length=1, max_length=8000)


class InvokeRequest(StrictModel):
    incident_ref: str = Field(pattern=r"^[A-Za-z0-9_-]{4,64}$")
    handover_template: HandoverTemplate
    records: list[InvokeRecord] = Field(min_length=1, max_length=200)
    context: RequestContext | None = None


class IngestContext(StrictModel):
    business_timezone: str = Field(default="Asia/Tokyo")


class PerspectiveRow(StrictModel):
    record_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    perspective_kind: Literal[
        "unsupported_assertion",
        "missing_check",
        "ambiguous_correlation",
        "missing_timestamp",
        "counterpart_input_pending",
    ]
    perspective_text: str = Field(min_length=1, max_length=8000)
    source_ref: str


class IngestRequest(StrictModel):
    perspectives: list[PerspectiveRow] = Field(min_length=1, max_length=200)
    context: IngestContext | None = None


class FeedbackRequest(StrictModel):
    source_record_id: str
    decision: Literal["confirm", "revise", "retire"]
    revised_perspective_text: str | None = None
    rationale_ref: str
    context: IngestContext | None = None

    @model_validator(mode="after")
    def validate_revise_text(self) -> "FeedbackRequest":
        if self.decision == "revise" and self.revised_perspective_text is None:
            raise ValueError("revised_perspective_text is required when decision=revise")
        if self.decision != "revise" and self.revised_perspective_text is not None:
            raise ValueError("revised_perspective_text is allowed only when decision=revise")
        return self


@app.on_event("startup")
async def startup() -> None:
    wire_services(agent.config)


@app.exception_handler(FastAPIRequestValidationError)
async def validation_exception_handler(_request: Request, exc: FastAPIRequestValidationError):
    keys = []
    for error in exc.errors():
        loc = [str(item) for item in error.get("loc", []) if item != "body"]
        if loc:
            keys.append(".".join(loc))
    reserved = [key for key in keys if key.split(".")[-1] in {"payload_ref", "canonical_payload_ref"}]
    return JSONResponse(
        status_code=422,
        content={
            "error_code": "reserved_key" if reserved else "invalid_request_key",
            "keys": reserved or sorted(set(keys)),
        },
    )


def _bearer_matches(supplied: str, expected: str) -> bool:
    return secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode())


def _resolve_standalone_trust(
    current: TrustLevel, authorization: str, invoke_auth_token: str | None, internal_runner_token: str | None
) -> TrustLevel:
    if current is not TrustLevel.ANONYMOUS:
        return current
    if internal_runner_token and _bearer_matches(authorization, internal_runner_token):
        return TrustLevel.INTERNAL
    if invoke_auth_token and _bearer_matches(authorization, invoke_auth_token):
        return TrustLevel.VERIFIED_EXTERNAL
    if internal_runner_token or invoke_auth_token:
        raise HTTPException(status_code=401, detail="Token is invalid or expired.")
    return TrustLevel.ANONYMOUS


def _stamp_internal_runner(request: Request) -> None:
    token = os.environ.get("STG_INTERNAL_RUNNER_TOKEN")
    authorization = request.headers.get("authorization", "")
    if not token or not _bearer_matches(authorization, token):
        return
    request.state.trust_level = TrustLevel.INTERNAL
    request.state.caller_id = "stg-internal-runner"
    request.state.counterpart_carrier_id = os.environ.get("STG_DEFAULT_COUNTERPART_CARRIER_ID", "")


def _auth(request: Request, operation: Operation):
    _stamp_internal_runner(request)
    context = authenticate(request, operation=operation)
    if operation in {Operation.WRITE_INGEST, Operation.WRITE_FEEDBACK} and context.trust is not TrustLevel.INTERNAL:
        raise HTTPException(status_code=403, detail="Internal caller trust is required.")
    return context


def _invoke_graph(payload: dict[str, Any], request: Request, *, operation: Operation, mode: str) -> dict:
    auth = _auth(request, operation)
    business_timezone = _business_timezone(payload)
    input_context = {
        "mode": mode,
        "request_scope": auth.scope,
        "caller_id": auth.principal.get("caller_id", ""),
    }
    if mode == "feedback":
        input_context["decided_at"] = _now_in_timezone(business_timezone)
    ctx = InvocationContext(
        correlation_id=str(uuid4()),
        session_id=request.headers.get("x-session-id") or str(uuid4()),
        caller_id=auth.principal.get("caller_id", ""),
        caller_trust_level=auth.trust,
        hitl_allowed=False,
    )
    with bound_secrets(agent._secrets_provider):
        return agent.invoke(
            json.dumps(payload, ensure_ascii=False, default=str),
            session_id=ctx.session_id,
            ctx=ctx,
            input_context=input_context,
        )


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    return _invoke_graph(req.model_dump(exclude_none=True), request, operation=Operation.READ_INVOKE, mode="invoke")


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    return _invoke_graph(req.model_dump(exclude_none=True), request, operation=Operation.WRITE_INGEST, mode="ingest")


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request):
    return _invoke_graph(
        req.model_dump(exclude_none=True), request, operation=Operation.WRITE_FEEDBACK, mode="feedback"
    )


@app.get("/health")
def health():
    return {"status": "ok", "agent": AGENT_NAME}


def example_template_pin() -> dict[str, str]:
    return {
        "ref": APPROVED_TEMPLATE_REF,
        "version": APPROVED_TEMPLATE_VERSION,
        "content_hash": APPROVED_TEMPLATE_HASH,
    }


def _business_timezone(payload: dict[str, Any]) -> str:
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    return str(context.get("business_timezone") or agent.config.get("default_business_timezone", "Asia/Tokyo"))


def _now_in_timezone(name: str) -> str:
    return datetime.now(ZoneInfo(name)).isoformat(timespec="seconds")
