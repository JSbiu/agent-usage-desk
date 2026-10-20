"""Incremental, transactional index. Source logs are always opened read-only."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from statistics import median
import time

from .core import Row, Session, cost_value, effective_tier, response_percentiles

STATE_FIELDS = ("offset", "session", "model", "boundary", "start", "end", "tier",
                "request_tier", "output_tier", "modern", "last_total", "skipped",
                "turn_id", "turn_first", "response_version")
TUPLE_FIELDS = {"tier", "request_tier", "output_tier", "last_total"}
DAILY_COLUMNS = ("day", "model", "mode", "records", "timed", "inp", "out", "cache",
                 "cost", "priced", "timed_out", "seconds", "median", "minimum", "maximum")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@contextmanager
def import_lock(cache):
    """Chrome workers/tabs and standalone hosts share this single import lock."""
    path = Path(cache) / "import.lock"
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("另一项刷新正在进行，请稍后查看结果。") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Store:
    def __init__(self, home, cache):
        self.home, self.cache = Path(home).resolve(), Path(cache).resolve()
        self.cache.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256(str(self.home).casefold().encode()).hexdigest()[:16]
        self.db = sqlite3.connect(self.cache / ("index-" + fingerprint + ".sqlite3"), timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, identity TEXT,
                prefix TEXT, prefix_len INTEGER, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                path TEXT, key TEXT, data TEXT NOT NULL, PRIMARY KEY(path, key));
            CREATE INDEX IF NOT EXISTS observations_key ON observations(key);
            CREATE TABLE IF NOT EXISTS requests (
                key TEXT PRIMARY KEY, at REAL, model TEXT, mode TEXT, inp INTEGER,
                out INTEGER, cache INTEGER, duration REAL, speed REAL, cost REAL,
                data TEXT NOT NULL, source TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS requests_at ON requests(at);
            CREATE INDEX IF NOT EXISTS requests_filter ON requests(model, mode, at);
            CREATE TABLE IF NOT EXISTS daily (
                day TEXT, model TEXT, mode TEXT, records INTEGER, timed INTEGER,
                inp INTEGER, out INTEGER, cache INTEGER, cost REAL, priced INTEGER,
                timed_out INTEGER, seconds REAL, median REAL, minimum REAL, maximum REAL,
                PRIMARY KEY(day, model, mode));
            PRAGMA user_version=2;
            """)
        self.ensure_daily()
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
            # Separate tables keep the existing request data and frozen costs intact.
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS turn_observations (
                path TEXT, key TEXT, data TEXT NOT NULL, PRIMARY KEY(path, key));
            CREATE INDEX IF NOT EXISTS turn_observations_key ON turn_observations(key);
            CREATE TABLE IF NOT EXISTS turns (
                key TEXT PRIMARY KEY, first_token REAL, data TEXT NOT NULL, source TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS response_daily (
                day TEXT, model TEXT, mode TEXT, turns INTEGER, samples INTEGER,
                median REAL, p90 REAL, PRIMARY KEY(day, model, mode));
            PRAGMA user_version=3;
            """)
        self.ensure_response_daily()

    def close(self):
        self.db.close()

    def metadata(self):
        return {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT * FROM meta")}

    def put_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, encode(value)))

    def index_summary(self):
        r = self.db.execute("SELECT COUNT(*) records, MIN(at) earliest, MAX(at) latest FROM requests").fetchone()
        result = dict(r)
        result["models"] = [r[0] for r in self.db.execute("SELECT DISTINCT model FROM requests ORDER BY model")]
        self.put_meta("indexSummary", result)

    @staticmethod
    def group_key(row):
        return (datetime.fromtimestamp(row["at"]).date().isoformat(), row["model"], row["mode"])

    def save_groups(self, rows, keys=None):
        """Aggregate indexed numeric fields, never reparse source logs or reprice old rows."""
        groups = {}
        for r in rows:
            key = self.group_key(r)
            if key not in groups:
                groups[key] = dict(zip(("day", "model", "mode"), key), records=0, timed=0,
                    inp=0, out=0, cache=0, cost=0.0, priced=0, timed_out=0, seconds=0.0, speeds=[])
            g = groups[key]
            g["records"] += 1
            for column in ("inp", "out", "cache"):
                g[column] += r[column]
            if r["cost"] is not None:
                g["priced"] += 1
                g["cost"] += r["cost"]
            if r["speed"] is not None:
                g["timed"] += 1
                g["timed_out"] += r["out"]
                g["seconds"] += r["duration"]
                g["speeds"].append(r["speed"])
        for key in (keys if keys is not None else groups):
            g = groups.get(key)
            if g is None:
                self.db.execute("DELETE FROM daily WHERE day=? AND model=? AND mode=?", key)
                continue
            speeds = g.pop("speeds")
            g.update(median=median(speeds) if speeds else None,
                     minimum=min(speeds) if speeds else None, maximum=max(speeds) if speeds else None)
            if not g["priced"]:
                g["cost"] = None
            self.db.execute("INSERT OR REPLACE INTO daily VALUES (" + ",".join("?" for _ in DAILY_COLUMNS) + ")",
                            [g[c] for c in DAILY_COLUMNS])

    def ensure_daily(self):
        meta = self.metadata()
        zone = str((time.tzname, time.timezone))
        if (meta.get("dailyVersion") == meta.get("version", 0)
                and meta.get("calendarZone") == zone and "indexSummary" in meta):
            return
        # One upgrade pass uses the existing index. Keep the original detail schema
        # compatible with a reader process that was already running during upgrade.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            meta = self.metadata()
            if (meta.get("dailyVersion") != meta.get("version", 0)
                    or meta.get("calendarZone") != zone or "indexSummary" not in meta):
                self.db.execute("DELETE FROM daily")
                self.save_groups(self.db.execute("SELECT at,model,mode,inp,out,cache,cost,duration,speed FROM requests"))
                self.index_summary()
                self.put_meta("dailyVersion", meta.get("version", 0))
                self.put_meta("calendarZone", zone)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def update_groups(self, keys):
        for day, model, mode in sorted(keys):
            start = datetime.fromisoformat(day)
            end = start + timedelta(days=1)
            rows = self.db.execute("""SELECT at,model,mode,inp,out,cache,cost,duration,speed FROM requests
                WHERE model=? AND mode=? AND at>=? AND at<?""", (model, mode, start.timestamp(), end.timestamp()))
            self.save_groups(rows, [(day, model, mode)])

    def save_response_groups(self, rows, keys=None):
        groups = {}
        for row in rows:
            key = self.group_key(row)
            group = groups.setdefault(key, dict(turns=0, values=[]))
            group["turns"] += 1
            if row["first_token"] is not None:
                group["values"].append(row["first_token"])
        for key in (keys if keys is not None else groups):
            group = groups.get(key)
            if group is None:
                self.db.execute("DELETE FROM response_daily WHERE day=? AND model=? AND mode=?", key)
                continue
            middle, p90 = response_percentiles(group["values"])
            self.db.execute("INSERT OR REPLACE INTO response_daily VALUES (?,?,?,?,?,?,?)",
                (*key, group["turns"], len(group["values"]), middle, p90))

    def ensure_response_daily(self):
        meta = self.metadata()
        zone = str((time.tzname, time.timezone))
        if (meta.get("responseDailyVersion") == meta.get("version", 0)
                and meta.get("responseCalendarZone") == zone):
            return
        with self.db:
            self.db.execute("DELETE FROM response_daily")
            self.save_response_groups(self.db.execute("""SELECT r.at,r.model,r.mode,t.first_token
                FROM turns t JOIN requests r ON r.key=t.key"""))
            self.put_meta("responseDailyVersion", meta.get("version", 0))
            self.put_meta("responseCalendarZone", zone)

    def response_group(self, key):
        row = self.db.execute("""SELECT r.at,r.model,r.mode FROM turns t
            JOIN requests r ON r.key=t.key WHERE t.key=?""", (key,)).fetchone()
        return self.group_key(row) if row else None

    def reconcile_turns(self, keys, groups):
        updated = 0
        for key in keys:
            # Prefer the same source as the deduplicated first request.
            candidate = self.db.execute("""SELECT t.data,t.path FROM turn_observations t
                JOIN files f ON f.path=t.path JOIN requests r ON r.key=t.key
                WHERE t.key=? ORDER BY (t.path=r.source) DESC,f.mtime ASC,f.path ASC LIMIT 1""", (key,)).fetchone()
            old = self.db.execute("SELECT data,source FROM turns WHERE key=?", (key,)).fetchone()
            if candidate is None:
                if old:
                    self.db.execute("DELETE FROM turns WHERE key=?", (key,))
                    updated += 1
            elif not old or tuple(old) != tuple(candidate):
                data = json.loads(candidate["data"])
                self.db.execute("INSERT OR REPLACE INTO turns VALUES (?,?,?,?)",
                    (key, data["firstToken"], candidate["data"], candidate["path"]))
                updated += 1
            group = self.response_group(key)
            if group:
                groups.add(group)
        for day, model, mode in sorted(groups):
            start = datetime.fromisoformat(day)
            rows = self.db.execute("""SELECT r.at,r.model,r.mode,t.first_token FROM turns t
                JOIN requests r ON r.key=t.key WHERE r.model=? AND r.mode=? AND r.at>=? AND r.at<?""",
                (model, mode, start.timestamp(), (start + timedelta(days=1)).timestamp()))
            self.save_response_groups(rows, [(day, model, mode)])
        return updated

    def discover(self):
        if not (self.home / "sessions").is_dir():
            raise ValueError("找不到 Codex 会话目录，请在设置中指定包含 sessions 的 Codex 目录。")
        found = {}
        def scan(folder):
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        scan(entry.path)
                    elif entry.name.endswith(".jsonl"):
                        if entry.is_symlink():
                            path = Path(entry.path).resolve()
                            if path.is_relative_to(self.home) and path.is_file():
                                found[str(path)] = path.stat()
                        elif entry.is_file(follow_symlinks=False):
                            # DirEntry.stat() reports zero file IDs on Windows;
                            # os.stat() retains replacement detection and old offsets.
                            found[entry.path] = os.stat(entry.path, follow_symlinks=False)
        for folder in (self.home / "sessions", self.home / "archived_sessions"):
            if not folder.exists():
                continue
            scan(folder)
        return found

    @staticmethod
    def parser(path, saved=None):
        session = Session(Path(path), max_rows=None)
        if saved:
            state = json.loads(saved)
            for field in STATE_FIELDS:
                if field in state:
                    value = state[field]
                    setattr(session, field, tuple(value) if field in TUPLE_FIELDS and value else value)
        return session

    @staticmethod
    def read(session, usage_after=0):
        with session.path.open("rb") as stream:
            stream.seek(session.offset)
            while True:
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    break
                session.offset = stream.tell()
                try:
                    session.feed(json.loads(line))
                    if session.offset <= usage_after:
                        session.rows.clear()  # Upgrade reads only new timing facts from old bytes.
                except (ValueError, UnicodeError, TypeError, OverflowError):
                    session.skipped += 1
                    session.boundary = session.start = session.end = session.output_tier = None
                    session.tier = session.request_tier = ("unknown", "unknown")
                    session.last_total = None
                    session.turn_id = session.turn_first = None

    @staticmethod
    def row_data(row, session):
        # Keep only usage numbers. No prompts, output text, tool bodies or credentials.
        data = asdict(row)
        names = ("input_tokens", "output_tokens", "cached_input_tokens",
                 "reasoning_output_tokens", "cache_write_input_tokens")
        data["usage"] = {k: row.usage[k] for k in names if k in row.usage}
        data["sessionId"] = str(session)
        return data

    def refresh(self, progress=None):
        started = time.time()
        with import_lock(self.cache):
            paths = self.discover()
            previous = {r["path"]: r for r in self.db.execute("SELECT * FROM files")}
            dirty, warnings, changed_files, skipped = set(), [], 0, 0
            turn_dirty, backfilled = set(), 0
            with self.db:
                for number, (path, stat) in enumerate(sorted(paths.items()), 1):
                    if progress and (number == 1 or number % 20 == 0 or number == len(paths)):
                        progress({"completed": number - 1, "total": len(paths), "phase": "读取日志"})
                    old = previous.get(path)
                    backfill = bool(old and json.loads(old["state"]).get("response_version") != 1)
                    identity = str((stat.st_dev, stat.st_ino))
                    if (old and not backfill and old["size"] == stat.st_size and old["mtime"] == stat.st_mtime_ns
                            and old["identity"] == identity
                            and json.loads(old["state"])["offset"] >= stat.st_size):
                        continue
                    try:
                        prefix_len = old["prefix_len"] if old else min(512, stat.st_size)
                        with Path(path).open("rb") as stream:
                            prefix = hashlib.sha256(stream.read(prefix_len)).hexdigest()
                        reset = (not old or stat.st_size < old["size"] or old["identity"] != identity
                                 or old["prefix"] != prefix)
                        session = self.parser(path, None if reset or backfill else old["state"])
                        old_skipped = session.skipped
                        self.read(session, json.loads(old["state"])["offset"] if backfill and not reset else 0)
                        bad = session.skipped - old_skipped
                        skipped += bad
                        if bad:
                            warnings.append(Path(path).name + "：跳过 " + str(bad) + " 行损坏日志")
                    except OSError as exc:
                        warnings.append(Path(path).name + "：" + str(exc))
                        continue  # Keep the earlier good observations and file state.
                    changed_files += 1
                    backfilled += backfill
                    if reset or backfill:
                        turn_dirty.update(r[0] for r in self.db.execute(
                            "SELECT key FROM turn_observations WHERE path=?", (path,)))
                        self.db.execute("DELETE FROM turn_observations WHERE path=?", (path,))
                    for turn in session.turns:
                        self.db.execute("INSERT OR REPLACE INTO turn_observations VALUES (?,?,?)",
                            (path, turn["key"], encode(turn)))
                        turn_dirty.add(turn["key"])
                    if reset:
                        dirty.update(r[0] for r in self.db.execute(
                            "SELECT key FROM observations WHERE path=?", (path,)))
                        self.db.execute("DELETE FROM observations WHERE path=?", (path,))
                    else:
                        # Appending to a long session cannot change its earlier unique
                        # requests. Only duplicate sources can change their winner by mtime.
                        dirty.update(r[0] for r in self.db.execute("""SELECT o.key FROM observations o
                            WHERE o.path=? AND EXISTS (SELECT 1 FROM observations d
                            WHERE d.key=o.key AND d.path<>o.path)""", (path,)))
                    for row in session.rows:
                        if row.model == "codex-auto-review":
                            continue
                        data = encode(self.row_data(row, session.session))
                        self.db.execute("INSERT OR REPLACE INTO observations VALUES (?, ?, ?)",
                                        (path, row.key, data))
                        dirty.add(row.key)
                    state = encode({field: getattr(session, field) for field in STATE_FIELDS})
                    if prefix_len != min(512, stat.st_size):
                        prefix_len = min(512, stat.st_size)
                        with Path(path).open("rb") as stream:
                            prefix = hashlib.sha256(stream.read(prefix_len)).hexdigest()
                    self.db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (path, stat.st_size, stat.st_mtime_ns, identity, prefix, prefix_len, state))
                for path in previous.keys() - paths.keys():
                    dirty.update(r[0] for r in self.db.execute(
                        "SELECT key FROM observations WHERE path=?", (path,)))
                    self.db.execute("DELETE FROM observations WHERE path=?", (path,))
                    self.db.execute("DELETE FROM files WHERE path=?", (path,))
                    turn_dirty.update(r[0] for r in self.db.execute(
                        "SELECT key FROM turn_observations WHERE path=?", (path,)))
                    self.db.execute("DELETE FROM turn_observations WHERE path=?", (path,))
                updated = added = historical = 0
                changed_groups = set()
                turn_dirty.update(dirty)
                response_groups = {group for key in turn_dirty if (group := self.response_group(key))}
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                for key in dirty:
                    # CLI scans newest files first, and the last duplicate wins.
                    candidate = self.db.execute("""SELECT o.data, o.path FROM observations o
                        JOIN files f ON f.path=o.path WHERE o.key=?
                        ORDER BY f.mtime ASC, f.path ASC LIMIT 1""", (key,)).fetchone()
                    old = self.db.execute("SELECT data,source,at,model,mode FROM requests WHERE key=?", (key,)).fetchone()
                    if candidate is None:
                        updated += bool(old)
                        if old:
                            changed_groups.add(self.group_key(old))
                        self.db.execute("DELETE FROM requests WHERE key=?", (key,))
                    elif not old or (old["data"], old["source"]) != tuple(candidate):
                        data = json.loads(candidate["data"])
                        row = Row(**{k: data[k] for k in Row.__dataclass_fields__})
                        u = row.usage
                        if old:
                            changed_groups.add(self.group_key(old))
                        else:
                            added += 1
                            historical += row.at < today
                        changed_groups.add((datetime.fromtimestamp(row.at).date().isoformat(), row.model, effective_tier(row)))
                        speed = u["output_tokens"] / row.duration if row.duration and u["output_tokens"] else None
                        self.db.execute("INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (row.key, row.at, row.model, effective_tier(row), u["input_tokens"],
                                         u["output_tokens"], u["cached_input_tokens"], row.duration, speed,
                                         cost_value(row), candidate["data"], candidate["path"]))
                        updated += 1
                updated_turns = self.reconcile_turns(turn_dirty, response_groups)
                meta = self.metadata()
                version = meta.get("version", 0) + bool(updated or updated_turns)
                if updated:
                    self.update_groups(changed_groups)
                    self.index_summary()
                result = dict(added=added, historical=historical, rebuiltGroups=len(changed_groups),
                              responseBackfilledFiles=backfilled, updatedTurns=updated_turns,
                              changedFiles=changed_files, files=len(paths), skippedLines=skipped,
                              warnings=warnings[:20], partial=bool(warnings), elapsed=time.time() - started)
                self.put_meta("version", version)
                self.put_meta("dailyVersion", version)
                self.put_meta("responseDailyVersion", version)
                if not self.db.execute('SELECT 1 FROM files WHERE state NOT LIKE ? LIMIT 1',
                        ('%"response_version":1%',)).fetchone():
                    self.put_meta("responseVersion", 1)
                self.put_meta("lastAttempt", time.time())
                self.put_meta("lastImport", result)
                if not warnings:
                    self.put_meta("lastSuccess", time.time())
            return result
