# TEL-C2-113 — InterconnectIncidentHandoverAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: TEL

## Overview

InterconnectIncidentHandoverAgent turns the scattered, unstructured records written during a
carrier-to-carrier interconnect incident — incident notes, de-identified signalling and event
references, partner contact logs — into a handover draft that follows an approved, version-pinned
template. It is the document the next shift, or the counterpart carrier, actually reads.

The draft carries a timeline, the checks that were completed, the identifiers observed, the
assertions the records support, what is still unresolved, a list of candidate causes to check, and
the questions worth asking next — each anchored to a verbatim quote from a specific source record.

**It does not diagnose.** Root cause, fault location, routing changes, restoration status, incident
closure, SLA judgement and customer-impact confirmation are not things the agent decides — and it
cannot, because the output allowlist has no field that could carry any of them. Nor does it send
anything: the confirmation questions are a draft for a person to review, not a message to a partner.

Candidate causes are the one place where a "why did this happen" list appears, and the design keeps
it enumerative rather than diagnostic **by structure, not by wording**:

- the status field is a single-valued enum, so no candidate can be marked "the likely one";
- there is no rank and no confidence field anywhere in the output allowlist;
- the ordering is fixed deterministically after the model has run, so position carries no meaning;
- every candidate must carry its evidence and a confirmation question, so it always points at work
  to do rather than at a conclusion;
- a fixed disclaimer, checked for exact equality against the configured constant, is attached to
  the list — the model never writes that sentence.

The same principle governs the rest of the output. The language model performs the semantic
structuring; everything a reader would treat as a fact is verified deterministically afterwards:

- **Every quote is checked against the source record verbatim.** A quote that is not literal, too
  short (a one-character quote makes "is it in the text" vacuously true), too long (an upper bound
  stops the whole record body riding out disguised as a quote), or attributed to an unknown source
  id is dropped and the reason recorded.
- **An assertion is only marked as supported when a completed check actually grounds it** — by
  shared identifier, by explicit reference, or by a check recorded within a configured window of
  the assertion's own source record. Otherwise the support status is downgraded, visibly.
- **The reference instant is fixed once, at the entry adapter.** No node calls `datetime.now()`, so
  a UTC-hosted container and a JST-hosted one produce the same result for the same input.
- **A degraded run says so.** `degradation.reason_codes` distinguishes a template-version mismatch,
  a rejected input, an unavailable model, a failed provenance check and an unresolved knowledge-base
  scope, and `requires_human_review` is set rather than the result quietly looking complete.

Past review perspectives accumulate: non-diagnostic observations confirmed by a person are stored
and recalled on later incidents as things to check. What is deliberately **not** accumulated is the
handover template itself — it is a controlled document the organisation versions and distributes,
supplied per request as `ref` + `version` + `content_hash` and resolved against the approved copy,
because "which version was applied" is exactly what an audit asks.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to your
own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- a `BaseLLM` implementation through `config["llm"]`. It is **optional**: without one the
  deterministic layer still produces the sources, timeline, completed checks and observed
  identifiers, and the run comes back with `llm_unavailable` in `degradation.reason_codes` and
  `requires_human_review: true`. The bundled adapters build an Azure OpenAI client when
  `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_DEPLOYMENT` all resolve through
  the secrets provider **and** the installed framework wheel actually ships that client; otherwise
  they fall back to the same degraded mode rather than failing per call;
- the approved handover template, registered so the agent can resolve and hash-check the pin a
  request declares;
- durable knowledge-base and database implementations. The shipped ones are **in-memory** (see
  *Limitations*);
- caller authentication in front of the entry adapter, and the counterpart-carrier partition key —
  see *Input*.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `default_business_timezone` (`Asia/Tokyo`) / `default_counterpart_label` (`unspecified`) | Applied when the request omits `context` |
| `max_records` (`200`) / `max_record_chars` (`8000`) | Input size limits |
| `max_control_char_ratio` (`0.01`) / `max_replacement_char_ratio` (`0.005`) / `min_readable_char_ratio` (`0.80`) | Text quality gate — rejects mojibake and broken paste |
| `min_quote_chars` (`8`) / `max_quote_chars` (`300`) | Verbatim-quote bounds |
| `max_question_chars` (`120`) / `max_hypothesis_chars` (`160`) | Length caps on generated free text |
| `max_candidate_causes` (`5`) | Ceiling on the enumerated candidate list |
| `candidate_causes_disclaimer` | The fixed sentence attached to the candidate list; the output gate requires exact equality |
| `check_action_markers` | Vocabulary that, together with a timestamp and a target identifier in the *same* record, makes it a completed check. Calibrated per adopter at onboarding |
| `corroboration_window_minutes` (`120`) | How close in time a check must be to count as time co-reference for grounding |
| `recall_top_k` (`5`) | Past perspectives recalled per request |
| `payload_ttl_s` (`900`) | Lifetime of the transient store holding record bodies |
| `max_retry` (`3`) / `timeout_s` (`30`) | Execution limits |
| `memory_enabled` (`false`) / `hitl.enabled` (`false`) | No checkpointer is attached; the graph never suspends |

Every key above is validated at start-up — a declared key the code never reads is not allowed to
exist, so the configuration cannot become decorative.

The template does not fetch records, parse PDF, spreadsheet or binary files, perform OCR, or send
anything to a partner. Input is text.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | Required for production deployment and durable service integrations. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework package must be installed — `src/api/server.py` imports `framework.*` and `shared.*`
at module load, so without it the server does not start at all. With the framework present, the
standalone adapter starts without a platform connection, and three things then differ:

- **The knowledge base and database are in-memory.** Nothing survives a restart and nothing is
  shared between processes. This is for local validation and staging checks.
- **No language model is configured** unless the three Azure OpenAI values resolve *and* the
  installed wheel ships the Azure client. Either condition failing is a logged, reported
  `llm_unavailable` degradation, not a crash and not a per-call `ImportError`.
- **Authentication has to be configured before any caller is admitted.** Identity comes from
  upstream middleware, or from a bearer token: `INVOKE_AUTH_TOKEN` promotes a caller to
  verified-external, and the separate `STG_INTERNAL_RUNNER_TOKEN` to internal. With a token
  configured, a wrong one is a 401; with none configured, the caller stays anonymous and is refused
  by the trust gate.

`/health` is unauthenticated and returns `{"status": "ok", "agent": "InterconnectIncidentHandoverAgent"}`.
The test suite runs without a platform connection and without a language model.

## Input

All request bodies are **closed schemas**: an undeclared key is a 422 with an
`invalid_request_key` code, and a reserved internal key (`payload_ref`, `canonical_payload_ref`) is
rejected as `reserved_key` rather than ignored.

| Endpoint | Purpose | Body |
|---|---|---|
| `POST /invoke` | Draft a handover for one incident | `incident_ref`, `handover_template` (`ref`, `version`, `content_hash` as `sha256:<64 hex>`), `records[]` of `{record_id, record_kind, recorded_at, text}` (1–200), optional `context` |
| `POST /ingest` | Accumulate non-diagnostic past review perspectives | `perspectives[]` of `{record_id, perspective_kind, perspective_text, source_ref}` (1–200), optional `context` |
| `POST /feedback` | Record a person's decision on a stored perspective | `source_record_id`, `decision` (`confirm` / `revise` / `retire`), `rationale_ref`, `revised_perspective_text` (required for and only for `revise`), optional `context` |

`record_kind` is one of `incident_note`, `event_reference`, `partner_contact_log`.
`perspective_kind` is one of `unsupported_assertion`, `missing_check`, `ambiguous_correlation`,
`missing_timestamp`, `counterpart_input_pending` — the set is deliberately non-diagnostic.
`context` carries only `business_timezone` and `counterpart_label`.

`/invoke` is read-only and reachable by a verified-external caller. **`/ingest` and `/feedback`
require internal trust** — they write to the accumulated knowledge base, and a verified-external
caller is refused with 403. Every request is partitioned by a `counterpart_carrier_id` scope key
that comes from verified request state, never from the request body.

Record bodies never enter the graph state: the entry adapter puts them into a short-lived,
scope-bound store and passes a reference instead.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "incident_ref": "INC-2026-0817-014",
    "handover_template": {
      "ref": "HO-TMPL-INTERCONNECT",
      "version": "3.2",
      "content_hash": "sha256:739b93fd71b67d392ed8824364c89b88f91e3cf0323e7bbf2eb355ccf2a1aa3b"
    },
    "records": [
      {
        "record_id": "REC-001",
        "record_kind": "incident_note",
        "recorded_at": "2026-08-17T12:40:00+09:00",
        "text": "GW-TKY-03 で SIP 503 が増加。相手確認待ち"
      }
    ]
  }'
```

## Output

`output` carries `mode` and `gate_kind`, plus the fields for the mode:

| Mode | `gate_kind` | Additional fields |
|---|---|---|
| `invoke` | `handover_draft` | `incident_ref`, `template_ref`, `template_version`, `handover_draft`, `evidence_map`, `requires_human_review`, `degradation` |
| `ingest` | `ingest_summary` | `ingest_result` (`written_ids`, `rejected`) |
| `feedback` | `feedback_summary` | `feedback_result` |

`handover_draft` holds `sources`, `timeline`, `completed_checks`, `observed_identifiers`,
`assertions`, `unresolved`, `candidate_causes`, `candidate_causes_disclaimer`,
`confirmation_questions` and `assessment_complete`.

```json
{
  "mode": "invoke",
  "gate_kind": "handover_draft",
  "incident_ref": "INC-2026-0817-014",
  "template_ref": "HO-TMPL-INTERCONNECT",
  "template_version": "3.2",
  "handover_draft": {
    "sources": [],
    "timeline": [],
    "completed_checks": [],
    "observed_identifiers": [],
    "assertions": [],
    "unresolved": [],
    "candidate_causes": [],
    "confirmation_questions": [],
    "assessment_complete": false
  },
  "requires_human_review": true,
  "degradation": {"reason_codes": ["llm_unavailable"], "detail_codes": []}
}
```

The example above is a fully degraded run — a real one carries content in every list.
`degradation.reason_codes` is drawn from `template_version_mismatch`, `input_quality_rejected`,
`llm_unavailable`, `provenance_verification_failed` and `kb_scope_unresolved`; `detail_codes`
records exactly which element was dropped and why (`quote_not_verbatim`, `quote_too_short`,
`quote_too_long`, `unknown_source_id`, `unknown_check_id`, `support_status_downgraded`,
`question_target_missing`, `enum_value_rejected`, `llm_output_unparsable`, `candidate_dropped`,
`quote_masked_only`). A rejected input record reports its own reason — `record_too_long`,
`record_empty`, `control_char_ratio`, `mojibake_ratio`, `unreadable_ratio`, `binary_payload` or
`duplicate_record_id` — per record, so one bad row does not fail the request.

## Security and Limitations

Every node requires verified-external trust, and the two writing endpoints require internal trust
on top of that. Personal information is masked with the framework detector before the record text
reaches the model, and the output is re-scanned on the way out; a quote that survives only in masked
form is dropped rather than published, and the reason is recorded.

The output allowlist is the primary defence, not a list of forbidden words. Diagnostic and decision
fields — `root_cause`, `diagnosis`, `fault_location`, `restoration_status`, `incident_closed`,
`recommended_action`, `routing_change`, `sla_breach`, `customer_impact_confirmed`,
`partner_notification_sent`, `approval`, `verdict`, `most_likely_cause` and the rest — are refused
structurally. A word denylist is deliberately not implemented: paraphrase defeats it, and having
one invites the belief that the wording is what makes the output non-conclusive.

Free text stored in the knowledge base is split so that the model-visible projection is a separate,
bounded value rather than a filtered copy of the original — knowledge-base text rides into the next
request's prompt, so it is a persistent injection path that masking alone does not close. Text that
reads as an instruction, or that carries a diagnostic conclusion, is rejected at ingest rather than
stored.

Known limits, recorded rather than hidden:

- **The shipped knowledge base and database are in-memory.** The framework's durable storage
  services are not yet available; the injection seams are where a real backend connects.
- **Recall is substring-based in the interim backend.** Japanese text is matched on character
  bigrams rather than whitespace tokens, because whitespace splitting makes an entire Japanese
  sentence one token and recalls nothing. This is a stand-in until a full-text or vector backend is
  connected.
- **Past outcomes are recalled but never inherited.** A recalled perspective contributes its text
  and source id to the prompt; the earlier support status and conclusion are excluded, so a claim
  cannot become supported because a previous incident said so.
- **A quote proves provenance, not correctness.** It shows the draft is anchored to text a record
  actually contained; it does not establish that the record was right.
- **`assessment_complete` is a draft flag, not a completion judgement.** The draft is a starting
  point for a person, and `requires_human_review` marks the runs where that matters most.

Output is machine-generated and assistive. It does not substitute for expert review, and no
operational decision — diagnosis, restoration, notification — should be taken from it without a
person checking the primary records.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2) with an inner workflow graph over three modes
- Generation mode: `llm` — semantic structuring only, with a deterministic fallback
- HITL: not enabled — the graph never suspends; human confirmation is an ordinary `feedback` request
- Persistence: in-memory knowledge base and database; no checkpointer
- External API clients: an Azure OpenAI client is constructed when the three connection values resolve and the installed wheel ships that client; otherwise none

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection and without a language model, using a fake client and
in-memory backends. Running the agent itself needs authentication configured — see *Behaviour
without the platform* above.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the deterministic extraction rules, the
grounding and provenance contracts, the data-governance rules and the security design.
`docs/03_test_spec.md` records the test specification and the verification results, including a
live-model run and the platform-level findings it produced.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
