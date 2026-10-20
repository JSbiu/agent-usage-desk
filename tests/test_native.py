"""Checks specific to the new persistent index and Chrome wire protocol."""
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native"))
from codex_monitor.host import read_frame, serve, write_frame
from codex_monitor.service import Service


def event(at, kind, payload):
    return json.dumps({"timestamp": at.astimezone(timezone.utc).isoformat(), "type": kind,
                       "payload": payload}, ensure_ascii=False).encode() + b"\n"


def request(key, at, duration=10, output=100, model="gpt-6.1-sol", tier=None, legacy=False):
    context = {"model": model}
    if tier is not None:
        context["service_tier"] = tier
    usage = {"input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": output}
    end = at + timedelta(seconds=duration)
    body = event(at, "event_msg", {"type": "task_started"})
    body += event(at, "turn_context", context)
    body += event(at, "response_item", {"type": "message", "role": "user", "content": "私密正文"})
    body += event(end, "response_item", {"type": "message", "role": "assistant", "content": "回复正文"})
    if legacy:
        body += event(end, "event_msg", {"type": "token_count", "info": {"last_token_usage": usage,
                           "total_token_usage": {"input_tokens": key * 1000, "output_tokens": key * output}}})
    else:
        body += event(end, "token_usage_record", {"response_id": str(key), "usage": usage})
    return body


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "日志"
        self.folder = self.home / "sessions" / "2025" / "01" / "01"
        self.folder.mkdir(parents=True)
        self.path = self.folder / "resumed.jsonl"
        self.at = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        self.query = {"from": self.at.date().isoformat(), "to": self.at.date().isoformat()}
        self.service = Service(self.root / "cache", self.home)

    def tearDown(self):
        self.temp.cleanup()

    def test_grouped_statistics_and_exact_model_filter(self):
        self.path.write_bytes(request("a", self.at, 10, 100) + request("b", self.at + timedelta(minutes=1), 20, 600)
            + request("c", self.at + timedelta(minutes=2), 10, 400, tier="priority")
            + request("d", self.at + timedelta(minutes=3), 10, 50, model="gpt-6-luna", tier="default"))
        self.service.request("refresh")
        result = self.service.request("stats", self.query | {"model": "gpt-6.1-sol"})
        self.assertEqual(result["cards"]["records"], 3)
        standard, fast = result["rows"]
        self.assertEqual((standard["mode"], standard["records"]), ("standard", 2))
        self.assertAlmostEqual(standard["average"], 700 / 30)
        self.assertEqual((standard["median"], standard["minimum"], standard["maximum"]), (20, 10, 30))
        self.assertEqual((fast["mode"], fast["average"]), ("fast", 40))
        self.assertAlmostEqual(result["cards"]["cacheHit"], .9)
        db = next((self.root / "cache").glob("*.sqlite3"))
        self.assertNotIn("私密正文".encode(), db.read_bytes())

    def test_cold_restart_in_middle_of_response(self):
        data = request("a", self.at, tier="priority")
        lines = data.splitlines(keepends=True)
        self.path.write_bytes(b"".join(lines[:-1]))
        self.service.request("refresh")
        with self.path.open("ab") as stream:
            stream.write(lines[-1] + request("b", self.at + timedelta(minutes=1)))
        restarted = Service(self.root / "cache", self.home)
        self.assertEqual(restarted.request("refresh")["import"]["added"], 2)
        records = restarted.request("details", self.query)["rows"]
        self.assertTrue(all(r["mode"] == "fast" and r["speed"] == 10 for r in records))

    def test_partial_utf8_tail_and_legacy_duplicate_after_restart(self):
        data = request(1, self.at, legacy=True)
        split = data.index("私密正文".encode()) + 1
        self.path.write_bytes(data[:split])
        self.assertEqual(self.service.request("refresh")["status"]["records"], 0)
        with self.path.open("ab") as stream:
            stream.write(data[split:])
        self.assertEqual(self.service.request("refresh")["status"]["records"], 1)
        with self.path.open("ab") as stream:
            stream.write(data.splitlines(keepends=True)[-1])
            stream.write(request(2, self.at + timedelta(minutes=1), legacy=True))
        restarted = Service(self.root / "cache", self.home)
        self.assertEqual(restarted.request("refresh")["status"]["records"], 2)

    def test_archive_deduplication_and_truncation(self):
        self.path.write_bytes(request("a", self.at))
        self.service.request("refresh")
        archive = self.home / "archived_sessions"; archive.mkdir()
        copy = archive / "archived.jsonl"; copy.write_bytes(self.path.read_bytes())
        self.assertEqual(self.service.request("refresh")["import"]["added"], 0)
        self.path.unlink()
        self.assertEqual(self.service.request("refresh")["status"]["records"], 1)
        copy.write_bytes(request("b", self.at, output=1))
        result = self.service.request("refresh")
        self.assertEqual(result["status"]["records"], 1)
        self.assertEqual(self.service.request("details", self.query)["rows"][0]["key"], "b")

    def test_no_500_record_cap_and_paging(self):
        self.path.write_bytes(b"".join(request(str(i), self.at + timedelta(seconds=i * 10), 1, i + 1) for i in range(520)))
        self.service.request("refresh")
        data = self.service.request("details", self.query | {"page": 3, "pageSize": 100, "sort": "speed", "direction": "desc"})
        self.assertEqual((data["total"], len(data["rows"]), data["rows"][0]["speed"]), (520, 100, 320))
        self.assertEqual(self.service.request("refresh")["import"]["added"], 0)

    def test_unknown_prices_and_untimed_records(self):
        self.path.write_bytes(request("a", self.at, 0, model="unknown-model"))
        self.service.request("refresh")
        data = self.service.request("stats", self.query)
        self.assertEqual(data["cards"]["timed"], 0)
        self.assertIsNone(data["cards"]["cost"])
        self.assertIsNone(data["rows"][0]["average"])
        self.assertEqual(data["rows"][0]["mode"], "standard")

    def test_unicode_native_frames_and_invalid_operation(self):
        source, target = io.BytesIO(), io.BytesIO()
        write_frame(source, {"id": "中文", "op": "status"})
        write_frame(source, {"id": 2, "op": "unsupported"})
        source.seek(0); serve(self.service, source, target); target.seek(0)
        result = read_frame(target)
        self.assertEqual(result["id"], "中文")
        self.assertTrue(result["ok"])
        self.assertFalse(read_frame(target)["ok"])
        with self.assertRaises(ValueError): read_frame(io.BytesIO(struct.pack('<I', 1024 * 1024)))


if __name__ == "__main__":
    unittest.main()
