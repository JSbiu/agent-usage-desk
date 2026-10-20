"""Conservative per-request timing for WorkBuddy/CodeBuddy logs.

The structural pass keeps only parent links, timestamps, boundary flags and
request identifiers in memory.  Trace files are read in memory, but only
numeric durations, token counts and identifiers leave this module.
"""
from dataclasses import dataclass
from datetime import datetime
import json
import math

MAX_ID = 240
TOOL_BOUNDARIES = {"function_call_result", "function_call_output", "tool_result"}
COMPLETE = {"completed", "complete", "ok", "success"}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError):
        return None


def stamp(value):
    """Accept millisecond epoch numbers or ISO timestamps, like the old tool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = _number(value)
        if value is None:
            return None
        return value / 1000 if value >= 100_000_000_000 else value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def _id(value):
    if isinstance(value, str):
        value = value.strip()
        return value[:MAX_ID] if value else None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)[:MAX_ID]
    return None


def trace_id(value):
    text = _id(value)
    return text.removeprefix("trace_") if text else ""


def count(data, *keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = _number(data.get(key))
        if value is not None and value.is_integer():
            return int(value)
    return None


def nested_cache(data):
    if not isinstance(data, dict):
        return []
    detail = data.get("prompt_tokens_details") or data.get("input_tokens_details")
    values = [detail] if isinstance(detail, dict) else []
    detail = data.get("inputTokensDetails")
    values += detail if isinstance(detail, list) else [detail] if isinstance(detail, dict) else []
    return [count(item, "cached_tokens", "cache_read_input_tokens")
            for item in values if isinstance(item, dict)]


def usage_counts(data):
    """Inclusive input/output totals; reported total resolves cache inclusion."""
    if not isinstance(data, dict):
        return None
    inp = count(data, "input_tokens", "inputTokens", "prompt_tokens")
    out = count(data, "output_tokens", "outputTokens", "completion_tokens")
    if inp is None or out is None or inp + out == 0:
        return None
    caches = [count(data, key) for key in
              ("cache_read_input_tokens", "cacheReadInputTokens", "cacheTokens",
               "prompt_cache_hit_tokens", "cached_tokens", "cached_input_tokens")]
    caches += nested_cache(data)
    cached = next((value for value in caches if value is not None and value > 0),
                  next((value for value in caches if value is not None), None))
    write = count(data, "cache_creation_input_tokens", "cacheCreationInputTokens",
                  "cachedWriteTokens", "prompt_cache_write_tokens") or 0
    total = count(data, "total_tokens", "totalTokens")
    miss = count(data, "cachedMissTokens", "cacheMissTokens", "prompt_cache_miss_tokens")
    reasoning = count(data, "reasoning_output_tokens", "completion_thinking_tokens",
                      "completionThinkingTokens", "reasoningTokens") or 0
    if total == inp + out:
        full_input = inp
    elif miss is not None:
        full_input = miss + (cached or 0) + write
    elif "prompt_tokens" in data:
        full_input = inp
    else:
        full_input = inp + (cached or 0) + write
    if reasoning and total == full_input + out + reasoning:
        out += reasoning
    if cached is not None and cached > full_input:
        cached = None
    return full_input, out, cached


@dataclass(frozen=True)
class Timing:
    duration: float
    ended: float
    tokens: tuple | None


class TimingState:
    """Persisted per-file ancestry used for boundary estimates."""

    def __init__(self, nodes=None, boundary=None):
        self.nodes = dict(nodes or {})
        self.boundary = boundary
        self.changed = set()
        self.clear = False

    def parent_start(self, parent, request):
        seen = set()
        while parent and parent in self.nodes and parent not in seen:
            seen.add(parent)
            ts, parent, boundary, other = self.nodes[parent]
            if boundary or (other and other != request):
                return ts
        return None

    def observe(self, item):
        """Update ancestry and return ``(start, end)`` for a completed call."""
        if not isinstance(item, dict):
            return None
        ts = stamp(item.get("timestamp"))
        if ts is None:
            return None
        message = item.get("message") if isinstance(item.get("message"), dict) else {}
        provider = item.get("providerData") if isinstance(item.get("providerData"), dict) else {}
        kind = item.get("type")
        role = item.get("role") or message.get("role")
        parent = _id(item.get("parentId")) or _id(item.get("parentUuid"))
        node_id = _id(item.get("id")) or _id(item.get("uuid"))
        explicit = item.get("boundary") is True
        boundary = bool(explicit
                        or (kind in {"message", "user"} and role == "user")
                        or kind == "user"
                        or kind in TOOL_BOUNDARIES)
        request = (_id(provider.get("messageId")) or _id(item.get("requestId"))
                   or _id(message.get("id")) or node_id or "")
        generated = (kind in {"function_call", "assistant"}
                     or (kind == "message" and role == "assistant"))
        status = item.get("status")
        complete = status is None or str(status).lower() in COMPLETE
        start = self.parent_start(parent, request) if parent else self.boundary
        if node_id:
            self.nodes[node_id] = (ts, parent, boundary,
                                   request if generated and complete else None)
            self.changed.add(node_id)
        if boundary:
            self.boundary = ts
        if not generated or not complete:
            return None
        return (start, ts) if start is not None else None

    def feed(self, item):
        """Compatibility alias for the old structural pass."""
        return self.observe(item)


def _timing(document, since=None):
    if not isinstance(document, dict):
        return {}
    root = document.get("trace") if isinstance(document.get("trace"), dict) else {}
    identifier = trace_id(root.get("traceId"))
    found = {}
    spans = document.get("spans")
    if not isinstance(spans, list):
        return found
    for span in spans:
        if not isinstance(span, dict) or span.get("type") != "generation":
            continue
        if span.get("status") not in (None, "ok", "completed", "success"):
            continue
        started, ended = stamp(span.get("startedAt")), stamp(span.get("endedAt"))
        if started is None or ended is None or ended <= started:
            continue
        if since is not None and ended < since:
            continue
        duration = ended - started
        output = span.get("toolOutput", [])
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except (ValueError, TypeError):
                continue
        if isinstance(output, dict):
            output = [output]
        if not isinstance(output, list):
            continue
        for response in output:
            if not isinstance(response, dict) or not response.get("id"):
                continue
            key = (identifier, str(response["id"])[:MAX_ID])
            usage = response.get("usage")
            values = usage_counts(usage)
            timing = Timing(duration, ended,
                            (values[0], values[1]) if values else None)
            if key in found and found[key] != timing:
                found[key] = None
            elif key not in found:
                found[key] = timing
    return found


trace_timings = _timing


def merge(values):
    """Merge trace dictionaries, preserving conflicts as ``None``.

    ``values`` is an iterable of ``(trace_id, response_id) -> Timing|None``.
    """
    exact = {}
    for timings in values:
        for key, timing in timings.items():
            if key in exact and exact[key] != timing:
                exact[key] = None
            elif key not in exact:
                exact[key] = timing
    by_request = {}
    for (_, request), timing in exact.items():
        by_request.setdefault(request, set()).add(timing)
    fallback = {request: (None if None in timings or len(timings) != 1
                          else next(iter(timings)))
                for request, timings in by_request.items()}
    return exact, fallback
