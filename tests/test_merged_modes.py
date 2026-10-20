"""Merged Standard/Fast groups, mixed-source averages and large frames."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from codex_monitor.host import MAX_FRAME
from codex_monitor.service import Service
from codex_monitor.store import encode
from test_native import event

SECRET = "SECRET-合并正文"


def codex_turn(key, at, duration=10, output=100, tier=None, latency=3000):
    body = event(at, "event_msg", {"type": "task_started"})
    context = {"model": "gpt-6.1-sol"}
    if tier is not None:
        context["service_tier"] = tier
    body += event(at, "turn_context", context)
    body += event(at, "response_item", {"type": "message", "role": "user",
                                        "content": SECRET})
    body += event(at + timedelta(seconds=duration), "response_item",
                  {"type": "message", "role": "assistant", "content": SECRET})
    body += event(at + timedelta(seconds=duration), "token_usage_record", {
        "response_id": key,
        "usage": {"input_tokens": 1000, "cached_input_tokens": 900,
                  "output_tokens": output}})
    body += event(at + timedelta(seconds=duration), "event_msg", {
        "type": "task_complete", "turn_id": "turn-" + key,
        "duration_ms": duration * 1000, "time_to_first_token_ms": latency})
    return body


def buddy_line(session, message, at, model="deepseek-v4.1-flash",
               input_tokens=1200, output_tokens=80, cache=900):
    value = {
        "id": session + "-" + message,
        "sessionId": session,
        "timestamp": int(at.timestamp() * 1000),
        "type": "function_call",
        "providerData": {
            "model": model, "messageId": message,
            "conversationRequestId": "turn-" + session,
        },
        "message": {"usage": {
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": cache}},
    }
    return (json.dumps(value, ensure_ascii=False) + "\n").encode()


def buddy_turn(session, message, at, duration, output):
    user = {
        "id": session + "-user-" + message, "sessionId": session,
        "timestamp": int(at.timestamp() * 1000), "type": "message",
        "message": {"role": "user", "content": SECRET},
    }
    return ((json.dumps(user, ensure_ascii=False) + "\n").encode()
            + buddy_line(session, message, at + timedelta(seconds=duration),
                         output_tokens=output))


class MergedModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"
        (self.home / "sessions").mkdir(parents=True)
        self.codebuddy = self.root / "codebuddy" / "projects"
        self.workbuddy = self.root / "workbuddy" / "projects"
        self.codebuddy.mkdir(parents=True)
        self.workbuddy.mkdir(parents=True)
        self.bridge = self.root / "runs"
        self.bridge.mkdir()
        self.at = datetime(2026, 10, 19, 10, 0, tzinfo=datetime.now().astimezone().tzinfo)
        self.query = {"from": self.at.date().isoformat(), "to": self.at.date().isoformat()}
        self.service = Service(self.root / "cache", self.home,
                               buddy_roots={"workbuddy": self.workbuddy,
                                            "codebuddy": self.codebuddy},
                               bridge_dir=self.bridge)

    def tearDown(self):
        self.temp.cleanup()

    def codex(self, *turns):
        (self.home / "sessions" / "codex.jsonl").write_bytes(b"".join(turns))

    def buddy(self, *turns):
        (self.codebuddy / "buddy.jsonl").write_bytes(b"".join(turns))

    def stats(self, source, **extra):
        return self.service.request("stats", self.query | {"source": source} | extra)

    def test_merged_modes_sum_costs_and_use_original_requests(self):
        self.codex(
            codex_turn("s1", self.at, 10, 100, latency=1000),
            codex_turn("s2", self.at + timedelta(minutes=1), 10, 200, latency=2000),
            codex_turn("s3", self.at + timedelta(minutes=2), 10, 300, latency=3000),
            codex_turn("f1", self.at + timedelta(minutes=3), 20, 400,
                       tier="priority", latency=10000),
        )
        self.service.request("refresh")
        separate = {row["mode"]: row for row in self.stats("codex")["rows"] if row["records"]}
        self.assertEqual(set(separate), {"standard", "fast"})
        merged = self.stats("codex", mergeModes=True)
        self.assertEqual(len(merged["rows"]), 1)
        group = merged["rows"][0]
        self.assertEqual((group["mode"], group["records"], group["timed"]), ("", 4, 4))
        self.assertAlmostEqual(group["cost"], separate["standard"]["cost"] + separate["fast"]["cost"])
        self.assertEqual((group["timedOut"], group["seconds"]), (1000, 50))
        self.assertAlmostEqual(group["average"], 20)
        self.assertEqual((group["median"], group["minimum"], group["maximum"]), (20, 10, 30))
        # Global turn percentiles, not the median of per-mode medians.
        self.assertEqual((group["responseTurns"], group["responseSamples"]), (4, 4))
        self.assertAlmostEqual(group["responseMedian"], 2.5)
        self.assertEqual(group["responseP90"], 10)

    def test_mixed_source_cards_weight_seconds_and_sum_known_cost(self):
        self.codex(
            codex_turn("s1", self.at, 10, 100, latency=1000),
            codex_turn("s2", self.at + timedelta(minutes=1), 20, 200, latency=2000),
        )
        self.buddy(
            buddy_turn("b1", "m1", self.at, 5, 80),
            buddy_turn("b2", "m2", self.at + timedelta(minutes=1), 10, 100),
        )
        self.service.request("refresh")
        cards = self.stats("all", mergeModes=True)["cards"]
        self.assertEqual((cards["records"], cards["timed"], cards["priced"]), (4, 4, 4))
        self.assertEqual((cards["timedOut"], cards["seconds"]), (480, 45))
        self.assertAlmostEqual(cards["average"], 480 / 45)
        self.assertEqual(cards["traceTimed"] + cards["boundaryTimed"], 4)
        buddy_rows = self.service.request("details", self.query | {
            "source": "codebuddy-cli", "pageSize": 100})["rows"]
        codex_rows = self.service.request("details", self.query | {
            "source": "codex", "pageSize": 100})["rows"]
        expected = sum(row["cost"] for row in buddy_rows + codex_rows)
        self.assertAlmostEqual(cards["cost"], expected)
        self.assertEqual(cards["costUnknown"], 0)

    def test_full_buddy_page_stays_under_the_frame_limit(self):
        session = "S" * 240
        lines = []
        for index in range(100):
            lines.append(buddy_line(session, str(index).ljust(240, "M"),
                                    self.at + timedelta(seconds=index),
                                    model="unknown-model"))
        self.buddy(*lines)
        self.service.request("refresh")
        data = self.service.request("details", self.query | {
            "source": "codebuddy-cli", "pageSize": 100})
        self.assertEqual(len(data["rows"]), 100)
        self.assertLess(len(encode({"ok": True, "data": data}).encode("utf-8")), MAX_FRAME)


if __name__ == "__main__":
    unittest.main()
