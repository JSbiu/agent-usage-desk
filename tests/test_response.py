"""Turn latency, upgrade backfill and unchanged request accounting."""
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_native import event, request
from codex_monitor.service import Service
from codex_monitor.store import Store


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        (self.home / "sessions").mkdir(parents=True)
        self.path = self.home / "sessions" / "active.jsonl"
        self.at = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        self.query = {"from": (self.at - timedelta(days=1)).date().isoformat(), "to": self.at.date().isoformat()}
        self.service = Service(self.root / "cache", self.home)

    def tearDown(self):
        self.temp.cleanup()

    def completion(self, key, at=None, latency=3200):
        return event((at or self.at) + timedelta(seconds=50), "event_msg", {
            "type": "task_complete", "turn_id": "turn-" + key,
            "duration_ms": 50000, "time_to_first_token_ms": latency,
            "last_agent_message": "首字采集不能保存这段正文"})

    def test_only_first_request_counts_and_sort_keeps_missing_last(self):
        start = (self.at - timedelta(days=1)).replace(hour=23, minute=59, second=45)
        first = request("first", start)
        followup = request("tool", start + timedelta(seconds=20), model="gpt-6-luna", tier="priority")
        # A tool result is another model input in the SAME user turn.
        followup = b"".join(followup.splitlines(keepends=True)[1:])
        self.path.write_bytes(first + followup + self.completion("first", start))
        self.service.request("refresh")
        rows = self.service.request("details", self.query | {"sort": "firstToken", "direction": "asc"})["rows"]
        self.assertEqual([(r["key"], r["firstToken"]) for r in rows], [("first", 3.2), ("tool", None)])
        self.assertTrue(rows[0]["firstInTurn"])
        self.assertEqual(rows[0]["turnId"], "turn-first")
        stats = self.service.request("stats", self.query)
        measured = [r for r in stats["rows"] if r["responseSamples"]]
        self.assertEqual(len(measured), 1)
        self.assertEqual((measured[0]["day"], measured[0]["model"], measured[0]["mode"]),
                         (start.date().isoformat(), "gpt-6.1-sol", "standard"))
        self.assertEqual((stats["cards"]["records"], stats["cards"]["responseTurns"]), (2, 1))
        fast = self.service.request("stats", self.query | {"mode": "fast"})
        self.assertEqual(fast["cards"]["responseSamples"], 0)

    def test_completion_after_restart_updates_version_without_rebuilding_usage(self):
        self.path.write_bytes(request("a", self.at))
        before = self.service.request("refresh")["status"]
        completion = self.completion("a")
        with self.path.open("ab") as stream:
            stream.write(completion[:-1])
        self.assertEqual(self.service.request("refresh")["status"]["version"], before["version"])
        with self.path.open("ab") as stream:
            stream.write(completion[-1:])
        restarted = Service(self.root / "cache", self.home)
        with patch.object(Store, "save_groups", side_effect=AssertionError("Unchanged usage recomputed")):
            result = restarted.request("refresh")
        self.assertEqual((result["import"]["added"], result["import"]["updatedTurns"]), (0, 1))
        self.assertEqual(result["status"]["version"], before["version"] + 1)
        row = restarted.request("details", self.query)["rows"][0]
        self.assertEqual((row["firstToken"], row["speed"]), (3.2, 10))
        self.assertFalse(result["status"]["responseBackfillPending"])

    def test_range_percentiles_use_turns_not_daily_medians(self):
        yesterday = self.at - timedelta(days=1)
        data = b"".join(request(str(i), yesterday + timedelta(minutes=i))
            + self.completion(str(i), yesterday + timedelta(minutes=i), i * 1000) for i in range(1, 10))
        data += request("slow", self.at) + self.completion("slow", latency=40000)
        self.path.write_bytes(data)
        self.service.request("refresh")
        stats = self.service.request("stats", self.query)
        self.assertEqual([r["responseMedian"] for r in stats["rows"]], [5, 40])
        self.assertEqual((stats["cards"]["responseMedian"], stats["cards"]["responseP90"]), (5.5, 9))
        self.assertEqual((stats["cards"]["responseSamples"], stats["cards"]["responseTurns"]), (10, 10))

    def test_missing_invalid_and_aborted_turns_are_not_zero_samples(self):
        values = [0, None, -1, True, float("nan"), 50001]
        data = b"".join(request(str(i), self.at + timedelta(minutes=i))
            + self.completion(str(i), self.at + timedelta(minutes=i), value) for i, value in enumerate(values))
        data += request("abort", self.at + timedelta(minutes=10))
        data += event(self.at + timedelta(minutes=11), "event_msg", {"type": "turn_aborted"})
        self.path.write_bytes(data)
        self.service.request("refresh")
        cards = self.service.request("stats", self.query)["cards"]
        self.assertEqual((cards["responseSamples"], cards["responseTurns"]), (1, 7))
        self.assertEqual((cards["responseMedian"], cards["responseP90"]), (0, 0))

    def test_v2_backfill_preserves_frozen_usage_costs_and_then_reuses_cache(self):
        self.path.write_bytes(request("a", self.at) + self.completion("a"))
        self.service.request("refresh")
        database = next((self.root / "cache").glob("*.sqlite3"))
        with closing(sqlite3.connect(database)) as db:
            for table in ("turns", "turn_observations", "response_daily"):
                db.execute("DROP TABLE " + table)
            db.execute("DELETE FROM meta WHERE key LIKE 'response%'")
            state = json.loads(db.execute("SELECT state FROM files").fetchone()[0])
            for name in ("turn_id", "turn_first", "response_version"):
                state.pop(name, None)
            db.execute("UPDATE files SET state=?", (json.dumps(state),))
            db.execute("UPDATE requests SET cost=42")
            db.execute("UPDATE daily SET cost=42")
            db.execute("PRAGMA user_version=2")
            original = db.execute("SELECT * FROM requests").fetchall(), db.execute("SELECT * FROM daily").fetchall()
            db.commit()
        self.assertTrue(self.service.request("status")["responseBackfillPending"])
        with patch.object(Store, "save_groups", side_effect=AssertionError("Frozen daily data rebuilt")), \
                patch("codex_monitor.store.cost_value", side_effect=AssertionError("Old cost repriced")):
            imported = self.service.request("refresh")["import"]
        self.assertEqual((imported["responseBackfilledFiles"], imported["added"]), (1, 0))
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual((db.execute("SELECT * FROM requests").fetchall(), db.execute("SELECT * FROM daily").fetchall()), original)
            serialized = " ".join(row[0] for row in db.execute("SELECT data FROM turns"))
            self.assertNotIn("正文", serialized)
        row = self.service.request("details", self.query)["rows"][0]
        self.assertEqual((row["firstToken"], row["cost"]), (3.2, 42))
        with patch.object(Store, "read", side_effect=AssertionError("Already backfilled source reread")):
            self.assertEqual(self.service.request("refresh")["import"]["changedFiles"], 0)

    def test_archive_duplicates_and_truncation_update_response_groups(self):
        self.path.write_bytes(request("a", self.at) + self.completion("a"))
        self.service.request("refresh")
        archive = self.home / "archived_sessions"
        archive.mkdir()
        copy = archive / "copy.jsonl"
        shutil.copy2(self.path, copy)
        self.service.request("refresh")
        self.assertEqual(self.service.request("stats", self.query)["cards"]["responseTurns"], 1)
        self.path.unlink()
        self.service.request("refresh")
        self.assertEqual(self.service.request("stats", self.query)["cards"]["responseMedian"], 3.2)
        copy.write_bytes(request("b", self.at) + event(self.at + timedelta(seconds=50),
            "event_msg", {"type": "task_complete", "turn_id": "turn-b"}))
        self.service.request("refresh")
        cards = self.service.request("stats", self.query)["cards"]
        self.assertEqual((cards["responseTurns"], cards["responseSamples"]), (1, 0))
        self.assertIsNone(cards["responseMedian"])


if __name__ == "__main__":
    unittest.main()
