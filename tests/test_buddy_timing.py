"""Synthetic checks for boundary timing, trace preference and timing upgrades."""
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native"))
from codex_monitor.buddy import parse_record
from codex_monitor.service import Service

SECRET = "SECRET-计时正文-不得保存"
TOOL_SECRET = "SECRET-工具内容"


def line(session, node, at, *, parent=None, kind="function_call", role=None,
         model="deepseek-v4.1-flash", message=None, input_tokens=1200,
         output_tokens=80, cache=900, usage=True, trace="turn-a"):
    value = {
        "id": node, "sessionId": session,
        "timestamp": int(at.timestamp() * 1000), "type": kind,
        "providerData": {"model": model, "conversationRequestId": trace},
    }
    if parent:
        value["parentId"] = parent
    if role:
        value["role"] = role
    if usage:
        value["providerData"]["messageId"] = message or node
        value["message"] = {"usage": {
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": cache}}
        value["providerData"]["rawUsage"] = {
            "prompt_tokens": input_tokens, "completion_tokens": output_tokens,
            "prompt_cache_hit_tokens": cache, "credit": .06}
    else:
        value["message"] = {"role": role or "user", "content": SECRET}
    return (json.dumps(value, ensure_ascii=False) + "\n").encode()


def trace_document(identifier, response, started, ended, tokens=(1200, 80),
                   status="ok"):
    return {
        "trace": {"traceId": identifier},
        "spans": [{
            "type": "generation", "status": status,
            "startedAt": started.isoformat(), "endedAt": ended.isoformat(),
            "toolOutput": [{
                "id": response, "content": TOOL_SECRET,
                "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
            }],
        }],
    }


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"
        (self.home / "sessions").mkdir(parents=True)
        self.workbuddy = self.root / "workbuddy" / "projects"
        self.codebuddy = self.root / "codebuddy" / "projects"
        self.workbuddy.mkdir(parents=True)
        self.codebuddy.mkdir(parents=True)
        self.bridge = self.root / "runs"
        self.bridge.mkdir()
        self.at = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        self.query = {"from": (self.at - timedelta(days=1)).date().isoformat(),
                      "to": self.at.date().isoformat()}
        self.service = self.make_service()

    def tearDown(self):
        self.temp.cleanup()

    def make_service(self):
        return Service(self.root / "cache", self.home,
                       buddy_roots={"workbuddy": self.workbuddy, "codebuddy": self.codebuddy},
                       bridge_dir=self.bridge)

    def write(self, folder, name, *data):
        path = Path(folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(data))
        return path

    def details(self, source="codebuddy-cli"):
        return self.service.request("details", self.query | {"source": source})

    def rows(self):
        return {row["messageId"]: row for row in self.details()["rows"]}

    def traces(self, name, document, indent=2):
        path = self.codebuddy.parent / "traces" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=indent), encoding="utf-8")
        return path

    def test_parent_boundary_excludes_tool_execution_and_uses_ancestry(self):
        user = line("S", "u1", self.at, kind="message", role="user", usage=False)
        assistant = line("S", "a1", self.at + timedelta(seconds=5), parent="u1",
                         kind="message", role="assistant", message="m1")
        tool = line("S", "t1", self.at + timedelta(seconds=2),
                    kind="function_call_result", usage=False)
        later = line("S", "u2", self.at + timedelta(seconds=4),
                     kind="message", role="user", usage=False)
        progress = line("S", "p1", self.at + timedelta(seconds=2, milliseconds=500),
                        parent="t1", kind="progress", usage=False)
        follow = line("S", "a2", self.at + timedelta(seconds=5), parent="p1",
                      kind="message", role="assistant", message="m2")
        self.write(self.codebuddy, "timing.jsonl", user, assistant, tool, later,
                   progress, follow)
        self.service.request("refresh")
        rows = self.rows()
        self.assertEqual((rows["m1"]["duration"], rows["m1"]["timingSource"]), (5, "boundary"))
        self.assertAlmostEqual(rows["m1"]["speed"], 16)
        self.assertEqual((rows["m2"]["duration"], rows["m2"]["timingSource"]), (3, "boundary"))
        self.assertAlmostEqual(rows["m2"]["speed"], 80 / 3)
        self.assertIsNone(rows["m1"]["firstToken"])

    def test_legacy_uuid_ancestry_does_not_use_a_later_unrelated_boundary(self):
        user=json.loads(line("S", "u", self.at, kind="message", role="user", usage=False))
        user["uuid"]=user.pop("id")
        answer=json.loads(line("S", "a", self.at+timedelta(seconds=5),
                               kind="message", role="assistant", message="m"))
        answer["parentUuid"]="u"
        later=line("S", "other", self.at+timedelta(seconds=4),
                   kind="message", role="user", usage=False)
        self.write(self.codebuddy, "legacy.jsonl", (json.dumps(user)+"\n").encode(),
                   later, (json.dumps(answer)+"\n").encode())
        self.service.request("refresh")
        self.assertEqual(self.rows()["m"]["duration"], 5)

    def test_trace_preferred_then_rewritten_and_removed(self):
        self.write(self.codebuddy, "trace.jsonl",
                   line("S", "u1", self.at, kind="message", role="user", usage=False),
                   line("S", "a1", self.at + timedelta(seconds=5), parent="u1",
                        kind="message", role="assistant", message="m1"))
        first = self.service.request("refresh")
        version = first["status"]["version"]
        row = self.rows()["m1"]
        self.assertEqual((row["duration"], row["timingSource"]), (5, "boundary"))
        self.traces("one.json", trace_document("turn-a", "m1",
                                               self.at + timedelta(seconds=1),
                                               self.at + timedelta(seconds=4)))
        self.service.request("refresh")
        row = self.rows()["m1"]
        self.assertEqual((row["duration"], row["timingSource"]), (3, "trace"))
        self.assertTrue(row["timed"])
        # A late rewrite changes duration without any JSONL change.
        self.traces("one.json", trace_document("turn-a", "m1",
                                               self.at + timedelta(seconds=1),
                                               self.at + timedelta(seconds=7)), indent=4)
        changed = self.service.request("refresh")
        row = self.rows()["m1"]
        self.assertEqual((row["duration"], row["timingSource"]), (6, "trace"))
        self.assertGreater(changed["status"]["version"], version)
        # Removal falls back to the persisted boundary estimate.
        (self.codebuddy.parent / "traces" / "one.json").unlink()
        removed = self.service.request("refresh")
        row = self.rows()["m1"]
        self.assertEqual((row["duration"], row["timingSource"]), (5, "boundary"))
        self.assertGreater(removed["status"]["version"], changed["status"]["version"])

    def test_trace_directory_failure_falls_back_and_recovers_without_usage_changes(self):
        self.write(self.codebuddy, "fault.jsonl",
                   line("S", "u", self.at, kind="user", usage=False),
                   line("S", "a", self.at+timedelta(seconds=5), parent="u", message="m"))
        self.traces("fault.json", trace_document("turn-a", "m", self.at,
                                                 self.at+timedelta(seconds=3)))
        original=self.service.request("refresh")
        self.assertEqual(self.rows()["m"]["duration"], 3)
        with patch("codex_monitor.buddy_store.BuddyStore._scan_json", side_effect=PermissionError("synthetic")):
            failed=self.service.request("refresh")
        self.assertTrue(failed["buddyImport"]["warnings"])
        self.assertEqual(failed["buddyImport"]["added"], 0)
        self.assertEqual((self.rows()["m"]["duration"],self.rows()["m"]["timingSource"]), (5,"boundary"))
        self.assertGreater(failed["status"]["version"], original["status"]["version"])
        recovered=self.service.request("refresh")
        self.assertEqual((self.rows()["m"]["duration"],self.rows()["m"]["timingSource"]), (3,"trace"))
        self.assertFalse(recovered["buddyImport"]["warnings"])
        self.assertGreater(recovered["status"]["version"], failed["status"]["version"])

    def test_response_fallback_is_used_only_within_one_source(self):
        self.write(self.codebuddy, "fallback.jsonl",
                   line("S", "u1", self.at, kind="message", role="user", usage=False),
                   line("S", "a1", self.at + timedelta(seconds=4), parent="u1",
                        kind="message", role="assistant", message="m1"))
        # The trace ID differs, so only the response ID can join.
        self.traces("other.json", trace_document("other-trace", "m1",
                                                 self.at + timedelta(seconds=1),
                                                 self.at + timedelta(seconds=3)))
        self.service.request("refresh")
        row = self.rows()["m1"]
        self.assertEqual((row["duration"], row["timingSource"]), (2, "trace"))

    def test_ambiguous_or_mismatched_traces_fall_back_to_boundary(self):
        self.write(self.codebuddy, "ambiguous.jsonl",
                   line("S", "u1", self.at, kind="message", role="user", usage=False),
                   line("S", "a1", self.at + timedelta(seconds=5), parent="u1",
                        kind="message", role="assistant", message="m1"))
        self.traces("a.json", trace_document("turn-a", "m1",
                                             self.at + timedelta(seconds=1),
                                             self.at + timedelta(seconds=4)))
        self.traces("b.json", trace_document("turn-a", "m1",
                                             self.at + timedelta(seconds=1),
                                             self.at + timedelta(seconds=6)), indent=4)
        self.service.request("refresh")
        row = self.rows()["m1"]
        self.assertEqual((row["duration"], row["timingSource"]), (5, "boundary"))
        # A token mismatch also refuses the trace duration.
        for path in (self.codebuddy.parent / "traces").glob("*.json"):
            path.unlink()
        self.traces("bad.json", trace_document("turn-a", "m1",
                                               self.at + timedelta(seconds=1),
                                               self.at + timedelta(seconds=4),
                                               tokens=(1, 1)))
        self.service.request("refresh")
        row = self.rows()["m1"]
        self.assertEqual((row["duration"], row["timingSource"]), (5, "boundary"))

    def test_ambiguous_response_fallback_does_not_pick_a_duration(self):
        self.write(self.codebuddy, "fallback.jsonl",
                   line("S", "u1", self.at, kind="message", role="user", usage=False),
                   line("S", "a1", self.at + timedelta(seconds=5), parent="u1",
                        kind="message", role="assistant", message="m1"))
        self.traces("a.json", trace_document("other-a", "m1",
                                             self.at + timedelta(seconds=1),
                                             self.at + timedelta(seconds=4)))
        self.traces("b.json", trace_document("other-b", "m1",
                                             self.at + timedelta(seconds=1),
                                             self.at + timedelta(seconds=6)), indent=4)
        self.service.request("refresh")
        row = self.rows()["m1"]
        self.assertEqual((row["duration"], row["timingSource"]), (5, "boundary"))

    def test_restart_append_partial_line_and_truncation(self):
        path = self.write(self.codebuddy, "incremental.jsonl",
                          line("S", "u1", self.at, kind="message", role="user", usage=False),
                          line("S", "a1", self.at + timedelta(seconds=5), parent="u1",
                               kind="message", role="assistant", message="m1"))
        first = self.service.request("refresh")["buddyImport"]
        self.assertEqual(first["added"], 1)
        self.assertAlmostEqual(self.rows()["m1"]["duration"], 5)
        tool = line("S", "t1", self.at + timedelta(seconds=7),
                    kind="function_call_result", usage=False)
        follow = line("S", "a2", self.at + timedelta(seconds=9), parent="t1",
                      kind="message", role="assistant", message="m2")
        with path.open("ab") as stream:
            stream.write(tool[:40])
        self.assertEqual(self.service.request("refresh")["buddyImport"]["added"], 0)
        with path.open("ab") as stream:
            stream.write(tool[40:] + follow)
        appended = self.service.request("refresh")["buddyImport"]
        self.assertEqual(appended["added"], 1)
        self.assertEqual(self.rows()["m2"]["duration"], 2)
        restarted = self.make_service()
        self.assertEqual(restarted.request("refresh")["buddyImport"]["added"], 0)
        self.assertEqual(self.rows()["m1"]["duration"], 5)
        self.assertEqual(self.rows()["m2"]["duration"], 2)
        path.write_bytes(b"".join(path.read_bytes().splitlines(keepends=True)[:2]))
        truncated = self.service.request("refresh")["buddyImport"]
        self.assertEqual((truncated["removed"], self.details()["total"]), (1, 1))
        self.assertEqual(self.rows()["m1"]["duration"], 5)

    def test_old_schema_upgrades_and_backfills_once(self):
        path = self.write(self.codebuddy, "old.jsonl",
                          line("S", "u1", self.at, kind="message", role="user", usage=False),
                          line("S", "a1", self.at + timedelta(seconds=5), parent="u1",
                               kind="message", role="assistant", message="m1"))
        with path.open("rb") as stream:
            rows = [json.loads(value) for value in stream.read().decode("utf-8").splitlines()]
        record, _ = parse_record(rows[1], "codebuddy-cli")
        data = record.data()
        data.pop("traceId", None)
        data.pop("boundaryDuration", None)
        stat_value = path.stat()
        prefix = hashlib.sha256(path.read_bytes()[:512]).hexdigest()
        cache = self.root / "cache"
        cache.mkdir(exist_ok=True)
        with closing(sqlite3.connect(cache / "buddy-index.sqlite3")) as db:
            db.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE files (
                source TEXT, path TEXT, size INTEGER, mtime INTEGER, identity TEXT,
                prefix TEXT, prefix_len INTEGER, offset INTEGER, skipped INTEGER,
                invalid INTEGER, PRIMARY KEY(source, path));
            CREATE TABLE observations (path TEXT, key TEXT, data TEXT NOT NULL,
                PRIMARY KEY(path, key));
            CREATE TABLE requests (
                key TEXT PRIMARY KEY, source TEXT NOT NULL, session_id TEXT,
                at REAL, model TEXT, input INTEGER, output INTEGER, cache INTEGER,
                cache_known INTEGER, credit REAL, credit_valid INTEGER,
                path TEXT, data TEXT NOT NULL);
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, requested TEXT, actual TEXT, status TEXT,
                completed INTEGER, outcome TEXT, order_at REAL, data TEXT NOT NULL);
            PRAGMA user_version=1;
            """)
            db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                       ("codebuddy-cli", str(path), stat_value.st_size,
                        stat_value.st_mtime_ns, str((stat_value.st_dev, stat_value.st_ino)),
                        prefix, min(512, stat_value.st_size), stat_value.st_size, 0, 0))
            db.execute("INSERT INTO observations VALUES (?,?,?)",
                       (str(path), record.key, json.dumps(data)))
            db.execute("""INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (record.key, "codebuddy-cli", record.session_id, record.at,
                        record.model, record.input_tokens, record.output_tokens,
                        record.cached_input_tokens, 1, record.credit, 1, str(path),
                        json.dumps(data)))
            db.commit()
        result = self.service.request("refresh")
        imported = result["buddyImport"]
        self.assertEqual(imported["added"], 0)
        self.assertEqual(imported["warnings"], [])
        self.assertEqual(self.details()["total"], 1)
        self.assertAlmostEqual(self.rows()["m1"]["duration"], 5)
        with closing(sqlite3.connect(cache / "buddy-index.sqlite3")) as db:
            names = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
            self.assertIn("timing_source", names)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)

    def test_trace_and_jsonl_contents_never_reach_sqlite(self):
        self.write(self.codebuddy, "privacy.jsonl",
                   line("S", "u1", self.at, kind="message", role="user", usage=False),
                   line("S", "a1", self.at + timedelta(seconds=5), parent="u1",
                        kind="message", role="assistant", message="m1"))
        self.traces("privacy.json", trace_document("turn-a", "m1",
                                                    self.at + timedelta(seconds=1),
                                                    self.at + timedelta(seconds=4)))
        self.service.request("refresh")
        database = (self.root / "cache" / "buddy-index.sqlite3").read_bytes()
        self.assertNotIn(SECRET.encode(), database)
        self.assertNotIn(TOOL_SECRET.encode(), database)


if __name__ == "__main__":
    unittest.main()
