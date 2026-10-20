"""Read WorkBuddy desktop and CodeBuddy CLI request logs.

Only whitelisted usage metadata is returned. Message text, tool payloads,
reasoning, titles and credentials are never copied out of a source line.
"""
from dataclasses import dataclass
from math import isfinite

from .core import project_key

SOURCES = ("workbuddy-desktop", "codebuddy-cli")
LABELS = {"workbuddy-desktop": "WorkBuddy 桌面端", "codebuddy-cli": "CodeBuddy CLI"}
MAX_MODEL = 160
MAX_ID = 240
MIN_TIMESTAMP_MS = 946684800000      # 2000-01-01
MAX_TIMESTAMP_MS = 4102444800000     # 2100-01-01
CACHE_FIELDS = ("cache_read_input_tokens", "cached_input_tokens",
                "cache_read_tokens", "prompt_cache_hit_tokens")
BRIDGE_FAILURES = {"failed", "failure", "error", "aborted", "cancelled", "canceled",
                   "timeout", "permission_denied", "authentication_failed",
                   "model_mismatch", "session_mismatch", "execution_failed",
                   "incomplete", "configuration_error"}


@dataclass(frozen=True)
class Record:
    key: str
    source: str
    session_id: str
    request_id: str
    message_id: str | None
    conversation_request_id: str | None
    at: float
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int | None
    credit: float | None
    trace_id: str | None = None
    boundary_duration: float | None = None
    project: str = ""

    def data(self):
        return {
            "key": self.key, "source": self.source, "sessionId": self.session_id,
            "requestId": self.request_id, "messageId": self.message_id,
            "conversationRequestId": self.conversation_request_id, "at": self.at,
            "model": self.model, "input": self.input_tokens, "output": self.output_tokens,
            "cache": self.cached_input_tokens, "cacheKnown": self.cached_input_tokens is not None,
            "credit": self.credit, "creditValid": self.credit is not None,
            "traceId": self.trace_id, "boundaryDuration": self.boundary_duration,
            "project": self.project,
        }


def _text(value, limit):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _count(value):
    """Accept integral JSON numbers only. Booleans and fractions are invalid."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not isfinite(value) or value < 0 or value != int(value):
        return None
    return int(value)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if isfinite(value) else None


def _usage(row):
    provider = row.get("providerData")
    provider = provider if isinstance(provider, dict) else {}
    message = row.get("message")
    message = message if isinstance(message, dict) else {}
    usage = message.get("usage")
    if not isinstance(usage, dict):
        usage = provider.get("usage")
    return provider, message, usage if isinstance(usage, dict) else {}


def parse_record(row, source):
    """Return (Record, reason). reason is None for accepted records.

    messageId is the only confirmed per-request identifier. When it is absent,
    a stable line id is used, and only then a line fingerprint. The whole-turn
    conversationRequestId is never used alone: one turn can contain several
    model calls, so collapsing those would undercount requests. Without
    messageId this parser is deliberately conservative: it keeps distinct
    records separate and documents that the result may overcount rather than
    silently merging a turn.
    """
    if source not in SOURCES:
        return None, "unknown-source"
    if not isinstance(row, dict):
        return None, "invalid-json"
    timestamp = row.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None, "invalid-timestamp"
    if not isfinite(timestamp) or not MIN_TIMESTAMP_MS <= timestamp <= MAX_TIMESTAMP_MS:
        return None, "invalid-timestamp"

    provider, message, usage = _usage(row)
    raw = provider.get("rawUsage")
    raw = raw if isinstance(raw, dict) else {}
    usage_fields = any(field in usage for field in ("input_tokens", "output_tokens"))
    raw_fields = any(field in raw for field in ("prompt_tokens", "completion_tokens"))
    if not usage_fields and not raw_fields:
        # User messages, empty assistant messages and tool results are normal
        # structural records, not malformed usage records.
        return None, "ignored"

    if usage.get("input_tokens") is not None:
        input_tokens = _count(usage.get("input_tokens"))
        if input_tokens is None:
            return None, "invalid-usage"
    else:
        input_tokens = _count(raw.get("prompt_tokens"))
    if usage.get("output_tokens") is not None:
        output_tokens = _count(usage.get("output_tokens"))
        if output_tokens is None:
            return None, "invalid-usage"
    else:
        output_tokens = _count(raw.get("completion_tokens"))
    if input_tokens is None or output_tokens is None:
        return None, "invalid-usage"
    if input_tokens + output_tokens <= 0:
        return None, "empty-usage"

    present = next((field for field in CACHE_FIELDS if field in usage), None)
    if present is not None and usage[present] is not None:
        cached = _count(usage[present])
        if cached is None or cached > input_tokens:
            return None, "invalid-cache"
    else:
        # Explicit rawUsage cache fields are a compatibility fallback. Missing
        # or malformed values stay unknown; they are never treated as zero.
        raw_field = next((field for field in CACHE_FIELDS
                          if field in raw and raw[field] is not None), None)
        value = _count(raw.get(raw_field)) if raw_field else None
        cached = value if value is not None and value <= input_tokens else None
    credit = _number(raw.get("credit"))
    if credit is not None and credit < 0:
        credit = None

    session_id = (_text(row.get("sessionId"), MAX_ID)
                  or _text(provider.get("sessionId"), MAX_ID) or "unknown")
    message_id = _text(provider.get("messageId"), MAX_ID)
    conversation = _text(provider.get("conversationRequestId"), MAX_ID)
    trace = _text(provider.get("traceId"), MAX_ID) or conversation
    if trace:
        trace = trace.removeprefix("trace_") or None
    line_id = _text(row.get("id"), MAX_ID)
    if message_id:
        request_id = message_id
    elif line_id:
        request_id = "line:" + line_id
    else:
        request_id = f"line:{int(timestamp)}:{input_tokens}:{output_tokens}"
    model = (_text(provider.get("model"), MAX_MODEL)
             or _text(provider.get("requestModelId"), MAX_MODEL)
             or _text(message.get("model"), MAX_MODEL) or "unknown")
    record = Record(
        key=source + "\x1f" + session_id + "\x1f" + request_id,
        source=source, session_id=session_id, request_id=request_id,
        message_id=message_id, conversation_request_id=conversation,
        at=float(timestamp) / 1000.0, model=model,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cached_input_tokens=cached, credit=credit, trace_id=trace,
        project=project_key(row.get("cwd")))
    return record, None


def run_outcome(requested, actual, completed, status):
    """Classify one bridge run using only the evidence actually present.

    A successful resume requires both completed=true and status="completed".
    A completed flag with permission_denied/authentication_failed/etc. is a
    failure, never proof of a resume.
    """
    status_name = (_text(status, 40) or "").lower()
    if requested and actual and requested != actual:
        return "mismatch"
    if status_name in BRIDGE_FAILURES:
        return "failed"
    if not actual:
        return "pending" if status_name in ("", "pending", "running", "queued") else "unknown"
    if requested and requested != actual:
        return "mismatch"
    if requested:
        return "resumed" if completed and status_name == "completed" else "unconfirmed"
    return "new" if completed and status_name == "completed" else "unknown"


def session_summary(runs):
    """Aggregate bridge evidence for one actual CLI session ID.

    A successful resume requires completed=true and requested == actual.
    A run that only created a session is reported as new, never as a resume.
    """
    if not runs:
        return None
    ordered = sorted(runs, key=lambda item: (item.get("orderAt") or 0, item.get("runId") or ""),
                     reverse=True)
    latest = ordered[0]
    confirmed = sum(item["outcome"] == "resumed" for item in runs)
    matched = sum(bool(item.get("requested")) and item.get("requested") == item.get("actual")
                  for item in runs)
    new = sum(item["outcome"] == "new" for item in runs)
    mismatch = sum(item["outcome"] == "mismatch" for item in runs)
    failed = sum((_text(item.get("status"), 40) or "").lower() in BRIDGE_FAILURES
                 or item["outcome"] == "failed" for item in runs)
    unconfirmed = sum(item["outcome"] == "unconfirmed" for item in runs)
    pending = sum(item["outcome"] == "pending" for item in runs)
    unknown = sum(item["outcome"] == "unknown" for item in runs)
    if mismatch:
        outcome = "mismatch"
    elif failed:
        outcome = "failed"
    elif confirmed:
        outcome = "resumed"
    elif runs and new == len(runs):
        outcome = "new"
    elif runs and pending == len(runs):
        outcome = "pending"
    else:
        outcome = "unknown"
    requested_values = []
    for item in ordered:
        value = _text(item.get("requested"), MAX_ID)
        if value and value not in requested_values:
            requested_values.append(value)
    evidence = []
    # Each paged request carries this summary. Bound repeated evidence so a
    # 100-row Native Messaging response also fits with maximum-length IDs.
    for item in ordered[:3]:
        evidence.append({
            "runId": item.get("runId"), "requested": item.get("requested"),
            "actual": item.get("actual"), "status": item.get("status"),
            "completed": bool(item.get("completed")), "outcome": item.get("outcome"),
            "observedModels": (item.get("observedModels") or [])[:3],
        })
    return {
        "outcome": outcome,
        "actualSessionId": next((item.get("actual") for item in ordered if item.get("actual")), None),
        "requested": latest.get("requested"), "requestedValues": requested_values[:3],
        "runCount": len(runs), "confirmedResumeCount": confirmed,
        "matchedResumeCount": matched,
        "newSessionCount": new, "mismatchCount": mismatch, "failedCount": failed,
        "unconfirmedCount": unconfirmed, "pendingCount": pending, "unknownCount": unknown,
        "runs": evidence, "runsTruncated": len(runs) > len(evidence),
        "contextCompression": None,
    }
