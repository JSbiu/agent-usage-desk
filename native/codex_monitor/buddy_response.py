"""Whitelisted first-token evidence from Buddy diagnostic logs.

Only the diagnostic event's numeric timing, request/session identifiers and
optional model/token facts leave this module.  Log text, JSON strings, prompts,
tool arguments and credentials are never retained.
"""
from dataclasses import dataclass
import json
import math
import re

MAX_ID = 240
MAX_MODEL = 160
COMPLETED_EVENT = "agent.model_call.completed"
COMPLETED_BASIS = "completed-log"
PLAIN_BASIS = "first-token-log"
TTFT_BASIS = "diagnostic-ttft"

_PLAIN = re.compile(
    r"^\s*(?:\[[^\]\r\n]{0,120}\]\s*)*\[ModelProvider\]\s+"
    r"First meaningful token received:\s*(?P<body>[^\r\n]*)$")
_JSON = re.compile(r"^\s*(?:\[[^\]\r\n]{0,120}\]\s*){0,4}\{")
_REQUEST = re.compile(r"(?:^|[,;\s])requestId\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_TTFT = re.compile(r"(?:^|[,;\s])ttft\s*=\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*ms\b")


@dataclass(frozen=True)
class Evidence:
    request_id: str
    session_id: str | None
    ttft: float
    basis: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def _id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_ID:
        return None
    if any(ord(character) < 32 or character.isspace() for character in value):
        return None
    return value


def _text(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:MAX_MODEL] if value else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _integer(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError
    if not math.isfinite(value) or value < 0 or value != int(value):
        raise ValueError
    return int(value)


def _quoted(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _plain(text):
    match = _PLAIN.match(text)
    if not match:
        return None, "ignored"
    body = match.group("body")
    request = _REQUEST.search(body)
    ttft = _TTFT.search(body)
    if not request or not ttft:
        return None, "invalid"
    request_id = _id(_quoted(request.group(1)))
    try:
        milliseconds = float(_quoted(ttft.group(1)))
    except (TypeError, ValueError, OverflowError):
        return None, "invalid"
    if request_id is None or not math.isfinite(milliseconds) or milliseconds < 0:
        return None, "invalid"
    return Evidence(request_id, None, milliseconds / 1000, PLAIN_BASIS), None


def _completed(text):
    match = _JSON.match(text)
    if not match:
        return None, "ignored"
    if COMPLETED_EVENT not in text:
        return None, "ignored"
    try:
        value = json.loads(text[match.end() - 1:])
    except (ValueError, UnicodeError, TypeError, OverflowError):
        return None, "invalid"
    if not isinstance(value, dict) or value.get("event_name") != COMPLETED_EVENT:
        return None, "ignored"
    result = value.get("result")
    if not isinstance(result, str) or result.strip().lower() != "success":
        return None, "ignored"
    # ``stage`` describes the finish phase in the observed diagnostics (for
    # example ``stop``), not the event category.  Completion is identified by
    # the explicit event name plus success result, so stage is not a filter.
    milliseconds = _number(value.get("time_to_first_token_ms"))
    if milliseconds is None:
        return None, "invalid"
    duration = None
    if value.get("duration_ms") is not None:
        duration = _number(value.get("duration_ms"))
        if duration is None:
            return None, "invalid"
    if duration is not None and milliseconds > duration:
        return None, "invalid"
    request_id = _id(value.get("conversation_message_id")
                     or value.get("request_id") or value.get("requestId"))
    if request_id is None:
        return None, "invalid"
    session = value.get("conversation_id")
    if session is not None:
        session = _id(session)
        if session is None:
            return None, "invalid"
    model = value.get("model")
    if model is not None:
        model = _text(model)
        if model is None:
            return None, "invalid"
    try:
        input_tokens = _integer(value.get("input_tokens"))
        output_tokens = _integer(value.get("output_tokens"))
        total = _integer(value.get("total_tokens"))
        for name in ("cached_read_tokens", "cached_write_tokens"):
            _integer(value.get(name))
    except ValueError:
        return None, "invalid"
    if (total is not None and input_tokens is not None and output_tokens is not None
            and total != input_tokens + output_tokens):
        return None, "invalid"
    return Evidence(request_id, session, milliseconds / 1000, COMPLETED_BASIS,
                    model, input_tokens, output_tokens), None


def parse_line(line):
    """Return ``(Evidence, None)`` or ``(None, reason)``.

    ``reason`` is ``ignored`` for unrelated log lines and ``invalid`` for a
    line that carries the supported diagnostic event but cannot be trusted.
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    if not isinstance(line, str):
        return None, "invalid"
    text = line.rstrip("\r\n").lstrip("\ufeff")
    evidence, reason = _plain(text)
    if evidence is not None or reason == "invalid":
        return evidence, reason
    return _completed(text)
