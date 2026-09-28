"""Deterministic helpers for interconnect incident handover processing."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Mapping

from framework.security import detect_pii
from framework.security.pii_masking import mask_pii

from src.services import contracts

IDENTIFIER_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9_-]{2,}\b")
RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
HHMM_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
INCIDENT_REF_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
RECORD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HASH_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


class RequestValidationError(ValueError):
    def __init__(self, error_code: str, *, keys: list[str] | None = None, detail: str | None = None) -> None:
        self.error_code = error_code
        self.keys = keys or []
        self.detail = detail or error_code
        super().__init__(self.detail)


def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RequestValidationError("invalid_json", detail="request body must be a JSON object") from exc
    elif isinstance(value, Mapping):
        parsed = dict(value)
    else:
        raise RequestValidationError("invalid_request_type", detail="request body must be a JSON object")
    if not isinstance(parsed, dict):
        raise RequestValidationError("invalid_request_type", detail="request body must be a JSON object")
    return parsed


def closed_keys(payload: Mapping[str, Any], allowed: set[str], *, reserved: set[str] | None = None) -> None:
    reserved = reserved or set()
    reserved_seen = sorted(key for key in payload if key in reserved)
    if reserved_seen:
        raise RequestValidationError("reserved_key", keys=reserved_seen)
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise RequestValidationError("invalid_request_key", keys=unknown)


def ensure_clean_scope(scope: Any) -> dict[str, str]:
    if not isinstance(scope, Mapping) or set(scope) != set(contracts.SCOPE_KEYS):
        raise ValueError("scope_missing_or_invalid")
    cleaned = {}
    for key in contracts.SCOPE_KEYS:
        value = scope.get(key)
        if not isinstance(value, str) or not value.strip() or value != value.strip() or "\n" in value or "\r" in value:
            raise ValueError("scope_missing_or_invalid")
        cleaned[key] = value
    return cleaned


def mask_framework_pii(text: str) -> tuple[str, int]:
    findings = list(detect_pii(text) or [])
    return mask_pii(text, findings) if findings else text, len(findings)


def normalize_guard_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def contains_instruction_content(text: str) -> bool:
    normalized = normalize_guard_text(text)
    return any(marker.casefold() in normalized for marker in contracts.INSTRUCTION_MARKERS)


def contains_diagnostic_content(text: str) -> bool:
    normalized = normalize_guard_text(text)
    return any(marker.casefold() in normalized for marker in contracts.DIAGNOSTIC_MARKERS)


def quality_reasons(text: Any, *, max_record_chars: int) -> list[str]:
    if not isinstance(text, str):
        return ["record_empty"]
    if text == "":
        return ["record_empty"]
    if len(text) > max_record_chars:
        return ["record_too_long"]
    if text.startswith(("%PDF", "PK\x03\x04", "\x89PNG", "GIF89a", "GIF87a")):
        return ["binary_payload"]
    if not text.strip():
        return ["record_empty"]
    total = max(len(text), 1)
    control = sum(1 for ch in text if (ord(ch) < 32 or 127 <= ord(ch) <= 159) and ch not in "\t\n\r")
    if control / total > 0.01:
        return ["control_char_ratio"]
    replacements = text.count("\ufffd")
    if replacements / total > 0.005:
        return ["mojibake_ratio"]
    readable = sum(
        1
        for ch in text
        if ch.isalnum()
        or "\u3040" <= ch <= "\u30ff"
        or "\u4e00" <= ch <= "\u9fff"
        or ch.isspace()
        or ch in "。、，,.:-_/+()[]{}<>@#%&=・"
    )
    if readable / total < 0.80:
        return ["unreadable_ratio"]
    return []


def parse_aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed


def first_sentence(text: str, *, max_chars: int) -> str:
    cut = len(text)
    for marker in ("。", ".", "\n"):
        index = text.find(marker)
        if index >= 0:
            cut = min(cut, index + len(marker))
    return text[: min(cut, max_chars)].strip()


def substantial_quote_text(quote: str, *, min_quote_chars: int) -> bool:
    remainder = quote.replace("[MASKED]", "").strip()
    return len(quote) >= min_quote_chars and len(remainder) >= min_quote_chars


def identifier_tokens(text: str) -> list[str]:
    tokens = []
    for match in IDENTIFIER_RE.finditer(text):
        token = match.group(0)
        if any(ch.isalpha() for ch in token) and any(ch.isdigit() for ch in token):
            tokens.append(token)
    return tokens


def token_set(text: str) -> set[str]:
    return set(identifier_tokens(text))


def quote_in_any_source(
    quote: str,
    source_ids: list[str],
    *,
    canonical_by_source: Mapping[str, str],
    min_quote_chars: int,
    max_quote_chars: int,
) -> str | None:
    if not isinstance(quote, str):
        return "quote_not_verbatim"
    if len(quote) < min_quote_chars:
        return "quote_too_short"
    if len(quote) > max_quote_chars:
        return "quote_too_long"
    if not substantial_quote_text(quote, min_quote_chars=min_quote_chars):
        return "quote_masked_only"
    for source_id in source_ids:
        text = canonical_by_source.get(source_id, "")
        if quote and quote in text:
            return None
    return "quote_not_verbatim"


def build_fact_layer(
    records: list[dict[str, Any]],
    *,
    min_quote_chars: int,
    max_quote_chars: int,
    check_action_markers: list[str],
    counterpart_label: str,
) -> dict[str, Any]:
    sorted_records = sorted(records, key=lambda item: (parse_aware_datetime(item["recorded_at"]), item["record_id"]))
    source_registry = []
    source_by_record_id = {}
    for index, record in enumerate(sorted_records, start=1):
        source_id = f"S-{index:03d}"
        source_by_record_id[record["record_id"]] = source_id
        text = record.get("canonical_text", "")
        source_registry.append(
            {
                "source_id": source_id,
                "record_id": record["record_id"],
                "record_kind": record["record_kind"],
                "recorded_at": record["recorded_at"],
                "char_offset_range": [0, len(text)],
            }
        )

    short_records: list[str] = []
    timeline = []
    for record in sorted_records:
        quote = first_sentence(record.get("canonical_text", ""), max_chars=max_quote_chars)
        if not substantial_quote_text(quote, min_quote_chars=min_quote_chars):
            short_records.append(record["record_id"])
            continue
        timeline.append(
            {
                "at": record["recorded_at"],
                "quote": quote,
                "source_ids": [source_by_record_id[record["record_id"]]],
            }
        )

    completed_checks = []
    missing_elements = []
    for record in sorted_records:
        if record["record_kind"] != "event_reference":
            continue
        text = record.get("canonical_text", "")
        has_time = bool(RFC3339_RE.search(text) or HHMM_RE.search(text) or record.get("recorded_at"))
        ids = identifier_tokens(text)
        marker = next((item for item in check_action_markers if item.casefold() in text.casefold()), "")
        if not (has_time and ids and marker):
            missing = []
            if not has_time:
                missing.append("timestamp")
            if not ids:
                missing.append("target_identifier")
            if not marker:
                missing.append("check_action_marker")
            missing_elements.append({"record_id": record["record_id"], "missing": missing})
            continue
        completed_checks.append(
            {
                "check_id": f"C-{len(completed_checks) + 1:02d}",
                "quote": first_sentence(text, max_chars=max_quote_chars),
                "source_ids": [source_by_record_id[record["record_id"]]],
            }
        )

    identifier_sources: dict[str, set[str]] = defaultdict(set)
    counts: Counter[str] = Counter()
    for record in sorted_records:
        source_id = source_by_record_id[record["record_id"]]
        for token in identifier_tokens(record.get("canonical_text", "")):
            identifier_sources[token].add(source_id)
            counts[token] += 1
    observed_identifiers = [
        {
            "identifier": identifier,
            "source_ids": sorted(identifier_sources[identifier]),
            "occurrence_count": counts[identifier],
        }
        for identifier in counts
    ]
    observed_identifiers.sort(key=lambda item: (-item["occurrence_count"], item["identifier"]))

    event = {}
    incident_notes = [record for record in sorted_records if record["record_kind"] == "incident_note"]
    no_usable_incident_note = False
    if incident_notes:
        record = incident_notes[0]
        quote = first_sentence(record.get("canonical_text", ""), max_chars=max_quote_chars)
        if substantial_quote_text(quote, min_quote_chars=min_quote_chars):
            event = {"opening_quote": quote, "source_ids": [source_by_record_id[record["record_id"]]]}
        else:
            no_usable_incident_note = True
    else:
        no_usable_incident_note = True

    structured_query = {
        "record_kinds": sorted({record["record_kind"] for record in sorted_records}),
        "completed_check_count": len(completed_checks),
        "observed_identifier_count": len(observed_identifiers),
        "has_incident_note": bool(incident_notes),
    }

    return {
        "source_registry": source_registry,
        "completed_checks": completed_checks,
        "timeline": timeline,
        "observed_identifiers": observed_identifiers,
        "event": event,
        "structured_query": structured_query,
        "structure_quality": {
            "missing_elements": missing_elements,
            "short_records": short_records,
            "no_usable_incident_note": no_usable_incident_note,
        },
        "counterpart_label": counterpart_label,
    }


def canonical_by_source(records: list[dict[str, Any]], source_registry: list[dict[str, Any]]) -> dict[str, str]:
    by_record = {record["record_id"]: record.get("canonical_text", "") for record in records}
    return {source["source_id"]: by_record.get(source["record_id"], "") for source in source_registry}


def source_recorded_at(source_registry: list[dict[str, Any]]) -> dict[str, datetime]:
    return {source["source_id"]: parse_aware_datetime(source["recorded_at"]) for source in source_registry}


def has_grounding_path(
    assertion: Mapping[str, Any],
    check: Mapping[str, Any],
    *,
    canonical_by_sid: Mapping[str, str],
    recorded_at_by_sid: Mapping[str, datetime],
    window_minutes: int,
) -> bool:
    assertion_sources = set(assertion.get("assertion_source_ids") or [])
    check_sources = set(check.get("source_ids") or [])
    if assertion_sources & check_sources:
        return True

    assertion_quote = str(assertion.get("recorded_assertion_quote", ""))
    check_quote = str(check.get("quote", ""))
    if token_set(assertion_quote) & token_set(check_quote):
        return True

    window = timedelta(minutes=window_minutes)
    for assertion_sid in assertion_sources:
        assertion_at = recorded_at_by_sid.get(assertion_sid)
        if assertion_at is None:
            continue
        for check_sid in check_sources:
            check_at = recorded_at_by_sid.get(check_sid)
            if check_at is not None and abs(check_at - assertion_at) <= window:
                return True
    return False


def normalize_query(value: Any) -> str:
    if isinstance(value, Mapping):
        return unicodedata.normalize("NFKC", json.dumps(value, ensure_ascii=False, sort_keys=True))
    return unicodedata.normalize("NFKC", str(value or ""))


def walk_string_leaves(value: Any, path: str = "") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, Mapping):
        out: list[tuple[str, str]] = []
        for key, item in value.items():
            out.extend(walk_string_leaves(item, f"{path}.{key}" if path else str(key)))
        return out
    if isinstance(value, list):
        out = []
        for index, item in enumerate(value):
            out.extend(walk_string_leaves(item, f"{path}[{index}]"))
        return out
    return []


def reject_prohibited_keys(value: Any, path: str = "") -> list[str]:
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_str = str(key)
            current = f"{path}.{key_str}" if path else key_str
            if key_str in contracts.PROHIBITED_OUTPUT_KEYS:
                violations.append(current)
            violations.extend(reject_prohibited_keys(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            violations.extend(reject_prohibited_keys(item, f"{path}[{index}]"))
    return violations
