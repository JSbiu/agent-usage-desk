"""Checks for the changed cache lifecycle and subscription display contract."""
from datetime import datetime, timedelta
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from test_native import event, request
from codex_monitor.quota import snapshot
from codex_monitor.service import Service
from codex_monitor.store import Store


class CacheQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "日志"
        (self.home / "sessions").mkdir(parents=True)
        self.path = self.home / "sessions" / "active.jsonl"
        self.at = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        self.yesterday = self.at - timedelta(days=1)
        self.query = {"from": self.yesterday.date().isoformat(), "to": self.at.date().isoformat()}
        self.service = Service(self.root / "cache", self.home)

    def tearDown(self):
        self.temp.cleanup()

    def test_cached_days_reused_and_only_affected_groups_rebuilt(self):
        self.path.write_bytes(request("y", self.yesterday, output=100) + request("t", self.at, output=200))
        self.service.request("refresh")
        original = self.service.request("stats", self.query)["rows"]
        self.assertTrue(original[0]["frozen"])
        with patch.object(Store, "save_groups", side_effect=AssertionError("Unchanged days recomputed")):
            self.assertEqual(self.service.request("stats", self.query)["rows"], original)
            self.assertEqual(self.service.request("refresh")["import"]["rebuiltGroups"], 0)
        with self.path.open("ab") as stream:
            stream.write(request("new", self.at + timedelta(minutes=2), output=500))
        self.assertEqual(self.service.request("refresh")["import"]["rebuiltGroups"], 1)
        changed = self.service.request("stats", self.query)["rows"]
        self.assertEqual(changed[0], original[0])
        self.assertEqual((changed[1]["records"], changed[1]["output"], changed[1]["median"]), (2, 700, 35))
        late = self.home / "sessions" / "late.jsonl"
        late.write_bytes(request("late", self.yesterday + timedelta(minutes=2), output=300))
        imported = self.service.request("refresh")["import"]
        self.assertEqual((imported["historical"], imported["rebuiltGroups"]), (1, 1))
        self.assertEqual(self.service.request("stats", self.query)["rows"][0]["records"], 2)
        late.unlink()
        self.service.request("refresh")
        self.assertEqual(self.service.request("stats", self.query)["rows"][0], original[0])

    def test_upgrade_materializes_existing_index_without_reading_logs(self):
        self.path.write_bytes(request("a", self.at))
        self.service.request("refresh")
        original = self.service.request("stats", self.query)
        database = next((self.root / "cache").glob("*.sqlite3"))
        with closing(sqlite3.connect(database)) as db:
            db.execute("DROP TABLE daily")
            db.execute("DELETE FROM meta WHERE key IN ('dailyVersion','calendarZone','indexSummary')")
            db.execute("PRAGMA user_version=0")
            db.commit()
        with patch.object(Store, "read", side_effect=AssertionError("Source log reread on upgrade")):
            upgraded = self.service.request("stats", self.query)
        self.assertEqual(upgraded["rows"], original["rows"])
        self.assertEqual(upgraded["cards"], original["cards"])
        self.assertEqual(upgraded["status"]["version"], original["status"]["version"])

    def test_quota_prefers_all_buckets_and_keeps_unknown_values(self):
        result = snapshot({"rateLimits": {"primary": {"usedPercent": 99}}, "rateLimitsByLimitId": {
            "codex": {"planType": "plus", "primary": {"usedPercent": 27.5, "windowDurationMins": 300, "resetsAt": 123},
                "secondary": {"usedPercent": None, "windowDurationMins": 10080}},
            "other": {"primary": {"usedPercent": 105, "windowDurationMins": 60}}}}, 100)
        codex, other = result["buckets"]
        self.assertEqual((codex["windows"][0]["used"], codex["windows"][0]["remaining"]), (27.5, 72.5))
        self.assertIsNone(codex["windows"][1]["remaining"])
        self.assertIsNone(codex["windows"][1]["resetAt"])
        self.assertEqual(other["windows"][0]["remaining"], 0)
        legacy = snapshot({"rateLimits": {"primary": {"used_percent": 52, "window_minutes": 300, "resets_at": 123}}})
        self.assertEqual(legacy["buckets"][0]["windows"][0]["remaining"], 48)

    def test_quota_cache_and_log_fallback_are_separate_from_token_index(self):
        self.path.write_bytes(request("a", self.at))
        self.service.request("refresh")
        self.service.quota_reader = lambda home: snapshot({"rateLimits": {
            "primary": {"usedPercent": 43, "windowDurationMins": 300, "resetsAt": time.time() + 1000}}})
        value = self.service.request("quota")["quota"]
        self.assertEqual(value["source"], "account")
        self.assertIsNone(value["error"])
        before = self.service.request("status")
        with patch.object(self.service, "quota_reader", side_effect=RuntimeError("offline")) as reader:
            retained = self.service.request("quota")["quota"]
            self.assertEqual(retained["buckets"], value["buckets"])
            self.assertTrue(retained["error"])
            self.service.request("quota", {"live": False})
            self.assertEqual(reader.call_count, 1)
            log_at = datetime.fromtimestamp(value["fetchedAt"] + 2).astimezone()
            with self.path.open("ab") as stream:
                stream.write(event(log_at, "event_msg", {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": 52, "window_minutes": 300, "resets_at": time.time() + 1000}},
                    "content": "不会保存的正文"}))
            self.service.request("refresh")
            fallback = self.service.request("quota")["quota"]
        self.assertEqual(fallback["source"], "log")
        self.assertEqual(fallback["buckets"][0]["windows"][0]["remaining"], 48)
        self.assertNotIn("不会保存的正文", json.dumps(fallback, ensure_ascii=False))
        after = self.service.request("status")
        self.assertEqual((after["records"], after["version"]), (before["records"], before["version"]))


if __name__ == "__main__":
    unittest.main()
