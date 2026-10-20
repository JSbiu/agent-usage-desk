"""Incrementally index WorkBuddy desktop and CodeBuddy CLI JSONL logs.

The source files are opened read-only. Only usage, time, model, request,
session, run identifiers, numeric timing facts and the necessary source path
are persisted.
"""
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from statistics import median
import stat
import time

from . import pricing
from .buddy import BRIDGE_FAILURES, SOURCES, parse_record, run_outcome, session_summary
from .buddy_response import TTFT_BASIS, parse_line
from .buddy_timing import Timing, TimingState, trace_timings
from .core import response_percentiles
from .store import encode, import_lock

SCHEMA = 3


class ParseSession:
    def __init__(self, source):
        self.source = source
        self.records = {}
        self.skipped = 0
        self.invalid = 0
        self.duplicates = 0


class BuddyStore:
    def __init__(self, cache, roots, bridge=None):
        self.cache = Path(cache).resolve()
        self.cache.mkdir(parents=True, exist_ok=True)
        self.roots = {source: Path(roots[source]).resolve()
                      for source in SOURCES if roots.get(source)}
        self.bridge = Path(bridge).resolve() if bridge else None
        self.db = sqlite3.connect(self.cache / "buddy-index.sqlite3", timeout=20)
        self.db.row_factory = sqlite3.Row
        try:
            self._initialize()
        except BaseException:
            self.db.close()
            raise

    def _initialize(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files (
                source TEXT, path TEXT, size INTEGER, mtime INTEGER, identity TEXT,
                prefix TEXT, prefix_len INTEGER, offset INTEGER, skipped INTEGER,
                invalid INTEGER, PRIMARY KEY(source, path));
            CREATE TABLE IF NOT EXISTS observations (
                path TEXT, key TEXT, data TEXT NOT NULL, PRIMARY KEY(path, key));
            CREATE INDEX IF NOT EXISTS buddy_observations_key ON observations(key);
            CREATE TABLE IF NOT EXISTS requests (
                key TEXT PRIMARY KEY, source TEXT NOT NULL, session_id TEXT,
                at REAL, model TEXT, input INTEGER, output INTEGER, cache INTEGER,
                cache_known INTEGER, credit REAL, credit_valid INTEGER,
                path TEXT, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS buddy_requests_at ON requests(at);
            CREATE INDEX IF NOT EXISTS buddy_requests_filter
                ON requests(source, model, at);
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, requested TEXT, actual TEXT, status TEXT,
                completed INTEGER, outcome TEXT, order_at REAL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS buddy_runs_actual ON runs(actual);
            CREATE TABLE IF NOT EXISTS timing_nodes (
                path TEXT, node_id TEXT, at REAL, parent TEXT, boundary INTEGER,
                request TEXT, PRIMARY KEY(path, node_id));
            CREATE TABLE IF NOT EXISTS trace_files (
                path TEXT PRIMARY KEY, source TEXT NOT NULL, size INTEGER,
                mtime INTEGER, signature TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS trace_spans (
                path TEXT, trace_id TEXT, request_id TEXT, duration REAL,
                ended REAL, input INTEGER, output INTEGER,
                PRIMARY KEY(path, trace_id, request_id));
            CREATE INDEX IF NOT EXISTS buddy_trace_spans_request
                ON trace_spans(request_id);
            CREATE TABLE IF NOT EXISTS response_logs (
                source TEXT, path TEXT, size INTEGER, mtime INTEGER, identity TEXT,
                prefix TEXT, prefix_len INTEGER, offset INTEGER, skipped INTEGER,
                invalid INTEGER, PRIMARY KEY(source, path));
            CREATE TABLE IF NOT EXISTS response_observations (
                path TEXT, seq TEXT, source TEXT, request_id TEXT, session_id TEXT,
                ttft REAL, basis TEXT, model TEXT, input INTEGER, output INTEGER,
                PRIMARY KEY(path, seq));
            CREATE INDEX IF NOT EXISTS buddy_response_request
                ON response_observations(source, request_id);
            """)
            columns = {
                "files": (("boundary", "REAL"), ("timing_version", "INTEGER")),
                "requests": (("trace_id", "TEXT"), ("request_id", "TEXT"),
                             ("boundary_duration", "REAL"),
                             ("duration", "REAL"), ("timing_source", "TEXT"),
                             ("speed", "REAL"), ("cost", "REAL"),
                             ("cost_basis", "TEXT"), ("priced", "INTEGER"),
                             ("message_id", "TEXT"), ("first_token", "REAL"),
                             ("first_token_source", "TEXT"),
                             ("first_token_basis", "TEXT")),
            }
            for table, additions in columns.items():
                existing = {row[1] for row in self.db.execute(
                    "PRAGMA table_info(" + table + ")")}
                for name, kind in additions:
                    if name not in existing:
                        self.db.execute("ALTER TABLE " + table + " ADD COLUMN "
                                        + name + " " + kind)
            self.db.execute("CREATE INDEX IF NOT EXISTS buddy_requests_message "
                            "ON requests(source, message_id)")
            if version < 3:
                # Existing rows predate the dedicated messageId column. Derive
                # only the whitelisted identifier; never retain the JSON body.
                for row in self.db.execute(
                        "SELECT key,data FROM requests WHERE message_id IS NULL"):
                    try:
                        value = json.loads(row["data"]).get("messageId")
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if isinstance(value, str) and value.strip():
                        self.db.execute("UPDATE requests SET message_id=? WHERE key=?",
                                        (value.strip()[:240], row["key"]))
            if version < 2:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                                ("timingBackfill", encode(1)))
            if self.db.execute("SELECT 1 FROM requests LIMIT 1").fetchone():
                self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                                ("responseBackfill", encode(1)))
            else:
                self.db.execute("DELETE FROM meta WHERE key='responseBackfill'")
            self.db.execute("PRAGMA user_version=3")
        self.db.commit()

    def close(self):
        self.db.close()

    def metadata(self):
        return {row["key"]: json.loads(row["value"])
                for row in self.db.execute("SELECT * FROM meta")}

    def put_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, encode(value)))

    @staticmethod
    def _scan(folder, root, found):
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        BuddyStore._scan(entry.path, root, found)
                    elif entry.name.endswith(".jsonl"):
                        if entry.is_symlink():
                            path = Path(entry.path).resolve()
                            if path.is_relative_to(root) and path.is_file():
                                found[str(path)] = path.stat()
                        elif entry.is_file(follow_symlinks=False):
                            found[entry.path] = os.stat(entry.path, follow_symlinks=False)
        except OSError:
            # An incomplete directory listing must never delete cached rows.
            raise

    @staticmethod
    def _scan_json(folder, found, root=None):
        root = Path(root or folder).resolve()
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    BuddyStore._scan_json(entry.path, found, root)
                elif entry.name.endswith(".json"):
                    if entry.is_symlink():
                        path = Path(entry.path).resolve()
                        if path.is_relative_to(root) and path.is_file():
                            found[str(path)] = path.stat()
                    elif entry.is_file(follow_symlinks=False):
                        found[entry.path] = os.stat(entry.path, follow_symlinks=False)

    @staticmethod
    def _scan_logs(folder, root, found):
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        BuddyStore._scan_logs(entry.path, root, found)
                    elif entry.name.endswith(".log"):
                        if entry.is_symlink():
                            path = Path(entry.path).resolve()
                            if path.is_relative_to(root) and path.is_file():
                                found[str(path)] = path.stat()
                        elif entry.is_file(follow_symlinks=False):
                            found[entry.path] = os.stat(entry.path, follow_symlinks=False)
        except OSError:
            # An incomplete listing cannot certify any diagnostic file as
            # current; retain all previously observed evidence.
            raise

    def discover(self, source):
        root = self.roots.get(source)
        if root is None:
            return {}
        try:
            if not stat.S_ISDIR(root.stat().st_mode):
                return {}
        except FileNotFoundError:
            return {}
        found = {}
        self._scan(root, root, found)
        return found

    @staticmethod
    def _prefix(path, length):
        with Path(path).open("rb") as stream:
            return hashlib.sha256(stream.read(length)).hexdigest()

    def _read(self, path, source, offset, state, prior):
        session = ParseSession(source)
        session.prior = dict(prior)
        try:
            with Path(path).open("rb") as stream:
                stream.seek(offset)
                while True:
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        break  # retry partial lines and split UTF-8 next poll
                    offset = stream.tell()
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError, TypeError, OverflowError):
                        session.skipped += 1
                        state.nodes.clear()
                        state.changed.clear()
                        state.boundary = None
                        state.clear = True
                        continue
                    timing = state.observe(row)
                    record, reason = parse_record(row, source)
                    if record is None:
                        if reason == "ignored":
                            continue
                        if reason == "invalid-json":
                            session.skipped += 1
                        else:
                            session.invalid += 1
                        continue
                    duration = None
                    end = record.at
                    if timing is not None:
                        start, end = timing
                        if start is not None and end > start:
                            duration = end - start
                    previous = session.prior.get(record.key)
                    if previous is not None:
                        prior_at, prior_duration = previous
                        original_start = prior_at - prior_duration
                        if end > original_start:
                            # A later duplicate copy of the same request
                            # keeps the first observed start.
                            duration = end - original_start
                    if duration is not None and (not math.isfinite(duration) or duration <= 0):
                        duration = None
                    if duration is not None:
                        session.prior[record.key] = (record.at, duration)
                        record = replace(record, boundary_duration=duration)
                    if record.key in session.records:
                        session.duplicates += 1
                    # A later line with the same request ID supersedes an
                    # earlier partial/updated copy of that request.
                    session.records[record.key] = record
        except OSError:
            return offset, session, "read-failed"
        return offset, session, None

    def _insert_request(self, data, path):
        cache = data.get("cache") if data.get("cacheKnown") else None
        boundary = data.get("boundaryDuration")
        if (isinstance(boundary, bool) or not isinstance(boundary, (int, float))
                or not math.isfinite(boundary) or boundary <= 0):
            boundary = None
        priced = {"model": data["model"], "at": data["at"], "input": data["input"],
                  "output": data["output"], "cache": cache}
        cost = pricing.cost(priced)
        basis = pricing.basis(priced)
        message_id = data.get("messageId")
        previous = self.db.execute("""SELECT message_id,first_token,first_token_source,
            first_token_basis FROM requests WHERE key=?""", (data["key"],)).fetchone()
        if previous is not None and previous["message_id"] == message_id:
            first_token = previous["first_token"]
            first_source = previous["first_token_source"]
            first_basis = previous["first_token_basis"]
        else:
            first_token = first_source = first_basis = None
        self.db.execute("""INSERT OR REPLACE INTO requests
            (key, source, session_id, at, model, input, output, cache, cache_known,
             credit, credit_valid, path, data, trace_id, request_id, boundary_duration,
             duration, timing_source, speed, cost, cost_basis, priced, message_id,
             first_token, first_token_source, first_token_basis)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data["key"], data["source"], data["sessionId"], data["at"], data["model"],
             data["input"], data["output"], data["cache"], int(bool(data["cacheKnown"])),
             data["credit"], int(bool(data["creditValid"])), path, encode(data),
             data.get("traceId"), data.get("requestId"), boundary, None, None, None,
             cost, basis, int(cost is not None), message_id, first_token,
             first_source, first_basis))

    @staticmethod
    def _evidence_matches(observation, candidate):
        session = observation["session_id"]
        if (session and candidate["session_id"]
                and candidate["session_id"] not in ("", "unknown")
                and session != candidate["session_id"]):
            return False
        model = observation["model"]
        if (model and candidate["model"]
                and candidate["model"] not in ("", "unknown")
                and model.strip() != candidate["model"].strip()):
            return False
        for field in ("input", "output"):
            value = observation[field]
            if value is not None and value != candidate[field]:
                return False
        return True

    def _resolve_response(self, source, request_id):
        """Resolve one requestId without guessing across sessions/sources."""
        candidates = list(self.db.execute("""SELECT key,session_id,model,input,output
            FROM requests WHERE source=? AND message_id=? ORDER BY key""",
            (source, request_id)))
        if not candidates:
            return []
        observations = list(self.db.execute("""SELECT session_id,ttft,basis,model,
            input,output FROM response_observations
            WHERE source=? AND request_id=? ORDER BY path,seq""",
            (source, request_id)))
        completed = [item for item in observations if item["basis"] == "completed-log"]
        plain = [item for item in observations if item["basis"] == "first-token-log"]
        unique = len(candidates) == 1
        matched_by_observation = [
            (item, [candidate["key"] for candidate in candidates
                    if self._evidence_matches(item, candidate)])
            for item in completed]
        changes = []
        for candidate in candidates:
            relevant = [(item, keys) for item, keys in matched_by_observation
                        if candidate["key"] in keys]
            if any(len(keys) > 1 for _, keys in relevant):
                # A structured event without enough identity to choose one
                # request must not silently enrich several sessions.
                changes.append((candidate["key"], None, None, None))
                continue
            if relevant:
                values = {item["ttft"] for item, _ in relevant}
                if len(values) == 1:
                    changes.append((candidate["key"], next(iter(values)),
                                    "completed-log", TTFT_BASIS))
                else:
                    changes.append((candidate["key"], None, None, None))
                continue
            if completed or not unique:
                # Exact diagnostic evidence exists but does not describe this
                # usage row, or a plain line cannot distinguish sessions.
                changes.append((candidate["key"], None, None, None))
                continue
            values = {item["ttft"] for item in plain}
            if len(values) == 1:
                changes.append((candidate["key"], next(iter(values)),
                                "first-token-log", TTFT_BASIS))
            else:
                changes.append((candidate["key"], None, None, None))
        return changes

    def _apply_response_evidence(self, source, request_ids):
        updated = 0
        for request_id in sorted(value for value in request_ids if value):
            for key, ttft, first_source, first_basis in self._resolve_response(
                    source, request_id):
                old = self.db.execute("""SELECT first_token,first_token_source,
                    first_token_basis FROM requests WHERE key=?""", (key,)).fetchone()
                if old is None:
                    continue
                value = (ttft, first_source, first_basis)
                if tuple(old) != value:
                    self.db.execute("""UPDATE requests SET first_token=?,
                        first_token_source=?,first_token_basis=? WHERE key=?""",
                        (ttft, first_source, first_basis, key))
                    updated += 1
        return updated

    def _refresh_response_logs(self, source, report):
        result = {"files": 0, "changedFiles": 0, "invalid": 0, "skipped": 0,
                  "evidence": 0}
        root = self.roots.get(source)
        if root is None:
            return set(), False, result
        log_root = root.parent / "logs"
        if not log_root.is_dir():
            return set(), False, result
        found = {}
        try:
            self._scan_logs(log_root, log_root.resolve(), found)
        except OSError as exc:
            report["warnings"].append("诊断日志：" + str(exc))
            return set(), True, result
        previous = {row["path"]: row for row in self.db.execute(
            "SELECT * FROM response_logs WHERE source=?", (source,))}
        dirty = set()
        failed = False
        for path, stat_value in sorted(found.items()):
            old = previous.get(path)
            identity = str((stat_value.st_dev, stat_value.st_ino))
            if (old and old["size"] == stat_value.st_size
                    and old["mtime"] == stat_value.st_mtime_ns
                    and old["identity"] == identity):
                continue
            try:
                prefix_len = min(512, stat_value.st_size)
                prefix = self._prefix(path, prefix_len)
                reset = (not old or stat_value.st_size < old["size"]
                         or old["identity"] != identity
                         or old["prefix"] != prefix
                         or (stat_value.st_size == old["size"]
                             and stat_value.st_mtime_ns != old["mtime"]))
                offset = 0 if reset else int(old["offset"])
                skipped = invalid = 0
                observed = []
                with Path(path).open("rb") as stream:
                    stream.seek(offset)
                    while True:
                        line = stream.readline()
                        if not line or not line.endswith(b"\n"):
                            break  # retry the partial line on the next poll
                        offset = stream.tell()
                        evidence, reason = parse_line(line)
                        if evidence is None:
                            if reason == "invalid":
                                invalid += 1
                            elif reason == "skipped":
                                skipped += 1
                            continue
                        observed.append((offset, evidence))
            except OSError as exc:
                failed = True
                report["warnings"].append(Path(path).name + "：" + str(exc))
                continue
            if reset:
                dirty.update(row[0] for row in self.db.execute(
                    "SELECT DISTINCT request_id FROM response_observations "
                    "WHERE path=?", (path,)))
                self.db.execute("DELETE FROM response_observations WHERE path=?", (path,))
            for sequence, evidence in observed:
                dirty.add(evidence.request_id)
                self.db.execute("""INSERT OR REPLACE INTO response_observations
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (path, str(sequence), source, evidence.request_id,
                     evidence.session_id or "", evidence.ttft, evidence.basis,
                     evidence.model, evidence.input_tokens, evidence.output_tokens))
            self.db.execute("""INSERT OR REPLACE INTO response_logs
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (source, path, stat_value.st_size, stat_value.st_mtime_ns,
                 identity, prefix, prefix_len, offset, skipped, invalid))
            result["changedFiles"] += 1
            result["invalid"] += invalid
            result["skipped"] += skipped
            result["evidence"] += len(observed)
        for path in previous.keys() - found.keys():
            dirty.update(row[0] for row in self.db.execute(
                "SELECT DISTINCT request_id FROM response_observations WHERE path=?",
                (path,)))
            self.db.execute("DELETE FROM response_observations WHERE path=?", (path,))
            self.db.execute("DELETE FROM response_logs WHERE path=?", (path,))
            result["changedFiles"] += 1
        result["files"] = len(found)
        return dirty, failed, result

    def _refresh_traces(self, report):
        changed = set()
        meta = self.metadata()
        signatures = dict(meta.get("traceSignatures") or {})
        for source in SOURCES:
            root = self.roots.get(source)
            trace_root = root.parent / "traces" if root else None
            found = {}
            if trace_root is not None and trace_root.is_dir():
                try:
                    self._scan_json(trace_root, found)
                except OSError as exc:
                    report["warnings"].append("轨迹目录：" + str(exc))
                    # The incomplete listing cannot certify any trace as
                    # current. Keep usage and fall back to its own boundaries.
                    found.clear()
            previous = {row["path"]: row for row in self.db.execute(
                "SELECT * FROM trace_files WHERE source=?", (source,))}
            for path, stat_value in sorted(found.items()):
                old = previous.get(path)
                signature = str((stat_value.st_size, stat_value.st_mtime_ns))
                if (old and old["size"] == stat_value.st_size
                        and old["mtime"] == stat_value.st_mtime_ns
                        and old["signature"] == signature and old["status"] == "ok"):
                    continue
                try:
                    document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
                except (OSError, ValueError, UnicodeError) as exc:
                    report["warnings"].append(Path(path).name + "：" + str(exc))
                    # A missing/unreadable trace must not contribute timing.
                    self.db.execute("DELETE FROM trace_spans WHERE path=?", (path,))
                    self.db.execute("INSERT OR REPLACE INTO trace_files VALUES (?,?,?,?,?,?)",
                                    (path, source, stat_value.st_size,
                                     stat_value.st_mtime_ns, signature, "stale"))
                    continue
                timings = trace_timings(document)
                self.db.execute("DELETE FROM trace_spans WHERE path=?", (path,))
                for (identifier, request), timing in timings.items():
                    if timing is None:
                        values = (path, identifier, request, None, None, None, None)
                    else:
                        tokens = timing.tokens or (None, None)
                        values = (path, identifier, request, timing.duration,
                                  timing.ended, tokens[0], tokens[1])
                    self.db.execute("INSERT OR REPLACE INTO trace_spans VALUES (?,?,?,?,?,?,?)",
                                    values)
                self.db.execute("INSERT OR REPLACE INTO trace_files VALUES (?,?,?,?,?,?)",
                                (path, source, stat_value.st_size,
                                 stat_value.st_mtime_ns, signature, "ok"))
            for path in previous.keys() - found.keys():
                self.db.execute("DELETE FROM trace_spans WHERE path=?", (path,))
                self.db.execute("DELETE FROM trace_files WHERE path=?", (path,))
            rows = list(self.db.execute("""SELECT s.trace_id,s.request_id,s.duration,
                s.ended,s.input,s.output FROM trace_spans s
                JOIN trace_files f ON f.path=s.path WHERE f.source=?
                ORDER BY s.trace_id,s.request_id,s.duration,s.ended""", (source,)))
            signature = hashlib.sha256(encode([list(row) for row in rows]).encode()).hexdigest()
            if signatures.get(source) != signature:
                signatures[source] = signature
                changed.add(source)
        if changed:
            self.put_meta("traceSignatures", signatures)
        return changed

    def _timing_maps(self, source):
        exact = {}
        for row in self.db.execute("""SELECT s.trace_id,s.request_id,s.duration,s.ended,
            s.input,s.output FROM trace_spans s JOIN trace_files f ON f.path=s.path
            WHERE f.source=?""", (source,)):
            if row["duration"] is None or row["duration"] <= 0:
                timing = None
            else:
                tokens = ((row["input"], row["output"])
                          if row["input"] is not None and row["output"] is not None else None)
                timing = Timing(row["duration"], row["ended"], tokens)
            key = (row["trace_id"] or "", row["request_id"])
            if key in exact and exact[key] != timing:
                exact[key] = None
            elif key not in exact:
                exact[key] = timing
        by_request = {}
        for (_, request), timing in exact.items():
            by_request.setdefault(request, set()).add(timing)
        fallback = {request: (None if None in values or len(values) != 1
                              else next(iter(values)))
                    for request, values in by_request.items()}
        return exact, fallback

    def _apply_timing(self, sources):
        updated = 0
        for source in sources:
            exact, fallback = self._timing_maps(source)
            rows = list(self.db.execute("""SELECT key,request_id,trace_id,input,output,
                boundary_duration,duration,timing_source,speed FROM requests
                WHERE source=?""", (source,)))
            for row in rows:
                timing = None
                key = (row["trace_id"] or "", row["request_id"])
                if key in exact:
                    timing = exact[key]
                elif row["request_id"] in fallback:
                    timing = fallback[row["request_id"]]
                duration = None
                timing_source = None
                if (timing is not None
                        and (timing.tokens is None
                             or timing.tokens[:2] == (row["input"], row["output"]))):
                    duration = timing.duration
                    timing_source = "trace"
                else:
                    boundary = row["boundary_duration"]
                    if (isinstance(boundary, (int, float)) and not isinstance(boundary, bool)
                            and math.isfinite(boundary) and boundary > 0):
                        duration = boundary
                        timing_source = "boundary"
                speed = (row["output"] / duration
                         if duration and row["output"] > 0 else None)
                if (row["duration"], row["timing_source"], row["speed"]) != (
                        duration, timing_source, speed):
                    self.db.execute("""UPDATE requests SET duration=?,timing_source=?,
                        speed=? WHERE key=?""", (duration, timing_source, speed, row["key"]))
                    updated += 1
        return updated

    def _apply_costs(self, sources):
        updated = 0
        for source in sources:
            rows = list(self.db.execute("""SELECT key,at,model,input,output,cache,
                cache_known,cost,cost_basis,priced FROM requests WHERE source=?""",
                (source,)))
            for row in rows:
                priced = {"model": row["model"], "at": row["at"],
                          "input": row["input"], "output": row["output"],
                          "cache": row["cache"] if row["cache_known"] else None}
                value = pricing.cost(priced)
                basis = pricing.basis(priced)
                marker = int(value is not None)
                if (row["cost"], row["cost_basis"], row["priced"]) != (value, basis, marker):
                    self.db.execute("""UPDATE requests SET cost=?,cost_basis=?,priced=?
                        WHERE key=?""", (value, basis, marker, row["key"]))
                    updated += 1
        return updated

    def refresh(self, progress=None):
        started = time.time()
        with import_lock(self.cache):
            report = {"sources": {}, "bridge": {}, "warnings": [], "skippedLines": 0,
                      "invalidValues": 0, "duplicates": 0, "added": 0, "updated": 0,
                      "removed": 0, "changedFiles": 0, "elapsed": 0.0,
                      "timingUpdated": 0, "costUpdated": 0, "responseUpdated": 0,
                      "diagnosticFiles": 0, "diagnosticChangedFiles": 0,
                      "diagnosticEvidence": 0, "diagnosticInvalid": 0,
                      "diagnosticSkipped": 0, "responseBackfillPending": False}
            request_changed = False
            bridge_changed = False
            response_changed = False
            diagnostic_failed = False
            diagnostic_incomplete = False
            usage_sources = set()
            timing_backfill_failed = False
            with self.db:
                meta = self.metadata()
                timing_backfill = bool(meta.get("timingBackfill"))
                response_backfill = bool(meta.get("responseBackfill"))
                for number, source in enumerate(SOURCES, 1):
                    if progress:
                        progress({"completed": number - 1, "total": len(SOURCES) + 1,
                                  "phase": "读取 " + source})
                    try:
                        paths = self.discover(source)
                    except OSError as exc:
                        report["warnings"].append(source + "：读取目录失败，保留已有索引。" + str(exc))
                        report["sources"][source] = {"available": False, "stale": True}
                        diagnostic_incomplete = True
                        continue
                    previous = {row["path"]: row for row in self.db.execute(
                        "SELECT * FROM files WHERE source=?", (source,))}
                    source_report = dict(available=bool(self.roots.get(source)
                                                         and self.roots[source].is_dir()),
                                         files=len(paths), changedFiles=0, added=0,
                                         updated=0, removed=0, skippedLines=0,
                                         invalidValues=0, duplicates=0)
                    dirty = set()
                    response_dirty = set()
                    if not paths:
                        # A removed/moved root must not leave old records that
                        # look current. Missing directories are not an error.
                        dirty.update(row[0] for row in self.db.execute(
                            "SELECT key FROM observations WHERE path IN "
                            "(SELECT path FROM files WHERE source=?)", (source,)))
                        for path in previous:
                            self.db.execute("DELETE FROM observations WHERE path=?", (path,))
                            self.db.execute("DELETE FROM timing_nodes WHERE path=?", (path,))
                            self.db.execute("DELETE FROM files WHERE path=?", (path,))
                    for path, stat_value in sorted(paths.items()):
                        old = previous.get(path)
                        identity = str((stat_value.st_dev, stat_value.st_ino))
                        if (not timing_backfill and old
                                and old["size"] == stat_value.st_size
                                and old["mtime"] == stat_value.st_mtime_ns
                                and old["identity"] == identity
                                and old["timing_version"] == 1):
                            continue
                        try:
                            prefix_len = old["prefix_len"] if old else min(512, stat_value.st_size)
                            prefix = self._prefix(path, prefix_len)
                            if prefix_len != min(512, stat_value.st_size):
                                prefix_len = min(512, stat_value.st_size)
                                prefix = self._prefix(path, prefix_len)
                            reset = (not old or stat_value.st_size < old["size"]
                                     or old["identity"] != identity
                                     or old["prefix"] != prefix
                                     or old["timing_version"] != 1
                                     or timing_backfill)
                            if reset:
                                state = TimingState()
                                prior = {}
                            else:
                                nodes = {row["node_id"]: (row["at"], row["parent"],
                                                          bool(row["boundary"]), row["request"])
                                         for row in self.db.execute(
                                             "SELECT * FROM timing_nodes WHERE path=?", (path,))}
                                state = TimingState(nodes, old["boundary"])
                                prior = {}
                                for prior_row in self.db.execute(
                                        "SELECT key,data FROM observations WHERE path=?", (path,)):
                                    try:
                                        value = json.loads(prior_row["data"])
                                    except (ValueError, TypeError):
                                        continue
                                    boundary = value.get("boundaryDuration")
                                    if (isinstance(boundary, (int, float))
                                            and not isinstance(boundary, bool)
                                            and boundary > 0):
                                        prior[prior_row["key"]] = (value["at"], boundary)
                            offset, session, error = self._read(
                                path, source, 0 if reset else old["offset"], state, prior)
                        except OSError as exc:
                            source_report["stale"] = True
                            report["warnings"].append(Path(path).name + "：" + str(exc))
                            timing_backfill_failed = True
                            continue
                        if error:
                            source_report["stale"] = True
                            report["warnings"].append(Path(path).name + "：" + error)
                            timing_backfill_failed = True
                            continue
                        source_report["changedFiles"] += 1
                        source_report["skippedLines"] += session.skipped
                        source_report["invalidValues"] += session.invalid
                        source_report["duplicates"] += session.duplicates
                        report["skippedLines"] += session.skipped
                        report["invalidValues"] += session.invalid
                        report["duplicates"] += session.duplicates
                        report["changedFiles"] += 1
                        if reset:
                            dirty.update(row[0] for row in self.db.execute(
                                "SELECT key FROM observations WHERE path=?", (path,)))
                            self.db.execute("DELETE FROM observations WHERE path=?", (path,))
                        if reset or state.clear:
                            self.db.execute("DELETE FROM timing_nodes WHERE path=?", (path,))
                        for node_id in state.changed:
                            values = state.nodes[node_id]
                            self.db.execute("""INSERT OR REPLACE INTO timing_nodes
                                VALUES (?,?,?,?,?,?)""",
                                (path, node_id, values[0], values[1],
                                 int(bool(values[2])), values[3]))
                        for record in session.records.values():
                            self.db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?)",
                                            (path, record.key, encode(record.data())))
                            dirty.add(record.key)
                        self.db.execute("""INSERT OR REPLACE INTO files
                            (source, path, size, mtime, identity, prefix, prefix_len,
                             offset, skipped, invalid, boundary, timing_version)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (source, path, stat_value.st_size, stat_value.st_mtime_ns,
                             identity, prefix, min(512, stat_value.st_size), offset,
                             session.skipped, session.invalid, state.boundary, 1))
                    for path in previous.keys() - paths.keys():
                        dirty.update(row[0] for row in self.db.execute(
                            "SELECT key FROM observations WHERE path=?", (path,)))
                        self.db.execute("DELETE FROM observations WHERE path=?", (path,))
                        self.db.execute("DELETE FROM timing_nodes WHERE path=?", (path,))
                        self.db.execute("DELETE FROM files WHERE path=?", (path,))
                    for key in dirty:
                        candidate = self.db.execute("""SELECT o.data, o.path, f.mtime
                            FROM observations o JOIN files f ON f.path=o.path
                            WHERE o.key=? ORDER BY f.mtime DESC, f.path ASC LIMIT 1""",
                            (key,)).fetchone()
                        old = self.db.execute("SELECT data, path FROM requests WHERE key=?",
                                              (key,)).fetchone()
                        if candidate is None:
                            if old:
                                try:
                                    removed_id = json.loads(old["data"]).get("messageId")
                                except (ValueError, TypeError, AttributeError):
                                    removed_id = None
                                if removed_id:
                                    response_dirty.add(removed_id)
                                self.db.execute("DELETE FROM requests WHERE key=?", (key,))
                                source_report["removed"] += 1
                                report["removed"] += 1
                                request_changed = True
                                usage_sources.add(source)
                            continue
                        value = json.loads(candidate["data"])
                        message_id = value.get("messageId")
                        if message_id:
                            response_dirty.add(message_id)
                        if old is None:
                            self._insert_request(value, candidate["path"])
                            source_report["added"] += 1
                            report["added"] += 1
                            request_changed = True
                            usage_sources.add(source)
                        elif old["data"] != candidate["data"]:
                            self._insert_request(value, candidate["path"])
                            source_report["updated"] += 1
                            report["updated"] += 1
                            request_changed = True
                            usage_sources.add(source)
                        elif old["path"] != candidate["path"]:
                            # A moved/archive copy with identical business data
                            # does not change totals or the published version.
                            self.db.execute("UPDATE requests SET path=? WHERE key=?",
                                            (candidate["path"], key))
                    diagnostic_dirty, diagnostic_error, diagnostics = (
                        self._refresh_response_logs(source, report))
                    diagnostic_failed = diagnostic_failed or diagnostic_error
                    response_dirty.update(diagnostic_dirty)
                    if response_backfill:
                        response_dirty.update(row[0] for row in self.db.execute(
                            "SELECT DISTINCT request_id FROM response_observations "
                            "WHERE source=?", (source,)))
                    updated_response = self._apply_response_evidence(source, response_dirty)
                    report["responseUpdated"] += updated_response
                    report["diagnosticFiles"] += diagnostics["files"]
                    report["diagnosticChangedFiles"] += diagnostics["changedFiles"]
                    report["diagnosticEvidence"] += diagnostics["evidence"]
                    report["diagnosticInvalid"] += diagnostics["invalid"]
                    report["diagnosticSkipped"] += diagnostics["skipped"]
                    source_report["diagnostics"] = diagnostics
                    source_report["responseUpdated"] = updated_response
                    if updated_response:
                        response_changed = True
                    report["sources"][source] = source_report

                trace_changed = self._refresh_traces(report)
                timing_updated = self._apply_timing(set(usage_sources) | trace_changed)
                pricing_all = meta.get("pricingVersion") != pricing.PRICE_VERSION
                cost_updated = self._apply_costs(set(SOURCES) if pricing_all else usage_sources)
                report["timingUpdated"] = timing_updated
                report["costUpdated"] = cost_updated
                report["traceChanged"] = sorted(trace_changed)

                runs, bridge_error = self._scan_bridge()
                if bridge_error:
                    report["warnings"].append("桥接运行记录：" + bridge_error)
                elif runs is not None:
                    signature = hashlib.sha256(encode([
                        [run["runId"], run["requested"], run["actual"], run["status"],
                         run["completed"], run["outcome"], run["observedModels"]]
                        for run in runs]).encode()).hexdigest()
                    if signature != meta.get("bridgeSignature"):
                        self.db.execute("DELETE FROM runs")
                        for run in runs:
                            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?)",
                                            (run["runId"], run["requested"], run["actual"],
                                             run["status"], int(bool(run["completed"])),
                                             run["outcome"], run["orderAt"], encode(run)))
                        self.put_meta("bridgeSignature", signature)
                        bridge_changed = True
                    report["bridge"] = {"runs": len(runs), "changed": bridge_changed}
                version = meta.get("version", 0)
                if (request_changed or bridge_changed or trace_changed
                        or timing_updated or cost_updated or response_changed):
                    version += 1
                    self.put_meta("version", version)
                self.put_meta("pricingVersion", pricing.PRICE_VERSION)
                self.put_meta("timingVersion", 1)
                if timing_backfill and not timing_backfill_failed:
                    self.put_meta("timingBackfill", 0)
                if response_backfill and not diagnostic_failed and not diagnostic_incomplete:
                    self.put_meta("responseBackfill", 0)
                report["responseBackfillPending"] = bool(
                    self.metadata().get("responseBackfill"))
                report["elapsed"] = time.time() - started
                report["records"] = self.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
                report["partial"] = bool(report["warnings"])
                self.put_meta("lastAttempt", time.time())
                self.put_meta("lastImport", report)
                if not report["warnings"]:
                    self.put_meta("lastSuccess", time.time())
            return report

    def _scan_bridge(self):
        if self.bridge is None:
            return None, None
        if not self.bridge.is_dir():
            return [], None
        runs = []
        try:
            with os.scandir(self.bridge) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    policy_value = self._json_file(Path(entry.path) / "policy.json")
                    policy_known = bool(policy_value and isinstance(policy_value.get("cwd"), str)
                                        and policy_value["cwd"].strip())
                    policy = policy_value or {}
                    result_path = Path(entry.path) / "result.json"
                    result_present = result_path.is_file()
                    result_value = self._json_file(result_path)
                    result = result_value or {}
                    if not policy and not result and not result_present:
                        continue
                    requested = policy.get("resume") if isinstance(policy.get("resume"), str) else None
                    requested = requested.strip()[:240] if requested and requested.strip() else None
                    actual = result.get("session_id") if isinstance(result.get("session_id"), str) else None
                    actual = actual.strip()[:240] if actual and actual.strip() else None
                    status = result.get("status") if isinstance(result.get("status"), str) else None
                    status = status.strip()[:40] if status and status.strip() else None
                    if not result_present:
                        status = "pending"
                    elif result_value is None or not status:
                        status = "unknown"
                    completed = result.get("completed") is True
                    run_id = result.get("run_id") if isinstance(result.get("run_id"), str) else None
                    run_id = (run_id or entry.name).strip()[:240]
                    observed = result.get("actual_models")
                    if not isinstance(observed, list):
                        observed = result.get("observed_models")
                    observed = [value.strip()[:160] for value in observed
                                if isinstance(value, str) and value.strip()][:50] if isinstance(observed, list) else []
                    outcome = run_outcome(requested, actual, completed, status)
                    if outcome == "new" and not policy_known:
                        outcome = "unknown"
                    runs.append({
                        "runId": run_id, "requested": requested, "actual": actual,
                        "status": status, "completed": completed,
                        "outcome": outcome,
                        "orderAt": entry.stat(follow_symlinks=False).st_mtime,
                        "observedModels": observed,
                    })
        except OSError as exc:
            return None, str(exc)
        runs.sort(key=lambda run: run["runId"])
        return runs, None

    @staticmethod
    def _json_file(path):
        try:
            if not path.is_file() or path.stat().st_size > 512 * 1024:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, UnicodeError):
            return None

    def rows(self, since, until, sources, model=None):
        selected = [source for source in SOURCES if source in sources]
        if not selected:
            return []
        marks = ",".join("?" for _ in selected)
        sql = ("SELECT path, data, duration, speed, timing_source, trace_id, "
               "cost, cost_basis, priced, first_token, first_token_source, "
               "first_token_basis FROM requests WHERE source IN ("
               + marks + ") AND at>=? AND at<?")
        values = selected + [since, until]
        if model:
            sql += " AND model=?"
            values.append(model)
        sql += " ORDER BY at DESC, key ASC"
        result = []
        for row in self.db.execute(sql, values):
            value = json.loads(row["data"])
            value.update(path=row["path"], duration=row["duration"], speed=row["speed"],
                         timingSource=row["timing_source"], traceId=row["trace_id"],
                         cost=row["cost"], costBasis=row["cost_basis"],
                         priced=bool(row["priced"]),
                         firstToken=row["first_token"],
                         firstTokenSource=row["first_token_source"],
                         firstTokenBasis=row["first_token_basis"])
            result.append(value)
        return result

    @staticmethod
    def cards(rows):
        records = len(rows)
        total_input = sum(row["input"] for row in rows)
        cache_known = sum(1 for row in rows if row["cacheKnown"])
        cache = sum(row["cache"] for row in rows if row["cacheKnown"])
        credits = [row["credit"] for row in rows if row["creditValid"]]
        credit = sum(credits) if credits else None
        timed_rows = [row for row in rows
                      if row.get("duration") and row["duration"] > 0 and row["output"] > 0]
        speeds = [row["output"] / row["duration"] for row in timed_rows]
        costs = [row["cost"] for row in rows if row.get("cost") is not None]
        seconds = sum(row["duration"] for row in timed_rows)
        timed_out = sum(row["output"] for row in timed_rows)
        values = [row["firstToken"] for row in rows
                  if row.get("firstToken") is not None]
        middle, p90 = response_percentiles(values)
        return dict(records=records, timed=len(timed_rows), input=total_input,
                    output=sum(row["output"] for row in rows),
                    cache=cache,
                    cacheHit=(cache / total_input if total_input and cache_known == records else None),
                    cacheKnown=cache_known,
                    cost=sum(costs) if costs else None, priced=len(costs),
                    costUnknown=records - len(costs),
                    average=timed_out / seconds if seconds else None,
                    median=median(speeds) if speeds else None,
                    minimum=min(speeds) if speeds else None,
                    maximum=max(speeds) if speeds else None,
                    timedOut=timed_out, seconds=seconds,
                    traceTimed=sum(1 for row in timed_rows
                                   if row.get("timingSource") == "trace"),
                    boundaryTimed=sum(1 for row in timed_rows
                                      if row.get("timingSource") == "boundary"),
                    responseTurns=records, responseSamples=len(values),
                    responseMedian=middle, responseP90=p90,
                    credit=credit, creditCount=len(credits),
                    creditApplicable=records, creditMissing=records - len(credits))

    def models(self):
        result = {}
        for source in SOURCES:
            result[source] = [row[0] for row in self.db.execute(
                "SELECT DISTINCT model FROM requests WHERE source=? ORDER BY model", (source,))]
        return result

    def status(self):
        meta = self.metadata()
        sources = {}
        for source in SOURCES:
            root = self.roots.get(source)
            files = self.db.execute("SELECT COUNT(*) FROM files WHERE source=?", (source,)).fetchone()[0]
            summary = self.db.execute("SELECT COUNT(*),MIN(at),MAX(at) FROM requests WHERE source=?", (source,)).fetchone()
            records = summary[0]
            sources[source] = {"root": str(root) if root else None,
                               "exists": bool(root and root.is_dir()),
                               "files": files, "records": records,
                               "earliest": summary[1], "latest": summary[2],
                               "stale": bool(meta.get("lastImport", {}).get("sources", {}).get(source, {}).get("stale"))}
        bridge = {"root": str(self.bridge) if self.bridge else None,
                  "exists": bool(self.bridge and self.bridge.is_dir()),
                  "runs": 0, "sessions": 0, "resumed": 0, "new": 0,
                  "mismatch": 0, "unconfirmed": 0, "pending": 0, "unknown": 0, "failed": 0}
        bridge["runs"] = self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        bridge["sessions"] = self.db.execute(
            "SELECT COUNT(DISTINCT actual) FROM runs WHERE actual IS NOT NULL").fetchone()[0]
        for row in self.db.execute("SELECT outcome, COUNT(*) FROM runs GROUP BY outcome"):
            if row[0] in ("resumed", "new", "mismatch", "unconfirmed", "pending", "unknown"):
                bridge[row[0]] = row[1]
        bridge["failed"] = self.db.execute(
            "SELECT COUNT(*) FROM runs WHERE lower(coalesce(status,'')) IN (" +
            ",".join("?" for _ in BRIDGE_FAILURES) + ")", tuple(sorted(BRIDGE_FAILURES))).fetchone()[0]
        return {"enabled": True, "version": meta.get("version", 0),
                "roots": {"workbuddy": str(self.roots.get("workbuddy-desktop"))
                          if self.roots.get("workbuddy-desktop") else None,
                          "codebuddy": str(self.roots.get("codebuddy-cli"))
                          if self.roots.get("codebuddy-cli") else None,
                          "bridge": str(self.bridge) if self.bridge else None},
                "sources": sources, "bridge": bridge,
                "lastAttempt": meta.get("lastAttempt"), "lastSuccess": meta.get("lastSuccess"),
                "lastImport": meta.get("lastImport"), "models": self.models(),
                "responseBackfillPending": bool(meta.get("responseBackfill")),
                "warnings": meta.get("lastImport", {}).get("warnings", [])}

    def session_summary(self, session_id):
        rows = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM runs WHERE actual=? OR (actual IS NULL AND requested=?) ORDER BY order_at DESC",
            (session_id, session_id))]
        if not rows:
            return {"outcome": "unknown", "actualSessionId": session_id,
                    "requested": None, "requestedValues": [], "runCount": 0,
                    "confirmedResumeCount": 0, "newSessionCount": 0,
                    "mismatchCount": 0, "failedCount": 0, "unconfirmedCount": 0,
                    "pendingCount": 0, "unknownCount": 0, "runs": [],
                    "runsTruncated": False, "contextCompression": None}
        return session_summary(rows)
