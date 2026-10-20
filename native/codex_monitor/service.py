"""Small local API; all filters apply to one indexed data snapshot."""
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
from statistics import median as _median
import time

from . import __version__
from .buddy import LABELS, SOURCES
from .buddy_quota import BuddyQuotaReader, empty_snapshot
from .buddy_store import BuddyStore
from .core import response_percentiles
from .pricing import CHECKED as PRICING_CHECKED, PRICE_VERSION
from .quota import read_live, read_logs
from .store import Store, encode, import_lock

BUDDY_SOURCES = SOURCES
SOURCE_IDS = ("codex",) + SOURCES + ("all",)
MODE_NAMES = {"-": -1, "": -1, "standard": 0, "fast": 1}
SOURCE_ORDER = {"codex": 0, "workbuddy-desktop": 1, "codebuddy-cli": 2, "": 3}


def default_codex_home():
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def default_buddy_roots():
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home))
    return {"workbuddy": home / ".workbuddy" / "projects",
            "codebuddy": home / ".codebuddy" / "projects",
            "bridge": local / "CodexDeepSeek" / "runs"}


def empty_cards():
    return dict(records=0, codexRecords=0, timed=0, input=0, output=0, cache=0,
                cacheHit=None, cacheKnown=0, cost=None, priced=0, costUnknown=0,
                average=None, median=None, minimum=None, maximum=None,
                timedOut=0, seconds=0.0, traceTimed=0, boundaryTimed=0,
                responseTurns=0, responseSamples=0, responseMedian=None,
                responseP90=None, credit=None, creditCount=0,
                creditApplicable=0, creditMissing=0, sourceRecords={})


class Service:
    def __init__(self, cache=None, home=None, quota_reader=None, buddy_roots=None,
                 bridge_dir=None, buddy_quota_reader=None):
        self.cache = Path(cache or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CodexMonitor")
        self.cache.mkdir(parents=True, exist_ok=True)
        self.explicit_home = home
        self.explicit_buddy_roots = buddy_roots
        self.explicit_bridge = bridge_dir
        self._quota_reader_injected = quota_reader is not None
        self._buddy_quota_injected = buddy_quota_reader is not None
        self.quota_reader = quota_reader or read_live
        self.buddy_quota_reader = self._buddy_quota_reader(buddy_quota_reader)

    def _buddy_quota_reader(self, reader):
        if reader is not None:
            return reader if callable(reader) else None
        # An injected Codex reader is a test/automation boundary.  WorkBuddy is
        # independent of the Codex home, so a custom home alone must not disable
        # the real WorkBuddy account reader.
        if self._quota_reader_injected:
            return None
        return BuddyQuotaReader(self.cache)

    def config(self):
        path = self.cache / "config.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def home(self):
        if self.explicit_home:
            return Path(self.explicit_home).expanduser().resolve()
        value = self.config().get("home")
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser().resolve()
        return default_codex_home()

    def buddy_roots(self):
        """Return ({source: root}, bridge). Defaults are only enabled for the
        conventional installation, so programmatic/custom-home uses do not
        silently read an unrelated user's logs."""
        config = self.config()
        defaults = default_buddy_roots()
        explicit = self.explicit_buddy_roots if isinstance(self.explicit_buddy_roots, dict) else {}
        explicit = {key: value for key, value in explicit.items() if value}
        bridge_explicit = self.explicit_bridge
        configured = any(key in config for key in ("workbuddy", "codebuddy", "bridge"))
        enabled = bool(explicit or bridge_explicit or configured)
        if not enabled and self.explicit_home is None:
            enabled = True
        if not enabled and isinstance(config.get("home"), str) and config["home"].strip():
            enabled = True
        if not enabled and self.explicit_home is not None:
            try:
                enabled = Path(self.explicit_home).expanduser().resolve() == default_codex_home()
            except OSError:
                enabled = False
        if not enabled:
            return {}, None

        def pick(key, default):
            value = explicit.get(key)
            if value is None:
                value = config.get(key)
            if isinstance(value, (str, Path)):
                text = str(value)
                return Path(text).expanduser().resolve() if text.strip() else default.resolve()
            return default.resolve()

        roots = {"workbuddy-desktop": pick("workbuddy", defaults["workbuddy"]),
                 "codebuddy-cli": pick("codebuddy", defaults["codebuddy"])}
        bridge = bridge_explicit
        if bridge is None:
            bridge = config.get("bridge")
        bridge = (Path(bridge).expanduser().resolve()
                  if isinstance(bridge, (str, Path)) and str(bridge).strip()
                  else defaults["bridge"].resolve())
        return roots, bridge

    def buddy_store(self):
        roots, bridge = self.buddy_roots()
        if not roots and bridge is None:
            return None
        return BuddyStore(self.cache, roots, bridge)

    @staticmethod
    def filters(params):
        today = date.today()
        start_value = params.get("from") or (today - timedelta(days=2)).isoformat()
        end_value = params.get("to") or today.isoformat()
        if not isinstance(start_value, str) or not isinstance(end_value, str):
            raise ValueError("日期条件无效。")
        try:
            start = date.fromisoformat(start_value)
            end = date.fromisoformat(end_value)
        except ValueError as exc:
            raise ValueError("日期条件无效。") from exc
        days = (end - start).days + 1
        if not 1 <= days <= 3660:
            raise ValueError("日期范围应为 1 至 3660 天。")
        since = datetime.combine(start, datetime.min.time()).timestamp()
        until = datetime.combine(end + timedelta(days=1), datetime.min.time()).timestamp()
        conditions, values = ["at>=?", "at<?"], [since, until]
        model, mode = params.get("model", ""), params.get("mode", "")
        if not isinstance(model, str) or len(model) > 160:
            raise ValueError("模型条件无效。")
        if model:
            conditions.append("model=?")
            values.append(model)
        if mode not in ("", "standard", "fast"):
            raise ValueError("模式条件无效。")
        if mode:
            conditions.append("mode=?")
            values.append(mode)
        return " AND ".join(conditions), values, since, days

    @staticmethod
    def source(params):
        value = params.get("source", "codex")
        if value in (None, ""):
            value = "codex"
        if value not in SOURCE_IDS:
            raise ValueError("来源条件无效。")
        return value

    @staticmethod
    def paging(params, default=50):
        page, size = params.get("page", 1), params.get("pageSize", default)
        if type(page) is not int or page < 1 or type(size) is not int or size not in (20, 50, 100):
            raise ValueError("分页参数无效。")
        return page, size

    @staticmethod
    def response_latencies(store, request_where, request_values):
        return [row[0] for row in store.db.execute("""SELECT t.first_token FROM turns t
            JOIN requests r ON r.key=t.key WHERE t.first_token IS NOT NULL AND """
            + request_where, request_values)]

    @staticmethod
    def cards(store, where, values, request_where, request_values, latencies=None):
        r = store.db.execute("""SELECT SUM(records) records, SUM(inp) inp, SUM(out) out, SUM(cache) cache,
            SUM(cost) cost, SUM(priced) priced, SUM(timed) timed,
            SUM(timed_out) timed_out, SUM(seconds) seconds
            FROM daily WHERE """ + where, values).fetchone()
        response = store.db.execute("SELECT SUM(turns) turns,SUM(samples) samples FROM response_daily WHERE " + where,
                                    values).fetchone()
        # Percentiles across a range come from individual turns, never daily medians.
        if latencies is None:
            latencies = Service.response_latencies(store, request_where, request_values)
        middle, p90 = response_percentiles(latencies)
        records = r["records"] or 0
        timed = r["timed"] or 0
        seconds = r["seconds"] or 0
        speeds = [row[0] for row in store.db.execute(
            "SELECT speed FROM requests WHERE speed IS NOT NULL AND " + request_where,
            request_values)]
        return dict(responseTurns=response["turns"] or 0, responseSamples=response["samples"] or 0,
                    responseMedian=middle, responseP90=p90,
                    records=records, codexRecords=records, timed=timed,
                    input=r["inp"] or 0, output=r["out"] or 0,
                    cache=r["cache"] or 0,
                    cacheHit=r["cache"] / r["inp"] if r["inp"] else None, cacheKnown=records,
                    cost=r["cost"], priced=r["priced"] or 0,
                    costUnknown=records - (r["priced"] or 0),
                    average=r["timed_out"] / seconds if seconds else None,
                    median=_median(speeds) if speeds else None,
                    minimum=min(speeds) if speeds else None,
                    maximum=max(speeds) if speeds else None,
                    timedOut=r["timed_out"] or 0, seconds=seconds,
                    traceTimed=0, boundaryTimed=timed,
                    credit=None, creditCount=0, creditApplicable=0, creditMissing=0)

    @staticmethod
    def merge_cards(codex, buddy, codex_speeds=None, buddy_speeds=None,
                    response_values=None):
        result = empty_cards()
        for key in ("records", "timed", "input", "output", "cacheKnown", "priced",
                    "responseTurns", "responseSamples", "timedOut", "seconds",
                    "traceTimed", "boundaryTimed"):
            result[key] = (codex.get(key) or 0) + (buddy.get(key) or 0)
        result["priced"] = (codex.get("priced") or 0) + (buddy.get("priced") or 0)
        result["costUnknown"] = result["records"] - result["priced"]
        costs = [value for value in (codex.get("cost"), buddy.get("cost"))
                 if value is not None]
        result["cost"] = sum(costs) if costs else None
        result["average"] = (result["timedOut"] / result["seconds"]
                             if result["seconds"] else None)
        speeds = list(codex_speeds or []) + list(buddy_speeds or [])
        if speeds:
            result["median"] = _median(speeds)
            result["minimum"] = min(speeds)
            result["maximum"] = max(speeds)
        else:
            bounds = [(value[0], value[1]) for value in
                      ((codex.get("minimum"), codex.get("maximum")),
                       (buddy.get("minimum"), buddy.get("maximum")))
                      if value[0] is not None and value[1] is not None]
            result["minimum"] = min((value[0] for value in bounds), default=None)
            result["maximum"] = max((value[1] for value in bounds), default=None)
            result["median"] = (codex.get("median") if not buddy.get("timed")
                                else buddy.get("median") if not codex.get("timed")
                                else None)
        result["responseMedian"] = codex.get("responseMedian")
        result["responseP90"] = codex.get("responseP90")
        if response_values is not None:
            # Mixed ranges use the union of original Codex turn latencies and
            # valid Buddy per-request latencies, never group/card medians.
            middle, p90 = response_percentiles(response_values)
            result["responseMedian"] = middle
            result["responseP90"] = p90
        result["codexRecords"] = codex.get("records") or 0
        result["credit"] = buddy.get("credit")
        result["creditCount"] = buddy.get("creditCount") or 0
        result["creditApplicable"] = buddy.get("creditApplicable") or 0
        result["creditMissing"] = buddy.get("creditMissing") or 0
        cache = (codex.get("cache") or 0) + (buddy.get("cache") or 0)
        result["cache"] = cache
        cache_known = result["cacheKnown"]
        result["cacheHit"] = (cache / result["input"]
                              if result["input"] and cache_known == result["records"] else None)
        result["sourceRecords"] = dict(codex.get("sourceRecords") or {}, **buddy.get("sourceRecords") or {})
        return result

    @staticmethod
    def status(store, buddy=None, buddy_warning=None):
        meta = store.metadata()
        r = meta["indexSummary"]
        buddy_status = Service.buddy_status(buddy)
        if buddy_warning:
            buddy_status["warning"] = buddy_warning
        version = int(meta.get("version", 0)) + int(buddy_status.get("version", 0))
        source_models = {"codex": r["models"]}
        source_models.update(buddy_status.get("models") or {})
        try:
            codex_health = store.health_summary()
        except (OSError, sqlite3.Error):
            codex_health = {"skipped": 0, "invalid": 0}
        sources = [{"id": "codex", "label": "Codex", "available": True, "records": r["records"],
                    "skipped": codex_health["skipped"], "invalid": codex_health["invalid"]}]
        for source in BUDDY_SOURCES:
            info = buddy_status["sources"][source]
            sources.append({"id": source, "label": LABELS[source],
                            "available": info["exists"], "records": info["records"],
                            "skipped": int(info.get("skipped") or 0),
                            "invalid": int(info.get("invalid") or 0)})
        health = {"skipped": sum(item["skipped"] for item in sources),
                  "invalid": sum(item["invalid"] for item in sources)}
        return dict(home=str(store.home), version=version, records=r["records"],
                    earliest=r["earliest"], latest=r["latest"], lastSuccess=meta.get("lastSuccess"),
                    lastAttempt=meta.get("lastAttempt"), lastImport=meta.get("lastImport"),
                    pricingVersion=PRICE_VERSION, pricingChecked=PRICING_CHECKED,
                    models=r["models"], sourceModels=source_models,
                    allModels=sorted({model for values in source_models.values() for model in values}),
                    sources=sources, health=health, buddy=buddy_status, appVersion=__version__,
                    quota=meta.get("quota"), responseBackfillPending=meta.get("responseVersion") != 1)

    @staticmethod
    def disabled_buddy_status():
        return {"enabled": False, "version": 0,
                "roots": {"workbuddy": None, "codebuddy": None, "bridge": None},
                "sources": {source: {"root": None, "exists": False, "files": 0, "records": 0}
                            for source in BUDDY_SOURCES},
                "bridge": {"root": None, "exists": False, "runs": 0, "sessions": 0,
                           "resumed": 0, "new": 0, "mismatch": 0, "unconfirmed": 0,
                           "pending": 0, "failed": 0},
                "lastAttempt": None, "lastSuccess": None, "lastImport": None,
                "models": {source: [] for source in BUDDY_SOURCES},
                "responseBackfillPending": False}

    @staticmethod
    def buddy_status(buddy):
        if buddy is None:
            return Service.disabled_buddy_status()
        try:
            return buddy.status()
        except (OSError, ValueError, sqlite3.Error):
            value = Service.disabled_buddy_status()
            value["warning"] = "来源索引暂不可用。"
            return value

    @staticmethod
    def detail(row, home):
        data = json.loads(row["data"])
        turn = json.loads(row["turn_data"]) if row["turn_data"] else None
        return dict(key=row["key"], at=row["at"], model=row["model"], mode=row["mode"],
                    input=row["inp"], output=row["out"], cacheHit=row["cache"] / row["inp"] if row["inp"] else None,
                    cacheKnown=True, cost=row["cost"], credit=None, creditValid=False,
                    speed=row["speed"], duration=row["duration"],
                    firstToken=row["first_token"], turnId=turn["turnId"] if turn else None,
                    firstInTurn=turn is not None,
                    sessionId=data["sessionId"], source=str(Path(row["source"]).relative_to(home)),
                    sourceId="codex", messageId=None, requestId=None, session=None,
                    modeSource=data["tier_source"], modeAssumed=data["tier"] not in ("standard", "fast"),
                    longContext=row["inp"] > 272000, pricingVersion=PRICE_VERSION,
                    pricingBasis="OpenAI 官方价格" if row["cost"] is not None else None)

    @staticmethod
    def buddy_detail(row, buddy, sessions=None):
        path = row.get("path")
        root = buddy.roots.get(row["source"]) if buddy else None
        try:
            source = str(Path(path).relative_to(root)) if path and root else (Path(path).name if path else None)
        except (ValueError, TypeError):
            source = Path(path).name if path else None
        session = None
        if buddy and row["source"] == "codebuddy-cli":
            sessions = sessions if sessions is not None else {}
            if row["sessionId"] not in sessions:
                sessions[row["sessionId"]] = buddy.session_summary(row["sessionId"])
            session = sessions[row["sessionId"]]
        return dict(key=row["key"], at=row["at"], model=row["model"], mode="",
                    input=row["input"], output=row["output"],
                    cacheHit=(row["cache"] / row["input"] if row.get("cacheKnown") and row["input"] else None),
                    cacheKnown=bool(row.get("cacheKnown")), cost=row.get("cost"),
                    costBasis=row.get("costBasis"), priced=bool(row.get("priced")),
                    credit=row.get("credit"), creditValid=bool(row.get("creditValid")),
                    speed=row.get("speed"), duration=row.get("duration"),
                    timed=row.get("duration") is not None,
                    timingSource=row.get("timingSource"), traceId=row.get("traceId"),
                    firstToken=row.get("firstToken"), firstTokenSource=row.get("firstTokenSource"),
                    firstTokenBasis=row.get("firstTokenBasis"), turnId=None,
                    firstInTurn=False, sessionId=row["sessionId"], source=source,
                    sourceId=row["source"], messageId=row.get("messageId"),
                    requestId=row.get("requestId"), session=session,
                    modeSource="log", modeAssumed=False, longContext=None,
                    pricingVersion=PRICE_VERSION,
                    pricingBasis=row.get("costBasis"))

    @staticmethod
    def codex_groups(store, group_where, group_values, merge_modes=False):
        if not merge_modes:
            groups = []
            responses = {(r["day"], r["model"], r["mode"]): r for r in store.db.execute(
                "SELECT * FROM response_daily WHERE " + group_where, group_values)}
            for g in store.db.execute("SELECT * FROM daily WHERE " + group_where + " ORDER BY day,model,mode DESC", group_values):
                response = responses.get((g["day"], g["model"], g["mode"]))
                groups.append(dict(day=g["day"], source="codex", model=g["model"], mode=g["mode"],
                    records=g["records"], responseTurns=response["turns"] if response else 0,
                    responseSamples=response["samples"] if response else 0,
                    responseMedian=response["median"] if response else None,
                    responseP90=response["p90"] if response else None,
                    timed=g["timed"], input=g["inp"], output=g["out"],
                    cacheHit=g["cache"] / g["inp"] if g["inp"] else None,
                    cacheKnown=g["records"], cacheMissing=0,
                    cost=g["cost"] if g["priced"] else None, priced=g["priced"],
                    costUnknown=g["records"] - g["priced"],
                    credit=None, creditCount=0, creditApplicable=0, creditMissing=0,
                    average=g["timed_out"] / g["seconds"] if g["seconds"] else None,
                    timedOut=g["timed_out"], seconds=g["seconds"],
                    traceTimed=0, boundaryTimed=g["timed"],
                    median=g["median"], minimum=g["minimum"], maximum=g["maximum"],
                    frozen=g["day"] < date.today().isoformat()))
            return groups
        merged = {}
        for g in store.db.execute("SELECT * FROM daily WHERE " + group_where
                                  + " ORDER BY day,model,mode", group_values):
            key = (g["day"], g["model"])
            group = merged.setdefault(key, dict(
                day=g["day"], source="codex", model=g["model"], mode="", records=0,
                timed=0, timedOut=0, seconds=0.0, input=0, output=0, cache=0,
                cost=0.0, priced=0))
            group["records"] += g["records"]
            group["timed"] += g["timed"]
            group["timedOut"] += g["timed_out"]
            group["seconds"] += g["seconds"] or 0
            group["input"] += g["inp"]
            group["output"] += g["out"]
            group["cache"] += g["cache"]
            if g["priced"]:
                group["priced"] += g["priced"]
                group["cost"] += g["cost"] or 0
        groups = []
        for (day, model), group in merged.items():
            start = datetime.fromisoformat(day).timestamp()
            end = start + 86400
            speeds = [row[0] for row in store.db.execute(
                "SELECT speed FROM requests WHERE model=? AND at>=? AND at<? AND speed IS NOT NULL",
                (model, start, end))]
            turns = [row[0] for row in store.db.execute(
                """SELECT t.first_token FROM turns t JOIN requests r ON r.key=t.key
                WHERE r.model=? AND r.at>=? AND r.at<?""", (model, start, end))]
            middle, p90 = response_percentiles([value for value in turns
                                                if value is not None])
            values = sorted(speeds)
            groups.append(dict(
                day=day, source="codex", model=model, mode="",
                records=group["records"], responseTurns=len(turns),
                responseSamples=len([value for value in turns if value is not None]),
                responseMedian=middle, responseP90=p90,
                timed=group["timed"], input=group["input"], output=group["output"],
                cacheHit=group["cache"] / group["input"] if group["input"] else None,
                cacheKnown=group["records"], cacheMissing=0,
                cost=group["cost"] if group["priced"] else None,
                priced=group["priced"], costUnknown=group["records"] - group["priced"],
                credit=None, creditCount=0, creditApplicable=0, creditMissing=0,
                average=group["timedOut"] / group["seconds"] if group["seconds"] else None,
                timedOut=group["timedOut"], seconds=group["seconds"],
                traceTimed=0, boundaryTimed=group["timed"],
                median=_median(values) if values else None,
                minimum=values[0] if values else None,
                maximum=values[-1] if values else None,
                frozen=day < date.today().isoformat()))
        return groups

    @staticmethod
    def project_groups(store, buddy, since, days, source):
        """Aggregate indexed requests by working directory.

        Rows without a recorded directory stay in their own bucket and are never
        merged into a guess; the caller decides how to label them.
        """
        until = since + days * 86400
        merged = {}

        def bucket(project):
            key = project or ""
            item = merged.get(key)
            if item is None:
                item = merged[key] = {"project": key, "records": 0, "input": 0,
                                      "output": 0, "cache": 0, "cost": None,
                                      "priced": 0, "sources": {}}
            return item

        def add(project, source_id, records, input_tokens, output_tokens,
                cache_tokens, cost, priced):
            item = bucket(project)
            item["records"] += records
            item["input"] += input_tokens or 0
            item["output"] += output_tokens or 0
            item["cache"] += cache_tokens or 0
            per_source = item["sources"].setdefault(source_id, {"records": 0, "cost": None})
            per_source["records"] += records
            if cost is not None:
                item["cost"] = (item["cost"] or 0) + cost
                item["priced"] += priced
                per_source["cost"] = (per_source["cost"] or 0) + cost

        if source in (None, "codex"):
            for row in store.db.execute(
                    "SELECT project, COUNT(*), SUM(inp), SUM(out), SUM(cache),"
                    " SUM(cost), COUNT(cost) FROM requests WHERE at >= ? AND at < ?"
                    " GROUP BY project", (since, until)):
                add(row[0], "codex", row[1], row[2], row[3], row[4], row[5], row[6])

        if buddy is not None and source in (None, "workbuddy-desktop", "codebuddy-cli"):
            for row in buddy.db.execute(
                    "SELECT project, source, COUNT(*), SUM(input), SUM(output), SUM(cache),"
                    " SUM(cost), COUNT(cost) FROM requests WHERE at >= ? AND at < ?"
                    " GROUP BY project, source", (since, until)):
                add(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7])

        rows = sorted(merged.values(),
                      key=lambda item: (item["cost"] is None, -(item["cost"] or 0),
                                        -item["records"]))
        return rows

    @staticmethod
    def buddy_groups(rows):
        groups = {}
        for row in rows:
            day = datetime.fromtimestamp(row["at"]).date().isoformat()
            key = (day, row["source"], row["model"])
            if key not in groups:
                groups[key] = dict(day=day, source=row["source"], model=row["model"], mode="",
                    records=0, timed=0, timedOut=0, seconds=0.0, speeds=[],
                    traceTimed=0, boundaryTimed=0,
                    input=0, output=0, cache=0, cacheKnown=0,
                    cost=0.0, priced=0, credit=0.0, creditCount=0,
                    average=None, median=None, minimum=None, maximum=None,
                    responseTurns=0, responseSamples=0, responseMedian=None,
                    responseP90=None, frozen=False, firstTokens=[])
            group = groups[key]
            group["records"] += 1
            if row.get("firstToken") is not None:
                group["firstTokens"].append(row["firstToken"])
            group["input"] += row["input"]
            group["output"] += row["output"]
            if row.get("cacheKnown"):
                group["cacheKnown"] += 1
                group["cache"] += row["cache"]
            if row.get("creditValid"):
                group["credit"] += row["credit"]
                group["creditCount"] += 1
            duration = row.get("duration")
            if duration and duration > 0 and row["output"] > 0:
                group["timed"] += 1
                group["timedOut"] += row["output"]
                group["seconds"] += duration
                group["speeds"].append(row["output"] / duration)
                if row.get("timingSource") == "trace":
                    group["traceTimed"] += 1
                elif row.get("timingSource") == "boundary":
                    group["boundaryTimed"] += 1
            if row.get("cost") is not None:
                group["cost"] += row["cost"]
                group["priced"] += 1
        for group in groups.values():
            group["cacheHit"] = (group["cache"] / group["input"]
                                 if group["input"] and group["cacheKnown"] == group["records"] else None)
            group["cacheMissing"] = group["records"] - group["cacheKnown"]
            group["creditApplicable"] = group["records"]
            group["creditMissing"] = group["records"] - group["creditCount"]
            if not group["creditCount"]:
                group["credit"] = None
            speeds = group.pop("speeds")
            first_tokens = group.pop("firstTokens")
            middle, p90 = response_percentiles(first_tokens)
            group["responseTurns"] = group["records"]
            group["responseSamples"] = len(first_tokens)
            group["responseMedian"] = middle
            group["responseP90"] = p90
            group["average"] = (group["timedOut"] / group["seconds"]
                                if group["seconds"] else None)
            group["median"] = _median(speeds) if speeds else None
            group["minimum"] = min(speeds) if speeds else None
            group["maximum"] = max(speeds) if speeds else None
            group["cost"] = group["cost"] if group["priced"] else None
            group["costUnknown"] = group["records"] - group["priced"]
        return list(groups.values())

    @staticmethod
    def empty_group(day, source):
        return dict(day=day, source=source, model="-", mode="-", records=0, timed=0,
                    input=0, output=0, cacheHit=None, cacheKnown=0, cacheMissing=0,
                    cost=None, priced=0, costUnknown=0, credit=None, creditCount=0,
                    creditApplicable=0, creditMissing=0,
                    average=None, responseTurns=0, responseSamples=0, responseMedian=None,
                    responseP90=None, median=None, minimum=None, maximum=None,
                    timedOut=0, seconds=0.0, traceTimed=0, boundaryTimed=0,
                    frozen=day < date.today().isoformat())

    @staticmethod
    def sort_details(rows, sort, direction):
        field = {"time": "at", "speed": "speed", "cost": "cost", "firstToken": "firstToken"}[sort]
        def key(row):
            value = row.get(field)
            ordered = -value if direction == "desc" and value is not None else value
            return (value is None, ordered, -row["at"], row["key"])
        return sorted(rows, key=key)

    @staticmethod
    def buddy_report():
        return {"enabled": False, "sources": {}, "bridge": {}, "warnings": [],
                "added": 0, "updated": 0, "removed": 0, "changedFiles": 0,
                "skippedLines": 0, "invalidValues": 0, "duplicates": 0,
                "elapsed": 0.0, "partial": False, "timingUpdated": 0,
                "costUpdated": 0, "traceChanged": [], "responseUpdated": 0,
                "diagnosticFiles": 0, "diagnosticChangedFiles": 0,
                "diagnosticEvidence": 0, "diagnosticInvalid": 0,
                "diagnosticSkipped": 0, "responseBackfillPending": False}

    def buddy_quota(self):
        reader = self.buddy_quota_reader
        if reader is None:
            return empty_snapshot("unknown", "WorkBuddy 额度查询未启用。")
        if not self._buddy_quota_injected and self.quota_reader is not read_live:
            # Existing offline quota tests inject a Codex reader after
            # construction; do not let the default Buddy reader reach the real
            # credential boundary in that situation.
            return empty_snapshot("unknown", "WorkBuddy 额度查询未启用。")
        try:
            value = reader()
        except Exception:
            return empty_snapshot("error", "WorkBuddy 额度暂时无法读取。")
        if not isinstance(value, dict) or value.get("status") not in (
                "known", "stale", "error", "unknown"):
            return empty_snapshot("error", "WorkBuddy 额度暂时无法读取。")
        return value

    def quota(self, params):
        home = self.home()
        store = Store(home, self.cache)
        try:
            previous = store.metadata().get("quota")
            paths = [r[0] for r in store.db.execute("SELECT path FROM files ORDER BY mtime DESC LIMIT 4")]
        finally:
            store.close()
        if params.get("live", True) is False:
            return {"home": str(home), "quota": previous}
        # Close SQLite before waiting on either account API so data queries stay fast.
        try:
            value = self.quota_reader(home)
            error = None
        except (RuntimeError, OSError) as exc:
            value = read_logs(paths)
            if previous and (not value or (previous.get("fetchedAt") or 0) >= value["fetchedAt"]):
                value = previous.copy()
            error = str(exc) if isinstance(exc, RuntimeError) else "账号额度暂未更新，请检查 Codex 登录或网络。"
            if value:
                error += " 当前显示上次可用结果。"
        value = value if isinstance(value, dict) else {}
        if not value:
            value = {"buckets": [], "fetchedAt": None, "source": None}
        # WorkBuddy is an independent account read: a Codex failure must not
        # hide it and a WorkBuddy failure must not affect the Codex snapshot.
        value["buddy"] = self.buddy_quota()
        value.update(lastAttempt=time.time(), error=error)
        store = Store(home, self.cache)
        try:
            with store.db:
                store.put_meta("quota", value)
        finally:
            store.close()
        return {"home": str(home), "quota": value}

    def configure(self, params):
        config = self.config()
        if "home" in params:
            home = params.get("home")
            if not isinstance(home, str) or not home.strip():
                raise ValueError("请选择包含 sessions 子目录的 Codex 目录。")
            try:
                path = Path(home).expanduser().resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError("Codex 目录无效。") from exc
            if not (path / "sessions").is_dir():
                raise ValueError("请选择包含 sessions 子目录的 Codex 目录。")
            config["home"] = str(path)
        for key, label in (("workbuddy", "WorkBuddy"), ("codebuddy", "CodeBuddy CLI"),
                           ("bridge", "桥接运行")):
            if key not in params:
                continue
            value = params[key]
            if not isinstance(value, str) or len(value) > 2048 or "\x00" in value:
                raise ValueError(label + "目录无效。")
            value = value.strip()
            if not value:
                config.pop(key, None)
                continue
            try:
                path = Path(value).expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                raise ValueError(label + "目录无效。") from exc
            if path.exists() and not path.is_dir():
                raise ValueError(label + "目录不是文件夹。")
            config[key] = str(path)
        with import_lock(self.cache):
            path = self.cache / "config.json"
            temporary = self.cache / "config.tmp"
            temporary.write_text(encode(config), encoding="utf-8")
            temporary.replace(path)
        roots, bridge = self.buddy_roots()
        return {"home": str(self.home()),
                "buddy": {"workbuddy": str(roots["workbuddy-desktop"]) if roots else None,
                          "codebuddy": str(roots["codebuddy-cli"]) if roots else None,
                          "bridge": str(bridge) if bridge else None}}

    def request(self, operation, params=None, progress=None):
        params = params or {}
        if not isinstance(params, dict):
            raise ValueError("请求参数必须为对象。")
        if operation == "quota":
            return self.quota(params)
        if operation == "configure":
            return self.configure(params)
        if operation not in ("refresh", "status", "details", "stats", "projects"):
            raise ValueError("不支持的操作。")
        store = Store(self.home(), self.cache)
        buddy = None
        try:
            buddy_warning = None
            try:
                buddy = self.buddy_store()
            except (OSError, sqlite3.Error):
                buddy_warning = "WorkBuddy/CLI 索引暂不可用；Codex 仍可使用。"
            if operation == "refresh":
                result = store.refresh(progress)
                try:
                    buddy_result = buddy.refresh(progress) if buddy else self.buddy_report()
                except (OSError, sqlite3.Error):
                    buddy.close()
                    buddy = None
                    buddy_warning = "WorkBuddy/CLI 更新失败；保留已有索引，Codex 已独立更新。"
                    buddy_result = self.buddy_report()
                if buddy_warning:
                    buddy_result["warnings"].append(buddy_warning)
                return {"import": result, "buddyImport": buddy_result,
                        "status": self.status(store, buddy, buddy_warning)}
            # SQLite read transaction keeps cards, rows and version mutually consistent.
            store.db.execute("BEGIN")
            if buddy is not None:
                try:
                    buddy.db.execute("BEGIN")
                except sqlite3.Error:
                    buddy.close()
                    buddy = None
                    buddy_warning = "WorkBuddy/CLI 查询暂不可用；Codex 仍可使用。"
            status = self.status(store, buddy, buddy_warning)
            if operation == "status":
                return status
            source = self.source(params)
            where, values, since, days = self.filters(params)
            if operation == "projects":
                # 项目视图始终汇总全部来源：问题问的是“这个项目花了多少”，
                # 与当前选中的来源筛选无关。
                return {"rows": self.project_groups(store, buddy, since, days, None),
                        "status": status, "days": days}
            page, size = self.paging(params, 100 if operation == "stats" else 50)
            first = datetime.fromtimestamp(since).date()
            last = first + timedelta(days=days - 1)
            group_where, group_values = "day>=? AND day<=?", [first.isoformat(), last.isoformat()]
            for field, column in (("model", "model"), ("mode", "mode")):
                if params.get(field):
                    group_where += " AND " + column + "=?"
                    group_values.append(params[field])
            include_codex = source in ("codex", "all")
            selected = ([source] if source in BUDDY_SOURCES
                        else (list(BUDDY_SOURCES) if source == "all" else []))
            until = datetime.combine(first + timedelta(days=days), datetime.min.time()).timestamp()
            buddy_rows = (buddy.rows(since, until, selected,
                                     params.get("model") or None) if buddy and selected else [])
            codex_latencies = (self.response_latencies(store, where, values)
                               if include_codex else [])
            codex_card = (self.cards(store, group_where, group_values, where, values,
                                     latencies=codex_latencies)
                          if include_codex else empty_cards())
            buddy_card = BuddyStore.cards(buddy_rows) if selected else empty_cards()
            if selected:
                buddy_card["sourceRecords"] = {s: sum(1 for row in buddy_rows if row["source"] == s)
                                               for s in selected}
            else:
                buddy_card["sourceRecords"] = {}
            codex_card["sourceRecords"] = {"codex": codex_card["records"]}
            codex_speeds = ([row[0] for row in store.db.execute(
                "SELECT speed FROM requests WHERE speed IS NOT NULL AND " + where, values)]
                if include_codex else [])
            buddy_speeds = [row["output"] / row["duration"] for row in buddy_rows
                            if row.get("duration") and row["duration"] > 0
                            and row["output"] > 0]
            buddy_latencies = [row["firstToken"] for row in buddy_rows
                               if row.get("firstToken") is not None]
            cards = self.merge_cards(codex_card, buddy_card, codex_speeds, buddy_speeds,
                                     response_values=codex_latencies + buddy_latencies)
            common = dict(status=status, cards=cards, page=page, pageSize=size, source=source)
            if operation == "details":
                sort, direction = params.get("sort", "time"), params.get("direction", "desc")
                columns = {"time": "r.at", "speed": "r.speed", "cost": "r.cost",
                           "firstToken": "t.first_token"}
                if sort not in columns or direction not in ("asc", "desc"):
                    raise ValueError("排序条件无效。")
                if source == "codex":
                    # Keep the original single-source query paged in SQLite.
                    column = columns[sort]
                    rows = store.db.execute("""SELECT r.*,t.first_token,t.data turn_data FROM requests r
                        LEFT JOIN turns t ON t.key=r.key WHERE """ + where +
                        " ORDER BY " + column + " IS NULL, " + column + " " + direction +
                        ", r.at DESC, r.key ASC LIMIT ? OFFSET ?",
                        values + [size, (page - 1) * size])
                    return dict(common, total=cards["records"],
                                rows=[self.detail(row, store.home) for row in rows])
                codex_rows = []
                if include_codex:
                    column = columns[sort]
                    rows = store.db.execute("""SELECT r.*,t.first_token,t.data turn_data FROM requests r
                        LEFT JOIN turns t ON t.key=r.key WHERE """ + where +
                        " ORDER BY " + column + " IS NULL, " + column + " " + direction +
                        ", r.at DESC, r.key ASC LIMIT ?", values + [page * size])
                    codex_rows = [self.detail(row, store.home) for row in rows]
                combined = self.sort_details(codex_rows + buddy_rows, sort, direction)
                selected_rows = combined[(page - 1) * size:page * size]
                sessions = {}
                return dict(common, total=cards["records"], rows=[
                    row if row.get("sourceId") == "codex" else self.buddy_detail(row, buddy, sessions)
                    for row in selected_rows])
            groups = (self.codex_groups(store, group_where, group_values,
                                        params.get("mergeModes") is True)
                      if include_codex else [])
            groups += self.buddy_groups(buddy_rows) if selected else []
            populated = {g["day"] for g in groups}
            empty_source = "codex" if include_codex and not selected else ""
            for i in range(days):
                day = (first + timedelta(days=i)).isoformat()
                if day not in populated:
                    groups.append(self.empty_group(day, empty_source))
            groups.sort(key=lambda g: (g["day"], SOURCE_ORDER.get(g["source"], 9),
                                       g["model"], MODE_NAMES.get(g["mode"], -1)))
            return dict(common, total=len(groups), rows=groups[(page - 1) * size:page * size],
                        nextPage=page + 1 if page * size < len(groups) else None)
        finally:
            if buddy is not None:
                buddy.close()
            store.close()
