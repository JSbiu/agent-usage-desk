"""Metrics preserved from the approved standalone Codex monitor."""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from math import ceil, isfinite
from statistics import median

PRICES = {
    "gpt-6.1-sol": (2, .10, 10, 2.50),
    "gpt-6-sol": (2, .20, 10, 2.50),
    "gpt-6-astra": (10, 1, 50, 12.50),
    "gpt-6-luna": (.10, .01, .50, .125),
    "gpt-5.6-sol": (4, .40, 20, 5),
}

OUTPUTS = {"reasoning", "function_call", "custom_tool_call",
           "web_search_call", "local_shell_call", "image_generation_call"}

RESULTS = {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}

def stamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None

def service_tier(value):
    if not isinstance(value, str):
        return "unknown"
    return {"default": "standard", "standard": "standard",
            "priority": "fast", "fast": "fast"}.get(value.lower(), "unknown")

def effective_tier(row):
    # Preserve raw history in Row; unknown modes use Standard for reporting.
    return "fast" if row.tier == "fast" else "standard"


def response_percentiles(values):
    """Median and nearest-rank P90 of recorded turn latencies, in seconds."""
    values = sorted(values)
    return (median(values), values[ceil(.9 * len(values)) - 1]) if values else (None, None)


def first_token_seconds(payload):
    value = payload.get("time_to_first_token_ms")
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        return None
    duration = payload.get("duration_ms")
    if type(duration) in (int, float) and isfinite(duration) and value > duration:
        return None
    return value / 1000

@dataclass
class Row:
    key: str
    at: float
    model: str
    usage: dict
    duration: float | None
    tier: str = "unknown"
    tier_source: str = "unknown"

class Session:
    def __init__(self, path, max_rows=500, since=None):
        self.path, self.offset = path, 0
        self.session, self.model = str(path), "?"
        self.boundary = self.start = self.end = None
        self.tier = ("unknown", "unknown")
        self.request_tier = self.output_tier = None
        self.modern, self.last_total, self.skipped = False, None, 0
        self.max_rows, self.since = max_rows, since
        self.rows = deque(maxlen=max_rows)
        self.turn_id = self.turn_first = None
        self.response_version = 1
        self.turns = []

    def apply_tier(self, fields, context=False):
        if not isinstance(fields, dict) or "service_tier" not in fields:
            return  # missing key does not reset earlier historical settings
        tier = service_tier(fields["service_tier"])
        current = (tier, "settings" if tier != "unknown" else "unknown")
        # No exact request-start event: a toggle between the input boundary and
        # first saved output makes that request ambiguous. Do not guess its bill.
        if (current != self.tier and self.boundary is not None
                and self.end is None and self.request_tier is not None):
            self.request_tier = ("unknown", "unknown")
        self.tier = current
        if context and self.end is None:
            self.request_tier = current

    def feed(self, row):
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            return
        p = row["payload"]
        ts, kind = stamp(row.get("timestamp")), p.get("type")
        if ts is None:
            return
        if row.get("type") == "session_meta":
            self.session = p.get("id") or self.session
            self.apply_tier(p)
        elif row.get("type") == "turn_context":
            self.model = p.get("model") or "?"
            self.apply_tier(p, context=True)
        elif kind == "thread_settings_applied":
            self.apply_tier(p.get("thread_settings"))
        elif kind == "task_started":
            self.boundary, self.start, self.end = ts, None, None
            self.request_tier, self.output_tier = self.tier, None
            self.turn_id, self.turn_first = p.get("turn_id"), None
        elif kind in {"task_complete", "turn_aborted"}:
            if self.turn_first:
                self.turns.append(dict(self.turn_first,
                    turnId=p.get("turn_id") or self.turn_id,
                    firstToken=first_token_seconds(p) if kind == "task_complete" else None))
            self.turn_id = self.turn_first = None
            self.boundary = self.start = self.end = None
            self.request_tier = self.output_tier = None
        elif row.get("type") == "response_item":
            if (kind == "message" and p.get("role") == "user") or kind in RESULTS:
                self.boundary = ts
                self.request_tier = self.tier
            elif kind in OUTPUTS or (kind == "message" and p.get("role") == "assistant"):
                if self.end is None:
                    self.start = self.boundary
                    self.output_tier = self.request_tier or self.tier
                self.end = ts
        elif row.get("type") == "token_usage_record" or kind == "token_usage_record":
            self.modern = True
            self.finish(p.get("usage"), p.get("response_id"), ts, p)
        elif kind == "token_count" and not self.modern:
            info = p.get("info")
            if not isinstance(info, dict):
                return
            total = info.get("total_token_usage")
            if isinstance(total, dict):
                signature = tuple(total.get(k) for k in ("input_tokens", "output_tokens"))
                if signature == self.last_total:
                    return
                self.last_total = signature
            self.finish(info.get("last_token_usage"), None, ts, info)

    def finish(self, usage, response_id, ts, record=None):
        if not isinstance(usage, dict) or self.end is None:
            return
        names = ("input_tokens", "cached_input_tokens", "output_tokens")
        counts = [usage.get(k, 0) for k in names]
        if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in counts):
            self.start = self.end = None
            return
        inp, cache, out = counts
        if inp + out == 0 or cache > inp:
            self.start = self.end = None
            return  # status/compaction events with no request breakdown
        usage = dict(usage, **dict(zip(names, counts)))
        duration = self.end - self.start if self.start is not None else None
        if duration is not None and duration <= 0:
            duration = None
        key = response_id or f"{self.session}:{ts}:{inp}:{cache}:{out}"
        tier, source = self.output_tier or ("unknown", "unknown")
        for fields in (record, usage):
            if isinstance(fields, dict) and "service_tier" in fields:
                tier = service_tier(fields["service_tier"])
                source = "response" if tier != "unknown" else "unknown"
                break
        if self.turn_first is None:
            # Attribute a turn's latency to its FIRST usable request, even when
            # later tool calls change model/mode or finish on another local day.
            self.turn_first = dict(key=key, turnId=self.turn_id)
        if self.since is None or self.end >= self.since:
            self.rows.append(Row(key, self.end, self.model, usage, duration, tier, source))
        # Old token_count may arrive AFTER tool results. Preserve the latest input
        # boundary, but use the last generated item's timestamp as response end.
        if self.boundary is None or self.boundary <= self.end:
            self.request_tier = self.tier
        self.boundary = max(self.boundary or self.end, self.end)
        self.start = self.end = self.output_tier = None

    def read(self):
        try:
            if self.path.stat().st_size < self.offset:
                self.__init__(self.path, self.max_rows, self.since)
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                while True:
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        break  # retry partial lines, including split UTF-8
                    self.offset = stream.tell()
                    try:
                        self.feed(json.loads(line))
                    except (ValueError, UnicodeError):
                        self.skipped += 1
                        self.start = self.end = self.boundary = self.output_tier = None
                        self.tier = self.request_tier = ("unknown", "unknown")
                        self.turn_id = self.turn_first = None
        except OSError:
            pass  # log rotation/transient read failure: retry next poll

def cost_value(row):
    prices = PRICES.get(row.model)
    if prices is None:
        return None
    u = row.usage
    inp, cache, out = u["input_tokens"], u["cached_input_tokens"], u["output_tokens"]
    write = u.get("cache_write_input_tokens", 0)
    if not isinstance(write, int) or write < 0 or cache + write > inp:
        return None
    a, b, c, d = prices
    if inp > 272_000:
        a, b, c, d = a * 2, b * 2, c * 1.5, d * 2
    value = ((inp - cache - write) * a + cache * b + out * c + write * d) / 1_000_000
    return value * (2 if effective_tier(row) == "fast" else 1)

def daily_summary(rows, since, days):
    """One row per local day, model and effective mode; weighted TPS per group."""
    first_day = datetime.fromtimestamp(since).date()
    calendar = {(first_day + timedelta(days=i)).isoformat() for i in range(days)}
    groups = {}
    def empty(day, model="-", tier="-"):
        return dict(day=day, model=model, tier=tier, inferred=False, assumed=0,
                    records=0, timed=0, inp=0, out=0, cache=0, timed_out=0,
                    duration=0.0, cost=0.0, priced=0, speeds=[])
    for row in rows:
        day = datetime.fromtimestamp(row.at).date().isoformat()
        if day not in calendar:
            continue
        tier = effective_tier(row)
        key = (day, row.model, tier)
        if key not in groups:
            groups[key] = empty(*key)
        group = groups[key]
        group["inferred"] |= row.tier_source != "response"
        group["assumed"] += row.tier not in {"standard", "fast"}
        group["records"] += 1
        group["inp"] += row.usage["input_tokens"]
        group["out"] += row.usage["output_tokens"]
        group["cache"] += row.usage["cached_input_tokens"]
        if row.duration is not None and row.duration > 0 and row.usage["output_tokens"] > 0:
            group["timed"] += 1
            group["timed_out"] += row.usage["output_tokens"]
            group["duration"] += row.duration
            group["speeds"].append(row.usage["output_tokens"] / row.duration)
        value = cost_value(row)
        if value is not None:
            group["cost"] += value  # round only after summing the group
            group["priced"] += 1
    populated = {key[0] for key in groups}
    for day in calendar - populated:
        groups[(day, "-", "-")] = empty(day)
    for group in groups.values():
        speeds = group.pop("speeds")
        group["median_speed"] = median(speeds) if speeds else None
        group["min_speed"] = min(speeds) if speeds else None
        group["max_speed"] = max(speeds) if speeds else None
    order = {"-": -1, "standard": 0, "fast": 1}
    return [groups[key] for key in sorted(groups, key=lambda k: (k[0], k[1], order[k[2]]))]
