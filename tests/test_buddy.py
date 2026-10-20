"""Synthetic checks for WorkBuddy desktop / CodeBuddy CLI parsing and indexing."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native"))
from codex_monitor.service import Service
from codex_monitor.buddy_store import BuddyStore
from codex_monitor.buddy import run_outcome
from codex_monitor.pricing import buddy_cost
from codex_monitor.host import MAX_FRAME
from codex_monitor.store import encode

SECRET = "SECRET-聊天正文-不得保存"
TOOL_SECRET = "SECRET-工具参数"
POLICY_SECRET = "SECRET-policy-task-command"
RESULT_SECRET = "SECRET-result-text"


def line(session, message, at, *, source="codebuddy", model="deepseek-v4.1-flash",
         input_tokens=1200, output_tokens=80, cache=900, credit=0.06,
         conversation="turn-a", line_id=None, raw=True, secrets=True, message_id=True,
         usage_cache=True):
    value = {
        "id": line_id or f"{session}-{message}-{int(at.timestamp())}",
        "sessionId": session,
        "timestamp": int(at.timestamp() * 1000),
        "type": "function_call",
        "providerData": {
            "model": model,
            "conversationRequestId": conversation,
        },
        "message": {"usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}},
    }
    if message_id:
        value["providerData"]["messageId"] = message
    if cache is not None and usage_cache:
        value["message"]["usage"]["cache_read_input_tokens"] = cache
    if raw:
        value["providerData"]["rawUsage"] = {
            "prompt_tokens": input_tokens, "completion_tokens": output_tokens,
            "prompt_cache_hit_tokens": cache, "credit": credit,
        }
    if secrets:
        value["message"]["content"] = SECRET
        value["providerData"]["toolArgs"] = TOOL_SECRET
        value["title"] = SECRET
    return (json.dumps(value, ensure_ascii=False) + "\n").encode()


def run_files(bridge, name, requested, actual, completed, status="completed", secret=False,
              actual_models=None, result=True):
    folder = Path(bridge) / name
    folder.mkdir(parents=True, exist_ok=True)
    policy = {"resume": requested, "cwd": "C:/work", "task": POLICY_SECRET}
    value = {"run_id": "run-" + name, "session_id": actual,
             "status": status, "completed": completed,
             "actual_models": actual_models or ["deepseek-v4.1-flash"]}
    if secret:
        value["result"] = RESULT_SECRET
    (folder / "policy.json").write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    if result:
        (folder / "result.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class BuddyTests(unittest.TestCase):
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

    def details(self, source, **extra):
        return self.service.request("details", self.query | {"source": source} | extra)

    def stats(self, source, **extra):
        return self.service.request("stats", self.query | {"source": source} | extra)

    def test_message_dedupe_cache_credit_and_privacy(self):
        path = self.write(self.codebuddy, "session-a.jsonl",
                          line("s-a", "m-1", self.at),
                          line("s-a", "m-1", self.at + timedelta(seconds=1), line_id="copy"))
        result = self.service.request("refresh")
        self.assertEqual((result["buddyImport"]["added"], result["buddyImport"]["duplicates"]), (1, 1))
        details = self.details("codebuddy-cli")
        self.assertEqual(details["total"], 1)
        row = details["rows"][0]
        self.assertEqual((row["input"], row["output"], row["cacheHit"]), (1200, 80, .75))
        self.assertEqual((row["credit"], row["creditValid"]), (.06, True))
        expected = buddy_cost({"model": row["model"], "at": row["at"],
                               "input": 1200, "output": 80, "cache": 900})
        self.assertIsNotNone(expected)
        self.assertAlmostEqual(row["cost"], expected)
        self.assertIsNone(row["speed"])
        self.assertIsNone(row["firstToken"])
        self.assertEqual(row["session"]["outcome"], "unknown")
        stats = self.stats("codebuddy-cli")
        self.assertEqual((stats["cards"]["records"], stats["cards"]["cacheHit"]), (1, .75))
        self.assertAlmostEqual(stats["cards"]["cost"], expected)
        self.assertEqual((stats["cards"]["credit"], stats["cards"]["creditCount"]), (.06, 1))
        group = [row for row in stats["rows"] if row["records"]][0]
        self.assertEqual(group["mode"], "")
        self.assertEqual(group["source"], "codebuddy-cli")
        database = (self.root / "cache" / "buddy-index.sqlite3").read_bytes()
        self.assertNotIn(SECRET.encode(), database)
        self.assertNotIn(TOOL_SECRET.encode(), database)
        self.assertNotIn(SECRET.encode(), json.dumps(details, ensure_ascii=False).encode())

    def test_conversation_id_alone_does_not_merge_model_calls(self):
        self.write(self.codebuddy, "calls.jsonl",
                   line("s-c", "call-1", self.at, conversation="one-turn", line_id="rec-1",
                        message_id=False, input_tokens=100, output_tokens=10, cache=0),
                   line("s-c", "call-2", self.at + timedelta(seconds=1),
                        conversation="one-turn", line_id="rec-2", message_id=False,
                        input_tokens=200, output_tokens=20, cache=0))
        result = self.service.request("refresh")
        self.assertEqual(result["buddyImport"]["added"], 2)
        self.assertEqual(self.details("codebuddy-cli")["total"], 2)
        with (self.codebuddy / "calls.jsonl").open("ab") as stream:
            stream.write(line("s-c", "call-1", self.at, conversation="one-turn",
                              line_id="rec-1", message_id=False, input_tokens=100, output_tokens=10,
                              cache=0))
        result = self.service.request("refresh")
        self.assertEqual((result["buddyImport"]["added"], result["buddyImport"]["updated"]), (0, 0))
        self.assertEqual(self.details("codebuddy-cli")["total"], 2)

    def test_raw_cache_fallback_and_unknown_cache(self):
        self.write(self.codebuddy, "cache.jsonl",
                   line("s-cache", "fallback", self.at, cache=900, usage_cache=False),
                   line("s-cache", "unknown", self.at + timedelta(minutes=1), cache=None,
                        usage_cache=False))
        self.service.request("refresh")
        rows = {row["messageId"]: row for row in self.details("codebuddy-cli")["rows"]}
        self.assertEqual((rows["fallback"]["cacheHit"], rows["fallback"]["cacheKnown"]), (.75, True))
        self.assertEqual(rows["fallback"]["input"], 1200)
        self.assertEqual((rows["unknown"]["cacheHit"], rows["unknown"]["cacheKnown"]), (None, False))
        cards = self.stats("codebuddy-cli")["cards"]
        self.assertIsNone(cards["cacheHit"])
        self.assertEqual((cards["cacheKnown"], cards["records"]), (1, 2))

    def test_non_usage_records_are_ignored_not_counted_invalid(self):
        self.write(self.codebuddy, "structural.jsonl",
                   (json.dumps({"id": "user", "sessionId": "s", "timestamp": int(self.at.timestamp() * 1000),
                                "type": "message", "message": {"role": "user", "content": SECRET}}) + "\n").encode(),
                   (json.dumps({"id": "assistant", "sessionId": "s",
                                "timestamp": int(self.at.timestamp() * 1000),
                                "type": "message", "message": {"role": "assistant", "content": ""}}) + "\n").encode(),
                   (json.dumps({"id": "tool", "sessionId": "s",
                                "timestamp": int(self.at.timestamp() * 1000),
                                "type": "function_call_output", "message": {}}) + "\n").encode(),
                   line("s", "usage", self.at, line_id="usage"))
        result = self.service.request("refresh")
        self.assertEqual(result["buddyImport"]["added"], 1)
        self.assertEqual(result["buddyImport"]["invalidValues"], 0)
        self.assertEqual(result["buddyImport"]["skippedLines"], 0)

    def test_duplicate_append_does_not_change_data_version(self):
        path = self.write(self.codebuddy, "stable.jsonl", line("s-stable", "m-stable", self.at))
        first = self.service.request("refresh")
        version = first["status"]["version"]
        with path.open("ab") as stream:
            stream.write(line("s-stable", "m-stable", self.at))
        second = self.service.request("refresh")
        self.assertEqual((second["buddyImport"]["added"], second["buddyImport"]["updated"]), (0, 0))
        self.assertEqual(second["status"]["version"], version)
        self.assertEqual(self.details("codebuddy-cli")["total"], 1)

    def test_bridge_status_requires_completed_status_and_reports_failures(self):
        self.write(self.codebuddy, "status.jsonl", line("S-status", "m-status", self.at))
        run_files(self.bridge, "1-resumed", "S-status", "S-status", True,
                  status="completed", actual_models=["deepseek-v4.1-flash"])
        run_files(self.bridge, "2-denied", "S-status", "S-status", True,
                  status="permission_denied")
        run_files(self.bridge, "3-unconfirmed", "S-status", "S-status", False,
                  status="completed")
        run_files(self.bridge, "4-pending", "S-status", None, False, result=False)
        result = self.service.request("refresh")
        row = self.details("codebuddy-cli")["rows"][0]
        session = row["session"]
        self.assertEqual(session["outcome"], "failed")
        self.assertEqual((session["runCount"], session["confirmedResumeCount"],
                          session["failedCount"], session["unconfirmedCount"]), (4, 1, 1, 1))
        self.assertEqual(session["pendingCount"], 1)
        self.assertEqual(session["actualSessionId"], "S-status")
        self.assertTrue(any(run["observedModels"] == ["deepseek-v4.1-flash"] for run in session["runs"]))
        bridge = result["status"]["buddy"]["bridge"]
        self.assertEqual((bridge["pending"], bridge["failed"], bridge["resumed"]), (1, 1, 1))

    def test_credit_applicability_excludes_codex(self):
        from test_native import request as codex_request
        (self.home / "sessions" / "codex.jsonl").write_bytes(codex_request("c-credit", self.at))
        self.write(self.codebuddy, "credit.jsonl", line("S-credit", "m-credit", self.at))
        self.service.request("refresh")
        all_cards = self.stats("all")["cards"]
        self.assertEqual((all_cards["records"], all_cards["codexRecords"]), (2, 1))
        self.assertEqual((all_cards["creditApplicable"], all_cards["creditCount"],
                          all_cards["creditMissing"]), (1, 1, 0))
        codex_cards = self.stats("codex")["cards"]
        self.assertEqual((codex_cards["creditApplicable"], codex_cards["creditMissing"]), (0, 0))
        self.assertEqual((codex_cards["priced"], codex_cards["codexRecords"]), (1, 1))
        self.assertIsNone(self.details("codebuddy-cli")["rows"][0]["speed"])

    def test_removed_source_root_clears_stale_records(self):
        self.write(self.codebuddy, "gone.jsonl", line("S-gone", "m-gone", self.at))
        self.service.request("refresh")
        self.assertEqual(self.details("codebuddy-cli")["total"], 1)
        import shutil
        shutil.rmtree(self.codebuddy)
        result = self.service.request("refresh")
        self.assertEqual(result["buddyImport"]["removed"], 1)
        self.assertEqual(self.details("codebuddy-cli")["total"], 0)
        self.assertFalse(result["status"]["buddy"]["sources"]["codebuddy-cli"]["exists"])

    def test_unknown_cache_is_not_zero_and_zero_cache_is_valid(self):
        self.write(self.workbuddy, "unknown.jsonl", line("u-a", "m-a", self.at, cache=None))
        self.service.request("refresh")
        cards = self.stats("workbuddy-desktop")["cards"]
        self.assertIsNone(cards["cacheHit"])
        self.assertEqual((cards["cacheKnown"], cards["records"]), (0, 1))
        self.assertIsNone(self.details("workbuddy-desktop")["rows"][0]["cacheHit"])
        # A second, separate file proves explicit zero is retained as a known value.
        self.write(self.workbuddy, "zero.jsonl", line("u-b", "m-b", self.at + timedelta(minutes=1), cache=0))
        self.service.request("refresh")
        self.assertIsNone(self.stats("workbuddy-desktop")["cards"]["cacheHit"])
        self.assertEqual(self.stats("workbuddy-desktop")["cards"]["cacheKnown"], 1)
        zero = [row for row in self.details("workbuddy-desktop")["rows"] if row["sessionId"] == "u-b"][0]
        self.assertEqual((zero["cacheHit"], zero["cacheKnown"]), (0, True))

    def test_sources_and_sessions_never_merge(self):
        self.write(self.workbuddy, "a.jsonl", line("shared-session", "same-message", self.at))
        self.write(self.codebuddy, "a.jsonl", line("shared-session", "same-message", self.at))
        self.write(self.codebuddy, "b.jsonl", line("other-session", "same-message", self.at + timedelta(minutes=1)))
        self.service.request("refresh")
        self.assertEqual(self.details("codex")["total"], 0)
        self.assertEqual(self.details("workbuddy-desktop")["total"], 1)
        self.assertEqual(self.details("codebuddy-cli")["total"], 2)
        combined = self.details("all")
        self.assertEqual(combined["total"], 3)
        self.assertEqual({row["sourceId"] for row in combined["rows"]},
                         {"workbuddy-desktop", "codebuddy-cli"})
        self.assertEqual(len({row["key"] for row in combined["rows"]}), 3)
        rows = self.stats("all")["rows"]
        self.assertEqual({row["source"] for row in rows if row["records"]},
                         {"workbuddy-desktop", "codebuddy-cli"})

    def test_half_line_restart_archive_update_and_truncation(self):
        path = self.write(self.codebuddy, "incremental.jsonl",
                          line("s-i", "m-1", self.at),
                          line("s-i", "m-2", self.at + timedelta(minutes=1))[:80])
        first = self.service.request("refresh")["buddyImport"]
        self.assertEqual(first["added"], 1)
        full = line("s-i", "m-2", self.at + timedelta(minutes=1))
        with path.open("ab") as stream:
            stream.write(full[80:] + line("s-i", "m-3", self.at + timedelta(minutes=2)))
        second = self.service.request("refresh")["buddyImport"]
        self.assertEqual(second["added"], 2)
        restarted = self.make_service()
        self.assertEqual(restarted.request("refresh")["buddyImport"]["added"], 0)
        archive = self.codebuddy / "archive" / "copy.jsonl"
        archive.parent.mkdir()
        archive.write_bytes(path.read_bytes())
        self.assertEqual(self.service.request("refresh")["buddyImport"]["added"], 0)
        with path.open("ab") as stream:
            stream.write(line("s-i", "m-1", self.at, input_tokens=5000, output_tokens=100,
                              cache=0, credit=0))
        updated = self.service.request("refresh")["buddyImport"]
        self.assertEqual((updated["added"], updated["updated"]), (0, 1))
        row = [r for r in self.details("codebuddy-cli")["rows"] if r["messageId"] == "m-1"][0]
        self.assertEqual((row["input"], row["cacheHit"]), (5000, 0))
        path.write_bytes(line("s-i", "m-1", self.at, input_tokens=5000, output_tokens=100,
                              cache=0, credit=0))
        archive.unlink()
        truncated = self.service.request("refresh")["buddyImport"]
        self.assertEqual((truncated["removed"], truncated["added"]), (2, 0))
        self.assertEqual(self.details("codebuddy-cli")["total"], 1)

    def test_bridge_resume_mismatch_new_unknown_and_metadata_version(self):
        self.write(self.codebuddy, "s.jsonl",
                   line("S1", "m-1", self.at),
                   line("S2", "m-2", self.at + timedelta(minutes=1)),
                   line("S3", "m-3", self.at + timedelta(minutes=2)))
        run_files(self.bridge, "1-new", None, "S1", True)
        run_files(self.bridge, "2-resume", "S1", "S1", True)
        run_files(self.bridge, "3-mismatch", "S1", "S2", True)
        run_files(self.bridge, "4-unconfirmed", "S1", "S1", False, status="running")
        run_files(self.bridge, "5-secret", None, "S3", False, status="running", secret=True)
        first = self.service.request("refresh")
        self.assertTrue(first["buddyImport"]["bridge"]["changed"])
        rows = self.details("codebuddy-cli")["rows"]
        row = [item for item in rows if item["sessionId"] == "S1"][0]
        session = row["session"]
        self.assertEqual(session["outcome"], "resumed")
        self.assertEqual((session["runCount"], session["confirmedResumeCount"],
                          session["newSessionCount"], session["unconfirmedCount"]), (3, 1, 1, 1))
        self.assertEqual(session["contextCompression"], None)
        self.assertTrue(session["runs"])
        mismatched = [r for r in rows if r["sessionId"] == "S2"]
        self.assertTrue(mismatched)
        self.assertEqual(mismatched[0]["session"]["mismatchCount"], 1)
        self.assertEqual([r for r in rows if r["sessionId"] == "S3"][0]["session"]["outcome"], "unknown")
        status = first["status"]
        self.assertEqual((status["buddy"]["bridge"]["runs"], status["buddy"]["bridge"]["sessions"]), (5, 3))
        self.assertGreaterEqual(status["buddy"]["bridge"]["mismatch"], 1)
        version = status["version"]
        self.assertNotIn(POLICY_SECRET.encode(), (self.root / "cache" / "buddy-index.sqlite3").read_bytes())
        self.assertNotIn(RESULT_SECRET.encode(), (self.root / "cache" / "buddy-index.sqlite3").read_bytes())
        # Bridge metadata changes without any new JSONL request must publish a version.
        result = json.loads((self.bridge / "1-new" / "result.json").read_text(encoding="utf-8"))
        result["status"] = "failed"
        (self.bridge / "1-new" / "result.json").write_text(json.dumps(result), encoding="utf-8")
        changed = self.service.request("refresh")
        self.assertEqual(changed["import"]["added"], 0)
        self.assertTrue(changed["buddyImport"]["bridge"]["changed"])
        self.assertEqual(changed["status"]["version"], version + 1)
        self.assertGreaterEqual(changed["status"]["buddy"]["bridge"]["failed"], 1)

    def test_mode_filter_applies_only_to_codex(self):
        from test_native import request as codex_request
        (self.home / "sessions" / "codex.jsonl").write_bytes(codex_request("c-1", self.at, tier="priority"))
        self.write(self.codebuddy, "b.jsonl", line("S-mode", "m-mode", self.at))
        self.service.request("refresh")
        self.assertEqual(self.stats("codex")["cards"]["records"], 1)
        fast = self.stats("all", mode="fast")
        self.assertEqual((fast["cards"]["records"], fast["cards"]["timed"]), (2, 1))
        self.assertEqual({row["source"] for row in fast["rows"] if row["records"]},
                         {"codex", "codebuddy-cli"})
        standard = self.stats("all", mode="standard")
        self.assertEqual(standard["cards"]["records"], 1)
        buddy_fast = self.stats("codebuddy-cli", mode="fast")
        self.assertEqual(buddy_fast["cards"]["records"], 1)
        self.assertEqual(buddy_fast["cards"]["timed"], 0)

    def test_sorting_and_paging_merge_nulls_last(self):
        from test_native import request as codex_request
        (self.home / "sessions" / "codex.jsonl").write_bytes(
            codex_request("c-1", self.at - timedelta(minutes=2)))
        self.write(self.codebuddy, "b.jsonl", line("S-sort", "m-sort", self.at,
                                                   model="unknown-model"))
        self.service.request("refresh")
        by_time = self.details("all", sort="time", direction="desc")
        self.assertEqual([row["sourceId"] for row in by_time["rows"]], ["codebuddy-cli", "codex"])
        by_speed = self.details("all", sort="speed", direction="desc")
        self.assertEqual([row["sourceId"] for row in by_speed["rows"]], ["codex", "codebuddy-cli"])
        by_cost = self.details("all", sort="cost", direction="asc")
        self.assertEqual(by_cost["rows"][-1]["sourceId"], "codebuddy-cli")
        paged = self.details("all", page=1, pageSize=20)
        self.assertEqual((paged["total"], len(paged["rows"])), (2, 2))

    def test_invalid_values_are_skipped_and_reported(self):
        bad = line("S-bad", "m-bad", self.at)
        value = json.loads(bad)
        value["message"]["usage"]["input_tokens"] = -1
        bad_negative = (json.dumps(value) + "\n").encode()
        value = json.loads(line("S-bad", "m-bad2", self.at))
        value["message"]["usage"]["cache_read_input_tokens"] = 99999
        bad_cache = (json.dumps(value) + "\n").encode()
        value = json.loads(line("S-bad", "m-bad3", self.at))
        value["timestamp"] = 1
        bad_time = (json.dumps(value) + "\n").encode()
        self.write(self.codebuddy, "bad.jsonl", bad_negative, bad_cache, bad_time,
                   b"not-json\n", line("S-ok", "m-ok", self.at))
        result = self.service.request("refresh")
        imported = result["buddyImport"]
        self.assertEqual(imported["added"], 1)
        self.assertGreaterEqual(imported["invalidValues"], 3)
        self.assertGreaterEqual(imported["skippedLines"], 1)
        self.assertEqual(self.details("codebuddy-cli")["total"], 1)

    def test_directory_read_failure_preserves_index_and_success_time(self):
        self.write(self.codebuddy, "readable.jsonl", line("S-read", "m-read", self.at))
        first = self.service.request("refresh")
        with patch.object(BuddyStore, "_scan", side_effect=PermissionError("synthetic denial")):
            second = self.service.request("refresh")
        self.assertTrue(second["buddyImport"]["warnings"])
        self.assertEqual(second["status"]["version"], first["status"]["version"])
        self.assertEqual(second["status"]["buddy"]["lastSuccess"], first["status"]["buddy"]["lastSuccess"])
        self.assertEqual(self.details("codebuddy-cli")["total"], 1)

    def test_mismatch_is_visible_even_when_the_run_failed(self):
        self.assertEqual(run_outcome("requested", "different", False, "session_mismatch"), "mismatch")

    def test_missing_policy_and_invalid_results_stay_unknown(self):
        self.write(self.codebuddy, "evidence.jsonl", line("S-evidence", "message", self.at))
        run_files(self.bridge, "only-result", None, "S-evidence", True)
        (self.bridge / "only-result" / "policy.json").unlink()
        for name, text in (("empty", ""), ("bad", "{"), ("large", "x" * (512 * 1024 + 1))):
            run_files(self.bridge, name, "S-evidence", None, False)
            (self.bridge / name / "result.json").write_text(text, encoding="utf-8")
        result = self.service.request("refresh")
        session = self.details("codebuddy-cli")["rows"][0]["session"]
        self.assertEqual((session["outcome"], session["newSessionCount"]), ("unknown", 0))
        self.assertEqual(result["status"]["buddy"]["bridge"]["pending"], 0)
        self.assertEqual(result["status"]["buddy"]["bridge"]["unknown"], 4)
        run_files(self.bridge, "empty-policy", None, "S-evidence", True)
        (self.bridge / "empty-policy" / "policy.json").write_text("{}", encoding="utf-8")
        self.service.request("refresh")
        self.assertEqual(self.details("codebuddy-cli")["rows"][0]["session"]["newSessionCount"], 0)
        run_files(self.bridge, "legacy", "S-evidence", "S-evidence", True)
        path = self.bridge / "legacy" / "result.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["observed_models"] = value.pop("actual_models")
        path.write_text(json.dumps(value), encoding="utf-8")
        self.service.request("refresh")
        self.assertEqual(self.details("codebuddy-cli")["rows"][0]["session"]["confirmedResumeCount"], 1)

    def test_broken_optional_index_does_not_block_codex(self):
        from test_native import request as codex_request
        (self.home / "sessions" / "codex.jsonl").write_bytes(codex_request("codex-ok", self.at))
        cache = self.root / "cache"
        cache.mkdir(exist_ok=True)
        (cache / "buddy-index.sqlite3").write_bytes(b"synthetic broken sqlite")
        result = self.service.request("refresh")
        self.assertTrue(result["buddyImport"]["warnings"])
        data = self.stats("codex")
        self.assertEqual(data["cards"]["records"], 1)
        self.assertTrue(data["status"]["buddy"]["warning"])

    def test_combined_paging_keeps_every_request_once(self):
        from test_native import request as codex_request
        (self.home / "sessions" / "paged.jsonl").write_bytes(b"".join(
            codex_request("codex-" + str(i), self.at + timedelta(seconds=i * 20)) for i in range(31)))
        self.write(self.codebuddy, "paged.jsonl", *[
            line("S-paged", str(i), self.at + timedelta(seconds=i * 21)) for i in range(27)])
        self.service.request("refresh")
        for sort in ("time", "cost", "speed", "firstToken"):
            for direction in ("asc", "desc"):
                whole = self.details("all", pageSize=100, sort=sort, direction=direction)
                pages = [self.details("all", pageSize=20, page=p, sort=sort, direction=direction)
                         for p in range(1, 4)]
                keys = [row["key"] for data in pages for row in data["rows"]]
                self.assertEqual(len(set(keys)), 58)
                self.assertEqual(keys, [row["key"] for row in whole["rows"]])
                self.assertTrue(all(data["total"] == 58 for data in pages))

    def test_optional_refresh_and_transaction_failure_preserve_codex(self):
        from test_native import request as codex_request
        (self.home / "sessions" / "codex.jsonl").write_bytes(codex_request("still-available", self.at))
        with patch.object(BuddyStore, "refresh", side_effect=sqlite3.OperationalError("synthetic write failure")):
            result = self.service.request("refresh")
        self.assertTrue(result["buddyImport"]["warnings"])
        fake = Mock()
        fake.db.execute.side_effect = sqlite3.OperationalError("synthetic read failure")
        with patch.object(self.service, "buddy_store", return_value=fake):
            data = self.stats("codex")
        self.assertEqual(data["cards"]["records"], 1)
        self.assertTrue(data["status"]["buddy"]["warning"])
        fake.close.assert_called_once()

    def test_long_session_evidence_fits_a_full_native_page(self):
        session_id = "S" * 240
        self.write(self.codebuddy, "many.jsonl", *[
            line(session_id, str(i).ljust(240, "M"), self.at, model="model".ljust(160, "x"))
            for i in range(100)])
        for i in range(20):
            run_files(self.bridge, str(i), session_id, session_id, True,
                      actual_models=[str(n).ljust(160, "x") for n in range(50)])
        self.service.request("refresh")
        data = self.details("codebuddy-cli", pageSize=100)
        self.assertEqual(len(data["rows"]), 100)
        summary = data["rows"][0]["session"]
        self.assertEqual((summary["runCount"], summary["matchedResumeCount"]), (20, 20))
        self.assertTrue(summary["runsTruncated"])
        self.assertEqual(len(summary["runs"]), 3)
        self.assertLess(len(encode({"ok": True, "data": data}).encode("utf-8")), MAX_FRAME)

    def test_configure_validation_and_missing_directories(self):
        empty = self.root / "empty"
        empty.mkdir()
        service = Service(self.root / "cache2", self.home)
        status = service.request("status")
        self.assertFalse(status["buddy"]["enabled"])
        result = service.request("configure", {"workbuddy": str(empty), "codebuddy": str(empty),
                                               "bridge": str(empty)})
        self.assertEqual(Path(result["buddy"]["workbuddy"]), empty.resolve())
        status = service.request("status")
        self.assertTrue(status["buddy"]["enabled"])
        self.assertTrue(status["buddy"]["sources"]["workbuddy-desktop"]["exists"])
        self.assertEqual(status["buddy"]["sources"]["workbuddy-desktop"]["records"], 0)
        empty.rmdir()
        status = service.request("status")
        self.assertFalse(status["buddy"]["sources"]["workbuddy-desktop"]["exists"])
        service.request("refresh")
        with self.assertRaises(ValueError):
            service.request("configure", {"workbuddy": 3})
        file_path = self.root / "not-a-directory.txt"
        file_path.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            service.request("configure", {"workbuddy": str(file_path)})
        with self.assertRaises(ValueError):
            service.request("configure", {"home": str(file_path)})


if __name__ == "__main__":
    unittest.main()
