"""Synthetic checks for Buddy diagnostic first-token metadata."""
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from codex_monitor.buddy_store import BuddyStore
from codex_monitor.service import Service
from test_native import event

SECRET = "SECRET-诊断正文-不得保存"
TOOL_SECRET = "SECRET-诊断工具参数"


def usage_line(session, message, at, *, model="deepseek-v4.1-flash",
               input_tokens=1200, output_tokens=80, cache=900,
               conversation="turn-a", message_id=True):
    value = {
        "id": session + "-" + message,
        "sessionId": session,
        "timestamp": int(at.timestamp() * 1000),
        "type": "function_call",
        "providerData": {"model": model, "conversationRequestId": conversation},
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache,
            },
            "content": SECRET,
        },
    }
    if message_id:
        value["providerData"]["messageId"] = message
    value["providerData"]["toolArgs"] = TOOL_SECRET
    return (json.dumps(value, ensure_ascii=False) + "\n").encode()


def plain(request_id, milliseconds, extra=""):
    return ("[2026/10/19 10:00:01.000] [Info] [ModelProvider] "
            "First meaningful token received: agent=cli, requestId="
            + request_id + ", ttft=" + str(milliseconds) + "ms, rawChunks=2, bytes=664"
            + extra + "\n").encode()


def completed(request_id, session="S1", milliseconds=1233, duration=5000,
              model="deepseek-v4.1-flash", input_tokens=1200, output_tokens=80):
    return {
        "model": model, "step": 1, "attempt_no": 1,
        "time_to_first_token_ms": milliseconds,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cached_read_tokens": 900, "cached_write_tokens": 0,
        "finish_reason": "stop", "event_id": "ev-" + request_id,
        "event_name": "agent.model_call.completed", "service": "cli",
        "msg": "model call completed", "result": "success",
        "duration_ms": duration, "stage": "stop",
        "conversation_id": session,
        "conversation_request_id": "turn-" + session,
        "conversation_message_id": request_id,
        "trace_id": "trace-" + request_id,
        "raw": SECRET,
    }


def structured(value, prefix=True):
    text = json.dumps(value, ensure_ascii=False)
    if prefix:
        text = "[2026/10/19 10:00:01.000] [Info] " + text
    return (text + "\n").encode()


def codex_turn(key, at, latency=1000, duration=30, output=100):
    body = event(at, "event_msg", {"type": "task_started"})
    body += event(at, "turn_context", {"model": "gpt-6.1-sol"})
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


class BuddyResponseTests(unittest.TestCase):
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

    def log(self, projects, name, *data):
        return self.write(Path(projects).parent / "logs", name, *data)

    def details(self, source="codebuddy-cli", **extra):
        return self.service.request("details", self.query | {"source": source} | extra)

    def stats(self, source="codebuddy-cli", **extra):
        return self.service.request("stats", self.query | {"source": source} | extra)

    def rows(self, source="codebuddy-cli"):
        return {row["messageId"]: row for row in self.details(source)["rows"]}

    def test_plain_unique_join_late_version_and_cards(self):
        self.write(self.codebuddy, "usage.jsonl", usage_line("S1", "m-1", self.at))
        first = self.service.request("refresh")
        self.assertFalse(first["status"]["buddy"]["responseBackfillPending"])
        self.assertIsNone(self.rows()["m-1"]["firstToken"])
        self.assertEqual(self.stats()["cards"]["responseSamples"], 0)
        version = first["status"]["version"]
        # A file under an older date is still discovered by the recursive scan.
        self.log(self.codebuddy, "2025/01/01/agent.log", plain("m-1", 1234))
        result = self.service.request("refresh")
        self.assertEqual(result["buddyImport"]["added"], 0)
        self.assertEqual(result["buddyImport"]["responseUpdated"], 1)
        self.assertEqual(result["status"]["version"], version + 1)
        row = self.rows()["m-1"]
        self.assertAlmostEqual(row["firstToken"], 1.234)
        self.assertEqual(row["firstTokenSource"], "first-token-log")
        self.assertEqual(row["firstTokenBasis"], "diagnostic-ttft")
        cards = self.stats()["cards"]
        self.assertEqual((cards["responseTurns"], cards["responseSamples"]), (1, 1))
        self.assertAlmostEqual(cards["responseMedian"], 1.234)
        self.assertAlmostEqual(cards["responseP90"], 1.234)
        group = [item for item in self.stats()["rows"] if item["records"]][0]
        self.assertEqual((group["responseTurns"], group["responseSamples"]), (1, 1))
        self.assertAlmostEqual(group["responseMedian"], 1.234)
        self.assertFalse(result["buddyImport"]["responseBackfillPending"])
        unchanged = self.service.request("refresh")
        self.assertEqual(unchanged["status"]["version"], version + 1)
        self.assertEqual(unchanged["buddyImport"]["diagnosticChangedFiles"], 0)

    def test_plain_ambiguity_is_per_source_and_structured_sessions_disambiguate(self):
        self.write(self.codebuddy, "usage.jsonl",
                   usage_line("S1", "m-shared", self.at),
                   usage_line("S2", "m-shared", self.at + timedelta(minutes=1)))
        self.write(self.workbuddy, "usage.jsonl",
                   usage_line("W1", "m-shared", self.at))
        self.service.request("refresh")
        self.log(self.codebuddy, "codebuddy.log", plain("m-shared", 1111))
        self.log(self.workbuddy, "workbuddy.log", plain("m-shared", 2222))
        self.service.request("refresh")
        self.assertTrue(all(row["firstToken"] is None for row in self.details()["rows"]))
        self.assertAlmostEqual(self.details("workbuddy-desktop")["rows"][0]["firstToken"], 2.222)
        # Structured session IDs identify each same-ID request in one source.
        self.log(self.codebuddy, "structured.log",
                 structured(completed("m-shared", "S1", 1000)),
                 structured(completed("m-shared", "S2", 2000)))
        self.service.request("refresh")
        values = {row["sessionId"]: row["firstToken"] for row in self.details()["rows"]}
        self.assertAlmostEqual(values["S1"], 1.0)
        self.assertAlmostEqual(values["S2"], 2.0)
        self.assertEqual({row["firstTokenSource"] for row in self.details()["rows"]},
                         {"completed-log"})

    def test_completed_is_preferred_and_conflicts_stay_unknown(self):
        self.write(self.codebuddy, "usage.jsonl",
                   usage_line("S1", "m-pref", self.at),
                   usage_line("S2", "m-conflict", self.at),
                   usage_line("S3", "m-plain-conflict", self.at))
        self.service.request("refresh")
        self.log(self.codebuddy, "preference.log",
                 plain("m-pref", 2000),
                 structured(completed("m-pref", "S1", 1233)),
                 structured(completed("m-conflict", "S2", 1000)),
                 structured(completed("m-conflict", "S2", 1001)),
                 plain("m-plain-conflict", 1000),
                 plain("m-plain-conflict", 1001))
        self.service.request("refresh")
        rows = self.rows()
        self.assertAlmostEqual(rows["m-pref"]["firstToken"], 1.233)
        self.assertEqual(rows["m-pref"]["firstTokenSource"], "completed-log")
        self.assertIsNone(rows["m-conflict"]["firstToken"])
        self.assertIsNone(rows["m-plain-conflict"]["firstToken"])

    def test_completed_stage_stop_is_accepted_and_preferred(self):
        self.write(self.codebuddy, "usage.jsonl",
                   usage_line("S1", "m-stage", self.at),
                   usage_line("S2", "m-unrelated", self.at),
                   usage_line("S3", "m-failed", self.at))
        self.service.request("refresh")
        unrelated = completed("m-unrelated", "S2", 500)
        unrelated["event_name"] = "agent.model_call.started"
        failed = completed("m-failed", "S3", 500)
        failed["result"] = "failed"
        sample = completed("m-stage", "S1", 1233, duration=5000)
        self.assertEqual(sample["stage"], "stop")
        self.log(self.codebuddy, "stage.log",
                 plain("m-stage", 2000),
                 structured(sample),
                 structured(unrelated),
                 structured(failed))
        self.service.request("refresh")
        rows = self.rows()
        self.assertAlmostEqual(rows["m-stage"]["firstToken"], 1.233)
        self.assertEqual(rows["m-stage"]["firstTokenSource"], "completed-log")
        self.assertIsNone(rows["m-unrelated"]["firstToken"])
        self.assertIsNone(rows["m-failed"]["firstToken"])

    def test_completed_validation_duration_zero_null_and_event_filters(self):
        names = ("m-zero", "m-null", "m-over", "m-negdur", "m-baddur",
                 "m-badttft", "m-bool", "m-fail", "m-model", "m-tokens",
                 "m-total", "m-session", "m-unrelated")
        self.write(self.codebuddy, "usage.jsonl",
                   *[usage_line("S1", name, self.at) for name in names])
        self.service.request("refresh")
        zero = completed("m-zero", "S1", 0, duration=0)
        null_duration = completed("m-null", "S1", 500)
        null_duration.pop("duration_ms")
        over = completed("m-over", "S1", 600, duration=500)
        negative_duration = completed("m-negdur", "S1", 500, duration=-1)
        bad_duration = completed("m-baddur", "S1", 500)
        bad_duration["duration_ms"] = "5000"
        bad_ttft = completed("m-badttft", "S1", -1)
        boolean_ttft = completed("m-bool", "S1", True)
        failure = completed("m-fail", "S1", 500)
        failure["result"] = "failed"
        model = completed("m-model", "S1", 500, model="other-model")
        tokens = completed("m-tokens", "S1", 500, input_tokens=1)
        total = completed("m-total", "S1", 500)
        total["total_tokens"] = 1
        session = completed("m-session", "S2", 500)
        unrelated = completed("m-unrelated", "S1", 500)
        unrelated["event_name"] = "agent.model_call.started"
        self.log(self.codebuddy, "validation.log",
                 structured(zero), structured(null_duration), structured(over),
                 structured(negative_duration), structured(bad_duration),
                 structured(bad_ttft), structured(boolean_ttft), structured(failure),
                 structured(model), structured(tokens), structured(total),
                 structured(session), structured(unrelated))
        result = self.service.request("refresh")
        rows = self.rows()
        self.assertEqual(rows["m-zero"]["firstToken"], 0.0)
        self.assertAlmostEqual(rows["m-null"]["firstToken"], 0.5)
        for name in ("m-over", "m-negdur", "m-baddur", "m-badttft", "m-bool",
                     "m-fail", "m-model", "m-tokens", "m-total", "m-session",
                     "m-unrelated"):
            self.assertIsNone(rows[name]["firstToken"], name)
        cards = self.stats()["cards"]
        self.assertEqual((cards["responseTurns"], cards["responseSamples"]),
                         (len(names), 2))
        self.assertGreaterEqual(result["buddyImport"]["diagnosticInvalid"], 2)

    def test_half_line_restart_archive_truncate_and_removal(self):
        self.write(self.codebuddy, "usage.jsonl",
                   usage_line("S1", "m-1", self.at),
                   usage_line("S1", "m-2", self.at + timedelta(minutes=1)))
        self.service.request("refresh")
        path = self.log(self.codebuddy, "incremental.log", plain("m-1", 1000))
        self.service.request("refresh")
        self.assertAlmostEqual(self.rows()["m-1"]["firstToken"], 1.0)
        full = plain("m-2", 2000)
        with path.open("ab") as stream:
            stream.write(full[:40])
        partial = self.service.request("refresh")
        self.assertEqual(partial["buddyImport"]["responseUpdated"], 0)
        self.assertIsNone(self.rows()["m-2"]["firstToken"])
        with path.open("ab") as stream:
            stream.write(full[40:])
        self.service.request("refresh")
        self.assertAlmostEqual(self.rows()["m-2"]["firstToken"], 2.0)
        restarted = self.make_service()
        before = restarted.request("refresh")
        self.assertEqual(before["buddyImport"]["diagnosticChangedFiles"], 0)
        self.assertAlmostEqual(restarted.request("details", self.query | {
            "source": "codebuddy-cli"})["rows"][0]["firstToken"], 2.0)
        archive = self.write(Path(self.codebuddy).parent / "logs", "archive/copy.log",
                             path.read_bytes())
        copied = self.service.request("refresh")
        self.assertEqual(copied["buddyImport"]["responseUpdated"], 0)
        self.assertAlmostEqual(self.rows()["m-2"]["firstToken"], 2.0)
        archive.unlink()
        path.write_bytes(plain("m-1", 1000))
        truncated = self.service.request("refresh")
        self.assertGreaterEqual(truncated["buddyImport"]["responseUpdated"], 1)
        self.assertAlmostEqual(self.rows()["m-1"]["firstToken"], 1.0)
        self.assertIsNone(self.rows()["m-2"]["firstToken"])
        path.unlink()
        self.service.request("refresh")
        self.assertIsNone(self.rows()["m-1"]["firstToken"])

    def test_mixed_source_first_token_sort_and_global_percentiles(self):
        (self.home / "sessions" / "codex.jsonl").write_bytes(
            codex_turn("c1", self.at, latency=1000)
            + codex_turn("c2", self.at + timedelta(minutes=1), latency=20000))
        self.write(self.codebuddy, "usage.jsonl",
                   usage_line("S1", "b-1", self.at,
                              ),
                   usage_line("S1", "b-2", self.at + timedelta(minutes=1)),
                   usage_line("S1", "b-3", self.at + timedelta(minutes=2)))
        self.log(self.codebuddy, "percentiles.log",
                 plain("b-1", 2000), plain("b-2", 30000))
        self.service.request("refresh")
        ascending = self.details("all", pageSize=20, sort="firstToken", direction="asc")
        self.assertEqual([row["firstToken"] for row in ascending["rows"]],
                         [1.0, 2.0, 20.0, 30.0, None])
        descending = self.details("all", pageSize=20, sort="firstToken", direction="desc")
        self.assertEqual([row["firstToken"] for row in descending["rows"]],
                         [30.0, 20.0, 2.0, 1.0, None])
        combined = self.stats("all")["cards"]
        self.assertEqual((combined["responseTurns"], combined["responseSamples"]), (5, 4))
        self.assertAlmostEqual(combined["responseMedian"], 11)
        self.assertEqual(combined["responseP90"], 30)
        codex = self.stats("codex")["cards"]
        self.assertEqual((codex["responseTurns"], codex["responseSamples"]), (2, 2))
        self.assertAlmostEqual(codex["responseMedian"], 10.5)
        buddy = self.stats("codebuddy-cli")["cards"]
        self.assertEqual((buddy["responseTurns"], buddy["responseSamples"]), (3, 2))
        self.assertAlmostEqual(buddy["responseMedian"], 16)
        self.assertEqual(buddy["responseP90"], 30)

    def test_v2_upgrade_backfills_existing_usage_without_reimport(self):
        self.write(self.codebuddy, "usage.jsonl", usage_line("S1", "m-1", self.at))
        self.service.request("refresh")
        database = self.root / "cache" / "buddy-index.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            db.execute("""UPDATE requests SET first_token=NULL,
                first_token_source=NULL,first_token_basis=NULL""")
            db.execute("DROP TABLE response_observations")
            db.execute("DROP TABLE response_logs")
            db.execute("DELETE FROM meta WHERE key='responseBackfill'")
            db.execute("PRAGMA user_version=2")
            db.commit()
        self.log(self.codebuddy, "upgrade.log", plain("m-1", 1234))
        upgraded = self.make_service()
        pending = upgraded.request("status")
        self.assertTrue(pending["buddy"]["responseBackfillPending"])
        result = upgraded.request("refresh")
        self.assertEqual(result["buddyImport"]["added"], 0)
        self.assertEqual(result["buddyImport"]["responseUpdated"], 1)
        self.assertFalse(result["buddyImport"]["responseBackfillPending"])
        row = upgraded.request("details", self.query | {"source": "codebuddy-cli"})["rows"][0]
        self.assertAlmostEqual(row["firstToken"], 1.234)

    def test_status_backfill_pending_shape_and_refresh_clears_it(self):
        self.write(self.codebuddy, "usage.jsonl", usage_line("S1", "m-1", self.at))
        self.log(self.codebuddy, "one.log", plain("m-1", 1234))
        self.service.request("refresh")
        database = self.root / "cache" / "buddy-index.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('responseBackfill','1')")
            db.commit()
        status = self.service.request("status")
        self.assertTrue(status["buddy"]["responseBackfillPending"])
        self.assertFalse(status["responseBackfillPending"])
        refreshed = self.service.request("refresh")
        self.assertFalse(refreshed["buddyImport"]["responseBackfillPending"])
        self.assertFalse(refreshed["status"]["buddy"]["responseBackfillPending"])

    def test_privacy_and_quoted_event_text_are_not_persisted_or_joined(self):
        self.write(self.codebuddy, "usage.jsonl", usage_line("S1", "m-1", self.at))
        quoted = ('text {"event_name":"agent.model_call.completed",'
                  '"conversation_message_id":"m-1","time_to_first_token_ms":9999,'
                  '"result":"success"}\n').encode()
        self.log(self.codebuddy, "privacy.log",
                 plain("m-1", 1234, ", note=" + SECRET),
                 quoted,
                 structured(completed("m-1", "S1", 1233)))
        result = self.service.request("refresh")
        row = self.rows()["m-1"]
        self.assertAlmostEqual(row["firstToken"], 1.233)
        database = (self.root / "cache" / "buddy-index.sqlite3").read_bytes()
        self.assertNotIn(SECRET.encode(), database)
        self.assertNotIn(TOOL_SECRET.encode(), database)
        self.assertNotIn(SECRET.encode(), json.dumps(result, ensure_ascii=False).encode())

    def test_unreadable_diagnostics_retain_evidence_and_report_warning(self):
        self.write(self.codebuddy, "usage.jsonl", usage_line("S1", "m-1", self.at))
        self.log(self.codebuddy, "one.log", plain("m-1", 1234))
        first = self.service.request("refresh")
        version = first["status"]["version"]
        with patch.object(BuddyStore, "_scan_logs",
                          side_effect=PermissionError("synthetic diagnostics denial")):
            failed = self.service.request("refresh")
        self.assertTrue(failed["buddyImport"]["warnings"])
        self.assertEqual(failed["status"]["version"], version)
        self.assertAlmostEqual(self.rows()["m-1"]["firstToken"], 1.234)

    def test_empty_source_model_and_date_filters(self):
        self.write(self.codebuddy, "usage.jsonl", usage_line("S1", "m-1", self.at))
        self.log(self.codebuddy, "one.log", plain("m-1", 1234))
        self.service.request("refresh")
        missing = self.stats("codebuddy-cli", model="missing-model")["cards"]
        self.assertEqual((missing["records"], missing["responseTurns"],
                          missing["responseSamples"]), (0, 0, 0))
        self.assertIsNone(missing["responseMedian"])
        self.assertIsNone(missing["responseP90"])
        outside = {"from": (self.at - timedelta(days=5)).date().isoformat(),
                   "to": (self.at - timedelta(days=4)).date().isoformat()}
        self.assertEqual(self.service.request("stats", outside)["cards"]["records"], 0)
        self.assertEqual(self.service.request("details", outside)["total"], 0)
        self.assertEqual(self.service.request("details", outside)["rows"], [])
        codex = self.stats("codex")["cards"]
        self.assertEqual((codex["records"], codex["responseSamples"]), (0, 0))
        workbuddy = self.stats("workbuddy-desktop")["cards"]
        self.assertEqual((workbuddy["records"], workbuddy["responseSamples"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
