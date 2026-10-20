"""Unparsed lines must stay visible instead of being silently dropped."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from test_native import request
from codex_monitor.service import Service


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "日志"
        (self.home / "sessions").mkdir(parents=True)
        self.path = self.home / "sessions" / "active.jsonl"
        self.at = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        self.yesterday = self.at - timedelta(days=1)
        self.service = Service(self.root / "cache", self.home)
        self.query = {"from": self.yesterday.date().isoformat(), "to": self.at.date().isoformat()}

    def tearDown(self):
        self.temp.cleanup()

    def test_unparsed_line_is_counted_without_hiding_valid_records(self):
        self.path.write_bytes(request("ok", self.at, output=100) + b"{broken\n")
        self.service.request("refresh")
        status = self.service.request("status")
        self.assertEqual(status["health"], {"skipped": 1, "invalid": 0})
        codex = next(item for item in status["sources"] if item["id"] == "codex")
        self.assertEqual((codex["records"], codex["skipped"]), (1, 1))
        rows = self.service.request("stats", self.query)["rows"]
        self.assertEqual(sum(row["records"] for row in rows), 1)

    def test_clean_log_reports_zero_health(self):
        self.path.write_bytes(request("ok", self.at, output=100))
        self.service.request("refresh")
        status = self.service.request("status")
        self.assertEqual(status["health"], {"skipped": 0, "invalid": 0})
        for item in status["sources"]:
            self.assertEqual((item["skipped"], item["invalid"]), (0, 0))

    def test_health_is_well_formed_before_first_refresh(self):
        status = self.service.request("status")
        self.assertEqual(status["health"], {"skipped": 0, "invalid": 0})
        for item in status["sources"]:
            self.assertEqual((item["skipped"], item["invalid"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
